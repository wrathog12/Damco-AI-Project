"""
Harvest the myScheme corpus to disk.

    python backend/scripts/harvest_schemes.py --stage index
    python backend/scripts/harvest_schemes.py --limit 50            # smoke test
    python backend/scripts/harvest_schemes.py                       # full run
    python backend/scripts/harvest_schemes.py --langs en            # English only
    python backend/scripts/harvest_schemes.py --force               # re-fetch everything

Defaults target 5 req/s. That looks timid next to the ~172 req/s the API will
serve in a burst, and it is deliberate: bursts fit inside their token bucket, so
burst measurements say nothing about a run this long. 40 req/s sustained earns a
429 after ~6 minutes; 8 req/s sustained over an hour collected 213 of them while
only achieving ~4.8 req/s of real throughput, so 5 is roughly where this API
actually lives. The client adapts downward on its own and will not climb back to
a rate that has already been throttled — see `client._Pacer`.
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# The KB is full of Devanagari/Bengali; Windows cp1252 would kill the process.
sys.stdout.reconfigure(encoding="utf-8")

from ingestion.apikey import get_api_key            # noqa: E402
from ingestion.client import MySchemeClient          # noqa: E402
from ingestion.harvest import DEFAULT_LANGS, Harvester, OUT_ROOT  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--stage", choices=("index", "details", "all"), default="all")
    p.add_argument("--langs", default=",".join(DEFAULT_LANGS),
                   help="comma-separated translation languages besides English, "
                        "or 'en' for English only (default: %(default)s)")
    p.add_argument("--limit", type=int, default=None,
                   help="only harvest the first N schemes (smoke test)")
    p.add_argument("--rps", type=float, default=5.0,
                   help="target request rate; the client backs off below this "
                        "on its own if the API throttles (default: %(default)s)")
    p.add_argument("--floor-rps", type=float, default=1.5,
                   help="slowest the adaptive pacer may go before giving up")
    p.add_argument("--concurrency", type=int, default=4,
                   help="max in-flight HTTP requests")
    p.add_argument("--scheme-concurrency", type=int, default=3,
                   help="max schemes processed at once")
    p.add_argument("--force", action="store_true",
                   help="re-fetch schemes already on disk")
    p.add_argument("--no-documents", action="store_true",
                   help="skip the documents_required sub-resource")
    p.add_argument("--refresh-key", action="store_true",
                   help="harvest a fresh x-api-key before starting")
    p.add_argument("--out", default=str(OUT_ROOT))
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    langs = [l.strip() for l in args.langs.split(",") if l.strip() and l.strip() != "en"]

    key = await get_api_key(force_refresh=args.refresh_key)
    print(f"[auth] x-api-key {key[:6]}...{key[-4:]}")
    print(f"[rate] target {args.rps} req/s (adaptive, floor {args.floor_rps}), "
          f"concurrency {args.concurrency}, "
          f"{args.scheme_concurrency} schemes in flight")

    started = time.perf_counter()
    async with MySchemeClient(
        key,
        rps=args.rps,
        floor_rps=args.floor_rps,
        concurrency=args.concurrency,
        # If the key rotates mid-run, re-harvest it instead of dying at scheme 3,000.
        on_auth_rejected=lambda: get_api_key(force_refresh=True),
    ) as client:
        h = Harvester(
            client,
            langs=langs,
            out_root=Path(args.out),
            scheme_concurrency=args.scheme_concurrency,
            force=args.force,
            fetch_documents=not args.no_documents,
        )

        if args.stage in ("index", "all"):
            items = await h.harvest_index()
        else:
            items = h.load_index()
            print(f"[index] loaded {len(items)} slugs from disk")

        if args.stage in ("details", "all"):
            await h.harvest_schemes(items, limit=args.limit)
            h.write_report(time.perf_counter() - started)
        else:
            print(f"[done] index stage only, {time.perf_counter()-started:.1f}s")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
