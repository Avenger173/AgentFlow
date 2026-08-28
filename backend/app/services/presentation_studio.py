"""PPT 制作 V2 的“自然语言意图 -> 创作简报 -> 逐页计划”服务。

这个服务只生成并持久化可确认的计划，不写 PPTX、不读取工作区外路径，也不联网抓取图片。
模型只在受控 JSON 契约里提出内容和视觉方向；文件写入、外部素材、模板和用户确认仍由后续
交付层负责，避免一次聊天回复直接变成不可审计的文件。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.database.task_repository import load_workflow_run, save_workflow_run
from app.schemas.events import TaskLogEvent
from app.schemas.presentation_studio import (
    PresentationStudioAssetPlan,
    PresentationStudioAssetSlot,
    PresentationStudioBrief,
    PresentationStudioDataPlan,
    PresentationStudioPlanRequest,
    PresentationStudioPlanResponse,
    PresentationStudioResearchPlan,
    PresentationStudioSlidePlan,
)
from app.schemas.workflow import (
    RuntimeExecutionLimits,
    RuntimeExecutionMetrics,
    WorkflowRun,
    WorkflowStepRun,
)
from app.services.llm_chat import is_llm_enabled
from app.services.model_gateway import (
    ModelConversationMessage,
    ModelGatewayError,
    ModelRuntime,
    resolve_model_runtime_for_route,
)
from app.workflow.dry_run import clear_dry_run_memory_cache


_PRESENTATION_STUDIO_AGENT_ID = "document_agent"
_PRESENTATION_STUDIO_STEP_ID = "presentation_studio_plan"
# 默认给“用户只说一句主题”的场景留出完整叙事空间：封面、目录、六页正文、总结和事实边界。
# 客户仍可在后续版本选择页数；这里不把复杂参数暴露给首次使用者。
_DEFAULT_SLIDE_COUNT = 10
_MIN_SLIDE_COUNT = 5
_MAX_SLIDE_COUNT = 12
_THEMES = {"executive_blue", "technology_emerald", "narrative_warm", "impact_contrast"}
_CONTENT_LAYOUTS = {
    "insight_cards",
    "comparison",
    "process",
    "timeline",
    "metrics",
    "quote",
    "image_statement",
}
_FALLBACK_LAYOUT_SEQUENCE = (
    "comparison",
    "process",
    "timeline",
    "metrics",
    "quote",
    "insight_cards",
)
_ProgressCallback = Callable[[str, str], Awaitable[None]]


class PresentationStudioServiceError(RuntimeError):
    """PPT 创作计划的可预期业务错误。"""


class _ResearchBlueprintRequestError(PresentationStudioServiceError):
    """携带研究规划器已实际发出的调用次数，供任务审计如实记录。"""

    def __init__(self, message: str, *, call_count: int) -> None:
        super().__init__(message)
        self.call_count = call_count


class _StudioContentSlide(BaseModel):
    """模型可写入的一张正文页；最终角色和核验页由 Runtime 统一补齐。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    bullets: list[str] = Field(min_length=2, max_length=5)
    # 模型只在受控枚举中选择页面叙事结构；缺省时由 Runtime 按稳定序列补齐。
    layout: str = Field(default="", max_length=40)
    visual_direction: str = Field(min_length=1, max_length=300)


class _StudioResearchBlueprint(BaseModel):
    """规划阶段产生的研究意图，不包含任何声称已查到的事实或数值。"""

    model_config = ConfigDict(extra="forbid")

    needed: bool = False
    research_question: str = Field(default="", max_length=500)
    entities: list[str] = Field(default_factory=list, max_length=6)
    entity_search_names: list[str] = Field(default_factory=list, max_length=6)
    metrics: list[str] = Field(default_factory=list, max_length=6)
    trend_metric: str = Field(default="", max_length=120)
    time_scope: str = Field(default="", max_length=200)
    comparison_scope: str = Field(default="", max_length=300)
    chart_type: str = Field(default="none", max_length=40)
    # 专用规划器可以根据数据形态推荐多个互补视图。Harness 仍会覆盖客户明确点名的
    # 数量要求，并限制类型、总数和页面容量，不能把推荐直接当作执行权限。
    recommended_visuals: list[str] = Field(default_factory=list, max_length=8)
    visual_metrics: list[list[str]] = Field(default_factory=list, max_length=8)
    target_slide_index: int = Field(default=0, ge=0, le=8)
    required_data_points: int = Field(default=0, ge=0, le=36)
    search_queries: list[str] = Field(default_factory=list, max_length=6)
    preferred_source_types: list[str] = Field(default_factory=list, max_length=5)


class _StudioResearchDetails(BaseModel):
    """专用研究规划器只填写研究细节；是否触发由 Harness 根据客户明确意图决定。"""

    model_config = ConfigDict(extra="forbid")

    research_question: str = Field(min_length=1, max_length=500)
    entities: list[str] = Field(min_length=1, max_length=6)
    entity_search_names: list[str] = Field(default_factory=list, max_length=6)
    metrics: list[str] = Field(min_length=1, max_length=6)
    trend_metric: str = Field(default="", max_length=120)
    time_scope: str = Field(min_length=1, max_length=200)
    comparison_scope: str = Field(min_length=1, max_length=300)
    chart_type: str = Field(min_length=1, max_length=40)
    recommended_visuals: list[str] = Field(default_factory=list, max_length=8)
    visual_metrics: list[list[str]] = Field(default_factory=list, max_length=8)
    target_slide_index: int = Field(ge=1, le=8)
    required_data_points: int = Field(ge=1, le=36)
    search_queries: list[str] = Field(min_length=3, max_length=6)
    preferred_source_types: list[str] = Field(default_factory=list, max_length=5)


