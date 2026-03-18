"""
pipeline/simulator.py

Replays a transcript through any system prompt turn by turn.
Used by run_pipeline.py to simulate agent responses before evaluation.

This is a reusable module — import simulate_transcript() directly
or use it standalone for debugging a single call.
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
PROVIDER        = os.getenv("LLM_PROVIDER", "gemini").lower()
GEMINI_MODEL    = "gemini-2.0-flash"
OPENAI_MODEL    = "gpt-4o-mini"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS      = 512
TEMPERATURE     = 0.2


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


def call_llm(client, system_prompt: str, history: list) -> str:
    """Send multi-turn history to the LLM. Returns agent response string."""
    if PROVIDER == "gemini":
        from google import genai as _genai
        contents = [
            {"role": "user" if m["role"] == "user" else "model",
             "parts": [{"text": m["content"]}]}
            for m in history
        ]
        r = client.models.generate_content(
            model=GEMINI_MODEL, contents=contents,
            config=_genai.types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
            ),
        )
        return r.text.strip()

    elif PROVIDER == "openai":
        msgs = [{"role": "system", "content": system_prompt}] + history
        r = client.chat.completions.create(
            model=OPENAI_MODEL, max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE, messages=msgs,
        )
        return r.choices[0].message.content.strip()

    elif PROVIDER == "anthropic":
        r = client.messages.create(
            model=ANTHROPIC_MODEL, max_tokens=MAX_TOKENS,
            system=system_prompt, messages=history,
        )
        return r.content[0].text.strip()


def active_model() -> str:
    return {"gemini": GEMINI_MODEL, "openai": OPENAI_MODEL,
            "anthropic": ANTHROPIC_MODEL}[PROVIDER]


# ──────────────────────────────────────────────
# PROMPT PREPARATION
# ──────────────────────────────────────────────
def load_prompt_file(prompt_path: str) -> str:
    """
    Load a prompt from .md or .txt file.
    For .md files: strips markdown headers and code fences,
    keeping only the actual prompt content inside them.
    """
    path = Path(prompt_path)
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")

    raw = path.read_text(encoding="utf-8")

    if path.suffix != ".md":
        return raw.strip()

    # For .md files extract content inside ``` blocks + non-header plain text
    lines   = raw.split("\n")
    clean   = []
    in_code = False
    for line in lines:
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            clean.append(line)
        elif line.startswith("#"):
            continue  # skip markdown headers
        else:
            clean.append(line)
    return "\n".join(clean).strip()


def inject_context(prompt: str, call: dict) -> str:
    """Replace {{template_vars}} in prompt with actual call data."""
    customer = call.get("customer", {})
    subs = {
        "{{customer_name}}":     customer.get("name", "the borrower"),
        "{{pending_amount}}":    customer.get("pending_amount", ""),
        "{{closure_amount}}":    customer.get("closure_amount", ""),
        "{{settlement_amount}}": customer.get("settlement_amount", ""),
        "{{dpd}}":               customer.get("dpd", ""),
        "{{tos}}":               customer.get("pending_amount", ""),
        "{{pos}}":               customer.get("closure_amount", ""),
        "{{due_date}}":          "end of month",
        "{{today_date}}":        "18/03/2026",
        "{{today_day}}":         "Wednesday",
        "{{loan_id}}":           call.get("call_id", ""),
        "{{lender_name}}":       "DEMO_LENDER",
        "{{bank_name}}":         "DemoLender",
        "{{agent_name}}":        "Alex",
    }
    for k, v in subs.items():
        prompt = prompt.replace(k, str(v))
    return prompt


# ──────────────────────────────────────────────
# CORE SIMULATION
# ──────────────────────────────────────────────
def simulate_transcript(call: dict, system_prompt: str, client,
                        max_customer_turns: int = 999) -> dict:
    """
    Replay the borrower's messages through the given system prompt.
    Generates new agent responses turn by turn, maintaining full conversation context.

    Returns a new call dict with:
      - original transcript replaced by simulated transcript
      - simulation_meta with stats
    """
    # Extract borrower turns in order, merging consecutive customer messages
    customer_turns = _extract_customer_turns(call, max_customer_turns)

    history    = []  # running conversation context for the LLM
    new_turns  = []  # output transcript

    for i, customer_text in enumerate(customer_turns):
        # Customer message
        history.append({"role": "user", "content": customer_text})
        new_turns.append({"speaker": "customer", "text": customer_text})

        # New agent response
        try:
            agent_response = call_llm(client, system_prompt, history)
        except Exception as e:
            agent_response = f"[SIMULATION_ERROR: {e}]"

        history.append({"role": "assistant", "content": agent_response})
        new_turns.append({"speaker": "agent", "text": agent_response})

    # Build the simulated call object — same shape as original
    simulated = dict(call)  # shallow copy preserves metadata
    simulated["transcript"]       = new_turns
    simulated["function_calls"]   = []  # function calls can't be extracted from text sim
    simulated["simulation_meta"]  = {
        "simulated":      True,
        "model":          active_model(),
        "customer_turns": len(customer_turns),
        "total_turns":    len(new_turns),
        "original_turns": call.get("total_turns", 0),
    }
    return simulated


def _extract_customer_turns(call: dict, max_turns: int) -> list:
    """Merge consecutive customer turns, return list of customer messages."""
    turns  = []
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
            if len(turns) >= max_turns:
                break
    if buffer:
        turns.append(" ".join(buffer))
    return turns[:max_turns]


# ──────────────────────────────────────────────
# STANDALONE USAGE
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Simulate a single transcript with a prompt")
    parser.add_argument("--transcript", required=True, help="Path to transcript JSON")
    parser.add_argument("--prompt",     required=True, help="Path to system prompt .md or .txt")
    parser.add_argument("--output",     default=None,  help="Save simulated transcript JSON here")
    parser.add_argument("--max_turns",  type=int, default=15, help="Max customer turns to replay")
    args = parser.parse_args()

    with open(args.transcript, encoding="utf-8") as f:
        call = json.load(f)

    raw_prompt    = load_prompt_file(args.prompt)
    system_prompt = inject_context(raw_prompt, call)
    client        = get_client()

    print(f"Provider : {PROVIDER.upper()}")
    print(f"Model    : {active_model()}")
    print(f"Call     : {call.get('call_id')}")
    print(f"Prompt   : {args.prompt}")
    print()

    simulated = simulate_transcript(call, system_prompt, client, args.max_turns)

    print("Simulated transcript:")
    for turn in simulated["transcript"]:
        label = "AGENT" if turn["speaker"] == "agent" else "CUSTOMER"
        print(f"  [{label}]: {turn['text'][:120]}")

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(simulated, f, ensure_ascii=False, indent=2)
        print(f"\nSaved: {args.output}")