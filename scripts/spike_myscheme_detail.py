"""
P1.a spike, part 2 — the detail endpoint, deep pagination, and facet filtering.

Part 1 proved the search API is replayable with a captured `x-api-key` and reports
4,772 schemes. Search results carry only metadata, so this script establishes:

  1. Whether a per-scheme DETAIL endpoint exists (benefits / eligibility /
     documents / application process) and what it returns.
  2. Whether pagination works past the first page, and how deep it goes before
     the backend refuses (Elasticsearch-style `from + size` caps are common).
  3. Whether facet filters work server-side, which is what lets us harvest a
     balanced corpus per (state x category) instead of skewed default ordering.

Run:  python scripts/spike_myscheme_detail.py
"""
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
from playwright.async_api import async_playwright

sys.stdout.reconfigure(encoding="utf-8")

API_HOST = "api.myscheme.gov.in"
SEARCH_URL = f"https://{API_HOST}/search/v6/schemes"
OUT_DIR = Path(__file__).resolve().parent.parent / "scraped_data" / "spike"
CAPTURED = OUT_DIR / "captured_requests.json"

DROP_HEADERS = {
    "host", "connection", "content-length", "accept-encoding",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-ch-ua",
    "sec-ch-ua-mobile", "sec-ch-ua-platform", "priority",
}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def load_headers() -> dict:
    """Reuse the x-api-key captured by part 1."""
    reqs = json.loads(CAPTURED.read_text(encoding="utf-8"))
    hdrs = {k: v for k, v in reqs[0]["headers"].items()
            if k.lower() not in DROP_HEADERS}
    if not any(k.lower() == "x-api-key" for k in hdrs):
        raise SystemExit("No x-api-key in captured_requests.json — rerun part 1.")
    return hdrs


async def capture_detail_call(slug: str) -> list[dict]:
    """Open a scheme page and record the API calls the SPA makes for it."""
    captured: list[dict] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, locale="en-IN")
        page = await ctx.new_page()
        statuses: dict[str, int] = {}

        page.on("request", lambda r: captured.append(
            {"method": r.method, "url": r.url, "headers": dict(r.headers)}
        ) if API_HOST in r.url else None)
        page.on("response", lambda r: statuses.update({r.url: r.status})
                if API_HOST in r.url else None)

        url = f"https://www.myscheme.gov.in/schemes/{slug}"
        print(f"[1] Loading detail page {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(8_000)
        except Exception as e:
            print(f"    navigation issue (continuing): {type(e).__name__}: {e}")

        for c in captured:
            print(f"    {c['method']} [{statuses.get(c['url'],'?')}] {c['url'][:130]}")
        await browser.close()
    return captured


def probe_detail(slug: str, hdrs: dict, captured: list[dict]) -> dict | None:
    """Replay the detail call server-side; fall back to guessed URL shapes."""
    candidates = [c["url"] for c in captured if slug in c["url"]]
    candidates += [
        f"https://{API_HOST}/schemes/v5/public/schemes/{slug}?lang=en",
        f"https://{API_HOST}/schemes/v4/public/schemes/{slug}?lang=en",
    ]
    for url in dict.fromkeys(candidates):
        try:
            r = httpx.get(url, headers=hdrs, timeout=30.0)
        except Exception as e:
            print(f"    {type(e).__name__} on {url[:90]}")
            continue
        print(f"[2] HTTP {r.status_code}  {url[:110]}")
        if r.status_code == 200:
            (OUT_DIR / "api_detail_sample.json").write_text(
                json.dumps(r.json(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"    saved -> {OUT_DIR / 'api_detail_sample.json'}")
            return r.json()
    return None


def probe_pagination(hdrs: dict) -> None:
    """Find the deepest `from` offset the backend will serve."""
    print("\n[3] Pagination depth")
    for frm in (0, 100, 1000, 4000, 4770, 9000):
        params = {"lang": "en", "q": "[]", "keyword": "", "sort": "",
                  "from": str(frm), "size": "10"}
        try:
            r = httpx.get(SEARCH_URL, headers=hdrs, params=params, timeout=30.0)
            n = len(r.json().get("data", {}).get("hits", {}).get("items", [])) \
                if r.status_code == 200 else 0
            print(f"    from={frm:<5} HTTP {r.status_code}  items={n}")
        except Exception as e:
            print(f"    from={frm:<5} {type(e).__name__}: {e}")

    print("\n[4] Max page size")
    for size in (10, 100, 500, 1000):
        params = {"lang": "en", "q": "[]", "keyword": "", "sort": "",
                  "from": "0", "size": str(size)}
        try:
            r = httpx.get(SEARCH_URL, headers=hdrs, params=params, timeout=60.0)
            n = len(r.json().get("data", {}).get("hits", {}).get("items", [])) \
                if r.status_code == 200 else 0
            print(f"    size={size:<5} HTTP {r.status_code}  items returned={n}")
        except Exception as e:
            print(f"    size={size:<5} {type(e).__name__}: {e}")


def probe_facet_filter(hdrs: dict) -> None:
    """Confirm server-side faceting — the fix for our category skew."""
    print("\n[5] Facet filtering (the balanced-harvest mechanism)")
    checks = [
        ("beneficiaryState", "Bihar"),
        ("schemeCategory", "Health & Wellness"),
        ("level", "Central"),
    ]
    for ident, value in checks:
        q = json.dumps([{"identifier": ident, "value": value}], separators=(",", ":"))
        url = (f"{SEARCH_URL}?lang=en&q={quote(q)}&keyword=&sort=&from=0&size=1")
        try:
            r = httpx.get(url, headers=hdrs, timeout=30.0)
            if r.status_code == 200:
                total = r.json()["data"]["summary"]["total"]
                print(f"    {ident}={value!r:28} total={total}")
            else:
                print(f"    {ident}={value!r:28} HTTP {r.status_code}: {r.text[:120]}")
        except Exception as e:
            print(f"    {ident}={value!r:28} {type(e).__name__}: {e}")


def summarise_detail(payload: dict) -> None:
    print("\n[6] Detail record shape")
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        print(f"    unexpected type: {type(data).__name__}")
        return
    for k, v in data.items():
        t = type(v).__name__
        if isinstance(v, (list, dict)):
            print(f"    {k:24} {t} len={len(v)}  {json.dumps(v, ensure_ascii=False)[:90]}")
        else:
            print(f"    {k:24} {t}  {str(v)[:90]}")


async def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    hdrs = load_headers()
    slug = "sui"  # Stand-Up India, from part 1's first page

    captured = await capture_detail_call(slug)
    detail = probe_detail(slug, hdrs, captured)
    if detail is None:
        print("\n    No detail endpoint reachable — scheme bodies would need "
              "page rendering even though metadata comes from the API.")
    else:
        summarise_detail(detail)

    probe_pagination(hdrs)
    probe_facet_filter(hdrs)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
