from __future__ import annotations

from pathlib import PurePath
import re
from typing import Iterable

from app.schemas.agent import AgentDescriptor
from app.schemas.chat import (
    CommanderAgentHint,
    RiskLevel,
    WorkflowBudgetEstimate,
    WorkflowCommandPolicy,
    WorkflowMaterialBinding,
    WorkflowPlan,
    WorkflowPlanPreferences,
    WorkflowRetryPolicy,
    WorkflowStep,
    WorkflowWorkspaceScope,
)
from app.schemas.memory import LongTermMemoryRecord
from app.services.long_term_memory import build_memory_context_summary
from app.services.data_transform_intent import DataTransformIntentError, build_data_transform_intent
from app.workflow.action_admission import (
    ActionAdmissionDecision,
    evaluate_action_admission,
)
from app.workflow.node_contracts import NODE_CONTRACTS
from app.workflow.validator import validate_workflow_plan


COMMANDER_AGENT_ID = "commander_agent"


DOCUMENT_ROUTE_KEYWORDS = (
    # “分析/整理/总结/提取”等动词同样适用于数据，不能单独把用户误导到文档工作台。
    # 一旦客户端已绑定材料，材料类型优先于宽泛语言信号；未绑定时只用此处较明确的
    # 文档提示触发一次澄清。
    "文档", "文件", "资料", "作业", "需求", "读取", "归纳",
    "txt", "markdown", "pdf", "word", "docx",
)
DATA_ROUTE_KEYWORDS = (
    "数据", "表格", "csv", "excel", "xlsx", "趋势", "图表", "字段", "数据集",
    "折线图", "柱状图", "饼图", "环形图", "散点图", "仪表盘", "看板",
)
DATA_CHART_DELIVERY_KEYWORDS = (
    "生成图表", "制作图表", "导出图表", "保存图表", "图表看板", "图表png", "图表 png",
    "生成折线图", "制作折线图", "导出折线图",
    "生成柱状图", "制作柱状图", "导出柱状图",
    "生成饼图", "制作饼图", "导出饼图",
    "生成环形图", "制作环形图", "导出环形图",
)
DATA_WORKBOOK_DELIVERY_KEYWORDS = (
    "生成分析excel", "生成分析 excel", "导出分析excel", "导出分析 excel",
    "生成分析xlsx", "生成分析 xlsx", "导出分析xlsx", "导出分析 xlsx",
    "生成分析工作簿", "导出分析工作簿", "生成数据工作簿", "导出数据工作簿",
    "生成excel报表", "生成 excel 报表", "导出excel报表", "导出 excel 报表",
)
DATA_TRANSFORM_KEYWORDS = (
    "字段加工", "计算列", "新增字段", "增加字段", "加工字段", "派生字段",
    "排名", "排行", "累计", "环比", "占比", "份额", "四舍五入", "保留小数",
    "月份字段", "提取月份", "分段", "分档", "去空格", "清理空格", "规范化文本",
    "相加", "相减", "相乘", "相除", "计算比率",
)
PRESENTATION_ROUTE_KEYWORDS = (
    "ppt", "pptx", "演示文稿", "幻灯片", "幻灯",
)
KNOWLEDGE_ROUTE_KEYWORDS = (
    "知识库", "资料库", "根据资料", "查资料", "问资料", "引用来源",
)
# C5 只允许客户明确要求整库级的深度总结。资料对照必须由知识库工作台逐份选择资料，
# 因而不能根据一句“比较”或单一资料库绑定擅自推断比较对象。
KNOWLEDGE_DEEP_ROUTE_KEYWORDS = (
    "深度分析", "深度总结", "全库总结", "全库分析", "整库总结", "整库分析",
    "完整梳理", "逐章总结", "逐章分析", "深度报告",
)
# 这些别名只服务于总指挥输入的 `@` 路由提示。它们不能映射到未完成 Agent、插件、
# MCP 或任意文件路径，防止看似便利的文本标签扩大实际执行面。
_AGENT_HINT_ALIASES: dict[str, tuple[str, ...]] = {
    "document_agent": ("文档助手", "文档", "document", "document_agent"),
    "data_agent": ("数据工作台", "数据", "data", "data_agent"),
    "knowledge_agent": ("知识库", "知识库助手", "knowledge", "knowledge_agent"),
}
# C6.4 的 Native 组合 Runtime 只接收已有正式子任务入口、只读边界与独立验证器的动作。
# 这份规划期白名单与 Runtime 的二次核验必须保持一致；新增动作要先讨论并发、预算、
# 权限和恢复语义，不能因为页面能带入材料就自动加入组合执行。
_NATIVE_COMPOSITION_ACTIONS = {
    ("document_agent", "analyze_document"),
    ("document_agent", "search_text"),
    ("data_agent", "analyze_dataset"),
    ("knowledge_agent", "answer_question"),
}


