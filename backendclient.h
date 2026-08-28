#ifndef BACKENDCLIENT_H
#define BACKENDCLIENT_H

#include <QList>
#include <QAbstractSocket>
#include <QByteArray>
#include <QNetworkAccessManager>
#include <QNetworkRequest>
#include <QObject>
#include <QJsonObject>
#include <QJsonArray>
#include <QString>
#include <QStringList>
#include <QUrl>
#include <QWebSocket>

struct AgentInfo
{
    QString id;
    QString name;
    QString description;
    QString category;
    QString version;
    bool enabled = false;
    bool builtin = false;
    QStringList capabilities;
};

// 与后端 workflow_plan.steps[] 对齐的轻量结构。
// Qt 端先只关心展示和日志串联，不在这里做真正 DAG 执行。
struct WorkflowStepInfo
{
    QString id;
    QString agent;
    QString action;
    QString title;
    QStringList dependsOn;
    QString parallelGroup;
    QJsonObject input;
    QString reason;
    QString expectedOutput;
    QStringList requiredPermissions;
    QString riskLevel = QStringLiteral("low");
    bool requiresConfirmation = false;
    QString toolName;
    QJsonObject commandPolicy;
    QStringList successCriteria;
    int timeoutMs = 0;
    QJsonObject retryPolicy;
    // action 准入来自后端；Qt 只解释和展示，绝不在前端自行放宽执行边界。
    QString executionMode = QStringLiteral("execute");
    QString admissionStatus = QStringLiteral("ready");
    QString admissionReason;
    QString verificationScope;
    QString recoveryHint;
};

struct WorkflowBudgetEstimateInfo
{
    int stepCount = 0;
    QString timeLevel;
    QString modelCostLevel;
    bool requiresNetwork = false;
    bool requiresCommand = false;
};

struct WorkflowWorkspaceScopeInfo
{
    QStringList readPaths;
    QStringList writePaths;
    QStringList externalServices;
    QString notes;
};

// Commander 在生成计划时固化的运行偏好快照。
// 它用于解释“本次计划按什么偏好生成”，不能替代 Runtime 的实时权限判断。
struct WorkflowPlanPreferencesInfo
{
    QString permissionPolicy = QStringLiteral("smart_confirm");
    QString personality = QStringLiteral("professional");
    QString costMode = QStringLiteral("balanced");
    QString executionStyle = QStringLiteral("plan_then_confirm");
    QString detailLevel = QStringLiteral("standard");
    bool memoryEnabled = false;
};

// workflow_plan 的计划级摘要。Qt 调度台只展示这些高价值字段，
// 真实执行和权限判断仍以后端 Runtime / Governance 为准。
struct WorkflowPlanSummaryInfo
{
    QString schemaVersion;
    QString planId;
    int planVersion = 0;
    // 版本关系与修订原因来自后端不可变快照，Qt 仅展示，绝不在本地拼接步骤或权限。
    QString parentPlanId;
    QString userGoal;
    QString changeSummary;
    QString intent;
    QString summary;
    QString nextAction;
    // 组合计划先可审阅，只有具备对应 Runtime 时才允许转入真实执行。
    QString executionReadiness = QStringLiteral("ready");
    QString projectScope = QStringLiteral("global");
    // 本轮 `@` 路由偏好来自后端规范化计划，而非 Qt 对输入文本的猜测。
    QStringList agentHints;
    QStringList clarifyingQuestions;
    QStringList definitionOfDone;
    WorkflowPlanPreferencesInfo preferences;
    WorkflowBudgetEstimateInfo budgetEstimate;
    WorkflowWorkspaceScopeInfo workspaceScope;
};

// /api/chat 的标准返回结果。后续接真实 LLM 或 Workflow Engine 时，
// 尽量保持这个结构稳定，避免反复改 MainWindow 的 UI 绑定逻辑。
struct ChatResult
{
    QString taskId;
    QString agentId;
    // 后端在首次发送时创建、后续回传同一会话 ID；Qt 不保存会话正文，只用它让服务端
    // 取回受控摘要和近轮上下文。
    QString conversationId;
    QString mode;
    QString model;
    QString reply;
    WorkflowPlanSummaryInfo planSummary;
    QList<WorkflowStepInfo> steps;
};

// 调度会话恢复只传回已脱敏的短摘要和有限消息；不把材料正文、绝对路径或任务内部日志
// 复制到桌面端。该结构专门服务于“重启后客户仍看得见当前会话”的体验。
struct ConversationTranscriptMessage
{
    QString role;
    QString content;
};

struct ConversationContextInfo
{
    QString conversationId;
    QString projectScope;
    QString title;
    QString summary;
    QList<ConversationTranscriptMessage> recentMessages;
};

// 会话列表只返回客户可见的摘要元数据；正文始终走独立的按页读取接口，避免一次弹出菜单
// 就把全部聊天记录传到桌面端。
struct ConversationSessionInfo
{
    QString conversationId;
    QString projectScope;
    QString title;
    QString summary;
    int archivedMessageCount = 0;
    QString updatedAt;
};

struct ConversationSessionListResult
{
    QString projectScope;
    QList<ConversationSessionInfo> conversations;
};

struct ConversationTranscriptPageResult
{
    ConversationSessionInfo session;
    int offset = 0;
    int limit = 0;
    int total = 0;
    QList<ConversationTranscriptMessage> messages;
};

// 历史页按需读取的 Commander 计划详情。
// 列表页不携带 plan，避免历史任务多时响应变重。
struct WorkflowPlanDetailResult
{
    QString taskId;
    WorkflowPlanSummaryInfo planSummary;
    QList<WorkflowStepInfo> steps;
};

// 计划版本列表保持轻量，用户选中版本后再按需读取完整步骤，避免版本多时拖慢调度台。
struct WorkflowPlanVersionInfo
{
    QString taskId;
    QString planId;
    int planVersion = 0;
    QString parentPlanId;
    QString userGoal;
    QString changeSummary;
    QString createdAt;
    bool current = false;
};

struct WorkflowPlanVersionListResult
{
    QString taskId;
    QString currentPlanId;
    QList<WorkflowPlanVersionInfo> versions;
};

// 修订接口返回新的干跑计划。执行状态仍由现有 Runtime 接口和日志流维护。
struct WorkflowPlanRevisionResult
{
    QString taskId;
    QString message;
    QString workflowRunStatus;
    WorkflowPlanSummaryInfo planSummary;
    QList<WorkflowStepInfo> steps;
};

// 受控 workspace 文档。Qt 只保存后端确认过的相对文件名，不保留用户的绝对源路径。
struct WorkspaceDocumentInfo
{
    QString name;
    QString relativePath;
    int sizeBytes = 0;
    QString modifiedAt;
    QString documentType = QStringLiteral("text");
    QString preview;
};

// 文档助手页需要的 workspace 文件列表。导入后显式刷新，避免在高频 UI 路径反复扫描磁盘。
struct WorkspaceDocumentListResult
{
    int total = 0;
    QList<WorkspaceDocumentInfo> documents;
};

// 知识库 K1 只向 Qt 暴露脱敏元数据和真实状态；私有副本路径、原文、分块与向量始终留在后端。
struct KnowledgeBaseInfo
{
    QString knowledgeBaseId;
    QString name;
    QString description;
    QString status;
    int activeIndexGeneration = 0;
    int activeDocumentVersionCount = 0;
    QString updatedAt;
};

struct KnowledgeBaseListResult
{
    QList<KnowledgeBaseInfo> knowledgeBases;
};

struct KnowledgeDocumentInfo
{
    QString documentId;
    QString knowledgeBaseId;
    QString displayName;
    QString documentType;
    QString activeVersionId;
    QString activeVersionStatus;
    int activeOcrPageCount = 0;
    int activeOcrCompletedPageCount = 0;
    int activeOcrFailedPageCount = 0;
    int activeOcrRetriedPageCount = 0;
    QString activeFailureSummary;
    QString updatedAt;
};

struct KnowledgeDocumentListResult
{
    QString knowledgeBaseId;
    QList<KnowledgeDocumentInfo> documents;
};

struct KnowledgeIndexJobInfo
{
    QString indexJobId;
    QString knowledgeBaseId;
    QString status;
    QString stage;
    int totalDocumentCount = 0;
    int parsedDocumentCount = 0;
    int indexedDocumentCount = 0;
    int failedDocumentCount = 0;
    QStringList failureSummaries;
};

struct KnowledgeVectorCapabilityInfo
{
    bool chromaAvailable = false;
    bool fastembedAvailable = false;
    bool modelInitialized = false;
    QString message;
};

// K7.4 OCR 与语义模型一样只向 Qt 提供脱敏能力状态。前端不接触缓存目录、模型文件、
// 下载链接或客户材料；准备动作必须由用户确认后经后端受控入口执行。
struct KnowledgeOcrCapabilityInfo
{
    bool paddleocrAvailable = false;
    bool modelInitialized = false;
    QString profile;
    QString message;
};

struct KnowledgeOcrPreparationInfo
{
    QString preparationId;
    QString status;
    QString modelProfile;
    QString message;
    QString startedAt;
    QString completedAt;
};

