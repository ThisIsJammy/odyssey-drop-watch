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
from datetime import datetime, timedelta, timezone

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


MAX_BODY = 3500          # ntfy turns >4096-byte bodies into an attachment


def cap(lines, tail):
    """Keep an alert readable inside ntfy's message limit."""
    body = "\n".join(lines) + tail
    if len(body.encode()) <= MAX_BODY:
        return body
    kept = []
    for ln in lines:
        if len("\n".join(kept + [ln]).encode()) > MAX_BODY - len(tail.encode()) - 120:
            break
        kept.append(ln)
    return ("\n".join(kept)
            + f"\n...and {len(lines) - len(kept)} more (see the tracker)" + tail)


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


def alarm(title, body, sig_key=None):
    """Push now, then repeat every NAG_INTERVAL for NAG_MINUTES.

    The repeats run INLINE (blocking), not on a background thread: each poll is
    a short-lived process and a daemon thread dies the moment it exits, which
    silently swallowed every repeat in testing. Blocking costs this poller a
    few polls; the other two pollers keep watching in the meantime, and the
    dedupe check above means only one poller ever blocks.""" 
    sig = "[drop:" + hashlib.sha1((sig_key or body).encode()).hexdigest()[:8] + "]"
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
    # A frozen upstream renders a perfect page: every guard passes and we would
    # report "no change" forever. Their own lastCheckedAt exposes it.
    FRESHNESS.clear()
    for _sec in sections:
        _v = _sec.get("venue") or {}
        if _v.get("lastCheckedAt"):
            FRESHNESS[_v.get("shortName", "?")] = _v["lastCheckedAt"]
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
    # NOTE: do NOT threshold on an absolute count here. The run legitimately
    # winds down to 3, 2, 1 showtimes as dates pass (Lincoln Square ends Sep 16),
    # and a fixed floor of 4 would raise on every poll from Sep 16 10:00 ET -
    # freezing the watcher on the last day of the run. Only a structurally
    # broken parse is an error; a shrinking-but-parsed calendar is normal.
    if not any(cal.values()):
        raise RuntimeError(f"no showtimes parsed for any venue: "
                           f"{ {k: len(v) for k, v in cal.items()} }")
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

SHOW_YEAR = int(os.environ.get("SHOW_YEAR", "2026"))
# The tracker's availability flaps: one Sep 16 showtime changed status 17 times
# in 30 snapshots, producing 37 pushes overnight for a seat that never really
# opened. Two-poll hysteresis is not enough on its own - each flap outlives it.
SEAT_COOLDOWN_H = float(os.environ.get("SEAT_COOLDOWN_H", "8"))
LEDGER = {}          # cooldown ledger, written by persist() on every path
FRESHNESS = {}       # venue -> upstream lastCheckedAt, to detect a frozen feed
STALE_MIN = float(os.environ.get("STALE_MIN", "45"))
ET = timezone(timedelta(hours=-4))       # venue-local; DST-exact is not needed


