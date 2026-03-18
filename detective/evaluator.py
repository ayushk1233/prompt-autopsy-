"""
detective/evaluator.py

Scores transcripts using an LLM judge with outcome gates.
Language violation and loop penalties are enforced by code after LLM counts them.
Provider: gemini (default) | openai | anthropic — set LLM_PROVIDER in .env
"""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
PROVIDER            = os.getenv("LLM_PROVIDER", "gemini").lower()
GEMINI_MODEL        = "gemini-2.0-flash"
OPENAI_MODEL        = "gpt-4o-mini"
ANTHROPIC_MODEL     = "claude-haiku-4-5-20251001"
MAX_TOKENS          = 1500
TEMPERATURE         = 0.0
VERDICT_THRESHOLD   = 65
SCORING_PROMPT_PATH = Path(__file__).parent / "scoring_prompt.txt"

# Fixed scores per gate — code enforces these, LLM cannot override
GATE_SCORES = {
    "A": {"empathy_tone": 18, "phase_discipline": 18, "negotiation_quality": 20, "policy_compliance": 18, "resolution_effectiveness": 18},
    "B": {"empathy_tone": 10, "phase_discipline":  5, "negotiation_quality":  0, "policy_compliance":  0, "resolution_effectiveness":  5},
    "C": {"empathy_tone":  8, "phase_discipline":  8, "negotiation_quality":  8, "policy_compliance":  8, "resolution_effectiveness":  8},
    "D": {"empathy_tone": 12, "phase_discipline": 10, "negotiation_quality": 12, "policy_compliance": 14, "resolution_effectiveness":  5},
    "E": {"empathy_tone": 10, "phase_discipline":  8, "negotiation_quality":  8, "policy_compliance": 14, "resolution_effectiveness":  5},
}

# Per-violation deductions applied by code after LLM counts
LANG_VIOLATION_DEDUCTION = 5   # per violation, applied to BOTH empathy and compliance
LANG_MAX_DEDUCTION       = 15  # cap per dimension
LOOP_PHASE_THRESHOLD     = 3   # loop_count >= this -> phase_discipline -5
LOOP_POLICY_THRESHOLD    = 5   # loop_count >= this -> policy_compliance -7
LOOP_RESOLUTION_THRESHOLD= 3   # loop_count >= this AND no resolution -> resolution -5


# ──────────────────────────────────────────────
# PROVIDER SETUP
# ──────────────────────────────────────────────
def get_client():
    if PROVIDER == "gemini":
        from google import genai
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            print("ERROR: GEMINI_API_KEY not set in .env"); sys.exit(1)
        return genai.Client(api_key=key)
    elif PROVIDER == "openai":
        from openai import OpenAI
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            print("ERROR: OPENAI_API_KEY not set in .env"); sys.exit(1)
        return OpenAI()
    elif PROVIDER == "anthropic":
        import anthropic
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            print("ERROR: ANTHROPIC_API_KEY not set in .env"); sys.exit(1)
        return anthropic.Anthropic()
    else:
        print(f"ERROR: Unknown LLM_PROVIDER '{PROVIDER}'"); sys.exit(1)


