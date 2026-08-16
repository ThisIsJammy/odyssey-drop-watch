"""One-off probe: can a real browser reach Apple Cinemas, and what is on the page?

applecinemas.com sits behind a Cloudflare JS challenge, so plain HTTP always
returns 403. This loads the page in headless Chromium, waits for the challenge
to resolve, and dumps enough structure to build a watcher against.

Run in CI (see .github/workflows/apple-probe.yml) - it prints, it never alerts.
"""
import json
import re
import sys

from playwright.sync_api import sync_playwright

URLS = [
    "https://www.applecinemas.com/The-Odyssey---The-IMAX-70MM-Experience--2026-/6a39f902d7095f1524f155f9",
    "https://www.applecinemas.com/The-Odyssey---The-IMAX-70MM-Experience--2026-/6a20725637200b3febb373a1",
]


def probe(page, url):
    print(f"\n{'='*70}\nURL: {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    except Exception as e:
        print(f"  goto failed: {e}")
        return
    # let the Cloudflare challenge run
    for _ in range(30):
        title = page.title()
        if "just a moment" not in title.lower():
            break
        page.wait_for_timeout(1000)
    print(f"  final title : {page.title()!r}")
    print(f"  final url   : {page.url}")
    html = page.content()
    print(f"  html bytes  : {len(html)}")
    if "just a moment" in page.title().lower():
        print("  STILL CHALLENGED - headless Chromium did not pass")
        return

    text = page.inner_text("body")[:4000]
    print(f"\n  --- visible text (first 1200 chars) ---\n{text[:1200]}")

    # what looks like showtimes / dates / sold-out markers?
    for label, pat in [("times", r"\b\d{1,2}:\d{2}\s?[APap][Mm]\b"),
                       ("dates", r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+\w{3,9}\s+\d{1,2}\b"),
                       ("soldout", r"(?i)sold\s?out|unavailable|no longer available")]:
        found = re.findall(pat, text)
        print(f"  {label:8}: {len(found)} -> {found[:8]}")

    # any embedded JSON that carries the schedule?
    for m in re.finditer(r'<script[^>]*type="application/(?:ld\+)?json"[^>]*>(.*?)</script>',
                         html, re.S):
        blob = m.group(1).strip()
        if len(blob) > 80:
            print(f"\n  --- embedded JSON ({len(blob)}b) ---\n  {blob[:500]}")

    # does the page call an API we could hit directly instead?
    print("\n  --- XHR/fetch calls observed ---")
    for u in sorted(set(CALLS)):
        print(f"    {u[:150]}")


CALLS = []

with sync_playwright() as p:
    browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
    ctx = browser.new_context(
        user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        viewport={"width": 1280, "height": 900}, locale="en-US")
    page = ctx.new_page()
    page.on("request", lambda r: CALLS.append(r.url)
            if r.resource_type in ("xhr", "fetch") else None)
    for u in URLS:
        CALLS.clear()
        probe(page, u)
    browser.close()
