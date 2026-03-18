# Prompt Autopsy — Flaw Analysis

Identified from reading all 10 transcripts against the system prompt.
Each flaw has: the exact prompt text that causes it, the transcript evidence, and the failure mechanism.

---

## FLAW 1 — Identity confirmation is instructed but not enforced as a hard gate

### The broken prompt instruction
From Phase 1 (Opening):
> "IMPORTANT: The greeting did NOT mention any amounts. You must disclose amounts only AFTER the borrower responds and you confirm their identity."
> "AFTER BORROWER RESPONDS (identity confirmed): State: 'Your total outstanding is {{tos}} rupees...'"

### What the prompt actually produces
The phrase "after the borrower responds" is treated by the agent as equivalent to "after identity is confirmed." A simple "Yes" or "Hello" counts as identity confirmation because the prompt says to proceed after any simple acknowledgment:

> "Simple acknowledgment ('Hello'/'Yes'): Proceed with TOS/POS disclosure above."

These two instructions contradict each other. The agent resolves the contradiction by doing what is easiest — disclosing amounts immediately after any response.

### Transcript evidence
**call_01, turn 3:**
```
[customer]: Yes.
[agent]: Your total outstanding is fifty thousand rupees. But we can remove all charges
         and close your loan at just thirty five thousand rupees.
```
The borrower said one word. Identity was never confirmed. Amounts were disclosed immediately.

**call_04, turn 5:**
```
[agent]: Okay, Vikram, I'm calling about your DemoLender education loan.
         There's an outstanding amount of forty-three thousand seven hundred
         and forty-seven rupees.
```
No identity confirmation step occurred at all.

### Failure mechanism
The prompt defines identity confirmation as a concept but provides no enforcement mechanism — no required question, no required response, no gate before disclosure. When the LLM sees a borrower respond, it pattern-matches to the closest instruction ("After borrower responds → disclose amounts") and skips the confirmation intent entirely.

### Impact
Every call in the dataset disclosed amounts before confirming identity. This is both a compliance risk (sharing financial details with the wrong person) and a quality failure (our evaluator deducted -5 on phase_discipline for every call where this occurred).

---

## FLAW 2 — Language switching is instructed as a function call but the prompt gives no urgency or failure behavior

### The broken prompt instruction
From the Global System Prompt, there is **no mention of language switching behavior at all.** The `switch_language` function exists in the available functions list but the system prompt never instructs when to call it, how quickly to call it, or what to do if the agent fails to switch.

The agent is left to infer language-switching behavior from the function definition alone:
```json
{
  "name": "switch_language",
  "description": "Switch the conversation language.",
  "parameters": { "language": "..." }
}
```

### Transcript evidence
**call_02, 82 turns — the most damaging example:**
The borrower requested Hindi at least 4 times explicitly:
```
[customer]: अभी अभिषेक जी पहले आप मुझे हिंदी में बात करिए.
            मैं बार बार request कर रही हूं आपको
[agent]:    मुझे माफ़ करना, मैं आपकी बात समझ गया हूँ।
            अब से मैं आपसे हिंदी में ही बात करूँगा।
[customer]: हां जब मैं आपसे हिंदी हिंदी बोल रही हूं
            आप बार बार english में बोले चले जा रहे हैं.
```
The agent acknowledged the request and then immediately reverted to English. This happened across multiple turns. The `switch_language` function was called 3 times but the agent continued generating English responses between calls.

**call_03, 105 turns:**
The borrower switched between Tamil and Hindi throughout. The agent called `switch_language` 6 times but kept generating Romanized Hindi and English responses:
```
[agent]: Theek hai, main aapke saath Hindi mein baat karta hoon.
[agent]: Aapne kaha ki aapne August mein EMI bhara tha,
[customer]: Hello हिंदी में note कर लो sir.
```
After being told twice to use proper Hindi script, the agent continued in Romanized/English mix.

