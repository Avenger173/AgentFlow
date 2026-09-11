# AgentFlow Memory MEM-6 Acceptance

> Status: passed
>
> Acceptance date: 2026-09-11
>
> Data boundary: temporary SQLite and mock chat only. No provider, network, customer material, embedding model download, or production database was used.

## Delivered Contract

- `20260911_memory_lifecycle_observability_v1` adds recoverable `archived_at` to conversations and an append-only `memory_observations` table.
- Conversation archive, single deletion, project-scope clearing, retention maintenance, transcript retrieval, and restoration all enforce `project_scope`.
- Deletion removes conversation-linked proposals before deleting the parent conversation. SQLite foreign keys then remove messages and Working State; workflow task history and confirmed long-term memories remain independent.
- Runtime preferences expose `conversation_retention_days` with `0` as the default disabled state. When enabled, cleanup uses `updated_at < cutoff`, leaving a record exactly at the cutoff intact.
- Observations contain only fixed enum values, counts, latency, and validated `memory_*` identifiers. The writer rejects arbitrary retrieval modes, fallback reasons, lifecycle actions, and non-memory IDs.
- Qt settings provide the retention control; memory management shows scope, state, source, and last use; pending candidates show source; history actions archive, delete the current conversation, or clear the current project only after confirmation.

## Gate Results

| Check | Expected result | Result |
|---|---|---|
| Cross-scope read/delete | Rejected; target intact | Passed |
| Archive and restore | Same scoped session remains recoverable | Passed |
| Single deletion | Messages, Working State, proposals removed | Passed |
| Project clear | Only selected scope removed | Passed |
| Foreign keys | `PRAGMA foreign_key_check` empty | Passed |
| Retention disabled | No automatic deletion | Passed |
| Retention boundary | `< cutoff` deleted, `== cutoff` retained | Passed |
| Observation privacy | No body/title/file/path/credential/embedding field or value | Passed |
| Qt Debug build | `AgentFlow` and `BackendManagerTests` build | Passed |
| CTest | `BackendManagerTests` | Passed |

## Commands

```powershell
cd D:\project\AgentFlow\AgentFlow
python backend\scripts\verify_memory_lifecycle_observability.py
python -m compileall -q backend\app backend\scripts backend\main.py
python -m pip check

# Qt kit environment is required before this build command.
cmake --build build\Desktop_Qt_6_11_0_MSVC2022_64bit-Debug --config Debug --parallel 2
ctest --test-dir build\Desktop_Qt_6_11_0_MSVC2022_64bit-Debug -C Debug --output-on-failure
```

The scripted checks do not modify the production database. The desktop application has been compiled and its unit test passed; real-provider and manual customer-path acceptance remain MEM-7 work.
