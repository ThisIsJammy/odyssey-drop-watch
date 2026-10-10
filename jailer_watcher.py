#!/usr/bin/env python3
"""Alert the moment Jailer 2 bookings open in Pune.

BookMyShow itself is unreadable (Cloudflare 403 on every page and on its
mobile API), so this watches the two sources that ARE scriptable and that
carry the same release:

  A. District / Zomato  - the Pune page for MV187766. A film that is bookable
     has a non-empty `showDates` array and `arrangedSessions` blocks; one that
     is not yet on sale has neither. Verified 2026-10-10 against a live
     control (The Vvaan, playing in Pune: 6 showDates, 2 session blocks;
     Jailer 2: zero of both).
  B. PVR/INOX first-party API - the Pune cinema listing. A loaded film shows
     up in some cinema's `movieRes`.

Both channels carry a CANARY in the same request shape, because the real risk
is not "no seats" but "we stopped being able to read the page and reported
closed forever". If a canary fails, this raises instead of saying "not open".

Never trust page text: the literal string "Book tickets" is present on BOTH
the bookable and the not-yet-bookable District page (same decoy as Zomato's
"Currently closed for online ordering"). Only structure counts.

Env: NTFY_TOPIC (required), POLL_SECONDS, MAX_POLLS, CUTOFF_IST, STATE_FILE
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, os.environ.get("STATE_FILE", "jailer_state.json"))
HISTORY = os.path.join(HERE, "jailer_history.jsonl")
TOPIC = os.environ.get("NTFY_TOPIC", "")
CUTOFF_IST = os.environ.get("CUTOFF_IST", "2026-10-17 23:59")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

MOVIE_ID = "MV187766"
DISTRICT_URL = ("https://www.district.in/movies/"
                "jailer-2-movie-tickets-in-pune-MV187766")
# control: must stay bookable in Pune for the parser to be trusted
DISTRICT_CANARY = ("https://www.district.in/movies/"
                   "the-vvaan-movie-tickets-in-pune-MV183084")
BMS_URL = "https://in.bookmyshow.com/movies/pune/jailer-2/ET00518674"

PVR_API = "https://api3.pvrcinemas.com/api/v1/booking/content/cinemas"
PVR_HEADERS = {
    "User-Agent": UA, "Content-Type": "application/json",
    "Authorization": "Bearer ", "chain": "PVR", "city": "Pune",
    "country": "INDIA", "appVersion": "1.0", "platform": "WEBSITE",
    "Origin": "https://www.pvrcinemas.com",
    "Referer": "https://www.pvrcinemas.com/",
}
TITLE_RE = re.compile(r"JAILER", re.I)
# Cinemas the user actually wants to book (asked 2026-10-10). Matched as a
# case-insensitive substring of PVR's cinema name. PVR Market City Pune is
# theatreId 375 = the "PVR Phoenix Market City" BMS link. MovieMax Gold
# Mariplex (BMS venue MKML) is NOT trackable: MovieMax has no booking site of
# its own (moviemax.co.in is an empty shell) and sells only through
# in.bookmyshow.com, which is Cloudflare-blocked from every vantage point.
PRIORITY_CINEMAS = ("Market City", "Mariplex")


def now_ist():
    return datetime.now(IST)


def log(msg):
    print(f"{now_ist():%H:%M:%S} {msg}", flush=True)


class Undelivered(Exception):
    pass


def notify(title, body, priority="urgent", tags="movie,bell", click=BMS_URL):
    if not TOPIC:
        raise RuntimeError("NTFY_TOPIC is unset - refusing to run mute")
    # ntfy turns >4KB bodies into an unreadable attachment
    body = body[:3500]
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{TOPIC}", data=body.encode("utf-8"),
                headers={"Title": title, "Priority": priority, "Tags": tags,
                         "Click": click})
            with urllib.request.urlopen(req, timeout=20) as r:
                if r.status < 300:
                    return
                last = f"HTTP {r.status}"
        except Exception as e:                                # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        time.sleep(2 + attempt * 3)
    raise Undelivered(last or "unknown")


def get(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.geturl(), r.read().decode(errors="replace")


def district_signal(url, expect_id=None):
    """(n_dates, n_session_blocks, dates) for a District movie page."""
    final, raw = get(url)
    if "/movies/" not in final:
        raise RuntimeError(f"redirected away -> {final}")
    if len(raw) < 60_000:
        raise RuntimeError(f"page suspiciously small ({len(raw)}b)")
    # Check the RESOLVED URL, not the body: every Pune movie page carries
    # MV187766 in its "upcoming" carousel, so a body match cannot tell pages
    # apart and a redirect would have us reading another film's showtimes.
    if expect_id and expect_id not in final:
        raise RuntimeError(
            f"{expect_id} not in resolved url {final} - wrong page/redirect")
    flat = raw.replace('\\"', '"')
    dates = []
    for m in re.finditer(r'"showDates"\s*:\s*\[(.*?)\]', flat, re.S):
        dates += re.findall(r'"(\d{4}-\d{2}-\d{2})"', m.group(1))
    blocks = len(re.findall(r'"arrangedSessions"\s*:\s*\[\s*\{', flat))
    # District lists every Pune chain, including ones with no site of their own
    # (MovieMax Gold Mariplex sells only via BMS, which is unreadable) - so the
    # venue names here are the only way to say WHERE it opened.
    names = set()
    for pat in (r'"venueName"\s*:\s*"([^"]{3,70})"',
                r'"cinemaName"\s*:\s*"([^"]{3,70})"',
                r'"name"\s*:\s*"([^"]{3,70})"'):
        names |= set(re.findall(pat, flat))
    cinemas = sorted(n for n in names if re.search(
        r'pvr|inox|cinepolis|moviemax|city ?pride|miraj|rajhans|funsquare'
        r'|carnival|mariplex|multiplex|cinema', n, re.I))
    return len(set(dates)), blocks, sorted(set(dates)), cinemas


def district_check():
    """(is_open, detail). Raises if the canary says we cannot read District."""
    nd, nb, dates, cinemas = district_signal(DISTRICT_URL, expect_id=MOVIE_ID)
    cd, cb, _, _ = district_signal(DISTRICT_CANARY)
    if cd == 0 and cb == 0:
        raise RuntimeError(
            "District canary has no showDates either - parser or layout "
            "broke; refusing to report 'not open'")
    if nd or nb:
        def is_mine(c):
            return any(p.lower() in c.lower() for p in PRIORITY_CINEMAS)
        # the user's own cinemas FIRST - the list is capped for ntfy, and
        # alphabetical order would push them past the cut
        ordered = sorted(cinemas, key=lambda c: (not is_mine(c), c.lower()))
        named = [("* " + c if is_mine(c) else c) for c in ordered]
        where = ("; ".join(named[:12]) if named else "cinemas not listed yet")
        if len(named) > 12:
            where += f" (+{len(named) - 12} more)"
        return True, f"{nd} show dates {dates[:6]} | {where}"
    return False, f"no showDates (canary healthy: {cd} dates)"


def pvr_check():
    """(is_open, detail). Raises if the Pune listing looks unreadable."""
    body = json.dumps({"city": "Pune", "lat": "18.5204",
                       "lng": "73.8567", "text": ""}).encode()
    req = urllib.request.Request(PVR_API, data=body, headers=PVR_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode(errors="replace"))
    if d.get("result") != "success":
        raise RuntimeError(f"PVR result={d.get('result')} msg={d.get('msg')}")
    cinemas = ((d.get("output") or {}).get("c")) or []
    films = {}
    for c in cinemas:
        for mr in (c.get("movieRes") or []):
            nm = mr.get("filmName") or ""
            if nm:
                films.setdefault(nm, set()).add(c.get("name", "?"))
    # canary: a real Pune listing always has many cinemas and many films
    if len(cinemas) < 4 or len(films) < 8:
        raise RuntimeError(
            f"PVR Pune listing looks truncated ({len(cinemas)} cinemas, "
            f"{len(films)} films) - refusing to report 'not open'")
    hits = {n: v for n, v in films.items() if TITLE_RE.search(n)}
    if hits:
        parts = []
        for n, v in hits.items():
            # star the cinemas the user wants, so the alert says whether THEIR
            # theatre has it rather than just how many did
            named = sorted(
                ("* " + c if any(p.lower() in c.lower()
                                 for p in PRIORITY_CINEMAS) else c)
                for c in v)
            parts.append(f"{n}: " + ", ".join(named))
        return True, " | ".join(parts)
    return False, f"not in {len(films)} films across {len(cinemas)} cinemas"


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:                                         # noqa: BLE001
        return {}


def save_state(s):
    with open(STATE, "w") as f:
        json.dump(s, f, indent=1, sort_keys=True)


def append_history(rec):
    with open(HISTORY, "a") as f:
        f.write(json.dumps(rec) + "\n")


def problem(msg, state):
    """Low-priority, rate-limited: a broken watcher must not be silent."""
    log(f"PROBLEM: {msg}")
    last = state.get("last_problem_alert")
    if last:
        try:
            if now_ist() - datetime.fromisoformat(last) < timedelta(hours=3):
                return
        except Exception:                                     # noqa: BLE001
            pass
    try:
        notify("Jailer 2 watcher problem", msg[:500],
               priority="low", tags="warning")
        state["last_problem_alert"] = now_ist().isoformat()
        save_state(state)
    except Undelivered as e:
        log(f"  problem-alert undelivered: {e}")


def past_cutoff():
    try:
        c = datetime.strptime(CUTOFF_IST, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    except Exception:                                         # noqa: BLE001
        return False
    return now_ist() > c


def one_pass():
    state = load_state()
    results, errors = {}, []
    for name, fn in (("district", district_check), ("pvr", pvr_check)):
        try:
            results[name] = fn()
        except Exception as e:                                # noqa: BLE001
            errors.append(f"{name}: {type(e).__name__}: {e}")
    if not results:
        problem("both channels failed -> " + " | ".join(errors), state)
        return
    for e in errors:
        log(f"  (channel down) {e}")

    opened = {k: v[1] for k, v in results.items() if v[0]}
    for k, (is_open, detail) in results.items():
        log(f"  {k}: {'OPEN' if is_open else 'not open'} - {detail}")

    already = set(state.get("alerted_channels") or [])
    fresh = [k for k in opened if k not in already]
    if fresh:
        lines = [f"{k.upper()}: {opened[k]}" for k in fresh]
        body = ("Jailer 2 BOOKINGS ARE OPEN in Pune\n\n"
                + "\n".join(lines)
                + f"\n\nBookMyShow: {BMS_URL}"
                + f"\nDistrict:   {DISTRICT_URL}"
                + "\n\n(BMS cannot be read by script - check it too, it often "
                  "lists first.)")
        try:
            notify("Jailer 2 BOOKINGS OPEN - Pune", body)
        except Undelivered as e:
            # do NOT persist: leaving state untouched makes the next poll retry
            log(f"ALERT UNDELIVERED ({e}) - state untouched, will retry")
            return
        state["alerted_channels"] = sorted(already | set(fresh))
        append_history({"ts": now_ist().isoformat(), "opened": fresh,
                        "detail": {k: opened[k] for k in fresh}})
        log(f"ALERTED via {fresh}")

    state["checked"] = now_ist().isoformat()
    state["status"] = {k: {"open": v[0], "detail": v[1]}
                       for k, v in results.items()}
    save_state(state)


def main():
    polls = int(os.environ.get("MAX_POLLS", "1"))
    gap = int(os.environ.get("POLL_SECONDS", "180"))
    for i in range(polls):
        if past_cutoff():
            log(f"past cutoff {CUTOFF_IST} - stopping")
            return
        log(f"--- poll {i + 1}/{polls} ---")
        try:
            one_pass()
        except Exception as e:                                # noqa: BLE001
            log(f"unhandled: {type(e).__name__}: {e}")
        if i < polls - 1:
            time.sleep(gap)


if __name__ == "__main__":
    main()
