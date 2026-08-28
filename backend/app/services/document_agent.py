"""首个正式内置 Agent：文档助手。

它只处理用户显式导入到受控 workspace 的 UTF-8 文本、PDF 和 DOCX。搜索和读取由确定性
Tool 完成，模型只在有限循环中决定下一步与生成结构化归纳；所有模型结论都必须引用 Runtime
分配的来源 ID，不能自行捏造文件路径、行号、页码或段落定位。
"""

from __future__ import annotations

import asyncio
import json
import re
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from pydantic import BaseModel, Field

from app.agents.runner import (
    AgentDefinition,
    AgentRunProgress,
    AgentRunner,
    AgentTool,
    AgentToolTrace,
    AgentTurnTrace,
    ToolCallingModel,
)
from app.database.task_repository import append_workflow_artifact, list_workflow_runs, save_workflow_run
from app.schemas.chat import (
    WorkflowBudgetEstimate,
    WorkflowPlan,
    WorkflowPlanPreferences,
    WorkflowRetryPolicy,
    WorkflowStep,
    WorkflowWorkspaceScope,
)
from app.schemas.document_agent import (
    DocumentAgentRunRequest,
    DocumentAgentRunResponse,
    DocumentDraftReviewRequest,
    DocumentDraftReviewSeed,
    DocumentDraftRestoreSeed,
    DocumentDraftSectionManualRevisionPreview,
    DocumentDraftSectionManualRevisionRequest,
    DocumentDraftSectionManualRevisionSeed,
    DocumentDraftTemplatePreview,
    DocumentDraftTemplatePreviewRequest,
    DocumentDraftTemplateSeed,
    DocumentDraftMergeCandidate,
    DocumentDraftMergeCandidateListResponse,
    DocumentDraftMergeConflict,
    DocumentDraftMergePlanResponse,
    DocumentDraftMergePreview,
    DocumentDraftMergePreviewRequest,
    DocumentDraftMergeResolution,
    DocumentDraftMergeSeed,
    DocumentDraftSectionRevisionPreview,
    DocumentDraftSectionBatchRevisionRequest,
    DocumentDraftSectionBatchRevisionSeed,
    DocumentDraftVersionDiffResponse,
    DocumentDraftVersionDiffSection,
    DocumentDraftVersionInfo,
    DocumentDraftSectionRevisionRequest,
    DocumentDraftSectionRevisionSeed,
    DocumentDraftSectionReviewRequest,
    DocumentDraftSectionReviewSeed,
    DocumentDraftSectionRequest,
    DocumentDraftSectionSeed,
    DocumentDraftSaveRequest,
    DocumentDraftSaveResponse,
    DocumentBriefField,
    DocumentComparison,
    DocumentChunkSummary,
    DocumentContext,
    DocumentDraftBriefField,
    DocumentDraftComparison,
    DocumentDraftFinding,
    DocumentDraftOutlineSection,
    DocumentDraftPreviewSection,
    DocumentDraftRevisionSuggestion,
    DocumentDraftRequirement,
    DocumentFinding,
    DocumentModelOutput,
    DocumentDraftSection,
    DocumentOutlineSection,
    DocumentRequirement,
    DocumentRevisionSuggestion,
    DocumentSourceRef,
)
from app.schemas.events import TaskLogEvent, TaskLogLevel
from app.schemas.model import ModelRouteAuditSnapshot
from app.schemas.workflow import (
    RuntimeExecutionLimits,
    RuntimeExecutionMetrics,
    WorkflowRun,
    WorkflowArtifact,
    WorkflowStepRun,
    WorkflowToolCall,
)
from app.services.llm_chat import is_llm_enabled
from app.services.model_gateway import (
    ModelConversationMessage,
    ModelGatewayError,
    ModelRuntime,
    ModelToolCall,
    ModelToolDefinition,
    ModelToolTurn,
    resolve_model_runtime_for_route,
)
from app.services.runtime_preferences_store import load_runtime_preferences
from app.core.config import settings
from app.services.workspace_documents import (
    WorkspaceDocumentError,
    get_workspace_document_preview,
    list_workspace_documents,
    read_workspace_document_chunks,
    read_workspace_document_excerpt,
    search_workspace_documents,
)
from app.workflow.dry_run import clear_dry_run_memory_cache, get_workflow_run


DOCUMENT_AGENT_ID = "document_agent"
# 48k 字符可以完整覆盖常见的中小型 Markdown，同时四份材料的最坏上下文仍受 192k 字符
# 的总量约束。超过该范围时必须继续分页，不能把“读到开头”伪装成“读完整份文档”。
_MODEL_EXCERPT_MAX_CHARS = 48_000
# 直接 Tool 循环最多为每份材料预留两页；超过此范围后改走“每块压缩 -> 最终归并”，避免
# 让前序原文不断累积在同一个模型会话里。
_DIRECT_DOCUMENT_CONTEXT_MAX_BYTES = _MODEL_EXCERPT_MAX_CHARS * 2
_COMPACTION_CHUNK_MAX_CHARS = 32_000
_COMPACTION_MAX_CHUNKS_PER_TASK = 12
_COMPACTION_MAX_ATTEMPTS_PER_CHUNK = 2
# 同一异步文档任务里的直接读取、分块压缩与最终归并共用一个已解析模型路由。ContextVar
# 只保存脱敏快照，不保存 Runtime、Key 或材料；它避免多层 fallback 在落库时重解析当前配置，
# 从而把“任务当时实际使用什么”错误写成“客户现在配置什么”。
_DOCUMENT_MODEL_ROUTE_AUDITS: ContextVar[tuple[ModelRouteAuditSnapshot, ...]] = ContextVar(
    "document_model_route_audits",
    default=(),
)


@dataclass(frozen=True)
class _DocumentTemplateSection:
    """内置交付模板中一个可归类章节的稳定定义。"""

    key: str
    title: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class _DocumentTemplateSpec:
    """模板只定义结构和缺失项，不包含模型提示或未验证的默认正文。"""

    template_id: str
    name: str
    title_prefix: str
    sections: tuple[_DocumentTemplateSection, ...]


@dataclass(frozen=True)
class _DocumentDraftMergePlan:
    """同根草稿的确定性三方合并计算结果。

    这里不保存到数据库，也不混入客户端提交的正文。它只持有从已完成任务恢复的 Pydantic
    快照与冲突清单，供“展示计划”和“创建预览”两条路径重新验证同一套判断。
    """

    primary_result: DocumentAgentRunResponse
    secondary_result: DocumentAgentRunResponse
    base_result: DocumentAgentRunResponse
    root_task_id: str
    merged_title: str | None
    merged_sections: tuple[DocumentDraftSection, ...]
    conflicts: tuple[DocumentDraftMergeConflict, ...]


_DOCUMENT_TEMPLATE_SPECS: dict[str, _DocumentTemplateSpec] = {
    "project_proposal": _DocumentTemplateSpec(
        template_id="project_proposal",
        name="项目方案",
        title_prefix="项目方案",
        sections=(
            _DocumentTemplateSection("background", "项目背景", ("背景", "现状", "问题", "需求背景")),
            _DocumentTemplateSection("goals", "目标与范围", ("目标", "目的", "范围", "边界")),
            _DocumentTemplateSection("plan", "实施计划", ("计划", "里程碑", "阶段", "时间", "进度")),
            _DocumentTemplateSection("deliverables", "交付与验收", ("交付", "产物", "验收", "成果")),
            _DocumentTemplateSection("risks", "风险与依赖", ("风险", "依赖", "阻塞", "注意")),
        ),
    ),
    "product_requirements": _DocumentTemplateSpec(
        template_id="product_requirements",
        name="产品需求文档",
        title_prefix="PRD",
        sections=(
            _DocumentTemplateSection("background", "背景与目标", ("背景", "现状", "目标", "目的")),
            _DocumentTemplateSection("scenarios", "用户与场景", ("用户", "角色", "场景", "流程")),
            _DocumentTemplateSection("functional", "功能需求", ("功能", "需求", "能力", "模块")),
            _DocumentTemplateSection("non_functional", "非功能需求", ("性能", "安全", "权限", "兼容", "稳定")),
            _DocumentTemplateSection("acceptance", "验收标准", ("验收", "标准", "测试", "完成条件")),
            _DocumentTemplateSection("risks", "风险与待确认", ("风险", "待确认", "问题", "依赖", "限制")),
        ),
    ),
    "meeting_minutes": _DocumentTemplateSpec(
        template_id="meeting_minutes",
        name="会议纪要",
        title_prefix="会议纪要",
        sections=(
            _DocumentTemplateSection("topic", "会议主题与背景", ("主题", "背景", "议题", "会议")),
            _DocumentTemplateSection("discussion", "讨论与结论", ("讨论", "结论", "决定", "共识")),
            _DocumentTemplateSection("actions", "行动项", ("待办", "行动", "负责人", "下一步", "任务")),
            _DocumentTemplateSection("follow_up", "待确认与跟进", ("待确认", "风险", "问题", "跟进", "依赖")),
        ),
    ),
}


def _uses_multiple_documents(output_mode: str) -> bool:
    """返回需要逐份读取材料的文档任务模式。

    多文档对比、问答和整合共用读取、分页和预算策略，但最终输出契约不同。把这个判断集中
    在这里，避免后续新增模式时只改 UI 或只改 Runtime，重新出现“选了多份却只读一份”的问题。
    """

    return output_mode in {"comparison", "cross_qa", "synthesis"}
_AUDIT_EXCERPT_MAX_CHARS = 1_200
# DocumentSourceRef 是最终 API/UI 的稳定来源协议，schema 允许 360 字符。模型可见文本、
# 审计摘要和可展示来源片段必须分别限长，不能让已成功读取的长文档在来源映射阶段失败。
_SOURCE_EXCERPT_MAX_CHARS = 360
# 保守需求降级只能依赖原文中可见的明确规则标记。这里同时覆盖中英文常用表达，但不做
# 同义词推断或语义分类，避免在模型失效时把普通叙述误报为需求。
_EXPLICIT_REQUIREMENT_MARKERS = ("必须", "需要", "应", "不得", "验收", "限制", "约束")
_EXPLICIT_REQUIREMENT_MARKERS_EN = (
    "must",
    "must not",
    "shall",
    "should",
    "required",
    "requirement",
    "acceptance",
    "constraint",
)
# 内置“关键信息卡”采用固定字段，而不是允许模型临时创造表单列。每个字段只使用显式
# 线索；没有命中的字段不会被确定性 mock 虚构，真实模型也会收到同样的来源约束。
_BRIEF_FIELD_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("purpose", ("目的", "目标", "用于", "goal", "purpose", "objective")),
    ("scope", ("范围", "边界", "scope", "in scope", "out of scope")),
    ("stakeholders", ("角色", "用户", "负责人", "团队", "客户", "stakeholder", "owner")),
    ("deliverables", ("交付", "输出", "产物", "deliverable", "output")),
    ("milestones", ("时间", "日期", "阶段", "计划", "截止", "milestone", "deadline")),
    ("risks", ("风险", "依赖", "阻塞", "注意", "不得", "risk", "dependency")),
)

# 只上报已经发生的 Runtime 阶段，不传模型正在生成的原始 token。文档任务需要先验证 JSON
# 与来源引用，不能把可能错误的中间文本伪装成客户可使用的结论。
DocumentProgressCallback = Callable[[str, str, TaskLogLevel], Awaitable[None] | None]


class DocumentReadToolInput(BaseModel):
    relative_path: str = Field(min_length=1, max_length=255)
    # 由 Runtime 返回 next_start_char 后才能读取后续分页。字符偏移只服务于同一任务的连续
    # 受控读取，最终展示仍统一使用来源行号，避免把机器内部偏移暴露为客户引用。
    start_char: int = Field(default=0, ge=0, le=1_000_000)
    max_chars: int = Field(default=_MODEL_EXCERPT_MAX_CHARS, ge=1_000, le=_MODEL_EXCERPT_MAX_CHARS)


class DocumentSearchToolInput(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=10, ge=1, le=20)


class DocumentAgentServiceError(RuntimeError):
    """记录已持久化后仍需交给 API 层解释的业务错误。"""


class DocumentDraftSaveNotFoundError(DocumentAgentServiceError):
    """保存请求关联不到一个已完成的文档草稿任务。"""


class DocumentDraftSaveConflictError(DocumentAgentServiceError):
    """默认不覆盖同名草稿，冲突必须由用户改名后重新确认。"""


class DocumentDraftSaveConfirmationError(DocumentAgentServiceError):
    """客户端未表达本次文件写入确认。"""


class DocumentDraftSectionNotFoundError(DocumentAgentServiceError):
    """派生章节预览时找不到原任务或目标章节。"""


class DocumentDraftRevisionSuggestionNotFoundError(DocumentAgentServiceError):
    """用户选择的审校建议不属于一个可安全修订的已完成任务。"""


class DocumentDraftRestoreNotFoundError(DocumentAgentServiceError):
    """用户请求恢复的历史任务不存在，或它并非可恢复的文档草稿快照。"""


class DocumentDraftVersionDiffNotFoundError(DocumentAgentServiceError):
    """版本对比缺少当前草稿、直接父版本或其中之一的可读快照。"""


class DocumentDraftMergeNotFoundError(DocumentAgentServiceError):
    """章节合并找不到可用快照、共同祖先或同根候选。"""


class _DocumentToolRuntime:
    """文档 Tool 的运行时本地上下文。

    这里只保存当前任务选择的文档和来源映射。绝对路径仍由 workspace 服务内部使用，不进入
    模型消息、最终 API 或任务输出。
    """

    def __init__(self, selected_documents: list[str], *, require_full_document_coverage: bool) -> None:
        self.selected_documents = tuple(selected_documents)
        self.require_full_document_coverage = require_full_document_coverage
        self.sources: dict[str, DocumentSourceRef] = {}
        # 原文只在本次 Runtime 内存中短暂保留，用于验证/兜底来源；不会写进审计摘要。
        self.source_texts: dict[str, str] = {}
        # 对比模式必须实际读取每一份用户选中的材料；搜索命中不能替代完整的连续文本读取。
        self.read_documents: set[str] = set()
        # 仅对比模式要求从 0 开始连续分页。这样模型即使知道某个文件名，也不能跳到文件尾部
        # 读一小段后就声称“已比较全文”。
        self._next_read_offsets: dict[str, int] = {
            document: 0 for document in selected_documents
        }
        self._source_counter = 0
        self.warnings: list[str] = []
        # 精确搜索没有命中不等于文档不存在相关语义。记录这个事实，既供模型决定回读，
        # 也让最终失败时能给用户“缩小/改写问题”的解释，而不是暴露 JSON 校验细节。
        self.exact_search_misses: list[str] = []

    def new_source(
        self,
        *,
        relative_path: str,
        start_line: int,
        end_line: int,
        source_kind: str = "line",
        source_locator: str = "",
        excerpt: str,
    ) -> DocumentSourceRef:
        self._source_counter += 1
        source = DocumentSourceRef(
            source_id=f"src_{self._source_counter:03d}",
            relative_path=relative_path,
            start_line=max(1, start_line),
            end_line=max(1, end_line),
            source_kind=(
                source_kind
                if source_kind in {"line", "page", "paragraph", "table", "mixed"}
                else "mixed"
            ),
            source_locator=source_locator,
            excerpt=_compact_text(excerpt, _SOURCE_EXCERPT_MAX_CHARS),
        )
        self.sources[source.source_id] = source
        return source

    async def read(self, request: DocumentReadToolInput) -> dict[str, Any]:
        if request.relative_path not in self.selected_documents:
            raise DocumentAgentServiceError("文档助手只能读取本次明确选择的 workspace 文档。")
        expected_start = self._next_read_offsets.get(request.relative_path, 0)
        if self.require_full_document_coverage and request.start_char != expected_start:
            raise DocumentAgentServiceError(
                "多文档对比必须从文件开头连续读取；请使用上一段返回的 next_start_char。"
            )
        try:
            # PyMuPDF/python-docx 首次解析可能比普通文本读取慢。解析离开事件循环，才能让
            # WebSocket 阶段事件和其他任务继续流动，桌面端不会看起来“卡死”。
            excerpt = await asyncio.to_thread(
                read_workspace_document_excerpt,
                relative_path=request.relative_path,
                start_char=request.start_char,
                # 比较模式按实际材料数规划了轮次，因此至少取一个完整的标准页，避免模型沿用
                # 旧版 8k 参数时重新触发“1063 行后未读”的历史问题。
                max_chars=(
                    max(request.max_chars, _MODEL_EXCERPT_MAX_CHARS)
                    if self.require_full_document_coverage
                    else request.max_chars
                ),
            )
        except WorkspaceDocumentError as exc:
            raise DocumentAgentServiceError(str(exc)) from exc

        text = str(excerpt["text"])
        source = self.new_source(
            relative_path=str(excerpt["relative_path"]),
            start_line=int(excerpt["start_line"]),
            end_line=int(excerpt["end_line"]),
            source_kind=str(excerpt.get("source_kind", "line")),
            source_locator=str(excerpt.get("source_locator", "")),
            excerpt=text,
        )
        self.source_texts[source.source_id] = text
        truncated = bool(excerpt.get("truncated"))
        next_start_char = excerpt.get("next_start_char")
        if truncated:
            if not isinstance(next_start_char, int) or next_start_char <= request.start_char:
                raise DocumentAgentServiceError("文档分页读取没有返回可继续的偏移量。")
            self._next_read_offsets[source.relative_path] = next_start_char
            warning = (
                f"{source.relative_path} 当前已读取{_source_location_text(source)}，"
                f"尚有后续内容待继续读取。"
            )
            if warning not in self.warnings:
                self.warnings.append(warning)
        else:
            self.read_documents.add(source.relative_path)
        return {
            "document": source.relative_path,
            "bytes": int(excerpt["bytes"]),
            "document_type": str(excerpt.get("document_type", "text")),
            "total_lines": int(excerpt["total_lines"]),
            "start_char": int(excerpt["start_char"]),
            "end_char": int(excerpt["end_char"]),
            "next_start_char": next_start_char if isinstance(next_start_char, int) else None,
            "truncated": truncated,
            "full_document_read": not truncated,
            "source": source.model_dump(),
            "content_status": "available" if text.strip() else "empty",
            # 这是受限模型上下文，不会被完整写进 tool-call 审计或 Qt 日志。
            "text": text,
        }

    def has_read_all_selected_documents(self) -> bool:
        """仅用于 Runner 的工具阶段收束，不把本地文件范围交给模型判断。"""

        return bool(self.selected_documents) and all(
            document in self.read_documents for document in self.selected_documents
        )

    def mark_document_compacted(self, relative_path: str) -> None:
        """标记一份文档的所有连续分块已完成压缩。

        长文档分块不是“只读了几个代表片段”：只有从第一个分块到最后一个分块都成功获得
        受控摘要后，才允许最终汇总把它视为完整材料。这个状态仍只在 Runtime 内存存在。
        """

        if relative_path not in self.selected_documents:
            raise DocumentAgentServiceError("上下文压缩尝试标记了未选择的文档。")
        self.read_documents.add(relative_path)

    async def search(self, request: DocumentSearchToolInput) -> dict[str, Any]:
        try:
            response = await asyncio.to_thread(
                search_workspace_documents,
                query=request.query,
                limit=request.limit,
                allowed_relative_paths=self.selected_documents,
            )
        except WorkspaceDocumentError as exc:
            raise DocumentAgentServiceError(str(exc)) from exc

        matches: list[dict[str, Any]] = []
        for match in response.matches:
            source = self.new_source(
                relative_path=match.relative_path,
                start_line=match.line_number,
                end_line=match.line_number,
                source_kind=match.source_kind,
                source_locator=match.source_locator,
                excerpt=match.preview,
            )
            self.source_texts[source.source_id] = match.line_text
            matches.append(
                {
                    "source": source.model_dump(),
                    "line_text": _compact_text(match.line_text, _AUDIT_EXCERPT_MAX_CHARS),
                    "preview": match.preview,
                }
            )
        fallback_read_path: str | None = None
        if response.total == 0:
            if request.query not in self.exact_search_misses:
                self.exact_search_misses.append(request.query)
            if len(self.selected_documents) == 1:
                fallback_read_path = self.selected_documents[0]
                warning = (
                    f"关键词“{request.query}”没有精确文本命中；已建议读取所选材料，"
                    "避免把零命中误判为内容不存在。"
                )
            else:
                warning = (
                    f"关键词“{request.query}”没有精确文本命中；这不代表所选材料中不存在"
                    "相关语义，请改用同义词或缩小问题范围。"
                )
            if warning not in self.warnings:
                self.warnings.append(warning)
        return {
            "query": response.query,
            "total": response.total,
            "searched_documents": response.searched_documents,
            "limit_reached": response.limit_reached,
            "matched_documents": response.matched_documents,
            "matches": matches,
            # 只在单文档零命中时提供受控的下一步建议。Runtime 不会替模型暗中读取文件，
            # 因而工具审计仍能如实展示 search -> read 的两步过程。
            "recommended_fallback_read_path": fallback_read_path,
        }


class _DeterministicDocumentModel:
    """离线验证与 mock 模式使用的确定性 Tool Calling 模型。

    它不是假装真实 LLM，而是让 UI/API 在没有 Key 时仍能走同一 AgentRunner、Tool、来源映射
    与任务审计链路。真实模式会替换为 ModelRuntime，协议不变。
    """

    def __init__(self, *, request: DocumentAgentRunRequest, selected_documents: list[str]) -> None:
        self.request = request
        self.selected_documents = selected_documents

    async def tool_turn(
        self,
        *,
        system_prompt: str,
        messages: list[ModelConversationMessage],
        tools: list[ModelToolDefinition],
    ) -> ModelToolTurn:
        del system_prompt, tools
        tool_messages = [message for message in messages if message.role == "tool"]
        if not tool_messages:
            # 跨文档任务不能依赖模糊搜索或第一份材料。先逐份读取已勾选的材料，之后才允许收束。
            if _uses_multiple_documents(self.request.output_mode):
                return ModelToolTurn(
                    tool_calls=(
                        ModelToolCall(
                            call_id="mock_compare_read_001",
                            name="document_read_text",
                            arguments={
                                "relative_path": self.selected_documents[0],
                                "start_char": 0,
                                "max_chars": _MODEL_EXCERPT_MAX_CHARS,
                            },
                        ),
                    )
                )
            if self.request.query.strip():
                return ModelToolTurn(
                    tool_calls=(
                        ModelToolCall(
                            call_id="mock_search_001",
                            name="document_search_text",
                            arguments={"query": self.request.query.strip(), "limit": 10},
                        ),
                    )
                )
            return ModelToolTurn(
                tool_calls=(
                    ModelToolCall(
                        call_id="mock_read_001",
                        name="document_read_text",
                        arguments={
                            "relative_path": self.selected_documents[0],
                            "max_chars": _MODEL_EXCERPT_MAX_CHARS,
                        },
                    ),
                )
            )

        if _uses_multiple_documents(self.request.output_mode):
            read_results: dict[str, dict[str, Any]] = {}
            tool_results: list[dict[str, Any]] = []
            for message in tool_messages:
                payload = _tool_message_payload(message.content)
                result = payload.get("result") if payload.get("ok") else None
                if not isinstance(result, dict):
                    continue
                tool_results.append(result)
                source = result.get("source")
                if message.tool_name == "document_read_text" and isinstance(source, dict):
                    document = source.get("relative_path")
                    if isinstance(document, str):
                        read_results[document] = result
            for document in self.selected_documents:
                previous_result = read_results.get(document)
                if previous_result is None:
                    return ModelToolTurn(
                        tool_calls=(
                            ModelToolCall(
                                call_id=f"mock_compare_read_{len(read_results) + 1:03d}",
                                name="document_read_text",
                                arguments={
                                    "relative_path": document,
                                    "start_char": 0,
                                    "max_chars": _MODEL_EXCERPT_MAX_CHARS,
                                },
                            ),
                        )
                    )
                if bool(previous_result.get("truncated")):
                    next_start_char = previous_result.get("next_start_char")
                    if isinstance(next_start_char, int) and next_start_char >= 0:
                        return ModelToolTurn(
                            tool_calls=(
                                ModelToolCall(
                                    call_id=f"mock_compare_page_{len(tool_results) + 1:03d}",
                                    name="document_read_text",
                                    arguments={
                                        "relative_path": document,
                                        "start_char": next_start_char,
                                        "max_chars": _MODEL_EXCERPT_MAX_CHARS,
                                    },
                                ),
                            )
                        )
            return ModelToolTurn(content=_mock_document_output_json(
                request=self.request,
                tool_results=tool_results,
            ))

        latest = tool_messages[-1]
        latest_payload = _tool_message_payload(latest.content)
        latest_result = latest_payload.get("result", {}) if latest_payload.get("ok") else {}
        if latest.tool_name == "document_search_text" and isinstance(latest_result, dict):
            matched = latest_result.get("matched_documents")
            if isinstance(matched, list) and matched:
                return ModelToolTurn(
                    tool_calls=(
                        ModelToolCall(
                            call_id="mock_read_002",
                            name="document_read_text",
                            arguments={"relative_path": str(matched[0]), "max_chars": 8_000},
                        ),
                    )
                )
            fallback_read_path = latest_result.get("recommended_fallback_read_path")
            if isinstance(fallback_read_path, str) and fallback_read_path:
                return ModelToolTurn(
                    tool_calls=(
                        ModelToolCall(
                            call_id="mock_read_after_zero_hit_002",
                            name="document_read_text",
                            arguments={
                                "relative_path": fallback_read_path,
                                "max_chars": _MODEL_EXCERPT_MAX_CHARS,
                            },
                        ),
                    )
                )
        return ModelToolTurn(content=_mock_document_output_json(
            request=self.request,
            tool_results=[latest_result] if isinstance(latest_result, dict) else [],
        ))


class _CompactedDocumentModel:
    """mock 模式下消费分块摘要的最终归并模型。

    离线验证不应绕过“分块来源 -> 最终 JSON”的真实协议。它复用既有确定性输出器，只把每个
    已压缩分块作为受控材料记录输入，因而仍会经过来源映射与跨文件比较校验。
    """

    def __init__(self, *, request: DocumentAgentRunRequest, chunk_results: list[dict[str, Any]]) -> None:
        self.request = request
        self.chunk_results = chunk_results

    async def tool_turn(
        self,
        *,
        system_prompt: str,
        messages: list[ModelConversationMessage],
        tools: list[ModelToolDefinition],
    ) -> ModelToolTurn:
        del system_prompt, messages, tools
        return ModelToolTurn(
            content=_mock_document_output_json(
                request=self.request,
                tool_results=self.chunk_results,
            )
        )


async def _should_use_document_compaction(selected_documents: list[str]) -> bool:
    """判断是否需要从直接 Tool 循环切换到分块压缩，不为常规材料增加模型调用。"""

    for document in selected_documents:
        try:
            preview = await asyncio.to_thread(
                get_workspace_document_preview,
                relative_path=document,
                preview_chars=0,
            )
        except WorkspaceDocumentError as exc:
            raise DocumentAgentServiceError(str(exc)) from exc
        # 字节数是保守触发条件：ASCII 长文一定不会被漏掉，中文文档可能略早进入压缩路径，
        # 但不会损失正确性；常见几十 KB 的 Markdown 仍沿用更快的一次/两次 Tool 读取。
        if preview.size_bytes > _DIRECT_DOCUMENT_CONTEXT_MAX_BYTES:
            return True
    return False


