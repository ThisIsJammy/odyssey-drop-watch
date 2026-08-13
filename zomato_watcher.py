"""Watch a Zomato restaurant's online-delivery status and alert the moment it
opens.

Signal: the order page server-renders window.__PRELOADED_STATE__, and
  pages.restaurant.<resId>.orderDetails.isServiceable
is the delivery switch. Validated 2026-08-12 against a control restaurant that
was open at the time:
  Burger King Koregaon Park : isServiceable=False, deliveryTime=""
  Cafe Goodluck (open)      : isServiceable=True,  deliveryTime="25 min"

Alerts (ntfy):
  closed -> OPEN : urgent, repeated once a minute for NAG_MINUTES so it can't
                   be missed while the window is short
  OPEN -> closed : single low-priority note, so the history shows the window
Every transition is appended to zomato_history.jsonl, which builds the pattern
of when this restaurant actually opens.

Env: ZOMATO_URL, NTFY_TOPIC, NAG_MINUTES, NAG_INTERVAL, STATE_FILE
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

URL = os.environ.get(
    "ZOMATO_URL",
    "https://www.zomato.com/pune/burger-king-koregaon-park/order")
# A control restaurant in the SAME area/pincode as the target. If the canary
# ever reads serviceable while the target does not, our vantage point can
# clearly see Koregaon Park and the target really is closed for online
# ordering - which is the doubt this whole watcher would otherwise carry.
CANARY_URL = os.environ.get(
    "CANARY_URL",
    "https://www.zomato.com/pune/german-bakery-koregaon-park/order")
TOPIC = os.environ.get("NTFY_TOPIC", "")
# Only check while the outlet can plausibly be serving. User's observed hours
# are 11:30-22:30 IST; a small buffer either side covers early/late openings
# without polling all night.
WINDOW_START = os.environ.get("WINDOW_START", "11:15")   # IST, inclusive
WINDOW_END = os.environ.get("WINDOW_END", "22:45")       # IST, exclusive
NAG_MINUTES = int(os.environ.get("NAG_MINUTES", "15"))
NAG_INTERVAL = int(os.environ.get("NAG_INTERVAL", "60"))
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, os.environ.get("STATE_FILE", "zomato_state.json"))
HISTORY = os.path.join(HERE, "zomato_history.jsonl")
IST = timezone(timedelta(hours=5, minutes=30))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def now_ist():
    return datetime.now(IST)


def in_window(t=None):
    """True if IST time-of-day is inside the monitoring window."""
    t = t or now_ist()
    def mins(hhmm):
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    now_m = t.hour * 60 + t.minute
    a, b = mins(WINDOW_START), mins(WINDOW_END)
    return a <= now_m < b if a <= b else (now_m >= a or now_m < b)


def notify(title, body, priority="urgent", tags="hamburger,bell"):
    if not TOPIC:
        print("NTFY_TOPIC unset - would have sent:", title)
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{TOPIC}", data=body.encode("utf-8"),
        headers={"Title": title.encode("ascii", "ignore").decode(),
                 "Priority": priority, "Tags": tags, "Click": URL},
        method="POST")
    urllib.request.urlopen(req, timeout=15)


def _alarm_running(sig):
    try:
        u = f"https://ntfy.sh/{TOPIC}/json?poll=1&since={NAG_MINUTES + 5}m"
        with urllib.request.urlopen(
                urllib.request.Request(u, headers={"User-Agent": UA}), timeout=15) as r:
            return any(sig in ln for ln in r.read().decode().splitlines() if ln.strip())
    except Exception as e:
        print(f"dedupe check failed ({e}) - alerting anyway")
        return False


def alarm(title, body):
    """Urgent push, then repeat every minute for NAG_MINUTES. Inline on
    purpose: this process is short-lived, so a background thread would die
    before sending anything."""
    sig = "[bk:" + hashlib.sha1(body.encode()).hexdigest()[:8] + "]"
    if _alarm_running(sig):
        notify(title, body)
        print("alarm already running elsewhere - single push")
        return
    reps = max(1, (NAG_MINUTES * 60) // NAG_INTERVAL) if NAG_MINUTES else 1
    notify(title, f"{body}\n{sig} alert 1/{reps}")
    for n in range(2, reps + 1):
        time.sleep(NAG_INTERVAL)
        try:
            notify(f"STILL OPEN? {title}", f"{body}\n{sig} alert {n}/{reps}")
        except Exception as e:
            print(f"repeat {n} failed: {e}")
            return
    print(f"alarm finished: {reps} alerts")


# The literal sentence shown on the page when ordering is off. It also exists
# as an invisible UI template string in EVERY page, so only count it when it
# sits between HTML tags (i.e. actually rendered).
CLOSED_TEXT = "Currently closed for online ordering"
CLOSED_RENDERED = re.compile(r">\s*" + re.escape(CLOSED_TEXT) + r"\s*<")


def fetch_one(url):
    """(isServiceable, deliveryTime, name, res_status, timing_desc, ui_closed)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode(errors="replace")
    m = re.search(
        r'window\.__PRELOADED_STATE__\s*=\s*JSON\.parse\("(.*?)"\);?\s*</script>',
        raw, re.S)
    if not m:
        raise RuntimeError("PRELOADED_STATE not found - page layout changed")
    data = json.loads(json.loads('"' + m.group(1) + '"'))
    ui_closed = bool(CLOSED_RENDERED.search(raw))
    for rid, blob in ((data.get("pages") or {}).get("restaurant") or {}).items():
        od = blob.get("orderDetails") or {}
        if "isServiceable" in od:
            bi = (blob.get("sections") or {}).get("SECTION_BASIC_INFO") or {}
            return (bool(od["isServiceable"]), od.get("deliveryTime") or "",
                    bi.get("name") or f"res {rid}", bi.get("res_status_text") or "",
                    (bi.get("timing") or {}).get("timing_desc") or "", ui_closed)
    raise RuntimeError("orderDetails.isServiceable missing - schema changed")


