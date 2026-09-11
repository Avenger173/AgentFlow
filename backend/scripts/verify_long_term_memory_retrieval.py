"""MEM-5 长期记忆检索的离线准入评测。

只使用临时 SQLite、合成短事实和可控 Dense 排名夹具。默认不加载或下载 Embedding 模型；
``--with-local-dense`` 只复用已经由客户确认下载的知识库 FastEmbed 缓存，用于单独补充真实
本地 Dense 证据。输出仅含 case ID、聚合指标和耗时，不含客户正文、标题、路径或向量。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from typing import Sequence


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_memory_retrieval_mem5_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / "data" / "memory_retrieval_mem5.db")
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import sqlite as sqlite_service  # noqa: E402
from app.database.memory_repository import (  # noqa: E402
    create_long_term_memory,
    search_long_term_memory_retrieval,
    update_long_term_memory,
)
from app.database.sqlite import _apply_long_term_memory_bm25_v1, get_connection  # noqa: E402
from app.memory_search import build_memory_fts_shadow  # noqa: E402
from app.schemas.memory import LongTermMemoryRecord  # noqa: E402
from app.services.knowledge_vector_index import vector_index_capability  # noqa: E402
from app.services.memory_retrieval import LocalMemoryDenseCandidateProvider  # noqa: E402
from unittest.mock import patch  # noqa: E402


class FixtureDenseCandidateProvider:
    """只验证候选融合契约的确定性 Dense 排名夹具，不伪装成真实语义模型。"""

    def __init__(self, rankings: dict[str, list[str]]) -> None:
        self._rankings = rankings

    def rank(
        self,
        *,
        query: str,
        candidates: Sequence[LongTermMemoryRecord],
        limit: int,
    ) -> list[str]:
        allowed = {item.memory_id for item in candidates}
        return [
            memory_id
            for memory_id in self._rankings.get(query, [])
            if memory_id in allowed
        ][:limit]


class UnavailableDenseCandidateProvider:
    def rank(
        self,
        *,
        query: str,
        candidates: Sequence[LongTermMemoryRecord],
        limit: int,
    ) -> list[str]:
        raise RuntimeError("fixture dense unavailable")


def _percentile_95(samples: list[float]) -> float:
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95 + 0.999999) - 1))
    return ordered[index]


def _verify_legacy_migration() -> None:
    legacy_path = VERIFY_ROOT / "legacy_memory.db"
    with sqlite3.connect(legacy_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE long_term_memories (
                memory_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                scope TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                source_task_id TEXT NOT NULL DEFAULT '',
                user_confirmed INTEGER NOT NULL DEFAULT 1,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL DEFAULT '',
                memory_key TEXT NOT NULL DEFAULT '',
                replaced_by_memory_id TEXT NOT NULL DEFAULT ''
            );
            """
        )
        connection.execute(
            """
            INSERT INTO long_term_memories(
                memory_id, kind, scope, title, summary, tags_json, source_task_id,
                user_confirmed, enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, '', 1, 1, ?, ?)
            """,
            (
                "legacy_memory_1",
                "project_constraint",
                "project:legacy",
                "交付格式",
                "默认 PDF",
                '["pdf"]',
                "2026-09-11T00:00:00Z",
                "2026-09-11T00:00:00Z",
            ),
        )
        _apply_long_term_memory_bm25_v1(connection)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(long_term_memories)")}
        assert "retrieval_shadow" in columns
        row = connection.execute(
            "SELECT memory_id, retrieval_shadow FROM long_term_memory_fts WHERE memory_id = ?",
            ("legacy_memory_1",),
        ).fetchone()
        assert row is not None and str(row["retrieval_shadow"])


def _create_fixture_records() -> dict[str, LongTermMemoryRecord]:
    records = {
        "project_pdf": create_long_term_memory(
            kind="project_constraint",
            scope="project:atlas",
            title="交付格式",
            summary="项目 Atlas 默认导出 PDF。",
            tags=["pdf", "交付", "formatlegacytag"],
            source_task_id=None,
            user_confirmed=True,
        ),
        "global_brief": create_long_term_memory(
            kind="user_preference",
            scope="global",
            title="表达风格",
            summary="长期使用简洁中文，结论优先。",
            tags=["简洁", "中文"],
            source_task_id=None,
            user_confirmed=True,
        ),
        "experience_brief": create_long_term_memory(
            kind="experience",
            scope="project:atlas",
            title="沟通复盘",
            summary="复盘表明短句和结论优先能提升审批效率。",
            tags=["沟通", "复盘"],
            source_task_id=None,
            user_confirmed=True,
        ),
        "other_scope": create_long_term_memory(
            kind="project_constraint",
            scope="project:other",
            title="交付格式",
            summary="其他项目固定 Markdown。",
            tags=["markdown", "交付"],
            source_task_id=None,
            user_confirmed=True,
        ),
    }
    old_value = create_long_term_memory(
        kind="project_constraint",
        scope="project:atlas",
        title="历史格式",
        summary="已废弃的 Markdown 规范。",
        tags=["markdown", "历史"],
        source_task_id=None,
        user_confirmed=True,
    )
    update_long_term_memory(old_value.memory_id, enabled=False)
    return records


