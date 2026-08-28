from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePath

from app.schemas.workflow import WorkflowCommandPolicyCheckResponse
from app.services.runtime_preferences_store import VALID_PERMISSION_POLICIES


@dataclass(frozen=True)
class CommandRiskPattern:
    """静态高危规则。

    rule_id 用于日志和 UI 审计，reason 解释为什么命中，warning 面向用户说明可能造成的损害。
    """

    rule_id: str
    pattern: re.Pattern[str]
    reason: str
    warning: str = ""
    examples: tuple[str, ...] = ()
    safer_alternatives: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandPolicyResult:
    """命令静态风险检查结果。

    这里借鉴 Claude Code / Codex 的思路：先把命令当作工具调用风险来治理，再考虑是否执行。
    当前模块只做保守分类，不执行命令，也不承诺完整理解所有 shell 语法；解析不清时宁可提高风险。
    """

    command: str
    normalized_command: str
    risk_level: str
    allowed: bool
    requires_confirmation: bool
    audit_required: bool
    concurrency_safe: bool
    default_timeout_ms: int
    max_output_chars: int
    detected_commands: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    destructive_warnings: list[str] = field(default_factory=list)
    safer_alternatives: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    suggested_tool: str = ""
    effective_permission_policy: str = "smart_confirm"
    effective_action: str = "confirm"
    effective_reason: str = ""
    execution_scope: str = "none"
    execution_route: str = ""
    cwd_policy: str = ""
    sandbox_hint: str = ""
    audit_fields: list[str] = field(default_factory=list)
    execution_notes: list[str] = field(default_factory=list)
    runtime_ready: bool = False
    permission_required: bool = False
    runtime_request_status: str = "blocked"
    approval_prompt: str = ""
    block_reason_code: str = ""
    audit_record_preview: dict[str, object] = field(default_factory=dict)

    def to_response(self) -> WorkflowCommandPolicyCheckResponse:
        return WorkflowCommandPolicyCheckResponse(**self.__dict__)


_RISK_ORDER = {
    "none": 0,
    "read_only": 1,
    "diagnostic": 2,
    "modifying": 3,
    "network": 4,
    "high_risk": 5,
}

_READ_ONLY_COMMANDS = {
    "ack",
    "ag",
    "awk",
    "cat",
    "dir",
    "du",
    "file",
    "find",
    "get-childitem",
    "get-command",
    "get-content",
    "get-process",
    "grep",
    "head",
    "jq",
    "less",
    "locate",
    "ls",
    "more",
    "rg",
    "select-string",
    "sort",
    "stat",
    "tail",
    "tree",
    "type",
    "uniq",
    "wc",
    "where",
    "whereis",
    "which",
}

_DIAGNOSTIC_COMMANDS = {
    "bun",
    "cmake",
    "dotnet",
    "go",
    "jom",
    "mvn",
    "ninja",
    "npm",
    "pnpm",
    "pytest",
    "py",
    "python",
    "ruff",
    "tsc",
    "yarn",
}

_MODIFYING_COMMANDS = {
    "add-content",
    "copy",
    "copy-item",
    "cp",
    "del",
    "erase",
    "git",
    "mkdir",
    "move",
    "move-item",
    "mv",
    "new-item",
    "rd",
    "remove-item",
    "ren",
    "rmdir",
    "rm",
    "set-content",
    "touch",
}

_NETWORK_COMMANDS = {
    "curl",
    "git",
    "iwr",
    "invoke-restmethod",
    "invoke-webrequest",
    "pip",
    "pip3",
    "wget",
}

