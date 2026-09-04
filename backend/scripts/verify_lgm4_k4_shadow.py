"""LGM4：以冻结 K4 夹具对照 Native 与 LangGraph 影子执行。

本脚本只使用临时 data/database/checkpointer 和确定性假模型，不读取客户材料、不加载模型配置、
不调用真实 Provider、网络或 MCP。它验证影子图没有重写业务算法，而是精确复用 K4 的范围、
Map/Reduce checkpoint、来源闭合与报告资格。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from hashlib import sha256
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_lgm4_k4_shadow_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / "data" / "lgm4_k4_shadow.db")
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import sqlite as sqlite_service
from app.database.knowledge_repository import create_knowledge_base, import_workspace_documents_to_knowledge_base
from app.harness.langgraph_k4_shadow import LangGraphK4ShadowBackend
from app.schemas.knowledge import KnowledgeDeepTaskRequest
from app.services.knowledge_deep_task import (
    build_knowledge_deep_task_scope,
    create_knowledge_deep_task_map_queued_run,
    get_knowledge_deep_task_result,
    request_knowledge_deep_task_cancel,
    run_knowledge_deep_task,
)
from app.services.knowledge_keyword_index import create_knowledge_index_job, run_knowledge_index_job
from app.services.model_gateway import ModelConversationMessage, ModelToolDefinition, ModelToolTurn
from app.services.workspace_documents import import_workspace_document


class _K4FixtureModel:
    """稳定输出 K4 的最小结构化草稿，并记录实际被调用的节点。"""

    def __init__(
        self,
        *,
        failing_map_unit_id: str = "",
        fail_reduce: bool = False,
        on_map_turn=None,
    ) -> None:
        self.failing_map_unit_id = failing_map_unit_id
        self.fail_reduce = fail_reduce
        self.on_map_turn = on_map_turn
        self.map_unit_ids: list[str] = []
        self.reduce_turn_count = 0

    async def tool_turn(
        self,
        *,
        system_prompt: str,
        messages: list[ModelConversationMessage],
        tools: list[ModelToolDefinition],
    ) -> ModelToolTurn:
        assert not tools
        if "Reduce 分析器" in system_prompt:
            self.reduce_turn_count += 1
            if self.fail_reduce:
                return ModelToolTurn(content="not-json")
            return ModelToolTurn(
                content=json.dumps(
                    {
                        "overview": "已基于冻结章节的小结完成分层归纳。",
                        "findings": ["交付与风险约束需要一并进入最终报告。"],
                        "conflicts": [],
                        "warnings": [],
                    },
                    ensure_ascii=False,
                )
            )
        payload = _map_payload(messages)
        unit = payload["map_unit"]
        assert isinstance(unit, dict)
        unit_id = str(unit["map_unit_id"])
        self.map_unit_ids.append(unit_id)
        if self.on_map_turn is not None:
            self.on_map_turn()
        if unit_id == self.failing_map_unit_id:
            return ModelToolTurn(content="not-json")
        return ModelToolTurn(
            content=json.dumps(
                {
                    "summary": f"{unit['document_name']} 已完成受控章节小结。",
                    "findings": ["章节包含可追溯的交付或风险约束。"],
                    "warnings": [],
                },
                ensure_ascii=False,
            )
        )


def _map_payload(messages: list[ModelConversationMessage]) -> dict[str, object]:
    for message in messages:
        if message.role != "user":
            continue
        try:
            payload = json.loads(message.content)
        except json.JSONDecodeError:
            continue
        if isinstance(payload.get("map_unit"), dict) and "chapter_content" in payload:
            return payload
    raise AssertionError("K4 fixture 没有收到当前唯一章节。")


def _digest_result(task_id: str) -> str:
    result = get_knowledge_deep_task_result(task_id)
    assert result is not None and result.result is not None
    payload = json.dumps(result.result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _index_active_materials(*, knowledge_base_id: str, document_names: list[str]) -> None:
    import_workspace_documents_to_knowledge_base(
        knowledge_base_id=knowledge_base_id,
        workspace_document_names=document_names,
    )
    completed = run_knowledge_index_job(create_knowledge_index_job(knowledge_base_id).index_job_id)
    assert completed.status == "completed", completed.failure_summaries


async def _verify_success(*, checkpoint_root: Path, scope) -> str:
    native_task_id = "task_k4_lgm4native"
    create_knowledge_deep_task_map_queued_run(task_id=native_task_id, scope=scope)
    native_model = _K4FixtureModel()
    native = await run_knowledge_deep_task(task_id=native_task_id, scope=scope, model=native_model)
    assert native.status == "completed" and native.result is not None
    native_digest = _digest_result(native_task_id)
    shadow_model = _K4FixtureModel()
    events: list[str] = []

    async def collect(event) -> None:
        events.append(event.kind)

    backend = LangGraphK4ShadowBackend(
        checkpoint_path=checkpoint_root / "success.db",
        scope=scope,
        model=shadow_model,
    )
    try:
        shadow = await backend.execute_task("task_k4_lgm4success", event_sink=collect)
        assert shadow.status == "completed", shadow
        assert shadow.result_digest == native_digest
        assert native_model.map_unit_ids == shadow_model.map_unit_ids
        assert native_model.reduce_turn_count == shadow_model.reduce_turn_count == 2
        assert shadow.metrics.graph_checkpoint_node_total == 4
        assert shadow.metrics.native_step_total == shadow.metrics.native_step_completed == 4
        assert shadow.metrics.native_step_failed == 0
        assert shadow.metrics.graph_elapsed_ms >= 0 and shadow.metrics.native_duration_ms >= 0
        assert events[0] == "runtime_started"
        assert "runtime_heartbeat" in events and events[-1] == "assistant_final"
        snapshot = await backend.inspect_task("task_k4_lgm4success")
        assert snapshot is not None
        assert snapshot.completed_nodes == ("scope_frozen", "map_completed", "reduce_completed", "delivery_verified")
        assert not snapshot.next_nodes
        result = get_knowledge_deep_task_result("task_k4_lgm4success")
        assert result is not None and result.coverage is not None and result.coverage.state == "complete"
        assert result.report_readiness is not None and result.report_readiness.can_export
    finally:
        await backend.close()
    return native_digest


async def _verify_map_recovery(*, checkpoint_root: Path, scope, native_digest: str) -> None:
    task_id = "task_k4_lgm4maprec"
    failing = _K4FixtureModel(failing_map_unit_id=scope.map_units[1].map_unit_id)
    backend = LangGraphK4ShadowBackend(
        checkpoint_path=checkpoint_root / "map_recovery.db",
        scope=scope,
        model=failing,
    )
    try:
        stopped = await backend.execute_task(task_id)
        assert stopped.status == "blocked" and stopped.stage == "map", stopped
        assert failing.map_unit_ids[0] == scope.map_units[0].map_unit_id
        snapshot = await backend.inspect_task(task_id)
        assert snapshot is not None and snapshot.completed_nodes == ("scope_frozen",)
        assert snapshot.next_nodes == ("map_completed",)
    finally:
        await backend.close()

    recovered_model = _K4FixtureModel()
    recovered_backend = LangGraphK4ShadowBackend(
        checkpoint_path=checkpoint_root / "map_recovery.db",
        scope=scope,
        model=recovered_model,
    )
    try:
        recovered = await recovered_backend.resume_task(task_id)
        assert recovered.status == "completed" and recovered.resumed
        assert recovered.result_digest == native_digest
        assert recovered_model.map_unit_ids == [scope.map_units[1].map_unit_id]
        assert recovered_model.reduce_turn_count == 2
    finally:
        await recovered_backend.close()


async def _verify_reduce_recovery(*, checkpoint_root: Path, scope, native_digest: str) -> None:
    task_id = "task_k4_lgm4redrec"
    failing = _K4FixtureModel(fail_reduce=True)
    backend = LangGraphK4ShadowBackend(
        checkpoint_path=checkpoint_root / "reduce_recovery.db",
        scope=scope,
        model=failing,
    )
    try:
        stopped = await backend.execute_task(task_id)
        assert stopped.status == "blocked" and stopped.stage == "reduce", stopped
        snapshot = await backend.inspect_task(task_id)
        assert snapshot is not None
        assert snapshot.completed_nodes == ("scope_frozen", "map_completed")
        assert snapshot.next_nodes == ("reduce_completed",)
    finally:
        await backend.close()

    recovered_model = _K4FixtureModel()
    recovered_backend = LangGraphK4ShadowBackend(
        checkpoint_path=checkpoint_root / "reduce_recovery.db",
        scope=scope,
        model=recovered_model,
    )
    try:
        recovered = await recovered_backend.resume_task(task_id)
        assert recovered.status == "completed" and recovered.resumed
        assert recovered.result_digest == native_digest
        assert recovered_model.map_unit_ids == []
        assert recovered_model.reduce_turn_count == 2
    finally:
        await recovered_backend.close()


async def _verify_cancellation(*, checkpoint_root: Path, scope) -> None:
    task_id = "task_k4_lgm4cancel"

    def cancel_after_first_map() -> None:
        response = request_knowledge_deep_task_cancel(task_id)
        assert response is not None and response.accepted

    backend = LangGraphK4ShadowBackend(
        checkpoint_path=checkpoint_root / "cancel.db",
        scope=scope,
        model=_K4FixtureModel(on_map_turn=cancel_after_first_map),
    )
    try:
        cancelled = await backend.execute_task(task_id)
        assert cancelled.status == "cancelled" and cancelled.stage == "map", cancelled
        result = get_knowledge_deep_task_result(task_id)
        assert result is not None and result.coverage is not None
        assert result.coverage.state == "partial"
    finally:
        await backend.close()


async def _verify_stale_scope(*, checkpoint_root: Path, scope) -> None:
    model = _K4FixtureModel()
    backend = LangGraphK4ShadowBackend(
        checkpoint_path=checkpoint_root / "stale.db",
        scope=scope,
        model=model,
    )
    try:
        stale = await backend.execute_task("task_k4_lgm4stale")
        assert stale.status == "failed" and stale.stage == "scope", stale
        assert model.map_unit_ids == [] and model.reduce_turn_count == 0
    finally:
        await backend.close()


async def main() -> None:
    checkpoint_root = VERIFY_ROOT / "langgraph"
    try:
        import_workspace_document(
            filename="lgm4_delivery.md",
            content="# 交付约束\n\nLGM4_ONLY_DELIVERY：交付前必须固定验收范围。\n",
        )
        import_workspace_document(
            filename="lgm4_risk.md",
            content="# 风险约束\n\nLGM4_ONLY_RISK：发布前必须完成风险复核。\n",
        )
        base = create_knowledge_base(name="LGM4 K4 影子对照")
        _index_active_materials(
            knowledge_base_id=base.knowledge_base_id,
            document_names=["lgm4_delivery.md", "lgm4_risk.md"],
        )
        scope = build_knowledge_deep_task_scope(
            KnowledgeDeepTaskRequest(
                knowledge_base_id=base.knowledge_base_id,
                task_kind="audit",
                task_goal="核对交付与风险约束。",
            )
        )
        assert len(scope.map_units) == 2
        native_digest = await _verify_success(checkpoint_root=checkpoint_root, scope=scope)
        await _verify_map_recovery(checkpoint_root=checkpoint_root, scope=scope, native_digest=native_digest)
        await _verify_reduce_recovery(checkpoint_root=checkpoint_root, scope=scope, native_digest=native_digest)
        await _verify_cancellation(checkpoint_root=checkpoint_root, scope=scope)

        # 活动 generation 变化后，影子图必须在 scope 节点拒绝旧范围，不能读取任何章节或调用模型。
        updated = import_workspace_document(
            filename="lgm4_delivery.md",
            content="# 交付约束\n\nLGM4_ONLY_UPDATED：新的索引版本已经替换旧范围。\n",
        )
        _index_active_materials(
            knowledge_base_id=base.knowledge_base_id,
            document_names=[updated.relative_path, "lgm4_risk.md"],
        )
        await _verify_stale_scope(checkpoint_root=checkpoint_root, scope=scope)

        # checkpoint 只含稳定摘要，绝不能持有章节正文标记。
        checkpoint_bytes = b"".join(path.read_bytes() for path in checkpoint_root.glob("*.db"))
        assert b"LGM4_ONLY_DELIVERY" not in checkpoint_bytes
        assert b"LGM4_ONLY_RISK" not in checkpoint_bytes
        assert b"LGM4_ONLY_UPDATED" not in checkpoint_bytes
    finally:
        sqlite_service._INITIALIZED_PATHS.clear()
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)

    print(
        "LGM4 K4 shadow verification passed: Native and LangGraph final results match; "
        "Map/Reduce recovery skips completed work, cancellation and stale generations remain bounded."
    )


if __name__ == "__main__":
    asyncio.run(main())
