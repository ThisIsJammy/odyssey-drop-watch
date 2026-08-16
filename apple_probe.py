"""Probe 2: capture the schedule API payload behind Apple Cinemas' Cloudflare.

Probe 1 established that headless Chromium passes the challenge and that the
page fetches:
    /MovieSchedule/GetScheduleByShowId/<showId>
This grabs that response body so a watcher can be built against the real shape.
"""
import json
import re

from playwright.sync_api import sync_playwright

URLS = [
    ("A", "https://www.applecinemas.com/The-Odyssey---The-IMAX-70MM-Experience--2026-/6a39f902d7095f1524f155f9"),
    ("B", "https://www.applecinemas.com/The-Odyssey---The-IMAX-70MM-Experience--2026-/6a20725637200b3febb373a1"),
]

captured = {}


def on_response(resp):
    if "GetScheduleByShowId" in resp.url:
        try:
            captured[resp.url] = resp.text()
        except Exception as e:
            captured[resp.url] = f"<body unavailable: {e}>"


with sync_playwright() as p:
    browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
    ctx = browser.new_context(
        user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        viewport={"width": 1280, "height": 900}, locale="en-US")
    page = ctx.new_page()
    page.on("response", on_response)

    for label, url in URLS:
        print(f"\n{'='*70}\n[{label}] {url}")
        for attempt in range(3):
            try:
                page.goto(url, wait_until="networkidle", timeout=60_000)
            except Exception as e:
                print(f"  goto: {e}")
            for _ in range(30):
                if "just a moment" not in page.title().lower():
                    break
                page.wait_for_timeout(1000)
            if "just a moment" not in page.title().lower():
                break
            print(f"  attempt {attempt+1}: still challenged, retrying")
            page.wait_for_timeout(4000)
        print(f"  title: {page.title()!r}")
        # give the XHR time to land
        page.wait_for_timeout(4000)
        txt = page.inner_text("body")
        print(f"  visible text ({len(txt)}b), first 700:\n{txt[:700]}")

    browser.close()

print(f"\n{'='*70}\nCAPTURED API RESPONSES: {len(captured)}")
for url, body in captured.items():
    print(f"\n--- {url}\n    {len(body)} bytes")
    try:
        data = json.loads(body)
        print("    parsed JSON. top-level type:", type(data).__name__)
        if isinstance(data, dict):
            print("    keys:", list(data.keys())[:20])
        sample = json.dumps(data, indent=1)[:2500]
        print("    sample:\n", sample)
    except Exception:
        print("    not JSON. first 1500 chars:\n", body[:1500])