_HIGH_RISK_PATTERNS = (
    CommandRiskPattern(
        "shell.rm_recursive_force",
        re.compile(r"\brm\s+[^&|;\n]*-[^\s]*r[^\s]*f", re.IGNORECASE),
        "递归强制删除。",
        "可能永久删除目录树；真实执行前必须确认目标路径、备份和回滚方式。",
        safer_alternatives=(
            "先用只读命令列出目标，例如 `ls`、`tree` 或 `rg --files`。",
            "把删除范围缩小到受控 workspace 内的明确路径，并让用户确认清单。",
        ),
    ),
    CommandRiskPattern(
        "powershell.remove_item_recursive_force",
        re.compile(r"\bremove-item\b[^&|;\n]*(?:-recurse|-force)", re.IGNORECASE),
        "PowerShell 递归/强制删除。",
        "可能递归删除大量文件；应改成先列出目标，再由用户确认具体删除范围。",
        safer_alternatives=(
            "先用 `Get-ChildItem` 展示将受影响的文件和目录。",
            "拆成单个明确路径的删除请求，并记录可恢复方案。",
        ),
    ),
    CommandRiskPattern(
        "windows.rmdir_recursive",
        re.compile(r"\b(?:rd|rmdir)\b[^&|;\n]*/s\b", re.IGNORECASE),
        "Windows 递归删除目录。",
        "会删除目录及其子项；应先用只读命令确认目录内容和授权范围。",
        safer_alternatives=(
            "先用 `dir` 或 `tree` 查看目录内容。",
            "把删除动作改成用户确认后的受控文件操作。",
        ),
    ),
    CommandRiskPattern(
        "windows.del_recursive_quiet",
        re.compile(r"\b(?:del|erase)\b[^&|;\n]*(?:/s|/q)", re.IGNORECASE),
        "Windows 静默或递归删除文件。",
        "可能批量删除且缺少交互提示；需要先展示匹配文件清单。",
        safer_alternatives=(
            "先列出通配符命中的文件，不使用 `/q` 静默删除。",
            "把通配符删除改成逐项确认的文件清单。",
        ),
    ),
    CommandRiskPattern(
        "git.reset_hard",
        re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),
        "强制重置 Git 工作区。",
        "会丢弃未提交改动；真实执行前必须明确用户已经备份或同意丢弃。",
        safer_alternatives=(
            "先运行 `git status --short` 和 `git diff` 查看未保存改动。",
            "如需保留现场，先生成 patch 或让用户确认可以丢弃。",
        ),
    ),
    CommandRiskPattern(
        "git.clean_force",
        re.compile(r"\bgit\s+clean\b[^&|;\n]*-[^\s]*f", re.IGNORECASE),
        "强制清理未跟踪文件。",
        "会删除未跟踪文件；应先执行 dry-run 或列出目标文件。",
        safer_alternatives=(
            "先运行 `git clean -nd` 查看将被删除的文件。",
            "只清理明确确认的路径，避免清掉用户临时资料。",
        ),
    ),
    CommandRiskPattern(
        "git.checkout_restore",
        re.compile(r"\bgit\s+checkout\b[^&|;\n]*(?:--force|-f|--\s+)", re.IGNORECASE),
        "Git checkout 强制切换或恢复文件。",
        "可能覆盖工作区文件；需要先确认会影响哪些路径。",
        safer_alternatives=(
            "先运行 `git status --short` 和 `git diff -- <path>` 查看影响范围。",
            "只针对用户确认的单个文件恢复，不批量恢复整个工作区。",
        ),
    ),
    CommandRiskPattern(
        "git.restore",
        re.compile(r"\bgit\s+restore\b", re.IGNORECASE),
        "Git restore 可能覆盖工作区改动。",
        "可能丢弃用户未提交修改；执行前必须展示目标文件和恢复来源。",
        safer_alternatives=(
            "先用 `git diff -- <path>` 查看将被覆盖的内容。",
            "要求用户确认目标路径和恢复来源后再进入真实执行。",
        ),
    ),
    CommandRiskPattern(
        "git.stash_destructive",
        re.compile(r"\bgit\s+stash\s+(?:drop|clear)\b", re.IGNORECASE),
        "删除 Git stash 记录。",
        "可能永久丢失暂存工作；应先列出 stash 并要求用户选择。",
        safer_alternatives=(
            "先运行 `git stash list` 和 `git stash show` 查看内容。",
            "让用户选择具体 stash 条目，不自动清空全部记录。",
        ),
    ),
    CommandRiskPattern(
        "git.branch_force_delete",
        re.compile(r"\bgit\s+branch\b[^&|;\n]*(?:-D|--delete\s+--force|--force\s+--delete)\b", re.IGNORECASE),
        "强制删除 Git 分支。",
        "可能删除未合并工作；应先显示分支状态和合并关系。",
        safer_alternatives=(
            "先运行 `git branch --merged`、`git log` 或 `git status` 判断是否安全。",
            "优先使用普通删除，并让用户确认未合并分支的处理方式。",
        ),
    ),
    CommandRiskPattern(
        "git.push_force",
        re.compile(r"\bgit\s+push\b[^&|;\n]*(?:--force|-f)\b", re.IGNORECASE),
        "强制推送远端分支。",
        "会改写远端历史；平台不能自动批准，应由用户明确确认。",
        safer_alternatives=(
            "先运行 `git status`、`git log --oneline --decorate -n 10` 查看历史差异。",
            "确需改写历史时优先提示用户手动执行 `--force-with-lease`。",
        ),
    ),
    CommandRiskPattern(
        "shell.download_pipe_execute",
        re.compile(
            r"\b(?:curl|wget|iwr|invoke-webrequest)\b[^|;\n]*\|\s*(?:sh|bash|pwsh|powershell|iex|invoke-expression)\b",
            re.IGNORECASE,
        ),
        "下载脚本后直接执行。",
        "会执行未经审查的远端内容；应先下载到受控位置并展示内容摘要。",
        safer_alternatives=(
            "先下载到受控临时文件，展示来源、哈希和关键内容摘要。",
            "把脚本拆成明确、可审计的工具步骤，不直接管道执行。",
        ),
    ),
    CommandRiskPattern(
        "shell.dynamic_expression",
        re.compile(r"\b(?:iex|invoke-expression)\b", re.IGNORECASE),
        "动态执行字符串。",
        "可能执行拼接出的未知代码；需要转成显式脚本或专用工具调用。",
        safer_alternatives=(
            "把要执行的内容保存为可审查文本，再由用户确认。",
            "改用结构化工具参数，不让模型拼接任意代码字符串。",
        ),
    ),
    CommandRiskPattern(
        "powershell.execution_policy",
        re.compile(r"\bset-executionpolicy\b", re.IGNORECASE),
        "修改 PowerShell 执行策略。",
        "会改变系统脚本执行边界；不应由 Agent 自动修改。",
        safer_alternatives=(
            "提示用户手动在系统设置中处理执行策略。",
            "优先使用不需要改变系统策略的受控脚本运行方式。",
        ),
    ),
    CommandRiskPattern(
        "system.machine_control",
        re.compile(r"\b(?:format|mkfs|diskpart|shutdown|reboot)\b", re.IGNORECASE),
        "系统级危险命令。",
        "可能格式化磁盘、修改分区或中断系统；必须平台硬拦截。",
        safer_alternatives=(
            "只给出人工操作说明，不进入自动执行。",
            "如需诊断系统状态，改用只读查询命令。",
        ),
    ),
    CommandRiskPattern(
        "filesystem.chmod_broad",
        re.compile(r"\bchmod\b[^&|;\n]*(?:777|-r)", re.IGNORECASE),
        "宽泛或递归修改权限。",
        "可能扩大文件访问权限；需要限定路径和权限目标。",
        safer_alternatives=(
            "先查看当前权限，再只修改明确文件的最小必要权限。",
            "避免 `777` 和递归修改，改为用户确认后的精确权限变更。",
        ),
    ),
    CommandRiskPattern(
        "windows.ownership_acl",
        re.compile(r"\b(?:takeown|icacls)\b", re.IGNORECASE),
        "修改系统文件归属或 ACL。",
        "可能破坏系统或项目权限边界；必须保留强确认和审计。",
        safer_alternatives=(
            "先用只读命令查看 ACL 和文件归属。",
            "仅给出人工修复建议，不自动修改系统权限。",
        ),
    ),
    CommandRiskPattern(
        "database.drop_truncate",
        re.compile(r"\b(?:drop|truncate)\s+(?:database|schema|table)\b", re.IGNORECASE),
        "数据库 DROP/TRUNCATE 操作。",
        "可能删除库表或清空数据；执行前必须要求备份、环境确认和人工审批。",
        safer_alternatives=(
            "先执行只读 `SELECT COUNT(*)` 或 schema 查询确认影响范围。",
            "要求备份、环境标识和人工审批后再考虑迁移脚本。",
        ),
    ),
    CommandRiskPattern(
        "database.delete_without_where",
        re.compile(r"\bdelete\s+from\s+[a-zA-Z_][\w.]*\s*(?:;|$)", re.IGNORECASE),
        "数据库 DELETE 未带 WHERE 条件。",
        "可能清空整张表；需要拒绝自动执行并提示补充条件或备份。",
        safer_alternatives=(
            "先补充 WHERE 条件并用 SELECT 预览将影响的行。",
            "在事务中执行并准备回滚方案，不允许自动清表。",
        ),
    ),
    CommandRiskPattern(
        "kubernetes.delete",
        re.compile(r"\bkubectl\s+delete\b", re.IGNORECASE),
        "Kubernetes 删除资源。",
        "可能删除线上资源；必须明确 namespace、资源名和回滚方案。",
        safer_alternatives=(
            "先运行 `kubectl get` / `kubectl describe` 查看资源和 namespace。",
            "要求用户确认集群、namespace、资源名和回滚方案。",
        ),
    ),
    CommandRiskPattern(
        "terraform.destroy",
        re.compile(r"\bterraform\s+destroy\b", re.IGNORECASE),
        "Terraform 销毁基础设施。",
        "可能删除云资源；不能由 Agent 自动放行。",
        safer_alternatives=(
            "先运行 `terraform plan -destroy` 并展示资源变更摘要。",
            "销毁动作必须由用户在确认环境和备份后手动触发。",
        ),
    ),
    CommandRiskPattern(
        "docker.prune",
        re.compile(r"\bdocker\s+(?:system|volume|image|container)\s+prune\b", re.IGNORECASE),
        "Docker 批量清理资源。",
        "可能删除镜像、容器、卷或缓存；应先列出影响范围。",
        safer_alternatives=(
            "先运行 `docker system df` 或列出镜像/卷/容器。",
            "只清理用户确认的具体资源，避免批量 prune。",
        ),
    ),
)

