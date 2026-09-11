# AgentFlow Memory MEM-7 Acceptance

> Status: automated real-provider pass recorded on 2026-09-11; Qt manual verification remains required.
>
> Scope: bounded real-provider verification plus a separate local Qt manual checklist.

## Automated Real-Provider Contract

- The verifier uses only synthetic, non-customer text in a temporary SQLite database and a temporary runtime-preferences file.
- It reads the existing encrypted model configuration only to invoke the currently configured Commander profile. API keys, prompts, replies, source text, paths, and embeddings are neither printed nor persisted in the report.
- The default invocation allows at most 17 provider requests, with at most 2048 output tokens and a 120-second timeout per request. It also spaces requests by at least 21 seconds to respect the currently observed 3 RPM account limit. The bound includes the real answer and Commander intent-resolution calls required by the eight scenarios, and matches the configured thinking model profile, which can exhaust lower completion budgets before producing a customer-visible answer. It records only provider/model identity, request count, timeout, pacing, and provider-reported usage fields when returned.
- Each scenario combines a nonempty real-provider answer with deterministic evidence from the same request: Working State and compacted-context snapshots for short-term memory, and retrieved-memory observations plus the Commander workflow snapshot for long-term memory. Exact wording of a generative reply is intentionally not treated as proof of injection; the Qt checklist verifies that customer-visible phrasing presents the expected values.
- Eight scenarios cover latest Working State, compaction continuity, async-delivery continuation, global preference recall, project isolation, paraphrase recall, dense-unavailable fallback to admitted BM25, and memory-disabled no-read behavior.
- The Dense probe is diagnostic only: Hybrid remains unadmitted, so the customer path continues on BM25 after the optional Dense component fails closed.

```powershell
cd D:\project\AgentFlow\AgentFlow
python backend\scripts\verify_memory_real_provider.py
```

Preconditions: the desktop model settings must already contain a valid Commander profile, and `backend/.env` or the active environment must set `AGENTFLOW_CHAT_MODE=llm`. The command consumes up to 17 real provider requests, may take up to roughly seven minutes under the observed 3 RPM limit, and writes only a temporary SQLite database and temporary preferences file, both removed on exit. It does not change the production database, runtime preferences, or model configuration. On failure, retain only the final exception type, the redacted script report, provider/model name, request count, and HTTP status; never share API keys, prompts, replies, customer material, local paths, or database files.

The verifier's 120-second timeout is isolated to the verification process. The active desktop configuration currently uses a 60-second timeout and must be manually rechecked with the same model profile before this result can be treated as a customer-path pass.

## Required Qt Manual Checklist

The following customer-path checks cannot be claimed from an internal Python script and remain required before MEM-7 can be marked passed:

1. In **AI 调度台**, create a conversation, change budget/format/material constraints, then verify the next reply uses only the latest values.
2. Open **会话历史**, switch away and back to the same project-scoped conversation, then verify the transcript and current Working State recover together.
3. In **长期记忆管理**, confirm one synthetic preference, start a new conversation, verify it is recalled; then disable long-term memory and verify later requests stop using it.
4. Create two project scopes with similarly named synthetic constraints, verify each project sees only its own value, then archive, restore, delete, and clear after the confirmation dialogs.

Preconditions: run the Qt Debug application with the same local backend and configured model profile. This consumes real model/network quota for the chat checks and writes synthetic test conversations, memories, tasks, and observations to the active local database. Success means the displayed state matches the server response after each action. If a check fails, capture only the action name, timestamp, redacted status/error code, project scope label, and task/conversation ID; do not export message bodies, file names, paths, credentials, or screenshots containing customer content.

## Completion Rule

MEM-7 is complete only after the bounded verifier reports all eight scenarios passed and the four Qt customer-path checks have been performed once against the same build. Record the actual provider/model, request count, provider-reported usage coverage, and test date below without customer text.

| Run date | Provider/model | Request count | Usage coverage | Automated | Qt manual |
|---|---|---:|---|---|---|
| 2026-09-11 | kimi / kimi-k2.6 | 17 / 17 cap | 17 / 17 reported; input 13,371, output 4,704, total 18,075 | Passed: all 8 scenarios | Pending |

The recorded run used only synthetic fixtures, a temporary SQLite database, and a temporary preferences file. It did not modify the production database or production runtime preferences. The verifier used a 120-second process-local request timeout and a 21-second request interval; the active desktop configuration remains at 60 seconds, so the Qt checklist must be run before claiming the customer path passed.
