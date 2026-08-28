"""K5.1 本地检索短缓存回归。

只使用临时资料库、SQLite FTS 和关键词检索；不下载向量模型、不调用 LLM 或外网。重点验证
缓存隔离键包含活动 generation 与 ``top_k``，因此更新索引后不能返回旧来源。
"""

from __future__ import annotations

import gc
import os
from pathlib import Path
import shutil
import sys
import tempfile


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_knowledge_retrieval_cache_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / "data" / "knowledge_retrieval_cache_verify.db")
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import sqlite as sqlite_service
from app.database.knowledge_repository import create_knowledge_base, import_workspace_documents_to_knowledge_base
from app.schemas.knowledge import KnowledgeRetrievalRequest
from app.services import knowledge_retrieval as retrieval
from app.services.knowledge_keyword_index import create_knowledge_index_job, run_knowledge_index_job
from app.services.workspace_documents import WorkspaceDocumentError, import_workspace_document, resolve_workspace_document_path


def _import_and_index(*, base_id: str, filename: str, content: str) -> None:
    """复用受控导入与真实 generation 切换，不直接写知识库私有目录。"""

    try:
        path = resolve_workspace_document_path(filename)
    except WorkspaceDocumentError:
        import_workspace_document(filename=filename, content=content)
    else:
        path.write_text(content, encoding="utf-8")
    import_workspace_documents_to_knowledge_base(
        knowledge_base_id=base_id,
        workspace_document_names=[filename],
    )
    completed = run_knowledge_index_job(create_knowledge_index_job(base_id).index_job_id)
    assert completed.status in {"completed", "partial_failure"}


def main() -> None:
    try:
        retrieval.clear_knowledge_retrieval_cache()
        base = create_knowledge_base(name="K5 检索短缓存回归")
        _import_and_index(
            base_id=base.knowledge_base_id,
            filename="cache_policy.md",
            content="# 第一版\n\n编号 AF-204 的审批窗口为每周一上午。\n",
        )

        request = KnowledgeRetrievalRequest(
            knowledge_base_id=base.knowledge_base_id,
            query="AF-204 审批窗口",
            top_k=2,
        )
        first = retrieval.retrieve_knowledge_evidence(request)
        assert first.diagnostics.local_cache_state == "miss"
        assert first.diagnostics.local_cache_age_ms is None
        assert first.diagnostics.active_index_generation == 1
        assert first.evidences and "AF-204" in first.evidences[0].parent_content

        # 证明缓存结果不会被调用方意外改写；下一次命中仍从缓存的不可变副本返回。
        first.evidences[0].document_name = "不应污染缓存.md"
        second = retrieval.retrieve_knowledge_evidence(request)
        assert second.diagnostics.local_cache_state == "hit"
        assert second.diagnostics.local_cache_age_ms is not None
        assert second.evidences[0].document_name == "cache_policy.md"

        # 结果预算是缓存合同的一部分，不能由 top_k=2 静默复用到 top_k=1。
        limited = retrieval.retrieve_knowledge_evidence(request.model_copy(update={"top_k": 1}))
        assert limited.diagnostics.local_cache_state == "miss"

        _import_and_index(
            base_id=base.knowledge_base_id,
            filename="cache_policy.md",
            content="# 第二版\n\n编号 AF-310 的审批窗口调整为每周三下午。\n",
        )
        current = retrieval.retrieve_knowledge_evidence(
            KnowledgeRetrievalRequest(
                knowledge_base_id=base.knowledge_base_id,
                query="AF-310 审批窗口",
                top_k=2,
            )
        )
        assert current.diagnostics.local_cache_state == "miss"
        assert current.diagnostics.active_index_generation == 2
        assert current.evidences and "AF-310" in current.evidences[0].parent_content
        stale = retrieval.retrieve_knowledge_evidence(request)
        assert stale.diagnostics.local_cache_state == "miss"
        assert stale.diagnostics.active_index_generation == 2
        assert not stale.evidences
        print("Knowledge K5.1 retrieval cache verification passed: hit, top_k isolation and generation invalidation.")
    finally:
        retrieval.clear_knowledge_retrieval_cache()
        gc.collect()
        sqlite_service._INITIALIZED_PATHS.clear()
        shutil.rmtree(VERIFY_ROOT, ignore_errors=False)


if __name__ == "__main__":
    main()
