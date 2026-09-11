"""
Run a fixed query set through the LLM + tool loop and dump the result as JSON.

This is the "diff /chat before and after" gate from the migration plan. The point
is *not* to diff the prose — the model is sampled, so identical input gives
different wording every run. What has to hold across a migration is structural:

  * the same tool gets called, with arguments of the same shape;
  * the tool returns a non-zero number of real schemes rather than an error;
  * the scheme ids the model then talks about are ones the tool actually returned.

So each turn records the tool calls, a summary of every tool result, and the
final text. Run it on both builds and read the two files side by side.

    python backend/scripts/chat_query_set.py --out before.json    # at HEAD
    python backend/scripts/chat_query_set.py --out after.json
    python backend/scripts/chat_query_set.py --compare before.json after.json

Needs GROQ_API_KEY. The v2 build also needs Postgres, Qdrant and GEMINI_API_KEY.
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

sys.stdout.reconfigure(encoding="utf-8")

# Deliberately fixed and committed: a query set that changes between runs cannot
# detect a regression. Chosen to cover each tool path and each language the
# prompt claims to support.
# None of these may be a verbatim few-shot input from `voice/prompts.py`: the
# model recognises its own example and replays the canned answer without calling
# a tool, which tests the prompt rather than the data layer.
QUERIES = [
    ("en", "Show me education schemes in Bihar"),
    ("hi", "Jharkhand mein kisano ke liye kya madad milti hai?"),
    ("hi", "मुझे बेटी की पढ़ाई के लिए स्कॉलरशिप चाहिए"),
    ("en", "I am 25, female, from Maharashtra with income 1 lakh — what can I get?"),
    ("en", "Is there any central government health insurance scheme?"),
    ("en", "Tell me about the Pradhan Mantri Suraksha Bima Yojana"),
    ("bn", "আমি একজন কৃষক, আমার জন্য কোনো প্রকল্প আছে?"),
    ("en", "I need help paying for a house"),
]


def _summarise_tool_result(raw: str) -> dict:
    """Compress a tool result to the parts a regression would show up in."""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {"unparseable": raw[:120]}
    if not isinstance(parsed, dict):
        return {"type": type(parsed).__name__}

    out: dict = {}
    if "error" in parsed:
        out["error"] = parsed["error"]
    if "matches" in parsed:
        out["matches"] = parsed["matches"]
    if "schemes" in parsed:
        out["scheme_ids"] = [s.get("scheme_id") for s in parsed["schemes"]]
        out["scheme_names"] = [s.get("scheme_name") for s in parsed["schemes"]]
        out["states"] = [s.get("state") for s in parsed["schemes"]]
    for key in ("scheme_id", "scheme_name", "eligible", "status", "message"):
        if key in parsed:
            out[key] = parsed[key]
    if "reasons" in parsed:
        out["reasons"] = parsed["reasons"]
    if "top_benefits" in parsed:
        out["n_benefits"] = len(parsed["top_benefits"])
    return out


async def run_set() -> dict:
    from services import resources as resource_factory
    from tools.events import EventCollector
    from tools.registry import ToolContext
    from voice import llm

    res = await resource_factory.create()
    # A collector rather than None: `show_scheme_card` is one of the five tool
    # paths this set is meant to cover, and with no deliver it would still
    # succeed but exercise nothing.
    ctx = ToolContext(resources=res, deliver=EventCollector())

    results = []
    for language, message in QUERIES:
        t0 = time.perf_counter()
        error = None
        text, history = "", []
        try:
            text, history = await llm.chat(ctx, user_message=message,
                                           conversation_history=[],
                                           detected_language=language)
        except Exception as exc:                              # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"

        calls, tool_results = [], []
        for msg in history:
            for call in (msg.get("tool_calls") or []):
                fn = call.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {"unparseable": fn.get("arguments")}
                calls.append({"name": fn.get("name"), "args": args})
            if msg.get("role") == "tool":
                tool_results.append(_summarise_tool_result(msg.get("content") or ""))

        results.append({
            "language": language,
            "message": message,
            "error": error,
            "tool_calls": calls,
            "tool_results": tool_results,
            "response": text,
            "ms": round((time.perf_counter() - t0) * 1000),
        })
        status = "ERR" if error else f"{len(calls)} call(s)"
        print(f"  [{language}] {message[:52]:<52} {status:>12}  "
              f"{results[-1]['ms']:>6} ms")

    await llm.close_client()
    await res.close()
    return {"queries": len(QUERIES), "results": results}


def compare(before_path: str, after_path: str) -> int:
    before = json.loads(Path(before_path).read_text(encoding="utf-8"))
    after = json.loads(Path(after_path).read_text(encoding="utf-8"))
    regressions = 0

    for b, a in zip(before["results"], after["results"]):
        print(f"\n{'=' * 70}\n[{a['language']}] {a['message']}")
        for label, run in (("before", b), ("after", a)):
            names = [n for r in run["tool_results"]
                     for n in (r.get("scheme_names") or [])]
            errors = [r["error"] for r in run["tool_results"] if r.get("error")]
            print(f"  {label:<7} {run['ms']:>6} ms  "
                  f"tools={[c['name'] for c in run['tool_calls']]}")
            for call in run["tool_calls"]:
                print(f"          args {json.dumps(call['args'], ensure_ascii=False)}")
            print(f"          -> {len(names)} scheme(s): "
                  f"{', '.join(n[:34] for n in names[:3]) or '(none)'}")
            if errors:
                print(f"          !! {errors}")
            if run["error"]:
                print(f"          !! turn failed: {run['error']}")
            print(f"          text: {(run['response'] or '')[:150]}")

        # The only hard regression: the old build found schemes and the new one
        # does not. Different schemes are expected — the corpus is 10x larger.
        b_found = sum(r.get("matches") or 0 for r in b["tool_results"])
        a_found = sum(r.get("matches") or 0 for r in a["tool_results"])
        if a["error"] or any(r.get("error") for r in a["tool_results"]):
            print("  >> REGRESSION: after-build errored")
            regressions += 1
        elif b_found and not a_found:
            print(f"  >> REGRESSION: before found {b_found}, after found 0")
            regressions += 1

    print(f"\n{'=' * 70}\nregressions: {regressions}")
    return 1 if regressions else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="chat_query_set.json")
    p.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    args = p.parse_args()

    if args.compare:
        return compare(*args.compare)

    payload = asyncio.run(run_set())
    Path(args.out).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
