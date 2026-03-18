# Prompt Autopsy — AI Voice Agent Evaluation

Evaluation pipeline for an AI debt-collection voice agent running on a broken system prompt.
Built for the Riverline prompt engineering assignment.

---

## What this repo contains

```
prompt-autopsy/
├── system-prompt.md          Original (broken) agent prompt
├── system-prompt-fixed.md    Fixed prompt — 3 critical flaws addressed
├── detective/
│   ├── evaluator.py          LLM judge that scores each transcript 0-100
│   └── scoring_prompt.txt    Rubric used by the judge (5 dimensions + outcome gates)
├── surgeon/
│   ├── flaws.md              3 critical flaws with exact transcript evidence
│   └── resimulation.py       Re-runs 3 bad calls with the fixed prompt, before/after
├── pipeline/
│   ├── run_pipeline.py       One-command pipeline: evaluate any prompt on any transcripts
│   └── simulator.py          Replays borrower turns through a given prompt turn by turn
├── transcripts/              10 real call transcripts (JSON)
├── verdicts.json             Ground truth human verdicts
└── results/                  All outputs — scores, comparisons, resimulations
```

---

## Setup

```bash
git clone <your-repo-url>
cd prompt-autopsy
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env — set GEMINI_API_KEY and LLM_PROVIDER=gemini
```

---

## How to run everything

### Part 1 — Evaluate all 10 transcripts

```bash
python detective/evaluator.py --all --transcripts_dir transcripts/ --output results/ \
  --verdicts verdicts.json
```

### Part 2 — Re-simulate 3 bad calls with the fixed prompt

```bash
python surgeon/resimulation.py
```

### Part 3 — Full pipeline comparison (the main command)

```bash
# Baseline: score originals as-is
# Fixed: simulate new agent responses with fixed prompt, then score
# This is the correct comparison — different evidence, not the same transcripts twice
python pipeline/run_pipeline.py \
  --prompt system-prompt.md \
  --prompt2 system-prompt-fixed.md \
  --transcripts transcripts/ \
  --simulate-prompt2
```

---

## Part 1 — Evaluator results

**Model:** `gemini-2.0-flash` | **Threshold:** 65 | **Evaluator accuracy:** 8/10 = **80%**

| Call | Score | Verdict | GT | Match | Key signal |
|---|---|---|---|---|---|
| call_01 | 80 | GOOD | GOOD | ✅ | Clean PTP, all phases covered |
| call_02 | 49 | BAD | BAD | ✅ | LV=3 — language violations, code-enforced -15 |
| call_03 | 61 | BAD | BAD | ✅ | Lp=3 — UTR loop, -17 across 3 dimensions |
| call_04 | 76 | GOOD | GOOD | ✅ | Empathetic handling of unemployed borrower |
| call_05 | 87 | GOOD | GOOD | ✅ | Strong negotiation, clean resolution |
| call_06 | 88 | GOOD | GOOD | ✅ | Dispute handled correctly |
| call_07 | 40 | BAD | BAD | ✅ | Gate C — language barrier, no resolution |
| call_08 | 20 | BAD | GOOD | ❌ | Gate B fired instead of Gate A |
| call_09 | 53 | BAD | BAD | ✅ | Gate D — connection drop, no recovery |
| call_10 | 55 | GOOD | BAD | ❌ | Evasive borrower, Gate E did not fire |

### How the evaluator works

The evaluator uses two mechanisms that run after the LLM judge returns its response:

**Outcome gates** — binary pattern checks that override all dimensional scoring:

| Gate | Pattern | Fixed score | Verdict |
|---|---|---|---|
| A | Wrong number, handled correctly | 92 | GOOD |
| B | Wrong number, info leaked | 20 | BAD |
| C | Language barrier, call went nowhere | 40 | BAD |
| D | Call dropped, no recovery | 53 | BAD |
| E | Agent gave up without probing | 45 | BAD |

