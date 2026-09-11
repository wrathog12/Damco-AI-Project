"""
test.py — Measure latency of each pipeline stage independently.

Usage:
    cd backend
    python test.py

Tests (runs in sequence):
  1. Data layer warm-up (Postgres pool + Qdrant client + embedder)
  2. Tool execution time (search, details, eligibility)
  3. LLM response time (with tool calling)
  4. STT time (mock audio)
  5. TTS time (sample text)
  6. Full pipeline roundtrip estimate

Requires: .env with valid API keys.

This is the repo's only per-stage timing tool, and P5's verification compares
against the numbers it produces — so keep it working and keep the output format
stable. Note that stages 4 and 5 measure the *batch* STT/TTS wrappers, not the
streaming services the live pipeline now uses: they are the pre-migration
baseline, which is exactly their value. End-to-end voice latency is measured with
Pipecat's own metrics (`PipelineParams(enable_metrics=True)`), not here.
"""
import asyncio
import os
import sys
import time

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings                                    # noqa: E402


def measure(label: str, fn, *args, **kwargs):
    """Run a sync function and print its execution time."""
    t0 = time.perf_counter()
    result = fn(*args, **kwargs)
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  {label:40s}  {elapsed:>8.1f} ms")
    return result, elapsed


async def ameasure(label: str, coro):
    """Same, for an awaitable. The whole tool path is async as of P2."""
    t0 = time.perf_counter()
    result = await coro
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  {label:40s}  {elapsed:>8.1f} ms")
    return result, elapsed


def divider(title: str):
    print(f"\n{'-' * 60}")
    print(f"  {title}")
    print(f"{'-' * 60}")