async def _prepare_compacted_document_context(
    *,
    request: DocumentAgentRunRequest,
    selected_documents: list[str],
    runtime: _DocumentToolRuntime,
    model: ToolCallingModel,
    mode: str,
    progress_callback: DocumentProgressCallback | None,
) -> tuple[list[AgentToolTrace], int, str]:
    """把超长材料压缩成有限、连续且带来源的上下文包。

    每个分块的模型调用都是独立短会话，旧分块原文不会滚入下一次调用；最后一次归并只接收
    短摘要和来源 ID。这是 context compaction，不是 RAG，也不建立跨任务向量索引。
    """

    chunks_by_document: dict[str, list[dict[str, object]]] = {}
    total_chunks = 0
    for document in selected_documents:
        try:
            chunks = await asyncio.to_thread(
                read_workspace_document_chunks,
                relative_path=document,
                chunk_chars=_COMPACTION_CHUNK_MAX_CHARS,
            )
        except WorkspaceDocumentError as exc:
            raise DocumentAgentServiceError(str(exc)) from exc
        chunks_by_document[document] = chunks
        total_chunks += len(chunks)

    if total_chunks > _COMPACTION_MAX_CHUNKS_PER_TASK:
        raise DocumentAgentServiceError(
            f"所选超长材料需要 {total_chunks} 个连续分块，超过本次 {_COMPACTION_MAX_CHUNKS_PER_TASK} 块的受控压缩预算。"
            "请减少材料数量或缩小问题范围后重试。"
        )

    chunk_results: list[dict[str, Any]] = []
    traces: list[AgentToolTrace] = []
    model_turn_count = 0
    chunk_index = 0
    for document in selected_documents:
        chunks = chunks_by_document[document]
        for index_in_document, chunk in enumerate(chunks, start=1):
            chunk_index += 1
            text = str(chunk["text"])
            source = runtime.new_source(
                relative_path=str(chunk["relative_path"]),
                start_line=int(chunk["start_line"]),
                end_line=int(chunk["end_line"]),
                source_kind=str(chunk.get("source_kind", "line")),
                source_locator=str(chunk.get("source_locator", "")),
                excerpt=text,
            )
            runtime.source_texts[source.source_id] = text
            await _emit_document_progress(
                progress_callback,
                "context_compaction_started",
                f"正在压缩 {source.relative_path} 第 {index_in_document}/{len(chunks)} 个文本分段（{_source_location_text(source)}）。",
            )
            summary, turns_used = await _summarize_document_chunk(
                model=model,
                mode=mode,
                relative_path=source.relative_path,
                source=source,
                text=text,
            )
            model_turn_count += turns_used
            result = {
                "document": source.relative_path,
                "bytes": int(chunk["bytes"]),
                "document_type": str(chunk.get("document_type", "text")),
                "total_lines": int(chunk["total_lines"]),
                "start_char": int(chunk["start_char"]),
                "end_char": int(chunk["end_char"]),
                "next_start_char": chunk.get("next_start_char"),
                "truncated": bool(chunk["truncated"]),
                "full_document_read": index_in_document == len(chunks),
                "source": source.model_dump(),
                "content_status": "available" if text.strip() else "empty",
                "context_strategy": "chunk_summary",
                "context_summary": summary.model_dump(mode="json"),
                # 原文仅供当前 Runtime 之后的保守需求兜底和 token 估算；持久化审计会截断它。
                "text": text,
            }
            chunk_results.append(result)
            traces.append(
                AgentToolTrace(
                    call_id=f"context_chunk_{chunk_index:03d}",
                    turn_index=chunk_index,
                    tool_name="document.read_text",
                    arguments={
                        "relative_path": source.relative_path,
                        "start_char": int(chunk["start_char"]),
                        "max_chars": _COMPACTION_CHUNK_MAX_CHARS,
                        "context_strategy": "chunk_summary",
                    },
                    result=result,
                )
            )
            await _emit_document_progress(
                progress_callback,
                "context_compaction_completed",
                f"已压缩 {source.relative_path} 第 {index_in_document}/{len(chunks)} 个文本分段。",
            )
        runtime.mark_document_compacted(document)
        runtime.warnings.append(
            f"{document} 已按 {len(chunks)} 个连续分段完成上下文压缩；最终引用仍保留原始来源定位。"
        )

    return traces, model_turn_count, _compacted_document_user_message(
        request=request,
        selected_documents=selected_documents,
        chunk_results=chunk_results,
    )


async def _summarize_document_chunk(
    *,
    model: ToolCallingModel,
    mode: str,
    relative_path: str,
    source: DocumentSourceRef,
    text: str,
) -> tuple[DocumentChunkSummary, int]:
    """为一个连续分块生成短摘要；失败仅有限重试，避免长文档任务无限消耗额度。"""

    if mode == "mock":
        return _mock_document_chunk_summary(text), 1

    last_error = ""
    for attempt in range(1, _COMPACTION_MAX_ATTEMPTS_PER_CHUNK + 1):
        try:
            turn = await model.tool_turn(
                system_prompt=_document_chunk_system_prompt(),
                messages=[
                    ModelConversationMessage(
                        role="user",
                        content=(
                            f"材料：{relative_path}\n"
                            f"原文范围：{_source_location_text(source)}\n"
                            "请只压缩下面这一段，不要引用或猜测段外内容：\n\n"
                            f"{text}"
                        ),
                    )
                ],
                tools=[],
            )
            if turn.tool_calls:
                raise DocumentAgentServiceError("分块摘要阶段不允许调用工具。")
            return _parse_document_chunk_summary(turn.content), attempt
        except Exception as exc:
            last_error = str(exc)

    raise DocumentAgentServiceError(
        f"{relative_path} {_source_location_text(source)}的分块摘要连续 "
        f"{_COMPACTION_MAX_ATTEMPTS_PER_CHUNK} 次失败：{last_error or '模型没有返回可用 JSON'}"
    )


def _document_chunk_system_prompt() -> str:
    """返回单块压缩的严格 JSON 提示，限制中间结果大小和事实范围。"""

    return """你是 AgentFlow 的文档上下文压缩器。只总结当前输入的一个原文分块，不能补充段外事实，
不能编造文件名、行号或来源。只返回 JSON object，不要 Markdown 代码围栏：
{"summary":"不超过1200字的忠实摘要","key_points":["要点"],"requirement_candidates":["明确需求或约束"],"open_questions":["材料本身留下的待确认问题"],"confidence":"low|medium|high"}
没有明确需求或待确认问题时对应数组为空。"""


def _parse_document_chunk_summary(content: str) -> DocumentChunkSummary:
    """解析供应商返回的中间 JSON，兼容偶尔附带的代码围栏或自然语言前缀。"""

    raw = content.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    start_index = raw.find("{")
    if start_index < 0:
        raise DocumentAgentServiceError("分块摘要没有返回 JSON object。")
    try:
        payload, _remaining = json.JSONDecoder().raw_decode(raw[start_index:])
    except json.JSONDecodeError as exc:
        raise DocumentAgentServiceError("分块摘要没有返回合法 JSON。") from exc
    if not isinstance(payload, dict):
        raise DocumentAgentServiceError("分块摘要顶层必须是 JSON object。")
    try:
        return DocumentChunkSummary.model_validate(payload)
    except Exception as exc:
        raise DocumentAgentServiceError("分块摘要没有通过结构化协议校验。") from exc


def _mock_document_chunk_summary(text: str) -> DocumentChunkSummary:
    """离线模式的保守分块压缩：只摘取原文可见行，不假装具备模型语义。"""

    lines = [line.strip(" -\t#") for line in text.splitlines() if line.strip()]
    headings = [line for line in lines if line.startswith("#")]
    key_points = (headings + lines)[:6]
    requirement_candidates = [
        line for line in lines
        if any(marker in line for marker in ("必须", "需要", "应", "不得", "验收", "限制", "约束"))
    ][:8]
    summary = _compact_text("；".join((headings + lines)[:3]), 1_200)
    return DocumentChunkSummary(
        summary=summary or "该文本分块为空或没有可提取的文字。",
        key_points=[_compact_text(line, 400) for line in key_points],
        requirement_candidates=[_compact_text(line, 400) for line in requirement_candidates],
        open_questions=[],
        confidence="medium" if lines else "low",
    )


def _compacted_document_user_message(
    *,
    request: DocumentAgentRunRequest,
    selected_documents: list[str],
    chunk_results: list[dict[str, Any]],
) -> str:
    """仅把受控短摘要给最终归并模型，避免原文分块在对话历史中再次膨胀。"""

    evidence: list[dict[str, Any]] = []
    for result in chunk_results:
        source = result.get("source")
        summary = result.get("context_summary")
        if not isinstance(source, dict) or not isinstance(summary, dict):
            continue
        evidence.append(
            {
                "source_id": source.get("source_id"),
                "document": source.get("relative_path"),
                "source_location": source.get("source_locator")
                or f"第 {source.get('start_line')}-{source.get('end_line')} 行",
                "summary": summary.get("summary"),
                "key_points": summary.get("key_points", []),
                "requirement_candidates": summary.get("requirement_candidates", []),
                "open_questions": summary.get("open_questions", []),
                "confidence": summary.get("confidence", "medium"),
            }
        )
    section_seed_message = ""
    if request.output_mode == "section_draft" and request.section_draft is not None:
        seed = request.section_draft
        section_seed_message = (
            "\n既有章节 ID：%s\n既有章节标题：%s\n既有章节正文：%s\n用户本次调整要求：%s\n"
            % (seed.section_id, seed.heading, seed.current_body, seed.instruction)
        )
    elif request.output_mode == "draft_review" and request.draft_review is not None:
        seed = request.draft_review
        section_seed_message = (
            "\n待核验草稿标题：%s\n待核验章节：%s\n核验关注点：%s\n"
            % (
                seed.draft_title,
                json.dumps([item.model_dump(mode="json") for item in seed.draft_sections], ensure_ascii=False),
                seed.focus or "无，优先核对材料是否支持草稿中的具体表述。",
            )
        )
    elif request.output_mode == "section_review" and request.section_review is not None:
        seed = request.section_review
        section_seed_message = (
            "\n待审校章节 ID：%s\n待审校章节标题：%s\n待审校章节正文：%s\n审校关注点：%s\n"
            % (
                seed.section_id,
                seed.heading,
                seed.current_body,
                seed.focus or "无，优先检查准确性、清晰度、一致性和结构。",
            )
        )
    return (
        f"任务目标：{request.task_goal.strip()}\n"
        f"本次已选择并完成连续压缩的文档：{', '.join(selected_documents)}\n"
        f"可选定位关键词：{request.query.strip() or '无'}\n"
        f"输出模式：{request.output_mode}\n"
        + section_seed_message
        + "下面是按原文连续分块获得的受控摘要证据。只能使用其中的 source_id；"
        + "不要声称段外细节，也不要编造来源。\n\n"
        + json.dumps(evidence, ensure_ascii=False)
    )


def _document_output_contract(output_mode: str) -> str:
    """返回当前交付模式所需的最小 JSON 契约与输出大小。

    ``DocumentModelOutput`` 是 Runtime 内部的统一接收模型，但不代表每次调用都该让模型输出
    全部可选字段。早期通用 schema 会让草稿任务同时生成需求、对比、审校等无关数组，在 2k
    token 输出预算下经常截断 JSON。这里按交付目标收窄模型可见的字段，后端仍保持同一来源
    校验和 Pydantic Guardrail。
    """

    common = (
        '所有对象只能引用已出现的 source_id。answer、summary 各不超过 280 个汉字；'
        'confidence 只能是 low、medium 或 high。'
    )
    contracts = {
        "requirements": (
            '只返回 {"answer":"","answer_source_ids":["src_001"],"summary":"",'
            '"requirements":[{"id":"req_01","text":"","category":"functional|output|constraint|acceptance|unknown",'
            '"priority":"must|should|could|unknown","source_ids":["src_001"],"confidence":"medium"}],'
            '"open_questions":[{"text":"","source_ids":["src_001"],"confidence":"low"}],"confidence":"medium"}。'
            "requirements 最多 10 条，每条不超过 260 个汉字；open_questions 最多 4 条。"
        ),
        "comparison": (
            '只返回 {"answer":"","answer_source_ids":["src_001","src_002"],"summary":"",'
            '"comparisons":[{"dimension":"","kind":"common|difference|missing|uncertain","summary":"",'
            '"source_ids":["src_001","src_002"],"confidence":"medium"}],'
            '"open_questions":[{"text":"","source_ids":["src_001"],"confidence":"low"}],"confidence":"medium"}。'
            "comparisons 最多 8 条，每条不超过 260 个汉字；每项必须至少引用两份不同文档。"
        ),
        "cross_qa": (
            '只返回 {"answer":"","answer_source_ids":["src_001","src_002"],"summary":"",'
            '"open_questions":[{"text":"","source_ids":["src_001"],"confidence":"low"}],"confidence":"medium"}。'
            "answer_source_ids 必须覆盖至少两份不同文档；open_questions 最多 4 条。"
        ),
        "synthesis": (
            '只返回 {"answer":"","answer_source_ids":["src_001","src_002"],"summary":"",'
            '"requirements":[{"id":"req_01","text":"","category":"functional|output|constraint|acceptance|unknown",'
            '"priority":"must|should|could|unknown","source_ids":["src_001","src_002"],"confidence":"medium"}],'
            '"open_questions":[{"text":"","source_ids":["src_001"],"confidence":"low"}],"confidence":"medium"}。'
            "requirements 最多 10 条；冲突或证据不足只放入 open_questions，最多 6 条。"
        ),
        "brief": (
            '只返回 {"answer":"","answer_source_ids":["src_001"],"summary":"",'
            '"brief_fields":[{"key":"subject|purpose|scope|stakeholders|deliverables|milestones|risks",'
            '"value":"","source_ids":["src_001"],"confidence":"medium"}],'
            '"open_questions":[{"text":"","source_ids":["src_001"],"confidence":"low"}],"confidence":"medium"}。'
            "brief_fields 最多 7 项，每项 value 不超过 220 个汉字；open_questions 最多 4 条。"
        ),
        "outline": (
            '只返回 {"answer":"","answer_source_ids":["src_001"],"summary":"",'
            '"outline_sections":[{"id":"section_01","title":"","intent":"","key_points":[""],'
            '"source_ids":["src_001"],"confidence":"medium"}],'
            '"open_questions":[{"text":"","source_ids":["src_001"],"confidence":"low"}],"confidence":"medium"}。'
            "outline_sections 最多 6 项，每项最多 5 个要点；intent 不超过 220 个汉字。"
        ),
        "draft": (
            '只返回 {"answer":"","answer_source_ids":["src_001"],"summary":"","draft_title":"",'
            '"draft_sections":[{"id":"draft_01","heading":"","body":"","source_ids":["src_001"],'
            '"confidence":"medium"}],"open_questions":[{"text":"","source_ids":["src_001"],'
            '"confidence":"low"}],"confidence":"medium"}。'
            "draft_sections 为 1 至 4 项，每项 body 不超过 420 个汉字；不得输出 requirements、"
            "comparisons、brief_fields、outline_sections、revision_suggestions、constraints、todos 或 entities。"
        ),
        "section_draft": (
            '只返回 {"answer":"","answer_source_ids":["src_001"],"summary":"","draft_title":"",'
            '"draft_sections":[{"id":"既有章节 ID","heading":"既有章节标题","body":"",'
            '"source_ids":["src_001"],"confidence":"medium"}],"open_questions":[],"confidence":"medium"}。'
            "draft_sections 必须恰好 1 项，id 与 heading 必须保留用户消息中的值，body 不超过 900 个汉字。"
        ),
        "draft_review": (
            '只返回 {"answer":"","answer_source_ids":["src_001"],"summary":"",'
            '"constraints":[{"text":"","source_ids":["src_001"],"confidence":"medium"}],'
            '"open_questions":[{"text":"","source_ids":["src_001"],"confidence":"low"}],"confidence":"medium"}。'
            "constraints 与 open_questions 各最多 8 条；不得生成或改写草稿章节。"
        ),
        "section_review": (
            '只返回 {"answer":"","answer_source_ids":["src_001"],"summary":"",'
            '"revision_suggestions":[{"id":"review_01","severity":"important|suggestion",'
            '"category":"accuracy|clarity|consistency|structure|style","original_excerpt":"",'
            '"suggested_text":"","reason":"","source_ids":["src_001"],"confidence":"medium"}],'
            '"open_questions":[{"text":"","source_ids":["src_001"],"confidence":"low"}],"confidence":"medium"}。'
            "revision_suggestions 最多 6 条；original_excerpt 不超过 240 个汉字，suggested_text 不超过 420 个汉字。"
        ),
    }
    fallback = (
        '只返回 {"answer":"","answer_source_ids":["src_001"],"summary":"",'
        '"requirements":[{"id":"req_01","text":"","category":"unknown","priority":"unknown",'
        '"source_ids":["src_001"],"confidence":"medium"}],'
        '"open_questions":[{"text":"","source_ids":["src_001"],"confidence":"low"}],"confidence":"medium"}。'
        "requirements 最多 10 条，open_questions 最多 4 条。"
    )
    return f"{contracts.get(output_mode, fallback)}\n{common}"


def _document_model_with_output_budget(
    model: ToolCallingModel,
    output_mode: str,
) -> ToolCallingModel:
    """仅为文档交付最终 JSON 申请与契约相称的受控输出预算。

    这不是全局设置，也不改变聊天、Tool 调用或用户保存的模型配置。不同 Provider 都通过
    ``ModelRuntime`` 使用这个固定的请求级副本；测试/Mock 模型则保持原样，避免伪造能力。
    """

    if not isinstance(model, ModelRuntime):
        return model
    required_tokens = {
        "draft": 4_096,
        "section_draft": 3_072,
        "section_review": 3_072,
        "draft_review": 3_072,
    }.get(output_mode, 2_560)
    return replace(model, max_tokens=max(model.max_tokens, required_tokens))


def _document_compacted_system_prompt(output_mode: str) -> str:
    """为最终归并阶段声明“证据已压缩、工具已关闭”的输出约束。"""

    if output_mode == "comparison":
        mode_rule = "当前是多文档对比模式：comparisons 的每项必须同时引用至少两份不同文档的 source_id。"
    elif output_mode == "cross_qa":
        mode_rule = "当前是跨文档问答模式：answer_source_ids 必须覆盖至少两份不同文档；只回答用户问题，不要求生成 comparisons。"
    elif output_mode == "synthesis":
        mode_rule = (
            "当前是跨文档整合模式：answer_source_ids 必须覆盖至少两份不同文档。"
            "请将可兼容的内容整理为 requirements；重复或互相支持的条目可合并 source_id，"
            "存在冲突或无法确认的说法只能列入 open_questions，不能擅自裁决。"
        )
    elif output_mode == "brief":
        mode_rule = (
            "当前是关键信息卡模式：仅从证据中提取 subject、purpose、scope、stakeholders、"
            "deliverables、milestones、risks 七类明确事实，写入 brief_fields。没有明确证据的"
            "字段不要猜测；可在 open_questions 说明材料缺口。"
        )
    elif output_mode == "outline":
        mode_rule = (
            "当前是结构化大纲模式：基于证据生成 1 至 8 个 outline_sections，每项包含章节标题、"
            "写作意图、关键要点和 source_id。大纲只供用户审阅后再讨论正式创作，不代表已经写入、"
            "覆盖或导出文件；材料未明确表达的章节不要按通用模板补写。"
        )
    elif output_mode == "draft":
        mode_rule = (
            "当前是 Markdown 草稿预览模式：基于证据生成 draft_title 和 1 至 6 个 draft_sections。"
            "每个章节必须包含 heading、body 和 source_id；正文只能整理材料已经表达的事实，必要时"
            "明确保留待确认项。当前只生成供用户审阅的预览，不代表已经创建、覆盖、保存或导出文件。"
        )
    elif output_mode == "section_draft":
        mode_rule = (
            "当前是单章节创作预览模式：用户消息给出了既有章节 ID、标题、正文和调整要求。只生成"
            "一个 draft_section，必须保留该章节 ID 与标题，并只能根据分块证据改写或扩展正文；"
            "不生成其他章节，也不声明已修改、保存或导出原草稿。"
        )
    elif output_mode == "draft_review":
        mode_rule = (
            "当前是草稿事实核验模式：先完整读取所选材料，再核对用户消息中的草稿快照。"
            "只在 constraints 返回材料明确支持的关键表述，在 open_questions 返回需复核的表述；"
            "不得生成、改写、保存或导出 draft_sections。"
        )
    elif output_mode == "section_review":
        mode_rule = (
            "当前是单章节审校模式：用户消息给出了已验证的章节 ID、标题、正文和关注点。"
            "只在 revision_suggestions 返回 0 至 6 条建议，每条必须含 id、severity、category、"
            "original_excerpt、suggested_text、reason、source_ids、confidence。original_excerpt 必须逐字"
            "摘自当前章节，suggested_text 只是候选表达；不得生成、替换、保存或导出 draft_sections。"
        )
    else:
        mode_rule = "当前是单文档分析模式：请基于已提供的分块证据直接收束。"
    return (
        f"""你是 AgentFlow 的文档助手归并阶段。所有受控材料已被连续分块压缩；当前没有可调用工具。
只能根据输入中的分块摘要和 source_id 输出结论，不能编造文件名、行号、原文内容或 source_id。
{mode_rule}
完成后只返回 JSON object，不要 Markdown 代码围栏。"""
        + _document_output_contract(output_mode)
        + "\n"
        + "当证据不足时，在 answer 或 open_questions 中明确说明，不要用推测填充。"
    )


async def _run_compacted_document_agent(
    *,
    request: DocumentAgentRunRequest,
    task_id: str,
    selected_documents: list[str],
    runtime: _DocumentToolRuntime,
    model: ToolCallingModel,
    mode: str,
    started_at: datetime,
    progress_callback: DocumentProgressCallback | None,
) -> DocumentAgentRunResponse:
    """运行“连续分块压缩 -> 最终归并”的长文档子流程，并沿用正式任务持久化。"""

    await _emit_document_progress(
        progress_callback,
        "context_compaction_planned",
        "材料超过直接读取范围，正在建立受控分块压缩计划。",
    )
    try:
        chunk_traces, chunk_turns, final_user_message = await _prepare_compacted_document_context(
            request=request,
            selected_documents=selected_documents,
            runtime=runtime,
            model=model,
            mode=mode,
            progress_callback=progress_callback,
        )
    except DocumentAgentServiceError as exc:
        context = DocumentContext(
            documents=selected_documents,
            warnings=runtime.warnings,
            missing_context=[str(exc)],
            confidence="low",
        )
        return _persist_document_result(
            task_id=task_id,
            request=request,
            selected_documents=selected_documents,
            mode=mode,
            status="budget_exhausted",
            stop_reason="context_compaction_budget_exhausted",
            reply=f"文档助手无法在本次受控上下文预算内完成分析：{exc}",
            context=context,
            tool_traces=(),
            turn_count=0,
            started_at=started_at,
        )

    await _emit_document_progress(
        progress_callback,
        "context_compaction_ready",
        "全部分段已压缩，正在根据可追溯证据归并最终结论。",
    )
    final_definition = AgentDefinition(
        agent_id=DOCUMENT_AGENT_ID,
        system_prompt=_document_compacted_system_prompt(request.output_mode),
        tools=(),
        output_model=DocumentModelOutput,
        # 最终归并不需要 Tool，但为供应商偶发的非 JSON 输出预留一次纯格式修复回合。
        max_turns=2,
        max_tool_calls=0,
    )
    final_model: ToolCallingModel = (
        model if mode == "llm" else _CompactedDocumentModel(
            request=request,
            chunk_results=[trace.result for trace in chunk_traces],
        )
    )
    result = await AgentRunner().run(
        definition=final_definition,
        model=final_model,
        user_message=final_user_message,
        progress_callback=_compacted_result_progress_callback(progress_callback),
    )
    total_turn_count = chunk_turns + len(result.turn_traces)
    _record_output_repair_warning(runtime.warnings, result.turn_traces)
    if result.status != "completed" or not isinstance(result.output, DocumentModelOutput):
        brief_fallback_response = _build_conservative_brief_fallback_response(
            request=request,
            task_id=task_id,
            selected_documents=selected_documents,
            mode=mode,
            runtime=runtime,
            stop_reason=result.stop_reason,
            tool_traces=tuple(chunk_traces),
            turn_count=total_turn_count,
            output_format_repair_count=_output_repair_count(result.turn_traces),
            started_at=started_at,
        )
        if brief_fallback_response is not None:
            return brief_fallback_response
        outline_fallback_response = _build_conservative_outline_fallback_response(
            request=request,
            task_id=task_id,
            selected_documents=selected_documents,
            mode=mode,
            runtime=runtime,
            stop_reason=result.stop_reason,
            tool_traces=tuple(chunk_traces),
            turn_count=total_turn_count,
            output_format_repair_count=_output_repair_count(result.turn_traces),
            started_at=started_at,
        )
        if outline_fallback_response is not None:
            return outline_fallback_response
        context = DocumentContext(
            documents=selected_documents,
            warnings=runtime.warnings,
            missing_context=[result.message or "模型未能生成可用的结构化文档结果。"],
            confidence="low",
        )
        mapped_status = "budget_exhausted" if result.status == "budget_exhausted" else "failed"
        if result.status == "max_turns_exceeded":
            mapped_status = "max_turns_exceeded"
        return _persist_document_result(
            task_id=task_id,
            request=request,
            selected_documents=selected_documents,
            mode=mode,
            status=mapped_status,
            stop_reason=result.stop_reason,
            reply=_failure_reply(result.message, result.stop_reason),
            context=context,
            tool_traces=tuple(chunk_traces),
            turn_count=total_turn_count,
            output_format_repair_count=_output_repair_count(result.turn_traces),
            started_at=started_at,
        )

    traces = tuple(chunk_traces)
    try:
        await _emit_document_progress(progress_callback, "materializing_result", "正在生成可展示的结论、来源和任务记录。")
        context = _materialize_document_context(
            output=result.output,
            selected_documents=selected_documents,
            source_map=runtime.sources,
            warnings=runtime.warnings,
        )
        _normalize_section_draft_context(request=request, context=context)
        _normalize_draft_review_context(request=request, context=context)
        _normalize_section_review_context(request=request, context=context)
        _validate_multi_document_context(
            request=request,
            context=context,
            runtime=runtime,
            answer_source_ids=result.output.answer_source_ids,
        )
        _validate_requested_output_context(request=request, context=context)
        _apply_requested_output_fallback(request=request, context=context, runtime=runtime)
    except DocumentAgentServiceError as exc:
        brief_fallback_response = _build_conservative_brief_fallback_response(
            request=request,
            task_id=task_id,
            selected_documents=selected_documents,
            mode=mode,
            runtime=runtime,
            stop_reason="model_output_invalid",
            tool_traces=traces,
            turn_count=total_turn_count,
            output_format_repair_count=_output_repair_count(result.turn_traces),
            started_at=started_at,
        )
        if brief_fallback_response is not None:
            return brief_fallback_response
        outline_fallback_response = _build_conservative_outline_fallback_response(
            request=request,
            task_id=task_id,
            selected_documents=selected_documents,
            mode=mode,
            runtime=runtime,
            stop_reason="model_output_invalid",
            tool_traces=traces,
            turn_count=total_turn_count,
            output_format_repair_count=_output_repair_count(result.turn_traces),
            started_at=started_at,
        )
        if outline_fallback_response is not None:
            return outline_fallback_response
        context = DocumentContext(
            documents=selected_documents,
            warnings=runtime.warnings,
            missing_context=[str(exc)],
            confidence="low",
        )
        return _persist_document_result(
            task_id=task_id,
            request=request,
            selected_documents=selected_documents,
            mode=mode,
            status="failed",
            stop_reason="model_output_invalid",
            reply=f"文档助手没有生成可验证的来源引用：{exc}",
            context=context,
            tool_traces=traces,
            turn_count=total_turn_count,
            output_format_repair_count=_output_repair_count(result.turn_traces),
            started_at=started_at,
        )

    return _persist_document_result(
        task_id=task_id,
        request=request,
        selected_documents=selected_documents,
        mode=mode,
        status="completed",
        stop_reason="completed",
        reply=result.output.answer,
        context=context,
        tool_traces=traces,
        turn_count=total_turn_count,
        output_format_repair_count=_output_repair_count(result.turn_traces),
        started_at=started_at,
    )




