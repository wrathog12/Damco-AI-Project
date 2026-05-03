"""
test.py — Measure latency of each pipeline stage independently.

Usage:
    cd backend
    python test.py

Tests (runs in sequence):
  1. KB load time
  2. Tool execution time (search, details, eligibility)
  3. LLM response time (with tool calling)
  4. STT time (mock audio)
  5. TTS time (sample text)
  6. Full pipeline roundtrip estimate

Requires: .env with valid API keys.
"""
import json
import time
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def measure(label: str, fn, *args, **kwargs):
    """Run a function and print its execution time."""
    t0 = time.perf_counter()
    result = fn(*args, **kwargs)
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  {label:40s}  {elapsed:>8.1f} ms")
    return result, elapsed


def divider(title: str):
    print(f"\n{'-' * 60}")
    print(f"  {title}")
    print(f"{'-' * 60}")


def main():
    timings = {}

    # ════════════════════════════════════════════════════
    # 1. Knowledge Base Loading
    # ════════════════════════════════════════════════════
    divider("1. Knowledge Base Loading")

    from config import settings
    from knowledge import loader

    _, t = measure("Load KB JSON", loader.load, settings.kb_path)
    timings["kb_load"] = t

    stats = loader.get_stats()
    print(f"  -> {stats['total_schemes']} schemes, {len(stats['states'])} states, {len(stats['categories'])} categories")

    # ════════════════════════════════════════════════════
    # 2. Tool Execution (no API calls — pure Python)
    # ════════════════════════════════════════════════════
    divider("2. Tool Execution (in-memory)")

    from tools.search import search_schemes
    from tools.details import get_scheme_details
    from tools.eligibility import check_eligibility

    result, t = measure("search_schemes(state='Bihar')", search_schemes, state="Bihar")
    timings["tool_search"] = t
    parsed = json.loads(result)
    print(f"  -> {parsed.get('matches', 0)} matches")

    result, t = measure("search_schemes(category='Education')", search_schemes, category="Education & Learning")
    timings["tool_search_cat"] = t
    parsed = json.loads(result)
    print(f"  -> {parsed.get('matches', 0)} matches")

    result, t = measure("search_schemes(state='Bihar', occ='Farmer')", search_schemes, state="Bihar", occupation="Farmer")
    timings["tool_search_multi"] = t
    parsed = json.loads(result)
    print(f"  -> {parsed.get('matches', 0)} matches")

    # Get a real scheme_id from search results
    search_result = json.loads(search_schemes(state="Bihar"))
    if search_result.get("schemes"):
        test_id = search_result["schemes"][0]["scheme_id"]
    else:
        test_id = "bihar_education_student_credit_card"

    result, t = measure(f"get_scheme_details('{test_id}')", get_scheme_details, test_id)
    timings["tool_details"] = t
    print(f"  -> {len(result)} chars returned")

    result, t = measure(f"check_eligibility(age=22, state='Bihar')", check_eligibility, test_id, user_age=22, user_state="Bihar")
    timings["tool_eligibility"] = t
    parsed = json.loads(result)
    print(f"  -> eligible: {parsed.get('eligible')}")

    # ════════════════════════════════════════════════════
    # 3. LLM (Groq) — requires API key
    # ════════════════════════════════════════════════════
    divider("3. LLM (Groq Llama 3.3)")

    if not settings.groq_api_key or settings.groq_api_key.startswith("gsk_xxx"):
        print("  [SKIP] GROQ_API_KEY not set, skipping LLM test")
        timings["llm_simple"] = None
        timings["llm_tool_call"] = None
    else:
        from voice import llm

        # Simple response (no tools)
        _, t = measure(
            "Simple greeting",
            llm.chat,
            user_message="Hello, what can you help me with?",
            conversation_history=[],
            detected_language="en",
        )
        timings["llm_simple"] = t

        # With tool calling
        try:
            _, t = measure(
                "Tool call: 'Bihar mein farmer schemes'",
                llm.chat,
                user_message="Bihar mein farmer ke liye koi scheme hai?",
                conversation_history=[],
                detected_language="hi",
            )
            timings["llm_tool_call"] = t
        except Exception as e:
            print(f"  [ERROR] Tool call failed: {e}")
            timings["llm_tool_call"] = None

    # ════════════════════════════════════════════════════
    # 4. STT (Deepgram) — requires API key
    # ════════════════════════════════════════════════════
    divider("4. STT (Deepgram Nova-3)")

    if not settings.deepgram_api_key or settings.deepgram_api_key.startswith("xxx"):
        print("  [SKIP] DEEPGRAM_API_KEY not set, skipping STT test")
        timings["stt"] = None
    else:
        import numpy as np
        from voice import stt

        # Generate 2 seconds of fake audio (silence with some noise)
        fake_audio = (np.random.randn(48000 * 2) * 100).astype(np.int16)

        _, t = measure("Transcribe 2s audio (will be empty)", stt.transcribe, fake_audio, 48000)
        timings["stt"] = t

    # ════════════════════════════════════════════════════
    # 5. TTS (Cartesia) — requires API key
    # ════════════════════════════════════════════════════
    divider("5. TTS (Cartesia Sonic)")

    if not settings.cartesia_api_key or settings.cartesia_api_key.startswith("xxx"):
        print("  [SKIP] CARTESIA_API_KEY not set, skipping TTS test")
        timings["tts_short"] = None
        timings["tts_hindi"] = None
    else:
        from voice import tts

        _, t = measure(
            "Short English text",
            tts.synthesize,
            "Hello, I found 3 schemes for you.",
        )
        timings["tts_short"] = t

        _, t = measure(
            "Hindi text",
            tts.synthesize,
            "Maine aapke liye 3 yojanaen dhundhi hain.",
        )
        timings["tts_hindi"] = t

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

    # Estimate full roundtrip
    stt_t = timings.get("stt") or 400  # estimate if skipped
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
    main()