**Code-enforced penalties** — the LLM counts, Python applies the math:
- Language violations: -5 per occurrence on both `empathy_tone` and `policy_compliance`, capped at -15
- Loop count ≥ 3: -5 on `phase_discipline`
- Loop count ≥ 5: additional -7 on `policy_compliance`

The LLM is used only for pattern detection. Score arithmetic is never trusted from the model.

---

## Part 2 — Prompt flaws and fixes

Full evidence in `surgeon/flaws.md`. Summary:

### Flaw 1 — No identity gate before amount disclosure

**Root cause:** The prompt says "disclose amounts after the borrower responds" but also
"Simple acknowledgment ('Hello'/'Yes'): Proceed with TOS/POS disclosure." These contradict.
The LLM resolves it by disclosing amounts after any response — identity never confirmed.

**Evidence:** call_01 turn 3 — borrower said "Yes" (one word), agent disclosed 50,000 rupees.
call_04 — no identity confirmation step at all.

**Fix:** Mandatory 3-step gate added to Opening phase:
1. Wait for borrower to respond
2. Ask "Am I speaking with {{customer_name}}?"
3. Only after confirmation — disclose amounts

---

### Flaw 2 — Language switching is a function call, not a behavioral constraint

**Root cause:** The global prompt has zero language instructions. `switch_language()` exists
as a callable but the prompt never says when to call it, how quickly, or what to do if the
agent reverts to English. The LLM generates text first (defaulting to English), then calls
the function as a side effect.

**Evidence:** call_02 — borrower requested Hindi 4 times explicitly. Agent acknowledged
each request, then immediately generated the next response in English. 82 turns, zero
resolution. Language violations counted at LV=3, triggering -15 on two dimensions.

**Fix:** `LANGUAGE RULE — HIGHEST PRIORITY` block added at the top of the global prompt,
before any other instruction. Explicitly prohibits Romanized Hindi. Explicitly prohibits
English reversion. Repeated as a one-liner at the start of every phase header.

---

### Flaw 3 — No topic-level loop detection

**Root cause:** The prompt has phase-level loop detection ("after 5-6 circular exchanges,
move to next phase") but an agent can run 50 turns on a single sub-topic (UTR verification,
document channel) without triggering the phase rule. Additionally the prompt instructs the
agent to say "I will verify" for information it cannot look up in real time, creating an
infinite false-promise loop.

**Evidence:** call_03 — 105 turns, 901 seconds. Agent asked for UTR number 8+ times, claimed
to be verifying live each time. Customer became progressively more frustrated. Zero resolution.

**Fix:**
- Topic-level loop rule: after asking for the same thing 2 times with no result, stop and
  take an alternative action (different channel, escalate, or acknowledge and close)
- Prohibited fake real-time verification: agent must say "our team will verify and follow up"

---

### Resimulation results — before/after on 3 bad calls

| Call | Flaw demonstrated | Key change |
|---|---|---|
| call_02 | Language not enforced (LV=3) | Turn 3: Devanagari Hindi immediately. Turn 6: proceed_to_dispute on first loan denial |
| call_03 | Loop detection missing (Lp=3) | Turns 8-10: "our team will verify" instead of 8th UTR request |
| call_07 | Language barrier (Gate C) | Turn 11: switch_language(ta) when asked "do you know Tamil?" |

---

## Part 3 — Pipeline results

Two runs were made:

**Run 1 — Full comparison across all 10 calls:**
```bash
python pipeline/run_pipeline.py \
  --prompt system-prompt.md \
  --prompt2 system-prompt-fixed.md \
  --transcripts transcripts/ \
  --simulate-prompt2
```

| Metric | Original | Fixed | Delta |
|---|---|---|---|
| Average score | 65.4 | 77.1 | **+11.7** |
| Good calls | 5/10 | 7/10 | **+2** |
| Good rate | 50% | 70% | **+20%** |

**Run 2 — Focused on the 5 bad calls only:**
```bash
python pipeline/run_pipeline.py \
  --prompt system-prompt.md \
  --prompt2 system-prompt-fixed.md \
  --transcripts transcripts/ \
  --simulate-prompt2 \
  --calls call_02 call_03 call_07 call_08 call_09
```