async def run_document_agent(
    request: DocumentAgentRunRequest,
    *,
    task_id: str | None = None,
    progress_callback: DocumentProgressCallback | None = None,
) -> DocumentAgentRunResponse:
    """执行一次文档助手任务并把 trace 持久化到现有任务历史。"""

    task_id = task_id or f"task_document_{uuid4().hex[:12]}"
    _DOCUMENT_MODEL_ROUTE_AUDITS.set(())
    started_at = datetime.now(UTC)
    # 修订、恢复和模板交付预览都只操作已验证的 SQLite 快照，不应重新消耗模型或读取文件。
    # 它们仍沿用相同的异步任务事件和 SQLite 审计，方便客户在历史任务中复盘版本来源。
    if request.output_mode in {"section_revision", "section_revision_batch"}:
        return await _run_document_draft_section_revision_preview(
            request=request,
            task_id=task_id,
            started_at=started_at,
            progress_callback=progress_callback,
        )
    if request.output_mode == "section_manual_revision":
        return await _run_document_draft_section_manual_revision_preview(
            request=request,
            task_id=task_id,
            started_at=started_at,
            progress_callback=progress_callback,
        )
    if request.output_mode == "draft_restore":
        return await _run_document_draft_restore_preview(
            request=request,
            task_id=task_id,
            started_at=started_at,
            progress_callback=progress_callback,
        )
    if request.output_mode == "draft_template":
        return await _run_document_draft_template_preview(
            request=request,
            task_id=task_id,
            started_at=started_at,
            progress_callback=progress_callback,
        )
    if request.output_mode == "draft_merge":
        return await _run_document_draft_merge_preview(
            request=request,
            task_id=task_id,
            started_at=started_at,
            progress_callback=progress_callback,
        )
    await _emit_document_progress(progress_callback, "scope_checking", "正在确认本次允许读取的材料范围。")
    selected_documents, selection_warning, early_status, early_message = await _select_documents(request)
    runtime = _DocumentToolRuntime(
        selected_documents,
        # 跨文档问答也必须读全用户明确选中的材料，不能只靠其中一份给出“综合”回答。
        require_full_document_coverage=_uses_multiple_documents(request.output_mode),
    )
    if selection_warning:
        runtime.warnings.append(selection_warning)

    if early_status:
        context = DocumentContext(
            documents=selected_documents,
            warnings=runtime.warnings,
            missing_context=[early_message],
            confidence="low",
        )
        return _persist_document_result(
            task_id=task_id,
            request=request,
            selected_documents=selected_documents,
            mode="mock" if not is_llm_enabled() else "llm",
            status=early_status,
            stop_reason=early_status,
            reply=early_message,
            context=context,
            tool_traces=(),
            turn_count=0,
            started_at=started_at,
        )

    await _emit_document_progress(
        progress_callback,
        "scope_ready",
        "材料范围已确认，正在准备受控分析。",
    )

    read_tool = AgentTool(
        name="document.read_text",
        model_name="document_read_text",
        # 单文档任务读取一次即可收束；跨文档任务必须允许逐份读取已勾选材料。
        closes_tool_phase=not _uses_multiple_documents(request.output_mode),
        description="按顺序读取本次选择的受控 workspace 文档；长文档会返回可继续的分页偏移和来源 ID。",
        input_model=DocumentReadToolInput,
        handler=runtime.read,
    )
    search_tool = AgentTool(
        name="document.search_text",
        model_name="document_search_text",
        description="在本次选择的受控 workspace 文档中精确搜索关键词，返回行号和来源 ID。",
        input_model=DocumentSearchToolInput,
        handler=runtime.search,
    )
    definition = AgentDefinition(
        agent_id=DOCUMENT_AGENT_ID,
        system_prompt=_document_system_prompt(request.output_mode),
        # 字段卡、大纲和草稿预览都只处理单份、已选择的材料。暴露精确搜索会诱导模型逐字段
        # 或逐章节探测，白白消耗轮次且不能扩大事实范围；因此只提供一次连续读取工具，随后收束。
        tools=(read_tool,)
        if request.output_mode in {
            "brief",
            "outline",
            "draft",
            "section_draft",
            "draft_review",
            "section_review",
            "section_revision",
            "section_revision_batch",
        }
        else (search_tool, read_tool),
        output_model=DocumentModelOutput,
        # 跨文档任务至少要为“每份材料一次读取 + 一次最终 JSON”预留轮次。每份材料最多允许
        # 再取一页，专门处理接近 48k 字符边界的文本；这不是把通用上限粗暴调大。
        max_turns=_document_agent_turn_limit(request, selected_documents),
        max_tool_calls=_document_agent_tool_call_limit(request, selected_documents),
        # 真实模型偶尔会在证据已经齐全后继续搜索。跨文档任务由 Runtime 判断是否读全，
        # 下一轮不再暴露 Tool，避免浪费预算并强制进入 JSON/来源校验。
        close_tool_phase_when=(
            runtime.has_read_all_selected_documents
            if _uses_multiple_documents(request.output_mode)
            else None
        ),
    )
    mode = "llm" if is_llm_enabled() else "mock"
    if mode == "llm":
        try:
            resolution = resolve_model_runtime_for_route("document_analysis")
            model = _document_model_with_output_budget(
                resolution.runtime,
                request.output_mode,
            )
            _DOCUMENT_MODEL_ROUTE_AUDITS.set(
                (resolution.audit_snapshot(stage="document_analysis"),)
            )
        except ModelGatewayError as exc:
            context = DocumentContext(
                documents=selected_documents,
                warnings=runtime.warnings,
                missing_context=["模型配置不可用。"],
                confidence="low",
            )
            return _persist_document_result(
                task_id=task_id,
                request=request,
                selected_documents=selected_documents,
                mode=mode,
                status="failed",
                stop_reason="model_configuration_invalid",
                reply=f"文档助手无法调用当前模型：{exc}",
                context=context,
                tool_traces=(),
                turn_count=0,
                started_at=started_at,
            )
    else:
        model = _DeterministicDocumentModel(request=request, selected_documents=selected_documents)

    if await _should_use_document_compaction(selected_documents):
        return await _run_compacted_document_agent(
            request=request,
            task_id=task_id,
            selected_documents=selected_documents,
            runtime=runtime,
            model=model,
            mode=mode,
            started_at=started_at,
            progress_callback=progress_callback,
        )

    await _emit_document_progress(
        progress_callback,
        "analysis_started",
        "正在重新读取材料并撰写所选章节。"
        if request.output_mode == "section_draft"
        else "正在重新读取材料并核验草稿事实。"
        if request.output_mode == "draft_review"
        else "正在重新读取材料并审校所选章节。"
        if request.output_mode == "section_review"
        else "文档助手已开始分析，将先获取必要证据再整理结论。",
    )

    async def on_runner_progress(event: AgentRunProgress) -> None:
        if event.stage == "tool_execution_started":
            message = (
                "正在在选定文档中定位相关内容。"
                if event.tool_name == "document.search_text"
                else "正在读取选定文档，并建立可追溯来源。"
            )
        elif event.stage == "tool_execution_completed":
            message = "材料已读取，正在根据来源整理结论。"
        elif event.stage == "output_validation_started":
            message = "正在核对结论结构与来源引用。"
        elif event.stage == "output_format_repair_started":
            message = "模型回复格式不完整，正在进行一次不调用工具的安全修复。"
        else:
            message = "正在请求模型规划下一步分析。"
        await _emit_document_progress(progress_callback, event.stage, message)

    result = await AgentRunner().run(
        definition=definition,
        model=model,
        user_message=_document_user_message(request, selected_documents),
        progress_callback=on_runner_progress,
    )
    _record_output_repair_warning(runtime.warnings, result.turn_traces)
    if result.status != "completed" or not isinstance(result.output, DocumentModelOutput):
        no_hit_response = _build_exact_search_miss_response(
            request=request,
            task_id=task_id,
            selected_documents=selected_documents,
            mode=mode,
            runtime=runtime,
            tool_traces=result.tool_traces,
            turn_count=len(result.turn_traces),
            output_format_repair_count=_output_repair_count(result.turn_traces),
            started_at=started_at,
        )
        if no_hit_response is not None:
            return no_hit_response
        brief_fallback_response = _build_conservative_brief_fallback_response(
            request=request,
            task_id=task_id,
            selected_documents=selected_documents,
            mode=mode,
            runtime=runtime,
            stop_reason=result.stop_reason,
            tool_traces=result.tool_traces,
            turn_count=len(result.turn_traces),
            output_format_repair_count=_output_repair_count(result.turn_traces),
            started_at=started_at,
        )
        if brief_fallback_response is not None:
            return brief_fallback_response
        outline_fallback_response = _build_conservative_outline_fallback_response(
            request=request,
            task_id=task_id,
            selected_documents=selected_documents,
            mode=mode,
            runtime=runtime,
            stop_reason=result.stop_reason,
            tool_traces=result.tool_traces,
            turn_count=len(result.turn_traces),
            output_format_repair_count=_output_repair_count(result.turn_traces),
            started_at=started_at,
        )
        if outline_fallback_response is not None:
            return outline_fallback_response
        requirements_fallback_response = _build_conservative_requirements_fallback_response(
            request=request,
            task_id=task_id,
            selected_documents=selected_documents,
            mode=mode,
            runtime=runtime,
            stop_reason=result.stop_reason,
            tool_traces=result.tool_traces,
            turn_count=len(result.turn_traces),
            output_format_repair_count=_output_repair_count(result.turn_traces),
            started_at=started_at,
        )
        if requirements_fallback_response is not None:
            return requirements_fallback_response
        context = DocumentContext(
            documents=selected_documents,
            warnings=runtime.warnings,
            missing_context=[result.message or "模型未能生成可用的结构化文档结果。"],
            confidence="low",
        )
        mapped_status = "budget_exhausted" if result.status == "budget_exhausted" else "failed"
        if result.status == "max_turns_exceeded":
            mapped_status = "max_turns_exceeded"
        return _persist_document_result(
            task_id=task_id,
            request=request,
            selected_documents=selected_documents,
            mode=mode,
            status=mapped_status,
            stop_reason=result.stop_reason,
            reply=_failure_reply(result.message, result.stop_reason),
            context=context,
            tool_traces=result.tool_traces,
            turn_count=len(result.turn_traces),
            output_format_repair_count=_output_repair_count(result.turn_traces),
            started_at=started_at,
        )

    try:
        await _emit_document_progress(progress_callback, "materializing_result", "正在生成可展示的结论、来源和任务记录。")
        context = _materialize_document_context(
            output=result.output,
            selected_documents=selected_documents,
            source_map=runtime.sources,
            warnings=runtime.warnings,
        )
        _normalize_section_draft_context(request=request, context=context)
        _normalize_draft_review_context(request=request, context=context)
        _normalize_section_review_context(request=request, context=context)
        _validate_multi_document_context(
            request=request,
            context=context,
            runtime=runtime,
            answer_source_ids=result.output.answer_source_ids,
        )
        _validate_requested_output_context(request=request, context=context)
        _apply_requested_output_fallback(request=request, context=context, runtime=runtime)
    except DocumentAgentServiceError as exc:
        no_hit_response = _build_exact_search_miss_response(
            request=request,
            task_id=task_id,
            selected_documents=selected_documents,
            mode=mode,
            runtime=runtime,
            tool_traces=result.tool_traces,
            turn_count=len(result.turn_traces),
            output_format_repair_count=_output_repair_count(result.turn_traces),
            started_at=started_at,
        )
        if no_hit_response is not None:
            return no_hit_response
        brief_fallback_response = _build_conservative_brief_fallback_response(
            request=request,
            task_id=task_id,
            selected_documents=selected_documents,
            mode=mode,
            runtime=runtime,
            stop_reason="model_output_invalid",
            tool_traces=result.tool_traces,
            turn_count=len(result.turn_traces),
            output_format_repair_count=_output_repair_count(result.turn_traces),
            started_at=started_at,
        )
        if brief_fallback_response is not None:
            return brief_fallback_response
        outline_fallback_response = _build_conservative_outline_fallback_response(
            request=request,
            task_id=task_id,
            selected_documents=selected_documents,
            mode=mode,
            runtime=runtime,
            stop_reason="model_output_invalid",
            tool_traces=result.tool_traces,
            turn_count=len(result.turn_traces),
            output_format_repair_count=_output_repair_count(result.turn_traces),
            started_at=started_at,
        )
        if outline_fallback_response is not None:
            return outline_fallback_response
        requirements_fallback_response = _build_conservative_requirements_fallback_response(
            request=request,
            task_id=task_id,
            selected_documents=selected_documents,
            mode=mode,
            runtime=runtime,
            stop_reason="model_output_invalid",
            tool_traces=result.tool_traces,
            turn_count=len(result.turn_traces),
            output_format_repair_count=_output_repair_count(result.turn_traces),
            started_at=started_at,
        )
        if requirements_fallback_response is not None:
            return requirements_fallback_response
        context = DocumentContext(
            documents=selected_documents,
            warnings=runtime.warnings,
            missing_context=[str(exc)],
            confidence="low",
        )
        return _persist_document_result(
            task_id=task_id,
            request=request,
            selected_documents=selected_documents,
            mode=mode,
            status="failed",
            stop_reason="model_output_invalid",
            reply=f"文档助手没有生成可验证的来源引用：{exc}",
            context=context,
            tool_traces=result.tool_traces,
            turn_count=len(result.turn_traces),
            output_format_repair_count=_output_repair_count(result.turn_traces),
            started_at=started_at,
        )

    return _persist_document_result(
        task_id=task_id,
        request=request,
        selected_documents=selected_documents,
        mode=mode,
        status="completed",
        stop_reason="completed",
        reply=result.output.answer,
        context=context,
        tool_traces=result.tool_traces,
        turn_count=len(result.turn_traces),
        output_format_repair_count=_output_repair_count(result.turn_traces),
        started_at=started_at,
    )


def get_document_agent_result(task_id: str) -> DocumentAgentRunResponse | None:
    """从已持久化的正式任务恢复文档助手结果，供异步页面收取终态。"""

    run = get_workflow_run(task_id)
    if run is None:
        return None

    final_step = next((step for step in reversed(run.steps) if step.step_id == "document_analysis"), None)
    if final_step is None:
        return None
    output = final_step.output
    context_payload = output.get("document_context")
    if not isinstance(context_payload, dict):
        return None
    try:
        context = DocumentContext.model_validate(context_payload)
    except Exception:
        return None

    status = str(output.get("agent_status") or "")
    allowed_statuses = {
        "completed",
        "needs_clarification",
        "insufficient_context",
        "failed",
        "max_turns_exceeded",
        "budget_exhausted",
    }
    if status not in allowed_statuses:
        status = "completed" if run.status == "completed" else "failed"
    mode = "llm" if output.get("model_mode") == "llm" else "mock"
    reply = str(output.get("reply") or run.summary)
    return DocumentAgentRunResponse(
        task_id=task_id,
        mode=mode,
        status=status,  # type: ignore[arg-type]
        stop_reason=str(output.get("stop_reason") or status),
        reply=reply,
        document_context=context,
        workflow_run=run,
    )


def get_document_draft_parent_diff(*, task_id: str) -> DocumentDraftVersionDiffResponse:
    """只读比较一个草稿快照和它的直接父版本。

    版本链的对比从 SQLite 中已验证的 Pydantic 结果读取，不重新请求模型、不读取 workspace、
    不创建任务或文件。只比较直接父版本可让用户看清“这一次派生到底变了什么”，避免把整条
    历史链压成难以理解的大型文本差异。
    """

    current_result = get_document_agent_result(task_id)
    if current_result is None:
        raise DocumentDraftVersionDiffNotFoundError("未找到需要比较的文档草稿任务。")
    if current_result.status != "completed":
        raise DocumentAgentServiceError("文档草稿任务尚未完成，暂时不能比较版本。")

    current_context = current_result.document_context
    current_version = current_context.draft_version
    if not current_context.draft_title or not current_context.draft_sections:
        raise DocumentDraftVersionDiffNotFoundError("当前任务不是可比较的 Markdown 草稿快照。")
    if current_version is None or not current_version.parent_task_id:
        raise DocumentAgentServiceError("当前草稿没有直接父版本，无法建立版本差异。")

    parent_result = get_document_agent_result(current_version.parent_task_id)
    if parent_result is None:
        raise DocumentDraftVersionDiffNotFoundError("未找到当前草稿的直接父版本快照。")
    if parent_result.status != "completed":
        raise DocumentAgentServiceError("直接父版本尚未完成，暂时不能比较版本。")

    parent_context = parent_result.document_context
    if not parent_context.draft_title or not parent_context.draft_sections:
        raise DocumentDraftVersionDiffNotFoundError("直接父版本不是可比较的 Markdown 草稿快照。")

    parent_by_id = {section.id: section for section in parent_context.draft_sections}
    current_by_id = {section.id: section for section in current_context.draft_sections}
    sections: list[DocumentDraftVersionDiffSection] = []
    for current_section in current_context.draft_sections:
        parent_section = parent_by_id.pop(current_section.id, None)
        if parent_section is None:
            sections.append(
                DocumentDraftVersionDiffSection(
                    id=current_section.id,
                    heading=current_section.heading,
                    change_kind="added",
                    current_body=current_section.body,
                )
            )
            continue
        sections.append(
            DocumentDraftVersionDiffSection(
                id=current_section.id,
                heading=current_section.heading,
                change_kind=(
                    "unchanged"
                    if parent_section.heading == current_section.heading
                    and parent_section.body == current_section.body
                    else "modified"
                ),
                parent_body=parent_section.body,
                current_body=current_section.body,
            )
        )
    # 正常派生版本会保留章节 ID；仍把父版本独有章节放到列表末尾，避免未来章节删除功能
    # 出现后差异视图悄悄遗漏内容。
    for parent_section in parent_context.draft_sections:
        if parent_section.id in current_by_id:
            continue
        sections.append(
            DocumentDraftVersionDiffSection(
                id=parent_section.id,
                heading=parent_section.heading,
                change_kind="removed",
                parent_body=parent_section.body,
            )
        )

    changed_count = sum(section.change_kind != "unchanged" for section in sections)
    title_changed = parent_context.draft_title != current_context.draft_title
    summary = (
        f"与直接父版本相比，标题{'已修改' if title_changed else '未修改'}，"
        f"{len(sections)} 个章节中有 {changed_count} 个发生变化。"
    )
    return DocumentDraftVersionDiffResponse(
        task_id=task_id,
        parent_task_id=current_version.parent_task_id,
        root_task_id=current_version.root_task_id,
        parent_title=parent_context.draft_title,
        current_title=current_context.draft_title,
        title_changed=title_changed,
        summary=summary,
        sections=sections,
        warnings=[
            "这是只读版本对比：未调用模型、Tool、工作区读取或文件写入。",
            "对比对象是当前快照与直接父版本；历史任务和已保存 Markdown 均不会改变。",
        ],
    )


def build_document_draft_section_request(
    *,
    source_task_id: str,
    request: DocumentDraftSectionRequest,
) -> DocumentAgentRunRequest:
    """从已完成草稿恢复一个受控章节种子，构造新的派生任务请求。

    章节创作不是把客户端提交的文本直接交给模型续写。Runtime 先从原任务恢复已通过来源
    Guardrail 的章节和文档范围，再让新的 Agent 重新读取原材料。这样派生预览不会把任意
    富文本、路径或未经校验的事实混入创作上下文，也不会修改原任务或已保存文件。
    """

    source_result = get_document_agent_result(source_task_id)
    if source_result is None:
        raise DocumentDraftSectionNotFoundError("未找到可用于分章节创作的原草稿任务。")
    if source_result.status != "completed":
        raise DocumentAgentServiceError("原草稿任务尚未完成，暂时不能基于它撰写章节。")

    source_context = source_result.document_context
    if not source_context.draft_title or not source_context.draft_sections:
        raise DocumentAgentServiceError("原任务不是带来源的 Markdown 草稿预览，无法选择章节。")
    section = next(
        (item for item in source_context.draft_sections if item.id == request.section_id),
        None,
    )
    if section is None:
        raise DocumentDraftSectionNotFoundError("原草稿中没有找到所选章节，可能已过期，请重新打开结果。")
    if not source_context.documents:
        raise DocumentAgentServiceError("原草稿没有保留可读取的材料范围，无法安全生成章节预览。")

    instruction = request.instruction.strip()
    if not instruction:
        raise DocumentAgentServiceError("请说明希望如何调整本章后再继续。")
    root_task_id = _document_version_root_task_id(source_context, source_task_id)
    seed = DocumentDraftSectionSeed(
        source_task_id=source_task_id,
        root_task_id=root_task_id,
        section_id=section.id,
        heading=section.heading,
        current_body=section.body,
        instruction=instruction,
    )
    return DocumentAgentRunRequest(
        task_goal=(
            f"基于已验证草稿“{source_context.draft_title}”中的章节“{section.heading}”，"
            "生成一份可审阅的单章节 Markdown 预览。"
        ),
        document_refs=list(source_context.documents),
        output_mode="section_draft",
        constraints=[
            "只生成当前选择章节，不覆盖原草稿或任何已保存文件。",
            "只能使用重新读取的材料事实，并为正文保留本轮来源。",
        ],
        section_draft=seed,
    )


def build_document_draft_review_request(
    *,
    source_task_id: str,
    request: DocumentDraftReviewRequest,
) -> DocumentAgentRunRequest:
    """从已完成草稿恢复受控快照，派生一次不修改正文的事实核验。"""

    source_result = get_document_agent_result(source_task_id)
    if source_result is None:
        raise DocumentDraftSectionNotFoundError("未找到可用于事实核验的草稿任务。")
    if source_result.status != "completed":
        raise DocumentAgentServiceError("原草稿任务尚未完成，暂时不能进行事实核验。")
    source_context = source_result.document_context
    if not source_context.draft_title or not source_context.draft_sections or not source_context.documents:
        raise DocumentAgentServiceError("原任务不是带来源的 Markdown 草稿预览，无法进行事实核验。")

    root_task_id = _document_version_root_task_id(source_context, source_task_id)
    seed = DocumentDraftReviewSeed(
        source_task_id=source_task_id,
        root_task_id=root_task_id,
        draft_title=source_context.draft_title,
        draft_sections=source_context.draft_sections,
        focus=request.focus.strip(),
        # 手动修订不能继续继承原草稿的“已验证”状态；本次核验会重新读取材料后，才决定
        # 该预览是否能解锁保存。普通草稿核验仍只生成说明，不改变其既有可交付状态。
        requires_reverification=source_context.draft_verification_state != "verified",
    )
    return DocumentAgentRunRequest(
        task_goal=(
            f"重新核验用户手动修订草稿“{source_context.draft_title}”中的事实是否可由原材料支持。"
            if seed.requires_reverification
            else f"核验已验证草稿“{source_context.draft_title}”中的事实是否可由原材料支持。"
        ),
        document_refs=list(source_context.documents),
        output_mode="draft_review",
        constraints=[
            "只生成带来源的核验说明，不改写草稿正文、不保存或导出文件。",
            "材料不足时列为待确认问题，不把推测判为错误。",
            "手动修订草稿必须在本轮没有待确认事实后才能解锁保存。"
            if seed.requires_reverification
            else "保留原草稿的可交付状态，不把核验说明误作正文修改。",
        ],
        draft_review=seed,
    )


def build_document_draft_section_review_request(
    *,
    source_task_id: str,
    request: DocumentDraftSectionReviewRequest,
) -> DocumentAgentRunRequest:
    """从已完成草稿恢复一个章节，构造只读的审校建议任务。

    这是“建议怎么改”，不是“替用户改掉”：章节正文和完整草稿快照均从原任务恢复，
    客户端只可选择稳定章节 ID 与填写关注点。派生任务会重新读取原 workspace 材料，
    因而建议的事实依据不会借用旧任务中过期或未经本轮验证的来源。
    """

    source_result = get_document_agent_result(source_task_id)
    if source_result is None:
        raise DocumentDraftSectionNotFoundError("未找到可用于本章审校的原草稿任务。")
    if source_result.status != "completed":
        raise DocumentAgentServiceError("原草稿任务尚未完成，暂时不能进行本章审校。")
    source_context = source_result.document_context
    if not source_context.draft_title or not source_context.draft_sections:
        raise DocumentAgentServiceError("原任务没有可审校的已验证 Markdown 草稿。")
    if not source_context.documents:
        raise DocumentAgentServiceError("原草稿没有保留受控材料范围，暂时不能安全审校。")

    section = next(
        (item for item in source_context.draft_sections if item.id == request.section_id),
        None,
    )
    if section is None:
        raise DocumentDraftSectionNotFoundError("原草稿中没有找到所选章节，可能已过期，请重新打开结果。")

    root_task_id = _document_version_root_task_id(source_context, source_task_id)
    seed = DocumentDraftSectionReviewSeed(
        source_task_id=source_task_id,
        root_task_id=root_task_id,
        draft_title=source_context.draft_title,
        draft_sections=list(source_context.draft_sections),
        section_id=section.id,
        heading=section.heading,
        current_body=section.body,
        focus=request.focus.strip(),
    )
    return DocumentAgentRunRequest(
        task_goal=f"审校已验证草稿“{source_context.draft_title}”中的章节“{section.heading}”。",
        document_refs=list(source_context.documents),
        output_mode="section_review",
        constraints=[
            "只返回问题、原文片段和候选建议，不改写原草稿、不保存或导出文件。",
            "原文片段必须来自当前选择章节；建议只能依据重新读取的材料事实。",
        ],
        section_review=seed,
    )


def _load_section_revision_source(
    *,
    source_review_task_id: str,
    suggestion_id: str,
) -> tuple[DocumentAgentRunResponse, DocumentDraftSection, DocumentRevisionSuggestion]:
    """从已完成的本章审校任务恢复一条可安全应用到预览的候选建议。

    修订预览不能接收客户端传来的原文或候选正文：这会绕过审校证据与章节身份。这里把
    review task、章节 ID 和 suggestion ID 重新绑定，并要求候选原文在章节中恰好出现一次，
    从而避免相同句子重复出现时被悄悄替换到错误位置。
    """

    review_result, section, suggestions = _load_section_revision_sources(
        source_review_task_id=source_review_task_id,
        suggestion_ids=[suggestion_id],
    )
    return review_result, section, suggestions[0]


def _document_version_root_task_id(context: DocumentContext, fallback_task_id: str) -> str:
    """优先继承既有根草稿；旧任务没有版本字段时以其自身任务 ID 兼容。"""

    version = context.draft_version
    if version is not None and version.root_task_id.strip():
        return version.root_task_id
    return fallback_task_id


def _build_document_draft_version_info(
    *,
    request: DocumentAgentRunRequest,
    task_id: str,
) -> DocumentDraftVersionInfo | None:
    """为可保存的草稿快照生成版本链身份，不把版本管理变成隐式覆盖。"""

    if request.output_mode == "draft":
        return DocumentDraftVersionInfo(
            version_id=task_id,
            root_task_id=task_id,
            kind="base_draft",
            label="草稿初版",
            change_summary="根据已选择材料生成的首个待审阅 Markdown 草稿。",
        )

    if request.output_mode == "section_draft" and request.section_draft is not None:
        seed = request.section_draft
        return DocumentDraftVersionInfo(
            version_id=task_id,
            root_task_id=seed.root_task_id,
            parent_task_id=seed.source_task_id,
            kind="section_preview",
            label="分章节创作预览",
            change_summary=f"基于原草稿的章节“{seed.heading}”生成独立预览，未修改原草稿。",
        )

    if request.output_mode == "draft_review" and request.draft_review is not None:
        seed = request.draft_review
        return DocumentDraftVersionInfo(
            version_id=task_id,
            root_task_id=seed.root_task_id,
            parent_task_id=seed.source_task_id,
            kind="fact_review",
            label="草稿事实核验",
            change_summary="重新读取材料进行只读事实核验，正文保持原样。",
        )

    if request.output_mode == "section_review" and request.section_review is not None:
        seed = request.section_review
        return DocumentDraftVersionInfo(
            version_id=task_id,
            root_task_id=seed.root_task_id,
            parent_task_id=seed.source_task_id,
            kind="section_review",
            label="本章审校",
            change_summary=f"对章节“{seed.heading}”给出只读候选建议，正文保持原样。",
        )

    if request.output_mode == "section_revision" and request.section_revision is not None:
        seed = request.section_revision
        return DocumentDraftVersionInfo(
            version_id=task_id,
            root_task_id=seed.root_task_id,
            parent_task_id=seed.source_review_task_id,
            kind="revision_preview",
            label="单建议修订预览",
            change_summary="精确应用一条已审校建议生成独立预览，未覆盖旧草稿或文件。",
        )

    if request.output_mode == "section_revision_batch" and request.section_revision_batch is not None:
        seed = request.section_revision_batch
        return DocumentDraftVersionInfo(
            version_id=task_id,
            root_task_id=seed.root_task_id,
            parent_task_id=seed.source_review_task_id,
            kind="revision_batch_preview",
            label="多建议合并预览",
            change_summary=(
                f"安全合并 {len(seed.suggestion_ids)} 条不重叠审校建议，未覆盖旧草稿或文件。"
            ),
        )

    if request.output_mode == "section_manual_revision" and request.section_manual_revision is not None:
        seed = request.section_manual_revision
        return DocumentDraftVersionInfo(
            version_id=task_id,
            root_task_id=seed.root_task_id,
            parent_task_id=seed.source_task_id,
            kind="manual_revision_pending_review",
            label="手动修订待核验",
            change_summary=(
                f"用户手动修改章节“{seed.heading}”生成独立预览；必须重新核验来源后才能保存。"
            ),
        )

    if request.output_mode == "draft_restore" and request.draft_restore is not None:
        seed = request.draft_restore
        return DocumentDraftVersionInfo(
            version_id=task_id,
            root_task_id=seed.root_task_id,
            parent_task_id=seed.source_task_id,
            kind="restored_preview",
            label="恢复预览",
            change_summary="从已完成历史草稿快照建立新的独立预览，未覆盖旧任务或文件。",
        )
    if request.output_mode == "draft_template" and request.draft_template is not None:
        seed = request.draft_template
        spec = _document_template_spec(seed.template_id)
        return DocumentDraftVersionInfo(
            version_id=task_id,
            root_task_id=seed.root_task_id,
            parent_task_id=seed.source_task_id,
            kind="template_preview",
            label=f"{spec.name}交付预览",
            change_summary=(
                f"将已核验草稿按“{spec.name}”固定结构重组；未调用模型、Tool 或文件写入。"
            ),
        )
    if request.output_mode == "draft_merge" and request.draft_merge is not None:
        seed = request.draft_merge
        return DocumentDraftVersionInfo(
            version_id=task_id,
            root_task_id=seed.root_task_id,
            # 合并版本保留“当前详情”作为直接父版本；另一分支与共同祖先记录在 merge_preview
            # 元数据中，避免把树状版本链伪装成 SQLite 并不支持的多父提交图。
            parent_task_id=seed.primary_task_id,
            kind="merge_preview",
            label="章节合并预览",
            change_summary=(
                f"与版本 {seed.secondary_task_id[-12:]} 建立同根三方合并预览；"
                f"已显式处理 {len(seed.resolutions)} 项冲突，未覆盖旧任务或文件。"
            ),
        )
    return None


