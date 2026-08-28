"""知识库 K1 删除、索引取消与启动恢复离线回归。"""

from __future__ import annotations

import gc
import os
from pathlib import Path
import shutil
import sys
import tempfile


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_knowledge_deletion_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / "data" / "knowledge_deletion_verify.db")
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import sqlite as sqlite_service
from app.database.knowledge_repository import (
    KnowledgeBaseNotFoundError,
    create_knowledge_base,
    finalize_knowledge_base_deletion,
    get_knowledge_base,
    import_workspace_documents_to_knowledge_base,
    recover_pending_knowledge_base_deletions,
    request_knowledge_base_deletion,
)
from app.services.knowledge_keyword_index import (
    create_knowledge_index_job,
    get_knowledge_index_job,
    run_knowledge_index_job,
)
from app.services.workspace_documents import import_workspace_document, resolve_workspace_document_path


def _create_base_with_source(name: str, filename: str) -> tuple[str, str]:
    """建立独立资料库和 workspace 文件，便于逐项验证删除不会影响源材料。"""

    knowledge_base = create_knowledge_base(name=name)
    import_workspace_document(filename=filename, content="# Test source\n\nDeletion must not remove workspace files.\n")
    import_workspace_documents_to_knowledge_base(
        knowledge_base_id=knowledge_base.knowledge_base_id,
        workspace_document_names=[filename],
    )
    return knowledge_base.knowledge_base_id, filename


def main() -> None:
    try:
        # 未开始的 job 应直接取消，删除完成后资料库业务记录不可再读，workspace 原文件仍在。
        queued_base_id, queued_source = _create_base_with_source("K1 queued delete", "queued.md")
        queued_job = create_knowledge_index_job(queued_base_id)
        assert queued_job.status == "queued"
        assert request_knowledge_base_deletion(queued_base_id).status == "deleting"
        assert get_knowledge_index_job(queued_job.index_job_id).status == "cancelled"
        assert finalize_knowledge_base_deletion(queued_base_id).status == "deleted"
        assert resolve_workspace_document_path(queued_source).is_file()
        try:
            get_knowledge_base(queued_base_id)
        except KnowledgeBaseNotFoundError:
            pass
        else:  # pragma: no cover - deleted 资料库不能再被当作可用对象读取。
            raise AssertionError("已删除资料库仍被作为可用资料库返回。")
        # 删除后的业务记录仍保留作脱敏审计，但客户原先的名称必须可立即复用。
        recreated = create_knowledge_base(name="K1 queued delete")
        assert recreated.name == "K1 queued delete"
        assert recreated.knowledge_base_id != queued_base_id

        # 模拟索引线程已领取任务后收到删除请求。Runner 必须观察 cancel_requested，绝不能
        # 激活 generation 或把资料库从 deleting 恢复成 failed/ready。
        running_base_id, _ = _create_base_with_source("K1 running delete", "running.md")
        running_job = create_knowledge_index_job(running_base_id)
        connection = sqlite_service.get_connection()
        try:
            connection.execute(
                """
                UPDATE knowledge_index_jobs
                SET status = 'running', stage = 'parsing', started_at = '2026-08-21T00:00:00Z'
                WHERE index_job_id = ?
                """,
                (running_job.index_job_id,),
            )
            connection.commit()
        finally:
            connection.close()
        request_knowledge_base_deletion(running_base_id)
        cancelled = run_knowledge_index_job(running_job.index_job_id)
        assert cancelled.status == "cancelled"
        assert get_knowledge_base(running_base_id, include_deleted=True).status == "deleting"
        assert finalize_knowledge_base_deletion(running_base_id).status == "deleted"

        # 进程在 deleting 之后退出时，下一次启动前的恢复函数应继续清理，而不需要客户重传。
        recover_base_id, _ = _create_base_with_source("K1 restart delete", "restart.md")
        request_knowledge_base_deletion(recover_base_id)
        assert recover_base_id in recover_pending_knowledge_base_deletions()
        assert get_knowledge_base(recover_base_id, include_deleted=True).status == "deleted"

        print("Knowledge K1 deletion verification passed: queued cancellation, running cancellation, recovery, source boundary.")
    finally:
        gc.collect()
        sqlite_service._INITIALIZED_PATHS.clear()
        shutil.rmtree(VERIFY_ROOT, ignore_errors=False)


if __name__ == "__main__":
    main()