class _StudioModelOutput(BaseModel):
    """模型最小输出契约，限制它只决定创作内容而不决定文件副作用。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=160)
    purpose: str = Field(min_length=1, max_length=360)
    audience: str = Field(min_length=1, max_length=240)
    core_message: str = Field(min_length=1, max_length=420)
    theme: str = Field(min_length=1, max_length=80)
    theme_reason: str = Field(min_length=1, max_length=420)
    content_slides: list[_StudioContentSlide] = Field(min_length=1, max_length=8)
    asset_queries: list[str] = Field(default_factory=list, max_length=6)
    # 主创作模型允许省略该字段；明确的数据意图由职责单一的研究规划器复核，避免扩大主契约。
    research_blueprint: _StudioResearchBlueprint = Field(default_factory=_StudioResearchBlueprint)


@dataclass(frozen=True)
class _DataVisualIntent:
    """Harness 从自然语言中提取的最小交付数量，不包含任何事实或数据值。"""

    table_count: int = 0
    bar_count: int = 0
    line_count: int = 0
    pie_count: int = 0
    doughnut_count: int = 0
    area_count: int = 0
    generic_visual_count: int = 0
    explicit: bool = False

    @property
    def total(self) -> int:
        return max(
            self.generic_visual_count,
            self.table_count + self.bar_count + self.line_count + self.pie_count + self.doughnut_count + self.area_count,
        )


async def build_presentation_studio_plan(
    *,
    request: PresentationStudioPlanRequest,
    task_id: str | None = None,
    progress_callback: _ProgressCallback | None = None,
) -> PresentationStudioPlanResponse:
    """为一句用户意图创建可确认的 PPT 创作计划并写入任务历史。"""

    stable_task_id = task_id or f"task_presentation_studio_{uuid4().hex[:12]}"
    started_at = datetime.now(UTC)
    requested_slide_count = _requested_slide_count(request.target_slide_count)
    requested_slide_count = max(
        requested_slide_count,
        4 + _requested_data_visual_count_hint(request),
    )
    await _emit_progress(progress_callback, "presentation_brief_started", "正在理解主题并整理演示简报。")

    mode = "mock"
    warnings: list[str] = []
    repair_used = False
    research_planner_used = False
    # 研究规划器最多调用两次：首次规划与一次纯格式修复。这个数字必须进入任务历史，
    # 否则客户看到“已修复”时无法判断系统是否真的做过第二次模型调用。
    research_planner_call_count = 0
    if is_llm_enabled():
        try:
            runtime = resolve_model_runtime_for_route("document_presentation").runtime
            try:
                output, repair_used = await _request_model_plan(
                    runtime=runtime,
                    request=request,
                    requested_slide_count=requested_slide_count,
                )
            except (ModelGatewayError, PresentationStudioServiceError):
                # C6.5 起，客户在模型路由页看到的 Provider / 模型就是本次实际请求的唯一模型。
                # 不能因为结构化 JSON 出错而暗中换到另一份已保存 Key；外层会清楚标注本次计划
                # 降级为确定性草案，客户可再显式修改“文档与 PPT 制作”路由后重试。
                raise
            # 创作规划器要同时处理叙事、版式和素材，真实供应商有时会把“当前没有数据”误判为
            # “不需要研究”。客户已启用智能数据时，用一次职责单一的无工具规划复核，避免继续
            # 往主提示词堆领域关键词，也避免为每个主题增加专用 MCP。
            if (
                request.structured_data_enabled
                and _normalize_research_blueprint(output.research_blueprint) is None
                and _has_explicit_data_research_intent(request.intent)
                and not _has_world_bank_shortcut(request.intent)
            ):
                try:
                    research_planner_call_count = 1
                    research_runtime = runtime
                    research_blueprint, research_repair_used, research_fallback_used = await _request_research_blueprint(
                        runtime=research_runtime,
                        request=request,
                        output=output,
                    )
                    if research_repair_used:
                        research_planner_call_count = 2
                except (ModelGatewayError, PresentationStudioServiceError) as exc:
                    if isinstance(exc, _ResearchBlueprintRequestError):
                        research_planner_call_count = exc.call_count
                    # 研究复核失败不能拖垮已经通过契约校验的主创作计划；计划预览会明确说明
                    # 本次没有形成数据蓝图，后续也不会联网猜数。
                    warnings.append(f"数据研究规划未通过校验，本次不生成数据图表：{exc}")
                else:
                    output = output.model_copy(update={"research_blueprint": research_blueprint})
                    research_planner_used = True
                    if research_repair_used:
                        warnings.append("数据研究规划首次输出格式不完整，已完成 1 次不联网、无工具的结构修复。")
                    if research_fallback_used:
                        warnings.append(
                            "数据研究规划器未能收束 JSON；已根据明确的对比对象和页面主题建立保守研究蓝图，"
                            "其中不包含任何事实数值。"
                        )
            mode = "llm"
        except (ModelGatewayError, PresentationStudioServiceError) as exc:
            # 创作计划可以在模型暂不可用时保守降级，但必须把降级事实留在计划与历史中，不能把
            # 固定模板伪装成“AI 已理解客户需求”。用户仍可查看、调整或稍后用模型重新生成。
            output = _build_fallback_output(request=request, requested_slide_count=requested_slide_count)
            mode = "fallback"
            warnings.append(f"模型未能生成结构化创作计划，已提供基础计划：{exc}")
    else:
        output = _build_fallback_output(request=request, requested_slide_count=requested_slide_count)
        warnings.append("当前未启用模型，已按主题关键词生成基础创作计划；请在导出前核验内容。")

    if repair_used:
        warnings.append("模型首次结构化输出未通过校验，已完成 1 次不联网、无工具的格式修复。")
    if research_planner_used:
        warnings.append(
            f"已调用 {research_planner_call_count} 次无工具研究规划复核；"
            "该步骤没有联网，也没有生成或补入事实数值。"
        )
    # 创作规划与数据研究分属不同职责。即使主模型没有联网，它仍可能把记忆中的数字写进正文；
    # 这些数字既没有来源，也可能与导出阶段真正核验出的表格冲突，因此必须在计划落库前移除。
    output, stripped_numeric_claims = _strip_unverified_numeric_claims(output, request=request)
    if stripped_numeric_claims:
        warnings.append("已移除创作计划中的未核验数值表述；具体数值只会在导出阶段通过数据验证后写入图表。")
    await _emit_progress(progress_callback, "presentation_outline_ready", "创作简报已完成，正在建立逐页计划和视觉方向。")

    response = _materialize_plan(
        task_id=stable_task_id,
        request=request,
        output=output,
        requested_slide_count=requested_slide_count,
        mode=mode,
        warnings=warnings,
    )
    workflow_run = _persist_plan(
        response=response,
        request=request,
        started_at=started_at,
        repair_used=repair_used,
        research_planner_call_count=research_planner_call_count,
    )
    response.workflow_run = workflow_run
    clear_dry_run_memory_cache()
    await _emit_progress(progress_callback, "presentation_plan_completed", "PPT 创作计划已生成，等待确认后再进入文件交付。")
    return response


def get_presentation_studio_result(task_id: str) -> PresentationStudioPlanResponse | None:
    """从统一任务历史恢复已经完成的创作计划。"""

    run = load_workflow_run(task_id)
    if run is None:
        return None
    step = next((item for item in reversed(run.steps) if item.step_id == _PRESENTATION_STUDIO_STEP_ID), None)
    if step is None or step.status != "completed":
        return None
    payload = step.output.get("presentation_studio_plan")
    if not isinstance(payload, dict):
        return None
    try:
        response = PresentationStudioPlanResponse.model_validate(payload)
    except ValidationError:
        return None
    response.workflow_run = run
    return response


async def _request_model_plan(
    *,
    runtime: ModelRuntime,
    request: PresentationStudioPlanRequest,
    requested_slide_count: int,
) -> tuple[_StudioModelOutput, bool]:
    """请求一次受控 JSON；失败时最多做一次不开放工具的格式修复。"""

    turn = await runtime.tool_turn(
        system_prompt=_studio_system_prompt(requested_slide_count),
        messages=[ModelConversationMessage(role="user", content=_studio_user_message(request))],
        tools=[],
    )
    try:
        return _parse_model_output(
            turn.content,
            expected_content_slide_count=requested_slide_count - 4,
        ), False
    except PresentationStudioServiceError as first_error:
        # 只修复格式，不扩大主题、事实或素材范围，也不允许模型调用联网/文件工具。
        repair_turn = await runtime.tool_turn(
            system_prompt=_studio_repair_system_prompt(requested_slide_count),
            messages=[
                ModelConversationMessage(
                    role="user",
                    content=(
                        "原始创作需求：\n"
                        f"{_studio_user_message(request)}\n\n"
                        "首次输出（仅用于格式修复，不得新增外部事实）：\n"
                        f"{turn.content[:10_000]}"
                    ),
                )
            ],
            tools=[],
        )
        try:
            return _parse_model_output(
                repair_turn.content,
                expected_content_slide_count=requested_slide_count - 4,
            ), True
        except PresentationStudioServiceError as repair_error:
            raise PresentationStudioServiceError(
                f"模型最终结果没有通过创作计划契约校验；已修复 1 次。{repair_error}"
            ) from first_error


async def _request_research_blueprint(
    *,
    runtime: ModelRuntime,
    request: PresentationStudioPlanRequest,
    output: _StudioModelOutput,
) -> tuple[_StudioResearchBlueprint, bool, bool]:
    """在主创作规划遗漏数据需求时，生成可审计的研究蓝图。

    返回值依次表示蓝图、是否使用过 JSON 修复、是否使用了确定性保守回退。回退只规划查询，
    不生成数值；它用于保护明确的双对象数据对比不因模型字段写法偏差而整条失效。
    """

    visual_intent = _data_visual_intent(request)
    comparison_table_count = max(
        0,
        visual_intent.table_count - int(visual_intent.table_count > 0 and visual_intent.line_count > 0),
    )
    minimum_comparison_metrics = (
        min(6, max(2, comparison_table_count * 2)) if comparison_table_count else 1
    )
    if not visual_intent.explicit and _wants_rich_data_story(request.intent):
        minimum_comparison_metrics = max(3, minimum_comparison_metrics)
    visual_contract = {
        "minimum_native_tables": visual_intent.table_count,
        "minimum_bar_charts": visual_intent.bar_count,
        "minimum_line_charts": visual_intent.line_count,
        "minimum_comparison_metrics": minimum_comparison_metrics,
    }
    system_prompt = (
        "你是 AgentFlow 的数据研究规划器。只输出一个 JSON object，不要 markdown 或解释。"
        "你不联网、不回答事实数值、不创建 URL，也不声称已经查到资料。"
        "上游 Harness 已确认客户明确要求数据对比、统计或趋势；你不再判断是否需要研究，只填写"
        "后续检索计划。必须原样保留客户点名的人名、公司名、产品名和其他实体，不得翻译、缩写或"
        "把人名误解为行业名；输出内容优先使用中文。"
        "必须完整输出 research_question、entities、entity_search_names、metrics、trend_metric、time_scope、comparison_scope、"
        "chart_type、recommended_visuals、visual_metrics、target_slide_index、required_data_points、search_queries、"
        "preferred_source_types。"
        "entities 和 metrics 各至少 1 项，search_queries 必须 3 到 6 条且不得含 URL；"
        "entity_search_names 必须与 entities 等长且顺序一致：每项写该实体用于公开资料检索的正式名称，"
        "优先包含客户原名和完整国际通用名称；无法确定时直接复制对应 entities 项，不得猜造别名。"
        "chart_type 和 recommended_visuals 只能使用 comparison_table、trend_table、comparison_bar、grouped_bar、"
        "horizontal_bar、trend_line、trend_area、share_pie、share_doughnut。chart_type 必须等于 recommended_visuals 第一项；"
        "visual_metrics 必须与 recommended_visuals 等长，每一项列出该视图实际需要的 1 到 3 个 metrics；"
        "target_slide_index 使用从 1 开始的正文页序号；required_data_points 必须是 1 到 36 的整数，"
        "表示预期数值总数，绝不能输出数组。"
        "time_scope 只能复述客户意图中明确给出的年份、日期、季度、赛季或截至条件；客户未给出时，"
        "必须填写“同一来源中明确的统计期间；若动态页面未说明截止日期，仅允许同一页面读取快照”，"
        "不能自行填写某一年、某个赛季或“截至最新完整赛季”。"
        "search_queries 至少一条必须面向全球公开索引；当客户点名的人物、机构或产品有确定的国际通用拼写时，"
        "该条必须同时包含客户原始名称、完整国际通用名称及英文检索词，不能只在中文实体后附加 statistics。"
        "对简称、缩写或可能有其他含义的名称（例如单字母、短简称），必须在每条相关查询中补全其不歧义的正式名称；"
        "这是检索别名，不算新增实体。仍不得新增客户没有点名的研究对象。"
        "例如人物数据对比应规划人物、候选可比指标、统一统计范围和查询语句，但绝不能填写数据值。"
        "客户只给简短数据主题时，应主动把它扩展成 3 到 5 个信息互补的数据视图，而不是固定一张表："
        "双对象或多对象通常使用总览表、适合排名的横向条形图或分组柱图，以及至少一项逐期趋势；"
        "单对象生涯或经营数据通常使用指标总览表、指标画像条形图和逐年/逐季趋势。"
        "饼图或环形图只用于真正的构成占比，面积图只用于连续趋势，不能为了视觉花样误用。"
        "客户明确要求多种数据、多张表格、柱状图或折线图但没有逐项填写指标时，应根据主题主动选择"
        "足以填满 Harness 数量合同的 2 到 6 个常用可量化候选指标，不能只返回一个指标。"
        "客户详细点名图表或数量时必须优先遵从；客户写‘按数据类型选择’时由你根据指标形态推荐组合。"
        "trend_metric 是折线图所需的一个逐年、逐季或逐赛季指标；不需要折线图时可为空字符串。"
        "需要折线图时至少一条查询必须明确请求不少于三个共同期间的数据。Harness 数量合同是硬约束，"
        "模型只负责选择有意义的指标和查询，不能自行减少表格或图表数量。"
    )
    content_titles = [slide.title for slide in output.content_slides]
    explicit_entity_scope = _requested_entity_scope(request.intent)
    entity_scope_notice = (
        "客户明确对象范围（不可增加、不可替换）："
        f"{json.dumps(explicit_entity_scope, ensure_ascii=False)}\n"
        if explicit_entity_scope
        else "客户没有用可确定的对象短语限定范围；不要凭常识补入具体人物、品牌或城市。\n"
    )
    turn = await runtime.tool_turn(
        system_prompt=system_prompt,
        messages=[
            ModelConversationMessage(
                role="user",
                content=(
                    f"客户意图：{request.intent.strip()}\n"
                    f"{entity_scope_notice}"
                    f"演示标题：{output.title}\n"
                    f"正文页标题：{json.dumps(content_titles, ensure_ascii=False)}\n"
                    f"Harness 数量合同：{json.dumps(visual_contract, ensure_ascii=False)}"
                ),
            )
        ],
        tools=[],
    )
    try:
        blueprint = _parse_research_blueprint(turn.content)
        _validate_blueprint_visual_contract(blueprint, visual_contract)
        return blueprint, False, False
    except PresentationStudioServiceError as first_error:
        # 研究规划与主创作计划一样允许一次纯格式修复。修复提示不包含工具，也不允许补事实，
        # 避免为了“有图表”把模型记忆伪装成可验证数据。
        try:
            repair_turn = await runtime.tool_turn(
                system_prompt=_research_blueprint_repair_system_prompt(),
                messages=[
                    ModelConversationMessage(
                        role="user",
                        content=(
                            "客户意图：\n"
                            f"{request.intent.strip()}\n\n"
                            "演示标题：\n"
                            f"{output.title}\n\n"
                            "正文页标题：\n"
                            f"{json.dumps(content_titles, ensure_ascii=False)}\n\n"
                            "Harness 数量合同：\n"
                            f"{json.dumps(visual_contract, ensure_ascii=False)}\n\n"
                            "首次输出（仅用于结构修复，不得补入任何事实数值或来源）：\n"
                            f"{turn.content[:8_000]}"
                        ),
                    )
                ],
                tools=[],
            )
            blueprint = _parse_research_blueprint(repair_turn.content)
            _validate_blueprint_visual_contract(blueprint, visual_contract)
            return blueprint, True, False
        except (ModelGatewayError, PresentationStudioServiceError) as repair_error:
            fallback = _infer_conservative_research_blueprint(request=request, output=output)
            if fallback is not None:
                if visual_contract.get("minimum_line_charts", 0) and fallback.metrics:
                    fallback = fallback.model_copy(
                        update={"trend_metric": _trend_metric_for(fallback.metrics[0])}
                    )
                try:
                    _validate_blueprint_visual_contract(fallback, visual_contract)
                except PresentationStudioServiceError:
                    # 简单的双对象回退只适合一表一图。用户明确要求复杂数据章节时，宁可在计划阶段
                    # 说明缺少哪些规划内容，也不能把两项指标伪装成已经满足三张不同表格。
                    pass
                else:
                    return fallback, True, True
            raise _ResearchBlueprintRequestError(
                "模型没有返回满足表格/图表数量合同的数据研究蓝图；已进行 1 次不联网格式修复。",
                call_count=2,
            ) from repair_error


def _research_blueprint_repair_system_prompt() -> str:
    """返回研究规划专用的最小 JSON 修复约束。"""

    return (
        "你正在修复一份 AgentFlow 数据研究蓝图。只输出一个合法 JSON object，不要 markdown、解释或代码围栏。"
        "不要联网、不要填写事实数值、不要创建 URL、不要新增客户没有点名的实体。"
        "必须且只能包含这些字段：research_question（字符串）、entities（字符串数组）、"
        "entity_search_names（与 entities 同序等长的检索名字符串数组）、metrics（字符串数组）、"
        "trend_metric（折线图的逐期指标字符串；不需要折线图时为空字符串）、"
        "time_scope（字符串）、comparison_scope（字符串）、chart_type（主视图字符串）、"
        "recommended_visuals（1 到 8 个图表类型的字符串数组）、visual_metrics（与推荐视图等长的指标二维数组）、"
        "target_slide_index（1 到 8 的整数）、required_data_points（1 到 36 的整数）、"
        "search_queries（3 到 6 条不含 URL 的字符串数组）、preferred_source_types（字符串数组）。"
        "图表类型只能是 comparison_table、trend_table、comparison_bar、grouped_bar、horizontal_bar、"
        "trend_line、trend_area、share_pie、share_doughnut；chart_type 必须等于 recommended_visuals 第一项。"
        "required_data_points 是数值总数，不是数组。time_scope 只能保留客户明确提出的时间条件；若客户"
        "没有指定时间，填写“同一来源中明确的统计期间；若动态页面未说明截止日期，仅允许同一页面读取快照”。"
        "保留客户点名的 entities；entity_search_names 优先包含客户原名与完整国际通用名称，"
        "无法确定时复制 entities 对应项；这只是后续联网查询计划，不是事实回答。"
        "客户输入简短时应根据数据形态推荐 3 到 5 个互补视图；客户明确要求多种数据或多种图表时，"
        "应保留 2 到 6 个可量化指标；客户要求折线或趋势时，"
        "查询语句必须包含逐年、逐赛季或不少于三个期间的数据意图。"
    )


def _parse_research_blueprint(content: str) -> _StudioResearchBlueprint:
    """把不同 provider 的紧凑字段写法收敛为严格研究蓝图。"""

    try:
        details = _StudioResearchDetails.model_validate(
            _normalize_research_details_payload(_first_json_object(content))
        )
    except (ValidationError, PresentationStudioServiceError) as exc:
        raise PresentationStudioServiceError("模型没有返回合法的数据研究蓝图 JSON。") from exc
    blueprint = _StudioResearchBlueprint(needed=True, **details.model_dump())
    if _normalize_research_blueprint(blueprint) is None:
        raise PresentationStudioServiceError("数据研究蓝图缺少明确对象、指标、图表类型或查询语句。")
    return blueprint


def _validate_blueprint_visual_contract(
    blueprint: _StudioResearchBlueprint,
    contract: dict[str, int],
) -> None:
    """规划模型可以挑指标，但不能把 Harness 已确定的多表/趋势需求缩成一条数据。"""

    minimum_metrics = contract.get("minimum_comparison_metrics", 0)
    if minimum_metrics and len(blueprint.metrics) < minimum_metrics:
        raise PresentationStudioServiceError(
            f"数据研究蓝图仅规划 {len(blueprint.metrics)} 个横向指标，"
            f"不足以承载 {contract.get('minimum_native_tables', 0)} 张不同内容的数据表。"
        )
    if contract.get("minimum_line_charts", 0) and not blueprint.trend_metric.strip():
        raise PresentationStudioServiceError("数据研究蓝图缺少折线图所需的逐期指标。")


def _normalize_research_details_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """兼容不改变研究语义的字段别名，保留正式 Pydantic 契约做最终把关。"""

    raw: dict[str, Any] = dict(payload)
    # 部分模型会套一层 research_plan/data_plan；只在外层没有正式字段时解包，防止任意嵌套
    # 覆盖已经清楚的顶层内容。
    canonical_fields = {
        "research_question", "entities", "entity_search_names", "metrics", "trend_metric", "time_scope", "comparison_scope",
        "chart_type", "recommended_visuals", "visual_metrics", "target_slide_index", "required_data_points",
        "search_queries", "preferred_source_types",
    }
    if not canonical_fields.intersection(raw):
        for container_key in ("research_blueprint", "research_plan", "data_plan", "research", "plan"):
            nested = raw.get(container_key)
            if isinstance(nested, dict):
                raw = dict(nested)
                break

    aliases = {
        "research_question": ("question", "research_goal", "goal", "query_goal"),
        "entities": ("entity", "subjects", "objects", "comparison_entities", "participants"),
        "entity_search_names": (
            "search_entities", "entity_queries", "entity_aliases", "canonical_entities"
        ),
        "metrics": ("metric", "indicators", "indicator", "metrics_to_compare", "data_fields"),
        "trend_metric": ("trend_indicator", "time_series_metric", "line_metric"),
        "time_scope": ("timeframe", "time_range", "period_scope", "date_scope"),
        "comparison_scope": ("comparison_basis", "comparison_rule", "scope", "comparison_standard"),
        "chart_type": ("chart", "chart_kind", "visualization", "visual_type"),
        "recommended_visuals": ("recommended_charts", "visuals", "chart_types", "delivery_views"),
        "visual_metrics": ("chart_metrics", "metric_groups", "visual_metric_groups"),
        "target_slide_index": ("target_slide", "slide_index", "target_page", "page_index"),
        "required_data_points": ("data_points", "point_count", "required_points", "data_point_count"),
        "search_queries": ("queries", "search_terms", "query_list", "research_queries"),
        "preferred_source_types": ("source_types", "preferred_sources", "source_preferences"),
    }
    normalized: dict[str, Any] = {}
    for field_name in canonical_fields:
        value = raw.get(field_name)
        if value is None:
            for alias in aliases[field_name]:
                if raw.get(alias) is not None:
                    value = raw[alias]
                    break
        if value is not None:
            normalized[field_name] = value

    for field_name in (
        "entities", "entity_search_names", "metrics", "search_queries", "preferred_source_types",
        "recommended_visuals",
    ):
        if field_name in normalized:
            normalized[field_name] = _research_string_list(normalized[field_name])
    if "visual_metrics" in normalized:
        normalized["visual_metrics"] = _research_string_groups(normalized["visual_metrics"])
    for field_name in ("research_question", "trend_metric", "time_scope", "comparison_scope"):
        value = normalized.get(field_name)
        if isinstance(value, list):
            normalized[field_name] = "；".join(_research_string_list(value))
    if "chart_type" in normalized:
        normalized["chart_type"] = _normalize_research_chart_type(normalized["chart_type"])
    if "recommended_visuals" in normalized:
        normalized["recommended_visuals"] = [
            _normalize_research_chart_type(value)
            for value in normalized["recommended_visuals"]
        ]
    for field_name in ("target_slide_index", "required_data_points"):
        if field_name in normalized:
            normalized[field_name] = _research_integer(normalized[field_name])
    return normalized


def _research_string_list(value: object) -> list[str]:
    """把单字符串或轻量列表收敛为干净字符串数组，不从自然语言段落推造事实。"""

    if isinstance(value, str):
        values: list[object] = re.split(r"[\n,，;；、]", value)
    elif isinstance(value, list):
        values = value
    elif isinstance(value, dict):
        values = list(value.values())
    else:
        return []
    return [_compact_text(item, 180) for item in values if isinstance(item, str) and item.strip()]


def _research_string_groups(value: object) -> list[list[str]]:
    """兼容模型把每个视图的指标写成字符串或数组，同时保留二维顺序。"""

    if not isinstance(value, list):
        return []
    groups: list[list[str]] = []
    for item in value[:8]:
        group = _research_string_list(item)
        groups.append(group[:3])
    return groups


def _research_integer(value: object) -> int:
    """解析 provider 常把整数写成字符串或单项数组的兼容格式。"""

    if isinstance(value, list):
        return len(value)
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"\d+", value)
        return int(match.group()) if match else 0
    return 0


def _normalize_research_chart_type(value: object) -> str:
    """将常见中文/英文图表名称折叠为交付层可验证的图表语法。"""

    text = _compact_text(value, 80).casefold() if isinstance(value, str) else ""
    if text in _RESEARCH_CHART_TYPES:
        return text
    if any(marker in text for marker in ("环形", "圆环", "doughnut", "donut")):
        return "share_doughnut"
    if any(marker in text for marker in ("饼", "pie")):
        return "share_pie"
    if any(marker in text for marker in ("面积", "area")):
        return "trend_area"
    if any(marker in text for marker in ("折线", "趋势", "line", "trend")):
        return "trend_line"
    if any(marker in text for marker in ("横向", "条形", "horizontal")):
        return "horizontal_bar"
    if any(marker in text for marker in ("分组", "grouped", "clustered")):
        return "grouped_bar"
    if any(marker in text for marker in ("柱", "bar", "column")):
        return "comparison_bar"
    if any(marker in text for marker in ("表", "table", "对比", "比较", "comparison")):
        return "comparison_table"
    return text


def _infer_conservative_research_blueprint(
    *,
    request: PresentationStudioPlanRequest,
    output: _StudioModelOutput,
) -> _StudioResearchBlueprint | None:
    """为明确对象和数据主题建立不含数值的保守研究蓝图。

    这不是按人物或网站硬编码，而是从客户意图和已经生成的页面标题中提取成对对象与可比较指标。
    如果没有这两个明确信号，宁可返回 ``None`` 让用户补充，而不是把模糊主题伪装成数据任务。
    """

    # 客户原句的对象范围优先级高于创作标题。标题可用于补指标，但不能把模型常见联想（例如
    # “梅西”自动带出“C 罗”）升级成研究对象，避免数据页与客户需求错位。
    entity_pair = _infer_entity_pair([request.intent]) or _infer_entity_pair([output.title])
    context = " ".join(
        [request.intent, output.title, *[slide.title for slide in output.content_slides],
         *[bullet for slide in output.content_slides for bullet in slide.bullets]]
    ).casefold()
    metrics = _infer_research_metrics(context)
    if not metrics:
        return None
    target_slide_index = _infer_data_target_slide_index(output.content_slides)
    if entity_pair is None:
        entity = _infer_single_data_entity([request.intent, output.title])
        if not entity:
            return None
        trend_metric = _trend_metric_for(metrics[0])
        return _StudioResearchBlueprint(
            needed=True,
            research_question=f"整理{entity}的{'、'.join(metrics)}及{trend_metric}，明确单位和统计期间。",
            entities=[entity],
            entity_search_names=[entity],
            metrics=metrics,
            trend_metric=trend_metric,
            time_scope="公开来源明确的统计期间",
            comparison_scope="同一对象、明确单位和统计范围",
            chart_type="comparison_table",
            recommended_visuals=["comparison_table", "horizontal_bar", "trend_line"],
            visual_metrics=[metrics[:3], metrics[:3], [trend_metric]],
            target_slide_index=target_slide_index,
            required_data_points=min(36, len(metrics) + 4),
            search_queries=[
                f"{entity} {' '.join(metrics)} official statistics",
                f"{entity} {trend_metric} by period statistics table",
                f"{entity} career profile data statistics",
            ],
            preferred_source_types=["official_statistics", "official_profile"],
        )
    first, second = entity_pair
    return _StudioResearchBlueprint(
        needed=True,
        research_question=f"比较{first}与{second}的{'、'.join(metrics)}，逐项保留公开统计范围与期间。",
        entities=[first, second],
        # 确定性回退不负责翻译或猜别名；正式研究规划器可提供国际通用检索名。
        entity_search_names=[first, second],
        metrics=metrics,
        time_scope="截至公开来源给出的同一统计截止日期",
        comparison_scope="同一统计范围、单位和截止日期",
        chart_type="comparison_table",
        target_slide_index=target_slide_index,
        required_data_points=len(metrics) * 2,
        search_queries=[
            f"{first} {second} {' '.join(metrics)} official statistics",
            f"{first} {' '.join(metrics)} official profile statistics",
            f"{second} {' '.join(metrics)} official profile statistics",
        ],
        preferred_source_types=["official_statistics", "official_profile"],
    )


def _infer_single_data_entity(candidates: list[str]) -> str:
    """仅从“某对象生涯/经营数据”这类明确短句中提取单对象，不从普通主题猜测。"""

    pattern = re.compile(
        r"(?P<entity>[A-Za-z0-9\u4e00-\u9fff·' -]{1,40}?)(?=(?:的)?(?:职业)?生涯(?:数据|统计)|(?:的)?数据(?:全景|统计|\s*ppt|$))",
        flags=re.IGNORECASE,
    )
    for candidate in candidates:
        match = pattern.search(candidate)
        if match is None:
            continue
        entity = _clean_inferred_entity(match.group("entity"))
        if entity:
            return entity
    return ""


def _requested_entity_scope(intent: str) -> list[str]:
    """从客户原句提取不可越界的数据对象范围。

    数据规划模型可以补充指标和图表，但不能把高频关联对象偷偷加入客户只点名的一人、一城或
    一项业务中。这里只接受原句中明确出现的成对对象，或“某对象生涯/数据”形式的单对象；
    无法确定时返回空列表，保留模型对抽象主题的正常规划能力。
    """

    entity_pair = _infer_entity_pair([intent])
    if entity_pair is not None:
        return list(entity_pair)
    entity = _infer_single_data_entity([intent])
    return [entity] if entity else []


def _infer_entity_pair(candidates: list[str]) -> tuple[str, str] | None:
    """仅识别明确写出的“甲与乙 / 甲 vs 乙”，不尝试从句子猜实体。"""

    pattern = re.compile(
        r"(?P<first>[A-Za-z0-9\u4e00-\u9fff·' -]{1,32}?)\s*(?:与|和|vs\.?|对比|比较)\s*"
        r"(?P<second>[A-Za-z0-9\u4e00-\u9fff·' -]{1,32}?)(?=(?:的)?(?:数据|统计|对比|比较|趋势|表现|$|[：:，,。！？!?]))",
        flags=re.IGNORECASE,
    )
    for candidate in candidates:
        match = pattern.search(candidate)
        if match is None:
            continue
        first = _clean_inferred_entity(match.group("first"))
        second = _clean_inferred_entity(match.group("second"))
        if first and second and first.casefold() != second.casefold():
            return first, second
    return None


def _clean_inferred_entity(value: str) -> str:
    """移除句首动作词和 PPT 后缀，仍只保留用户或标题中出现的原文字面。"""

    text = _compact_text(value, 80)
    text = re.sub(
        r"^(?:(?:请|帮我|帮忙|制作|生成|创建|审查|核验|检查|分析|做一份|做个|做|关于|一个|一位|两位|两名)\s*)+",
        "",
        text,
    )
    # 成对短语中的第二个对象常被正则连同“的各种/多种”捕获；这些是范围修饰语，不属于
    # 实体本身。先剥离，避免后续对象合同把“C 罗的各种”误当成正式名称。
    text = re.sub(r"(?:的)?(?:各种|多种|各项|全部|相关|职业生涯|职业|生涯)$", "", text, flags=re.IGNORECASE)
    # “梅西与 C 罗的进球数据对比”里的第二个捕获组可能停在“C 罗的进球”。这里仅剥离
    # 已知的量化指标后缀；指标仍由 `_infer_research_metric()` 从完整主题单独推断，不能混入实体。
    text = re.sub(
        r"(?:的)?(?:(?:职业生涯)?(?:总)?(?:进球|助攻)(?:数)?|销量|营业?收入|市场份额|增长率|人口|gdp)$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(?:\s*(?:pptx?|演示文稿|数据|统计|对比|比较))+$", "", text, flags=re.IGNORECASE)
    return _compact_text(text, 80)


def _infer_research_metric(context: str) -> str:
    """只从客户已经提到的主题词选择常见且可量化的单一指标。"""

    metrics = _infer_research_metrics(context)
    return metrics[0] if metrics else ""


def _infer_research_metrics(context: str) -> list[str]:
    """从客户意图和已生成页面中保留至多三个明确出现的可量化指标。"""

    metric_rules = (
        (("进球", "goals"), "职业生涯总进球数"),
        (("助攻", "assists"), "职业生涯助攻数"),
        (("出场", "appearances", "matches played"), "职业生涯出场次数"),
        (("胜率", "win rate"), "胜率"),
        (("销量", "sales"), "销量"),
        (("营收", "收入", "revenue"), "营业收入"),
        (("市场份额", "market share"), "市场份额"),
        (("增长率", "growth rate"), "增长率"),
        (("人口", "population"), "总人口"),
        (("gdp", "国内生产总值", "经济总量"), "GDP（现价美元）"),
    )
    def contains_marker(marker: str) -> bool:
        if re.fullmatch(r"[a-z ]+", marker):
            return re.search(rf"(?<![a-z]){re.escape(marker)}(?![a-z])", context) is not None
        return marker in context

    metrics = [metric for markers, metric in metric_rules if any(contains_marker(marker) for marker in markers)]
    return list(dict.fromkeys(metrics))[:3]


def _infer_data_target_slide_index(slides: list[_StudioContentSlide]) -> int:
    """优先让图表落到已有数据/对比页，未命中时保持第一张正文页的稳定默认。"""

    markers = ("数据", "统计", "对比", "比较", "趋势", "进球", "销量", "营收", "市场份额")
    for index, slide in enumerate(slides, start=1):
        text = f"{slide.title} {' '.join(slide.bullets)}".casefold()
        if any(marker in text for marker in markers):
            return index
    return 1


_EXPLICIT_DATA_RESEARCH_MARKERS = (
    "数据",
    "统计",
    "对比",
    "比较",
    "趋势",
    "排名",
    "排行",
    "比例",
    "份额",
    "增长率",
    "市场规模",
    "业绩",
    "指标",
    "data",
    "statistic",
    "compare",
    "comparison",
    "trend",
    "ranking",
    "ratio",
    "market share",
    "growth rate",
)


def _has_explicit_data_research_intent(intent: str) -> bool:
    """识别跨领域通用的数据意图，不按人物、行业或站点堆专用关键词。"""

    normalized = intent.casefold()
    return any(marker in normalized for marker in _EXPLICIT_DATA_RESEARCH_MARKERS)


def _has_world_bank_shortcut(intent: str) -> bool:
    """固定 Provider 已能确定性规划时跳过额外模型调用，减少延迟和 Token 消耗。"""

    normalized = intent.casefold()
    has_country = any(alias.casefold() in normalized for alias, _, _ in _WORLD_BANK_COUNTRIES)
    has_indicator = any(
        alias.casefold() in normalized
        for aliases, _, _ in _WORLD_BANK_INDICATORS
        for alias in aliases
    )
    return has_country and has_indicator


def _parse_model_output(
    content: str,
    *,
    expected_content_slide_count: int,
) -> _StudioModelOutput:
    """兼容代码围栏前缀，但仍只接受通过 Pydantic 的完整 JSON object。"""

    payload = _first_json_object(content)
    # 主创作提示词不再要求研究蓝图，但部分供应商会沿用上一轮格式多返回该字段。研究计划
    # 只能来自职责单一的专用规划器，因此这里显式忽略它，既不信任也不让半成品拖垮主计划。
    payload.pop("research_blueprint", None)
    try:
        output = _StudioModelOutput.model_validate(payload)
    except ValidationError as exc:
        raise PresentationStudioServiceError("模型没有返回合法的 PPT 创作计划 JSON。") from exc
    if len(output.content_slides) != expected_content_slide_count:
        raise PresentationStudioServiceError("模型返回的正文页数量与当前 PPT 计划不一致。")
    return output


def _first_json_object(content: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(content):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise PresentationStudioServiceError("模型输出中没有可解析的 JSON 对象。")


def _materialize_plan(
    *,
    task_id: str,
    request: PresentationStudioPlanRequest,
    output: _StudioModelOutput,
    requested_slide_count: int,
    mode: str,
    warnings: list[str],
) -> PresentationStudioPlanResponse:
    """将模型正文页包装成完整、可验证的演示计划。"""

    theme = _normalize_theme(output.theme, request.theme_preference)
    content_slide_count = requested_slide_count - 4
    content_slides = _normalize_content_slides(output.content_slides, target_count=content_slide_count)
    brief = PresentationStudioBrief(
        title=_compact_text(output.title, 160),
        purpose=_compact_text(output.purpose, 360),
        audience=_compact_text(output.audience, 240),
        core_message=_compact_text(output.core_message, 420),
        theme=theme,
        theme_reason=_compact_text(output.theme_reason, 420),
        fact_check_notice=(
            "本计划基于用户提供的主题生成，未联网核验数据、案例或引用；请在确认导出前补充或复核关键事实。"
        ),
    )
    slides: list[PresentationStudioSlidePlan] = [
        PresentationStudioSlidePlan(
            slide_id="cover",
            role="cover",
            layout="cover",
            title=brief.title,
            bullets=[brief.purpose, f"核心信息：{brief.core_message}"],
            visual_direction="以简洁封面、主题色块和留白建立第一印象。",
        ),
        PresentationStudioSlidePlan(
            slide_id="agenda",
            role="agenda",
            layout="agenda",
            title="演示结构",
            bullets=[item.title for item in content_slides],
            visual_direction="用清晰目录建立叙事节奏，避免堆砌细节。",
        ),
    ]
    slides.extend(content_slides)
    slides.append(
        PresentationStudioSlidePlan(
            slide_id="summary",
            role="summary",
            layout="summary",
            title="行动与下一步",
            bullets=_summary_bullets(content_slides, brief.core_message),
            visual_direction="突出行动建议，保留一项明确的结束动作。",
        )
    )
    slides.append(
        PresentationStudioSlidePlan(
            slide_id="sources",
            role="sources",
            layout="sources",
            title="事实核验与创作依据",
            bullets=[
                "创作依据：用户提交的主题说明。",
                "未联网检索或下载外部素材；关键数字、案例和引用需在导出前人工确认。",
            ],
            visual_direction="使用紧凑的说明页，明确当前内容边界和待补充事实。",
        )
    )
    # 数据章节可以占用多张正文页。同一批来源会在导出阶段复用为表格、柱图和有证据的趋势图，
    # 外部图片不能再与这些页面争夺版面，否则客户勾选数据增强后仍只会看到装饰图片。
    data_plan = _data_plan(request, slides=slides, brief=brief, blueprint=output.research_blueprint)
    # 外部视觉只服务封面与具体正文页；目录、总结和来源页保持克制，避免图片稀释信息层级。
    asset_queries = _normalize_asset_queries(output.asset_queries, limit=min(6, content_slide_count + 1))
    reserved_slide_ids: set[str] = set()
    if data_plan.state in {"planned", "provider_planned", "research_planned"}:
        reserved_slide_ids = set(data_plan.visual_slide_ids or [data_plan.slide_id])
    # 先排除数据页再顺序绑定查询。旧实现先绑定后删除，若前几张正文恰好都是数据页，后面的
    # 有效视觉查询也会一起丢失，最终即使 Seedream 成功也没有图片可嵌入叙事页。
    asset_slots = _build_asset_slots(
        slides,
        asset_queries,
        excluded_slide_ids=reserved_slide_ids,
    )
    asset_queries = [slot.query for slot in asset_slots]
    visual_asset_provider = _visual_asset_provider(request)
    asset_plan = PresentationStudioAssetPlan(
        state="planned" if visual_asset_provider != "none" else "not_requested",
        provider="" if visual_asset_provider == "none" else visual_asset_provider,
        queries=asset_queries if visual_asset_provider != "none" else [],
        slots=asset_slots if visual_asset_provider != "none" else [],
        notice=_asset_plan_notice(visual_asset_provider, asset_slots),
    )
    research_plan = _research_plan(request)
    plan_id = _plan_id(
        task_id=task_id,
        brief=brief,
        slides=slides,
        asset_plan=asset_plan,
        research_plan=research_plan,
        data_plan=data_plan,
    )
    return PresentationStudioPlanResponse(
        task_id=task_id,
        plan_id=plan_id,
        mode=mode,  # type: ignore[arg-type]
        brief=brief,
        slides=slides,
        asset_plan=asset_plan,
        research_plan=research_plan,
        data_plan=data_plan,
        warnings=warnings,
    )


def _normalize_content_slides(
    content_slides: list[_StudioContentSlide],
    *,
    target_count: int,
) -> list[PresentationStudioSlidePlan]:
    """限制每页文字密度，同时拒绝让模型创建客户看不见的额外页面。"""

    normalized: list[PresentationStudioSlidePlan] = []
    for index, item in enumerate(content_slides[:target_count], start=1):
        bullets = [_compact_text(bullet, 150) for bullet in item.bullets if bullet.strip()]
        if len(bullets) < 2:
            continue
        normalized.append(
            PresentationStudioSlidePlan(
                slide_id=f"content_{index}",
                role="content",
                layout=_normalize_content_layout(item.layout, index=index),
                title=_compact_text(item.title, 120),
                bullets=bullets[:5],
                visual_direction=_compact_text(item.visual_direction, 300),
            )
        )
    if len(normalized) != target_count:
        raise PresentationStudioServiceError("模型生成的正文页数量或要点密度不符合当前演示计划。")
    return normalized


def _normalize_content_layout(value: str, *, index: int) -> str:
    """只接受已实现的版式语法；模型省略或写错时按稳定序列回退。"""

    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in _CONTENT_LAYOUTS:
        return normalized
    return _FALLBACK_LAYOUT_SEQUENCE[(index - 1) % len(_FALLBACK_LAYOUT_SEQUENCE)]


_NUMERIC_TOKEN_PATTERN = re.compile(r"\d+(?:[.,]\d+)?")


def _strip_unverified_numeric_claims(
    output: _StudioModelOutput,
    *,
    request: PresentationStudioPlanRequest,
) -> tuple[_StudioModelOutput, bool]:
    """移除模型擅自补入、且客户没有给出的数值型事实断言。

    该步骤不修改用户自己输入的数字，也不改动后续 ResearchGateway 的结构化数据。它只处理计划
    阶段模型写进正文的数字：这些数值尚未联网、没有来源，绝不能与确认导出后经过 Verifier 的表格
    并列展示。用待核验表达替换整条要点比局部删字符更清楚，也避免留下“约”“+”等误导性残片。
    """

    user_numbers = set(_NUMERIC_TOKEN_PATTERN.findall(request.intent))

    def is_unverified_numeric_text(text: str) -> bool:
        numbers = _NUMERIC_TOKEN_PATTERN.findall(text)
        return bool(numbers) and any(number not in user_numbers for number in numbers)

    changed = False
    sanitized_slides: list[_StudioContentSlide] = []
    for slide in output.content_slides:
        bullets: list[str] = []
        for bullet in slide.bullets:
            if is_unverified_numeric_text(bullet):
                bullets.append("具体数值将在确认导出后依据可追溯来源补充，并与统计口径一并展示。")
                changed = True
            else:
                bullets.append(bullet)
        sanitized_slides.append(slide.model_copy(update={"bullets": bullets}))
    if not changed:
        return output, False
    return output.model_copy(update={"content_slides": sanitized_slides}), True


def _summary_bullets(slides: list[PresentationStudioSlidePlan], core_message: str) -> list[str]:
    bullets = [f"回到核心信息：{_compact_text(core_message, 130)}"]
    for slide in slides[:2]:
        if slide.bullets:
            bullets.append(_compact_text(slide.bullets[0], 130))
    bullets.append("确认关键事实与素材范围后，再导出正式演示文稿。")
    return bullets[:4]


def _normalize_theme(model_theme: str, preference: str) -> str:
    if preference in _THEMES:
        return preference
    normalized = model_theme.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized if normalized in _THEMES else "executive_blue"


def _normalize_asset_queries(values: list[str], *, limit: int) -> list[str]:
    """保留短且不重复的英文检索词，顺序会在随后绑定到具体幻灯片。"""

    seen: set[str] = set()
    queries: list[str] = []
    for value in values:
        query = _compact_text(value, 120)
        key = query.casefold()
        if len(query) < 3 or key in seen:
            continue
        seen.add(key)
        queries.append(query)
        if len(queries) == max(1, min(limit, 6)):
            break
    return queries


def _build_asset_slots(
    slides: list[PresentationStudioSlidePlan],
    queries: list[str],
    *,
    excluded_slide_ids: set[str] | None = None,
) -> list[PresentationStudioAssetSlot]:
    """把模型给出的有序检索词绑定到封面和正文，交付层不得再猜测图文关系。"""

    excluded_slide_ids = excluded_slide_ids or set()
    visual_slides = [
        slide
        for slide in slides
        if slide.role in {"cover", "content"} and slide.slide_id not in excluded_slide_ids
    ]
    slots: list[PresentationStudioAssetSlot] = []
    for slide, query in zip(visual_slides, queries, strict=False):
        purpose = (
            "建立主题的第一视觉印象"
            if slide.role == "cover"
            else f"支撑“{slide.title}”这一页的主要视觉信息"
        )
        slots.append(
            PresentationStudioAssetSlot(
                slide_id=slide.slide_id,
                slide_title=slide.title,
                query=query,
                purpose=purpose,
            )
        )
    return slots


def _visual_asset_provider(request: PresentationStudioPlanRequest) -> str:
    """归一视觉素材策略，并继续兼容只认识 V2.1 布尔字段的历史客户端。"""

    if request.visual_asset_provider != "none":
        return request.visual_asset_provider
    return "pexels" if request.allow_licensed_assets else "none"


def _asset_plan_notice(provider: str, asset_slots: list[PresentationStudioAssetSlot]) -> str:
    """让计划预览清楚区分“已规划”与“已经联网/已经生成”。"""

    if provider == "pexels":
        return (
            f"已为 {len(asset_slots)} 个页面生成 Pexels 授权图片槽位；计划阶段没有联网。"
            "确认导出时会显示本次联网读取说明，并保留摄影师与来源。"
        )
    if provider == "seedream":
        return (
            f"已为 {len(asset_slots)} 个页面生成 Seedream AI 图片槽位；计划阶段没有调用图像模型。"
            "确认导出时最多生成 4 张无文字水印的横向图片，并在任务历史保留模型和提示词摘要。"
        )
    return "当前计划只使用内置主题、构图和信息层级，不请求外部图片；内置版式始终会生效。"


def _research_plan(request: PresentationStudioPlanRequest) -> PresentationStudioResearchPlan:
    """把公开资料开关显式固化进计划，禁止在导出时暗中扩大联网范围。"""

    if request.public_research_enabled:
        return PresentationStudioResearchPlan(
            state="planned",
            provider="wikimedia",
            max_sources=3,
            notice=(
                "已预留最多 3 条 Wikimedia 公开资料参考；计划阶段没有联网。确认导出时才会读取"
                "固定公开接口，并在来源页和任务历史记录标题、链接与抓取时间。它不替代专业数据核验，"
                "也不会自动把资料内容写成结论或统计图表。"
            ),
        )
    return PresentationStudioResearchPlan(
        notice="当前计划不补充公开资料来源；关键数字、案例和引用仍需人工核验。"
    )


_WORLD_BANK_COUNTRIES = (
    ("中国", "CHN", "中国"),
    ("china", "CHN", "中国"),
    ("美国", "USA", "美国"),
    ("united states", "USA", "美国"),
    ("日本", "JPN", "日本"),
    ("japan", "JPN", "日本"),
    ("德国", "DEU", "德国"),
    ("germany", "DEU", "德国"),
    ("法国", "FRA", "法国"),
    ("france", "FRA", "法国"),
    ("英国", "GBR", "英国"),
    ("united kingdom", "GBR", "英国"),
    ("印度", "IND", "印度"),
    ("india", "IND", "印度"),
    ("巴西", "BRA", "巴西"),
    ("brazil", "BRA", "巴西"),
    ("韩国", "KOR", "韩国"),
    ("south korea", "KOR", "韩国"),
)
_WORLD_BANK_INDICATORS = (
    (("人均国内生产总值", "人均gdp", "per capita gdp"), "NY.GDP.PCAP.CD", "人均 GDP（现价美元）"),
    (("国内生产总值", "gdp", "经济总量", "gross domestic product"), "NY.GDP.MKTP.CD", "GDP（现价美元）"),
    (("人口", "population"), "SP.POP.TOTL", "总人口"),
)


def _data_plan(
    request: PresentationStudioPlanRequest,
    *,
    slides: list[PresentationStudioSlidePlan],
    brief: PresentationStudioBrief,
    blueprint: _StudioResearchBlueprint,
) -> PresentationStudioDataPlan:
    """将模型研究蓝图固化为通用计划，并优先路由到已验证的快捷 Provider。"""

    if not request.structured_data_enabled:
        return PresentationStudioDataPlan(
            notice="当前计划未请求结构化数据图表；不会为了装饰而自动加入图表。"
        )
    data_slide = _research_target_slide(slides, blueprint.target_slide_index)
    if data_slide is None:
        return PresentationStudioDataPlan(notice="当前创作计划没有可承载数据图表的正文页，已跳过图表。")

    # 研究蓝图来自模型，不能直接拥有“研究对象”的最终决定权。客户明确点名的对象在此固化，
    # 后续 World Bank 路由、数据模型和 PPT 渲染都只会看到收敛后的范围。
    grounded_blueprint = _enforce_requested_entity_scope(request, blueprint)

    # World Bank 仍是高可信快捷通道，但识别范围同时消费模型给出的对象/指标，不再独占
    # 整个数据研究能力。未命中时保留通用研究蓝图，交给下一段 ResearchGateway 执行。
    text = " ".join(
        (
            request.intent,
            brief.title,
            brief.purpose,
            brief.core_message,
            " ".join(grounded_blueprint.entities),
            " ".join(grounded_blueprint.metrics),
        )
    ).casefold()
    countries: list[tuple[str, str]] = []
    seen_codes: set[str] = set()
    for alias, code, label in _WORLD_BANK_COUNTRIES:
        if alias.casefold() in text and code not in seen_codes:
            countries.append((code, label))
            seen_codes.add(code)
        if len(countries) == 4:
            break
    indicator = next(
        ((code, label) for aliases, code, label in _WORLD_BANK_INDICATORS if any(alias.casefold() in text for alias in aliases)),
        None,
    )
    if not countries or indicator is None:
        normalized_blueprint = _normalize_research_blueprint(grounded_blueprint)
        if normalized_blueprint is None:
            return PresentationStudioDataPlan(
                notice=(
                    "已请求智能补充，但本次规划没有形成足够明确的研究对象、指标和查询语句；"
                    "不会以模型记忆或模糊主题强行生成数据图表。"
                )
            )
        # 数据增强的产品目标是一个可用的数据章节，而不是把客户的多指标要求压成一格数字。
        # 蓝图最多保留六个指标，可按用户数量合同拆成多个各有主题的可编辑表格。
        focused_blueprint = _focus_broad_comparison_blueprint(normalized_blueprint)
        # 研究规划器只能复述客户明确提出的时间条件，不能为了看起来具体而凭空写入某个年份。
        # 客户未指定截止日时，统一允许“同源明确期间”或受限的“同页读取快照”两种可审计口径。
        scoped_blueprint = _align_research_time_scope(request, focused_blueprint)
        # 模型经常把每条查询都写成“实体 + statistics”。对双对象单指标比较，这会把搜索结果
        # 推向两份单对象资料；补一条不带英文尾缀的原始成对查询，有利于召回同页对比资料，且
        # 仍完全由已确认的对象和指标组成，不新增主题、Provider 或联网预算。
        scoped_blueprint = _ensure_pair_comparison_query(scoped_blueprint)
        requested_visuals = _requested_data_visuals(request, scoped_blueprint)
        scoped_blueprint = _ensure_data_visual_queries(
            scoped_blueprint,
            requested_visuals=requested_visuals,
        )
        trend_metric = (
            scoped_blueprint.trend_metric.strip() or _trend_metric_for(scoped_blueprint.metrics[0])
            if any(visual in {"trend_line", "trend_area"} for visual in requested_visuals)
            and scoped_blueprint.metrics
            else ""
        )
        visual_slide_ids = _data_visual_slide_ids(
            slides,
            target_slide_id=data_slide.slide_id,
            count=len(requested_visuals),
        )
        requested_visuals = requested_visuals[: len(visual_slide_ids)]
        visual_metrics = _data_visual_metric_groups(
            requested_visuals,
            metrics=scoped_blueprint.metrics,
            trend_metric=trend_metric,
            entity_count=len(scoped_blueprint.entities),
            recommended_visuals=scoped_blueprint.recommended_visuals,
            recommended_groups=scoped_blueprint.visual_metrics,
        )
        data_point_budget = _data_point_budget(scoped_blueprint, requested_visuals)
        visual_intent = _data_visual_intent(request)
        focus_notice = ""
        if scoped_blueprint.metrics != normalized_blueprint.metrics:
            focus_notice = (
                "为保证研究预算和页面可读性，本次数据章节最多使用六项候选指标；"
                "每张横向表最多承载三项。"
            )
        return PresentationStudioDataPlan(
            state="research_planned",
            provider="research_gateway",
            chart_type=requested_visuals[0],
            slide_id=visual_slide_ids[0],
            requested_visuals=requested_visuals,
            visual_slide_ids=visual_slide_ids,
            visual_metrics=visual_metrics,
            required_table_count=visual_intent.table_count,
            required_bar_chart_count=visual_intent.bar_count,
            required_line_chart_count=visual_intent.line_count,
            required_visual_count=visual_intent.total,
            visual_contract_explicit=visual_intent.explicit,
            max_points=data_point_budget,
            research_question=scoped_blueprint.research_question,
            entities=scoped_blueprint.entities,
            entity_search_names=scoped_blueprint.entity_search_names,
            metrics=scoped_blueprint.metrics,
            trend_metric=trend_metric,
            time_scope=scoped_blueprint.time_scope,
            comparison_scope=scoped_blueprint.comparison_scope,
            required_data_points=data_point_budget,
            search_queries=scoped_blueprint.search_queries,
            preferred_source_types=scoped_blueprint.preferred_source_types,
            notice=(
                f"已生成“{_compact_text(scoped_blueprint.research_question, 120)}”的数据研究蓝图，"
                f"包含 {len(scoped_blueprint.search_queries)} 条内部数据规划语句；计划阶段尚未生成数值。"
                "确认导出后，已配置模型会按这份蓝图直接生成可编辑数据，默认不联网、不做网页核验；"
                f"本次会交付 {len(requested_visuals)} 个数据视图；"
                f"其中数量合同为 {visual_intent.table_count} 张表、{visual_intent.bar_count} 张柱图、"
                f"{visual_intent.line_count} 张折线图、{visual_intent.pie_count + visual_intent.doughnut_count} 张饼/环图。"
                "数据图表会作为可编辑 PowerPoint 原生对象写入文件。"
                + (focus_notice if focus_notice else "")
            )
        )
    chart_type = "comparison_bar" if len(countries) >= 2 else "trend_line"
    indicator_code, indicator_name = indicator
    return PresentationStudioDataPlan(
        state="provider_planned",
        provider="world_bank",
        chart_type=chart_type,
        slide_id=data_slide.slide_id,
        requested_visuals=[chart_type],
        visual_slide_ids=[data_slide.slide_id],
        indicator_code=indicator_code,
        indicator_name=indicator_name,
        country_codes=[code for code, _ in countries],
        country_names=[label for _, label in countries],
        max_points=6 if chart_type == "trend_line" else len(countries),
        research_question=grounded_blueprint.research_question,
        entities=grounded_blueprint.entities or [label for _, label in countries],
        entity_search_names=grounded_blueprint.entity_search_names or grounded_blueprint.entities or [
            label for _, label in countries
        ],
        metrics=grounded_blueprint.metrics or [indicator_name],
        time_scope=grounded_blueprint.time_scope,
        comparison_scope=grounded_blueprint.comparison_scope,
        required_data_points=grounded_blueprint.required_data_points,
        search_queries=grounded_blueprint.search_queries,
        preferred_source_types=grounded_blueprint.preferred_source_types,
        notice=(
            f"已预留 1 张 {indicator_name}{'对比' if chart_type == 'comparison_bar' else '趋势'}图；"
            "计划阶段没有读取数据。确认导出时才会调用固定 World Bank 指标接口，且仅在所有国家"
            "存在同一年度数据时生成对比图。"
        ),
    )


_RESEARCH_CHART_TYPES = {
    "comparison_table",
    "trend_table",
    "comparison_bar",
    "grouped_bar",
    "horizontal_bar",
    "trend_line",
    "trend_area",
    "share_pie",
    "share_doughnut",
}


def _normalize_research_blueprint(
    blueprint: _StudioResearchBlueprint,
) -> _StudioResearchBlueprint | None:
    """拒绝没有明确对象、指标和查询语句的装饰性研究计划。"""

    if not blueprint.needed or blueprint.chart_type not in _RESEARCH_CHART_TYPES:
        return None
    entities = [_compact_text(item, 80) for item in blueprint.entities if item.strip()]
    search_names = [
        _compact_text(item, 140) for item in blueprint.entity_search_names if item.strip()
    ]
    # 别名是检索优化而不是执行前提；数量错位时整体退回显示名，避免把甲方别名用于乙方。
    if len(search_names) != len(entities):
        search_names = list(entities)
    metrics = [_compact_text(item, 100) for item in blueprint.metrics if item.strip()]
    recommended_visuals = [
        visual for visual in blueprint.recommended_visuals if visual in _RESEARCH_CHART_TYPES
    ]
    if not recommended_visuals:
        recommended_visuals = [blueprint.chart_type]
    if recommended_visuals[0] != blueprint.chart_type:
        recommended_visuals.insert(0, blueprint.chart_type)
    recommended_visuals = recommended_visuals[:8]
    visual_metrics: list[list[str]] = []
    metric_keys = {item.casefold(): item for item in metrics}
    for group in blueprint.visual_metrics[: len(recommended_visuals)]:
        normalized_group = [
            metric_keys[item.casefold()]
            for item in group
            if item.casefold() in metric_keys
        ][:3]
        visual_metrics.append(list(dict.fromkeys(normalized_group)))
    while len(visual_metrics) < len(recommended_visuals):
        visual_metrics.append([])
    queries = _normalize_research_queries(blueprint.search_queries)
    if not entities or not metrics or len(queries) < 3 or not blueprint.research_question.strip():
        return None
    return blueprint.model_copy(
        update={
            "research_question": _compact_text(blueprint.research_question, 500),
            "entities": entities[:6],
            "entity_search_names": search_names[:6],
            "metrics": metrics[:6],
            "trend_metric": _compact_text(blueprint.trend_metric, 120),
            "recommended_visuals": recommended_visuals,
            "visual_metrics": visual_metrics,
            "time_scope": _compact_text(blueprint.time_scope or "以可获得的最新统一口径为准", 200),
            "comparison_scope": _compact_text(blueprint.comparison_scope or "同一来源、单位和时间口径", 300),
            "required_data_points": max(1, min(36, blueprint.required_data_points)),
            "search_queries": queries,
            "preferred_source_types": [
                _compact_text(item, 80) for item in blueprint.preferred_source_types if item.strip()
            ][:5],
        }
    )


def _enforce_requested_entity_scope(
    request: PresentationStudioPlanRequest,
    blueprint: _StudioResearchBlueprint,
) -> _StudioResearchBlueprint:
    """让模型蓝图回到客户明确指定的对象范围内。

    这是一层本地 Harness，不依赖模型是否理解“不要新增实体”的提示。只有原句能稳定提取
    单对象或成对对象时才接管；抽象主题仍保留模型的规划空间。重建内部查询是为了避免被剔除
    的实体残留在后续数据模型上下文中，即使当前默认流程不联网也保持任务快照自洽。
    """

    entities = _requested_entity_scope(request.intent)
    metrics = [_compact_text(metric, 100) for metric in blueprint.metrics if metric.strip()]
    if not entities or not metrics:
        return blueprint

    trend_metric = _compact_text(blueprint.trend_metric, 120) or _trend_metric_for(metrics[0])
    metric_text = "、".join(metrics)
    if len(entities) == 1:
        entity = entities[0]
        research_question = f"整理{entity}的{metric_text}与{trend_metric}，仅覆盖客户明确指定的对象。"
        comparison_scope = f"仅限{entity}；不引入客户未点名的对比对象、品牌、城市或人物。"
        search_queries = [
            f"{entity} {' '.join(metrics)} data",
            f"{entity} {trend_metric} data by period",
            f"{entity} career profile statistics",
        ]
    else:
        first, second = entities[:2]
        research_question = f"比较{first}与{second}的{metric_text}，仅覆盖客户明确指定的对象。"
        comparison_scope = f"仅限{first}与{second}；不引入客户未点名的对比对象、品牌、城市或人物。"
        search_queries = [
            f"{first} {second} {' '.join(metrics)} data comparison",
            f"{first} {trend_metric} data by period",
            f"{second} {trend_metric} data by period",
        ]

    return blueprint.model_copy(
        update={
            "research_question": research_question,
            "entities": entities,
            # 客户原始写法是对象合同；检索别名只能在后续明确、可验证的 alias 层补充，不能
            # 伪装为第二个研究对象写回这里。
            "entity_search_names": list(entities),
            "trend_metric": trend_metric,
            "comparison_scope": comparison_scope,
            "search_queries": search_queries,
        }
    )


def _focus_broad_comparison_blueprint(
    blueprint: _StudioResearchBlueprint,
) -> _StudioResearchBlueprint:
    """把无限扩张的研究范围限制为六个指标，足够拆成多个可读数据表。"""

    if len(blueprint.entities) != 2 or len(blueprint.metrics) <= 6:
        return blueprint
    metrics = blueprint.metrics[:6]
    first, second = blueprint.entities
    search_names = (
        blueprint.entity_search_names
        if len(blueprint.entity_search_names) == len(blueprint.entities)
        else blueprint.entities
    )
    first_search, second_search = search_names
    return blueprint.model_copy(
        update={
            "research_question": (
                f"比较{first}与{second}的{'、'.join(metrics)}，逐项保留公开来源、单位和统计期间。"
            ),
            "metrics": metrics,
            "required_data_points": min(36, len(blueprint.entities) * len(metrics)),
            # 查询仍是通用研究语言，不按人物或网站硬编码；共同查询与两端查询都携带完整指标。
            "search_queries": [
                f"{first_search} {second_search} {' '.join(metrics)} statistics",
                f"{first_search} {' '.join(metrics)} statistics",
                f"{second_search} {' '.join(metrics)} statistics",
            ],
        }
    )


def _requested_data_visuals(
    request: PresentationStudioPlanRequest,
    blueprint: _StudioResearchBlueprint,
) -> list[str]:
    """融合客户硬要求与规划模型的数据形态判断，生成可解释的多视图章节。"""

    intent = _data_visual_intent(request)
    visuals: list[str] = []
    table_count = intent.table_count
    bar_count = intent.bar_count
    line_count = intent.line_count
    if not intent.explicit:
        # 简短主题不应被关键词规则压成一张表。专用研究规划器先按数据形态推荐，Harness
        # 再补足基础信息层级并限制总数；详细点名数量的请求仍走下面的硬合同分支。
        visuals = [
            visual for visual in blueprint.recommended_visuals if visual in _RESEARCH_CHART_TYPES
        ]
        if not visuals and blueprint.chart_type in _RESEARCH_CHART_TYPES:
            visuals = [blueprint.chart_type]
        if len(blueprint.entities) <= 1:
            # 单对象“生涯数据”常包含进球、助攻、出场等不同量纲。分组柱图需要同量纲的
            # 多系列，直接采用它会让数据层只能拒绝视图；这里改为对单对象更稳的指标画像，
            # 趋势仍由折线/面积图承担。客户明确点名柱图时不会走这条自动替换。
            visuals = [
                "horizontal_bar" if visual in {"comparison_bar", "grouped_bar"} else visual
                for visual in visuals
            ]
            visuals = list(dict.fromkeys(visuals))
        # 数据表是客户复核数值和单位的底稿；即使规划模型先推荐图形，也让总览表先出现，
        # 后续原生图表再承担模式识别。只有客户明确点名数量时才不自动加入。
        visuals = [visual for visual in visuals if visual != "comparison_table"]
        visuals.insert(0, "comparison_table")
        minimum = 4 if _wants_rich_data_story(request.intent) else 3
        candidates = (
            ["comparison_table", "grouped_bar", "horizontal_bar", "trend_line", "trend_area"]
            if len(blueprint.entities) >= 2
            else ["comparison_table", "horizontal_bar", "trend_line", "trend_area"]
        )
        for candidate in candidates:
            if len(visuals) >= minimum:
                break
            if candidate not in visuals:
                visuals.append(candidate)
        return visuals[:8]

    # 折线图的数据底稿本身就是一张有价值的趋势明细表。客户要求多张表时，优先让其中
    # 一张承载折线序列，其余表格再按不同指标组拆分，避免重复同一总览。
    trend_table_count = 1 if table_count and line_count else 0
    visuals.extend("comparison_table" for _ in range(max(0, table_count - trend_table_count)))
    visuals.extend("trend_table" for _ in range(trend_table_count))
    visuals.extend("comparison_bar" for _ in range(bar_count))
    visuals.extend("trend_line" for _ in range(line_count))
    visuals.extend("share_pie" for _ in range(intent.pie_count))
    visuals.extend("share_doughnut" for _ in range(intent.doughnut_count))
    visuals.extend("trend_area" for _ in range(intent.area_count))
    # 客户只写“做 4 张图表”时，数量本身也是硬要求。未点名的部分采用互补的数据视图补齐，
    # 而不是把它退化成一张表；点名的类型仍保留在前面，方便用户预期交付结果。
    fallback_visuals = ["comparison_table", "comparison_bar", "trend_line", "share_pie", "horizontal_bar"]
    fallback_index = 0
    while len(visuals) < intent.total and len(visuals) < 8:
        candidate = fallback_visuals[fallback_index % len(fallback_visuals)]
        fallback_index += 1
        if candidate in visuals and fallback_index <= len(fallback_visuals):
            continue
        visuals.append(candidate)
    if not visuals:
        visuals.append(blueprint.chart_type if blueprint.chart_type in _RESEARCH_CHART_TYPES else "comparison_table")
    # 没有点名图形时采用“表格 + 主指标柱图”的实用默认；明确数量时绝不擅自加减。
    if not intent.explicit and len(blueprint.entities) >= 2 and visuals == ["comparison_table"]:
        visuals.append("comparison_bar")
    return visuals[:8]


def _wants_rich_data_story(intent: str) -> bool:
    """识别用户希望系统自行展开的广义数据故事，不绑定具体行业或人物。"""

    text = intent.casefold()
    markers = (
        "各种数据", "多种数据", "数据全景", "生涯数据", "多张图表", "多个图表",
        "按数据类型", "全面数据", "丰富数据", "various data", "multiple charts",
    )
    return any(marker in text for marker in markers)


_VISUAL_COUNT_WORDS = {
    "一": 1, "一个": 1, "二": 2, "两": 2, "两个": 2, "三": 3, "三个": 3,
    "四": 4, "四个": 4, "五": 5, "五个": 5, "六": 6, "六个": 6,
}


def _data_visual_intent(request: PresentationStudioPlanRequest) -> _DataVisualIntent:
    """读取用户点名的视图类型和最小数量，重复表达取最大值而不是相加。"""

    text = request.intent.casefold()

    def count_for(pattern: str, markers: tuple[str, ...], *, maximum: int) -> tuple[int, bool]:
        counts: list[int] = []
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            token = match.group("count")
            if token.isdigit():
                counts.append(int(token))
            else:
                counts.append(_VISUAL_COUNT_WORDS.get(token, 1))
        mentioned = any(marker in text for marker in markers)
        if counts:
            return min(maximum, max(counts)), True
        if mentioned:
            return 1, True
        return 0, False

    table_count, table_explicit = count_for(
        r"(?:至少|不少于|最少|要有|需要|包含|生成)?\s*(?P<count>\d+|一|二|两|三|四|五|六)(?:个|张)?\s*(?:数据)?(?:表格|数据表)",
        ("表格", "数据表", "table"),
        maximum=6,
    )
    if re.search(r"(?:多|几)[^，。；,;\n]{0,12}(?:表格|数据表)", text) or "multiple tables" in text:
        table_count = max(table_count, 2)
        table_explicit = True
    bar_count, bar_explicit = count_for(
        r"(?:至少|不少于|最少|要有|需要|包含|生成)?\s*(?P<count>\d+|一|二|两)(?:个|张)?\s*(?:柱状图|柱形图|条形图|bar charts?)",
        ("柱状", "柱形", "条形", "bar chart", "column chart"),
        maximum=1,
    )
    line_count, line_explicit = count_for(
        r"(?:至少|不少于|最少|要有|需要|包含|生成)?\s*(?P<count>\d+|一|二|两)(?:个|张)?\s*(?:折线图|趋势图|line charts?)",
        ("折线", "趋势图", "line chart"),
        maximum=1,
    )
    # 用户没有写数量但明确说“逐年/历年/赛季趋势”时，仍应判断需要一张折线图。
    if not line_explicit and any(marker in text for marker in ("趋势", "逐年", "历年", "赛季")):
        line_count = 1
    pie_count, pie_explicit = count_for(
        r"(?:至少|不少于|最少|要有|需要|包含|生成)?\s*(?P<count>\d+|一|二|两)(?:个|张)?\s*(?:饼状图|饼图|pie charts?)",
        ("饼状图", "饼图", "pie chart"),
        maximum=2,
    )
    doughnut_count, doughnut_explicit = count_for(
        r"(?:至少|不少于|最少|要有|需要|包含|生成)?\s*(?P<count>\d+|一|二|两)(?:个|张)?\s*(?:环形图|圆环图|doughnut|donut)",
        ("环形图", "圆环图", "doughnut", "donut"),
        maximum=2,
    )
    area_count, area_explicit = count_for(
        r"(?:至少|不少于|最少|要有|需要|包含|生成)?\s*(?P<count>\d+|一|二|两)(?:个|张)?\s*(?:面积图|area charts?)",
        ("面积图", "area chart"),
        maximum=2,
    )
    generic_visual_count, generic_explicit = count_for(
        r"(?:至少|不少于|最少|要有|需要|包含|生成)?\s*(?P<count>\d+|一|二|两|三|四|五|六)(?:个|张)?\s*(?:数据)?(?:图表|图形|charts?)",
        ("图表", "数据图", "charts"),
        maximum=8,
    )
    return _DataVisualIntent(
        table_count=table_count,
        bar_count=bar_count,
        line_count=line_count,
        pie_count=pie_count,
        doughnut_count=doughnut_count,
        area_count=area_count,
        generic_visual_count=generic_visual_count,
        explicit=(
            table_explicit or bar_explicit or line_explicit or pie_explicit or doughnut_explicit
            or area_explicit or generic_explicit
        ),
    )


def _data_visual_metric_groups(
    visuals: list[str],
    *,
    metrics: list[str],
    trend_metric: str,
    entity_count: int,
    recommended_visuals: list[str] | None = None,
    recommended_groups: list[list[str]] | None = None,
) -> list[list[str]]:
    """为每个视图分配独立指标组；相邻表格不会再默认展示同一组数据。"""

    comparison_total = visuals.count("comparison_table")
    comparison_groups: list[list[str]] = []
    if comparison_total:
        group_size = min(3, max(1, math.ceil(len(metrics) / comparison_total)))
        comparison_groups = [
            metrics[index * group_size : (index + 1) * group_size]
            for index in range(comparison_total)
        ]
    groups: list[list[str]] = []
    comparison_index = 0
    used_recommendations: set[int] = set()
    recommended_visuals = recommended_visuals or []
    recommended_groups = recommended_groups or []
    metric_keys = {metric.casefold(): metric for metric in metrics}
    for visual in visuals:
        planned_group: list[str] = []
        for index, recommended in enumerate(recommended_visuals):
            if index in used_recommendations or recommended != visual or index >= len(recommended_groups):
                continue
            planned_group = [
                metric_keys[item.casefold()]
                for item in recommended_groups[index]
                if item.casefold() in metric_keys
            ][:3]
            used_recommendations.add(index)
            break
        # 单对象饼图必须有至少三项组成数据。不能因为规划模型只给了一项“主指标”，就让
        # 渲染阶段必然失败；此处改用下方的三项兜底组。
        if planned_group and not (
            visual in {"share_pie", "share_doughnut"}
            and entity_count <= 1
            and len(planned_group) < 3
        ):
            groups.append(list(dict.fromkeys(planned_group)))
            continue
        if visual == "comparison_table":
            groups.append(comparison_groups[comparison_index] if comparison_index < len(comparison_groups) else [])
            comparison_index += 1
        elif visual in {"trend_table", "trend_line", "trend_area"}:
            groups.append([trend_metric] if trend_metric else [])
        elif visual in {"grouped_bar", "horizontal_bar"}:
            groups.append(metrics[:3])
        elif visual in {"share_pie", "share_doughnut"}:
            groups.append(metrics[:3] if entity_count <= 1 else metrics[:1])
        else:
            groups.append(metrics[:1])
    return groups


def _ensure_data_visual_queries(
    blueprint: _StudioResearchBlueprint,
    *,
    requested_visuals: list[str],
) -> _StudioResearchBlueprint:
    """为多指标和趋势视图补足可执行查询，不新增客户未要求的对象或指标。"""

    if len(blueprint.entities) < 2:
        return blueprint
    search_names = (
        blueprint.entity_search_names
        if len(blueprint.entity_search_names) == len(blueprint.entities)
        else blueprint.entities
    )
    entities = " ".join(search_names[:2])
    metrics = blueprint.metrics[:6]
    candidates: list[str] = []
    if any(visual in {"trend_line", "trend_area"} for visual in requested_visuals) and metrics:
        trend_metric = _trend_metric_for(metrics[0])
        trend_columns = _trend_search_columns(trend_metric)
        # 规划器生成的查询通常包含客户原名与国际通用名。把前三条分别改造成“共同对比表”和
        # “个体逐期表”查询，比只在中文实体后追加 statistics 更容易召回结构化序列；仍只消费
        # 原有六条查询预算，不新增实体、URL 或事实。
        pair_seed, entity_seeds = _trend_query_seeds(blueprint)
        if pair_seed:
            candidates.append(f"{pair_seed} {trend_metric} {trend_columns} by period comparison table")
            candidates.extend(
                f"{query} {trend_metric} {trend_columns} every season yearly statistics table"
                for query in entity_seeds
            )
        else:
            candidates.append(f"{entities} {trend_metric} {trend_columns} by period comparison table")
            candidates.extend(
                f"{entity} {trend_metric} {trend_columns} every season yearly statistics table"
                for entity in search_names[:2]
            )
    # 后半组三条保留规划器已经给出的总量/官方资料查询；趋势与总量各占有限配额。
    candidates.extend(blueprint.search_queries[:3])
    candidates.extend(f"{entities} {metric} statistics" for metric in metrics)
    candidates.append(f"{entities} {' '.join(metrics)} official statistics")
    return blueprint.model_copy(update={"search_queries": _normalize_research_queries(candidates)})


def _trend_query_seeds(blueprint: _StudioResearchBlueprint) -> tuple[str, list[str]]:
    """从规划查询中挑出一条双方查询和每个对象各一条查询，避免机械截前三条漏掉一方。"""

    if (
        len(blueprint.entities) >= 2
        and len(blueprint.entity_search_names) == len(blueprint.entities)
    ):
        # 强类型检索名比从自由文本 search_queries 猜实体覆盖关系更可靠，也能稳定处理中文简称。
        search_names = blueprint.entity_search_names[:2]
        return " ".join(search_names), list(search_names)
    queries = [query for query in blueprint.search_queries if query.strip()]
    if len(blueprint.entities) < 2 or not queries:
        return "", []
    entity_keys = [re.sub(r"\s+", "", entity).casefold() for entity in blueprint.entities[:2]]

    def coverage(query: str) -> tuple[bool, bool]:
        key = re.sub(r"\s+", "", query).casefold()
        return tuple(entity_key in key for entity_key in entity_keys)  # type: ignore[return-value]

    pair_seed = next((query for query in queries if all(coverage(query))), queries[0])
    entity_seeds: list[str] = []
    for index in range(2):
        seed = next(
            (
                query
                for query in queries
                if coverage(query)[index] and not coverage(query)[1 - index]
            ),
            "",
        )
        if seed:
            entity_seeds.append(seed)
    # 模型若没有提供可识别别名的个体查询，才回退到客户原始实体；不能再次拿双方查询占位。
    while len(entity_seeds) < 2:
        entity = blueprint.entities[len(entity_seeds)]
        entity_seeds.append(entity)
    return pair_seed, entity_seeds[:2]


def _trend_metric_for(metric: str) -> str:
    """从横向总量指标派生检索用的逐期指标，不生成任何事实数值。"""

    normalized = _compact_text(metric, 100)
    replacements = (
        # 公开赛季表最稳定、可比的列通常是联赛出场/进球；“所有赛事总计”在不同站点口径
        # 差异很大，不能与联赛数据混成一条折线。
        ("职业生涯总进球数", "逐赛季联赛进球数"),
        ("进球数", "逐赛季联赛进球数"),
        ("进球", "逐赛季联赛进球数"),
        ("职业生涯助攻数", "逐赛季助攻数"),
        ("助攻数", "逐赛季助攻数"),
        ("助攻", "逐赛季助攻数"),
        ("职业生涯出场次数", "逐赛季出场次数"),
        ("出场次数", "逐赛季出场次数"),
        ("总人口", "年度总人口"),
        ("营业收入", "年度营业收入"),
        ("销量", "年度销量"),
        ("市场份额", "年度市场份额"),
        ("GDP（现价美元）", "年度 GDP（现价美元）"),
    )
    for source, target in replacements:
        if source.casefold() == normalized.casefold():
            return target
    if any(marker in normalized.casefold() for marker in ("逐年", "年度", "逐季", "赛季", "trend", "yearly")):
        return normalized
    return _compact_text(f"{normalized}逐年或逐期值", 120)


def _trend_search_columns(metric: str) -> str:
    """为趋势检索补充结构化表头词，帮助搜索服务返回能解释列含义的片段。"""

    key = metric.casefold()
    if any(marker in key for marker in ("进球", "goal")):
        return "season appearances goals"
    if any(marker in key for marker in ("助攻", "assist")):
        return "season appearances assists"
    if any(marker in key for marker in ("出场", "appearance", "match")):
        return "season appearances matches"
    if any(marker in key for marker in ("收入", "营收", "revenue", "sales")):
        return "year period value revenue"
    return "year period value data"


def _data_point_budget(
    blueprint: _StudioResearchBlueprint,
    requested_visuals: list[str],
) -> int:
    """按视图给抽取器留足数据点，避免旧的两点估算截断趋势序列。"""

    entity_total = max(1, len(blueprint.entities))
    metric_total = max(1, min(6, len(blueprint.metrics)))
    budget = max(blueprint.required_data_points, entity_total * metric_total)
    if any(visual in {"trend_line", "trend_area"} for visual in requested_visuals):
        # 趋势指标与横向总量指标是两组数据：每个总量指标各需一组对象值，趋势另需至少三期。
        budget = max(budget, entity_total * metric_total + entity_total * 3)
    return min(36, budget)


def _data_visual_slide_ids(
    slides: list[PresentationStudioSlidePlan],
    *,
    target_slide_id: str,
    count: int,
) -> list[str]:
    """从正文页中为数据视图分配稳定页面，避免多个视图互相覆盖。"""

    content_ids = [slide.slide_id for slide in slides if slide.role == "content"]
    if not content_ids:
        return [target_slide_id]
    start = content_ids.index(target_slide_id) if target_slide_id in content_ids else 0
    ordered = content_ids[start:] + content_ids[:start]
    return ordered[: max(1, min(count, len(ordered)))]


_EXPLICIT_TIME_SCOPE_PATTERN = re.compile(
    r"(?:19|20)\d{2}|\d{1,2}\s*(?:年|月|日|季度|q[1-4])|(?:上|下|本|今|去|前|后)\s*(?:年|月|季度|赛季)|"
    r"(?:截至|截止|近|过去|最近)\s*\d+\s*(?:年|月|天|季|届|轮)|\d{4}\s*[-/]\s*\d{2}",
    flags=re.IGNORECASE,
)
_UNBOUNDED_TIME_SCOPE = "同一来源中明确的统计期间；若动态页面未说明截止日期，仅允许同一页面读取快照"
_UNBOUNDED_COMPARISON_SCOPE = "同一来源、同一统计范围、同一单位和同一明确期间；无明确期间时仅允许同页读取快照"


def _align_research_time_scope(
    request: PresentationStudioPlanRequest,
    blueprint: _StudioResearchBlueprint,
) -> _StudioResearchBlueprint:
    """阻止研究计划凭空增加年份，并为未限定主题提供受控快照口径。

    客户提出“截至 2023 年”这类条件时，模型给出的时间范围仍会保留；没有时间条件时，不允许
    计划阶段把“截至 2023 年底”一类模型臆测变成导出阶段的硬门槛。后续 Verifier 仍会验证原文
    明确期间，或只在严格同页条件下把动态数据标为读取快照。
    """

    if _EXPLICIT_TIME_SCOPE_PATTERN.search(request.intent):
        return blueprint
    return blueprint.model_copy(
        update={
            # 研究问题也属于模型可见约束。不能只覆盖单独的 time_scope，却保留“截止日期”
            # 这种模型擅自补出的措辞，否则抽取器仍会把同页动态统计误判为不满足计划。
            "research_question": (
                f"比较{'与'.join(blueprint.entities)}的{'、'.join(blueprint.metrics)}，"
                "使用同一公开来源和单位；如页面未披露统计期间，仅允许同页读取快照。"
            ),
            "time_scope": _UNBOUNDED_TIME_SCOPE,
            "comparison_scope": _UNBOUNDED_COMPARISON_SCOPE,
        }
    )


def _ensure_pair_comparison_query(blueprint: _StudioResearchBlueprint) -> _StudioResearchBlueprint:
    """为双对象单指标研究保留一条最小的同页比较检索语句。"""

    if len(blueprint.entities) != 2 or blueprint.chart_type not in {
        "comparison_table", "comparison_bar", "grouped_bar", "horizontal_bar", "trend_line",
        "trend_area", "share_pie", "share_doughnut"
    }:
        return blueprint
    search_names = (
        blueprint.entity_search_names
        if len(blueprint.entity_search_names) == len(blueprint.entities)
        else blueprint.entities
    )
    exact_query = _compact_text(f"{search_names[0]} {search_names[1]} {' '.join(blueprint.metrics[:3])}", 140)
    if not exact_query:
        return blueprint
    existing = _normalize_research_queries(blueprint.search_queries)
    if exact_query.casefold() in {query.casefold() for query in existing}:
        return blueprint
    return blueprint.model_copy(update={"search_queries": [exact_query, *existing][:6]})


def _normalize_research_queries(values: list[str]) -> list[str]:
    """查询语句只允许短文本，不接受 URL、命令或换行拼接。"""

    queries: list[str] = []
    seen: set[str] = set()
    for value in values:
        query = _compact_text(value, 140)
        key = query.casefold()
        if (
            len(query) < 4
            or key in seen
            or "http://" in key
            or "https://" in key
            or any(character in query for character in ("\n", "\r", "\x00"))
        ):
            continue
        seen.add(key)
        queries.append(query)
        if len(queries) == 6:
            break
    return queries


def _research_target_slide(
    slides: list[PresentationStudioSlidePlan],
    target_slide_index: int,
) -> PresentationStudioSlidePlan | None:
    content_slides = [slide for slide in slides if slide.role == "content"]
    if not content_slides:
        return None
    # 模型使用从 1 开始的正文页序号；0 或越界时回到第一张正文页。
    index = target_slide_index - 1
    return content_slides[index] if 0 <= index < len(content_slides) else content_slides[0]


def _plan_id(
    *,
    task_id: str,
    brief: PresentationStudioBrief,
    slides: list[PresentationStudioSlidePlan],
    asset_plan: PresentationStudioAssetPlan,
    research_plan: PresentationStudioResearchPlan,
    data_plan: PresentationStudioDataPlan,
) -> str:
    payload = {
        "task_id": task_id,
        "brief": brief.model_dump(mode="json"),
        "slides": [slide.model_dump(mode="json") for slide in slides],
        "asset_plan": asset_plan.model_dump(mode="json"),
        "research_plan": research_plan.model_dump(mode="json"),
        "data_plan": data_plan.model_dump(mode="json"),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:48]


def _persist_plan(
    *,
    response: PresentationStudioPlanResponse,
    request: PresentationStudioPlanRequest,
    started_at: datetime,
    repair_used: bool,
    research_planner_call_count: int,
) -> WorkflowRun:
    """把计划而非模型原始文本写入统一历史，供后续确认导出复用。"""

    finished_at = datetime.now(UTC)
    summary = f"已生成“{response.brief.title}”的 PPT 创作简报与 {len(response.slides)} 页计划。"
    plan_payload = response.model_dump(mode="json", exclude={"workflow_run"})
    steps = [
        WorkflowStepRun(
            step_id=_PRESENTATION_STUDIO_STEP_ID,
            agent=_PRESENTATION_STUDIO_AGENT_ID,
            action="plan_presentation_studio",
            status="completed",
            message=summary,
            output={
                "runtime": True,
                "presentation_studio_plan": plan_payload,
                "intent": _compact_text(request.intent, 500),
                "output_format_repair_count": 1 if repair_used else 0,
                "research_planner_call_count": research_planner_call_count,
                "external_assets_fetched": False,
                "public_research_fetched": False,
                "structured_data_fetched": False,
            },
        )
    ]
    run = WorkflowRun(
        task_id=response.task_id,
        mode="runtime",
        status="completed",
        summary=summary,
        max_risk_level="low",
        requires_confirmation=False,
        steps=steps,
        limits=RuntimeExecutionLimits(
            max_steps=1,
            max_tool_calls=0,
            max_retries_per_tool=0,
            tool_timeout_ms=0,
            task_timeout_ms=90_000,
            token_budget=3_000,
        ),
        metrics=RuntimeExecutionMetrics(
            started_at=started_at.isoformat(timespec="seconds"),
            finished_at=finished_at.isoformat(timespec="seconds"),
            duration_ms=max(0, int((finished_at - started_at).total_seconds() * 1000)),
            step_total=1,
            step_completed=1,
            estimated_input_tokens=max(1, len(request.intent) // 4),
            estimated_output_tokens=max(1, len(json.dumps(plan_payload, ensure_ascii=False)) // 4),
            validation_error_total=1 if repair_used else 0,
        ),
    )
    events = [
        TaskLogEvent(
            task_id=response.task_id,
            sequence=1,
            event="presentation_studio_started",
            agent_id=_PRESENTATION_STUDIO_AGENT_ID,
            step_id=_PRESENTATION_STUDIO_STEP_ID,
            message="PPT 创作计划已受理，正在从用户意图整理简报。",
        ),
        TaskLogEvent(
            task_id=response.task_id,
            sequence=2,
            event="presentation_studio_plan_completed",
            agent_id=_PRESENTATION_STUDIO_AGENT_ID,
            step_id=_PRESENTATION_STUDIO_STEP_ID,
            level="warning" if response.warnings else "info",
            message=summary,
        ),
    ]
    save_workflow_run(run=run, events=events, plan=None, tool_calls=[], artifacts=[])
    return run


def _requested_slide_count(value: int) -> int:
    if value == 0:
        return _DEFAULT_SLIDE_COUNT
    return max(_MIN_SLIDE_COUNT, min(_MAX_SLIDE_COUNT, value))


def _requested_data_visual_count_hint(request: PresentationStudioPlanRequest) -> int:
    """在调用模型前为硬数量合同或简短数据主题预留正文页。"""

    if not request.structured_data_enabled:
        return 0
    explicit_total = _data_visual_intent(request).total
    if explicit_total:
        return min(8, explicit_total)
    if _has_explicit_data_research_intent(request.intent):
        return 4 if _wants_rich_data_story(request.intent) else 3
    return 0


def _studio_system_prompt(requested_slide_count: int) -> str:
    content_count = requested_slide_count - 4
    asset_count = min(6, content_count + 1)
    return (
        "你是 AgentFlow 的 PPT 创作规划器。只输出一个 JSON object，不要 markdown、解释或代码围栏。"
        "你不联网、不引用外部资料，不得杜撰数字、客户名称、案例、研究结论或来源。"
        "用户可能只提供一句主题；应把未知事实写成待核验的表达，而不是编造。"
        f"必须输出 title、purpose、audience、core_message、theme、theme_reason、content_slides、asset_queries。"
        "theme 只能是 executive_blue、technology_emerald、narrative_warm、impact_contrast 之一。"
        f"content_slides 必须恰好 {content_count} 项；每项含 title、2 到 5 条 bullets、layout、visual_direction。"
        "layout 只能是 insight_cards、comparison、process、timeline、metrics、quote、image_statement 之一；"
        "按内容叙事选择，流程不用伪装成对比，缺少真实数值时不得选 metrics 来制造虚假图表。"
        "每张正文页必须回答一个不同的问题，形成从背景、判断框架、关键展开到行动收束的递进；"
        "不要把同一批名词拆成连续清单页。bullets 必须短、可放入单页，至少包含一条解释其意义、影响或选择依据的表达，"
        "不要重复泛泛的背景和价值表述。"
        f"asset_queries 必须恰好 {asset_count} 条，顺序依次服务封面和每一张正文页；每条使用 3 到 8 个英文单词，"
        "必须描述该页实际主题、人物、场景或概念，禁止使用 technology team、modern workspace 一类与主题无关的通用词。"
        "这些视觉查询词只用于未来 Pexels 检索或 Seedream 生成，不代表已经联网或生成图片。"
    )


def _studio_repair_system_prompt(requested_slide_count: int) -> str:
    return (
        "你正在修复一份 PPT 创作计划的 JSON 格式。只输出合法 JSON object，不输出解释。"
        "不要增加外部事实、数据、引用或素材来源；保留原创作意图。"
        f"正文页 content_slides 必须恰好 {requested_slide_count - 4} 项，每项 2 到 5 个短要点。"
        "theme 只能是 executive_blue、technology_emerald、narrative_warm、impact_contrast；"
        "layout 只能是 insight_cards、comparison、process、timeline、metrics、quote、image_statement；"
        "asset_queries 保持原有条数和顺序。"
    )


def _studio_user_message(request: PresentationStudioPlanRequest) -> str:
    preference = "由系统自动判断" if request.theme_preference == "auto" else request.theme_preference
    page_hint = "由系统自动判断" if request.target_slide_count == 0 else str(request.target_slide_count)
    explicit_entity_scope = _requested_entity_scope(request.intent)
    entity_scope_notice = (
        f"客户明确对象范围（不可增加、不可替换）：{json.dumps(explicit_entity_scope, ensure_ascii=False)}\n"
        if explicit_entity_scope
        else "客户明确对象范围：未从原句提取；不要自行补入具体人物、品牌或城市。\n"
    )
    return (
        f"客户意图：{request.intent.strip()}\n"
        f"{entity_scope_notice}"
        f"目标页数：{page_hint}\n"
        f"视觉偏好：{preference}\n"
        f"后续视觉素材策略：{_visual_asset_provider(request)}（计划阶段不调用外部 Provider）\n"
        f"公开资料参考：{'导出确认后读取固定 Wikimedia 接口' if request.public_research_enabled else '不请求'}\n"
        f"结构化数据：{'由后续专用研究规划器生成蓝图；确认导出后才允许由受控数据 Provider 或 ResearchGateway 联网执行' if request.structured_data_enabled else '不请求'}"
    )


def _build_fallback_output(
    *,
    request: PresentationStudioPlanRequest,
    requested_slide_count: int,
) -> _StudioModelOutput:
    """模型不可用时生成保守结构，不伪装为联网研究或完整事实稿。"""

    topic = _compact_text(request.intent, 70)
    content_count = requested_slide_count - 4
    titles = [
        "问题与背景",
        "目标与价值",
        "判断框架",
        "核心展开",
        "实施路径",
        "资源与风险",
        "预期成果",
        "行动建议",
    ]
    slides: list[_StudioContentSlide] = []
    for index in range(content_count):
        label = titles[index]
        slides.append(
            _StudioContentSlide(
                title=label,
                bullets=[
                    f"围绕“{topic}”梳理 {label} 的核心信息。",
                    f"说明 {label} 对整体判断或下一步选择的实际意义。",
                    "关键事实、数据和案例需要在导出前由客户补充或确认。",
                ],
                layout=_FALLBACK_LAYOUT_SEQUENCE[index % len(_FALLBACK_LAYOUT_SEQUENCE)],
                visual_direction="使用清晰的信息层级、少量强调色和可替换的图像占位区域。",
            )
        )
    preference = request.theme_preference
    theme = preference if preference in _THEMES else "executive_blue"
    return _StudioModelOutput(
        title=topic,
        purpose="把客户的一句主题整理为可讨论、可确认的演示结构。",
        audience="由客户在确认计划时补充；第一版按通用业务受众组织。",
        core_message="先确认要传达的核心价值，再补充经过核验的事实与素材。",
        theme=theme,
        theme_reason="当前采用清晰、稳健的内置视觉 token，便于后续替换为客户确认的风格。",
        content_slides=slides,
        # mock/fallback 无法可靠把任意中文主题翻译成图库语义，故只保留中性设计词，并明确降级事实。
        # 真实模型路径会为封面和正文逐页生成检索词；运行时不会把这组兜底词伪装成语义验证后的素材。
        asset_queries=[
            "professional presentation abstract cover",
            "collaboration planning discussion",
            "strategy roadmap visual concept",
            "business outcome presentation",
            "next step action concept",
        ][: min(6, content_count + 1)],
        research_blueprint=_StudioResearchBlueprint(),
    )


async def _emit_progress(
    callback: _ProgressCallback | None,
    event: str,
    message: str,
) -> None:
    if callback is not None:
        await callback(event, message)


def _compact_text(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) <= limit:
        return text
    return f"{text[: max(1, limit - 1)].rstrip()}…"