def is_future(key):
    """True if this showtime has not started yet (or cannot be parsed).

    Unparseable -> treated as future ON PURPOSE: a relabel is exactly what
    breaks parsing, and that case SHOULD count toward the reshape guard.
    """
    try:
        _, date_label, time_label = key.split("|")
        mon_day = date_label.split(", ")[-1]              # "Wed, Sep 16" -> "Sep 16"
        t = datetime.strptime(f"{mon_day} {SHOW_YEAR} {time_label}",
                              "%b %d %Y %I:%M %p").replace(tzinfo=ET)
        return t > datetime.now(ET)
    except Exception:
        return True


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
    """Any unexpected failure must be loud, never a silent no-op."""
    try:
        _main()
    except AlertUndelivered as e:
        print(f"ALERT DELIVERY FAILED ({e}) - state left unchanged, will retry "
              f"next poll", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        # previously any non-AlertUndelivered error skipped persist() silently
        report_broken(f"unexpected error: {type(e).__name__}: {e}")
        raise


def report_broken(msg):
    """Loud, throttled 'the watcher itself is broken' alert."""
    print(f"BROKEN: {msg}", file=sys.stderr)
    try:
        marker = STATE + ".problem"
        bsig = "[broken:" + hashlib.sha1(msg.encode()).hexdigest()[:8] + "]"
        # throttle PER MESSAGE: a benign hold-off must not mute a fatal
        # "cannot read the tracker at all" for the next hour
        last, last_sig = 0.0, ""
        if os.path.exists(marker):
            last = os.path.getmtime(marker)
            try:
                last_sig = open(marker).read().split("|", 1)[1]
            except Exception:
                pass
        if (last_sig != bsig or time.time() - last > 3600) \
                and not _nag_already_running(bsig):
            notify("AMC WATCHER IS BROKEN - not just quiet",
                   f"{msg}\n\nSilence from now on does NOT mean 'no drop'.\n{bsig}",
                   "high")
            open(marker, "w").write(f"{time.time()}|{bsig}")
    except Exception as e:
        print(f"  broken-alert failed: {e}", file=sys.stderr)


def _main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if not TOPIC:
        print(f"[{now}] ERROR: NTFY_TOPIC secret not set", file=sys.stderr)
        sys.exit(1)
    try:
        cal = fetch_calendar()
    except Exception as e:
        report_broken(f"cannot read the tracker: {e}")
        sys.exit(1)

    flat = {f"{t}|{k}": v for t, d in cal.items() for k, v in d.items()}
    _hb_written = False
    seat_now = {k: STATUS.get(k) for k in flat
                if k.startswith(ALERT_THEATER) and any(d in k for d in SEAT_WATCH)}
    seat_path = STATE.replace(".json", "_seats.json")
    guard_marker = STATE + ".reshape"

    old = None
    try:
        with open(STATE) as f:
            loaded = json.load(f)
        old = loaded if isinstance(loaded, dict) and loaded else None
        if old is None:
            report_broken(f"{STATE} was empty/not-a-dict - re-baselining, so a "
                          f"drop happening right now could be missed")
    except FileNotFoundError:
        pass
    except Exception as e:
        report_broken(f"{STATE} unreadable ({e}) - re-baselining, so a drop "
                      f"happening right now could be missed")

    seat_prev = {}
    try:
        with open(seat_path) as f:
            sp = json.load(f)
        seat_prev = sp if isinstance(sp, dict) else {}
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"  seat snapshot unreadable ({e})")

    def heartbeat(ok=True, note=""):
        """Proof that a poll actually completed, for the external watchdog."""
        try:
            hb = STATE.replace(".json", "_heartbeat.json")
            prev_n = 0
            if os.path.exists(hb):
                try:
                    prev_n = int(json.load(open(hb)).get("polls", 0))
                except Exception:
                    pass
            write_json(hb, {"at": datetime.now(timezone.utc).isoformat(),
                            "ok": ok, "polls": prev_n + 1, "note": note,
                            "showtimes": len(flat), "poller": os.path.basename(STATE)})
        except Exception as e:
            print(f"  heartbeat write failed: {e}")

    def persist():
        write_json(STATE, flat)
        seat_out = dict(seat_now)
        led = LEDGER.get("alerted") or {}
        if led:
            # keep entries for anything still upcoming, not just what is in this
            # render: a showtime blinking out for one poll used to drop its
            # cooldown and let the flap storm restart
            seat_out["__alerted__"] = {k: v for k, v in led.items() if is_future(k)}
        write_json(seat_path, seat_out)
        if os.path.exists(guard_marker):
            os.remove(guard_marker)

    # Proof a poll really completed. A run can stay green while every poll
    # fails, because the workflow invokes this script with `|| true`, so run
    # status alone tells the watchdog nothing.
    heartbeat(True, "fetched")

    _lc = FRESHNESS.get(ALERT_THEATER)
    if _lc:
        try:
            _age = (datetime.now(timezone.utc) - datetime.fromisoformat(
                _lc.replace("Z", "+00:00"))).total_seconds() / 60
            if _age > STALE_MIN:
                report_broken(f"the tracker's own data for {ALERT_THEATER} is "
                              f"{_age:.0f} min old (it normally refreshes every "
                              f"~2 min) - its scraper looks stalled, so 'no "
                              f"change' right now proves nothing")
            else:
                print(f"  upstream data {_age:.0f} min old (fresh)")
        except Exception as e:
            print(f"  freshness check skipped: {e}")

    if old is None:
        persist()
        print(f"[{now}] baseline seeded: "
              + ", ".join(f"{t}={len(d)}" for t, d in cal.items())
              + " showtimes. No alert.")
        return

    prev_ls = sum(1 for k in old if k.startswith(ALERT_THEATER))
    cur_ls = sum(1 for k in flat if k.startswith(ALERT_THEATER))
    if prev_ls >= 8 and cur_ls * 2 < prev_ls:
        # Lost more than half of a healthy Lincoln Square listing in one poll.
        # Roll-off is 1-2 per poll, so this is a truncated render, not reality.
        # Do NOT persist: the next good poll must not see a phantom drop.
        report_broken(f"Lincoln Square went from {prev_ls} to {cur_ls} showtimes "
                      f"in one poll - treating as a partial page render, not a "
                      f"real change. Not updating state.")
        return

    if prev_ls > 0 and cur_ls == 0:
        # only reachable once the partial-render guard above has cleared it, so
        # this really is an empty listing rather than a truncated page
        try:
            notify_retry(f"{ALERT_THEATER}: no showtimes left",
                         f"The last {ALERT_THEATER} showtime has gone from the "
                         f"listing while other venues are still listed. The run "
                         f"appears to have ended, so silence from here means "
                         f"there is nothing left to watch.", "default")
        except AlertUndelivered as e:
            print(f"  run-ended note undelivered ({e}) - will retry next poll")
            return          # do not persist: retry rather than lose it

    new_keys = [k for k in flat if k not in old]
    vanished = [k for k in old if k not in flat]

    # A relabel upstream makes every key look new. Refuse to alert - but do NOT
    # deadlock: after 3 consecutive polls showing the same reshape, accept the
    # new key space, otherwise the watcher never alerts again (it has already
    # happened once, on the CityWalk -> CityWalk Hollywood rename).
    # Roll-off is not reshape: showtimes vanish as they screen. Counting those
    # meant a 1-day runner outage plus a genuine 8-showtime extension looked
    # like a relabel and the drop was suppressed permanently.
    vanished_future = [k for k in vanished if is_future(k)]
    if len(new_keys) > 6 and len(vanished_future) > 6:
        trips = 0
        try:
            trips = int(open(guard_marker).read().strip())
        except Exception:
            pass
        trips += 1
        try:
            open(guard_marker, "w").write(str(trips))
        except OSError:
            pass
        if trips < 3:
            print(f"[{now}] reshape #{trips}/3: {len(new_keys)} appeared, "
                  f"{len(vanished_future)} future-dated vanished - not alerting yet",
                  file=sys.stderr)
            report_broken(f"{len(new_keys)} showtimes appeared and "
                          f"{len(vanished_future)} future-dated ones vanished in "
                          f"one poll - looks like an upstream relabel, not a drop. "
                          f"Holding off.")
            return
        print(f"[{now}] reshape persisted after {trips} polls - adopting new "
              f"key space", file=sys.stderr)
        adopt_ls = [k for k in new_keys if k.startswith(ALERT_THEATER)]
        if adopt_ls:
            # late, but never silent: if any of the adopted keys are real new
            # showtimes, the user still needs to hear about them
            lines = [f"{k.split('|')[1]} {k.split('|')[2]} -> "
                     f"https://www.amctheatres.com/showtimes/{flat[k]}/seats"
                     for k in sorted(adopt_ls)]
            alarm(f"POSSIBLE DROP (after a page reshape): {len(adopt_ls)} showtime(s)",
                  cap(lines, "\n\nThe page changed shape, so this may be a relabel "
                             "rather than a real release - CHECK AMC."))
        persist()
        return

    # --- drop detection first: it is the alert that matters most ---
    ls_new = [k for k in new_keys if k.startswith(ALERT_THEATER)]
    if ls_new:
        lines = []
        for k in sorted(ls_new):
            parts = k.split("|")
            lines.append(f"{parts[1]} {parts[2]} -> "
                         f"https://www.amctheatres.com/showtimes/{flat[k]}/seats")
        alarm(f"LINCOLN SQUARE DROP: {len(ls_new)} new showtime(s)!",
              cap(lines, "\n\nBUY NOW - 80% sells in the first hour."),
              sig_key="drop:" + ",".join(sorted(flat[k] for k in ls_new)))
        print(f"[{now}] ALERT sent: {ls_new}")

    # --- then seat flips, with hysteresis: the tracker's status flaps between
    # wheelchairOnly and tickets many times a day, which produced false alarm
    # storms. Require the same 'tickets' reading on two consecutive polls. ---
    alerted = seat_prev.get("__alerted__") or {}      # showtime -> last alert iso
    if not isinstance(alerted, dict):
        alerted = {}
    LEDGER["alerted"] = alerted      # persist() owns writing it back

    def in_cooldown(k):
        try:
            last = datetime.fromisoformat(alerted[k])
            return (datetime.now(timezone.utc) - last).total_seconds() \
                < SEAT_COOLDOWN_H * 3600
        except Exception:
            return False        # fail OPEN: a bad ledger entry must not mute us

    pending = {k for k, v in seat_prev.items() if v == "__pending__"}
    confirmed, now_pending = [], {}
    for k, v in seat_now.items():
        # distinguish "never seen" (key absent) from "seen as SOLD OUT" (None).
        # The tracker encodes sold-out as availabilityStatus null, and treating
        # that as "no prior reading" meant a sold-out show reopening NEVER
        # alerted - the one transition that actually gets a ticket.
        was = seat_prev.get(k, "__unseen__")
        if v == "tickets" and was not in ("__unseen__", "tickets", "__pending__"):
            now_pending[k] = "__pending__"          # first sighting: wait one poll
        elif v == "tickets" and k in pending:
            if in_cooldown(k):
                print(f"  {k.split('|', 1)[1]} on sale again but alerted within "
                      f"{SEAT_COOLDOWN_H}h - suppressed (tracker flaps)")
            else:
                confirmed.append(k)
    if confirmed:
        lines = [f"{k.split('|')[1]} {k.split('|')[2]} -> "
                 f"https://www.amctheatres.com/showtimes/{flat[k]}/seats"
                 for k in sorted(confirmed)]
        dates = sorted({k.split("|")[1] for k in confirmed})
        alarm(f"SEATS MAY HAVE OPENED: {', '.join(dates)} "
              f"({len(confirmed)} showtime(s))",
              cap(lines, "\n\nOn sale for two polls running. VERIFY ON AMC."),
              sig_key="seats:" + ",".join(sorted(flat[k] for k in confirmed)))
        for k in confirmed:
            alerted[k] = datetime.now(timezone.utc).isoformat()
        print(f"[{now}] seat alert: {confirmed}")
    seat_now.update(now_pending)     # remember first sightings for the next poll

    other_new = [k for k in new_keys if not k.startswith(ALERT_THEATER)]
    if other_new:
        try:
            notify("Other 70mm venue added showtimes", cap(sorted(other_new), ""),
                   "default")
        except Exception as e:
            print(f"  other-venue note failed (ignored): {e}")

    persist()
    if vanished:
        print(f"[{now}] note: {len(vanished)} showtime(s) rolled off")
    if not new_keys and not vanished and not confirmed:
        print(f"[{now}] no change ({len(flat)} showtimes tracked, "
              f"{sum(1 for k in seat_now if not k.startswith('__'))} seats watched)")

if __name__ == "__main__":
    main()