def _verify_bm25_scope_and_sync(records: dict[str, LongTermMemoryRecord]) -> dict[str, object]:
    query = "Atlas 项目的交付格式要求"
    result = search_long_term_memory_retrieval(query=query, scopes={"global", "project:atlas"})
    returned = [item.memory_id for item in result.records]
    assert result.diagnostics.mode == "bm25"
    assert returned and returned[0] == records["project_pdf"].memory_id
    assert records["other_scope"].memory_id not in returned
    assert any(item.memory_id == records["global_brief"].memory_id for item in result.records)

    updated = update_long_term_memory(
        records["project_pdf"].memory_id,
        title="交付归档",
        summary="项目 Atlas 默认导出 PDF。",
        tags=["pdf", "归档"],
    )
    old_title_result = search_long_term_memory_retrieval(
        query="formatlegacytag",
        scopes={"project:atlas"},
    )
    new_title_result = search_long_term_memory_retrieval(
        query="交付归档",
        scopes={"project:atlas"},
    )
    assert updated.memory_id not in [item.memory_id for item in old_title_result.records]
    assert updated.memory_id in [item.memory_id for item in new_title_result.records]
    return {
        "mode": result.diagnostics.mode,
        "structured_candidate_count": result.diagnostics.structured_candidate_count,
        "global_preference_candidate_count": result.diagnostics.global_preference_candidate_count,
    }


def _verify_dense_contract(records: dict[str, LongTermMemoryRecord]) -> dict[str, object]:
    query = "请把审批说明写得干练一些，去掉冗余内容"
    provider = FixtureDenseCandidateProvider(
        {query: [records["experience_brief"].memory_id]}
    )
    rrf = search_long_term_memory_retrieval(
        query=query,
        scopes={"global", "project:atlas"},
        dense_candidate_provider=provider,
        fusion_strategy="rrf",
    )
    weighted = search_long_term_memory_retrieval(
        query=query,
        scopes={"global", "project:atlas"},
        dense_candidate_provider=provider,
        fusion_strategy="weighted",
    )
    assert rrf.diagnostics.mode == "hybrid_rrf"
    assert weighted.diagnostics.mode == "hybrid_weighted"
    assert rrf.records[0].memory_id == records["experience_brief"].memory_id
    assert weighted.records[0].memory_id == records["experience_brief"].memory_id

    unavailable = search_long_term_memory_retrieval(
        query="Atlas 项目的交付归档要求",
        scopes={"global", "project:atlas"},
        dense_candidate_provider=UnavailableDenseCandidateProvider(),
    )
    assert unavailable.diagnostics.mode == "bm25_dense_unavailable"
    assert unavailable.diagnostics.fallback_reason == "dense_unavailable"
    assert unavailable.records[0].memory_id == records["project_pdf"].memory_id
    return {
        "rrf_mode": rrf.diagnostics.mode,
        "weighted_mode": weighted.diagnostics.mode,
        "fallback_mode": unavailable.diagnostics.mode,
    }


def _verify_fts_failure_fallback(records: dict[str, LongTermMemoryRecord]) -> str:
    with patch(
        "app.database.memory_repository._search_bm25_candidates",
        side_effect=sqlite3.DatabaseError("fixture fts unavailable"),
    ):
        result = search_long_term_memory_retrieval(
            query="Atlas 项目的交付归档要求",
            scopes={"global", "project:atlas"},
        )
    assert result.diagnostics.mode == "lexical_fallback"
    assert result.diagnostics.fallback_reason == "fts_unavailable"
    assert result.records[0].memory_id == records["project_pdf"].memory_id
    return result.diagnostics.mode


def _verify_quality_metrics(records: dict[str, LongTermMemoryRecord]) -> dict[str, float]:
    cases = (
        ("required_project_constraint", "Atlas 项目的交付归档要求", records["project_pdf"].memory_id),
        ("required_global_preference", "输出时保持中文和简洁", records["global_brief"].memory_id),
        ("diagnostic_semantic", "请把审批说明写得干练一些，去掉冗余内容", records["experience_brief"].memory_id),
    )
    dense_provider = FixtureDenseCandidateProvider(
        {cases[2][1]: [records["experience_brief"].memory_id]}
    )
    required_recall: list[float] = []
    required_mrr: list[float] = []
    diagnostic_recall: list[float] = []
    for case_id, query, expected_id in cases:
        result = search_long_term_memory_retrieval(
            query=query,
            scopes={"global", "project:atlas"},
            dense_candidate_provider=dense_provider if case_id == "diagnostic_semantic" else None,
        )
        ids = [item.memory_id for item in result.records]
        recall = 1.0 if expected_id in ids else 0.0
        reciprocal_rank = 1.0 / (ids.index(expected_id) + 1) if expected_id in ids else 0.0
        if case_id.startswith("required"):
            required_recall.append(recall)
            required_mrr.append(reciprocal_rank)
        else:
            diagnostic_recall.append(recall)
    metrics = {
        "required_recall_at_3": statistics.fmean(required_recall),
        "required_mrr": statistics.fmean(required_mrr),
        "diagnostic_semantic_recall_at_3": statistics.fmean(diagnostic_recall),
    }
    assert metrics["required_recall_at_3"] == 1.0
    assert metrics["required_mrr"] >= 0.95
    assert metrics["diagnostic_semantic_recall_at_3"] >= 0.85
    return metrics


