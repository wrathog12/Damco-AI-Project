"""
Load harvested schemes from disk into Postgres.

    python backend/scripts/load_schemes.py                  # incremental
    python backend/scripts/load_schemes.py --dry-run         # normalise only
    python backend/scripts/load_schemes.py --force           # rewrite every row
    python backend/scripts/load_schemes.py --mark-delisted   # after a FULL harvest

Safe to re-run: unchanged schemes only get `last_seen_at` touched.

`--mark-delisted` is opt-in on purpose. It stamps `delisted_at` on every scheme
in the database that this run did not see, which is correct after a complete
harvest and destructive after a partial one — and the harvest is resumable, so
partial runs are the normal case.
"""
import argparse
import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# The corpus is full of Devanagari/Bengali; Windows cp1252 would kill the process.
sys.stdout.reconfigure(encoding="utf-8")

from db.session import dispose_engine                          # noqa: E402
from ingestion.harvest import DEFAULT_LANGS, OUT_ROOT           # noqa: E402
from ingestion.load import Loader, iter_harvested              # noqa: E402
from ingestion.normalize import build_scheme, validate_scheme  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--langs", default=",".join(DEFAULT_LANGS),
                   help="translation languages to load (default: %(default)s)")
    p.add_argument("--dry-run", action="store_true",
                   help="normalise and validate only; no database connection")
    p.add_argument("--force", action="store_true",
                   help="rewrite rows even when content_hash is unchanged")
    p.add_argument("--mark-delisted", action="store_true",
                   help="stamp delisted_at on schemes absent from this run "
                        "(only correct after a complete harvest)")
    p.add_argument("--batch-size", type=int, default=200)
    p.add_argument("--out", default=str(OUT_ROOT))
    return p.parse_args()


def dry_run(out_root: Path) -> int:
    """Exercise the whole normalisation layer with no database at all."""
    total = ok = 0
    problems: list[tuple[str, list[str]]] = []
    for raw in iter_harvested(out_root / "schemes"):
        total += 1
        try:
            record = build_scheme(raw)
        except Exception as exc:                       # noqa: BLE001
            problems.append((raw.get("slug", "?"), [f"{type(exc).__name__}: {exc}"]))
            continue
        found = validate_scheme(record)
        if found:
            problems.append((record["scheme_id"], found))
        else:
            ok += 1

    print(f"[dry-run] {ok}/{total} records valid")
    for slug, found in problems[:20]:
        print(f"  {slug}: {'; '.join(found[:3])}")
    if len(problems) > 20:
        print(f"  ... and {len(problems) - 20} more")
    return 1 if problems else 0


async def main() -> int:
    args = parse_args()
    out_root = Path(args.out)

    if args.dry_run:
        return dry_run(out_root)

    langs = [l.strip() for l in args.langs.split(",") if l.strip() and l.strip() != "en"]
    loader = Loader(
        out_root=out_root,
        langs=langs,
        batch_size=args.batch_size,
        mark_delisted=args.mark_delisted,
        force=args.force,
    )
    try:
        await loader.run()
        loader.report()
    finally:
        await dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
