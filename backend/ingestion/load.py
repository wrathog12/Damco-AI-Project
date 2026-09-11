"""
Pass A, stage 3 — harvested JSON on disk → Postgres.

Reads nothing from the network. That separation is the point: the API is a
government service with a token-bucket rate limit we can trip in six minutes, so
normalisation and loading must be re-runnable as often as we like without
touching it. Fix a parser bug, re-run this, done — no re-crawl.

Change detection is by `content_hash` (sha256 of the canonical English block):

    hash unchanged  ->  touch `last_seen_at` only, skip the column writes
    hash changed    ->  rewrite every column, re-load translations
    not in harvest  ->  set `delisted_at` (v1's knowledge base could only grow)

Translations are upserted on `(scheme_pk, lang)` and stamped with
`source_content_hash`, so a scheme whose English text changed can be spotted as
having stale translations rather than silently serving them forever.
"""
import json
import sys
import time
from pathlib import Path
from typing import Iterable, Iterator

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from db.session import session_scope
from models import Scheme, SchemeTranslation

from .harvest import DEFAULT_LANGS, OUT_ROOT
from .normalize import build_scheme, build_translation, validate_scheme

# Columns that on-conflict must overwrite. Excludes the primary key, the
# conflict target, and `created_at` (which must keep its original value).
_IMMUTABLE = {"id", "scheme_id", "created_at"}


def iter_harvested(schemes_dir: Path) -> Iterator[dict]:
    """Yield each harvested record. Skips unreadable files rather than aborting.

    A single truncated file — the harvest can be killed mid-write — must not
    cost us the other 4,732 schemes.
    """
    for path in sorted(schemes_dir.glob("*.json")):
        try:
            yield json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            print(f"[load] unreadable {path.name}: {type(exc).__name__}: {exc}")


