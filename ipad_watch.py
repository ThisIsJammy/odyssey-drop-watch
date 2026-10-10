#!/usr/bin/env python3
"""Entry point for iPad (a-Shell) and any shell that cannot set env vars.

a-Shell's shell does not reliably support `VAR=value command`, so the
configuration is set here in Python before importing the watcher, which reads
its env at import time. Run with just:  python3 ipad_watch.py
"""
import os

os.environ.setdefault("NTFY_TOPIC", "jailer2_pune_ak2026")
os.environ.setdefault("POLL_SECONDS", "180")       # 3 minutes
os.environ.setdefault("MAX_POLLS", "1000000")      # keep going until stopped
os.environ.setdefault("STATE_FILE", "jailer_state_ipad.json")

import jailer_watcher  # noqa: E402  (must come after the env is set)

if __name__ == "__main__":
    print("Jailer 2 Pune watcher - iPad. Keep this app in the FOREGROUND.")
    print("Alerts go to ntfy topic: " + os.environ["NTFY_TOPIC"])
    jailer_watcher.main()