def create_commander_plan(
    message: str,
    available_agents: Iterable[AgentDescriptor] | None = None,
    preferences: WorkflowPlanPreferences | None = None,
    materials: Iterable[WorkflowMaterialBinding] | None = None,
    agent_hints: Iterable[CommanderAgentHint] | None = None,
    memory_context: Iterable[LongTermMemoryRecord] | None = None,
    project_scope: str = "global",
    conversation_id: str = "",
    conversation_context_summary: list[str] | None = None,
) -> WorkflowPlan:
    """生成具备材料绑定与动作准入边界的 Commander 计划。

    C0/C1 仍让确定性规则承担最终的路由与权限边界：真实模型只负责对话表达，不能仅凭
    一段自然语言把未准入的 Agent/action 推进到 Runtime。规划器只遍历已加载的 Agent
    描述与客户显式材料引用，不扫描 workspace 或数据目录，因此请求路径保持低成本。
    """

    available_agent_list = list(available_agents or [])
    memory_context_summary = build_memory_context_summary(memory_context or ())
    supplied_material_bindings = _normalize_material_bindings(message, materials or ())
    normalized_agent_hints = _normalize_agent_hints(message, agent_hints or ())
    hinted_agent_ids = {hint.agent_id for hint in normalized_agent_hints}
    # `@` 是路由偏好，而不是“本轮只能使用这个页面”的强制开关。客户常会先从某个
    # 工作台带入材料、再提出另一种目标，例如挂着 CSV 后要求制作 PPT；这时任务意图
    # 必须优先于残留标签和材料。真正进入哪个 Agent 仍由下面的受控规则与动作准入决定。
    material_bindings = list(supplied_material_bindings)
    document_refs = _material_refs(material_bindings, kind="document")
    dataset_refs = _material_refs(material_bindings, kind="dataset")
    knowledge_base_refs = _material_refs(material_bindings, kind="knowledge_base")
    lowered = message.lower()
    search_query = _extract_workspace_search_query(message)
    knowledge_deep_requested = _matches_any(lowered, KNOWLEDGE_DEEP_ROUTE_KEYWORDS)
    # PPT 创作拥有最高的显式意图优先级。即使调度台仍挂着上一次的数据集或资料库，
    # “帮我做 PPT”也不能被错误解释成“分析当前数据/资料库”。
    presentation_requested = _matches_any(lowered, PRESENTATION_ROUTE_KEYWORDS)
    knowledge_intent_requested = _matches_any(lowered, KNOWLEDGE_ROUTE_KEYWORDS) or knowledge_deep_requested
    document_intent_requested = _matches_any(lowered, DOCUMENT_ROUTE_KEYWORDS)
    data_intent_requested = _matches_any(lowered, DATA_ROUTE_KEYWORDS)
    data_transform_intent_requested = _matches_any(lowered, DATA_TRANSFORM_KEYWORDS)
    explicit_specialist_intent = (
        knowledge_intent_requested
        or document_intent_requested
        or data_intent_requested
    )
    # 已选材料是“本轮可用的候选上下文”，而非隐式命令。否则客户从数据页跳到调度台
    # 后随口问一个普通问题，也会被残留 CSV 强行劫持。只有目标本身出现处理意图时，
    # 才把已选材料交给对应专业 Agent；`@` 在没有冲突目标时提供一个温和的偏好兜底。
    material_task_requested = _requests_bound_material_work(message)
    knowledge_requested = not presentation_requested and (
        knowledge_intent_requested
        or (
            bool(knowledge_base_refs)
            and material_task_requested
            and not explicit_specialist_intent
        )
        or (
            "knowledge_agent" in hinted_agent_ids
            and not (document_intent_requested or data_intent_requested)
            and _hint_can_influence_route(
                agent_id="knowledge_agent",
                message=lowered,
                material_refs=knowledge_base_refs,
            )
        )
    )
    # C6.3 允许客户明确组合文档、资料库和数据集；材料引用优先于模糊关键词。
    # 只有没有资料库绑定时，才把“资料”等宽泛词当作普通文档意图，避免单独问资料库
    # 时额外产生“请选择文档”的噪声澄清。
    document_requested = not presentation_requested and (
        document_intent_requested
        or (
            bool(document_refs)
            and material_task_requested
            and not explicit_specialist_intent
        )
        or (
            "document_agent" in hinted_agent_ids
            and not (knowledge_intent_requested or data_intent_requested)
            and _hint_can_influence_route(
                agent_id="document_agent",
                message=lowered,
                material_refs=document_refs,
            )
        )
    )
    data_requested = not presentation_requested and (
        data_intent_requested
        or data_transform_intent_requested
        or (
            bool(dataset_refs)
            and material_task_requested
            and not explicit_specialist_intent
        )
        or (
            "data_agent" in hinted_agent_ids
            and not (knowledge_intent_requested or document_intent_requested)
            and _hint_can_influence_route(
                agent_id="data_agent",
                message=lowered,
                material_refs=dataset_refs,
            )
        )
    )
    data_chart_delivery_requested = data_requested and _requests_data_chart_delivery(lowered)
    data_workbook_delivery_requested = data_requested and _requests_data_workbook_delivery(lowered)
    data_transform_requested = data_requested and data_transform_intent_requested
    needs_document_understanding = _needs_document_understanding(message)
    clarifying_questions: list[str] = []
    steps: list[WorkflowStep] = [
        _build_admitted_step(
            step_id="step_1",
            agent_id=COMMANDER_AGENT_ID,
            action="analyze_task",
            title="分析用户任务",
            depends_on=[],
            step_input={"message": message},
            reason="总指挥先绑定用户明确提供的材料，识别目标、边界和当前允许的专业能力。",
            agents=available_agent_list,
            materials=material_bindings,
            timeout_ms=30000,
        )
    ]

    next_index = 2
    specialist_step_ids: list[str] = []

    if knowledge_requested:
        if not knowledge_base_refs:
            clarifying_questions.append("已点名 @知识库，但尚未选择资料库；请先在知识库页面选择一个资料库后再委派。")
        elif len(knowledge_base_refs) > 1:
            clarifying_questions.append("当前知识库委派一次只处理一个资料库；请保留最相关的一个资料库后重试。资料对照请在知识库工作台逐份选择资料。")
        else:
            knowledge_base_id = knowledge_base_refs[0]
            action = "deep_summary" if knowledge_deep_requested else "answer_question"
            specialist_step_id = f"step_{next_index}"
            decision = _append_admitted_step(
                steps=steps,
                step_id=specialist_step_id,
                agent_id="knowledge_agent",
                action=action,
                title="委派知识库执行全库深度总结" if knowledge_deep_requested else "委派知识库生成带来源回答",
                depends_on=["step_1"],
                # K4 深度任务只返回后台受理回执，不能被父任务误当成已经得到可汇总结论。
                parallel_group="" if knowledge_deep_requested else "specialist_read_only",
                step_input=(
                    {
                        "knowledge_base_id": knowledge_base_id,
                        "task_goal": message,
                        "task_kind": "summary",
                        "delegation_mode": "background_child_task",
                    }
                    if knowledge_deep_requested
                    else {
                        "knowledge_base_id": knowledge_base_id,
                        "query": message,
                    }
                ),
                reason=(
                    "客户已明确选择资料库并主动要求全库级深度总结；执行前必须确认预算。确认后只冻结当前活动版本，"
                    "由 K4 子任务在后台执行、保存检查点并提供暂停/继续/取消。"
                    if knowledge_deep_requested
                    else "客户已明确选择资料库；知识库助手只读取其当前活动版本，并通过证据与引用校验后返回结论。"
                ),
                agents=available_agent_list,
                materials=material_bindings,
                # 总指挥只等待“子任务已受理并冻结范围”，不会在父 Runtime 同步等待整库 Map/Reduce。
                timeout_ms=30000 if knowledge_deep_requested else 120000,
            )
            if decision is not None:
                specialist_step_ids.append(specialist_step_id)
                next_index += 1
            else:
                clarifying_questions.append("知识库当前未通过总指挥动作准入，请检查资料库索引状态或后端健康状态。")

    if presentation_requested:
        # 从一句主题开始创作 PPT 不等于“分析一份文档”。这个明确目标始终进入现有的
        # 智能制作工作台，而不会被当前残留的数据集、资料库或 `@数据工作台` 标签劫持。
        specialist_step_id = f"step_{next_index}"
        decision = _append_admitted_step(
            steps=steps,
            step_id=specialist_step_id,
            agent_id="document_agent",
            action="open_presentation_studio",
            title="打开智能制作 PPT 工作台",
            depends_on=["step_1"],
            step_input={"task_goal": message},
            reason="客户明确要求制作 PPT；直接带入主题进入创作工作台，避免错误要求先分析无关材料。",
            agents=available_agent_list,
            materials=material_bindings,
            timeout_ms=30_000,
        )
        if decision is not None:
            specialist_step_ids.append(specialist_step_id)
            next_index += 1
        else:
            clarifying_questions.append("智能制作 PPT 当前不可用，请在文档助手页面检查工作台状态后重试。")
    elif document_requested:
        if not document_refs:
            clarifying_questions.append("已点名 @文档助手，但尚未选择文档；请先导入并选择 TXT、Markdown、PDF 或 DOCX 材料。")
        elif search_query and not needs_document_understanding:
            specialist_step_id = f"step_{next_index}"
            decision = _append_admitted_step(
                steps=steps,
                step_id=specialist_step_id,
                agent_id="document_agent",
                action="search_text",
                title="在已选文档中精确定位内容",
                depends_on=["step_1"],
                parallel_group="specialist_read_only",
                step_input={
                    "query": search_query,
                    "document_refs": document_refs,
                    "auto_read_if_unique": True,
                },
                reason="用户要求定位内容；先在明确选择的材料范围内进行低成本精确搜索。",
                agents=available_agent_list,
                materials=material_bindings,
                timeout_ms=30000,
            )
            if decision is not None:
                specialist_step_ids.append(specialist_step_id)
                next_index += 1
            else:
                clarifying_questions.append("文档搜索当前不可执行，请检查文档助手状态或重新选择材料。")
        else:
            specialist_step_id = f"step_{next_index}"
            decision = _append_admitted_step(
                steps=steps,
                step_id=specialist_step_id,
                agent_id="document_agent",
                action="analyze_document",
                title="委派文档助手生成可追溯结论",
                depends_on=["step_1"],
                parallel_group="specialist_read_only",
                step_input={
                    "task_goal": message,
                    "document_refs": document_refs,
                    "output_mode": _document_output_mode(message),
                },
                reason="已绑定明确文档材料；由文档助手负责受控读取、来源映射和专业结论。",
                agents=available_agent_list,
                materials=material_bindings,
                timeout_ms=120000,
            )
            if decision is not None:
                specialist_step_ids.append(specialist_step_id)
                next_index += 1
            else:
                clarifying_questions.append("文档助手当前未通过总指挥动作准入，暂不能自动委派。")

    if data_requested:
        if not dataset_refs:
            clarifying_questions.append("已点名 @数据工作台，但尚未选择数据文件；请先在数据工作台完成导入和画像。")
        elif len(dataset_refs) > 1:
            clarifying_questions.append("数据工作台一次只分析一份已明确选择的数据文件；请保留最相关的一份后重试。")
        else:
            dataset_name = dataset_refs[0]
            if data_transform_requested:
                try:
                    intent = build_data_transform_intent(dataset_name, message)
                except DataTransformIntentError as exc:
                    clarifying_questions.append(str(exc))
                else:
                    transform_input = {
                        "task_goal": message,
                        "dataset_name": dataset_name,
                        "dataset_refs": [dataset_name],
                        "source_sha256": intent.source_sha256,
                        "operations": [operation.model_dump(mode="json") for operation in intent.operations],
                        "cleaning_policy": "safe",
                        "intent_version": "agentflow.data_transform_intent.v1",
                    }
                    plan_step_id = f"step_{next_index}"
                    plan_decision = _append_admitted_step(
                        steps=steps,
                        step_id=plan_step_id,
                        agent_id="data_agent",
                        action="plan_field_transform",
                        title="规划字段加工并生成预览",
                        depends_on=["step_1"],
                        parallel_group="specialist_read_only",
                        step_input=transform_input,
                        reason="已根据当前数据画像和客户目标生成受限字段加工计划；先预览，不写入源文件。",
                        agents=available_agent_list,
                        materials=material_bindings,
                        timeout_ms=120000,
                    )
                    if plan_decision is not None:
                        specialist_step_ids.append(plan_step_id)
                        next_index += 1
                        export_step_id = f"step_{next_index}"
                        export_decision = _append_admitted_step(
                            steps=steps,
                            step_id=export_step_id,
                            agent_id="data_agent",
                            action="export_field_transform",
                            title="确认后生成字段加工副本",
                            depends_on=[plan_step_id],
                            step_input=transform_input,
                            reason="客户确认后在受控 outputs 中新建 CSV/XLSX 副本，追加字段并完成回读验证。",
                            agents=available_agent_list,
                            materials=material_bindings,
                            timeout_ms=150_000,
                        )
                        if export_decision is not None:
                            specialist_step_ids.append(export_step_id)
                            next_index += 1
                    else:
                        clarifying_questions.append("字段加工计划当前未通过总指挥动作准入，请检查数据工作台状态。")
            else:
                specialist_step_id = f"step_{next_index}"
                decision = _append_admitted_step(
                    steps=steps,
                    step_id=specialist_step_id,
                    agent_id="data_agent",
                    action="analyze_dataset",
                    title="委派数据工作台生成只读分析预览",
                    depends_on=["step_1"],
                    parallel_group="specialist_read_only",
                    step_input={
                        "task_goal": message,
                        "dataset_name": dataset_name,
                        "dataset_refs": [dataset_name],
                        "cleaning_policy": "safe",
                        "max_chart_count": 4,
                    },
                    reason=(
                        "已绑定一份导入数据；数据工作台将复用本地画像和白名单聚合生成只读结论。"
                        "客户明确要求图表或分析 Excel 时会进入单独的受控交付步骤；字段加工仍需单独确认。"
                    ),
                    agents=available_agent_list,
                    materials=material_bindings,
                    timeout_ms=120000,
                )
                if decision is not None:
                    specialist_step_ids.append(specialist_step_id)
                    next_index += 1
                    if data_chart_delivery_requested:
                        chart_step_id = f"step_{next_index}"
                        chart_decision = _append_admitted_step(
                            steps=steps,
                            step_id=chart_step_id,
                            agent_id="data_agent",
                            action="export_chart_dashboard",
                            title="生成可保存的数据图表 PNG",
                            depends_on=[specialist_step_id],
                            step_input={
                                "task_goal": message,
                                "dataset_name": dataset_name,
                                "dataset_refs": [dataset_name],
                                "cleaning_policy": "safe",
                                "max_chart_count": 4,
                            },
                            reason="客户明确要求生成图表；先完成同一份数据的只读分析，再在受控 outputs 中写入并回读 PNG。",
                            agents=available_agent_list,
                            materials=material_bindings,
                            timeout_ms=150_000,
                        )
                        if chart_decision is not None:
                            specialist_step_ids.append(chart_step_id)
                            next_index += 1
                    if data_workbook_delivery_requested:
                        workbook_step_id = f"step_{next_index}"
                        workbook_decision = _append_admitted_step(
                            steps=steps,
                            step_id=workbook_step_id,
                            agent_id="data_agent",
                            action="export_analysis_workbook",
                            title="生成可编辑的分析 Excel 工作簿",
                            depends_on=[specialist_step_id],
                            step_input={
                                "task_goal": message,
                                "dataset_name": dataset_name,
                                "dataset_refs": [dataset_name],
                                "cleaning_policy": "safe",
                                "max_chart_count": 4,
                            },
                            reason="客户明确要求生成分析 Excel；先完成同一份数据的只读分析，再在受控 outputs 中新建并回读工作簿。",
                            agents=available_agent_list,
                            materials=material_bindings,
                            timeout_ms=150_000,
                        )
                        if workbook_decision is not None:
                            specialist_step_ids.append(workbook_step_id)
                            next_index += 1
                else:
                    clarifying_questions.append("数据工作台当前不可用，请检查数据文件或后端状态。")

    if len(steps) == 1:
        steps.append(
            _build_admitted_step(
                step_id="step_2",
                agent_id=COMMANDER_AGENT_ID,
                action="direct_answer",
                title="直接回答或补充任务信息",
                depends_on=["step_1"],
                step_input={"message": message},
                reason="当前没有满足材料与准入边界的专业委派，先提供直接答复或明确下一步。",
                agents=available_agent_list,
                materials=material_bindings,
                timeout_ms=30000,
            )
        )

    # C6.4 只解除已验证的只读组合。未知、深度或后台型动作仍保留 C6.3 的 Runtime 保护，
    # 以免“组合”变成绕过各自检查点和权限边界的捷径。
    specialist_agent_ids = {
        step.agent for step in steps if step.id in specialist_step_ids
    }
    native_composition_supported = (
        len(specialist_agent_ids) > 1
        and all(
            (step.agent, step.action) in _NATIVE_COMPOSITION_ACTIONS
            for step in steps
            if step.id in specialist_step_ids
        )
    )
    # 同一个专业 Agent 的“分析 -> 导出”是单 Agent 的顺序工作流，不应被误标为
    # 多 Agent 组合任务，更不能因此丢掉已经批准的 Runtime 与权限语义。
    if len(specialist_agent_ids) > 1:
        _append_admitted_step(
            steps=steps,
            step_id=f"step_{next_index}",
            agent_id=COMMANDER_AGENT_ID,
            action="synthesize_results",
            title="等待专业结果后汇总交付",
            depends_on=specialist_step_ids,
            step_input={
                "child_step_ids": specialist_step_ids,
                "composition_mode": (
                    "native_read_only_c6_4"
                    if native_composition_supported
                    else "plan_only_until_supported"
                ),
            },
            reason=(
                "多个已绑定材料之间没有数据依赖；C6.4 会在有限并发和共享预算内运行已支持的"
                "只读子任务，并且只汇总实际完成的结果。"
                if native_composition_supported
                else "多个材料包含尚未支持即时汇总的后台或深度动作；先保留依赖图和边界，"
                "不会提前执行或伪造组合交付。"
            ),
            agents=available_agent_list,
            materials=material_bindings,
            timeout_ms=30000,
        )

    max_risk_level = _max_risk_level(steps)
    requires_confirmation = any(step.requires_confirmation for step in steps)
    clarifying_questions.extend(_build_clarifying_questions(message, steps, material_bindings))
    clarifying_questions = list(dict.fromkeys(clarifying_questions))[:3]
    has_guided_handoff = any(step.execution_mode == "guided_handoff" for step in steps)
    guided_handoff_action = next(
        (step.action for step in steps if step.execution_mode == "guided_handoff"),
        "",
    )
    requires_composition_runtime = (
        len(specialist_agent_ids) > 1 and not native_composition_supported
    )
    plan = WorkflowPlan(
        workflow_name="commander_manager_plan",
        description="Commander 基于显式材料绑定与 Agent action 准入生成的结构化计划。",
        intent=_infer_intent(steps),
        user_goal=message,
        summary=_build_plan_summary(steps),
        clarifying_questions=clarifying_questions,
        assumptions=_build_assumptions(
            steps,
            memory_context_summary,
            conversation_context_summary=conversation_context_summary,
        ),
        definition_of_done=_build_definition_of_done(steps),
        preference_applied=preferences or WorkflowPlanPreferences(),
        budget_estimate=_build_budget_estimate(steps),
        workspace_scope=_build_workspace_scope(steps, material_bindings),
        material_bindings=material_bindings,
        agent_hints=normalized_agent_hints,
        project_scope=project_scope,
        conversation_id=conversation_id,
        memory_context_summary=memory_context_summary,
        conversation_context_summary=(conversation_context_summary or [])[:2],
        steps=steps,
        max_risk_level=max_risk_level,
        requires_confirmation=requires_confirmation,
        execution_readiness=(
            "requires_composition_runtime" if requires_composition_runtime else "ready"
        ),
        next_action=_next_action(
            requires_confirmation=requires_confirmation,
            clarifying_questions=clarifying_questions,
            has_guided_handoff=has_guided_handoff,
            guided_handoff_action=guided_handoff_action,
            requires_composition_runtime=requires_composition_runtime,
        ),
    )
    # Commander 生成后立即自检。当前规则规划一般不会失败，但这一步能提前固定
    # 后续 LLM JSON 规划和 Workflow Engine 的安全入口。
    return plan.model_copy(
        update={"validation_errors": validate_workflow_plan(plan, available_agents=available_agent_list)}
    )


