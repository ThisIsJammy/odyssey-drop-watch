"""Watch a Zomato restaurant's online-ordering status and alert when it opens.

Signal, in priority order:
  1. the rendered sentence "Currently closed for online ordering" - the words
     actually shown on the page. It also ships as an invisible UI template
     string in EVERY page, so it only counts when rendered between HTML tags.
  2. orderDetails.isServiceable - cross-check. A disagreement is surfaced, not
     silently trusted.
  3. res_status_text - the outlet's own open/closed state (context only).
  4. a canary restaurant in the same area, compared on the SAME signal as the
     target, to prove our vantage point can see that area at all.

DESIGN RULE: silence must mean "no news", never "broken".
  * The alert is delivered BEFORE state is persisted. If delivery fails, state
    is left untouched so the next poll retries - an ntfy blip during a real
    opening used to lose it permanently.
  * State writes are atomic (temp file + os.replace), so a kill mid-write can't
    leave JSON that silently reseeds the baseline.
  * Parse/fetch failures alert at default priority (throttled), because a
    permanently broken parser otherwise looks exactly like a quiet restaurant.

Env: ZOMATO_URL, CANARY_URL, NTFY_TOPIC, NAG_MINUTES, NAG_INTERVAL,
     WINDOW_START, WINDOW_END, STATE_FILE
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
    "ZOMATO_URL", "https://www.zomato.com/pune/burger-king-koregaon-park/order")
CANARY_URL = os.environ.get(
    "CANARY_URL", "https://www.zomato.com/pune/german-bakery-koregaon-park/order")
TOPIC = os.environ.get("NTFY_TOPIC", "")
NAG_MINUTES = int(os.environ.get("NAG_MINUTES", "10"))
# keep nagging while it is genuinely still open, up to this cap (observed
# windows are ~23 min, and a fixed burst can end while it's still orderable)
MAX_NAG_MINUTES = int(os.environ.get("MAX_NAG_MINUTES", "40"))
NAG_INTERVAL = int(os.environ.get("NAG_INTERVAL", "60"))
WINDOW_START = os.environ.get("WINDOW_START", "11:15")   # IST, inclusive
WINDOW_END = os.environ.get("WINDOW_END", "22:45")       # IST, exclusive
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, os.environ.get("STATE_FILE", "zomato_state.json"))
HISTORY = os.path.join(HERE, "zomato_history.jsonl")
IST = timezone(timedelta(hours=5, minutes=30))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

CLOSED_TEXT = "Currently closed for online ordering"
# Only count the sentence when it is actually rendered between tags. Tolerate
# nbsp / trailing punctuation / nested markup so a cosmetic tweak upstream does
# not read as "open".
CLOSED_RENDERED = re.compile(
    r">[\s ]*" + r"[\s <>/a-zA-Z]*?".join(
        re.escape(w) for w in CLOSED_TEXT.split()) + r"[\s .!]*<",
    re.I)


def now_ist():
    return datetime.now(IST)


def in_window(t=None):
    t = t or now_ist()
    def mins(hhmm):
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    n = t.hour * 60 + t.minute
    a, b = mins(WINDOW_START), mins(WINDOW_END)
    return a <= n < b if a <= b else (n >= a or n < b)


def notify(title, body, priority="urgent", tags="hamburger,bell"):
    """Raises on failure - callers decide whether that's fatal."""
    if not TOPIC:
        raise RuntimeError("NTFY_TOPIC is unset - refusing to run mute")
    req = urllib.request.Request(
        f"https://ntfy.sh/{TOPIC}", data=body.encode("utf-8"),
        headers={"Title": title.encode("ascii", "ignore").decode(),
                 "Priority": priority, "Tags": tags, "Click": URL},
        method="POST")
    urllib.request.urlopen(req, timeout=15)


def notify_retry(title, body, priority="urgent", tags="hamburger,bell", tries=3):
    """Deliver or raise after retries. Used for the alert that must not be lost."""
    last = None
    for i in range(tries):
        try:
            notify(title, body, priority, tags)
            return
        except Exception as e:
            last = e
            print(f"  notify attempt {i+1}/{tries} failed: {e}")
            if i < tries - 1:
                time.sleep(5)
    raise last


def alarm_already_running(sig):
    """True if an alarm for this same event is already in flight elsewhere."""
    try:
        u = f"https://ntfy.sh/{TOPIC}/json?poll=1&since={NAG_MINUTES + 5}m"
        with urllib.request.urlopen(
                urllib.request.Request(u, headers={"User-Agent": UA}), timeout=15) as r:
            return any(sig in ln for ln in r.read().decode().splitlines() if ln.strip())
    except Exception as e:
        print(f"  dedupe check failed ({e}) - alerting anyway")
        return False