def _load_document_draft_restore_source(
    *,
    source_task_id: str,
    source_version_id: str,
) -> DocumentAgentRunResponse:
    """从历史任务恢复可保存草稿，并核对版本身份防止错误指向其他快照。"""

    source_result = get_document_agent_result(source_task_id)
    if source_result is None:
        raise DocumentDraftRestoreNotFoundError("未找到可恢复的历史文档草稿任务。")
    if source_result.status != "completed":
        raise DocumentAgentServiceError("历史草稿任务尚未完成，暂时不能建立恢复预览。")

    source_context = source_result.document_context
    if not source_context.draft_title or not source_context.draft_sections:
        raise DocumentDraftRestoreNotFoundError("当前历史任务不是可恢复的 Markdown 草稿快照。")

    actual_version_id = (
        source_context.draft_version.version_id
        if source_context.draft_version is not None
        else source_task_id
    )
    if actual_version_id != source_version_id:
        # 任务结果一经完成不可变；仍做这一层显式比对，避免未来版本链检索扩展后把一个旧 ID
        # 误绑定到另一份快照。
        raise DocumentAgentServiceError("历史草稿版本身份不一致，已停止建立恢复预览。")
    return source_result


def build_document_draft_restore_request(*, source_task_id: str) -> DocumentAgentRunRequest:
    """将一个已完成历史草稿转为零模型、零文件写入的独立恢复预览任务。"""

    # 构造时立即确认任务可恢复，使 API 在入队前给用户明确 404/400，而不是创建一条必然失败
    # 的后台任务。真正运行时还会再取一次相同快照，保证异步边界仍以服务端状态为准。
    source_result = get_document_agent_result(source_task_id)
    if source_result is None:
        raise DocumentDraftRestoreNotFoundError("未找到可恢复的历史文档草稿任务。")
    if source_result.status != "completed":
        raise DocumentAgentServiceError("历史草稿任务尚未完成，暂时不能建立恢复预览。")
    source_context = source_result.document_context
    if not source_context.draft_title or not source_context.draft_sections:
        raise DocumentDraftRestoreNotFoundError("当前历史任务不是可恢复的 Markdown 草稿快照。")

    source_version_id = (
        source_context.draft_version.version_id
        if source_context.draft_version is not None
        else source_task_id
    )
    seed = DocumentDraftRestoreSeed(
        source_task_id=source_task_id,
        root_task_id=_document_version_root_task_id(source_context, source_task_id),
        source_version_id=source_version_id,
    )
    return DocumentAgentRunRequest(
        task_goal=f"从历史草稿“{source_context.draft_title}”建立新的独立恢复预览。",
        document_refs=list(source_context.documents),
        output_mode="draft_restore",
        constraints=[
            "只恢复已完成任务中的带来源草稿快照，不调用模型、不读取材料。",
            "不修改原任务、不覆盖已保存文件；如需写入，仍必须另走保存确认。",
        ],
        draft_restore=seed,
    )


async def _run_document_draft_restore_preview(
    *,
    request: DocumentAgentRunRequest,
    task_id: str,
    started_at: datetime,
    progress_callback: DocumentProgressCallback | None,
) -> DocumentAgentRunResponse:
    """从历史快照建立一个新的草稿版本，不调用模型、Tool 或文件写入。"""

    seed = request.draft_restore
    if seed is None:
        raise DocumentAgentServiceError("恢复预览缺少已验证的历史草稿身份。")

    await _emit_document_progress(progress_callback, "scope_checking", "正在校验历史草稿快照与版本身份。")
    source_result = _load_document_draft_restore_source(
        source_task_id=seed.source_task_id,
        source_version_id=seed.source_version_id,
    )
    source_context = source_result.document_context
    source_version_label = (
        source_context.draft_version.label
        if source_context.draft_version is not None
        else "历史草稿快照"
    )

    await _emit_document_progress(progress_callback, "scope_ready", "历史快照已校验，正在建立新的独立恢复预览。")
    await _emit_document_progress(progress_callback, "version_restoring", "正在复制已验证正文和来源；旧任务与文件均不会改动。")

    # 只复制 Pydantic 已验证的结构化快照。不要保留旧任务的审校建议、前后差异或运行警告，
    # 否则用户会把“恢复后的草稿”误解成仍在应用旧建议的活跃编辑界面。
    context = DocumentContext(
        documents=list(source_context.documents),
        summary=(
            f"已从“{source_version_label}”建立新的独立恢复预览。正文与来源保持历史快照，"
            "未调用模型、未读取材料，也未修改旧任务或文件。"
        ),
        draft_title=source_context.draft_title,
        draft_sections=[item.model_copy(deep=True) for item in source_context.draft_sections],
        sources=[item.model_copy(deep=True) for item in source_context.sources],
        warnings=[
            "这是恢复后的独立预览：原历史任务、原草稿与已保存 Markdown 文件均未修改。",
            "如需交付，请使用“保存 Markdown”命名并另存为新的版本文件。",
        ],
        confidence=source_context.confidence,
    )
    reply = (
        f"已从历史版本“{source_version_label}”建立新的独立恢复预览；"
        "旧草稿和文件未改动，确认后可另存为新的 Markdown 版本。"
    )
    return _persist_document_result(
        task_id=task_id,
        request=request,
        selected_documents=list(source_context.documents),
        mode="deterministic",
        status="completed",
        stop_reason="completed",
        reply=reply,
        context=context,
        tool_traces=(),
        turn_count=0,
        started_at=started_at,
    )


def _document_template_spec(template_id: str) -> _DocumentTemplateSpec:
    """解析内置模板，集中拒绝未知 ID，避免任务历史出现无法解释的交付类型。"""

    spec = _DOCUMENT_TEMPLATE_SPECS.get(template_id)
    if spec is None:
        raise DocumentAgentServiceError("所选文档交付模板不存在或当前版本不可用。")
    return spec


def _load_document_draft_template_source(
    *,
    source_task_id: str,
    source_version_id: str,
) -> DocumentAgentRunResponse:
    """恢复可用于模板交付的已核验草稿，并再次核对其版本身份。"""

    source_result = get_document_agent_result(source_task_id)
    if source_result is None:
        raise DocumentDraftRestoreNotFoundError("未找到可用于模板交付的文档草稿任务。")
    if source_result.status != "completed":
        raise DocumentAgentServiceError("原草稿任务尚未完成，暂时不能建立交付预览。")

    source_context = source_result.document_context
    if not source_context.draft_title or not source_context.draft_sections:
        raise DocumentAgentServiceError("原任务不是带来源的 Markdown 草稿预览，无法套用交付模板。")
    if source_context.draft_verification_state != "verified":
        raise DocumentAgentServiceError("请先完成草稿事实核验，再建立模板化交付预览。")

    actual_version_id = (
        source_context.draft_version.version_id
        if source_context.draft_version is not None
        else source_task_id
    )
    if actual_version_id != source_version_id:
        raise DocumentAgentServiceError("原草稿版本身份已变化，已停止建立模板化交付预览。")
    return source_result


def build_document_draft_template_preview_request(
    *,
    source_task_id: str,
    request: DocumentDraftTemplatePreviewRequest,
) -> DocumentAgentRunRequest:
    """把一个已核验草稿绑定到固定交付模板，不接受正文或路径输入。"""

    spec = _document_template_spec(request.template_id)
    source_result = get_document_agent_result(source_task_id)
    if source_result is None:
        raise DocumentDraftRestoreNotFoundError("未找到可用于模板交付的文档草稿任务。")
    if source_result.status != "completed":
        raise DocumentAgentServiceError("原草稿任务尚未完成，暂时不能建立交付预览。")
    source_context = source_result.document_context
    if not source_context.draft_title or not source_context.draft_sections:
        raise DocumentAgentServiceError("原任务不是带来源的 Markdown 草稿预览，无法套用交付模板。")
    if source_context.draft_verification_state != "verified":
        raise DocumentAgentServiceError("请先完成草稿事实核验，再建立模板化交付预览。")

    source_version_id = (
        source_context.draft_version.version_id
        if source_context.draft_version is not None
        else source_task_id
    )
    seed = DocumentDraftTemplateSeed(
        source_task_id=source_task_id,
        root_task_id=_document_version_root_task_id(source_context, source_task_id),
        source_version_id=source_version_id,
        template_id=spec.template_id,  # type: ignore[arg-type]
    )
    return DocumentAgentRunRequest(
        task_goal=f"将已核验草稿“{source_context.draft_title}”整理为“{spec.name}”交付预览。",
        document_refs=list(source_context.documents),
        output_mode="draft_template",
        constraints=[
            "只重组已验证的草稿章节与来源，不调用模型、不读取材料、不新增事实。",
            "模板缺少的章节必须明确标记为待补充，不得用通用文字伪装为已完成内容。",
            "不修改原草稿、历史任务或任何已保存文件；如需交付仍必须由用户确认保存。",
        ],
        draft_template=seed,
    )


def _template_section_key(section: DocumentDraftSection, spec: _DocumentTemplateSpec) -> str:
    """仅用可见标题和短正文做保守归类，无法判断时保留为补充材料。"""

    searchable = f"{section.heading}\n{section.body[:240]}".casefold()
    for template_section in spec.sections:
        if any(keyword.casefold() in searchable for keyword in template_section.keywords):
            return template_section.key
    return "supporting_material"


def _template_sources(sections: list[DocumentDraftSection]) -> list[DocumentSourceRef]:
    """按真实定位去重模板结果的来源，避免从不同任务复制的 source_id 互相误判。"""

    sources: list[DocumentSourceRef] = []
    seen: set[tuple[str, int, int, str, str]] = set()
    for section in sections:
        for source in section.source_refs:
            key = (
                source.relative_path,
                source.start_line,
                source.end_line,
                source.source_locator,
                source.excerpt,
            )
            if key in seen:
                continue
            seen.add(key)
            sources.append(source.model_copy(deep=True))
    return sources


def _compose_document_template(
    *,
    source_context: DocumentContext,
    spec: _DocumentTemplateSpec,
) -> tuple[str, list[DocumentDraftSection], list[str]]:
    """用固定模板重排已验证章节，绝不从标题或常识补造正文。"""

    grouped: dict[str, list[DocumentDraftSection]] = {section.key: [] for section in spec.sections}
    supporting_sections: list[DocumentDraftSection] = []
    for source_section in source_context.draft_sections:
        key = _template_section_key(source_section, spec)
        if key in grouped:
            grouped[key].append(source_section)
        else:
            supporting_sections.append(source_section)

    rendered_sections: list[DocumentDraftSection] = []
    missing_sections: list[str] = []
    output_index = 1
    for template_section in spec.sections:
        matched = grouped[template_section.key]
        if not matched:
            missing_sections.append(template_section.title)
            continue
        for item_index, source_section in enumerate(matched, start=1):
            heading = (
                template_section.title
                if len(matched) == 1
                else f"{template_section.title} · {source_section.heading}"
            )
            rendered_sections.append(
                DocumentDraftSection(
                    id=f"template_{spec.template_id}_{output_index:02d}",
                    heading=heading[:160],
                    body=source_section.body,
                    source_refs=[source.model_copy(deep=True) for source in source_section.source_refs],
                    confidence=source_section.confidence,
                )
            )
            output_index += 1

    # 未能被当前模板保守归类的原章节不会消失，统一放在末尾供客户继续判断或手动修订。
    for source_section in supporting_sections:
        rendered_sections.append(
            DocumentDraftSection(
                id=f"template_{spec.template_id}_{output_index:02d}",
                heading=f"补充材料 · {source_section.heading}"[:160],
                body=source_section.body,
                source_refs=[source.model_copy(deep=True) for source in source_section.source_refs],
                confidence=source_section.confidence,
            )
        )
        output_index += 1

    # 源草稿最多八章；仅重排不新增正文，因而这里仍落在已有草稿/保存协议的章节上限内。
    return (
        f"{spec.title_prefix}：{source_context.draft_title}"[:240],
        rendered_sections,
        missing_sections,
    )


async def _run_document_draft_template_preview(
    *,
    request: DocumentAgentRunRequest,
    task_id: str,
    started_at: datetime,
    progress_callback: DocumentProgressCallback | None,
) -> DocumentAgentRunResponse:
    """生成零模型、零 Tool、零写入的结构化交付预览。"""

    seed = request.draft_template
    if seed is None:
        raise DocumentAgentServiceError("模板化交付预览缺少已验证的草稿身份。")
    spec = _document_template_spec(seed.template_id)

    await _emit_document_progress(progress_callback, "scope_checking", "正在校验已核验草稿与版本身份。")
    source_result = _load_document_draft_template_source(
        source_task_id=seed.source_task_id,
        source_version_id=seed.source_version_id,
    )
    source_context = source_result.document_context
    await _emit_document_progress(progress_callback, "scope_ready", "原草稿快照已校验，正在按固定模板组织交付结构。")
    await _emit_document_progress(progress_callback, "template_composing", "正在重组已有章节与来源；不会补写未知事实。")

    draft_title, draft_sections, missing_sections = _compose_document_template(
        source_context=source_context,
        spec=spec,
    )
    if not draft_sections:
        raise DocumentAgentServiceError("原草稿没有可组织的带来源章节，无法建立交付预览。")
    source_version_label = (
        source_context.draft_version.label
        if source_context.draft_version is not None
        else "历史草稿快照"
    )
    context = DocumentContext(
        documents=list(source_context.documents),
        summary=(
            f"已基于“{source_version_label}”建立“{spec.name}”交付预览。"
            "本次仅重组已验证章节，不调用模型、不读取材料，也不新增事实。"
        ),
        draft_title=draft_title,
        draft_sections=draft_sections,
        template_preview=DocumentDraftTemplatePreview(
            source_task_id=seed.source_task_id,
            source_version_id=seed.source_version_id,
            template_id=seed.template_id,
            template_name=spec.name,
            missing_sections=missing_sections,
        ),
        open_questions=[item.model_copy(deep=True) for item in source_context.open_questions],
        sources=_template_sources(draft_sections),
        warnings=[
            "这是独立模板化交付预览：原草稿、历史任务和已保存 Markdown 文件均未修改。",
            "模板只重组现有已验证章节；未匹配的模板章节不会被自动补写。",
            *(
                [f"交付前建议补充：{'、'.join(missing_sections)}。"]
                if missing_sections
                else []
            ),
            "确认内容后可使用“保存 Markdown”另存为新的交付版本。",
        ],
        draft_verification_state="verified",
        confidence=source_context.confidence,
    )
    reply = (
        f"已建立“{spec.name}”交付预览，重组 {len(draft_sections)} 个已验证章节；"
        + (f"仍有 {len(missing_sections)} 个模板章节待补充。" if missing_sections else "模板章节已全部匹配。")
    )
    return _persist_document_result(
        task_id=task_id,
        request=request,
        selected_documents=list(source_context.documents),
        mode="deterministic",
        status="completed",
        stop_reason="completed",
        reply=reply,
        context=context,
        tool_traces=(),
        turn_count=0,
        started_at=started_at,
    )


# 只有“完整文档快照”可以作为三方合并分支。单章节创作、审校和模板重排会改变章节语义或
# 身份，直接参与基于稳定 section_id 的合并容易制造重复章节；这些版本仍可回看或另存。
_MERGEABLE_DRAFT_VERSION_KINDS = frozenset(
    {
        "base_draft",
        "revision_preview",
        "revision_batch_preview",
        "manual_revision_pending_review",
        "restored_preview",
        "merge_preview",
    }
)


def _document_draft_version_id(context: DocumentContext, fallback_task_id: str) -> str:
    """返回不可变任务快照对应的版本 ID，兼容版本链上线前的历史草稿。"""

    if context.draft_version is not None and context.draft_version.version_id:
        return context.draft_version.version_id
    return fallback_task_id


def _is_mergeable_draft_snapshot(result: DocumentAgentRunResponse) -> bool:
    """判断一个任务能否作为章节级合并分支，而不是靠 UI 菜单可见性猜测。"""

    context = result.document_context
    return (
        result.status == "completed"
        and bool(context.draft_title)
        and bool(context.draft_sections)
        and context.draft_verification_state == "verified"
        and context.draft_version is not None
        and context.draft_version.kind in _MERGEABLE_DRAFT_VERSION_KINDS
    )


def _load_document_draft_merge_source(
    *,
    task_id: str,
    source_role: str,
) -> DocumentAgentRunResponse:
    """从 SQLite 恢复一个可参与三方合并的已核验完整草稿。"""

    result = get_document_agent_result(task_id)
    if result is None:
        raise DocumentDraftMergeNotFoundError(f"未找到{source_role}文档草稿任务。")
    if result.status != "completed":
        raise DocumentAgentServiceError(f"{source_role}草稿任务尚未完成，暂时不能参与章节合并。")
    context = result.document_context
    if not context.draft_title or not context.draft_sections:
        raise DocumentDraftMergeNotFoundError(f"{source_role}任务不是可合并的完整 Markdown 草稿快照。")
    if context.draft_verification_state != "verified":
        raise DocumentAgentServiceError(f"请先完成{source_role}草稿的事实核验，再进行章节合并。")
    if context.draft_version is None or context.draft_version.kind not in _MERGEABLE_DRAFT_VERSION_KINDS:
        raise DocumentAgentServiceError(
            f"{source_role}草稿不是可合并的完整版本；单章节、审校和模板重排预览不能直接参与合并。"
        )
    return result


def _document_draft_ancestry(task_id: str) -> list[tuple[str, DocumentAgentRunResponse]]:
    """沿单父版本链恢复祖先快照，并检测异常环，避免合并逻辑被错误历史卡死。"""

    ancestry: list[tuple[str, DocumentAgentRunResponse]] = []
    seen: set[str] = set()
    current_task_id = task_id
    for _ in range(32):  # 当前 MVP 不允许无限派生版本；异常链必须明确中止。
        if not current_task_id or current_task_id in seen:
            raise DocumentAgentServiceError("文档版本链存在循环或无效父版本，已停止章节合并。")
        seen.add(current_task_id)
        current_result = get_document_agent_result(current_task_id)
        if current_result is None or current_result.status != "completed":
            raise DocumentDraftMergeNotFoundError("章节合并缺少可读取的历史版本快照。")
        current_context = current_result.document_context
        if not current_context.draft_title or not current_context.draft_sections:
            raise DocumentDraftMergeNotFoundError("章节合并的历史版本不是完整 Markdown 草稿快照。")
        ancestry.append((current_task_id, current_result))
        parent_task_id = (
            current_context.draft_version.parent_task_id
            if current_context.draft_version is not None
            else ""
        )
        if not parent_task_id:
            return ancestry
        current_task_id = parent_task_id
    raise DocumentAgentServiceError("文档版本链超过当前合并上限，请先从需要的历史草稿恢复为新预览。")


def _document_draft_common_ancestor(
    *,
    primary_task_id: str,
    secondary_task_id: str,
) -> tuple[str, DocumentAgentRunResponse]:
    """查找两条单父版本链距离最近的共同祖先，不假定当前版本互为父子。"""

    primary_chain = _document_draft_ancestry(primary_task_id)
    secondary_chain = _document_draft_ancestry(secondary_task_id)
    primary_distances = {task_id: index for index, (task_id, _) in enumerate(primary_chain)}
    candidates: list[tuple[int, int, str, DocumentAgentRunResponse]] = []
    for secondary_index, (task_id, result) in enumerate(secondary_chain):
        primary_index = primary_distances.get(task_id)
        if primary_index is not None:
            candidates.append((primary_index + secondary_index, secondary_index, task_id, result))
    if not candidates:
        raise DocumentDraftMergeNotFoundError("两个草稿版本没有可验证的共同祖先，不能安全合并。")
    _, _, task_id, result = min(candidates, key=lambda item: (item[0], item[1], item[2]))
    return task_id, result


def _document_draft_sections_equal(
    left: DocumentDraftSection | None,
    right: DocumentDraftSection | None,
) -> bool:
    """合并只比较稳定章节 ID 对应的标题/正文；来源随最终选中的已核验快照保留。"""

    if left is None or right is None:
        return left is right
    return left.heading == right.heading and left.body == right.body


def _document_draft_merge_conflict(
    *,
    section_id: str,
    base: DocumentDraftSection | None,
    primary: DocumentDraftSection | None,
    secondary: DocumentDraftSection | None,
) -> DocumentDraftMergeConflict:
    """把无法自动裁决的章节压成可展示、可选择而不携带文件路径的冲突记录。"""

    kind = (
        "addition"
        if base is None
        else "deletion"
        if primary is None or secondary is None
        else "content"
    )
    heading = (
        primary.heading
        if primary is not None
        else secondary.heading
        if secondary is not None
        else base.heading
        if base is not None
        else ""
    )
    return DocumentDraftMergeConflict(
        conflict_id=f"section:{section_id}",
        kind=kind,
        section_id=section_id,
        heading=heading,
        base_text=base.body if base is not None else "",
        primary_text=primary.body if primary is not None else "",
        secondary_text=secondary.body if secondary is not None else "",
    )


def _resolve_document_draft_merge_value(
    *,
    base: Any,
    primary: Any,
    secondary: Any,
    equal: Callable[[Any, Any], bool],
    conflict: DocumentDraftMergeConflict,
    resolutions: dict[str, str],
) -> tuple[bool, Any | None, DocumentDraftMergeConflict | None, bool]:
    """执行通用三方规则，并在需要时只接受用户对既有冲突的显式选择。"""

    if equal(primary, secondary):
        return True, primary, None, False
    if equal(primary, base):
        return True, secondary, None, False
    if equal(secondary, base):
        return True, primary, None, False

    choice = resolutions.get(conflict.conflict_id)
    if not choice:
        return False, None, conflict, False
    selected = {"primary": primary, "secondary": secondary, "base": base}.get(choice)
    if choice not in {"primary", "secondary", "base"}:
        raise DocumentAgentServiceError("章节合并包含未知冲突选择，已停止建立预览。")
    if choice == "base" and base is None:
        raise DocumentAgentServiceError("该新增章节没有共同祖先正文，不能选择“共同祖先”。")
    return True, selected, None, True


def _compose_document_draft_merge(
    *,
    primary_context: DocumentContext,
    secondary_context: DocumentContext,
    base_context: DocumentContext,
    resolutions: dict[str, str],
) -> tuple[str | None, list[DocumentDraftSection], list[DocumentDraftMergeConflict], int]:
    """根据共同祖先合并标题和章节；未解决冲突绝不悄悄落进结果正文。"""

    title_conflict = DocumentDraftMergeConflict(
        conflict_id="title",
        kind="title",
        base_text=base_context.draft_title,
        primary_text=primary_context.draft_title,
        secondary_text=secondary_context.draft_title,
    )
    title_resolved, merged_title, unresolved_title, title_was_manual = _resolve_document_draft_merge_value(
        base=base_context.draft_title,
        primary=primary_context.draft_title,
        secondary=secondary_context.draft_title,
        equal=lambda left, right: left == right,
        conflict=title_conflict,
        resolutions=resolutions,
    )
    conflicts: list[DocumentDraftMergeConflict] = []
    if not title_resolved and unresolved_title is not None:
        conflicts.append(unresolved_title)

    base_by_id = {section.id: section for section in base_context.draft_sections}
    primary_by_id = {section.id: section for section in primary_context.draft_sections}
    secondary_by_id = {section.id: section for section in secondary_context.draft_sections}
    ordered_ids = [section.id for section in base_context.draft_sections]
    ordered_ids.extend(section.id for section in primary_context.draft_sections if section.id not in base_by_id)
    ordered_ids.extend(
        section.id
        for section in secondary_context.draft_sections
        if section.id not in base_by_id and section.id not in primary_by_id
    )
    if len(ordered_ids) > 8:
        raise DocumentAgentServiceError("两个版本合计超过当前 8 个章节上限，请先拆分或整理草稿后再合并。")

    merged_sections: list[DocumentDraftSection] = []
    manual_resolution_count = 1 if title_was_manual else 0
    for section_id in ordered_ids:
        base_section = base_by_id.get(section_id)
        primary_section = primary_by_id.get(section_id)
        secondary_section = secondary_by_id.get(section_id)
        conflict = _document_draft_merge_conflict(
            section_id=section_id,
            base=base_section,
            primary=primary_section,
            secondary=secondary_section,
        )
        resolved, selected, unresolved, used_manual_choice = _resolve_document_draft_merge_value(
            base=base_section,
            primary=primary_section,
            secondary=secondary_section,
            equal=_document_draft_sections_equal,
            conflict=conflict,
            resolutions=resolutions,
        )
        if not resolved:
            if unresolved is not None:
                conflicts.append(unresolved)
            continue
        if used_manual_choice:
            manual_resolution_count += 1
        if selected is not None:
            merged_sections.append(selected.model_copy(deep=True))

    return (
        str(merged_title) if merged_title is not None else None,
        merged_sections,
        conflicts,
        manual_resolution_count,
    )


def _build_document_draft_merge_plan(
    *,
    primary_task_id: str,
    secondary_task_id: str,
) -> _DocumentDraftMergePlan:
    """重新恢复两端与共同祖先，得到无副作用的三方合并计划。"""

    if primary_task_id == secondary_task_id:
        raise DocumentAgentServiceError("请选择另一个版本进行章节合并，不能与当前草稿自身合并。")
    primary_result = _load_document_draft_merge_source(task_id=primary_task_id, source_role="当前")
    secondary_result = _load_document_draft_merge_source(task_id=secondary_task_id, source_role="候选")
    primary_root = _document_version_root_task_id(primary_result.document_context, primary_task_id)
    secondary_root = _document_version_root_task_id(secondary_result.document_context, secondary_task_id)
    if primary_root != secondary_root:
        raise DocumentAgentServiceError("只能合并同一根草稿派生出的版本，当前两个版本的来源链不同。")
    ancestor_task_id, ancestor_result = _document_draft_common_ancestor(
        primary_task_id=primary_task_id,
        secondary_task_id=secondary_task_id,
    )
    merged_title, merged_sections, conflicts, _ = _compose_document_draft_merge(
        primary_context=primary_result.document_context,
        secondary_context=secondary_result.document_context,
        base_context=ancestor_result.document_context,
        resolutions={},
    )
    return _DocumentDraftMergePlan(
        primary_result=primary_result,
        secondary_result=secondary_result,
        base_result=ancestor_result,
        root_task_id=primary_root,
        merged_title=merged_title,
        merged_sections=tuple(merged_sections),
        conflicts=tuple(conflicts),
    )


