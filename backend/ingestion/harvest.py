"""
Pass A, stage 1 — pull the raw corpus from the API onto disk.

Deliberately *only* fetches. Normalisation into the canonical schema and the
Postgres write are separate stages, and embedding is a separate pass entirely,
so that none of them can force a re-crawl of a government API when they change.

Layout produced under `scraped_data/api/`:

    index.json              the whole scheme index (metadata + slugs)
    schemes/<slug>.json     one file per scheme, every language, + documents
    errors.jsonl            append-only failure log, one JSON object per line
    harvest_report.json     counts, timings, translation coverage

Resumability is by **file existence per slug** — an O(1) check. (The v1 scraper
re-read every file in `processed/` for every URL, which was O(urls x files);
that is the specific thing this replaces.)
"""
import asyncio
import hashlib
import json
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Iterable

from .client import (MAX_PAGE_SIZE, MySchemeClient, PermanentApiError,
                     SchemeApiError, language_of)

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
OUT_ROOT = REPO_ROOT / "scraped_data" / "api"

DEFAULT_LANGS = ("hi", "bn", "mr")   # English is always fetched as the canonical record

# Unicode blocks a genuine translation must be written in. Used to catch the
# silent English fallback, which no status code would reveal.
SCRIPT_RANGES = {
    "hi": ((0x0900, 0x097F),),   # Devanagari
    "mr": ((0x0900, 0x097F),),
    "bn": ((0x0980, 0x09FF),),   # Bengali
    "ta": ((0x0B80, 0x0BFF),),   # Tamil
    "te": ((0x0C00, 0x0C7F),),
    "kn": ((0x0C80, 0x0CFF),),
    "ml": ((0x0D00, 0x0D7F),),
    "gu": ((0x0A80, 0x0AFF),),
    "pa": ((0x0A00, 0x0A7F),),
    "or": ((0x0B00, 0x0B7F),),
    "as": ((0x0980, 0x09FF),),
    "ur": ((0x0600, 0x06FF),),
}

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(slug: str) -> str:
    """Slugs come from an external system; never trust them as paths."""
    cleaned = _UNSAFE_FILENAME.sub("-", slug).strip("-.") or "unnamed"
    if len(cleaned) > 120:
        # Keep it readable but collision-free.
        cleaned = f"{cleaned[:100]}-{hashlib.sha256(slug.encode()).hexdigest()[:12]}"
    return cleaned


def _script_ratio(text: str, lang: str) -> float:
    """Fraction of letters written in the language's own script."""
    ranges = SCRIPT_RANGES.get(lang)
    if not ranges:
        return 1.0                     # unknown script: cannot judge, don't flag
    letters = [c for c in text if unicodedata.category(c).startswith("L")]
    if not letters:
        return 0.0
    hits = sum(1 for c in letters if any(lo <= ord(c) <= hi for lo, hi in ranges))
    return hits / len(letters)


def _user_facing_text(block: dict) -> str:
    """The fields we would actually read aloud or render on a card."""
    bd = block.get("basicDetails") or {}
    sc = block.get("schemeContent") or {}
    ec = block.get("eligibilityCriteria") or {}
    parts = (
        bd.get("schemeName") or "",
        sc.get("briefDescription") or "",
        sc.get("detailedDescription_md") or "",
        sc.get("benefits_md") or "",
        ec.get("eligibilityDescription_md") or "",
    )
    return "\n".join(p for p in parts if p)


def classify_translation(block: dict | None, lang: str) -> str:
    """TRANSLATED / EN_FALLBACK / EMPTY / MISSING — see client.language_of."""
    if block is None:
        return "MISSING"
    text = _user_facing_text(block)
    if not text.strip():
        return "EMPTY"
    return "TRANSLATED" if _script_ratio(text, lang) >= 0.60 else "EN_FALLBACK"


