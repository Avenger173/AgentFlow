"""验证 LGM5 Commander 组合任务 LangGraph 影子图。

本脚本只构造临时 SQLite checkpoint、确定性 C6.4 计划与假专业 adapter；不读取客户文件、
不调用模型、网络、MCP 或主任务数据库。重点验证并行调用、部分完成恢复和 checkpoint
正文边界，业务 Agent 仍由 Native Runtime 执行。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_lgm5_composition_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.harness.langgraph_commander_composition_shadow import (
    CommanderCompositionInvocation,
    CommanderCompositionOutcome,
    LangGraphCommanderCompositionShadowBackend,
    build_composition_invocations,
)
from app.schemas.chat import WorkflowMaterialBinding
from app.services.agent_catalog import list_agents
from app.services.commander import create_commander_plan


def _material(kind: str, ref: str, name: str) -> WorkflowMaterialBinding:
    return WorkflowMaterialBinding(
        binding_id=f"lgm5_{kind}",
        kind=kind,
        ref=ref,
        display_name=name,
        origin="client_selected",
        usage="LGM5 组合影子回归材料。",
    )


async def _verify() -> None:
    secret_goal = "请结合已选文档和数据分析主要趋势。内部秘密目标：不要写入 LangGraph checkpoint。"
    document_ref = "客户文档秘密名称.md"
    data_ref = "客户数据秘密名称.csv"
    plan = create_commander_plan(
        secret_goal,
        available_agents=list_agents(),
        materials=[
            _material("document", document_ref, "项目说明.md"),
            _material("dataset", data_ref, "销售数据.csv"),
        ],
    )
    invocations, plan_digest = build_composition_invocations(plan)
    assert len(invocations) == 2
    assert {item.agent_id for item in invocations} == {"document_agent", "data_agent"}
    assert all(len(item.material_digest) == 64 and len(item.input_digest) == 64 for item in invocations)
    calls: list[str] = []
    failing_once = {"data_agent"}

    async def adapter(invocation: CommanderCompositionInvocation) -> CommanderCompositionOutcome:
        calls.append(invocation.agent_id)
        await asyncio.sleep(0.01 if invocation.agent_id == "document_agent" else 0.02)
        if invocation.agent_id in failing_once:
            failing_once.remove(invocation.agent_id)
            return CommanderCompositionOutcome(
                invocation_id=invocation.invocation_id,
                status="failed",
                summary="数据分支按夹具要求首次失败。",
                recovery_hint="恢复时只重试数据分支。",
            )
        return CommanderCompositionOutcome(
            invocation_id=invocation.invocation_id,
            status="completed",
            summary=f"{invocation.agent_id} 已完成受控只读结果。",
            delegated_task_id=f"fixture_{invocation.step_id}",
            source_count=2 if invocation.agent_id == "document_agent" else None,
            chart_count=1 if invocation.agent_id == "data_agent" else None,
        )

    events: list[str] = []

    async def event_sink(event) -> None:
        events.append(event.kind)

    checkpoint_path = VERIFY_ROOT / "checkpoints" / "lgm5.db"
    backend = LangGraphCommanderCompositionShadowBackend(
        checkpoint_path=checkpoint_path,
        adapters={
            ("document_agent", "analyze_document"): adapter,
            ("data_agent", "analyze_dataset"): adapter,
        },
    )
    try:
        first = await backend.execute_task(
            task_id="task_lgm5_shadow",
            plan=plan,
            event_sink=event_sink,
        )
        assert first.status == "partial", first
        assert len(first.completed_invocation_ids) == len(first.failed_invocation_ids) == 1
        assert first.delivery["status"] == "partial"
        assert first.delivery["result_scope"].startswith("仅汇总已完成")
        snapshot = await backend.inspect_task("task_lgm5_shadow")
        assert snapshot is not None
        assert snapshot.plan_digest == plan_digest
        assert len(snapshot.completed_invocation_ids) == 1
        assert len(snapshot.failed_invocation_ids) == 1
        assert not snapshot.pending_invocation_ids and not snapshot.next_nodes
        assert calls.count("document_agent") == calls.count("data_agent") == 1

        recovered = await backend.resume_task(
            task_id="task_lgm5_shadow",
            plan=plan,
            event_sink=event_sink,
        )
        assert recovered.resumed and recovered.status == "completed", recovered
        assert len(recovered.completed_invocation_ids) == 2
        assert not recovered.failed_invocation_ids
        assert calls.count("document_agent") == 1
        assert calls.count("data_agent") == 2
        assert events[0] == "runtime_started"
        assert "assistant_final" in events
    finally:
        await backend.close()

    payload = checkpoint_path.read_bytes()
    assert secret_goal.encode("utf-8") not in payload
    assert document_ref.encode("utf-8") not in payload
    assert data_ref.encode("utf-8") not in payload


def main() -> None:
    asyncio.run(_verify())
    print("LGM5 Commander composition shadow verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)