def get_document_draft_merge_candidates(*, task_id: str) -> DocumentDraftMergeCandidateListResponse:
    """列出当前草稿同根、已核验的完整历史版本；用户触发时才做低频历史扫描。"""

    current_result = _load_document_draft_merge_source(task_id=task_id, source_role="当前")
    root_task_id = _document_version_root_task_id(current_result.document_context, task_id)
    # 历史页已有分页存储；合并入口是用户明确点击的低频操作，先在最近完成任务中筛选，避免
    # 给高频详情刷新增加全库 JSON 解析。超过 40 个候选时保留最近版本，仍可从历史恢复后再合并。
    _, run_items = list_workflow_runs(limit=160, offset=0, status="completed", mode="runtime")
    candidates: list[DocumentDraftMergeCandidate] = []
    for item in run_items:
        if item.task_id == task_id:
            continue
        candidate_result = get_document_agent_result(item.task_id)
        if candidate_result is None or not _is_mergeable_draft_snapshot(candidate_result):
            continue
        candidate_context = candidate_result.document_context
        if _document_version_root_task_id(candidate_context, item.task_id) != root_task_id:
            continue
        version = candidate_context.draft_version
        if version is None:
            continue
        candidates.append(
            DocumentDraftMergeCandidate(
                task_id=item.task_id,
                version_id=_document_draft_version_id(candidate_context, item.task_id),
                label=version.label,
                kind=version.kind,
                draft_title=candidate_context.draft_title,
            )
        )
        if len(candidates) >= 40:
            break
    return DocumentDraftMergeCandidateListResponse(
        task_id=task_id,
        root_task_id=root_task_id,
        candidates=candidates,
    )


def get_document_draft_merge_plan(
    *,
    primary_task_id: str,
    secondary_task_id: str,
) -> DocumentDraftMergePlanResponse:
    """返回创建任务前的只读合并计划，让用户先看冲突、后确认选择。"""

    plan = _build_document_draft_merge_plan(
        primary_task_id=primary_task_id,
        secondary_task_id=secondary_task_id,
    )
    primary_version = plan.primary_result.document_context.draft_version
    secondary_version = plan.secondary_result.document_context.draft_version
    if primary_version is None or secondary_version is None:
        raise DocumentDraftMergeNotFoundError("章节合并缺少稳定版本身份。")
    return DocumentDraftMergePlanResponse(
        primary_task_id=primary_task_id,
        secondary_task_id=secondary_task_id,
        root_task_id=plan.root_task_id,
        common_ancestor_task_id=plan.base_result.task_id,
        primary_label=primary_version.label,
        secondary_label=secondary_version.label,
        automatic_section_count=len(plan.merged_sections),
        conflicts=list(plan.conflicts),
        warnings=[
            "这是只读三方合并计划：未调用模型、Tool、工作区读取或文件写入。",
            "只有两边未同时修改的章节会自动采用；冲突必须由用户逐项选择。",
            "创建合并预览后仍需另存 Markdown，所有旧任务和文件保持不变。",
        ],
    )


def build_document_draft_merge_preview_request(
    *,
    primary_task_id: str,
    request: DocumentDraftMergePreviewRequest,
) -> DocumentAgentRunRequest:
    """把确认后的冲突选择绑定到两个不可变快照，拒绝自由正文与遗漏选择。"""

    secondary_task_id = request.other_task_id
    plan = _build_document_draft_merge_plan(
        primary_task_id=primary_task_id,
        secondary_task_id=secondary_task_id,
    )
    expected_conflict_ids = {conflict.conflict_id for conflict in plan.conflicts}
    supplied_conflict_ids = {item.conflict_id for item in request.resolutions}
    if supplied_conflict_ids != expected_conflict_ids:
        if expected_conflict_ids:
            raise DocumentAgentServiceError("请先逐项确认所有章节合并冲突，再建立合并预览。")
        raise DocumentAgentServiceError("当前版本没有待处理冲突，不需要提交冲突选择。")

    primary_context = plan.primary_result.document_context
    secondary_context = plan.secondary_result.document_context
    seed = DocumentDraftMergeSeed(
        primary_task_id=primary_task_id,
        secondary_task_id=secondary_task_id,
        root_task_id=plan.root_task_id,
        primary_version_id=_document_draft_version_id(primary_context, primary_task_id),
        secondary_version_id=_document_draft_version_id(secondary_context, secondary_task_id),
        resolutions=[item.model_copy(deep=True) for item in request.resolutions],
    )
    return DocumentAgentRunRequest(
        task_goal=(
            f"将已核验草稿“{primary_context.draft_title}”与“{secondary_context.draft_title}”"
            "建立同根章节合并预览。"
        ),
        document_refs=list(dict.fromkeys([*primary_context.documents, *secondary_context.documents])),
        output_mode="draft_merge",
        constraints=[
            "只从两个已核验版本和共同祖先恢复章节与来源，不调用模型、不读取材料。",
            "所有同时修改的章节必须使用用户已确认的冲突选择，不自动偏向任一版本。",
            "不修改原草稿、历史任务或任何已保存文件；交付仍必须由用户确认另存。",
        ],
        draft_merge=seed,
    )


async def _run_document_draft_merge_preview(
    *,
    request: DocumentAgentRunRequest,
    task_id: str,
    started_at: datetime,
    progress_callback: DocumentProgressCallback | None,
) -> DocumentAgentRunResponse:
    """以用户已确认的冲突选择建立零模型、零 Tool、零写入的合并草稿预览。"""

    seed = request.draft_merge
    if seed is None:
        raise DocumentAgentServiceError("章节合并预览缺少两个已验证版本身份。")
    await _emit_document_progress(progress_callback, "scope_checking", "正在校验两个已核验草稿与共同版本链。")
    plan = _build_document_draft_merge_plan(
        primary_task_id=seed.primary_task_id,
        secondary_task_id=seed.secondary_task_id,
    )
    primary_context = plan.primary_result.document_context
    secondary_context = plan.secondary_result.document_context
    if _document_draft_version_id(primary_context, seed.primary_task_id) != seed.primary_version_id:
        raise DocumentAgentServiceError("当前草稿版本身份已变化，已停止建立章节合并预览。")
    if _document_draft_version_id(secondary_context, seed.secondary_task_id) != seed.secondary_version_id:
        raise DocumentAgentServiceError("候选草稿版本身份已变化，已停止建立章节合并预览。")
    if plan.root_task_id != seed.root_task_id:
        raise DocumentAgentServiceError("两个草稿不再属于同一根版本链，已停止建立章节合并预览。")

    expected_conflict_ids = {conflict.conflict_id for conflict in plan.conflicts}
    resolutions = {item.conflict_id: item.choice for item in seed.resolutions}
    if set(resolutions) != expected_conflict_ids:
        raise DocumentAgentServiceError("章节合并冲突选择不完整或已过期，请重新查看合并计划。")
    await _emit_document_progress(progress_callback, "scope_ready", "共同祖先已校验，正在按已确认选择合并章节。")
    await _emit_document_progress(progress_callback, "draft_merging", "正在生成独立合并预览；原版本与文件均不会改动。")
    merged_title, merged_sections, unresolved_conflicts, resolved_count = _compose_document_draft_merge(
        primary_context=primary_context,
        secondary_context=secondary_context,
        base_context=plan.base_result.document_context,
        resolutions=resolutions,
    )
    if unresolved_conflicts:
        raise DocumentAgentServiceError("章节合并仍存在未处理冲突，请重新打开合并计划后确认选择。")
    if not merged_title or not merged_sections:
        raise DocumentAgentServiceError("合并结果没有可交付的标题或章节，已停止建立预览。")

    primary_label = primary_context.draft_version.label if primary_context.draft_version else "当前版本"
    secondary_label = secondary_context.draft_version.label if secondary_context.draft_version else "候选版本"
    context = DocumentContext(
        documents=list(dict.fromkeys([*primary_context.documents, *secondary_context.documents])),
        summary=(
            f"已基于“{primary_label}”与“{secondary_label}”建立三方章节合并预览。"
            "系统只采用已核验快照中的标题、正文和来源，未调用模型或读取材料。"
        ),
        draft_title=merged_title,
        draft_sections=merged_sections,
        merge_preview=DocumentDraftMergePreview(
            primary_task_id=seed.primary_task_id,
            secondary_task_id=seed.secondary_task_id,
            common_ancestor_task_id=plan.base_result.task_id,
            automatic_section_count=len(plan.merged_sections),
            resolved_conflict_count=resolved_count,
            conflicts=list(plan.conflicts),
        ),
        sources=_template_sources(merged_sections),
        warnings=[
            "这是独立章节合并预览：两个源版本、共同祖先和已保存 Markdown 文件均未修改。",
            "自动合并只采用单边修改或相同修改；同时修改的章节均已使用用户的显式选择。",
            "确认内容后可使用“保存 Markdown”另存为新的合并版本。",
        ],
        draft_verification_state="verified",
        confidence=primary_context.confidence,
    )
    reply = (
        f"已建立章节合并预览：自动合并 {len(plan.merged_sections)} 个章节，"
        f"已按确认选择处理 {resolved_count} 项冲突。"
    )
    return _persist_document_result(
        task_id=task_id,
        request=request,
        selected_documents=list(context.documents),
        mode="deterministic",
        status="completed",
        stop_reason="completed",
        reply=reply,
        context=context,
        tool_traces=(),
        turn_count=0,
        started_at=started_at,
    )


def _load_document_draft_manual_revision_source(
    *,
    source_task_id: str,
    source_version_id: str,
    section_id: str,
    original_body: str,
) -> tuple[DocumentAgentRunResponse, DocumentDraftSection]:
    """重新绑定手动编辑请求到同一份已完成草稿快照。

    Qt 只能提交目标章节 ID 与新正文；原正文、版本 ID、材料范围都必须从 SQLite 恢复并在
    运行前再次比对。这样“手动修订”不会退化成绕过历史链的任意富文本写入接口。
    """

    source_result = get_document_agent_result(source_task_id)
    if source_result is None:
        raise DocumentDraftSectionNotFoundError("未找到可用于手动修订的原草稿任务。")
    if source_result.status != "completed":
        raise DocumentAgentServiceError("原草稿任务尚未完成，暂时不能建立手动修订预览。")

    source_context = source_result.document_context
    if not source_context.draft_title or not source_context.draft_sections or not source_context.documents:
        raise DocumentAgentServiceError("原任务不是带来源且保留材料范围的 Markdown 草稿预览。")
    actual_version_id = (
        source_context.draft_version.version_id
        if source_context.draft_version is not None
        else source_task_id
    )
    if actual_version_id != source_version_id:
        raise DocumentAgentServiceError("原草稿版本身份已变化，已停止建立手动修订预览。")

    section = next((item for item in source_context.draft_sections if item.id == section_id), None)
    if section is None:
        raise DocumentDraftSectionNotFoundError("原草稿中没有找到所选章节，可能已过期，请重新打开结果。")
    if section.body != original_body:
        raise DocumentAgentServiceError("原章节正文与当前版本快照不一致，已停止建立手动修订预览。")
    return source_result, section


def build_document_draft_section_manual_revision_request(
    *,
    source_task_id: str,
    request: DocumentDraftSectionManualRevisionRequest,
) -> DocumentAgentRunRequest:
    """为用户手动编辑建立独立、待重新核验的章节版本预览。

    该步骤不调用模型、Tool 或文件写入。编辑后的文字暂时没有新的证据资格；只有后续的
    ``draft_review`` 重新读取材料且无待确认事实时，保存接口才会接受这个版本。
    """

    source_result = get_document_agent_result(source_task_id)
    if source_result is None:
        raise DocumentDraftSectionNotFoundError("未找到可用于手动修订的原草稿任务。")
    if source_result.status != "completed":
        raise DocumentAgentServiceError("原草稿任务尚未完成，暂时不能建立手动修订预览。")
    source_context = source_result.document_context
    if not source_context.draft_title or not source_context.draft_sections or not source_context.documents:
        raise DocumentAgentServiceError("原任务不是带来源且保留材料范围的 Markdown 草稿预览。")
    section = next(
        (item for item in source_context.draft_sections if item.id == request.section_id),
        None,
    )
    if section is None:
        raise DocumentDraftSectionNotFoundError("原草稿中没有找到所选章节，可能已过期，请重新打开结果。")

    revised_body = request.revised_body.strip()
    if revised_body == section.body.strip():
        raise DocumentAgentServiceError("手动修订后的正文没有变化，无需建立新的版本预览。")
    source_version_id = (
        source_context.draft_version.version_id
        if source_context.draft_version is not None
        else source_task_id
    )
    seed = DocumentDraftSectionManualRevisionSeed(
        source_task_id=source_task_id,
        root_task_id=_document_version_root_task_id(source_context, source_task_id),
        source_version_id=source_version_id,
        section_id=section.id,
        heading=section.heading,
        original_body=section.body,
        revised_body=revised_body,
    )
    return DocumentAgentRunRequest(
        task_goal=(
            f"基于草稿“{source_context.draft_title}”中的章节“{section.heading}”，"
            "建立用户手动修订后的待事实核验预览。"
        ),
        document_refs=list(source_context.documents),
        output_mode="section_manual_revision",
        constraints=[
            "只建立当前章节的独立手动修订预览，不修改原草稿、历史任务或任何已保存文件。",
            "手动输入不能继承为已验证事实；必须重新读取原材料核验后，才能保存 Markdown。",
        ],
        section_manual_revision=seed,
    )


async def _run_document_draft_section_manual_revision_preview(
    *,
    request: DocumentAgentRunRequest,
    task_id: str,
    started_at: datetime,
    progress_callback: DocumentProgressCallback | None,
) -> DocumentAgentRunResponse:
    """建立零模型、零写入的手动修订预览，并锁定为待核验状态。"""

    seed = request.section_manual_revision
    if seed is None:
        raise DocumentAgentServiceError("手动修订预览缺少已验证的章节身份。")

    await _emit_document_progress(progress_callback, "scope_checking", "正在校验原草稿版本与目标章节身份。")
    source_result, source_section = _load_document_draft_manual_revision_source(
        source_task_id=seed.source_task_id,
        source_version_id=seed.source_version_id,
        section_id=seed.section_id,
        original_body=seed.original_body,
    )
    if source_section.heading != seed.heading:
        raise DocumentAgentServiceError("原章节标题与当前版本快照不一致，已停止建立手动修订预览。")

    await _emit_document_progress(progress_callback, "scope_ready", "原草稿快照已校验，正在建立待来源核验的独立预览。")
    await _emit_document_progress(progress_callback, "manual_revision_previewing", "正在记录章节差异；原草稿和文件均不会改动。")

    revised_sections = [item.model_copy(deep=True) for item in source_result.document_context.draft_sections]
    for section in revised_sections:
        if section.id == source_section.id:
            section.body = seed.revised_body
            # 用户编辑后的文字不应伪装成高置信来源结论；历史来源只作为后续核验的回看线索。
            section.confidence = "low"
            break

    context = DocumentContext(
        documents=list(source_result.document_context.documents),
        summary=(
            f"已建立“{source_section.heading}”的手动修订预览。该章节尚未根据材料重新核验，"
            "因此当前版本不能保存为 Markdown。"
        ),
        draft_title=source_result.document_context.draft_title,
        draft_sections=revised_sections,
        manual_revision_preview=DocumentDraftSectionManualRevisionPreview(
            source_task_id=seed.source_task_id,
            section_id=source_section.id,
            heading=source_section.heading,
            original_body=source_section.body,
            revised_body=seed.revised_body,
        ),
        # 这些来源只可帮助用户回看原草稿证据，不能证明新输入已核验；保存端会基于状态硬拦截。
        sources=[item.model_copy(deep=True) for item in source_result.document_context.sources],
        warnings=[
            "这是用户手动修订的独立预览：原草稿、历史任务和已保存 Markdown 均未修改。",
            "当前章节沿用历史来源作为回看线索，但手动正文尚未重新核验，暂不能保存。",
            "请使用“核验事实”重新读取材料；没有待确认事实后才能另存为新的 Markdown 版本。",
        ],
        draft_verification_state="requires_review",
        confidence="low",
    )
    reply = (
        f"已建立“{source_section.heading}”的手动修订预览；原草稿未改动。"
        "请先核验事实，确认没有待确认问题后再保存新版本。"
    )
    return _persist_document_result(
        task_id=task_id,
        request=request,
        selected_documents=list(source_result.document_context.documents),
        mode="deterministic",
        status="completed",
        stop_reason="completed",
        reply=reply,
        context=context,
        tool_traces=(),
        turn_count=0,
        started_at=started_at,
    )


def _load_section_revision_sources(
    *,
    source_review_task_id: str,
    suggestion_ids: list[str],
) -> tuple[DocumentAgentRunResponse, DocumentDraftSection, list[DocumentRevisionSuggestion]]:
    """从一份完成的本章审校中恢复可安全合并的候选建议。

    每条候选片段都必须在原章节中出现一次，且任意两个原文区间不得重叠。服务端按该规则
    拒绝模糊应用，而不是靠客户端勾选顺序或 ``str.replace`` 的偶然结果决定最后正文。
    """

    normalized_ids = [item.strip() for item in suggestion_ids]
    if not normalized_ids or any(not item for item in normalized_ids):
        raise DocumentAgentServiceError("修订预览缺少已验证的审校建议身份。")
    if len(set(normalized_ids)) != len(normalized_ids):
        raise DocumentAgentServiceError("批量修订不能重复选择同一条建议。")

    review_result = get_document_agent_result(source_review_task_id)
    if review_result is None:
        raise DocumentDraftRevisionSuggestionNotFoundError("未找到可用于生成修订预览的本章审校任务。")
    if review_result.status != "completed":
        raise DocumentAgentServiceError("本章审校任务尚未完成，暂时不能生成修订预览。")

    review_context = review_result.document_context
    section_id = review_context.revision_target_section_id
    if not section_id or not review_context.revision_target_title:
        raise DocumentAgentServiceError("原任务不是已完成的本章审校结果，不能直接应用候选建议。")
    if not review_context.draft_title or not review_context.draft_sections or not review_context.documents:
        raise DocumentAgentServiceError("本章审校结果缺少原草稿或受控材料范围，不能安全生成修订预览。")

    section = next((item for item in review_context.draft_sections if item.id == section_id), None)
    if section is None:
        raise DocumentDraftRevisionSuggestionNotFoundError("本章审校结果中的目标章节已不存在，请重新发起审校。")
    by_id = {item.id: item for item in review_context.revision_suggestions}
    selected: list[DocumentRevisionSuggestion] = []
    spans: list[tuple[int, int, str]] = []
    for suggestion_id in normalized_ids:
        suggestion = by_id.get(suggestion_id)
        if suggestion is None:
            raise DocumentDraftRevisionSuggestionNotFoundError(
                "没有找到所选审校建议，可能已过期，请重新打开审校结果。"
            )
        original_excerpt = suggestion.original_excerpt
        occurrence_count = section.body.count(original_excerpt)
        if occurrence_count == 0:
            raise DocumentAgentServiceError("候选建议的原文片段无法在当前章节中精确定位，不能安全应用。")
        if occurrence_count > 1:
            raise DocumentAgentServiceError(
                "候选建议的原文片段在当前章节中出现多次，系统不会猜测替换位置；请缩小审校范围后重试。"
            )
        if suggestion.suggested_text.strip() == original_excerpt.strip():
            raise DocumentAgentServiceError("候选建议没有产生可见文本变化，无需生成修订预览。")
        start = section.body.find(original_excerpt)
        spans.append((start, start + len(original_excerpt), suggestion.id))
        selected.append(suggestion)

    ordered_spans = sorted(spans)
    for previous, current in zip(ordered_spans, ordered_spans[1:]):
        if previous[1] > current[0]:
            raise DocumentAgentServiceError(
                "所选建议的原文片段存在重叠，系统不会猜测合并顺序；请减少选择后重试。"
            )
    return review_result, section, selected


def build_document_draft_section_revision_request(
    *,
    source_review_task_id: str,
    request: DocumentDraftSectionRevisionRequest,
) -> DocumentAgentRunRequest:
    """把用户明确选择的一条建议转成受控的“修订预览”任务。

    本步骤不请求模型，也不写入文件。它只记录稳定的审校任务/章节/建议身份，真正执行时会
    再从 SQLite 取回同一份审校结果并作精确替换，保证 Qt 无法把任意文本伪装成已核验建议。
    """

    review_result, section, suggestion = _load_section_revision_source(
        source_review_task_id=source_review_task_id,
        suggestion_id=request.suggestion_id,
    )
    return DocumentAgentRunRequest(
        task_goal=(
            f"基于已审校草稿“{review_result.document_context.draft_title}”中的章节“{section.heading}”，"
            f"生成候选建议“{suggestion.id}”的独立修订预览。"
        ),
        document_refs=list(review_result.document_context.documents),
        output_mode="section_revision",
        constraints=[
            "只精确替换用户明确选择的候选原文片段，生成独立版本预览。",
            "不修改原草稿、审校任务或任何已保存文件；如需落盘，必须另走保存确认。",
        ],
        section_revision=DocumentDraftSectionRevisionSeed(
            source_review_task_id=source_review_task_id,
            root_task_id=_document_version_root_task_id(
                review_result.document_context,
                source_review_task_id,
            ),
            section_id=section.id,
            suggestion_id=suggestion.id,
        ),
    )


def build_document_draft_section_batch_revision_request(
    *,
    source_review_task_id: str,
    request: DocumentDraftSectionBatchRevisionRequest,
) -> DocumentAgentRunRequest:
    """把同章、无重叠的多条建议转为一个独立批量修订预览任务。"""

    review_result, section, suggestions = _load_section_revision_sources(
        source_review_task_id=source_review_task_id,
        suggestion_ids=request.suggestion_ids,
    )
    suggestion_ids = [item.id for item in suggestions]
    return DocumentAgentRunRequest(
        task_goal=(
            f"基于已审校草稿“{review_result.document_context.draft_title}”中的章节“{section.heading}”，"
            f"生成 {len(suggestion_ids)} 条候选建议的独立合并修订预览。"
        ),
        document_refs=list(review_result.document_context.documents),
        output_mode="section_revision_batch",
        constraints=[
            "只合并用户明确选择、可唯一定位且彼此不重叠的候选原文片段。",
            "不修改原草稿、审校任务或任何已保存文件；如需落盘，必须另走保存确认。",
        ],
        section_revision_batch=DocumentDraftSectionBatchRevisionSeed(
            source_review_task_id=source_review_task_id,
            root_task_id=_document_version_root_task_id(
                review_result.document_context,
                source_review_task_id,
            ),
            section_id=section.id,
            suggestion_ids=suggestion_ids,
        ),
    )


async def _run_document_draft_section_revision_preview(
    *,
    request: DocumentAgentRunRequest,
    task_id: str,
    started_at: datetime,
    progress_callback: DocumentProgressCallback | None,
) -> DocumentAgentRunResponse:
    """生成一份无模型、无写入的候选建议修订预览。

    审校阶段已经为候选文本和材料来源做过验证，因此这里不应再次消耗模型或读取文件；只需
    对服务端恢复的章节做一次精确替换、保留前后快照并写入普通任务审计。这样既可复盘，又
    不把“应用建议”误实现为一条隐藏的文件写入通道。
    """

    single_seed = request.section_revision
    batch_seed = request.section_revision_batch
    if request.output_mode == "section_revision":
        if single_seed is None:
            raise DocumentAgentServiceError("修订预览缺少已验证的审校建议身份。")
        source_review_task_id = single_seed.source_review_task_id
        section_id = single_seed.section_id
        suggestion_ids = [single_seed.suggestion_id]
    elif request.output_mode == "section_revision_batch":
        if batch_seed is None:
            raise DocumentAgentServiceError("批量修订预览缺少已验证的审校建议身份。")
        source_review_task_id = batch_seed.source_review_task_id
        section_id = batch_seed.section_id
        suggestion_ids = list(batch_seed.suggestion_ids)
    else:
        raise DocumentAgentServiceError("修订预览缺少已验证的审校建议身份。")

    await _emit_document_progress(progress_callback, "scope_checking", "正在校验审校建议与原草稿快照。")
    review_result, source_section, suggestions = _load_section_revision_sources(
        source_review_task_id=source_review_task_id,
        suggestion_ids=suggestion_ids,
    )
    if source_section.id != section_id:
        raise DocumentAgentServiceError("修订预览的章节身份与审校建议不一致，已安全停止。")

    # 按原文位置倒序替换：后段先替换不会改变前段下标，且此前已经拒绝任何区间重叠。
    replacements = sorted(
        (
            source_section.body.find(item.original_excerpt),
            source_section.body.find(item.original_excerpt) + len(item.original_excerpt),
            item,
        )
        for item in suggestions
    )
    revised_body = source_section.body
    for start, end, suggestion in reversed(replacements):
        revised_body = f"{revised_body[:start]}{suggestion.suggested_text}{revised_body[end:]}"
    revised_body = revised_body.strip()
    if not revised_body or revised_body == source_section.body.strip():
        raise DocumentAgentServiceError("候选建议未生成可见的章节变化，无法创建修订预览。")
    if len(revised_body) > 1_500:
        raise DocumentAgentServiceError("候选建议应用后章节超过 1500 字符上限，请缩短建议后重新审校。")

    await _emit_document_progress(progress_callback, "scope_ready", "审校建议与章节身份已校验，正在建立独立版本预览。")
    await _emit_document_progress(progress_callback, "revision_previewing", "正在生成章节前后差异；原草稿和文件均不会改动。")

    def merge_sources(*groups: list[DocumentSourceRef]) -> list[DocumentSourceRef]:
        # 不以 source_id 去重：不同派生任务可各自从 src_001 起编号，展示层应按真实定位去重。
        merged: list[DocumentSourceRef] = []
        seen: set[tuple[str, int, int, str, str]] = set()
        for group in groups:
            for source in group:
                key = (
                    source.relative_path,
                    source.start_line,
                    source.end_line,
                    source.source_locator,
                    source.excerpt,
                )
                if key not in seen:
                    seen.add(key)
                    merged.append(source)
        return merged

    revised_sections: list[DocumentDraftSection] = []
    for section in review_result.document_context.draft_sections:
        if section.id != source_section.id:
            revised_sections.append(section)
            continue
        revised_sections.append(
            DocumentDraftSection(
                id=section.id,
                heading=section.heading,
                body=revised_body,
                source_refs=merge_sources(
                    section.source_refs,
                    *(suggestion.source_refs for suggestion in suggestions),
                ),
                confidence=(
                    "low"
                    if any(item.confidence == "low" for item in suggestions)
                    else "medium"
                    if any(item.confidence == "medium" for item in suggestions)
                    else "high"
                ),
            )
        )

    all_sources = merge_sources(*(section.source_refs for section in revised_sections))
    context = DocumentContext(
        documents=list(review_result.document_context.documents),
        summary=(
            f"已把“{source_section.heading}”中的 {len(suggestions)} 条已审校候选建议应用到独立预览；"
            "原草稿、审校结果和任何已保存文件均未修改。"
        ),
        draft_title=review_result.document_context.draft_title,
        draft_sections=revised_sections,
        revision_preview=DocumentDraftSectionRevisionPreview(
            source_review_task_id=source_review_task_id,
            suggestion_id=suggestions[0].id,
            suggestion_ids=[item.id for item in suggestions],
            section_id=source_section.id,
            heading=source_section.heading,
            original_body=source_section.body,
            revised_body=revised_body,
        ),
        sources=all_sources,
        warnings=[
            "这是独立修订预览：不会覆盖原草稿或已有 Markdown 文件。",
            "确认无误后可使用“保存 Markdown”另存为新版本；保存仍需文件名与二次确认。",
        ],
        confidence=(
            "low"
            if any(item.confidence == "low" for item in suggestions)
            else "medium"
            if any(item.confidence == "medium" for item in suggestions)
            else "high"
        ),
    )
    reply = (
        f"已生成“{source_section.heading}”的 {len(suggestions)} 条建议合并预览与前后差异；"
        "原草稿未改动，确认后可另存为新的 Markdown 版本。"
    )
    return _persist_document_result(
        task_id=task_id,
        request=request,
        selected_documents=list(review_result.document_context.documents),
        mode="deterministic",
        status="completed",
        stop_reason="completed",
        reply=reply,
        context=context,
        tool_traces=(),
        turn_count=0,
        started_at=started_at,
    )