_SEPARATORS_RE = re.compile(r"\s*(?:&&|\|\||[;|\n])\s*")
_REDIRECTION_RE = re.compile(r"(^|[^<>])(?:>{1,2}|<)\s*[^&|;\n]+")


def classify_command_policy(
    command: str,
    *,
    cwd: str = "",
    permission_policy: str = "smart_confirm",
) -> CommandPolicyResult:
    """静态检查命令风险。

    设计目标是“执行前解释和分流”，不是替代真正 sandbox。未来 Runtime 真要执行命令时，
    还需要结合用户的审批模式、工作目录、文件边界、超时和输出截断再次校验。
    """

    normalized = _normalize_command(command)
    if not normalized:
        effective_policy, effective_action, effective_reason = _effective_policy_decision(
            risk_level="none",
            allowed=True,
            requires_confirmation=False,
            permission_policy=permission_policy,
        )
        execution_preview = _build_execution_preview(
            risk_level="none",
            allowed=True,
            categories=[],
            detected_commands=[],
            cwd=cwd,
        )
        runtime_request_preview = _build_runtime_request_preview(
            command=command,
            normalized_command="",
            risk_level="none",
            categories=[],
            detected_commands=[],
            rule_ids=[],
            allowed=True,
            requires_confirmation=False,
            effective_policy=effective_policy,
            effective_action=effective_action,
            execution_preview=execution_preview,
            default_timeout_ms=0,
            max_output_chars=0,
            cwd=cwd,
        )
        return CommandPolicyResult(
            command=command,
            normalized_command="",
            risk_level="none",
            allowed=True,
            requires_confirmation=False,
            audit_required=False,
            concurrency_safe=True,
            default_timeout_ms=0,
            max_output_chars=0,
            reasons=["命令为空。"],
            effective_permission_policy=effective_policy,
            effective_action=effective_action,
            effective_reason=effective_reason,
            **execution_preview,
            **runtime_request_preview,
        )

    risk_level = "none"
    categories: set[str] = set()
    detected_commands: list[str] = []
    reasons: list[str] = []
    warnings: list[str] = []
    rule_ids: list[str] = []
    destructive_warnings: list[str] = []
    safer_alternatives: list[str] = []
    suggested_tool = ""

    for rule in _HIGH_RISK_PATTERNS:
        if rule.pattern.search(normalized):
            risk_level = _max_risk(risk_level, "high_risk")
            categories.add("high_risk")
            rule_ids.append(rule.rule_id)
            reasons.append(rule.reason)
            if rule.warning:
                destructive_warnings.append(rule.warning)
            safer_alternatives.extend(rule.safer_alternatives)

    if _REDIRECTION_RE.search(normalized):
        risk_level = _max_risk(risk_level, "modifying")
        categories.add("modifying")
        reasons.append("命令包含重定向，可能写入或覆盖文件。")

    for part in _split_command_parts(normalized):
        base = _base_command(part)
        if not base:
            continue
        if base not in detected_commands:
            detected_commands.append(base)

        part_risk, part_category, part_reason = _classify_part(base, part)
        risk_level = _max_risk(risk_level, part_risk)
        if part_category:
            categories.add(part_category)
        if part_reason:
            reasons.append(part_reason)

    if risk_level == "none" and detected_commands:
        risk_level = "modifying"
        categories.add("unknown")
        reasons.append("命令不在已知只读/诊断白名单中，按可能有副作用处理。")
        warnings.append("静态分类器无法完整理解该命令，执行前应要求用户确认。")

    if "high_risk" in categories:
        warnings.append("高危命令默认不应自动执行；即使用户开启高权限模式，也要保留强提示和审计。")
    if "network" in categories:
        warnings.append("联网命令可能泄露上下文或消耗时间，必须记录目标和用途。")
    if cwd and not PurePath(cwd).is_absolute():
        warnings.append("cwd 不是绝对路径，真实执行前需要解析到受控工作区。")

    if detected_commands and all(command_name in _READ_ONLY_COMMANDS for command_name in detected_commands):
        suggested_tool = "优先使用 workspace/document search/read 这类专用只读工具，减少 shell 解析风险。"
    elif risk_level == "diagnostic":
        suggested_tool = "优先使用专用测试/构建工具封装，记录超时、输出截断和退出码。"

    allowed = risk_level != "high_risk"
    requires_confirmation = risk_level in {"modifying", "network", "high_risk"}
    concurrency_safe = risk_level == "read_only"
    default_timeout_ms = 120_000 if risk_level in {"diagnostic", "network"} else 30_000
    max_output_chars = 100_000 if risk_level in {"diagnostic", "network"} else 60_000

    if not reasons:
        reasons.append("未发现明显风险。")

    effective_policy, effective_action, effective_reason = _effective_policy_decision(
        risk_level=risk_level,
        allowed=allowed,
        requires_confirmation=requires_confirmation,
        permission_policy=permission_policy,
    )
    execution_preview = _build_execution_preview(
        risk_level=risk_level,
        allowed=allowed,
        categories=sorted(categories, key=lambda item: (_RISK_ORDER.get(item, 99), item)),
        detected_commands=detected_commands,
        cwd=cwd,
    )
    runtime_request_preview = _build_runtime_request_preview(
        command=command,
        normalized_command=normalized,
        risk_level=risk_level,
        categories=sorted(categories, key=lambda item: (_RISK_ORDER.get(item, 99), item)),
        detected_commands=detected_commands,
        rule_ids=_dedupe(rule_ids),
        allowed=allowed,
        requires_confirmation=requires_confirmation,
        effective_policy=effective_policy,
        effective_action=effective_action,
        execution_preview=execution_preview,
        default_timeout_ms=default_timeout_ms,
        max_output_chars=max_output_chars,
        cwd=cwd,
    )

    return CommandPolicyResult(
        command=command,
        normalized_command=normalized,
        risk_level=risk_level,
        allowed=allowed,
        requires_confirmation=requires_confirmation,
        audit_required=risk_level != "none",
        concurrency_safe=concurrency_safe,
        default_timeout_ms=default_timeout_ms,
        max_output_chars=max_output_chars,
        detected_commands=detected_commands,
        categories=sorted(categories, key=lambda item: (_RISK_ORDER.get(item, 99), item)),
        rule_ids=_dedupe(rule_ids),
        reasons=_dedupe(reasons),
        destructive_warnings=_dedupe(destructive_warnings),
        safer_alternatives=_dedupe(safer_alternatives),
        warnings=_dedupe(warnings),
        suggested_tool=suggested_tool,
        effective_permission_policy=effective_policy,
        effective_action=effective_action,
        effective_reason=effective_reason,
        **execution_preview,
        **runtime_request_preview,
    )