// K3 问答的受理与终态保持轻量：正文、来源和检索诊断保留在 result JSON，供独立阅读窗口
// 按需渲染；BackendClient 不复制父块、绝对路径或模型上下文到长期 C++ 状态。
struct KnowledgeAnswerTaskStartResult
{
    QString taskId;
    QString status;
};

struct KnowledgeAnswerTaskResult
{
    QString taskId;
    QString status;
    QString summary;
    QString message;
    QJsonObject result;
};

// K4 深度任务的范围、Map/Reduce 结果仍以受控 JSON 形式保留，避免客户端为了展示而复制父块正文、
// 文件绝对路径或模型上下文。独立工作台只读取其面向客户的摘要、覆盖和导出资格字段。
struct KnowledgeDeepTaskStartResult
{
    QString taskId;
    QString status;
};

struct KnowledgeDeepTaskResult
{
    QString taskId;
    QString status;
    QString summary;
    QJsonObject scope;
    QJsonObject result;
    QJsonObject coverage;
    QJsonObject reportReadiness;
};

struct KnowledgeDeepTaskControlResult
{
    QString taskId;
    QString action;
    bool accepted = false;
    QString status;
    QString message;
};

struct KnowledgeDeepTaskReportExportResult
{
    QString taskId;
    QString artifactId;
    QString filename;
    QString relativePath;
    QString artifactUri;
    int characterCount = 0;
    QString message;
};

// 数据工作台使用独立的受控数据区。这里只保存后端确认的相对名称和轻量元数据，绝不保存
// 客户端的源文件绝对路径或表格单元格内容。
struct DataDatasetInfo
{
    QString name;
    QString relativePath;
    int sizeBytes = 0;
    QString modifiedAt;
    QString datasetType;
};

struct DataDatasetListResult
{
    int total = 0;
    QList<DataDatasetInfo> datasets;
};

// 文档助手运行结果。documentContext 保留后端稳定 JSON 契约，Qt 只挑选面向客户的字段渲染；
// 后续增加新分类时无需为了展示协议频繁改动 C++ 结构体。
struct DocumentAgentRunResult
{
    QString taskId;
    QString mode;
    QString status;
    QString stopReason;
    QString reply;
    QJsonObject documentContext;
};

// 异步文档任务的受理回执。最终内容不放在这里，避免页面在结果校验前展示模型中间文本。
struct DocumentAgentTaskStartResult
{
    QString taskId;
    QString status;
};

// PDF 整理是文档助手下的确定性 Tool；受理回执和模型分析任务分开，避免 UI 把文件处理
// 误显示为正在生成模型文本。
struct PdfProcessingTaskStartResult
{
    QString taskId;
    QString status;
};

// 用户确认保存 Markdown 草稿后的回执。相对路径用于产品提示，绝对路径只保留在后端产物元数据。
struct DocumentDraftSaveResult
{
    QString taskId;
    QString artifactId;
    QString filename;
    QString relativePath;
    QString artifactUri;
    QString message;
};

// 项目方案 PPT 的预览只包含后端确认过的计划快照与自动交付预检。Qt 不参与章节抽取、材料
// 审查或本机路径解析，因此“查看计划”和“确认导出”可以共享同一个 planId 做过期保护。
struct PresentationPreviewResult
{
    QString sourceTaskId;
    QString sourceVersionId;
    QString presentationType;
    QString planId;
    QString title;
    QJsonArray slides;
    QJsonObject preflight;
    QStringList warnings;
};

struct PresentationExportResult
{
    QString taskId;
    QString artifactId;
    QString filename;
    QString relativePath;
    QString artifactUri;
    int slideCount = 0;
    QJsonObject verification;
    QString message;
};

// PPT 制作 V2 的计划只携带后端已校验的简报、页面和素材状态；Qt 不从中解析模型原始文本，
// 也不接触输出路径或素材凭据，避免创作工作台跨越 Runtime 的安全边界。
struct PresentationStudioPlanResult
{
    QString taskId;
    QString planId;
    QString mode;
    QJsonObject brief;
    QJsonArray slides;
    QJsonObject assetPlan;
    QJsonObject researchPlan;
    QJsonObject dataPlan;
    QStringList warnings;
};

struct PresentationStudioTaskStartResult
{
    QString taskId;
    QString status;
};

// 项目文档审查返回完整的结构化报告。报告内的 finding/check 继续保留原始 JSON，避免每增加
// 一条质量规则都迫使 Qt 修改多层 C++ DTO；展示层只读取已经由后端校验过的字段。
struct ProjectReviewResult
{
    QString taskId;
    QString status;
    QJsonObject report;
};

// 论文审查沿用相同的“任务 ID + 已校验报告”形状，但规则与声明边界独立，不能把项目审查
// 的分类误用于论文结果。
struct PaperReviewResult
{
    QString taskId;
    QString status;
    QJsonObject report;
};

// 任务历史列表的一行摘要，对应 GET /api/tasks 的 tasks[] 元素。
struct TaskHistoryItem
{
    QString taskId;
    QString mode;
    QString status;
    QString summary;
    QString maxRiskLevel;
    bool requiresConfirmation = false;
    int stepCount = 0;
    QString createdAt;
    QString updatedAt;
};

struct TaskHistoryResult
{
    int total = 0;
    int limit = 0;
    int offset = 0;
    QList<TaskHistoryItem> tasks;
};

// /api/tasks/{task_id}/steps 返回的单步执行结果。
// 这里不做真实执行，只保留后端已经落库的 step 级状态、消息和输出摘要。
struct WorkflowStepRunInfo
{
    QString stepId;
    QString agent;
    QString action;
    QString status;
    QString message;
    bool requiresConfirmation = false;
    QString riskLevel;
    QJsonObject output;
};

// 任务步骤列表的轻量响应。
struct TaskStepListResult
{
    QString taskId;
    int total = 0;
    QList<WorkflowStepRunInfo> steps;
};

// 任务运行态快照。这个结构给历史页的“开始/继续执行”按钮和状态提示用。
struct WorkflowRuntimeStateInfo
{
    QString taskId;
    QString mode;
    QString status;
    bool terminal = false;
    QStringList allowedActions;
    QStringList allowedNextStatuses;
    QString message;
};

// 任务产物清单。artifact 可能是真实文件，也可能是 dry-run 虚拟产物。
struct WorkflowArtifactInfo
{
    QString artifactId;
    QString taskId;
    QString stepId;
    QString agentId;
    QString kind;
    QString name;
    QString summary;
    QString uri;
    QString mimeType;
    QJsonObject metadata;
    QString createdAt;
};

struct WorkflowArtifactListResult
{
    QString taskId;
    int total = 0;
    QList<WorkflowArtifactInfo> artifacts;
};

// PDF Tool 的终态只包含任务摘要、受控 artifact 和确定性验证数据；正文预览仍交给历史页。
struct PdfProcessingTaskResult
{
    QString taskId;
    QString status;
    QString operation;
    QString summary;
    QString message;
    bool hasArtifact = false;
    WorkflowArtifactInfo artifact;
    QJsonObject verification;
};

// 单个产物的受控预览。后端负责目录边界、类型判断和字节上限；
// Qt 只展示结果，不再为了预览去猜或读取任意本地路径。
struct WorkflowArtifactPreviewResult
{
    QString taskId;
    QString artifactId;
    bool available = false;
    QString reason;
    QString kind;
    QString name;
    QString uri;
    QString mimeType;
    QString source;
    QString text;
    QString encoding;
    int bytesRead = 0;
    bool truncated = false;
    QJsonObject metadata;
};

// 工具调用审计记录。后面接真实 Runtime 时，这里就是历史页查看 tool call 的主要入口。
struct WorkflowToolCallInfo
{
    QString callId;
    QString taskId;
    QString stepId;
    QString agentId;
    QString toolName;
    QString status;
    QString riskLevel;
    bool permissionRequired = false;
    int attempt = 1;
    int maxAttempts = 3;
    int timeoutMs = 0;
    int durationMs = 0;
    int failureCount = 0;
    QJsonObject request;
    QJsonObject result;
    QString error;
    QString startedAt;
    QString finishedAt;
};

struct WorkflowToolCallListResult
{
    QString taskId;
    int total = 0;
    QList<WorkflowToolCallInfo> toolCalls;
};

// Runtime 执行预算。后端用于限制步骤数、工具调用数、重试和超时；
// Qt 端只展示这些刹车参数，不在前端重新执行业务判断。
struct RuntimeExecutionLimitsInfo
{
    int maxSteps = 0;
    int maxToolCalls = 0;
    int maxRetriesPerTool = 0;
    int toolTimeoutMs = 0;
    int taskTimeoutMs = 0;
    int tokenBudget = -1;
};

// Runtime 运行指标。它是 Agent 效果评估的基础，比单看模型回复更可靠。
struct RuntimeExecutionMetricsInfo
{
    QString startedAt;
    QString finishedAt;
    int durationMs = 0;
    int stepTotal = 0;
    int stepCompleted = 0;
    int stepFailed = 0;
    int toolCallTotal = 0;
    int toolCallSimulated = 0;
    int toolCallFailed = 0;
    int retryTotal = 0;
    int permissionRequestTotal = 0;
    int validationErrorTotal = 0;
    int estimatedInputTokens = 0;
    int estimatedOutputTokens = 0;
    double estimatedCostCny = 0.0;
    bool budgetExceeded = false;
};

