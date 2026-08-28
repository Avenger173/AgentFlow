"""知识库 K2 受控混合检索离线回归。

夹具使用临时目录、确定性假向量和真实 Chroma PersistentClient；不下载 Embedding 权重、不调用
LLM 或外网。它覆盖当前 generation 边界、中文/编号关键词、RRF 合并和语义故障降级。
"""

from __future__ import annotations

import gc
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from types import SimpleNamespace


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_knowledge_retrieval_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / "data" / "knowledge_retrieval_verify.db")
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import sqlite as sqlite_service
from app.database.knowledge_repository import create_knowledge_base, import_workspace_documents_to_knowledge_base
from app.schemas.knowledge import KnowledgeRetrievalRequest
from app.services import knowledge_keyword_index as keyword_index
from app.services import knowledge_retrieval as retrieval
from app.services.knowledge_keyword_index import create_knowledge_index_job, run_knowledge_index_job
from app.services.workspace_documents import (
    WorkspaceDocumentError,
    import_workspace_document,
    resolve_workspace_document_path,
)


def _import_and_index(*, base_id: str, filename: str, content: str) -> None:
    """写入受控 workspace 后建立一个新 generation，模拟客户正常导入/索引流程。"""

    try:
        workspace_path = resolve_workspace_document_path(filename)
    except WorkspaceDocumentError:
        # 首次导入没有可解析的工作区相对路径；由既有受控导入服务创建它，不能手写路径。
        import_workspace_document(filename=filename, content=content)
    else:
        workspace_path.write_text(content, encoding="utf-8")
    import_workspace_documents_to_knowledge_base(
        knowledge_base_id=base_id,
        workspace_document_names=[filename],
    )
    job = create_knowledge_index_job(base_id)
    completed = run_knowledge_index_job(job.index_job_id)
    assert completed.status in {"completed", "partial_failure"}


def _fake_embed(texts: list[str], *, allow_download: bool) -> list[list[float]]:
    """明确断言 K2 的所有嵌入均为 local-files-only，并给“审批”稳定的同向量。"""

    assert allow_download is False
    vectors: list[list[float]] = []
    for text in texts:
        # 这个正交向量没有任何索引文档的相近项，用来固定 K3 的“向量最近邻不等于可回答”
        # 边界。若后续有人移除 Dense 门槛，此题会重新出现伪证据。
        if "无关" in text:
            vectors.append([0.0, 0.0, 1.0])
        elif "审批" in text:
            vectors.append([1.0, 0.0, 0.0])
        else:
            vectors.append([0.0, 1.0, 0.0])
    return vectors


def _verify_keyword_generation_isolation() -> None:
    base = create_knowledge_base(name="K2 当前 generation 关键词回归")
    _import_and_index(
        base_id=base.knowledge_base_id,
        filename="policy_generation.md",
        content="# 审批制度\n\n编号 AF-204 的审批窗口为每周一上午。\n",
    )
    first = retrieval.retrieve_knowledge_evidence(
        KnowledgeRetrievalRequest(knowledge_base_id=base.knowledge_base_id, query="AF-204 审批窗口")
    )
    assert first.diagnostics.mode == "keyword"
    assert first.diagnostics.active_index_generation == 1
    assert first.evidences
    assert first.evidences[0].document_name == "policy_generation.md"
    assert "keyword" in first.evidences[0].retrieval_channels
    assert first.evidences[0].source.source_locator

    # FTS 是派生索引，故障时不能把资料库误判为“无答案”。回退只能搜索当前活动
    # generation 的受控子块，并把降级事实交给后续 Evidence Gate。
    original_fts_query = retrieval._query_fts_candidate_rows
    try:
        def _failing_fts(*_args: object, **_kwargs: object) -> list[object]:
            raise sqlite3.OperationalError("offline fixture FTS unavailable")

        retrieval._query_fts_candidate_rows = _failing_fts
        fts_fallback = retrieval.retrieve_knowledge_evidence(
            KnowledgeRetrievalRequest(knowledge_base_id=base.knowledge_base_id, query="AF-204")
        )
        assert fts_fallback.evidences
        assert any("逐块搜索" in item for item in fts_fallback.diagnostics.warnings)
    finally:
        retrieval._query_fts_candidate_rows = original_fts_query

    # 同名文件的新版本必须替换活动 generation；检索不能因旧 FTS 仍存在而读到 AF-204。
    _import_and_index(
        base_id=base.knowledge_base_id,
        filename="policy_generation.md",
        content="# 审批制度\n\n编号 AF-310 的审批窗口调整为每周三下午。\n",
    )
    stale = retrieval.retrieve_knowledge_evidence(
        KnowledgeRetrievalRequest(knowledge_base_id=base.knowledge_base_id, query="AF-204")
    )
    assert stale.diagnostics.mode == "no_result"
    assert not stale.evidences
    current = retrieval.retrieve_knowledge_evidence(
        KnowledgeRetrievalRequest(knowledge_base_id=base.knowledge_base_id, query="AF-310")
    )
    assert current.diagnostics.active_index_generation == 2
    assert current.evidences and "AF-310" in current.evidences[0].parent_content


