"""知识库 K3 受约束回答离线回归。

本脚本用固定假模型覆盖“Gate -> 模型 JSON -> 引用 Verifier -> 再次 Gate”的完整后端链，绝不读取
真实模型配置或发送网络请求。它验证的是回答边界和失效行为，不以模型文采作为测试真值。
"""

from __future__ import annotations

import gc
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_knowledge_answer_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / "data" / "knowledge_answer_verify.db")
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from app.database import sqlite as sqlite_service
from app.database.knowledge_repository import create_knowledge_base, import_workspace_documents_to_knowledge_base
from app.database.task_repository import load_task_log_events, load_workflow_run
from app.schemas.knowledge import KnowledgeAnswerRequest
from app.services.knowledge_answer import (
    answer_knowledge_question,
    create_knowledge_answer_queued_run,
    get_knowledge_answer_task_result,
    run_knowledge_answer_task,
)
from app.services.knowledge_keyword_index import create_knowledge_index_job, run_knowledge_index_job
from app.services.workspace_documents import WorkspaceDocumentError, import_workspace_document, resolve_workspace_document_path
from app.services.model_gateway import ModelConversationMessage, ModelToolDefinition, ModelToolTurn


def _write_workspace(filename: str, content: str) -> None:
    """创建或更新临时受控材料，不读取项目中的客户文件。"""

    try:
        resolve_workspace_document_path(filename).write_text(content, encoding="utf-8")
    except WorkspaceDocumentError:
        import_workspace_document(filename=filename, content=content)


def _import_and_index(base_id: str, filename: str, content: str) -> None:
    _write_workspace(filename, content)
    import_workspace_documents_to_knowledge_base(
        knowledge_base_id=base_id,
        workspace_document_names=[filename],
    )
    completed = run_knowledge_index_job(create_knowledge_index_job(base_id).index_job_id)
    assert completed.status == "completed"


class _FixedAnswerModel:
    """可复用的离线模型替身；它只从 Runner 传入的受控 JSON 中取 source_id。"""

    def __init__(
        self,
        *,
        invalid_source: bool = False,
        repair_first: bool = False,
        cite_all_sources: bool = False,
        on_turn=None,
    ) -> None:
        self.invalid_source = invalid_source
        self.repair_first = repair_first
        self.cite_all_sources = cite_all_sources
        self.on_turn = on_turn
        self.turn_count = 0

    async def tool_turn(
        self,
        *,
        system_prompt: str,
        messages: list[ModelConversationMessage],
        tools: list[ModelToolDefinition],
    ) -> ModelToolTurn:
        self.turn_count += 1
        assert not tools
        assert "只能依据本轮提供" in system_prompt
        if self.on_turn is not None and self.turn_count == 1:
            self.on_turn()
        if self.repair_first and self.turn_count == 1:
            return ModelToolTurn(content="这不是合法 JSON")

        source_ids, evidence_state = _source_ids_from_messages(messages)
        source_id = "kb_src_8" if self.invalid_source else source_ids[0]
        used_source_ids = source_ids if self.cite_all_sources and not self.invalid_source else [source_id]
        payload = {
            "answer_markdown": "材料明确要求验收过程保留来源定位，并由负责人确认。",
            "claims": [
                {
                    "claim_id": "kb_claim_1",
                    "statement": "验收过程需要保留来源定位和负责人确认。",
                    "source_ids": used_source_ids,
                }
            ],
            "source_ids": used_source_ids,
            "evidence_state": evidence_state,
            "warnings": [],
        }
        return ModelToolTurn(content=json.dumps(payload, ensure_ascii=False))


def _source_ids_from_messages(messages: list[ModelConversationMessage]) -> tuple[list[str], str]:
    """忽略格式修复指令，找到原始受控证据消息。"""

    for message in messages:
        if message.role != "user":
            continue
        try:
            payload = json.loads(message.content)
        except json.JSONDecodeError:
            continue
        sources = payload.get("sources")
        if not isinstance(sources, list):
            continue
        source_ids = [str(item.get("source_id")) for item in sources if isinstance(item, dict)]
        if source_ids:
            return source_ids, str(payload.get("evidence_state") or "sufficient")
    raise AssertionError("假模型没有收到受控证据上下文。")


