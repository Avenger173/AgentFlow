from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.schemas.chat import WorkflowStep
from app.services.runtime_preferences_store import VALID_PERMISSION_POLICIES


PermissionPolicyAction = Literal["allow", "confirm", "block"]

_SIDE_EFFECT_PERMISSIONS = {
    "file_write",
    "network",
    "shell",
    "database",
    "plugin_install",
}
_HIGH_RISK_PERMISSIONS = {"shell", "plugin_install"}
_AUTO_APPROVE_DENYLIST = {"network", "shell", "database", "plugin_install"}
_EXPLICIT_LOCAL_DELIVERY_ACTIONS = {
    ("data_agent", "export_chart_dashboard"),
    ("data_agent", "export_analysis_workbook"),
}


@dataclass(frozen=True)
class PermissionPolicyDecision:
    """平台 Governance 对单个计划步骤给出的确定性裁决。"""

    policy: str
    action: PermissionPolicyAction
    reason: str
    audit_required: bool = True


def evaluate_permission_policy(
    *,
    permission_policy: str,
    step: WorkflowStep,
) -> PermissionPolicyDecision:
    """按平台策略裁决步骤权限，不信任模型自行降低风险。

    `allow` 只代表 Harness 可以按已注册工具边界继续；工具仍需做路径、参数和超时校验。
    高危命令或平台明确禁止的命令始终不能因用户选择高权限模式而自动放行。
    """

    policy = permission_policy.strip().lower()
    if policy not in VALID_PERMISSION_POLICIES:
        policy = "smart_confirm"

    permissions = set(step.required_permissions)
    has_side_effect = bool(permissions & _SIDE_EFFECT_PERMISSIONS)
    has_high_risk_permission = bool(permissions & _HIGH_RISK_PERMISSIONS)
    command_is_hard_blocked = step.command_policy.may_run_command and not step.command_policy.allowed

    if command_is_hard_blocked:
        return PermissionPolicyDecision(
            policy=policy,
            action="block",
            reason="命令策略已将该步骤标记为禁止执行，高权限模式也不能绕过平台硬边界。",
        )

    if step.risk_level == "high" or has_high_risk_permission:
        return PermissionPolicyDecision(
            policy=policy,
            action="confirm",
            reason="步骤包含高风险权限，必须由用户明确确认。",
        )

    # 图表/分析 Excel 的写入目标固定在受控 outputs，且只有规划器在识别到用户明确的
    # “生成/导出/保存”请求时才会写入这个内部标记。它等价于用户已经在自然语言中给出
    # 本次交付确认，不应再弹一次“开始执行”；路径、参数、像素/工作簿回读仍由专业 Tool
    # 和 Runtime 验证，不能推广到任意 file_write。
    if (
        (step.agent, step.action) in _EXPLICIT_LOCAL_DELIVERY_ACTIONS
        and step.input.get("explicit_output_request") is True
        and permissions.issubset({"file_read", "file_write"})
    ):
        return PermissionPolicyDecision(
            policy=policy,
            action="allow",
            reason="用户已明确要求本地受控交付，图表/工作簿仅写入 outputs 并保留审计。",
        )

    if policy == "always_ask":
        if step.requires_confirmation or has_side_effect:
            return PermissionPolicyDecision(
                policy=policy,
                action="confirm",
                reason="当前为请求批准模式，产生外部副作用前必须询问用户。",
            )
        return PermissionPolicyDecision(policy=policy, action="allow", reason="只读低风险步骤可直接执行。")

    if policy == "smart_confirm":
        if step.requires_confirmation or step.risk_level == "medium" or has_side_effect:
            return PermissionPolicyDecision(
                policy=policy,
                action="confirm",
                reason="风险操作确认模式检测到中风险或外部副作用，需要用户确认。",
            )
        return PermissionPolicyDecision(policy=policy, action="allow", reason="未检测到需要确认的风险。")

    if policy == "auto_approve":
        if permissions & _AUTO_APPROVE_DENYLIST:
            return PermissionPolicyDecision(
                policy=policy,
                action="confirm",
                reason="替我审批模式不会自动批准联网、数据库、Shell 或插件操作。",
            )
        if step.requires_confirmation and not permissions.issubset({"file_read", "file_write"}):
            return PermissionPolicyDecision(
                policy=policy,
                action="confirm",
                reason="步骤声明了未识别的敏感权限，按安全回退要求用户确认。",
            )
        return PermissionPolicyDecision(
            policy=policy,
            action="allow",
            reason="替我审批模式允许已注册工具在受控工作区内读写，并保留审计记录。",
        )

    # full_access 只降低中风险步骤的打断频率；高风险和硬禁止项已在上方先行收紧。
    if step.requires_confirmation and not permissions.issubset(
        {"file_read", "file_write", "network", "database"}
    ):
        return PermissionPolicyDecision(
            policy=policy,
            action="confirm",
            reason="完全访问模式遇到未知敏感权限，按安全回退要求用户确认。",
        )
    return PermissionPolicyDecision(
        policy=policy,
        action="allow",
        reason="完全访问模式允许已注册的中低风险工具继续执行，但仍保留工具边界和审计。",
    )