def list_command_policy_rules() -> list[dict[str, object]]:
    """返回可展示、可审计的命令规则目录。

    不直接暴露内部正则，避免 UI 或外部调用者依赖实现细节；真正执行前仍以
    classify_command_policy 的实时匹配结果为准。
    """

    rules: list[dict[str, object]] = []
    for rule in _HIGH_RISK_PATTERNS:
        category = rule.rule_id.split(".", 1)[0]
        rules.append(
            {
                "rule_id": rule.rule_id,
                "risk_level": "high_risk",
                "category": category,
                "default_action": "block",
                "reason": rule.reason,
                "destructive_warning": rule.warning,
                "examples": list(rule.examples),
                "safer_alternatives": list(rule.safer_alternatives),
            }
        )
    return rules


def _effective_policy_decision(
    *,
    risk_level: str,
    allowed: bool,
    requires_confirmation: bool,
    permission_policy: str,
) -> tuple[str, str, str]:
    """合成当前运行偏好下的命令处理预期。

    这里仍然只是“执行前预览”：真正运行命令时，Runtime 还必须按工作目录、参数、超时、
    输出截断和审计记录再次校验。高危或静态策略禁止的命令始终不能被高权限模式绕过。
    """

    policy = permission_policy.strip().lower()
    if policy not in VALID_PERMISSION_POLICIES:
        policy = "smart_confirm"

    if not allowed or risk_level == "high_risk":
        return (
            policy,
            "block",
            "命令被静态策略标记为高危或禁止执行，任何权限模式都不能自动绕过。",
        )

    if policy == "always_ask":
        if risk_level in {"modifying", "network"} or requires_confirmation:
            return (
                policy,
                "confirm",
                "请求批准模式会在修改、联网或其他有副作用的命令前询问用户。",
            )
        return policy, "allow", "请求批准模式允许只读定位和诊断类命令先进入审计执行。"

    if policy == "smart_confirm":
        if requires_confirmation:
            return policy, "confirm", "风险操作确认模式会要求用户确认该命令。"
        return policy, "allow", "当前未检测到需要确认的命令风险。"

    if policy == "auto_approve":
        if risk_level in {"modifying", "network"}:
            return (
                policy,
                "confirm",
                "替我审批模式不会自动批准修改型或联网命令。",
            )
        return policy, "allow", "替我审批模式可低摩擦放行只读定位和诊断类命令，并保留审计。"

    if policy == "full_access":
        return (
            policy,
            "allow",
            "完全访问模式可减少中低风险命令打断，但仍保留命令边界、超时和审计。",
        )

    return policy, "confirm", "未知权限模式已按风险操作确认处理。"


