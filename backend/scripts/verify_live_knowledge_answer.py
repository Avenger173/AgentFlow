"""K3 可信问答的最小真实模型验收。

仅在显式传入 ``--live`` 时执行。脚本创建临时知识库和两份合成材料，验证真实配置的
ModelGateway 能在受控证据范围内生成带来源回答；不会读取用户工作区文档，也不会输出 API Key、
模型正文、来源片段或绝对路径。
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import os
from pathlib import Path
import shutil
import sys
import tempfile


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_live_knowledge_answer_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / "data" / "live_knowledge_answer.db")
os.environ["AGENTFLOW_CHAT_MODE"] = "llm"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database.knowledge_repository import create_knowledge_base, import_workspace_documents_to_knowledge_base
from app.schemas.knowledge import KnowledgeAnswerRequest
from app.services.knowledge_answer import answer_knowledge_question
from app.services.knowledge_keyword_index import create_knowledge_index_job, run_knowledge_index_job
from app.services.workspace_documents import WorkspaceDocumentError, import_workspace_document, resolve_workspace_document_path


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 K3 知识库真实模型最小验收。")
    parser.add_argument(
        "--live",
        action="store_true",
        help="明确允许调用当前已配置的回答模型；不传入时只说明未执行。",
    )
    return parser.parse_args()


def write_workspace_document(filename: str, content: str) -> None:
    """把合成材料放入临时受控 workspace，不接触用户实际资料。"""

    try:
        resolve_workspace_document_path(filename).write_text(content, encoding="utf-8")
    except WorkspaceDocumentError:
        import_workspace_document(filename=filename, content=content)


def prepare_fixture() -> str:
    """建立两份可独立定位的验收材料并完成关键词 generation。"""

    base = create_knowledge_base(name="K3 真实模型验收")
    write_workspace_document(
        "acceptance_policy.md",
        "# 交付验收\n\nAF-204 的验收要求包括保留来源定位，并由项目负责人确认。\n",
    )
    write_workspace_document(
        "retention_policy.md",
        "# 交付留档\n\nAF-204 的交付材料应保留验收记录至少 12 个月。\n",
    )
    import_workspace_documents_to_knowledge_base(
        knowledge_base_id=base.knowledge_base_id,
        workspace_document_names=["acceptance_policy.md", "retention_policy.md"],
    )
    job = run_knowledge_index_job(create_knowledge_index_job(base.knowledge_base_id).index_job_id)
    if job.status != "completed":
        raise RuntimeError(f"临时资料索引未完成：{job.status}")
    return base.knowledge_base_id


def main() -> None:
    arguments = parse_arguments()
    if not arguments.live:
        print("Knowledge K3 live verification not run. Pass --live to use the configured answer model.")
        return

    try:
        knowledge_base_id = prepare_fixture()
        result = asyncio.run(
            answer_knowledge_question(
                KnowledgeAnswerRequest(
                    knowledge_base_id=knowledge_base_id,
                    query="AF-204 的验收和交付留档要求分别是什么？",
                )
            )
        )
        if result.status != "completed" or result.answer is None:
            raise RuntimeError(f"可信回答未完成：status={result.status} stop_reason={result.stop_reason}")
        if len(result.answer.source_ids) < 2:
            raise RuntimeError("可信回答没有覆盖两份独立材料。")
        print(
            "Knowledge K3 live verification passed: "
            f"status={result.status} model_turns={result.model_turn_count} "
            f"source_count={len(result.answer.source_ids)} "
            f"retrieval_mode={result.retrieval_diagnostics.mode}"
        )
    finally:
        # SQLite/Chroma 等可选依赖在 Windows 上可能延迟释放句柄；显式 GC 后再清理临时验收目录。
        gc.collect()
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
