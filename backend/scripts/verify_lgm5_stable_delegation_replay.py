"""验证 LGM5 稳定调用键只复用已完成的只读专业子任务。

脚本通过受控回读夹具覆盖文档、数据和知识库 handoff：同一 delegation call ID 必须映射到
同一子任务 ID，并且命中已完成快照时不得再次创建任务、读取材料或调用模型。它不连接真实
模型、网络、MCP 或客户文件。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_lgm5_stable_delegation_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
sys.path.insert(0, str(BACKEND_ROOT))

from app.schemas.chat import WorkflowStep
from app.database.task_repository import save_workflow_run
from app.schemas.workflow import WorkflowRun, WorkflowStepRun
from app.workflow import runtime


_CALL_ID = "lgm5call_0123456789abcdef01234567"


class _DocumentContextFixture:
    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return {"documents": [], "warnings": [], "missing_context": [], "confidence": "high"}


def _step(*, agent: str, action: str, payload: dict[str, object]) -> WorkflowStep:
    return WorkflowStep(
        id=f"step_{agent}",
        agent=agent,
        action=action,
        title=f"fixture {agent}",
        input={**payload, "_agentflow_delegation_call_id": _CALL_ID},
    )


def _reject(*args, **kwargs):
    raise AssertionError("命中已完成稳定子任务时不得创建或执行新的专业任务。")


async def _reject_async(*args, **kwargs):
    _reject(*args, **kwargs)


def main() -> None:
    document_id = f"task_document_{_CALL_ID}"
    data_id = f"task_data_preview_{_CALL_ID}"
    knowledge_id = f"task_kb_{_CALL_ID}"

    document_response = SimpleNamespace(
        task_id=document_id,
        mode="mock",
        status="completed",
        stop_reason="completed",
        reply="fixture：文档子任务已完成。",
        document_context=_DocumentContextFixture(),
    )
    knowledge_answer = SimpleNamespace(
        source_ids=["source_fixture"],
    )
    knowledge_response = SimpleNamespace(
        task_id=knowledge_id,
        status="completed",
        message="fixture：知识库子任务已完成。",
        result=SimpleNamespace(
            answer=knowledge_answer,
            stop_reason="completed",
            retrieval_diagnostics=SimpleNamespace(mode="hybrid"),
        ),
    )

    original_document_get = runtime.get_document_agent_result
    original_document_run = runtime.run_document_agent
    original_data_create = runtime.create_data_analysis_preview_queued_run
    original_data_run = runtime.run_data_analysis_preview_task
    original_knowledge_get = runtime.get_knowledge_answer_task_result
    original_knowledge_create = runtime.create_knowledge_answer_queued_run
    original_knowledge_run = runtime.run_knowledge_answer_task

    runtime.get_document_agent_result = lambda task_id: document_response if task_id == document_id else None
    runtime.run_document_agent = _reject_async
    runtime.create_data_analysis_preview_queued_run = _reject
    runtime.run_data_analysis_preview_task = _reject_async
    runtime.get_knowledge_answer_task_result = lambda task_id: knowledge_response if task_id == knowledge_id else None
    runtime.create_knowledge_answer_queued_run = _reject
    runtime.run_knowledge_answer_task = _reject_async
    try:
        # 数据委派的回读入口没有伪造：写入最小历史快照后，由实际 Repository 查询并恢复结果。
        save_workflow_run(
            run=WorkflowRun(
                task_id=data_id,
                mode="runtime",
                status="completed",
                summary="fixture：数据子任务已完成。",
                steps=[
                    WorkflowStepRun(
                        step_id="data_analysis_preview",
                        agent="data_agent",
                        action="data.preview_analysis",
                        status="completed",
                        message="fixture：数据子任务已完成。",
                        output={
                            "result": {
                                "delegated_task_id": data_id,
                                "agent_status": "completed",
                                "reply": "fixture：数据子任务已完成。",
                                "source_sha256": "a" * 64,
                                "insight_mode": "local",
                                "chart_count": 2,
                                "table_count": 1,
                            }
                        },
                    )
                ],
            ),
            events=[],
            plan=None,
            artifacts=[],
            tool_calls=[],
        )
        document_step = _step(
            agent="document_agent",
            action="analyze_document",
            payload={"task_goal": "fixture", "document_refs": ["fixture.md"], "output_mode": "auto"},
        )
        document_run, _, document_artifacts = runtime._execute_document_agent_handoff(
            runtime_task_id="task_parent_fixture",
            step=document_step,
            attempt=1,
        )
        assert document_run.status == "completed"
        assert document_run.output["result"]["delegated_task_id"] == document_id
        assert document_artifacts[0].uri == f"agentflow-task://{document_id}"

        data_step = _step(
            agent="data_agent",
            action="analyze_dataset",
            payload={
                "dataset_refs": ["fixture.csv"],
                "dataset_name": "fixture.csv",
                "task_goal": "fixture",
                "cleaning_policy": "safe",
                "max_chart_count": 2,
            },
        )
        data_run, _, data_artifacts = runtime._execute_data_analysis_handoff(
            runtime_task_id="task_parent_fixture",
            step=data_step,
            attempt=1,
        )
        assert data_run.status == "completed"
        assert data_run.output["result"]["delegated_task_id"] == data_id
        assert data_artifacts[0].uri == f"agentflow-task://{data_id}"

        knowledge_step = _step(
            agent="knowledge_agent",
            action="answer_question",
            payload={"knowledge_base_id": "kb_fixture123", "query": "fixture question"},
        )
        knowledge_run, _, knowledge_artifacts = runtime._execute_knowledge_agent_handoff(
            runtime_task_id="task_parent_fixture",
            step=knowledge_step,
            attempt=1,
        )
        assert knowledge_run.status == "completed"
        assert knowledge_run.output["result"]["delegated_task_id"] == knowledge_id
        assert knowledge_artifacts[0].uri == f"agentflow-task://{knowledge_id}"

        normal_step = WorkflowStep(id="step_native", agent="document_agent", action="analyze_document", title="native")
        normal_id = runtime._delegated_task_id_for_step(task_prefix="task_document", step=normal_step)
        assert normal_id.startswith("task_document_")
        assert _CALL_ID not in normal_id
    finally:
        runtime.get_document_agent_result = original_document_get
        runtime.run_document_agent = original_document_run
        runtime.create_data_analysis_preview_queued_run = original_data_create
        runtime.run_data_analysis_preview_task = original_data_run
        runtime.get_knowledge_answer_task_result = original_knowledge_get
        runtime.create_knowledge_answer_queued_run = original_knowledge_create
        runtime.run_knowledge_answer_task = original_knowledge_run

    print("LGM5 stable delegation replay verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True)