def alarm(title, body, sig_key):
    """Deliver the first push (raising if it cannot be delivered), then repeat.

    sig_key is a STABLE identifier for the event - never the message body,
    which contains timestamps and would make every poller's signature differ.
    """
    sig = "[bk:" + hashlib.sha1(sig_key.encode()).hexdigest()[:8] + "]"
    if alarm_already_running(sig):
        notify_retry(title, f"{body}\n{sig} (alarm already running elsewhere)")
        print("  alarm already running elsewhere - single push")
        return
    max_reps = max(1, (MAX_NAG_MINUTES * 60) // NAG_INTERVAL)
    notify_retry(title, f"{body}\n{sig} alert 1")   # raises if undeliverable
    for n in range(2, max_reps + 1):
        time.sleep(NAG_INTERVAL)
        try:
            still_open = fetch_one(URL)[0]
        except Exception as e:
            print(f"  re-check {n} failed ({e}) - continuing to nag")
            still_open = True
        if not still_open:
            try:
                notify(f"{title} - WINDOW CLOSED",
                       f"It shut again after ~{n} min. Alarm stopping.\n{sig}",
                       "low", "no_entry")
            except Exception:
                pass
            print(f"  target closed after {n} repeats - alarm stopped")
            return
        try:
            notify(f"STILL OPEN ({n} min): {title}", f"{body}\n{sig} alert {n}")
        except Exception as e:
            print(f"  repeat {n} failed: {e}")
    print(f"  alarm hit the {MAX_NAG_MINUTES} min cap while still open")


def write_state(payload):
    """Atomic write - a kill mid-write must not leave parseable-but-wrong JSON."""
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE)