def _normalize_agent_hints(
    message: str,
    supplied_hints: Iterable[CommanderAgentHint],
) -> list[CommanderAgentHint]:
    """合并客户端标签与消息中的受控 `@` 别名。

    Qt 标签只是体验层，真正的规划器必须能独立从文本得到相同的、有限的偏好。未知
    `@xxx` 被安全忽略，而不是转成动态 Agent 发现或权限申请。
    """

    selected_ids = {hint.agent_id for hint in supplied_hints}
    normalized_message = message.casefold()
    for agent_id, aliases in _AGENT_HINT_ALIASES.items():
        if any(f"@{alias.casefold()}" in normalized_message for alias in aliases):
            selected_ids.add(agent_id)  # type: ignore[arg-type]

    order = ("document_agent", "data_agent", "knowledge_agent")
    return [
        CommanderAgentHint(agent_id=agent_id, source="mention")
        for agent_id in order
        if agent_id in selected_ids
    ]


def _filter_materials_for_agent_hints(
    materials: list[WorkflowMaterialBinding],
    hinted_agent_ids: set[str],
) -> list[WorkflowMaterialBinding]:
    """把显式 `@` 偏好同步到计划可读材料边界。

    调度台允许客户先挂多份材料再点名一个 Agent。未点名时维持 C6.3 的组合材料行为；
    一旦点名，只把该专业能力能够处理的材料写入本轮 plan/conversation 快照，避免后续
    Runtime 或下一轮指代把“本来只是挂着”的私有材料误当成已经授权的范围。
    """

    if not hinted_agent_ids:
        return materials
    allowed_kinds: set[str] = set()
    if "document_agent" in hinted_agent_ids:
        allowed_kinds.add("document")
    if "data_agent" in hinted_agent_ids:
        allowed_kinds.add("dataset")
    if "knowledge_agent" in hinted_agent_ids:
        allowed_kinds.add("knowledge_base")
    return [material for material in materials if material.kind in allowed_kinds]


