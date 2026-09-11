"""
The myScheme API key.

`api.myscheme.gov.in` requires an `x-api-key` header. That key is not secret in
any meaningful sense — it ships to every browser that loads myscheme.gov.in —
but it is not published either, and it can rotate. So we obtain it the way a
browser does: load the site under Playwright and read the header off the first
API request the SPA makes.

Resolution order:
  1. `MYSCHEME_API_KEY` in .env  — pin it explicitly if you already know it.
  2. the on-disk cache            — normal path, costs nothing.
  3. a live Playwright harvest    — first run, or after the key rotates.

The cache lives outside git (see `/.cache/` in .gitignore).
"""
import json
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
CACHE_PATH = REPO_ROOT / ".cache" / "myscheme_key.json"

# The spike already captured a working key; reuse it rather than launching a
# browser on the very first run.
SPIKE_CAPTURE = REPO_ROOT / "scraped_data" / "spike" / "captured_requests.json"

SEARCH_PAGE = "https://www.myscheme.gov.in/search"
API_HOST = "api.myscheme.gov.in"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _read_cache() -> str | None:
    if not CACHE_PATH.exists():
        return None
    try:
        blob = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return blob.get("key") or None
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(key: str) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps({"key": key, "captured_at": time.time()}, indent=2),
        encoding="utf-8",
    )


def _read_spike_capture() -> str | None:
    """Salvage the key the P1.a spike already recorded."""
    if not SPIKE_CAPTURE.exists():
        return None
    try:
        reqs = json.loads(SPIKE_CAPTURE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    for req in reqs:
        for name, value in req.get("headers", {}).items():
            if name.lower() == "x-api-key" and value:
                return value
    return None


async def harvest_key() -> str:
    """Load the real site and intercept the header the SPA sends."""
    from playwright.async_api import async_playwright

    found: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, locale="en-IN")
        page = await ctx.new_page()

        def on_request(req) -> None:
            if API_HOST not in req.url:
                return
            for name, value in req.headers.items():
                if name.lower() == "x-api-key" and value:
                    found.append(value)

        page.on("request", on_request)
        try:
            await page.goto(SEARCH_PAGE, wait_until="domcontentloaded", timeout=60_000)
            # The scheme list is fetched client-side after hydration.
            await page.wait_for_timeout(8_000)
        finally:
            await browser.close()

    if not found:
        raise RuntimeError(
            "Could not intercept an x-api-key from myscheme.gov.in. The site "
            "layout or auth scheme may have changed."
        )
    _write_cache(found[0])
    return found[0]


async def get_api_key(force_refresh: bool = False) -> str:
    """Resolve the key, harvesting only when there is no usable cached value."""
    if not force_refresh:
        try:
            from config import settings
            if settings.myscheme_api_key:
                return settings.myscheme_api_key
        except Exception:      # config import shouldn't be able to block ingestion
            pass

        cached = _read_cache()
        if cached:
            return cached

        salvaged = _read_spike_capture()
        if salvaged:
            _write_cache(salvaged)
            return salvaged

    print("[apikey] harvesting a fresh x-api-key via Playwright ...", file=sys.stderr)
    key = await harvest_key()
    print(f"[apikey] got one ({key[:6]}...{key[-4:]})", file=sys.stderr)
    return key
