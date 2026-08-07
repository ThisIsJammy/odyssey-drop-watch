# odyssey-drop-watch

Watches the AMC Lincoln Square 13 calendar for new **The Odyssey** IMAX 70mm
showtimes (via the public [drop70mm](https://drop70mm.com) tracker page) and
pushes an [ntfy](https://ntfy.sh) alert when new showtimes appear.

- Runs every ~5 minutes on a GitHub Actions schedule (best-effort timing).
- `state.json` is the last-seen calendar snapshot, committed back by the workflow.
- The ntfy topic name is stored in the `NTFY_TOPIC` repo secret.
- Silence means "no news": fetch/parse failures push a low-priority alert.