def read_state():
    """Previous state, or None if absent/unusable."""
    try:
        with open(STATE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        print(f"  state unreadable ({e}) - treating as first run")
        return None


def fetch_one(url):
    """(ordering_open, isServiceable, eta, name, res_status, timing) for a url.

    Raises on anything that isn't a recognisable restaurant page, so a block
    page / redirect / redesign can never be mistaken for 'ordering is open'.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=30) as r:
        final_url = r.geturl()
        raw = r.read().decode(errors="replace")
    if "/order" not in final_url:
        raise RuntimeError(f"redirected away from the order page -> {final_url}")
    if len(raw) < 50_000:
        raise RuntimeError(f"page suspiciously small ({len(raw)}b) - block page?")
    m = re.search(
        r'window\.__PRELOADED_STATE__\s*=\s*JSON\.parse\("(.*?)"\);?\s*</script>',
        raw, re.S)
    if not m:
        raise RuntimeError("PRELOADED_STATE not found - layout changed or blocked")
    # the template string must be present; its absence means this isn't the page
    # we think it is, and 'closed sentence missing' would wrongly read as open
    if CLOSED_TEXT not in raw:
        raise RuntimeError("closed-text template missing - unexpected page shape")
    data = json.loads(json.loads('"' + m.group(1) + '"'))
    ui_closed = bool(CLOSED_RENDERED.search(raw))
    for rid, blob in ((data.get("pages") or {}).get("restaurant") or {}).items():
        od = blob.get("orderDetails") or {}
        if "isServiceable" in od:
            bi = (blob.get("sections") or {}).get("SECTION_BASIC_INFO") or {}
            return (not ui_closed, bool(od["isServiceable"]),
                    od.get("deliveryTime") or "", bi.get("name") or f"res {rid}",
                    bi.get("res_status_text") or "",
                    (bi.get("timing") or {}).get("timing_desc") or "")
    raise RuntimeError("orderDetails.isServiceable missing - schema changed")


def fetch_retry(url):
    last = None
    for attempt in range(3):
        try:
            return fetch_one(url)
        except Exception as e:
            last = e
            if attempt < 2:
                time.sleep(10)
    raise last


def canary_open():
    """Same signal as the target (ordering_open), not isServiceable."""
    try:
        return fetch_retry(CANARY_URL)[0]
    except Exception as e:
        print(f"  canary check failed: {e}")
        return None


def problem(msg, prev):
    """Announce a broken watcher, throttled to once an hour."""
    print(f"[{now_ist():%a %d %b %H:%M IST}] PROBLEM: {msg}", file=sys.stderr)
    last = (prev or {}).get("last_problem_alert")
    if last:
        try:
            if (now_ist() - datetime.fromisoformat(last)).total_seconds() < 3600:
                return
        except ValueError:
            pass
    try:
        notify("Zomato watcher is BROKEN", f"{msg}\n\n(Not a restaurant update - "
               "the watcher itself cannot read the page, so silence right now "
               "does NOT mean 'still closed'.)", "default", "warning")
        st = dict(prev or {})
        st["last_problem_alert"] = now_ist().isoformat()
        write_state(st)
    except Exception as e:
        print(f"  problem-alert failed: {e}")


def main():
    stamp = f"{now_ist():%a %d %b %H:%M IST}"
    if "--test" in sys.argv:
        notify("Test: Zomato watcher live", "If you see this, alerts work.", "default")
        print("test push sent")
        return

    prev_state = read_state()
    if not in_window():
        # Record the closed state rather than skipping silently: the outlet is
        # shut outside its hours, and leaving yesterday's state in place made
        # the first morning poll compare against 12 hours ago.
        if (prev_state or {}).get("ordering_open") is not False:
            write_state({"ordering_open": False, "note": "outside window",
                         "checked": now_ist().isoformat(),
                         "last_problem_alert": (prev_state or {}).get("last_problem_alert")})
        print(f"[{stamp}] outside window ({WINDOW_START}-{WINDOW_END} IST) - skipped")
        return

    try:
        ordering_open, serviceable, eta, name, status, timing = fetch_retry(URL)
    except Exception as e:
        problem(f"cannot read {URL}: {e}", prev_state)
        sys.exit(1)

    canary = canary_open()
    prev = (prev_state or {}).get("ordering_open")
    prev_status = (prev_state or {}).get("res_status")
    disagree = ordering_open != serviceable
    new_state = {"ordering_open": ordering_open, "isServiceable": serviceable,
                 "eta": eta, "name": name, "res_status": status, "timing": timing,
                 "canary_open": canary, "checked": now_ist().isoformat(),
                 "last_problem_alert": (prev_state or {}).get("last_problem_alert")}

    if disagree:
        print(f"[{stamp}] NOTE: page text says open={ordering_open} but "
              f"isServiceable={serviceable}")
        if ordering_open is False and serviceable is True:
            # text claims closed while the API says orderable: the text signal
            # may have silently broken (e.g. hidden template node)
            problem("page text says CLOSED but isServiceable=True - the text "
                    "signal may be broken; check manually", prev_state)

    if prev is None:
        write_state(new_state)
        print(f"[{stamp}] baseline: {name} | page says "
              f"{'ORDERING OPEN' if ordering_open else 'closed'} | outlet={status!r} "
              f"{timing!r} | isServiceable={serviceable} | canary={canary}")
        return

    if ordering_open == prev and status == prev_status:
        note = ("  [canary IS open -> we can see this area; target genuinely shut]"
                if canary and not ordering_open else "")
        write_state(new_state)
        print(f"[{stamp}] no change (page: {'OPEN' if ordering_open else 'closed'}, "
              f"outlet={status!r}, canary={canary}){note}")
        return

    # --- something changed: alert FIRST, persist only if delivery succeeded ---
    try:
        if ordering_open != prev:
            if ordering_open:
                alarm(f"{name}: ONLINE ORDERING IS OPEN",
                      f"The page no longer says '{CLOSED_TEXT}'"
                      f"{' - ETA ' + eta if eta else ''}.\n"
                      f"Opened at {stamp}\n{URL}\nORDER NOW - may be brief."
                      + ("\n(NB: isServiceable still False - verify in the app.)"
                         if disagree else ""),
                      sig_key=f"{name}|open|{now_ist():%Y-%m-%d}")
            else:
                notify_retry(f"{name}: ordering closed again",
                             f"Closed at {stamp}", "low", "no_entry")
        elif status != prev_status:
            # log only. res_status_text is a live countdown ("Opens in 29
            # minutes"), so pushing it meant ~11 junk alerts before every
            # opening - exactly the noise that makes a real alert get ignored.
            print(f"[{stamp}] outlet {prev_status!r} -> {status!r} (not pushed)")
    except Exception as e:
        # Do NOT persist: leaving state unchanged means the next poll sees the
        # same transition and tries again, instead of losing the event forever.
        print(f"[{stamp}] ALERT DELIVERY FAILED ({e}) - state left unchanged, "
              f"will retry next poll", file=sys.stderr)
        sys.exit(1)

    write_state(new_state)
    if ordering_open != prev:
        try:
            with open(HISTORY, "a") as f:
                f.write(json.dumps({"ts": now_ist().isoformat(),
                                    "ordering_open": ordering_open,
                                    "isServiceable": serviceable, "eta": eta,
                                    "res_status": status}) + "\n")
        except OSError as e:
            print(f"  history append failed: {e}")
    print(f"[{stamp}] change handled (ordering_open {prev} -> {ordering_open}, "
          f"outlet {prev_status!r} -> {status!r})")


if __name__ == "__main__":
    main()