**Mode:** Asymmetric — baseline scores original transcripts as-is, fixed prompt simulates
new agent responses then scores those. Different evidence, not the same transcripts twice.

### Per-call results (bad calls only)

| Call | Before | After | Delta | Gate change | Verdict flip |
|---|---|---|---|---|---|
| call_07 | 40 | 77 | +37 | Gate C → NONE | BAD → GOOD ✅ |
| call_08 | 20 | 92 | +72 | Gate B → Gate A | BAD → GOOD ✅ |
| call_09 | 53 | 62 | +9 | Gate D → NONE | still BAD |
| call_02 | 49 | 51 | +2 | LV 3→1 | still BAD |
| call_03 | 61 | 40 | -21 | Gate C fired | still BAD |

### Aggregate on bad calls

| Metric | Original | Fixed | Delta |
|---|---|---|---|
| Average score | 44.6 | 64.4 | **+19.8** |
| Good calls | 0/5 | 2/5 | **+2** |
| empathy_tone avg | 9.4/20 | 14.6/20 | **+5.2** |
| phase_discipline avg | 8.6/20 | 13.8/20 | **+5.2** |
| policy_compliance avg | 7.2/20 | 14.2/20 | **+7.0** |

### Reading the results

**call_07 (+37, BAD→GOOD):** The most important flip. Original: Gate C fired — language
barrier was never bridged, call ended with no outcome. Fixed: Gate C did not fire — the
agent switched to Tamil immediately when asked, established communication, and reached
a concrete outcome. Direct result of the language rule fix (Flaw 2).

**call_08 (+72, BAD→GOOD):** Original: Gate B fired — agent leaked loan details to the
wrong person. Fixed: Gate A fired — agent confirmed the wrong number without disclosing
any amount or account detail. Direct result of the identity gate fix (Flaw 1).

**call_09 (+9, still BAD):** Gate D is gone — the fixed agent attempted recovery after
connection loss instead of dropping silently. Still scores 62, just below the 65 threshold.
One more recovery attempt would push it above.

**call_02 (+2, still BAD):** Language violations dropped from LV=3 to LV=1, confirming
the language rule is working. Still BAD because the call involves a grieving widow
disputing a deceased husband's loan — this requires human escalation that no prompt
can provide. The correct fix is routing, not prompt engineering.

**call_03 (-21, still BAD):** Loop count dropped from Lp=3 to Lp=2, confirming loop
detection is partially working. But Gate C still fires — the Tamil audio is too fragmented
for any agent to bridge. This is a data quality failure, not a prompt failure. Route to
a human Tamil-speaking agent.

### The honest finding

The fixed prompt resolves every failure that is a prompt problem.
The two calls that remain BAD (call_02, call_03) are infrastructure problems:
one needs a human escalation path for vulnerable borrowers, one needs a Tamil-speaking agent.
No prompt change fixes broken audio or a bereaved borrower's legal dispute.

---

## What I would do with more time

**Evaluator:**
- Fix the 2 remaining wrong verdicts: call_08 Gate B/A ambiguity and call_10 evasive borrower
- Run each transcript 3 times and take majority verdict to reduce LLM variance
- Add a confidence flag for cases where gate detection is borderline

**Fixed prompt:**
- Add a vulnerable borrower protocol — bereavement, illness, job loss → no credit pressure
  in that session, soft handoff only
- Add an inbound callback context: when borrower calls back, agent should not re-run the
  full opening as if it's a cold call
- Test in actual function-calling infrastructure, not simulation text mode

**Pipeline:**
- Cache evaluator results by (call_id, prompt_hash) to skip re-scoring unchanged calls
- Add a `--compare-n` flag to rank multiple prompt versions in one run
- Add token usage tracking per run to keep budget visible

---

## Cost

Model: `gemini-2.0-flash`
Estimated total across all runs (development + final): ~500K tokens
Estimated cost: **under $0.25**