def call_llm(client, system_prompt: str, user_content: str) -> str:
    if PROVIDER == "gemini":
        from google import genai as _genai
        r = client.models.generate_content(
            model=GEMINI_MODEL, contents=user_content,
            config=_genai.types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=MAX_TOKENS, temperature=TEMPERATURE,
            ),
        )
        return r.text.strip()
    elif PROVIDER == "openai":
        r = client.chat.completions.create(
            model=OPENAI_MODEL, max_tokens=MAX_TOKENS, temperature=TEMPERATURE,
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_content}],
        )
        return r.choices[0].message.content.strip()
    elif PROVIDER == "anthropic":
        r = client.messages.create(
            model=ANTHROPIC_MODEL, max_tokens=MAX_TOKENS, system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        return r.content[0].text.strip()


def active_model() -> str:
    return {"gemini": GEMINI_MODEL, "openai": OPENAI_MODEL, "anthropic": ANTHROPIC_MODEL}[PROVIDER]


# ──────────────────────────────────────────────
# FORMATTING
# ──────────────────────────────────────────────
def format_transcript_for_judge(call: dict) -> str:
    lines = [
        f"CALL_ID: {call.get('call_id', 'unknown')}",
        f"PHASES_VISITED: {', '.join(call.get('phases_visited', []))}",
        f"DISPOSITION: {call.get('disposition', 'unknown')}",
        f"DPD: {call.get('customer', {}).get('dpd', 'unknown')}",
        "", "TRANSCRIPT:",
    ]
    for turn in call.get("transcript", []):
        text = turn.get("text", "").strip()
        if text:
            lines.append(f"[{turn.get('speaker','?')}]: {text}")
    lines.append("")
    lines.append("FUNCTION_CALLS:")
    for fc in call.get("function_calls", []):
        params_str = json.dumps(fc.get("params", {}), ensure_ascii=False) or "{}"
        lines.append(f"turn {fc.get('turn','?')}: {fc.get('function','?')}({params_str})")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# POST-PROCESSING: ENFORCE COUNTS IN CODE
# ──────────────────────────────────────────────
def enforce_gate(result: dict) -> dict:
    """If LLM fired a gate, override scores with fixed gate values."""
    gate = result.get("gate_fired", "NONE").strip().upper()
    if gate in GATE_SCORES:
        result["scores"]        = GATE_SCORES[gate].copy()
        result["gate_enforced"] = True
    else:
        result["gate_enforced"] = False
    return result


def enforce_violation_penalties(result: dict) -> dict:
    """
    Apply language violation and loop penalties directly in code.
    Only runs when no gate fired (gate_enforced = False).
    This prevents the LLM from under-counting or under-penalising.
    """
    if result.get("gate_enforced"):
        return result

    scores = result["scores"]
    lang_v = int(result.get("language_violations", 0))
    loops  = int(result.get("loop_count", 0))

    # Language violations hit both empathy_tone AND policy_compliance
    lang_deduction = min(lang_v * LANG_VIOLATION_DEDUCTION, LANG_MAX_DEDUCTION)
    if lang_deduction > 0:
        scores["empathy_tone"]    = max(0, scores["empathy_tone"]    - lang_deduction)
        scores["policy_compliance"]= max(0, scores["policy_compliance"]- lang_deduction)
        result["code_applied"] = result.get("code_applied", [])
        result["code_applied"].append(
            f"language_violations={lang_v} -> -{lang_deduction} on empathy_tone and policy_compliance"
        )

    # Loop penalties
    if loops >= LOOP_PHASE_THRESHOLD:
        scores["phase_discipline"] = max(0, scores["phase_discipline"] - 5)
        result["code_applied"] = result.get("code_applied", [])
        result["code_applied"].append(f"loop_count={loops} -> -5 on phase_discipline")

    if loops >= LOOP_POLICY_THRESHOLD:
        scores["policy_compliance"] = max(0, scores["policy_compliance"] - 7)
        result["code_applied"] = result.get("code_applied", [])
        result["code_applied"].append(f"loop_count={loops} -> -7 on policy_compliance")

    result["scores"] = scores
    return result


def recompute_totals(result: dict) -> dict:
    """Always recompute total and verdict from final scores. Never trust LLM arithmetic."""
    result["total_score"] = sum(result["scores"].values())
    result["verdict"]     = "good" if result["total_score"] >= VERDICT_THRESHOLD else "bad"
    return result


# ──────────────────────────────────────────────
# MAIN SCORING PIPELINE
# ──────────────────────────────────────────────
def score_transcript(call: dict, system_prompt: str, client) -> dict:
    formatted = format_transcript_for_judge(call)
    raw       = call_llm(client, system_prompt, formatted)

    # Strip markdown fences
    if raw.startswith("```"):
        lines = raw.split("\n")
        end   = -1 if lines[-1].strip() == "```" else len(lines)
        raw   = "\n".join(lines[1:end])

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Non-JSON for {call.get('call_id')}.\nError: {e}\nRaw:\n{raw[:500]}"
        )

    # Pipeline: gate -> violation penalties -> recompute
    result = enforce_gate(result)
    result = enforce_violation_penalties(result)
    result = recompute_totals(result)

    # Metadata
    result["call_id"]        = call.get("call_id", result.get("call_id", "unknown"))
    result["disposition"]    = call.get("disposition", "unknown")
    result["phases_visited"] = call.get("phases_visited", [])
    result["model_used"]     = active_model()
    return result


