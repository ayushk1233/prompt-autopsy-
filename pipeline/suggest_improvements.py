"""
pipeline/suggest_improvements.py

Bonus: automatically suggest prompt improvements based on evaluation results.

Reads the pipeline report JSON, finds the lowest-scoring dimensions and
worst agent messages across all calls, and asks the LLM to suggest
specific, actionable prompt changes.

Usage:
    python pipeline/suggest_improvements.py --report results/pipeline_system-prompt_report.json
    python pipeline/suggest_improvements.py --report results/pipeline_system-prompt_report.json \
                                            --prompt system-prompt.md
"""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

PROVIDER  = os.getenv("LLM_PROVIDER", "gemini").lower()
GEMINI_MODEL    = "gemini-2.0-flash"
OPENAI_MODEL    = "gpt-4o-mini"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1500


def get_client():
    if PROVIDER == "gemini":
        from google import genai
        return genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    elif PROVIDER == "openai":
        from openai import OpenAI
        return OpenAI()
    elif PROVIDER == "anthropic":
        import anthropic
        return anthropic.Anthropic()


def call_llm(client, prompt: str) -> str:
    if PROVIDER == "gemini":
        from google import genai as _genai
        r = client.models.generate_content(
            model=GEMINI_MODEL, contents=prompt,
            config=_genai.types.GenerateContentConfig(max_output_tokens=MAX_TOKENS),
        )
        return r.text.strip()
    elif PROVIDER == "openai":
        r = client.chat.completions.create(
            model=OPENAI_MODEL, max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        return r.choices[0].message.content.strip()
    elif PROVIDER == "anthropic":
        r = client.messages.create(
            model=ANTHROPIC_MODEL, max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        return r.content[0].text.strip()


def build_suggestion_prompt(report: dict, system_prompt_text: str = None) -> str:
    """
    Build a focused prompt for the LLM to suggest improvements.
    Extracts the most useful signal: bad calls, worst messages, weak dimensions.
    """
    results     = report.get("detailed_results", [])
    bad_calls   = [r for r in results if r.get("verdict") == "bad"]
    dim_avgs    = report.get("dimension_averages", {})

    # Find the 2 weakest dimensions
    sorted_dims = sorted(dim_avgs.items(), key=lambda x: x[1])
    weak_dims   = sorted_dims[:2]

    # Collect worst messages across all bad calls (top 6 total)
    all_worst = []
    for r in bad_calls:
        for m in r.get("worst_messages", [])[:2]:
            all_worst.append({
                "call_id": r["call_id"],
                "text":    m.get("text", "")[:150],
                "reason":  m.get("reason", ""),
            })
    all_worst = all_worst[:6]

    # Collect failure reasons across bad calls
    all_reasons = []
    for r in bad_calls:
        for reason in r.get("reasons", [])[:2]:
            all_reasons.append(f"[{r['call_id']}] {reason}")
    all_reasons = all_reasons[:10]

    prompt_section = ""
    if system_prompt_text:
        # Include only the first 800 chars to stay within token budget
        prompt_section = f"""
CURRENT SYSTEM PROMPT (first 800 chars):
{system_prompt_text[:800]}
...
"""

    return f"""You are a prompt engineering expert reviewing an AI voice agent used for debt collection.

The agent was evaluated on 10 calls. Here are the results:

OVERALL:
- Average score: {report.get('average_score')}/100
- Good calls: {report.get('good_calls')} / {report.get('total_calls')}
- Bad calls: {report.get('bad_calls')} / {report.get('total_calls')}

WEAKEST DIMENSIONS (out of 20):
{chr(10).join(f"- {d}: {v}/20" for d, v in weak_dims)}

WORST AGENT MESSAGES FROM BAD CALLS:
{chr(10).join(f"[{m['call_id']}] \"{m['text']}\" → {m['reason']}" for m in all_worst)}

FAILURE REASONS:
{chr(10).join(all_reasons)}
{prompt_section}
Based on this evidence, suggest exactly 3 specific, actionable improvements to the system prompt.

For each improvement:
1. Name the problem in one line
2. Show the exact prompt text to ADD or REPLACE (keep it under 50 words)
3. Explain which failure pattern it addresses

Format your response as JSON:
{{
  "improvements": [
    {{
      "problem": "...",
      "prompt_addition": "...",
      "addresses": "..."
    }}
  ]
}}

Return only valid JSON. No preamble. No markdown fences.
"""


def main():
    parser = argparse.ArgumentParser(description="Suggest prompt improvements from evaluation report")
    parser.add_argument("--report", required=True, help="Path to pipeline report JSON")
    parser.add_argument("--prompt", default=None,  help="Path to system prompt (optional context)")
    parser.add_argument("--output", default=None,  help="Save suggestions to this JSON file")
    args = parser.parse_args()

    # Load report
    with open(args.report, encoding="utf-8") as f:
        report = json.load(f)

    # Load prompt text if provided
    prompt_text = None
    if args.prompt and Path(args.prompt).exists():
        prompt_text = Path(args.prompt).read_text(encoding="utf-8")

    print(f"Provider : {PROVIDER.upper()}")
    print(f"Report   : {args.report}")
    print(f"Calls    : {report.get('total_calls')} | Avg score: {report.get('average_score')} | Bad: {report.get('bad_calls')}")
    print("\nGenerating suggestions...\n")

    client  = get_client()
    prompt  = build_suggestion_prompt(report, prompt_text)
    raw     = call_llm(client, prompt)

    # Strip fences
    if raw.startswith("```"):
        lines = raw.split("\n")
        end   = -1 if lines[-1].strip() == "```" else len(lines)
        raw   = "\n".join(lines[1:end])

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        print("Raw response (non-JSON):")
        print(raw)
        sys.exit(1)

    # Print suggestions
    print("=" * 60)
    print("SUGGESTED PROMPT IMPROVEMENTS")
    print("=" * 60)
    for i, s in enumerate(result.get("improvements", []), 1):
        print(f"\n{i}. {s.get('problem', '')}")
        print(f"   Add to prompt:")
        print(f"   \"{s.get('prompt_addition', '')}\"")
        print(f"   Addresses: {s.get('addresses', '')}")

    # Save
    output_path = args.output or args.report.replace("_report.json", "_suggestions.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"\nSaved: {output_path}")


if __name__ == "__main__":
    main()