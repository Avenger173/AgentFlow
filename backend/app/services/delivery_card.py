"""R5.4D 统一会话结果卡的只读聚合器。

结果卡只消费已经落库的 Runtime、artifact、权限和评估事实，不重新读取源文件、不调用模型，
也不把完整日志、工具参数、内部路径或任务 ID 写入客户正文。这样 Qt 可以用一个接口渲染
“结论优先”的结果区，详细过程仍由任务历史和 Inspector 按需提供。
"""

from __future__ import annotations

import re

from app.schemas.workflow import (
    RuntimePermissionItem,
    WorkflowArtifact,
    WorkflowDeliveryArtifact,
    WorkflowDeliveryCard,
    WorkflowDeliveryFact,
    WorkflowRun,
    WorkflowToolCall,
)
from app.workflow.evaluation import evaluate_workflow_task


_INTERNAL_ID_PATTERN = re.compile(r"\b(?:task|runtime|plan|conv|artifact)_[a-z0-9_]{6,}\b", re.IGNORECASE)
_ABSOLUTE_PATH_PATTERN = re.compile(r"(?i)(?:\b[a-z]:[\\/][^\s<>\"']+|\\\\[^\\/\s]+[\\/][^\s<>\"']+)")


def build_delivery_card(
    *,
    run: WorkflowRun,
    artifacts: list[WorkflowArtifact],
    tool_calls: list[WorkflowToolCall],
    permissions: list[RuntimePermissionItem],
) -> WorkflowDeliveryCard:
    """将任务的分散事实收束成稳定、脱敏且适合客户阅读的交付对象。"""

    evaluation = evaluate_workflow_task(run=run, tool_calls=tool_calls, permissions=permissions)
    facts = _facts(run=run, artifacts=artifacts)
    warnings = [_safe_text(item, maximum=220) for item in dict.fromkeys([*evaluation.warnings, *run.validation_errors])][:8]
    return WorkflowDeliveryCard(
        delivery_id=f"delivery_{run.task_id}",
        task_id=run.task_id,
        mode=run.mode,
        status=run.status,
        terminal=run.status in {"completed", "failed", "blocked", "cancelled"},
        headline=_headline(run=run, artifacts=artifacts),
        summary_markdown=_summary(run=run, evaluation=evaluation, artifacts=artifacts),
        facts=facts,
        warnings=warnings,
        artifacts=[_artifact(item) for item in artifacts[:8]],
        next_actions=_next_actions(run=run, artifacts=artifacts, evaluation=evaluation),
        updated_at=run.metrics.finished_at or run.metrics.started_at,
    )


def _headline(*, run: WorkflowRun, artifacts: list[WorkflowArtifact]) -> str:
    """生成不暴露内部动作名的短标题。"""

    if run.mode == "dry_run" and run.status == "completed":
        return "计划已生成，等待确认"
    if run.status == "completed":
        return f"任务已完成 · {len(artifacts)} 项交付"
    if run.status == "waiting_permission":
        return "等待权限确认"
    if run.status in {"pending", "running", "paused"}:
        return "任务正在处理"
    if run.status == "cancelled":
        return "任务已取消"
    return "任务需要处理"


def _summary(*, run: WorkflowRun, evaluation: object, artifacts: list[WorkflowArtifact]) -> str:
    """只保留一段客户可读结论，不把复盘指标或事件流直接塞进正文。"""

    if run.status == "completed" and artifacts:
        names = "、".join(item.name for item in artifacts[:3])
        suffix = "等交付物已登记，可直接打开。" if len(artifacts) > 3 else "已登记，可直接打开。"
        return _safe_text(f"{run.summary or '真实执行已完成。'}\n\n已生成：{names}{suffix}", maximum=2_200)
    return _safe_text(run.summary or "任务暂无可展示的结果。", maximum=2_200)


