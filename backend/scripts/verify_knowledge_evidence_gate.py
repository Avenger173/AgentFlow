"""知识库 K3 Evidence Gate 离线回归。

验证 Gate 不调用模型或网络：单资料可通过、比较资料不足标为 partial、零命中拒答，以及
generation 切换后旧证据不能继续被模型使用。
"""

from __future__ import annotations

import gc
import os
from pathlib import Path
import shutil
import sys
import tempfile


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_knowledge_evidence_gate_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / "data" / "knowledge_evidence_gate_verify.db")
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import sqlite as sqlite_service
from app.database.knowledge_repository import create_knowledge_base, import_workspace_documents_to_knowledge_base
from app.schemas.knowledge import KnowledgeRetrievalRequest
from app.services.knowledge_evidence_gate import gate_knowledge_evidence
from app.services.knowledge_keyword_index import create_knowledge_index_job, run_knowledge_index_job
from app.services.knowledge_retrieval import retrieve_knowledge_evidence
from app.services.workspace_documents import WorkspaceDocumentError, import_workspace_document, resolve_workspace_document_path


def _write_workspace(filename: str, content: str) -> None:
    """首次受控导入，后续测试版本更新只覆写 workspace 副本。"""

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


def main() -> None:
    try:
        base = create_knowledge_base(name="K3 Evidence Gate 回归")
        _import_and_index(
            base.knowledge_base_id,
            "delivery_gate.md",
            "# 交付要求\n\n编号 AF-204 的验收需要保留来源定位和负责人确认。\n",
        )
        retrieved = retrieve_knowledge_evidence(
            KnowledgeRetrievalRequest(knowledge_base_id=base.knowledge_base_id, query="AF-204 验收要求")
        )
        sufficient = gate_knowledge_evidence(retrieved)
        assert sufficient.evidence_state == "sufficient"
        assert sufficient.covered_document_count == 1
        assert sufficient.sources and sufficient.sources[0].source_id == "kb_src_1"
        assert "AF-204" in sufficient.sources[0].excerpt

        comparison = retrieve_knowledge_evidence(
            KnowledgeRetrievalRequest(knowledge_base_id=base.knowledge_base_id, query="对比两个交付方案的验收要求")
        )
        partial = gate_knowledge_evidence(comparison)
        assert partial.evidence_state == "partial"
        assert partial.required_document_count == 2
        assert partial.covered_document_count == 1

        no_result = retrieve_knowledge_evidence(
            KnowledgeRetrievalRequest(knowledge_base_id=base.knowledge_base_id, query="ZX-999 的预算上限")
        )
        insufficient = gate_knowledge_evidence(no_result)
        assert insufficient.evidence_state == "insufficient"
        assert not insufficient.sources
        assert any("当前资料不足" in item for item in insufficient.warnings)

        # 更新生成第二个活动 generation 后，第一轮检索的内容即使仍在历史表中也不能使用。
        _import_and_index(
            base.knowledge_base_id,
            "delivery_gate.md",
            "# 交付要求\n\n编号 AF-310 的验收需要保留来源定位和双人确认。\n",
        )
        stale = gate_knowledge_evidence(retrieved)
        assert stale.evidence_state == "insufficient"
        assert stale.active_index_generation == 2
        assert not stale.sources
        assert any("索引已更新" in item for item in stale.warnings)

        print("Knowledge K3 Evidence Gate verification passed: sufficient, partial, insufficient and stale evidence.")
    finally:
        gc.collect()
        sqlite_service._INITIALIZED_PATHS.clear()
        shutil.rmtree(VERIFY_ROOT, ignore_errors=False)


if __name__ == "__main__":
    main()
