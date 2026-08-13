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
import hashlib
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
# showtimes whose ticket availability we track (sold-out -> on-sale flips).
# NOTE: this status comes from the tracker, not AMC, and has been observed to
# lag reality - alerts say "verify on AMC" for that reason.
SEAT_WATCH = [d.strip() for d in os.environ.get("SEAT_WATCH", "Sep 16").split(",") if d.strip()]
STATUS = {}
# a real drop repeats the alert once a minute for this long, so it cannot be
# slept through; NAG_MINUTES=0 disables it
NAG_MINUTES = int(os.environ.get("NAG_MINUTES", "15"))
NAG_INTERVAL = int(os.environ.get("NAG_INTERVAL", "60"))

class AlertUndelivered(Exception):
    """Raised when an alert could not be delivered; state must not advance."""


def notify_retry(title, body, priority="urgent", tries=3):
    """Deliver or raise AlertUndelivered. Used for alerts that must not be lost:
    an ntfy blip during a real drop used to be swallowed while state advanced,
    losing the drop permanently."""
    last = None
    for i in range(tries):
        try:
            notify(title, body, priority)
            return
        except Exception as e:
            last = e
            print(f"  notify attempt {i+1}/{tries} failed: {e}")
            if i < tries - 1:
                time.sleep(5)
    raise AlertUndelivered(str(last))


def notify(title, body, priority="urgent"):
    req = urllib.request.Request(
        f"https://ntfy.sh/{TOPIC}",
        data=body.encode(),
        headers={"Title": title.encode("ascii", "ignore").decode(),
                 "Priority": priority, "Tags": "rotating_light,clapper",
                 "Click": PAGE},
        method="POST")
    urllib.request.urlopen(req, timeout=15)


def _nag_already_running(sig):
    """True if another poller already started the repeat-alarm for this drop.
    Three pollers watch the same calendar; without this each would start its
    own 15-minute alarm (45 notifications for one drop)."""
    try:
        url = f"https://ntfy.sh/{TOPIC}/json?poll=1&since={NAG_MINUTES + 5}m"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            for line in r.read().decode().splitlines():
                if line.strip() and sig in line:
                    return True
    except Exception as e:
        print(f"nag dedupe check failed ({e}) - alerting anyway")
    return False