def canary_status():
    """Serviceability of the same-area control; None if it can't be read."""
    try:
        return fetch_one(CANARY_URL)[0]
    except Exception as e:
        print(f"canary check failed: {e}")
        return None


def fetch_status():
    """Target restaurant status, with retries. Single parsing path (fetch_one)
    so the target and the canary can never drift apart."""
    last = None
    for attempt in range(3):
        try:
            return fetch_one(URL)
        except Exception as e:
            last = e
            if attempt < 2:
                time.sleep(10)
    raise last


def main():
    if "--test" in sys.argv:
        notify("Test: Zomato watcher live", "If you see this, alerts work.", "default")
        print("test push sent")
        return
    if not in_window():
        print(f"[{now_ist():%a %d %b %H:%M IST}] outside monitoring window "
              f"({WINDOW_START}-{WINDOW_END} IST) - skipping check")
        return
    try:
        open_now, eta, name, status, timing, ui_closed = fetch_status()
    except Exception as e:
        print(f"[{now_ist():%Y-%m-%d %H:%M IST}] fetch/parse failed: {e}", file=sys.stderr)
        try:
            notify("Zomato watcher: problem", str(e), "low", "warning")
        except Exception:
            pass
        sys.exit(1)

    ordering_open = (not ui_closed)      # what the page literally says
    prev = prev_status = None
    if os.path.exists(STATE):
        try:
            old = json.load(open(STATE))
            prev, prev_status = old.get("ordering_open"), old.get("res_status")
        except json.JSONDecodeError:
            pass
    canary = canary_status()
    json.dump({"ordering_open": ordering_open, "page_says_closed": ui_closed,
               "isServiceable": open_now, "eta": eta, "name": name,
               "res_status": status, "timing": timing, "canary_open": canary,
               "checked": now_ist().isoformat()}, open(STATE, "w"), indent=1)

    stamp = f"{now_ist():%a %d %b %H:%M IST}"
    if prev is None:
        print(f"[{stamp}] baseline: {name} | page says "
              f"{'CLOSED for online ordering' if ui_closed else 'ORDERING OPEN'} | "
              f"outlet={status!r} {timing!r} | isServiceable={open_now} | "
              f"canary={canary} - no alert")
        return
    if ordering_open != open_now:
        print(f"[{stamp}] NOTE: page text and isServiceable disagree "
              f"(page_open={ordering_open}, isServiceable={open_now})")
    if ordering_open == prev and status == prev_status:
        note = ""
        if canary and not ordering_open:
            note = "  [canary IS open -> our view reaches Koregaon Park, target genuinely shut]"
        print(f"[{stamp}] no change (page: "
              f"{'closed' if ui_closed else 'OPEN'}, outlet={status!r}, "
              f"canary={canary}){note}")
        return

    with open(HISTORY, "a") as f:
        f.write(json.dumps({"ts": now_ist().isoformat(),
                            "ordering_open": ordering_open,
                            "isServiceable": open_now, "eta": eta,
                            "res_status": status}) + "\n")
    # Outlet open/closed is context, not the thing being waited for: send it as
    # a single quiet note, and skip it entirely when the ordering state changed
    # too (that alert already carries the news, and two repeating alarms for one
    # event meant 30 pushes).
    if status != prev_status:
        print(f"[{stamp}] outlet status {prev_status!r} -> {status!r}")
        if ordering_open == prev:
            notify(f"{name}: outlet now {status}",
                   f"Outlet status changed: {prev_status!r} -> {status!r} at {stamp}\n"
                   f"{timing}\n{URL}\n"
                   f"(Online ordering still {'OPEN' if ordering_open else 'closed'}.)",
                   "low", "information_source")
    if ordering_open == prev:
        return
    if ordering_open:
        alarm(f"{name}: ONLINE ORDERING IS OPEN",
              f"The page no longer says '{CLOSED_TEXT}'"
              f"{' - ETA ' + eta if eta else ''}.\n"
              f"Opened at {stamp}\n{URL}\nORDER NOW - this window may be short.")
        print(f"[{stamp}] OPENED - alarm sent")
    else:
        notify(f"{name}: delivery closed again", f"Window closed at {stamp}",
               "low", "no_entry")
        print(f"[{stamp}] closed again")


if __name__ == "__main__":
    main()