def _build_execution_preview(
    *,
    risk_level: str,
    allowed: bool,
    categories: list[str],
    detected_commands: list[str],
    cwd: str,
) -> dict[str, object]:
    """给 UI 和未来 Runtime 的命令执行壳提供审计预案。

    这里不执行命令，也不替代真正 sandbox。它把 Claude Code / Codex 一类 coding harness
    的经验收敛成 AgentFlow 自己的产品语义：优先专用工具、固定工作区、记录可复盘字段、
    对联网和修改动作保持人工确认入口。
    """

    audit_fields = [
        "task_id",
        "step_id",
        "agent_id",
        "command",
        "normalized_command",
        "cwd",
        "risk_level",
        "permission_policy",
        "policy_action",
        "timeout_ms",
        "max_output_chars",
        "exit_code",
        "duration_ms",
        "output_truncated",
    ]
    notes: list[str] = []

    if not allowed or risk_level == "high_risk":
        return {
            "execution_scope": "blocked",
            "execution_route": "blocked_by_command_governance",
            "cwd_policy": "不进入执行阶段；如用户确有需求，必须改写为更窄、更可恢复的安全操作。",
            "sandbox_hint": "平台硬拦截，不生成 Shell 执行请求。",
            "audit_fields": audit_fields + ["block_reason"],
            "execution_notes": [
                "高危或禁止命令不能被 full_access/完全访问模式自动绕过。",
                "建议先用只读命令确认目标范围，再拆成可审计的小步骤。",
            ],
        }

    if risk_level in {"none", "read_only"}:
        scope = "read_only" if risk_level == "read_only" else "none"
        route = "prefer_agentic_search_or_read_tool" if detected_commands else "no_execution_needed"
        notes.append("只读定位优先走 grep/ripgrep/glob/read 等专用工具，减少 Shell 解析风险。")
        notes.append("即便未来进入 Shell，也应限制输出长度，避免把大日志或大文件塞进上下文。")
        return {
            "execution_scope": scope,
            "execution_route": route,
            "cwd_policy": _cwd_policy_text(cwd, low_risk=True),
            "sandbox_hint": "只读路径可低摩擦执行；仍要固定 cwd 并记录审计。",
            "audit_fields": audit_fields,
            "execution_notes": notes,
        }

    if risk_level == "diagnostic":
        notes.append("测试、构建和类型检查应优先封装成诊断工具，记录退出码和截断输出。")
        notes.append("诊断命令可能耗时较长，必须设置超时，失败后用结构化错误回传。")
        return {
            "execution_scope": "diagnostic",
            "execution_route": "diagnostic_runner_after_policy_check",
            "cwd_policy": _cwd_policy_text(cwd, low_risk=False),
            "sandbox_hint": "优先在受控项目工作区执行；不应让命令修改系统路径或全局环境。",
            "audit_fields": audit_fields + ["test_summary", "failure_excerpt"],
            "execution_notes": notes,
        }

    if risk_level == "network":
        notes.append("联网命令要记录目标、用途和是否会上传上下文；失败时应给用户可理解原因。")
        notes.append("安装依赖、拉取代码或访问外部 API 前必须经过权限策略和成本/隐私提示。")
        return {
            "execution_scope": "network",
            "execution_route": "network_tool_or_shell_after_permission",
            "cwd_policy": _cwd_policy_text(cwd, low_risk=False),
            "sandbox_hint": "联网动作需要单独权限、短超时和输出截断；后续可接代理/域名 allowlist。",
            "audit_fields": audit_fields + ["network_target", "network_purpose"],
            "execution_notes": notes,
        }

    notes.append("修改型命令必须说明预期改动范围，执行前保留用户确认入口。")
    notes.append("文件写入优先走受控 workspace/outputs 工具，Shell 修改只作为后续受限能力。")
    return {
        "execution_scope": "modifying",
        "execution_route": "workspace_action_after_permission",
        "cwd_policy": _cwd_policy_text(cwd, low_risk=False),
        "sandbox_hint": "修改动作应限制在受控工作区，并记录预期文件变化；危险路径仍需平台硬拦截。",
        "audit_fields": audit_fields + ["expected_file_changes", "rollback_hint"],
        "execution_notes": notes,
    }


