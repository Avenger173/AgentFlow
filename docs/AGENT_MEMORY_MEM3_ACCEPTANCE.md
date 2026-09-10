# AgentFlow Memory MEM-3 Acceptance

> Status: passed
>
> Acceptance date: 2026-09-10
>
> Data boundary: all checks use synthetic conversation records, mock runtimes, and no provider or network call.

## Delivered

| Capability | Implementation boundary | Verification fact |
|---|---|---|
| One selected context | `ContextEnvelope` is built once per request and supplied to Commander Intent, plan audit, and final reply | Intent payload, plan snapshot, and reply prompt receive the same current message, working-state summary, and selected context identity |
| Model-aware budget | Verified DeepSeek V4 uses its confirmed 1M window; all other runtimes use a 16,384-token conservative fallback | Budget formula reserves configured output, system/tool overhead, current input, and safety margin; selected memory never exceeds `min(20k, calculated budget)` |
| Priority and turn integrity | Current Working State, confirmed long-term memory, deterministic compaction anchors/summary, then recent complete turns | Latest full user/assistant pair is retained together; delivery-only assistant records never enter a half-turn |
| Working-state preservation | Prompt state summary now includes active task and plan IDs plus unfinished open items | No-budget condition stops before a model call instead of silently dropping the structured state |
| Auditable estimates | `WorkflowPlan.context_envelope_audit` records source, reserves, local estimates, selected counts, and omissions without customer body | Audit note explicitly states estimates are not provider token usage |

## Deterministic Compaction Decision

MEM-3 keeps the existing deterministic structured conversation compaction. No LLM summarizer is admitted: no measured retention gain, provider failure fallback, or usage-cost record has yet established a reason to add it. The next candidate-sedimentation phase must continue to preserve this deterministic fallback.

## Gate

```powershell
cd D:\project\AgentFlow\AgentFlow
python backend\scripts\verify_commander_context_envelope.py
python backend\scripts\verify_commander_memory_quality.py --mode gate --gate-profile mem3
python backend\scripts\verify_commander_c6_conversation.py
python backend\scripts\verify_commander_c6_planning.py
python backend\scripts\verify_commander_intent_routing.py
python backend\scripts\verify_commander_memory.py
python backend\scripts\verify_conversation_working_state.py
python backend\scripts\verify_backend.py
python -m compileall backend/app backend/scripts
python -m pip check
```

The MEM-3 dedicated verifier covers verified and fallback budget sources, budget caps, current state fields, complete-turn clipping, no assistant-only fragments, Intent payload identity, Planner audit identity, Reply prompt identity, and unsafe no-budget rejection. The existing memory gate remains 51/51 passed.

## Limits

- Provider-reported input/output usage is still recorded only when a provider response supplies it; ContextEnvelope values are local estimates.
- The chat request is now limited to 4,000 characters so the budget contract has a bounded current input.
- Candidate lifecycle, conflict replacement, pre-compaction memory proposals, lifecycle cleanup, and Hybrid retrieval remain later MEM stages.