def main() -> None:
    try:
        base = create_knowledge_base(name="K3 回答回归")
        _import_and_index(
            base.knowledge_base_id,
            "knowledge_answer.md",
            "# 交付验收\n\n编号 AF-204 的验收需要保留来源定位，并由负责人确认。\n",
        )

        completed_model = _FixedAnswerModel()
        completed = _run(
            base.knowledge_base_id,
            "AF-204 的验收要求是什么？",
            completed_model,
        )
        assert completed.status == "completed"
        assert completed.answer is not None
        assert completed.answer.source_ids == ["kb_src_1"]
        assert completed.model_turn_count == 1
        assert completed.context_route is not None
        assert completed.context_route.route == "retrieval_evidence"
        assert completed.context_route.stage == "knowledge_answer"
        assert completed.context_route.budget_state == "within_budget"
        # 固定假模型不是 Provider Runtime；离线回归不能伪称已经核验某个真实长窗口。
        assert completed.context_route.provider_long_context_state == "not_checked"

        # K3 第三段：后台任务必须在模型调用前进入历史；真实阶段通过注入的回调被观察，完成后
        # 只能从 SQLite 快照恢复受控答案和来源卡，不能把 parent_content 这类模型上下文写进去。
        task_id = "task_kb_a1b2c3d4"
        task_request = KnowledgeAnswerRequest(
            knowledge_base_id=base.knowledge_base_id,
            query="AF-204 的验收要求是什么？",
        )
        queued = create_knowledge_answer_queued_run(task_id=task_id, request=task_request)
        assert queued.status == "pending"
        staged_events: list[tuple[str, str | None]] = []

        async def capture_stage(event: str, _message: str, step_id: str | None, _level: str) -> None:
            staged_events.append((event, step_id))

        task_result = _run_task(task_id, task_request, _FixedAnswerModel(), capture_stage)
        assert task_result.status == "completed"
        assert task_result.result is not None and task_result.result.status == "completed"
        assert staged_events[0] == ("task_started", "knowledge_retrieval")
        assert ("knowledge_retrieval_completed", "knowledge_retrieval") in staged_events
        assert staged_events[-1] == ("task_completed", "knowledge_answer")
        stored_events = load_task_log_events(task_id)
        assert stored_events is not None
        assert [event.event for event in stored_events][-1] == "task_completed"
        stored_run = load_workflow_run(task_id)
        assert stored_run is not None and stored_run.status == "completed"
        stored_answer = next(step for step in stored_run.steps if step.step_id == "knowledge_answer")
        assert "parent_content" not in json.dumps(stored_answer.output, ensure_ascii=False)
        assert stored_answer.output["context_route"]["route"] == "retrieval_evidence"
        assert stored_answer.output["context_route"]["long_context_direct_execution"] is False
        recovered = get_knowledge_answer_task_result(task_id)
        assert recovered is not None and recovered.result is not None
        assert recovered.result.answer is not None and recovered.result.answer.source_ids == ["kb_src_1"]

        blocked_task = _run_task(
            "task_kb_e5f6a7b8",
            KnowledgeAnswerRequest(knowledge_base_id=base.knowledge_base_id, query="ZX-999 的预算上限是什么？"),
            _FixedAnswerModel(),
        )
        assert blocked_task.status == "blocked"
        assert blocked_task.result is not None and blocked_task.result.status == "insufficient_evidence"

        repaired_model = _FixedAnswerModel(repair_first=True)
        repaired = _run(base.knowledge_base_id, "AF-204 的验收要求是什么？", repaired_model)
        assert repaired.status == "completed"
        assert repaired.model_turn_count == 2

        partial = _run(base.knowledge_base_id, "对比 AF-204 与 AF-205 的验收要求", _FixedAnswerModel())
        assert partial.status == "completed"
        assert partial.evidence_gate.evidence_state == "partial"
        assert partial.answer is not None and partial.answer.evidence_state == "partial"

        _import_and_index(
            base.knowledge_base_id,
            "comparison_gate.md",
            "# 对比资料\n\n编号 AF-205 的验收要求包括范围留档和双人确认。\n",
        )
        # Gate 虽然覆盖两份资料，但模型若只引用第一份，不能伪装成充分的比较回答。
        incomplete_comparison = _run(
            base.knowledge_base_id,
            "对比 AF-204 与 AF-205 的验收要求",
            _FixedAnswerModel(),
        )
        assert incomplete_comparison.status == "failed"
        assert incomplete_comparison.stop_reason == "model_output_invalid"

        complete_comparison = _run(
            base.knowledge_base_id,
            "对比 AF-204 与 AF-205 的验收要求",
            _FixedAnswerModel(cite_all_sources=True),
        )
        assert complete_comparison.status == "completed"
        assert complete_comparison.answer is not None
        assert complete_comparison.answer.evidence_state == "sufficient"
        assert len(complete_comparison.answer.source_ids) >= 2

        invalid = _run(base.knowledge_base_id, "AF-204 的验收要求是什么？", _FixedAnswerModel(invalid_source=True))
        assert invalid.status == "failed"
        assert invalid.stop_reason == "model_output_invalid"
        assert "来源引用校验" in invalid.message

        no_evidence_model = _FixedAnswerModel()
        insufficient = _run(base.knowledge_base_id, "ZX-999 的预算上限是什么？", no_evidence_model)
        assert insufficient.status == "insufficient_evidence"
        assert insufficient.model_turn_count == 0
        assert no_evidence_model.turn_count == 0

        def update_generation() -> None:
            _import_and_index(
                base.knowledge_base_id,
                "knowledge_answer.md",
                "# 交付验收\n\n编号 AF-310 的验收需要双人确认和范围留档。\n",
            )

        stale = _run(
            base.knowledge_base_id,
            "AF-204 的验收要求是什么？",
            _FixedAnswerModel(on_turn=update_generation),
        )
        assert stale.status == "failed"
        assert stale.stop_reason == "evidence_changed"
        assert stale.answer is None

        # API 只验证 HTTP 契约；用 patch 注入相同假模型，确保测试绝不意外命中客户配置的真实模型。
        from app.api import knowledge as knowledge_api
        from main import app

        async def fake_answer_endpoint(request: KnowledgeAnswerRequest):
            return await answer_knowledge_question(request, model=_FixedAnswerModel())

        with patch.object(knowledge_api, "answer_knowledge_question", fake_answer_endpoint):
            client = TestClient(app)
            response = client.post(
                "/api/knowledge/answer",
                json={
                    "knowledge_base_id": base.knowledge_base_id,
                    "query": "AF-310 的验收要求是什么？",
                },
            )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "completed"
        assert response.json()["answer"]["source_ids"] == ["kb_src_1"]

        # 异步 API 只应返回受理回执；在模块边界注入假 Runtime 后，确认后台任务最终能从结果接口
        # 回读，而整个过程不读取真实模型配置或网络。
        async def fake_task_runtime(*, task_id: str, request: KnowledgeAnswerRequest, progress_callback=None):
            return await run_knowledge_answer_task(
                task_id=task_id,
                request=request,
                model=_FixedAnswerModel(),
                progress_callback=progress_callback,
            )

        with patch.object(knowledge_api, "run_knowledge_answer_task", fake_task_runtime):
            start = client.post(
                "/api/knowledge/answer/start",
                json={
                    "knowledge_base_id": base.knowledge_base_id,
                    "query": "AF-310 的验收要求是什么？",
                },
            )
            assert start.status_code == 202, start.text
            started_task_id = start.json()["task_id"]
            # Starlette TestClient 会在一次同步 request 结束时收束事件循环，因此这里不能把它的
            # create_task 生命周期误当作常驻 Uvicorn。API 层只验证立即受理和初始可回读状态；
            # 真实终态、事件顺序和 SQLite 恢复已由上面的服务层任务回归覆盖。
            accepted = client.get(f"/api/knowledge/answers/{started_task_id}/result")
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["status"] in {"pending", "running", "completed"}

        print("Knowledge K3 answer verification passed: citations, repair, task lifecycle, partial, rejection, stale generation and API.")
    finally:
        _cleanup_verify_root()


def _run(knowledge_base_id: str, query: str, model: _FixedAnswerModel):
    """同步脚本中以独立事件循环运行服务，避免把测试辅助逻辑混进生产服务。"""

    import asyncio

    return asyncio.run(
        answer_knowledge_question(
            KnowledgeAnswerRequest(knowledge_base_id=knowledge_base_id, query=query),
            model=model,
        )
    )


def _run_task(task_id: str, request: KnowledgeAnswerRequest, model: _FixedAnswerModel, callback=None):
    """以假模型跑一次可恢复 K3 后台任务，不读取真实 Provider 配置。"""

    import asyncio

    return asyncio.run(
        run_knowledge_answer_task(
            task_id=task_id,
            request=request,
            model=model,
            progress_callback=callback,
        )
    )


def _cleanup_verify_root() -> None:
    """Windows 上失败 traceback 偶尔会短暂持有 SQLite 文件；有限重试后仍失败才显式报错。"""

    sqlite_service._INITIALIZED_PATHS.clear()
    last_error: PermissionError | None = None
    for _ in range(20):
        gc.collect()
        try:
            shutil.rmtree(VERIFY_ROOT, ignore_errors=False)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.1)
    if last_error is not None:
        raise last_error


if __name__ == "__main__":
    main()
