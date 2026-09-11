"""
P1.a — what does the API do for a scheme that has NOT been translated?

Open question: `lang=hi` worked on Stand-Up India (a flagship central scheme,
almost certainly hand-translated). Obscure state schemes are the likely gap.
This script establishes the *failure mode* — error, missing key, empty body, or
silent English fallback — because each demands different ingestion code.

Silent English fallback is the dangerous case: it would write English prose into
the Hindi row and the voice agent would read it out in the wrong language.

Method: sample slugs from across the whole 4,772 corpus (not just page 1, where
central flagship schemes cluster), fetch each in en/hi/bn/mr/ta, and classify by
script content rather than by HTTP status.

Run:  python scripts/probe_myscheme_translations.py
"""
import asyncio
import json
import sys
import unicodedata
from collections import Counter
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

LANGS = ("hi", "bn", "mr", "ta")
CONCURRENCY = 8  # well under the measured ceiling

# Unicode blocks each language must be written in, if genuinely translated.
SCRIPT_RANGES = {
    "hi": ((0x0900, 0x097F),),               # Devanagari
    "mr": ((0x0900, 0x097F),),               # Devanagari
    "bn": ((0x0980, 0x09FF),),               # Bengali
    "ta": ((0x0B80, 0x0BFF),),               # Tamil
}

# Sample offsets spread across all 478 pages so we see state schemes too.
SAMPLE_OFFSETS = (0, 600, 1200, 1800, 2400, 3000, 3600, 4200, 4700)
PER_OFFSET = 6


def load_headers() -> dict:
    reqs = json.loads(CAPTURED.read_text(encoding="utf-8"))
    return {k: v for k, v in reqs[0]["headers"].items()
            if k.lower() not in DROP_HEADERS}


def script_ratio(text: str, lang: str) -> float:
    """Fraction of letters that belong to the language's own script."""
    ranges = SCRIPT_RANGES[lang]
    letters = [c for c in text if unicodedata.category(c).startswith("L")]
    if not letters:
        return 0.0
    hits = sum(1 for c in letters
               if any(lo <= ord(c) <= hi for lo, hi in ranges))
    return hits / len(letters)


def extract_text(block: dict) -> str:
    """Concatenate the fields we actually serve to users."""
    bd = block.get("basicDetails") or {}
    sc = block.get("schemeContent") or {}
    ec = block.get("eligibilityCriteria") or {}
    parts = [
        bd.get("schemeName") or "",
        sc.get("briefDescription") or "",
        sc.get("detailedDescription_md") or "",
        sc.get("benefits_md") or "",
        ec.get("eligibilityDescription_md") or "",
    ]
    return "\n".join(p for p in parts if p)


def classify(status, payload, lang: str, en_text: str) -> tuple[str, float]:
    """Return (verdict, script_ratio)."""
    if status != 200:
        return f"HTTP_{status}", 0.0
    data = (payload or {}).get("data")
    if not isinstance(data, dict):
        return "NO_DATA", 0.0
    if lang not in data:
        return "LANG_KEY_MISSING", 0.0
    text = extract_text(data[lang])
    if not text.strip():
        return "EMPTY_BODY", 0.0
    ratio = script_ratio(text, lang)
    if ratio >= 0.60:
        return "TRANSLATED", ratio
    if en_text and text.strip()[:200] == en_text.strip()[:200]:
        return "SILENT_EN_FALLBACK", ratio
    if ratio < 0.15:
        return "LATIN_TEXT", ratio          # English-ish but not byte-identical
    return "PARTIAL", ratio


async def get_json(client, params) -> tuple:
    try:
        r = await client.get(DETAIL, params=params)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, None
    except Exception as e:
        return type(e).__name__, None


async def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    hdrs = load_headers()
    sem = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient(headers=hdrs, timeout=60.0) as client:
        # ---- gather a spread-out sample of slugs ----------------------
        print(f"[1] Sampling {PER_OFFSET} slugs at each of {len(SAMPLE_OFFSETS)} "
              f"offsets across 4,772 schemes")
        slugs: list[tuple[str, str, int]] = []   # (slug, state, offset)
        for off in SAMPLE_OFFSETS:
            r = await client.get(SEARCH, params={
                "lang": "en", "q": "[]", "keyword": "", "sort": "",
                "from": str(off), "size": str(PER_OFFSET)})
            for it in r.json()["data"]["hits"]["items"]:
                f = it["fields"]
                state = (f.get("beneficiaryState") or ["-"])
                slugs.append((f["slug"],
                              state[0] if isinstance(state, list) else str(state),
                              off))
        print(f"    got {len(slugs)} slugs")

        # ---- fetch en + every language for each -----------------------
        print(f"\n[2] Fetching en + {'/'.join(LANGS)} for each (c={CONCURRENCY})")

        async def one(slug, lang):
            async with sem:
                return await get_json(client, {"slug": slug, "lang": lang})

        en_results = await asyncio.gather(*(one(s, "en") for s, _, _ in slugs))
        en_texts = {}
        for (slug, _, _), (st, pl) in zip(slugs, en_results):
            blk = ((pl or {}).get("data") or {}).get("en") or {}
            en_texts[slug] = extract_text(blk) if st == 200 else ""

        rows = []
        for lang in LANGS:
            res = await asyncio.gather(*(one(s, lang) for s, _, _ in slugs))
            for (slug, state, off), (st, pl) in zip(slugs, res):
                verdict, ratio = classify(st, pl, lang, en_texts[slug])
                rows.append({"slug": slug, "state": state, "offset": off,
                             "lang": lang, "verdict": verdict,
                             "script_ratio": round(ratio, 3)})

    # ---- report ------------------------------------------------------
    print("\n[3] Verdict counts per language")
    print(f"    {'lang':6} " + "  ".join(f"{v:<19}" for v in
          ("TRANSLATED", "other verdicts")))
    for lang in LANGS:
        sub = [r for r in rows if r["lang"] == lang]
        c = Counter(r["verdict"] for r in sub)
        tr = c.pop("TRANSLATED", 0)
        print(f"    {lang:6} {tr:>3}/{len(sub):<3} translated   "
              f"{dict(c) if c else 'no other verdicts'}")

    print("\n[4] Any non-TRANSLATED cases (the fallback behaviour)")
    bad = [r for r in rows if r["verdict"] != "TRANSLATED"]
    if not bad:
        print("    none — every sampled scheme was genuinely translated in every language")
    else:
        for r in bad[:30]:
            print(f"    {r['lang']}  {r['verdict']:<19} ratio={r['script_ratio']:<5} "
                  f"off={r['offset']:<5} {r['state'][:18]:<18} {r['slug'][:40]}")
        print(f"    ({len(bad)} total)")

    print("\n[5] Deliberately bogus slug — the true error shape")
    async with httpx.AsyncClient(headers=hdrs, timeout=30.0) as client:
        for slug, lang in (("this-scheme-does-not-exist-xyz", "hi"), ("sui", "zz")):
            st, pl = await get_json(client, {"slug": slug, "lang": lang})
            body = json.dumps(pl, ensure_ascii=False)[:200] if pl else "<non-json>"
            print(f"    slug={slug[:32]:<34} lang={lang:3} HTTP {st}  {body}")

    (OUT_DIR / "translation_coverage.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsaved -> {OUT_DIR / 'translation_coverage.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