### Failure mechanism
The system prompt treats language switching as a function to call, not a behavioral constraint. The LLM generates text first (in whatever language it defaults to) and calls the function as a side effect. There is no instruction that says: "If the borrower requests a language switch, your NEXT response must be entirely in that language. Any English in subsequent turns is a violation."

### Impact
Language violations are the single largest scoring factor in bad calls. call_02 scored 45 (BAD) primarily due to this flaw. The borrower — a grieving widow handling a deceased husband's loan — was forced to repeat her Hindi request 4+ times while being lectured about credit scores in English.

---

## FLAW 3 — No loop detection or escalation instruction — agent can cycle indefinitely

### The broken prompt instruction
From Phase 2 (Discovery):
> "DO NOT GET STUCK: After 5-6 genuinely circular exchanges where the borrower repeats the same point without progress, call proceed_to_negotiation with your best assessment."

From Phase 3 (Negotiation):
> "DO NOT GET STUCK: After 5-6 genuinely circular exchanges, move to closing with best assessment."

### What the prompt actually produces
The "5-6 circular exchanges" rule only applies to phase transitions — it does not address loops within a phase on a specific sub-topic. When a borrower claims to have already paid and provides a UTR number, the agent has no instruction for what to do when UTR verification fails. It loops on the same verification request indefinitely.

More critically, the prompt has no instruction for what to do when the agent cannot actually verify something the borrower claims. The agent has no real-time lookup capability, but no instruction prevents it from pretending to verify.

### Transcript evidence
**call_03, 105 turns — the most extreme example:**
The agent cycled "Let me verify your UTR number" at least 8 times across 40+ turns:
```
[agent]: "ठीक है, मैं यह यूटीआर नंबर नोट कर रहा हूँ - सीएम552522।
          मैं इसे चेक करवाता हूँ।"
[agent]: "मुझे इस नंबर से भी कोई भुगतान नहीं मिल रहा है।"
[agent]: "मैं अभी भी इसे सत्यापित करने की कोशिश कर रहा हूँ।
          इसमें थोड़ा समय लग रहा है।"
[agent]: "जी, मुझे अभी तक आपके द्वारा दिए गए यूटीआर नंबर से
          कोई भुगतान नहीं मिला है।"
```
The customer became progressively more frustrated:
```
[customer]: "अरे भाई यह UTR number बता दिया फिर आप लोगों को पड़ता है
             UTR गलत है यह कैसा हो सकता है काग़ज़ से यह बताया"
```
The agent never escalated to a dispute flow or offered a resolution path. The call ended after 105 turns with the customer still unresolved.

**call_02:** Agent looped on document submission — told the borrower to email support@demolender.com at least 5 times after being told repeatedly the borrower cannot use email.

### Failure mechanism
The prompt defines phase-level loop detection but not topic-level loop detection. An agent can run 50 turns on a single sub-problem (UTR verification, document submission channel) without triggering the "5-6 circular exchanges" rule because the exchanges are not circular at the phase level — only at the topic level.

Additionally, the prompt instructs the agent to say "I will verify" but provides no instruction for what to do when verification is not possible. This creates a false promise loop: agent promises to check, cannot check, reports failure, borrower re-provides, repeat.

### Impact
call_03 is a 15-minute call (901 seconds, 105 turns) that ends with zero resolution. The customer — who claims to have already paid — is left frustrated with no clear next step. This is the worst possible outcome for a dispute scenario.

---

## Summary Table

| Flaw | Root cause in prompt | Calls affected | Evaluator dimension hit |
|---|---|---|---|
| 1. No identity gate | "After borrower responds" treated as confirmation | call_01, call_04 (all calls) | phase_discipline -5 |
| 2. No language enforcement | Language switch is a function, not a constraint | call_02, call_03, call_07 | empathy_tone -5/occurrence, policy_compliance -5/occurrence |
| 3. No loop/escalation protocol | Only phase-level loop detection, no topic-level | call_02, call_03 | phase_discipline -5, resolution_effectiveness -5 |

These three flaws account for the majority of scoring deductions across all bad calls.