def save_document_draft(
    *,
    task_id: str,
    request: DocumentDraftSaveRequest,
) -> DocumentDraftSaveResponse:
    """把用户已经审阅的 Markdown 草稿保存为受控项目产物。

    该函数不重新调用模型，也不接收输出目录或绝对路径。它只读取同一任务已落库且通过来源
    Guardrail 的 ``DocumentContext``，在用户显式确认后写入固定目录，并把文件追加到原任务
    的 artifact 与日志链中。默认 ``open('x')`` 禁止覆盖，避免一次误点覆盖客户旧草稿。
    """

    if not request.confirmed:
        raise DocumentDraftSaveConfirmationError("保存 Markdown 草稿前需要用户确认。")

    result = get_document_agent_result(task_id)
    if result is None:
        raise DocumentDraftSaveNotFoundError("未找到对应的文档分析任务。")
    if result.status != "completed":
        raise DocumentAgentServiceError("只有已完成且可追溯的分析结果可以保存草稿。")
    if not result.document_context.draft_title or not result.document_context.draft_sections:
        raise DocumentAgentServiceError("当前任务不是可保存的 Markdown 草稿预览。")
    if result.document_context.draft_verification_state != "verified":
        if result.document_context.draft_verification_state == "reviewed_with_questions":
            raise DocumentAgentServiceError(
                "当前草稿的手动修订仍有待确认事实；请先补充或修改后再次核验，暂不能保存。"
            )
        raise DocumentAgentServiceError(
            "当前草稿包含尚未重新核验的手动修订；请先使用“核验事实”，再确认保存。"
        )

    filename = _safe_document_draft_filename(
        request.filename,
        fallback_title=result.document_context.draft_title,
        task_id=task_id,
    )
    output_root = settings.document_draft_output_dir
    output_root.mkdir(parents=True, exist_ok=True)
    target = (output_root / filename).resolve()
    try:
        target.relative_to(output_root)
    except ValueError as exc:
        # 理论上安全文件名已排除路径片段；保留这一层防御，避免未来修改清洗规则时越界。
        raise DocumentAgentServiceError("草稿文件名未通过受控输出目录校验。") from exc

    markdown = _render_document_draft_markdown(result.document_context, task_id=task_id)
    try:
        # x 模式是文件系统级的不覆盖保障，不能只依赖前面的 exists 检查。
        with target.open("x", encoding="utf-8", newline="\n") as file:
            file.write(markdown)
    except FileExistsError as exc:
        raise DocumentDraftSaveConflictError(
            f"output/document_drafts 中已存在同名文件“{filename}”，请改名后再次确认保存。"
        ) from exc
    except OSError as exc:
        raise DocumentAgentServiceError(f"无法写入 Markdown 草稿：{exc}") from exc

    artifact_id = f"{task_id}:document_draft:{uuid4().hex[:10]}"
    relative_path = f"output/document_drafts/{filename}"
    version = result.document_context.draft_version
    artifact = WorkflowArtifact(
        artifact_id=artifact_id,
        task_id=task_id,
        step_id="document_analysis",
        agent_id=DOCUMENT_AGENT_ID,
        kind="markdown",
        name=filename,
        summary=f"用户确认保存的 Markdown 草稿，包含 {len(result.document_context.draft_sections)} 个带来源章节。",
        uri=f"agentflow-output://document_drafts/{filename}",
        mime_type="text/markdown; charset=utf-8",
        metadata={
            # 历史页只会对这两个明确 output_scope 中的路径开放预览/打开，不能由 URI 反推路径。
            "runtime": True,
            "output_scope": "document_drafts",
            "output_path": str(target),
            "relative_output_path": relative_path,
            "confirmed_by": "local_user",
            "source_task_id": task_id,
            # 产物继承任务快照的版本身份。它仅供历史审计和“回看/另存”使用，不能反向指向
            # 任意文件路径，也不赋予覆盖旧版本的权限。
            "document_version_id": version.version_id if version is not None else task_id,
            "document_root_task_id": version.root_task_id if version is not None else task_id,
            "document_parent_task_id": version.parent_task_id if version is not None else "",
            "document_version_kind": version.kind if version is not None else "legacy_draft",
            "document_version_label": version.label if version is not None else "历史草稿快照",
        },
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    try:
        append_workflow_artifact(
            artifact=artifact,
            event_name="artifact_saved",
            message=f"用户已确认保存 Markdown 草稿：{relative_path}",
        )
    except Exception as exc:
        # 文件已经创建但审计失败会破坏“可追溯”的产品承诺；仅删除本次由 x 模式新建的受控文件。
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise DocumentAgentServiceError("草稿保存审计失败，已撤回本次新建文件。") from exc

    clear_dry_run_memory_cache()
    return DocumentDraftSaveResponse(
        task_id=task_id,
        artifact_id=artifact_id,
        filename=filename,
        relative_path=relative_path,
        artifact_uri=artifact.uri,
        message="Markdown 草稿已保存，可在任务历史中预览或打开。",
    )


def _safe_document_draft_filename(raw_filename: str, *, fallback_title: str, task_id: str) -> str:
    """把用户命名限制为单个 UTF-8 Markdown 文件名，不允许客户端指定目录。"""

    candidate = raw_filename.strip()
    if not candidate:
        candidate = f"{fallback_title.strip() or 'AgentFlow 文档草稿'}-{task_id[-6:]}.md"
    if "/" in candidate or "\\" in candidate or Path(candidate).name != candidate:
        raise DocumentAgentServiceError("草稿名称只能是文件名，不能包含目录或路径分隔符。")
    if not candidate.lower().endswith(".md"):
        raise DocumentAgentServiceError("草稿名称必须以 .md 结尾。")

    stem = candidate[:-3].strip()
    # Windows 非法字符不能原样落盘；其余 Unicode 字符（包括中文）保留，避免把客户标题变乱码。
    sanitized_stem = re.sub(r'[<>:"|?*\x00-\x1f]+', "-", stem)
    sanitized_stem = re.sub(r"\s+", " ", sanitized_stem).strip(" .-")
    if not sanitized_stem:
        sanitized_stem = f"AgentFlow 文档草稿-{task_id[-6:]}"
    return f"{sanitized_stem[:96]}.md"


def _render_document_draft_markdown(context: DocumentContext, *, task_id: str) -> str:
    """把已验证的草稿结构渲染为可交付 Markdown，并将来源留在每个章节之后。"""

    lines = [f"# {context.draft_title.strip()}", ""]
    for section in context.draft_sections:
        lines.extend((f"## {section.heading.strip()}", "", section.body.strip(), ""))
        source_labels = [
            _markdown_source_label(source)
            for source in section.source_refs
        ]
        lines.append(f"> 来源：{'；'.join(source_labels)}")
        lines.append("")
    version = context.draft_version
    version_note = (
        f"<!-- AgentFlow 文档助手 · 来源已校验 · 任务 {task_id} · 版本 {version.label} "
        f"· 根任务 {version.root_task_id} · 父任务 {version.parent_task_id or '无'} -->"
        if version is not None
        else f"<!-- AgentFlow 文档助手 · 来源已校验 · 任务 {task_id} -->"
    )
    lines.extend(("---", version_note, ""))
    return "\n".join(lines)


def _markdown_source_label(source: DocumentSourceRef) -> str:
    """生成不含绝对路径的简短来源文本，供草稿读者回到 workspace 原材料复核。"""

    locator = source.source_locator.replace("\n", " ").strip()
    if not locator:
        if source.source_kind == "page":
            locator = f"第 {source.start_line} 页"
        elif source.start_line == source.end_line:
            locator = f"第 {source.start_line} 行"
        else:
            locator = f"第 {source.start_line}-{source.end_line} 行"
    return f"{source.relative_path} · {locator}"


async def _emit_document_progress(
    callback: DocumentProgressCallback | None,
    stage: str,
    message: str,
    level: TaskLogLevel = "info",
) -> None:
    """观察面故障不应阻止文档结论与审计链落库。"""

    if callback is None:
        return
    try:
        result = callback(stage, message, level)
        if hasattr(result, "__await__"):
            await result
    except Exception:
        return


def _compacted_result_progress_callback(
    callback: DocumentProgressCallback | None,
) -> Callable[[AgentRunProgress], Awaitable[None] | None] | None:
    """把归并阶段 Runner 的协议事件映射为文档页可理解的真实状态。"""

    if callback is None:
        return None

    async def on_progress(event: AgentRunProgress) -> None:
        if event.stage == "output_validation_started":
            message = "正在核对归并结论的结构与来源引用。"
        elif event.stage == "output_format_repair_started":
            message = "归并结论格式不完整，正在进行一次不调用工具的安全修复。"
        else:
            message = "正在根据已压缩的来源证据归并最终结论。"
        await _emit_document_progress(callback, event.stage, message)

    return on_progress


def _output_repair_count(turn_traces: tuple[AgentTurnTrace, ...]) -> int:
    """统计受控格式修复次数；它是成本和失败诊断的一部分，不保存模型原始回复。"""

    return sum(1 for trace in turn_traces if trace.output_repair_requested)


def _record_output_repair_warning(
    warnings: list[str],
    turn_traces: tuple[AgentTurnTrace, ...],
) -> None:
    """把发生过的格式修复显式留给客户和历史页，避免额外模型回合成为黑盒。"""

    repair_count = _output_repair_count(turn_traces)
    if not repair_count:
        return
    message = (
        f"模型首次结构化输出未通过校验，已进行 {repair_count} 次不调用工具的格式修复；"
        "来源范围未因此扩大。"
    )
    if message not in warnings:
        warnings.append(message)


async def _select_documents(
    request: DocumentAgentRunRequest,
) -> tuple[list[str], str, str, str]:
    """根据显式选择和 workspace 状态决定本次文档范围。"""

    # 首次列出 PDF/DOCX 会解析少量预览；放到线程池，不让“确认材料范围”阻塞事件循环。
    available_documents = await asyncio.to_thread(list_workspace_documents)
    available = {document.relative_path for document in available_documents}
    requested = list(dict.fromkeys(ref.strip() for ref in request.document_refs if ref.strip()))
    if requested:
        missing = [ref for ref in requested if ref not in available]
        if missing:
            return requested, "", "insufficient_context", f"未找到已选择的文档：{'、'.join(missing)}。请重新导入或选择当前 workspace 中的文件。"
        # 再调用服务的公开边界做一次后缀/存在性检查，不依赖列表结果作为安全授权。
        try:
            for ref in requested:
                await asyncio.to_thread(
                    get_workspace_document_preview,
                    relative_path=ref,
                    preview_chars=0,
                )
        except WorkspaceDocumentError as exc:
            return requested, "", "insufficient_context", str(exc)
        if _uses_multiple_documents(request.output_mode) and len(requested) < 2:
            mode_name = {
                "comparison": "多文档对比",
                "cross_qa": "跨文档问答",
                "synthesis": "跨文档整合",
            }.get(request.output_mode, "跨文档任务")
            return (
                requested,
                "",
                "needs_clarification",
                f"{mode_name}至少需要选择两份材料；请在文档助手页勾选两份或更多文档。",
            )
        return requested, "", "", ""

    documents = sorted(available)
    if not documents:
        return [], "", "insufficient_context", "还没有可分析的文档。请先导入 TXT、Markdown、PDF 或 DOCX 文件。"
    if len(documents) > 1:
        preview = "、".join(documents[:4])
        suffix = "等" if len(documents) > 4 else ""
        return [], "", "needs_clarification", f"当前 workspace 有多个文档（{preview}{suffix}）。请在文档助手页选择要分析的文件，避免误读材料。"
    return documents, "当前 workspace 只有一份文档，已自动选中。", "", ""


def _document_agent_turn_limit(
    request: DocumentAgentRunRequest,
    selected_documents: list[str],
) -> int:
    """根据已知材料读取工作量计算有限轮次，不把普通 Agent 的 4 轮误用于跨文档任务。"""

    if not _uses_multiple_documents(request.output_mode):
        return 4
    # 每份材料最多两页，再加最终 JSON 与一次仅格式修复；选满四份时为 10 轮，读取预算
    # 仍保持 8 次不变，额外一轮不能读取或搜索材料。
    return min(10, max(4, len(selected_documents) * 2 + 2))


def _document_agent_tool_call_limit(
    request: DocumentAgentRunRequest,
    selected_documents: list[str],
) -> int:
    """让 Tool 预算与分页轮次一致，避免 Runner 在合法的第二页读取前提前终止。"""

    if not _uses_multiple_documents(request.output_mode):
        return 8
    return min(8, max(4, len(selected_documents) * 2))


def _build_exact_search_miss_response(
    *,
    request: DocumentAgentRunRequest,
    task_id: str,
    selected_documents: list[str],
    mode: str,
    runtime: _DocumentToolRuntime,
    tool_traces: tuple[AgentToolTrace, ...],
    turn_count: int,
    output_format_repair_count: int,
    started_at: datetime,
) -> DocumentAgentRunResponse | None:
    """把“零命中且没有任何可引用证据”收束成可操作的澄清结果。

    单文档时正常路径会按 Tool 建议回读全文；这里仅兜住模型忽略建议、直接输出且因缺少
    source_id 被协议拒绝的情况。这样既不把零命中误报成“文档没有内容”，也不把模型格式
    问题直接抛给用户。只要已经存在实际读取/搜索来源，就保持原始失败，便于排查模型输出。
    """

    if not runtime.exact_search_misses or runtime.sources:
        return None

    queries = "、".join(f"“{item}”" for item in runtime.exact_search_misses)
    message = (
        f"未在所选材料中找到 {queries} 的精确文本命中。为避免把零命中误判为内容不存在，"
        "本次没有生成无来源结论。请改用同义词、缩小问题范围，或先使用“生成摘要”了解材料结构。"
    )
    context = DocumentContext(
        documents=selected_documents,
        warnings=runtime.warnings,
        missing_context=[message],
        confidence="low",
    )
    return _persist_document_result(
        task_id=task_id,
        request=request,
        selected_documents=selected_documents,
        mode=mode,
        status="insufficient_context",
        stop_reason="exact_search_no_match",
        reply=message,
        context=context,
        tool_traces=tool_traces,
        turn_count=turn_count,
        output_format_repair_count=output_format_repair_count,
        started_at=started_at,
    )


def _build_conservative_requirements_fallback_response(
    *,
    request: DocumentAgentRunRequest,
    task_id: str,
    selected_documents: list[str],
    mode: str,
    runtime: _DocumentToolRuntime,
    stop_reason: str,
    tool_traces: tuple[AgentToolTrace, ...],
    turn_count: int,
    output_format_repair_count: int,
    started_at: datetime,
) -> DocumentAgentRunResponse | None:
    """在需求提取的模型 JSON 持续失效时，保守复用已读取的原文证据。

    这条降级绝不回答自由问答、不归纳摘要、更不生成跨文档比较。它只提取原文中明确出现的
    “必须/需要/不得/验收”等标记行，并逐条保留 Runtime 已校验的来源。用户主动选择
    requirements 时，这比丢弃已读取材料并只显示协议错误更有用，也不会把规则提取伪装成
    模型语义理解。
    """

    if (
        request.output_mode != "requirements"
        or stop_reason != "model_output_invalid"
        or not runtime.sources
    ):
        return None

    context = DocumentContext(
        documents=selected_documents,
        # 没有使用模型的最终摘要，避免把未校验的模型原文或推断写进最终结果。
        summary="",
        sources=list(runtime.sources.values()),
        warnings=list(runtime.warnings),
        confidence="low",
    )
    _apply_requested_output_fallback(request=request, context=context, runtime=runtime)
    if not context.requirements:
        return None

    warning = (
        "模型在一次无工具格式修复后仍未生成可验证 JSON；已仅依据原文中的明确要求词"
        "生成保守需求条目，未补充模型推断。"
    )
    if warning not in context.warnings:
        context.warnings.append(warning)
    context.summary = f"已从已读取原文中生成 {len(context.requirements)} 条可追溯的保守需求。"
    reply = (
        "模型结构化输出未通过校验；已根据原文中的明确要求词生成可追溯需求条目。"
        "请结合每条来源复核未覆盖的隐含需求。"
    )
    return _persist_document_result(
        task_id=task_id,
        request=request,
        selected_documents=selected_documents,
        mode=mode,
        status="completed",
        stop_reason="completed_with_conservative_requirements_fallback",
        reply=reply,
        context=context,
        tool_traces=tool_traces,
        turn_count=turn_count,
        output_format_repair_count=output_format_repair_count,
        started_at=started_at,
    )


def _build_conservative_brief_fallback_response(
    *,
    request: DocumentAgentRunRequest,
    task_id: str,
    selected_documents: list[str],
    mode: str,
    runtime: _DocumentToolRuntime,
    stop_reason: str,
    tool_traces: tuple[AgentToolTrace, ...],
    turn_count: int,
    output_format_repair_count: int,
    started_at: datetime,
) -> DocumentAgentRunResponse | None:
    """在关键信息卡的 JSON 输出失效后，按原文显式标记做一次保守降级。

    此路径刻意不理解同义词、不合并句意，也不尝试补齐所有七个字段；它只复用 mock 的
    字面字段规则，因而仍能保证每项来自 Runtime 已读取的文本与 source_ref。这样用户
    选择“关键信息卡”时不会因模型偶发格式错误得到一张空白结果卡。
    """

    if request.output_mode != "brief" or not runtime.sources:
        return None

    records = [
        (source_id, source.relative_path, runtime.source_texts.get(source_id, ""))
        for source_id, source in runtime.sources.items()
    ]
    raw_fields = _mock_brief_fields(records)
    if not raw_fields:
        return None

    brief_fields = [
        DocumentBriefField(
            key=item["key"],  # type: ignore[arg-type]
            value=item["value"],
            source_refs=_resolve_source_ids(item["source_ids"], runtime.sources),
            confidence=item["confidence"],  # type: ignore[arg-type]
        )
        for item in raw_fields
    ]
    used_sources = _used_sources(
        requirements=[],
        brief_fields=brief_fields,
        outline_sections=[],
        draft_sections=[],
        findings=[],
        comparisons=[],
    )
    context = DocumentContext(
        documents=selected_documents,
        summary=f"已从已读取原文中整理 {len(brief_fields)} 项可追溯关键信息。",
        brief_fields=brief_fields,
        sources=used_sources,
        warnings=[
            *runtime.warnings,
            "模型未输出合法关键信息卡 JSON；已仅依据原文中的明确标题和字段标记生成保守结果，请结合来源复核。",
        ],
        confidence="medium",
    )
    return _persist_document_result(
        task_id=task_id,
        request=request,
        selected_documents=selected_documents,
        mode=mode,
        status="completed",
        stop_reason=f"completed_with_conservative_brief_fallback:{stop_reason}",
        reply=f"模型未能完成结构化字段输出；已从已读取原文中生成 {len(brief_fields)} 项可追溯关键信息。",
        context=context,
        tool_traces=tool_traces,
        turn_count=turn_count,
        output_format_repair_count=output_format_repair_count,
        started_at=started_at,
    )


def _build_conservative_outline_fallback_response(
    *,
    request: DocumentAgentRunRequest,
    task_id: str,
    selected_documents: list[str],
    mode: str,
    runtime: _DocumentToolRuntime,
    stop_reason: str,
    tool_traces: tuple[AgentToolTrace, ...],
    turn_count: int,
    output_format_repair_count: int,
    started_at: datetime,
) -> DocumentAgentRunResponse | None:
    """在结构化大纲 JSON 失效后，按已读取材料生成最小只读蓝图。

    降级只根据标题和原文中的显式范围、交付、计划、风险线索分组；不补写正文、不创建
    文件，也不把通用文档模板包装成材料事实。这样真实 Provider 偶发输出协议错误时，用户
    仍能审阅一个带来源的大纲，而不是丢失已经完成的受控读取。
    """

    if request.output_mode != "outline" or not runtime.sources:
        return None

    records = [
        (source_id, source.relative_path, runtime.source_texts.get(source_id, ""))
        for source_id, source in runtime.sources.items()
    ]
    raw_sections = _mock_outline_sections(records)
    if not raw_sections:
        return None

    outline_sections = [
        DocumentOutlineSection(
            id=item["id"],
            title=item["title"],
            intent=item["intent"],
            key_points=item["key_points"],
            source_refs=_resolve_source_ids(item["source_ids"], runtime.sources),
            confidence=item["confidence"],  # type: ignore[arg-type]
        )
        for item in raw_sections
    ]
    used_sources = _used_sources(
        requirements=[],
        brief_fields=[],
        outline_sections=outline_sections,
        draft_sections=[],
        findings=[],
        comparisons=[],
    )
    context = DocumentContext(
        documents=selected_documents,
        summary=f"已从已读取原文中整理 {len(outline_sections)} 个可追溯大纲章节。",
        outline_sections=outline_sections,
        sources=used_sources,
        warnings=[
            *runtime.warnings,
            "模型未输出合法结构化大纲 JSON；已仅依据原文标题与明确标记生成保守只读蓝图，请结合来源审阅后再进入正式创作。",
        ],
        confidence="medium",
    )
    return _persist_document_result(
        task_id=task_id,
        request=request,
        selected_documents=selected_documents,
        mode=mode,
        status="completed",
        stop_reason=f"completed_with_conservative_outline_fallback:{stop_reason}",
        reply=f"模型未能完成结构化大纲输出；已从已读取原文中生成 {len(outline_sections)} 个可追溯章节，供审阅确认。",
        context=context,
        tool_traces=tool_traces,
        turn_count=turn_count,
        output_format_repair_count=output_format_repair_count,
        started_at=started_at,
    )


def _document_system_prompt(output_mode: str) -> str:
    """根据任务模式给模型最小而明确的证据收束规则。"""

    if output_mode == "comparison":
        mode_rule = (
            "当前是多文档对比模式：必须从 start_char=0 开始逐份调用 document_read_text，每次请求 "
            "max_chars=48000。若 Tool 返回 truncated=true，必须对同一文档使用 next_start_char 继续读取，"
            "直到 full_document_read=true；完成所有已选择文档后再输出最终 JSON。comparisons 的每项必须"
            "同时引用至少两份不同文档的 source_id；若某份材料仍未读完，必须在 open_questions 中说明覆盖范围。"
        )
    elif output_mode == "cross_qa":
        mode_rule = (
            "当前是跨文档问答模式：必须从 start_char=0 开始逐份调用 document_read_text，每次请求 "
            "max_chars=48000。若 Tool 返回 truncated=true，必须对同一文档使用 next_start_char 继续读取，"
            "直到 full_document_read=true；完成所有已选择文档后再输出最终 JSON。answer_source_ids 必须"
            "覆盖至少两份不同文档；只回答用户问题，不要求生成 comparisons。"
        )
    elif output_mode == "synthesis":
        mode_rule = (
            "当前是跨文档整合模式：必须从 start_char=0 开始逐份调用 document_read_text，每次请求 "
            "max_chars=48000。若 Tool 返回 truncated=true，必须对同一文档使用 next_start_char 继续读取，"
            "直到 full_document_read=true；完成所有已选择文档后再输出最终 JSON。answer_source_ids 必须"
            "覆盖至少两份不同文档。请把可兼容的需求、约束或待办合并为 requirements；同一事实被多份"
            "材料支持时保留多个 source_id。若说法冲突、范围不一致或证据不足，只能放入 open_questions，"
            "不能自行裁决。"
        )
    elif output_mode == "brief":
        mode_rule = (
            "当前是关键信息卡模式：先读取所选材料，再从原文明确表达的事实中填写 brief_fields。"
            "只允许 subject、purpose、scope、stakeholders、deliverables、milestones、risks 七种 key；"
            "每项必须带 source_id。缺少证据的字段不要猜测，可在 open_questions 说明材料缺口。"
        )
    elif output_mode == "outline":
        mode_rule = (
            "当前是结构化大纲模式：先完整读取所选材料，再生成 1 至 8 个 outline_sections。每个章节"
            "必须包含 id、title、intent、key_points、source_ids 和 confidence；key_points 只保留材料明确"
            "表达的事实或待确认事项。它是供用户审阅的只读蓝图，不是已生成、已写入或已导出的文档。"
            "不要用通用模板补齐材料没有表达的章节。"
        )
    elif output_mode == "draft":
        mode_rule = (
            "当前是 Markdown 草稿预览模式：先完整读取所选材料，再生成 draft_title 和 1 至 6 个"
            "draft_sections。每个章节必须包含 id、heading、body、source_ids 和 confidence；body 只能"
            "组织材料明确表达的事实，不得虚构案例、数据、承诺或引用。草稿仅供用户审阅，不是文件"
            "写入、覆盖、保存或导出请求。"
        )
    elif output_mode == "section_draft":
        mode_rule = (
            "当前是单章节创作预览模式：先完整读取所选材料，再只生成 1 个 draft_section。用户消息"
            "给出了既有章节 ID、标题、原正文和调整要求；必须保留该章节 ID 与标题，只能根据本轮"
            "读取到的事实扩展或改写正文，不得虚构案例、数据、承诺或引用。不得生成其他章节，不得"
            "声明已修改原草稿、已保存或已导出文件。"
        )
    elif output_mode == "section_review":
        mode_rule = (
            "当前是单章节审校模式：先完整读取所选材料，再只审校用户消息中的既有章节。"
            "revision_suggestions 最多返回 6 条，每条必须包含 id、severity（important 或 suggestion）、"
            "category（accuracy、clarity、consistency、structure、style）、original_excerpt、suggested_text、"
            "reason、source_ids 和 confidence。original_excerpt 必须来自既有章节的实际正文；建议表述"
            "只能依据本轮读取的材料事实。它们只供用户决定是否采纳，不得修改、保存或导出草稿。"
        )
    else:
        mode_rule = "一旦 document_read_text 成功返回 source_id，材料获取阶段已经结束：下一轮必须直接输出最终 JSON，不能再次搜索或读取。"
    return f"""你是 AgentFlow 的文档助手。只根据 Tool 返回的材料回答，不得声称读取过未提供的文档。
需要定位内容时先调用 document_search_text；需要理解内容时调用 document_read_text。不得调用未声明的工具。
当 document_search_text 返回 total=0 时，这只表示关键词没有精确文本命中，不能据此断言材料不存在相关内容。
若 Tool 给出 recommended_fallback_read_path，必须读取该单份已选材料后再输出结论；应在 answer 或 open_questions
中说明“精确搜索未命中”，但只能根据实际读取到的 source_id 判断材料内容。不要因为零命中跳过来源引用。
{mode_rule}
Tool 返回 ``content_status=available`` 且 ``text`` 非空时，表示已经拿到原始 UTF-8 文本；不得把它误判成乱码、占位符或未读取。
所有关键结论必须引用 Tool 结果中的 source_id；不得自行编造文件名、路径、行号或 source_id。
完成后只返回 JSON object，不要 Markdown 代码围栏。
{_document_output_contract(output_mode)}
只允许输出上一行契约列出的字段；未列出的字段不要用空数组、空 object 或占位文本补齐。优先保留可回溯的高价值结论，不要重复复述原文。
若材料不足，在 answer 中说明不足；不要把推测伪装成文档事实。"""


def _document_user_message(request: DocumentAgentRunRequest, selected_documents: list[str]) -> str:
    constraints = "；".join(item.strip() for item in request.constraints if item.strip()) or "无"
    query = request.query.strip() or "无"
    if request.output_mode == "comparison":
        mode_instruction = "这是多文档对比，请先读取每一份所选材料；完成后返回共识、差异或缺失项，并为每项提供跨文档来源。"
    elif request.output_mode == "cross_qa":
        mode_instruction = "这是跨文档问答，请先读取每一份所选材料；直接回答用户问题，并让 answer_source_ids 至少覆盖两份不同材料。"
    elif request.output_mode == "synthesis":
        mode_instruction = "这是跨文档整合，请先读取每一份所选材料；整理可兼容内容，重复证据合并来源，冲突只列为待确认事项。"
    elif request.output_mode == "brief":
        mode_instruction = "请按关键信息卡提取材料中明确表达的主题、目的、范围、相关角色、交付物、时间节点和风险；没有证据的字段不要补写。"
    elif request.output_mode == "outline":
        mode_instruction = "请先读取所选材料，再生成可追溯的结构化大纲。大纲仅供审阅，章节要点必须来自材料；不要写入、覆盖或导出任何文件。"
    elif request.output_mode == "draft":
        mode_instruction = "请先读取所选材料，再生成可审阅的 Markdown 草稿预览。按章节组织正文并保留每章来源；当前不写入、覆盖、保存或导出文件。"
    elif request.output_mode == "section_draft" and request.section_draft is not None:
        seed = request.section_draft
        mode_instruction = (
            "请先重新读取所选材料，再只生成该章节的一份可审阅预览。必须保留既有章节 ID 和标题，"
            "只根据本轮读取材料改写或扩展正文；不写入、覆盖、保存或导出文件。\n"
            f"既有章节 ID：{seed.section_id}\n"
            f"既有章节标题：{seed.heading}\n"
            f"既有章节正文：{seed.current_body}\n"
            f"用户本次调整要求：{seed.instruction}"
        )
    elif request.output_mode == "draft_review" and request.draft_review is not None:
        seed = request.draft_review
        mode_instruction = (
            "请先重新读取所选材料，再核验下列草稿。constraints 仅列材料明确支持的关键表述；"
            "open_questions 仅列来源不足、表述过度或需要用户确认的内容。不得改写或保存草稿。\n"
            f"草稿标题：{seed.draft_title}\n"
            f"草稿章节：{json.dumps([item.model_dump(mode='json') for item in seed.draft_sections], ensure_ascii=False)}\n"
            f"用户关注点：{seed.focus or '无，优先核对具体事实与承诺是否有材料依据。'}"
        )
    elif request.output_mode == "section_review" and request.section_review is not None:
        seed = request.section_review
        mode_instruction = (
            "请先重新读取所选材料，再审校下列章节。只在 revision_suggestions 中列问题与候选表述；"
            "original_excerpt 必须复制自既有章节正文，建议不能引入材料外的事实。不得改写、保存或导出草稿。\n"
            f"草稿标题：{seed.draft_title}\n"
            f"章节 ID：{seed.section_id}\n"
            f"章节标题：{seed.heading}\n"
            f"既有章节正文：{seed.current_body}\n"
            f"用户关注点：{seed.focus or '无，优先检查准确性、清晰度、一致性和结构。'}"
        )
    elif request.output_mode == "draft_review":
        mode_instruction = "草稿事实核验缺少已验证的草稿快照，不能继续。"
    elif request.output_mode == "section_review":
        mode_instruction = "本章审校缺少已验证的章节种子，不能继续。"
    elif request.output_mode == "section_draft":
        mode_instruction = "单章节创作缺少已验证的章节种子，不能继续生成。"
    else:
        mode_instruction = "请按需获取 Tool 来源。没有定位关键词时优先读取所选文档；读取成功后立即根据已有 source_id 返回最终 JSON。"
    return (
        f"任务目标：{request.task_goal.strip()}\n"
        f"本次已选择文档：{', '.join(selected_documents)}\n"
        f"可选定位关键词：{query}\n"
        f"输出模式：{request.output_mode}\n"
        f"额外约束：{constraints}\n"
        + mode_instruction
    )


def _materialize_document_context(
    *,
    output: DocumentModelOutput,
    selected_documents: list[str],
    source_map: dict[str, DocumentSourceRef],
    warnings: list[str],
) -> DocumentContext:
    requirements = [
        _materialize_requirement(item, source_map)
        for item in output.requirements
    ]
    comparisons = [_materialize_comparison(item, source_map) for item in output.comparisons]
    brief_fields = [_materialize_brief_field(item, source_map) for item in output.brief_fields]
    outline_sections = [_materialize_outline_section(item, source_map) for item in output.outline_sections]
    draft_sections = [_materialize_draft_section(item, source_map) for item in output.draft_sections]
    revision_suggestions = [
        _materialize_revision_suggestion(item, source_map)
        for item in output.revision_suggestions
    ]
    constraints = [_materialize_finding(item, source_map) for item in output.constraints]
    todos = [_materialize_finding(item, source_map) for item in output.todos]
    entities = [_materialize_finding(item, source_map) for item in output.entities]
    questions = [_materialize_finding(item, source_map) for item in output.open_questions]
    answer_sources = _resolve_source_ids(output.answer_source_ids, source_map)
    used_sources = _used_sources(
        requirements=requirements,
        brief_fields=brief_fields,
        outline_sections=outline_sections,
        draft_sections=draft_sections,
        revision_suggestions=revision_suggestions,
        findings=constraints + todos + entities + questions,
        comparisons=comparisons,
    )
    for source in answer_sources:
        if source.source_id not in {item.source_id for item in used_sources}:
            used_sources.append(source)
    if not used_sources:
        raise DocumentAgentServiceError("最终结论没有引用任何已读取或搜索到的来源。")
    return DocumentContext(
        documents=selected_documents,
        summary=output.summary,
        requirements=requirements,
        comparisons=comparisons,
        brief_fields=brief_fields,
        outline_sections=outline_sections,
        draft_title=output.draft_title,
        draft_sections=draft_sections,
        revision_suggestions=revision_suggestions,
        constraints=constraints,
        todos=todos,
        entities=entities,
        open_questions=questions,
        sources=used_sources,
        warnings=list(dict.fromkeys(warnings)),
        confidence=output.confidence,
    )


def _materialize_requirement(
    item: DocumentDraftRequirement,
    source_map: dict[str, DocumentSourceRef],
) -> DocumentRequirement:
    return DocumentRequirement(
        id=item.id,
        text=item.text,
        category=item.category,
        priority=item.priority,
        source_refs=_resolve_source_ids(item.source_ids, source_map),
        confidence=item.confidence,
    )


def _materialize_brief_field(
    item: DocumentDraftBriefField,
    source_map: dict[str, DocumentSourceRef],
) -> DocumentBriefField:
    """把模型的固定字段映射为可展示来源，拒绝无来源的信息卡。"""

    return DocumentBriefField(
        key=item.key,
        value=item.value,
        source_refs=_resolve_source_ids(item.source_ids, source_map),
        confidence=item.confidence,
    )


def _materialize_outline_section(
    item: DocumentDraftOutlineSection,
    source_map: dict[str, DocumentSourceRef],
) -> DocumentOutlineSection:
    """把待审阅大纲映射为正式来源，避免章节看起来完整却无法追溯。"""

    return DocumentOutlineSection(
        id=item.id,
        title=item.title,
        intent=item.intent,
        key_points=item.key_points,
        source_refs=_resolve_source_ids(item.source_ids, source_map),
        confidence=item.confidence,
    )


def _materialize_draft_section(
    item: DocumentDraftPreviewSection,
    source_map: dict[str, DocumentSourceRef],
) -> DocumentDraftSection:
    """把模型草稿章节映射为已验证的来源，草稿内容本身仍不触发任何文件写入。"""

    return DocumentDraftSection(
        id=item.id,
        heading=item.heading,
        body=item.body,
        source_refs=_resolve_source_ids(item.source_ids, source_map),
        confidence=item.confidence,
    )


def _materialize_revision_suggestion(
    item: DocumentDraftRevisionSuggestion,
    source_map: dict[str, DocumentSourceRef],
) -> DocumentRevisionSuggestion:
    """把模型的审校建议映射为来源受控的只读候选项。"""

    return DocumentRevisionSuggestion(
        id=item.id,
        severity=item.severity,
        category=item.category,
        original_excerpt=item.original_excerpt,
        suggested_text=item.suggested_text,
        reason=item.reason,
        source_refs=_resolve_source_ids(item.source_ids, source_map),
        confidence=item.confidence,
    )


def _materialize_finding(
    item: DocumentDraftFinding,
    source_map: dict[str, DocumentSourceRef],
) -> DocumentFinding:
    return DocumentFinding(
        text=item.text,
        source_refs=_resolve_source_ids(item.source_ids, source_map),
        confidence=item.confidence,
    )


def _materialize_comparison(
    item: DocumentDraftComparison,
    source_map: dict[str, DocumentSourceRef],
) -> DocumentComparison:
    """映射模型来源，并拒绝把单文档结论伪装成跨文档对比。"""

    source_refs = _resolve_source_ids(item.source_ids, source_map)
    if len({source.relative_path for source in source_refs}) < 2:
        raise DocumentAgentServiceError("对比结论必须同时引用至少两份不同材料。")
    return DocumentComparison(
        dimension=item.dimension,
        kind=item.kind,
        summary=item.summary,
        source_refs=source_refs,
        confidence=item.confidence,
    )


def _resolve_source_ids(
    source_ids: list[str],
    source_map: dict[str, DocumentSourceRef],
) -> list[DocumentSourceRef]:
    if not source_ids:
        raise DocumentAgentServiceError("关键结论缺少 source_id。")
    refs: list[DocumentSourceRef] = []
    for source_id in dict.fromkeys(source_ids):
        source = source_map.get(source_id)
        if source is None:
            raise DocumentAgentServiceError(f"模型引用了未知来源 ID：{source_id}。")
        refs.append(source)
    return refs


def _used_sources(
    *,
    requirements: list[DocumentRequirement],
    brief_fields: list[DocumentBriefField],
    outline_sections: list[DocumentOutlineSection],
    draft_sections: list[DocumentDraftSection],
    findings: list[DocumentFinding],
    comparisons: list[DocumentComparison],
    revision_suggestions: list[DocumentRevisionSuggestion] | None = None,
) -> list[DocumentSourceRef]:
    sources: dict[str, DocumentSourceRef] = {}
    for requirement in requirements:
        for source in requirement.source_refs:
            sources[source.source_id] = source
    for field in brief_fields:
        for source in field.source_refs:
            sources[source.source_id] = source
    for section in outline_sections:
        for source in section.source_refs:
            sources[source.source_id] = source
    for section in draft_sections:
        for source in section.source_refs:
            sources[source.source_id] = source
    for suggestion in revision_suggestions or []:
        for source in suggestion.source_refs:
            sources[source.source_id] = source
    for finding in findings:
        for source in finding.source_refs:
            sources[source.source_id] = source
    for comparison in comparisons:
        for source in comparison.source_refs:
            sources[source.source_id] = source
    return list(sources.values())


def _validate_multi_document_context(
    *,
    request: DocumentAgentRunRequest,
    context: DocumentContext,
    runtime: _DocumentToolRuntime,
    answer_source_ids: list[str],
) -> None:
    """跨文档模式的硬校验：未读全或证据不足时都不能显示为完成。"""

    if not _uses_multiple_documents(request.output_mode):
        return
    unread = [document for document in context.documents if document not in runtime.read_documents]
    if unread:
        raise DocumentAgentServiceError(f"跨文档任务尚未读取全部材料：{'、'.join(unread)}。")
    if request.output_mode == "comparison":
        if not context.comparisons:
            raise DocumentAgentServiceError("多文档对比没有返回任何带跨文档来源的比较结论。")
        return

    # 不能仅看 context.sources：模型可能给某个附带条目引用两份材料，但用户真正读到的
    # answer 却只基于一份。跨文档问答和整合都要直接核对 answer_source_ids 的文件覆盖范围。
    answer_sources = _resolve_source_ids(answer_source_ids, runtime.sources)
    if len({source.relative_path for source in answer_sources}) < 2:
        mode_name = "跨文档整合" if request.output_mode == "synthesis" else "跨文档问答"
        raise DocumentAgentServiceError(f"{mode_name}的本次结论必须同时引用至少两份不同材料。")


def _apply_requested_output_fallback(
    *,
    request: DocumentAgentRunRequest,
    context: DocumentContext,
    runtime: _DocumentToolRuntime,
) -> None:
    """在“提取需求”明确没有模型条目时做保守的本地兜底。

    这不是第二个模型，也不尝试理解隐含语义：只抽取已读取原文中包含明确要求词的句/行，
    并沿用真实 source_ref。这样模型偶尔只给摘要时，用户选择的输出模式仍有可核验结果；
    没有明确标记时保持为空，避免把普通叙述伪装成需求。
    """

    if request.output_mode != "requirements" or context.requirements:
        return

    fallback_requirements: list[DocumentRequirement] = []
    for source_id, source in runtime.sources.items():
        text = runtime.source_texts.get(source_id, "")
        for line in (item.strip(" -\t#") for item in text.splitlines() if item.strip()):
            if not _contains_explicit_requirement_marker(line):
                continue
            fallback_requirements.append(
                DocumentRequirement(
                    id=f"rule_req_{len(fallback_requirements) + 1:02d}",
                    text=_compact_text(line, 1_200),
                    category=_mock_category(line),
                    priority=_explicit_requirement_priority(line),
                    source_refs=[source],
                    confidence="high",
                )
            )
            if len(fallback_requirements) >= 8:
                break
        if len(fallback_requirements) >= 8:
            break

    if fallback_requirements:
        context.requirements = fallback_requirements
        context.warnings.append(
            "模型未输出结构化需求；已仅依据原文中的明确要求词生成保守条目，请结合来源复核。"
        )


def _normalize_section_draft_context(
    *,
    request: DocumentAgentRunRequest,
    context: DocumentContext,
) -> None:
    """锁定派生预览的章节身份，防止模型把“撰写本章”扩展为改标题或新增其他章节。"""

    if request.output_mode != "section_draft":
        return
    seed = request.section_draft
    if seed is None:
        raise DocumentAgentServiceError("单章节创作缺少已验证的章节种子。")
    if len(context.draft_sections) != 1:
        raise DocumentAgentServiceError("单章节创作必须只返回一个带来源的章节预览。")

    section = context.draft_sections[0]
    # ID 与标题属于用户在原草稿中明确选定的对象身份，不是模型要重新决定的内容。
    section.id = seed.section_id
    section.heading = seed.heading
    context.draft_title = f"{seed.heading} · 分章节创作预览"


def _normalize_draft_review_context(
    *,
    request: DocumentAgentRunRequest,
    context: DocumentContext,
) -> None:
    """核验结论不能趁机替换草稿；始终把原任务的已验证章节原样保留。"""

    if request.output_mode != "draft_review":
        return
    seed = request.draft_review
    if seed is None:
        raise DocumentAgentServiceError("草稿事实核验缺少已验证的草稿快照。")
    context.draft_title = seed.draft_title
    context.draft_sections = list(seed.draft_sections)
    context.review_target_title = f"{seed.draft_title} · 事实核验"
    if seed.requires_reverification:
        # 核验结论必须由本轮重新读取的材料得出。只要还有待确认项，用户编辑后的文字就不能
        # 因为“做过一次核验”而自动获得保存资格。
        if context.open_questions:
            context.draft_verification_state = "reviewed_with_questions"
            context.warnings.append(
                "手动修订已重新核验，但仍有待确认事实；请补充或修改后再次核验，暂不能保存。"
            )
        else:
            context.draft_verification_state = "verified"
            context.warnings.append(
                "手动修订已重新核验，未发现待确认事实；当前预览现在可以另存为新的 Markdown 版本。"
            )


def _normalize_section_review_context(
    *,
    request: DocumentAgentRunRequest,
    context: DocumentContext,
) -> None:
    """锁定审校对象，并拒绝模型点评用户并未选择的原文片段。"""

    if request.output_mode != "section_review":
        return
    seed = request.section_review
    if seed is None:
        raise DocumentAgentServiceError("本章审校缺少已验证的章节种子。")

    normalized_body = re.sub(r"\s+", "", seed.current_body)
    for suggestion in context.revision_suggestions:
        excerpt = re.sub(r"\s+", "", suggestion.original_excerpt)
        if not excerpt or excerpt not in normalized_body:
            raise DocumentAgentServiceError("审校建议引用的原文片段不属于当前选择章节，已拒绝展示。")

    # 与事实核验一样，审校结果永远恢复服务端持有的原草稿快照；模型只有建议权，没有写入权。
    context.draft_title = seed.draft_title
    context.draft_sections = list(seed.draft_sections)
    context.revision_target_section_id = seed.section_id
    context.revision_target_title = f"{seed.heading} · 本章审校"


def _validate_requested_output_context(
    *,
    request: DocumentAgentRunRequest,
    context: DocumentContext,
) -> None:
    """校验用户主动选择的输出形态，避免“完成”却没有对应内容。"""

    if request.output_mode == "brief" and not context.brief_fields:
        raise DocumentAgentServiceError("关键信息卡没有提取到带来源的字段，请改用摘要或补充更具体的材料。")
    if request.output_mode == "outline" and not context.outline_sections:
        raise DocumentAgentServiceError("结构化大纲没有提取到带来源的章节，请补充更具体的材料后重试。")
    if request.output_mode == "draft" and (not context.draft_title or not context.draft_sections):
        raise DocumentAgentServiceError("Markdown 草稿没有生成带来源的标题和章节，请补充更具体的材料后重试。")
    if request.output_mode == "section_draft" and (
        request.section_draft is None or not context.draft_title or len(context.draft_sections) != 1
    ):
        raise DocumentAgentServiceError("本章创作没有生成唯一且带来源的章节预览，请补充更具体的要求后重试。")
    if request.output_mode == "draft_review" and (
        request.draft_review is None or not context.review_target_title or not context.draft_sections
    ):
        raise DocumentAgentServiceError("草稿事实核验没有保留原草稿，请重新打开已完成的草稿后重试。")
    if request.output_mode == "section_review" and (
        request.section_review is None
        or not context.revision_target_title
        or not context.revision_target_section_id
        or not context.draft_sections
    ):
        raise DocumentAgentServiceError("本章审校没有保留原草稿和章节身份，请重新打开已完成的草稿后重试。")
    if request.output_mode == "draft_template" and (
        request.draft_template is None
        or context.template_preview is None
        or not context.draft_title
        or not context.draft_sections
    ):
        raise DocumentAgentServiceError("模板化交付预览没有保留已验证草稿与模板身份，请重新打开结果后重试。")
    if request.output_mode == "draft_merge" and (
        request.draft_merge is None
        or context.merge_preview is None
        or not context.draft_title
        or not context.draft_sections
        or context.draft_verification_state != "verified"
    ):
        raise DocumentAgentServiceError("章节合并预览没有保留已核验版本与冲突选择，请重新打开合并计划后重试。")


def _persist_document_result(
    *,
    task_id: str,
    request: DocumentAgentRunRequest,
    selected_documents: list[str],
    mode: str,
    status: str,
    stop_reason: str,
    reply: str,
    context: DocumentContext,
    tool_traces: tuple[AgentToolTrace, ...],
    turn_count: int,
    started_at: datetime,
    output_format_repair_count: int = 0,
) -> DocumentAgentRunResponse:
    """把 Document Agent 的运行事实压进已有 Workflow/SQLite 观察面。"""

    # 版本链只引用已存在的任务快照，绝不复制正文或修改旧任务。这样历史页和保存产物能解释
    # “这个版本从哪里来”，同时保留用户随时回看旧任务、另存旧版本的安全回退方式。
    version_info = _build_document_draft_version_info(request=request, task_id=task_id)
    if status == "completed" and context.draft_title and context.draft_sections and version_info is not None:
        context.draft_version = version_info

    finished_at = datetime.now(UTC)
    tool_steps, tool_calls = _tool_trace_records(task_id, tool_traces)
    # 直接读取通常不超过 8 次 Tool 调用；长文档压缩的每个连续分块都会形成一条可审计
    # 读取记录，因而这里必须按真实轨迹扩展上限，避免历史页显示的预算小于实际执行量。
    recorded_max_steps = max(12, len(tool_steps) + 1)
    recorded_max_tool_calls = max(8, len(tool_calls))
    final_step_status = "completed" if status == "completed" else "blocked" if status in {"needs_clarification", "insufficient_context"} else "failed"
    is_section_draft = request.output_mode == "section_draft"
    is_draft_review = request.output_mode == "draft_review"
    is_section_review = request.output_mode == "section_review"
    is_section_revision = request.output_mode == "section_revision"
    is_section_revision_batch = request.output_mode == "section_revision_batch"
    is_section_manual_revision = request.output_mode == "section_manual_revision"
    is_draft_restore = request.output_mode == "draft_restore"
    is_draft_template = request.output_mode == "draft_template"
    is_draft_merge = request.output_mode == "draft_merge"
    final_step = WorkflowStepRun(
        step_id="document_analysis",
        agent=DOCUMENT_AGENT_ID,
        action=(
            "create_section_preview"
            if is_section_draft
            else "review_draft_facts"
            if is_draft_review
            else "review_draft_section"
            if is_section_review
            else "create_section_revision_preview"
            if is_section_revision
            else "create_section_revision_batch_preview"
            if is_section_revision_batch
            else "create_section_manual_revision_preview"
            if is_section_manual_revision
            else "restore_draft_preview"
            if is_draft_restore
            else "create_template_delivery_preview"
            if is_draft_template
            else "create_draft_merge_preview"
            if is_draft_merge
            else "analyze_document"
        ),
        status=final_step_status,
        message=reply,
        output={
            "runtime": True,
            "document_context": context.model_dump(mode="json"),
            "reply": reply,
            "stop_reason": stop_reason,
            "agent_status": status,
            "model_mode": mode,
            "model_turn_count": turn_count,
            "output_format_repair_count": output_format_repair_count,
        },
    )
    steps = [*tool_steps, final_step]
    workflow_status = "completed" if status == "completed" else "blocked" if final_step_status == "blocked" else "failed"
    plan = _document_plan(
        request=request,
        selected_documents=selected_documents,
        tool_traces=tool_traces,
    )
    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status=workflow_status,
        summary=reply,
        max_risk_level="low",
        requires_confirmation=False,
        steps=steps,
        model_routes=list(_DOCUMENT_MODEL_ROUTE_AUDITS.get()),
        limits=RuntimeExecutionLimits(
            max_steps=recorded_max_steps,
            max_tool_calls=recorded_max_tool_calls,
            max_retries_per_tool=1,
            tool_timeout_ms=30_000,
            task_timeout_ms=120_000,
        ),
        metrics=RuntimeExecutionMetrics(
            started_at=started_at.isoformat(timespec="seconds"),
            finished_at=finished_at.isoformat(timespec="seconds"),
            duration_ms=max(0, int((finished_at - started_at).total_seconds() * 1000)),
            step_total=len(steps),
            step_completed=sum(1 for step in steps if step.status == "completed"),
            step_failed=sum(1 for step in steps if step.status == "failed"),
            tool_call_total=len(tool_calls),
            tool_call_failed=sum(1 for call in tool_calls if call.status == "failed"),
            # 分块原文会分别进入摘要模型，最终归并只接收其短摘要。两类输入都计入估算，
            # 让用户能辨别长文档任务的真实成本，而不是只看到最后一句任务目标的 token。
            estimated_input_tokens=_rough_token_estimate(request.task_goal) + sum(
                _rough_token_estimate(json.dumps(trace.arguments, ensure_ascii=False))
                + _rough_token_estimate(str(trace.result.get("text", "")))
                + _rough_token_estimate(
                    json.dumps(trace.result.get("context_summary", {}), ensure_ascii=False)
                )
                for trace in tool_traces
            ),
            estimated_output_tokens=_rough_token_estimate(reply),
            budget_exceeded=status in {"budget_exhausted", "max_turns_exceeded"},
        ),
    )
    events = _document_events(task_id, steps, tool_calls, run)
    save_workflow_run(run=run, events=events, plan=plan, tool_calls=tool_calls, artifacts=[])
    # 任务查询层可能刚刚缓存过同 ID；写完后统一清理，确保历史页读到正式 Agent 的 trace。
    clear_dry_run_memory_cache()
    return DocumentAgentRunResponse(
        task_id=task_id,
        mode="llm" if mode == "llm" else "mock",
        status=status,  # type: ignore[arg-type]
        stop_reason=stop_reason,
        reply=reply,
        document_context=context,
        workflow_run=run,
    )


