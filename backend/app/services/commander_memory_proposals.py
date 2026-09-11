"""确认式长期记忆候选的提取与持久化边界。

候选绝不由模型自由归纳，也不会在发现后直接写入正式长期记忆。这里仅处理客户明确的长期
表达，以及已经完成 Runtime 佐证的项目约束；候选账本负责去重、拒绝、替代和确认幂等。
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re

from app.database.memory_repository import create_or_reuse_long_term_memory_proposal
from app.schemas.chat import WorkflowPlan
from app.schemas.memory import LongTermMemoryProposal
from app.schemas.workflow import WorkflowArtifact, WorkflowRun
from app.services.long_term_memory import (
    LongTermMemorySafetyError,
    build_memory_conflict_key,
    normalize_memory_scope,
    normalize_memory_tags,
    sanitize_memory_text,
)


_MAX_PROPOSALS_PER_SOURCE = 3
_DURABLE_SIGNAL_PATTERN = re.compile(
    r"(?:以后|今后|后续|长期|始终|一直|默认|每次|一律|固定|"
    r"未来.{0,6}(?:都|需要|统一)|项目(?:中|内)?.{0,8}(?:都|统一|默认))"
)
_DURABLE_NEGATION_PATTERN = re.compile(
    r"(?:一次性|本次).{0,32}(?:不|无需|不要|禁止).{0,16}(?:长期|偏好|记忆)|"
    r"(?:不|无需|不要|禁止).{0,16}(?:保存|写入|作为|当作).{0,16}(?:长期|偏好|记忆)"
)
_SENTENCE_SPLIT_PATTERN = re.compile(r"[。！？!?；;\n]+")
_FORMAT_PATTERN = re.compile(r"\b(?:PPTX?|DOCX?|PDF|XLSX?|CSV|Markdown)\b", re.IGNORECASE)


@dataclass(frozen=True)
class MemoryProposalDraft:
    """尚未入库的候选草稿，只含允许写入候选账本的短事实。"""

    task_id: str
    kind: str
    suggested_scope: str
    title: str
    summary: str
    tags: list[str]
    reason: str
    source_type: str
    source_id: str
    source_conversation_id: str | None = None

    @property
    def conflict_key(self) -> str:
        return build_memory_conflict_key(
            kind=self.kind,
            scope=self.suggested_scope,
            title=self.title,
        )

    @property
    def fingerprint(self) -> str:
        canonical = "\n".join(
            (
                self.kind,
                self.suggested_scope,
                self.conflict_key,
                " ".join(self.summary.lower().split()),
            )
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def proposal_id(self) -> str:
        return f"memory_proposal_{self.fingerprint[:16]}"


def prepare_pre_compaction_memory_proposals(
    *,
    task_id: str,
    project_scope: str,
    conversation_id: str,
    user_message: str,
) -> list[MemoryProposalDraft]:
    """在消息归档压缩前提取客户明确的长期表达。

    调用方必须先完成正常聊天响应，再在归档成功后调用 ``persist_memory_proposal_drafts``。
    因此提取能够看到即将被压缩的原句，但响应/归档失败不会留下孤立候选。
    """

    return _build_drafts_from_text(
        task_id=task_id,
        project_scope=project_scope,
        source_text=user_message,
        source_type="explicit_user",
        source_id=conversation_id,
        source_conversation_id=conversation_id,
        reason="仅识别客户明确的长期表达；尚未写入长期记忆，可在确认前编辑范围、标题、摘要和标签。",
    )


def ensure_completed_task_memory_proposals(
    *,
    task_id: str,
    plan: WorkflowPlan,
    run: WorkflowRun,
    artifacts: list[WorkflowArtifact] | None = None,
) -> list[LongTermMemoryProposal]:
    """从已完成 Commander Runtime 提供可重放的候选来源。

    任务恢复会多次保存同一 completed checkpoint；内容指纹不含任务运行次数，故这里可以安全地
    重复调用。没有完成 Runtime、没有 Commander 步骤或没有明确长期表达时不创建候选。
    """

    if run.mode != "runtime" or run.status != "completed":
        return []
    if not any(step.agent == "commander_agent" for step in plan.steps):
        return []
    source_type = (
        "successful_task_experience"
        if _is_explicit_reusable_experience(plan.user_goal, artifacts or [])
        else "verified_project_constraint"
    )
    return persist_memory_proposal_drafts(
        _build_drafts_from_text(
            task_id=task_id,
            project_scope=plan.project_scope or "global",
            source_text=plan.user_goal,
            source_type=source_type,
            source_id=task_id,
            source_conversation_id=plan.conversation_id or None,
            reason=(
                "该候选来自已完成任务中客户明确的可复用经验；尚未写入长期记忆。"
                if source_type == "successful_task_experience"
                else "该候选来自已完成任务中客户明确的项目长期约束；尚未写入长期记忆。"
            ),
        )
    )


def persist_memory_proposal_drafts(drafts: list[MemoryProposalDraft]) -> list[LongTermMemoryProposal]:
    """将已安全提取的草稿写入候选账本，而非正式长期记忆表。"""

    records: list[LongTermMemoryProposal] = []
    for draft in drafts[:_MAX_PROPOSALS_PER_SOURCE]:
        records.append(
            create_or_reuse_long_term_memory_proposal(
                proposal_id=draft.proposal_id,
                task_id=draft.task_id,
                kind=draft.kind,
                suggested_scope=draft.suggested_scope,
                title=draft.title,
                summary=draft.summary,
                tags=draft.tags,
                reason=draft.reason,
                source_type=draft.source_type,
                source_id=draft.source_id,
                source_conversation_id=draft.source_conversation_id,
                conflict_key=draft.conflict_key,
                fingerprint=draft.fingerprint,
            )
        )
    return records


def build_commander_memory_proposals(
    *,
    task_id: str,
    plan: WorkflowPlan,
) -> tuple[list[LongTermMemoryProposal], str]:
    """兼容旧调用方的纯构建入口，不写数据库。"""

    if not any(step.agent == "commander_agent" for step in plan.steps):
        return [], "本次不是总指挥任务，不生成长期记忆候选。"
    drafts = _build_drafts_from_text(
        task_id=task_id,
        project_scope=plan.project_scope or "global",
        source_text=plan.user_goal,
        source_type="verified_project_constraint",
        source_id=task_id,
        source_conversation_id=plan.conversation_id or None,
        reason="仅识别客户明确的长期表达；尚未保存。",
    )
    if not drafts:
        return [], "本次是一次性任务，未发现客户明确表达的长期偏好或项目约束。"
    return [_draft_to_proposal(draft) for draft in drafts], "系统发现待确认的长期记忆候选，尚未保存。"


def is_current_memory_proposal(
    *,
    proposal_id: str,
    task_id: str,
    plan: WorkflowPlan,
) -> bool:
    """保留旧接口兼容性；新确认路径以持久化候选状态为唯一事实。"""

    proposals, _ = build_commander_memory_proposals(task_id=task_id, plan=plan)
    return any(item.proposal_id == proposal_id for item in proposals)


def _build_drafts_from_text(
    *,
    task_id: str,
    project_scope: str,
    source_text: str,
    source_type: str,
    source_id: str,
    source_conversation_id: str | None,
    reason: str,
) -> list[MemoryProposalDraft]:
    try:
        scope = normalize_memory_scope(project_scope)
    except LongTermMemorySafetyError:
        return []
    drafts: list[MemoryProposalDraft] = []
    for clause in _find_durable_clauses(source_text):
        try:
            summary = sanitize_memory_text(clause, field_name="候选记忆摘要", maximum=1000)
            kind, title, tags = _classify_candidate(summary)
            drafts.append(
                MemoryProposalDraft(
                    task_id=task_id[:160],
                    kind=kind,
                    suggested_scope=scope,
                    title=title,
                    summary=summary,
                    tags=normalize_memory_tags(tags),
                    reason=reason,
                    source_type=source_type,
                    source_id=source_id[:180],
                    source_conversation_id=(source_conversation_id or "")[:64] or None,
                )
            )
        except LongTermMemorySafetyError:
            # 原任务/会话可继续保留受控归档，但候选层不能放宽秘密、绝对路径或长度边界。
            continue
        if len(drafts) >= _MAX_PROPOSALS_PER_SOURCE:
            break
    return drafts


def _find_durable_clauses(source_text: str) -> list[str]:
    """只挑选短而明确的长期表达，不把一次性“统一处理本文件”记成偏好。"""

    # 候选层不能从一大段原始聊天里“摘一句”后声称已经安全压缩：那会把完整客户原文间接
    # 变成长期存储入口。长消息应由会话摘要和后续明确确认处理，而不是自动候选化。
    if len(" ".join(source_text.strip().split())) > 600:
        return []
    clauses: list[str] = []
    for raw_clause in _SENTENCE_SPLIT_PATTERN.split(source_text):
        clause = " ".join(raw_clause.strip().split())
        if not 6 <= len(clause) <= 600:
            continue
        if _DURABLE_SIGNAL_PATTERN.search(clause) and not _DURABLE_NEGATION_PATTERN.search(clause):
            clauses.append(clause)
    return list(dict.fromkeys(clauses))


def _classify_candidate(summary: str) -> tuple[str, str, list[str]]:
    """把可替代的偏好压到稳定的键，不用模型猜测更深语义。"""

    lower = summary.lower()
    if re.search(r"项目|团队|验收|规范", summary):
        return "project_constraint", "项目交付规范", ["project", "constraint"]
    if _FORMAT_PATTERN.search(summary):
        return "user_preference", "交付格式偏好", ["preference", "format"]
    if "中文" in summary or "英文" in summary:
        return "user_preference", "语言偏好", ["preference", "language"]
    if re.search(r"简洁|详细|风格|语气|措辞", summary):
        return "user_preference", "表达风格偏好", ["preference", "style"]
    if re.search(r"模板|经验|复用|流程", summary):
        return "experience", "可复用工作经验", ["experience"]
    if "默认" in lower or "每次" in summary:
        return "user_preference", "默认工作偏好", ["preference"]
    return "project_constraint", "长期工作约束", ["constraint"]


def _is_explicit_reusable_experience(user_goal: str, artifacts: list[WorkflowArtifact]) -> bool:
    """仅在客户同时要求沉淀成功经验且 Runtime 已实际完成时使用 experience 来源。"""

    return bool(
        artifacts
        and re.search(r"(?:沉淀|复用).{0,12}(?:经验|模板|流程)|(?:经验|模板|流程).{0,12}(?:复用|沉淀)", user_goal)
    )


def _draft_to_proposal(draft: MemoryProposalDraft) -> LongTermMemoryProposal:
    return LongTermMemoryProposal(
        proposal_id=draft.proposal_id,
        task_id=draft.task_id,
        kind=draft.kind,
        title=draft.title,
        summary=draft.summary,
        tags=draft.tags,
        suggested_scope=draft.suggested_scope,
        reason=draft.reason,
        source_type=draft.source_type,
        source_id=draft.source_id,
        source_conversation_id=draft.source_conversation_id,
    )
