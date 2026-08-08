"""Watch AMC Lincoln Square's Odyssey IMAX 70mm calendar for NEW showtimes
and push a phone alert via ntfy when something appears.

Runs on a GitHub Actions schedule (see .github/workflows/watch.yml).
State persists as state.json, committed back to the repo after each change.
The ntfy topic name comes from the NTFY_TOPIC repo secret.

Parsing note: the drop70mm page is a streamed Next.js/RSC response whose venue
sections arrive in NON-DETERMINISTIC ORDER, so we must not infer venue from
text position. Instead we bracket-match the embedded "sections":[...] JSON
array and read each section's venue.shortName directly.
"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone

PAGE = "https://drop70mm.com/movie/e7b76748-c975-4aaf-ba4e-9c65dee2057a"
# each parallel poller keeps its own state file so concurrent runs never
# collide on git push (STATE_FILE is set per workflow)
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     os.environ.get("STATE_FILE", "state.json"))
TOPIC = os.environ.get("NTFY_TOPIC", "")
ALERT_THEATER = "Lincoln Square"

def notify(title, body, priority="urgent"):
    req = urllib.request.Request(
        f"https://ntfy.sh/{TOPIC}",
        data=body.encode(),
        headers={"Title": title, "Priority": priority, "Tags": "rotating_light,clapper",
                 "Click": PAGE},
        method="POST")
    urllib.request.urlopen(req, timeout=15)

def parse_calendar(raw):
    """Return {venue_shortName: {"dateLabel|timeLabel": showtime_id}}."""
    text = raw.replace('\\"', '"')
    key = '"sections":['
    i = text.find(key)
    if i < 0:
        raise RuntimeError("sections array not found in page")
    start = i + len(key) - 1  # position of the opening [
    depth, end = 0, None
    for j in range(start, min(len(text), start + 2_000_000)):
        c = text[j]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    if end is None:
        raise RuntimeError("sections array unterminated")
    sections = json.loads(text[start:end])
    cal = {}
    for sec in sections:
        name = (sec.get("venue") or {}).get("shortName", "?")
        d = cal.setdefault(name, {})
        for g in sec.get("groups", []):
            dl = g.get("dateLabel", "?")
            for st in g.get("showtimes", []):
                m = re.search(r"showtimes/(\d+)/seats", st.get("ticketUrl") or "")
                if m:
                    d[f"{dl}|{st.get('timeLabel', '?')}"] = m.group(1)
    if len(cal.get(ALERT_THEATER, {})) < 4:
        counts = {k: len(v) for k, v in cal.items()}
        raise RuntimeError(f"parse suspiciously small for {ALERT_THEATER}: {counts}")
    return cal

def fetch_calendar():
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(PAGE, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode(errors="replace")
            return parse_calendar(raw)
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(20)
    raise last_err

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
