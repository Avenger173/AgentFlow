"""知识库 K0.3 契约离线回归。

此脚本不连接数据库、不读取客户文件、不初始化 Chroma/FastEmbed，也不调用模型。它只锁定
导入路径边界、版本化索引激活条件和后台 job 状态机，防止 K1 接入时重新引入旧来源或路径泄漏。
"""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import ValidationError


# 该脚本按文档约定从 ``backend/scripts`` 直接运行；显式加入后端根目录，避免执行位置不同
# 时误把 scripts 目录当作应用根目录。
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.schemas.knowledge import (
    KnowledgeBaseRecord,
    KnowledgeDocumentImportRequest,
    KnowledgeIndexGenerationRecord,
    KnowledgeIndexJobRecord,
    KnowledgeIndexProfile,
    KnowledgeSourceAnchor,
)
from app.services.knowledge_contracts import (
    KnowledgeContractError,
    assert_generation_can_activate,
    assert_index_job_transition,
)


PROFILE = KnowledgeIndexProfile(
    profile_id="kb_profile_local_hybrid_v1",
    keyword_profile_version="fts5_cjk_v1",
    splitter_profile_version="parent_child_v1",
    embedding_profile_version="fastembed_bge_small_zh_v1",
)


def _expect_validation_error(factory, label: str) -> None:
    try:
        factory()
    except ValidationError:
        return
    raise AssertionError(f"{label}: 预期契约校验失败，但意外通过。")


def _expect_contract_error(factory, label: str) -> None:
    try:
        factory()
    except KnowledgeContractError:
        return
    raise AssertionError(f"{label}: 预期状态守卫拒绝，但意外通过。")


def main() -> None:
    request = KnowledgeDocumentImportRequest(
        knowledge_base_id="kb_1234abcd",
        workspace_document_names=["课程制度.md", "项目计划.pdf"],
    )
    assert request.workspace_document_names == ["课程制度.md", "项目计划.pdf"]
    _expect_validation_error(
        lambda: KnowledgeDocumentImportRequest(
            knowledge_base_id="kb_1234abcd",
            workspace_document_names=[r"D:\\private\\policy.md"],
        ),
        "absolute_workspace_path",
    )
    _expect_validation_error(
        lambda: KnowledgeDocumentImportRequest(
            knowledge_base_id="kb_1234abcd",
            workspace_document_names=["../policy.md"],
        ),
        "parent_workspace_path",
    )

    ready_base = KnowledgeBaseRecord(
        knowledge_base_id="kb_1234abcd",
        name="课程资料",
        status="ready",
        default_index_profile_id=PROFILE.profile_id,
        active_index_generation=1,
        active_document_version_count=2,
        created_at="2026-08-20T00:00:00Z",
        updated_at="2026-08-20T00:01:00Z",
    )
    assert ready_base.active_index_generation == 1
    _expect_validation_error(
        lambda: KnowledgeBaseRecord(
            knowledge_base_id="kb_1234abcd",
            name="空资料库",
            status="ready",
            default_index_profile_id=PROFILE.profile_id,
            active_index_generation=0,
            active_document_version_count=0,
            created_at="2026-08-20T00:00:00Z",
            updated_at="2026-08-20T00:01:00Z",
        ),
        "ready_without_generation",
    )

    generation = KnowledgeIndexGenerationRecord(
        index_generation_id="kb_gen_1234abcd",
        knowledge_base_id="kb_1234abcd",
        generation_number=1,
        status="ready",
        index_profile=PROFILE,
        document_version_ids=["kb_ver_1234abcd", "kb_ver_5678efgh"],
        created_at="2026-08-20T00:00:00Z",
        activated_at="2026-08-20T00:02:00Z",
    )
    assert_generation_can_activate(generation)
    _expect_contract_error(
        lambda: assert_generation_can_activate(generation.model_copy(update={"status": "building"})),
        "activate_building_generation",
    )

    completed_job = KnowledgeIndexJobRecord(
        index_job_id="kb_job_1234abcd",
        knowledge_base_id="kb_1234abcd",
        target_generation_number=1,
        status="completed",
        stage="completed",
        total_document_count=2,
        parsed_document_count=2,
        indexed_document_count=2,
        failed_document_count=0,
        created_at="2026-08-20T00:00:00Z",
        updated_at="2026-08-20T00:02:00Z",
    )
    assert completed_job.status == "completed"
    _expect_validation_error(
        lambda: KnowledgeIndexJobRecord(
            index_job_id="kb_job_1234abcd",
            knowledge_base_id="kb_1234abcd",
            target_generation_number=1,
            status="completed",
            stage="completed",
            total_document_count=2,
            parsed_document_count=2,
            indexed_document_count=1,
            failed_document_count=0,
            created_at="2026-08-20T00:00:00Z",
            updated_at="2026-08-20T00:02:00Z",
        ),
        "completed_job_with_missing_document",
    )
    assert_index_job_transition("queued", "running")
    assert_index_job_transition("running", "partial_failure")
    _expect_contract_error(
        lambda: assert_index_job_transition("completed", "running"),
        "terminal_job_restart",
    )

    anchor = KnowledgeSourceAnchor(
        document_id="kb_doc_1234abcd",
        document_version_id="kb_ver_1234abcd",
        source_kind="paragraph",
        source_locator="第 3 段",
        start_char=120,
        end_char=260,
        heading_path=["第一章", "范围"],
    )
    assert anchor.end_char > anchor.start_char
    _expect_validation_error(
        lambda: KnowledgeSourceAnchor(
            document_id="kb_doc_1234abcd",
            document_version_id="kb_ver_1234abcd",
            source_kind="line",
            source_locator="第 8 行",
            start_char=20,
            end_char=20,
        ),
        "invalid_source_range",
    )

    serialized = generation.model_dump()
    forbidden_keys = {"absolute_path", "source_text", "api_key", "embedding_vector"}
    assert not (forbidden_keys & set(serialized)), "知识库元数据契约泄漏了敏感字段名。"
    print("Knowledge K0.3 contract verification passed: all boundary checks.")


if __name__ == "__main__":
    main()
