"""数据工作台 D1 的稳定 API 契约。

这个阶段只描述受控导入与确定性数据画像。原始表格只在本机 API 与 Qt 预览之间短暂
传递，模型、普通任务日志和 manifest 都不会接收行级数据。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DataDatasetCreateRequest(BaseModel):
    """导入一个 Excel/CSV 的传输请求，不接受任意本机绝对路径。"""

    filename: str = Field(min_length=1, max_length=180)
    # 20MB 原始字节编码为 Base64 后约 26.7MB。协议层先设 28MB 上限，服务层仍会按
    # 解码后的真实字节数二次验证，避免客户端绕过文件大小边界。
    content_base64: str = Field(min_length=4, max_length=28_000_000)


class DataDatasetInfo(BaseModel):
    """受控数据集的轻量元数据，不包含源文件绝对路径或单元格内容。"""

    name: str
    relative_path: str
    size_bytes: int
    modified_at: str
    dataset_type: Literal["xlsx", "csv"]


class DataDatasetListResponse(BaseModel):
    total: int
    datasets: list[DataDatasetInfo] = Field(default_factory=list)


class DataHeaderCandidate(BaseModel):
    """Excel 的候选表头行；D1 仅推荐，客户后续可在 D2 显式调整。"""

    row_number: int = Field(ge=1)
    score: float = Field(ge=0)
    non_empty_cells: int = Field(ge=0)
    preview: list[str] = Field(default_factory=list)


class DataSheetInfo(BaseModel):
    name: str
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)
    recommended: bool = False
    header_candidates: list[DataHeaderCandidate] = Field(default_factory=list)


class DataColumnProfile(BaseModel):
    """一列的聚合画像，数值范围只供本地客户端扫描，不写入普通日志。"""

    index: int = Field(ge=1)
    name: str
    inferred_type: Literal["number", "date", "boolean", "text", "mixed"]
    non_null_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    unique_count: int = Field(ge=0)
    parse_issue_count: int = Field(ge=0)
    numeric_min: float | None = None
    numeric_max: float | None = None
    numeric_mean: float | None = None
    earliest: str | None = None
    latest: str | None = None


class DataQualitySummary(BaseModel):
    missing_cell_count: int = Field(ge=0)
    duplicate_row_count: int = Field(ge=0)
    empty_column_count: int = Field(ge=0)
    duplicate_header_count: int = Field(ge=0)
    parse_issue_column_count: int = Field(ge=0)


class DataDatasetProfileResponse(BaseModel):
    """D1 一次画像的完整响应，预览严格限行限列以保护内存和 Qt 主线程。"""

    dataset: DataDatasetInfo
    source_sha256: str
    selected_sheet: str
    header_row: int = Field(ge=1)
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)
    sheets: list[DataSheetInfo] = Field(default_factory=list)
    columns: list[DataColumnProfile] = Field(default_factory=list)
    preview_columns: list[str] = Field(default_factory=list)
    preview_rows: list[list[str]] = Field(default_factory=list)
    quality_summary: DataQualitySummary
    warnings: list[str] = Field(default_factory=list)


class DataRecommendationRequest(BaseModel):
    """D5.1 的下一步建议请求。

    客户只提交当前受控文件名与可选的一句目标。后端始终从本地画像重新取得字段信息，不信任
    客户提交的列名、类型或表达式，因此推荐器不能成为绕过 D2 白名单的计划入口。
    """

    dataset_name: str = Field(min_length=1, max_length=180)
    goal: str = Field(default="", max_length=1_200)


class DataRecommendation(BaseModel):
    """一张可直接转入 D2 的受控建议卡。"""

    recommendation_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    question: str = Field(min_length=1, max_length=160)
    route: Literal["quality", "comparison", "trend", "composition", "distribution", "transform_candidate"]
    source_columns: list[str] = Field(default_factory=list, max_length=2)
    aggregation: Literal["count", "sum", "mean"] | None = None
    chart_candidate: Literal["bar", "line", "pie", "doughnut"] | None = None
    rationale: str = Field(min_length=1, max_length=240)
    expected_output: str = Field(min_length=1, max_length=160)


class DataRecommendationResponse(BaseModel):
    """D5.1 推荐响应，仅含 L1 画像与可执行的下一步。"""

    dataset_name: str
    source_sha256: str = Field(min_length=64, max_length=64)
    recommendations: list[DataRecommendation] = Field(default_factory=list, max_length=4)
    guidance: str = Field(min_length=1, max_length=220)
    # 模型仅能在 L1 画像范围内重排已有候选和润色引导语；无法调用时稳定回退本地推荐。
    recommendation_mode: Literal["local_profile", "model_assisted", "local_fallback"] = "local_profile"
    warnings: list[str] = Field(default_factory=list, max_length=8)


class DataAnalysisPreviewRequest(BaseModel):
    """D2 分析预览请求。

    客户只提交受控文件名与自然语言目标；不接收 DataFrame 表达式、公式、SQL、路径或由
    客户拼出的执行计划。这样后端可以始终用白名单操作解释用户目标。
    """

    dataset_name: str = Field(min_length=1, max_length=180)
    goal: str = Field(default="", max_length=1_200)
    cleaning_policy: Literal["safe"] = "safe"
    max_chart_count: int = Field(default=4, ge=1, le=4)


class DataAnalysisOperation(BaseModel):
    """经过白名单校验的一个确定性计算动作。"""

    operation_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    operation_type: Literal["overview", "numeric_summary", "group_aggregate", "time_series", "numeric_series"]
    title: str = Field(min_length=1, max_length=120)
    # 数值概览可以一次对最多四个字段计算同一套基础统计；其它操作仍由 Validator 限制为一到两列。
    source_columns: list[str] = Field(default_factory=list, max_length=4)
    aggregation: Literal["count", "sum", "mean"] | None = None
    sort_direction: Literal["ascending", "descending"] = "descending"
    row_limit: int = Field(default=12, ge=2, le=50)
    chart_type: Literal["bar", "line", "pie", "doughnut"] | None = None
    rationale: str = Field(min_length=1, max_length=240)


class DataAnalysisPlan(BaseModel):
    """D2 的有限分析合同，不允许包含任意可执行表达式。"""

    dataset_name: str
    source_sha256: str = Field(min_length=64, max_length=64)
    goal: str
    planning_mode: Literal["deterministic_profile"] = "deterministic_profile"
    cleaning_policy: Literal["safe"] = "safe"
    operations: list[DataAnalysisOperation] = Field(min_length=1, max_length=6)
    warnings: list[str] = Field(default_factory=list, max_length=12)


class DataMetric(BaseModel):
    """由本地确定性计算得到的一个可复算指标。"""

    metric_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,95}$")
    name: str
    value: float
    unit: str
    aggregation: Literal["count", "sum", "mean", "median", "min", "max"]
    source_columns: list[str] = Field(default_factory=list, max_length=2)
    operation_id: str


class DataAnalysisTable(BaseModel):
    """有限聚合表预览，绝不作为原始整表回传通道。"""

    table_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,95}$")
    title: str
    columns: list[str] = Field(min_length=1, max_length=12)
    rows: list[list[str]] = Field(default_factory=list, max_length=50)
    # 数值概览可关联最多四个数值字段；图表合同仍只引用一列分类和一列数值。
    source_columns: list[str] = Field(default_factory=list, max_length=4)
    operation_id: str
    truncated: bool = False


class DataChartContract(BaseModel):
    """D3 渲染原生 Excel 图表时唯一可用的图表输入合同。"""

    chart_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,95}$")
    chart_type: Literal["bar", "line", "pie", "doughnut"]
    title: str
    table_id: str
    category_column: str
    value_column: str
    operation_id: str


class DataQualityFinding(BaseModel):
    finding_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,95}$")
    severity: Literal["info", "warning"]
    title: str
    impact: str
    affected_count: int = Field(ge=0)
    handling: str


class DataCleaningAction(BaseModel):
    action_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,95}$")
    title: str
    affected_count: int = Field(ge=0)
    detail: str


class DataAnalysisTraceStep(BaseModel):
    """D2 的脱敏步骤轨迹；D4 会把相同结构写入正式任务历史。"""

    stage: Literal["profile", "plan", "validate", "execute"]
    status: Literal["completed", "skipped"]
    detail: str


class DataAnalysisInsight(BaseModel):
    """数据结果页使用的、可追溯的短结论。

    LLM 只能解释已经由本地 Tool 计算出的聚合指标和图表合同；引用 ID 必须在服务端再次
    校验，避免一句看似流畅的总结脱离真实数据。
    """

    mode: Literal["model", "local"]
    headline: str = Field(min_length=4, max_length=80)
    conclusion: str = Field(min_length=12, max_length=640)
    highlights: list[str] = Field(min_length=1, max_length=3)
    # 结论不应停在“看见了什么”。这组建议只能要求客户继续核对、筛选或下钻当前数据，
    # 不得把相关性包装成因果关系，也不得擅自给出外部业务事实或自动经营决策。
    next_actions: list[str] = Field(default_factory=list, max_length=3)
    evidence_metric_ids: list[str] = Field(default_factory=list, max_length=8)
    evidence_table_ids: list[str] = Field(default_factory=list, max_length=4)
    evidence_chart_ids: list[str] = Field(default_factory=list, max_length=4)


class DataAnalysisPreviewResponse(BaseModel):
    """D2 只读分析预览，不含 artifact、绝对路径或原始整表。"""

    dataset_profile: DataDatasetProfileResponse
    analysis_plan: DataAnalysisPlan
    quality_findings: list[DataQualityFinding] = Field(default_factory=list)
    cleaning_actions: list[DataCleaningAction] = Field(default_factory=list)
    metrics: list[DataMetric] = Field(default_factory=list)
    analysis_tables: list[DataAnalysisTable] = Field(default_factory=list)
    charts: list[DataChartContract] = Field(default_factory=list)
    # 由 API 在本地计算完成后补入；低可用或未配置模型时同样会给出可回溯的本地结论。
    insight: DataAnalysisInsight | None = None
    warnings: list[str] = Field(default_factory=list)
    skipped_items: list[str] = Field(default_factory=list)
    trace: list[DataAnalysisTraceStep] = Field(default_factory=list)


class DataWorkbookExportRequest(BaseModel):
    """D3 正式工作簿导出请求。

    用户确认的是“基于当前受控数据新建一个 Excel”，而不是提交本机路径或覆盖目标。源哈希
    将阻止用户在预览之后悄悄换文件，确保写出的工作簿仍对应其已查看的那份数据。
    """

    dataset_name: str = Field(min_length=1, max_length=180)
    source_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[a-fA-F0-9]{64}$")
    goal: str = Field(default="", max_length=1_200)
    cleaning_policy: Literal["safe"] = "safe"
    max_chart_count: int = Field(default=4, ge=1, le=4)
    confirmed: Literal[True]


class DataWorkbookArtifact(BaseModel):
    """不暴露绝对路径的数据工作簿交付物引用。"""

    name: str
    uri: str
    size_bytes: int = Field(ge=1)
    created_at: str


class DataWorkbookVerification(BaseModel):
    """D3 重新打开 Excel 后得到的受控验证摘要。"""

    passed: bool
    sheet_names: list[str] = Field(default_factory=list)
    table_count: int = Field(ge=0)
    chart_count: int = Field(ge=0)
    metric_count: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)


class DataWorkbookExportResponse(BaseModel):
    """D3 成功导出后的最小交付回执；D4 才会登记到正式任务历史。"""

    artifact: DataWorkbookArtifact
    verification: DataWorkbookVerification
    warnings: list[str] = Field(default_factory=list)
    skipped_items: list[str] = Field(default_factory=list)


class DataWorkbookTaskStartResponse(BaseModel):
    """D4 异步导出任务的受理回执。

    Qt 收到这个响应后即可订阅既有任务事件流或刷新任务历史；实际 Excel 仍只会在后台的
    受控输出目录中创建，避免长文件写入占住 UI 请求。
    """

    task_id: str
    status: Literal["queued"] = "queued"


class DataWorkbookTaskResultResponse(BaseModel):
    """从任务历史恢复的数据工作簿导出终态。

    ``pending`` 与 ``running`` 只说明后台尚未结束；只有 ``completed`` 会包含通过回读
    验证的 artifact。失败或取消的任务都不会登记半成品文件。
    """

    task_id: str
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    summary: str
    message: str
    artifact: DataWorkbookArtifact | None = None
    verification: DataWorkbookVerification | None = None
    warnings: list[str] = Field(default_factory=list)
    skipped_items: list[str] = Field(default_factory=list)


class DataChartExportRequest(DataWorkbookExportRequest):
    """D5.2 图表 PNG 导出请求。

    它沿用 D3 的版本锁定和显式确认字段，但交付物是根据同一份已验证聚合表绘制的新 PNG，
    不会改写 Excel、CSV 或原始数据集。
    """


class DataChartArtifact(BaseModel):
    """一个经过像素回读验证的数据图表交付物。"""

    artifact_id: str
    chart_id: str
    chart_type: Literal["bar", "line", "pie", "doughnut"]
    title: str
    name: str
    uri: str
    size_bytes: int = Field(ge=1)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    created_at: str


class DataChartVerification(BaseModel):
    """D5.2 对写出 PNG 的独立回读结果。"""

    passed: bool
    chart_count: int = Field(ge=0)
    chart_ids: list[str] = Field(default_factory=list, max_length=4)
    image_sizes: list[str] = Field(default_factory=list, max_length=4)
    warnings: list[str] = Field(default_factory=list, max_length=12)


class DataChartExportResponse(BaseModel):
    """D5.2 本地渲染结果；只有通过像素验证的 PNG 才会出现在 artifacts 中。"""

    artifacts: list[DataChartArtifact] = Field(default_factory=list, min_length=1, max_length=4)
    verification: DataChartVerification
    warnings: list[str] = Field(default_factory=list, max_length=12)
    skipped_items: list[str] = Field(default_factory=list, max_length=12)


class DataChartTaskStartResponse(BaseModel):
    """图表看板后台任务的即时受理回执。"""

    task_id: str
    status: Literal["queued"] = "queued"


class DataChartTaskResultResponse(BaseModel):
    """从任务历史恢复的 D5.2 终态。"""

    task_id: str
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    summary: str
    message: str
    artifacts: list[DataChartArtifact] = Field(default_factory=list, max_length=4)
    verification: DataChartVerification | None = None
    warnings: list[str] = Field(default_factory=list)
    skipped_items: list[str] = Field(default_factory=list)


# D5.3 的加工合同刻意不接受表达式、公式、脚本或任意 DataFrame 参数。模型或 UI 只能在
# 这些有限动作中选择；每个动作都能在本地工具中以相同输入重复计算和验证。
DataTransformOperationType = Literal[
    "arithmetic",
    "date_part",
    "round_number",
    "rank",
    "share",
    "segment",
    "cumulative",
    "period_change",
    "period_rate",
    "text_trim",
]


class DataTransformOperationInput(BaseModel):
    """字段加工队列中的一项受限操作。

    队列只描述“对已有字段新增哪一列”，不接受公式、代码或原始数据。一次最多十二项，既允许
    用户一次处理一组相关字段，也让预览、回读和历史审计保持在可理解范围内。
    """

    operation_type: DataTransformOperationType
    primary_column: str | None = Field(default=None, max_length=180)
    secondary_column: str | None = Field(default=None, max_length=180)
    result_column: str | None = Field(default=None, max_length=180)
    date_part: Literal["year", "month", "quarter", "weekday"] = "month"
    arithmetic_operator: Literal["add", "subtract", "multiply", "divide"] = "multiply"
    round_digits: int = Field(default=2, ge=0, le=6)


class DataTransformPreviewRequest(BaseModel):
    """生成字段加工预览的请求。

    ``operation_type`` 和列名必须由引导式 UI 明确选择；``goal`` 仅保留给关联任务的展示与审计，
    不参与字段加工推断，更不会被当作公式、SQL 或代码执行。
    """

    dataset_name: str = Field(min_length=1, max_length=180)
    source_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[a-fA-F0-9]{64}$")
    goal: str = Field(default="", max_length=600)
    operation_type: DataTransformOperationType | None = None
    primary_column: str | None = Field(default=None, max_length=180)
    secondary_column: str | None = Field(default=None, max_length=180)
    result_column: str | None = Field(default=None, max_length=180)
    # 日期拆分的一个有限子类型；其它操作忽略该字段，避免不同操作共享隐式参数。
    date_part: Literal["year", "month", "quarter", "weekday"] = "month"
    arithmetic_operator: Literal["add", "subtract", "multiply", "divide"] = "multiply"
    round_digits: int = Field(default=2, ge=0, le=6)
    # 为空时兼容早期单字段请求；非空时按队列顺序在同一份副本中新增多个字段。
    operations: list[DataTransformOperationInput] = Field(default_factory=list, max_length=12)


class DataTransformPlan(BaseModel):
    """经 Validator 固定的单次字段变更计划。"""

    plan_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    dataset_name: str
    source_sha256: str = Field(min_length=64, max_length=64)
    operation_type: DataTransformOperationType
    primary_column: str
    secondary_column: str | None = None
    result_column: str = Field(min_length=1, max_length=180)
    parameters: dict[str, str | float | int | list[str | float | int]] = Field(default_factory=dict)
    scope_description: str = Field(min_length=1, max_length=240)
    rationale: str = Field(min_length=1, max_length=240)


class DataTransformFieldPreview(BaseModel):
    """前后样例只保留有限行，不能变成原始数据导出通道。"""

    row_number: int = Field(ge=1)
    source_values: list[str] = Field(default_factory=list, max_length=2)
    result_value: str


class DataTransformPreviewResponse(BaseModel):
    """确认写入前的受限字段变更预览。"""

    plan: DataTransformPlan
    plans: list[DataTransformPlan] = Field(default_factory=list, max_length=12)
    row_count: int = Field(ge=0)
    affected_count: int = Field(ge=0)
    empty_result_count: int = Field(ge=0)
    previews: list[DataTransformFieldPreview] = Field(default_factory=list, max_length=12)
    warnings: list[str] = Field(default_factory=list, max_length=12)


class DataTransformationExportRequest(BaseModel):
    """客户确认后才允许生成新的字段加工工作簿。"""

    dataset_name: str = Field(min_length=1, max_length=180)
    source_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[a-fA-F0-9]{64}$")
    goal: str = Field(default="", max_length=600)
    operation_type: DataTransformOperationType
    primary_column: str = Field(min_length=1, max_length=180)
    secondary_column: str | None = Field(default=None, max_length=180)
    result_column: str | None = Field(default=None, max_length=180)
    date_part: Literal["year", "month", "quarter", "weekday"] = "month"
    arithmetic_operator: Literal["add", "subtract", "multiply", "divide"] = "multiply"
    round_digits: int = Field(default=2, ge=0, le=6)
    operations: list[DataTransformOperationInput] = Field(default_factory=list, max_length=12)
    confirmed: Literal[True]


class DataTransformationArtifact(BaseModel):
    """字段加工副本的脱敏 artifact 描述。"""

    name: str
    uri: str
    size_bytes: int = Field(ge=1)
    created_at: str


class DataTransformationVerification(BaseModel):
    """重新打开工作簿后的确定性验证摘要。"""

    passed: bool
    sheet_names: list[str] = Field(default_factory=list, max_length=4)
    row_count: int = Field(ge=0)
    result_column: str = ""
    result_columns: list[str] = Field(default_factory=list, max_length=12)
    warnings: list[str] = Field(default_factory=list, max_length=12)


class DataTransformationExportResponse(BaseModel):
    artifact: DataTransformationArtifact
    plan: DataTransformPlan
    plans: list[DataTransformPlan] = Field(default_factory=list, max_length=12)
    affected_count: int = Field(ge=0)
    empty_result_count: int = Field(ge=0)
    verification: DataTransformationVerification
    warnings: list[str] = Field(default_factory=list, max_length=12)


class DataTransformationTaskStartResponse(BaseModel):
    task_id: str
    status: Literal["queued"] = "queued"


class DataTransformationTaskResultResponse(BaseModel):
    task_id: str
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    summary: str
    message: str
    artifact: DataTransformationArtifact | None = None
    plan: DataTransformPlan | None = None
    plans: list[DataTransformPlan] = Field(default_factory=list, max_length=12)
    verification: DataTransformationVerification | None = None
    affected_count: int = Field(default=0, ge=0)
    empty_result_count: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list, max_length=12)


# R5.4C 先把“多数据集合并”收口为一个可验证的连接合同。它不接受 SQL、pandas
# 表达式或任意脚本；Commander 只能传递已经从两份数据画像中确认过的连接键。
DataJoinType = Literal["left", "inner"]


class DataJoinOperationInput(BaseModel):
    """两份记录型数据的一次受控关联输入。"""

    left_dataset: str = Field(min_length=1, max_length=180)
    right_dataset: str = Field(min_length=1, max_length=180)
    left_key: str = Field(min_length=1, max_length=180)
    right_key: str = Field(min_length=1, max_length=180)
    join_type: DataJoinType = "left"
    # 首版拒绝重复键，避免把一对多扩张误当成普通合并结果。
    duplicate_policy: Literal["reject"] = "reject"


class DataJoinIntent(BaseModel):
    """由本地画像编译出的、可供预览和导出复用的多数据集意图。"""

    intent_version: Literal["agentflow.data_join_intent.v1"] = "agentflow.data_join_intent.v1"
    operation: DataJoinOperationInput
    source_hashes: dict[str, str] = Field(min_length=2, max_length=2)
    output_columns: list[str] = Field(default_factory=list, min_length=1, max_length=100)
    right_column_renames: dict[str, str] = Field(default_factory=dict, max_length=100)
    summary: str = Field(min_length=1, max_length=320)


class DataJoinPreviewRequest(BaseModel):
    """连接预览请求；正文和原始行不通过接口进入模型或计划。"""

    left_dataset: str = Field(min_length=1, max_length=180)
    right_dataset: str = Field(min_length=1, max_length=180)
    left_key: str = Field(min_length=1, max_length=180)
    right_key: str = Field(min_length=1, max_length=180)
    join_type: DataJoinType = "left"
    duplicate_policy: Literal["reject"] = "reject"
    source_hashes: dict[str, str] = Field(min_length=2, max_length=2)
    goal: str = Field(default="", max_length=1_200)


class DataJoinPlan(BaseModel):
    """回传给客户端的连接计划摘要，不包含绝对路径。"""

    intent_version: Literal["agentflow.data_join_intent.v1"] = "agentflow.data_join_intent.v1"
    left_dataset: str
    right_dataset: str
    left_key: str
    right_key: str
    join_type: DataJoinType
    duplicate_policy: Literal["reject"] = "reject"
    output_columns: list[str] = Field(default_factory=list, min_length=1, max_length=100)
    right_column_renames: dict[str, str] = Field(default_factory=dict, max_length=100)
    summary: str = Field(min_length=1, max_length=320)


class DataJoinPreviewResponse(BaseModel):
    """连接写入前的脱敏统计预览。"""

    plan: DataJoinPlan
    left_row_count: int = Field(ge=0)
    right_row_count: int = Field(ge=0)
    output_row_count: int = Field(ge=0)
    matched_row_count: int = Field(ge=0)
    left_only_row_count: int = Field(ge=0)
    right_only_row_count: int = Field(ge=0)
    duplicate_left_key_count: int = Field(ge=0)
    duplicate_right_key_count: int = Field(ge=0)
    preview_rows: list[list[str]] = Field(default_factory=list, max_length=8)
    warnings: list[str] = Field(default_factory=list, max_length=12)


class DataJoinExportRequest(DataJoinPreviewRequest):
    """客户确认后才允许生成新的合并副本。"""

    confirmed: Literal[True]


class DataJoinArtifact(BaseModel):
    """多数据集合并副本的脱敏 artifact 描述。"""

    name: str
    uri: str
    size_bytes: int = Field(ge=1)
    created_at: str


class DataJoinVerification(BaseModel):
    """重新读取合并副本后的确定性验证摘要。"""

    passed: bool
    dataset_type: Literal["xlsx", "csv"]
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)
    output_columns: list[str] = Field(default_factory=list, max_length=100)
    source_hashes_unchanged: bool
    warnings: list[str] = Field(default_factory=list, max_length=12)


class DataJoinExportResponse(BaseModel):
    """多数据集合并完成后的最小交付回执。"""

    artifact: DataJoinArtifact
    plan: DataJoinPlan
    verification: DataJoinVerification
    output_row_count: int = Field(ge=0)
    matched_row_count: int = Field(ge=0)
    left_only_row_count: int = Field(ge=0)
    right_only_row_count: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list, max_length=12)


class DataJoinTaskStartResponse(BaseModel):
    task_id: str
    status: Literal["queued"] = "queued"


class DataJoinTaskResultResponse(BaseModel):
    """从任务历史恢复的多数据集合并终态。"""

    task_id: str
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    summary: str
    message: str
    artifact: DataJoinArtifact | None = None
    plan: DataJoinPlan | None = None
    verification: DataJoinVerification | None = None
    output_row_count: int = Field(default=0, ge=0)
    matched_row_count: int = Field(default=0, ge=0)
    left_only_row_count: int = Field(default=0, ge=0)
    right_only_row_count: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list, max_length=12)