# ──────────────────────────────────────────────
# FILE I/O
# ──────────────────────────────────────────────
def load_transcript(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_scoring_prompt() -> str:
    if not SCORING_PROMPT_PATH.exists():
        raise FileNotFoundError(f"Scoring prompt not found: {SCORING_PROMPT_PATH}")
    with open(SCORING_PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()

def save_result(result: dict, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, f"{result.get('call_id', 'unknown')}_eval.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return out_path


# ──────────────────────────────────────────────
# ACCURACY CHECK
# ──────────────────────────────────────────────
def accuracy_check(results: list, verdicts_path: str):
    try:
        with open(verdicts_path, "r", encoding="utf-8") as f:
            ground_truth = json.load(f)
    except Exception as e:
        print(f"Could not load verdicts.json: {e}"); return

    if isinstance(ground_truth, list):
        gt_map = {item["call_id"]: item["verdict"] for item in ground_truth}
    elif isinstance(ground_truth, dict):
        raw = ground_truth.get("verdicts", ground_truth)
        gt_map = {k: (v["verdict"] if isinstance(v, dict) else v) for k, v in raw.items()}
    else:
        print("Unrecognized verdicts.json format."); return

    correct, total, rows = 0, 0, []
    print(f"\n{'Call ID':<12} {'Gate':<6} {'LV':>4} {'Lp':>4} {'Our':<8} {'GT':<8} {'Match'}")
    print("-" * 58)

    for r in results:
        cid  = r["call_id"]
        if cid not in gt_map:
            continue
        our, gt = r["verdict"], gt_map[cid]
        gate    = r.get("gate_fired", "NONE")
        lv      = r.get("language_violations", 0)
        lp      = r.get("loop_count", 0)
        match   = our == gt
        if match: correct += 1
        total  += 1
        print(f"{cid:<12} {gate:<6} {lv:>4} {lp:>4} {our:<8} {gt:<8} {'OK' if match else 'WRONG'}")
        rows.append({"call_id": cid, "gate": gate, "language_violations": lv,
                     "loop_count": lp, "our_verdict": our, "ground_truth": gt, "match": match})

    accuracy = round(correct / total * 100, 1) if total > 0 else 0
    print(f"\nAccuracy: {correct}/{total} = {accuracy}%")

    os.makedirs("results", exist_ok=True)
    with open("results/accuracy_report.json", "w") as f:
        json.dump({"accuracy": accuracy, "correct": correct, "total": total, "per_call": rows}, f, indent=2)
    print("Accuracy report saved: results/accuracy_report.json")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--transcript",      type=str)
    parser.add_argument("--all",             action="store_true")
    parser.add_argument("--transcripts_dir", type=str, default="transcripts/")
    parser.add_argument("--output",          type=str, default="results/")
    parser.add_argument("--verdicts",        type=str, default=None)
    args = parser.parse_args()

    if not args.transcript and not args.all:
        parser.print_help(); sys.exit(1)

    print(f"Provider : {PROVIDER.upper()}")
    print(f"Model    : {active_model()}")
    print(f"Threshold: {VERDICT_THRESHOLD}\n")

    client        = get_client()
    system_prompt = load_scoring_prompt()
    results       = []

    if args.transcript:
        call     = load_transcript(args.transcript)
        cid      = call.get("call_id", args.transcript)
        print(f"Scoring {cid}...")
        result   = score_transcript(call, system_prompt, client)
        out_path = save_result(result, args.output)
        results.append(result)

        print(f"\n  Gate fired         : {result.get('gate_fired','NONE')} ({result.get('gate_reason','N/A')})")
        print(f"  Language violations: {result.get('language_violations',0)}")
        print(f"  Loop count         : {result.get('loop_count',0)}")
        if result.get("code_applied"):
            print(f"  Code enforced      :")
            for c in result["code_applied"]:
                print(f"    • {c}")
        print(f"  Score              : {result['total_score']}/100  ({result['verdict'].upper()})")
        print(f"  Breakdown          :")
        for dim, val in result.get("scores", {}).items():
            bar = "█" * val + "░" * (20 - val)
            print(f"    {dim:<28} {val:>2}/20  {bar}")
        print(f"\n  Issues:")
        for r in result.get("reasons", []):
            print(f"    • {r}")
        print(f"\n  Summary: {result.get('summary','')}")
        print(f"  Saved  : {out_path}")

    elif args.all:
        paths = sorted(Path(args.transcripts_dir).glob("*.json"))
        if not paths:
            print(f"No JSON files found in {args.transcripts_dir}"); sys.exit(1)
        print(f"Scoring {len(paths)} files...\n")
        for tp in paths:
            try:
                call = load_transcript(str(tp))
                if not isinstance(call, dict) or "call_id" not in call:
                    print(f"  Skipping {tp.name} — not a transcript file")
                    continue
                cid = call.get("call_id", str(tp))
                print(f"  {cid} ...", end=" ", flush=True)
                result   = score_transcript(call, system_prompt, client)
                out_path = save_result(result, args.output)
                results.append(result)
                gate = result.get("gate_fired", "NONE")
                lv   = result.get("language_violations", 0)
                lp   = result.get("loop_count", 0)
                tag  = f"[Gate {gate}]" if gate != "NONE" else ""
                lv_tag = f"[LV:{lv}]" if lv > 0 else ""
                lp_tag = f"[Lp:{lp}]" if lp > 0 else ""
                print(f"Score: {result['total_score']:>3}/100  |  {result['verdict'].upper()}  {tag}{lv_tag}{lp_tag}")
            except Exception as e:
                print(f"FAILED — {e}")

    if results:
        print("\n" + "=" * 70)
        print("EVALUATION SUMMARY")
        print("=" * 70)
        print(f"{'Call ID':<12} {'Gate':<6} {'LV':>4} {'Lp':>4} {'Score':>6}  {'Verdict':<8}  {'Disposition'}")
        print("-" * 70)
        for r in results:
            print(f"{r['call_id']:<12} {r.get('gate_fired','NONE'):<6} "
                  f"{r.get('language_violations',0):>4} {r.get('loop_count',0):>4} "
                  f"{r['total_score']:>6}  {r['verdict'].upper():<8}  {r.get('disposition','?')}")
        avg  = sum(r["total_score"] for r in results) / len(results)
        good = sum(1 for r in results if r["verdict"] == "good")
        print("-" * 70)
        print(f"{'Average':<12} {'':6} {'':>4} {'':>4} {avg:>6.1f}  {good} good / {len(results)-good} bad")

    if args.verdicts and results:
        print("\n" + "=" * 70)
        print("ACCURACY vs GROUND TRUTH")
        print("=" * 70)
        accuracy_check(results, args.verdicts)


if __name__ == "__main__":
    main()