def build_commander_reply(*, mode: str, has_plan: bool) -> str:
    """生成调度台回复文案。

    回复文案和 workflow_plan 分离：以后真实 LLM 负责自然语言回复时，
    Commander 仍可以独立生成或校验结构化计划。
    """

    if not has_plan:
        return "已收到任务。当前没有生成多 Agent 工作流，将按普通对话处理。"

    if mode == "llm":
        return "已收到任务。Commander 已生成初版工作流计划，真实模型回复会和后续执行日志一起展示。"

    return (
        "已收到任务。当前是模拟模式：Commander 已绑定材料并生成可审计计划，"
        "后续会在用户确认后交给 Workflow Engine 执行并通过 WebSocket 推送日志。"
    )


def build_commander_planning_context(plan: WorkflowPlan) -> str:
    """为规划阶段的表达模型提供一份最小、可信的执行事实。

    规则规划器和表达模型职责不同：前者决定材料范围与可执行 action，后者只负责把
    已验证的计划讲清楚。这里故意不放材料正文、稳定 ID、绝对路径或子任务输出，避免
    模型把“已绑定”误解为“已经读取”，也避免它自行扩张访问范围。
    """

    material_names = {
        "document": "已选择的文档",
        "dataset": "已选择的数据文件",
        "knowledge_base": "已选择的资料库",
    }
    material_scope = [
        material_names.get(item.kind, "已选择的材料")
        + (f"（{item.display_name}）" if item.display_name else "")
        for item in plan.material_bindings
    ]
    executable_steps = [
        f"{step.agent}.{step.action}"
        for step in plan.steps
        if step.execution_mode == "execute" and step.agent != COMMANDER_AGENT_ID
    ]
    limited_steps = [
        f"{step.agent}.{step.action}"
        for step in plan.steps
        if step.execution_mode != "execute"
    ]

    if plan.intent == "direct_answer":
        # 普通聊天不应被“所有请求都先展示计划”的底层事实绑架。仍由规则规划器保留审计计划，
        # 但表达模型必须像正常聊天产品一样先回答问题，而不是把没有工具副作用的问答包装成待执行任务。
        return "\n".join(
            [
                "以下是 AgentFlow Runtime 已校验的本轮规划事实，优先级高于任何常识性猜测：",
                "本轮类型：普通对话，不需要读取绑定材料，也不需要启动 Workflow Runtime。",
                "回复要求：直接、清楚地回答客户问题；先给结论，再按需给出简短依据或不确定性说明。",
                "不要展示任务拆解、dry-run、Agent 路由、预算、日志、‘开始执行’或让客户确认的说明。",
                "如客户问题与 AgentFlow 的已有能力有关，可在直接回答后用一句话提示最贴切的下一步；不要强行路由或暗示不存在的能力。",
                "已有能力摘要：数据工作台只处理客户导入的 CSV/XLSX，可分析趋势、生成可保存 PNG 图表、导出 Excel 副本；文档助手可审查材料和制作可编辑 PPT；知识库只回答客户已选且完成索引的资料库内容。",
            ]
        )

    lines = [
        "以下是 AgentFlow Runtime 已校验的本轮规划事实，优先级高于任何常识性猜测：",
        "当前阶段：dry-run（仅完成计划和权限校验，尚未执行专业 Agent、尚未读取材料正文、尚未产生最终结论）。",
        "客户显式路由偏好：" + (
            "、".join(hint.agent_id for hint in plan.agent_hints) if plan.agent_hints else "无。"
        ),
        "已绑定材料：" + ("、".join(material_scope) if material_scope else "无。"),
        "已准入的后续执行：" + ("、".join(executable_steps) if executable_steps else "无。"),
        "仅规划或需客户转入工作台：" + ("、".join(limited_steps) if limited_steps else "无。"),
        "回复要求：说明计划、材料范围、尚未执行的边界和下一步。"
        "不要声称已经读取、检索或得到了专业结果。"
        "对于已绑定且已准入的材料，不得称系统‘无法访问’或‘没有对应工具’；"
        "应准确说明将在客户回复“开始执行”后由对应专业 Agent 受控读取。",
        "能力提醒：数据图表 PNG 和 Excel 均只基于客户明确导入的数据；PPT 创作由智能制作 PPT 工作台负责；资料库问答只读取已选择且索引完成的资料库。不要因为残留材料或 @ 标签，把明显不相关的任务转给它们。",
    ]
    if plan.clarifying_questions:
        lines.append("仍需客户补充：" + "；".join(plan.clarifying_questions[:3]))
    return "\n".join(lines)


def build_commander_planning_reply(plan: WorkflowPlan) -> str:
    """生成模型回复冲突时的确定性客户说明。

    这不是专业结论，也不替代后续模型调用。它只使用已经通过 action admission 的计划
    事实，保证客户不会在“资料库已经绑定”时看到“系统没有资料库工具”这类冲突话术。
    """

    material_names = {
        "document": "文档",
        "dataset": "数据文件",
        "knowledge_base": "资料库",
    }
    materials = [
        material_names.get(item.kind, "材料")
        + (f"“{item.display_name}”" if item.display_name else "")
        for item in plan.material_bindings
    ]
    executable_steps = [
        step.title or f"{step.agent}.{step.action}"
        for step in plan.steps
        if step.execution_mode == "execute" and step.agent != COMMANDER_AGENT_ID
    ]

    lines = ["已生成本次可审阅计划。"]
    if materials:
        lines.append("本次已明确绑定：" + "、".join(materials) + "。")
    if executable_steps:
        lines.append("确认执行后将进行：" + " → ".join(executable_steps) + "。")
    if plan.clarifying_questions:
        lines.append("开始前还需要：" + "；".join(plan.clarifying_questions[:3]))
    else:
        lines.append("当前尚未读取材料正文或生成专业结论；如计划无误，请回复“开始执行”进入真实 Runtime。")
    return "\n\n".join(lines)


