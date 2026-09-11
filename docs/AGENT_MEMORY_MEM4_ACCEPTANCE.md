# AgentFlow Memory MEM-4 Acceptance

> Status: passed
>
> Acceptance date: 2026-09-11
>
> Data boundary: all checks use temporary SQLite, synthetic messages, mock Commander runtimes, and no provider, network, or customer material.

## Delivered

| Capability | Implementation boundary | Verification fact |
|---|---|---|
| Persistent candidate ledger | SQLite migration `20260910_long_term_memory_candidate_lifecycle_v1` adds `long_term_memory_proposals`, content fingerprints, sources, lifecycle states, and replacement links | Pending, confirmed, rejected, expired, and superseded are separate from `long_term_memories`; pending entries never enter retrieval |
| Controlled sources | Pre-compaction user messages require a short, explicit durable signal; completed Commander Runtime can contribute verified project constraints or explicitly requested successful experience with an artifact | One-time request yields no candidate; explicit durable, verified-task, and verified-experience fixtures each retain explainable source metadata |
| De-duplication and replacement | Fingerprint is stable across task recovery, compaction, and API retries; a same-key newer value supersedes a pending candidate, and confirmation disables the older active memory | Repeated confirmation returns one memory ID; replacing Markdown with PDF returns only PDF through active retrieval |
| Explicit review flow | Task and settings APIs list pending candidates; the Qt task history and long-term-memory manager expose the same review actions; title, summary, tags, and scope are validated again at confirmation; reject and confirm are idempotent | Task result and `/api/memories/proposals` both exercise list, confirm, and reject routes; Qt Debug build and CTest pass |
| Privacy boundary | Candidate extraction rejects secrets, absolute paths, and source messages longer than 600 normalized characters | Candidate ledger contains only sanitized short facts and source identifiers, never raw chat passages or task logs |

## Lifecycle Contract

`pending -> confirmed` is the only path that creates or reuses a formal long-term record. `pending -> rejected` is explicit and retry-safe. A newly observed pending item with the same conflict key supersedes an older pending one; confirming a different current value disables the prior effective formal memory and retains its replacement pointer for audit.

`expired` is represented in the persisted candidate state contract, but no automatic expiry job or retention setting is enabled in MEM-4. That cleanup policy remains MEM-6 work.

For a normal chat turn, extraction occurs before `save_conversation_turn()` can compact old messages, while persistence occurs only after the user/assistant pair and Working State update have succeeded. Thus the just-compacted expression can be proposed without leaving a candidate for a failed chat request.

## Gate

```powershell
cd D:\project\AgentFlow\AgentFlow
python backend\scripts\verify_commander_memory_lifecycle.py
python backend\scripts\verify_commander_memory_proposals.py
python backend\scripts\verify_commander_memory_quality.py --mode gate --gate-profile mem4
python backend\scripts\verify_commander_context_envelope.py
python backend\scripts\verify_commander_memory.py
python backend\scripts\verify_conversation_working_state.py
python backend\scripts\verify_commander_c6_conversation.py
python backend\scripts\verify_commander_c6_planning.py
python backend\scripts\verify_commander_intent_routing.py
python backend\scripts\verify_backend.py
python -m compileall -q backend/app backend/scripts
python -m pip check
& 'D:\IDE\qtcreator\Tools\CMake_64\bin\cmake.exe' --build build/codex-debug --config Debug
& 'D:\IDE\qtcreator\Tools\CMake_64\bin\ctest.exe' --test-dir build/codex-debug --output-on-failure
```

The dedicated lifecycle verifier covers SQLite migration and foreign keys, no automatic formal write, completed-task source, explicit successful-experience source, pre-compaction user source, confirmation idempotency, replacement retrieval, rejection idempotency, settings routes, recovery/retry de-duplication, and secret/path/long-source rejection. The fixture quality gate remains the 51-case MEM-3 regression floor.

## Limits

- Candidate extraction intentionally accepts only strong, short customer expressions. It does not attempt LLM semantic mining from long chat messages or task summaries.
- Long-term retrieval remains the existing scope-filtered lexical Top-3 implementation. BM25, Dense, and Hybrid admission are MEM-5 decisions.
- Qt candidate review is covered by Debug compilation and CTest. A manual desktop interaction pass remains part of the MEM-7 real-scenario acceptance.
- Conversation deletion, project cleanup, retention configuration, automatic expiration, and candidate cleanup are MEM-6 work.
