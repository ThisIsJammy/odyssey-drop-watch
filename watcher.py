"""Watch AMC Lincoln Square's Odyssey IMAX 70mm calendar for NEW showtimes
and push a phone alert via ntfy when something appears.

Runs on a GitHub Actions schedule (see .github/workflows/watch.yml).
State persists as state.json, committed back to the repo after each change.
The ntfy topic name comes from the NTFY_TOPIC repo secret.
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

PAGE = "https://drop70mm.com/movie/e7b76748-c975-4aaf-ba4e-9c65dee2057a"
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
TOPIC = os.environ.get("NTFY_TOPIC", "")
THEATERS = ["Lincoln Square", "CityWalk", "Metreon"]
ALERT_THEATER = "Lincoln Square"

def notify(title, body, priority="urgent"):
    req = urllib.request.Request(
        f"https://ntfy.sh/{TOPIC}",
        data=body.encode(),
        headers={"Title": title, "Priority": priority, "Tags": "rotating_light,clapper",
                 "Click": PAGE},
        method="POST")
    urllib.request.urlopen(req, timeout=15)

def fetch_calendar():
    """Return {theater: {"dateLabel|timeLabel": showtime_id}} parsed from the page."""
    req = urllib.request.Request(PAGE, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode(errors="replace")
    text = raw.replace('\\"', '"')
    marks = sorted((m.start(), t) for t in THEATERS
                   for m in [next(re.finditer(re.escape(t), text), None)] if m)
    if not marks:
        raise RuntimeError("no theater names found - page layout changed")
    cal = {t: {} for _, t in marks}
    block_re = re.compile(r'"dateLabel":"([^"]+)","showtimes":\[(.*?)\]\}')
    item_re = re.compile(r'"timeLabel":"([^"]+)","ticketUrl":"https://www\.amctheatres\.com/showtimes/(\d+)/seats"')
    for bm in block_re.finditer(text):
        theater = None
        for pos, t in marks:
            if pos < bm.start():
                theater = t
        if theater is None:
            continue
        for tm in item_re.finditer(bm.group(2)):
            cal[theater][f"{bm.group(1)}|{tm.group(1)}"] = tm.group(2)
    if len(cal.get(ALERT_THEATER, {})) < 4:
        raise RuntimeError(f"parse suspiciously small for {ALERT_THEATER}")
    return cal

def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if not TOPIC:
        print(f"[{now}] ERROR: NTFY_TOPIC secret not set", file=sys.stderr)
        sys.exit(1)
    try:
        cal = fetch_calendar()
    except Exception as e:
        # loud failure so silence always means "no news"
        try:
            notify("gh-actions watcher: fetch/parse problem", str(e), "low")
        except Exception:
            pass
        print(f"[{now}] fetch/parse failed: {e}", file=sys.stderr)
        sys.exit(1)
    flat = {f"{t}|{k}": v for t, d in cal.items() for k, v in d.items()}
    old = None
    try:
        with open(STATE) as f:
            old = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    with open(STATE, "w") as f:
        json.dump(flat, f, indent=1, sort_keys=True)
    if old is None:
        print(f"[{now}] baseline seeded: "
              + ", ".join(f"{t}={len(d)}" for t, d in cal.items()) + " showtimes. No alert.")
        return
    new_keys = [k for k in flat if k not in old]
    ls_new = [k for k in new_keys if k.startswith(ALERT_THEATER)]
    if ls_new:
        lines = []
        for k in sorted(ls_new):
            _, date_label, time_label = k.split("|")
            lines.append(f"{date_label} {time_label} -> amctheatres.com/showtimes/{flat[k]}/seats")
        notify(f"LINCOLN SQUARE DROP: {len(ls_new)} new showtime(s)!",
               "\n".join(lines) + "\n\nBUY NOW - 80% sells in the first hour.")
        print(f"[{now}] ALERT sent: {ls_new}")
    other_new = [k for k in new_keys if not k.startswith(ALERT_THEATER)]
    if other_new:
        notify("Other 70mm venue added showtimes", "\n".join(sorted(other_new)), "default")
        print(f"[{now}] info: other venues added {other_new}")
    gone = [k for k in old if k not in flat]
    if gone:
        print(f"[{now}] note: {len(gone)} showtime(s) disappeared: {sorted(gone)[:6]}")
    if not new_keys and not gone:
        print(f"[{now}] no change ({len(flat)} showtimes tracked)")

if __name__ == "__main__":
    main()
