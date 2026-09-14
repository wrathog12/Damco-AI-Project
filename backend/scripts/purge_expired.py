"""
Run the retention sweep by hand.

The app runs `services/retention.py::purge` from its own lifespan every
`RETENTION_SWEEP_HOURS`, so this script is not how the policy is enforced — it
exists for the three cases where the loop is not enough: proving the sweep works
without waiting a day, running it after shortening a window, and answering "what
is currently past its retention?" during an audit.

    python backend/scripts/purge_expired.py --dry-run   # count, delete nothing
    python backend/scripts/purge_expired.py
"""
import argparse
import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

if hasattr(sys.stdout, "reconfigure"):                          # Windows cp1252
    sys.stdout.reconfigure(encoding="utf-8")

from services import resources as resources_service  # noqa: E402
from services import retention  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description="Apply the retention windows.")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what is past its window without deleting it")
    args = parser.parse_args()

    print("=" * 70)
    print("  RETENTION SWEEP")
    print("=" * 70)
    for key, value in retention.policy().items():
        print(f"  {key:<26} {value}")
    print()

    res = await resources_service.create()
    try:
        before = await retention.pending(res)
        print("-- past its window ------------------------------")
        for key, value in before.items():
            print(f"  {key:<20} {value}")

        if args.dry_run:
            print("\nDry run — nothing deleted.")
            return 0

        print("\n-- sweeping -------------------------------------")
        counts = await retention.purge(res)
        failures = [k for k, v in counts.items() if v < 0]
        for key, value in counts.items():
            print(f"  {key:<20} {'FAILED' if value < 0 else f'{value} deleted'}")

        after = await retention.pending(res)
        # Expected to be zero across the board. Reported rather than asserted:
        # a row created between the two counts is a legitimate non-zero, and a
        # script that failed the sweep over a race would be noise.
        print("\n-- remaining ------------------------------------")
        for key, value in after.items():
            print(f"  {key:<20} {value}")

        if failures:
            print(f"\nFAILED steps: {', '.join(failures)} — see the log above.")
            return 1
        print("\nSweep complete.")
        return 0
    finally:
        await res.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