def _tool_trace_records(
    task_id: str,
    traces: tuple[AgentToolTrace, ...],
) -> tuple[list[WorkflowStepRun], list[WorkflowToolCall]]:
    steps: list[WorkflowStepRun] = []
    calls: list[WorkflowToolCall] = []
    for index, trace in enumerate(traces, start=1):
        action = "read_text" if trace.tool_name == "document.read_text" else "search_text"
        step_id = f"document_tool_{index:02d}"
        failed = bool(trace.error_code)
        audit_result = _audit_tool_result(trace.result)
        steps.append(
            WorkflowStepRun(
                step_id=step_id,
                agent=DOCUMENT_AGENT_ID,
                action=action,
                status="failed" if failed else "completed",
                message=trace.error_message if failed else f"已完成 {trace.tool_name}。",
                output={
                    "runtime": True,
                    "tool_name": trace.tool_name,
                    "result": audit_result,
                    "error": {"code": trace.error_code, "message": trace.error_message} if failed else {},
                },
            )
        )
        calls.append(
            WorkflowToolCall(
                call_id=f"{task_id}:{trace.call_id}",
                task_id=task_id,
                step_id=step_id,
                agent_id=DOCUMENT_AGENT_ID,
                tool_name=trace.tool_name,
                status="failed" if failed else "completed",
                risk_level="low",
                permission_required=False,
                attempt=1,
                max_attempts=2,
                timeout_ms=30_000,
                failure_count=1 if failed else 0,
                request=trace.arguments,
                result=audit_result,
                error=trace.error_message,
            )
        )
    return steps, calls


def _audit_tool_result(value: dict[str, Any]) -> dict[str, Any]:
    """缩短审计副本，避免把长文本片段重复写入 SQLite 与历史 UI。"""

    result = dict(value)
    text = result.get("text")
    if isinstance(text, str):
        result["text"] = _compact_text(text, _AUDIT_EXCERPT_MAX_CHARS)
        result["text_truncated_for_audit"] = len(text) > _AUDIT_EXCERPT_MAX_CHARS
    return result