def _verify_10k_bm25_latency() -> float:
    scope = "project:benchmark"
    now = "2026-09-11T00:00:00Z"
    target_id = "memory_benchmark_target"
    rows: list[tuple[object, ...]] = []
    for index in range(9_999):
        title = f"无关记录 {index}"
        summary = "该条合成短事实用于本地 FTS5 性能夹具。"
        tags = ["fixture"]
        rows.append(
            (
                f"memory_benchmark_{index}",
                "experience",
                scope,
                title,
                summary,
                json.dumps(tags, ensure_ascii=False),
                build_memory_fts_shadow([title, summary, *tags]),
                "",
                1,
                1,
                f"benchmark:{index}",
                "",
                now,
                now,
                "",
            )
        )
    target_title = "性能基准 benchmarktoken20260911"
    target_summary = "唯一命中的性能检索目标。"
    target_tags = ["benchmarktoken20260911"]
    rows.append(
        (
            target_id,
            "project_constraint",
            scope,
            target_title,
            target_summary,
            json.dumps(target_tags, ensure_ascii=False),
            build_memory_fts_shadow([target_title, target_summary, *target_tags]),
            "",
            1,
            1,
            "benchmark:target",
            "",
            now,
            now,
            "",
        )
    )
    with get_connection() as connection:
        connection.executemany(
            """
            INSERT INTO long_term_memories(
                memory_id, kind, scope, title, summary, tags_json, retrieval_shadow, source_task_id,
                user_confirmed, enabled, memory_key, replaced_by_memory_id,
                created_at, updated_at, last_used_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    query = "benchmarktoken20260911"
    for _ in range(5):
        warm = search_long_term_memory_retrieval(query=query, scopes={scope})
        assert warm.records and warm.records[0].memory_id == target_id
    samples_ms: list[float] = []
    for _ in range(30):
        started = time.perf_counter()
        result = search_long_term_memory_retrieval(query=query, scopes={scope})
        samples_ms.append((time.perf_counter() - started) * 1_000)
        assert result.records and result.records[0].memory_id == target_id
    p95_ms = _percentile_95(samples_ms)
    assert p95_ms <= 100.0, f"10,000 条本地记忆 BM25 P95 超标：{p95_ms:.2f} ms"
    return p95_ms


def _maybe_verify_real_local_dense(records: dict[str, LongTermMemoryRecord], enabled: bool) -> str:
    capability = vector_index_capability()
    if not enabled:
        return "not_requested"
    if not capability.model_initialized:
        raise RuntimeError("本地 Embedding 模型未由客户确认初始化，拒绝下载或伪造 Hybrid 评测。")
    result = search_long_term_memory_retrieval(
        query="Atlas 项目的交付归档要求",
        scopes={"global", "project:atlas"},
        dense_candidate_provider=LocalMemoryDenseCandidateProvider(),
    )
    assert result.diagnostics.mode == "hybrid_rrf"
    assert result.records and result.records[0].memory_id == records["project_pdf"].memory_id
    return "passed"


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 MEM-5 长期记忆 FTS5/BM25 与 Hybrid 准入评测。")
    parser.add_argument(
        "--with-local-dense",
        action="store_true",
        help="只读复用已确认的 FastEmbed 模型；模型未初始化时失败关闭且不会下载。",
    )
    arguments = parser.parse_args()
    try:
        _verify_legacy_migration()
        records = _create_fixture_records()
        bm25 = _verify_bm25_scope_and_sync(records)
        dense = _verify_dense_contract(records)
        fallback_mode = _verify_fts_failure_fallback(records)
        metrics = _verify_quality_metrics(records)
        bm25_p95_ms = _verify_10k_bm25_latency()
        real_dense = _maybe_verify_real_local_dense(records, arguments.with_local_dense)
        report = {
            "version": "agentflow.memory_retrieval_mem5.v1",
            "default_mode": "bm25",
            "hybrid_default_admitted": False,
            "bm25": bm25,
            "dense_contract": dense,
            "fts_fallback_mode": fallback_mode,
            "metrics": {**metrics, "bm25_10000_p95_ms": round(bm25_p95_ms, 3)},
            "real_local_dense": real_dense,
        }
        print("Commander MEM-5 memory retrieval verification passed: " + json.dumps(report, ensure_ascii=False))
    finally:
        sqlite_service._INITIALIZED_PATHS.clear()
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