def _verify_hybrid_and_fallback() -> None:
    original_capability = keyword_index.vector_index_capability
    original_index_embed = keyword_index.embed_local_texts
    original_retrieval_embed = retrieval.embed_local_texts
    try:
        keyword_index.vector_index_capability = lambda: SimpleNamespace(
            model_initialized=True,
            chroma_available=True,
            fastembed_available=True,
        )
        keyword_index.embed_local_texts = _fake_embed
        retrieval.embed_local_texts = _fake_embed
        base = create_knowledge_base(name="K2 混合检索回归")
        _import_and_index(
            base_id=base.knowledge_base_id,
            filename="hybrid_policy.md",
            content=(
                "# 审批规则\n\n"
                "审批窗口为每周一上午，编号 AF-204，必须保留来源定位。\n\n"
                "# 发布规则\n\n发布前需要完成负责人确认。\n"
            ),
        )
        hybrid = retrieval.retrieve_knowledge_evidence(
            KnowledgeRetrievalRequest(knowledge_base_id=base.knowledge_base_id, query="审批窗口 AF-204")
        )
        assert hybrid.diagnostics.mode == "hybrid"
        assert hybrid.diagnostics.keyword_candidate_count >= 1
        assert hybrid.diagnostics.dense_candidate_count >= 1
        assert hybrid.evidences
        assert set(hybrid.evidences[0].retrieval_channels) == {"keyword", "dense"}

        # 即使本机 Dense 正常，完全无关的问题也不能因为“最相近”而得到一个假的来源。
        dense_no_answer = retrieval.retrieve_knowledge_evidence(
            KnowledgeRetrievalRequest(knowledge_base_id=base.knowledge_base_id, query="无关问题")
        )
        assert dense_no_answer.diagnostics.mode == "no_result"
        assert dense_no_answer.diagnostics.dense_candidate_count == 0
        assert not dense_no_answer.evidences

        def _failing_embed(texts: list[str], *, allow_download: bool) -> list[list[float]]:
            assert allow_download is False
            raise RuntimeError("offline fixture semantic cache unavailable")

        retrieval.embed_local_texts = _failing_embed
        fallback = retrieval.retrieve_knowledge_evidence(
            KnowledgeRetrievalRequest(knowledge_base_id=base.knowledge_base_id, query="AF-204")
        )
        assert fallback.diagnostics.mode == "keyword_fallback"
        assert fallback.evidences
        assert fallback.evidences[0].retrieval_channels == ["keyword"]
        assert fallback.diagnostics.warnings
    finally:
        keyword_index.vector_index_capability = original_capability
        keyword_index.embed_local_texts = original_index_embed
        retrieval.embed_local_texts = original_retrieval_embed


def main() -> None:
    try:
        _verify_keyword_generation_isolation()
        _verify_hybrid_and_fallback()
        print("Knowledge K2 retrieval verification passed: current generation, hybrid RRF and explicit fallback.")
    finally:
        gc.collect()
        sqlite_service._INITIALIZED_PATHS.clear()
        shutil.rmtree(VERIFY_ROOT, ignore_errors=False)


if __name__ == "__main__":
    main()