struct WorkflowRuntimeMetricsResult
{
    QString taskId;
    RuntimeExecutionLimitsInfo limits;
    RuntimeExecutionMetricsInfo metrics;
};

// 任务历史中保存的实际模型路由。只保留脱敏后的路由、Provider、模型和思考状态，
// 不把 API Key、Base URL 或模型原始请求复制进 Qt 进程。
struct WorkflowModelRouteAuditInfo
{
    QString stage;
    QString routeId;
    QString profileId;
    QString mode;
    QString provider;
    QString label;
    QString model;
    QString thinking;
    QString compatibility;
    QString note;
};

struct WorkflowModelRouteAuditResult
{
    QString taskId;
    QList<WorkflowModelRouteAuditInfo> modelRoutes;
};

// Harness Evaluation 层的任务评估摘要。后端按现有运行事实计算，
// Qt 只负责展示成功率、效率分、风险提示和下一步建议。
struct WorkflowTaskEvaluationResult
{
    QString taskId;
    QString mode;
    QString status;
    QString outcome;
    QString summary;
    double stepSuccessRate = 0.0;
    double toolSuccessRate = 0.0;
    double efficiencyScore = 0.0;
    double overallScore = 0.0;
    int durationMs = 0;
    int retryTotal = 0;
    int failedToolCalls = 0;
    int blockedToolCalls = 0;
    int pendingPermissions = 0;
    int deniedPermissions = 0;
    QStringList warnings;
    QStringList recommendations;
};

// 阶段 5 的内置节点契约。它描述 Agent/action 对应哪个稳定工具、需要哪些权限、
// 可能有哪些失败码和评估信号；Qt 只缓存并展示，不在前端执行这些工具。
struct WorkflowNodeContractInfo
{
    QString agentId;
    QString action;
    QString toolName;
    QString nodeType;
    QJsonObject inputSchema;
    QJsonObject outputSchema;
    QStringList stateWrites;
    QStringList requiredPermissions;
    QStringList failureCodes;
    QStringList evaluationSignals;
};

struct WorkflowNodeContractListResult
{
    int total = 0;
    QList<WorkflowNodeContractInfo> contracts;
};

// 命令安全策略静态检查结果。这里不代表已经执行命令，
// 只用于代码工坊和后续 Shell Runtime 在执行前向用户解释风险。
struct WorkflowCommandPolicyCheckResult
{
    QString command;
    QString normalizedCommand;
    QString riskLevel = QStringLiteral("none");
    bool allowed = true;
    bool requiresConfirmation = false;
    bool auditRequired = true;
    bool concurrencySafe = false;
    int defaultTimeoutMs = 0;
    int maxOutputChars = 0;
    QStringList detectedCommands;
    QStringList categories;
    QStringList ruleIds;
    QStringList reasons;
    QStringList destructiveWarnings;
    QStringList saferAlternatives;
    QStringList warnings;
    QString suggestedTool;
    QString effectivePermissionPolicy = QStringLiteral("smart_confirm");
    QString effectiveAction = QStringLiteral("confirm");
    QString effectiveReason;
    QString executionScope = QStringLiteral("none");
    QString executionRoute;
    QString cwdPolicy;
    QString sandboxHint;
    QStringList auditFields;
    QStringList executionNotes;
    bool runtimeReady = false;
    bool permissionRequired = false;
    QString runtimeRequestStatus = QStringLiteral("blocked");
    QString approvalPrompt;
    QString blockReasonCode;
    QJsonObject auditRecordPreview;
};

// 平台运行偏好。它影响默认审批模式、Agent 表达风格和是否允许 Commander 读取
// 用户确认过的长期记忆，但不能绕过后端 Runtime 的权限边界和审计记录。
struct RuntimePreferencesResult
{
    QString permissionPolicy = QStringLiteral("smart_confirm");
    QString personality = QStringLiteral("professional");
    bool memoryEnabled = false;
    QString updatedAt;
    QString notes;
};

// 长期记忆只传递用户确认后的短摘要。桌面端不保存或展示 API Key、原始文件正文、完整聊天记录
// 和绝对路径；这些边界后端会再次校验。
struct LongTermMemoryInfo
{
    QString memoryId;
    QString kind;
    QString scope;
    QString title;
    QString summary;
    QStringList tags;
    QString sourceTaskId;
    bool userConfirmed = false;
    bool enabled = true;
    QString createdAt;
    QString updatedAt;
    QString lastUsedAt;
};

// 任务完成后的候选只是“待客户确认的草稿”，与已保存的 LongTermMemoryInfo 严格分开，避免
// 界面把建议误标成系统已经记住的事实。
struct TaskMemoryProposalInfo
{
    QString proposalId;
    QString taskId;
    QString kind;
    QString title;
    QString summary;
    QStringList tags;
    QString suggestedScope = QStringLiteral("global");
    QString reason;
    bool requiresUserConfirmation = true;
};

struct TaskMemoryProposalListResult
{
    QString taskId;
    QList<TaskMemoryProposalInfo> items;
    QString note;
};

// 模型供应商静态信息。只传递脱敏的 Key 是否已保存，不包含任何密钥内容。
// 这样客户可以预先配置不同 Agent 所需模型，而不必把未启用的 provider 误判为未设置。
struct ModelProviderInfo
{
    QString provider;
    QString label;
    QString transport;
    QString defaultBaseUrl;
    QString defaultModel;
    bool supportsThinking = false;
    bool supportsJsonOutput = true;
    bool supportsToolCalls = true;
    bool apiKeyConfigured = false;
    QString notes;
};

// 当前解析出来的模型运行时状态。
// 这里对应后端 /api/models/providers 返回的 current 字段。
struct ModelProviderStatus
{
    QString provider;
    QString label;
    QString transport;
    QString baseUrl;
    QString model;
    QString thinking;
    QString apiKeySource;
    QString configurationSource;
    QString secureStorage;
    bool apiKeyConfigured = false;
    bool secureStorageAvailable = false;
    bool supportsThinking = false;
    QString notes;
    QString configurationError;
};

// 模型供应商列表接口的轻量响应。
struct ModelProviderListResult
{
    ModelProviderStatus current;
    QList<ModelProviderInfo> providers;
};

// 一个产品作用域的显式模型路由。它只保存/展示 Provider、模型与思考偏好；API Key 始终
// 留在后端的按 Provider 安全存储中，不能因为“任务模型设置”再复制一份密钥到 Qt。
struct ModelRouteInfo
{
    QString routeId;
    QString label;
    QString description;
    QStringList requiredCapabilities;
    QString mode;
    QString provider;
    QString baseUrl;
    QString model;
    QString thinking;
    QString updatedAt;
    QString availability;
    QString availabilityMessage;
    QString resolvedProfileId;
    QString resolvedProvider;
    QString resolvedLabel;
    QString resolvedModel;
    QString resolvedThinking;
    QString resolvedCompatibility;
    QString resolvedNote;
    bool hasResolved = false;
};

struct ModelRouteListResult
{
    QList<ModelRouteInfo> routes;
};

// 模型连接测试的轻量结果。
// 这个结果专门给“测试连接”按钮使用，只回传测试状态、耗时和响应摘要，不包含 API Key。
struct ModelConnectionTestResult
{
    bool ok = false;
    QString provider;
    QString label;
    QString transport;
    QString baseUrl;
    QString model;
    QString apiKeySource;
    int elapsedMs = 0;
    QString message;
    QString responsePreview;
};

// 历史任务控制接口的轻量响应。
// 前端这里只关心动作、是否接受、提示信息和 retry 产生的新 task_id。
struct TaskControlResult
{
    QString taskId;
    QString action;
    bool accepted = false;
    QString status;
    QString message;
    QString newTaskId;
};

// 显式执行/继续执行真实 Runtime 的轻量响应。
struct WorkflowExecutionResult
{
    QString sourceTaskId;
    QString runtimeTaskId;
    bool accepted = false;
    QString status;
    QString message;
    QJsonObject workflowRun;
};

// 后端权限审计接口的轻量结构。
// 这里完全对齐 RuntimePermission* 协议，避免在 Qt 端再写一套“猜字段”的逻辑。
struct RuntimePermissionRequest
{
    QString requestId;
    QString taskId;
    QString stepId;
    QString agentId;
    QStringList permissions;
    QString riskLevel;
    QString summary;
    QJsonObject details;
    // Runtime 写入的确定性 Governance 快照；旧 dry-run 记录可以为空。
    QString permissionPolicy;
    QString policyAction;
    QString policyReason;
    QString createdAt;
};

struct RuntimePermissionDecision
{
    QString requestId;
    QString taskId;
    QString stepId;
    QString decision;
    QString decidedBy;
    QString decidedAt;
    QString note;
};

struct RuntimePermissionItem
{
    RuntimePermissionRequest request;
    RuntimePermissionDecision decision;
    QString createdAt;
    QString updatedAt;
};

struct RuntimePermissionListResult
{
    QString taskId;
    int total = 0;
    QList<RuntimePermissionItem> permissions;
};

