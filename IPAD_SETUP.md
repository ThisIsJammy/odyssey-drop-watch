# Run the Jailer 2 watcher on an iPad

The iPad works because it polls from your own Indian residential IP, which is
the only kind of address District and PVR will answer. Two routes — do route A
for real coverage, route B as a free backstop.

---

## Route A — a-Shell (3-minute polling, iPad as a dedicated watcher)

1. Install **a-Shell** from the App Store (free). It ships Python 3; the
   watcher needs no extra packages.

2. Open a-Shell and run these three lines:

```
cd ~/Documents
curl -O https://raw.githubusercontent.com/ThisIsJammy/odyssey-drop-watch/main/jailer_watcher.py
curl -O https://raw.githubusercontent.com/ThisIsJammy/odyssey-drop-watch/main/ipad_watch.py
```

3. Start it:

```
python3 ipad_watch.py
```

You should see, every 3 minutes:

```
district: not open - no showDates (canary healthy: 6 dates)
pvr: not open - not in 28 films across 10 cinemas
```

`canary healthy` is the line that matters — it proves the watcher can still
read the page, so "not open" is trustworthy rather than a silent failure.

4. Make the iPad hold the job (this is the important part):
   - Plug it into power.
   - Settings > Display & Brightness > **Auto-Lock > Never**.
   - Leave **a-Shell in the foreground**. iPadOS suspends backgrounded apps,
     so if you switch apps or lock the screen, polling stops.
   - Turn brightness right down; the screen staying on is the cost.

5. To stop: press Ctrl-C in a-Shell (or just close the app).

**Limitation, stated plainly:** iPadOS gives no way to run this truly in the
background. The iPad must stay awake with a-Shell on screen. That is fine for
a spare device sitting on a desk, which is exactly what makes this better than
the Mac — you are not giving up your working machine.

---

## Route B — Shortcuts (no terminal, survives a locked screen, but coarse)

Shortcuts can fetch the page itself and notify you, and it runs even when the
iPad is locked. The catch: a "Time of Day" automation fires **once per day**,
so you get as many checks per day as automations you create.

Build it once:

1. Shortcuts app > Automation tab > **+** > **Time of Day** > pick a time >
   Daily > **Next**. Turn **Ask Before Running OFF** (otherwise it does
   nothing unattended).
2. Add these actions in order:
   - **Get Contents of URL** —
     `https://www.district.in/movies/jailer-2-movie-tickets-in-pune-MV187766`
   - **Match Text** — Text = the result above, Regular Expression:
     `showDates[^]]{0,40}20\d\d-\d\d-\d\d`
   - **Count** — Items = Matches
   - **If** — Count **is greater than** 0
     - **Show Notification**: "JAILER 2 BOOKINGS OPEN in Pune - book now"
     - (optional) **Get Contents of URL**, POST to
       `https://ntfy.sh/jailer2_pune_ak2026`, so it reaches your phone too
   - **Otherwise** — nothing (stay silent)

That regex was verified on 10 Oct 2026 against the live pages: 0 matches on
Jailer 2, 4 matches on a film already on sale in Pune. It reads structure, not
wording — the words "Book tickets" appear on the page even when booking is
closed, so never match on text.

3. Duplicate the automation for a few times a day (e.g. 09:05, 13:05, 17:05,
   21:05). Advance booking for a Rajinikanth film can open at any hour, so
   more automations means less delay.

---

## Which to use

- Spare iPad available: **Route A**, plugged in, auto-lock never. Best
  coverage, 3-minute polling, nothing else needed.
- Want zero babysitting: **Route B** as well — it keeps working when the iPad
  is locked and a-Shell is closed.
- Either way also tap **Notify Me** on BookMyShow's Jailer 2 page. It is the
  only channel that covers MovieMax Gold Mariplex, which sells solely through
  BMS and cannot be read by any script.
