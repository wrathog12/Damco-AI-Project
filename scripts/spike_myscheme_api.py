"""
P1.a spike — can we read myScheme's own JSON API instead of rendering + LLM-extracting pages?

`https://api.myscheme.gov.in/schemes/v5/public/schemes?...` returns 401 when called
bare, so the SPA must send an auth header. This script:

  1. Loads the real site under Chromium and intercepts every XHR to api.myscheme.gov.in,
     recording the exact request headers the SPA uses.
  2. Replays the search request with plain httpx using those headers.
  3. Reports `total` (the true corpus size) and the shape of one record, so we can
     judge how much of our schema the API supplies directly.

Run:  python scripts/spike_myscheme_api.py
"""
import asyncio
import json
import sys
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

# The KB is full of Devanagari/Bengali; Windows cp1252 will otherwise kill the process.
sys.stdout.reconfigure(encoding="utf-8")

SEARCH_PAGE = "https://www.myscheme.gov.in/search"
API_HOST = "api.myscheme.gov.in"
OUT_DIR = Path(__file__).resolve().parent.parent / "scraped_data" / "spike"

# Headers a browser sends that are irrelevant when replaying server-side.
DROP_HEADERS = {
    "host", "connection", "content-length", "accept-encoding",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-ch-ua",
    "sec-ch-ua-mobile", "sec-ch-ua-platform", "priority",
}


async def capture_api_calls() -> list[dict]:
    """Drive the SPA and record every request it makes to the API host."""
    captured: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-IN",
        )
        page = await context.new_page()

        def on_request(req):
            if API_HOST in req.url:
                captured.append({"method": req.method, "url": req.url,
                                 "headers": dict(req.headers)})

        page.on("request", on_request)

        responses: dict[str, int] = {}
        page.on("response", lambda r: responses.update({r.url: r.status})
                if API_HOST in r.url else None)

        print(f"[1] Loading {SEARCH_PAGE} ...")
        try:
            await page.goto(SEARCH_PAGE, wait_until="domcontentloaded", timeout=60_000)
            # The scheme list is fetched client-side after hydration.
            await page.wait_for_timeout(8_000)
        except Exception as e:
            print(f"    navigation issue (continuing): {type(e).__name__}: {e}")

        print(f"[2] Intercepted {len(captured)} request(s) to {API_HOST}")
        for c in captured:
            status = responses.get(c["url"], "?")
            print(f"    {c['method']} [{status}] {c['url'][:120]}")

        await browser.close()

    return captured


def pick_search_request(captured: list[dict]) -> dict | None:
    """Prefer the paginated scheme-search call; fall back to any API call."""
    for c in captured:
        if "/public/schemes" in c["url"]:
            return c
    return captured[0] if captured else None


def auth_headers(headers: dict) -> dict:
    """Strip browser-only headers, keep whatever carries authorisation."""
    return {k: v for k, v in headers.items() if k.lower() not in DROP_HEADERS}


def replay(req: dict) -> dict | None:
    """Re-issue the captured request from plain httpx, outside the browser."""
    hdrs = auth_headers(req["headers"])
    interesting = [k for k in hdrs if k.lower() in
                   ("x-api-key", "authorization", "apikey", "x-auth-token")]
    print(f"[3] Replaying with httpx. Auth-ish headers present: {interesting or 'NONE'}")

    try:
        r = httpx.get(req["url"], headers=hdrs, timeout=30.0)
    except Exception as e:
        print(f"    request failed: {type(e).__name__}: {e}")
        return None

    print(f"    HTTP {r.status_code}")
    if r.status_code != 200:
        print(f"    body: {r.text[:300]}")
        return None
    return r.json()


def summarise(payload: dict) -> None:
    """Report corpus size and the field shape of a single record."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "api_sample.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    data = payload.get("data", payload)
    total = None
    for key in ("total", "totalCount", "count"):
        if isinstance(data, dict) and key in data:
            total = data[key]
            break
        if key in payload:
            total = payload[key]
            break

    print(f"[4] total schemes reported: {total}")

    items = None
    if isinstance(data, dict):
        for key in ("hits", "schemes", "items", "results"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
    if items is None and isinstance(data, list):
        items = data

    if items:
        print(f"    records in this page: {len(items)}")
        print(f"    top-level fields of record[0]: {sorted(items[0].keys())}")
    else:
        print(f"    could not locate the record list; payload keys: {list(payload)[:15]}")

    print(f"    full sample written to {OUT_DIR / 'api_sample.json'}")


async def main() -> int:
    captured = await capture_api_calls()
    if not captured:
        print("\nRESULT: no API traffic intercepted — the page may be fully "
              "server-rendered, or it blocked headless Chromium.")
        return 1

    req = pick_search_request(captured)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "captured_requests.json").write_text(
        json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    payload = replay(req)
    if payload is None:
        print("\nRESULT: the API is NOT usable by replaying captured headers.")
        return 1

    summarise(payload)
    print("\nRESULT: the API IS usable. Prefer it over render + LLM extraction.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
