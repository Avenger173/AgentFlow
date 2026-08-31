"""总指挥可路由动作的准入目录。

manifest 说明 Agent 是否存在，Node Contract 说明节点的输入输出，而本模块回答更贴近
产品的问题：某个具体 action 现在能不能被总指挥委派、需要何种材料、如何验证，以及
失败后应把客户带向哪里。它不执行 Tool，也不读取用户文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

from app.schemas.agent import AgentDescriptor
from app.schemas.chat import WorkflowMaterialBinding


AdmissionExecutionMode = Literal["execute", "guided_handoff", "planning_only"]
AdmissionStatus = Literal["ready", "guided", "blocked"]


@dataclass(frozen=True)
class AgentActionAdmission:
    """一个由产品确认过的总指挥动作。

    ``guided_handoff`` 允许总指挥识别尚未开放自动委派的专业能力，但 Runtime 必须停在
    明确的用户行动点，不能把“打开工作台”伪装为专业 Agent 已执行。
    """

    agent_id: str
    action: str
    execution_mode: AdmissionExecutionMode
    requires_runtime_ready: bool
    material_kind: str | None
    expected_output: str
    verification_scope: str
    recovery_hint: str
    # 这类权限不是 manifest 中的底层 Tool 权限，而是产品级的执行预算确认。例如全库
    # 深度分析是只读操作，却会冻结整库并持续消耗模型额度，不能被当作普通 file_read 自动放行。
    additional_permissions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActionAdmissionDecision:
    admission: AgentActionAdmission
    status: AdmissionStatus
    reason: str


ACTION_ADMISSIONS: dict[tuple[str, str], AgentActionAdmission] = {
    ("commander_agent", "analyze_task"): AgentActionAdmission(
        agent_id="commander_agent",
        action="analyze_task",
        execution_mode="planning_only",
        requires_runtime_ready=True,
        material_kind=None,
        expected_output="可校验计划、材料范围、风险和下一步。",
        verification_scope="WorkflowPlan Validator 与 Agent action 准入检查。",
        recovery_hint="计划信息不足时转为最多三个关键澄清问题。",
    ),
    ("commander_agent", "direct_answer"): AgentActionAdmission(
        agent_id="commander_agent",
        action="direct_answer",
        execution_mode="planning_only",
        requires_runtime_ready=True,
        material_kind=None,
        expected_output="直接答复或下一步澄清问题。",
        verification_scope="无副作用；只记录任务与计划摘要。",
        recovery_hint="用户补充目标、材料或交付形式后重新规划。",
    ),
    ("commander_agent", "synthesize_results"): AgentActionAdmission(
        agent_id="commander_agent",
        action="synthesize_results",
        execution_mode="execute",
        requires_runtime_ready=True,
        material_kind=None,
        expected_output="仅基于已完成专业子任务脱敏摘要的最终汇总；未完成分支必须明确保留边界。",
        verification_scope="组合 DAG、并发白名单、共享预算、子任务状态与汇总范围二次校验。",
        recovery_hint="查看失败或阻塞的关联子任务后重试；已完成子任务不会被写成失败结果。",
    ),
    ("document_agent", "analyze_document"): AgentActionAdmission(
        agent_id="document_agent",
        action="analyze_document",
        execution_mode="execute",
        requires_runtime_ready=True,
        material_kind="document",
        expected_output="带来源的文档结论或后续文档交付子任务。",
        verification_scope="文档助手自身的受控读取、来源映射和输出契约。",
        recovery_hint="选择正确材料、缩小问题范围或查看关联子任务的失败说明。",
    ),
    ("document_agent", "search_text"): AgentActionAdmission(
        agent_id="document_agent",
        action="search_text",
        execution_mode="execute",
        requires_runtime_ready=True,
        material_kind="document",
        expected_output="仅在已绑定材料范围内的精确命中、定位与短上下文。",
        verification_scope="受控 workspace 搜索、材料范围校验和工具审计。",
        recovery_hint="修改搜索词、补充材料范围或转为文档理解任务。",
    ),
    ("document_agent", "open_presentation_studio"): AgentActionAdmission(
        agent_id="document_agent",
        action="open_presentation_studio",
        execution_mode="guided_handoff",
        requires_runtime_ready=False,
        material_kind=None,
        expected_output="已带入客户主题的智能制作 PPT 工作台入口。",
        verification_scope="只传递客户本轮主题，不读取文档、不调用模型、不创建或导出文件。",
        recovery_hint="在智能制作 PPT 工作台确认创作计划；导出 PPTX 时仍单独确认文件写入与可选外部素材。",
    ),
    ("knowledge_agent", "answer_question"): AgentActionAdmission(
        agent_id="knowledge_agent",
        action="answer_question",
        execution_mode="execute",
        requires_runtime_ready=True,
        material_kind="knowledge_base",
        expected_output="仅依据所选资料库当前活动版本生成的带来源可信回答。",
        verification_scope="K2 混合检索、K3 Evidence Gate、claim/source_id Verifier 与活动版本二次核验。",
        recovery_hint="选择正确且已完成索引的资料库；资料不足时补充材料或缩小问题范围。",
    ),
    ("knowledge_agent", "deep_summary"): AgentActionAdmission(
        agent_id="knowledge_agent",
        action="deep_summary",
        execution_mode="execute",
        requires_runtime_ready=True,
        material_kind="knowledge_base",
        expected_output="已冻结范围的全库深度总结子任务；最终结论、检查点与报告在关联子任务中查看。",
        verification_scope="K4 全章节 Map checkpoint、递归 Reduce、恢复/暂停/取消和报告导出资格校验。",
        recovery_hint="在关联子任务查看实时阶段；可在安全边界暂停、继续或取消，不会重跑已完成章节。",
        additional_permissions=("knowledge_deep_analysis",),
    ),
    ("data_agent", "open_workspace"): AgentActionAdmission(
        agent_id="data_agent",
        action="open_workspace",
        execution_mode="guided_handoff",
        requires_runtime_ready=False,
        material_kind="dataset",
        expected_output="带预填分析目标的数据工作台入口。",
        verification_scope="当前不创建子任务；D5.4 完成前只能由数据工作台独立完成画像和交付。",
        recovery_hint="在数据工作台选择或重新导入 CSV/XLSX，再从当前材料开始分析。",
    ),
    ("data_agent", "analyze_dataset"): AgentActionAdmission(
        agent_id="data_agent",
        action="analyze_dataset",
        execution_mode="execute",
        requires_runtime_ready=True,
        material_kind="dataset",
        expected_output="单份已导入数据的只读画像、确定性聚合、图表建议和可追溯短结论。",
        verification_scope="数据集显式绑定、D1 画像、D2 白名单计算、L1/L2 脱敏结论和父子任务审计。",
        recovery_hint="回到数据工作台确认当前数据文件、字段画像和分析目标后重新委派；不会重放或修改历史数据。",
    ),
    ("data_agent", "export_chart_dashboard"): AgentActionAdmission(
        agent_id="data_agent",
        action="export_chart_dashboard",
        execution_mode="execute",
        requires_runtime_ready=True,
        material_kind="dataset",
        expected_output="基于一份已导入数据生成并像素回读验证的 PNG 图表交付物。",
        verification_scope="数据版本哈希、D2 白名单聚合、PNG 像素回读、受控 outputs 路径与任务 artifact 审计。",
        recovery_hint="确认数据文件和目标后重新规划；任何失败都不会修改原始 CSV/XLSX 或覆盖已有图表。",
        additional_permissions=("file_write",),
    ),
    ("data_agent", "export_analysis_workbook"): AgentActionAdmission(
        agent_id="data_agent",
        action="export_analysis_workbook",
        execution_mode="execute",
        requires_runtime_ready=True,
        material_kind="dataset",
        expected_output="基于一份已导入数据新建并回读验证的分析 Excel 工作簿。",
        verification_scope="数据版本哈希、D2 白名单计算、原生表格/图表/指标回读、受控 outputs 路径与任务 artifact 审计。",
        recovery_hint="确认数据文件和目标后重新规划；任何失败都不会修改原始 CSV/XLSX 或覆盖已有工作簿。",
        additional_permissions=("file_write",),
    ),
    ("data_agent", "plan_field_transform"): AgentActionAdmission(
        agent_id="data_agent",
        action="plan_field_transform",
        execution_mode="execute",
        requires_runtime_ready=True,
        material_kind="dataset",
        expected_output="依据当前数据画像生成受限字段加工预览，不修改源文件。",
        verification_scope="数据版本哈希、字段画像、操作白名单、类型兼容性和结果字段冲突校验。",
        recovery_hint="字段或类型不明确时请补充列名；预览失败不会修改源 CSV/XLSX。",
    ),
    ("data_agent", "export_field_transform"): AgentActionAdmission(
        agent_id="data_agent",
        action="export_field_transform",
        execution_mode="execute",
        requires_runtime_ready=True,
        material_kind="dataset",
        expected_output="在受控 outputs 中生成追加派生字段的新 CSV/XLSX 副本，并完成回读验证。",
        verification_scope="源版本哈希、新字段、行数、输出格式和副本回读验证。",
        recovery_hint="确认字段加工预览后再执行；失败时不覆盖源文件或已有交付物。",
        additional_permissions=("file_write",),
    ),
}


def action_admission_for(agent_id: str, action: str) -> AgentActionAdmission | None:
    """按精确 Agent/action 查找准入项，不做模糊匹配。"""

    return ACTION_ADMISSIONS.get((agent_id, action))


def evaluate_action_admission(
    *,
    agent_id: str,
    action: str,
    agents: Iterable[AgentDescriptor],
    materials: Iterable[WorkflowMaterialBinding] = (),
) -> ActionAdmissionDecision:
    """根据 Registry 生命周期与材料引用返回可执行、引导或阻塞状态。"""

    admission = action_admission_for(agent_id, action)
    if admission is None:
        # 未登记动作没有安全默认值；调用方只能把它留在解释层，不能送进 Runtime。
        fallback = AgentActionAdmission(
            agent_id=agent_id,
            action=action,
            execution_mode="planning_only",
            requires_runtime_ready=True,
            material_kind=None,
            expected_output="",
            verification_scope="",
            recovery_hint="该动作尚未完成总指挥准入。",
        )
        return ActionAdmissionDecision(fallback, "blocked", "该 Agent 动作尚未完成总指挥准入。")

    # API 请求始终传入 Registry 快照。保留空列表兼容性是为了让纯规划单元测试可以只
    # 验证材料/结构，不把“没有注入测试 Registry”误判为产品中的 Agent 不可用。
    # Runtime 启动前仍会用真实 Registry 和 Validator 再做一次完整校验。
    agent_list = list(agents)
    if agent_list:
        agent = next((item for item in agent_list if item.id == agent_id), None)
        if agent is None or not agent.enabled:
            return ActionAdmissionDecision(admission, "blocked", "对应 Agent 当前不存在或未启用。")
        if agent.health != "ready":
            return ActionAdmissionDecision(admission, "blocked", "对应 Agent 当前健康状态不是 ready。")
        if admission.requires_runtime_ready and not agent.runtime_ready:
            return ActionAdmissionDecision(admission, "blocked", "对应 Agent 尚未通过可执行 Runtime 准入。")
    if admission.material_kind and not any(item.kind == admission.material_kind for item in materials):
        labels = {
            "document": "文档",
            "dataset": "数据文件",
            "knowledge_base": "资料库",
        }
        label = labels.get(admission.material_kind, "材料")
        return ActionAdmissionDecision(admission, "blocked", f"本步骤需要先明确绑定一份{label}。")
    if admission.execution_mode == "guided_handoff":
        return ActionAdmissionDecision(admission, "guided", "该能力已被识别，但当前只能引导至专业工作台继续。")
    return ActionAdmissionDecision(admission, "ready", "已通过当前总指挥动作准入。")


def list_action_admissions() -> list[AgentActionAdmission]:
    """返回稳定排序的准入目录，供后续 API、UI 与回归验证复用。"""

    return sorted(ACTION_ADMISSIONS.values(), key=lambda item: (item.agent_id, item.action))