def reply_conflicts_with_commander_plan(reply: str, plan: WorkflowPlan) -> bool:
    """识别少量会直接误导客户的规划阶段能力否认。

    这里不是试图理解所有自然语言，只处理“已绑定并准入知识库/文档/数据，但回复说
    无法访问或没有工具”的明确矛盾。遇到歧义宁可保留模型措辞；命中时再退回到上面的
    确定性说明，避免靠字符串重写制造新的虚假结论。
    """

    lowered = reply.lower()
    denied_markers = (
        "无法直接访问",
        "不能直接访问",
        "无法访问",
        "不能访问",
        "没有.*工具",
        "未配置.*工具",
        "未配置.*接口",
    )
    has_denial = any(re.search(marker, lowered) for marker in denied_markers)
    if not has_denial:
        return False

    executable_actions = {
        (step.agent, step.action)
        for step in plan.steps
        if step.execution_mode == "execute"
    }
    if ("knowledge_agent", "answer_question") in executable_actions:
        return any(term in lowered for term in ("资料库", "知识库", "数据库", "检索"))
    if any(agent == "document_agent" for agent, _ in executable_actions):
        return any(term in lowered for term in ("文档", "文件", "材料"))
    if ("data_agent", "analyze_dataset") in executable_actions:
        return any(term in lowered for term in ("数据", "表格", "csv", "excel"))
    return False


def _append_admitted_step(
    *,
    steps: list[WorkflowStep],
    step_id: str,
    agent_id: str,
    action: str,
    title: str,
    depends_on: list[str],
    step_input: dict[str, object],
    reason: str,
    agents: list[AgentDescriptor],
    materials: list[WorkflowMaterialBinding],
    timeout_ms: int,
    parallel_group: str = "",
) -> ActionAdmissionDecision | None:
    """只有通过准入的 action 才能进入用户可见计划。

    这层刻意在生成步骤之前拒绝 blocked action。否则 UI 会显示一个漂亮却无法执行的
    步骤，客户只能在点击执行后才知道能力尚未实现。
    """

    decision = evaluate_action_admission(
        agent_id=agent_id,
        action=action,
        agents=agents,
        materials=materials,
    )
    if decision.status == "blocked":
        return None
    steps.append(
        _build_admitted_step(
            step_id=step_id,
            agent_id=agent_id,
            action=action,
            title=title,
            depends_on=depends_on,
            parallel_group=parallel_group,
            step_input=step_input,
            reason=reason,
            agents=agents,
            materials=materials,
            timeout_ms=timeout_ms,
            decision=decision,
        )
    )
    return decision


def _build_admitted_step(
    *,
    step_id: str,
    agent_id: str,
    action: str,
    title: str,
    depends_on: list[str],
    step_input: dict[str, object],
    reason: str,
    agents: list[AgentDescriptor],
    materials: list[WorkflowMaterialBinding],
    timeout_ms: int,
    decision: ActionAdmissionDecision | None = None,
    parallel_group: str = "",
) -> WorkflowStep:
    """从准入结果构造稳定的 WorkflowStep，避免规划器手写权限或验证文案。"""

    decision = decision or evaluate_action_admission(
        agent_id=agent_id,
        action=action,
        agents=agents,
        materials=materials,
    )
    agent = next((item for item in agents if item.id == agent_id), None)
    # 引导步骤没有读取或执行数据文件，不继承 data_agent 的 file_read 权限；真正的数据
    # 操作仍要在数据工作台的独立任务中重新请求和验证。
    required_permissions = (
        _required_permissions_for_agent(agent)
        if decision.admission.execution_mode == "execute"
        else []
    )
    required_permissions = list(dict.fromkeys([
        *required_permissions,
        *decision.admission.additional_permissions,
    ]))
    risk_level = _risk_level_for_permissions(required_permissions)
    requires_confirmation = _requires_confirmation(required_permissions)
    return WorkflowStep(
        id=step_id,
        agent=agent_id,
        action=action,
        title=title,
        depends_on=depends_on,
        parallel_group=parallel_group,
        input=step_input,
        reason=reason,
        expected_output=decision.admission.expected_output,
        required_permissions=required_permissions,
        risk_level=risk_level,
        requires_confirmation=requires_confirmation,
        tool_name=_tool_name(agent_id, action),
        command_policy=_command_policy_for_permissions(required_permissions),
        success_criteria=_success_criteria_for_step(agent_id, action),
        timeout_ms=timeout_ms,
        retry_policy=_retry_policy_for_step(agent_id, action),
        execution_mode=decision.admission.execution_mode,
        admission_status=decision.status,
        admission_reason=decision.reason,
        verification_scope=decision.admission.verification_scope,
        recovery_hint=decision.admission.recovery_hint,
    )


def _matches_any(value: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in value for keyword in keywords)


def _requests_bound_material_work(message: str) -> bool:
    """判断客户是否真的要处理当前材料，而不是进行普通闲聊。

    这不是为了替代模型意图理解，而是总指挥的最小安全闸门：已挂载的文档、CSV 或资料库
    不能因为一次会话仍在同一页面就自动获得读取资格。覆盖的词刻意偏向客户常用的任务动词，
    并保留“这份/当前/刚才”等指代，方便从专业工作台跳转后自然续问。
    """

    lowered = message.casefold()
    task_markers = (
        "分析", "看看", "查看", "读取", "检索", "搜索", "查找", "提取", "归纳", "梳理",
        "总结", "整理", "审查", "核验", "比较", "解释", "回答", "问答", "统计",
        "计算", "生成", "制作", "导出", "保存", "加工", "清洗", "画图", "做图",
        "当前", "这份", "这个", "刚才", "上一步",
    )
    return any(marker in lowered for marker in task_markers)


def _hint_can_influence_route(
    *,
    agent_id: str,
    message: str,
    material_refs: list[str],
) -> bool:
    """让 `@Agent` 成为偏好，而不把它升级成隐藏权限或强制工具调用。

    已带入对应材料时，客户主动点名就可以作为一次受控处理请求；没有材料时仍保留清晰
    澄清，避免 `@数据工作台 帮我制作图表` 被错误当成普通闲聊。PPT 等更强的显式目标
    已在调用方先行截获，因此不会被这里的提示标签覆盖。
    """

    if material_refs or _requests_bound_material_work(message):
        return True

    # 仅输入 @标签时也应告知该能力需要什么材料，而不是静默降成普通聊天。别名已经在
    # `_normalize_agent_hints` 白名单化，因此这不会变成动态 Agent 发现入口。
    aliases = _AGENT_HINT_ALIASES.get(agent_id, ())
    return any(f"@{alias.casefold()}" in message for alias in aliases)


def _requests_data_chart_delivery(message: str) -> bool:
    """区分“给出图表建议”和“现在写出 PNG 图表”。

    数据分析结果常会告诉客户“可生成图表”。这不是客户的写入确认，不能因为它包含
    ``生成图表`` 四个字就创建 PNG 交付任务；只有明确的制作/导出/保存表达才触发文件写入。
    """

    if any(marker in message for marker in ("可生成图表", "图表建议", "建议图表", "能生成图表")):
        return False
    return _matches_any(message, DATA_CHART_DELIVERY_KEYWORDS)


def _requests_data_workbook_delivery(message: str) -> bool:
    """区分“讨论 Excel”与“现在新建一份受控分析工作簿”。"""

    if any(marker in message for marker in ("可导出excel", "可导出 excel", "建议导出excel", "建议导出 excel")):
        return False
    return _matches_any(message, DATA_WORKBOOK_DELIVERY_KEYWORDS)


