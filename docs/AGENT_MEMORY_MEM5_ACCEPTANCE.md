# AgentFlow Memory MEM-5 Acceptance

> Status: passed with BM25 selected; Hybrid is not admitted
>
> Acceptance date: 2026-09-11
>
> Data boundary: temporary SQLite, synthetic short facts, deterministic Dense-ranking fixtures, no provider, network, customer material, or model download.

## Decision

`search_long_term_memories()` now uses the scoped SQLite FTS5/BM25 path by default. The `20260911_long_term_memory_bm25_v1` forward migration adds a rebuildable FTS5 table plus a Chinese bigram shadow derived only from the already stored title, summary, and tags. Its query joins back to `long_term_memories` and applies `scope + enabled + user_confirmed` before ranking.

The implementation includes an optional `LocalMemoryDenseCandidateProvider` that reuses the knowledge-base FastEmbed adapter with `allow_download=False`. It creates no second embedding provider or persistent vector store. The local environment used for this gate has neither `fastembed` nor `chromadb`, so no real Dense quality or latency evidence exists. Hybrid remains outside the Commander default path.

## Gate Results

| Measure | Required threshold | Result |
|---|---:|---:|
| Required Recall@3 | 100% | 100% |
| Required MRR | >= 0.95 | 1.000 |
| Conflict old-value recall | 0 | 0 |
| Cross-scope leakage | 0 | 0 |
| 10,000-record local BM25 P95 | <= 100 ms | 6.044 ms |
| FTS failure fallback | Scoped lexical result | Passed |
| Dense unavailable fallback | BM25 result with a reason | Passed |

The verifier also covers legacy migration/backfill, insert/update/delete FTS synchronization, structured project constraints, global preferences, synthetic semantic-candidate fusion, RRF and 7:3 weighted comparison, disabled old values, and range isolation. Its deterministic Dense fixture validates the fusion contract only; it is not real embedding evidence.

## Commands

```powershell
cd D:\project\AgentFlow\AgentFlow
python backend\scripts\verify_long_term_memory_retrieval.py
python backend\scripts\verify_commander_memory_quality.py --mode gate --gate-profile mem5
python backend\scripts\verify_commander_memory_lifecycle.py
python backend\scripts\verify_commander_memory.py
python backend\scripts\verify_commander_context_envelope.py
python -m compileall -q backend/app backend/scripts
```

To gather a real local Dense candidate result later, first prepare the existing knowledge-base FastEmbed dependency and model through its customer-visible confirmation path. Then run:

```powershell
python backend\scripts\verify_long_term_memory_retrieval.py --with-local-dense
```

That command uses temporary synthetic records and disallows model download. It may consume local CPU and model-cache disk reads, but does not call a provider, network, or production database. It fails closed when the local dependency or confirmed model is unavailable.

## Follow-up Gate

Hybrid may enter the default Commander path only after a real local Dense run proves semantic rewrite improvement over BM25, required metrics remain intact, local Hybrid P95 is at most 500 ms, packaging impact is accepted, and the Dense-unavailable fallback still passes. MEM-6 remains responsible for persisting privacy-safe retrieval metrics and lifecycle cleanup.
