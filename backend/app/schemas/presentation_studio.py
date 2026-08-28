"""PPT 制作 V2 的创作计划协议。

该协议承接“用户只说一句需求”的低摩擦入口。它和既有的项目方案 PPT V1 共用文档助手、
任务历史与受控导出目录，但不会把自由文本直接交给文件渲染器。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.presentation import PresentationSlidePlan
from app.schemas.workflow import WorkflowRun


PresentationStudioTheme = Literal[
    "executive_blue",
    "technology_emerald",
    "narrative_warm",
    "impact_contrast",
]
PresentationStudioThemePreference = Literal[
    "auto",
    "executive_blue",
    "technology_emerald",
    "narrative_warm",
    "impact_contrast",
]
PresentationStudioAssetState = Literal["not_requested", "planned"]
PresentationStudioResearchState = Literal["not_requested", "planned"]
# ``planned`` 保留给 2026-08-12 以前已持久化的 World Bank 计划，避免升级后旧计划无法恢复。
PresentationStudioDataState = Literal[
    "not_requested",
    "research_planned",
    "provider_planned",
    "planned",
]
PresentationStudioDataChartType = Literal[
    "none",
    "comparison_table",
    "trend_table",
    "comparison_bar",
    "grouped_bar",
    "horizontal_bar",
    "trend_line",
    "trend_area",
    "share_pie",
    "share_doughnut",
]
PresentationStudioMode = Literal["llm", "mock", "fallback"]
PresentationStudioVisualAssetProvider = Literal["none", "pexels", "seedream"]
PresentationStudioSlideLayout = Literal[
    "cover",
    "agenda",
    "insight_cards",
    "comparison",
    "process",
    "timeline",
    "metrics",
    "quote",
    "image_statement",
    "summary",
    "sources",
]


class PresentationStudioPlanRequest(BaseModel):
    """用户用一句意图发起的 PPT 创作计划请求。

    ``target_slide_count`` 和视觉偏好都是可选项。正常客户只需要描述要做什么；系统会根据
    受众、目的和信息密度给出第一版判断，客户在计划确认前仍可调整。
    """

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(min_length=4, max_length=2_000)
    target_slide_count: int = Field(default=0, ge=0, le=12)
    theme_preference: PresentationStudioThemePreference = "auto"
    # 新入口明确表达视觉素材类型；计划阶段绝不联网、下载或生成图片。
    visual_asset_provider: PresentationStudioVisualAssetProvider = "none"
    # 公开资料只作为来源页的补充参考。计划阶段不联网，导出时仍需再次确认。
    public_research_enabled: bool = False
    # 结构化数据和公开资料共用客户端的“智能补充”选择。计划阶段只做受控意图识别，
    # 不访问任何数据接口；导出时仍需明确确认联网。
    structured_data_enabled: bool = False
    # V2.1 兼容字段。旧客户端只会提交这个开关，服务层会把 true 解释为 Pexels。
    allow_licensed_assets: bool = False


class PresentationStudioBrief(BaseModel):
    """模型生成、用户确认前可阅读的创作简报。"""

    title: str = Field(min_length=1, max_length=160)
    purpose: str = Field(min_length=1, max_length=360)
    audience: str = Field(min_length=1, max_length=240)
    core_message: str = Field(min_length=1, max_length=420)
    theme: PresentationStudioTheme
    theme_reason: str = Field(min_length=1, max_length=420)
    fact_check_notice: str = Field(min_length=1, max_length=500)


class PresentationStudioSlidePlan(PresentationSlidePlan):
    """创作型 PPT 的单页计划，增加受控版式和视觉意图但不接收富文本或外部路径。

    ``layout`` 是交付层的版式语法，不是模型可自由拼接的模板路径。这样同一主题在不同页面
    可以有对比、流程、时间线或指标等清晰层级，同时仍由本地渲染器保证可编辑与可验证。
    """

    layout: PresentationStudioSlideLayout = "insight_cards"
    visual_direction: str = Field(default="", max_length=360)


class PresentationStudioAssetSlot(BaseModel):
    """一张外部图片预定服务的页面和语义，避免所有图库结果随机贴到正文页。"""

    slide_id: str = Field(min_length=1, max_length=80)
    slide_title: str = Field(min_length=1, max_length=160)
    query: str = Field(min_length=3, max_length=120)
    purpose: str = Field(min_length=1, max_length=240)


class PresentationStudioAssetPlan(BaseModel):
    """外部视觉素材的待执行计划，避免“已有查询词”被误导为“已实际取得图片”。"""

    state: PresentationStudioAssetState = "not_requested"
    provider: str = ""
    # 查询顺序与 slots 对齐；Pexels 最多六张，即梦在运行时会再收紧到四张，控制成本与等待时间。
    queries: list[str] = Field(default_factory=list, max_length=6)
    slots: list[PresentationStudioAssetSlot] = Field(default_factory=list, max_length=6)
    notice: str = Field(min_length=1, max_length=500)


class PresentationStudioResearchPlan(BaseModel):
    """导出阶段可选的公开资料参考计划。

    它刻意不包含任何已抓取正文、统计数据或模型结论：计划阶段没有联网，且第一版资料仅用于
    来源页和任务审计，不会静默写进客户内容或伪装成已经核验的事实。
    """

    state: PresentationStudioResearchState = "not_requested"
    provider: str = ""
    max_sources: int = Field(default=0, ge=0, le=3)
    notice: str = Field(min_length=1, max_length=500)


class PresentationStudioDataPlan(BaseModel):
    """导出阶段可选的数据图表计划。

    普通创作默认由已配置的模型按已确认的对象、指标和图表合同直接生成数据底稿；这条路径
    不读取网页，也不把来源核验当作交付前置条件。固定 Provider 和 ResearchGateway 仍保留
    给客户以后明确选择的联网核验场景，不能反过来阻断常规 PPT 创作。
    """

    state: PresentationStudioDataState = "not_requested"
    provider: str = ""
    chart_type: PresentationStudioDataChartType = "none"
    slide_id: str = ""
    # 一次研究可以复用同一批证据交付多个视图。首项与 chart_type/slide_id 保持兼容，
    # 旧客户端仍能读取主视图，新交付层则按两个数组的一一对应关系渲染数据章节。
    requested_visuals: list[PresentationStudioDataChartType] = Field(default_factory=list, max_length=8)
    visual_slide_ids: list[str] = Field(default_factory=list, max_length=8)
    # 与 requested_visuals 同序保存每个视图使用的指标。它让多个表格各自承载不同信息，
    # 避免“用户要三张表，渲染器却把同一张总览表复制三遍”。旧计划没有该字段时由
    # ResearchGateway 回退到 metrics/trend_metric。
    visual_metrics: list[list[str]] = Field(default_factory=list, max_length=8)
    # ``ai_direct`` 是普通创作默认值：模型直接生成可编辑数据底稿，不触发网页读取。旧快照
    # 仍可保持 verified_* 值，以兼容已经落库的研究型计划。
    evidence_mode: Literal["ai_direct", "verified_only", "verified_or_ai_draft"] = "ai_direct"
    # 数量合同由 Harness 从客户原话确定，不交给模型自行缩减。explicit=true 时，交付摘要
    # 必须明确说明未满足项，不能只因 PPTX 文件可打开就报告完整成功。
    required_table_count: int = Field(default=0, ge=0, le=6)
    required_bar_chart_count: int = Field(default=0, ge=0, le=2)
    required_line_chart_count: int = Field(default=0, ge=0, le=2)
    # “4 张图表”这类总数量合同不能被分类计数遗漏；导出后按实际原生对象总数复核。
    required_visual_count: int = Field(default=0, ge=0, le=8)
    visual_contract_explicit: bool = False
    indicator_code: str = ""
    indicator_name: str = ""
    country_codes: list[str] = Field(default_factory=list, max_length=4)
    country_names: list[str] = Field(default_factory=list, max_length=4)
    # 单图时代上限为 8；多指标表 + 双对象三期趋势最低需要 10 点，统一与研究契约上限对齐。
    max_points: int = Field(default=0, ge=0, le=36)
    research_question: str = Field(default="", max_length=500)
    entities: list[str] = Field(default_factory=list, max_length=6)
    # entities 用于客户界面；检索名可补充正式英文名或国际通用拼写，并与 entities 同序。
    # 两者分离后，中文简称不会降低英文公开资料的召回率，旧计划缺省时仍回退显示名。
    entity_search_names: list[str] = Field(default_factory=list, max_length=6)
    metrics: list[str] = Field(default_factory=list, max_length=6)
    # 折线图需要逐年/逐季序列，不能把“职业生涯总量”等横向指标重复当作趋势指标。
    # 旧计划没有该字段时保持空白，ResearchGateway 会从首项指标做兼容推导。
    trend_metric: str = Field(default="", max_length=120)
    time_scope: str = Field(default="", max_length=200)
    comparison_scope: str = Field(default="", max_length=300)
    required_data_points: int = Field(default=0, ge=0, le=36)
    search_queries: list[str] = Field(default_factory=list, max_length=6)
    preferred_source_types: list[str] = Field(default_factory=list, max_length=5)
    notice: str = Field(min_length=1, max_length=500)


class PresentationStudioPlanResponse(BaseModel):
    """已持久化的 PPT 创作计划快照。"""

    task_id: str = Field(min_length=1, max_length=120)
    plan_id: str = Field(min_length=16, max_length=96)
    mode: PresentationStudioMode
    brief: PresentationStudioBrief
    slides: list[PresentationStudioSlidePlan] = Field(min_length=5, max_length=12)
    asset_plan: PresentationStudioAssetPlan
    research_plan: PresentationStudioResearchPlan = Field(
        default_factory=lambda: PresentationStudioResearchPlan(
            notice="当前计划不补充公开资料来源；关键数字、案例和引用仍需人工核验。"
        )
    )
    data_plan: PresentationStudioDataPlan = Field(
        default_factory=lambda: PresentationStudioDataPlan(
            notice="当前主题未规划结构化数据图表；不会为了装饰而自动加入图表。"
        )
    )
    warnings: list[str] = Field(default_factory=list, max_length=8)
    workflow_run: WorkflowRun | None = None


class PresentationStudioTaskStartResponse(BaseModel):
    """异步受理回执；完整计划须在终态后读取。"""

    task_id: str = Field(min_length=1, max_length=120)
    status: Literal["queued"] = "queued"


class PresentationStudioTaskResultResponse(BaseModel):
    """查询异步 PPT 创作任务的结果，不暴露未验证的模型中间文本。"""

    task_id: str = Field(min_length=1, max_length=120)
    status: Literal["running", "completed", "failed"]
    result: PresentationStudioPlanResponse | None = None


class PresentationStudioExportRequest(BaseModel):
    """用户确认将当前创作计划渲染为新的可编辑 PPTX。"""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=16, max_length=96)
    filename: str = Field(default="", max_length=120)
    confirmed: bool = False
    # 旧客户端使用的兼容字段。新客户端应使用 fetch_external_assets。
    fetch_licensed_assets: bool = False
    # Pexels 检索或即梦生成都属于导出阶段才允许发生的外部副作用。
    fetch_external_assets: bool = False
    # 只在已规划公开资料、用户明确勾选并完成联网确认时读取固定 Wikimedia 接口。
    fetch_public_research: bool = False
    # 只在已规划结构化数据、用户明确勾选并完成联网确认时读取固定 World Bank 指标接口。
    fetch_structured_data: bool = False
    network_confirmed: bool = False