def _facts(*, run: WorkflowRun, artifacts: list[WorkflowArtifact]) -> list[WorkflowDeliveryFact]:
    """提取固定的通用事实，并补充数据类动作已经提供的统计事实。"""

    facts = [
        WorkflowDeliveryFact(label="执行步骤", value=str(len(run.steps))),
        WorkflowDeliveryFact(label="已完成", value=f"{run.metrics.step_completed}/{len(run.steps)}"),
    ]
    if run.metrics.duration_ms > 0:
        facts.append(WorkflowDeliveryFact(label="耗时", value=_format_duration(run.metrics.duration_ms)))
    if artifacts:
        facts.append(WorkflowDeliveryFact(label="交付物", value=f"{len(artifacts)} 项"))

    for step in run.steps:
        result = step.output.get("result") if isinstance(step.output, dict) else None
        if not isinstance(result, dict):
            continue
        _append_result_fact(facts, result, "output_row_count", "输出行数", suffix=" 行")
        _append_result_fact(facts, result, "matched_row_count", "匹配行数", suffix=" 行")
        _append_result_fact(facts, result, "chart_count", "图表", suffix=" 张")
        _append_result_fact(facts, result, "field_count", "新增字段", suffix=" 个")
        if result.get("source_files_unchanged") is True or result.get("original_files_unchanged") is True:
            _append_unique_fact(facts, WorkflowDeliveryFact(label="源文件", value="未修改"))
    return facts[:12]


def _append_result_fact(facts: list[WorkflowDeliveryFact], result: dict, key: str, label: str, *, suffix: str) -> None:
    """把已由专用 Agent 校验过的整数统计转为结果卡事实。"""

    value = result.get(key)
    if isinstance(value, int) and value >= 0:
        _append_unique_fact(facts, WorkflowDeliveryFact(label=label, value=f"{value}{suffix}"))


def _append_unique_fact(facts: list[WorkflowDeliveryFact], fact: WorkflowDeliveryFact) -> None:
    if not any(item.label == fact.label for item in facts):
        facts.append(fact)


def _artifact(artifact: WorkflowArtifact) -> WorkflowDeliveryArtifact:
    """映射为不带 metadata 的安全产物摘要。"""

    return WorkflowDeliveryArtifact(
        artifact_id=artifact.artifact_id,
        name=artifact.name,
        kind=artifact.kind,
        summary=_safe_text(artifact.summary, maximum=240),
        uri=artifact.uri,
        mime_type=artifact.mime_type,
        openable=bool(artifact.metadata.get("runtime") is True and artifact.uri.startswith("agentflow-output://")),
        previewable=artifact.kind in {"text", "markdown", "report"},
    )


def _next_actions(*, run: WorkflowRun, artifacts: list[WorkflowArtifact], evaluation: object) -> list[str]:
    """生成少量直接动作，不把日志、内部 ID 或复盘指标暴露给客户。"""

    if run.mode == "dry_run" and run.status == "completed":
        return ["确认计划后开始执行"]
    if run.status == "waiting_permission":
        return ["查看并处理权限确认"]
    if run.status in {"pending", "running", "paused"}:
        return ["等待任务完成"]
    if run.status == "completed" and artifacts:
        return ["打开交付物", "继续提出下一步要求"]
    if run.status in {"failed", "blocked"}:
        return ["查看失败原因后重试"]
    if run.status == "cancelled":
        return ["重新提交任务"]
    return ["继续提出下一步要求"]


def _format_duration(duration_ms: int) -> str:
    if duration_ms < 1_000:
        return f"{duration_ms} ms"
    return f"{duration_ms / 1_000:.1f} 秒"


def _safe_text(value: str, *, maximum: int) -> str:
    """过滤内部标识和本机路径，避免公共结果卡变成审计信息出口。"""

    text = _INTERNAL_ID_PATTERN.sub("[内部标识已隐藏]", str(value))
    text = _ABSOLUTE_PATH_PATTERN.sub("[本地路径已隐藏]", text)
    return text[:maximum].strip() or "[内容已省略]"
