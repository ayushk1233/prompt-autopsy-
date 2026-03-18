"""
pipeline/run_pipeline.py

Full prompt evaluation pipeline.
Takes a system prompt + transcript folder → simulates + evaluates → report.

Usage:
    # Score all transcripts with one prompt
    python pipeline/run_pipeline.py --prompt system-prompt.md --transcripts transcripts/

    # Score originals vs simulate fixed prompt — shows real per-call improvement
    python pipeline/run_pipeline.py --prompt system-prompt.md --prompt2 system-prompt-fixed.md \
                                    --transcripts transcripts/ --simulate-prompt2

    # Score original transcripts under both prompts (fast, no simulation cost)
    python pipeline/run_pipeline.py --prompt system-prompt.md --prompt2 system-prompt-fixed.md \
                                    --transcripts transcripts/ --no-simulate

Output:
    results/pipeline_<prompt_name>_report.json
    results/pipeline_comparison_report.json  (when --prompt2 is given)
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Add project root to path so we can import detective and pipeline modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from detective.evaluator import (
    score_transcript, load_scoring_prompt, save_result, get_client, active_model
)
from pipeline.simulator import (
    load_prompt_file, inject_context, simulate_transcript
)

TRANSCRIPTS_DIR = Path("transcripts")
OUTPUT_DIR      = Path("results")


# ──────────────────────────────────────────────
# SINGLE PROMPT RUN
# ──────────────────────────────────────────────
def run_single_prompt(prompt_path: str, transcript_paths: list,
                      client, scoring_prompt: str,
                      simulate: bool = True,
                      label: str = None) -> dict:
    """
    For each transcript:
      1. Simulate agent responses using the given system prompt (if simulate=True)
      2. Score the resulting conversation using the LLM judge
    Returns an aggregate report dict.
    """
    prompt_name  = Path(prompt_path).stem
    run_label    = label or prompt_name
    raw_prompt   = load_prompt_file(prompt_path)
    results      = []
    failed       = []

    print(f"\n{'='*65}")
    print(f"Running: {run_label}")
    print(f"Prompt : {prompt_path}")
    print(f"Calls  : {len(transcript_paths)}")
    print(f"Mode   : {'simulate + evaluate' if simulate else 'evaluate only'}")
    print(f"{'='*65}\n")

    for tp in transcript_paths:
        call_id = tp.stem
        try:
            with open(tp, encoding="utf-8") as f:
                call = json.load(f)

            if not isinstance(call, dict) or "call_id" not in call:
                continue

            print(f"  {call_id} ...", end=" ", flush=True)
            t0 = time.time()

            if simulate:
                # Step 1: simulate with the given prompt
                system_prompt = inject_context(raw_prompt, call)
                simulated     = simulate_transcript(call, system_prompt, client)
                call_to_score = simulated
            else:
                # Evaluate the original transcript directly (no simulation)
                call_to_score = call

            # Step 2: evaluate
            result   = score_transcript(call_to_score, scoring_prompt, client)
            elapsed  = round(time.time() - t0, 1)

            # Preserve original disposition and metadata
            result["original_disposition"] = call.get("disposition", "unknown")
            result["elapsed_seconds"]      = elapsed
            result["prompt_name"]          = prompt_name
            result["simulated"]            = simulate

            results.append(result)

            gate = result.get("gate_fired", "NONE")
            lv   = result.get("language_violations", 0)
            lp   = result.get("loop_count", 0)
            tag  = f"[Gate {gate}]" if gate != "NONE" else ""
            lv_t = f"[LV:{lv}]"    if lv > 0 else ""
            lp_t = f"[Lp:{lp}]"    if lp > 0 else ""
            print(f"Score: {result['total_score']:>3}/100  |  "
                  f"{result['verdict'].upper():<6}  {tag}{lv_t}{lp_t}  ({elapsed}s)")

        except Exception as e:
            print(f"FAILED — {e}")
            failed.append({"call_id": call_id, "error": str(e)})

    # Aggregate
    if not results:
        return {"error": "no results", "prompt": prompt_path}

    avg_score = round(sum(r["total_score"] for r in results) / len(results), 1)
    good      = sum(1 for r in results if r["verdict"] == "good")
    bad       = len(results) - good

    dim_avgs = {}
    for dim in ["empathy_tone", "phase_discipline", "negotiation_quality",
                "policy_compliance", "resolution_effectiveness"]:
        vals = [r["scores"].get(dim, 0) for r in results]
        dim_avgs[dim] = round(sum(vals) / len(vals), 1)

    report = {
        "prompt":             prompt_path,
        "prompt_name":        prompt_name,
        "label":              run_label,
        "model":              active_model(),
        "simulated":          simulate,
        "total_calls":        len(results),
        "average_score":      avg_score,
        "good_calls":         good,
        "bad_calls":          bad,
        "good_rate":          round(good / len(results) * 100, 1),
        "dimension_averages": dim_avgs,
        "failed_calls":       failed,
        "detailed_results":   results,
    }

    # Print summary
    print(f"\n  Average score : {avg_score}/100")
    print(f"  Good / Bad    : {good} / {bad}")
    print(f"  Dimension avgs:")
    for dim, avg in dim_avgs.items():
        bar = "█" * int(avg) + "░" * (20 - int(avg))
        print(f"    {dim:<28} {avg:>4}/20  {bar}")

    return report


# ──────────────────────────────────────────────
# COMPARISON RUN
# ──────────────────────────────────────────────
def run_comparison(prompt1: str, prompt2: str,
                   report1: dict, report2: dict) -> dict:
    """
    Compare two prompt runs. Produces a diff showing which calls
    improved, degraded, or stayed the same.
    """
    r1_map = {r["call_id"]: r for r in report1.get("detailed_results", [])}
    r2_map = {r["call_id"]: r for r in report2.get("detailed_results", [])}

    call_diffs = []
    improved   = []
    degraded   = []
    unchanged  = []

    for call_id in sorted(set(r1_map) | set(r2_map)):
        s1 = r1_map.get(call_id, {}).get("total_score", 0)
        s2 = r2_map.get(call_id, {}).get("total_score", 0)
        v1 = r1_map.get(call_id, {}).get("verdict", "?")
        v2 = r2_map.get(call_id, {}).get("verdict", "?")
        delta = s2 - s1

        entry = {
            "call_id":          call_id,
            "score_before":     s1,
            "score_after":      s2,
            "delta":            delta,
            "verdict_before":   v1,
            "verdict_after":    v2,
            "verdict_changed":  v1 != v2,
        }
        call_diffs.append(entry)

        if delta > 5:
            improved.append(call_id)
        elif delta < -5:
            degraded.append(call_id)
        else:
            unchanged.append(call_id)

    avg_delta = round(
        sum(d["delta"] for d in call_diffs) / len(call_diffs), 1
    ) if call_diffs else 0

    verdict_flips = [d for d in call_diffs if d["verdict_changed"]]

    comparison = {
        "prompt_before":      prompt1,
        "prompt_after":       prompt2,
        "avg_score_before":   report1.get("average_score"),
        "avg_score_after":    report2.get("average_score"),
        "avg_score_delta":    avg_delta,
        "good_calls_before":  report1.get("good_calls"),
        "good_calls_after":   report2.get("good_calls"),
        "good_rate_before":   report1.get("good_rate"),
        "good_rate_after":    report2.get("good_rate"),
        "calls_improved":     improved,
        "calls_degraded":     degraded,
        "calls_unchanged":    unchanged,
        "verdict_flips":      verdict_flips,
        "per_call_diff":      call_diffs,
        "dimension_delta":    {
            dim: round(
                report2["dimension_averages"].get(dim, 0) -
                report1["dimension_averages"].get(dim, 0), 1
            )
            for dim in report1.get("dimension_averages", {})
        },
    }
    return comparison


def print_comparison(comparison: dict):
    print(f"\n{'='*65}")
    print("PROMPT COMPARISON")
    print(f"{'='*65}")
    print(f"  Before : {comparison['prompt_before']}")
    print(f"  After  : {comparison['prompt_after']}")
    print()

    avg_b = comparison['avg_score_before']
    avg_a = comparison['avg_score_after']
    delta = comparison['avg_score_delta']
    arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "─")
    print(f"  Avg score   : {avg_b} → {avg_a}  {arrow} {abs(delta):+.1f}")
    print(f"  Good calls  : {comparison['good_calls_before']} → {comparison['good_calls_after']}")
    print(f"  Good rate   : {comparison['good_rate_before']}% → {comparison['good_rate_after']}%")

    print(f"\n  Dimension deltas (after − before):")
    for dim, d in comparison["dimension_delta"].items():
        arrow = "▲" if d > 0 else ("▼" if d < 0 else "─")
        print(f"    {dim:<28} {arrow} {d:+.1f}")

    if comparison["calls_improved"]:
        print(f"\n  Improved  : {', '.join(comparison['calls_improved'])}")
    if comparison["calls_degraded"]:
        print(f"  Degraded  : {', '.join(comparison['calls_degraded'])}")
    if comparison["calls_unchanged"]:
        print(f"  Unchanged : {', '.join(comparison['calls_unchanged'])}")

    if comparison["verdict_flips"]:
        print(f"\n  Verdict flips:")
        for flip in comparison["verdict_flips"]:
            direction = "BAD→GOOD ✅" if flip["verdict_after"] == "good" else "GOOD→BAD ❌"
            print(f"    {flip['call_id']}: {direction}  "
                  f"({flip['score_before']} → {flip['score_after']})")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Evaluate a system prompt against a set of transcripts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Evaluate original prompt (no simulation — uses original transcripts)
  python pipeline/run_pipeline.py --prompt system-prompt.md --transcripts transcripts/ --no-simulate

  # Simulate + evaluate with fixed prompt
  python pipeline/run_pipeline.py --prompt system-prompt-fixed.md --transcripts transcripts/

  # Compare two prompts
  python pipeline/run_pipeline.py --prompt system-prompt.md --prompt2 system-prompt-fixed.md \\
                                  --transcripts transcripts/ --no-simulate
        """
    )
    parser.add_argument("--prompt",       required=True, help="System prompt .md or .txt")
    parser.add_argument("--prompt2",      default=None,  help="Second prompt to compare against")
    parser.add_argument("--transcripts",  default="transcripts/", help="Transcripts directory")
    parser.add_argument("--output",       default="results/", help="Output directory")
    parser.add_argument("--no-simulate",  action="store_true",
                        help="Score original transcripts without simulation")
    parser.add_argument("--simulate-prompt2", action="store_true",
                        help="Score prompt1 on originals, simulate prompt2 — shows real improvement")
    parser.add_argument("--max-calls",    type=int, default=None,
                        help="Limit number of calls (for quick testing)")
    parser.add_argument("--calls",        nargs="+", default=None,
                        help="Only run specific call IDs e.g. --calls call_02 call_03 call_07")
    args = parser.parse_args()

    simulate         = not args.no_simulate
    simulate_prompt2 = args.simulate_prompt2
    # --simulate-prompt2 implies prompt1 runs on originals, prompt2 runs with simulation
    if simulate_prompt2:
        simulate = False

    # Load transcripts
    transcript_paths = sorted(Path(args.transcripts).glob("*.json"))
    transcript_paths = [
        tp for tp in transcript_paths
        if tp.stem not in ("_manifest",) and tp.suffix == ".json"
    ]
    # Filter out non-transcript files
    valid_paths = []
    for tp in transcript_paths:
        try:
            with open(tp, encoding="utf-8") as f:
                obj = json.load(f)
            if isinstance(obj, dict) and "call_id" in obj:
                valid_paths.append(tp)
        except Exception:
            pass
    transcript_paths = valid_paths

    if args.calls:
        transcript_paths = [tp for tp in transcript_paths if tp.stem in args.calls]
        print(f"Filtering to : {[tp.stem for tp in transcript_paths]}")

    if args.max_calls:
        transcript_paths = transcript_paths[:args.max_calls]

    if not transcript_paths:
        print(f"No valid transcript files found in {args.transcripts}")
        sys.exit(1)

    print(f"Provider    : {os.getenv('LLM_PROVIDER', 'gemini').upper()}")
    print(f"Model       : {active_model()}")
    print(f"Transcripts : {len(transcript_paths)} calls")
    if simulate_prompt2:
        print(f"Simulate    : prompt1=original | prompt2=simulated  (asymmetric comparison)")
    else:
        print(f"Simulate    : {simulate}")

    client         = get_client()
    scoring_prompt = load_scoring_prompt()
    output_dir     = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Run prompt 1 ──
    report1 = run_single_prompt(
        args.prompt, transcript_paths, client, scoring_prompt,
        simulate=simulate, label=Path(args.prompt).stem,
    )
    report1_path = output_dir / f"pipeline_{Path(args.prompt).stem}_report.json"
    with open(report1_path, "w", encoding="utf-8") as f:
        json.dump(report1, f, ensure_ascii=False, indent=2)
    print(f"\n  Report saved: {report1_path}")

    # ── Run prompt 2 and compare ──
    if args.prompt2:
        simulate2 = True if simulate_prompt2 else simulate
        report2 = run_single_prompt(
            args.prompt2, transcript_paths, client, scoring_prompt,
            simulate=simulate2, label=Path(args.prompt2).stem,
        )
        report2_path = output_dir / f"pipeline_{Path(args.prompt2).stem}_report.json"
        with open(report2_path, "w", encoding="utf-8") as f:
            json.dump(report2, f, ensure_ascii=False, indent=2)
        print(f"\n  Report saved: {report2_path}")

        comparison      = run_comparison(args.prompt, args.prompt2, report1, report2)
        print_comparison(comparison)

        comp_path = output_dir / "pipeline_comparison_report.json"
        with open(comp_path, "w", encoding="utf-8") as f:
            json.dump(comparison, f, ensure_ascii=False, indent=2)
        print(f"\n  Comparison saved: {comp_path}")


if __name__ == "__main__":
    main()