def _build_runtime_request_preview(
    *,
    command: str,
    normalized_command: str,
    risk_level: str,
    categories: list[str],
    detected_commands: list[str],
    rule_ids: list[str],
    allowed: bool,
    requires_confirmation: bool,
    effective_policy: str,
    effective_action: str,
    execution_preview: dict[str, object],
    default_timeout_ms: int,
    max_output_chars: int,
    cwd: str,
) -> dict[str, object]:
    """生成未来 Runtime Shell 请求的审计骨架。

    这一步仍然不执行命令，也不把静态检查结果当成执行许可。它只是把“接下来会直接进入
    执行、等待用户批准，还是被平台阻止”变成结构化字段，方便 Qt 展示和后续审计落库复用。
    """

    normalized = normalized_command.strip()
    if not normalized:
        status = "none"
        approval_prompt = "命令为空，不会创建运行请求。"
        block_reason_code = "command_empty"
    elif effective_action == "block" or not allowed:
        status = "blocked"
        approval_prompt = "该命令命中高危或禁止规则，平台不会创建自动执行请求。"
        block_reason_code = "command_governance_high_risk" if risk_level == "high_risk" else "command_governance_blocked"
    elif effective_action == "confirm":
        status = "needs_approval"
        approval_prompt = "该命令需要用户批准后才可以进入后续 Runtime 执行。请先核对命令、cwd、影响范围和审计字段。"
        block_reason_code = ""
    else:
        status = "ready"
        approval_prompt = "该命令可进入后续 Runtime 执行请求，但真正执行前仍会再次校验 cwd、权限、超时和输出截断。"
        block_reason_code = ""

    audit_record_preview = {
        "command": command,
        "normalized_command": normalized,
        "cwd": cwd,
        "risk_level": risk_level,
        "categories": categories,
        "detected_commands": detected_commands,
        "rule_ids": rule_ids,
        "permission_policy": effective_policy,
        "policy_action": effective_action,
        "runtime_request_status": status,
        "execution_scope": execution_preview.get("execution_scope", "none"),
        "execution_route": execution_preview.get("execution_route", ""),
        "cwd_policy": execution_preview.get("cwd_policy", ""),
        "timeout_ms": default_timeout_ms,
        "max_output_chars": max_output_chars,
        "requires_confirmation": requires_confirmation,
        "allowed_by_static_policy": allowed,
    }

    return {
        "runtime_ready": status == "ready",
        "permission_required": status == "needs_approval",
        "runtime_request_status": status,
        "approval_prompt": approval_prompt,
        "block_reason_code": block_reason_code,
        "audit_record_preview": audit_record_preview,
    }


