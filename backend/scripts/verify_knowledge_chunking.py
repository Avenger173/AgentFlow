"""知识库 K1.3 解析、父子分块与来源锚点离线回归。

本脚本只使用临时 UTF-8 Markdown 文件与临时 SQLite/私有副本目录。它不调用模型、网络、
Chroma 或 FTS，验证 K1.3 最重要的事实：受控副本解析后的块可回读、来源可定位、子块邻接
完整，空材料失败时不留下半块，也不破坏其它候选版本。
"""

from __future__ import annotations

import gc
import os
from pathlib import Path
import shutil
import sys
import tempfile


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_knowledge_chunking_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT)
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / "knowledge_chunking_verify.db")
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import sqlite as sqlite_service
from app.database.knowledge_repository import (
    create_knowledge_base,
    import_workspace_documents_to_knowledge_base,
    parse_knowledge_document_version,
)
from app.services.workspace_documents import import_workspace_document


def main() -> None:
    try:
        knowledge_base = create_knowledge_base(name="K1.3 分块回归")
        source_text = (
            "# 项目总则\n\n"
            "本材料用于验证知识库的标题、行号与版本化分块。\n\n"
            "## 范围\n\n"
            "范围包含导入、解析、父块、子块以及后续索引，不应修改源文件。\n\n"
            "### 例外\n\n"
            "OCR 与联网检索不在本阶段范围内。\n\n"
            "# 验收\n\n"
            "每个结论都需要保留可回读的来源位置。\n"
        )
        import_workspace_document(filename="分块材料.md", content=source_text)
        imported = import_workspace_documents_to_knowledge_base(
            knowledge_base_id=knowledge_base.knowledge_base_id,
            workspace_document_names=["分块材料.md"],
        ).items[0]
        parsed_version = parse_knowledge_document_version(
            imported.document_version.document_version_id
        )
        assert parsed_version.status == "parsed"
        assert parsed_version.extracted_char_count == len(source_text)
        assert parsed_version.parent_chunk_count >= 2
        assert parsed_version.child_chunk_count >= parsed_version.parent_chunk_count

        connection = sqlite_service.get_connection()
        try:
            parents = connection.execute(
                """
                SELECT parent_chunk_id, ordinal, heading_path_json, source_kind, source_locator,
                       start_char, end_char, content, content_sha256
                FROM knowledge_parent_chunks WHERE document_version_id = ? ORDER BY ordinal
                """,
                (parsed_version.document_version_id,),
            ).fetchall()
            children = connection.execute(
                """
                SELECT child_chunk_id, parent_chunk_id, ordinal, previous_child_chunk_id,
                       next_child_chunk_id, source_kind, source_locator, start_char, end_char,
                       content, content_sha256
                FROM knowledge_child_chunks WHERE document_version_id = ? ORDER BY ordinal
                """,
                (parsed_version.document_version_id,),
            ).fetchall()
            stored_suffix = connection.execute(
                "SELECT storage_suffix FROM knowledge_document_versions WHERE document_version_id = ?",
                (parsed_version.document_version_id,),
            ).fetchone()
        finally:
            connection.close()

        assert str(stored_suffix["storage_suffix"]) == ".md"
        assert len(parents) == parsed_version.parent_chunk_count
        assert len(children) == parsed_version.child_chunk_count
        parent_ranges = {
            str(parent["parent_chunk_id"]): (int(parent["start_char"]), int(parent["end_char"]))
            for parent in parents
        }
        assert any("项目总则" in str(parent["heading_path_json"]) for parent in parents)
        for index, child in enumerate(children):
            child_start = int(child["start_char"])
            child_end = int(child["end_char"])
            parent_start, parent_end = parent_ranges[str(child["parent_chunk_id"])]
            assert parent_start <= child_start < child_end <= parent_end
            assert str(child["source_kind"]) == "line"
            assert "第 " in str(child["source_locator"])
            assert str(child["content"]).strip()
            assert len(str(child["content_sha256"])) == 64
            assert str(child["previous_child_chunk_id"]) == (
                "" if index == 0 else str(children[index - 1]["child_chunk_id"])
            )
            assert str(child["next_child_chunk_id"]) == (
                "" if index + 1 == len(children) else str(children[index + 1]["child_chunk_id"])
            )

        # 空白材料可进入受控副本版本，但解析阶段必须明确失败，且不会写入一条假“空块”。
        import_workspace_document(filename="空白材料.md", content="\n\n")
        empty_import = import_workspace_documents_to_knowledge_base(
            knowledge_base_id=knowledge_base.knowledge_base_id,
            workspace_document_names=["空白材料.md"],
        ).items[0]
        failed_version = parse_knowledge_document_version(
            empty_import.document_version.document_version_id
        )
        assert failed_version.status == "failed"
        assert failed_version.failure_summary
        connection = sqlite_service.get_connection()
        try:
            empty_chunk_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM knowledge_child_chunks WHERE document_version_id = ?",
                    (failed_version.document_version_id,),
                ).fetchone()["total"]
            )
        finally:
            connection.close()
        assert empty_chunk_count == 0

        print("Knowledge K1.3 chunking verification passed: parser, anchors, parents, children, failure.")
    finally:
        gc.collect()
        sqlite_service._INITIALIZED_PATHS.clear()
        shutil.rmtree(VERIFY_ROOT, ignore_errors=False)


if __name__ == "__main__":
    main()