class Harvester:
    def __init__(
        self,
        client: MySchemeClient,
        *,
        langs: Iterable[str] = DEFAULT_LANGS,
        out_root: Path = OUT_ROOT,
        scheme_concurrency: int = 6,
        force: bool = False,
        fetch_documents: bool = True,
    ) -> None:
        self.client = client
        self.langs = tuple(langs)
        self.all_langs = ("en", *self.langs)
        self.out_root = out_root
        self.schemes_dir = out_root / "schemes"
        self.errors_path = out_root / "errors.jsonl"
        self.force = force
        self.fetch_documents = fetch_documents
        self._sem = asyncio.Semaphore(scheme_concurrency)
        self.counts: dict[str, int] = {}
        self.translation_counts: dict[str, dict[str, int]] = {
            lang: {} for lang in self.langs
        }

    # ── bookkeeping ─────────────────────────────────────────────────────
    def _bump(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def _log_error(self, slug: str, stage: str, exc: BaseException) -> None:
        self.errors_path.parent.mkdir(parents=True, exist_ok=True)
        with self.errors_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": time.time(), "slug": slug, "stage": stage,
                "error": f"{type(exc).__name__}: {exc}",
            }, ensure_ascii=False) + "\n")

    # ── stage 1: the index ──────────────────────────────────────────────
    async def harvest_index(self) -> list[dict]:
        """Walk the whole index at the maximum permitted page size."""
        self.out_root.mkdir(parents=True, exist_ok=True)
        total = await self.client.total_schemes()
        print(f"[index] API reports {total} schemes "
              f"({-(-total // MAX_PAGE_SIZE)} pages of {MAX_PAGE_SIZE})")

        items: list[dict] = []
        seen: set[str] = set()
        raw_count = 0
        duplicate_slugs: list[str] = []
        offset = 0
        while offset < total:
            page = await self.client.search(offset=offset, size=MAX_PAGE_SIZE)
            hits = (page.get("hits") or {}).get("items") or []
            if not hits:
                print(f"[index] empty page at from={offset}; stopping early")
                break
            for hit in hits:
                fields = hit.get("fields") or {}
                slug = fields.get("slug")
                if not slug:
                    continue
                raw_count += 1
                if slug in seen:
                    duplicate_slugs.append(slug)
                    continue
                seen.add(slug)
                # Search hits carry no usable `_id` (verified: always null), so
                # the documents sub-resource key must come from the detail call.
                items.append({"slug": slug, **fields})
            offset += MAX_PAGE_SIZE
            print(f"\r[index] {len(items)} unique / {raw_count} raw of {total}",
                  end="", flush=True)

        print()
        # Their index genuinely contains repeated documents for the same slug
        # (verified: 39 slugs twice over, identical names). That is upstream
        # duplication, not schemes we failed to see — so it is worth naming
        # precisely rather than reporting it as missing data.
        if duplicate_slugs:
            print(f"[index] {len(duplicate_slugs)} duplicate index entries collapsed "
                  f"by slug (upstream duplicates, e.g. {duplicate_slugs[:3]})")
        missing = total - raw_count
        if missing:
            print(f"[index] WARNING: saw {raw_count} of {total} reported items "
                  f"— {missing} genuinely unseen, pagination may have shifted")

        (self.out_root / "index.json").write_text(
            json.dumps({"fetched_at": time.time(), "total_reported": total,
                        "raw_items_seen": raw_count,
                        "duplicate_entries": len(duplicate_slugs),
                        "total_collected": len(items), "items": items},
                       ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"[index] saved -> {self.out_root / 'index.json'}")
        return items

    def load_index(self) -> list[dict]:
        path = self.out_root / "index.json"
        if not path.exists():
            raise FileNotFoundError(f"{path} missing — run the index stage first")
        return json.loads(path.read_text(encoding="utf-8"))["items"]

    # ── stage 2: per-scheme detail ──────────────────────────────────────
    async def _harvest_one(self, entry: dict) -> str:
        """Fetch every language + documents for one scheme. Returns an outcome tag."""
        slug = entry["slug"]
        path = self.schemes_dir / f"{safe_filename(slug)}.json"

        if path.exists() and not self.force:
            return "skipped"

        # `except*` bodies may not contain `return`, so failures set an outcome
        # and we bail out after the block instead.
        outcome: str | None = None
        en: dict | None = None
        scheme_id: str | None = None
        jobs: dict[str, asyncio.Task] = {}

        async with self._sem:
            try:
                # English first: it is canonical, and it supplies the _id that
                # the documents sub-resource is keyed on.
                en = await self.client.detail(slug, "en")
                if en is None:
                    self._log_error(slug, "detail:en", SchemeApiError("data was null"))
                    outcome = "not_found"
                else:
                    scheme_id = en.get("_id")
                    async with asyncio.TaskGroup() as tg:
                        for lang in self.langs:
                            jobs[f"detail:{lang}"] = tg.create_task(
                                self.client.detail(slug, lang))
                        if self.fetch_documents and scheme_id:
                            for lang in self.all_langs:
                                jobs[f"docs:{lang}"] = tg.create_task(
                                    self.client.documents(scheme_id, lang))
            except* PermanentApiError as eg:
                self._log_error(slug, "detail", eg.exceptions[0])
                outcome = "permanent_error"
            except* SchemeApiError as eg:
                self._log_error(slug, "detail", eg.exceptions[0])
                outcome = "failed"
            except* Exception as eg:
                self._log_error(slug, "detail", eg.exceptions[0])
                outcome = "failed"

        if outcome or en is None:
            return outcome or "failed"

        # ── assemble, validating which language we actually received ─────
        record: dict = {
            "slug": slug,
            "scheme_id": scheme_id,
            "fetched_at": time.time(),
            "index_fields": {k: v for k, v in entry.items() if k != "slug"},
            "langs": {},
            "documents": {},
            "translation_status": {},
        }

        en_block = en.get(language_of(en, "en") or "en") or {}
        record["langs"]["en"] = en_block

        for lang in self.langs:
            data = jobs[f"detail:{lang}"].result()
            got = language_of(data, lang) if data else None
            # Only accept the block if the API actually gave us that language.
            block = (data or {}).get(got) if got == lang else None
            verdict = classify_translation(block, lang)
            if verdict != "TRANSLATED" and block is None and data is not None:
                verdict = "EN_FALLBACK"
            record["langs"][lang] = block
            record["translation_status"][lang] = verdict
            counts = self.translation_counts[lang]
            counts[verdict] = counts.get(verdict, 0) + 1

        if self.fetch_documents and scheme_id:
            for lang in self.all_langs:
                data = jobs.get(f"docs:{lang}")
                data = data.result() if data else None
                got = language_of(data, lang) if data else None
                block = (data or {}).get(got) if got == lang else None
                record["documents"][lang] = (block or {}).get("documents_required")

        # Hash the canonical English content only: translations and fetch
        # timestamps must not make an unchanged scheme look changed.
        record["content_hash"] = hashlib.sha256(
            json.dumps(en_block, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

        self.schemes_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        return "fetched"

    async def harvest_schemes(self, items: list[dict], limit: int | None = None) -> None:
        todo = items[:limit] if limit else items
        started = time.perf_counter()
        done = 0

        async def run(entry: dict) -> None:
            nonlocal done
            outcome = await self._harvest_one(entry)
            self._bump(outcome)
            done += 1
            if done % 25 == 0 or done == len(todo):
                elapsed = time.perf_counter() - started
                rate = done / elapsed if elapsed else 0
                eta = (len(todo) - done) / rate if rate else 0
                print(f"\r[detail] {done}/{len(todo)}  {rate:5.2f} schemes/s  "
                      f"ETA {eta/60:5.1f} min  "
                      f"{self.client.current_rps:5.1f} req/s  "
                      f"thr={self.client.stats['throttled']}  "
                      f"{ {k: v for k, v in self.counts.items()} }",
                      end="", flush=True)

        print(f"[detail] {len(todo)} schemes x langs {self.all_langs}"
              f"{' + documents' if self.fetch_documents else ''}")
        # Chunked so we never materialise ~40k coroutines at once. Kept small:
        # every queued coroutine parked on the semaphore still costs a frame, and
        # each running one holds four full scheme payloads plus four document
        # responses in memory until it writes. A 200-wide chunk got OOM-killed.
        chunk = 50
        for start in range(0, len(todo), chunk):
            await asyncio.gather(*(run(e) for e in todo[start:start + chunk]))
        print()

    # ── report ──────────────────────────────────────────────────────────
    def write_report(self, wall_seconds: float) -> dict:
        report = {
            "finished_at": time.time(),
            "wall_seconds": round(wall_seconds, 1),
            "langs": list(self.all_langs),
            "outcomes": self.counts,
            "translation_coverage": self.translation_counts,
            "client_stats": self.client.stats,
        }
        (self.out_root / "harvest_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"\n-- Harvest report --------------------------------")
        print(f"  wall time      {wall_seconds/60:.1f} min")
        print(f"  outcomes       {self.counts}")
        print(f"  requests       {self.client.stats['requests']} "
              f"(retries {self.client.stats['retries']}, "
              f"null data {self.client.stats['null_data']})")
        for lang, counts in self.translation_counts.items():
            total = sum(counts.values()) or 1
            tr = counts.get("TRANSLATED", 0)
            print(f"  {lang} coverage    {tr}/{total} ({100*tr/total:.1f}%) "
                  f"{ {k: v for k, v in counts.items() if k != 'TRANSLATED'} or ''}")
        errs = self.errors_path
        if errs.exists():
            n = sum(1 for _ in errs.open(encoding="utf-8"))
            print(f"  errors logged  {n} -> {errs}")
        print(f"  saved          {self.out_root / 'harvest_report.json'}")
        return report


if __name__ == "__main__":  # pragma: no cover
    print("Run via: python backend/scripts/harvest_schemes.py", file=sys.stderr)
