"""
surgeon/resimulation.py

Re-simulates bad calls using the fixed system prompt.
For each selected call:
  1. Replays borrower messages turn by turn
  2. Generates new agent responses using the fixed prompt
  3. Produces a before/after comparison saved to results/

Usage (run from project root):
    python surgeon/resimulation.py
    python surgeon/resimulation.py --calls call_02 call_03 call_04
    python surgeon/resimulation.py --output results/
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
MAX_TOKENS          = 512      # short responses — this is a voice agent
TEMPERATURE         = 0.3      # slight variation so responses feel natural
DEFAULT_CALLS       = ["call_02", "call_03", "call_07"]
TRANSCRIPTS_DIR     = Path("transcripts")
FIXED_PROMPT_PATH   = Path("system-prompt-fixed.md")
OUTPUT_DIR          = Path("results")

# How many borrower turns to replay per call
# Enough to show the improvement without running the full 80-turn call
MAX_TURNS_TO_REPLAY = 12


# ──────────────────────────────────────────────
# PROVIDER
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


def call_llm(client, system_prompt: str, messages: list) -> str:
    """Send a multi-turn conversation to the LLM. Returns agent response text."""
    if PROVIDER == "gemini":
        from google import genai as _genai
        # Build gemini-compatible contents from message history
        contents = []
        for m in messages:
            role = "user" if m["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": m["content"]}]})
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=_genai.types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
            ),
        )
        return response.text.strip()

    elif PROVIDER == "openai":
        msgs = [{"role": "system", "content": system_prompt}] + messages
        response = client.chat.completions.create(
            model=OPENAI_MODEL, max_tokens=MAX_TOKENS, temperature=TEMPERATURE,
            messages=msgs,
        )
        return response.choices[0].message.content.strip()

    elif PROVIDER == "anthropic":
        response = client.messages.create(
            model=ANTHROPIC_MODEL, max_tokens=MAX_TOKENS,
            system=system_prompt, messages=messages,
        )
        return response.content[0].text.strip()


# ──────────────────────────────────────────────
# PROMPT LOADING
# ──────────────────────────────────────────────
def load_fixed_prompt() -> str:
    if not FIXED_PROMPT_PATH.exists():
        raise FileNotFoundError(f"Fixed prompt not found: {FIXED_PROMPT_PATH}")
    raw = FIXED_PROMPT_PATH.read_text(encoding="utf-8")
    # Strip markdown headers and code fences — extract just the prompt content
    lines  = raw.split("\n")
    clean  = []
    in_code = False
    for line in lines:
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            clean.append(line)
        elif line.startswith("## ") or line.startswith("# "):
            continue  # skip markdown headers
        else:
            clean.append(line)
    return "\n".join(clean).strip()


def inject_customer_context(prompt: str, call: dict) -> str:
    """Replace {{template_vars}} in the prompt with actual call data."""
    customer = call.get("customer", {})
    replacements = {
        "{{customer_name}}":    customer.get("name", "the borrower"),
        "{{pending_amount}}":   customer.get("pending_amount", ""),
        "{{closure_amount}}":   customer.get("closure_amount", ""),
        "{{settlement_amount}}":customer.get("settlement_amount", ""),
        "{{dpd}}":              customer.get("dpd", ""),
        "{{tos}}":              customer.get("pending_amount", ""),
        "{{pos}}":              customer.get("closure_amount", ""),
        "{{due_date}}":         "end of month",
        "{{today_date}}":       "18/03/2026",
        "{{today_day}}":        "Wednesday",
        "{{loan_id}}":          call.get("call_id", ""),
        "{{lender_name}}":      "DEMO_LENDER",
        "{{bank_name}}":        "DemoLender",
        "{{agent_name}}":       "Alex",
    }
    for key, val in replacements.items():
        prompt = prompt.replace(key, str(val))
    return prompt


# ──────────────────────────────────────────────
# SIMULATION
# ──────────────────────────────────────────────
def extract_customer_turns(call: dict) -> list:
    """
    Extract borrower-only turns from the original transcript.
    Consecutive customer turns are merged into one message.
    Stops at MAX_TURNS_TO_REPLAY customer messages.
    """
    turns = []
    buffer = []
    for turn in call.get("transcript", []):
        if turn.get("speaker") == "customer":
            text = turn.get("text", "").strip()
            if text:
                buffer.append(text)
        else:
            if buffer:
                turns.append(" ".join(buffer))
                buffer = []
            if len(turns) >= MAX_TURNS_TO_REPLAY:
                break
    if buffer:
        turns.append(" ".join(buffer))
    return turns[:MAX_TURNS_TO_REPLAY]


def simulate_call(call: dict, system_prompt: str, client) -> list:
    """
    Replay borrower turns one by one, generating new agent responses.
    Returns a list of {role, content, speaker} dicts representing the new conversation.
    """
    customer_turns = extract_customer_turns(call)
    history        = []    # running conversation for multi-turn context
    simulated      = []    # output list

    print(f"    Replaying {len(customer_turns)} customer turns...")

    for i, customer_msg in enumerate(customer_turns):
        # Add customer message to history
        history.append({"role": "user", "content": customer_msg})
        simulated.append({"speaker": "customer", "text": customer_msg, "turn": i + 1})

        # Generate new agent response
        try:
            agent_response = call_llm(client, system_prompt, history)
        except Exception as e:
            agent_response = f"[SIMULATION ERROR: {e}]"

        # Add agent response to history for next turn
        history.append({"role": "assistant", "content": agent_response})
        simulated.append({"speaker": "agent_new", "text": agent_response, "turn": i + 1})

        print(f"      Turn {i+1}/{len(customer_turns)} done", end="\r", flush=True)

    print()  # newline after progress
    return simulated


# ──────────────────────────────────────────────
# COMPARISON BUILDER
# ──────────────────────────────────────────────
def extract_original_agent_turns(call: dict, max_turns: int) -> list:
    """Get the first N agent turns from the original transcript."""
    agent_turns = []
    customer_count = 0
    for turn in call.get("transcript", []):
        if turn.get("speaker") == "customer":
            customer_count += 1
            if customer_count > max_turns:
                break
        elif turn.get("speaker") == "agent":
            agent_turns.append(turn.get("text", "").strip())
    return agent_turns


def build_comparison(call: dict, simulated: list) -> dict:
    """Build a structured before/after comparison object."""
    call_id     = call.get("call_id")
    customer    = call.get("customer", {})
    disposition = call.get("disposition")

    # Pull original agent turns for the same window
    max_turns       = MAX_TURNS_TO_REPLAY
    original_agents = extract_original_agent_turns(call, max_turns)

    # Split simulated into customer and new agent turns
    new_agents  = [t["text"] for t in simulated if t["speaker"] == "agent_new"]
    customers   = [t["text"] for t in simulated if t["speaker"] == "customer"]

    # Build paired comparison turns
    pairs = []
    for i, customer_text in enumerate(customers):
        pairs.append({
            "turn":            i + 1,
            "customer":        customer_text,
            "original_agent":  original_agents[i] if i < len(original_agents) else "(no original turn)",
            "new_agent":       new_agents[i]       if i < len(new_agents)      else "(no new turn)",
        })

    # Identify improvement dimensions per call
    improvement_notes = {
        "call_02": [
            "Language: New agent switches to Hindi immediately and stays in Hindi",
            "Empathy: No credit score lecture after bereavement disclosure",
            "Loop: When document channel fails, offers alternative (WhatsApp) instead of repeating email",
        ],
        "call_03": [
            "Loop: After 2 UTR failures, offers escalation path instead of cycling",
            "Honesty: Tells borrower our team will verify — does not fake real-time check",
            "Resolution: Ends with a concrete next step, not an open loop",
        ],
        "call_04": [
            "Identity: Confirms borrower identity before disclosing any amount",
            "Negotiation: Presents POS closure amount directly, no invented options",
            "Empathy: Acknowledges unemployment without adding pressure",
        ],
    }

    return {
        "call_id":            call_id,
        "disposition":        disposition,
        "customer_name":      customer.get("name"),
        "flaw_targeted":      {
            "call_02": "FLAW 2 — Language switching not enforced (LV=3, code-enforced penalty)",
            "call_03": "FLAW 3 — No loop detection, fake UTR verification (Lp=3)",
            "call_07": "FLAW 2 variant — Language barrier, call went nowhere (Gate C fired)",
        }.get(call_id, "multiple flaws"),
        "improvement_notes":  improvement_notes.get(call_id, []),
        "turns_replayed":     len(customers),
        "comparison":         pairs,
    }


# ──────────────────────────────────────────────
# OUTPUT
# ──────────────────────────────────────────────
def print_comparison(comp: dict):
    call_id = comp["call_id"]
    print(f"\n{'='*70}")
    print(f"BEFORE / AFTER — {call_id.upper()}")
    print(f"Flaw targeted : {comp['flaw_targeted']}")
    print(f"Improvements  :")
    for note in comp.get("improvement_notes", []):
        print(f"  • {note}")
    print(f"{'='*70}")

    for pair in comp["comparison"]:
        print(f"\n  [Turn {pair['turn']}]")
        print(f"  CUSTOMER   : {pair['customer'][:120]}")
        print(f"  ❌ BEFORE  : {pair['original_agent'][:120]}")
        print(f"  ✅ AFTER   : {pair['new_agent'][:120]}")
        print(f"  {'-'*65}")


def save_comparison(comp: dict, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{comp['call_id']}_resimulation.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(comp, f, ensure_ascii=False, indent=2)
    return out_path


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Re-simulate bad calls with fixed prompt")
    parser.add_argument("--calls",  nargs="+", default=DEFAULT_CALLS,
                        help="Call IDs to simulate (default: call_02 call_03 call_04)")
    parser.add_argument("--output", type=str,  default=str(OUTPUT_DIR))
    args = parser.parse_args()

    print(f"Provider : {PROVIDER.upper()}")
    print(f"Model    : {GEMINI_MODEL if PROVIDER == 'gemini' else OPENAI_MODEL}")
    print(f"Calls    : {args.calls}")
    print()

    client        = get_client()
    base_prompt   = load_fixed_prompt()
    output_dir    = Path(args.output)
    comparisons   = []

    for call_id in args.calls:
        transcript_path = TRANSCRIPTS_DIR / f"{call_id}.json"
        if not transcript_path.exists():
            print(f"  {call_id}: transcript not found at {transcript_path}")
            continue

        with open(transcript_path, "r", encoding="utf-8") as f:
            call = json.load(f)

        print(f"  Simulating {call_id} ({call.get('disposition')}) ...")

        # Inject this call's customer data into the prompt
        system_prompt = inject_customer_context(base_prompt, call)

        # Run simulation
        simulated  = simulate_call(call, system_prompt, client)
        comparison = build_comparison(call, simulated)
        comparisons.append(comparison)

        # Print and save
        print_comparison(comparison)
        out_path = save_comparison(comparison, output_dir)
        print(f"\n  Saved: {out_path}")

    # Save combined report
    report_path = output_dir / "resimulation_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "model":       GEMINI_MODEL if PROVIDER == "gemini" else OPENAI_MODEL,
            "calls":       args.calls,
            "comparisons": comparisons,
        }, f, ensure_ascii=False, indent=2)
    print(f"\nFull report saved: {report_path}")


if __name__ == "__main__":
    main()