async def main():
    timings = {}

    # ════════════════════════════════════════════════════
    # 1. Data layer warm-up
    # ════════════════════════════════════════════════════
    # P1 replaced the in-memory JSON knowledge base with Postgres + Qdrant, so
    # what used to be "load a file" is now "open a pool and a client". Measured
    # separately because it is a one-off cost the first caller must not pay —
    # `main.py`'s lifespan does the same warm-up at boot.
    divider("1. Data layer warm-up")

    from services import resources as resource_factory
    from services import schemes as scheme_service

    res, t = await ameasure("Open pool + Qdrant client",
                            resource_factory.create())
    timings["data_open"] = t

    stats, t = await ameasure("First query (warms the caches)",
                              scheme_service.stats(res))
    timings["data_warmup"] = t
    print(f"  -> {stats['total_schemes']} schemes, "
          f"{stats['indexed_schemes']} indexed, "
          f"{len(stats['states'])} states, {len(stats['categories'])} categories")

    try:
        # ════════════════════════════════════════════════
        # 2. Tool Execution
        # ════════════════════════════════════════════════
        # No longer "in-memory": each search is an embedding round-trip plus a
        # Qdrant query plus a Postgres join. Expect ~600 ms where v1 measured
        # ~1 ms — that regression is known, measured, and P5's problem.
        divider("2. Tool Execution (Postgres + Qdrant)")

        from tools.details import get_scheme_details
        from tools.eligibility import check_eligibility
        from tools.search import search_schemes

        result, t = await ameasure("search_schemes(state='Bihar')",
                                   search_schemes(res, state="Bihar"))
        timings["tool_search"] = t
        print(f"  -> {result.get('matches', 0)} matches")

        result, t = await ameasure(
            "search_schemes(category='Education')",
            search_schemes(res, category="Education & Learning"))
        timings["tool_search_cat"] = t
        print(f"  -> {result.get('matches', 0)} matches")

        result, t = await ameasure(
            "search_schemes(query=..., state, occupation)",
            search_schemes(res, query="help for farmers after crop loss",
                           state="Bihar", occupation="Farmer"))
        timings["tool_search_multi"] = t
        print(f"  -> {result.get('matches', 0)} matches")

        # Use a real scheme_id from the results rather than a hardcoded one —
        # the corpus is rebuilt by ingestion and ids change.
        found = await search_schemes(res, state="Bihar")
        schemes = found.get("schemes") or []
        test_id = (schemes[0]["scheme_id"] if schemes
                   else "bihar_education_student_credit_card")

        result, t = await ameasure(f"get_scheme_details('{test_id[:24]}')",
                                   get_scheme_details(res, scheme_id=test_id))
        timings["tool_details"] = t
        print(f"  -> {len(result)} fields returned")

        result, t = await ameasure(
            "check_eligibility(age=22, state='Bihar')",
            check_eligibility(res, scheme_id=test_id, user_age=22,
                              user_state="Bihar"))
        timings["tool_eligibility"] = t
        print(f"  -> eligible: {result.get('eligible')}")

        # ════════════════════════════════════════════════
        # 3. LLM (Groq) — requires API key
        # ════════════════════════════════════════════════
        divider(f"3. LLM (Groq {settings.llm_model})")

        if not settings.groq_api_key or settings.groq_api_key.startswith("gsk_xxx"):
            print("  [SKIP] GROQ_API_KEY not set, skipping LLM test")
            timings["llm_simple"] = None
            timings["llm_tool_call"] = None
        else:
            from tools.events import EventCollector
            from tools.registry import ToolContext
            from voice import llm

            ctx = ToolContext(resources=res, deliver=EventCollector())

            _, t = await ameasure(
                "Simple greeting",
                llm.chat(ctx, user_message="Hello, what can you help me with?",
                         conversation_history=[], detected_language="en"))
            timings["llm_simple"] = t

            try:
                _, t = await ameasure(
                    "Tool call: 'Bihar mein farmer schemes'",
                    llm.chat(ctx,
                             user_message="Bihar mein farmer ke liye koi scheme hai?",
                             conversation_history=[], detected_language="hi"))
                timings["llm_tool_call"] = t
            except Exception as exc:                          # noqa: BLE001
                print(f"  [ERROR] Tool call failed: {exc}")
                timings["llm_tool_call"] = None

            await llm.close_client()

        # ════════════════════════════════════════════════
        # 4. STT (Deepgram) — requires API key
        # ════════════════════════════════════════════════
        divider("4. STT (Deepgram Nova-3, batch — baseline only)")

        if not settings.deepgram_api_key or settings.deepgram_api_key.startswith("xxx"):
            print("  [SKIP] DEEPGRAM_API_KEY not set, skipping STT test")
            timings["stt"] = None
        else:
            import numpy as np

            from voice import stt

            # 2 seconds of noise. Transcribes to nothing; we are timing the
            # round-trip, not the accuracy.
            fake_audio = (np.random.randn(48000 * 2) * 100).astype(np.int16)
            _, t = measure("Transcribe 2s audio (will be empty)",
                           stt.transcribe, fake_audio, 48000)
            timings["stt"] = t

        # ════════════════════════════════════════════════
        # 5. TTS (Cartesia) — requires API key
        # ════════════════════════════════════════════════
        divider("5. TTS (Cartesia Sonic, batch — baseline only)")

        if not settings.cartesia_api_key or settings.cartesia_api_key.startswith("xxx"):
            print("  [SKIP] CARTESIA_API_KEY not set, skipping TTS test")
            timings["tts_short"] = None
            timings["tts_hindi"] = None
        else:
            from voice import tts

            _, t = measure("Short English text", tts.synthesize,
                           "Hello, I found 3 schemes for you.")
            timings["tts_short"] = t

            _, t = measure("Hindi text", tts.synthesize,
                           "Maine aapke liye 3 yojanaen dhundhi hain.")
            timings["tts_hindi"] = t
    finally:
        # Close the Postgres pool and Qdrant client, or asyncpg complains on exit.
        await res.close()

    # ════════════════════════════════════════════════════
    # Summary
    # ════════════════════════════════════════════════════
    divider("SUMMARY — Latency Breakdown")

    print(f"\n  {'Stage':<35s}  {'Time':>10s}")
    print(f"  {'-' * 35}  {'-' * 10}")

    for key, val in timings.items():
        label = key.replace("_", " ").title()
        if val is None:
            print(f"  {label:<35s}  {'SKIPPED':>10s}")
        else:
            print(f"  {label:<35s}  {val:>8.1f} ms")

    stt_t = timings.get("stt") or 400        # estimate if skipped
    llm_t = timings.get("llm_tool_call") or 1500
    tts_t = timings.get("tts_short") or 300

    total = stt_t + llm_t + tts_t
    print(f"\n  {'-' * 35}  {'-' * 10}")
    print(f"  {'Estimated Full Roundtrip':<35s}  {total:>8.1f} ms")
    print(f"\n  Breakdown: STT({stt_t:.0f}) + LLM({llm_t:.0f}) + TTS({tts_t:.0f})")

    target = 2000
    if total < target:
        print(f"\n  [OK] Under {target}ms target -- good for voice!")
    else:
        print(f"\n  [WARN] Over {target}ms target -- may feel laggy in voice")


if __name__ == "__main__":
    asyncio.run(main())