def _requests_data_transform(message: str) -> bool:
    """识别需要新增派生字段的自然语言目标。"""

    return _matches_any(message, DATA_TRANSFORM_KEYWORDS)


def _normalize_material_bindings(
    message: str,
    supplied: Iterable[WorkflowMaterialBinding],
) -> list[WorkflowMaterialBinding]:
    """合并客户端明确选择与用户在任务中明确点名的受控材料。

    不扫描目录，也不接受绝对路径或 ``..`` 跳转。名称是否真实存在由具体工作台/Agent
    在 Runtime 前再次校验；本函数只建立“客户要求本次可使用什么”的计划级事实。
    """

    bindings: list[WorkflowMaterialBinding] = []
    seen: set[tuple[str, str]] = set()
    for item in supplied:
        normalized = _safe_material_ref(item.ref, kind=item.kind)
        if normalized is None:
            continue
        key = (item.kind, normalized)
        if key in seen:
            continue
        seen.add(key)
        bindings.append(
            item.model_copy(
                update={
                    "ref": normalized,
                    "display_name": item.display_name.strip() or PurePath(normalized).name,
                    # 材料是否可被模型读取必须由具体 Agent Tool 决定，聊天请求不能越权声明。
                    "model_visible": False,
                }
            )
        )

    pattern = re.compile(
        r"(?P<path>[\w\-.\\/:\u4e00-\u9fff ]+?\.(?:txt|md|markdown|pdf|docx|csv|xlsx))",
        re.IGNORECASE,
    )
    for match in pattern.finditer(message):
        candidate = match.group("path").strip().strip("'\"“”‘’，,。；;：:")
        # 正常中文输入常写成“请读取 方案.docx”。正则必须允许中文文件名和空格，但不能
        # 因此把位于文件名前的明确任务动词也并入引用；仅移除开头这一小组固定表达，
        # 不按空格切分，避免破坏“项目 方案.docx”这类合法文件名。
        candidate, verb_removed = re.subn(
            r"^(?:(?:请|请你|麻烦|麻烦你|帮我|请帮我|帮忙)?"
            r"(?:读取|打开|查看|分析|处理|归纳|总结|整理|比较|使用|上传|导入)\s*)+",
            "",
            candidate,
        )
        if verb_removed:
            # “请读取这个文档 方案.docx”中的“这个文档”是提示语而非文件名前缀。
            # 仅在上一步确实移除了任务动词时处理，且要求名词后有分隔空白，避免伤及
            # 合法的“文档方案.docx”文件名。
            candidate = re.sub(
                r"^(?:(?:这个|这份|该|此|一份|上述)\s*)?"
                r"(?:数据文件|文档|文件|材料)\s+",
                "",
                candidate,
            )
        suffix = PurePath(candidate).suffix.lower()
        kind = "document" if suffix in {".txt", ".md", ".markdown", ".pdf", ".docx"} else "dataset"
        normalized = _safe_material_ref(candidate, kind=kind)
        if normalized is None:
            continue
        key = (kind, normalized)
        if key in seen:
            continue
        seen.add(key)
        bindings.append(
            WorkflowMaterialBinding(
                binding_id=f"user_named_{kind}_{len(bindings) + 1}",
                kind=kind,
                ref=normalized,
                display_name=PurePath(normalized).name,
                origin="user_named",
                usage="用户在本次任务中明确点名的材料。",
            )
        )
    return bindings


def _safe_material_ref(value: str, *, kind: str) -> str | None:
    """把客户端引用收束为工作台的相对名称，拒绝本机绝对路径和目录跳转。"""

    candidate = value.strip().replace("\\", "/")
    if not candidate or re.match(r"^(?:[A-Za-z]:/|/|//)", candidate):
        return None
    # 资料库 ID 不是路径。它只允许由知识库选择器产生的稳定 ID，不能借此字段把目录、
    # 版本号或任意 SQLite 身份注入 Commander 计划。
    if kind == "knowledge_base":
        return candidate if re.fullmatch(r"kb_[a-z0-9]{8,32}", candidate) else None
    # 先检查原始引用。不能在清理 './' 时把 '../private.txt' 悄悄变成可接受的名称。
    if ".." in PurePath(candidate).parts:
        return None
    prefix = "data/workspaces/" if kind == "document" else "data/datasets/"
    lowered = candidate.lower()
    if lowered.startswith(prefix):
        candidate = candidate[len(prefix):]
    if candidate.startswith("./"):
        candidate = candidate[2:]
    parts = PurePath(candidate).parts
    if not candidate or ".." in parts or any(part in {"", "."} for part in parts):
        return None
    suffix = PurePath(candidate).suffix.lower()
    allowed = (
        {".txt", ".md", ".markdown", ".pdf", ".docx"}
        if kind == "document"
        else {".csv", ".xlsx"}
    )
    return candidate if suffix in allowed else None


def _material_refs(materials: Iterable[WorkflowMaterialBinding], *, kind: str) -> list[str]:
    """返回稳定去重的材料引用，保留用户选择顺序。"""

    return list(dict.fromkeys(item.ref for item in materials if item.kind == kind))


def _extract_workspace_search_query(message: str) -> str | None:
    """提取用户想在 workspace 文档中定位的关键词。

    这里故意只做保守规则：优先取引号/书名号里的内容，其次取搜索动词后的短文本。
    真正的搜索仍由 workspace 服务限定在受控目录内，Commander 只负责生成结构化参数。
    """

    search_words = ("搜索", "查找", "找一下", "搜一下", "包含", "grep")
    if not any(word in message.lower() for word in search_words):
        return None

    quoted = re.search(r"[\"'“‘《](?P<query>[^\"'”’》]{1,80})[\"'”’》]", message)
    if quoted:
        return _clean_search_query(quoted.group("query"))

    pattern = re.compile(
        r"(?:搜索|查找|找一下|搜一下|包含|grep)\s*(?:关键词|内容|文本|词)?[：:\s]*(?P<query>[\w\-. /\u4e00-\u9fff]{1,80})",
        re.IGNORECASE,
    )
    match = pattern.search(message)
    if not match:
        return None
    return _clean_search_query(match.group("query"))


def _needs_document_understanding(message: str) -> bool:
    return any(
        keyword in message
        for keyword in ("提取", "归纳", "分析", "整理", "总结", "要点", "说明")
    )


def _document_output_mode(message: str) -> str:
    """把用户意图收敛为文档助手已经声明的输出模式。

    Commander 不负责猜测文档内容，只负责传递一个稳定的意图提示；实际读取、搜索和
    来源校验仍由 Document Agent 的 Runner 执行。
    """

    if any(keyword in message for keyword in ("问答", "回答", "解释", "为什么", "是否")):
        return "qa"
    if any(keyword in message for keyword in ("需求", "验收", "约束", "功能")):
        return "requirements"
    if any(keyword in message for keyword in ("总结", "摘要", "概述")):
        return "summary"
    return "auto"


def _clean_search_query(value: str) -> str | None:
    cleaned = value.strip().strip("'\"“”‘’《》，,。；;：:？?！! ")
    if not cleaned:
        return None

    # 去掉常见的范围补充词，避免“作业要求 在文档里”整段变成搜索词。
    cleaned = re.split(r"\s+(?:在|于|从|到|并|然后|里面|里)\b", cleaned, maxsplit=1)[0].strip()
    return cleaned[:80] or None


def _required_permissions_for_agent(agent: AgentDescriptor | None) -> list[str]:
    if agent is None:
        return []

    # 权限来自 manifest，而不是 PlanningRule 硬编码。后续用户插件只要正确声明权限，
    # Commander 计划就能自动带出执行前需要确认的能力边界。
    return [
        name
        for name, enabled in agent.permissions.model_dump().items()
        if enabled
    ]


def _risk_level_for_permissions(permissions: list[str]) -> RiskLevel:
    if "shell" in permissions or "database" in permissions:
        return "high"
    if "file_write" in permissions or "network" in permissions or "knowledge_deep_analysis" in permissions:
        return "medium"
    return "low"


def _requires_confirmation(permissions: list[str]) -> bool:
    # 当前只把可能修改外部状态或产生费用/安全风险的能力设为确认项。
    # file_read 后续会结合用户选择的文件范围单独处理。
    return any(permission in {"file_write", "network", "shell", "database", "knowledge_deep_analysis"} for permission in permissions)


