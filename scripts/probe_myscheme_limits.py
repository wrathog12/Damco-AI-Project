"""
P1.a — measure myScheme's rate limits before committing to a full harvest.

We are hitting a government API with a key harvested from their own SPA. The
point of this script is to find the *polite* ceiling empirically rather than
guess it, so ingestion can be tuned to sit under it.

Method:
  1. Inspect response headers for rate-limit / CDN hints.
  2. Serial baseline: per-request latency with concurrency 1.
  3. Concurrency ladder: 2 -> 4 -> 8 -> 16 -> 32, aborting the moment we see a
     429/503, so we never keep pushing past a limit we have already found.
  4. Sustained run at the best safe concurrency, to catch token-bucket limits
     that only bite after a burst allowance is spent.

Total budget is ~250 requests, which is under 3% of one full harvest.

Run:  python scripts/probe_myscheme_limits.py
"""
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8")

API = "https://api.myscheme.gov.in"
SEARCH = f"{API}/search/v6/schemes"
DETAIL = f"{API}/schemes/v6/public/schemes"
OUT_DIR = Path(__file__).resolve().parent.parent / "scraped_data" / "spike"
CAPTURED = OUT_DIR / "captured_requests.json"

DROP_HEADERS = {
    "host", "connection", "content-length", "accept-encoding",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-ch-ua",
    "sec-ch-ua-mobile", "sec-ch-ua-platform", "priority",
}

# Headers that would tell us the server-side budget, if the API publishes one.
RATE_HINT_KEYS = (
    "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
    "ratelimit-limit", "ratelimit-remaining", "ratelimit-reset",
    "retry-after", "x-rate-limit-limit", "x-kong-upstream-latency",
    "x-amzn-ratelimit-limit", "server", "via", "x-cache", "cf-ray",
    "x-amz-cf-id", "x-envoy-upstream-service-time",
)

THROTTLE_CODES = {429, 503, 403, 509}


def load_headers() -> dict:
    reqs = json.loads(CAPTURED.read_text(encoding="utf-8"))
    hdrs = {k: v for k, v in reqs[0]["headers"].items()
            if k.lower() not in DROP_HEADERS}
    if not any(k.lower() == "x-api-key" for k in hdrs):
        raise SystemExit("No x-api-key in captured_requests.json — rerun the spike.")
    return hdrs


async def fetch_slugs(client: httpx.AsyncClient, n: int) -> list[str]:
    """Grab n slugs to use as realistic detail-request targets."""
    r = await client.get(SEARCH, params={"lang": "en", "q": "[]", "keyword": "",
                                        "sort": "", "from": "0", "size": "100"})
    items = r.json()["data"]["hits"]["items"]
    return [it["fields"]["slug"] for it in items][:n]


async def timed_get(client: httpx.AsyncClient, url: str, params: dict) -> tuple:
    """Return (status, elapsed_seconds, bytes). Never raises."""
    t0 = time.perf_counter()
    try:
        r = await client.get(url, params=params)
        return r.status_code, time.perf_counter() - t0, len(r.content)
    except Exception as e:
        return type(e).__name__, time.perf_counter() - t0, 0


def summarise(label: str, results: list[tuple], wall: float) -> dict:
    codes: dict = {}
    for status, _, _ in results:
        codes[status] = codes.get(status, 0) + 1
    lat = [d for s, d, _ in results if s == 200]
    ok = len(lat)
    payload = sum(b for _, _, b in results)
    stats = {
        "label": label, "requests": len(results), "ok": ok,
        "wall_s": round(wall, 2),
        "rps": round(len(results) / wall, 2) if wall else 0,
        "p50_ms": round(statistics.median(lat) * 1000) if lat else None,
        "p95_ms": round(sorted(lat)[int(len(lat) * 0.95) - 1] * 1000) if len(lat) > 1 else None,
        "mean_kb": round(payload / max(len(results), 1) / 1024, 1),
        "codes": codes,
    }
    print(f"    {label:22} {ok}/{len(results)} ok  {stats['rps']:>6.2f} req/s  "
          f"p50={stats['p50_ms']}ms p95={stats['p95_ms']}ms  "
          f"avg={stats['mean_kb']}KB  codes={codes}")
    return stats


