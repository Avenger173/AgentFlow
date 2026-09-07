"""验证 LGM5.3 主任务/Graph checkpoint bridge 的幂等与恢复边界。

本脚本只使用临时 SQLite、确定性专业调用替身和 LGM5 影子图。它不会读取客户文件、调用模型、
联网或 MCP；“秘密”字符串用于确认 bridge 记录和 Graph checkpoint 都不复制客户正文或材料名。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_lgm5_bridge_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database.langgraph_bridge_repository import (
    LangGraphBridgeConflictError,
    ensure_langgraph_composition_bridge,
    load_langgraph_composition_bridge,
)
from app.database.task_repository import save_workflow_run
from app.harness.langgraph_commander_composition_bridge import (
    build_composition_bridge_record,
    mark_composition_bridge_running,
    sync_composition_bridge_result,
)
from app.harness.langgraph_commander_composition_shadow import (
    CommanderCompositionInvocation,
    CommanderCompositionOutcome,
    LangGraphCommanderCompositionShadowBackend,
)
from app.schemas.chat import WorkflowPlan, WorkflowStep
from app.schemas.events import TaskLogEvent
from app.schemas.workflow import WorkflowRun


_RUNTIME_TASK_ID = "task_lgm5_bridge_runtime"
_SECRET_GOAL = "秘密目标：不要把这段客户正文写入 bridge 或 Graph checkpoint。"
_SECRET_DOCUMENT_REF = "客户机密材料名称.md"
_SECRET_DATASET_REF = "客户机密数据名称.csv"


def _plan(*, version: int = 1) -> WorkflowPlan:
    return WorkflowPlan(
        workflow_name="verify_lgm5_composition_bridge",
        description="LGM5.3 脱敏 bridge 夹具。",
        plan_version=version,
        user_goal=_SECRET_GOAL,
        material_bindings=[
            {
                "binding_id": "material_document",
                "kind": "document",
                "ref": _SECRET_DOCUMENT_REF,
                "display_name": "说明材料.md",
                "origin": "client_selected",
                "usage": "LGM5.3 夹具。",
            },
            {
                "binding_id": "material_dataset",
                "kind": "dataset",
                "ref": _SECRET_DATASET_REF,
                "display_name": "样本数据.csv",
                "origin": "client_selected",
                "usage": "LGM5.3 夹具。",
            },
        ],
        steps=[
            WorkflowStep(
                id="step_1",
                agent="commander_agent",
                action="analyze_task",
                title="分析用户任务",
                input={"message": _SECRET_GOAL},
                execution_mode="planning_only",
            ),
            WorkflowStep(
                id="step_2",
                agent="document_agent",
                action="analyze_document",
                title="文档受控分析",
                depends_on=["step_1"],
                parallel_group="specialist_read_only",
                input={"document_refs": [_SECRET_DOCUMENT_REF]},
            ),
            WorkflowStep(
                id="step_3",
                agent="data_agent",
                action="analyze_dataset",
                title="数据只读预览",
                depends_on=["step_1"],
                parallel_group="specialist_read_only",
                input={"dataset_refs": [_SECRET_DATASET_REF]},
            ),
            WorkflowStep(
                id="step_4",
                agent="commander_agent",
                action="synthesize_results",
                title="汇总已完成结果",
                depends_on=["step_2", "step_3"],
                input={
                    "child_step_ids": ["step_2", "step_3"],
                    "composition_mode": "native_read_only_c6_4",
                },
            ),
        ],
    )


async def _verify() -> None:
    plan = _plan()
    save_workflow_run(
        run=WorkflowRun(
            task_id=_RUNTIME_TASK_ID,
            mode="runtime",
            status="pending",
            summary="LGM5.3 组合 bridge 临时夹具。",
        ),
        events=[
            TaskLogEvent(
                task_id=_RUNTIME_TASK_ID,
                sequence=1,
                event="runtime_accepted",
                agent_id="workflow_engine",
                message="fixture",
            )
        ],
        plan=plan,
        artifacts=[],
        tool_calls=[],
    )
    initial = build_composition_bridge_record(runtime_task_id=_RUNTIME_TASK_ID, plan=plan)
    assert ensure_langgraph_composition_bridge(initial) == initial
    assert ensure_langgraph_composition_bridge(initial) == initial
    assert load_langgraph_composition_bridge(_RUNTIME_TASK_ID) == initial

    try:
        ensure_langgraph_composition_bridge(
            build_composition_bridge_record(runtime_task_id=_RUNTIME_TASK_ID, plan=_plan(version=2))
        )
    except LangGraphBridgeConflictError:
        pass
    else:
        raise AssertionError("同一 Runtime 任务必须拒绝不同计划摘要的 bridge。")

    calls: list[str] = []
    fail_once = {"data_agent"}

    async def adapter(invocation: CommanderCompositionInvocation) -> CommanderCompositionOutcome:
        calls.append(invocation.agent_id)
        if invocation.agent_id in fail_once:
            fail_once.remove(invocation.agent_id)
            return CommanderCompositionOutcome(
                invocation_id=invocation.invocation_id,
                status="failed",
                summary="fixture：数据分支首次失败。",
                recovery_hint="恢复时只重试数据分支。",
            )
        return CommanderCompositionOutcome(
            invocation_id=invocation.invocation_id,
            status="completed",
            summary=f"fixture：{invocation.agent_id} 已完成。",
            delegated_task_id=f"fixture_{invocation.step_id}",
            source_count=1,
        )

    checkpoint_path = VERIFY_ROOT / "checkpoints" / "composition.db"
    backend = LangGraphCommanderCompositionShadowBackend(
        checkpoint_path=checkpoint_path,
        adapters={
            ("document_agent", "analyze_document"): adapter,
            ("data_agent", "analyze_dataset"): adapter,
        },
    )
    try:
        running = mark_composition_bridge_running(runtime_task_id=_RUNTIME_TASK_ID)
        assert running.status == "running"
        first = await backend.execute_task(task_id=_RUNTIME_TASK_ID, plan=plan)
        partial = sync_composition_bridge_result(runtime_task_id=_RUNTIME_TASK_ID, result=first)
        assert first.status == partial.status == "partial"
        assert partial.delivery_state == "partial"
        assert len(partial.completed_invocation_ids) == len(partial.failed_invocation_ids) == 1

        resumed_running = mark_composition_bridge_running(runtime_task_id=_RUNTIME_TASK_ID)
        assert resumed_running.status == "running"
        recovered = await backend.resume_task(task_id=_RUNTIME_TASK_ID, plan=plan)
        completed = sync_composition_bridge_result(runtime_task_id=_RUNTIME_TASK_ID, result=recovered)
        assert recovered.status == completed.status == "completed"
        assert completed.delivery_state == "completed"
        assert len(completed.completed_invocation_ids) == 2
        assert not completed.failed_invocation_ids
        assert calls.count("document_agent") == 1
        assert calls.count("data_agent") == 2
        try:
            mark_composition_bridge_running(runtime_task_id=_RUNTIME_TASK_ID)
        except LangGraphBridgeConflictError:
            pass
        else:
            raise AssertionError("已完成的 bridge 不能再次进入运行态。")
    finally:
        await backend.close()

    with sqlite3.connect(VERIFY_ROOT / "data" / "agentflow.db") as connection:
        bridge_json = connection.execute(
            "SELECT bridge_json FROM langgraph_runtime_bridges WHERE runtime_task_id = ?",
            (_RUNTIME_TASK_ID,),
        ).fetchone()[0]
    assert _SECRET_GOAL not in bridge_json
    assert _SECRET_DOCUMENT_REF not in bridge_json
    assert _SECRET_DATASET_REF not in bridge_json
    checkpoint_bytes = checkpoint_path.read_bytes()
    assert _SECRET_GOAL.encode("utf-8") not in checkpoint_bytes
    assert _SECRET_DOCUMENT_REF.encode("utf-8") not in checkpoint_bytes
    assert _SECRET_DATASET_REF.encode("utf-8") not in checkpoint_bytes


def main() -> None:
    asyncio.run(_verify())
    print("LGM5 composition bridge mapping verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)