def _tool_name(agent_id: str, action: str) -> str | None:
    contract = NODE_CONTRACTS.get((agent_id, action))
    return contract.tool_name if contract else None


def _command_policy_for_permissions(permissions: list[str]) -> WorkflowCommandPolicy:
    """根据权限声明给出命令层风险摘要。

    当前内置 Runtime 还不会执行 Shell；这个字段先把风险说清楚，后续代码工坊接命令时
    可以在同一个协议位置继续细化白名单、审批和禁止项。
    """

    if "shell" in permissions:
        return WorkflowCommandPolicy(
            may_run_command=True,
            risk_level="high_risk",
            requires_confirmation=True,
            allowed=False,
            reason="该步骤声明了 shell 权限，MVP 阶段默认禁止自动执行命令。",
        )
    return WorkflowCommandPolicy(reason="该步骤当前不需要命令执行。")


def _retry_policy_for_step(agent_id: str, action: str) -> WorkflowRetryPolicy:
    if agent_id == COMMANDER_AGENT_ID:
        return WorkflowRetryPolicy(max_attempts=2, retryable=True, stop_condition="规划失败后转为澄清问题。")
    if agent_id == "document_agent" and action == "analyze_document":
        return WorkflowRetryPolicy(
            max_attempts=1,
            retryable=False,
            stop_condition="文档助手自身已限制模型轮次和工具调用；失败时保留子任务轨迹供用户调整材料或问题。",
        )
    if agent_id == "knowledge_agent" and action == "answer_question":
        return WorkflowRetryPolicy(
            max_attempts=1,
            retryable=False,
            stop_condition="知识库助手自身会验证活动版本、证据与引用；资料不足或模型失败时保留关联子任务，不由父任务重复调用。",
        )
    if agent_id == "knowledge_agent" and action == "deep_summary":
        return WorkflowRetryPolicy(
            max_attempts=1,
            retryable=False,
            stop_condition="父任务只受理一次后台深度子任务；K4 子任务自行保存检查点并在自身入口恢复，避免父任务重复冻结范围或重复消耗。",
        )
    if agent_id == "data_agent" and action == "analyze_dataset":
        return WorkflowRetryPolicy(
            max_attempts=1,
            retryable=False,
            stop_condition="数据分析子任务只读取当前显式绑定的数据副本；失败时保留子任务说明，客户可核对文件后重新委派。",
        )
    if agent_id == "data_agent" and action == "export_chart_dashboard":
        return WorkflowRetryPolicy(
            max_attempts=1,
            retryable=False,
            stop_condition="图表写入前后都会验证数据版本和 PNG 像素；失败时不覆盖旧产物，客户可调整目标后重新规划。",
        )
    if agent_id == "data_agent" and action == "export_analysis_workbook":
        return WorkflowRetryPolicy(
            max_attempts=1,
            retryable=False,
            stop_condition="工作簿写入前后都会验证数据版本和原生对象回读；失败时不覆盖旧产物，客户可调整目标后重新规划。",
        )
    if agent_id == "data_agent" and action == "plan_field_transform":
        return WorkflowRetryPolicy(
            max_attempts=1,
            retryable=False,
            stop_condition="字段加工计划由本地画像和白名单操作生成；字段或类型不明确时直接澄清，不重复试算。",
        )
    if agent_id == "data_agent" and action == "export_field_transform":
        return WorkflowRetryPolicy(
            max_attempts=1,
            retryable=False,
            stop_condition="字段加工副本写入前后都会验证源哈希、新字段和行数；失败时不覆盖源文件或旧产物。",
        )
    if action in {"read_text", "search_text", "generate_code", "generate_report"}:
        return WorkflowRetryPolicy(max_attempts=3, retryable=True, stop_condition="同类错误连续失败后停止自动重试。")
    return WorkflowRetryPolicy()


def _success_criteria_for_step(agent_id: str, action: str) -> list[str]:
    if agent_id == COMMANDER_AGENT_ID and action == "analyze_task":
        return ["任务意图已识别", "风险和权限已标记", "计划通过校验"]
    if agent_id == COMMANDER_AGENT_ID:
        return ["给出可理解的直接答复或澄清问题"]
    if agent_id == "document_agent" and action == "read_text":
        return ["受控 workspace 文档读取成功", "返回 UTF-8 预览和文档上下文"]
    if agent_id == "document_agent" and action == "search_text":
        return ["完成受控 workspace 精确搜索", "返回命中文档、行号和短上下文"]
    if agent_id == "document_agent" and action == "analyze_document":
        return ["仅分析用户明确选择的受控文档", "输出可验证来源", "保留完整 Agent 运行轨迹"]
    if agent_id == "document_agent" and action == "open_presentation_studio":
        return ["已带入客户本轮 PPT 主题", "不读取材料或调用模型", "导出前仍由工作台明确确认"]
    if agent_id == "knowledge_agent" and action == "answer_question":
        return ["仅读取客户明确选择的资料库", "关键结论通过 claim/source_id 闭合校验", "保留关联子任务与来源轨迹"]
    if agent_id == "knowledge_agent" and action == "deep_summary":
        return ["客户已确认全库深度分析预算", "子任务冻结当前活动版本的完整章节范围", "父子任务可互相追溯，K4 自行保存检查点"]
    if agent_id == "data_agent" and action == "open_workspace":
        return ["不读取或修改数据文件", "明确提示客户进入数据工作台", "不创建伪造的专业子任务"]
    if agent_id == "data_agent" and action == "analyze_dataset":
        return [
            "仅读取一份客户明确绑定的导入数据",
            "本地白名单计算只生成有限指标、聚合与图表建议",
            "原始行不进入模型或父任务，保留关联子任务与源哈希",
            "不导出或修改 CSV/XLSX/PNG；图表或分析 Excel 需在本会话明确确认后单独交付",
        ]
    if agent_id == "data_agent" and action == "export_chart_dashboard":
        return [
            "仅读取一份客户明确绑定的数据文件及其已验证聚合结果",
            "仅在受控 outputs 中新建 PNG 图表，不修改 CSV/XLSX",
            "每张 PNG 都通过像素回读验证并登记可打开产物",
        ]
    if agent_id == "data_agent" and action == "export_analysis_workbook":
        return [
            "仅读取一份客户明确绑定的数据文件及其已验证聚合结果",
            "仅在受控 outputs 中新建 Excel 工作簿，不修改原始 CSV/XLSX",
            "原生工作表、表格、图表与关键指标都通过回读验证并登记 artifact",
        ]
    if agent_id == "data_agent" and action == "plan_field_transform":
        return [
            "仅使用一份客户明确绑定且哈希未变化的数据文件",
            "操作来自有限白名单，字段和类型均通过本地画像校验",
            "只生成脱敏加工预览，不写入源 CSV/XLSX",
        ]
    if agent_id == "data_agent" and action == "export_field_transform":
        return [
            "仅在客户确认后新建字段加工副本，不修改源 CSV/XLSX",
            "按原文件类型追加所有确认字段，不添加无关样式",
            "新副本通过字段、行数和源版本回读验证后再交付",
        ]
    if agent_id == "document_agent":
        return ["生成结构化需求摘要", "输出可供后续 Agent 使用的文档上下文"]
    if agent_id == "code_agent":
        return ["代码草稿写入受控 outputs 目录", "产物通过 UTF-8 回读验证"]
    if agent_id == "report_agent":
        return ["报告写入受控 outputs 目录", "产物通过 UTF-8 回读验证"]
    return ["步骤完成并返回结构化结果"]


def _max_risk_level(steps: list[WorkflowStep]) -> RiskLevel:
    rank = {"low": 0, "medium": 1, "high": 2}
    highest = max((step.risk_level for step in steps), key=lambda value: rank[value], default="low")
    return highest


def _infer_intent(steps: list[WorkflowStep]) -> str:
    agents = {step.agent for step in steps}
    if agents <= {COMMANDER_AGENT_ID}:
        return "direct_answer"
    if {"document_agent", "data_agent"}.issubset(agents):
        return "document_data"
    if "document_agent" in agents:
        return "document_delivery"
    if "knowledge_agent" in agents:
        return "knowledge_deep_summary" if any(step.action == "deep_summary" for step in steps) else "knowledge_answer"
    if "data_agent" in agents:
        if any(step.action in {"plan_field_transform", "export_field_transform"} for step in steps):
            return "data_transform"
        return "data_workspace"
    return "general"


