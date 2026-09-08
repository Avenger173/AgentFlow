"""端到端验证 LGM5.7 开发者试点 CLI，不接触真实任务库或外部服务。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_lgm57_cli_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database.task_repository import load_workflow_run, save_workflow_run
from app.schemas.chat import WorkflowPlan, WorkflowStep
from app.schemas.events import TaskLogEvent
from app.schemas.workflow import WorkflowRun


SOURCE_TASK_ID = "task_lgm57_cli_source"
PRIVATE_FIXTURE_TEXT = "cli_private_fixture_text_must_not_be_printed"


def _composition_plan() -> WorkflowPlan:
    """构造最小 C6.4 只读组合计划，供子进程 CLI 在临时库中发现。"""

    specialist_steps = [
        WorkflowStep(
            id="step_2",
            agent="document_agent",
            action="analyze_document",
            title="文档受控分析",
            depends_on=["step_1"],
            parallel_group="specialist_read_only",
        ),
        WorkflowStep(
            id="step_3",
            agent="data_agent",
            action="analyze_dataset",
            title="数据只读预览",
            depends_on=["step_1"],
            parallel_group="specialist_read_only",
        ),
    ]
    return WorkflowPlan(
        workflow_name="verify_lgm57_trial_cli",
        description="LGM5.7 CLI 临时夹具。",
        steps=[
            WorkflowStep(
                id="step_1",
                agent="commander_agent",
                action="analyze_task",
                title="分析用户任务",
                input={"message": PRIVATE_FIXTURE_TEXT},
                execution_mode="planning_only",
            ),
            *specialist_steps,
            WorkflowStep(
                id="step_4",
                agent="commander_agent",
                action="synthesize_results",
                title="汇总已完成结果",
                depends_on=[step.id for step in specialist_steps],
                input={
                    "child_step_ids": [step.id for step in specialist_steps],
                    "composition_mode": "native_read_only_c6_4",
                },
            ),
        ],
    )


def _save_source() -> None:
    save_workflow_run(
        run=WorkflowRun(
            task_id=SOURCE_TASK_ID,
            mode="dry_run",
            status="completed",
            summary="LGM5.7 CLI 临时源计划。",
        ),
        events=[
            TaskLogEvent(
                task_id=SOURCE_TASK_ID,
                sequence=1,
                event="dry_run_completed",
                agent_id="workflow_engine",
                message="fixture",
            )
        ],
        plan=_composition_plan(),
        artifacts=[],
        tool_calls=[],
    )


def _run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["AGENTFLOW_CHAT_MODE"] = "mock"
    environment["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
    environment["PYTHONUTF8"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(BACKEND_ROOT / "scripts" / "prepare_lgm57_composition_trial.py"),
            *arguments,
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    return f"{result.stdout}\n{result.stderr}"


def _assert_no_private_fixture_output(result: subprocess.CompletedProcess[str]) -> None:
    assert PRIVATE_FIXTURE_TEXT not in _combined_output(result)


def main() -> None:
    try:
        _save_source()

        listing = _run_cli("--limit", "20")
        assert listing.returncode == 0, _combined_output(listing)
        assert SOURCE_TASK_ID in listing.stdout
        _assert_no_private_fixture_output(listing)

        refused = _run_cli("--source-task-id", SOURCE_TASK_ID)
        assert refused.returncode != 0
        assert "--confirm-prepare" in _combined_output(refused)
        _assert_no_private_fixture_output(refused)

        prepared = _run_cli(
            "--source-task-id", SOURCE_TASK_ID, "--confirm-prepare"
        )
        assert prepared.returncode == 0, _combined_output(prepared)
        assert "native_runtime_task_id=" in prepared.stdout
        assert "graph_candidate_runtime_task_id=" in prepared.stdout
        _assert_no_private_fixture_output(prepared)

        runtime_ids = {
            line.split("=", 1)[0].removeprefix("- "): line.split("=", 1)[1].strip()
            for line in prepared.stdout.splitlines()
            if line.startswith(
                ("- native_runtime_task_id=", "- graph_candidate_runtime_task_id=")
            )
        }
        assert set(runtime_ids) == {
            "native_runtime_task_id",
            "graph_candidate_runtime_task_id",
        }
        assert (
            runtime_ids["native_runtime_task_id"]
            != runtime_ids["graph_candidate_runtime_task_id"]
        )
        candidate_task_id = runtime_ids["graph_candidate_runtime_task_id"]
        candidate_run = load_workflow_run(candidate_task_id)
        assert candidate_run is not None
        assert candidate_run.status == "pending"
        assert candidate_run.steps[0].status == "completed"
        assert all(step.status == "pending" for step in candidate_run.steps[1:])
        print("LGM5.7 composition trial CLI verification passed.")
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
