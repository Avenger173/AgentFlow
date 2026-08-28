"""总指挥任务结束后的长期记忆候选。

候选层刻意不用模型、也不写数据库：它只从已保存的 Commander 计划里识别客户明确表达的
长期约束。这样既不会额外消耗额度，也不会把一次性任务或模型猜测静默升级为长期记忆。
"""

from __future__ import annotations

from hashlib import sha256
import re

from app.schemas.chat import WorkflowPlan
from app.schemas.memory import LongTermMemoryProposal
from app.services.long_term_memory import LongTermMemorySafetyError, sanitize_memory_text


_DURABLE_SIGNAL_PATTERN = re.compile(
    r"(?:以后|今后|后续|长期|始终|一直|默认|统一|每次|一律|固定|项目(?:中|内)?(?:都|统一|默认))"
)
_SENTENCE_SPLIT_PATTERN = re.compile(r"[。！？!?；;\n]+")


def build_commander_memory_proposals(
    *,
    task_id: str,
    plan: WorkflowPlan,
) -> tuple[list[LongTermMemoryProposal], str]:
    """只对明确、稳定的客户表达提出最多一条可编辑候选。"""

    if not any(step.agent == "commander_agent" for step in plan.steps):
        return [], "本次不是总指挥任务，不生成长期记忆候选。"

    candidate_summary = _find_durable_clause(plan.user_goal)
    if not candidate_summary:
        return [], "本次是一次性任务，未发现客户明确表达的长期偏好或项目约束。"

    try:
        summary = sanitize_memory_text(candidate_summary, field_name="候选记忆摘要", maximum=1000)
    except LongTermMemorySafetyError:
        # 计划历史允许保留原用户目标用于任务复盘，但长期层绝不能因此放宽秘密/路径边界。
        return [], "候选内容包含不适合长期保存的信息，系统未创建候选。"

    kind, title, tags = _classify_candidate(summary)
    scope = plan.project_scope or "global"
    proposal_id = _proposal_id(task_id=task_id, kind=kind, summary=summary, scope=scope)
    return [
        LongTermMemoryProposal(
            proposal_id=proposal_id,
            task_id=task_id,
            kind=kind,
            title=title,
            summary=summary,
            tags=tags,
            suggested_scope=scope,
            reason="仅识别客户明确的长期表达；保存前仍可编辑范围、标题、摘要和标签。",
        )
    ], "系统发现一条可能可复用的长期约束，尚未保存。"


def is_current_memory_proposal(
    *,
    proposal_id: str,
    task_id: str,
    plan: WorkflowPlan,
) -> bool:
    """确认请求必须对应当前任务仍可重新推导出的候选，避免客户端伪造来源。"""

    proposals, _ = build_commander_memory_proposals(task_id=task_id, plan=plan)
    return any(item.proposal_id == proposal_id for item in proposals)


def _find_durable_clause(user_goal: str) -> str:
    """从客户原句中挑出含强长期信号的一句，不把普通“必须完成本次任务”误记下来。"""

    for raw_clause in _SENTENCE_SPLIT_PATTERN.split(user_goal):
        clause = " ".join(raw_clause.strip().split())
        if 6 <= len(clause) <= 600 and _DURABLE_SIGNAL_PATTERN.search(clause):
            return clause
    return ""


def _classify_candidate(summary: str) -> tuple[str, str, list[str]]:
    """保持分类保守：偏好与项目约束有不同检索语义，不能混成泛化“记忆”。"""

    if re.search(r"(?:默认|优先|风格|语气|简洁|详细|格式)", summary):
        return "user_preference", "默认工作偏好", ["preference"]
    if re.search(r"(?:项目|交付|规范|统一|团队|文档|验收)", summary):
        return "project_constraint", "项目长期约束", ["project", "constraint"]
    return "experience", "可复用工作约束", ["experience"]


def _proposal_id(*, task_id: str, kind: str, summary: str, scope: str) -> str:
    digest = sha256(f"{task_id}\n{kind}\n{scope}\n{summary}".encode("utf-8")).hexdigest()[:16]
    return f"memory_proposal_{digest}"
