"""验证 C6.5.3 专业子任务模型审计的持久化与只读历史接口。

脚本只使用临时 SQLite 和固定快照，不会启动真实模型请求、读取客户材料或使用任何本机密钥。
它覆盖单阶段子任务、同一任务多阶段/多模型、旧任务缺失快照与 API Key 脱敏四个边界。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_model_route_audit_c653_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from app.database.task_repository import load_workflow_run, save_workflow_run
from app.schemas.model import ModelRouteAuditSnapshot
from app.schemas.workflow import WorkflowRun
from main import app


def _snapshot(
    *,
    stage: str,
    route_id: str,
    provider: str,
    label: str,
    model: str,
    thinking: str = "disabled",
) -> ModelRouteAuditSnapshot:
    """构造与真实 Runtime 相同形状的脱敏审计快照，不模拟网络或模型正文。"""

    return ModelRouteAuditSnapshot(
        stage=stage,
        route_id=route_id,  # type: ignore[arg-type]
        profile_id=f"route:{route_id}",
        mode="configured",
        provider=provider,
        label=label,
        model=model,
        thinking=thinking,  # type: ignore[arg-type]
        compatibility="ready",
        note="离线审计夹具。",
    )


def _save_run(task_id: str, routes: list[ModelRouteAuditSnapshot]) -> None:
    save_workflow_run(
        run=WorkflowRun(
            task_id=task_id,
            mode="runtime",
            status="completed",
            summary="C6.5.3 离线模型审计夹具",
            model_routes=routes,
        ),
        events=[],
        plan=None,
    )


def main() -> None:
    single_route = _snapshot(
        stage="document_analysis",
        route_id="document_analysis",
        provider="deepseek",
        label="DeepSeek",
        model="deepseek-v4-flash",
        thinking="enabled",
    )
    multi_routes = [
        _snapshot(
            stage="knowledge_deep_map",
            route_id="knowledge_deep_analysis",
            provider="deepseek",
            label="DeepSeek",
            model="deepseek-v4-pro",
            thinking="enabled",
        ),
        _snapshot(
            stage="knowledge_deep_reduce",
            route_id="knowledge_deep_analysis",
            provider="kimi",
            label="Kimi",
            model="kimi-k2.5",
        ),
    ]
    _save_run("task_c653_single", [single_route])
    _save_run("task_c653_multi", multi_routes)
    _save_run("task_c653_legacy", [])

    # 即使调用方错误地附带 Key 字段，Pydantic 审计模型也不会序列化未声明字段。
    key_probe = ModelRouteAuditSnapshot.model_validate(
        {
            **single_route.model_dump(),
            "api_key": "test-api-key-placeholder",
            "base_url": "https://account-token.example.invalid/v1",
        }
    )
    serialized_probe = key_probe.model_dump_json()
    assert "test-api-key-placeholder" not in serialized_probe
    assert "account-token.example.invalid" not in serialized_probe

    persisted_multi = load_workflow_run("task_c653_multi")
    assert persisted_multi is not None
    assert [(item.stage, item.provider, item.model) for item in persisted_multi.model_routes] == [
        ("knowledge_deep_map", "deepseek", "deepseek-v4-pro"),
        ("knowledge_deep_reduce", "kimi", "kimi-k2.5"),
    ]

    with TestClient(app) as client:
        single_response = client.get("/api/tasks/task_c653_single/model-routes")
        single_response.raise_for_status()
        assert single_response.json() == {
            "task_id": "task_c653_single",
            "model_routes": [single_route.model_dump()],
        }

        multi_response = client.get("/api/tasks/task_c653_multi/model-routes")
        multi_response.raise_for_status()
        multi_payload = multi_response.json()
        assert multi_payload["task_id"] == "task_c653_multi"
        assert [item["stage"] for item in multi_payload["model_routes"]] == [
            "knowledge_deep_map",
            "knowledge_deep_reduce",
        ]
        assert [item["provider"] for item in multi_payload["model_routes"]] == ["deepseek", "kimi"]

        legacy_response = client.get("/api/tasks/task_c653_legacy/model-routes")
        legacy_response.raise_for_status()
        assert legacy_response.json() == {"task_id": "task_c653_legacy", "model_routes": []}

        serialized_history = f"{single_response.text}\n{multi_response.text}\n{legacy_response.text}"
        assert "test-api-key-placeholder" not in serialized_history
        assert "account-token.example.invalid" not in serialized_history

    print("C6.5.3 model route audit verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True)