// WebSocket 任务日志事件，字段名与后端 TaskLogEvent 保持一致。
struct TaskLogEvent
{
    QString taskId;
    int sequence = 0;
    QString event;
    QString agentId;
    QString stepId;
    QString level;
    QString message;
    QString createdAt;
};

struct TaskLogListResult
{
    QString taskId;
    int total = 0;
    QList<TaskLogEvent> events;
};

// 后端 /updates 聚合视图。它把日志、步骤、工具、权限和产物按时间线整理好，
// Qt 端只负责展示，不再从多个详情接口里猜“当前发生到了哪一步”。
struct WorkflowTaskUpdateInfo
{
    int sequence = 0;
    QString updateType;
    QString event;
    QString level;
    QString agentId;
    QString stepId;
    QString status;
    QString title;
    QString message;
    QString occurredAt;
    QJsonObject payload;
};

struct WorkflowTaskUpdateListResult
{
    QString taskId;
    int total = 0;
    QList<WorkflowTaskUpdateInfo> updates;
};

// 历史任务列表的筛选条件。当前先覆盖后端已经支持的字段，
// 关键词搜索留在 Qt 本地做，避免把更多协议参数提前塞进 API。
struct TaskHistoryQuery
{
    int limit = 20;
    int offset = 0;
    QString status;
    QString mode;
    QString maxRiskLevel;
    // -1 = 全部，0 = 不需要确认，1 = 需要确认。
    int requiresConfirmation = -1;
};

class BackendClient : public QObject
{
    Q_OBJECT

public:
    explicit BackendClient(QObject *parent = nullptr);

