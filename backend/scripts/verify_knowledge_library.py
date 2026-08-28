"""知识库 K1.2 受控副本与文档版本离线回归。

脚本使用临时 data 目录和临时 SQLite 数据库，不读取项目中的客户文件或 `.env` 模型配置，
不解析 PDF/DOCX、不建立索引，也不触发网络。它只验证导入边界：原 workspace 文件不修改，
知识库保存私有副本，同内容去重，内容变化形成新的候选版本，失败不会留下半成品。
"""

from __future__ import annotations

import gc
import os
from pathlib import Path
import shutil
import sys
import tempfile


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_knowledge_library_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT)
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / "knowledge_library_verify.db")
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import settings
from app.database import sqlite as sqlite_service
from app.database.knowledge_repository import (
    create_knowledge_base,
    get_knowledge_base,
    import_workspace_documents_to_knowledge_base,
    list_knowledge_document_versions,
    list_knowledge_documents,
)
from app.services.workspace_documents import (
    WorkspaceDocumentError,
    import_workspace_document,
    resolve_workspace_document_path,
)


def _expect_workspace_error(action, label: str) -> None:
    """确认非法 workspace 引用不会被资料库导入悄悄忽略。"""

    try:
        action()
    except WorkspaceDocumentError:
        return
    raise AssertionError(f"{label}: 预期受控 workspace 错误，但意外通过。")


def main() -> None:
    try:
        knowledge_base = create_knowledge_base(name="K1.2 版本回归")
        original_v1 = "# 第一版材料\n\n仅用于验证受控副本。\n"
        import_workspace_document(filename="课程笔记.md", content=original_v1)

        created = import_workspace_documents_to_knowledge_base(
            knowledge_base_id=knowledge_base.knowledge_base_id,
            workspace_document_names=["课程笔记.md"],
        )
        created_item = created.items[0]
        assert created.status == "queued"
        assert created_item.outcome == "created"
        assert created_item.document_version.version_number == 1
        assert created_item.document_version.status == "queued"
        assert get_knowledge_base(knowledge_base.knowledge_base_id).status == "empty"
        first_copy = (
            settings.knowledge_storage_dir
            / knowledge_base.knowledge_base_id
            / "sources"
            / f"{created_item.document_version.storage_ref}.md"
        )
        assert first_copy.read_text(encoding="utf-8") == original_v1

        duplicate = import_workspace_documents_to_knowledge_base(
            knowledge_base_id=knowledge_base.knowledge_base_id,
            workspace_document_names=["课程笔记.md"],
        )
        duplicate_item = duplicate.items[0]
        assert duplicate_item.outcome == "duplicate"
        assert duplicate_item.document_version.document_version_id == (
            created_item.document_version.document_version_id
        )
        assert not list(settings.knowledge_storage_dir.rglob("*.part"))

        # 模拟一个已经受控的 workspace 文件内容随后更新（未来同步/重新选择流程会复用该
        # 路径）。资料库的 v1 副本必须仍保持原内容，不能因源文件改变被静默替换。
        original_v2 = "# 第二版材料\n\n内容已更新，应产生一个新的候选版本。\n"
        resolve_workspace_document_path("课程笔记.md").write_text(original_v2, encoding="utf-8")
        updated = import_workspace_documents_to_knowledge_base(
            knowledge_base_id=knowledge_base.knowledge_base_id,
            workspace_document_names=["课程笔记.md"],
        )
        updated_item = updated.items[0]
        assert updated_item.outcome == "updated"
        assert updated_item.document.document_id == created_item.document.document_id
        assert updated_item.document_version.version_number == 2
        assert updated_item.document_version.status == "queued"
        assert first_copy.read_text(encoding="utf-8") == original_v1
        second_copy = (
            settings.knowledge_storage_dir
            / knowledge_base.knowledge_base_id
            / "sources"
            / f"{updated_item.document_version.storage_ref}.md"
        )
        assert second_copy.read_text(encoding="utf-8") == original_v2
        assert resolve_workspace_document_path("课程笔记.md").read_text(encoding="utf-8") == original_v2

        versions = list_knowledge_document_versions(created_item.document.document_id)
        assert [item.version_number for item in versions] == [2, 1]
        assert versions[0].status == "queued"
        assert versions[1].status == "superseded"
        documents = list_knowledge_documents(knowledge_base.knowledge_base_id)
        assert [item.document_id for item in documents] == [created_item.document.document_id]

        before_failure = (len(documents), len(versions))
        _expect_workspace_error(
            lambda: import_workspace_documents_to_knowledge_base(
                knowledge_base_id=knowledge_base.knowledge_base_id,
                workspace_document_names=["不存在的资料.md"],
            ),
            "missing_workspace_source",
        )
        after_failure = (
            len(list_knowledge_documents(knowledge_base.knowledge_base_id)),
            len(list_knowledge_document_versions(created_item.document.document_id)),
        )
        assert after_failure == before_failure
        assert not list(settings.knowledge_storage_dir.rglob("*.part"))

        print("Knowledge K1.2 library verification passed: copy, version, duplicate, rollback boundary.")
    finally:
        # SQLite WAL 文件和 Repository 短连接在 Windows 上需要在删除临时目录前尽量释放。
        gc.collect()
        sqlite_service._INITIALIZED_PATHS.clear()
        shutil.rmtree(VERIFY_ROOT, ignore_errors=False)


if __name__ == "__main__":
    main()
