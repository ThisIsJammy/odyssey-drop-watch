#!/usr/bin/env python3
"""
BookMyShow seat monitor (white-label cinema sites, e.g. Prasads Hyderabad).

Sibling of monitor.py (which watches District/Zomato). Same idea, different
backend: BMS's white-label cinema portal exposes a clean JSON API that returns
per-show seat counts without any bot wall — unlike in.bookmyshow.com itself,
which 403s every scripted request.

  Discovery path (kept here so it's reproducible):
    prasadz.com embeds  cinemas.bookmyshow.com/iframe?domain=www.prasadz.com
    that Next.js app calls  /api/getDEData  (a proxy to BMS's DE backend)
    site config gives companyCode=PRSD, subregionCode=HYD; venue PRHN
    cmd=IBVGETEVENTBYSUBREGION -> movie EventCodes
    cmd=IBVGETSHOWTIMESBYEVENT -> the show list (session ids, times, screens)
    cmd=DEGETSHOWINFO&sc=<sessionId> -> the REAL numbers per price category
    POST /api/doTrans {cmd:GETSEATLAYOUT, venueCode, sessionId} -> full seat map

  Seat-map string format (BMS's own parser, mirrored in parse_seatmap below):
    "<areas>||<rows>";  rows are "|"-separated, each "rowId:ROWLETTER:tok:tok:..."
    token "A2011+47" = areaId A, statusDigit 2, internal id, display seat 47
    status: 0 gangway, 1 AVAILABLE, 2 booked/held, 3 other category,
            4 best-seat (also available), 7 handicap, 8 companion, 9 distancing

  TRAP (verified 2026-08-08): the show-list field `SeatsAvailable` is the
  screen's TOTAL CAPACITY, not availability - it read 629 for a show that had
  only 69 seats left. Always use DEGETSHOWINFO's AvailableSeats/TotalSeats,
  summed across price categories.

Alerts (ntfy, same pattern as the AMC watchers):
  - seats INCREASE on a watched show (a cancellation/release) -> urgent push
  - a show goes from sold out to available            -> urgent push
  - a NEW showtime appears for a watched date         -> urgent push
  - seats fall below a threshold (selling out fast)   -> default push, once
Silence means no news: fetch/parse failures push a low-priority diagnostic.

Run:    python3 bms_monitor.py            # one poll (cron/Actions friendly)
        python3 bms_monitor.py --loop     # continuous, INTERVAL_SEC apart
        python3 bms_monitor.py --test     # send a test push
State:  bms_state.json next to this script.
Config: bms_config.json next to this script.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "bms_config.json")))
STATE_PATH = os.path.join(HERE, "bms_state.json")
LOG_PATH = os.path.join(HERE, "bms_monitor.log") if os.access(HERE, os.W_OK) else "/tmp/bms_monitor.log"

BASE = "https://cinemas.bookmyshow.com/api/getDEData"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
DOMAIN = CFG["domain"]
CC, SR, VC = CFG["company_code"], CFG["subregion_code"], CFG["venue_code"]
TOPIC = os.environ.get("NTFY_TOPIC") or CFG["ntfy_topic"]
DATES = CFG["dates"]            # [] or ["auto"] => discover every listed date
AUTO_DATES = (not DATES) or DATES == ["auto"]
MOVIE_MATCH = CFG["movie_match"].lower()
SCREEN_MATCH = (CFG.get("screen_match") or "").lower()
TIME_MATCH = CFG.get("showtime_match") or ""
LOW_SEATS = CFG.get("low_seat_alert", 0)
WATCH_ROWS = [r.upper() for r in CFG.get("watch_rows", [])]
ROWS_ONLY = CFG.get("alert_on_watched_rows_only", False)
INTERVAL_SEC = CFG.get("interval_sec", 120)
REF = f"https://cinemas.bookmyshow.com/buytickets/{VC}/{DATES[0]}?domain={DOMAIN}"


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def push(title, body, priority="urgent", tags="ticket,bell,star"):
    try:
        title = title.encode("ascii", "ignore").decode().strip() or "Seat alert"
        req = urllib.request.Request(
            f"https://ntfy.sh/{TOPIC}", data=body.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags,
                     "Click": f"https://in.bookmyshow.com/cinemas/hyderabad/"
                              f"prasads-multiplex-hyderabad/buytickets/{VC}/{DATES[0]}"},
            method="POST")
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        log(f"push failed: {e}")


def api(params):
    q = dict(params)
    q.update({"et": "MT", "f": "json", "domain": DOMAIN})
    url = f"{BASE}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": REF, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def events():
    """EventCodes whose title matches the configured movie."""
    d = api({"cmd": "IBVGETEVENTBYSUBREGION", "cc": CC, "sr": SR})
    out = []
    for e in d["BookMyShow"]["arrEvents"]:
        if MOVIE_MATCH in (e.get("EventTitle") or "").lower():
            out.append((e["EventCode"], e["EventTitle"]))
    return out


def showinfo(session_id):
    """Real availability for a session: (available, total, categories)."""
    for attempt in range(3):
        try:
            d = api({"cmd": "DEGETSHOWINFO", "vc": VC, "sc": session_id})
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(5)
    rows = d["BookMyShow"].get("arrShowInfo") or []
    avail = sum(int(r.get("AvailableSeats") or 0) for r in rows)
    total = sum(int(r.get("TotalSeats") or 0) for r in rows)
    cats = ", ".join(f"{r.get('CategoryName')} Rs{r.get('Price')}={r.get('AvailableSeats')}"
                     for r in rows)
    return avail, total, cats


AVAILABLE_STATUS = (1, 4)  # 1 = available, 4 = available + "best seat"


def parse_seatmap(str_data):
    """Seat-map string -> {ROW_LETTER: [display seat numbers currently free]}."""
    rows = {}
    if "||" not in str_data:
        raise RuntimeError("seat map missing '||' separator - format changed")
    for line in str_data.split("||", 1)[1].split("|"):
        if not line.strip():
            continue
        parts = line.split(":")
        if len(parts) < 3:
            continue
        letter = parts[1].upper()
        free = []
        for tok in parts[2:]:
            bits = tok.split("+")
            code = bits[0]
            if len(code) < 2 or not code[1].isdigit():
                continue
            status = int(code[1])
            if status == 0:
                continue
            if status in AVAILABLE_STATUS:
                disp = bits[1] if len(bits) > 1 and bits[1] not in ("0", "00") else code[2:]
                free.append(disp)
        rows[letter] = free
    if not rows:
        raise RuntimeError("no rows parsed from seat map - format changed")
    return rows


def seatmap(session_id):
    """Live seat map for a session; {} if the layout call fails."""
    body = json.dumps({"cmd": "GETSEATLAYOUT", "venueCode": VC,
                       "sessionId": str(session_id)}).encode()
    req = urllib.request.Request(
        f"https://cinemas.bookmyshow.com/api/doTrans?domain={DOMAIN}", data=body,
        headers={"User-Agent": UA, "Referer": REF, "Accept": "application/json",
                 "Content-Type": "application/json"}, method="POST")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                d = json.load(r)
            bms = d.get("BookMyShow", {})
            if str(bms.get("blnSuccess")).lower() != "true" or not bms.get("strData"):
                raise RuntimeError(f"layout call unsuccessful: {bms.get('strException')!r}")
            return parse_seatmap(bms["strData"])
        except Exception as e:
            if attempt == 2:
                log(f"seatmap failed for session {session_id}: {e}")
                return {}
            time.sleep(5)


def dates_for(event_code):
    """Every date BMS currently lists for this movie (so newly opened dates
    are picked up automatically - not watching for these was how the Aug 10-16
    release slipped through silently)."""
    if not AUTO_DATES:
        return DATES
    d = api({"cmd": "IBVGETSHOWDATESBYEVENT", "cc": CC, "sr": SR, "ec": event_code})
    return sorted({x.get("ShowDateCode") for x in
                   (d["BookMyShow"].get("arrShowDates") or []) if x.get("ShowDateCode")})


def shows():
    """All matching shows -> {key: record}. Retries transient failures."""
    found = {}
    for ec, title in events():
        for dc in dates_for(ec):
            for attempt in range(3):
                try:
                    d = api({"cmd": "IBVGETSHOWTIMESBYEVENT", "cc": CC, "sr": SR,
                             "ec": ec, "dc": dc})
                    break
                except Exception as e:
                    if attempt == 2:
                        raise
                    time.sleep(5)
            for s in d["BookMyShow"].get("arrShowTimes", []) or []:
                if s.get("VenueCode") != VC:
                    continue
                if SCREEN_MATCH and SCREEN_MATCH not in (s.get("ScreenName") or "").lower():
                    continue
                if TIME_MATCH and TIME_MATCH not in (s.get("ShowTimeDisplay") or ""):
                    continue
                key = f"{s['ShowDateCode']}|{s.get('ShowTimeDisplay')}|{s.get('ScreenName')}|{ec}"
                # NOT s['SeatsAvailable'] - that is total capacity (see header TRAP)
                avail, total, cats = showinfo(s.get("SessionId"))
                rows = seatmap(s.get("SessionId")) if (WATCH_ROWS or avail) else {}
                watched = {r: rows[r] for r in WATCH_ROWS if rows.get(r)}
                found[key] = {
                    "title": title,
                    "date": s["ShowDateCode"],
                    "time": s.get("ShowTimeDisplay"),
                    "screen": s.get("ScreenName"),
                    "session": s.get("SessionId"),
                    "seats": avail,
                    "capacity": total,
                    "categories": cats,
                    "soldout": s.get("SoldOut") == "1" or avail == 0,
                    "price": f"{s.get('MinPrice')}-{s.get('MaxPrice')}",
                    "cutoff": s.get("CutOffDateTime"),
                    "rows": {r: sorted(v) for r, v in rows.items() if v},
                    "watched_rows": {r: sorted(v) for r, v in watched.items()},
                    "watched_count": sum(len(v) for v in watched.values()),
                }
    if not found:
        raise RuntimeError(f"no shows matched movie={MOVIE_MATCH!r} screen={SCREEN_MATCH!r} "
                           f"time={TIME_MATCH!r} dates={DATES} - filters or listing changed")
    return found


def describe(r):
    return (f"{r['date']} {r['time']} {r['screen']} ({r['title']}) Rs{r['price']} "
            f"[{r['seats']}/{r.get('capacity', '?')} free]")


def poll():
    try:
        cur = shows()
    except Exception as e:
        log(f"fetch/parse failed: {e}")
        push("BMS watcher: problem", str(e), "low", "warning")
        return
    old = {}
    if os.path.exists(STATE_PATH):
        try:
            old = json.load(open(STATE_PATH))
        except json.JSONDecodeError:
            pass
    alerts = []
    for k, r in cur.items():
        prev = old.get(k)
        if prev is None:
            if old:  # not the first-ever run -> genuinely new date/showtime
                new_date = r["date"] not in {v.get("date") for v in old.values()}
                w = r.get("watched_rows", {})
                rowbit = ("\n" + "; ".join(f"ROW {k}: {', '.join(v[:12])}" for k, v in w.items())
                          if w else "\n(watched rows all booked)")
                alerts.append(("urgent",
                               f"{'*** NEW DATE ON SALE ***' if new_date else 'NEW SHOWTIME:'} "
                               f"{describe(r)}{rowbit}"))
            continue
        # --- watched rows: the seats you actually want ---
        if WATCH_ROWS:
            prev_rows = prev.get("watched_rows", {})
            gained = []
            for row in WATCH_ROWS:
                new_seats = [s for s in r["watched_rows"].get(row, [])
                             if s not in prev_rows.get(row, [])]
                if new_seats:
                    gained.append(f"ROW {row}: {', '.join(new_seats)}")
            if gained:
                alerts.append(("urgent", f"*** ROW {'/'.join(WATCH_ROWS)} SEAT OPEN *** "
                                         f"{describe(r)}\n" + "\n".join(gained) +
                                         "\nGRAB IT NOW - these rows are otherwise sold out."))
            if ROWS_ONLY:
                continue  # watched rows are the only thing worth a push
        if prev.get("soldout") and not r["soldout"]:
            alerts.append(("urgent", f"BACK ON SALE: {describe(r)} - {r['seats']} seats"))
        elif r["seats"] > prev.get("seats", 0):
            alerts.append(("urgent", f"SEATS RELEASED: {describe(r)} "
                                     f"{prev.get('seats')} -> {r['seats']} (+{r['seats']-prev.get('seats',0)})"))
        elif LOW_SEATS and prev.get("seats", 0) > LOW_SEATS >= r["seats"] and r["seats"] > 0:
            alerts.append(("default", f"SELLING OUT: {describe(r)} down to {r['seats']} seats - book now"))
        elif not prev.get("soldout") and r["soldout"]:
            log(f"note: SOLD OUT now - {describe(r)}")
    with open(STATE_PATH, "w") as f:
        json.dump(cur, f, indent=1, sort_keys=True)
    if not old:
        for r in sorted(cur.values(), key=lambda x: (x["date"], x["time"])):
            rowinfo = ""
            if WATCH_ROWS:
                w = r.get("watched_rows", {})
                rowinfo = (" | WATCHED " + "/".join(WATCH_ROWS) + ": " +
                           ("; ".join(f"{k}={','.join(v)}" for k, v in w.items()) if w else "all booked"))
            log(f"baseline: {describe(r)}{rowinfo}")
        return
    if alerts:
        urgent = [m for p, m in alerts if p == "urgent"]
        normal = [m for p, m in alerts if p != "urgent"]
        if urgent:
            push(f"{CFG['movie_match']} @ {CFG.get('venue_name', VC)}", "\n".join(urgent), "urgent")
        if normal:
            push(f"{CFG['movie_match']} selling out", "\n".join(normal), "default", "hourglass")
        for _, m in alerts:
            log("ALERT " + m)
    else:
        log("no change (" + ", ".join(
            f"{r['time']}:{r['seats']}free"
            + (f",watched={r.get('watched_count', 0)}" if WATCH_ROWS else "")
            for r in sorted(cur.values(), key=lambda x: (x["date"], x["time"]))) + ")")


if __name__ == "__main__":
    if "--test" in sys.argv:
        push("Test: BMS watcher is live", "If you can read this, pushes work.", "default")
        log(f"test push sent to ntfy.sh/{TOPIC}")
    elif "--loop" in sys.argv:
        log(f"START loop: {CFG['movie_match']} @ {VC} dates={DATES} every {INTERVAL_SEC}s")
        while True:
            poll()
            time.sleep(INTERVAL_SEC)
    else:
        poll()