    void refresh();
    // 发送调度台消息。materials 只能包含已由本地工作台确认的相对引用；Qt 不发送绝对路径
    // 或文件正文，实际可读取范围仍由后端 Runtime 再次校验。
    void sendChatMessage(const QString &message,
                         const QString &agentId = QString(),
                         const QJsonArray &materials = QJsonArray(),
                         const QString &projectScope = QStringLiteral("global"),
                         const QString &conversationId = QString(),
                         const QJsonArray &agentHints = QJsonArray());
    // 重启后按不透明会话 ID 取回已脱敏的有限聊天记录。不存在的 ID 不会隐式创建会话。
    void requestConversationContext(const QString &conversationId);
    // 最近会话切换入口只取元数据；客户点选后才读取该会话的受控摘要和近轮内容。
    void requestConversationSessions(const QString &projectScope, int limit = 40);
    // 阅读完整历史时按页取回已脱敏正文，避免会话很长时阻塞调度台或无界占用内存。
    void requestConversationTranscript(
        const QString &conversationId,
        const QString &projectScope,
        int offset = 0,
        int limit = 100);
    // 拉取历史任务摘要列表，用于历史页表格展示。
    void requestTaskHistory(const TaskHistoryQuery &query = TaskHistoryQuery{});
    // 拉取模型供应商清单和当前运行时状态，用于模型页只读概览。
    void requestModelProviders();
    // 拉取各 Agent/任务作用域的显式模型路由；只读取本地脱敏配置，不触发模型调用。
    void requestModelRoutes();
    // 保存一个作用域的模型路由。显式模式只引用已有的 Provider Key，不传递或保存新 Key。
    void saveModelRoute(
        const QString &routeId,
        const QString &mode,
        const QString &provider = QString(),
        const QString &baseUrl = QString(),
        const QString &model = QString(),
        const QString &thinking = QStringLiteral("disabled"));
    // 保存全局模型配置。apiKey 为空时默认不修改已保存 Key，clearApiKey=true 时才清空本地 Key。
    void saveModelConfig(
        const QString &provider,
        const QString &baseUrl,
        const QString &model,
        const QString &thinking,
        const QString &apiKey,
        bool clearApiKey);
    // 用当前表单内容测试一次模型连接，不会写入本地配置。
    void testModelConnection(
        const QString &provider,
        const QString &baseUrl,
        const QString &model,
        const QString &thinking,
        const QString &apiKey);
    // 把用户选择的 UTF-8 txt/markdown 导入后端受控 workspace。
    void importWorkspaceDocument(const QString &filename, const QString &content);
    // PDF/DOCX 以 Base64 通过同一受控协议上传；后端负责格式、大小和解析边界校验。
    void importWorkspaceBinaryDocument(const QString &filename, const QByteArray &content);
    // 仅在文档助手打开、导入成功或用户主动刷新时读取 workspace 列表，避免无意义磁盘扫描。
    void requestWorkspaceDocuments();
    // K1 资料库管理：只访问后端已确认的稳定 ID 与 workspace 相对文件名，不接收绝对路径。
    void requestKnowledgeBases();
    void createKnowledgeBase(const QString &name, const QString &description = QString());
    void requestKnowledgeDocuments(const QString &knowledgeBaseId);
    void importWorkspaceDocumentsToKnowledgeBase(const QString &knowledgeBaseId, const QStringList &documentNames);
    void startKnowledgeIndex(const QString &knowledgeBaseId);
    void requestKnowledgeIndexJob(const QString &indexJobId);
    void requestKnowledgeVectorCapability();
    // 调用前由 MainWindow 展示客户确认框；后端仍会要求 confirm_download=true，双层防止误下载。
    void prepareKnowledgeVectorModel();
    // K7.4 的能力诊断不初始化 Paddle、不加载模型且不联网；OCR 模型准备与每次材料导入严格分离。
    void requestKnowledgeOcrCapability();
    void prepareKnowledgeOcrModel();
    void requestKnowledgeOcrPreparation(const QString &preparationId);
    void deleteKnowledgeBase(const QString &knowledgeBaseId);
    // K3 可信问答只接收当前资料库的稳定 ID 与客户问题；后端先创建后台任务，再由 WebSocket
    // 推送检索、Gate 和引用验证阶段，避免一次模型请求阻塞 Qt 主线程。
    void startKnowledgeAnswer(const QString &knowledgeBaseId, const QString &query);
    void requestKnowledgeAnswerResult(const QString &taskId);
    // K4 深度任务始终冻结当前 ready 索引版本。Qt 只驱动后台任务与补读，不把原始资料传到界面层。
    void startKnowledgeDeepTask(
        const QString &knowledgeBaseId,
        const QString &taskKind,
        const QString &taskGoal,
        const QStringList &documentIds = {});
    void requestKnowledgeDeepTaskResult(const QString &taskId);
    void pauseKnowledgeDeepTask(const QString &taskId);
    void resumeKnowledgeDeepTask(const QString &taskId);
    void cancelKnowledgeDeepTask(const QString &taskId);
    // 正式报告会创建一个新的受控 Markdown artifact；调用方必须在 UI 完成客户确认后才调用。
    void exportKnowledgeDeepTaskReport(const QString &taskId, const QString &filename);
    // 数据工作台 D1 只导入 Excel/CSV 到专属工作区，并由后端在线程池完成画像。
    // 这里的 Base64 仅用于本机 HTTP 传输，不把源文件绝对路径传给后端。
    void importDataDataset(const QString &filename, const QByteArray &content);
    void requestDataDatasets();
    void requestDataDatasetProfile(const QString &datasetName);
    // D5.1 只使用后端已经建立的字段画像生成建议；不会上传原始表格，也不触发分析或写入。
    void requestDataRecommendations(const QString &datasetName, const QString &goal = QString());
    // D2 只生成本地确定性分析预览；它不调用模型、不创建工作簿，也不会把整表传回 Qt。
    void requestDataAnalysisPreview(const QString &datasetName, const QString &goal, int maxChartCount = 4);
    // D3 只在用户复核预览后调用。后端会比对源哈希并写入新的受控工作簿，不接收本机输出路径。
    void requestDataAnalysisWorkbookExport(
        const QString &datasetName,
        const QString &sourceSha256,
        const QString &goal,
        int maxChartCount = 4);
    // D4 后台导出先只受理任务，实时阶段由既有 TaskLog WebSocket 推送；结果单独查询，
    // 避免一次 Excel 写入、回读验证长期占住同一个 HTTP 响应。
    void requestDataAnalysisWorkbookExportResult(const QString &taskId);
    // D5.2 同样必须由客户在预览页明确确认。后端只渲染已验证聚合结果为 PNG，不接收
    // 输出路径，也不会重新把原始表格交给模型。
    void requestDataChartExport(
        const QString &datasetName,
        const QString &sourceSha256,
        const QString &goal,
        int maxChartCount = 4);
    void requestDataChartExportResult(const QString &taskId);
    // 图表字节只通过专用的 task/artifact 受控接口读取，主窗口不触碰本机路径。
    void requestDataChartImage(const QString &taskId, const QString &artifactId);
    // D5.3 字段加工始终先取得内存预览，再由客户明确确认写出一个新 Excel 副本。这里接收
    // 引导式 UI 生成的有限参数对象，不支持公式、路径、脚本或任意表达式。
    void requestDataTransformationPreview(const QJsonObject &request);
    void requestDataTransformationExport(const QJsonObject &request);
    void requestDataTransformationExportResult(const QString &taskId);
    // 受理首个正式只读 Agent。后端会立即返回 task_id，再由 WebSocket 推送真实阶段事件。
    void runDocumentAgent(
        const QString &taskGoal,
        const QStringList &documentRefs,
        const QString &outputMode,
        const QString &query = QString());
    // 文档任务日志流结束后读取已校验终态；运行中不会返回未经验证的模型原文。
    void requestDocumentAgentResult(const QString &taskId);
    // 受理本地 PDF 合并、提取、旋转或删除任务。Tool 只读取已导入 workspace 文件，输出固定在受控目录。
    void startPdfProcessing(
        const QString &operation,
        const QStringList &documentRefs,
        const QString &pageRange = QString(),
        int rotationDegrees = 0);
    // PDF Tool 日志结束后读取最终 artifact 和验证结果；不会读取或解析本机绝对路径。
    void requestPdfProcessingResult(const QString &taskId);
    // 基于已完成草稿的一个章节发起派生创作任务；后端恢复原材料范围，不接收客户端文件路径。
    void expandDocumentDraftSection(
        const QString &sourceTaskId,
        const QString &sectionId,
        const QString &instruction);
    // 基于已完成草稿派生只读事实核验；后端只恢复任务中的受控草稿和材料范围。
    void reviewDocumentDraft(const QString &sourceTaskId, const QString &focus = QString());
    // 审校一个已验证草稿章节并返回候选建议；不把正文、路径或写入选项交给客户端接口。
    void reviewDocumentDraftSection(
        const QString &sourceTaskId,
        const QString &sectionId,
        const QString &focus = QString());
    // 仅根据已完成审校任务的 suggestion_id 建立独立修订预览；不会传递正文或触发文件写入。
    void createDocumentDraftSectionRevisionPreview(
        const QString &sourceReviewTaskId,
        const QString &suggestionId);
    // 多建议预览只上传同一审校任务的稳定建议 ID；后端验证唯一定位和区间冲突，不接收正文。
    void createDocumentDraftSectionBatchRevisionPreview(
        const QString &sourceReviewTaskId,
        const QStringList &suggestionIds);
    // 用户编辑只建立待来源核验的独立预览；服务端重新绑定原草稿版本，保存前必须经过事实核验。
    void createDocumentDraftSectionManualRevisionPreview(
        const QString &sourceTaskId,
        const QString &sectionId,
        const QString &revisedBody);
    // 从已完成历史草稿恢复一个新的独立预览；只传任务身份，正文、来源和版本链均由后端恢复。
    void restoreDocumentDraftPreview(const QString &sourceTaskId);
    // 只上传内置模板 ID；后端从已核验草稿快照重组章节与来源，不读取材料或写入文件。
    void createDocumentDraftTemplatePreview(const QString &sourceTaskId, const QString &templateId);
    // 章节合并先读取同根候选和只读三方计划；确认冲突后才建立独立预览，不接收正文或路径。
    void requestDocumentDraftMergeCandidates(const QString &taskId);
    void requestDocumentDraftMergePlan(const QString &primaryTaskId, const QString &secondaryTaskId);
    void createDocumentDraftMergePreview(
        const QString &primaryTaskId,
        const QString &secondaryTaskId,
        const QJsonArray &resolutions);
    // 只读比较当前草稿与直接父版本；后端从 SQLite 快照取数，不创建任务或触发模型/文件操作。
    void requestDocumentDraftParentDiff(const QString &taskId);
    // 仅在用户点击并确认后，把已验证 Markdown 草稿保存到后端固定的受控输出目录。
    void saveDocumentDraft(const QString &taskId, const QString &filename);
    // 先读取已核验草稿的确定性 PPT 计划；这个请求不调用模型、不创建文件。
    void requestProjectProposalPresentationPreview(const QString &taskId);
    // 只有用户在预览对话框中确认后才写入 .pptx，后端还会检查计划是否已过期。
    void exportProjectProposalPresentation(
        const QString &taskId,
        const QString &planId,
        const QString &filename);
    // PPT 制作 V2 先异步建立“意图 -> 创作简报 -> 逐页计划”，计划完成前不写文件、不联网。
    void startPresentationStudio(
        const QString &intent,
        int targetSlideCount,
        const QString &visualAssetProvider,
        bool publicResearchEnabled,
        bool structuredDataEnabled);
    void requestPresentationStudioResult(const QString &taskId);
    // 只有独立工作台的确认操作才能调用该接口；联网图片需独立确认，服务端仍会校验计划、
    // 输出目录与同名文件保护。
    void exportPresentationStudio(
        const QString &taskId,
        const QString &planId,
        const QString &filename,
        bool fetchExternalAssets,
        bool fetchPublicResearch,
        bool fetchStructuredData,
        bool networkConfirmed);
    // 在确认导出前创建新的实时状态通道，避免同一任务的旧计划日志先关闭 WebSocket。
    void preparePresentationStudioExport(const QString &taskId);
    // 项目审查只读取用户已经明确选择的受控 workspace 文档；QNetworkReply 异步返回，不能阻塞 Qt 主线程。
    void requestProjectDocumentReview(const QString &documentRef, const QString &documentType = QStringLiteral("auto"));
    void requestPaperReview(const QString &documentRef, const QString &paperType = QStringLiteral("auto"));
    // 工作台使用 start/result 组合：先接收 task_id 并订阅真实阶段，再读取已校验报告，避免长规则检查
    // 期间只有无响应的 HTTP 等待。同步 run 接口仍保留给兼容调用和离线验证。
    void startProjectDocumentReview(const QString &documentRef, const QString &documentType = QStringLiteral("auto"));
    void requestProjectDocumentReviewResult(const QString &taskId);
    void startPaperReview(const QString &documentRef, const QString &paperType = QStringLiteral("auto"));
    void requestPaperReviewResult(const QString &taskId);
    // 拉取单个任务的日志列表，用于历史页详情和高亮确认事件。
    void requestTaskLogs(const QString &taskId);
    // 拉取单个任务对应的 Commander 计划，用于历史详情复盘“为什么这样安排”。
    void requestTaskPlan(const QString &taskId);
    // 已完成 Runtime 的候选只读查询；保存仍必须走 confirmTaskMemoryProposal 的明确确认。
    void requestTaskMemoryProposals(const QString &taskId);
    void confirmTaskMemoryProposal(
        const QString &taskId,
        const TaskMemoryProposalInfo &proposal,
        const QString &kind,
        const QString &scope,
        const QString &title,
        const QString &summary,
        const QStringList &tags);
    // 计划版本只用于调度台的“执行前修改”。后端会拒绝真实执行后的任何修订请求。
    void requestTaskPlanVersions(const QString &taskId);
    void requestTaskPlanVersion(const QString &taskId, int planVersion);
    void reviseTaskPlan(
        const QString &taskId,
        const QString &userGoal,
        const QString &changeSummary);
    // 拉取单个任务的 step 级结果，用于历史详情展示每一步状态。
    void requestTaskSteps(const QString &taskId);
    // 拉取单个任务的权限审计记录，用于历史页权限确认区。
    void requestTaskPermissions(const QString &taskId);
    // 拉取单个任务的运行态快照，用于历史页显示开始/继续执行入口和允许动作。
    void requestTaskRuntimeState(const QString &taskId);
    // 拉取执行预算和运行指标，用于历史页判断耗时、重试、失败和成本。
    void requestTaskMetrics(const QString &taskId);
    // 拉取任务实际保存的脱敏模型审计，不根据当前配置推断旧任务曾使用的模型。
    void requestTaskModelRoutes(const QString &taskId);
    // 拉取任务效果评估摘要，用于把指标转成用户可理解的成功率、效率和下一步建议。
    void requestTaskEvaluation(const QString &taskId);
    // 拉取内置 Agent 节点契约，用于历史页/调度台解释步骤、工具、权限和失败边界。
    void requestNodeContracts();
    // 静态检查命令风险，不执行命令；给代码工坊和后续 Runtime Shell 工具做安全前置。
    void checkWorkflowCommandPolicy(const QString &command, const QString &cwd = QString());
    // 读取/保存平台运行偏好：权限确认策略、Agent 语言风格和长期记忆开关。
    void requestRuntimePreferences();
    void saveRuntimePreferences(
        const QString &permissionPolicy,
        const QString &personality,
        bool memoryEnabled);
    // 长期记忆管理始终走本地后端 API。创建和编辑只提交短事实，清空需要后端的二次确认。
    void requestLongTermMemories(const QString &scope = QStringLiteral("global"));
    void createLongTermMemory(
        const QString &kind,
        const QString &scope,
        const QString &title,
        const QString &summary,
        const QStringList &tags);
    void updateLongTermMemory(
        const QString &memoryId,
        const QString &title,
        const QString &summary,
        const QStringList &tags,
        bool enabled);
    void deleteLongTermMemory(const QString &memoryId);
    void clearLongTermMemories(const QString &scope);
    // 拉取单个任务产物，用于历史页展示真实/虚拟输出文件。
    void requestTaskArtifacts(const QString &taskId);
    // 拉取单个产物的受控文本预览。maxBytes 由后端再次限幅，前端只传期望上限。
    void requestTaskArtifactPreview(
        const QString &taskId,
        const QString &artifactId,
        int maxBytes = 64 * 1024);
    // 由后端校验固定 output 根后交给系统默认程序打开，Qt 不读取或拼接绝对路径。
    void requestTaskArtifactOpen(const QString &taskId, const QString &artifactId);
    // 拉取单个任务的工具调用记录，用于历史页展示 Harness 的 tool 层审计。
    void requestTaskToolCalls(const QString &taskId);
    // 拉取任务 updates 时间线，用于把预演、权限、工具和产物串成用户可读事件流。
    void requestTaskUpdates(const QString &taskId);
    // 批准或拒绝某个权限请求。
    void requestTaskPermissionDecision(
        const QString &taskId,
        const QString &requestId,
        const QString &decision,
        const QString &decidedBy = QStringLiteral("local_user"),
        const QString &note = QString());
    // 向当前历史任务发送取消控制。数据工作簿导出会走后端的协作式取消，其它任务沿用既有控制协议。
    void requestTaskCancel(const QString &taskId);
    // Runtime 暂停是协作式控制：当前 Tool 完成后在安全检查点停下，不会强杀后台线程。
    void requestTaskPause(const QString &taskId);
    // 基于缓存计划重新生成一个新的 dry-run 任务。
    void requestTaskRetry(const QString &taskId);
    // 显式启动或继续执行后台 Runtime；响应只表示任务已受理，不等待所有 Tool 完成。
    void requestTaskExecute(const QString &taskId);
    // 使用 chat 返回的 task_id 建立任务日志流。
    void connectTaskLog(const QString &taskId);
    QUrl baseUrl() const;

signals:
    void healthChecked(bool ok, const QString &message);
    void agentsLoaded(const QList<AgentInfo> &agents);
    void agentsLoadFailed(const QString &message);
    void chatCompleted(const ChatResult &result);
    void chatFailed(const QString &message);
    void conversationContextReceived(const ConversationContextInfo &context);
    void conversationContextFailed(const QString &message);
    void conversationSessionsReceived(const ConversationSessionListResult &result);
    void conversationSessionsFailed(const QString &message);
    void conversationTranscriptReceived(const ConversationTranscriptPageResult &result);
    void conversationTranscriptFailed(const QString &conversationId, const QString &message);
    void taskLogReceived(const TaskLogEvent &event);
    void taskLogFinished(const QString &taskId);
    void taskLogFailed(const QString &message);
    void taskHistoryReceived(const TaskHistoryResult &result);
    void taskHistoryFailed(const QString &message);
    void taskPlanReceived(const WorkflowPlanDetailResult &result);
    void taskPlanFailed(const QString &message);
    void taskMemoryProposalsReceived(const TaskMemoryProposalListResult &result);
    void taskMemoryProposalsFailed(const QString &taskId, const QString &message);
    void taskMemoryProposalConfirmed(const QString &taskId, const QString &message);
    void taskMemoryProposalConfirmFailed(const QString &taskId, const QString &message);
    void taskPlanVersionsReceived(const WorkflowPlanVersionListResult &result);
    void taskPlanVersionsFailed(const QString &message);
    void taskPlanVersionReceived(const WorkflowPlanDetailResult &result);
    void taskPlanVersionFailed(const QString &message);
    void taskPlanRevisionCompleted(const WorkflowPlanRevisionResult &result);
    void taskPlanRevisionFailed(const QString &message);
    void taskStepsReceived(const TaskStepListResult &result);
    void taskStepsFailed(const QString &message);
    void taskRuntimeStateReceived(const WorkflowRuntimeStateInfo &result);
    void taskRuntimeStateFailed(const QString &message);
    void taskMetricsReceived(const WorkflowRuntimeMetricsResult &result);
    void taskMetricsFailed(const QString &message);
    void taskModelRoutesReceived(const WorkflowModelRouteAuditResult &result);
    void taskModelRoutesFailed(const QString &message);
    void taskEvaluationReceived(const WorkflowTaskEvaluationResult &result);
    void taskEvaluationFailed(const QString &message);
    void nodeContractsReceived(const WorkflowNodeContractListResult &result);
    void nodeContractsFailed(const QString &message);
    void workflowCommandPolicyChecked(const WorkflowCommandPolicyCheckResult &result);
    void workflowCommandPolicyCheckFailed(const QString &message);
    void runtimePreferencesReceived(const RuntimePreferencesResult &result);
    void runtimePreferencesFailed(const QString &message);
    void runtimePreferencesSaved(const RuntimePreferencesResult &result);
    void runtimePreferencesSaveFailed(const QString &message);
    void longTermMemoriesReceived(const QList<LongTermMemoryInfo> &items);
    void longTermMemoriesFailed(const QString &message);
    void longTermMemoryMutationCompleted(const QString &message);
    void longTermMemoryMutationFailed(const QString &message);
    void taskArtifactsReceived(const WorkflowArtifactListResult &result);
    void taskArtifactsFailed(const QString &message);
    void taskArtifactPreviewReceived(const WorkflowArtifactPreviewResult &result);
    void taskArtifactPreviewFailed(const QString &taskId, const QString &artifactId, const QString &message);
    void taskArtifactOpened(const QString &taskId, const QString &artifactId, const QString &message);
    void taskArtifactOpenFailed(const QString &taskId, const QString &artifactId, const QString &message);
    void taskToolCallsReceived(const WorkflowToolCallListResult &result);
    void taskToolCallsFailed(const QString &message);
    void taskUpdatesReceived(const WorkflowTaskUpdateListResult &result);
    // 调度台和历史页可能同时请求 updates，失败时必须携带任务 ID，避免错误串到别的任务。
    void taskUpdatesFailed(const QString &taskId, const QString &message);
    void modelProvidersReceived(const ModelProviderListResult &result);
    void modelProvidersFailed(const QString &message);
    void modelRoutesReceived(const ModelRouteListResult &result);
    void modelRoutesFailed(const QString &message);
    void modelRouteSaved(const ModelRouteInfo &route);
    void modelRouteSaveFailed(const QString &message);
    void modelConfigSaved(const ModelProviderStatus &status);
    void modelConfigSaveFailed(const QString &message);
    void modelConnectionTestCompleted(const ModelConnectionTestResult &result);
    void modelConnectionTestFailed(const QString &message);
    void workspaceDocumentImported(const WorkspaceDocumentInfo &document);
    void workspaceDocumentImportFailed(const QString &message);
    void workspaceDocumentsReceived(const WorkspaceDocumentListResult &result);
    void workspaceDocumentsFailed(const QString &message);
    void knowledgeBasesReceived(const KnowledgeBaseListResult &result);
    void knowledgeBasesFailed(const QString &message);
    void knowledgeBaseCreated(const KnowledgeBaseInfo &knowledgeBase);
    void knowledgeBaseCreateFailed(const QString &message);
    void knowledgeDocumentsReceived(const KnowledgeDocumentListResult &result);
    void knowledgeDocumentsFailed(const QString &message);
    void knowledgeDocumentsImported(const QString &knowledgeBaseId);
    void knowledgeDocumentsImportFailed(const QString &message);
    void knowledgeIndexStarted(const KnowledgeIndexJobInfo &job);
    void knowledgeIndexStartFailed(const QString &message);
    void knowledgeIndexJobReceived(const KnowledgeIndexJobInfo &job);
    void knowledgeIndexJobFailed(const QString &message);
    void knowledgeVectorCapabilityReceived(const KnowledgeVectorCapabilityInfo &capability);
    void knowledgeVectorCapabilityFailed(const QString &message);
    void knowledgeVectorModelPrepared(const QString &message);
    void knowledgeVectorModelPrepareFailed(const QString &message);
    void knowledgeOcrCapabilityReceived(const KnowledgeOcrCapabilityInfo &capability);
    void knowledgeOcrCapabilityFailed(const QString &message);
    void knowledgeOcrPreparationReceived(const KnowledgeOcrPreparationInfo &preparation);
    void knowledgeOcrPreparationFailed(const QString &message);
    void knowledgeBaseDeletionRequested(const KnowledgeBaseInfo &knowledgeBase);
    void knowledgeBaseDeletionFailed(const QString &message);
    void knowledgeAnswerStarted(const KnowledgeAnswerTaskStartResult &result);
    void knowledgeAnswerCompleted(const KnowledgeAnswerTaskResult &result);
    void knowledgeAnswerStillRunning(const QString &taskId, const QString &status);
    void knowledgeAnswerFailed(const QString &message);
    void knowledgeDeepTaskStarted(const KnowledgeDeepTaskStartResult &result);
    void knowledgeDeepTaskResultReceived(const KnowledgeDeepTaskResult &result);
    void knowledgeDeepTaskStillRunning(const QString &taskId, const QString &status);
    void knowledgeDeepTaskControlCompleted(const KnowledgeDeepTaskControlResult &result);
    void knowledgeDeepTaskReportExported(const KnowledgeDeepTaskReportExportResult &result);
    void knowledgeDeepTaskFailed(const QString &message);
    void dataDatasetImported(const DataDatasetInfo &dataset);
    void dataDatasetImportFailed(const QString &message);
    void dataDatasetsReceived(const DataDatasetListResult &result);
    void dataDatasetsFailed(const QString &message);
    // 画像响应中的有限预览仅在 Qt 当前页消费，BackendClient 不做额外持久化或日志输出。
    void dataDatasetProfileReceived(const QJsonObject &profile);
    void dataDatasetProfileFailed(const QString &message);
    void dataRecommendationsReceived(const QJsonObject &recommendations);
    void dataRecommendationsFailed(const QString &message);
    void dataAnalysisPreviewReceived(const QJsonObject &preview);
    void dataAnalysisPreviewFailed(const QString &message);
    void dataAnalysisWorkbookExportStarted(const QString &taskId);
    void dataAnalysisWorkbookExported(const QJsonObject &result);
    void dataAnalysisWorkbookExportStillRunning(const QString &taskId, const QString &status);
    void dataAnalysisWorkbookExportCancelled(const QString &message);
    void dataAnalysisWorkbookExportFailed(const QString &message);
    void dataChartExportStarted(const QString &taskId);
    void dataChartExported(const QJsonObject &result);
    void dataChartExportStillRunning(const QString &taskId, const QString &status);
    void dataChartExportCancelled(const QString &message);
    void dataChartExportFailed(const QString &message);
    void dataChartImageReceived(const QString &taskId, const QString &artifactId, const QByteArray &imageBytes);
    void dataChartImageFailed(const QString &taskId, const QString &artifactId, const QString &message);
    void dataTransformationPreviewReceived(const QJsonObject &preview);
    void dataTransformationPreviewFailed(const QString &message);
    void dataTransformationExportStarted(const QString &taskId);
    void dataTransformationExported(const QJsonObject &result);
    void dataTransformationExportStillRunning(const QString &taskId, const QString &status);
    void dataTransformationExportCancelled(const QString &message);
    void dataTransformationExportFailed(const QString &message);
    void documentAgentStarted(const DocumentAgentTaskStartResult &result);
    void documentAgentCompleted(const DocumentAgentRunResult &result);
    void documentAgentStillRunning(const QString &taskId, const QString &status);
    void documentAgentFailed(const QString &message);
    void pdfProcessingStarted(const PdfProcessingTaskStartResult &result);
    void pdfProcessingCompleted(const PdfProcessingTaskResult &result);
    void pdfProcessingStillRunning(const QString &taskId, const QString &status);
    void pdfProcessingFailed(const QString &message);
    void documentDraftParentDiffReceived(const QJsonObject &result);
    void documentDraftParentDiffFailed(const QString &message);
    void documentDraftMergeCandidatesReceived(const QJsonObject &result);
    void documentDraftMergePlanReceived(const QJsonObject &result);
    void documentDraftMergeFailed(const QString &message);
    void documentDraftSaved(const DocumentDraftSaveResult &result);
    void documentDraftSaveFailed(const QString &message);
    void presentationPreviewReceived(const PresentationPreviewResult &result);
    void presentationPreviewFailed(const QString &message);
    void presentationExported(const PresentationExportResult &result);
    void presentationExportFailed(const QString &message);
    void presentationStudioStarted(const PresentationStudioTaskStartResult &result);
    void presentationStudioPlanReceived(const PresentationStudioPlanResult &result);
    void presentationStudioStillRunning(const QString &taskId, const QString &status);
    void presentationStudioFailed(const QString &message);
    void presentationStudioExported(const PresentationExportResult &result);
    void presentationStudioExportFailed(const QString &message);
    void presentationStudioExportPrepared(const QString &taskId);
    void projectReviewReceived(const ProjectReviewResult &result);
    void projectReviewFailed(const QString &message);
    void projectReviewStarted(const QString &taskId);
    void projectReviewStillRunning(const QString &taskId, const QString &status);
    void paperReviewReceived(const PaperReviewResult &result);
    void paperReviewFailed(const QString &message);
    void paperReviewStarted(const QString &taskId);
    void paperReviewStillRunning(const QString &taskId, const QString &status);
    void taskLogsReceived(const TaskLogListResult &result);
    void taskLogsFailed(const QString &message);
    void taskPermissionsReceived(const RuntimePermissionListResult &result);
    void taskPermissionsFailed(const QString &message);
    void taskPermissionDecisionCompleted(const RuntimePermissionItem &item);
    void taskPermissionDecisionFailed(const QString &message);
    void taskControlCompleted(const TaskControlResult &result);
    void taskControlFailed(const QString &message);
    void taskExecutionCompleted(const WorkflowExecutionResult &result);
    void taskExecutionFailed(const QString &message);

private:
    QNetworkRequest createRequest(const QString &path, int transferTimeoutMs = 3000) const;
    QNetworkRequest createRequest(const QUrl &url, int transferTimeoutMs = 3000) const;
    void requestHealth();
    void requestAgents();
    QUrl buildTaskHistoryUrl(const TaskHistoryQuery &query) const;
    QUrl buildModelProvidersUrl() const;
    QUrl buildModelRoutesUrl() const;
    QUrl buildModelRouteUrl(const QString &routeId) const;
    QUrl buildModelConfigUrl() const;
    QUrl buildModelTestUrl() const;
    QUrl buildWorkspaceDocumentsUrl() const;
    QUrl buildKnowledgeBasesUrl() const;
    QUrl buildKnowledgeDocumentsUrl() const;
    QUrl buildKnowledgeBaseDocumentsUrl(const QString &knowledgeBaseId) const;
    QUrl buildKnowledgeIndexStartUrl(const QString &knowledgeBaseId) const;
    QUrl buildKnowledgeIndexJobUrl(const QString &indexJobId) const;
    QUrl buildKnowledgeVectorCapabilityUrl() const;
    QUrl buildKnowledgeVectorPrepareUrl() const;
    QUrl buildKnowledgeOcrCapabilityUrl() const;
    QUrl buildKnowledgeOcrPrepareUrl() const;
    QUrl buildKnowledgeOcrPreparationUrl(const QString &preparationId) const;
    QUrl buildKnowledgeBaseUrl(const QString &knowledgeBaseId) const;
    QUrl buildKnowledgeAnswerStartUrl() const;
    QUrl buildKnowledgeAnswerResultUrl(const QString &taskId) const;
    QUrl buildKnowledgeDeepTaskStartUrl() const;
    QUrl buildKnowledgeDeepTaskResultUrl(const QString &taskId) const;
    QUrl buildKnowledgeDeepTaskControlUrl(const QString &taskId, const QString &action) const;
    QUrl buildKnowledgeDeepTaskReportUrl(const QString &taskId) const;
    QUrl buildDataAgentDatasetsUrl() const;
    QUrl buildDataAgentDatasetProfileUrl(const QString &datasetName) const;
    QUrl buildDataAgentRecommendationsUrl() const;
    QUrl buildDataAgentAnalysisPreviewUrl() const;
    QUrl buildDataAgentAnalysisExportStartUrl() const;
    QUrl buildDataAgentAnalysisExportResultUrl(const QString &taskId) const;
    QUrl buildDataAgentChartExportStartUrl() const;
    QUrl buildDataAgentChartExportResultUrl(const QString &taskId) const;
    QUrl buildDataAgentChartImageUrl(const QString &taskId, const QString &artifactId) const;
    QUrl buildDataAgentTransformationPreviewUrl() const;
    QUrl buildDataAgentTransformationExportStartUrl() const;
    QUrl buildDataAgentTransformationExportResultUrl(const QString &taskId) const;
    QUrl buildDocumentAgentStartUrl() const;
    QUrl buildDocumentAgentResultUrl(const QString &taskId) const;
    QUrl buildPdfProcessingStartUrl() const;
    QUrl buildPdfProcessingResultUrl(const QString &taskId) const;
    QUrl buildDocumentDraftSectionStartUrl(const QString &sourceTaskId) const;
    QUrl buildDocumentDraftReviewStartUrl(const QString &sourceTaskId) const;
    QUrl buildDocumentDraftSectionReviewStartUrl(const QString &sourceTaskId) const;
    QUrl buildDocumentDraftSectionRevisionPreviewStartUrl(const QString &sourceReviewTaskId) const;
    QUrl buildDocumentDraftSectionBatchRevisionPreviewStartUrl(const QString &sourceReviewTaskId) const;
    QUrl buildDocumentDraftSectionManualRevisionPreviewStartUrl(const QString &sourceTaskId) const;
    QUrl buildDocumentDraftRestorePreviewStartUrl(const QString &sourceTaskId) const;
    QUrl buildDocumentDraftTemplatePreviewStartUrl(const QString &sourceTaskId) const;
    QUrl buildDocumentDraftMergeCandidatesUrl(const QString &taskId) const;
    QUrl buildDocumentDraftMergePlanUrl(const QString &primaryTaskId, const QString &secondaryTaskId) const;
    QUrl buildDocumentDraftMergePreviewStartUrl(const QString &primaryTaskId) const;
    QUrl buildDocumentDraftParentDiffUrl(const QString &taskId) const;
    QUrl buildDocumentDraftSaveUrl(const QString &taskId) const;
    QUrl buildPresentationPreviewUrl(const QString &taskId) const;
    QUrl buildPresentationExportUrl(const QString &taskId) const;
    QUrl buildPresentationStudioStartUrl() const;
    QUrl buildPresentationStudioResultUrl(const QString &taskId) const;
    QUrl buildPresentationStudioExportUrl(const QString &taskId) const;
    QUrl buildPresentationStudioExportPrepareUrl(const QString &taskId) const;
    QUrl buildProjectReviewRunUrl() const;
    QUrl buildProjectReviewStartUrl() const;
    QUrl buildProjectReviewResultUrl(const QString &taskId) const;
    QUrl buildPaperReviewRunUrl() const;
    QUrl buildPaperReviewStartUrl() const;
    QUrl buildPaperReviewResultUrl(const QString &taskId) const;
    QUrl buildTaskLogsUrl(const QString &taskId) const;
    QUrl buildTaskPlanUrl(const QString &taskId) const;
    QUrl buildTaskMemoryProposalsUrl(const QString &taskId) const;
    QUrl buildTaskMemoryProposalConfirmUrl(const QString &taskId) const;
    QUrl buildTaskPlanVersionsUrl(const QString &taskId) const;
    QUrl buildTaskPlanVersionUrl(const QString &taskId, int planVersion) const;
    QUrl buildTaskPlanRevisionUrl(const QString &taskId) const;
    QUrl buildTaskStepsUrl(const QString &taskId) const;
    QUrl buildTaskPermissionsUrl(const QString &taskId) const;
    QUrl buildTaskRuntimeStateUrl(const QString &taskId) const;
    QUrl buildTaskMetricsUrl(const QString &taskId) const;
    QUrl buildTaskModelRoutesUrl(const QString &taskId) const;
    QUrl buildTaskEvaluationUrl(const QString &taskId) const;
    QUrl buildNodeContractsUrl() const;
    QUrl buildWorkflowCommandPolicyUrl() const;
    QUrl buildRuntimePreferencesUrl() const;
    QUrl buildLongTermMemoriesUrl(const QString &scope = QString(), bool confirm = false) const;
    QUrl buildTaskArtifactsUrl(const QString &taskId) const;
    QUrl buildTaskArtifactPreviewUrl(const QString &taskId, const QString &artifactId, int maxBytes) const;
    QUrl buildTaskArtifactOpenUrl(const QString &taskId, const QString &artifactId) const;
    QUrl buildTaskToolCallsUrl(const QString &taskId) const;
    QUrl buildTaskUpdatesUrl(const QString &taskId) const;
    QUrl buildTaskPermissionDecisionUrl(const QString &taskId, const QString &requestId) const;
    QUrl buildTaskControlUrl(const QString &taskId, const QString &action) const;
    QUrl buildTaskExecuteUrl(const QString &taskId) const;
    void handleTaskHistoryReply(QNetworkReply *reply);
    void handleModelProvidersReply(QNetworkReply *reply);
    void handleModelRoutesReply(QNetworkReply *reply);
    void handleModelRouteSaveReply(QNetworkReply *reply);
    void handleModelConfigSaveReply(QNetworkReply *reply);
    void handleModelConnectionTestReply(QNetworkReply *reply);
    void handleWorkspaceDocumentImportReply(QNetworkReply *reply);
    void handleWorkspaceDocumentsReply(QNetworkReply *reply);
    void handleKnowledgeBasesReply(QNetworkReply *reply);
    void handleKnowledgeBaseCreateReply(QNetworkReply *reply);
    void handleKnowledgeDocumentsReply(QNetworkReply *reply);
    void handleKnowledgeDocumentImportReply(QNetworkReply *reply);
    void handleKnowledgeIndexStartReply(QNetworkReply *reply);
    void handleKnowledgeIndexJobReply(QNetworkReply *reply);
    void handleKnowledgeVectorCapabilityReply(QNetworkReply *reply);
    void handleKnowledgeVectorPrepareReply(QNetworkReply *reply);
    void handleKnowledgeOcrCapabilityReply(QNetworkReply *reply);
    void handleKnowledgeOcrPrepareReply(QNetworkReply *reply);
    void handleKnowledgeOcrPreparationReply(QNetworkReply *reply);
    void handleKnowledgeBaseDeletionReply(QNetworkReply *reply);
    void handleKnowledgeAnswerStartReply(QNetworkReply *reply);
    void handleKnowledgeAnswerResultReply(QNetworkReply *reply);
    void handleKnowledgeDeepTaskStartReply(QNetworkReply *reply);
    void handleKnowledgeDeepTaskResultReply(QNetworkReply *reply);
    void handleKnowledgeDeepTaskControlReply(QNetworkReply *reply);
    void handleKnowledgeDeepTaskReportReply(QNetworkReply *reply);
    void handleDataDatasetImportReply(QNetworkReply *reply);
    void handleDataDatasetsReply(QNetworkReply *reply);
    void handleDataDatasetProfileReply(QNetworkReply *reply);
    void handleDataRecommendationsReply(QNetworkReply *reply);
    void handleDataAnalysisPreviewReply(QNetworkReply *reply);
    void handleDataAnalysisWorkbookExportStartReply(QNetworkReply *reply);
    void handleDataAnalysisWorkbookExportResultReply(QNetworkReply *reply);
    void handleDataChartExportStartReply(QNetworkReply *reply);
    void handleDataChartExportResultReply(QNetworkReply *reply);
    void handleDataTransformationPreviewReply(QNetworkReply *reply);
    void handleDataTransformationExportStartReply(QNetworkReply *reply);
    void handleDataTransformationExportResultReply(QNetworkReply *reply);
    void handleDocumentAgentStartReply(QNetworkReply *reply);
    void handleDocumentAgentResultReply(QNetworkReply *reply);
    void handlePdfProcessingStartReply(QNetworkReply *reply);
    void handlePdfProcessingResultReply(QNetworkReply *reply);
    void handleDocumentDraftSaveReply(QNetworkReply *reply);
    void handlePresentationPreviewReply(QNetworkReply *reply);
    void handlePresentationExportReply(QNetworkReply *reply);
    void handlePresentationStudioStartReply(QNetworkReply *reply);
    void handlePresentationStudioResultReply(QNetworkReply *reply);
    void handlePresentationStudioExportReply(QNetworkReply *reply);
    void handlePresentationStudioExportPrepareReply(QNetworkReply *reply);
    void handleProjectReviewReply(QNetworkReply *reply);
    void handleProjectReviewStartReply(QNetworkReply *reply);
    void handleProjectReviewResultReply(QNetworkReply *reply);
    void handlePaperReviewReply(QNetworkReply *reply);
    void handlePaperReviewStartReply(QNetworkReply *reply);
    void handlePaperReviewResultReply(QNetworkReply *reply);
    void handleTaskLogsReply(QNetworkReply *reply);
    void handleTaskPlanReply(QNetworkReply *reply);
    void handleTaskPlanVersionsReply(QNetworkReply *reply);
    void handleTaskPlanVersionReply(QNetworkReply *reply);
    void handleTaskPlanRevisionReply(QNetworkReply *reply);
    void handleTaskStepsReply(QNetworkReply *reply);
    void handleTaskPermissionsReply(QNetworkReply *reply);
    void handleTaskRuntimeStateReply(QNetworkReply *reply);
    void handleTaskMetricsReply(QNetworkReply *reply);
    void handleTaskModelRoutesReply(QNetworkReply *reply);
    void handleTaskEvaluationReply(QNetworkReply *reply);
    void handleNodeContractsReply(QNetworkReply *reply);
    void handleWorkflowCommandPolicyReply(QNetworkReply *reply);
    void handleRuntimePreferencesReply(QNetworkReply *reply);
    void handleRuntimePreferencesSaveReply(QNetworkReply *reply);
    void handleLongTermMemoriesReply(QNetworkReply *reply);
    void handleLongTermMemoryMutationReply(QNetworkReply *reply, const QString &successMessage);
    void handleTaskArtifactsReply(QNetworkReply *reply);
    void handleTaskArtifactPreviewReply(QNetworkReply *reply, const QString &requestedTaskId, const QString &requestedArtifactId);
    void handleTaskArtifactOpenReply(QNetworkReply *reply, const QString &requestedTaskId, const QString &requestedArtifactId);
    void handleTaskToolCallsReply(QNetworkReply *reply);
    void handleTaskUpdatesReply(QNetworkReply *reply, const QString &requestedTaskId);
    void handleTaskPermissionDecisionReply(QNetworkReply *reply);
    void handleTaskControlReply(QNetworkReply *reply);
    void handleTaskExecutionReply(QNetworkReply *reply);
    // WebSocket 收到的是 JSON 文本，这里统一转成 TaskLogEvent 再发给 UI。
    void handleTaskLogMessage(const QString &message);

    QNetworkAccessManager networkManager_;
    QWebSocket taskSocket_;
    QUrl baseUrl_;
    // workspace 清单被文档助手、PDF 工作区和启动完成回调共用。只读请求在飞行期间合并，
    // 避免慢响应以旧结果覆盖新页面状态；完成路径会在发出结果信号前释放该标记。
    bool workspaceDocumentsRequestInFlight_ = false;
    QString activeLogTaskId_;
    bool taskSocketHadError_ = false;
};

#endif // BACKENDCLIENT_H
