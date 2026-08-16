"""Probe 3: after Cloudflare clearance, call the schedule API from inside the page.

Probe 2 showed the document passes the challenge but the XHR to
/MovieSchedule/GetScheduleByShowId/<id> was itself served a challenge page -
most likely because it fired before cf_clearance existed. This waits for the
clearance cookie, then issues the request from the page's own context (so it
carries the cookie and correct origin), and finally falls back to reading the
rendered DOM.
"""
import json
import re

from playwright.sync_api import sync_playwright

URL = ("https://www.applecinemas.com/The-Odyssey---The-IMAX-70MM-Experience--2026-"
       "/6a39f902d7095f1524f155f9")
SHOW_ID = "6a39f902d7095f1524f155f9"
API = f"https://www.applecinemas.com/MovieSchedule/GetScheduleByShowId/{SHOW_ID}"

with sync_playwright() as p:
    browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
    ctx = browser.new_context(
        user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        viewport={"width": 1280, "height": 900}, locale="en-US",
        timezone_id="America/New_York")
    page = ctx.new_page()

    print(f"loading {URL}")
    page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
    for i in range(45):
        if "just a moment" not in page.title().lower():
            print(f"  cleared after ~{i}s, title={page.title()!r}")
            break
        page.wait_for_timeout(1000)
    else:
        print("  never cleared the challenge")

    cookies = {c["name"]: c["value"] for c in ctx.cookies()}
    print("  cf_clearance present:", "cf_clearance" in cookies)
    print("  cookie names:", sorted(cookies)[:12])

    page.wait_for_timeout(5000)   # let the app settle

    print("\n--- call the API from inside the page context ---")
    for attempt in range(1, 4):
        try:
            res = page.evaluate("""async (api) => {
                const r = await fetch(api, {credentials: 'include',
                    headers: {'X-Requested-With': 'XMLHttpRequest',
                              'Accept': 'application/json, text/plain, */*'}});
                const t = await r.text();
                return {status: r.status, len: t.length, body: t.slice(0, 3000)};
            }""", API)
            print(f"  attempt {attempt}: status={res['status']} len={res['len']}")
            body = res["body"]
            if "just a moment" in body.lower():
                print("    -> challenge page again")
            else:
                print("    -> REAL RESPONSE:")
                try:
                    print(json.dumps(json.loads(body), indent=1)[:2500])
                except Exception:
                    print(body[:2000])
                break
        except Exception as e:
            print(f"  attempt {attempt} error: {e}")
        page.wait_for_timeout(6000)

    print("\n--- rendered DOM fallback ---")
    txt = page.inner_text("body")
    print(f"  visible text: {len(txt)} bytes")
    print(txt[:1500])
    for label, pat in [("times", r"\b\d{1,2}:\d{2}\s?[APap]\.?[Mm]\.?\b"),
                       ("dates", r"\b\w{3,9}\s+\d{1,2},?\s+2026\b|\b(?:Sep|September)\s+\d{1,2}\b"),
                       ("soldout", r"(?i)sold\s?out")]:
        f = re.findall(pat, txt)
        print(f"  {label}: {len(f)} {f[:10]}")

    browser.close()