def _cwd_policy_text(cwd: str, *, low_risk: bool) -> str:
    """解释未来执行时 cwd 的边界，不在静态检查阶段解析任意本机路径。"""

    clean_cwd = cwd.strip()
    if not clean_cwd:
        return (
            "未指定 cwd；未来执行前应由 Runtime 固定到当前受控 workspace 或项目根。"
            if low_risk
            else "未指定 cwd；中高风险命令执行前必须明确解析到受控 workspace 或项目根。"
        )
    if PurePath(clean_cwd).is_absolute():
        return "cwd 已是绝对路径；未来执行前仍需确认它位于用户授权的 workspace 范围内。"
    return "cwd 是相对路径；未来执行前必须先解析成受控 workspace 内的绝对路径。"


def _normalize_command(command: str) -> str:
    return " ".join(command.strip().split())


def _split_command_parts(command: str) -> list[str]:
    return [part.strip() for part in _SEPARATORS_RE.split(command) if part.strip()]


def _base_command(part: str) -> str:
    tokens = re.findall(r'"[^"]+"|\'[^\']+\'|\S+', part)
    if not tokens:
        return ""

    index = 0
    while index < len(tokens) and "=" in tokens[index] and not tokens[index].startswith(("-", "/")):
        index += 1
    if index >= len(tokens):
        return ""

    token = tokens[index].strip("\"'").lower()
    token = token.replace("\\", "/")
    token = token.rsplit("/", 1)[-1]
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        if token.endswith(suffix):
            token = token[: -len(suffix)]
            break
    return token


