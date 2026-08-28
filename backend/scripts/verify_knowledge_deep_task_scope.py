"""知识库 K4 范围快照的离线回归。

该夹具验证深度任务只冻结当前活动 generation 的结构元数据，更新资料后旧范围会明确失效。
它不调用回答模型、不构建向量、不读取客户文件，也不产生报告或工作区写入。
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_knowledge_deep_scope_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / "data" / "knowledge_deep_scope.db")
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import sqlite as sqlite_service
from app.database.knowledge_repository import create_knowledge_base, import_workspace_documents_to_knowledge_base
from app.schemas.knowledge import KnowledgeDeepTaskRequest
from app.services.knowledge_deep_task import (
    KnowledgeDeepTaskScopeStaleError,
    build_knowledge_deep_task_scope,
    verify_knowledge_deep_task_scope,
)
from app.services.knowledge_keyword_index import create_knowledge_index_job, run_knowledge_index_job
from app.services.workspace_documents import import_workspace_document


def _index_active_materials(*, knowledge_base_id: str, document_names: list[str]) -> None:
    """走真实受控导入与 generation 切换，不手写父块或索引表。"""

    import_workspace_documents_to_knowledge_base(
        knowledge_base_id=knowledge_base_id,
        workspace_document_names=document_names,
    )
    completed = run_knowledge_index_job(create_knowledge_index_job(knowledge_base_id).index_job_id)
    assert completed.status == "completed", completed.failure_summaries


def main() -> None:
    try:
        import_workspace_document(
            filename="deep_scope_handbook.md",
            content="# 交付范围\n\n第一章说明验收与负责人。\n\n## 风险\n\n风险必须在发布前复核。\n",
        )
        import_workspace_document(
            filename="deep_scope_policy.md",
            content="# 审计制度\n\n审计记录至少保留十二个月，并由责任人确认。\n",
        )
        base = create_knowledge_base(name="K4 深度任务范围回归")
        _index_active_materials(
            knowledge_base_id=base.knowledge_base_id,
            document_names=["deep_scope_handbook.md", "deep_scope_policy.md"],
        )
        request = KnowledgeDeepTaskRequest(
            knowledge_base_id=base.knowledge_base_id,
            task_kind="audit",
            task_goal="审查当前资料的交付范围与风险覆盖。",
        )
        scope = build_knowledge_deep_task_scope(request)
        assert scope.active_index_generation == 1
        assert scope.covered_document_count == 2
        assert len(scope.map_units) >= 2
        assert len({unit.parent_chunk_id for unit in scope.map_units}) == len(scope.map_units)
        assert all(unit.character_count > 0 and unit.document_name for unit in scope.map_units)
        # scope 只存可恢复的 ID、来源和长度；任何父块正文都不得进入将来的 checkpoint/API。
        serialized_scope = scope.model_dump_json()
        assert "第一章说明" not in serialized_scope
        assert "审计记录至少" not in serialized_scope
        assert verify_knowledge_deep_task_scope(scope).index_generation_id == scope.index_generation_id

        # 更新材料会创建新 generation。旧范围即使父块事实仍存在，也绝不能被误当作可恢复任务。
        import_workspace_document(
            filename="deep_scope_handbook.md",
            content="# 交付范围\n\n第二版新增发布前演练与风险复核。\n",
        )
        _index_active_materials(
            knowledge_base_id=base.knowledge_base_id,
            document_names=["deep_scope_handbook.md"],
        )
        try:
            verify_knowledge_deep_task_scope(scope)
        except KnowledgeDeepTaskScopeStaleError:
            pass
        else:  # pragma: no cover - 防止旧 generation 被意外复用。
            raise AssertionError("资料更新后旧的 K4 深度任务范围必须失效。")

        print("Knowledge K4 deep task scope verification passed: active generation, no-content snapshot and stale rejection.")
    finally:
        sqlite_service._INITIALIZED_PATHS.clear()
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