def _document_plan(
    *,
    request: DocumentAgentRunRequest,
    selected_documents: list[str],
    tool_traces: tuple[AgentToolTrace, ...],
) -> WorkflowPlan:
    preferences = load_runtime_preferences().to_workflow_preferences()
    steps: list[WorkflowStep] = []
    for index, trace in enumerate(tool_traces, start=1):
        action = "read_text" if trace.tool_name == "document.read_text" else "search_text"
        steps.append(
            WorkflowStep(
                id=f"document_tool_{index:02d}",
                agent=DOCUMENT_AGENT_ID,
                action=action,
                title="读取文档" if action == "read_text" else "搜索文档",
                input=trace.arguments,
                reason="由文档助手在受控 workspace 内按需获取来源材料。",
                expected_output=(
                    "连续文本片段或受控分块摘要与可验证来源 ID。"
                    if trace.arguments.get("context_strategy") == "chunk_summary"
                    else "受限文本片段与可验证来源 ID。"
                ),
                required_permissions=["file_read"],
                tool_name=trace.tool_name,
                success_criteria=["只访问本次选择的受控文档", "返回来源引用"],
                timeout_ms=30_000,
                retry_policy=WorkflowRetryPolicy(max_attempts=2, retryable=False),
            )
        )
    is_section_draft = request.output_mode == "section_draft"
    is_draft_review = request.output_mode == "draft_review"
    is_section_review = request.output_mode == "section_review"
    is_section_revision = request.output_mode == "section_revision"
    is_section_revision_batch = request.output_mode == "section_revision_batch"
    is_section_manual_revision = request.output_mode == "section_manual_revision"
    is_draft_restore = request.output_mode == "draft_restore"
    is_draft_template = request.output_mode == "draft_template"
    is_draft_merge = request.output_mode == "draft_merge"
    section_seed = request.section_draft
    review_seed = request.draft_review
    section_review_seed = request.section_review
    section_revision_seed = request.section_revision
    section_revision_batch_seed = request.section_revision_batch
    section_manual_revision_seed = request.section_manual_revision
    draft_restore_seed = request.draft_restore
    draft_template_seed = request.draft_template
    draft_merge_seed = request.draft_merge
    steps.append(
        WorkflowStep(
            id="document_analysis",
            agent=DOCUMENT_AGENT_ID,
            action=(
                "create_section_preview"
                if is_section_draft
                else "review_draft_facts"
                if is_draft_review
                else "review_draft_section"
                if is_section_review
                else "create_section_revision_preview"
                if is_section_revision
                else "create_section_revision_batch_preview"
                if is_section_revision_batch
                else "create_section_manual_revision_preview"
                if is_section_manual_revision
                else "restore_draft_preview"
                if is_draft_restore
                else "create_template_delivery_preview"
                if is_draft_template
                else "create_draft_merge_preview"
                if is_draft_merge
                else "analyze_document"
            ),
            title=(
                "生成单章节创作预览"
                if is_section_draft
                else "核验草稿事实"
                if is_draft_review
                else "审校草稿章节"
                if is_section_review
                else "生成章节修订预览"
                if is_section_revision
                else "生成多建议合并修订预览"
                if is_section_revision_batch
                else "建立待核验的手动修订预览"
                if is_section_manual_revision
                else "建立历史草稿恢复预览"
                if is_draft_restore
                else "建立章节合并预览"
                if is_draft_merge
                else "生成结构化文档结论"
            ),
            depends_on=[step.id for step in steps],
            input={
                "task_goal": request.task_goal,
                "output_mode": request.output_mode,
                # 任务历史只记录被选章节的稳定 ID，不重复存放原正文或用户长指令。
                "source_section_id": section_seed.section_id if section_seed else "",
                # 两类审校都不在计划副本中重复草稿正文，只保留可辨识的章节或草稿身份。
                "review_target_section_id": section_review_seed.section_id if section_review_seed else "",
                "review_target_title": (
                    section_review_seed.heading if section_review_seed else review_seed.draft_title if review_seed else ""
                ),
                "source_review_task_id": (
                    section_revision_seed.source_review_task_id if section_revision_seed else ""
                ),
                "revision_suggestion_id": section_revision_seed.suggestion_id if section_revision_seed else "",
                "revision_suggestion_ids": (
                    list(section_revision_batch_seed.suggestion_ids)
                    if section_revision_batch_seed
                    else []
                ),
                # 手动正文保留在任务结果快照中，计划仅审计章节/版本身份，避免重复写入用户文本。
                "manual_revision_source_task_id": (
                    section_manual_revision_seed.source_task_id if section_manual_revision_seed else ""
                ),
                "manual_revision_source_version_id": (
                    section_manual_revision_seed.source_version_id if section_manual_revision_seed else ""
                ),
                "manual_revision_section_id": (
                    section_manual_revision_seed.section_id if section_manual_revision_seed else ""
                ),
                # 恢复只记录历史任务/版本身份，不能把正文、文件名或目录复制进计划输入。
                "restore_source_task_id": draft_restore_seed.source_task_id if draft_restore_seed else "",
                "restore_source_version_id": draft_restore_seed.source_version_id if draft_restore_seed else "",
                # 模板交付同样只审计历史快照身份与固定模板 ID，正文和来源继续由 SQLite
                # 中的已验证快照恢复，不能经由计划输入二次写入。
                "template_source_task_id": draft_template_seed.source_task_id if draft_template_seed else "",
                "template_source_version_id": draft_template_seed.source_version_id if draft_template_seed else "",
                "template_id": draft_template_seed.template_id if draft_template_seed else "",
                # 合并计划只审计两个版本身份与已确认的冲突 ID/选择，不重复写入任意章节正文。
                "merge_primary_task_id": draft_merge_seed.primary_task_id if draft_merge_seed else "",
                "merge_secondary_task_id": draft_merge_seed.secondary_task_id if draft_merge_seed else "",
                "merge_primary_version_id": draft_merge_seed.primary_version_id if draft_merge_seed else "",
                "merge_secondary_version_id": draft_merge_seed.secondary_version_id if draft_merge_seed else "",
                "merge_resolution_ids": (
                    [item.conflict_id for item in draft_merge_seed.resolutions]
                    if draft_merge_seed
                    else []
                ),
            },
            reason=(
                "重新读取原材料后生成独立的单章节预览，不修改原草稿或文件。"
                if is_section_draft
                else "重新读取原材料，只核验草稿中的表述与来源，不修改正文或文件。"
                if is_draft_review
                else "重新读取原材料，审校所选章节并返回候选建议，不修改正文或文件。"
                if is_section_review
                else "精确应用一条已审校候选建议，生成独立预览；不调用模型、不修改原草稿或文件。"
                if is_section_revision
                else "精确合并多条已审校且不重叠的候选建议；不调用模型、不修改原草稿或文件。"
                if is_section_revision_batch
                else "把用户手动编辑的本章正文绑定到历史草稿快照，建立待重新核验的独立预览；不调用模型、Tool，不修改原草稿或文件。"
                if is_section_manual_revision
                else "从已完成历史快照建立独立恢复预览；不调用模型、Tool，不修改原草稿或文件。"
                if is_draft_restore
                else "将已核验草稿按固定交付模板重组；不调用模型、Tool，不读取材料或补写未知事实。"
                if is_draft_template
                else "从同根已核验版本与共同祖先建立三方章节合并预览；冲突只采用用户显式选择。"
                if is_draft_merge
                else "基于实际 Tool 结果生成带来源的摘要、需求和直接回答。"
            ),
            expected_output=(
                "一个带来源的单章节 Markdown 预览。"
                if is_section_draft
                else "材料可支持的表述与待确认问题，且保留原草稿快照。"
                if is_draft_review
                else "带来源的本章问题、原文片段与候选建议，且保留原草稿快照。"
                if is_section_review
                else "修订前后差异与可另存的完整草稿版本；原草稿保持不变。"
                if is_section_revision
                else "多建议合并后的前后差异与可另存的完整草稿版本；原草稿保持不变。"
                if is_section_revision_batch
                else "手动修订的章节前后差异与待核验草稿版本；未重新核验前不能保存。"
                if is_section_manual_revision
                else "可另存的完整恢复草稿版本；历史快照和已保存文件保持不变。"
                if is_draft_restore
                else "带来源的模板化 Markdown 交付预览，以及明确列出的待补充章节。"
                if is_draft_template
                else "带来源的章节合并 Markdown 预览，以及共同祖先和冲突处理摘要。"
                if is_draft_merge
                else "agentflow.document_context.v1 与用户可读结论。"
            ),
            tool_name="agent.document_agent.analyze",
            success_criteria=["输出通过 Pydantic 契约校验", "关键结论可映射到来源 ID"],
            timeout_ms=120_000,
            retry_policy=WorkflowRetryPolicy(max_attempts=1, retryable=False),
        )
    )
    return WorkflowPlan(
        workflow_name="document_agent_run",
        description="文档助手正式只读 Agent 运行记录。",
        intent="document",
        user_goal=request.task_goal,
        summary="文档助手读取受控材料并生成带来源的结构化结论。",
        definition_of_done=["文档范围明确", "结论有可验证来源", "结果可在任务历史复盘"],
        preference_applied=WorkflowPlanPreferences.model_validate(preferences.model_dump()),
        budget_estimate=WorkflowBudgetEstimate(
            step_count=len(steps),
            time_level="medium" if len(tool_traces) > 4 else "low",
            model_cost_level=(
                "medium"
                if any(trace.arguments.get("context_strategy") == "chunk_summary" for trace in tool_traces)
                else "low"
            ),
        ),
        workspace_scope=WorkflowWorkspaceScope(
            read_paths=[]
            if is_draft_restore or is_section_manual_revision or is_draft_template or is_draft_merge
            else [f"data/workspaces/{document}" for document in selected_documents],
            notes=(
                "本次只从已完成任务恢复 Pydantic 已验证的草稿快照，不读取 workspace 文件、不写入原文或 outputs。"
                if is_draft_restore
                else "本次只建立用户手动修订的待核验版本预览，不读取 workspace 文件、不写入原文或 outputs。"
                if is_section_manual_revision
                else "本次只从已核验草稿快照建立模板化交付预览，不读取 workspace 文件、不写入原文或 outputs。"
                if is_draft_template
                else "本次只从两个已核验版本与共同祖先建立章节合并预览，不读取 workspace 文件、不写入原文或 outputs。"
                if is_draft_merge
                else "文档助手只读本次明确选择的 workspace 文档，不写入原文或 outputs。"
            ),
        ),
        steps=steps,
        next_action="completed",
    )


def _document_events(
    task_id: str,
    steps: list[WorkflowStepRun],
    calls: list[WorkflowToolCall],
    run: WorkflowRun,
) -> list[TaskLogEvent]:
    events = [
        TaskLogEvent(task_id=task_id, sequence=1, event="connected", agent_id="system", message="已连接文档助手运行通道。"),
        TaskLogEvent(task_id=task_id, sequence=2, event="task_started", agent_id=DOCUMENT_AGENT_ID, message="文档助手开始受控分析。"),
    ]
    sequence = 3
    calls_by_step = {call.step_id: call for call in calls}
    for step in steps:
        if step.step_id == "document_analysis":
            continue
        events.append(TaskLogEvent(task_id=task_id, sequence=sequence, event="step_started", agent_id=DOCUMENT_AGENT_ID, step_id=step.step_id, message=f"开始 {step.action}。"))
        sequence += 1
        call = calls_by_step.get(step.step_id)
        events.append(TaskLogEvent(task_id=task_id, sequence=sequence, event="tool_failed" if step.status == "failed" else "tool_completed", agent_id=DOCUMENT_AGENT_ID, step_id=step.step_id, level="error" if step.status == "failed" else "info", message=step.message))
        sequence += 1
        events.append(TaskLogEvent(task_id=task_id, sequence=sequence, event="step_failed" if step.status == "failed" else "step_completed", agent_id=DOCUMENT_AGENT_ID, step_id=step.step_id, level="error" if step.status == "failed" else "info", message=step.message))
        sequence += 1
        del call
    final_step = steps[-1]
    events.append(TaskLogEvent(task_id=task_id, sequence=sequence, event="step_failed" if final_step.status == "failed" else "step_completed", agent_id=DOCUMENT_AGENT_ID, step_id=final_step.step_id, level="error" if final_step.status == "failed" else "info", message=final_step.message))
    sequence += 1
    terminal_event = "task_completed" if run.status == "completed" else "task_failed" if run.status == "failed" else "task_waiting"
    events.append(TaskLogEvent(task_id=task_id, sequence=sequence, event=terminal_event, agent_id=DOCUMENT_AGENT_ID, level="error" if run.status == "failed" else "warning" if run.status == "blocked" else "info", message=run.summary))
    return events


def _mock_document_output_json(*, request: DocumentAgentRunRequest, tool_results: list[dict[str, Any]]) -> str:
    """让离线验证也遵守真实的来源契约，而不是绕开多文档校验。"""

    records: list[tuple[str, str, str]] = []
    for tool_result in tool_results:
        source = tool_result.get("source") if isinstance(tool_result.get("source"), dict) else None
        source_id = str(source.get("source_id", "")) if source else ""
        relative_path = str(source.get("relative_path", "")) if source else ""
        text = str(tool_result.get("text", ""))
        if not source_id:
            matches = tool_result.get("matches")
            if isinstance(matches, list) and matches and isinstance(matches[0], dict):
                nested_source = matches[0].get("source")
                if isinstance(nested_source, dict):
                    source_id = str(nested_source.get("source_id", ""))
                    relative_path = str(nested_source.get("relative_path", ""))
                text = str(matches[0].get("preview", ""))
        if source_id:
            records.append((source_id, relative_path, text))

    source_id = records[-1][0] if records else ""
    text = records[-1][2] if records else ""
    # 分块压缩时一份文档会对应多个来源。跨文档结果必须选取不同文件的代表来源，不能因为
    # 同一文件有两个 chunk 就错误通过“至少两份材料”的 Guardrail。
    distinct_records: list[tuple[str, str, str]] = []
    seen_documents: set[str] = set()
    for record in records:
        if record[1] and record[1] not in seen_documents:
            distinct_records.append(record)
            seen_documents.add(record[1])
    lines = [line.strip(" -\t#") for line in text.splitlines() if line.strip()]
    selected_lines = [
        line
        for line in lines
        if _contains_explicit_requirement_marker(line) or "功能" in line or "TODO" in line
    ]
    if not selected_lines:
        selected_lines = lines[:3]
    requirements: list[dict[str, Any]] = []
    brief_fields: list[dict[str, Any]] = []
    outline_sections: list[dict[str, Any]] = []
    draft_title = ""
    draft_sections: list[dict[str, Any]] = []
    review_constraints: list[dict[str, Any]] = []
    revision_suggestions: list[dict[str, Any]] = []
    if request.output_mode == "brief":
        brief_fields = _mock_brief_fields(records)
    if request.output_mode == "outline":
        outline_sections = _mock_outline_sections(records)
    if request.output_mode == "draft":
        draft_title, draft_sections = _mock_draft_preview(records)
    if request.output_mode == "section_draft":
        draft_title, draft_sections = _mock_section_draft_preview(records, request.section_draft)
    if request.output_mode == "draft_review":
        review_constraints = [
            {
                "text": f"原材料明确包含可支撑草稿核验的内容：{_compact_text(line, 500)}",
                "source_ids": [source_id],
                "confidence": "medium",
            }
            for line in selected_lines[:2]
            if source_id
        ]
    if request.output_mode == "section_review":
        revision_suggestions = _mock_section_review_suggestions(
            records,
            request.section_review,
        )
    if request.output_mode == "synthesis":
        # mock 也要体现“整合”而不是把最后读取的一份材料当作合并结果：完全相同的
        # 可见条目会归为同一 requirement，并累积不同文件的来源 ID。真实模式仍由模型
        # 负责语义层面的同义归并，确定性路径只做无歧义的字面归并。
        merged_requirements: dict[str, dict[str, Any]] = {}
        for item_source_id, _relative_path, item_text in distinct_records:
            item_lines = [line.strip(" -\t#") for line in item_text.splitlines() if line.strip()]
            candidate_lines = [
                line
                for line in item_lines
                if _contains_explicit_requirement_marker(line) or "功能" in line or "TODO" in line
            ] or item_lines[:3]
            for line in candidate_lines[:5]:
                key = " ".join(line.split()).lower()
                if not key:
                    continue
                existing = merged_requirements.get(key)
                if existing is not None:
                    source_ids = existing["source_ids"]
                    if item_source_id not in source_ids and len(source_ids) < 4:
                        source_ids.append(item_source_id)
                    continue
                if len(merged_requirements) >= 12:
                    break
                merged_requirements[key] = {
                    "id": f"merged_req_{len(merged_requirements) + 1:02d}",
                    "text": _compact_text(line, 1_200),
                    "category": _mock_category(line),
                    "priority": _explicit_requirement_priority(line),
                    "source_ids": [item_source_id],
                    "confidence": "medium",
                }
            if len(merged_requirements) >= 12:
                break
        requirements = list(merged_requirements.values())
    elif request.output_mode not in {
        "qa",
        "cross_qa",
        "brief",
        "outline",
        "draft",
        "section_draft",
        "draft_review",
        "section_review",
    }:
        for index, line in enumerate(selected_lines[:5], start=1):
            requirements.append(
                {
                    "id": f"req_{index:02d}",
                    "text": _compact_text(line, 1_200),
                    "category": _mock_category(line),
                    "priority": _explicit_requirement_priority(line),
                    "source_ids": [source_id],
                    "confidence": "medium",
                }
            )

    comparisons: list[dict[str, Any]] = []
    comparison_source_ids: list[str] = []
    if request.output_mode == "comparison" and len(records) >= 2:
        comparison_source_ids = [record[0] for record in distinct_records[:4]]
        requirement_lines: dict[str, list[tuple[str, str]]] = {}
        for item_source_id, item_path, item_text in records:
            item_lines = [line.strip(" -\t#") for line in item_text.splitlines() if line.strip()]
            item_lines = [
                line
                for line in item_lines
                if _contains_explicit_requirement_marker(line) or "功能" in line or "TODO" in line
            ] or item_lines[:3]
            for line in item_lines[:6]:
                key = " ".join(line.split()).lower()
                if key:
                    requirement_lines.setdefault(key, []).append((item_source_id, item_path))
        for line, line_sources in requirement_lines.items():
            source_by_document: dict[str, str] = {}
            for item_source_id, item_path in line_sources:
                source_by_document.setdefault(item_path, item_source_id)
            if len(source_by_document) >= 2:
                comparisons.append(
                    {
                        "dimension": "共同要求",
                        "kind": "common",
                        "summary": f"多份材料均明确提到：{_compact_text(line, 600)}",
                        "source_ids": list(source_by_document.values())[:4],
                        "confidence": "high",
                    }
                )
                break
        if not comparisons:
            comparisons.append(
                {
                    "dimension": "材料范围",
                    "kind": "difference",
                    "summary": "已读取全部所选材料；在本次可见片段中没有发现完全相同的明确要求，建议结合下方来源继续核验具体差异。",
                    "source_ids": comparison_source_ids,
                    "confidence": "medium",
                }
            )
        for item_source_id, relative_path, item_text in distinct_records[:3]:
            item_lines = [line.strip(" -\t#") for line in item_text.splitlines() if line.strip()]
            candidate = next(
                (
                    line
                    for line in item_lines
                    if _contains_explicit_requirement_marker(line) or "功能" in line or "TODO" in line
                ),
                "",
            )
            if candidate:
                comparisons.append(
                    {
                    "dimension": f"{relative_path or '材料'}的侧重点",
                    "kind": "difference",
                    "summary": f"在本次已读取材料范围内，该材料强调：{_compact_text(candidate, 600)}",
                    "source_ids": comparison_source_ids,
                    "confidence": "low",
                }
            )

    summary = _compact_text("；".join(lines[:3]), 420) or "文档内容为空或没有可提取的文本。"
    answer = f"已基于受控文档完成分析：{summary}"
    if request.output_mode == "comparison":
        answer = f"已完成 {len(distinct_records)} 份受控材料的对比，并给出可回溯的共识与差异线索。"
    elif request.output_mode == "cross_qa":
        answer = (
            f"已基于 {len(distinct_records)} 份受控材料回答本次问题；"
            "结论依据已覆盖多份已读取材料，请结合来源继续核验。"
        )
    elif request.output_mode == "synthesis":
        # f-string 表达式中不能直接写 ``'\\t'`` 这类反斜杠转义；先在普通 Python 表达式
        # 中取出每份材料的首个可见文本，既保持原来的保守降级逻辑，也让模块能被解释器加载。
        first_visible_records = [
            (
                relative_path,
                next(
                    (line.strip(" -\t#") for line in item_text.splitlines() if line.strip()),
                    "未提取到可见文本",
                ),
            )
            for _item_source_id, relative_path, item_text in distinct_records
        ]
        summary = _compact_text(
            "；".join(
                f"{relative_path or '材料'}：{first_text}"
                for relative_path, first_text in first_visible_records
            ),
            420,
        ) or summary
        answer = (
            f"已整合 {len(distinct_records)} 份受控材料，形成 {len(requirements)} 条"
            "可追溯条目；请结合待确认事项复核可能冲突的表述。"
        )
    elif request.output_mode == "brief":
        answer = (
            f"已按关键信息卡整理出 {len(brief_fields)} 项可追溯字段；"
            "未在材料中明确表达的内容不会补写。"
        )
    elif request.output_mode == "outline":
        answer = (
            f"已整理出 {len(outline_sections)} 个可追溯大纲章节；"
            "这是只读审阅蓝图，确认后再讨论分章节撰写与文件交付。"
        )
    elif request.output_mode == "draft":
        answer = (
            f"已生成“{draft_title}”的 {len(draft_sections)} 个可追溯草稿章节；"
            "当前仅为审阅预览，尚未创建、覆盖或导出文件。"
        )
    elif request.output_mode == "section_draft":
        answer = (
            "已基于重新读取的受控材料生成本章创作预览；"
            "它不会修改原草稿或已保存文件，请审阅来源后再另存为。"
        )
    elif request.output_mode == "section_review":
        answer = (
            f"已完成本章只读审校，给出 {len(revision_suggestions)} 条可追溯建议；"
            "原草稿和已保存文件均未改动。"
        )
    elif request.query.strip():
        answer = f"已围绕“{request.query.strip()}”定位并分析文档：{summary}"
    answer_source_ids = [record[0] for record in records[:4]]
    if _uses_multiple_documents(request.output_mode):
        answer_source_ids = [record[0] for record in distinct_records[:4]]
    return json.dumps(
        {
            "answer": answer,
            "answer_source_ids": answer_source_ids or [source_id],
            "summary": summary,
            "brief_fields": brief_fields,
            "outline_sections": outline_sections,
            "draft_title": draft_title,
            "draft_sections": draft_sections,
            "revision_suggestions": revision_suggestions,
            "requirements": requirements,
            "comparisons": comparisons,
            "constraints": review_constraints,
            "todos": [],
            "entities": [],
            "open_questions": [],
            "confidence": "medium" if source_id else "low",
        },
        ensure_ascii=False,
    )


def _mock_brief_fields(records: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
    """为离线模式生成不猜测的关键信息卡，和真实模型共用固定字段边界。"""

    fields: list[dict[str, Any]] = []
    subject_added = False
    for source_id, _relative_path, text in records:
        lines = [line.strip(" -\t#") for line in text.splitlines() if line.strip()]
        if not subject_added and lines:
            # 标题通常比正文首句更适合作为材料主题；没有 Markdown 标题时才退回首个可见文本。
            raw_heading = next((line for line in text.splitlines() if line.lstrip().startswith("#")), "")
            subject = raw_heading.lstrip("#").strip() or lines[0]
            fields.append(
                {
                    "key": "subject",
                    "value": _compact_text(subject, 1_200),
                    "source_ids": [source_id],
                    "confidence": "medium",
                }
            )
            subject_added = True

        normalized_lines = [(line, line.lower()) for line in lines]
        for key, markers in _BRIEF_FIELD_MARKERS:
            if any(field["key"] == key for field in fields):
                continue
            candidate = next(
                (
                    line
                    for line, normalized in normalized_lines
                    if any(marker in line or marker in normalized for marker in markers)
                ),
                "",
            )
            if not candidate:
                continue
            fields.append(
                {
                    "key": key,
                    "value": _compact_text(candidate, 1_200),
                    "source_ids": [source_id],
                    "confidence": "medium",
                }
            )
        if len(fields) >= 7:
            break
    return fields


def _mock_outline_sections(records: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
    """为离线模式生成来源驱动的大纲，不把通用模板伪装为材料内容。"""

    if not records:
        return []

    source_id, _relative_path, text = records[-1]
    lines = [line.strip(" -\t#") for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    def marked(*markers: str) -> list[str]:
        return [
            line
            for line in lines
            if any(marker in line or marker in line.lower() for marker in markers)
        ][:4]

    groups = [
        (
            "概览与目标",
            "说明材料的主题、背景与预期目标。",
            marked("目标", "目的", "用于", "背景", "goal", "purpose") or lines[:3],
        ),
        (
            "范围与交付",
            "归纳材料明确的范围、边界、功能或交付物。",
            marked("范围", "边界", "功能", "交付", "输出", "scope", "deliverable", "output"),
        ),
        (
            "计划、风险与待确认",
            "汇总材料中明确的时间节点、依赖、风险和待确认事项。",
            marked("时间", "日期", "阶段", "计划", "风险", "依赖", "待确认", "milestone", "risk", "dependency"),
        ),
    ]
    sections: list[dict[str, Any]] = []
    for title, intent, points in groups:
        if not points:
            continue
        sections.append(
            {
                "id": f"section_{len(sections) + 1:02d}",
                "title": title,
                "intent": intent,
                "key_points": [_compact_text(point, 600) for point in points[:6]],
                "source_ids": [source_id],
                "confidence": "medium",
            }
        )
    return sections


def _mock_draft_preview(records: list[tuple[str, str, str]]) -> tuple[str, list[dict[str, Any]]]:
    """为离线验证生成来源驱动的草稿预览，不模拟文件保存或自由扩写。"""

    if not records:
        return "", []

    source_id, _relative_path, text = records[-1]
    lines = [line.strip(" -\t#") for line in text.splitlines() if line.strip()]
    if not lines:
        return "", []

    raw_heading = next((line for line in text.splitlines() if line.lstrip().startswith("#")), "")
    title = _compact_text(raw_heading.lstrip("#").strip() or lines[0], 240)

    def marked(*markers: str) -> list[str]:
        return [
            line
            for line in lines
            if any(marker in line or marker in line.lower() for marker in markers)
        ][:3]

    groups = [
        (
            "项目背景与目标",
            marked("目标", "目的", "用于", "背景", "goal", "purpose") or lines[:2],
        ),
        (
            "范围与交付",
            marked("范围", "边界", "功能", "交付", "输出", "scope", "deliverable", "output"),
        ),
        (
            "计划与风险",
            marked("时间", "日期", "阶段", "计划", "风险", "依赖", "milestone", "risk", "dependency"),
        ),
    ]
    sections: list[dict[str, Any]] = []
    for heading, points in groups:
        if not points:
            continue
        sections.append(
            {
                "id": f"draft_{len(sections) + 1:02d}",
                "heading": heading,
                "body": "\n\n".join(_compact_text(point, 600) for point in points),
                "source_ids": [source_id],
                "confidence": "medium",
            }
        )
    return title, sections


def _mock_section_draft_preview(
    records: list[tuple[str, str, str]],
    seed: DocumentDraftSectionSeed | None,
) -> tuple[str, list[dict[str, Any]]]:
    """离线模式也按派生章节协议工作，避免测试绕开“单章节、带来源、不落盘”边界。"""

    if not records or seed is None:
        return "", []

    source_id, _relative_path, text = records[-1]
    visible_lines = [line.strip(" -\t#") for line in text.splitlines() if line.strip()]
    evidence_parts = [seed.current_body.strip()]
    for line in visible_lines:
        compact_line = _compact_text(line, 360)
        if compact_line and compact_line not in evidence_parts:
            evidence_parts.append(compact_line)
        if len(evidence_parts) >= 4:
            break
    body = _compact_text("\n\n".join(evidence_parts), 1_500)
    return (
        f"{seed.heading} · 分章节创作预览",
        [
            {
                "id": seed.section_id,
                "heading": seed.heading,
                "body": body,
                "source_ids": [source_id],
                "confidence": "medium",
            }
        ],
    )


def _mock_section_review_suggestions(
    records: list[tuple[str, str, str]],
    seed: DocumentDraftSectionReviewSeed | None,
) -> list[dict[str, Any]]:
    """为离线验收生成最多两条无重叠的只读审校建议，不修改章节快照。"""

    if not records or seed is None:
        return []
    source_id = records[-1][0]
    suggestions: list[dict[str, Any]] = []
    # 分段产生候选，确保离线批量预览覆盖的原文片段天然不重叠；真实模型路径仍由上层
    # Guardrail 重新计算范围，不能因为 mock 的保证而跳过安全校验。
    paragraphs = [item.strip() for item in seed.current_body.split("\n\n") if item.strip()]
    for paragraph in paragraphs[:2]:
        # 这里不能用 _compact_text：它在截断时会追加省略号，进而不再是原章节中的真实片段。
        original_excerpt = paragraph[:300].rstrip()
        suggested_text = re.sub(r"[ \t]{2,}", " ", original_excerpt)
        if suggested_text == original_excerpt:
            for old, new in (("，", "；"), ("。", "；"), ("、", "，")):
                if old in original_excerpt:
                    suggested_text = original_excerpt.replace(old, new, 1)
                    break
        if suggested_text == original_excerpt:
            continue
        suggestions.append(
            {
                "id": f"review_{len(suggestions) + 1:02d}",
                "severity": "suggestion",
                "category": "clarity",
                "original_excerpt": original_excerpt,
                "suggested_text": suggested_text,
                "reason": "建议统一本章的段落或标点节奏，便于读者区分原材料中的不同事实；该建议尚未应用。",
                "source_ids": [source_id],
                "confidence": "medium",
            }
        )
    return suggestions


def _mock_category(text: str) -> str:
    normalized = text.lower()
    if "验收" in text or "测试" in text or "acceptance" in normalized or "test" in normalized:
        return "acceptance"
    if "约束" in text or "限制" in text or "不得" in text or "constraint" in normalized or "must not" in normalized:
        return "constraint"
    if "输出" in text or "交付" in text or "output" in normalized or "deliver" in normalized:
        return "output"
    if "功能" in text or "需要" in text or "必须" in text or any(
        marker in normalized for marker in ("must", "shall", "should", "required", "requirement")
    ):
        return "functional"
    return "unknown"


def _contains_explicit_requirement_marker(text: str) -> bool:
    """只识别原文可见的中英文硬标记，作为模型失效时的保守需求依据。"""

    normalized = text.lower()
    return any(marker in text for marker in _EXPLICIT_REQUIREMENT_MARKERS) or any(
        marker in normalized for marker in _EXPLICIT_REQUIREMENT_MARKERS_EN
    )


def _explicit_requirement_priority(text: str) -> str:
    """把明确标记映射为稳定枚举，不在确定性降级阶段臆测优先级。"""

    normalized = text.lower()
    if "必须" in text or "不得" in text or any(
        marker in normalized for marker in ("must", "must not", "shall", "required")
    ):
        return "must"
    if "需要" in text or "应" in text or "should" in normalized:
        return "should"
    return "unknown"


def _tool_message_payload(content: str) -> dict[str, Any]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _failure_reply(message: str, stop_reason: str) -> str:
    if stop_reason == "max_turns_exceeded":
        return "文档助手已达到本次分析轮次上限。请缩小问题范围，或选择更具体的文档后重试。"
    if stop_reason == "max_tool_calls_exceeded":
        return "文档助手已达到本次工具调用上限。请缩小文档范围或关键词后重试。"
    if stop_reason == "model_timeout":
        return "当前模型响应超时，本次分析已安全停止。请稍后重试、缩小材料范围，或在模型密钥页选择更快的模型。"
    if stop_reason == "model_connection_failed":
        return "当前无法连接模型服务，本次分析已安全停止。请检查网络、Base URL 和模型密钥配置后重试。"
    return f"文档助手未能完成分析：{message or '请检查文档、关键词或模型配置后重试。'}"


def _source_location_text(source: DocumentSourceRef) -> str:
    """返回给用户和模型的统一来源文字，兼容旧任务只有行号的历史记录。"""

    if source.source_locator:
        return source.source_locator
    return (
        f"第 {source.start_line} 行"
        if source.start_line == source.end_line
        else f"第 {source.start_line}-{source.end_line} 行"
    )


def _compact_text(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else normalized[: max(1, limit - 3)] + "..."


def _rough_token_estimate(value: str) -> int:
    return max(1, (len(value) + 3) // 4) if value else 0