def throttled(results: list[tuple]) -> bool:
    return any(s in THROTTLE_CODES for s, _, _ in results)


async def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    hdrs = load_headers()
    report: dict = {}

    limits = httpx.Limits(max_connections=64, max_keepalive_connections=64)
    async with httpx.AsyncClient(headers=hdrs, timeout=60.0, limits=limits,
                                 http2=False) as client:

        # ---- 1. published limits / infrastructure ----------------------
        print("[1] Response headers (looking for a published budget)")
        r = await client.get(SEARCH, params={"lang": "en", "q": "[]", "keyword": "",
                                             "sort": "", "from": "0", "size": "1"})
        found = {k: v for k, v in r.headers.items() if k.lower() in RATE_HINT_KEYS}
        for k, v in sorted(found.items()):
            print(f"    {k}: {v}")
        if not any(k.lower().startswith(("x-ratelimit", "ratelimit")) for k in found):
            print("    -> no published rate-limit headers; limits must be inferred")
        report["headers"] = found

        slugs = await fetch_slugs(client, 40)
        print(f"    using {len(slugs)} real slugs as detail targets")
        dparams = [{"slug": s, "lang": "en"} for s in slugs]

        # ---- 2. serial baseline ---------------------------------------
        print("\n[2] Serial baseline (concurrency 1)")
        t0 = time.perf_counter()
        res = [await timed_get(client, DETAIL, dparams[i % len(dparams)])
               for i in range(10)]
        report["serial"] = summarise("detail c=1", res, time.perf_counter() - t0)

        t0 = time.perf_counter()
        res = [await timed_get(client, SEARCH,
                               {"lang": "en", "q": "[]", "keyword": "", "sort": "",
                                "from": str(i * 100), "size": "100"})
               for i in range(5)]
        report["serial_list"] = summarise("list c=1 size=100", res,
                                          time.perf_counter() - t0)

        # ---- 3. concurrency ladder ------------------------------------
        print("\n[3] Concurrency ladder (aborts on first 429/503)")
        ladder = []
        safe_c = 1
        for c in (2, 4, 8, 16, 32):
            batch = [dparams[i % len(dparams)] for i in range(c * 2)]
            sem = asyncio.Semaphore(c)

            async def one(p):
                async with sem:
                    return await timed_get(client, DETAIL, p)

            t0 = time.perf_counter()
            res = await asyncio.gather(*(one(p) for p in batch))
            stats = summarise(f"detail c={c}", list(res), time.perf_counter() - t0)
            ladder.append(stats)
            if throttled(res):
                print(f"    -> throttling appeared at c={c}; stopping the ladder")
                break
            if stats["ok"] != len(res):
                print(f"    -> non-throttle errors at c={c}; stopping the ladder")
                break
            safe_c = c
            await asyncio.sleep(1.0)  # let any token bucket refill between rungs
        report["ladder"] = ladder
        report["safe_concurrency"] = safe_c

        # ---- 4. sustained run ----------------------------------------
        print(f"\n[4] Sustained run at c={safe_c} (catches burst-allowance limits)")
        batch = [dparams[i % len(dparams)] for i in range(120)]
        sem = asyncio.Semaphore(safe_c)

        async def one2(p):
            async with sem:
                return await timed_get(client, DETAIL, p)

        t0 = time.perf_counter()
        res = list(await asyncio.gather(*(one2(p) for p in batch)))
        wall = time.perf_counter() - t0
        report["sustained"] = summarise(f"120 reqs c={safe_c}", res, wall)

        # Did latency drift upward across the run? That is soft throttling.
        lat = [d for s, d, _ in res if s == 200]
        if len(lat) >= 40:
            first, last = lat[:20], lat[-20:]
            drift = statistics.median(last) / max(statistics.median(first), 1e-6)
            print(f"    latency drift first20 -> last20: x{drift:.2f} "
                  f"({'soft throttling likely' if drift > 1.5 else 'stable'})")
            report["sustained"]["latency_drift"] = round(drift, 2)

    (OUT_DIR / "rate_limit_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsaved -> {OUT_DIR / 'rate_limit_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