def _classify_part(base: str, part: str) -> tuple[str, str, str]:
    lower_part = part.lower()

    if base == "git":
        return _classify_git(lower_part)
    if base in {"npm", "pnpm", "yarn", "bun"}:
        return _classify_js_package_command(base, lower_part)
    if base in {"pip", "pip3"}:
        return ("network", "network", f"{base} 通常会访问包索引或安装依赖。")
    if base in {"python", "py"}:
        return _classify_python(lower_part)

    if base in _READ_ONLY_COMMANDS:
        return ("read_only", "read_only", f"{base} 属于只读定位/查看命令。")
    if base in _NETWORK_COMMANDS:
        return ("network", "network", f"{base} 涉及联网或外部数据传输。")
    if base in _MODIFYING_COMMANDS:
        return ("modifying", "modifying", f"{base} 可能修改文件、目录或版本库状态。")
    if base in _DIAGNOSTIC_COMMANDS:
        return ("diagnostic", "diagnostic", f"{base} 属于测试/构建/诊断类命令。")

    return ("modifying", "unknown", f"{base} 不在安全白名单中，按需要确认处理。")


def _classify_git(part: str) -> tuple[str, str, str]:
    if re.search(r"\bgit\s+(status|diff|log|show|branch|rev-parse|ls-files)\b", part):
        return ("read_only", "read_only", "git 只读查询命令。")
    if re.search(r"\bgit\s+(clone|pull|fetch|submodule\s+update)\b", part):
        return ("network", "network", "git 命令涉及联网或拉取外部代码。")
    return ("modifying", "modifying", "git 命令可能改变工作区、索引或历史。")


def _classify_js_package_command(base: str, part: str) -> tuple[str, str, str]:
    if re.search(rf"\b{base}\s+(test|run\s+test|lint|run\s+lint|build|run\s+build)\b", part):
        return ("diagnostic", "diagnostic", f"{base} 诊断/构建命令。")
    if re.search(rf"\b{base}\s+(install|add|update|upgrade|dlx|exec)\b", part):
        return ("network", "network", f"{base} 命令可能联网下载或执行包。")
    return ("diagnostic", "diagnostic", f"{base} 命令按开发诊断类处理。")


def _classify_python(part: str) -> tuple[str, str, str]:
    if re.search(r"\b(?:python|py)\s+-m\s+(pytest|unittest|compileall|ruff|mypy)\b", part):
        return ("diagnostic", "diagnostic", "Python 模块诊断/测试命令。")
    if re.search(r"\b(?:python|py)\s+-m\s+pip\s+install\b", part):
        return ("network", "network", "pip install 可能联网安装依赖。")
    return ("modifying", "unknown", "Python 脚本可能执行任意逻辑，默认需要确认。")


def _max_risk(left: str, right: str) -> str:
    return left if _RISK_ORDER[left] >= _RISK_ORDER[right] else right


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    for item in items:
        if item not in result:
            result.append(item)
    return result
