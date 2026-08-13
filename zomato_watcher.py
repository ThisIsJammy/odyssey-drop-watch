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
TOPIC = os.environ.get("NTFY_TOPIC", "")
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


def fetch_status():
    """(isServiceable, deliveryTime, name, res_status, timing_desc).

    NOTE (2026-08-13): isServiceable is NOT "delivery is open" - it means
    "delivers to the location this request is assumed to be at". An anonymous
    request lands on Pune subzone 1165, which cannot reach Koregaon Park:
    German Bakery (Koregaon Park) reads res_status='Open now' AND
    isServiceable=False at the same time. So we also track res_status_text,
    which is the outlet's own open/closed state and is location-independent.
    """
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(URL, headers={
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
            res = (data.get("pages") or {}).get("restaurant") or {}
            for rid, blob in res.items():
                od = blob.get("orderDetails") or {}
                if "isServiceable" in od:
                    bi = (blob.get("sections") or {}).get("SECTION_BASIC_INFO") or {}
                    name = bi.get("name") or f"res {rid}"
                    status = bi.get("res_status_text") or ""
                    timing = (bi.get("timing") or {}).get("timing_desc") or ""
                    return (bool(od["isServiceable"]), od.get("deliveryTime") or "",
                            name, status, timing)
            raise RuntimeError("orderDetails.isServiceable missing - schema changed")
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
    try:
        open_now, eta, name, status, timing = fetch_status()
    except Exception as e:
        print(f"[{now_ist():%Y-%m-%d %H:%M IST}] fetch/parse failed: {e}", file=sys.stderr)
        try:
            notify("Zomato watcher: problem", str(e), "low", "warning")
        except Exception:
            pass
        sys.exit(1)

    prev = prev_status = None
    if os.path.exists(STATE):
        try:
            old = json.load(open(STATE))
            prev, prev_status = old.get("open"), old.get("res_status")
        except json.JSONDecodeError:
            pass
    json.dump({"open": open_now, "eta": eta, "name": name, "res_status": status,
               "timing": timing, "checked": now_ist().isoformat()},
              open(STATE, "w"), indent=1)

    stamp = f"{now_ist():%a %d %b %H:%M IST}"
    if prev is None:
        print(f"[{stamp}] baseline: {name} | outlet={status!r} {timing!r} | "
              f"deliverable-to-default={open_now} - no alert")
        return
    if open_now == prev and status == prev_status:
        print(f"[{stamp}] no change (outlet={status!r}, deliverable={open_now})")
        return

    with open(HISTORY, "a") as f:
        f.write(json.dumps({"ts": now_ist().isoformat(), "open": open_now,
                            "eta": eta, "res_status": status}) + "\n")
    if status != prev_status:
        opened = "open" in status.lower()
        (alarm if opened else lambda t, b: notify(t, b, "low", "no_entry"))(
            f"{name}: outlet now {status}",
            f"Outlet status changed: {prev_status!r} -> {status!r} at {stamp}\n"
            f"{timing}\n{URL}\n"
            f"(Delivery to your address may still differ - check the app.)")
        print(f"[{stamp}] outlet status {prev_status!r} -> {status!r}")
    if open_now == prev:
        return
    if open_now:
        alarm(f"{name}: DELIVERY IS OPEN",
              f"Online ordering just opened{' - ETA ' + eta if eta else ''}.\n"
              f"Opened at {stamp}\n{URL}\nORDER NOW - this window may be short.")
        print(f"[{stamp}] OPENED - alarm sent")
    else:
        notify(f"{name}: delivery closed again", f"Window closed at {stamp}",
               "low", "no_entry")
        print(f"[{stamp}] closed again")


if __name__ == "__main__":
    main()