class Loader:
    def __init__(
        self,
        *,
        out_root: Path = OUT_ROOT,
        langs: Iterable[str] = DEFAULT_LANGS,
        batch_size: int = 200,
        mark_delisted: bool = True,
        force: bool = False,
    ) -> None:
        self.out_root = out_root
        self.schemes_dir = out_root / "schemes"
        self.langs = tuple(langs)
        self.batch_size = batch_size
        self.mark_delisted = mark_delisted
        self.force = force
        self.counts: dict[str, int] = {
            "read": 0, "inserted": 0, "updated": 0, "unchanged": 0,
            "invalid": 0, "translations": 0, "delisted": 0, "revived": 0,
        }
        self.problems: list[tuple[str, list[str]]] = []

    def _bump(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    # ── normalise + validate before anything touches the database ───────
    def _prepare(self, raw: dict) -> tuple[dict, list[dict]] | None:
        try:
            record = build_scheme(raw)
        except Exception as exc:                      # noqa: BLE001 — one bad
            slug = raw.get("slug", "?")               # record must not kill the run
            self.problems.append((slug, [f"{type(exc).__name__}: {exc}"]))
            self._bump("invalid")
            return None

        problems = validate_scheme(record)
        if problems:
            # Nothing unvalidated reaches Postgres — the specific gap in v1,
            # where Gemini's output was written to disk unchecked.
            self.problems.append((record.get("scheme_id", "?"), problems))
            self._bump("invalid")
            return None

        rows = []
        for lang in self.langs:
            row = build_translation(raw, lang)
            if row:
                rows.append(row)
        return record, rows

    # ── the upsert ──────────────────────────────────────────────────────
    async def _flush(self, session, batch: list[tuple[dict, list[dict]]],
                     now) -> None:
        if not batch:
            return

        records = [rec for rec, _ in batch]
        ids = [r["scheme_id"] for r in records]

        # One round-trip to learn which of these we already have, and at which
        # revision — so `unchanged` rows can skip the column writes entirely.
        existing = {
            row.scheme_id: (row.id, row.content_hash, row.delisted_at)
            for row in (await session.execute(
                select(Scheme.scheme_id, Scheme.id, Scheme.content_hash,
                       Scheme.delisted_at)
                .where(Scheme.scheme_id.in_(ids))
            )).all()
        }

        to_write: list[dict] = []
        touch_only: list[str] = []
        for record, _ in batch:
            prior = existing.get(record["scheme_id"])
            if prior and prior[1] == record["content_hash"] and not self.force:
                self._bump("unchanged")
                touch_only.append(record["scheme_id"])
                if prior[2] is not None:
                    self._bump("revived")   # reappeared after being de-listed
                continue
            self._bump("updated" if prior else "inserted")
            to_write.append({**record, "last_seen_at": now, "delisted_at": None})

        if touch_only:
            # Cheap path: prove the scheme still exists upstream without
            # rewriting ~40 columns of identical content.
            await session.execute(
                update(Scheme)
                .where(Scheme.scheme_id.in_(touch_only))
                .values(last_seen_at=now, delisted_at=None)
            )

        if to_write:
            stmt = insert(Scheme).values(to_write)
            stmt = stmt.on_conflict_do_update(
                index_elements=[Scheme.scheme_id],
                set_={c.name: stmt.excluded[c.name]
                      for c in Scheme.__table__.columns
                      if c.name not in _IMMUTABLE},
            ).returning(Scheme.scheme_id, Scheme.id)
            for scheme_id, pk in (await session.execute(stmt)).all():
                existing[scheme_id] = (pk, None, None)

        # Translations, only for schemes whose content we just (re)wrote —
        # unchanged English means the cached translations are still current.
        written = {r["scheme_id"] for r in to_write}
        payloads = []
        for record, rows in batch:
            if record["scheme_id"] not in written:
                continue
            pk = existing[record["scheme_id"]][0]
            for row in rows:
                payloads.append({**row, "scheme_pk": pk, "fetched_at": now})

        if payloads:
            tstmt = insert(SchemeTranslation).values(payloads)
            tstmt = tstmt.on_conflict_do_update(
                constraint="uq_scheme_translation_lang",
                set_={c.name: tstmt.excluded[c.name]
                      for c in SchemeTranslation.__table__.columns
                      if c.name not in {"id", "scheme_pk", "lang"}},
            )
            await session.execute(tstmt)
            self._bump("translations", len(payloads))

        batch.clear()

    async def run(self) -> dict:
        if not self.schemes_dir.exists():
            raise FileNotFoundError(
                f"{self.schemes_dir} missing — run harvest_schemes.py first")

        started = time.perf_counter()
        now = func.now()          # one server-side clock, not the client's
        seen_ids: set[str] = set()
        batch: list[tuple[dict, list[dict]]] = []

        async with session_scope() as session:
            for raw in iter_harvested(self.schemes_dir):
                self._bump("read")
                prepared = self._prepare(raw)
                if prepared is None:
                    continue
                seen_ids.add(prepared[0]["scheme_id"])
                batch.append(prepared)
                if len(batch) >= self.batch_size:
                    await self._flush(session, batch, now)
                    print(f"\r[load] {self.counts['read']} read  "
                          f"+{self.counts['inserted']} new  "
                          f"~{self.counts['updated']} changed  "
                          f"={self.counts['unchanged']} same  "
                          f"!{self.counts['invalid']} invalid",
                          end="", flush=True)
            await self._flush(session, batch, now)
            print()

            if self.mark_delisted and seen_ids:
                # Only meaningful after a *complete* harvest; a partial run would
                # de-list everything it hadn't got to yet, hence the CLI guard.
                result = await session.execute(
                    update(Scheme)
                    .where(Scheme.scheme_id.not_in(seen_ids),
                           Scheme.delisted_at.is_(None))
                    .values(delisted_at=now)
                )
                self._bump("delisted", result.rowcount or 0)

        self.counts["wall_seconds"] = round(time.perf_counter() - started, 1)
        return self.counts

    def report(self) -> None:
        print("\n-- Load report -----------------------------------")
        for key in ("read", "inserted", "updated", "unchanged", "revived",
                    "translations", "delisted", "invalid"):
            print(f"  {key:<14} {self.counts.get(key, 0)}")
        print(f"  wall time      {self.counts.get('wall_seconds', 0)}s")
        if self.problems:
            print(f"\n  {len(self.problems)} record(s) rejected; first 10:")
            for slug, problems in self.problems[:10]:
                print(f"    {slug}: {'; '.join(problems[:3])}")
            path = self.out_root / "load_problems.json"
            path.write_text(json.dumps(
                [{"scheme_id": s, "problems": p} for s, p in self.problems],
                ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"  full list -> {path}")


if __name__ == "__main__":  # pragma: no cover
    print("Run via: python backend/scripts/load_schemes.py", file=sys.stderr)