def alarm(title, body):
    """Push now, then repeat every NAG_INTERVAL for NAG_MINUTES.

    The repeats run INLINE (blocking), not on a background thread: each poll is
    a short-lived process and a daemon thread dies the moment it exits, which
    silently swallowed every repeat in testing. Blocking costs this poller a
    few polls; the other two pollers keep watching in the meantime, and the
    dedupe check above means only one poller ever blocks.""" 
    sig = "[drop:" + hashlib.sha1(body.encode()).hexdigest()[:8] + "]"
    if _nag_already_running(sig):
        print(f"repeat-alarm already running elsewhere for {sig} - single push only")
        notify_retry(title, f"{body}\n{sig}")
        return
    reps = max(1, (NAG_MINUTES * 60) // NAG_INTERVAL) if NAG_MINUTES else 1
    notify_retry(title, f"{body}\n{sig} alert 1/{reps}")

    for n in range(2, reps + 1):
        time.sleep(NAG_INTERVAL)
        try:
            notify(f"STILL OPEN? {title}",
                   f"{body}\n{sig} alert {n}/{reps} - repeating for "
                   f"{NAG_MINUTES} min so you cannot miss it")
        except Exception as e:
            print(f"repeat alert {n} failed: {e}")
            return
    print(f"repeat-alarm finished: {reps} alerts over {NAG_MINUTES} min")

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
    STATUS.clear()
    cal = {}
    for sec in sections:
        name = (sec.get("venue") or {}).get("shortName", "?")
        d = cal.setdefault(name, {})
        for g in sec.get("groups", []):
            dl = g.get("dateLabel", "?")
            for st in g.get("showtimes", []):
                m = re.search(r"showtimes/(\d+)/seats", st.get("ticketUrl") or "")
                if m:
                    key = f"{dl}|{st.get('timeLabel', '?')}"
                    d[key] = m.group(1)
                    STATUS[f"{name}|{key}"] = st.get("availabilityStatus")
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

def write_json(path, payload):
    """Atomic write: a killed runner must not leave half-written JSON that
    silently re-baselines (and thereby swallows a real drop)."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main():
    try:
        _main()
    except AlertUndelivered as e:
        print(f"ALERT DELIVERY FAILED ({e}) - state left unchanged, will retry "
              f"next poll", file=sys.stderr)
        sys.exit(1)


def _main():
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
    # availability snapshot for the showtimes we're seat-watching
    seat_now = {k: STATUS.get(k) for k in flat
                if k.startswith(ALERT_THEATER) and any(d in k for d in SEAT_WATCH)}
    old = None
    try:
        with open(STATE) as f:
            old = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    # state is written at the end, only once alerts have been delivered
    seat_path = STATE.replace(".json", "_seats.json")
    seat_prev = {}
    if os.path.exists(seat_path):
        try:
            seat_prev = json.load(open(seat_path))
        except json.JSONDecodeError:
            pass

    def persist():
        write_json(STATE, flat)
        write_json(seat_path, seat_now)

    if seat_prev:
        opened = [k for k, v in seat_now.items()
                  if v == "tickets" and seat_prev.get(k) not in (None, "tickets")]
        if opened:
            lines = [f"{k.split('|', 1)[1]} -> amctheatres.com/showtimes/{flat[k]}/seats"
                     for k in sorted(opened)]
            dates = sorted({k.split("|")[1] for k in opened})
            alarm(f"SEATS MAY HAVE OPENED: {', '.join(dates)}"
                  f" ({len(opened)} showtime(s))",
                  "\n".join(lines) +
                  "\n\nTracker flipped sold-out -> on sale. VERIFY ON AMC - this "
                  "status has lagged reality before.")
            print(f"seat-availability alert: {opened}")
    if old is None:
        persist()
        print(f"[{now}] baseline seeded: "
              + ", ".join(f"{t}={len(d)}" for t, d in cal.items()) + " showtimes. No alert.")
        return
    new_keys = [k for k in flat if k not in old]
    vanished = [k for k in old if k not in flat]
    # A genuine release ADDS showtimes; almost nothing disappears at the same
    # moment. Simultaneous mass appearance AND disappearance is the fingerprint
    # of an upstream relabel (e.g. "Sat, Sep 12" -> "Saturday Sep 12"), which
    # would otherwise fire an urgent alarm for ~130 imaginary showtimes.
    if len(new_keys) > 6 and len(vanished) > 6:
        msg = (f"{len(new_keys)} keys appeared and {len(vanished)} vanished in "
               f"one poll - that is a relabel/degraded page, not a drop. "
               f"Not alerting; parser needs checking.")
        print(msg, file=sys.stderr)
        try:
            notify("AMC watcher needs attention", msg, "default")
        except Exception:
            pass
        return          # deliberately NOT persisting: keep the old baseline
    ls_new = [k for k in new_keys if k.startswith(ALERT_THEATER)]
    if ls_new:
        lines = []
        for k in sorted(ls_new):
            _, date_label, time_label = k.split("|")
            lines.append(f"{date_label} {time_label} -> amctheatres.com/showtimes/{flat[k]}/seats")
        alarm(f"LINCOLN SQUARE DROP: {len(ls_new)} new showtime(s)!",
              "\n".join(lines) + "\n\nBUY NOW - 80% sells in the first hour.")
        print(f"[{now}] ALERT sent: {ls_new}")
    other_new = [k for k in new_keys if not k.startswith(ALERT_THEATER)]
    if other_new:
        notify("Other 70mm venue added showtimes", "\n".join(sorted(other_new)), "default")
        print(f"[{now}] info: other venues added {other_new}")
    persist()
    gone = [k for k in old if k not in flat]
    if gone:
        print(f"[{now}] note: {len(gone)} showtime(s) disappeared: {sorted(gone)[:6]}")
    if not new_keys and not gone:
        print(f"[{now}] no change ({len(flat)} showtimes tracked)")

if __name__ == "__main__":
    main()