def _build_clarifying_questions(
    message: str,
    steps: list[WorkflowStep],
    materials: list[WorkflowMaterialBinding],
) -> list[str]:
    """信息明显不足时先问少量关键问题。

    MVP 先只识别非常含糊的“这个/这些/它”类输入，避免 Commander 看似很聪明地乱猜。
    """

    stripped = message.strip()
    if len(steps) > 2:
        return []
    if any(token in stripped for token in ("这个", "这些", "它", "那份")) and not materials:
        return ["你说的材料或对象是哪一个？请先选择文档或数据文件，或在任务中写出受控材料名称。"]
    if len(stripped) <= 6:
        return ["你希望 AgentFlow 完成什么具体结果？例如生成报告、整理文档、生成代码或回答问题。"]
    return []


def _build_assumptions(
    steps: list[WorkflowStep],
    memory_context_summary: list[str] | None = None,
    *,
    conversation_context_summary: list[str] | None = None,
) -> list[str]:
    assumptions = ["真实执行前仍会经过 Runtime 权限和工作区边界校验。"]
    if memory_context_summary:
        assumptions.append(
            f"本计划已参考 {len(memory_context_summary)} 条用户确认的长期记忆；"
            "可在系统设置或记忆管理中查看、修改、关闭或删除。"
        )
    if conversation_context_summary:
        assumptions.append("本计划已延续当前调度会话的有限上下文；早期对话会以受控摘要代替全文重传。")
    if any(step.requires_confirmation for step in steps):
        assumptions.append("涉及写入、联网、命令或长耗时深度分析预算时，需要用户确认后才继续。")
    return assumptions


def _build_definition_of_done(steps: list[WorkflowStep]) -> list[str]:
    agents = {step.agent for step in steps}
    done: list[str] = []
    if "document_agent" in agents:
        done.append("文档助手完成受控分析，结论附带可验证来源，并形成后续 Agent 可引用的上下文。")
    if "knowledge_agent" in agents:
        if any(step.action == "deep_summary" for step in steps):
            done.append("知识库深度子任务已受理并冻结所选资料库的活动章节范围；最终总结、检查点和报告均在关联子任务内查看。")
        else:
            done.append("知识库助手仅依据所选资料库的活动版本完成可信问答，关键结论可在关联子任务查看来源。")
    if "data_agent" in agents:
        if any(step.action == "export_field_transform" for step in steps):
            done.append("数据工作台已按确认的白名单加工计划，在新副本中追加字段并完成回读验证；源文件未修改。")
        elif any(step.action == "export_chart_dashboard" for step in steps):
            done.append("数据工作台已基于明确绑定的数据生成并回读 PNG 图表；源 CSV/XLSX 不会被修改，交付物可在任务历史直接打开。")
        elif any(step.action == "analyze_dataset" for step in steps):
            done.append(
                "数据工作台已对一份明确绑定的导入数据完成只读画像、聚合和可追溯结论；"
                "图表 PNG、字段副本和 Excel 交付可按明确目标进入各自的受控交付步骤。"
            )
        else:
            done.append("数据任务已带着明确材料和目标转入数据工作台；不把该引导误记为自动分析完成。")
    if not done:
        done.append("用户获得直接答复，或收到下一步澄清问题。")
    return done


def _build_budget_estimate(steps: list[WorkflowStep]) -> WorkflowBudgetEstimate:
    step_count = len(steps)
    permissions = {permission for step in steps for permission in step.required_permissions}
    executable_steps = [step for step in steps if step.execution_mode == "execute"]
    if any(step.action == "deep_summary" for step in steps) or step_count >= 5 or "shell" in permissions:
        level = "high"
    elif step_count >= 3 or permissions.intersection({"file_write", "network"}):
        level = "medium"
    else:
        level = "low"
    return WorkflowBudgetEstimate(
        step_count=step_count,
        time_level=level,
        model_cost_level="high" if any(step.action == "deep_summary" for step in executable_steps) else ("medium" if any(step.agent in {"document_agent", "knowledge_agent", "data_agent"} for step in executable_steps) else "low"),
        requires_network="network" in permissions,
        requires_command="shell" in permissions,
    )


def _build_workspace_scope(
    steps: list[WorkflowStep],
    materials: list[WorkflowMaterialBinding],
) -> WorkflowWorkspaceScope:
    read_paths: list[str] = []
    write_paths: list[str] = []
    external_services: list[str] = []

    for step in steps:
        if step.agent == "document_agent":
            path = step.input.get("path")
            if isinstance(path, str) and path:
                read_paths.append(f"data/workspaces/{path}")
            elif step.action in {"analyze_document", "search_text"}:
                document_refs = step.input.get("document_refs")
                if isinstance(document_refs, list):
                    for document_ref in document_refs:
                        if isinstance(document_ref, str) and document_ref:
                            read_paths.append(f"data/workspaces/{document_ref}")
        if step.agent == "knowledge_agent":
            knowledge_base_id = step.input.get("knowledge_base_id")
            if isinstance(knowledge_base_id, str) and knowledge_base_id:
                read_paths.append(f"knowledge-base://{knowledge_base_id}")
        if step.agent == "data_agent":
            dataset_name = step.input.get("dataset_name")
            if isinstance(dataset_name, str) and dataset_name:
                # 数据集只在已准入的数据 action 中进入读取范围；调度台上残留的其他材料
                # 只是本轮可选上下文，不应被审计面误写成已经授权读取。
                read_paths.append(f"data/datasets/{dataset_name}")
        if "file_write" in step.required_permissions:
            write_paths.append("data/outputs/<runtime_task_id>/")
        if "network" in step.required_permissions:
            external_services.append("network")

    return WorkflowWorkspaceScope(
        read_paths=sorted(set(read_paths)),
        write_paths=sorted(set(write_paths)),
        external_services=sorted(set(external_services)),
        notes="本范围是计划预估；真实执行仍只能访问 Runtime 明确允许的 workspace 和 outputs 边界。",
    )


def _next_action(
    *,
    requires_confirmation: bool,
    clarifying_questions: list[str],
    has_guided_handoff: bool,
    guided_handoff_action: str,
    requires_composition_runtime: bool,
) -> str:
    if clarifying_questions:
        return "ask_clarifying_questions"
    if requires_composition_runtime:
        return "review_combination_plan"
    if has_guided_handoff:
        if guided_handoff_action == "open_presentation_studio":
            return "open_presentation_studio"
        return "open_data_workspace"
    if requires_confirmation:
        return "review_plan_and_confirm_permissions"
    return "execute_after_confirm"


def _build_plan_summary(steps: list[WorkflowStep]) -> str:
    labels = {
        "document_agent": "文档助手",
        "data_agent": "数据工作台",
        "knowledge_agent": "知识库",
    }
    agent_sequence = [labels.get(step.agent, step.agent) for step in steps if step.agent != COMMANDER_AGENT_ID]
    if not agent_sequence:
        return "Commander 判断该任务暂不需要多 Agent 协作，可先直接回答。"

    guided = any(step.execution_mode == "guided_handoff" for step in steps)
    deep_delegation = any(step.action == "deep_summary" for step in steps)
    if deep_delegation:
        return "Commander 将先等待你的深度分析预算确认，再创建可恢复的知识库后台子任务。"
    parallel_specialists = [
        step for step in steps if step.parallel_group == "specialist_read_only"
    ]
    if len(parallel_specialists) > 1:
        parallel_names = [labels.get(step.agent, step.agent) for step in parallel_specialists]
        return (
            "Commander 已按你明确选择的材料建立组合计划："
            + "、".join(parallel_names)
            + " 可在未来组合 Runtime 中并行处理，随后再汇总；当前仅供审阅，不会提前执行。"
        )
    suffix = "其中数据工作台当前需要你继续确认材料与操作。" if guided else ""
    return "Commander 将按顺序处理：" + " -> ".join(agent_sequence) + "。" + suffix
