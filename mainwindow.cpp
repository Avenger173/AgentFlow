#include "mainwindow.h"

#include "dispatchmaterialdialog.h"
#include "modelroutedialog.h"
#include "datahelpdialog.h"
#include "datatransformationdialog.h"
#include "knowledgeanswerdialog.h"
#include "knowledgedeeptaskdialog.h"
#include "presentationstudiodialog.h"
#include "taskactivityindicator.h"
#include "ui_mainwindow.h"

#include <algorithm>
#include <cmath>
#include <functional>

#include <QAbstractItemView>
#include <QAction>
#include <QApplication>
#include <QBrush>
#include <QCheckBox>
#include <QCloseEvent>
#include <QClipboard>
#include <QComboBox>
#include <QDesktopServices>
#include <QDialog>
#include <QDialogButtonBox>
#include <QFile>
#include <QFileDialog>
#include <QFileInfo>
#include <QFrame>
#include <QFormLayout>
#include <QHeaderView>
#include <QHBoxLayout>
#include <QIcon>
#include <QInputDialog>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLabel>
#include <QLayout>
#include <QLineEdit>
#include <QListWidget>
#include <QListWidgetItem>
#include <QMessageBox>
#include <QMenu>
#include <QPlainTextEdit>
#include <QProgressBar>
#include <QPushButton>
#include <QRegularExpression>
#include <QResizeEvent>
#include <QScrollArea>
#include <QSet>
#include <QSignalBlocker>
#include <QSize>
#include <QSizePolicy>
#include <QSplitter>
#include <QSettings>
#include <QStringDecoder>
#include <QTableWidget>
#include <QTableWidgetItem>
#include <QTabWidget>
#include <QTextCursor>
#include <QTextBrowser>
#include <QTextEdit>
#include <QToolButton>
#include <QToolTip>
#include <QTimer>
#include <QUrl>
#include <QVBoxLayout>

#include <QStyle>
#include <QColor>

namespace {

constexpr int HistoryConfirmationCollapsedHeight = 58;
constexpr int HistoryConfirmationExpandedHeight = 150;
constexpr int HistoryConfirmationTextMaxHeight = 84;
constexpr int HistoryAutoRefreshIntervalMs = 4500;
constexpr int DispatchUpdatesPollIntervalMs = 3500;
constexpr int DispatchUpdatesRetryIntervalMs = 6000;
constexpr qint64 HistoryArtifactPreviewMaxBytes = 64 * 1024;
constexpr qint64 WorkspaceDocumentMaxBytes = 1'000'000;
constexpr qint64 WorkspaceBinaryDocumentMaxBytes = 10'000'000;
constexpr qint64 DataDatasetMaxBytes = 20'000'000;
constexpr int DocumentComparisonMaxDocuments = 4;

struct DispatchAgentHintDefinition
{
    const char *agentId;
    const char *displayName;
    const char *canonicalMention;
    QStringList aliases;
};

const QList<DispatchAgentHintDefinition> &dispatchAgentHintDefinitions()
{
    // `@` 只暴露已经有正式 Runtime action 的三项能力。这里不从导航、manifest 或插件
    // 动态枚举，避免客户以为历史占位页或未来 MCP 已经能够被总指挥调用。
    static const QList<DispatchAgentHintDefinition> definitions = {
        {"document_agent", "文档助手", "@文档助手", {"文档助手", "文档", "document", "document_agent"}},
        {"data_agent", "数据工作台", "@数据工作台", {"数据工作台", "数据", "data", "data_agent"}},
        {"knowledge_agent", "知识库", "@知识库", {"知识库", "知识库助手", "knowledge", "knowledge_agent"}},
    };
    return definitions;
}

QStringList parseDispatchAgentHintIds(const QString &message)
{
    const QString normalized = message.toCaseFolded();
    QStringList selected;
    for (const DispatchAgentHintDefinition &definition : dispatchAgentHintDefinitions()) {
        const bool matched = std::any_of(
            definition.aliases.cbegin(), definition.aliases.cend(), [&normalized](const QString &alias) {
                return normalized.contains(QStringLiteral("@").append(alias.toCaseFolded()));
            });
        if (matched) {
            selected.append(QString::fromLatin1(definition.agentId));
        }
    }
    return selected;
}

const DispatchAgentHintDefinition *dispatchAgentHintDefinition(const QString &agentId)
{
    for (const DispatchAgentHintDefinition &definition : dispatchAgentHintDefinitions()) {
        if (agentId == QLatin1String(definition.agentId)) {
            return &definition;
        }
    }
    return nullptr;
}

class ChartDashboardScrollArea final : public QScrollArea
{
public:
    explicit ChartDashboardScrollArea(QWidget *parent = nullptr)
        : QScrollArea(parent)
    {
    }

    std::function<void()> onViewportResized;

protected:
    void resizeEvent(QResizeEvent *event) override
    {
        QScrollArea::resizeEvent(event);
        if (onViewportResized) {
            onViewportResized();
        }
    }
};

QString formatDataDisplayNumber(double value)
{
    if (!std::isfinite(value)) {
        return QStringLiteral("—");
    }
    const double integerValue = std::round(value);
    if (std::abs(value - integerValue) < 0.0000001) {
        return QString::number(static_cast<qlonglong>(integerValue));
    }
    return QString::number(value, 'f', 2);
}

QString agentDisplayName(const QString &agentId)
{
    // 后端返回稳定的英文 id，UI 层负责转成中文名称。
    // 后续 Agent Registry 成熟后可以改为使用后端 name 字段。
    if (agentId == QStringLiteral("commander_agent")) {
        return QStringLiteral("总指挥");
    }
    if (agentId == QStringLiteral("document_agent")) {
        return QStringLiteral("文档助手");
    }
    if (agentId == QStringLiteral("code_agent")) {
        return QStringLiteral("代码工坊");
    }
    if (agentId == QStringLiteral("report_agent")) {
        return QStringLiteral("报告助手");
    }
    if (agentId == QStringLiteral("system")) {
        return QStringLiteral("系统");
    }
    return agentId.isEmpty() ? QStringLiteral("Agent") : agentId;
}

QString badgeForTaskEvent(const QString &eventName)
{
    if (eventName.contains(QStringLiteral("completed"))) {
        return QStringLiteral("badgeGreen");
    }
    if (eventName.contains(QStringLiteral("failed"))) {
        return QStringLiteral("badgeOrange");
    }
    if (eventName.contains(QStringLiteral("started")) || eventName == QStringLiteral("connected")) {
        return QStringLiteral("badgeBlue");
    }
    return QStringLiteral("badgeGray");
}

QString prefixForTaskEvent(const QString &eventName, int sequence)
{
    if (eventName.contains(QStringLiteral("completed"))) {
        return QStringLiteral("✓");
    }
    if (eventName.contains(QStringLiteral("started")) || eventName == QStringLiteral("connected")) {
        return QStringLiteral("●");
    }
    return QString::number(sequence);
}

QString agentSummary(const QList<WorkflowStepInfo> &steps)
{
    QStringList agents;
    for (const WorkflowStepInfo &step : steps) {
        const QString name = agentDisplayName(step.agent);
        if (!agents.contains(name)) {
            agents.append(name);
        }
    }
    return agents.isEmpty() ? QStringLiteral("总指挥") : agents.join(QStringLiteral(" · "));
}

QString chatModeSummary(const ChatResult &result)
{
    // 后端决定当前回答来自 mock 还是真实模型；UI 只负责把模式转成用户能看懂的状态。
    if (result.mode == QStringLiteral("llm")) {
        return result.model.isEmpty() ? QStringLiteral("真实模型 · Commander")
                                      : QStringLiteral("%1 · Commander").arg(result.model);
    }
    return QStringLiteral("模拟模式 · Commander");
}

QString historyStatusText(const QString &status)
{
    if (status == QStringLiteral("pending")) {
        return QStringLiteral("待处理");
    }
    if (status == QStringLiteral("running")) {
        return QStringLiteral("进行中");
    }
    if (status == QStringLiteral("waiting_permission")) {
        return QStringLiteral("待确认");
    }
    if (status == QStringLiteral("completed")) {
        return QStringLiteral("已完成");
    }
    if (status == QStringLiteral("blocked")) {
        return QStringLiteral("已阻塞");
    }
    if (status == QStringLiteral("failed")) {
        return QStringLiteral("已失败");
    }
    if (status == QStringLiteral("cancelled")) {
        return QStringLiteral("已取消");
    }
    return status.isEmpty() ? QStringLiteral("未知") : status;
}

QString historyModeText(const QString &mode)
{
    if (mode == QStringLiteral("dry_run")) {
        return QStringLiteral("预演");
    }
    if (mode == QStringLiteral("runtime")) {
        return QStringLiteral("真实执行");
    }
    return mode.isEmpty() ? QStringLiteral("未知") : mode;
}

QString historyRiskText(const QString &riskLevel)
{
    if (riskLevel == QStringLiteral("low")) {
        return QStringLiteral("低风险");
    }
    if (riskLevel == QStringLiteral("medium")) {
        return QStringLiteral("中风险");
    }
    if (riskLevel == QStringLiteral("high")) {
        return QStringLiteral("高风险");
    }
    return riskLevel.isEmpty() ? QStringLiteral("未知") : riskLevel;
}

QString historyConfirmationLabelText(bool requiresConfirmation)
{
    return requiresConfirmation ? QStringLiteral("需确认") : QStringLiteral("无需确认");
}

bool isPlatformPolicyDecision(const RuntimePermissionItem &item)
{
    return item.decision.decidedBy.startsWith(QStringLiteral("platform_policy:"));
}

QString permissionPolicyActionText(const QString &action)
{
    if (action == QStringLiteral("allow")) {
        return QStringLiteral("自动批准");
    }
    if (action == QStringLiteral("confirm")) {
        return QStringLiteral("等待确认");
    }
    if (action == QStringLiteral("block")) {
        return QStringLiteral("平台阻止");
    }
    return action;
}

QString commandRuntimeRequestStatusText(const QString &status)
{
    if (status == QStringLiteral("ready")) {
        return QStringLiteral("可创建运行请求");
    }
    if (status == QStringLiteral("needs_approval")) {
        return QStringLiteral("等待用户批准");
    }
    if (status == QStringLiteral("blocked")) {
        return QStringLiteral("平台已阻止");
    }
    if (status == QStringLiteral("none")) {
        return QStringLiteral("无需创建请求");
    }
    return status.isEmpty() ? QStringLiteral("未定义") : status;
}

QString permissionDecisionSourceText(const QString &decidedBy)
{
    if (decidedBy.startsWith(QStringLiteral("platform_policy:"))) {
        return QStringLiteral("平台权限策略");
    }
    if (decidedBy == QStringLiteral("local_user")) {
        return QStringLiteral("本地用户");
    }
    return decidedBy.isEmpty() ? QStringLiteral("尚未决策") : decidedBy;
}

int dispatchProgressIndexForEvent(const QString &eventName)
{
    // 调度台右侧只保留 5 个概念阶段，不直接使用后端日志 sequence。
    // 这样无论事件多密集，用户看到的都会是稳定的“提交 / 规划 / 推进 / 确认 / 结论”。
    if (eventName == QStringLiteral("connected") || eventName == QStringLiteral("task_started")) {
        return 1;
    }
    if (eventName == QStringLiteral("confirmation_required")
        || eventName == QStringLiteral("permission_required")
        || eventName == QStringLiteral("permission_auto_approved")
        || eventName == QStringLiteral("permission_denied")) {
        return 4;
    }
    if (eventName == QStringLiteral("task_completed")
        || eventName == QStringLiteral("task_failed")
        || eventName == QStringLiteral("task_waiting")
        || eventName == QStringLiteral("task_cancelled")) {
        return 5;
    }
    if (eventName == QStringLiteral("step_started")
        || eventName == QStringLiteral("step_completed")
        || eventName == QStringLiteral("step_failed")
        || eventName == QStringLiteral("step_retried")) {
        return 3;
    }
    return 3;
}

QString dispatchStageTitle(int index)
{
    switch (index) {
    case 1:
        return QStringLiteral("任务提交");
    case 2:
        return QStringLiteral("Commander 规划");
    case 3:
        return QStringLiteral("Workflow 推进");
    case 4:
        return QStringLiteral("权限 / 产物");
    case 5:
        return QStringLiteral("当前结论");
    default:
        return QStringLiteral("任务");
    }
}

QString dispatchNextActionText(const QString &action)
{
    if (action == QStringLiteral("ask_clarifying_questions")) {
        return QStringLiteral("需要补充信息");
    }
    if (action == QStringLiteral("review_plan_and_confirm_permissions")) {
        return QStringLiteral("确认范围后回复“开始执行”");
    }
    if (action == QStringLiteral("execute_after_confirm")) {
        return QStringLiteral("低风险任务会自动执行");
    }
    if (action == QStringLiteral("open_data_workspace")) {
        return QStringLiteral("前往数据工作台继续");
    }
    if (action == QStringLiteral("review_combination_plan")) {
        return QStringLiteral("组合计划待 Runtime 支持");
    }
    return action.isEmpty() ? QStringLiteral("等待下一步") : action;
}

QString dispatchBudgetLevelText(const QString &level)
{
    if (level == QStringLiteral("low")) {
        return QStringLiteral("低");
    }
    if (level == QStringLiteral("medium")) {
        return QStringLiteral("中");
    }
    if (level == QStringLiteral("high")) {
        return QStringLiteral("高");
    }
    return level.isEmpty() ? QStringLiteral("未知") : level;
}

QString dispatchCompactListText(const QStringList &values, int maxItems = 3)
{
    if (values.isEmpty()) {
        return QStringLiteral("无");
    }

    QStringList escaped;
    const int count = qMin(values.size(), maxItems);
    escaped.reserve(count);
    for (int index = 0; index < count; ++index) {
        escaped.append(values.at(index).toHtmlEscaped());
    }
    if (values.size() > maxItems) {
        escaped.append(QStringLiteral("等 %1 项").arg(values.size()));
    }
    return escaped.join(QStringLiteral("、"));
}

QString dispatchBulletListHtml(const QStringList &values)
{
    if (values.isEmpty()) {
        return QString();
    }

    QString html = QStringLiteral("<ul style=\"margin:4px 0 0 18px;padding:0;\">");
    for (const QString &value : values) {
        html += QStringLiteral("<li>%1</li>").arg(value.toHtmlEscaped());
    }
    html += QStringLiteral("</ul>");
    return html;
}

QString permissionDecisionLabel(const QString &decision)
{
    if (decision == QStringLiteral("approved")) {
        return QStringLiteral("已批准");
    }
    if (decision == QStringLiteral("denied")) {
        return QStringLiteral("已拒绝");
    }
    return QStringLiteral("待确认");
}

QString permissionDecisionBadge(const QString &decision)
{
    if (decision == QStringLiteral("approved")) {
        return QStringLiteral("badgeGreen");
    }
    if (decision == QStringLiteral("denied")) {
        return QStringLiteral("badgeGray");
    }
    return QStringLiteral("badgeOrange");
}

QString compactBadgeText(const QString &text, int limit = 28)
{
    // 模型名未来可能很长，徽标只放短文本，完整值放到 tooltip 或详情区。
    if (text.size() <= limit) {
        return text;
    }
    return text.left(qMax(0, limit - 3)) + QStringLiteral("...");
}

QString compactPlainPreview(const QString &text, int limit = 180)
{
    // 文档预览只用于聊天区的轻提示，先压平换行，避免导入长文档后撑乱调度台。
    QString preview = text.simplified();
    if (preview.size() <= limit) {
        return preview;
    }
    return preview.left(qMax(0, limit - 1)).trimmed() + QStringLiteral("…");
}

QString documentRequirementCategoryText(const QString &category)
{
    if (category == QStringLiteral("functional")) {
        return QStringLiteral("功能");
    }
    if (category == QStringLiteral("output")) {
        return QStringLiteral("输出");
    }
    if (category == QStringLiteral("constraint")) {
        return QStringLiteral("约束");
    }
    if (category == QStringLiteral("acceptance")) {
        return QStringLiteral("验收");
    }
    return QStringLiteral("待确认");
}

QString documentPriorityText(const QString &priority)
{
    if (priority == QStringLiteral("must")) {
        return QStringLiteral("必须");
    }
    if (priority == QStringLiteral("should")) {
        return QStringLiteral("建议");
    }
    if (priority == QStringLiteral("could")) {
        return QStringLiteral("可选");
    }
    return QStringLiteral("待判断");
}

QString documentBriefFieldText(const QString &key)
{
    // 后端 key 是稳定协议；这里集中映射客户可读中文，避免结果渲染散落英文业务字段。
    if (key == QStringLiteral("subject")) {
        return QStringLiteral("材料主题");
    }
    if (key == QStringLiteral("purpose")) {
        return QStringLiteral("目标与目的");
    }
    if (key == QStringLiteral("scope")) {
        return QStringLiteral("范围边界");
    }
    if (key == QStringLiteral("stakeholders")) {
        return QStringLiteral("相关角色");
    }
    if (key == QStringLiteral("deliverables")) {
        return QStringLiteral("交付物");
    }
    if (key == QStringLiteral("milestones")) {
        return QStringLiteral("时间与节点");
    }
    if (key == QStringLiteral("risks")) {
        return QStringLiteral("风险与依赖");
    }
    return QStringLiteral("关键信息");
}

QString documentSourceLocationText(const QJsonObject &source)
{
    // 新格式会携带页码/段落定位；旧任务只有行号时仍按原显示，保证历史记录不会突然变空。
    const QString locator = source.value(QStringLiteral("source_locator")).toString().trimmed();
    if (!locator.isEmpty()) {
        return locator;
    }
    const int startLine = source.value(QStringLiteral("start_line")).toInt();
    const int endLine = source.value(QStringLiteral("end_line")).toInt();
    return startLine == endLine ? QStringLiteral("第 %1 行").arg(startLine)
                                : QStringLiteral("第 %1-%2 行").arg(startLine).arg(endLine);
}

QString documentSourceRefsText(const QJsonArray &sourceRefs)
{
    QStringList references;
    references.reserve(sourceRefs.size());
    for (const QJsonValue &value : sourceRefs) {
        const QJsonObject source = value.toObject();
        const QString path = source.value(QStringLiteral("relative_path")).toString();
        if (path.isEmpty()) {
            continue;
        }
        references.append(QStringLiteral("%1 · %2").arg(path, documentSourceLocationText(source)));
    }
    return references.join(QStringLiteral("；"));
}

QString capabilityText(bool supported)
{
    return supported ? QStringLiteral("支持") : QStringLiteral("暂不支持");
}

QString historyArtifactDisplayText(const WorkflowArtifactInfo &artifact)
{
    QStringList parts;
    parts.append(artifact.name.isEmpty() ? QStringLiteral("未命名产物") : artifact.name);
    if (!artifact.kind.isEmpty()) {
        parts.append(artifact.kind);
    }
    if (!artifact.stepId.isEmpty()) {
        parts.append(artifact.stepId);
    }
    return parts.join(QStringLiteral(" · "));
}

QString historyArtifactTooltipText(const WorkflowArtifactInfo &artifact)
{
    QStringList lines;
    lines.append(QStringLiteral("名称：%1").arg(artifact.name.isEmpty() ? QStringLiteral("未命名产物")
                                                                      : artifact.name));
    lines.append(QStringLiteral("类型：%1").arg(artifact.kind.isEmpty() ? QStringLiteral("other") : artifact.kind));
    lines.append(QStringLiteral("Agent：%1").arg(agentDisplayName(artifact.agentId)));
    lines.append(QStringLiteral("步骤：%1").arg(artifact.stepId.isEmpty() ? QStringLiteral("未知步骤")
                                                                       : artifact.stepId));
    lines.append(QStringLiteral("URI：%1").arg(artifact.uri.isEmpty() ? QStringLiteral("未提供") : artifact.uri));
    if (!artifact.summary.isEmpty()) {
        lines.append(QStringLiteral("摘要：%1").arg(artifact.summary));
    }
    return lines.join(QStringLiteral("\n"));
}

bool historyArtifactLooksTextLike(const WorkflowArtifactInfo &artifact, const QFileInfo &fileInfo)
{
    const QString mimeType = artifact.mimeType.trimmed().toLower();
    if (mimeType.startsWith(QStringLiteral("text/"))
        || mimeType.contains(QStringLiteral("json"))
        || mimeType.contains(QStringLiteral("xml"))
        || mimeType.contains(QStringLiteral("yaml"))
        || mimeType.contains(QStringLiteral("markdown"))) {
        return true;
    }

    const QString kind = artifact.kind.trimmed().toLower();
    if (kind.contains(QStringLiteral("text"))
        || kind.contains(QStringLiteral("markdown"))
        || kind.contains(QStringLiteral("report"))
        || kind.contains(QStringLiteral("code"))) {
        return true;
    }

    // 后端可能只给 output_path，未给准确 MIME；这里用常见源码/文档扩展名兜底。
    const QString suffix = fileInfo.suffix().toLower();
    static const QStringList textSuffixes = {
        QStringLiteral("txt"), QStringLiteral("md"), QStringLiteral("markdown"),
        QStringLiteral("json"), QStringLiteral("yaml"), QStringLiteral("yml"),
        QStringLiteral("py"), QStringLiteral("cpp"), QStringLiteral("c"),
        QStringLiteral("h"), QStringLiteral("hpp"), QStringLiteral("js"),
        QStringLiteral("ts"), QStringLiteral("tsx"), QStringLiteral("jsx"),
        QStringLiteral("html"), QStringLiteral("css"), QStringLiteral("csv"),
        QStringLiteral("log")
    };
    return textSuffixes.contains(suffix);
}

QString historyArtifactSourceText(const WorkflowArtifactInfo &artifact, const QString &localPath)
{
    if (!localPath.isEmpty()) {
        if (artifact.metadata.value(QStringLiteral("output_scope")).toString()
            == QStringLiteral("document_drafts")) {
            return QStringLiteral("用户确认保存的 Markdown 草稿");
        }
        return QStringLiteral("真实 runtime outputs 文件");
    }
    if (artifact.uri.startsWith(QStringLiteral("artifact://dry-run/"))) {
        return QStringLiteral("dry-run 虚拟产物");
    }
    if (artifact.uri.startsWith(QStringLiteral("memory://"))) {
        return QStringLiteral("内存型产物");
    }
    if (artifact.uri.startsWith(QStringLiteral("agentflow-output://"))) {
        return QStringLiteral("runtime 产物引用");
    }
    return QStringLiteral("外部或未知产物引用");
}

QString historyArtifactOpenUnavailableText(const WorkflowArtifactInfo &artifact, const QString &localPath)
{
    if (!localPath.isEmpty()) {
        if (artifact.metadata.value(QStringLiteral("output_scope")).toString()
            == QStringLiteral("document_drafts")) {
            return QStringLiteral("后端提供了受控草稿路径，但文件当前不存在或不可访问。");
        }
        return QStringLiteral("后端提供了受控 outputs 路径，但文件当前不存在或不可访问。");
    }
    if (artifact.uri.startsWith(QStringLiteral("artifact://dry-run/"))) {
        return QStringLiteral("dry-run 只生成虚拟产物，没有可打开的本地文件。");
    }
    if (artifact.uri.startsWith(QStringLiteral("memory://"))) {
        return QStringLiteral("内存型产物尚未写入磁盘，只能查看预览和元数据。");
    }
    if (artifact.uri.startsWith(QStringLiteral("agentflow-output://"))) {
        return QStringLiteral("runtime 产物缺少后端声明的受控本地路径。");
    }
    return QStringLiteral("当前产物没有可安全打开的本地文件。");
}

QString historyRuntimeStageHint(const QString &mode, const QString &status, bool terminal)
{
    if (mode == QStringLiteral("dry_run")) {
        if (status == QStringLiteral("completed")) {
            return QStringLiteral("预演已完成，可转真实执行");
        }
        if (status == QStringLiteral("running") || status == QStringLiteral("pending")) {
            return QStringLiteral("正在预演计划");
        }
        if (status == QStringLiteral("failed")) {
            return QStringLiteral("预演失败，可查看日志后重试");
        }
        return QStringLiteral("预演任务");
    }

    if (status == QStringLiteral("waiting_permission")) {
        return QStringLiteral("等待权限确认");
    }
    if (status == QStringLiteral("blocked")) {
        return QStringLiteral("执行已阻塞，需要处理后继续或重试");
    }
    if (status == QStringLiteral("running") || status == QStringLiteral("pending")) {
        return QStringLiteral("真实执行中");
    }
    if (status == QStringLiteral("completed")) {
        return QStringLiteral("真实执行完成");
    }
    if (status == QStringLiteral("failed")) {
        return QStringLiteral("真实执行失败，请查看错误和工具调用");
    }
    if (status == QStringLiteral("cancelled")) {
        return QStringLiteral("任务已取消");
    }
    return terminal ? QStringLiteral("任务已结束") : QStringLiteral("任务仍可更新");
}

QString dispatchStateActionHint(const QString &mode, const QString &status)
{
    // 状态名只说明“发生了什么”，这里补充用户下一步，避免调度台变成只会刷日志的黑盒。
    if (mode == QStringLiteral("dry_run")) {
        if (status == QStringLiteral("completed")) {
            return QStringLiteral("预演已完成。请先审查计划，再点击「开始执行」进入真实 Runtime。");
        }
        if (status == QStringLiteral("failed")) {
            return QStringLiteral("预演失败。请进入历史任务查看错误日志，修正输入后再重试。");
        }
        if (status == QStringLiteral("cancelled")) {
            return QStringLiteral("预演已取消。可以修改任务描述后重新提交。");
        }
        return QString();
    }

    if (status == QStringLiteral("waiting_permission")) {
        return QStringLiteral("真实执行已暂停。点击「处理权限」进入历史页审查并决定是否批准。");
    }
    if (status == QStringLiteral("completed")) {
        return QStringLiteral("真实执行已完成。点击「查看产物」检查结果和回读验证。");
    }
    if (status == QStringLiteral("blocked")) {
        return QStringLiteral("真实执行已阻塞。请进入历史页查看权限、工具错误和可继续动作。");
    }
    if (status == QStringLiteral("failed")) {
        return QStringLiteral("真实执行失败。请进入历史页查看失败步骤、工具调用和重试建议。");
    }
    if (status == QStringLiteral("cancelled")) {
        return QStringLiteral("真实执行已取消，已完成步骤和审计记录仍可在历史页查看。");
    }
    return QString();
}

QString historyEvaluationOutcomeText(const QString &outcome)
{
    if (outcome == QStringLiteral("dry_run_ready")) {
        return QStringLiteral("预演通过");
    }
    if (outcome == QStringLiteral("completed")) {
        return QStringLiteral("执行完成");
    }
    if (outcome == QStringLiteral("waiting_permission")) {
        return QStringLiteral("等待权限");
    }
    if (outcome == QStringLiteral("blocked")) {
        return QStringLiteral("已阻塞");
    }
    if (outcome == QStringLiteral("failed")) {
        return QStringLiteral("执行失败");
    }
    if (outcome == QStringLiteral("cancelled")) {
        return QStringLiteral("已取消");
    }
    if (outcome == QStringLiteral("running")) {
        return QStringLiteral("执行中");
    }
    return outcome.isEmpty() ? QStringLiteral("待评估") : outcome;
}

void polishBadge(QLabel *label, const QString &objectName)
{
    if (!label) {
        return;
    }

    // 进度标签需要动态切换 QSS objectName，切换后必须重新 polish 才会立即生效。
    label->setObjectName(objectName);
    label->style()->unpolish(label);
    label->style()->polish(label);
    label->update();
}

void restoreDocumentReviewDialogGeometry(QDialog *dialog, const QString &reviewKind)
{
    if (!dialog) {
        return;
    }

    // 报告内容和任务状态都留在后端；本机只保存窗口几何，避免客户每次打开长报告都要重新拖拽。
    // restoreGeometry 失败时使用固定首开尺寸，同时仍由对话框自身的最小尺寸约束可读性。
    QSettings settings(QStringLiteral("AgentFlow"), QStringLiteral("AgentFlow"));
    settings.beginGroup(QStringLiteral("ui/documentReviewDialogs"));
    const QByteArray savedGeometry = settings.value(reviewKind).toByteArray();
    if (savedGeometry.isEmpty() || !dialog->restoreGeometry(savedGeometry)) {
        dialog->resize(980, 720);
    }
    settings.endGroup();

    QObject::connect(dialog, &QDialog::finished, dialog, [dialog, reviewKind]() {
        QSettings settings(QStringLiteral("AgentFlow"), QStringLiteral("AgentFlow"));
        settings.beginGroup(QStringLiteral("ui/documentReviewDialogs"));
        settings.setValue(reviewKind, dialog->saveGeometry());
        settings.endGroup();
    });
}

QFrame *createDocumentReviewInspector(const QJsonObject &report, QWidget *parent)
{
    auto *inspector = new QFrame(parent);
    inspector->setObjectName(QStringLiteral("reviewEvidenceInspector"));
    inspector->setMinimumWidth(230);
    inspector->setMaximumWidth(310);
    auto *layout = new QVBoxLayout(inspector);
    layout->setContentsMargins(14, 14, 14, 14);
    layout->setSpacing(8);

    auto *title = new QLabel(QStringLiteral("审查概览"), inspector);
    title->setObjectName(QStringLiteral("reviewInspectorTitle"));
    auto *hint = new QLabel(QStringLiteral("只读汇总 · 完整结论与依据在左侧"), inspector);
    hint->setObjectName(QStringLiteral("reviewInspectorHint"));
    hint->setWordWrap(true);
    layout->addWidget(title);
    layout->addWidget(hint);

    const QJsonArray findings = report.value(QStringLiteral("findings")).toArray();
    const QJsonArray checks = report.value(QStringLiteral("checks")).toArray();
    int highPriorityCount = 0;
    int mediumPriorityCount = 0;
    for (const QJsonValue &value : findings) {
        const QString severity = value.toObject().value(QStringLiteral("severity")).toString();
        if (severity == QStringLiteral("high")) {
            ++highPriorityCount;
        } else if (severity == QStringLiteral("medium")) {
            ++mediumPriorityCount;
        }
    }

    QStringList metricLines;
    metricLines.append(findings.isEmpty() ? QStringLiteral("待处理问题：未发现")
                                          : QStringLiteral("待处理问题：%1 项").arg(findings.size()));
    if (highPriorityCount > 0) {
        metricLines.append(QStringLiteral("高优先级：%1 项").arg(highPriorityCount));
    }
    if (mediumPriorityCount > 0) {
        metricLines.append(QStringLiteral("中优先级：%1 项").arg(mediumPriorityCount));
    }
    metricLines.append(QStringLiteral("规则检查：%1 项").arg(checks.size()));
    auto *metrics = new QLabel(metricLines.join(QStringLiteral("\n")), inspector);
    metrics->setObjectName(QStringLiteral("reviewInspectorMetrics"));
    metrics->setWordWrap(true);
    layout->addWidget(metrics);

    QSet<QString> sourceSet;
    QStringList sourceLines;
    const auto appendSources = [&sourceSet, &sourceLines](const QJsonArray &items) {
        for (const QJsonValue &itemValue : items) {
            const QJsonObject item = itemValue.toObject();
            for (const QJsonValue &sourceValue : item.value(QStringLiteral("source_refs")).toArray()) {
                const QJsonObject source = sourceValue.toObject();
                const QString path = source.value(QStringLiteral("relative_path")).toString().trimmed();
                if (path.isEmpty()) {
                    continue;
                }
                QString locator = source.value(QStringLiteral("source_locator")).toString().trimmed();
                if (locator.isEmpty()) {
                    const int startLine = source.value(QStringLiteral("start_line")).toInt();
                    const int endLine = source.value(QStringLiteral("end_line")).toInt();
                    if (startLine > 0) {
                        locator = endLine > startLine ? QStringLiteral("第 %1-%2 行").arg(startLine).arg(endLine)
                                                       : QStringLiteral("第 %1 行").arg(startLine);
                    }
                }
                const QString sourceText = locator.isEmpty() ? path : QStringLiteral("%1 · %2").arg(path, locator);
                if (sourceSet.contains(sourceText)) {
                    continue;
                }
                sourceSet.insert(sourceText);
                sourceLines.append(sourceText);
            }
        }
    };
    appendSources(findings);
    appendSources(checks);

    auto *sourceCaption = new QLabel(QStringLiteral("来源范围"), inspector);
    sourceCaption->setObjectName(QStringLiteral("reviewInspectorCaption"));
    auto *sources = new QTextBrowser(inspector);
    sources->setObjectName(QStringLiteral("reviewInspectorSources"));
    sources->setOpenExternalLinks(false);
    sources->setMinimumHeight(120);
    sources->setHtml(
        sourceLines.isEmpty()
            ? QStringLiteral("<p style=\"margin:0;color:#64748B;\">本轮没有可展示的来源锚点。</p>")
            : QStringLiteral("<ul style=\"margin:0;padding-left:18px;\">%1</ul>")
                  .arg([&sourceLines]() {
                      QStringList items;
                      items.reserve(sourceLines.size());
                      for (const QString &line : sourceLines) {
                          items.append(QStringLiteral("<li>%1</li>").arg(line.toHtmlEscaped()));
                      }
                      return items.join(QString());
                  }()));
    layout->addWidget(sourceCaption);
    layout->addWidget(sources, 1);
    return inspector;
}

} // namespace

MainWindow::MainWindow(QWidget *parent)
    : QMainWindow(parent)
    , ui(new Ui::MainWindow)
    , backendClient(nullptr)
    , backendManager(nullptr)
{
    ui->setupUi(this);
    // `uic` 不支持在此工程的 .ui item 上写 stretch 属性。布局层级仍由 Designer
    // 维护，这里只补运行时伸缩权重：对话正文拿到可用空间，Composer/材料条保持稳定高度。
    ui->dispatchPageLayout->setStretch(1, 1);
    ui->dispatchBodyLayout->setStretch(0, 1);
    ui->dispatchChatLayout->setStretch(1, 1);
    // 调度台的低风险只读任务会自动执行；写入型任务则由客户在对话中回复“开始执行”
    // 触发。隐藏旧的固定主按钮，避免客户误以为每句话都要走“计划 -> 点击按钮”的流程。
    ui->dispatchExecuteButton->setVisible(false);
    // 过程面板可按需展开；默认让对话和最终结果占用主工作区，避免固定侧栏长期挤压内容。
    ui->dispatchProgressPanel->setVisible(false);
    // Designer 历史页面曾把“新建资料库”误标为次级按钮。它是资料库空状态的唯一主操作，
    // 在对象树完成后统一更正语义并刷新 QSS，避免客户看到无色或与主流程相同的弱按钮。
    ui->knowledgeCreateButton->setObjectName(QStringLiteral("primaryButton"));
    ui->knowledgeCreateButton->style()->unpolish(ui->knowledgeCreateButton);
    ui->knowledgeCreateButton->style()->polish(ui->knowledgeCreateButton);
    // 先完成 UI 对象树创建，再初始化网络客户端和后端进程管家。
    // 这样启动期的 Qt Network/TLS 插件加载不会和 uic 生成的控件初始化交织在一起。
    backendClient = new BackendClient(this);
    backendManager = new BackendManager(this);

    // 固定侧边栏布局到顶部，避免折叠下方分组时上方“工作台”跟着移动。
    ui->sidebarLayout->setAlignment(Qt::AlignTop);
    resize(1500, 940);

    setupNavigation();
    setupBackendIntegration();
    setupDispatchChat();
    setupDocumentAgent();
    setupDataWorkspace();
    setupKnowledgeBase();
    setupCodeWorkshop();
    setupModelPage();
    setupSettingsPage();
    setupMcpConnectionsPage();
    setupHistoryPage();

    // Qt 6.11 + MSVC Debug 下，给大量 QFrame 动态挂 QGraphicsDropShadowEffect
    // 在退出销毁期曾触发 CRT heap corruption。核心视觉仍由 mainwindow.ui 的 QSS 控制，
    // 这里先关闭运行时阴影增强，优先保证用户能稳定启动和退出。

    switchPage(0);
    setBackendConnectingState();
    backendManager->ensureStarted();
}

MainWindow::~MainWindow()
{
    // 退出时先断开所有发往 MainWindow 的异步信号，再释放 UI。
    // 否则 QProcess / QNetworkReply / QWebSocket 在析构或关闭过程中如果补发信号，
    // 就可能访问已经由 delete ui 清理掉的控件。
    if (backendClient) {
        QObject::disconnect(backendClient, nullptr, this, nullptr);
        delete backendClient;
        backendClient = nullptr;
    }
    if (backendManager) {
        QObject::disconnect(backendManager, nullptr, this, nullptr);
        // 只停止本窗口自动启动的后端；用户手动启动的后端由 BackendManager::stop() 自己保护。
        backendManager->stopForFastExit();
        delete backendManager;
        backendManager = nullptr;
    }
    delete ui;
    ui = nullptr;
}

void MainWindow::setupNavigation()
{
    // 文本由 Designer 保存一次；折叠态只显示图标，Tooltip 仍提供入口名称，避免把可发现性
    // 换成单纯省宽度。这里不复制导航按钮，所有页面入口仍复用同一套信号和 active 状态。
    for (QPushButton *button : navigationButtons()) {
        button->setProperty("fullNavText", button->text());
    }

    loadNavigationPresentationPreferences();

    connect(ui->navOverviewButton, &QPushButton::clicked, this, [this]() { switchPage(0); });
    connect(ui->navDispatchButton, &QPushButton::clicked, this, [this]() { switchPage(1); });
    connect(ui->navAppsButton, &QPushButton::clicked, this, [this]() { switchPage(2); });
    connect(ui->navWorkflowButton, &QPushButton::clicked, this, [this]() { switchPage(3); });
    connect(ui->navDocumentButton, &QPushButton::clicked, this, [this]() { switchPage(4); });
    connect(ui->navCodeButton, &QPushButton::clicked, this, [this]() { switchPage(5); });
    connect(ui->navDataButton, &QPushButton::clicked, this, [this]() { switchPage(6); });
    connect(ui->navVisionButton, &QPushButton::clicked, this, [this]() { switchPage(7); });
    connect(ui->navVideoButton, &QPushButton::clicked, this, [this]() { switchPage(8); });
    connect(ui->navKnowledgeButton, &QPushButton::clicked, this, [this]() { switchPage(9); });
    connect(ui->navPluginsButton, &QPushButton::clicked, this, [this]() { switchPage(10); });
    connect(ui->navModelButton, &QPushButton::clicked, this, [this]() { switchPage(11); });
    connect(ui->navHistoryButton, &QPushButton::clicked, this, [this]() { switchPage(12); });
    connect(ui->navSettingsButton, &QPushButton::clicked, this, [this]() { switchPage(13); });

    connect(ui->sidebarCollapseButton, &QToolButton::clicked, this, [this]() {
        setSidebarCollapsed(!sidebarCollapsed);
    });

    connect(ui->navGroupAgentsButton, &QPushButton::clicked, this, [this]() {
        agentsNavigationExpanded = !agentsNavigationExpanded;
        updateSidebarNavigationPresentation();
        saveNavigationPresentationPreferences();
    });

    connect(ui->navGroupManageButton, &QPushButton::clicked, this, [this]() {
        managementNavigationExpanded = !managementNavigationExpanded;
        updateSidebarNavigationPresentation();
        saveNavigationPresentationPreferences();
    });

    updateSidebarNavigationPresentation();
}

void MainWindow::loadNavigationPresentationPreferences()
{
    // 导航布局属于本机的低风险界面偏好。显式使用独立的应用/组织名，避免与后端的
    // 运行时配置、模型密钥或任务数据混在同一份设置中。
    QSettings settings(QStringLiteral("AgentFlow"), QStringLiteral("AgentFlow"));
    settings.beginGroup(QStringLiteral("ui/navigation"));
    sidebarCollapsed = settings.value(QStringLiteral("collapsed"), false).toBool();
    agentsNavigationExpanded = settings.value(QStringLiteral("agentsExpanded"), true).toBool();
    managementNavigationExpanded = settings.value(QStringLiteral("managementExpanded"), true).toBool();
    settings.endGroup();
}

void MainWindow::saveNavigationPresentationPreferences() const
{
    // 只在用户点击折叠或分组时写入，既让工作台下次恢复上次的信息密度，也不把设置写入
    // 高频 UI 刷新路径，避免任何可感知的交互阻塞。
    QSettings settings(QStringLiteral("AgentFlow"), QStringLiteral("AgentFlow"));
    settings.beginGroup(QStringLiteral("ui/navigation"));
    settings.setValue(QStringLiteral("collapsed"), sidebarCollapsed);
    settings.setValue(QStringLiteral("agentsExpanded"), agentsNavigationExpanded);
    settings.setValue(QStringLiteral("managementExpanded"), managementNavigationExpanded);
    settings.endGroup();
}

QList<QPushButton *> MainWindow::navigationButtons() const
{
    return {
        ui->navOverviewButton,
        ui->navDispatchButton,
        ui->navAppsButton,
        ui->navWorkflowButton,
        ui->navDocumentButton,
        ui->navCodeButton,
        ui->navDataButton,
        ui->navVisionButton,
        ui->navVideoButton,
        ui->navKnowledgeButton,
        ui->navPluginsButton,
        ui->navModelButton,
        ui->navHistoryButton,
        ui->navSettingsButton
    };
}

void MainWindow::setSidebarCollapsed(bool collapsed)
{
    if (sidebarCollapsed == collapsed) {
        return;
    }

    sidebarCollapsed = collapsed;
    updateSidebarNavigationPresentation();
    saveNavigationPresentationPreferences();
}

void MainWindow::updateSidebarNavigationPresentation()
{
    const int sidebarWidth = sidebarCollapsed ? 76 : 270;
    ui->sidebarFrame->setMinimumWidth(sidebarWidth);
    ui->sidebarFrame->setMaximumWidth(sidebarWidth);
    ui->sidebarLayout->setContentsMargins(sidebarCollapsed ? 12 : 18,
                                          22,
                                          sidebarCollapsed ? 12 : 18,
                                          18);

    // 76px 图标轨无法同时容纳 50px 品牌标识和 32px 展开按钮。折叠时保留唯一、居中的
    // 方向按钮；展开后再恢复完整品牌，避免控件被挤出侧栏或遮挡。
    ui->logoLabel->setVisible(!sidebarCollapsed);
    ui->brandTitleLabel->setVisible(!sidebarCollapsed);
    ui->brandSubLabel->setVisible(!sidebarCollapsed);
    ui->brandCollapseSpacer->changeSize(sidebarCollapsed ? 0 : 12, 20);
    ui->brandRowLayout->setAlignment(ui->sidebarCollapseButton,
                                     sidebarCollapsed ? Qt::AlignHCenter : Qt::AlignRight);
    ui->proCard->setVisible(!sidebarCollapsed);
    ui->navGroupMainButton->setVisible(!sidebarCollapsed);
    ui->navGroupAgentsButton->setVisible(!sidebarCollapsed);
    ui->navGroupManageButton->setVisible(!sidebarCollapsed);
    ui->sidebarCollapseButton->setArrowType(sidebarCollapsed ? Qt::RightArrow : Qt::LeftArrow);
    ui->sidebarCollapseButton->setToolTip(sidebarCollapsed ? QStringLiteral("展开导航")
                                                           : QStringLiteral("收起导航"));
    ui->navGroupAgentsButton->setText(
        agentsNavigationExpanded ? QStringLiteral("▾  智能应用") : QStringLiteral("▸  智能应用"));
    ui->navGroupManageButton->setText(
        managementNavigationExpanded ? QStringLiteral("▾  系统管理") : QStringLiteral("▸  系统管理"));

    const QList<QPushButton *> agentButtons = {
        ui->navDocumentButton,
        ui->navCodeButton,
        ui->navDataButton,
        ui->navVisionButton,
        ui->navVideoButton,
        ui->navKnowledgeButton
    };
    const QList<QPushButton *> managementButtons = {
        ui->navPluginsButton,
        ui->navModelButton,
        ui->navHistoryButton,
        ui->navSettingsButton
    };

    for (QPushButton *button : navigationButtons()) {
        const bool visible = sidebarCollapsed
                             || (!agentButtons.contains(button) || agentsNavigationExpanded)
                                    && (!managementButtons.contains(button) || managementNavigationExpanded);
        button->setVisible(visible);
        const QString fullText = button->property("fullNavText").toString();
        button->setText(sidebarCollapsed ? QString() : fullText);
        button->setToolTip(sidebarCollapsed ? fullText.trimmed() : QString());
        button->setProperty("compactNav", sidebarCollapsed);
        button->style()->unpolish(button);
        button->style()->polish(button);
        button->update();
    }

    // 让窄屏图标轨与展开分组分别按自身内容重新计算，避免切换时中央工作区短暂抖动。
    ui->sidebarLayout->invalidate();
    ui->sidebarFrame->updateGeometry();
}

void MainWindow::setupBackendIntegration()
{
    // BackendManager 只负责本地进程生命周期；后端可用后再交给 BackendClient 拉业务数据。
    connect(ui->backendRetryButton, &QPushButton::clicked, this, [this]() {
        if (!backendManager || backendManager->isReady()) {
            return;
        }

        // 客户主动重试时立即给出可见反馈。retry() 仍由 BackendManager 负责端口探测、
        // 进程归属和单飞行健康检查，主窗口不直接操作 QProcess 或网络对象。
        ui->backendRetryButton->setEnabled(false);
        ui->backendRetryButton->setText(QStringLiteral("重试中"));
        setBackendConnectingState();
        backendManager->retry();
    });
    connect(backendManager, &BackendManager::starting, this, [this](const QString &message) {
        ui->backendRetryButton->setVisible(false);
        ui->backendRetryButton->setEnabled(true);
        ui->backendRetryButton->setText(QStringLiteral("重试后端"));
        ui->ambientTextLabel->setText(QStringLiteral("AgentFlow · %1").arg(message));
        ui->ambientStatusBadge->setText(QStringLiteral("启动中"));
        ui->apiUsageLabel->setText(QStringLiteral("后端服务：%1").arg(message));
        ui->taskUsageLabel->setText(QStringLiteral("Agent 注册表：等待后端就绪"));
        ui->apiUsageProgress->setValue(10);
    });
    connect(backendManager, &BackendManager::ready, this, [this](const QString &message) {
        ui->backendRetryButton->setVisible(false);
        ui->backendRetryButton->setEnabled(true);
        ui->backendRetryButton->setText(QStringLiteral("重试后端"));
        ui->ambientTextLabel->setText(QStringLiteral("AgentFlow · %1 · 正在加载 Agent 注册表").arg(message));
        ui->ambientStatusBadge->setText(QStringLiteral("后端就绪"));
        ui->apiUsageLabel->setText(QStringLiteral("后端服务：%1").arg(message));
        ui->taskUsageLabel->setText(QStringLiteral("Agent 注册表：正在加载"));
        ui->apiUsageProgress->setValue(30);
        backendClient->refresh();
        backendClient->requestNodeContracts();
        backendClient->requestRuntimePreferences();
        backendClient->requestMcpConnections();
        // 文档工作台的加载状态由统一入口维护。这样首启失败、后端重启或客户首次进入页面时
        // 都能清楚显示可恢复状态，而不是留下“等待后端加载文档”的静态占位。
        refreshDocumentAgentDocuments();
        // 若客户在启动期已经打开材料选择器，后端就绪后自动继续同步目录；不会要求客户
        // 关闭窗口、再点一次，也不会提前读取正文或触发数据画像。
        if (dispatchMaterialDialog && dispatchMaterialDialog->isVisible()) {
            refreshDispatchMaterialCatalog();
        }
        // 客户可以在本地后端仍在启动时先写好任务。健康检查通过后才真正发 HTTP，避免
        // 把请求交给尚未监听的端口并等到超时；材料范围已在点击发送时冻结。
        flushQueuedDispatchMessage();
        requestDispatchConversationContext();
    });
    connect(backendManager, &BackendManager::unavailable, this, [this](const QString &message) {
        restoreQueuedDispatchMessage(message);
        ui->backendRetryButton->setVisible(true);
        ui->backendRetryButton->setEnabled(true);
        ui->backendRetryButton->setText(QStringLiteral("重试后端"));
        updateBackendHealth(false, QStringLiteral("自动启动失败 · %1").arg(message));
    });
    connect(backendManager, &BackendManager::outputReceived, this, [this](const QString &line) {
        // 先把后端启动日志压缩显示在总览状态区；后面有独立日志面板后可直接复用此信号。
        ui->taskUsageLabel->setText(QStringLiteral("后端启动日志：%1").arg(line.left(96)));
    });
    connect(backendManager, &BackendManager::stopped, this, [this](const QString &message) {
        restoreQueuedDispatchMessage(message);
        ui->backendRetryButton->setVisible(true);
        ui->backendRetryButton->setEnabled(true);
        ui->backendRetryButton->setText(QStringLiteral("重试后端"));
        updateBackendHealth(false, message);
    });

    // MainWindow 只订阅 BackendClient 信号；HTTP/WebSocket 细节集中在 BackendClient。
    connect(backendClient, &BackendClient::healthChecked, this, &MainWindow::updateBackendHealth);
    connect(backendClient, &BackendClient::agentsLoaded, this, &MainWindow::updateAgentCards);
    connect(backendClient, &BackendClient::chatCompleted, this, &MainWindow::handleChatCompleted);
    connect(backendClient, &BackendClient::chatFailed, this, &MainWindow::handleChatFailed);
    connect(backendClient,
            &BackendClient::conversationContextReceived,
            this,
            &MainWindow::handleDispatchConversationContext);
    connect(backendClient,
            &BackendClient::conversationContextFailed,
            this,
            &MainWindow::handleDispatchConversationContextFailed);
    connect(backendClient,
            &BackendClient::conversationSessionsReceived,
            this,
            &MainWindow::handleDispatchConversationSessions);
    connect(backendClient,
            &BackendClient::conversationSessionsFailed,
            this,
            &MainWindow::handleDispatchConversationSessionsFailed);
    connect(backendClient,
            &BackendClient::conversationTranscriptReceived,
            this,
            &MainWindow::handleDispatchConversationTranscript);
    connect(backendClient,
            &BackendClient::conversationTranscriptFailed,
            this,
            &MainWindow::handleDispatchConversationTranscriptFailed);
    connect(backendClient, &BackendClient::taskLogReceived, this, &MainWindow::handleTaskLogReceived);
    connect(backendClient, &BackendClient::taskLogFinished, this, &MainWindow::handleTaskLogFinished);
    connect(backendClient, &BackendClient::taskLogFailed, this, &MainWindow::handleTaskLogFailed);
    connect(backendClient, &BackendClient::taskHistoryReceived, this, &MainWindow::handleTaskHistoryReceived);
    connect(backendClient, &BackendClient::taskHistoryFailed, this, &MainWindow::handleTaskHistoryFailed);
    connect(backendClient, &BackendClient::taskPlanReceived, this, &MainWindow::handleTaskPlanReceived);
    connect(backendClient, &BackendClient::taskPlanFailed, this, &MainWindow::handleTaskPlanFailed);
    connect(backendClient, &BackendClient::taskMemoryProposalsReceived, this, &MainWindow::handleTaskMemoryProposalsReceived);
    connect(backendClient, &BackendClient::taskMemoryProposalsFailed, this, &MainWindow::handleTaskMemoryProposalsFailed);
    connect(backendClient, &BackendClient::taskMemoryProposalConfirmed, this, &MainWindow::handleTaskMemoryProposalConfirmed);
    connect(backendClient, &BackendClient::taskMemoryProposalConfirmFailed, this, &MainWindow::handleTaskMemoryProposalConfirmFailed);
    connect(backendClient, &BackendClient::taskPlanVersionsReceived, this, &MainWindow::handleDispatchPlanVersionsReceived);
    connect(backendClient, &BackendClient::taskPlanVersionsFailed, this, &MainWindow::handleDispatchPlanVersionsFailed);
    connect(backendClient, &BackendClient::taskPlanVersionReceived, this, &MainWindow::handleDispatchPlanVersionReceived);
    connect(backendClient, &BackendClient::taskPlanVersionFailed, this, &MainWindow::handleDispatchPlanVersionFailed);
    connect(backendClient, &BackendClient::taskPlanRevisionCompleted, this, &MainWindow::handleDispatchPlanRevisionCompleted);
    connect(backendClient, &BackendClient::taskPlanRevisionFailed, this, &MainWindow::handleDispatchPlanRevisionFailed);
    connect(backendClient, &BackendClient::taskStepsReceived, this, &MainWindow::handleTaskStepsReceived);
    connect(backendClient, &BackendClient::taskStepsFailed, this, &MainWindow::handleTaskStepsFailed);
    connect(backendClient, &BackendClient::taskRuntimeStateReceived, this, &MainWindow::handleTaskRuntimeStateReceived);
    connect(backendClient, &BackendClient::taskRuntimeStateFailed, this, &MainWindow::handleTaskRuntimeStateFailed);
    connect(backendClient, &BackendClient::taskMetricsReceived, this, &MainWindow::handleTaskMetricsReceived);
    connect(backendClient, &BackendClient::taskMetricsFailed, this, &MainWindow::handleTaskMetricsFailed);
    connect(backendClient, &BackendClient::taskModelRoutesReceived, this, &MainWindow::handleTaskModelRoutesReceived);
    connect(backendClient, &BackendClient::taskModelRoutesFailed, this, &MainWindow::handleTaskModelRoutesFailed);
    connect(backendClient, &BackendClient::taskEvaluationReceived, this, &MainWindow::handleTaskEvaluationReceived);
    connect(backendClient, &BackendClient::taskEvaluationFailed, this, &MainWindow::handleTaskEvaluationFailed);
    connect(backendClient, &BackendClient::nodeContractsReceived, this, &MainWindow::handleNodeContractsReceived);
    connect(backendClient, &BackendClient::nodeContractsFailed, this, &MainWindow::handleNodeContractsFailed);
    connect(backendClient, &BackendClient::workflowCommandPolicyChecked, this, &MainWindow::handleWorkflowCommandPolicyChecked);
    connect(backendClient, &BackendClient::workflowCommandPolicyCheckFailed, this, &MainWindow::handleWorkflowCommandPolicyCheckFailed);
    connect(backendClient, &BackendClient::runtimePreferencesReceived, this, &MainWindow::handleRuntimePreferencesReceived);
    connect(backendClient, &BackendClient::runtimePreferencesFailed, this, &MainWindow::handleRuntimePreferencesFailed);
    connect(backendClient, &BackendClient::runtimePreferencesSaved, this, &MainWindow::handleRuntimePreferencesSaved);
    connect(backendClient, &BackendClient::runtimePreferencesSaveFailed, this, &MainWindow::handleRuntimePreferencesSaveFailed);
    connect(backendClient, &BackendClient::longTermMemoriesReceived, this, &MainWindow::handleLongTermMemoriesReceived);
    connect(backendClient, &BackendClient::longTermMemoriesFailed, this, &MainWindow::handleLongTermMemoriesFailed);
    connect(backendClient, &BackendClient::longTermMemoryMutationCompleted, this, &MainWindow::handleLongTermMemoryMutationCompleted);
    connect(backendClient, &BackendClient::longTermMemoryMutationFailed, this, &MainWindow::handleLongTermMemoryMutationFailed);
    connect(backendClient, &BackendClient::taskArtifactsReceived, this, &MainWindow::handleTaskArtifactsReceived);
    connect(backendClient, &BackendClient::taskArtifactsFailed, this, &MainWindow::handleTaskArtifactsFailed);
    connect(backendClient, &BackendClient::taskArtifactPreviewReceived, this, &MainWindow::handleTaskArtifactPreviewReceived);
    connect(backendClient, &BackendClient::taskArtifactPreviewFailed, this, &MainWindow::handleTaskArtifactPreviewFailed);
    connect(backendClient, &BackendClient::taskArtifactOpened, this, &MainWindow::handleTaskArtifactOpened);
    connect(backendClient, &BackendClient::taskArtifactOpenFailed, this, &MainWindow::handleTaskArtifactOpenFailed);
    connect(backendClient, &BackendClient::taskToolCallsReceived, this, &MainWindow::handleTaskToolCallsReceived);
    connect(backendClient, &BackendClient::taskToolCallsFailed, this, &MainWindow::handleTaskToolCallsFailed);
    connect(backendClient, &BackendClient::taskUpdatesReceived, this, &MainWindow::handleTaskUpdatesReceived);
    connect(backendClient, &BackendClient::taskUpdatesFailed, this, &MainWindow::handleTaskUpdatesFailed);
    connect(backendClient,
            &BackendClient::taskDeliveryCardReceived,
            this,
            &MainWindow::handleTaskDeliveryCardReceived);
    connect(backendClient,
            &BackendClient::taskDeliveryCardFailed,
            this,
            &MainWindow::handleTaskDeliveryCardFailed);
    connect(backendClient, &BackendClient::modelProvidersReceived, this, &MainWindow::handleModelProvidersReceived);
    connect(backendClient, &BackendClient::modelProvidersFailed, this, &MainWindow::handleModelProvidersFailed);
    connect(backendClient, &BackendClient::modelRoutesReceived, this, &MainWindow::handleModelRoutesReceived);
    connect(backendClient, &BackendClient::modelRoutesFailed, this, &MainWindow::handleModelRoutesFailed);
    connect(backendClient, &BackendClient::modelRouteSaved, this, &MainWindow::handleModelRouteSaved);
    connect(backendClient, &BackendClient::modelRouteSaveFailed, this, &MainWindow::handleModelRouteSaveFailed);
    connect(backendClient, &BackendClient::modelConfigSaved, this, &MainWindow::handleModelConfigSaved);
    connect(backendClient, &BackendClient::modelConfigSaveFailed, this, &MainWindow::handleModelConfigSaveFailed);
    connect(backendClient, &BackendClient::modelConnectionTestCompleted, this, &MainWindow::handleModelConnectionTestCompleted);
    connect(backendClient, &BackendClient::modelConnectionTestFailed, this, &MainWindow::handleModelConnectionTestFailed);
    connect(backendClient, &BackendClient::mcpConnectionsReceived, this, &MainWindow::handleMcpConnectionsReceived);
    connect(backendClient, &BackendClient::mcpConnectionsFailed, this, &MainWindow::handleMcpConnectionsFailed);
    connect(backendClient, &BackendClient::mcpConnectionUpdated, this, &MainWindow::handleMcpConnectionUpdated);
    connect(backendClient, &BackendClient::mcpConnectionUpdateFailed, this, &MainWindow::handleMcpConnectionUpdateFailed);
    connect(backendClient, &BackendClient::workspaceDocumentImported, this, &MainWindow::handleWorkspaceDocumentImported);
    connect(backendClient, &BackendClient::workspaceDocumentImportFailed, this, &MainWindow::handleWorkspaceDocumentImportFailed);
    connect(backendClient, &BackendClient::workspaceDocumentsReceived, this, &MainWindow::handleWorkspaceDocumentsReceived);
    connect(backendClient, &BackendClient::workspaceDocumentsFailed, this, &MainWindow::handleWorkspaceDocumentsFailed);
    connect(backendClient, &BackendClient::knowledgeBasesReceived, this, &MainWindow::handleKnowledgeBasesReceived);
    connect(backendClient, &BackendClient::knowledgeBasesFailed, this, &MainWindow::handleKnowledgeBasesFailed);
    connect(backendClient, &BackendClient::knowledgeBaseCreated, this, &MainWindow::handleKnowledgeBaseCreated);
    connect(backendClient, &BackendClient::knowledgeBaseCreateFailed, this, &MainWindow::handleKnowledgeBaseCreateFailed);
    connect(backendClient, &BackendClient::knowledgeDocumentsReceived, this, &MainWindow::handleKnowledgeDocumentsReceived);
    connect(backendClient, &BackendClient::knowledgeDocumentsFailed, this, &MainWindow::handleKnowledgeDocumentsFailed);
    connect(backendClient, &BackendClient::knowledgeDocumentsImported, this, &MainWindow::handleKnowledgeDocumentsImported);
    connect(backendClient, &BackendClient::knowledgeDocumentsImportFailed, this, &MainWindow::handleKnowledgeDocumentsImportFailed);
    connect(backendClient, &BackendClient::knowledgeIndexStarted, this, &MainWindow::handleKnowledgeIndexStarted);
    connect(backendClient, &BackendClient::knowledgeIndexStartFailed, this, &MainWindow::handleKnowledgeIndexStartFailed);
    connect(backendClient, &BackendClient::knowledgeIndexJobReceived, this, &MainWindow::handleKnowledgeIndexJobReceived);
    connect(backendClient, &BackendClient::knowledgeIndexJobFailed, this, &MainWindow::handleKnowledgeIndexJobFailed);
    connect(backendClient, &BackendClient::knowledgeVectorCapabilityReceived, this, &MainWindow::handleKnowledgeVectorCapabilityReceived);
    connect(backendClient, &BackendClient::knowledgeVectorCapabilityFailed, this, &MainWindow::handleKnowledgeVectorCapabilityFailed);
    connect(backendClient, &BackendClient::knowledgeVectorModelPrepared, this, &MainWindow::handleKnowledgeVectorModelPrepared);
    connect(backendClient, &BackendClient::knowledgeVectorModelPrepareFailed, this, &MainWindow::handleKnowledgeVectorModelPrepareFailed);
    connect(backendClient, &BackendClient::knowledgeOcrCapabilityReceived, this, &MainWindow::handleKnowledgeOcrCapabilityReceived);
    connect(backendClient, &BackendClient::knowledgeOcrCapabilityFailed, this, &MainWindow::handleKnowledgeOcrCapabilityFailed);
    connect(backendClient, &BackendClient::knowledgeOcrPreparationReceived, this, &MainWindow::handleKnowledgeOcrPreparationReceived);
    connect(backendClient, &BackendClient::knowledgeOcrPreparationFailed, this, &MainWindow::handleKnowledgeOcrPreparationFailed);
    connect(backendClient, &BackendClient::knowledgeBaseDeletionRequested, this, &MainWindow::handleKnowledgeBaseDeletionRequested);
    connect(backendClient, &BackendClient::knowledgeBaseDeletionFailed, this, &MainWindow::handleKnowledgeBaseDeletionFailed);
    connect(backendClient,
            &BackendClient::knowledgeAnswerCompleted,
            this,
            &MainWindow::handleDispatchKnowledgeAnswerCompleted);
    connect(backendClient,
            &BackendClient::knowledgeAnswerFailed,
            this,
            &MainWindow::handleDispatchKnowledgeAnswerFailed);
    connect(backendClient, &BackendClient::dataDatasetImported, this, &MainWindow::handleDataDatasetImported);
    connect(backendClient, &BackendClient::dataDatasetImportFailed, this, &MainWindow::handleDataDatasetImportFailed);
    connect(backendClient, &BackendClient::dataDatasetsReceived, this, &MainWindow::handleDataDatasetsReceived);
    connect(backendClient, &BackendClient::dataDatasetsFailed, this, &MainWindow::handleDataDatasetsFailed);
    connect(backendClient, &BackendClient::dataDatasetProfileReceived, this, &MainWindow::handleDataDatasetProfileReceived);
    connect(backendClient, &BackendClient::dataDatasetProfileFailed, this, &MainWindow::handleDataDatasetProfileFailed);
    connect(backendClient, &BackendClient::dataRecommendationsReceived, this, &MainWindow::handleDataRecommendationsReceived);
    connect(backendClient, &BackendClient::dataRecommendationsFailed, this, &MainWindow::handleDataRecommendationsFailed);
    connect(backendClient, &BackendClient::dataAnalysisPreviewReceived, this, &MainWindow::handleDataAnalysisPreviewReceived);
    connect(backendClient, &BackendClient::dataAnalysisPreviewFailed, this, &MainWindow::handleDataAnalysisPreviewFailed);
    connect(backendClient,
            &BackendClient::dataAnalysisWorkbookExportStarted,
            this,
            &MainWindow::handleDataAnalysisWorkbookExportStarted);
    connect(backendClient,
            &BackendClient::dataAnalysisWorkbookExported,
            this,
            &MainWindow::handleDataAnalysisWorkbookExported);
    connect(backendClient,
            &BackendClient::dataAnalysisWorkbookExportStillRunning,
            this,
            &MainWindow::handleDataAnalysisWorkbookExportStillRunning);
    connect(backendClient,
            &BackendClient::dataAnalysisWorkbookExportCancelled,
            this,
            &MainWindow::handleDataAnalysisWorkbookExportCancelled);
    connect(backendClient,
            &BackendClient::dataAnalysisWorkbookExportFailed,
            this,
            &MainWindow::handleDataAnalysisWorkbookExportFailed);
    connect(backendClient, &BackendClient::dataChartExportStarted, this, &MainWindow::handleDataChartExportStarted);
    connect(backendClient, &BackendClient::dataChartExported, this, &MainWindow::handleDataChartExported);
    connect(backendClient,
            &BackendClient::dataChartExportStillRunning,
            this,
            &MainWindow::handleDataChartExportStillRunning);
    connect(backendClient, &BackendClient::dataChartExportCancelled, this, &MainWindow::handleDataChartExportCancelled);
    connect(backendClient, &BackendClient::dataChartExportFailed, this, &MainWindow::handleDataChartExportFailed);
    connect(backendClient, &BackendClient::dataChartImageReceived, this, &MainWindow::handleDataChartImageReceived);
    connect(backendClient, &BackendClient::dataChartImageFailed, this, &MainWindow::handleDataChartImageFailed);
    connect(backendClient,
            &BackendClient::dataTransformationPreviewReceived,
            this,
            &MainWindow::handleDataTransformationPreviewReceived);
    connect(backendClient,
            &BackendClient::dataTransformationPreviewFailed,
            this,
            &MainWindow::handleDataTransformationPreviewFailed);
    connect(backendClient,
            &BackendClient::dataTransformationExportStarted,
            this,
            &MainWindow::handleDataTransformationExportStarted);
    connect(backendClient,
            &BackendClient::dataTransformationExported,
            this,
            &MainWindow::handleDataTransformationExported);
    connect(backendClient,
            &BackendClient::dataTransformationExportStillRunning,
            this,
            &MainWindow::handleDataTransformationExportStillRunning);
    connect(backendClient,
            &BackendClient::dataTransformationExportCancelled,
            this,
            &MainWindow::handleDataTransformationExportCancelled);
    connect(backendClient,
            &BackendClient::dataTransformationExportFailed,
            this,
            &MainWindow::handleDataTransformationExportFailed);
    connect(backendClient, &BackendClient::documentAgentStarted, this, &MainWindow::handleDocumentAgentStarted);
    connect(backendClient, &BackendClient::documentAgentCompleted, this, &MainWindow::handleDocumentAgentCompleted);
    connect(backendClient, &BackendClient::documentAgentStillRunning, this, &MainWindow::handleDocumentAgentStillRunning);
    connect(backendClient, &BackendClient::documentAgentFailed, this, &MainWindow::handleDocumentAgentFailed);
    connect(backendClient, &BackendClient::pdfProcessingStarted, this, &MainWindow::handlePdfProcessingStarted);
    connect(backendClient, &BackendClient::pdfProcessingCompleted, this, &MainWindow::handlePdfProcessingCompleted);
    connect(backendClient,
            &BackendClient::pdfProcessingStillRunning,
            this,
            &MainWindow::handlePdfProcessingStillRunning);
    connect(backendClient, &BackendClient::pdfProcessingFailed, this, &MainWindow::handlePdfProcessingFailed);
    connect(backendClient,
            &BackendClient::documentDraftParentDiffReceived,
            this,
            &MainWindow::handleDocumentDraftParentDiffReceived);
    connect(backendClient,
            &BackendClient::documentDraftParentDiffFailed,
            this,
            &MainWindow::handleDocumentDraftParentDiffFailed);
    connect(backendClient,
            &BackendClient::documentDraftMergeCandidatesReceived,
            this,
            &MainWindow::handleDocumentDraftMergeCandidatesReceived);
    connect(backendClient,
            &BackendClient::documentDraftMergePlanReceived,
            this,
            &MainWindow::handleDocumentDraftMergePlanReceived);
    connect(backendClient,
            &BackendClient::documentDraftMergeFailed,
            this,
            &MainWindow::handleDocumentDraftMergeFailed);
    connect(backendClient, &BackendClient::documentDraftSaved, this, &MainWindow::handleDocumentDraftSaved);
    connect(backendClient, &BackendClient::documentDraftSaveFailed, this, &MainWindow::handleDocumentDraftSaveFailed);
    connect(backendClient,
            &BackendClient::presentationPreviewReceived,
            this,
            &MainWindow::handlePresentationPreviewReceived);
    connect(backendClient,
            &BackendClient::presentationPreviewFailed,
            this,
            &MainWindow::handlePresentationPreviewFailed);
    connect(backendClient,
            &BackendClient::presentationExported,
            this,
            &MainWindow::handlePresentationExported);
    connect(backendClient,
            &BackendClient::presentationExportFailed,
            this,
            &MainWindow::handlePresentationExportFailed);
    connect(backendClient,
            &BackendClient::projectReviewReceived,
            this,
            &MainWindow::handleProjectReviewReceived);
    connect(backendClient,
            &BackendClient::projectReviewFailed,
            this,
            &MainWindow::handleProjectReviewFailed);
    connect(backendClient,
            &BackendClient::projectReviewStarted,
            this,
            &MainWindow::handleProjectReviewStarted);
    connect(backendClient,
            &BackendClient::projectReviewStillRunning,
            this,
            &MainWindow::handleProjectReviewStillRunning);
    connect(backendClient,
            &BackendClient::paperReviewReceived,
            this,
            &MainWindow::handlePaperReviewReceived);
    connect(backendClient,
            &BackendClient::paperReviewFailed,
            this,
            &MainWindow::handlePaperReviewFailed);
    connect(backendClient,
            &BackendClient::paperReviewStarted,
            this,
            &MainWindow::handlePaperReviewStarted);
    connect(backendClient,
            &BackendClient::paperReviewStillRunning,
            this,
            &MainWindow::handlePaperReviewStillRunning);
    connect(backendClient, &BackendClient::taskLogsReceived, this, &MainWindow::handleTaskLogsReceived);
    connect(backendClient, &BackendClient::taskLogsFailed, this, &MainWindow::handleTaskLogsFailed);
    connect(backendClient, &BackendClient::taskPermissionsReceived, this, &MainWindow::handleTaskPermissionsReceived);
    connect(backendClient, &BackendClient::taskPermissionsFailed, this, &MainWindow::handleTaskPermissionsFailed);
    connect(backendClient, &BackendClient::taskPermissionDecisionCompleted, this, &MainWindow::handleTaskPermissionDecisionCompleted);
    connect(backendClient, &BackendClient::taskPermissionDecisionFailed, this, &MainWindow::handleTaskPermissionDecisionFailed);
    connect(backendClient, &BackendClient::taskControlCompleted, this, &MainWindow::handleTaskControlCompleted);
    connect(backendClient, &BackendClient::taskControlFailed, this, &MainWindow::handleTaskControlFailed);
    connect(backendClient, &BackendClient::taskExecutionCompleted, this, &MainWindow::handleTaskExecutionCompleted);
    connect(backendClient, &BackendClient::taskExecutionFailed, this, &MainWindow::handleTaskExecutionFailed);
    connect(backendClient, &BackendClient::agentsLoadFailed, this, [this](const QString &message) {
        ui->taskUsageLabel->setText(QStringLiteral("Agent 注册表：加载失败 · %1").arg(message));
        ui->apiUsageProgress->setValue(20);
    });
}

void MainWindow::closeEvent(QCloseEvent *event)
{
    // 退出期先切断异步回调，再停掉本类启动的后端。
    // 这样可以避免某些环境里仅关闭主窗口但应用仍残留在进程表中的情况。
    QObject::disconnect(backendClient, nullptr, this, nullptr);
    QObject::disconnect(backendManager, nullptr, this, nullptr);
    if (backendManager) {
        backendManager->stopForFastExit();
    }

    event->accept();
    QTimer::singleShot(0, qApp, &QCoreApplication::quit);
    QMainWindow::closeEvent(event);
}

void MainWindow::resizeEvent(QResizeEvent *event)
{
    QMainWindow::resizeEvent(event);
    renderDispatchDeliveryImage();
}

void MainWindow::setupDispatchChat()
{
    loadDispatchConversationPreference();
    // Designer 中的对话内容只是早期页面示例。真实调度会话改为增量追加：首条成功发送前
    // 清空示例，后续轮次不会清屏，客户才能看清“刚才/上一步”究竟指向什么。
    ui->conversationTextEdit->clear();
    resetDispatchDeliveryCard();
    ui->dispatchChatStatus->setText(
        currentDispatchConversationId.isEmpty()
            ? QStringLiteral("待命")
            : QStringLiteral("待命 · 将延续当前会话"));
    // 任务状态不能只靠不断变化的文字猜测。这个固定尺寸指示器只在后端真实运行态旋转，
    // 也不会因“完成/失败”文字切换而挤压顶栏布局。
    if (auto *statusLayout = qobject_cast<QHBoxLayout *>(ui->dispatchChatStatus->parentWidget()->layout())) {
        dispatchActivityIndicator = new TaskActivityIndicator(ui->dispatchChatStatus->parentWidget());
        statusLayout->insertWidget(statusLayout->indexOf(ui->dispatchChatStatus), dispatchActivityIndicator);
        dispatchActivityIndicator->setRunning(false);
    }
    resetProgressPanel();
    ui->taskSettingButton->setText(QStringLiteral("查看历史"));
    ui->taskSettingButton->setToolTip(QStringLiteral("打开历史任务页并定位到当前调度任务。"));
    ui->dispatchPlanButton->setToolTip(
        QStringLiteral("查看当前任务的计划版本；真实执行前可修改目标并生成新计划。"));
    ui->dispatchPlanButton->setIcon(style()->standardIcon(QStyle::SP_FileDialogDetailedView));
    ui->dispatchProgressToggleButton->setCheckable(true);
    ui->dispatchProgressToggleButton->setChecked(false);
    ui->dispatchProgressToggleButton->setToolTip(
        QStringLiteral("按需展开阶段状态；完整事件和产物仍可在任务历史查看。"));
    ui->dispatchProjectScopeButton->setIcon(style()->standardIcon(QStyle::SP_DirIcon));
    ui->dispatchNewConversationButton->setIcon(style()->standardIcon(QStyle::SP_FileDialogNewFolder));
    ui->dispatchNewConversationButton->setText(QString());
    ui->dispatchConversationHistoryButton->setIcon(style()->standardIcon(QStyle::SP_FileDialogContentsView));
    ui->dispatchConversationHistoryButton->setText(QString());
    ui->dispatchModelRouteButton->setIcon(style()->standardIcon(QStyle::SP_ComputerIcon));
    ui->dispatchModelRouteButton->setToolTip(
        QStringLiteral("选择总指挥本轮规划与回复使用的模型；不会显示或复制 API Key。"));
    ui->dispatchClearDocumentButton->setIcon(style()->standardIcon(QStyle::SP_DialogCloseButton));
    ui->dispatchClearDocumentButton->setText(QString());
    ui->dispatchClearKnowledgeButton->setIcon(style()->standardIcon(QStyle::SP_DialogCloseButton));
    ui->dispatchClearKnowledgeButton->setText(QString());
    ui->dispatchClearDatasetButton->setIcon(style()->standardIcon(QStyle::SP_DialogCloseButton));
    ui->dispatchClearDatasetButton->setText(QString());
    updateDispatchProjectScopeButton();
    updateDispatchMaterialBindingsUi();
    ui->dispatchExecuteButton->setToolTip(
        QStringLiteral("把当前 dry-run 计划转入真实 Runtime；高风险步骤仍会等待权限确认。"));
    updateDispatchActionButtons();

    // updates 不是长连接，而是对当前任务做轻量聚合快照；这里用单次定时器做低频刷新，
    // 避免每条日志都直接打一条 HTTP 请求。
    dispatchUpdateRefreshTimer = new QTimer(this);
    dispatchUpdateRefreshTimer->setSingleShot(true);
    connect(dispatchUpdateRefreshTimer, &QTimer::timeout, this, &MainWindow::refreshCurrentDispatchUpdates);

    // “＋”是材料入口而非单一文件上传：导入新文件和选择已有受控材料同属本次任务范围，
    // 但用菜单渐进披露，避免在底部 Composer 堆出三个永久下拉框。
    auto *materialMenu = new QMenu(ui->attachButton);
    QAction *importMaterialAction = materialMenu->addAction(QStringLiteral("从电脑导入材料…"));
    importMaterialAction->setToolTip(QStringLiteral("导入文件后会自动绑定到本次任务。"));
    QAction *selectMaterialAction = materialMenu->addAction(QStringLiteral("选择已导入材料…"));
    selectMaterialAction->setToolTip(QStringLiteral("选择已有文档、资料库或数据集，不会读取正文。"));
    ui->attachButton->setMenu(materialMenu);
    ui->attachButton->setToolTip(QStringLiteral("添加材料：导入文件或选择已导入材料"));
    connect(importMaterialAction, &QAction::triggered, this, &MainWindow::importWorkspaceDocumentFromFile);
    connect(selectMaterialAction, &QAction::triggered, this, &MainWindow::openDispatchMaterialDialog);
    connect(ui->dispatchProjectScopeButton, &QToolButton::clicked, this, &MainWindow::configureDispatchProjectScope);
    connect(ui->dispatchProgressToggleButton, &QToolButton::toggled, this, [this](bool visible) {
        // 仅改变调度台的信息密度，不会影响后台执行、状态轮询或任务历史审计。
        ui->dispatchProgressPanel->setVisible(visible);
        ui->dispatchProgressToggleButton->setText(
            visible ? QStringLiteral("收起过程") : QStringLiteral("查看过程"));
    });
    connect(ui->dispatchModelRouteButton, &QToolButton::clicked, this, [this]() {
        openModelRouteDialogForRoute(QStringLiteral("commander_planning"));
    });
    connect(ui->dispatchDeliveryHistoryButton,
            &QPushButton::clicked,
            this,
            &MainWindow::openCurrentDispatchTaskInHistory);
    connect(ui->dispatchDeliveryOpenButton,
            &QPushButton::clicked,
            this,
            &MainWindow::openDispatchDeliveryArtifact);
    connect(ui->dispatchNewConversationButton, &QToolButton::clicked, this, &MainWindow::startNewDispatchConversation);
    connect(ui->dispatchConversationHistoryButton,
            &QToolButton::clicked,
            this,
            &MainWindow::openDispatchConversationHistory);
    auto *agentHintMenu = new QMenu(ui->dispatchAgentHintButton);
    for (const DispatchAgentHintDefinition &definition : dispatchAgentHintDefinitions()) {
        // canonicalMention 含中文，源码按 UTF-8 编译；用 Latin-1 会让菜单项在部分构建中
        // 显示为乱码。agentId 本身仍是稳定 ASCII 协议字段，继续使用 fromLatin1。
        QAction *action = agentHintMenu->addAction(QString::fromUtf8(definition.canonicalMention));
        connect(action, &QAction::triggered, this, [this, agentId = QString::fromLatin1(definition.agentId)]() {
            insertDispatchAgentHint(agentId);
        });
    }
    ui->dispatchAgentHintButton->setMenu(agentHintMenu);
    ui->dispatchAgentHintButton->setPopupMode(QToolButton::InstantPopup);
    connect(ui->dispatchInputEdit, &QLineEdit::textChanged, this, [this]() {
        // 文本标签变化也会改变“本次范围”条的显隐；不能只刷新 chip 内容，否则单独
        // 输入 `@知识库` 时标签会留在隐藏容器里。
        updateDispatchMaterialBindingsUi();
    });
    connect(ui->dispatchClearDocumentButton, &QToolButton::clicked, this, [this]() {
        if (!dispatchSubmissionWaitingForBackend && !currentDispatchExecutionInProgress) {
            dispatchSelectedDocumentRef.clear();
            updateDispatchMaterialBindingsUi();
        }
    });
    connect(ui->dispatchClearKnowledgeButton, &QToolButton::clicked, this, [this]() {
        if (!dispatchSubmissionWaitingForBackend && !currentDispatchExecutionInProgress) {
            dispatchSelectedKnowledgeBaseId.clear();
            updateDispatchMaterialBindingsUi();
        }
    });
    connect(ui->dispatchClearDatasetButton, &QToolButton::clicked, this, [this]() {
        if (!dispatchSubmissionWaitingForBackend && !currentDispatchExecutionInProgress) {
            dispatchSelectedDatasetRef.clear();
            updateDispatchMaterialBindingsUi();
        }
    });
    connect(ui->dispatchClearDocumentAgentHintButton, &QToolButton::clicked, this, [this]() {
        removeDispatchAgentHint(QStringLiteral("document_agent"));
    });
    connect(ui->dispatchClearDataAgentHintButton, &QToolButton::clicked, this, [this]() {
        removeDispatchAgentHint(QStringLiteral("data_agent"));
    });
    connect(ui->dispatchClearKnowledgeAgentHintButton, &QToolButton::clicked, this, [this]() {
        removeDispatchAgentHint(QStringLiteral("knowledge_agent"));
    });
    connect(ui->taskSettingButton, &QPushButton::clicked, this, &MainWindow::openCurrentDispatchTaskInHistory);
    connect(ui->dispatchPlanButton, &QPushButton::clicked, this, &MainWindow::openDispatchPlanManager);
    connect(ui->dispatchExecuteButton, &QPushButton::clicked, this, &MainWindow::executeCurrentDispatchTaskFromDispatch);
    connect(ui->sendTaskButton, &QPushButton::clicked, this, &MainWindow::sendDispatchMessage);
    connect(ui->dispatchInputEdit, &QLineEdit::returnPressed, this, &MainWindow::sendDispatchMessage);
    updateDispatchAgentHintsUi();
}

void MainWindow::loadDispatchConversationPreference()
{
    // 只恢复不透明会话 ID，不从 Qt 设置写入/读取聊天正文。实际近轮消息、摘要和材料引用
    // 始终由后端 SQLite 的受控会话层保存；若范围不一致，后端会自动切出新会话。
    QSettings settings(QStringLiteral("AgentFlow"), QStringLiteral("AgentFlow"));
    settings.beginGroup(QStringLiteral("ui/dispatchConversation"));
    currentDispatchConversationId = settings.value(QStringLiteral("conversationId")).toString().trimmed();
    settings.endGroup();
}

void MainWindow::saveDispatchConversationPreference() const
{
    QSettings settings(QStringLiteral("AgentFlow"), QStringLiteral("AgentFlow"));
    settings.beginGroup(QStringLiteral("ui/dispatchConversation"));
    if (currentDispatchConversationId.isEmpty()) {
        settings.remove(QStringLiteral("conversationId"));
    } else {
        settings.setValue(QStringLiteral("conversationId"), currentDispatchConversationId);
    }
    settings.endGroup();
}

void MainWindow::startNewDispatchConversation()
{
    if (dispatchSubmissionWaitingForBackend || currentDispatchExecutionInProgress) {
        ui->dispatchChatStatus->setText(QStringLiteral("当前任务仍在暂存或执行中，结束后再新建对话。"));
        return;
    }

    // “新对话”只切断自动短期上下文，不会删除历史任务、长期记忆或已导入文件。材料选择
    // 也随会话清空，避免客户在新问题中无意继续授权上一段私有资料。
    currentDispatchConversationId.clear();
    saveDispatchConversationPreference();
    dispatchConversationHasMessages = false;
    dispatchConversationRestoreInProgress = false;
    currentDispatchUserGoal.clear();
    dispatchSelectedDocumentRef.clear();
    dispatchSelectedKnowledgeBaseId.clear();
    dispatchSelectedDatasetRef.clear();
    updateDispatchMaterialBindingsUi();
    ui->conversationTextEdit->clear();
    ui->dispatchInputEdit->clear();
    ui->dispatchInputEdit->setFocus();
    ui->dispatchChatStatus->setText(QStringLiteral("已新建对话 · 历史任务保持不变"));
    resetProgressPanel();
}

void MainWindow::openDispatchConversationHistory()
{
    if (dispatchSubmissionWaitingForBackend || currentDispatchExecutionInProgress) {
        ui->dispatchChatStatus->setText(QStringLiteral("当前任务仍在暂存或执行中，结束后再切换会话。"));
        return;
    }
    if (!backendManager || !backendManager->isReady() || !backendClient) {
        ui->dispatchChatStatus->setText(QStringLiteral("后端准备中，暂时无法读取会话列表。"));
        return;
    }

    ui->dispatchConversationHistoryButton->setEnabled(false);
    ui->dispatchChatStatus->setText(QStringLiteral("正在读取已保存会话"));
    backendClient->requestConversationSessions(currentDispatchProjectScope);
}

void MainWindow::handleDispatchConversationSessions(const ConversationSessionListResult &result)
{
    ui->dispatchConversationHistoryButton->setEnabled(true);
    if (result.projectScope != currentDispatchProjectScope) {
        // 客户可能在请求期间切换了项目范围；过期列表不能被拿来切换当前会话。
        return;
    }

    auto *menu = new QMenu(ui->dispatchConversationHistoryButton);
    menu->setAttribute(Qt::WA_DeleteOnClose);
    QAction *newConversationAction = menu->addAction(
        style()->standardIcon(QStyle::SP_FileDialogNewFolder), QStringLiteral("新建对话"));
    connect(newConversationAction, &QAction::triggered, this, &MainWindow::startNewDispatchConversation);
    if (!result.conversations.isEmpty()) {
        menu->addSeparator();
    }

    for (const ConversationSessionInfo &session : result.conversations) {
        const QString title = session.title.trimmed().isEmpty() ? QStringLiteral("未命名会话")
                                                                 : session.title.trimmed().left(56);
        const QString timestamp = session.updatedAt.left(19).replace(QLatin1Char('T'), QLatin1Char(' '));
        QAction *action = menu->addAction(
            QStringLiteral("%1\n%2 条消息 · %3")
                .arg(title)
                .arg(QString::number(session.archivedMessageCount))
                .arg(timestamp));
        action->setCheckable(true);
        action->setChecked(session.conversationId == currentDispatchConversationId);
        action->setToolTip(session.summary.left(220));
        connect(action,
                &QAction::triggered,
                this,
                [this, conversationId = session.conversationId]() { selectDispatchConversation(conversationId); });
    }
    if (result.conversations.isEmpty()) {
        QAction *emptyAction = menu->addAction(QStringLiteral("当前范围还没有已保存的会话"));
        emptyAction->setEnabled(false);
    }
    if (!currentDispatchConversationId.isEmpty()) {
        menu->addSeparator();
        QAction *archiveAction = menu->addAction(
            style()->standardIcon(QStyle::SP_FileDialogDetailedView), QStringLiteral("查看当前完整记录"));
        connect(archiveAction, &QAction::triggered, this, &MainWindow::openDispatchConversationArchive);
    }

    menu->popup(ui->dispatchConversationHistoryButton->mapToGlobal(
        QPoint(0, ui->dispatchConversationHistoryButton->height())));
}

void MainWindow::handleDispatchConversationSessionsFailed(const QString &message)
{
    ui->dispatchConversationHistoryButton->setEnabled(true);
    ui->dispatchChatStatus->setText(
        QStringLiteral("会话列表暂时无法读取 · %1").arg(message.left(100)));
}

void MainWindow::openDispatchConversationArchive()
{
    if (currentDispatchConversationId.isEmpty() || !backendClient || !backendManager
        || !backendManager->isReady()) {
        ui->dispatchChatStatus->setText(QStringLiteral("当前没有可读取的已保存会话。"));
        return;
    }
    if (dispatchConversationArchiveDialog) {
        dispatchConversationArchiveDialog->showNormal();
        dispatchConversationArchiveDialog->raise();
        dispatchConversationArchiveDialog->activateWindow();
        return;
    }

    auto *dialog = new QDialog(this);
    dialog->setAttribute(Qt::WA_DeleteOnClose);
    dialog->setWindowTitle(QStringLiteral("会话完整记录"));
    dialog->setMinimumSize(720, 520);
    dialog->resize(900, 650);
    dialog->setProperty("conversationId", currentDispatchConversationId);
    dialog->setProperty("projectScope", currentDispatchProjectScope);
    dialog->setProperty("nextOffset", 0);
    dialog->setProperty("total", 0);
    dispatchConversationArchiveDialog = dialog;

    auto *layout = new QVBoxLayout(dialog);
    layout->setContentsMargins(22, 20, 22, 20);
    layout->setSpacing(10);
    auto *title = new QLabel(QStringLiteral("完整会话记录"), dialog);
    title->setObjectName(QStringLiteral("sectionTitle"));
    layout->addWidget(title);
    auto *hint = new QLabel(
        QStringLiteral("仅展示已脱敏的用户与 AI 调度台消息；任务日志、工具参数和文件正文不会写入会话记录。"),
        dialog);
    hint->setObjectName(QStringLiteral("tinyText"));
    hint->setWordWrap(true);
    layout->addWidget(hint);
    auto *browser = new QTextBrowser(dialog);
    browser->setOpenExternalLinks(false);
    browser->setHtml(QStringLiteral("<p style=\"color:#64748B;\">正在读取会话记录...</p>"));
    layout->addWidget(browser, 1);
    dispatchConversationArchiveText = browser;

    auto *footer = new QHBoxLayout();
    auto *moreButton = new QPushButton(QStringLiteral("加载更多"), dialog);
    moreButton->setObjectName(QStringLiteral("ghostButton"));
    moreButton->setProperty("archiveMoreControl", true);
    moreButton->setEnabled(false);
    footer->addWidget(moreButton);
    footer->addStretch(1);
    auto *closeButton = new QPushButton(QStringLiteral("关闭"), dialog);
    closeButton->setObjectName(QStringLiteral("ghostButton"));
    footer->addWidget(closeButton);
    layout->addLayout(footer);
    connect(closeButton, &QPushButton::clicked, dialog, &QDialog::close);
    connect(moreButton, &QPushButton::clicked, this, [this, dialog, moreButton]() {
        if (!dialog || !backendClient || moreButton->property("loading").toBool()) {
            return;
        }
        const int offset = dialog->property("nextOffset").toInt();
        const int total = dialog->property("total").toInt();
        if (offset >= total) {
            return;
        }
        moreButton->setProperty("loading", true);
        moreButton->setEnabled(false);
        moreButton->setText(QStringLiteral("正在加载"));
        backendClient->requestConversationTranscript(
            dialog->property("conversationId").toString(),
            dialog->property("projectScope").toString(),
            offset,
            100);
    });
    connect(dialog, &QObject::destroyed, this, [this]() {
        dispatchConversationArchiveDialog = nullptr;
        dispatchConversationArchiveText = nullptr;
    });
    dialog->show();
    backendClient->requestConversationTranscript(currentDispatchConversationId, currentDispatchProjectScope, 0, 100);
}

void MainWindow::handleDispatchConversationTranscript(const ConversationTranscriptPageResult &result)
{
    if (!dispatchConversationArchiveDialog || !dispatchConversationArchiveText
        || result.session.conversationId
            != dispatchConversationArchiveDialog->property("conversationId").toString()) {
        return;
    }
    const int expectedOffset = dispatchConversationArchiveDialog->property("nextOffset").toInt();
    if (result.offset != expectedOffset) {
        return;
    }
    if (result.offset == 0) {
        dispatchConversationArchiveText->clear();
        if (!result.session.summary.trimmed().isEmpty()) {
            dispatchConversationArchiveText->append(
                QStringLiteral("<p style=\"color:#475569;\"><b>早期会话摘要</b><br/>%1</p>")
                    .arg(result.session.summary.toHtmlEscaped().replace(QStringLiteral("\n"), QStringLiteral("<br/>"))));
        }
    }
    for (const ConversationTranscriptMessage &message : result.messages) {
        const QString speaker = message.role == QStringLiteral("user") ? QStringLiteral("我")
                                                                         : QStringLiteral("AI调度台");
        dispatchConversationArchiveText->append(
            QStringLiteral("<hr/><h3>%1</h3><p>%2</p>")
                .arg(speaker, message.content.toHtmlEscaped()));
    }
    const int nextOffset = result.offset + result.messages.size();
    dispatchConversationArchiveDialog->setProperty("nextOffset", nextOffset);
    dispatchConversationArchiveDialog->setProperty("total", result.total);
    dispatchConversationArchiveDialog->setWindowTitle(
        QStringLiteral("会话完整记录 · %1 / %2 条").arg(nextOffset).arg(result.total));
    QPushButton *moreButton = nullptr;
    for (QPushButton *candidate : dispatchConversationArchiveDialog->findChildren<QPushButton *>()) {
        if (candidate->property("archiveMoreControl").toBool()) {
            moreButton = candidate;
            break;
        }
    }
    if (moreButton) {
        moreButton->setProperty("loading", false);
        const bool hasMore = nextOffset < result.total;
        moreButton->setEnabled(hasMore);
        moreButton->setText(hasMore ? QStringLiteral("加载更多") : QStringLiteral("已加载全部记录"));
    }
}

void MainWindow::handleDispatchConversationTranscriptFailed(
    const QString &conversationId,
    const QString &message)
{
    if (!dispatchConversationArchiveDialog || conversationId
        != dispatchConversationArchiveDialog->property("conversationId").toString()) {
        return;
    }
    if (dispatchConversationArchiveText) {
        dispatchConversationArchiveText->append(
            QStringLiteral("<p style=\"color:#C2410C;\"><b>无法继续读取记录：</b>%1</p>")
                .arg(message.toHtmlEscaped()));
    }
    QPushButton *moreButton = nullptr;
    for (QPushButton *candidate : dispatchConversationArchiveDialog->findChildren<QPushButton *>()) {
        if (candidate->property("archiveMoreControl").toBool()) {
            moreButton = candidate;
            break;
        }
    }
    if (moreButton) {
        moreButton->setProperty("loading", false);
        moreButton->setEnabled(true);
        moreButton->setText(QStringLiteral("重试加载"));
    }
}

void MainWindow::selectDispatchConversation(const QString &conversationId)
{
    if (conversationId.isEmpty() || conversationId == currentDispatchConversationId) {
        return;
    }
    if (dispatchSubmissionWaitingForBackend || currentDispatchExecutionInProgress) {
        ui->dispatchChatStatus->setText(QStringLiteral("当前任务仍在暂存或执行中，结束后再切换会话。"));
        return;
    }

    // 切换的是客户可见的对话，不是任务重放。清除当前页面上的任务控制引用，防止旧计划或
    // 执行按钮被误当成新会话的状态；任务本身仍完整保留在历史页。
    currentDispatchConversationId = conversationId;
    saveDispatchConversationPreference();
    dispatchConversationHasMessages = false;
    dispatchConversationRestoreInProgress = false;
    currentDispatchUserGoal.clear();
    currentDispatchTaskId.clear();
    currentDispatchPlanSummary = WorkflowPlanSummaryInfo{};
    currentDispatchPlanSteps.clear();
    currentDispatchPlannedStepCount = 0;
    currentDispatchNeedsClarification = false;
    currentDispatchGuidedHandoff = false;
    currentDispatchPresentationHandoff = false;
    currentDispatchPresentationRunning = false;
    currentDispatchPresentationCompleted = false;
    currentDispatchRuntimeMode.clear();
    currentDispatchRuntimeStatus.clear();
    currentDispatchExecutionInProgress = false;
    currentDispatchExecutionSubmitted = false;
    currentDispatchUpdateWatermark = 0;
    currentDispatchUpdates.clear();
    dispatchSelectedDocumentRef.clear();
    dispatchSelectedKnowledgeBaseId.clear();
    dispatchSelectedDatasetRef.clear();
    updateDispatchMaterialBindingsUi();
    ui->conversationTextEdit->clear();
    ui->dispatchInputEdit->clear();
    resetProgressPanel();
    updateDispatchActionButtons();
    requestDispatchConversationContext();
}

void MainWindow::requestDispatchConversationContext()
{
    // 恢复只发生在“已保存旧会话 ID、当前画面尚无客户消息、后端已 ready”的窗口。发送新任务
    // 会立即把 dispatchConversationHasMessages 置 true，随后到达的旧恢复响应会被忽略，避免
    // 用旧记录覆盖客户刚输入的新任务。
    if (currentDispatchConversationId.isEmpty() || dispatchConversationHasMessages
        || dispatchConversationRestoreInProgress || dispatchSubmissionWaitingForBackend || !backendManager
        || !backendManager->isReady()) {
        return;
    }
    dispatchConversationRestoreInProgress = true;
    ui->dispatchChatStatus->setText(QStringLiteral("正在恢复当前会话"));
    backendClient->requestConversationContext(currentDispatchConversationId);
}

void MainWindow::handleDispatchConversationContext(const ConversationContextInfo &context)
{
    dispatchConversationRestoreInProgress = false;
    if (context.conversationId != currentDispatchConversationId || dispatchConversationHasMessages) {
        return;
    }

    ui->conversationTextEdit->clear();
    if (!context.summary.trimmed().isEmpty()) {
        const QString summaryHtml = context.summary.toHtmlEscaped().replace(QStringLiteral("\n"), QStringLiteral("<br/>"));
        appendConversationHtml(
            QStringLiteral("<p style=\"color:#475569;\"><b>已恢复的早期会话摘要</b><br/>%1</p>")
                .arg(summaryHtml));
    }
    for (const ConversationTranscriptMessage &message : context.recentMessages) {
        if (message.role == QStringLiteral("user")) {
            appendConversationHtml(formatDispatchUserMessageHtml(message.content));
        } else {
            appendConversationHtml(
                QStringLiteral("<hr/><h3>AI调度台</h3>%1")
                    .arg(formatDispatchAnswerMarkdownHtml(message.content)));
        }
    }
    dispatchConversationHasMessages = !context.summary.trimmed().isEmpty() || !context.recentMessages.isEmpty();
    ui->dispatchChatStatus->setText(
        QStringLiteral("已恢复当前会话 · %1 条近轮记录").arg(context.recentMessages.size()));
}

void MainWindow::handleDispatchConversationContextFailed(const QString &message)
{
    dispatchConversationRestoreInProgress = false;
    if (dispatchConversationHasMessages || currentDispatchConversationId.isEmpty()) {
        return;
    }
    // 会话恢复失败不能阻止客户开始新任务；下一次成功回复会以服务端实际回传的会话 ID 覆盖旧指针。
    ui->dispatchChatStatus->setText(
        QStringLiteral("会话历史暂未恢复，仍可继续对话 · %1").arg(message.left(80)));
}

void MainWindow::importWorkspaceDocumentFromFile()
{
    importWorkspaceDocumentForTarget(QStringLiteral("dispatch"));
}

void MainWindow::openDispatchMaterialDialog()
{
    if (dispatchSubmissionWaitingForBackend || currentDispatchExecutionInProgress) {
        ui->dispatchChatStatus->setText(QStringLiteral("当前任务正在暂存或执行中，结束后再调整材料范围。"));
        return;
    }

    if (!dispatchMaterialDialog) {
        dispatchMaterialDialog = new DispatchMaterialDialog(this);
        connect(dispatchMaterialDialog, &DispatchMaterialDialog::refreshRequested, this, [this]() {
            refreshDispatchMaterialCatalog();
        });
        connect(dispatchMaterialDialog,
                &DispatchMaterialDialog::materialsApplied,
                this,
                [this](const QString &documentRef, const QString &knowledgeBaseId, const QString &datasetRef) {
                    if (dispatchSubmissionWaitingForBackend || currentDispatchExecutionInProgress) {
                        return;
                    }
                    dispatchSelectedDocumentRef = documentRef;
                    dispatchSelectedKnowledgeBaseId = knowledgeBaseId;
                    dispatchSelectedDatasetRef = datasetRef;
                    updateDispatchMaterialBindingsUi();

                    const int materialCount = (!documentRef.isEmpty() ? 1 : 0)
                                              + (!knowledgeBaseId.isEmpty() ? 1 : 0)
                                              + (!datasetRef.isEmpty() ? 1 : 0);
                    ui->dispatchChatStatus->setText(
                        QStringLiteral("已应用 %1 项本次只读材料，可继续描述任务。 ").arg(materialCount));
                    ui->dispatchInputEdit->setFocus();
                });
    }

    dispatchMaterialDialog->setDocuments(currentWorkspaceDocuments);
    dispatchMaterialDialog->setKnowledgeBases(currentKnowledgeBases);
    dispatchMaterialDialog->setDatasets(currentDataDatasets);
    dispatchMaterialDialog->setSelections(
        dispatchSelectedDocumentRef, dispatchSelectedKnowledgeBaseId, dispatchSelectedDatasetRef);
    dispatchMaterialDialog->show();
    dispatchMaterialDialog->raise();
    dispatchMaterialDialog->activateWindow();
    refreshDispatchMaterialCatalog();
}

void MainWindow::refreshDispatchMaterialCatalog()
{
    if (!dispatchMaterialDialog || !dispatchMaterialDialog->isVisible()) {
        return;
    }
    if (!backendManager || !backendManager->isReady() || !backendClient) {
        dispatchMaterialDocumentsPending = false;
        dispatchMaterialKnowledgePending = false;
        dispatchMaterialDatasetsPending = false;
        dispatchMaterialCatalogError.clear();
        dispatchMaterialDialog->setCatalogStatus(
            QStringLiteral("本地后端正在准备；就绪后会自动同步已导入材料。"), QStringLiteral("running"));
        if (backendManager) {
            backendManager->ensureStarted();
        }
        return;
    }

    // 三份目录只含轻量元数据。同步不触发文档解析、知识库索引、数据画像或模型调用。
    dispatchMaterialDocumentsPending = true;
    dispatchMaterialKnowledgePending = true;
    dispatchMaterialDatasetsPending = true;
    dispatchMaterialCatalogError.clear();
    updateDispatchMaterialCatalogStatus();
    backendClient->requestWorkspaceDocuments();
    backendClient->requestKnowledgeBases();
    backendClient->requestDataDatasets();
}

void MainWindow::updateDispatchMaterialCatalogStatus()
{
    if (!dispatchMaterialDialog || !dispatchMaterialDialog->isVisible()) {
        return;
    }
    const bool pending = dispatchMaterialDocumentsPending || dispatchMaterialKnowledgePending
                         || dispatchMaterialDatasetsPending;
    if (pending) {
        dispatchMaterialDialog->setCatalogStatus(
            QStringLiteral("正在同步文档、资料库与数据集目录…"), QStringLiteral("running"));
        return;
    }
    if (!dispatchMaterialCatalogError.isEmpty()) {
        dispatchMaterialDialog->setCatalogStatus(
            QStringLiteral("部分材料目录未同步：%1。可点击“同步材料”重试。 ")
                .arg(dispatchMaterialCatalogError.left(100)),
            QStringLiteral("error"));
        return;
    }
    dispatchMaterialDialog->setCatalogStatus(
        QStringLiteral("已同步 %1 份文档、%2 个资料库、%3 份数据集。")
            .arg(currentWorkspaceDocuments.size())
            .arg(currentKnowledgeBases.size())
            .arg(currentDataDatasets.size()));
}

void MainWindow::configureDispatchProjectScope()
{
    // project_scope 只服务于会话与已确认长期记忆的隔离。它是按需弹出的低频设置，
    // 不代表本轮文件范围、目录权限或 Agent 权限，避免客户把它误当作任务前置条件。
    const QString previousScope = currentDispatchProjectScope;
    QDialog dialog(this);
    dialog.setWindowTitle(QStringLiteral("会话空间"));
    dialog.setMinimumWidth(520);
    auto *layout = new QVBoxLayout(&dialog);
    auto *description = new QLabel(
        QStringLiteral("会话空间用于隔离这段对话及已确认的长期偏好，不会扩大文件读取或工具权限。"),
        &dialog);
    description->setWordWrap(true);
    description->setObjectName(QStringLiteral("mutedText"));
    layout->addWidget(description);

    auto *modeCombo = new QComboBox(&dialog);
    modeCombo->addItem(QStringLiteral("所有会话（默认）"), QStringLiteral("global"));
    modeCombo->addItem(QStringLiteral("单独项目空间"), QStringLiteral("project"));
    auto *projectIdInput = new QLineEdit(&dialog);
    projectIdInput->setPlaceholderText(QStringLiteral("例如：marketing-2026"));
    projectIdInput->setMaxLength(64);
    const bool hasProjectScope = currentDispatchProjectScope.startsWith(QStringLiteral("project:"));
    modeCombo->setCurrentIndex(hasProjectScope ? 1 : 0);
    if (hasProjectScope) {
        projectIdInput->setText(currentDispatchProjectScope.mid(QStringLiteral("project:").size()));
    }
    projectIdInput->setEnabled(hasProjectScope);
    connect(modeCombo, &QComboBox::currentIndexChanged, &dialog, [modeCombo, projectIdInput](int) {
        projectIdInput->setEnabled(modeCombo->currentData().toString() == QStringLiteral("project"));
        if (projectIdInput->isEnabled()) {
            projectIdInput->setFocus();
        }
    });

    auto *formLayout = new QFormLayout();
    formLayout->addRow(QStringLiteral("会话空间"), modeCombo);
    formLayout->addRow(QStringLiteral("空间标识"), projectIdInput);
    layout->addLayout(formLayout);

    auto *statusLabel = new QLabel(&dialog);
    statusLabel->setObjectName(QStringLiteral("tinyText"));
    layout->addWidget(statusLabel);
    auto *buttons = new QDialogButtonBox(QDialogButtonBox::Cancel, &dialog);
    QPushButton *confirmButton = buttons->addButton(QStringLiteral("使用此空间"), QDialogButtonBox::AcceptRole);
    layout->addWidget(buttons);
    connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    connect(confirmButton, &QPushButton::clicked, &dialog, [&]() {
        if (modeCombo->currentData().toString() == QStringLiteral("global")) {
            currentDispatchProjectScope = QStringLiteral("global");
        } else {
            const QString projectId = projectIdInput->text().trimmed().toLower();
            static const QRegularExpression allowedId(QStringLiteral("^[a-z0-9][a-z0-9_-]{0,63}$"));
            if (!allowedId.match(projectId).hasMatch()) {
                statusLabel->setText(QStringLiteral("空间标识只能使用英文、数字、- 或 _，且必须以英文或数字开头。"));
                return;
            }
            currentDispatchProjectScope = QStringLiteral("project:%1").arg(projectId);
        }
        updateDispatchProjectScopeButton();
        if (currentDispatchProjectScope != previousScope && !currentDispatchConversationId.isEmpty()) {
            // project_scope 是会话隔离边界。切换范围时不能让旧范围的摘要或材料在 UI 上看似
            // 仍属于新范围；后端也会独立创建新 ID，这里提前清除本地指针和可见聊天记录。
            currentDispatchConversationId.clear();
            saveDispatchConversationPreference();
            dispatchConversationHasMessages = false;
            dispatchConversationRestoreInProgress = false;
            ui->conversationTextEdit->clear();
            ui->dispatchChatStatus->setText(QStringLiteral("会话空间已切换 · 已新建隔离会话"));
        }
        dialog.accept();
    });
    dialog.exec();
}

void MainWindow::updateDispatchProjectScopeButton()
{
    if (!ui || !ui->dispatchProjectScopeButton) {
        return;
    }
    const QString scope = currentDispatchProjectScope.isEmpty() ? QStringLiteral("global")
                                                                 : currentDispatchProjectScope;
    ui->dispatchProjectScopeButton->setToolTip(
        QStringLiteral("设置会话空间，不影响文件或 Agent 权限。当前：%1").arg(scope));
    ui->dispatchProjectScopeButton->setAccessibleName(QStringLiteral("会话空间：%1").arg(scope));
}

void MainWindow::updateDispatchMaterialBindingsUi()
{
    // C6.3 把原来隐含在各工作台跳转里的材料范围显式显示出来。标签只保存受控相对引用或
    // 稳定资料库 ID，不展示绝对路径、文档正文或数据预览；发送后仍会清空，避免跨任务继承。
    if (!ui || !ui->dispatchMaterialsFrame) {
        return;
    }

    QString knowledgeName;
    for (const KnowledgeBaseInfo &base : currentKnowledgeBases) {
        if (base.knowledgeBaseId == dispatchSelectedKnowledgeBaseId) {
            knowledgeName = base.name;
            break;
        }
    }
    if (knowledgeName.isEmpty() && !dispatchSelectedKnowledgeBaseId.isEmpty()) {
        knowledgeName = QStringLiteral("已选资料库");
    }

    const bool hasDocument = !dispatchSelectedDocumentRef.isEmpty();
    const bool hasKnowledge = !dispatchSelectedKnowledgeBaseId.isEmpty();
    const bool hasDataset = !dispatchSelectedDatasetRef.isEmpty();
    const bool canEdit = !dispatchSubmissionWaitingForBackend && !currentDispatchExecutionInProgress;

    const auto updateChip = [canEdit](QLabel *label,
                                      QToolButton *button,
                                      bool visible,
                                      const QString &text,
                                      const QString &tooltip) {
        label->setVisible(visible);
        button->setVisible(visible);
        button->setEnabled(canEdit);
        if (visible) {
            label->setText(text);
            label->setToolTip(tooltip);
        }
    };

    updateChip(ui->dispatchDocumentMaterialLabel,
               ui->dispatchClearDocumentButton,
               hasDocument,
               QStringLiteral("文档 · %1").arg(QFileInfo(dispatchSelectedDocumentRef).fileName()),
               QStringLiteral("本次只读文档：%1").arg(dispatchSelectedDocumentRef));
    updateChip(ui->dispatchKnowledgeMaterialLabel,
               ui->dispatchClearKnowledgeButton,
               hasKnowledge,
               QStringLiteral("资料库 · %1").arg(knowledgeName),
               QStringLiteral("本次只读资料库：%1").arg(knowledgeName));
    updateChip(ui->dispatchDatasetMaterialLabel,
               ui->dispatchClearDatasetButton,
               hasDataset,
               QStringLiteral("数据 · %1").arg(QFileInfo(dispatchSelectedDatasetRef).fileName()),
               QStringLiteral("本次只读数据文件：%1").arg(dispatchSelectedDatasetRef));

    const bool hasAgentHints = !buildDispatchAgentHints().isEmpty();
    ui->dispatchMaterialsFrame->setVisible(hasDocument || hasKnowledge || hasDataset || hasAgentHints);
    ui->dispatchMaterialsHint->setText(
        QStringLiteral("本次范围 · 材料可组合，路由可移除"));
    updateDispatchAgentHintsUi();
}

QJsonArray MainWindow::buildDispatchAgentHints() const
{
    QJsonArray hints;
    for (const QString &agentId : parseDispatchAgentHintIds(ui->dispatchInputEdit->text())) {
        QJsonObject hint;
        hint.insert(QStringLiteral("agent_id"), agentId);
        hint.insert(QStringLiteral("source"), QStringLiteral("mention"));
        hints.append(hint);
    }
    return hints;
}

void MainWindow::updateDispatchAgentHintsUi()
{
    if (!ui || !ui->dispatchMaterialsFrame) {
        return;
    }
    const QStringList parsedHints = parseDispatchAgentHintIds(ui->dispatchInputEdit->text());
    const QSet<QString> selected(parsedHints.cbegin(), parsedHints.cend());
    const bool canEdit = !dispatchSubmissionWaitingForBackend && !currentDispatchExecutionInProgress;
    const auto updateChip = [canEdit](QLabel *label,
                                      QToolButton *button,
                                      bool visible,
                                      const QString &text) {
        label->setVisible(visible);
        button->setVisible(visible);
        button->setEnabled(canEdit);
        if (visible) {
            label->setText(QStringLiteral("路由 · %1").arg(text));
            label->setToolTip(QStringLiteral("本轮只优先规划 %1，不会增加权限或自动读取材料。").arg(text));
        }
    };
    updateChip(ui->dispatchDocumentAgentHintLabel,
               ui->dispatchClearDocumentAgentHintButton,
               selected.contains(QStringLiteral("document_agent")),
               QStringLiteral("文档助手"));
    updateChip(ui->dispatchDataAgentHintLabel,
               ui->dispatchClearDataAgentHintButton,
               selected.contains(QStringLiteral("data_agent")),
               QStringLiteral("数据工作台"));
    updateChip(ui->dispatchKnowledgeAgentHintLabel,
               ui->dispatchClearKnowledgeAgentHintButton,
               selected.contains(QStringLiteral("knowledge_agent")),
               QStringLiteral("知识库"));
}

void MainWindow::insertDispatchAgentHint(const QString &agentId)
{
    const DispatchAgentHintDefinition *definition = dispatchAgentHintDefinition(agentId);
    if (!definition || dispatchSubmissionWaitingForBackend || currentDispatchExecutionInProgress) {
        return;
    }
    const QString mention = QString::fromUtf8(definition->canonicalMention);
    if (!parseDispatchAgentHintIds(ui->dispatchInputEdit->text()).contains(agentId)) {
        const QString separator = ui->dispatchInputEdit->text().trimmed().isEmpty() ? QString() : QStringLiteral(" ");
        ui->dispatchInputEdit->insert(separator + mention + QStringLiteral(" "));
    }
    ui->dispatchInputEdit->setFocus();
}

void MainWindow::removeDispatchAgentHint(const QString &agentId)
{
    const DispatchAgentHintDefinition *definition = dispatchAgentHintDefinition(agentId);
    if (!definition || dispatchSubmissionWaitingForBackend || currentDispatchExecutionInProgress) {
        return;
    }
    QString message = ui->dispatchInputEdit->text();
    for (const QString &alias : definition->aliases) {
        const QRegularExpression token(
            QStringLiteral("@%1(?=\\s|$)").arg(QRegularExpression::escape(alias)),
            QRegularExpression::CaseInsensitiveOption);
        message.replace(token, QString());
    }
    ui->dispatchInputEdit->setText(message.simplified());
    ui->dispatchInputEdit->setFocus();
}

void MainWindow::importDocumentAgentDocument()
{
    importWorkspaceDocumentForTarget(QStringLiteral("document"));
}

void MainWindow::importWorkspaceDocumentForTarget(const QString &target)
{
    const bool pdfProcessingTarget = target == QStringLiteral("pdf_processing");
    const bool knowledgeTarget = target == QStringLiteral("knowledge");
    const bool dispatchTarget = target == QStringLiteral("dispatch");
    if (!backendClient || !backendManager || !backendManager->isReady()) {
        // 文件内容不进入启动期重试队列：文件导入是独立写操作，必须在客户确认的本地后端
        // 已就绪后才开始，避免 Qt 把大请求发到尚未监听的端口并等到网络超时。
        if (backendManager) {
            backendManager->ensureStarted();
        }
        if (dispatchTarget) {
            ui->dispatchChatStatus->setText(QStringLiteral("后端准备中，暂不能导入材料；服务就绪后请再次选择文件。"));
        } else if (target == QStringLiteral("document")) {
            ui->documentRunStatus->setText(QStringLiteral("后端准备中，暂不能导入文档。"));
            polishBadge(ui->documentRunStatus, QStringLiteral("badgeGray"));
        }
        return;
    }
    if (dispatchTarget && !pendingDataDatasetImportTarget.isEmpty()) {
        ui->dispatchChatStatus->setText(QStringLiteral("已有数据文件正在导入，请等待当前导入完成。"));
        return;
    }
    const QString filePath = QFileDialog::getOpenFileName(
        this,
        QStringLiteral("导入 workspace 文档"),
        QString(),
        pdfProcessingTarget ? QStringLiteral("PDF 文件 (*.pdf)")
                            : dispatchTarget
                                  ? QStringLiteral("受控材料 (*.txt *.md *.markdown *.pdf *.docx *.png *.jpg *.jpeg *.csv *.xlsx)")
                                  : QStringLiteral("受控材料 (*.txt *.md *.markdown *.pdf *.docx *.png *.jpg *.jpeg)"));
    if (filePath.isEmpty()) {
        return;
    }

    const QFileInfo fileInfo(filePath);
    const QString suffix = fileInfo.suffix().toLower();
    const bool textDocument = suffix == QStringLiteral("txt")
                               || suffix == QStringLiteral("md")
                               || suffix == QStringLiteral("markdown");
    const bool binaryDocument = suffix == QStringLiteral("pdf") || suffix == QStringLiteral("docx")
                                || suffix == QStringLiteral("png") || suffix == QStringLiteral("jpg")
                                || suffix == QStringLiteral("jpeg");
    const bool datasetDocument = dispatchTarget
                                 && (suffix == QStringLiteral("csv") || suffix == QStringLiteral("xlsx"));
    if (datasetDocument) {
        if (fileInfo.size() <= 0 || fileInfo.size() > DataDatasetMaxBytes) {
            QMessageBox::warning(
                this,
                QStringLiteral("导入数据"),
                QStringLiteral("当前只支持大于 0 且不超过 20MB 的 Excel 或 CSV 文件。"));
            return;
        }
        QFile dataFile(filePath);
        if (!dataFile.open(QIODevice::ReadOnly)) {
            QMessageBox::warning(this,
                                 QStringLiteral("导入数据"),
                                 QStringLiteral("无法读取文件：%1").arg(fileInfo.fileName()));
            return;
        }
        const QByteArray data = dataFile.readAll();
        if (data.size() != fileInfo.size()) {
            QMessageBox::warning(this,
                                 QStringLiteral("导入数据"),
                                 QStringLiteral("读取数据文件不完整，请确认文件未被其它程序占用。"));
            return;
        }

        // 调度台只保存后端回执的受控数据集引用；画像和确定性聚合仍由 data_agent 在
        // Runtime 内执行，聊天区不接收 CSV/XLSX 原文或行级数据。
        pendingDataDatasetImportTarget = QStringLiteral("dispatch");
        ui->attachButton->setEnabled(false);
        ui->dispatchChatStatus->setText(QStringLiteral("正在导入数据文件，完成后会绑定到本次任务。"));
        backendClient->importDataDataset(fileInfo.fileName(), data);
        return;
    }
    if ((!textDocument && !binaryDocument) || (pdfProcessingTarget && suffix != QStringLiteral("pdf"))) {
        QMessageBox::warning(
            this,
            QStringLiteral("导入文档"),
            dispatchTarget
                ? QStringLiteral("当前支持 TXT、Markdown、PDF、DOCX、PNG、JPG、JPEG、CSV 和 Excel 文件。")
                : QStringLiteral("当前支持 TXT、Markdown、PDF、DOCX、PNG、JPG 和 JPEG 文件。"));
        return;
    }
    const qint64 sizeLimit = textDocument ? WorkspaceDocumentMaxBytes : WorkspaceBinaryDocumentMaxBytes;
    if (fileInfo.size() > sizeLimit) {
        QMessageBox::warning(
            this,
            QStringLiteral("导入文档"),
            textDocument ? QStringLiteral("当前只支持 1MB 以内的 UTF-8 文本文档。")
                         : QStringLiteral("当前只支持 10MB 以内的 PDF、DOCX 或图片材料。"));
        return;
    }

    QFile file(filePath);
    if (!file.open(QIODevice::ReadOnly)) {
        QMessageBox::warning(this,
                             QStringLiteral("导入文档"),
                             QStringLiteral("无法读取文件：%1").arg(fileInfo.fileName()));
        return;
    }

    const QByteArray bytes = file.readAll();
    QString content;
    if (textDocument) {
        QStringDecoder decoder(QStringDecoder::Utf8);
        content = decoder.decode(bytes);
        if (decoder.hasError()) {
            QMessageBox::warning(this,
                                 QStringLiteral("导入文档"),
                                 QStringLiteral("文件不是有效 UTF-8 文本，请另存为 UTF-8 后再导入。"));
            return;
        }
    }

    // 两个页面共用一个后端导入接口。导入期间统一禁用入口，避免异步回调失去归属。
    pendingWorkspaceImportTarget = target;
    ui->attachButton->setEnabled(false);
    ui->documentImportButton->setEnabled(false);
    if (pdfProcessingImportButton) {
        pdfProcessingImportButton->setEnabled(false);
    }
    if (pdfProcessingTarget) {
        pdfProcessingWorkspaceLoading = true;
        if (pdfProcessingStatusLabel) {
            pdfProcessingStatusLabel->setText(QStringLiteral("正在导入 PDF"));
            polishBadge(pdfProcessingStatusLabel, QStringLiteral("badgeBlue"));
        }
    } else if (target == QStringLiteral("document")) {
        ui->documentRunStatus->setText(QStringLiteral("正在导入文档"));
        polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    } else if (knowledgeTarget) {
        ui->knowledgeImportButton->setEnabled(false);
        ui->knowledgeIndexStatus->setText(QStringLiteral("正在导入受控材料，完成后可建立索引。"));
        ui->knowledgeIndexBadge->setText(QStringLiteral("导入中"));
        polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeBlue"));
    } else {
        ui->dispatchChatStatus->setText(QStringLiteral("导入文档"));
    }
    if (textDocument) {
        backendClient->importWorkspaceDocument(fileInfo.fileName(), content);
    } else {
        backendClient->importWorkspaceBinaryDocument(fileInfo.fileName(), bytes);
    }
}

void MainWindow::handleWorkspaceDocumentImported(const WorkspaceDocumentInfo &document)
{
    ui->attachButton->setEnabled(true);
    ui->documentImportButton->setEnabled(true);

    const bool pdfProcessingTarget = pendingWorkspaceImportTarget == QStringLiteral("pdf_processing");
    const bool documentTarget = pendingWorkspaceImportTarget == QStringLiteral("document");
    const bool knowledgeTarget = pendingWorkspaceImportTarget == QStringLiteral("knowledge");
    const QString knowledgeBaseId = pendingKnowledgeImportBaseId;
    pendingWorkspaceImportTarget.clear();
    if (pdfProcessingTarget) {
        pdfProcessingWorkspaceLoading = true;
        if (pdfProcessingStatusLabel) {
            pdfProcessingStatusLabel->setText(QStringLiteral("PDF 已导入，正在刷新文件列表"));
            polishBadge(pdfProcessingStatusLabel, QStringLiteral("badgeBlue"));
        }
        backendClient->requestWorkspaceDocuments();
        return;
    }
    if (documentTarget) {
        pendingDocumentSelection = document.relativePath.isEmpty() ? document.name : document.relativePath;
        ui->documentRunStatus->setText(QStringLiteral("文档已导入，正在刷新列表"));
        polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
        refreshDocumentAgentDocuments();
        return;
    }
    if (knowledgeTarget) {
        if (knowledgeBaseId.isEmpty()) {
            pendingKnowledgeImportBaseId.clear();
            ui->knowledgeImportButton->setEnabled(true);
            ui->knowledgeIndexStatus->setText(QStringLiteral("导入归属已失效，请重新选择资料库后再试。"));
            ui->knowledgeIndexBadge->setText(QStringLiteral("需要重试"));
            polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeOrange"));
            return;
        }
        // workspace 导入成功后才会执行第二次受控复制；这里传递的是后端确认过的相对名称，
        // 不把 QFileDialog 的源绝对路径保存在 Qt 状态或发送到资料库接口。
        const QString documentName = document.relativePath.isEmpty() ? document.name : document.relativePath;
        ui->knowledgeIndexStatus->setText(QStringLiteral("正在复制材料到资料库私有目录。"));
        backendClient->importWorkspaceDocumentsToKnowledgeBase(knowledgeBaseId, {documentName});
        return;
    }

    ui->dispatchChatStatus->setText(QStringLiteral("文档已导入"));

    const QString documentName = document.relativePath.isEmpty() ? document.name : document.relativePath;
    // 附件名称同时作为本次 Commander 的显式材料绑定。后端只接收该受控相对引用，不接收
    // QFileDialog 返回的绝对源路径；发送完成后会清空，避免串到下一次独立任务。
    dispatchSelectedDocumentRef = documentName;
    updateDispatchMaterialBindingsUi();
    const QString currentText = ui->dispatchInputEdit->text().trimmed();
    const QString documentPrompt = QStringLiteral("请读取 %1，并提取主要目标、要求和待确认事项。")
                                       .arg(documentName);
    ui->dispatchInputEdit->setText(currentText.isEmpty()
                                       ? documentPrompt
                                       : QStringLiteral("%1 %2").arg(currentText, documentName));
    ui->dispatchInputEdit->setFocus();

    QString importHtml =
        QStringLiteral("<p style=\"color:#2563EB;\"><b>系统</b> · 已导入 workspace 文档：%1（%2 字节），并已填入任务输入框。</p>")
            .arg(documentName.toHtmlEscaped())
            .arg(document.sizeBytes);
    const QString preview = compactPlainPreview(document.preview);
    if (!preview.isEmpty()) {
        importHtml += QStringLiteral(
            "<div style=\"margin:2px 0 8px 0;padding:8px 10px;border-left:3px solid #93C5FD;"
            "background:#EFF6FF;color:#334155;\">预览：%1</div>")
                          .arg(preview.toHtmlEscaped());
    }
    appendConversationHtml(importHtml);
}

void MainWindow::handleWorkspaceDocumentImportFailed(const QString &message)
{
    ui->attachButton->setEnabled(true);
    ui->documentImportButton->setEnabled(true);
    const bool pdfProcessingTarget = pendingWorkspaceImportTarget == QStringLiteral("pdf_processing");
    const bool documentTarget = pendingWorkspaceImportTarget == QStringLiteral("document");
    const bool knowledgeTarget = pendingWorkspaceImportTarget == QStringLiteral("knowledge");
    pendingWorkspaceImportTarget.clear();
    if (pdfProcessingTarget) {
        pdfProcessingWorkspaceLoading = false;
        if (pdfProcessingStatusLabel) {
            pdfProcessingStatusLabel->setText(QStringLiteral("PDF 导入失败"));
            polishBadge(pdfProcessingStatusLabel, QStringLiteral("badgeOrange"));
        }
        updatePdfProcessingWorkspaceUi();
    } else if (documentTarget) {
        ui->documentRunStatus->setText(QStringLiteral("导入失败"));
        polishBadge(ui->documentRunStatus, QStringLiteral("badgeOrange"));
    } else if (knowledgeTarget) {
        pendingKnowledgeImportBaseId.clear();
        ui->knowledgeImportButton->setEnabled(!activeKnowledgeBaseId.isEmpty());
        ui->knowledgeIndexStatus->setText(QStringLiteral("材料导入失败：%1").arg(message.left(120)));
        ui->knowledgeIndexBadge->setText(QStringLiteral("导入失败"));
        polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeOrange"));
    } else {
        ui->dispatchChatStatus->setText(QStringLiteral("导入失败"));
    }
    QMessageBox::warning(this,
                         QStringLiteral("导入文档"),
                         QStringLiteral("workspace 文档导入失败：%1").arg(message));
}

void MainWindow::setupKnowledgeBase()
{
    // K1 只交付“资料入库与索引基础”。页面因此采用稳定的列表 + 详情工作台，而不是提前摆放
    // 尚未实现的问答框或虚假的检索结果；K2 检索链路完成后会在右侧详情区自然扩展。
    ui->knowledgeDetailContent->setVisible(false);
    ui->knowledgeDetailEmpty->setVisible(true);
    ui->knowledgeBaseList->setSelectionMode(QAbstractItemView::SingleSelection);
    ui->knowledgeDocumentTable->setColumnCount(4);
    ui->knowledgeDocumentTable->setRowCount(0);
    ui->knowledgeDocumentTable->verticalHeader()->setVisible(false);
    ui->knowledgeDocumentTable->verticalHeader()->setDefaultSectionSize(34);
    ui->knowledgeDocumentTable->setTextElideMode(Qt::ElideRight);
    ui->knowledgeDocumentTable->horizontalHeader()->setStretchLastSection(false);
    ui->knowledgeDocumentTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::Stretch);
    ui->knowledgeDocumentTable->horizontalHeader()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
    ui->knowledgeDocumentTable->horizontalHeader()->setSectionResizeMode(2, QHeaderView::ResizeToContents);
    ui->knowledgeDocumentTable->horizontalHeader()->setSectionResizeMode(3, QHeaderView::ResizeToContents);
    ui->knowledgeDocumentTable->horizontalHeaderItem(2)->setText(QStringLiteral("处理状态"));
    ui->knowledgeWorkbenchSplitter->setStretchFactor(0, 2);
    ui->knowledgeWorkbenchSplitter->setStretchFactor(1, 7);
    ui->knowledgeBaseCountLabel->setText(QStringLiteral("0 个"));
    ui->knowledgeIndexStatus->setText(QStringLiteral("导入材料后即可建立关键词索引。"));
    ui->knowledgeIndexBadge->setText(QStringLiteral("待处理"));
    polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeGray"));
    // 索引也属于真实后台任务。这里复用统一指示器，让静态阶段文案不会被误判为仍在执行。
    knowledgeIndexActivityIndicator = new TaskActivityIndicator(this);
    ui->knowledgeIndexStatusLayout->insertWidget(1, knowledgeIndexActivityIndicator);
    ui->knowledgeVectorStatus->setText(QStringLiteral("正在检测"));
    polishBadge(ui->knowledgeVectorStatus, QStringLiteral("badgeGray"));
    // OCR 是资料导入链的一项可选本地能力，因此与语义索引并列呈现，而不是再加一个难发现的
    // 独立页面。此处仅创建稳定展示容器，所有模型准备和状态事实仍由后端 API 驱动。
    knowledgeOcrPanel = new QFrame(ui->knowledgeDetailContent);
    knowledgeOcrPanel->setObjectName(QStringLiteral("knowledgeOcrPanel"));
    auto *ocrLayout = new QHBoxLayout(knowledgeOcrPanel);
    ocrLayout->setContentsMargins(14, 12, 14, 12);
    ocrLayout->setSpacing(12);
    auto *ocrTextLayout = new QVBoxLayout();
    ocrTextLayout->setSpacing(2);
    auto *ocrTitle = new QLabel(QStringLiteral("本地 OCR"), knowledgeOcrPanel);
    ocrTitle->setObjectName(QStringLiteral("cardTitle"));
    knowledgeOcrHint = new QLabel(
        QStringLiteral("扫描 PDF 与图片可在本机识别；这是可选组件，资料不会上传。"),
        knowledgeOcrPanel);
    knowledgeOcrHint->setObjectName(QStringLiteral("subText"));
    knowledgeOcrHint->setWordWrap(true);
    ocrTextLayout->addWidget(ocrTitle);
    ocrTextLayout->addWidget(knowledgeOcrHint);
    ocrLayout->addLayout(ocrTextLayout, 1);
    knowledgeOcrActivityIndicator = new TaskActivityIndicator(knowledgeOcrPanel);
    ocrLayout->addWidget(knowledgeOcrActivityIndicator);
    knowledgeOcrStatus = new QLabel(QStringLiteral("正在检测"), knowledgeOcrPanel);
    knowledgeOcrStatus->setObjectName(QStringLiteral("badgeGray"));
    ocrLayout->addWidget(knowledgeOcrStatus);
    knowledgePrepareOcrButton = new QPushButton(QStringLiteral("准备本地 OCR"), knowledgeOcrPanel);
    knowledgePrepareOcrButton->setObjectName(QStringLiteral("ghostButton"));
    knowledgePrepareOcrButton->setMinimumSize(116, 38);
    knowledgePrepareOcrButton->setToolTip(QStringLiteral("明确确认后准备本机 OCR 可选组件的模型权重"));
    ocrLayout->addWidget(knowledgePrepareOcrButton);
    // header 为 0、语义索引为 1；插入 2 能保证两项本地能力相邻，材料操作保持在其后。
    ui->knowledgeDetailContentLayout->insertWidget(2, knowledgeOcrPanel);
    ui->knowledgeEmptyHint->setText(
        QStringLiteral("资料库用于隔离不同项目的本机材料。创建后可导入 TXT、Markdown、PDF、DOCX 或扫描图片，并按需建立索引。"));
    ui->knowledgeImportButton->setToolTip(
        QStringLiteral("导入 TXT、Markdown、PDF、DOCX、PNG、JPG 或 JPEG 到当前资料库"));
    ui->knowledgeAskButton->setEnabled(false);
    ui->knowledgeDeepTaskButton->setEnabled(false);
    ui->knowledgeDelegateButton->setEnabled(false);

    // 索引任务由后端后台运行。Qt 只在用户正在查看任务时低频读取状态，不扫描文件、不重建索引，
    // 这样窗口保持响应，也不会让页面切换造成额外磁盘负担。
    knowledgeIndexPollTimer = new QTimer(this);
    knowledgeIndexPollTimer->setInterval(850);
    connect(knowledgeIndexPollTimer, &QTimer::timeout, this, &MainWindow::refreshKnowledgeIndexJob);
    knowledgeOcrPreparationPollTimer = new QTimer(this);
    knowledgeOcrPreparationPollTimer->setInterval(900);
    connect(knowledgeOcrPreparationPollTimer,
            &QTimer::timeout,
            this,
            &MainWindow::refreshKnowledgeOcrPreparation);
    // 删除会等待后台索引安全停止与私有副本释放，单次延迟刷新不足以表达真实终态。
    knowledgeDeletionPollTimer = new QTimer(this);
    knowledgeDeletionPollTimer->setInterval(750);
    connect(knowledgeDeletionPollTimer, &QTimer::timeout, this, [this]() {
        if (!knowledgeDeletionPending) {
            knowledgeDeletionPollTimer->stop();
            return;
        }
        refreshKnowledgeBases();
    });

    // 知识库问答与 Map-Reduce 深度分析的上下文预算和 JSON 契约不同，必须保留两条可见
    // 路由，不能因为页面共用一个 Agent 就静默共用模型。
    auto *knowledgeModelRouteMenu = new QMenu(ui->knowledgeModelRouteButton);
    QAction *knowledgeAnswerRouteAction = knowledgeModelRouteMenu->addAction(
        QStringLiteral("知识库问答模型"));
    knowledgeAnswerRouteAction->setToolTip(QStringLiteral("调整后续带来源问答使用的模型路由。"));
    QAction *knowledgeDeepRouteAction = knowledgeModelRouteMenu->addAction(
        QStringLiteral("知识库深度分析模型"));
    knowledgeDeepRouteAction->setToolTip(
        QStringLiteral("调整后续 Map-Reduce 深度分析使用的模型路由。"));
    ui->knowledgeModelRouteButton->setMenu(knowledgeModelRouteMenu);
    ui->knowledgeModelRouteButton->setPopupMode(QToolButton::InstantPopup);
    connect(knowledgeAnswerRouteAction, &QAction::triggered, this, [this]() {
        openModelRouteDialogForRoute(QStringLiteral("knowledge_answer"));
    });
    connect(knowledgeDeepRouteAction, &QAction::triggered, this, [this]() {
        openModelRouteDialogForRoute(QStringLiteral("knowledge_deep_analysis"));
    });

    connect(ui->knowledgeRefreshButton, &QPushButton::clicked, this, &MainWindow::refreshKnowledgeBases);
    connect(ui->knowledgeCreateButton, &QPushButton::clicked, this, &MainWindow::createKnowledgeBase);
    connect(ui->knowledgeBaseList,
            &QListWidget::currentItemChanged,
            this,
            [this](QListWidgetItem *, QListWidgetItem *) { selectKnowledgeBaseFromList(); });
    connect(ui->knowledgeImportButton, &QPushButton::clicked, this, &MainWindow::importKnowledgeBaseDocument);
    connect(ui->knowledgeIndexButton, &QPushButton::clicked, this, &MainWindow::startKnowledgeIndex);
    connect(ui->knowledgePrepareVectorButton, &QPushButton::clicked, this, &MainWindow::prepareKnowledgeVectorModel);
    connect(knowledgePrepareOcrButton, &QPushButton::clicked, this, &MainWindow::prepareKnowledgeOcrModel);
    connect(ui->knowledgeDeleteButton, &QPushButton::clicked, this, &MainWindow::deleteSelectedKnowledgeBase);
    connect(ui->knowledgeAskButton, &QPushButton::clicked, this, &MainWindow::openKnowledgeAnswerDialog);
    connect(ui->knowledgeDeepTaskButton, &QPushButton::clicked, this, &MainWindow::openKnowledgeDeepTaskDialog);
    connect(ui->knowledgeDelegateButton, &QPushButton::clicked, this, &MainWindow::delegateKnowledgeBaseToCommander);
    updateKnowledgeBaseDetailUi();
}

void MainWindow::refreshKnowledgeBases()
{
    if (!backendManager || !backendManager->isReady()) {
        ui->knowledgeIndexStatus->setText(QStringLiteral("后端尚未就绪，无法读取本机资料库。"));
        ui->knowledgeIndexBadge->setText(QStringLiteral("等待后端"));
        polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeGray"));
        return;
    }

    if (!knowledgeBasesLoading) {
        knowledgeBasesLoading = true;
        ui->knowledgeRefreshButton->setEnabled(false);
        ui->knowledgeRefreshButton->setText(QStringLiteral("刷新中"));
        backendClient->requestKnowledgeBases();
    }
    // 模型能力是低频元数据，不会触发模型下载；与资料库列表并行获取可缩短首次进入等待。
    backendClient->requestKnowledgeVectorCapability();
    backendClient->requestKnowledgeOcrCapability();
}

void MainWindow::selectKnowledgeBaseFromList()
{
    QListWidgetItem *item = ui->knowledgeBaseList->currentItem();
    const QString selectedId = item ? item->data(Qt::UserRole).toString() : QString();
    if (selectedId == activeKnowledgeBaseId) {
        return;
    }

    activeKnowledgeBaseId = selectedId;
    currentKnowledgeDocuments.clear();
    updateKnowledgeBaseDetailUi();
    if (!activeKnowledgeBaseId.isEmpty()) {
        refreshSelectedKnowledgeDocuments();
    }
}

void MainWindow::refreshSelectedKnowledgeDocuments()
{
    if (activeKnowledgeBaseId.isEmpty() || knowledgeDocumentsLoading || !backendManager || !backendManager->isReady()) {
        return;
    }
    knowledgeDocumentsLoading = true;
    ui->knowledgeIndexStatus->setText(QStringLiteral("正在读取资料库材料。"));
    backendClient->requestKnowledgeDocuments(activeKnowledgeBaseId);
}

void MainWindow::updateKnowledgeBaseDetailUi()
{
    const KnowledgeBaseInfo *selectedBase = nullptr;
    for (const KnowledgeBaseInfo &candidate : currentKnowledgeBases) {
        if (candidate.knowledgeBaseId == activeKnowledgeBaseId) {
            selectedBase = &candidate;
            break;
        }
    }

    const bool hasSelection = selectedBase != nullptr;
    const bool selectedBaseDeleting = hasSelection && selectedBase->status == QStringLiteral("deleting");
    const bool indexRunning = hasSelection
                              && (knowledgeIndexStarting || !activeKnowledgeIndexJobId.isEmpty()
                                  || selectedBase->status == QStringLiteral("indexing"));
    const bool selectionLocked = knowledgeDeletionPending || selectedBaseDeleting;
    const bool materialActionsLocked = selectionLocked || indexRunning;
    if (knowledgeIndexActivityIndicator) {
        knowledgeIndexActivityIndicator->setRunning(knowledgeIndexStarting || !activeKnowledgeIndexJobId.isEmpty());
    }
    ui->knowledgeDetailEmpty->setVisible(!hasSelection);
    ui->knowledgeDetailContent->setVisible(hasSelection);
    ui->knowledgeDeleteButton->setEnabled(hasSelection && !selectionLocked);
    ui->knowledgeDeleteButton->setText(knowledgeDeletionPending ? QStringLiteral("删除中")
                                                                 : QStringLiteral("删除"));
    ui->knowledgeImportButton->setEnabled(hasSelection && !materialActionsLocked);
    ui->knowledgeIndexButton->setEnabled(hasSelection && !currentKnowledgeDocuments.isEmpty()
                                         && !indexRunning && !selectionLocked);
    ui->knowledgePrepareVectorButton->setEnabled(!knowledgeVectorPreparing
                                                 && !currentKnowledgeVectorCapability.modelInitialized
                                                 && backendManager && backendManager->isReady());
    const bool answerAvailable = hasSelection && !selectionLocked && backendManager && backendManager->isReady()
                                 && selectedBase->activeIndexGeneration > 0
                                 && (selectedBase->status == QStringLiteral("ready")
                                     || selectedBase->status == QStringLiteral("partial_failure"));
    ui->knowledgeAskButton->setEnabled(answerAvailable);
    ui->knowledgeAskButton->setToolTip(
        answerAvailable
            ? QStringLiteral("在当前资料库的活动索引中提问，并查看可定位来源。")
            : QStringLiteral("建立可用索引后，才能在该资料库中提问。"));
    // K4 必须冻结一套完整 ready generation；部分失败的索引仍可用于 K3 有限问答，却不能伪装成
    // 覆盖整库的深度结论。
    const bool deepTaskAvailable = answerAvailable && selectedBase->status == QStringLiteral("ready");
    ui->knowledgeDeepTaskButton->setEnabled(deepTaskAvailable);
    ui->knowledgeDeepTaskButton->setToolTip(
        deepTaskAvailable
            ? QStringLiteral("在当前完整索引上执行可暂停、可恢复的跨文档深度分析。")
            : QStringLiteral("深度分析需要资料库处于“索引可用”状态；部分可用资料只能进行有限问答。"));
    ui->knowledgeDelegateButton->setEnabled(answerAvailable);
    ui->knowledgeDelegateButton->setToolTip(
        answerAvailable
            ? QStringLiteral("把当前资料库带到 AI 调度台；输入问题后再由总指挥规划。")
            : QStringLiteral("建立可用索引后，才能交给总指挥。"));

    if (!hasSelection) {
        ui->knowledgeSelectedTitle->setText(QStringLiteral("未选择资料库"));
        ui->knowledgeSelectedMeta->setText(QStringLiteral("创建或选择一个资料库后，可导入受控材料。"));
        ui->knowledgeSelectedStatus->setText(QStringLiteral("未选择"));
        polishBadge(ui->knowledgeSelectedStatus, QStringLiteral("badgeGray"));
        ui->knowledgeIndexButton->setText(QStringLiteral("建立索引"));
        ui->knowledgeIndexButton->setToolTip(QStringLiteral("导入材料后建立可恢复的本机关键词索引。"));
        ui->knowledgeDocumentTable->setRowCount(0);
        return;
    }

    const QString status = selectedBase->status;
    QString statusText = QStringLiteral("等待材料");
    QString statusBadge = QStringLiteral("badgeGray");
    if (status == QStringLiteral("ready")) {
        statusText = QStringLiteral("索引可用");
        statusBadge = QStringLiteral("badgeGreen");
    } else if (status == QStringLiteral("indexing")) {
        statusText = QStringLiteral("索引中");
        statusBadge = QStringLiteral("badgeBlue");
    } else if (status == QStringLiteral("partial_failure")) {
        statusText = QStringLiteral("部分可用");
        statusBadge = QStringLiteral("badgeOrange");
    } else if (status == QStringLiteral("failed")) {
        statusText = QStringLiteral("索引失败");
        statusBadge = QStringLiteral("badgeOrange");
    } else if (status == QStringLiteral("deleting")) {
        statusText = QStringLiteral("正在删除");
        statusBadge = QStringLiteral("badgeOrange");
    } else if (status == QStringLiteral("empty")) {
        statusText = QStringLiteral("等待材料");
    }
    ui->knowledgeSelectedTitle->setText(selectedBase->name);
    const QString indexVersionText = selectedBase->activeIndexGeneration > 0
                                         ? QStringLiteral("已切分并建立索引 v%1").arg(selectedBase->activeIndexGeneration)
                                         : QStringLiteral("尚未建立索引");
    ui->knowledgeSelectedMeta->setText(
        selectedBase->description.isEmpty()
            ? QStringLiteral("%1 · %2 份活动材料 · 最近更新 %3")
                  .arg(indexVersionText,
                       QString::number(selectedBase->activeDocumentVersionCount),
                       selectedBase->updatedAt)
            : QStringLiteral("%1 · %2 · %3 份活动材料 · 最近更新 %4")
                  .arg(selectedBase->description,
                       indexVersionText,
                       QString::number(selectedBase->activeDocumentVersionCount),
                       selectedBase->updatedAt));
    ui->knowledgeSelectedStatus->setText(statusText);
    polishBadge(ui->knowledgeSelectedStatus, statusBadge);

    ui->knowledgeDocumentTable->setRowCount(currentKnowledgeDocuments.size());
    for (int row = 0; row < currentKnowledgeDocuments.size(); ++row) {
        const KnowledgeDocumentInfo &document = currentKnowledgeDocuments.at(row);
        QString processingState;
        QString processingToolTip;
        if (document.activeVersionId.isEmpty()) {
            processingState = QStringLiteral("待切分 / 索引");
            processingToolTip = QStringLiteral("材料已受控导入，尚未建立资料库索引。");
        } else if (document.activeOcrPageCount > 0) {
            processingState = QStringLiteral("OCR %1/%2 页可用")
                                  .arg(document.activeOcrCompletedPageCount)
                                  .arg(document.activeOcrPageCount);
            processingToolTip = document.activeOcrFailedPageCount > 0
                                    ? document.activeFailureSummary
                                    : QStringLiteral("本地 OCR 已完成，识别文本保留页码和区域来源。");
        } else {
            processingState = QStringLiteral("已切分 / 已索引");
            processingToolTip = QStringLiteral("材料已完成受控解析并进入当前资料库索引。");
        }
        const QStringList cells = {
            document.displayName,
            document.documentType.toUpper(),
            processingState,
            document.updatedAt,
        };
        for (int column = 0; column < cells.size(); ++column) {
            auto *cell = new QTableWidgetItem(cells.at(column));
            cell->setToolTip(column == 2 ? processingToolTip : cells.at(column));
            ui->knowledgeDocumentTable->setItem(row, column, cell);
        }
    }

    if (activeKnowledgeIndexJobId.isEmpty()) {
        if (status == QStringLiteral("ready")) {
            ui->knowledgeIndexStatus->setText(
                QStringLiteral("当前索引已就绪。再次建立会生成新版本，旧版本会在新版本激活后替换。"));
            ui->knowledgeIndexBadge->setText(QStringLiteral("可用"));
            polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeGreen"));
        } else if (status == QStringLiteral("deleting")) {
            ui->knowledgeIndexStatus->setText(QStringLiteral("正在撤销索引并清理资料库私有副本；原文件不会删除。"));
            ui->knowledgeIndexBadge->setText(QStringLiteral("删除中"));
            polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeOrange"));
        } else if (!knowledgeDocumentsLoading && currentKnowledgeDocuments.isEmpty()) {
            ui->knowledgeIndexStatus->setText(QStringLiteral("导入材料后即可建立关键词索引。"));
            ui->knowledgeIndexBadge->setText(QStringLiteral("待材料"));
            polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeGray"));
        } else if (!knowledgeDocumentsLoading && !currentKnowledgeDocuments.isEmpty()
                   && status != QStringLiteral("indexing")) {
            // 材料列表读取完成后必须覆盖临时的“正在读取”文案。导入只会产生待处理版本，
            // 客户仍需明确点击建立索引，不能让页面看起来像后台在无止境自动工作。
            ui->knowledgeIndexStatus->setText(
                QStringLiteral("已导入 %1 份材料，尚未建立索引。")
                    .arg(currentKnowledgeDocuments.size()));
            ui->knowledgeIndexBadge->setText(QStringLiteral("待建立索引"));
            polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeBlue"));
        }
    }

    if (status == QStringLiteral("ready")) {
        ui->knowledgeIndexButton->setText(QStringLiteral("重新建立索引"));
        ui->knowledgeIndexButton->setToolTip(
            QStringLiteral("当前已使用索引 v%1。仅在导入或替换材料后才需要重新建立。")
                .arg(selectedBase->activeIndexGeneration));
    } else if (indexRunning) {
        ui->knowledgeIndexButton->setText(QStringLiteral("索引进行中"));
        ui->knowledgeIndexButton->setToolTip(QStringLiteral("当前索引任务仍在后台执行，可等待状态变为已完成。"));
    } else {
        ui->knowledgeIndexButton->setText(QStringLiteral("建立索引"));
        ui->knowledgeIndexButton->setToolTip(QStringLiteral("为当前资料库建立可恢复的关键词索引。"));
    }

    if (currentKnowledgeVectorCapability.modelInitialized) {
        ui->knowledgeVectorStatus->setText(QStringLiteral("已就绪"));
        ui->knowledgePrepareVectorButton->setText(QStringLiteral("本机模型已就绪"));
        polishBadge(ui->knowledgeVectorStatus, QStringLiteral("badgeGreen"));
    } else if (currentKnowledgeVectorCapability.chromaAvailable && currentKnowledgeVectorCapability.fastembedAvailable) {
        ui->knowledgeVectorStatus->setText(QStringLiteral("可选"));
        ui->knowledgePrepareVectorButton->setText(QStringLiteral("准备本机模型"));
        polishBadge(ui->knowledgeVectorStatus, QStringLiteral("badgeBlue"));
    } else {
        ui->knowledgeVectorStatus->setText(QStringLiteral("不可用"));
        ui->knowledgePrepareVectorButton->setText(QStringLiteral("检查依赖"));
        polishBadge(ui->knowledgeVectorStatus, QStringLiteral("badgeOrange"));
    }
    if (!currentKnowledgeVectorCapability.message.trimmed().isEmpty()) {
        ui->knowledgeVectorHint->setText(currentKnowledgeVectorCapability.message);
    }
    // 模型准备是低频系统动作，不应长期挤占资料和结果的主阅读区。失败/未准备/进行中仍保留，
    // 让用户能重试和理解当前状态；成功后只保留后台能力事实，不再显示整块配置卡。
    const bool showVectorSetup = !currentKnowledgeVectorCapability.modelInitialized || knowledgeVectorPreparing;
    ui->knowledgeVectorPanel->setVisible(showVectorSetup);
    ui->knowledgeVectorPanel->setMaximumHeight(knowledgeVectorPreparing ? QWIDGETSIZE_MAX : 76);
    updateKnowledgeOcrUi();
}

void MainWindow::createKnowledgeBase()
{
    if (knowledgeBasesLoading || !backendManager || !backendManager->isReady()) {
        return;
    }
    QDialog dialog(this);
    dialog.setWindowTitle(QStringLiteral("新建资料库"));
    dialog.setMinimumWidth(460);
    auto *layout = new QVBoxLayout(&dialog);
    auto *hint = new QLabel(
        QStringLiteral("资料库用于隔离项目材料、索引和未来检索范围。名称创建后仍可通过新建资料库调整分类。"),
        &dialog);
    hint->setObjectName(QStringLiteral("subText"));
    hint->setWordWrap(true);
    auto *nameInput = new QLineEdit(&dialog);
    nameInput->setPlaceholderText(QStringLiteral("例如：产品需求资料库"));
    auto *descriptionInput = new QPlainTextEdit(&dialog);
    descriptionInput->setPlaceholderText(QStringLiteral("可选：写一句用途说明，方便以后识别。"));
    descriptionInput->setFixedHeight(82);
    auto *form = new QFormLayout();
    form->addRow(QStringLiteral("资料库名称"), nameInput);
    form->addRow(QStringLiteral("用途说明"), descriptionInput);
    auto *buttons = new QDialogButtonBox(QDialogButtonBox::Cancel, &dialog);
    QPushButton *createButton = buttons->addButton(QStringLiteral("创建资料库"), QDialogButtonBox::AcceptRole);
    createButton->setObjectName(QStringLiteral("primaryButton"));
    layout->addWidget(hint);
    layout->addLayout(form);
    layout->addWidget(buttons);
    connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    connect(buttons, &QDialogButtonBox::accepted, &dialog, [&dialog, nameInput]() {
        if (nameInput->text().trimmed().isEmpty()) {
            QMessageBox::warning(&dialog, QStringLiteral("新建资料库"), QStringLiteral("请先输入资料库名称。"));
            return;
        }
        dialog.accept();
    });
    nameInput->setFocus();
    if (dialog.exec() != QDialog::Accepted) {
        return;
    }
    knowledgeBasesLoading = true;
    ui->knowledgeCreateButton->setEnabled(false);
    ui->knowledgeCreateButton->setText(QStringLiteral("创建中"));
    ui->knowledgeIndexStatus->setText(QStringLiteral("正在创建资料库。"));
    ui->knowledgeIndexBadge->setText(QStringLiteral("创建中"));
    polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeBlue"));
    backendClient->createKnowledgeBase(nameInput->text(), descriptionInput->toPlainText());
}

void MainWindow::importKnowledgeBaseDocument()
{
    if (activeKnowledgeBaseId.isEmpty() || knowledgeDeletionPending || knowledgeDocumentsLoading) {
        return;
    }
    pendingKnowledgeImportBaseId = activeKnowledgeBaseId;
    importWorkspaceDocumentForTarget(QStringLiteral("knowledge"));
    if (pendingWorkspaceImportTarget != QStringLiteral("knowledge")) {
        // 用户取消文件选择或文件未通过前端格式校验时，不保留过期归属。
        pendingKnowledgeImportBaseId.clear();
    }
}

void MainWindow::startKnowledgeIndex()
{
    if (activeKnowledgeBaseId.isEmpty() || currentKnowledgeDocuments.isEmpty() || knowledgeIndexStarting
        || knowledgeDeletionPending) {
        return;
    }
    knowledgeIndexStarting = true;
    if (knowledgeIndexActivityIndicator) {
        knowledgeIndexActivityIndicator->setRunning(true);
    }
    ui->knowledgeIndexButton->setEnabled(false);
    ui->knowledgeImportButton->setEnabled(false);
    ui->knowledgeIndexButton->setText(QStringLiteral("提交中"));
    ui->knowledgeIndexStatus->setText(QStringLiteral("正在提交索引任务。"));
    ui->knowledgeIndexBadge->setText(QStringLiteral("准备中"));
    polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeBlue"));
    backendClient->startKnowledgeIndex(activeKnowledgeBaseId);
}

void MainWindow::refreshKnowledgeIndexJob()
{
    if (activeKnowledgeIndexJobId.isEmpty()) {
        knowledgeIndexPollTimer->stop();
        return;
    }
    backendClient->requestKnowledgeIndexJob(activeKnowledgeIndexJobId);
}

void MainWindow::prepareKnowledgeVectorModel()
{
    if (knowledgeVectorPreparing || currentKnowledgeVectorCapability.modelInitialized) {
        return;
    }
    const QMessageBox::StandardButton answer = QMessageBox::question(
        this,
        QStringLiteral("准备本机语义模型"),
        QStringLiteral("首次准备会下载约 91MB 的中文嵌入模型到 AgentFlow 本机缓存。\n\n"
                       "它只用于后续语义检索，不会上传你的资料；关键词索引仍可在不下载模型的情况下使用。\n\n"
                       "现在开始下载吗？"),
        QMessageBox::Yes | QMessageBox::Cancel,
        QMessageBox::Cancel);
    if (answer != QMessageBox::Yes) {
        return;
    }
    knowledgeVectorPreparing = true;
    ui->knowledgePrepareVectorButton->setEnabled(false);
    ui->knowledgeVectorStatus->setText(QStringLiteral("准备中"));
    polishBadge(ui->knowledgeVectorStatus, QStringLiteral("badgeBlue"));
    ui->knowledgeVectorHint->setText(QStringLiteral("正在下载并初始化本机模型；窗口可继续使用。"));
    backendClient->prepareKnowledgeVectorModel();
}

void MainWindow::prepareKnowledgeOcrModel()
{
    if (knowledgeOcrPreparing || currentKnowledgeOcrCapability.modelInitialized) {
        return;
    }
    const bool needsDependencyInstall = !currentKnowledgeOcrCapability.paddleocrAvailable;
    const QMessageBox::StandardButton answer = QMessageBox::question(
        this,
        needsDependencyInstall ? QStringLiteral("安装并准备本地 OCR") : QStringLiteral("准备本地 OCR"),
        needsDependencyInstall
            ? QStringLiteral("本次将在 AgentFlow 的本机 Python 环境安装固定的 OCR 可选组件，"
                             "预计占用约 850MB 磁盘空间；随后下载并初始化约 29MB 模型权重。\n\n"
                             "- 仅安装固定的 PaddleOCR/PaddlePaddle 依赖，不执行其它命令\n"
                             "- 仅在安装与模型准备时联网；不会读取或上传任何资料\n"
                             "- 导入、建立索引和普通使用仍不会自动安装或下载\n"
                             "- 安装可能需要几分钟，窗口可以继续使用\n\n"
                             "现在开始吗？")
            : QStringLiteral("本次将准备约 29MB 的本地 OCR 模型权重。\n\n"
                             "- 仅在本机处理扫描 PDF 和图片中的文字\n"
                             "- 不会上传已导入材料\n"
                             "- 导入与建立索引不会自动下载模型\n\n"
                             "现在开始准备吗？"),
        QMessageBox::Yes | QMessageBox::Cancel,
        QMessageBox::Cancel);
    if (answer != QMessageBox::Yes) {
        return;
    }
    currentKnowledgeOcrPreparation = KnowledgeOcrPreparationInfo{};
    currentKnowledgeOcrPreparation.status = QStringLiteral("queued");
    currentKnowledgeOcrPreparation.message = needsDependencyInstall
                                                ? QStringLiteral("本地 OCR 安装与准备已提交，正在等待本机任务开始。")
                                                : QStringLiteral("本地 OCR 准备已提交，正在等待本机任务开始。");
    knowledgeOcrPreparing = true;
    updateKnowledgeOcrUi();
    backendClient->prepareKnowledgeOcrModel();
}

void MainWindow::refreshKnowledgeOcrPreparation()
{
    if (activeKnowledgeOcrPreparationId.isEmpty()) {
        knowledgeOcrPreparationPollTimer->stop();
        return;
    }
    backendClient->requestKnowledgeOcrPreparation(activeKnowledgeOcrPreparationId);
}

void MainWindow::updateKnowledgeOcrUi()
{
    if (!knowledgeOcrPanel || !knowledgeOcrStatus || !knowledgeOcrHint || !knowledgePrepareOcrButton) {
        return;
    }
    if (knowledgeOcrActivityIndicator) {
        knowledgeOcrActivityIndicator->setRunning(knowledgeOcrPreparing);
    }
    if (knowledgeOcrPreparing) {
        knowledgeOcrPanel->setVisible(true);
        knowledgeOcrPanel->setMaximumHeight(QWIDGETSIZE_MAX);
        knowledgeOcrStatus->setText(
            currentKnowledgeOcrPreparation.status == QStringLiteral("queued") ? QStringLiteral("等待开始")
                                                                              : QStringLiteral("准备中"));
        polishBadge(knowledgeOcrStatus, QStringLiteral("badgeBlue"));
        knowledgeOcrHint->setText(currentKnowledgeOcrPreparation.message.isEmpty()
                                      ? QStringLiteral("正在安装或准备本地 OCR；窗口可继续使用，资料不会上传。")
                                      : currentKnowledgeOcrPreparation.message);
        knowledgePrepareOcrButton->setText(QStringLiteral("准备中"));
        knowledgePrepareOcrButton->setEnabled(false);
        return;
    }
    if (currentKnowledgeOcrCapability.modelInitialized) {
        knowledgeOcrPanel->setVisible(false);
        knowledgeOcrStatus->setText(QStringLiteral("已就绪"));
        polishBadge(knowledgeOcrStatus, QStringLiteral("badgeGreen"));
        knowledgeOcrHint->setText(QStringLiteral("扫描 PDF 和图片会在本机识别；原始材料不会上传。"));
        knowledgePrepareOcrButton->setText(QStringLiteral("本地 OCR 已就绪"));
        knowledgePrepareOcrButton->setEnabled(false);
        return;
    }
    if (!currentKnowledgeOcrPreparation.message.isEmpty()
        && currentKnowledgeOcrPreparation.status == QStringLiteral("failed")) {
        knowledgeOcrStatus->setText(QStringLiteral("未完成"));
        polishBadge(knowledgeOcrStatus, QStringLiteral("badgeOrange"));
        knowledgeOcrHint->setText(currentKnowledgeOcrPreparation.message);
    } else if (currentKnowledgeOcrCapability.paddleocrAvailable) {
        knowledgeOcrStatus->setText(QStringLiteral("可选"));
        polishBadge(knowledgeOcrStatus, QStringLiteral("badgeBlue"));
        knowledgeOcrHint->setText(currentKnowledgeOcrCapability.message.isEmpty()
                                      ? QStringLiteral("确认后准备约 29MB 模型；仅在本机识别扫描件。")
                                      : currentKnowledgeOcrCapability.message);
    } else {
        knowledgeOcrStatus->setText(QStringLiteral("需要组件"));
        polishBadge(knowledgeOcrStatus, QStringLiteral("badgeOrange"));
        knowledgeOcrHint->setText(
            QStringLiteral("OCR 是可选本地组件；确认后可安装并准备，导入不会自动下载。"));
    }
    const bool backendReady = backendManager && backendManager->isReady();
    knowledgeOcrPanel->setVisible(true);
    knowledgeOcrPanel->setMaximumHeight(76);
    knowledgePrepareOcrButton->setText(currentKnowledgeOcrCapability.paddleocrAvailable
                                           ? QStringLiteral("准备本地 OCR")
                                           : QStringLiteral("安装并准备 OCR"));
    knowledgePrepareOcrButton->setObjectName(currentKnowledgeOcrCapability.paddleocrAvailable
                                                  ? QStringLiteral("ghostButton")
                                                  : QStringLiteral("primaryButton"));
    knowledgePrepareOcrButton->style()->unpolish(knowledgePrepareOcrButton);
    knowledgePrepareOcrButton->style()->polish(knowledgePrepareOcrButton);
    knowledgePrepareOcrButton->setEnabled(backendReady);
}

void MainWindow::deleteSelectedKnowledgeBase()
{
    if (activeKnowledgeBaseId.isEmpty() || knowledgeDeletionPending) {
        return;
    }
    const QString name = ui->knowledgeSelectedTitle->text();
    QMessageBox confirmation(
        QMessageBox::Warning,
        QStringLiteral("删除资料库"),
        QStringLiteral("将删除“%1”的受控副本、索引和资料库任务记录。\n\n原始导入文件仍保留在 workspace，不会删除。此操作无法撤销。")
            .arg(name),
        QMessageBox::NoButton,
        this);
    QPushButton *deleteButton = confirmation.addButton(QStringLiteral("删除资料库"), QMessageBox::DestructiveRole);
    confirmation.addButton(QStringLiteral("取消"), QMessageBox::RejectRole);
    confirmation.exec();
    if (confirmation.clickedButton() != deleteButton) {
        return;
    }
    knowledgeDeletionPending = true;
    updateKnowledgeBaseDetailUi();
    ui->knowledgeIndexStatus->setText(QStringLiteral("正在受理删除；若索引正在运行，将先安全停止后再清理。"));
    backendClient->deleteKnowledgeBase(activeKnowledgeBaseId);
}

void MainWindow::openKnowledgeAnswerDialog()
{
    const KnowledgeBaseInfo *selectedBase = nullptr;
    for (const KnowledgeBaseInfo &candidate : currentKnowledgeBases) {
        if (candidate.knowledgeBaseId == activeKnowledgeBaseId) {
            selectedBase = &candidate;
            break;
        }
    }
    if (selectedBase == nullptr || selectedBase->activeIndexGeneration <= 0) {
        ui->knowledgeIndexStatus->setText(QStringLiteral("请先选择并建立一个可用的资料库索引。"));
        ui->knowledgeIndexBadge->setText(QStringLiteral("需要索引"));
        polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeOrange"));
        return;
    }

    // 阅读长回答与来源属于独立任务工作台，不能挤压资料导入/索引的生命周期管理区。
    auto *dialog = new KnowledgeAnswerDialog(
        backendClient,
        selectedBase->knowledgeBaseId,
        selectedBase->name,
        this);
    dialog->setAttribute(Qt::WA_DeleteOnClose);
    connect(dialog,
            &KnowledgeAnswerDialog::openTaskHistoryRequested,
            this,
            [this, dialog](const QString &taskId) {
                dialog->accept();
                openTaskInHistory(taskId);
            });
    dialog->open();
}

void MainWindow::openKnowledgeDeepTaskDialog()
{
    const KnowledgeBaseInfo *selectedBase = nullptr;
    for (const KnowledgeBaseInfo &candidate : currentKnowledgeBases) {
        if (candidate.knowledgeBaseId == activeKnowledgeBaseId) {
            selectedBase = &candidate;
            break;
        }
    }
    if (selectedBase == nullptr || selectedBase->activeIndexGeneration <= 0
        || selectedBase->status != QStringLiteral("ready")) {
        ui->knowledgeIndexStatus->setText(QStringLiteral("深度分析需要选择一个索引完整可用的资料库。"));
        ui->knowledgeIndexBadge->setText(QStringLiteral("需要完整索引"));
        polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeOrange"));
        return;
    }

    // 长任务在独立工作台处理，避免把任务控制、部分结果和范围检查器挤入资料库生命周期主页面。
    auto *dialog = new KnowledgeDeepTaskDialog(
        backendClient,
        selectedBase->knowledgeBaseId,
        selectedBase->name,
        currentKnowledgeDocuments,
        this);
    dialog->setAttribute(Qt::WA_DeleteOnClose);
    connect(dialog,
            &KnowledgeDeepTaskDialog::openTaskHistoryRequested,
            this,
            [this, dialog](const QString &taskId) {
                dialog->accept();
                openTaskInHistory(taskId);
            });
    dialog->open();
}

void MainWindow::openKnowledgeDeepTaskDialogForExistingTask(const QString &taskId)
{
    const QString normalizedTaskId = taskId.trimmed();
    if (normalizedTaskId.isEmpty()) {
        return;
    }

    // 关联 K4 的所有权属于知识库任务本身。历史页只提供深链接，不复制范围、checkpoint 或控制逻辑。
    auto *dialog = new KnowledgeDeepTaskDialog(backendClient, QString(), QString(), {}, this);
    dialog->setAttribute(Qt::WA_DeleteOnClose);
    connect(dialog,
            &KnowledgeDeepTaskDialog::openTaskHistoryRequested,
            this,
            [this, dialog](const QString &linkedTaskId) {
                dialog->accept();
                openTaskInHistory(linkedTaskId);
            });
    dialog->openExistingTask(normalizedTaskId);
    dialog->open();
}

void MainWindow::delegateKnowledgeBaseToCommander()
{
    const KnowledgeBaseInfo *selectedBase = nullptr;
    for (const KnowledgeBaseInfo &candidate : currentKnowledgeBases) {
        if (candidate.knowledgeBaseId == activeKnowledgeBaseId) {
            selectedBase = &candidate;
            break;
        }
    }

    const bool answerAvailable = selectedBase != nullptr && !knowledgeDeletionPending && backendManager
                                 && backendManager->isReady() && selectedBase->activeIndexGeneration > 0
                                 && (selectedBase->status == QStringLiteral("ready")
                                     || selectedBase->status == QStringLiteral("partial_failure"));
    if (!answerAvailable) {
        ui->knowledgeIndexStatus->setText(QStringLiteral("请先选择并建立一个可用的资料库索引。"));
        ui->knowledgeIndexBadge->setText(QStringLiteral("需要索引"));
        polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeOrange"));
        return;
    }

    // 从知识库工作台跳转时，当前资料库是客户刚刚明确选择的唯一知识来源。替换掉旧的
    // 文档/数据绑定和 @ 偏好，避免“资料库”被宽泛的“资料”词误路由到文档助手；需要
    // 多材料协作时仍可从调度台材料选择器显式添加。
    dispatchSelectedDocumentRef.clear();
    dispatchSelectedDatasetRef.clear();
    removeDispatchAgentHint(QStringLiteral("document_agent"));
    removeDispatchAgentHint(QStringLiteral("data_agent"));
    insertDispatchAgentHint(QStringLiteral("knowledge_agent"));
    dispatchSelectedKnowledgeBaseId = selectedBase->knowledgeBaseId;
    updateDispatchMaterialBindingsUi();
    switchPage(1);

    const QString prompt = QStringLiteral("请根据资料库“%1”回答：").arg(selectedBase->name);
    if (ui->dispatchInputEdit->text().trimmed().isEmpty()) {
        ui->dispatchInputEdit->setText(prompt);
        ui->dispatchInputEdit->setCursorPosition(prompt.size());
    }
    ui->dispatchInputEdit->setFocus();
    ui->dispatchChatStatus->setText(QStringLiteral("已选择资料库“%1”，请输入问题后发送。").arg(selectedBase->name));
}

void MainWindow::handleKnowledgeBasesReceived(const KnowledgeBaseListResult &result)
{
    knowledgeBasesLoading = false;
    ui->knowledgeRefreshButton->setEnabled(true);
    ui->knowledgeRefreshButton->setText(QStringLiteral("刷新"));
    ui->knowledgeCreateButton->setEnabled(true);
    ui->knowledgeCreateButton->setText(QStringLiteral("新建资料库"));
    currentKnowledgeBases = result.knowledgeBases;
    if (dispatchMaterialDialog && dispatchMaterialDialog->isVisible()) {
        dispatchMaterialKnowledgePending = false;
        dispatchMaterialDialog->setKnowledgeBases(currentKnowledgeBases);
        updateDispatchMaterialCatalogStatus();
    }
    ui->knowledgeBaseCountLabel->setText(QStringLiteral("%1 个").arg(currentKnowledgeBases.size()));

    QString desiredId = activeKnowledgeBaseId;
    bool stillExists = false;
    for (const KnowledgeBaseInfo &base : currentKnowledgeBases) {
        if (base.knowledgeBaseId == desiredId) {
            stillExists = true;
            break;
        }
    }
    if (!stillExists) {
        desiredId = currentKnowledgeBases.isEmpty() ? QString() : currentKnowledgeBases.first().knowledgeBaseId;
        knowledgeDeletionPending = false;
    }
    if (!knowledgeDeletionPending && knowledgeDeletionPollTimer) {
        knowledgeDeletionPollTimer->stop();
    }

    QSignalBlocker blocker(ui->knowledgeBaseList);
    ui->knowledgeBaseList->clear();
    for (const KnowledgeBaseInfo &base : currentKnowledgeBases) {
        QString stateLabel;
        if (base.status == QStringLiteral("ready")) {
            stateLabel = QStringLiteral("已索引 v%1").arg(base.activeIndexGeneration);
        } else if (base.status == QStringLiteral("indexing")) {
            stateLabel = QStringLiteral("索引中");
        } else if (base.status == QStringLiteral("partial_failure")) {
            stateLabel = QStringLiteral("部分可用");
        } else if (base.status == QStringLiteral("failed")) {
            stateLabel = QStringLiteral("索引失败");
        } else {
            stateLabel = QStringLiteral("待索引");
        }
        auto *item = new QListWidgetItem(QStringLiteral("%1  [%2]").arg(base.name, stateLabel), ui->knowledgeBaseList);
        item->setData(Qt::UserRole, base.knowledgeBaseId);
        item->setToolTip(base.description.isEmpty()
                             ? QStringLiteral("%1 · %2").arg(base.status, base.updatedAt)
                             : QStringLiteral("%1\n%2 · %3").arg(base.description, base.status, base.updatedAt));
        if (base.knowledgeBaseId == desiredId) {
            ui->knowledgeBaseList->setCurrentItem(item);
        }
    }
    activeKnowledgeBaseId = desiredId;
    currentKnowledgeDocuments.clear();
    updateKnowledgeBaseDetailUi();
    // 由调度台材料选择器发起的目录同步不应在后台展开资料库详情；切回知识库页时仍会
    // 主动刷新当前选择，避免为一个下拉框额外读取所有资料库材料。
    if (!activeKnowledgeBaseId.isEmpty() && ui->contentStack->currentIndex() == 9) {
        refreshSelectedKnowledgeDocuments();
    }
}

void MainWindow::handleKnowledgeBasesFailed(const QString &message)
{
    knowledgeBasesLoading = false;
    if (dispatchMaterialDialog && dispatchMaterialDialog->isVisible()) {
        dispatchMaterialKnowledgePending = false;
        if (dispatchMaterialCatalogError.isEmpty()) {
            dispatchMaterialCatalogError = QStringLiteral("资料库：%1").arg(message);
        }
        updateDispatchMaterialCatalogStatus();
    }
    ui->knowledgeRefreshButton->setEnabled(true);
    ui->knowledgeRefreshButton->setText(QStringLiteral("刷新"));
    ui->knowledgeCreateButton->setEnabled(true);
    ui->knowledgeCreateButton->setText(QStringLiteral("新建资料库"));
    ui->knowledgeIndexStatus->setText(QStringLiteral("无法读取资料库：%1").arg(message.left(140)));
    ui->knowledgeIndexBadge->setText(QStringLiteral("加载失败"));
    polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeOrange"));
}

void MainWindow::handleKnowledgeBaseCreated(const KnowledgeBaseInfo &knowledgeBase)
{
    knowledgeBasesLoading = false;
    activeKnowledgeBaseId = knowledgeBase.knowledgeBaseId;
    ui->knowledgeCreateButton->setEnabled(true);
    ui->knowledgeCreateButton->setText(QStringLiteral("新建资料库"));
    ui->knowledgeIndexStatus->setText(QStringLiteral("资料库已创建，可开始导入材料。"));
    ui->knowledgeIndexBadge->setText(QStringLiteral("已创建"));
    polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeBlue"));
    refreshKnowledgeBases();
}

void MainWindow::handleKnowledgeBaseCreateFailed(const QString &message)
{
    knowledgeBasesLoading = false;
    ui->knowledgeCreateButton->setEnabled(true);
    ui->knowledgeCreateButton->setText(QStringLiteral("新建资料库"));
    ui->knowledgeIndexStatus->setText(QStringLiteral("资料库创建失败：%1").arg(message.left(120)));
    ui->knowledgeIndexBadge->setText(QStringLiteral("创建失败"));
    polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeOrange"));
    QMessageBox::warning(this, QStringLiteral("新建资料库"), QStringLiteral("无法创建资料库：%1").arg(message));
}

void MainWindow::handleKnowledgeDocumentsReceived(const KnowledgeDocumentListResult &result)
{
    if (result.knowledgeBaseId != activeKnowledgeBaseId) {
        // 用户在请求返回前切换了资料库。旧响应不能覆盖当前视图，但必须释放加载锁并继续读取
        // 新选择，否则页面会停在“正在读取”且无法恢复。
        knowledgeDocumentsLoading = false;
        refreshSelectedKnowledgeDocuments();
        return;
    }
    knowledgeDocumentsLoading = false;
    currentKnowledgeDocuments = result.documents;
    updateKnowledgeBaseDetailUi();
}

void MainWindow::handleKnowledgeDocumentsFailed(const QString &message)
{
    knowledgeDocumentsLoading = false;
    ui->knowledgeIndexStatus->setText(QStringLiteral("无法读取材料：%1").arg(message.left(140)));
    ui->knowledgeIndexBadge->setText(QStringLiteral("加载失败"));
    polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeOrange"));
    updateKnowledgeBaseDetailUi();
}

void MainWindow::handleKnowledgeDocumentsImported(const QString &knowledgeBaseId)
{
    pendingKnowledgeImportBaseId.clear();
    ui->knowledgeImportButton->setEnabled(!activeKnowledgeBaseId.isEmpty());
    if (knowledgeBaseId == activeKnowledgeBaseId) {
        currentKnowledgeDocuments.clear();
        ui->knowledgeIndexStatus->setText(QStringLiteral("材料已入库。确认无误后可建立索引。"));
        ui->knowledgeIndexBadge->setText(QStringLiteral("待建立索引"));
        polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeBlue"));
        refreshSelectedKnowledgeDocuments();
    }
    refreshKnowledgeBases();
}

void MainWindow::handleKnowledgeDocumentsImportFailed(const QString &message)
{
    pendingKnowledgeImportBaseId.clear();
    ui->knowledgeImportButton->setEnabled(!activeKnowledgeBaseId.isEmpty() && !knowledgeDeletionPending);
    ui->knowledgeIndexStatus->setText(QStringLiteral("资料库导入失败：%1").arg(message.left(140)));
    ui->knowledgeIndexBadge->setText(QStringLiteral("导入失败"));
    polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeOrange"));
}

void MainWindow::handleKnowledgeIndexStarted(const KnowledgeIndexJobInfo &job)
{
    knowledgeIndexStarting = false;
    activeKnowledgeIndexJobId = job.indexJobId;
    ui->knowledgeIndexStatus->setText(QStringLiteral("索引任务已受理，正在后台准备。"));
    ui->knowledgeIndexBadge->setText(QStringLiteral("进行中"));
    polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeBlue"));
    updateKnowledgeBaseDetailUi();
    if (!knowledgeIndexPollTimer->isActive()) {
        knowledgeIndexPollTimer->start();
    }
    refreshKnowledgeIndexJob();
}

void MainWindow::handleKnowledgeIndexStartFailed(const QString &message)
{
    knowledgeIndexStarting = false;
    updateKnowledgeBaseDetailUi();
    ui->knowledgeIndexStatus->setText(QStringLiteral("无法启动索引：%1").arg(message.left(140)));
    ui->knowledgeIndexBadge->setText(QStringLiteral("启动失败"));
    polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeOrange"));
}

void MainWindow::handleKnowledgeIndexJobReceived(const KnowledgeIndexJobInfo &job)
{
    if (job.indexJobId != activeKnowledgeIndexJobId) {
        return;
    }
    const bool terminal = job.status == QStringLiteral("completed") || job.status == QStringLiteral("partial_failure")
                          || job.status == QStringLiteral("failed") || job.status == QStringLiteral("cancelled");
    const QString progress = QStringLiteral("已解析 %1/%2 份材料，已索引 %3 份")
                                 .arg(job.parsedDocumentCount)
                                 .arg(job.totalDocumentCount)
                                 .arg(job.indexedDocumentCount);
    const auto stageLabel = [](const QString &stage) {
        if (stage == QStringLiteral("ocr_recognizing")) {
            return QStringLiteral("正在识别扫描材料");
        }
        if (stage == QStringLiteral("parsing")) {
            return QStringLiteral("正在解析材料");
        }
        if (stage == QStringLiteral("chunking")) {
            return QStringLiteral("正在整理来源分块");
        }
        if (stage == QStringLiteral("vector_indexing")) {
            return QStringLiteral("正在建立语义索引");
        }
        if (stage == QStringLiteral("keyword_indexing")) {
            return QStringLiteral("正在建立关键词索引");
        }
        if (stage == QStringLiteral("verifying")) {
            return QStringLiteral("正在验证索引");
        }
        if (stage == QStringLiteral("activating")) {
            return QStringLiteral("正在启用资料库");
        }
        return QStringLiteral("正在处理");
    };
    if (!terminal) {
        if (knowledgeIndexActivityIndicator) {
            knowledgeIndexActivityIndicator->setRunning(true);
        }
        ui->knowledgeIndexStatus->setText(
            QStringLiteral("%1 · %2").arg(stageLabel(job.stage), progress));
        ui->knowledgeIndexBadge->setText(QStringLiteral("进行中"));
        polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeBlue"));
        return;
    }

    knowledgeIndexPollTimer->stop();
    activeKnowledgeIndexJobId.clear();
    if (knowledgeIndexActivityIndicator) {
        knowledgeIndexActivityIndicator->setRunning(false);
    }
    if (job.status == QStringLiteral("completed")) {
        ui->knowledgeIndexStatus->setText(QStringLiteral("索引完成 · %1").arg(progress));
        ui->knowledgeIndexBadge->setText(QStringLiteral("已完成"));
        polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeGreen"));
    } else if (job.status == QStringLiteral("partial_failure")) {
        const QString detail = job.failureSummaries.join(QStringLiteral("；")).left(180);
        const QString prefix = job.failedDocumentCount > 0
                                   ? QStringLiteral("索引已完成，但有 %1 份材料未完成：")
                                         .arg(job.failedDocumentCount)
                                   : QStringLiteral("索引已完成，但有处理范围需要留意：");
        ui->knowledgeIndexStatus->setText(prefix + detail);
        ui->knowledgeIndexBadge->setText(QStringLiteral("部分完成"));
        polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeOrange"));
    } else {
        ui->knowledgeIndexStatus->setText(
            QStringLiteral("索引未完成：%1").arg(job.failureSummaries.join(QStringLiteral("；")).left(160)));
        ui->knowledgeIndexBadge->setText(QStringLiteral("未完成"));
        polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeOrange"));
    }
    updateKnowledgeBaseDetailUi();
    refreshKnowledgeBases();
}

void MainWindow::handleKnowledgeIndexJobFailed(const QString &message)
{
    knowledgeIndexPollTimer->stop();
    activeKnowledgeIndexJobId.clear();
    knowledgeIndexStarting = false;
    updateKnowledgeBaseDetailUi();
    ui->knowledgeIndexStatus->setText(QStringLiteral("无法读取索引状态：%1").arg(message.left(140)));
    ui->knowledgeIndexBadge->setText(QStringLiteral("状态未知"));
    polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeOrange"));
}

void MainWindow::handleKnowledgeVectorCapabilityReceived(const KnowledgeVectorCapabilityInfo &capability)
{
    currentKnowledgeVectorCapability = capability;
    updateKnowledgeBaseDetailUi();
}

void MainWindow::handleKnowledgeVectorCapabilityFailed(const QString &message)
{
    currentKnowledgeVectorCapability = KnowledgeVectorCapabilityInfo{};
    ui->knowledgeVectorHint->setText(QStringLiteral("无法检测本机语义索引能力：%1").arg(message.left(120)));
    updateKnowledgeBaseDetailUi();
}

void MainWindow::handleKnowledgeVectorModelPrepared(const QString &message)
{
    knowledgeVectorPreparing = false;
    currentKnowledgeVectorCapability.modelInitialized = true;
    ui->knowledgeVectorHint->setText(message.isEmpty()
                                         ? QStringLiteral("本机语义模型已准备完成。之后建立的新索引会额外写入语义向量。")
                                         : message);
    updateKnowledgeBaseDetailUi();
    backendClient->requestKnowledgeVectorCapability();
}

void MainWindow::handleKnowledgeVectorModelPrepareFailed(const QString &message)
{
    knowledgeVectorPreparing = false;
    ui->knowledgeVectorHint->setText(QStringLiteral("本机模型未准备完成：%1").arg(message.left(140)));
    updateKnowledgeBaseDetailUi();
}

void MainWindow::handleKnowledgeOcrCapabilityReceived(const KnowledgeOcrCapabilityInfo &capability)
{
    currentKnowledgeOcrCapability = capability;
    updateKnowledgeOcrUi();
}

void MainWindow::handleKnowledgeOcrCapabilityFailed(const QString &message)
{
    currentKnowledgeOcrCapability = KnowledgeOcrCapabilityInfo{};
    currentKnowledgeOcrPreparation = KnowledgeOcrPreparationInfo{};
    currentKnowledgeOcrPreparation.status = QStringLiteral("failed");
    currentKnowledgeOcrPreparation.message = QStringLiteral("无法检测本地 OCR 能力：%1").arg(message.left(120));
    updateKnowledgeOcrUi();
}

void MainWindow::handleKnowledgeOcrPreparationReceived(const KnowledgeOcrPreparationInfo &preparation)
{
    currentKnowledgeOcrPreparation = preparation;
    activeKnowledgeOcrPreparationId = preparation.preparationId;
    knowledgeOcrPreparing = preparation.status == QStringLiteral("queued")
                            || preparation.status == QStringLiteral("preparing");
    if (knowledgeOcrPreparing) {
        if (!knowledgeOcrPreparationPollTimer->isActive()) {
            knowledgeOcrPreparationPollTimer->start();
        }
    } else {
        knowledgeOcrPreparationPollTimer->stop();
        activeKnowledgeOcrPreparationId.clear();
        if (preparation.status == QStringLiteral("ready")) {
            // 标记用于消除一次轮询间隙；随后仍以 capability endpoint 的 ready marker 为最终事实。
            currentKnowledgeOcrCapability.modelInitialized = true;
            backendClient->requestKnowledgeOcrCapability();
        }
    }
    updateKnowledgeOcrUi();
}

void MainWindow::handleKnowledgeOcrPreparationFailed(const QString &message)
{
    knowledgeOcrPreparationPollTimer->stop();
    activeKnowledgeOcrPreparationId.clear();
    knowledgeOcrPreparing = false;
    currentKnowledgeOcrPreparation.status = QStringLiteral("failed");
    currentKnowledgeOcrPreparation.message = QStringLiteral("本地 OCR 准备未完成：%1").arg(message.left(140));
    updateKnowledgeOcrUi();
}

void MainWindow::handleKnowledgeBaseDeletionRequested(const KnowledgeBaseInfo &knowledgeBase)
{
    if (knowledgeBase.knowledgeBaseId != activeKnowledgeBaseId) {
        return;
    }
    currentKnowledgeDocuments.clear();
    ui->knowledgeIndexStatus->setText(QStringLiteral("删除已受理，正在后台清理私有副本和索引。"));
    ui->knowledgeIndexBadge->setText(QStringLiteral("删除中"));
    polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeOrange"));
    if (knowledgeDeletionPollTimer && !knowledgeDeletionPollTimer->isActive()) {
        knowledgeDeletionPollTimer->start();
    }
}

void MainWindow::handleKnowledgeBaseDeletionFailed(const QString &message)
{
    knowledgeDeletionPending = false;
    if (knowledgeDeletionPollTimer) {
        knowledgeDeletionPollTimer->stop();
    }
    updateKnowledgeBaseDetailUi();
    ui->knowledgeIndexStatus->setText(QStringLiteral("无法删除资料库：%1").arg(message.left(140)));
    ui->knowledgeIndexBadge->setText(QStringLiteral("删除失败"));
    polishBadge(ui->knowledgeIndexBadge, QStringLiteral("badgeOrange"));
}

void MainWindow::setupDataWorkspace()
{
    // 数据文件就绪后，客户最常做的是提出一个问题并得到图表/结论。准备说明、字段表与原始预览
    // 因此默认收起，避免它们挤压真正的任务入口；需要核对时再从紧凑图标打开底部详情。
    ui->dataEmptyLayout->setAlignment(ui->dataEmptyImportButton, Qt::AlignHCenter);
    ui->dataMainCard->setVisible(false);
    ui->dataStateStack->setCurrentIndex(0);
    ui->dataDatasetCombo->addItem(QStringLiteral("等待后端加载数据文件"), QString());
    ui->dataDatasetCombo->setEnabled(false);
    ui->dataRefreshButton->setEnabled(false);
    ui->dataAnalyzeButton->setEnabled(false);
    ui->dataAnalysisDetailsButton->setEnabled(false);
    ui->dataDelegateButton->setEnabled(false);
    ui->dataInsightFrame->setVisible(false);
    ui->dataRecommendationFrame->setVisible(false);
    ui->dataRecommendationRefreshButton->setEnabled(false);
    for (QFrame *card : {ui->dataRecommendationCard1, ui->dataRecommendationCard2,
                         ui->dataRecommendationCard3, ui->dataRecommendationCard4}) {
        card->setVisible(false);
    }
    ui->dataWorkbookHistoryButton->setIcon(QIcon(QStringLiteral(":/icons/history.svg")));
    ui->dataWorkbookHistoryStrip->setVisible(false);
    ui->dataFieldTable->setColumnCount(5);
    ui->dataFieldTable->setRowCount(0);
    ui->dataFieldTable->verticalHeader()->setVisible(false);
    ui->dataFieldTable->verticalHeader()->setDefaultSectionSize(34);
    ui->dataFieldTable->setTextElideMode(Qt::ElideRight);
    ui->dataFieldTable->horizontalHeader()->setStretchLastSection(false);
    ui->dataFieldTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::Stretch);
    ui->dataFieldTable->horizontalHeader()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
    ui->dataFieldTable->horizontalHeader()->setSectionResizeMode(2, QHeaderView::ResizeToContents);
    ui->dataFieldTable->horizontalHeader()->setSectionResizeMode(3, QHeaderView::ResizeToContents);
    ui->dataFieldTable->horizontalHeader()->setSectionResizeMode(4, QHeaderView::ResizeToContents);
    ui->dataPreviewTable->verticalHeader()->setVisible(false);
    ui->dataPreviewTable->verticalHeader()->setDefaultSectionSize(32);
    ui->dataPreviewTable->setTextElideMode(Qt::ElideRight);
    ui->dataPreviewTable->horizontalHeader()->setStretchLastSection(false);
    ui->dataProfileSplitter->setStretchFactor(0, 4);
    ui->dataProfileSplitter->setStretchFactor(1, 6);
    ui->dataProfileSplitter->setVisible(false);
    ui->dataPreparationHint->setVisible(false);
    // 结果阅读区域在上，行动输入始终处于当前工作台最底部。详情展开时不会把输入框挤到
    // 数据文件和字段表之间，符合“先看结果，再继续提问”的连续工作流。
    ui->dataReadyLayout->removeWidget(ui->dataAnalysisCommandFrame);
    ui->dataReadyLayout->addWidget(ui->dataAnalysisCommandFrame);
    ui->dataPreparationHint->setText(
        QStringLiteral("导入后直接说你想看什么。字段与有限预览默认收在下方，不会占用分析工作区。"));
    ui->dataProfileStatus->setText(QStringLiteral("等待画像"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeGray"));
    polishBadge(ui->dataStageBadge, QStringLiteral("badgeBlue"));

    // Designer 只负责占位和稳定尺寸；指示器本身是可复用 Widget，避免每个工作台各画一套动画。
    auto *dataActivityLayout = new QHBoxLayout(ui->dataActivityIndicatorHost);
    dataActivityLayout->setContentsMargins(0, 0, 0, 0);
    dataActivityIndicator = new TaskActivityIndicator(ui->dataActivityIndicatorHost);
    dataActivityLayout->addWidget(dataActivityIndicator);
    dataActivityIndicator->setRunning(false);
    if (!workbenchActivityStateTimer) {
        workbenchActivityStateTimer = new QTimer(this);
        workbenchActivityStateTimer->setInterval(200);
        connect(workbenchActivityStateTimer, &QTimer::timeout, this, [this]() {
            updateDocumentActivityState();
            updateDataActivityState();
        });
    }
    updateDocumentActivityState();
    updateDataActivityState();

    // 帮助与字段详情是 Designer 中的稳定控件：默认不占用结果区域，但客户随时能找到。
    ui->dataProfileToggleButton->setIcon(style()->standardIcon(QStyle::SP_FileDialogDetailedView));
    ui->dataProfileToggleButton->setToolButtonStyle(Qt::ToolButtonTextBesideIcon);
    connect(ui->dataHelpButton, &QToolButton::clicked, this, [this]() {
        DataHelpDialog dialog(this);
        dialog.exec();
    });
    connect(ui->dataModelRouteButton, &QToolButton::clicked, this, [this]() {
        openModelRouteDialogForRoute(QStringLiteral("data_insight"));
    });

    connect(ui->dataProfileToggleButton, &QToolButton::toggled, this, [this](bool expanded) {
        ui->dataProfileSplitter->setVisible(expanded && dataProfileReady);
        ui->dataProfileToggleButton->setToolTip(
            expanded ? QStringLiteral("收起字段与有限预览") : QStringLiteral("展开字段与有限预览"));
    });

    connect(ui->dataEmptyImportButton, &QPushButton::clicked, this, &MainWindow::importDataDatasetFromFile);
    connect(ui->dataImportButton, &QPushButton::clicked, this, &MainWindow::importDataDatasetFromFile);
    connect(ui->dataRefreshButton, &QPushButton::clicked, this, &MainWindow::refreshDataDatasets);
    connect(ui->dataRecommendationRefreshButton, &QToolButton::clicked, this, &MainWindow::requestDataRecommendations);
    connect(ui->dataAnalyzeButton, &QPushButton::clicked, this, &MainWindow::requestDataAnalysisPreview);
    connect(ui->dataDelegateButton, &QPushButton::clicked, this, &MainWindow::delegateDataDatasetToCommander);
    connect(ui->dataAnalysisDetailsButton, &QPushButton::clicked, this, &MainWindow::showDataAnalysisPreviewDialog);
    connect(ui->dataWorkbookHistoryButton, &QToolButton::clicked, this, [this]() {
        // 输出文件只能从历史页的受控 artifact 入口打开；主页面不保存或展示本机绝对路径。
        openTaskInHistory(!lastDataTransformationTaskId.isEmpty()
                              ? lastDataTransformationTaskId
                              : (!lastDataChartExportTaskId.isEmpty() ? lastDataChartExportTaskId
                                                                       : lastDataWorkbookExportTaskId));
    });
    connect(ui->dataAnalysisGoalInput, &QLineEdit::returnPressed, this, &MainWindow::requestDataAnalysisPreview);
    connect(ui->dataDatasetCombo,
            qOverload<int>(&QComboBox::currentIndexChanged),
            this,
            [this](int) {
                // 用户主动切换材料时，允许对新选择重新执行一次“列表过期”恢复；程序内部刷新
                // 会用 QSignalBlocker 抑制此信号，避免同一 404 触发循环刷新。
                dataProfileRefreshRecoveryAttempted = false;
                requestSelectedDataDatasetProfile();
            });
    for (QPushButton *button : {ui->dataRecommendationUseButton1, ui->dataRecommendationUseButton2,
                                ui->dataRecommendationUseButton3, ui->dataRecommendationUseButton4}) {
        connect(button, &QPushButton::clicked, this, [this, button]() {
            const QString goal = button->property("recommendationGoal").toString().trimmed();
            if (goal.isEmpty() || !dataProfileReady || dataAnalysisLoading) {
                return;
            }
            // 客户看到的建议只做一件事：填入自然语言目标并复用既有 D2 本地计算链。
            ui->dataAnalysisGoalInput->setText(goal);
            requestDataAnalysisPreview();
        });
    }
}

void MainWindow::importDataDatasetFromFile()
{
    if (dataWorkspaceLoading || dataProfileLoading || dataAnalysisLoading || dataWorkbookExportLoading || dataChartExportLoading
        || dataTransformationPreviewLoading || dataTransformationExportLoading
        || !pendingDataDatasetImportTarget.isEmpty()) {
        return;
    }
    if (!backendClient || !backendManager || !backendManager->isReady()) {
        if (backendManager) {
            backendManager->ensureStarted();
        }
        ui->dataEmptyStatus->setText(QStringLiteral("后端准备中，暂不能导入数据；服务就绪后请重试。"));
        ui->dataProfileStatus->setText(QStringLiteral("等待后端"));
        polishBadge(ui->dataProfileStatus, QStringLiteral("badgeGray"));
        return;
    }

    const QString filePath = QFileDialog::getOpenFileName(
        this,
        QStringLiteral("导入数据文件"),
        QString(),
        QStringLiteral("数据文件 (*.xlsx *.csv)"));
    if (filePath.isEmpty()) {
        return;
    }

    const QFileInfo fileInfo(filePath);
    const QString suffix = fileInfo.suffix().toLower();
    if (suffix != QStringLiteral("xlsx") && suffix != QStringLiteral("csv")) {
        QMessageBox::warning(this, QStringLiteral("导入数据"), QStringLiteral("当前只支持 Excel (.xlsx) 和 CSV 文件。"));
        return;
    }
    if (fileInfo.size() <= 0 || fileInfo.size() > DataDatasetMaxBytes) {
        QMessageBox::warning(
            this,
            QStringLiteral("导入数据"),
            QStringLiteral("当前只支持大于 0 且不超过 20MB 的 Excel 或 CSV 文件。"));
        return;
    }

    QFile file(filePath);
    if (!file.open(QIODevice::ReadOnly)) {
        QMessageBox::warning(this,
                             QStringLiteral("导入数据"),
                             QStringLiteral("无法读取文件：%1").arg(fileInfo.fileName()));
        return;
    }
    // D1 对源文件严格限制为 20MB；读取后的 HTTP 发送始终异步，页面会立即显示提交状态。
    // 后续 D4 若扩展到更大文件，会换成分块上传而不是在 Qt 主线程堆积更大的 QByteArray。
    const QByteArray content = file.readAll();
    if (content.size() != fileInfo.size()) {
        QMessageBox::warning(this,
                             QStringLiteral("导入数据"),
                             QStringLiteral("读取数据文件不完整，请确认文件未被其它程序占用。"));
        return;
    }

    pendingDataDatasetSelection = fileInfo.fileName();
    pendingDataDatasetImportTarget = QStringLiteral("data_workspace");
    dataProfileRefreshRecoveryAttempted = false;
    activeDataProfileDataset.clear();
    dataWorkspaceLoading = true;
    updateDataActivityState();
    dataProfileReady = false;
    lastDataAnalysisPreview = QJsonObject{};
    // 数据集一旦切换，旧字段预览不再对应当前源文件，必须在客户端同时失效。
    pendingDataTransformationRequest = QJsonObject{};
    lastDataTransformationPreview = QJsonObject{};
    ui->dataImportButton->setEnabled(false);
    ui->dataEmptyImportButton->setEnabled(false);
    ui->dataRefreshButton->setEnabled(false);
    ui->dataDatasetCombo->setEnabled(false);
    ui->dataAnalyzeButton->setEnabled(false);
    ui->dataAnalysisDetailsButton->setEnabled(false);
    ui->dataDelegateButton->setEnabled(false);
    ui->dataEmptyStatus->setText(QStringLiteral("正在导入数据文件…"));
    ui->dataProfileStatus->setText(QStringLiteral("正在导入"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeBlue"));
    backendClient->importDataDataset(fileInfo.fileName(), content);
}

void MainWindow::refreshDataDatasets()
{
    if (dataWorkspaceLoading || dataProfileLoading || dataAnalysisLoading || dataWorkbookExportLoading || dataChartExportLoading
        || dataTransformationPreviewLoading || dataTransformationExportLoading) {
        return;
    }
    dataWorkspaceLoading = true;
    updateDataActivityState();
    ui->dataImportButton->setEnabled(false);
    ui->dataEmptyImportButton->setEnabled(false);
    ui->dataRefreshButton->setEnabled(false);
    ui->dataDatasetCombo->setEnabled(false);
    ui->dataEmptyStatus->setText(QStringLiteral("正在读取已导入的数据文件…"));
    ui->dataProfileStatus->setText(QStringLiteral("正在刷新"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeBlue"));
    backendClient->requestDataDatasets();
}

void MainWindow::requestSelectedDataDatasetProfile()
{
    if (dataWorkspaceLoading || dataProfileLoading || dataAnalysisLoading || dataWorkbookExportLoading || dataChartExportLoading
        || dataTransformationPreviewLoading || dataTransformationExportLoading) {
        return;
    }
    const QString datasetName = ui->dataDatasetCombo->currentData().toString().trimmed();
    if (datasetName.isEmpty()) {
        return;
    }
    // 记录本次请求目标，回调只允许更新同一份受控数据，避免迟到响应写入当前选择。
    activeDataProfileDataset = datasetName;
    dataProfileLoading = true;
    updateDataActivityState();
    dataProfileReady = false;
    dataRecommendationLoading = false;
    lastDataAnalysisPreview = QJsonObject{};
    pendingDataTransformationRequest = QJsonObject{};
    lastDataTransformationPreview = QJsonObject{};
    ui->dataDatasetCombo->setEnabled(false);
    ui->dataImportButton->setEnabled(false);
    ui->dataRefreshButton->setEnabled(false);
    ui->dataAnalyzeButton->setEnabled(false);
    ui->dataAnalysisDetailsButton->setEnabled(false);
    ui->dataDelegateButton->setEnabled(false);
    ui->dataRecommendationFrame->setVisible(false);
    ui->dataRecommendationRefreshButton->setEnabled(false);
    {
        const QSignalBlocker blocker(ui->dataProfileToggleButton);
        ui->dataProfileToggleButton->setChecked(false);
    }
    ui->dataProfileSplitter->setVisible(false);
    ui->dataProfileStatus->setText(QStringLiteral("正在建立画像"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeBlue"));
    ui->dataDatasetMeta->setText(QStringLiteral("正在本地识别工作表、表头、字段类型与质量问题…"));
    backendClient->requestDataDatasetProfile(datasetName);
}

void MainWindow::requestDataRecommendations()
{
    if (dataWorkspaceLoading || dataProfileLoading || dataRecommendationLoading || !dataProfileReady) {
        return;
    }
    const QString datasetName = ui->dataDatasetCombo->currentData().toString().trimmed();
    if (datasetName.isEmpty()) {
        return;
    }

    dataRecommendationLoading = true;
    updateDataActivityState();
    // 推荐是辅助信息而不是主流程；不再把它的等待、超时或空结果插回客户的工作台。
    ui->dataRecommendationFrame->setVisible(false);
    ui->dataRecommendationRefreshButton->setEnabled(false);
    ui->dataRecommendationHint->setText(QStringLiteral("正在根据已识别字段整理可执行方向…"));
    backendClient->requestDataRecommendations(datasetName, ui->dataAnalysisGoalInput->text());
}

void MainWindow::requestDataAnalysisPreview()
{
    if (dataWorkspaceLoading || dataProfileLoading || dataAnalysisLoading || dataWorkbookExportLoading || dataChartExportLoading
        || dataTransformationPreviewLoading || dataTransformationExportLoading || !dataProfileReady) {
        return;
    }
    const QString datasetName = ui->dataDatasetCombo->currentData().toString().trimmed();
    if (datasetName.isEmpty()) {
        return;
    }

    // D2 只提交用户目标和受控数据集引用。原始单元格不会经过 Qt 再发给模型或普通任务日志。
    dataAnalysisLoading = true;
    updateDataActivityState();
    // 新预览代表用户开始一轮新的分析意图，避免旧交付记录在当前命令区造成错误关联。
    lastDataWorkbookExportTaskId.clear();
    lastDataChartExportTaskId.clear();
    ui->dataWorkbookHistoryButton->setToolTip(QStringLiteral("查看最近数据交付任务与受控 artifact"));
    ui->dataWorkbookHistoryStrip->setVisible(false);
    lastDataAnalysisPreview = QJsonObject{};
    // 新分析计划会改变当前材料上下文，旧字段变更预览不能继续确认导出。
    pendingDataTransformationRequest = QJsonObject{};
    lastDataTransformationPreview = QJsonObject{};
    ui->dataDatasetCombo->setEnabled(false);
    ui->dataImportButton->setEnabled(false);
    ui->dataRefreshButton->setEnabled(false);
    ui->dataAnalyzeButton->setEnabled(false);
    ui->dataAnalysisDetailsButton->setEnabled(false);
    ui->dataDelegateButton->setEnabled(false);
    ui->dataAnalysisGoalInput->setEnabled(false);
    // 新请求一旦开始就不能继续展示上一轮的结论，避免客户把不同文件或目标的结果看混。
    ui->dataInsightFrame->setVisible(false);
    ui->dataProfileStatus->setText(QStringLiteral("正在计算预览"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeBlue"));
    ui->dataAnalysisHint->setText(
        QStringLiteral("正在执行本地受控计算：画像 → 计划 → 白名单校验 → 聚合。不会生成文件。"));
    backendClient->requestDataAnalysisPreview(datasetName, ui->dataAnalysisGoalInput->text());
}

void MainWindow::delegateDataDatasetToCommander()
{
    // 只允许已完成画像的当前数据进入 Commander。这里传递的是后端确认过的相对引用，
    // 而不是本机路径、有限预览行或字段内容；真正读取仍由后端准入后的 data_agent 完成。
    if (!dataProfileReady || dataProfileLoading || dataWorkspaceLoading) {
        return;
    }
    const QString datasetRef = ui->dataDatasetCombo->currentData().toString().trimmed();
    if (datasetRef.isEmpty()) {
        return;
    }

    // 从数据工作台跳转时，当前数据集是客户刚刚明确选择的唯一数据来源；清掉上一轮的
    // 文档/资料库绑定，防止数据请求被旧材料干扰。多材料任务通过调度台选择器显式组合。
    dispatchSelectedDocumentRef.clear();
    dispatchSelectedKnowledgeBaseId.clear();
    dispatchSelectedDatasetRef = datasetRef;
    updateDispatchMaterialBindingsUi();
    switchPage(1);

    // 从数据工作台交接时，当前数据是客户刚刚明确选择的唯一专业材料。清掉输入框遗留的
    // 文档/知识库 @ 路由并写入可见的数据标签，避免后端按旧显式偏好过滤掉数据材料。
    removeDispatchAgentHint(QStringLiteral("document_agent"));
    removeDispatchAgentHint(QStringLiteral("knowledge_agent"));
    insertDispatchAgentHint(QStringLiteral("data_agent"));

    if (ui->dispatchInputEdit->text().trimmed().isEmpty()) {
        const QString goal = ui->dataAnalysisGoalInput->text().trimmed();
        ui->dispatchInputEdit->setText(
            goal.isEmpty()
                ? QStringLiteral("请分析当前数据文件，识别主要趋势、差异和可生成的图表。")
                : goal);
    }
    ui->dispatchChatStatus->setText(QStringLiteral("已带入当前数据文件；发送后将自动进行只读分析。"));
    ui->dispatchInputEdit->setFocus();
}

void MainWindow::handleDataDatasetImported(const DataDatasetInfo &dataset)
{
    const bool dispatchTarget = pendingDataDatasetImportTarget == QStringLiteral("dispatch");
    pendingDataDatasetImportTarget.clear();
    if (dispatchTarget) {
        ui->attachButton->setEnabled(true);
        const QString datasetRef = dataset.relativePath.isEmpty() ? dataset.name : dataset.relativePath;
        dispatchSelectedDocumentRef.clear();
        dispatchSelectedKnowledgeBaseId.clear();
        dispatchSelectedDatasetRef = datasetRef;
        updateDispatchMaterialBindingsUi();
        removeDispatchAgentHint(QStringLiteral("document_agent"));
        removeDispatchAgentHint(QStringLiteral("knowledge_agent"));
        insertDispatchAgentHint(QStringLiteral("data_agent"));
        if (ui->dispatchInputEdit->text().trimmed().isEmpty()) {
            ui->dispatchInputEdit->setText(
                QStringLiteral("请分析当前数据文件，识别主要趋势、差异和可生成的图表。"));
        }
        ui->dispatchChatStatus->setText(
            QStringLiteral("数据已导入并绑定；发送后将自动进行只读分析。"));
        ui->dispatchInputEdit->setFocus();
        appendConversationHtml(
            QStringLiteral("<p style=\"color:#0F766E;\"><b>系统</b> · 已导入数据文件：%1。"
                           "本次任务将只读使用该数据集。</p>")
                .arg(dataset.name.toHtmlEscaped()));
        return;
    }

    // 同名导入可能被后端保护为 "(2)" 副本，因此始终使用回执中的受控相对名称重新选中。
    pendingDataDatasetSelection = dataset.relativePath.isEmpty() ? dataset.name : dataset.relativePath;
    dataWorkspaceLoading = false;
    ui->dataEmptyStatus->setText(QStringLiteral("数据已导入，正在刷新文件列表…"));
    refreshDataDatasets();
}

void MainWindow::handleDataDatasetImportFailed(const QString &message)
{
    const bool dispatchTarget = pendingDataDatasetImportTarget == QStringLiteral("dispatch");
    pendingDataDatasetImportTarget.clear();
    if (dispatchTarget) {
        ui->attachButton->setEnabled(true);
        ui->dispatchChatStatus->setText(QStringLiteral("数据导入失败：%1").arg(message.left(120)));
        return;
    }

    dataWorkspaceLoading = false;
    ui->dataImportButton->setEnabled(true);
    ui->dataEmptyImportButton->setEnabled(true);
    ui->dataRefreshButton->setEnabled(dataWorkspaceLoaded);
    ui->dataDatasetCombo->setEnabled(dataWorkspaceLoaded && ui->dataDatasetCombo->count() > 0);
    ui->dataEmptyStatus->setText(QStringLiteral("导入失败：%1").arg(message.left(120)));
    ui->dataProfileStatus->setText(QStringLiteral("导入失败"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeOrange"));
    QMessageBox::warning(this, QStringLiteral("导入数据"), QStringLiteral("数据文件导入失败：%1").arg(message));
}

void MainWindow::handleDataDatasetsReceived(const DataDatasetListResult &result)
{
    dataWorkspaceLoading = false;
    dataWorkspaceLoaded = true;
    currentDataDatasets = result.datasets;
    if (dispatchMaterialDialog && dispatchMaterialDialog->isVisible()) {
        dispatchMaterialDatasetsPending = false;
        dispatchMaterialDialog->setDatasets(currentDataDatasets);
        updateDispatchMaterialCatalogStatus();
    }
    ui->dataImportButton->setEnabled(true);
    ui->dataEmptyImportButton->setEnabled(true);
    ui->dataRefreshButton->setEnabled(true);

    const QString currentSelection = pendingDataDatasetSelection.isEmpty()
                                         ? ui->dataDatasetCombo->currentData().toString()
                                         : pendingDataDatasetSelection;
    const QSignalBlocker blocker(ui->dataDatasetCombo);
    ui->dataDatasetCombo->clear();
    for (const DataDatasetInfo &dataset : result.datasets) {
        const QString datasetRef = dataset.relativePath.isEmpty() ? dataset.name : dataset.relativePath;
        const QString typeText = dataset.datasetType.toUpper();
        ui->dataDatasetCombo->addItem(
            QStringLiteral("%1  ·  %2  ·  %3 KB")
                .arg(dataset.name)
                .arg(typeText)
                .arg(qMax(1, (dataset.sizeBytes + 1023) / 1024)),
            datasetRef);
    }

    if (result.datasets.isEmpty()) {
        const bool missingDatasetAfterRefresh = dataProfileRefreshRecoveryAttempted
            && !activeDataProfileDataset.isEmpty();
        ui->dataDatasetCombo->addItem(QStringLiteral("暂无已导入数据"), QString());
        ui->dataDatasetCombo->setEnabled(false);
        ui->dataStateStack->setCurrentIndex(0);
        // 画像 404 后的刷新若仍为空，说明后端工作区已没有本地副本。此时直接说明下一步，
        // 不把 HTTP 状态码或“再刷新一次”的无效建议抛给客户。
        ui->dataEmptyStatus->setText(
            missingDatasetAfterRefresh
                ? QStringLiteral("当前后端没有原数据副本，请重新导入该 CSV 或 Excel。")
                : QStringLiteral("尚未选择数据文件"));
        ui->dataProfileStatus->setText(
            missingDatasetAfterRefresh ? QStringLiteral("需要重新导入") : QStringLiteral("等待画像"));
        polishBadge(ui->dataProfileStatus,
                    missingDatasetAfterRefresh ? QStringLiteral("badgeOrange") : QStringLiteral("badgeGray"));
        dataProfileReady = false;
        activeDataProfileDataset.clear();
        lastDataAnalysisPreview = QJsonObject{};
        pendingDataTransformationRequest = QJsonObject{};
        lastDataTransformationPreview = QJsonObject{};
        ui->dataAnalyzeButton->setEnabled(false);
        ui->dataAnalysisDetailsButton->setEnabled(false);
        pendingDataDatasetSelection.clear();
        return;
    }

    int selectedIndex = 0;
    for (int index = 0; index < ui->dataDatasetCombo->count(); ++index) {
        if (ui->dataDatasetCombo->itemData(index).toString() == currentSelection) {
            selectedIndex = index;
            break;
        }
    }
    ui->dataDatasetCombo->setCurrentIndex(selectedIndex);
    ui->dataDatasetCombo->setEnabled(true);
    ui->dataStateStack->setCurrentIndex(1);
    pendingDataDatasetSelection.clear();
    // 调度台只需要材料目录，不能因为客户打开选择器就后台读取整份 CSV/XLSX 建画像。
    // 数据工作台真正进入前台时会补做这一步，保证原有本地画像流程不丢失。
    if (ui->contentStack->currentIndex() == 6) {
        requestSelectedDataDatasetProfile();
    }
}

void MainWindow::handleDataDatasetsFailed(const QString &message)
{
    dataWorkspaceLoading = false;
    if (dispatchMaterialDialog && dispatchMaterialDialog->isVisible()) {
        dispatchMaterialDatasetsPending = false;
        if (dispatchMaterialCatalogError.isEmpty()) {
            dispatchMaterialCatalogError = QStringLiteral("数据集：%1").arg(message);
        }
        updateDispatchMaterialCatalogStatus();
    }
    ui->dataImportButton->setEnabled(true);
    ui->dataEmptyImportButton->setEnabled(true);
    ui->dataRefreshButton->setEnabled(dataWorkspaceLoaded);
    ui->dataDatasetCombo->setEnabled(dataWorkspaceLoaded && ui->dataDatasetCombo->count() > 0);
    ui->dataEmptyStatus->setText(QStringLiteral("无法读取数据文件列表：%1").arg(message.left(120)));
    ui->dataProfileStatus->setText(QStringLiteral("列表加载失败"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeOrange"));
}

void MainWindow::handleDataDatasetProfileReceived(const QJsonObject &profile)
{
    const QString profileDatasetName = profile.value(QStringLiteral("dataset")).toObject().value(QStringLiteral("name")).toString();
    const QString currentDatasetName = ui->dataDatasetCombo->currentData().toString().trimmed();
    if (profileDatasetName.isEmpty() || profileDatasetName != activeDataProfileDataset
        || profileDatasetName != currentDatasetName) {
        // 极少数情况下，网络回调可能晚于列表刷新。宁可丢弃旧画像，也不能把 A 文件的字段显示给 B 文件。
        dataProfileLoading = false;
        dataProfileReady = false;
        if (!currentDatasetName.isEmpty() && currentDatasetName != activeDataProfileDataset) {
            requestSelectedDataDatasetProfile();
        }
        return;
    }
    dataProfileLoading = false;
    dataProfileReady = true;
    dataProfileRefreshRecoveryAttempted = false;
    ui->dataDatasetCombo->setEnabled(true);
    ui->dataImportButton->setEnabled(true);
    ui->dataRefreshButton->setEnabled(true);
    ui->dataAnalyzeButton->setEnabled(true);
    ui->dataAnalysisGoalInput->setEnabled(true);
    ui->dataDelegateButton->setEnabled(true);
    const QJsonObject dataset = profile.value(QStringLiteral("dataset")).toObject();
    const int rowCount = profile.value(QStringLiteral("row_count")).toInt();
    const int columnCount = profile.value(QStringLiteral("column_count")).toInt();
    const int headerRow = profile.value(QStringLiteral("header_row")).toInt(1);
    const QString selectedSheet = profile.value(QStringLiteral("selected_sheet")).toString();
    const QJsonObject quality = profile.value(QStringLiteral("quality_summary")).toObject();
    const int missingCells = quality.value(QStringLiteral("missing_cell_count")).toInt();
    const int duplicateRows = quality.value(QStringLiteral("duplicate_row_count")).toInt();
    const int parseColumns = quality.value(QStringLiteral("parse_issue_column_count")).toInt();

    ui->dataDatasetMeta->setText(
        QStringLiteral("%1 · %2 · %3 行 × %4 列 · 主表：%5")
            .arg(dataset.value(QStringLiteral("name")).toString())
            .arg(dataset.value(QStringLiteral("dataset_type")).toString().toUpper())
            .arg(rowCount)
            .arg(columnCount)
            .arg(selectedSheet));
    ui->dataSheetSummary->setText(
        QStringLiteral("推荐主表：%1 · 建议表头：第 %2 行 · 缺失单元格 %3 · 重复行 %4")
            .arg(selectedSheet)
            .arg(headerRow)
            .arg(missingCells)
            .arg(duplicateRows));

    const QJsonArray columns = profile.value(QStringLiteral("columns")).toArray();
    ui->dataFieldTable->setRowCount(columns.size());
    for (int row = 0; row < columns.size(); ++row) {
        const QJsonObject column = columns.at(row).toObject();
        QString range = QStringLiteral("—");
        const QString type = column.value(QStringLiteral("inferred_type")).toString();
        if (type == QStringLiteral("number") && column.value(QStringLiteral("numeric_min")).isDouble()) {
            range = QStringLiteral("%1 ~ %2")
                        .arg(formatDataDisplayNumber(column.value(QStringLiteral("numeric_min")).toDouble()))
                        .arg(formatDataDisplayNumber(column.value(QStringLiteral("numeric_max")).toDouble()));
        } else if (type == QStringLiteral("date")) {
            range = QStringLiteral("%1 ~ %2")
                        .arg(column.value(QStringLiteral("earliest")).toString())
                        .arg(column.value(QStringLiteral("latest")).toString());
        }
        const QList<QString> cells = {
            column.value(QStringLiteral("name")).toString(),
            type,
            QString::number(column.value(QStringLiteral("missing_count")).toInt()),
            QString::number(column.value(QStringLiteral("unique_count")).toInt()),
            range,
        };
        for (int index = 0; index < cells.size(); ++index) {
            auto *item = new QTableWidgetItem(cells.at(index));
            item->setToolTip(cells.at(index));
            if (index >= 2) {
                item->setTextAlignment(Qt::AlignRight | Qt::AlignVCenter);
            }
            ui->dataFieldTable->setItem(row, index, item);
        }
    }

    const QJsonArray previewColumns = profile.value(QStringLiteral("preview_columns")).toArray();
    const QJsonArray previewRows = profile.value(QStringLiteral("preview_rows")).toArray();
    ui->dataPreviewTable->clear();
    ui->dataPreviewTable->setColumnCount(previewColumns.size());
    ui->dataPreviewTable->setRowCount(previewRows.size());
    QStringList headerLabels;
    for (const QJsonValue &value : previewColumns) {
        headerLabels.append(value.toString());
    }
    ui->dataPreviewTable->setHorizontalHeaderLabels(headerLabels);
    for (int row = 0; row < previewRows.size(); ++row) {
        const QJsonArray values = previewRows.at(row).toArray();
        for (int column = 0; column < values.size(); ++column) {
            auto *item = new QTableWidgetItem(values.at(column).toString());
            item->setToolTip(item->text());
            ui->dataPreviewTable->setItem(row, column, item);
        }
    }
    ui->dataPreviewTable->horizontalHeader()->setSectionResizeMode(QHeaderView::ResizeToContents);

    const QJsonArray warnings = profile.value(QStringLiteral("warnings")).toArray();
    QStringList warningTexts;
    for (const QJsonValue &value : warnings) {
        warningTexts.append(value.toString());
    }
    QString sequenceColumn;
    QString measureColumn;
    for (const QJsonValue &value : columns) {
        const QJsonObject column = value.toObject();
        if (column.value(QStringLiteral("inferred_type")).toString() != QStringLiteral("number")) {
            continue;
        }
        const QString name = column.value(QStringLiteral("name")).toString();
        if (sequenceColumn.isEmpty() && (name.contains(QStringLiteral("位置")) || name.contains(QStringLiteral("序号"))
                                         || name.contains(QStringLiteral("轮次")))) {
            sequenceColumn = name;
        }
        if (measureColumn.isEmpty() && (name.contains(QStringLiteral("清晰")) || name.contains(QStringLiteral("评分"))
                                        || name.contains(QStringLiteral("得分")) || name.contains(QStringLiteral("质量")))) {
            measureColumn = name;
        }
    }
    if (!sequenceColumn.isEmpty() && !measureColumn.isEmpty()) {
        ui->dataAnalysisGoalInput->setPlaceholderText(
            QStringLiteral("例如：绘制 %1 与 %2 的变化曲线").arg(sequenceColumn, measureColumn));
    } else {
        ui->dataAnalysisGoalInput->setPlaceholderText(
            QStringLiteral("例如：比较各地区金额，或生成月度趋势图（可留空）"));
    }
    ui->dataPreparationHint->setText(
        warningTexts.isEmpty()
            ? QStringLiteral("数据已准备好。直接写下你想了解的内容；字段与有限预览已收在下方详情中。")
            : QStringLiteral("数据已准备好，但有 %1。可继续分析，必要时展开字段详情核对。")
                  .arg(warningTexts.join(QStringLiteral("  "))));
    ui->dataAnalysisHint->setText(
        QStringLiteral("可留空以生成基础概览。D2 不调用模型、不联网，也不会生成 Excel 文件。"));
    ui->dataProfileStatus->setText(
        parseColumns > 0 ? QStringLiteral("画像完成 · 有待确认格式") : QStringLiteral("画像完成 · 可进入 D2"));
    polishBadge(ui->dataProfileStatus, parseColumns > 0 ? QStringLiteral("badgeOrange") : QStringLiteral("badgeGreen"));
    // 建议卡不是客户完成任务的前置条件。旧版在这里同步等待可选模型，超时会挤占主页面并造成
    // “暂无法整理建议”的假失败；现在客户可直接进入分析，建议仅保留为未来按需打开的辅助能力。
    ui->dataRecommendationFrame->setVisible(false);
    ui->dataRecommendationRefreshButton->setEnabled(false);
}

void MainWindow::handleDataDatasetProfileFailed(const QString &message)
{
    dataProfileLoading = false;
    dataProfileReady = false;
    dataRecommendationLoading = false;
    lastDataAnalysisPreview = QJsonObject{};
    pendingDataTransformationRequest = QJsonObject{};
    lastDataTransformationPreview = QJsonObject{};
    const bool selectedFileMissing = message.contains(QStringLiteral("HTTP 404"))
        && message.contains(QStringLiteral("未找到指定的数据文件"));
    if (selectedFileMissing && !dataProfileRefreshRecoveryAttempted) {
        // 后端重启、工作区被清理或旧版本客户端列表滞后时，先自动刷新一次。客户不该看到
        // 没有行动价值的 HTTP 404，也无需手动判断“刷新还是重新导入”。
        dataProfileRefreshRecoveryAttempted = true;
        pendingDataDatasetSelection = activeDataProfileDataset;
        ui->dataProfileStatus->setText(QStringLiteral("正在更新文件列表"));
        polishBadge(ui->dataProfileStatus, QStringLiteral("badgeBlue"));
        ui->dataDatasetMeta->setText(QStringLiteral("当前文件在后端工作区中不可用，正在刷新可用材料…"));
        ui->dataPreparationHint->setText(QStringLiteral("不会修改源文件；若刷新后仍不存在，会提示你重新导入。"));
        refreshDataDatasets();
        return;
    }
    ui->dataDatasetCombo->setEnabled(true);
    ui->dataImportButton->setEnabled(true);
    ui->dataRefreshButton->setEnabled(true);
    ui->dataAnalyzeButton->setEnabled(false);
    ui->dataAnalysisDetailsButton->setEnabled(false);
    ui->dataDelegateButton->setEnabled(false);
    ui->dataRecommendationFrame->setVisible(false);
    ui->dataRecommendationRefreshButton->setEnabled(false);
    ui->dataAnalysisGoalInput->setEnabled(true);
    ui->dataProfileStatus->setText(selectedFileMissing ? QStringLiteral("需要重新导入") : QStringLiteral("画像失败"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeOrange"));
    ui->dataDatasetMeta->setText(
        selectedFileMissing
            ? QStringLiteral("当前后端没有这份数据副本。请重新导入该 CSV/Excel；源文件未被修改。")
            : QStringLiteral("无法建立数据画像：%1。源文件未被修改，可选择其它文件或重新导入。")
                  .arg(message.left(180)));
    ui->dataPreparationHint->setText(
        QStringLiteral("D1 已安全停止：请确认文件未加密、没有损坏，且不超过 100,000 行、100 列和 10 个可见工作表。"));
}

void MainWindow::handleDataRecommendationsReceived(const QJsonObject &recommendations)
{
    const QString currentDataset = ui->dataDatasetCombo->currentData().toString().trimmed();
    if (!dataProfileReady || recommendations.value(QStringLiteral("dataset_name")).toString() != currentDataset) {
        // 用户在短请求返回前已切换数据集时，丢弃旧结果，避免错把 A 文件的建议展示给 B 文件。
        return;
    }
    dataRecommendationLoading = false;

    ui->dataRecommendationFrame->setVisible(false);
    ui->dataRecommendationRefreshButton->setEnabled(true);
    ui->dataRecommendationHint->setText(recommendations.value(QStringLiteral("guidance")).toString());

    const QJsonArray cards = recommendations.value(QStringLiteral("recommendations")).toArray();
    const QList<QFrame *> frames = {ui->dataRecommendationCard1, ui->dataRecommendationCard2,
                                    ui->dataRecommendationCard3, ui->dataRecommendationCard4};
    const QList<QLabel *> titles = {ui->dataRecommendationTitle1, ui->dataRecommendationTitle2,
                                    ui->dataRecommendationTitle3, ui->dataRecommendationTitle4};
    const QList<QLabel *> details = {ui->dataRecommendationDetail1, ui->dataRecommendationDetail2,
                                     ui->dataRecommendationDetail3, ui->dataRecommendationDetail4};
    const QList<QLabel *> sources = {ui->dataRecommendationSource1, ui->dataRecommendationSource2,
                                     ui->dataRecommendationSource3, ui->dataRecommendationSource4};
    const QList<QPushButton *> buttons = {ui->dataRecommendationUseButton1, ui->dataRecommendationUseButton2,
                                          ui->dataRecommendationUseButton3, ui->dataRecommendationUseButton4};
    for (int index = 0; index < frames.size(); ++index) {
        const QJsonObject card = index < cards.size() ? cards.at(index).toObject() : QJsonObject{};
        const bool visible = !card.isEmpty();
        frames.at(index)->setVisible(visible);
        if (!visible) {
            buttons.at(index)->setProperty("recommendationGoal", QString());
            continue;
        }
        const QJsonArray sourceColumns = card.value(QStringLiteral("source_columns")).toArray();
        QStringList sourceNames;
        for (const QJsonValue &value : sourceColumns) {
            sourceNames.append(value.toString());
        }
        titles.at(index)->setText(card.value(QStringLiteral("question")).toString());
        details.at(index)->setText(
            QStringLiteral("%1\n%2")
                .arg(card.value(QStringLiteral("expected_output")).toString(),
                     card.value(QStringLiteral("rationale")).toString()));
        sources.at(index)->setText(
            sourceNames.isEmpty() ? QStringLiteral("范围：数据质量画像")
                                  : QStringLiteral("字段：%1").arg(sourceNames.join(QStringLiteral(" · "))));
        buttons.at(index)->setProperty("recommendationGoal", card.value(QStringLiteral("question")).toString());
        buttons.at(index)->setEnabled(true);
    }
}

void MainWindow::handleDataRecommendationsFailed(const QString &message)
{
    dataRecommendationLoading = false;
    if (!dataProfileReady) {
        return;
    }
    ui->dataRecommendationFrame->setVisible(false);
    ui->dataRecommendationRefreshButton->setEnabled(true);
    ui->dataRecommendationHint->setText(
        QStringLiteral("暂时无法整理建议：%1。你仍可直接输入目标开始本地分析。").arg(message.left(160)));
    for (QFrame *card : {ui->dataRecommendationCard1, ui->dataRecommendationCard2,
                         ui->dataRecommendationCard3, ui->dataRecommendationCard4}) {
        card->setVisible(false);
    }
}

void MainWindow::handleDataAnalysisPreviewReceived(const QJsonObject &preview)
{
    dataAnalysisLoading = false;
    lastDataAnalysisPreview = preview;
    ui->dataDatasetCombo->setEnabled(true);
    ui->dataImportButton->setEnabled(true);
    ui->dataRefreshButton->setEnabled(true);
    ui->dataAnalyzeButton->setEnabled(dataProfileReady);
    ui->dataAnalysisGoalInput->setEnabled(dataProfileReady);
    ui->dataDelegateButton->setEnabled(dataProfileReady);
    ui->dataAnalysisDetailsButton->setEnabled(true);

    const int tableCount = preview.value(QStringLiteral("analysis_tables")).toArray().size();
    const int chartCount = preview.value(QStringLiteral("charts")).toArray().size();
    const int skippedCount = preview.value(QStringLiteral("skipped_items")).toArray().size();
    ui->dataProfileStatus->setText(
        skippedCount > 0
            ? QStringLiteral("预览完成 · %1 项跳过").arg(skippedCount)
            : QStringLiteral("预览完成 · %1 表 %2 图").arg(tableCount).arg(chartCount));
    polishBadge(ui->dataProfileStatus, skippedCount > 0 ? QStringLiteral("badgeOrange") : QStringLiteral("badgeGreen"));
    const QJsonObject insight = preview.value(QStringLiteral("insight")).toObject();
    const QString headline = insight.value(QStringLiteral("headline")).toString().trimmed();
    const QString conclusion = insight.value(QStringLiteral("conclusion")).toString().trimmed();
    if (!headline.isEmpty() && !conclusion.isEmpty()) {
        const bool modelGenerated = insight.value(QStringLiteral("mode")).toString() == QStringLiteral("model");
        ui->dataInsightHeadline->setText(headline);
        ui->dataInsightConclusion->setText(conclusion);
        ui->dataInsightEvidence->setText(
            modelGenerated
                ? QStringLiteral("AI 只依据本次已验证的趋势、对比和聚合结果作出解读；可在“查看当前结果”复核依据。")
                : QStringLiteral("已根据本地聚合结果直接形成结论；可在“查看当前结果”复核依据。"));
        ui->dataInsightFrame->setVisible(true);
    } else {
        ui->dataInsightFrame->setVisible(false);
    }
    ui->dataAnalysisHint->setText(
        QStringLiteral("已生成 %1 个聚合表与 %2 个图表合同；本次结论已显示在上方，可打开详情查看完整依据。")
            .arg(tableCount)
            .arg(chartCount));
}

void MainWindow::handleDataAnalysisPreviewFailed(const QString &message)
{
    dataAnalysisLoading = false;
    ui->dataDatasetCombo->setEnabled(dataWorkspaceLoaded);
    ui->dataImportButton->setEnabled(true);
    ui->dataRefreshButton->setEnabled(dataWorkspaceLoaded);
    ui->dataAnalyzeButton->setEnabled(dataProfileReady);
    ui->dataAnalysisGoalInput->setEnabled(dataProfileReady);
    ui->dataDelegateButton->setEnabled(dataProfileReady);
    ui->dataProfileStatus->setText(QStringLiteral("预览失败"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeOrange"));
    ui->dataAnalysisHint->setText(
        QStringLiteral("分析预览未完成：%1。源文件未被修改，可调整目标或重新建立画像。")
            .arg(message.left(220)));
}

void MainWindow::requestDataAnalysisWorkbookExport()
{
    if (dataWorkspaceLoading || dataProfileLoading || dataAnalysisLoading || dataWorkbookExportLoading || dataChartExportLoading
        || dataTransformationPreviewLoading || dataTransformationExportLoading
        || !dataProfileReady || lastDataAnalysisPreview.isEmpty()) {
        return;
    }
    const QString datasetName = ui->dataDatasetCombo->currentData().toString().trimmed();
    const QString sourceSha256 = lastDataAnalysisPreview.value(QStringLiteral("dataset_profile"))
                                     .toObject()
                                     .value(QStringLiteral("source_sha256"))
                                     .toString()
                                     .trimmed();
    if (datasetName.isEmpty() || sourceSha256.size() != 64) {
        QMessageBox::warning(this,
                             QStringLiteral("导出 Excel"),
                             QStringLiteral("当前预览缺少有效数据版本，请重新生成预览后再确认导出。"));
        return;
    }

    // D3 的“确认导出”只能基于用户已经查看过的受控预览。后端会再次比对哈希，客户端不提供
    // 输出路径，也不会要求用户理解 Table/Chart 等实现细节。
    dataWorkbookExportLoading = true;
    updateDataActivityState();
    ui->dataDatasetCombo->setEnabled(false);
    ui->dataImportButton->setEnabled(false);
    ui->dataRefreshButton->setEnabled(false);
    ui->dataAnalyzeButton->setEnabled(false);
    ui->dataAnalysisDetailsButton->setEnabled(false);
    ui->dataAnalysisGoalInput->setEnabled(false);
    ui->dataProfileStatus->setText(QStringLiteral("正在导出 Excel"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeBlue"));
    ui->dataAnalysisHint->setText(
        QStringLiteral("正在受理 Excel 导出任务：随后会写入原生表格和图表并重新打开验证；源文件不会被修改。"));
    backendClient->requestDataAnalysisWorkbookExport(datasetName, sourceSha256, ui->dataAnalysisGoalInput->text());
}

void MainWindow::requestDataChartExport()
{
    if (dataWorkspaceLoading || dataProfileLoading || dataAnalysisLoading || dataWorkbookExportLoading || dataChartExportLoading
        || dataTransformationPreviewLoading || dataTransformationExportLoading
        || !dataProfileReady || lastDataAnalysisPreview.isEmpty()) {
        return;
    }
    const QString datasetName = ui->dataDatasetCombo->currentData().toString().trimmed();
    const QString sourceSha256 = lastDataAnalysisPreview.value(QStringLiteral("dataset_profile"))
                                     .toObject()
                                     .value(QStringLiteral("source_sha256"))
                                     .toString()
                                     .trimmed();
    const int chartCount = lastDataAnalysisPreview.value(QStringLiteral("charts")).toArray().size();
    if (datasetName.isEmpty() || sourceSha256.size() != 64 || chartCount < 1) {
        QMessageBox::information(
            this,
            QStringLiteral("无法生成图表"),
            QStringLiteral("当前预览缺少可绘制的已验证图表，请调整分析目标后重新生成预览。"));
        return;
    }

    const auto confirmation = QMessageBox::question(
        this,
        QStringLiteral("确认保存图表 PNG"),
        QStringLiteral("将基于当前预览的 %1 个聚合图表生成新的 PNG，并写入任务历史。\n"
                       "不会修改原始 CSV/Excel，也不会调用模型或联网。")
            .arg(chartCount),
        QMessageBox::Yes | QMessageBox::Cancel,
        QMessageBox::Yes);
    if (confirmation != QMessageBox::Yes) {
        return;
    }

    // PNG 只从 D2 的受控聚合表绘制；后端会再次比对 source_sha256 并回读像素文件。
    dataChartExportLoading = true;
    updateDataActivityState();
    ui->dataDatasetCombo->setEnabled(false);
    ui->dataImportButton->setEnabled(false);
    ui->dataRefreshButton->setEnabled(false);
    ui->dataAnalyzeButton->setEnabled(false);
    ui->dataAnalysisDetailsButton->setEnabled(false);
    ui->dataAnalysisGoalInput->setEnabled(false);
    ui->dataProfileStatus->setText(QStringLiteral("正在受理图表看板"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeBlue"));
    ui->dataAnalysisHint->setText(
        QStringLiteral("正在生成 PNG 看板：只使用当前已验证的聚合结果，完成后可在独立结果页查看。"));
    backendClient->requestDataChartExport(datasetName, sourceSha256, ui->dataAnalysisGoalInput->text());
}

void MainWindow::showDataTransformationWizard()
{
    if (dataWorkspaceLoading || dataProfileLoading || dataAnalysisLoading || dataWorkbookExportLoading
        || dataChartExportLoading || dataTransformationPreviewLoading || dataTransformationExportLoading
        || !dataProfileReady || lastDataAnalysisPreview.isEmpty()) {
        return;
    }
    const QJsonObject profile = lastDataAnalysisPreview.value(QStringLiteral("dataset_profile")).toObject();
    const QString datasetName = profile.value(QStringLiteral("dataset")).toObject().value(QStringLiteral("name")).toString();
    const QString sourceSha256 = profile.value(QStringLiteral("source_sha256")).toString();
    const QJsonArray columns = profile.value(QStringLiteral("columns")).toArray();
    if (datasetName.isEmpty() || sourceSha256.size() != 64 || columns.isEmpty()) {
        QMessageBox::warning(this, QStringLiteral("字段加工"), QStringLiteral("当前数据画像不完整，请重新生成分析预览后再试。"));
        return;
    }

    // 这个稳定工作台由 Qt Designer 管理视觉骨架。C++ 只注入当前画像字段并接收受限请求，
    // 避免把字段、说明和确认按钮散落在主结果阅读区。
    DataTransformationDialog dialog(lastDataAnalysisPreview, ui->dataAnalysisGoalInput->text(), this);
    connect(&dialog, &DataTransformationDialog::previewRequested, this, &MainWindow::requestDataTransformationPreview);
    dialog.exec();
}

void MainWindow::requestDataTransformationPreview(const QJsonObject &request)
{
    if (dataTransformationPreviewLoading || request.value(QStringLiteral("dataset_name")).toString().isEmpty()) {
        return;
    }
    pendingDataTransformationRequest = request;
    lastDataTransformationPreview = QJsonObject{};
    dataTransformationPreviewLoading = true;
    updateDataActivityState();
    ui->dataProfileStatus->setText(QStringLiteral("正在计算字段预览"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeBlue"));
    ui->dataAnalysisHint->setText(QStringLiteral("正在本地计算新增字段样例和影响范围；当前不会写入文件。"));
    backendClient->requestDataTransformationPreview(request);
}

void MainWindow::handleDataTransformationPreviewReceived(const QJsonObject &preview)
{
    dataTransformationPreviewLoading = false;
    lastDataTransformationPreview = preview;
    const int operationCount = qMax(1, preview.value(QStringLiteral("plans")).toArray().size());
    ui->dataProfileStatus->setText(QStringLiteral("字段预览已就绪"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeGreen"));
    ui->dataAnalysisHint->setText(
        QStringLiteral("已在内存中计算 %1 项字段加工；查看统一预览后再决定是否生成一个新副本。")
            .arg(operationCount));
    showDataTransformationPreviewDialog();
}

void MainWindow::handleDataTransformationPreviewFailed(const QString &message)
{
    dataTransformationPreviewLoading = false;
    pendingDataTransformationRequest = QJsonObject{};
    ui->dataProfileStatus->setText(QStringLiteral("字段预览失败"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeOrange"));
    ui->dataAnalysisHint->setText(QStringLiteral("未写入任何文件：%1").arg(message.left(220)));
}

void MainWindow::showDataTransformationPreviewDialog()
{
    if (lastDataTransformationPreview.isEmpty() || pendingDataTransformationRequest.isEmpty()) {
        return;
    }
    QDialog dialog(this);
    dialog.setWindowTitle(QStringLiteral("字段变更预览"));
    dialog.setMinimumSize(760, 520);
    dialog.resize(900, 640);
    auto *layout = new QVBoxLayout(&dialog);
    layout->setContentsMargins(18, 16, 18, 16);
    auto *title = new QLabel(QStringLiteral("确认新增字段"), &dialog);
    title->setObjectName(QStringLiteral("sectionTitle"));
    layout->addWidget(title);
    auto *browser = new QTextBrowser(&dialog);
    browser->setHtml(formatDataTransformationPreviewHtml());
    layout->addWidget(browser, 1);
    auto *buttons = new QDialogButtonBox(QDialogButtonBox::Close, &dialog);
    auto *confirmButton = buttons->addButton(QStringLiteral("确认生成新副本"), QDialogButtonBox::AcceptRole);
    confirmButton->setToolTip(QStringLiteral("写入 output/data_transformations，并重新打开验证；源文件不会被修改"));
    connect(confirmButton, &QPushButton::clicked, this, [this, &dialog]() {
        dialog.accept();
        requestDataTransformationExport();
    });
    connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    layout->addWidget(buttons);
    dialog.exec();
}

void MainWindow::requestDataTransformationExport()
{
    if (dataTransformationExportLoading || pendingDataTransformationRequest.isEmpty() || lastDataTransformationPreview.isEmpty()) {
        return;
    }
    dataTransformationExportLoading = true;
    updateDataActivityState();
    ui->dataDatasetCombo->setEnabled(false);
    ui->dataImportButton->setEnabled(false);
    ui->dataRefreshButton->setEnabled(false);
    ui->dataAnalyzeButton->setEnabled(false);
    ui->dataAnalysisDetailsButton->setEnabled(false);
    ui->dataAnalysisGoalInput->setEnabled(false);
    ui->dataProfileStatus->setText(QStringLiteral("正在受理字段加工"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeBlue"));
    ui->dataAnalysisHint->setText(QStringLiteral("正在生成新的字段加工副本并回读验证；源文件不会被修改。"));
    backendClient->requestDataTransformationExport(pendingDataTransformationRequest);
}

void MainWindow::handleDataTransformationExportStarted(const QString &taskId)
{
    if (!dataTransformationExportLoading || taskId.isEmpty()) {
        return;
    }
    activeDataTransformationTaskId = taskId;
    lastDataTransformationTaskId = taskId;
    ui->dataWorkbookHistoryStrip->setVisible(true);
    ui->dataWorkbookHistoryButton->setToolTip(QStringLiteral("查看本次字段加工任务与受控数据副本"));
    ui->dataProfileStatus->setText(QStringLiteral("字段加工已受理"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeBlue"));
    backendClient->connectTaskLog(taskId);
    QTimer::singleShot(450, this, [this, taskId]() {
        if (dataTransformationExportLoading && activeDataTransformationTaskId == taskId) {
            backendClient->requestDataTransformationExportResult(taskId);
        }
    });
}

void MainWindow::handleDataTransformationExportStillRunning(const QString &taskId, const QString &status)
{
    if (!dataTransformationExportLoading || taskId != activeDataTransformationTaskId) {
        return;
    }
    ui->dataProfileStatus->setText(status == QStringLiteral("queued") || status == QStringLiteral("pending")
        ? QStringLiteral("字段加工任务排队中") : QStringLiteral("正在写入并验证新副本"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeBlue"));
    QTimer::singleShot(450, this, [this, taskId]() {
        if (dataTransformationExportLoading && activeDataTransformationTaskId == taskId) {
            backendClient->requestDataTransformationExportResult(taskId);
        }
    });
}

void MainWindow::handleDataTransformationExported(const QJsonObject &result)
{
    dataTransformationExportLoading = false;
    activeDataTransformationTaskId.clear();
    ui->dataDatasetCombo->setEnabled(dataWorkspaceLoaded);
    ui->dataImportButton->setEnabled(true);
    ui->dataRefreshButton->setEnabled(dataWorkspaceLoaded);
    ui->dataAnalyzeButton->setEnabled(dataProfileReady);
    ui->dataAnalysisGoalInput->setEnabled(dataProfileReady);
    ui->dataDelegateButton->setEnabled(dataProfileReady);
    ui->dataAnalysisDetailsButton->setEnabled(!lastDataAnalysisPreview.isEmpty());
    const QJsonObject artifact = result.value(QStringLiteral("artifact")).toObject();
    const QJsonArray plans = result.value(QStringLiteral("plans")).toArray();
    const QJsonObject plan = result.value(QStringLiteral("plan")).toObject();
    const int operationCount = qMax(1, plans.size());
    ui->dataProfileStatus->setText(QStringLiteral("字段副本已交付 · 验证通过"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeGreen"));
    ui->dataAnalysisHint->setText(
        QStringLiteral("已新增 %1 个字段并交付“%2”。正在用系统默认程序打开副本；源文件保持不变。")
            .arg(operationCount)
            .arg(artifact.value(QStringLiteral("name")).toString()));
    const QString taskId = result.value(QStringLiteral("task_id")).toString();
    if (!taskId.isEmpty()) {
        pendingAutoOpenArtifactTaskId = taskId;
        backendClient->requestTaskArtifacts(taskId);
    }
}

void MainWindow::handleDataTransformationExportCancelled(const QString &message)
{
    dataTransformationExportLoading = false;
    activeDataTransformationTaskId.clear();
    ui->dataDatasetCombo->setEnabled(dataWorkspaceLoaded);
    ui->dataImportButton->setEnabled(true);
    ui->dataRefreshButton->setEnabled(dataWorkspaceLoaded);
    ui->dataAnalyzeButton->setEnabled(dataProfileReady);
    ui->dataAnalysisGoalInput->setEnabled(dataProfileReady);
    ui->dataAnalysisDetailsButton->setEnabled(!lastDataAnalysisPreview.isEmpty());
    ui->dataProfileStatus->setText(QStringLiteral("字段加工已取消"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeGray"));
    ui->dataAnalysisHint->setText(QStringLiteral("%1 当前字段预览仍可复核；源文件未被修改。").arg(message.left(180)));
}

void MainWindow::handleDataTransformationExportFailed(const QString &message)
{
    dataTransformationExportLoading = false;
    activeDataTransformationTaskId.clear();
    ui->dataDatasetCombo->setEnabled(dataWorkspaceLoaded);
    ui->dataImportButton->setEnabled(true);
    ui->dataRefreshButton->setEnabled(dataWorkspaceLoaded);
    ui->dataAnalyzeButton->setEnabled(dataProfileReady);
    ui->dataAnalysisGoalInput->setEnabled(dataProfileReady);
    ui->dataAnalysisDetailsButton->setEnabled(!lastDataAnalysisPreview.isEmpty());
    ui->dataProfileStatus->setText(QStringLiteral("字段加工未完成"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeOrange"));
    ui->dataAnalysisHint->setText(QStringLiteral("未生成正式副本：%1。源文件未被修改。").arg(message.left(220)));
}

QString MainWindow::formatDataTransformationPreviewHtml() const
{
    const auto escaped = [](const QString &value) { return value.toHtmlEscaped(); };
    QJsonArray plans = lastDataTransformationPreview.value(QStringLiteral("plans")).toArray();
    if (plans.isEmpty()) {
        plans.append(lastDataTransformationPreview.value(QStringLiteral("plan")).toObject());
    }
    const QJsonObject firstPlan = plans.first().toObject();
    QString html = QStringLiteral("<style>body{font-family:'Microsoft YaHei','Segoe UI';color:#17365D;}h3{margin:16px 0 8px;}table{border-collapse:collapse;width:100%;margin:6px 0 12px;}th{background:#EEF5FF;text-align:left;}td,th{border:1px solid #D8E5F6;padding:7px;}li{margin:4px 0;}</style>");
    html += QStringLiteral("<h3>本次变更</h3><p><b>将新增 %1 个字段。</b>所有加工先在内存中计算，确认后只写入一个新副本。</p>")
                .arg(plans.size());
    html += QStringLiteral("<table><tr><th>加工</th><th>来源字段</th><th>新字段</th><th>范围</th></tr>");
    for (const QJsonValue &value : plans) {
        const QJsonObject plan = value.toObject();
        const QString sources = plan.value(QStringLiteral("secondary_column")).toString().isEmpty()
            ? plan.value(QStringLiteral("primary_column")).toString()
            : QStringLiteral("%1、%2").arg(plan.value(QStringLiteral("primary_column")).toString(),
                                              plan.value(QStringLiteral("secondary_column")).toString());
        html += QStringLiteral("<tr><td>%1</td><td>%2</td><td>%3</td><td>%4</td></tr>")
                    .arg(escaped(plan.value(QStringLiteral("rationale")).toString()),
                         escaped(sources),
                         escaped(plan.value(QStringLiteral("result_column")).toString()),
                         escaped(plan.value(QStringLiteral("scope_description")).toString()));
    }
    html += QStringLiteral("</table>");
    html += QStringLiteral("<p><b>有效结果单元格：</b>%1　<b>空结果单元格：</b>%2</p>")
                .arg(lastDataTransformationPreview.value(QStringLiteral("affected_count")).toInt())
                .arg(lastDataTransformationPreview.value(QStringLiteral("empty_result_count")).toInt());
    html += QStringLiteral("<h3>首项“%1”的样例</h3><table><tr><th>行</th><th>来源值</th><th>新字段结果</th></tr>")
                .arg(escaped(firstPlan.value(QStringLiteral("result_column")).toString()));
    for (const QJsonValue &value : lastDataTransformationPreview.value(QStringLiteral("previews")).toArray()) {
        const QJsonObject row = value.toObject();
        QStringList sources;
        for (const QJsonValue &source : row.value(QStringLiteral("source_values")).toArray()) {
            sources.append(escaped(source.toString()));
        }
        html += QStringLiteral("<tr><td>%1</td><td>%2</td><td>%3</td></tr>")
                    .arg(row.value(QStringLiteral("row_number")).toInt())
                    .arg(sources.join(QStringLiteral("　|　")))
                    .arg(escaped(row.value(QStringLiteral("result_value")).toString()));
    }
    html += QStringLiteral("</table>");
    const QJsonArray warnings = lastDataTransformationPreview.value(QStringLiteral("warnings")).toArray();
    if (!warnings.isEmpty()) {
        html += QStringLiteral("<h3>需要留意</h3><ul>");
        for (const QJsonValue &warning : warnings) {
            html += QStringLiteral("<li>%1</li>").arg(escaped(warning.toString()));
        }
        html += QStringLiteral("</ul>");
    }
    html += QStringLiteral("<p style='color:#64748B;'>确认后只会新建一个与源文件同类型的数据副本，并重新打开验证字段与行数；不会覆盖当前 CSV/Excel。</p>");
    return html;
}

void MainWindow::handleDataAnalysisWorkbookExportStarted(const QString &taskId)
{
    if (!dataWorkbookExportLoading || taskId.isEmpty()) {
        return;
    }
    activeDataWorkbookExportTaskId = taskId;
    lastDataWorkbookExportTaskId = taskId;
    ui->dataWorkbookHistoryStrip->setVisible(true);
    ui->dataProfileStatus->setText(QStringLiteral("Excel 导出已受理"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeBlue"));
    ui->dataAnalysisHint->setText(
        QStringLiteral("正在连接真实阶段：复核数据版本、写入 Excel、回读验证。完成后会自动写入任务历史。"));
    backendClient->connectTaskLog(taskId);
    // 小型数据可能在 WebSocket 握手前已经结束。延迟轮询只作为事件流兜底，不显示虚假进度。
    QTimer::singleShot(450, this, [this, taskId]() {
        if (dataWorkbookExportLoading && activeDataWorkbookExportTaskId == taskId) {
            backendClient->requestDataAnalysisWorkbookExportResult(taskId);
        }
    });
}

void MainWindow::handleDataAnalysisWorkbookExportStillRunning(const QString &taskId, const QString &status)
{
    if (!dataWorkbookExportLoading || taskId != activeDataWorkbookExportTaskId) {
        return;
    }
    ui->dataProfileStatus->setText(
        status == QStringLiteral("queued") || status == QStringLiteral("pending")
            ? QStringLiteral("Excel 导出任务排队中")
            : QStringLiteral("正在生成并验证 Excel"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeBlue"));
    QTimer::singleShot(450, this, [this, taskId]() {
        if (dataWorkbookExportLoading && activeDataWorkbookExportTaskId == taskId) {
            backendClient->requestDataAnalysisWorkbookExportResult(taskId);
        }
    });
}

void MainWindow::handleDataAnalysisWorkbookExported(const QJsonObject &result)
{
    dataWorkbookExportLoading = false;
    activeDataWorkbookExportTaskId.clear();
    ui->dataDatasetCombo->setEnabled(dataWorkspaceLoaded);
    ui->dataImportButton->setEnabled(true);
    ui->dataRefreshButton->setEnabled(dataWorkspaceLoaded);
    ui->dataAnalyzeButton->setEnabled(dataProfileReady);
    ui->dataAnalysisGoalInput->setEnabled(dataProfileReady);
    ui->dataAnalysisDetailsButton->setEnabled(!lastDataAnalysisPreview.isEmpty());
    ui->dataWorkbookHistoryStrip->setVisible(!lastDataWorkbookExportTaskId.isEmpty());

    const QJsonObject artifact = result.value(QStringLiteral("artifact")).toObject();
    const QJsonObject verification = result.value(QStringLiteral("verification")).toObject();
    const int tableCount = verification.value(QStringLiteral("table_count")).toInt();
    const int chartCount = verification.value(QStringLiteral("chart_count")).toInt();
    ui->dataProfileStatus->setText(QStringLiteral("Excel 已交付 · 验证通过"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeGreen"));
    ui->dataAnalysisHint->setText(
        QStringLiteral("已生成“%1”：%2 个原生数据表、%3 个原生图表均已回读验证，正在用默认程序打开；源文件保持不变。")
            .arg(artifact.value(QStringLiteral("name")).toString())
            .arg(tableCount)
            .arg(chartCount));
    const QString taskId = result.value(QStringLiteral("task_id")).toString();
    if (!taskId.isEmpty()) {
        pendingAutoOpenArtifactTaskId = taskId;
        backendClient->requestTaskArtifacts(taskId);
    }
}

void MainWindow::handleDataAnalysisWorkbookExportCancelled(const QString &message)
{
    // 取消只影响本次新建工作簿，已在当前页生成的确定性预览仍有效。恢复可操作控件，让客户
    // 复核相同预览后再次确认导出，而不是从历史记录隐式重放旧目标。
    dataWorkbookExportLoading = false;
    activeDataWorkbookExportTaskId.clear();
    ui->dataDatasetCombo->setEnabled(dataWorkspaceLoaded);
    ui->dataImportButton->setEnabled(true);
    ui->dataRefreshButton->setEnabled(dataWorkspaceLoaded);
    ui->dataAnalyzeButton->setEnabled(dataProfileReady);
    ui->dataAnalysisGoalInput->setEnabled(dataProfileReady);
    ui->dataAnalysisDetailsButton->setEnabled(!lastDataAnalysisPreview.isEmpty());
    ui->dataWorkbookHistoryStrip->setVisible(!lastDataWorkbookExportTaskId.isEmpty());
    ui->dataProfileStatus->setText(QStringLiteral("Excel 导出已取消"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeGray"));
    ui->dataAnalysisHint->setText(
        QStringLiteral("%1 当前预览仍可查看；确认后可以重新导出，源文件未被修改。")
            .arg(message.left(180)));
}

void MainWindow::handleDataAnalysisWorkbookExportFailed(const QString &message)
{
    dataWorkbookExportLoading = false;
    activeDataWorkbookExportTaskId.clear();
    ui->dataDatasetCombo->setEnabled(dataWorkspaceLoaded);
    ui->dataImportButton->setEnabled(true);
    ui->dataRefreshButton->setEnabled(dataWorkspaceLoaded);
    ui->dataAnalyzeButton->setEnabled(dataProfileReady);
    ui->dataAnalysisGoalInput->setEnabled(dataProfileReady);
    ui->dataAnalysisDetailsButton->setEnabled(!lastDataAnalysisPreview.isEmpty());
    ui->dataWorkbookHistoryStrip->setVisible(!lastDataWorkbookExportTaskId.isEmpty());
    ui->dataProfileStatus->setText(QStringLiteral("Excel 导出失败"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeOrange"));
    ui->dataAnalysisHint->setText(
        QStringLiteral("Excel 未生成正式交付文件：%1。请重新查看预览或稍后再试；源文件未被修改。")
            .arg(message.left(220)));
}

void MainWindow::handleDataChartExportStarted(const QString &taskId)
{
    if (!dataChartExportLoading || taskId.isEmpty()) {
        return;
    }
    activeDataChartExportTaskId = taskId;
    lastDataChartExportTaskId = taskId;
    ui->dataWorkbookHistoryStrip->setVisible(true);
    ui->dataWorkbookHistoryButton->setToolTip(QStringLiteral("查看本次图表看板任务与受控 PNG artifact"));
    ui->dataProfileStatus->setText(QStringLiteral("图表看板已受理"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeBlue"));
    ui->dataAnalysisHint->setText(
        QStringLiteral("正在复核数据版本、绘制图表并回读 PNG。完成后会自动写入任务历史。"));
    backendClient->connectTaskLog(taskId);
    QTimer::singleShot(450, this, [this, taskId]() {
        if (dataChartExportLoading && activeDataChartExportTaskId == taskId) {
            backendClient->requestDataChartExportResult(taskId);
        }
    });
}

void MainWindow::handleDataChartExportStillRunning(const QString &taskId, const QString &status)
{
    if (!dataChartExportLoading || taskId != activeDataChartExportTaskId) {
        return;
    }
    ui->dataProfileStatus->setText(
        status == QStringLiteral("queued") || status == QStringLiteral("pending")
            ? QStringLiteral("图表看板任务排队中")
            : QStringLiteral("正在绘制并验证 PNG"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeBlue"));
    QTimer::singleShot(450, this, [this, taskId]() {
        if (dataChartExportLoading && activeDataChartExportTaskId == taskId) {
            backendClient->requestDataChartExportResult(taskId);
        }
    });
}

void MainWindow::handleDataChartExported(const QJsonObject &result)
{
    dataChartExportLoading = false;
    activeDataChartExportTaskId.clear();
    ui->dataDatasetCombo->setEnabled(dataWorkspaceLoaded);
    ui->dataImportButton->setEnabled(true);
    ui->dataRefreshButton->setEnabled(dataWorkspaceLoaded);
    ui->dataAnalyzeButton->setEnabled(dataProfileReady);
    ui->dataAnalysisGoalInput->setEnabled(dataProfileReady);
    ui->dataAnalysisDetailsButton->setEnabled(!lastDataAnalysisPreview.isEmpty());
    ui->dataWorkbookHistoryStrip->setVisible(!lastDataChartExportTaskId.isEmpty() || !lastDataWorkbookExportTaskId.isEmpty());

    const QJsonObject verification = result.value(QStringLiteral("verification")).toObject();
    const int chartCount = verification.value(QStringLiteral("chart_count")).toInt();
    ui->dataProfileStatus->setText(QStringLiteral("图表看板已交付 · 验证通过"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeGreen"));
    ui->dataAnalysisHint->setText(
        QStringLiteral("已生成 %1 张 PNG 图表。看板和受控 artifact 已写入任务历史，源文件未被修改。")
            .arg(chartCount));
    showDataChartDashboard(result);
}

void MainWindow::handleDataChartExportCancelled(const QString &message)
{
    dataChartExportLoading = false;
    activeDataChartExportTaskId.clear();
    ui->dataDatasetCombo->setEnabled(dataWorkspaceLoaded);
    ui->dataImportButton->setEnabled(true);
    ui->dataRefreshButton->setEnabled(dataWorkspaceLoaded);
    ui->dataAnalyzeButton->setEnabled(dataProfileReady);
    ui->dataAnalysisGoalInput->setEnabled(dataProfileReady);
    ui->dataAnalysisDetailsButton->setEnabled(!lastDataAnalysisPreview.isEmpty());
    ui->dataProfileStatus->setText(QStringLiteral("图表看板已取消"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeGray"));
    ui->dataAnalysisHint->setText(
        QStringLiteral("%1 当前预览仍可查看；确认后可再次保存图表，源文件未被修改。")
            .arg(message.left(180)));
}

void MainWindow::handleDataChartExportFailed(const QString &message)
{
    dataChartExportLoading = false;
    activeDataChartExportTaskId.clear();
    ui->dataDatasetCombo->setEnabled(dataWorkspaceLoaded);
    ui->dataImportButton->setEnabled(true);
    ui->dataRefreshButton->setEnabled(dataWorkspaceLoaded);
    ui->dataAnalyzeButton->setEnabled(dataProfileReady);
    ui->dataAnalysisGoalInput->setEnabled(dataProfileReady);
    ui->dataAnalysisDetailsButton->setEnabled(!lastDataAnalysisPreview.isEmpty());
    ui->dataProfileStatus->setText(QStringLiteral("图表看板未完成"));
    polishBadge(ui->dataProfileStatus, QStringLiteral("badgeOrange"));
    ui->dataAnalysisHint->setText(
        QStringLiteral("PNG 未生成正式交付文件：%1。请重新查看分析预览后再试；源文件未被修改。")
            .arg(message.left(220)));
}

void MainWindow::handleDataChartImageReceived(
    const QString &taskId,
    const QString &artifactId,
    const QByteArray &imageBytes)
{
    if (taskId == currentDispatchDeliveryPreviewArtifactTaskId
        && artifactId == currentDispatchDeliveryPreviewArtifactId
        && ui->dispatchDeliveryImage) {
        QPixmap pixmap;
        if (pixmap.loadFromData(imageBytes, "PNG")) {
            currentDispatchDeliveryImage = pixmap;
            renderDispatchDeliveryImage();
        } else {
            ui->dispatchDeliveryImage->setText(QStringLiteral("图表缩略图无法解码，可打开交付物查看。"));
        }
        return;
    }

    if (taskId != dataChartDashboardTaskId || !dataChartDashboardImageLabel || !dataChartDashboardList) {
        return;
    }
    QPixmap pixmap;
    if (!pixmap.loadFromData(imageBytes, "PNG")) {
        if (dataChartDashboardStatusLabel) {
            dataChartDashboardStatusLabel->setText(QStringLiteral("图表图片无法解码；可在任务历史查看 artifact。"));
        }
        return;
    }
    dataChartDashboardPixmapCache.insert(artifactId, pixmap);
    const auto *currentItem = dataChartDashboardList->currentItem();
    if (!currentItem || currentItem->data(Qt::UserRole).toString() != artifactId) {
        return;
    }
    renderDataChartDashboardPixmap(pixmap);
}

void MainWindow::renderDataChartDashboardPixmap(const QPixmap &pixmap)
{
    if (!dataChartDashboardImageLabel || pixmap.isNull()) {
        return;
    }

    QPixmap rendered = pixmap;
    if (dataChartDashboardScrollArea) {
        // 看板只负责阅读已验证 PNG。图像始终按当前可视区域等比缩放，窗口最大化、还原或拖动
        // 后都会重新渲染，让客户不必再手动选倍率或拖大对话框才能看全图表。
        const QSize viewport = dataChartDashboardScrollArea->viewport()->size() - QSize(20, 20);
        if (viewport.isValid()) {
            rendered = pixmap.scaled(viewport, Qt::KeepAspectRatio, Qt::SmoothTransformation);
        }
    }
    dataChartDashboardImageLabel->setPixmap(rendered);
    dataChartDashboardImageLabel->resize(rendered.size());
    if (dataChartDashboardStatusLabel) {
        dataChartDashboardStatusLabel->setText(
            QStringLiteral("已验证 PNG · 原图 %1 × %2 · 随窗口自适应")
                .arg(pixmap.width())
                .arg(pixmap.height()));
    }
}

void MainWindow::handleDataChartImageFailed(const QString &taskId, const QString &artifactId, const QString &message)
{
    if (taskId == currentDispatchDeliveryPreviewArtifactTaskId
        && artifactId == currentDispatchDeliveryPreviewArtifactId
        && ui->dispatchDeliveryImage) {
        ui->dispatchDeliveryImage->setText(
            QStringLiteral("图表缩略图暂时无法读取，可点击“打开交付物”。"));
    }
    Q_UNUSED(artifactId)
    if (taskId == dataChartDashboardTaskId && dataChartDashboardStatusLabel) {
        dataChartDashboardStatusLabel->setText(
            QStringLiteral("图表预览读取失败：%1").arg(message.left(160)));
    }
}

void MainWindow::showDataChartDashboard(const QJsonObject &result)
{
    const QString taskId = result.value(QStringLiteral("task_id")).toString().trimmed();
    const QJsonArray artifacts = result.value(QStringLiteral("artifacts")).toArray();
    if (taskId.isEmpty() || artifacts.isEmpty()) {
        return;
    }
    if (dataChartDashboardDialog) {
        dataChartDashboardDialog->close();
    }
    dataChartDashboardPixmapCache.clear();
    dataChartDashboardTaskId = taskId;

    auto *dialog = new QDialog(this);
    dialog->setAttribute(Qt::WA_DeleteOnClose);
    dialog->setWindowTitle(QStringLiteral("图表看板"));
    dialog->setWindowFlag(Qt::WindowMaximizeButtonHint, true);
    dialog->setMinimumSize(900, 640);
    dialog->setSizeGripEnabled(true);
    dialog->resize(1120, 760);
    dataChartDashboardDialog = dialog;
    auto *layout = new QVBoxLayout(dialog);
    layout->setContentsMargins(20, 18, 20, 16);
    layout->setSpacing(12);
    auto *title = new QLabel(QStringLiteral("图表看板"), dialog);
    title->setObjectName(QStringLiteral("sectionTitle"));
    layout->addWidget(title);
    auto *subtitle = new QLabel(
        QStringLiteral("只展示当前分析预览的本地聚合结果。PNG 已回读验证；可从左侧切换图表，完整审计保留在任务历史。"),
        dialog);
    subtitle->setObjectName(QStringLiteral("subText"));
    subtitle->setWordWrap(true);
    layout->addWidget(subtitle);

    auto *splitter = new QSplitter(Qt::Horizontal, dialog);
    splitter->setChildrenCollapsible(false);
    auto *list = new QListWidget(splitter);
    list->setMinimumWidth(235);
    list->setMaximumWidth(320);
    dataChartDashboardList = list;
    for (const QJsonValue &value : artifacts) {
        const QJsonObject artifact = value.toObject();
        const QString artifactId = artifact.value(QStringLiteral("artifact_id")).toString();
        if (artifactId.isEmpty()) {
            continue;
        }
        auto *item = new QListWidgetItem(
            QStringLiteral("%1\n%2 · %3×%4")
                .arg(artifact.value(QStringLiteral("title")).toString(),
                     artifact.value(QStringLiteral("chart_type")).toString(),
                     QString::number(artifact.value(QStringLiteral("width")).toInt()),
                     QString::number(artifact.value(QStringLiteral("height")).toInt())),
            list);
        item->setData(Qt::UserRole, artifactId);
        item->setToolTip(QStringLiteral("查看已验证的 PNG 图表"));
        item->setSizeHint(QSize(230, 62));
    }
    auto *scrollArea = new ChartDashboardScrollArea(splitter);
    scrollArea->setWidgetResizable(false);
    scrollArea->setAlignment(Qt::AlignCenter);
    auto *imageLabel = new QLabel(scrollArea);
    imageLabel->setAlignment(Qt::AlignCenter);
    imageLabel->setMinimumSize(1, 1);
    imageLabel->setSizePolicy(QSizePolicy::Fixed, QSizePolicy::Fixed);
    imageLabel->setText(QStringLiteral("正在读取已验证图表…"));
    imageLabel->setObjectName(QStringLiteral("subText"));
    scrollArea->setWidget(imageLabel);
    dataChartDashboardScrollArea = scrollArea;
    dataChartDashboardImageLabel = imageLabel;
    scrollArea->onViewportResized = [this]() {
        if (!dataChartDashboardList || !dataChartDashboardList->currentItem()) {
            return;
        }
        const QString artifactId = dataChartDashboardList->currentItem()->data(Qt::UserRole).toString();
        const auto cached = dataChartDashboardPixmapCache.constFind(artifactId);
        if (cached != dataChartDashboardPixmapCache.cend()) {
            renderDataChartDashboardPixmap(*cached);
        }
    };
    splitter->setStretchFactor(0, 0);
    splitter->setStretchFactor(1, 1);
    layout->addWidget(splitter, 1);
    auto *status = new QLabel(QStringLiteral("选择左侧图表以查看。"), dialog);
    status->setObjectName(QStringLiteral("subText"));
    dataChartDashboardStatusLabel = status;
    layout->addWidget(status);

    auto *buttons = new QDialogButtonBox(QDialogButtonBox::Close, dialog);
    auto *historyButton = buttons->addButton(QStringLiteral("查看任务历史"), QDialogButtonBox::ActionRole);
    connect(historyButton, &QPushButton::clicked, this, [this, taskId]() { openTaskInHistory(taskId); });
    connect(buttons, &QDialogButtonBox::rejected, dialog, &QDialog::close);
    layout->addWidget(buttons);
    connect(list, &QListWidget::currentItemChanged, this, [this](QListWidgetItem *current, QListWidgetItem *) {
        if (!current || !dataChartDashboardImageLabel) {
            return;
        }
        const QString artifactId = current->data(Qt::UserRole).toString();
        const auto cached = dataChartDashboardPixmapCache.constFind(artifactId);
        if (cached != dataChartDashboardPixmapCache.cend()) {
            renderDataChartDashboardPixmap(*cached);
            return;
        }
        dataChartDashboardImageLabel->setPixmap(QPixmap{});
        dataChartDashboardImageLabel->setText(QStringLiteral("正在读取已验证图表…"));
        if (dataChartDashboardStatusLabel) {
            dataChartDashboardStatusLabel->setText(QStringLiteral("正在加载图表预览…"));
        }
        backendClient->requestDataChartImage(dataChartDashboardTaskId, artifactId);
    });
    connect(dialog, &QObject::destroyed, this, [this, taskId]() {
        if (dataChartDashboardTaskId == taskId) {
            dataChartDashboardTaskId.clear();
            dataChartDashboardPixmapCache.clear();
        }
        dataChartDashboardDialog = nullptr;
        dataChartDashboardList = nullptr;
        dataChartDashboardScrollArea = nullptr;
        dataChartDashboardImageLabel = nullptr;
        dataChartDashboardStatusLabel = nullptr;
    });
    dialog->show();
    dialog->raise();
    list->setCurrentRow(0);
}

void MainWindow::showDataAnalysisPreviewDialog()
{
    if (lastDataAnalysisPreview.isEmpty()) {
        return;
    }

    QDialog dialog(this);
    dialog.setWindowTitle(QStringLiteral("数据分析预览"));
    dialog.setMinimumSize(760, 540);
    dialog.resize(920, 680);
    auto *layout = new QVBoxLayout(&dialog);
    layout->setContentsMargins(18, 16, 18, 16);
    layout->setSpacing(12);
    auto *title = new QLabel(QStringLiteral("本地分析预览"), &dialog);
    title->setObjectName(QStringLiteral("sectionTitle"));
    layout->addWidget(title);
    auto *subtitle = new QLabel(
        QStringLiteral("先查看本次结论和数据依据；需要图片时生成 PNG，看完后也可以创建含新增字段的原格式副本。"),
        &dialog);
    subtitle->setObjectName(QStringLiteral("subText"));
    subtitle->setWordWrap(true);
    layout->addWidget(subtitle);
    auto *browser = new QTextBrowser(&dialog);
    browser->setOpenExternalLinks(false);
    browser->setHtml(formatDataAnalysisPreviewHtml());
    layout->addWidget(browser, 1);
    auto *buttons = new QDialogButtonBox(QDialogButtonBox::Close, &dialog);
    auto *chartButton = buttons->addButton(QStringLiteral("确认生成 PNG 看板"), QDialogButtonBox::ActionRole);
    chartButton->setToolTip(QStringLiteral("按当前已验证聚合结果生成 PNG 图表，不修改源文件"));
    auto *transformButton = buttons->addButton(QStringLiteral("创建数据副本…"), QDialogButtonBox::ActionRole);
    transformButton->setToolTip(QStringLiteral("选择多个新增字段；确认后只生成一份保持原格式的新副本"));
    connect(chartButton, &QPushButton::clicked, this, [this, &dialog]() {
        // 图表 PNG 是单独的新交付物；点击后还会进入显式确认框，避免预览窗口被误操作写文件。
        dialog.accept();
        requestDataChartExport();
    });
    connect(transformButton, &QPushButton::clicked, this, [this, &dialog]() {
        dialog.accept();
        showDataTransformationWizard();
    });
    connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    layout->addWidget(buttons);
    dialog.exec();
}

QString MainWindow::formatDataAnalysisPreviewHtml() const
{
    const auto escaped = [](const QString &value) { return value.toHtmlEscaped(); };
    QString html = QStringLiteral("<style>body{font-family:'Microsoft YaHei','Segoe UI';color:#17365D;}h3{margin:18px 0 8px;}table{border-collapse:collapse;width:100%;margin:6px 0 12px;}th{background:#EEF5FF;text-align:left;}td,th{border:1px solid #D8E5F6;padding:7px;}li{margin:4px 0;}</style>");
    const QJsonObject profile = lastDataAnalysisPreview.value(QStringLiteral("dataset_profile")).toObject();
    const QJsonObject dataset = profile.value(QStringLiteral("dataset")).toObject();
    const QJsonObject insight = lastDataAnalysisPreview.value(QStringLiteral("insight")).toObject();
    if (!insight.isEmpty()) {
        const bool modelGenerated = insight.value(QStringLiteral("mode")).toString() == QStringLiteral("model");
        html += QStringLiteral(
                    "<div style='padding:16px 18px;background:#EEF6FF;border:1px solid #BFDBFE;border-radius:10px;'>"
                    "<div style='color:#1D4ED8;font-weight:700;font-size:13px;margin-bottom:5px;'>本次结论 · %1</div>"
                    "<div style='color:#0F172A;font-size:20px;font-weight:700;margin-bottom:8px;'>%2</div>"
                    "<div style='color:#334155;line-height:1.75;'>%3</div>")
                    .arg(modelGenerated ? QStringLiteral("AI 基于已验证数据生成") : QStringLiteral("基于本地已验证数据"),
                         escaped(insight.value(QStringLiteral("headline")).toString()),
                         escaped(insight.value(QStringLiteral("conclusion")).toString()));
        const QJsonArray highlights = insight.value(QStringLiteral("highlights")).toArray();
        if (!highlights.isEmpty()) {
            html += QStringLiteral("<ul style='margin:10px 0 0 18px;padding:0;color:#475569;'>");
            for (const QJsonValue &value : highlights) {
                html += QStringLiteral("<li>%1</li>").arg(escaped(value.toString()));
            }
            html += QStringLiteral("</ul>");
        }
        const QJsonArray nextActions = insight.value(QStringLiteral("next_actions")).toArray();
        if (!nextActions.isEmpty()) {
            html += QStringLiteral("<div style='margin-top:12px;color:#1E40AF;font-weight:700;'>建议下一步</div><ul style='margin:6px 0 0 18px;padding:0;color:#475569;'>");
            for (const QJsonValue &value : nextActions) {
                html += QStringLiteral("<li>%1</li>").arg(escaped(value.toString()));
            }
            html += QStringLiteral("</ul>");
        }
        html += QStringLiteral("</div>");
    }
    html += QStringLiteral("<h3>范围</h3><p><b>%1</b> · %2 行 × %3 列 · 主表：%4</p>")
                .arg(escaped(dataset.value(QStringLiteral("name")).toString()))
                .arg(profile.value(QStringLiteral("row_count")).toInt())
                .arg(profile.value(QStringLiteral("column_count")).toInt())
                .arg(escaped(profile.value(QStringLiteral("selected_sheet")).toString()));

    const QJsonArray metrics = lastDataAnalysisPreview.value(QStringLiteral("metrics")).toArray();
    html += QStringLiteral("<h3>关键指标</h3><table><tr><th>指标</th><th>数值</th><th>口径</th></tr>");
    for (const QJsonValue &value : metrics) {
        const QJsonObject metric = value.toObject();
        html += QStringLiteral("<tr><td>%1</td><td>%2</td><td>%3</td></tr>")
                    .arg(escaped(metric.value(QStringLiteral("name")).toString()))
                    .arg(formatDataDisplayNumber(metric.value(QStringLiteral("value")).toDouble()))
                    .arg(escaped(metric.value(QStringLiteral("aggregation")).toString()));
    }
    html += QStringLiteral("</table>");

    const QJsonArray tables = lastDataAnalysisPreview.value(QStringLiteral("analysis_tables")).toArray();
    html += QStringLiteral("<h3>聚合表</h3>");
    for (const QJsonValue &value : tables) {
        const QJsonObject table = value.toObject();
        html += QStringLiteral("<p><b>%1</b>%2</p><table><tr>")
                    .arg(escaped(table.value(QStringLiteral("title")).toString()))
                    .arg(table.value(QStringLiteral("truncated")).toBool() ? QStringLiteral("（仅显示前 12 项）") : QString());
        const QJsonArray columns = table.value(QStringLiteral("columns")).toArray();
        for (const QJsonValue &column : columns) {
            html += QStringLiteral("<th>%1</th>").arg(escaped(column.toString()));
        }
        html += QStringLiteral("</tr>");
        for (const QJsonValue &rowValue : table.value(QStringLiteral("rows")).toArray()) {
            html += QStringLiteral("<tr>");
            for (const QJsonValue &cell : rowValue.toArray()) {
                html += QStringLiteral("<td>%1</td>").arg(escaped(cell.toString()));
            }
            html += QStringLiteral("</tr>");
        }
        html += QStringLiteral("</table>");
    }

    html += QStringLiteral("<h3>已生成图表</h3><ul>");
    for (const QJsonValue &value : lastDataAnalysisPreview.value(QStringLiteral("charts")).toArray()) {
        const QJsonObject chart = value.toObject();
        html += QStringLiteral("<li>%1 · %2（%3 → %4）</li>")
                    .arg(escaped(chart.value(QStringLiteral("chart_type")).toString()))
                    .arg(escaped(chart.value(QStringLiteral("title")).toString()))
                    .arg(escaped(chart.value(QStringLiteral("category_column")).toString()))
                    .arg(escaped(chart.value(QStringLiteral("value_column")).toString()));
    }
    html += QStringLiteral("</ul>");

    const QJsonArray findings = lastDataAnalysisPreview.value(QStringLiteral("quality_findings")).toArray();
    const QJsonArray warnings = lastDataAnalysisPreview.value(QStringLiteral("warnings")).toArray();
    const QJsonArray skipped = lastDataAnalysisPreview.value(QStringLiteral("skipped_items")).toArray();
    if (!findings.isEmpty() || !warnings.isEmpty() || !skipped.isEmpty()) {
        html += QStringLiteral("<h3>需要留意</h3><ul>");
        for (const QJsonValue &value : findings) {
            const QJsonObject finding = value.toObject();
            html += QStringLiteral("<li><b>%1</b>：%2</li>")
                        .arg(escaped(finding.value(QStringLiteral("title")).toString()))
                        .arg(escaped(finding.value(QStringLiteral("handling")).toString()));
        }
        for (const QJsonValue &value : warnings) {
            html += QStringLiteral("<li>%1</li>").arg(escaped(value.toString()));
        }
        for (const QJsonValue &value : skipped) {
            html += QStringLiteral("<li>已跳过：%1</li>").arg(escaped(value.toString()));
        }
        html += QStringLiteral("</ul>");
    }
    return html;
}

void MainWindow::setupDocumentAgent()
{
    // 工作台只承载“选材料 -> 选交付 -> 查看状态”。Designer 的 documentPageFill 吸收页面
    // 多余高度；这里仍让卡片内部保持顶部对齐，避免说明和状态标签被拉散成大块空白。
    ui->documentWorkspaceLayout->setAlignment(Qt::AlignTop);
    ui->documentDocumentCombo->addItem(QStringLiteral("等待后端加载文档"), QString());
    ui->documentDocumentCombo->setEnabled(false);
    ui->documentRunButton->setEnabled(false);
    ui->documentCreatePresentationButton->setEnabled(false);
    ui->documentProjectReviewButton->setEnabled(false);
    ui->documentPaperReviewButton->setEnabled(false);
    // 旧的通用输出协议仍用于历史任务和内部交付链路，但不再作为客户主界面的功能选择。
    ui->documentOutputModeCombo->setCurrentIndex(0);
    ui->documentRunStatus->setText(QStringLiteral("请选择一份文档"));
    // Designer 保留固定 18px 锚点，运行态由可复用指示器驱动，避免状态文本变化时挤动结果区。
    auto *documentActivityLayout = new QHBoxLayout(ui->documentActivityIndicatorHost);
    documentActivityLayout->setContentsMargins(0, 0, 0, 0);
    documentActivityIndicator = new TaskActivityIndicator(ui->documentActivityIndicatorHost);
    documentActivityLayout->addWidget(documentActivityIndicator);
    documentActivityIndicator->setRunning(false);
    ui->documentResultText->setUndoRedoEnabled(false);
    ui->documentResultDetailText->setUndoRedoEnabled(false);
    ui->documentOpenResultButton->setEnabled(false);
    ui->documentResultDetailSaveButton->setEnabled(false);
    ui->documentResultDetailPresentationButton->setEnabled(false);
    ui->documentResultDetailProjectReviewButton->setEnabled(false);
    ui->documentResultDetailReviewButton->setEnabled(false);
    ui->documentResultDetailCopyButton->setEnabled(false);
    updateDocumentResultDetailSections(QJsonObject{}, false);

    connect(ui->documentImportButton, &QPushButton::clicked, this, &MainWindow::importDocumentAgentDocument);
    connect(ui->documentRefreshButton, &QPushButton::clicked, this, &MainWindow::refreshDocumentAgentDocuments);
    // 文档页同时有分析与创作/审查两条真实模型作用域；菜单让客户按功能调整，而不是用一项
    // 全局设置偷偷覆盖所有能力。
    auto *documentModelRouteMenu = new QMenu(ui->documentModelRouteButton);
    QAction *documentAnalysisRouteAction = documentModelRouteMenu->addAction(
        QStringLiteral("文档分析与来源追溯模型"));
    documentAnalysisRouteAction->setToolTip(QStringLiteral("调整后续文档分析任务的模型路由。"));
    QAction *documentPresentationRouteAction = documentModelRouteMenu->addAction(
        QStringLiteral("PPT 制作与文档审查模型"));
    documentPresentationRouteAction->setToolTip(
        QStringLiteral("调整后续 PPT 制作、项目审查和论文审查任务的模型路由。"));
    ui->documentModelRouteButton->setMenu(documentModelRouteMenu);
    ui->documentModelRouteButton->setPopupMode(QToolButton::InstantPopup);
    connect(documentAnalysisRouteAction, &QAction::triggered, this, [this]() {
        openModelRouteDialogForRoute(QStringLiteral("document_analysis"));
    });
    connect(documentPresentationRouteAction, &QAction::triggered, this, [this]() {
        openModelRouteDialogForRoute(QStringLiteral("document_presentation"));
    });
    connect(ui->documentOpenResultButton,
            &QPushButton::clicked,
            this,
            &MainWindow::showLatestDocumentWorkbenchResult);
    connect(ui->documentOpenPresentationStudioButton,
            &QPushButton::clicked,
            this,
            &MainWindow::openPresentationStudio);
    connect(ui->documentCreatePresentationButton,
            &QPushButton::clicked,
            this,
            &MainWindow::beginDocumentPresentationDraft);
    connect(ui->documentProjectReviewButton,
            &QPushButton::clicked,
            this,
            &MainWindow::requestProjectDocumentReview);
    connect(ui->documentPaperReviewButton,
            &QPushButton::clicked,
            this,
            &MainWindow::requestPaperReview);
    connect(ui->documentResultDetailBackButton, &QPushButton::clicked, this, &MainWindow::showDocumentWorkbench);
    auto *sectionActionMenu = new QMenu(ui->documentResultDetailSectionDraftButton);
    QAction *reviewSectionAction = sectionActionMenu->addAction(QStringLiteral("审校本章"));
    reviewSectionAction->setToolTip(
        QStringLiteral("返回问题、候选建议和来源，不改写草稿或已保存文件"));
    documentSectionRevisionAction = sectionActionMenu->addAction(QStringLiteral("生成修订预览"));
    documentSectionRevisionAction->setToolTip(
        QStringLiteral("从已完成的本章审校中选择一条建议，生成前后差异与可另存的新版本"));
    documentSectionRevisionAction->setEnabled(false);
    documentSectionBatchRevisionAction = sectionActionMenu->addAction(QStringLiteral("生成多建议预览"));
    documentSectionBatchRevisionAction->setToolTip(
        QStringLiteral("选择同一章节中 2 至 6 条不重叠建议，生成安全合并后的独立版本"));
    documentSectionBatchRevisionAction->setEnabled(false);
    documentSectionManualRevisionAction = sectionActionMenu->addAction(QStringLiteral("手动修订本章"));
    documentSectionManualRevisionAction->setToolTip(
        QStringLiteral("在独立编辑窗口修改本章；会建立待来源核验的新版本，不会覆盖原稿或文件"));
    documentSectionManualRevisionAction->setEnabled(false);
    documentDraftTemplateAction = sectionActionMenu->addAction(QStringLiteral("模板与交付"));
    documentDraftTemplateAction->setToolTip(
        QStringLiteral("在独立工作区选择内置模板，重组已核验章节与来源；不会调用模型或写入文件"));
    documentDraftTemplateAction->setEnabled(false);
    documentDraftMergeAction = sectionActionMenu->addAction(QStringLiteral("合并其他版本"));
    documentDraftMergeAction->setToolTip(
        QStringLiteral("选择同一根草稿的已核验版本，先查看三方冲突再建立独立合并预览"));
    documentDraftMergeAction->setEnabled(false);
    sectionActionMenu->addSeparator();
    documentDraftRestoreAction = sectionActionMenu->addAction(QStringLiteral("从此版本恢复预览"));
    documentDraftRestoreAction->setToolTip(
        QStringLiteral("从当前已完成草稿建立新的独立预览，不调用模型、不覆盖旧任务或文件"));
    documentDraftRestoreAction->setEnabled(false);
    documentDraftParentDiffAction = sectionActionMenu->addAction(QStringLiteral("与父版本对比"));
    documentDraftParentDiffAction->setToolTip(
        QStringLiteral("在独立阅读窗口比较当前草稿与直接父版本，不修改任务或文件"));
    documentDraftParentDiffAction->setEnabled(false);
    ui->documentResultDetailSectionDraftButton->setMenu(sectionActionMenu);
    ui->documentResultDetailSectionDraftButton->setPopupMode(QToolButton::MenuButtonPopup);
    connect(reviewSectionAction, &QAction::triggered, this, &MainWindow::reviewDocumentDraftSection);
    connect(documentSectionRevisionAction,
            &QAction::triggered,
            this,
            &MainWindow::createDocumentDraftSectionRevisionPreview);
    connect(documentSectionBatchRevisionAction,
            &QAction::triggered,
            this,
            &MainWindow::createDocumentDraftSectionBatchRevisionPreview);
    connect(documentSectionManualRevisionAction,
            &QAction::triggered,
            this,
            &MainWindow::createDocumentDraftSectionManualRevisionPreview);
    connect(documentDraftTemplateAction,
            &QAction::triggered,
            this,
            &MainWindow::createDocumentDraftTemplatePreview);
    connect(documentDraftMergeAction,
            &QAction::triggered,
            this,
            &MainWindow::createDocumentDraftMergePreview);
    connect(documentDraftRestoreAction, &QAction::triggered, this, &MainWindow::restoreDocumentDraftPreview);
    connect(documentDraftParentDiffAction, &QAction::triggered, this, &MainWindow::showDocumentDraftParentDiff);
    connect(ui->documentResultDetailSectionDraftButton,
            &QToolButton::clicked,
            this,
            &MainWindow::createDocumentDraftSectionPreview);
    connect(ui->documentResultDetailReviewButton, &QPushButton::clicked, this, &MainWindow::reviewDocumentDraft);
    connect(ui->documentResultDetailCopyButton,
            &QPushButton::clicked,
            this,
            &MainWindow::copyDocumentDraftToClipboard);
    connect(ui->documentResultDetailSaveButton, &QPushButton::clicked, this, &MainWindow::saveDocumentDraft);
    connect(ui->documentResultDetailPresentationButton,
            &QPushButton::clicked,
            this,
            &MainWindow::requestDocumentPresentationPreview);
    auto *reviewMenu = new QMenu(ui->documentResultDetailProjectReviewButton);
    QAction *projectReviewAction = reviewMenu->addAction(QStringLiteral("项目文档审查"));
    projectReviewAction->setToolTip(
        QStringLiteral("检查范围、验收、责任、节点、风险依赖与术语口径"));
    documentPaperReviewAction = reviewMenu->addAction(QStringLiteral("论文审查"));
    documentPaperReviewAction->setToolTip(
        QStringLiteral("检查论文结构、引用对应、图表提及、标题格式和明显可读性问题"));
    ui->documentResultDetailProjectReviewButton->setMenu(reviewMenu);
    ui->documentResultDetailProjectReviewButton->setPopupMode(QToolButton::MenuButtonPopup);
    connect(ui->documentResultDetailProjectReviewButton,
            &QToolButton::clicked,
            this,
            &MainWindow::requestProjectDocumentReview);
    connect(projectReviewAction, &QAction::triggered, this, &MainWindow::requestProjectDocumentReview);
    connect(documentPaperReviewAction, &QAction::triggered, this, &MainWindow::requestPaperReview);
    connect(ui->documentResultDetailSectionCombo,
            qOverload<int>(&QComboBox::currentIndexChanged),
            this,
            [this](int index) {
                const QString anchor = ui->documentResultDetailSectionCombo->itemData(index).toString();
                if (anchor.isEmpty()) {
                    return;
                }

                ui->documentResultDetailText->scrollToAnchor(anchor);
            });
    connect(ui->documentRunButton, &QPushButton::clicked, this, &MainWindow::runDocumentAgent);
    connect(ui->documentInput, &QLineEdit::returnPressed, this, &MainWindow::runDocumentAgent);
    connect(ui->documentDocumentCombo,
            qOverload<int>(&QComboBox::currentIndexChanged),
            this,
            [this](int) { updateDocumentAgentSelectionUi(); });
    connect(ui->documentComparisonList,
            &QListWidget::itemSelectionChanged,
            this,
            &MainWindow::updateDocumentAgentSelectionUi);
    connect(ui->documentOutputModeCombo,
            qOverload<int>(&QComboBox::currentIndexChanged),
            this,
            [this](int) {
                updateDocumentAgentTaskHint();
                updateDocumentAgentSelectionUi();
            });
    updateDocumentAgentTaskHint();
    updateDocumentActivityState();
}

void MainWindow::updateDocumentActivityState()
{
    const bool running = documentWorkspaceLoading || documentAgentRunning || documentDraftSaving
        || documentPresentationPreviewLoading || documentPresentationExporting || projectDocumentReviewLoading
        || paperReviewLoading || documentDraftMergeLoading || documentDraftParentDiffLoading;
    if (documentActivityIndicator) {
        documentActivityIndicator->setRunning(running);
    }

    if (!workbenchActivityStateTimer) {
        return;
    }
    const bool dataRunning = dataWorkspaceLoading || dataProfileLoading || dataRecommendationLoading
        || dataAnalysisLoading || dataWorkbookExportLoading || dataChartExportLoading
        || dataTransformationPreviewLoading || dataTransformationExportLoading;
    if (running || dataRunning) {
        if (!workbenchActivityStateTimer->isActive()) {
            workbenchActivityStateTimer->start();
        }
    } else {
        workbenchActivityStateTimer->stop();
    }
}

void MainWindow::updateDataActivityState()
{
    const bool running = dataWorkspaceLoading || dataProfileLoading || dataRecommendationLoading
        || dataAnalysisLoading || dataWorkbookExportLoading || dataChartExportLoading
        || dataTransformationPreviewLoading || dataTransformationExportLoading;
    if (dataActivityIndicator) {
        dataActivityIndicator->setRunning(running);
    }

    if (!workbenchActivityStateTimer) {
        return;
    }
    const bool documentRunning = documentWorkspaceLoading || documentAgentRunning || documentDraftSaving
        || documentPresentationPreviewLoading || documentPresentationExporting || projectDocumentReviewLoading
        || paperReviewLoading || documentDraftMergeLoading || documentDraftParentDiffLoading;
    if (running || documentRunning) {
        if (!workbenchActivityStateTimer->isActive()) {
            workbenchActivityStateTimer->start();
        }
    } else {
        workbenchActivityStateTimer->stop();
    }
}

void MainWindow::refreshDocumentAgentDocuments()
{
    if (documentWorkspaceLoading || documentAgentRunning) {
        return;
    }

    if (!backendClient || !backendManager || !backendManager->isReady()) {
        documentWorkspaceLoaded = false;
        ui->documentDocumentCombo->clear();
        ui->documentDocumentCombo->addItem(QStringLiteral("后端准备中，稍后自动加载"), QString());
        ui->documentDocumentCombo->setEnabled(false);
        ui->documentSelectionState->setText(QStringLiteral("等待后端"));
        polishBadge(ui->documentSelectionState, QStringLiteral("badgeGray"));
        ui->documentWorkspaceHint->setText(
            QStringLiteral("本地后端尚未就绪；页面会在服务准备完成后自动读取已导入文档。"));
        ui->documentRefreshButton->setEnabled(true);
        return;
    }

    documentWorkspaceLoading = true;
    updateDocumentActivityState();
    ui->documentRefreshButton->setEnabled(false);
    ui->documentSelectionState->setText(QStringLiteral("加载中"));
    polishBadge(ui->documentSelectionState, QStringLiteral("badgeBlue"));
    backendClient->requestWorkspaceDocuments();
}

void MainWindow::showPdfProcessingWorkspace()
{
    if (pdfProcessingDialog) {
        pdfProcessingDialog->raise();
        pdfProcessingDialog->activateWindow();
        return;
    }

    // PDF 整理是一个有明确输入、确认和交付的集中任务。使用独立工作区而不是继续向文档
    // 分析主页面堆控件，能让用户在处理文件时始终看到范围、原件保护和产物状态。
    auto *dialog = new QDialog(this);
    dialog->setAttribute(Qt::WA_DeleteOnClose);
    dialog->setWindowTitle(QStringLiteral("PDF 整理 · 文档助手"));
    dialog->setMinimumSize(720, 560);
    dialog->resize(820, 650);

    auto *rootLayout = new QVBoxLayout(dialog);
    rootLayout->setContentsMargins(22, 20, 22, 20);
    rootLayout->setSpacing(14);

    auto *headerFrame = new QFrame(dialog);
    headerFrame->setObjectName(QStringLiteral("heroCard"));
    auto *headerLayout = new QVBoxLayout(headerFrame);
    headerLayout->setContentsMargins(18, 14, 18, 14);
    headerLayout->setSpacing(4);
    auto *title = new QLabel(QStringLiteral("PDF 整理"), headerFrame);
    title->setObjectName(QStringLiteral("sectionTitle"));
    auto *subtitle = new QLabel(
        QStringLiteral("合并、提取、旋转、删除页面。所有操作只生成新文件，不会修改原件。"),
        headerFrame);
    subtitle->setObjectName(QStringLiteral("subText"));
    subtitle->setWordWrap(true);
    headerLayout->addWidget(title);
    headerLayout->addWidget(subtitle);
    rootLayout->addWidget(headerFrame);

    auto *workspaceFrame = new QFrame(dialog);
    workspaceFrame->setObjectName(QStringLiteral("contentCard"));
    auto *workspaceLayout = new QVBoxLayout(workspaceFrame);
    workspaceLayout->setContentsMargins(18, 16, 18, 16);
    workspaceLayout->setSpacing(12);

    auto *toolbarLayout = new QHBoxLayout();
    auto *fileTitle = new QLabel(QStringLiteral("选择 PDF 文件"), workspaceFrame);
    fileTitle->setObjectName(QStringLiteral("sectionTitle"));
    auto *toolbarSpacer = new QSpacerItem(10, 1, QSizePolicy::Expanding, QSizePolicy::Minimum);
    auto *refreshButton = new QPushButton(QStringLiteral("刷新"), workspaceFrame);
    refreshButton->setObjectName(QStringLiteral("ghostButton"));
    refreshButton->setMinimumSize(76, 34);
    refreshButton->setToolTip(QStringLiteral("重新读取已导入的 PDF 文件列表"));
    auto *importButton = new QPushButton(QStringLiteral("导入 PDF"), workspaceFrame);
    importButton->setObjectName(QStringLiteral("ghostButton"));
    importButton->setMinimumSize(94, 34);
    importButton->setToolTip(QStringLiteral("导入一份 PDF 到受控 workspace，原始路径不会发送给后端"));
    toolbarLayout->addWidget(fileTitle);
    toolbarLayout->addItem(toolbarSpacer);
    toolbarLayout->addWidget(refreshButton);
    toolbarLayout->addWidget(importButton);
    workspaceLayout->addLayout(toolbarLayout);

    auto *fileHint = new QLabel(
        QStringLiteral("合并按勾选顺序处理；提取、旋转和删除一次只能选择一份 PDF。当前单文件限制 10 MB。"),
        workspaceFrame);
    fileHint->setObjectName(QStringLiteral("tinyText"));
    fileHint->setWordWrap(true);
    workspaceLayout->addWidget(fileHint);

    auto *documentList = new QListWidget(workspaceFrame);
    documentList->setMinimumHeight(154);
    documentList->setSelectionMode(QAbstractItemView::NoSelection);
    documentList->setToolTip(QStringLiteral("勾选本次允许处理的 PDF；文件始终保留在受控 workspace 内"));
    workspaceLayout->addWidget(documentList);

    auto *operationLayout = new QHBoxLayout();
    operationLayout->setSpacing(10);
    auto *operationLabel = new QLabel(QStringLiteral("处理方式"), workspaceFrame);
    operationLabel->setMinimumWidth(70);
    auto *operationCombo = new QComboBox(workspaceFrame);
    operationCombo->setMinimumHeight(38);
    operationCombo->addItem(QStringLiteral("合并 PDF"), QStringLiteral("merge"));
    operationCombo->addItem(QStringLiteral("提取页面"), QStringLiteral("extract"));
    operationCombo->addItem(QStringLiteral("旋转页面"), QStringLiteral("rotate"));
    operationCombo->addItem(QStringLiteral("删除页面"), QStringLiteral("delete"));
    operationCombo->setToolTip(QStringLiteral("选择本次要执行的确定性 PDF 操作"));
    operationLayout->addWidget(operationLabel);
    operationLayout->addWidget(operationCombo, 1);
    workspaceLayout->addLayout(operationLayout);

    auto *settingsLayout = new QHBoxLayout();
    settingsLayout->setSpacing(10);
    auto *pageRangeLabel = new QLabel(QStringLiteral("页码范围"), workspaceFrame);
    pageRangeLabel->setMinimumWidth(70);
    auto *pageRangeInput = new QLineEdit(workspaceFrame);
    pageRangeInput->setMinimumHeight(38);
    pageRangeInput->setPlaceholderText(QStringLiteral("提取、旋转或删除时填写，例如 1-3,5"));
    pageRangeInput->setToolTip(QStringLiteral("页码从 1 开始，可使用 1-3,5 这样的范围；不会改变原 PDF"));
    auto *rotationCombo = new QComboBox(workspaceFrame);
    rotationCombo->setMinimumWidth(118);
    rotationCombo->setMinimumHeight(38);
    rotationCombo->addItem(QStringLiteral("顺时针 90°"), 90);
    rotationCombo->addItem(QStringLiteral("顺时针 180°"), 180);
    rotationCombo->addItem(QStringLiteral("顺时针 270°"), 270);
    rotationCombo->setToolTip(QStringLiteral("只在“旋转页面”操作中生效"));
    settingsLayout->addWidget(pageRangeLabel);
    settingsLayout->addWidget(pageRangeInput, 1);
    settingsLayout->addWidget(rotationCombo);
    workspaceLayout->addLayout(settingsLayout);

    auto *scopeLabel = new QLabel(workspaceFrame);
    scopeLabel->setObjectName(QStringLiteral("tinyText"));
    scopeLabel->setWordWrap(true);
    workspaceLayout->addWidget(scopeLabel);

    auto *footerLayout = new QHBoxLayout();
    footerLayout->setSpacing(10);
    auto *statusLabel = new QLabel(QStringLiteral("请选择 PDF 和处理方式"), workspaceFrame);
    statusLabel->setObjectName(QStringLiteral("badgeGray"));
    statusLabel->setWordWrap(true);
    auto *footerSpacer = new QSpacerItem(10, 1, QSizePolicy::Expanding, QSizePolicy::Minimum);
    auto *openArtifactButton = new QPushButton(QStringLiteral("打开产物"), workspaceFrame);
    openArtifactButton->setObjectName(QStringLiteral("ghostButton"));
    openArtifactButton->setMinimumSize(92, 38);
    openArtifactButton->setEnabled(false);
    openArtifactButton->setToolTip(QStringLiteral("打开本次已验证的 PDF 新文件"));
    auto *runButton = new QPushButton(QStringLiteral("生成新 PDF"), workspaceFrame);
    runButton->setObjectName(QStringLiteral("primaryButton"));
    runButton->setMinimumSize(118, 38);
    runButton->setToolTip(QStringLiteral("确认后生成受控新文件；不会覆盖或修改原 PDF"));
    footerLayout->addWidget(statusLabel, 1);
    footerLayout->addItem(footerSpacer);
    footerLayout->addWidget(openArtifactButton);
    footerLayout->addWidget(runButton);
    workspaceLayout->addLayout(footerLayout);

    auto *resultLabel = new QLabel(
        QStringLiteral("完成后会在这里显示产物、页数验证和原件保护说明；完整审计记录在历史任务中查看。"),
        workspaceFrame);
    resultLabel->setObjectName(QStringLiteral("subText"));
    resultLabel->setWordWrap(true);
    workspaceLayout->addWidget(resultLabel);
    rootLayout->addWidget(workspaceFrame, 1);

    pdfProcessingDialog = dialog;
    pdfProcessingDocumentList = documentList;
    pdfProcessingOperationCombo = operationCombo;
    pdfProcessingPageRangeInput = pageRangeInput;
    pdfProcessingRotationCombo = rotationCombo;
    pdfProcessingScopeLabel = scopeLabel;
    pdfProcessingStatusLabel = statusLabel;
    pdfProcessingResultLabel = resultLabel;
    pdfProcessingRunButton = runButton;
    pdfProcessingRefreshButton = refreshButton;
    pdfProcessingImportButton = importButton;
    pdfProcessingOpenArtifactButton = openArtifactButton;

    connect(refreshButton, &QPushButton::clicked, this, &MainWindow::refreshPdfProcessingWorkspaceDocuments);
    connect(importButton, &QPushButton::clicked, this, [this]() {
        importWorkspaceDocumentForTarget(QStringLiteral("pdf_processing"));
    });
    connect(operationCombo,
            qOverload<int>(&QComboBox::currentIndexChanged),
            this,
            [this](int) { updatePdfProcessingWorkspaceUi(); });
    connect(documentList,
            &QListWidget::itemChanged,
            this,
            [this](QListWidgetItem *) { updatePdfProcessingWorkspaceUi(); });
    connect(pageRangeInput,
            &QLineEdit::textChanged,
            this,
            [this](const QString &) { updatePdfProcessingWorkspaceUi(); });
    connect(runButton, &QPushButton::clicked, this, &MainWindow::startPdfProcessingTask);
    connect(openArtifactButton, &QPushButton::clicked, this, &MainWindow::openPdfProcessingArtifact);
    connect(dialog, &QObject::destroyed, this, [this]() {
        // 关闭工作区不取消后台处理；任务的终态和 artifact 仍应通过统一历史入口可追溯。
        pdfProcessingDialog = nullptr;
        pdfProcessingDocumentList = nullptr;
        pdfProcessingOperationCombo = nullptr;
        pdfProcessingPageRangeInput = nullptr;
        pdfProcessingRotationCombo = nullptr;
        pdfProcessingScopeLabel = nullptr;
        pdfProcessingStatusLabel = nullptr;
        pdfProcessingResultLabel = nullptr;
        pdfProcessingRunButton = nullptr;
        pdfProcessingRefreshButton = nullptr;
        pdfProcessingImportButton = nullptr;
        pdfProcessingOpenArtifactButton = nullptr;
    });

    updatePdfProcessingWorkspaceUi();
    refreshPdfProcessingWorkspaceDocuments();
    dialog->show();
    dialog->raise();
    dialog->activateWindow();
}

void MainWindow::refreshPdfProcessingWorkspaceDocuments()
{
    if (pdfProcessingWorkspaceLoading || pdfProcessingRunning || !pdfProcessingDialog) {
        return;
    }
    pdfProcessingWorkspaceLoading = true;
    if (pdfProcessingRefreshButton) {
        pdfProcessingRefreshButton->setEnabled(false);
    }
    if (pdfProcessingStatusLabel) {
        pdfProcessingStatusLabel->setText(QStringLiteral("正在读取已导入的 PDF"));
        polishBadge(pdfProcessingStatusLabel, QStringLiteral("badgeBlue"));
    }
    backendClient->requestWorkspaceDocuments();
}

void MainWindow::updatePdfProcessingWorkspaceUi()
{
    if (!pdfProcessingDialog || !pdfProcessingDocumentList || !pdfProcessingOperationCombo
        || !pdfProcessingPageRangeInput || !pdfProcessingRotationCombo) {
        return;
    }

    const QString operation = pdfProcessingOperationCombo->currentData().toString();
    QStringList selectedReferences;
    for (int index = 0; index < pdfProcessingDocumentList->count(); ++index) {
        QListWidgetItem *item = pdfProcessingDocumentList->item(index);
        if (item && item->checkState() == Qt::Checked) {
            const QString reference = item->data(Qt::UserRole).toString();
            if (!reference.isEmpty()) {
                selectedReferences.append(reference);
            }
        }
    }

    const bool mergeOperation = operation == QStringLiteral("merge");
    const bool rotateOperation = operation == QStringLiteral("rotate");
    const bool needsPageRange = !mergeOperation;
    pdfProcessingPageRangeInput->setEnabled(needsPageRange && !pdfProcessingRunning);
    pdfProcessingRotationCombo->setEnabled(rotateOperation && !pdfProcessingRunning);
    pdfProcessingPageRangeInput->setPlaceholderText(
        mergeOperation ? QStringLiteral("合并按勾选顺序，不需要页码范围")
                       : QStringLiteral("例如 1-3,5"));

    const bool selectionValid = mergeOperation ? selectedReferences.size() >= 2
                                               : selectedReferences.size() == 1;
    const bool pageRangeValid = !needsPageRange || !pdfProcessingPageRangeInput->text().trimmed().isEmpty();
    const bool canRun = selectionValid && pageRangeValid && !pdfProcessingRunning && !pdfProcessingWorkspaceLoading;
    if (pdfProcessingRunButton) {
        pdfProcessingRunButton->setEnabled(canRun);
    }
    if (pdfProcessingDocumentList) {
        pdfProcessingDocumentList->setEnabled(!pdfProcessingRunning && !pdfProcessingWorkspaceLoading);
    }
    if (pdfProcessingOperationCombo) {
        pdfProcessingOperationCombo->setEnabled(!pdfProcessingRunning);
    }
    if (pdfProcessingImportButton) {
        pdfProcessingImportButton->setEnabled(!pdfProcessingRunning);
    }
    if (pdfProcessingRefreshButton) {
        pdfProcessingRefreshButton->setEnabled(!pdfProcessingRunning && !pdfProcessingWorkspaceLoading);
    }

    if (pdfProcessingScopeLabel) {
        pdfProcessingScopeLabel->setText(
            mergeOperation
                ? QStringLiteral("将按勾选顺序合并至少两份 PDF，输出为新的受控副本。")
                : rotateOperation
                      ? QStringLiteral("只旋转填写范围内的页面，未选页面与原 PDF 保持一致。")
                      : operation == QStringLiteral("extract")
                            ? QStringLiteral("将按填写范围提取页面并生成新 PDF；原文件不会被裁剪。")
                            : QStringLiteral("将从新副本中删除填写范围内的页面；原文件不会被修改。"));
    }
    if (pdfProcessingRunning || pdfProcessingWorkspaceLoading || !pdfProcessingStatusLabel) {
        return;
    }
    if (!selectionValid) {
        pdfProcessingStatusLabel->setText(
            mergeOperation ? QStringLiteral("合并请勾选至少两份 PDF") : QStringLiteral("此操作请勾选一份 PDF"));
        polishBadge(pdfProcessingStatusLabel, QStringLiteral("badgeGray"));
    } else if (!pageRangeValid) {
        pdfProcessingStatusLabel->setText(QStringLiteral("请填写页码范围，例如 1-3,5"));
        polishBadge(pdfProcessingStatusLabel, QStringLiteral("badgeGray"));
    } else {
        pdfProcessingStatusLabel->setText(QStringLiteral("范围已确认 · 将生成新的 PDF 副本"));
        polishBadge(pdfProcessingStatusLabel, QStringLiteral("badgeGreen"));
    }
}

void MainWindow::startPdfProcessingTask()
{
    if (pdfProcessingRunning || !pdfProcessingDialog || !pdfProcessingDocumentList || !pdfProcessingOperationCombo
        || !pdfProcessingPageRangeInput || !pdfProcessingRotationCombo) {
        return;
    }

    QStringList documentRefs;
    QStringList displayNames;
    for (int index = 0; index < pdfProcessingDocumentList->count(); ++index) {
        QListWidgetItem *item = pdfProcessingDocumentList->item(index);
        if (item && item->checkState() == Qt::Checked) {
            documentRefs.append(item->data(Qt::UserRole).toString());
            displayNames.append(item->text());
        }
    }
    documentRefs.removeAll(QString());
    const QString operation = pdfProcessingOperationCombo->currentData().toString();
    const bool mergeOperation = operation == QStringLiteral("merge");
    const bool needsPageRange = !mergeOperation;
    if ((mergeOperation && documentRefs.size() < 2) || (!mergeOperation && documentRefs.size() != 1)) {
        updatePdfProcessingWorkspaceUi();
        return;
    }
    const QString pageRange = pdfProcessingPageRangeInput->text().trimmed();
    if (needsPageRange && pageRange.isEmpty()) {
        updatePdfProcessingWorkspaceUi();
        pdfProcessingPageRangeInput->setFocus();
        return;
    }

    const QString operationText = pdfProcessingOperationCombo->currentText();
    const QString confirmationText = QStringLiteral(
        "将执行：%1\n文件：%2\n\n输出会保存到 output/document_processing/，系统将验证新 PDF 可打开且页数符合预期。\n原文件不会被修改、覆盖或删除。")
                                         .arg(operationText, displayNames.join(QStringLiteral("\n")));
    if (QMessageBox::question(
            pdfProcessingDialog,
            QStringLiteral("确认生成新 PDF"),
            confirmationText,
            QMessageBox::Yes | QMessageBox::No,
            QMessageBox::No)
        != QMessageBox::Yes) {
        return;
    }

    pdfProcessingRunning = true;
    activePdfProcessingTaskId.clear();
    currentPdfProcessingArtifact = WorkflowArtifactInfo{};
    if (pdfProcessingOpenArtifactButton) {
        pdfProcessingOpenArtifactButton->setEnabled(false);
    }
    if (pdfProcessingResultLabel) {
        pdfProcessingResultLabel->setText(QStringLiteral("任务受理后会显示文件处理与验证状态。"));
    }
    if (pdfProcessingStatusLabel) {
        pdfProcessingStatusLabel->setText(QStringLiteral("正在提交 PDF 整理任务"));
        polishBadge(pdfProcessingStatusLabel, QStringLiteral("badgeBlue"));
    }
    updatePdfProcessingWorkspaceUi();
    backendClient->startPdfProcessing(
        operation,
        documentRefs,
        pageRange,
        operation == QStringLiteral("rotate") ? pdfProcessingRotationCombo->currentData().toInt() : 0);
}

void MainWindow::handlePdfProcessingStarted(const PdfProcessingTaskStartResult &result)
{
    activePdfProcessingTaskId = result.taskId;
    if (pdfProcessingStatusLabel) {
        pdfProcessingStatusLabel->setText(QStringLiteral("PDF 整理已受理 · 正在连接实时进度"));
        polishBadge(pdfProcessingStatusLabel, QStringLiteral("badgeBlue"));
    }
    backendClient->connectTaskLog(result.taskId);
    // 小型 PDF 可能在 WebSocket 完成握手前已处理结束。一次延迟查询作为事件流兜底，
    // 不依赖虚假进度，也不会阻塞界面；运行中的任务会由既有轮询继续确认终态。
    QTimer::singleShot(450, this, [this, taskId = result.taskId]() {
        if (pdfProcessingRunning && activePdfProcessingTaskId == taskId) {
            backendClient->requestPdfProcessingResult(taskId);
        }
    });
}

void MainWindow::handlePdfProcessingCompleted(const PdfProcessingTaskResult &result)
{
    if (!activePdfProcessingTaskId.isEmpty() && result.taskId != activePdfProcessingTaskId) {
        return;
    }
    pdfProcessingRunning = false;
    activePdfProcessingTaskId.clear();
    const bool completed = result.status == QStringLiteral("completed") && result.hasArtifact;
    if (completed) {
        currentPdfProcessingArtifact = result.artifact;
    }
    if (pdfProcessingStatusLabel) {
        pdfProcessingStatusLabel->setText(completed ? QStringLiteral("处理完成 · 产物已验证")
                                                     : QStringLiteral("处理未完成"));
        polishBadge(pdfProcessingStatusLabel, completed ? QStringLiteral("badgeGreen")
                                                         : QStringLiteral("badgeOrange"));
    }
    if (pdfProcessingResultLabel) {
        if (completed) {
            const int pageCount = result.verification.value(QStringLiteral("actual_page_count")).toInt();
            const qint64 sizeBytes = result.verification.value(QStringLiteral("output_size_bytes")).toVariant().toLongLong();
            pdfProcessingResultLabel->setText(
                QStringLiteral("%1\n产物：%2 · %3 页 · %4 KB\n验证：文件可打开，页数符合预期，原文件未被修改。")
                    .arg(result.summary,
                         result.artifact.name,
                         QString::number(pageCount),
                         QString::number(qMax<qint64>(1, (sizeBytes + 1023) / 1024))));
        } else {
            pdfProcessingResultLabel->setText(
                result.message.isEmpty() ? QStringLiteral("PDF 整理未完成，请在历史任务中查看原因。")
                                         : result.message);
        }
    }
    if (pdfProcessingOpenArtifactButton) {
        pdfProcessingOpenArtifactButton->setEnabled(completed);
    }
    if (!documentAgentRunning) {
        ui->documentRunStatus->setText(completed ? QStringLiteral("PDF 整理完成 · 已写入任务历史")
                                                 : QStringLiteral("PDF 整理未完成 · 请查看任务记录"));
        polishBadge(ui->documentRunStatus, completed ? QStringLiteral("badgeGreen")
                                                      : QStringLiteral("badgeOrange"));
    }
    updatePdfProcessingWorkspaceUi();
}

void MainWindow::handlePdfProcessingStillRunning(const QString &taskId, const QString &status)
{
    if (!pdfProcessingRunning || taskId != activePdfProcessingTaskId) {
        return;
    }
    if (pdfProcessingStatusLabel) {
        pdfProcessingStatusLabel->setText(
            status == QStringLiteral("queued") || status == QStringLiteral("pending")
                ? QStringLiteral("PDF 整理任务排队中")
                : QStringLiteral("正在处理 PDF，随后会验证新文件"));
        polishBadge(pdfProcessingStatusLabel, QStringLiteral("badgeBlue"));
    }
    QTimer::singleShot(450, this, [this, taskId]() {
        if (pdfProcessingRunning && taskId == activePdfProcessingTaskId) {
            backendClient->requestPdfProcessingResult(taskId);
        }
    });
}

void MainWindow::handlePdfProcessingFailed(const QString &message)
{
    pdfProcessingRunning = false;
    activePdfProcessingTaskId.clear();
    if (pdfProcessingStatusLabel) {
        pdfProcessingStatusLabel->setText(QStringLiteral("PDF 整理失败"));
        polishBadge(pdfProcessingStatusLabel, QStringLiteral("badgeOrange"));
    }
    if (pdfProcessingResultLabel) {
        pdfProcessingResultLabel->setText(
            message.isEmpty() ? QStringLiteral("后端没有返回具体原因，请在历史任务中查看记录。") : message);
    }
    if (!documentAgentRunning) {
        ui->documentRunStatus->setText(QStringLiteral("PDF 整理失败 · 请查看说明"));
        polishBadge(ui->documentRunStatus, QStringLiteral("badgeOrange"));
    }
    updatePdfProcessingWorkspaceUi();
}

void MainWindow::openPdfProcessingArtifact()
{
    const QString outputPath = historyArtifactLocalPath(currentPdfProcessingArtifact);
    if (outputPath.isEmpty()) {
        QMessageBox::warning(
            pdfProcessingDialog,
            QStringLiteral("无法打开 PDF"),
            QStringLiteral("当前产物路径不可用。请前往历史任务查看该任务的受控产物记录。"));
        return;
    }
    if (!QDesktopServices::openUrl(QUrl::fromLocalFile(outputPath))) {
        QMessageBox::warning(
            pdfProcessingDialog,
            QStringLiteral("无法打开 PDF"),
            QStringLiteral("系统无法打开该 PDF，请在历史任务中复制受控产物路径。"));
    }
}

void MainWindow::updateDocumentAgentSelectionUi()
{
    const bool selected = !ui->documentDocumentCombo->currentData().toString().trimmed().isEmpty();
    const bool actionBusy = documentAgentRunning || projectDocumentReviewLoading || paperReviewLoading
        || documentPresentationPreviewLoading || documentPresentationExporting;

    // 文档工作台只公开一份主材料上的交付或审查任务。多文档通用模式仍可为旧任务回放，
    // 但不再占用客户的主界面，避免把普通聊天能力包装成一级产品功能。
    ui->documentRunButton->setEnabled(false);
    ui->documentCreatePresentationButton->setEnabled(selected && !actionBusy);
    if (selected) {
        ui->documentSelectionState->setText(QStringLiteral("已选择"));
        polishBadge(ui->documentSelectionState, QStringLiteral("badgeGreen"));
        if (!actionBusy) {
            ui->documentRunStatus->setText(
                QStringLiteral("材料已就绪 · 请选择项目方案 PPT、项目审查或论文审查"));
            polishBadge(ui->documentRunStatus, QStringLiteral("badgeGray"));
        }
    } else {
        ui->documentSelectionState->setText(QStringLiteral("未选择"));
        polishBadge(ui->documentSelectionState, QStringLiteral("badgeGray"));
        if (!actionBusy) {
            ui->documentRunStatus->setText(QStringLiteral("请先选择一份要处理的材料"));
            polishBadge(ui->documentRunStatus, QStringLiteral("badgeGray"));
        }
    }
    updateProjectReviewAction();
    updateDocumentOpenResultAction();
}

QStringList MainWindow::selectedDocumentAgentReferences() const
{
    if (!documentOutputUsesMultipleMaterials()) {
        const QString documentRef = ui->documentDocumentCombo->currentData().toString();
        return documentRef.isEmpty() ? QStringList() : QStringList{documentRef};
    }

    QStringList selectedReferences;
    for (QListWidgetItem *item : ui->documentComparisonList->selectedItems()) {
        const QString documentRef = item->data(Qt::UserRole).toString();
        if (!documentRef.isEmpty()) {
            selectedReferences.append(documentRef);
        }
    }
    selectedReferences.removeDuplicates();
    return selectedReferences;
}

void MainWindow::openPresentationStudio()
{
    openPresentationStudioForPrompt({});
}

void MainWindow::openPresentationStudioForPrompt(const QString &prompt, bool directGenerate)
{
    // V2 从一句需求起步，不依赖当前材料选择。独立对话框承载较长的逐页计划，主工作台
    // 因而仍能专注于“已有材料的交付与审查”，也不会在窄窗口里挤压可阅读区域。
    if (directGenerate && dispatchPresentationDialog) {
        dispatchPresentationDialog->close();
        dispatchPresentationDialog->deleteLater();
        dispatchPresentationDialog.clear();
    }
    auto *dialog = new PresentationStudioDialog(backendClient, this);
    // 直出模式允许用户关闭窗口后继续后台制作；保留对象才能把完成/失败回执送回调度台。
    dialog->setAttribute(Qt::WA_DeleteOnClose, !directGenerate);
    dialog->setInitialGoal(prompt);
    connect(dialog,
            &PresentationStudioDialog::openTaskHistoryRequested,
            this,
            [this, dialog](const QString &taskId) {
                dialog->accept();
                openTaskInHistory(taskId);
            });
    if (directGenerate) {
        dispatchPresentationDialog = dialog;
        connect(dialog,
                &PresentationStudioDialog::directGenerationProgress,
                this,
                [this](const QString &message) {
                    if (!currentDispatchPresentationRunning) {
                        return;
                    }
                    const QString status = message.trimmed().isEmpty()
                        ? QStringLiteral("正在制作 PPT")
                        : message.trimmed().left(72);
                    ui->dispatchChatStatus->setText(status);
                    ui->summaryVal3->setText(QStringLiteral("正在制作 PPT"));
                    setDispatchActivityRunning(true);
                    setProgressStep(3,
                                    QStringLiteral("3 智能制作 PPT · %1").arg(status),
                                    QStringLiteral("badgeBlue"));
                });
        connect(dialog,
                &PresentationStudioDialog::directGenerationCompleted,
                this,
                [this](const PresentationExportResult &result) {
                    if (!currentDispatchPresentationRunning) {
                        return;
                    }
                    currentDispatchPresentationRunning = false;
                    currentDispatchPresentationCompleted = true;
                    ui->dispatchChatStatus->setText(QStringLiteral("PPT 已生成"));
                    ui->summaryVal3->setText(QStringLiteral("PPT 已生成并通过验证"));
                    setProgressStep(3, QStringLiteral("3 智能制作 PPT · 已完成"), QStringLiteral("badgeGreen"));
                    setProgressStep(4, QStringLiteral("4 输出校验 · 已通过"), QStringLiteral("badgeGreen"));
                    setProgressStep(5, QStringLiteral("5 当前结论 · PPT 已交付"), QStringLiteral("badgeGreen"));
                    setDispatchActivityRunning(false);
                    const QString deliveryMessage = result.message.trimmed().isEmpty()
                        ? QStringLiteral("PPT 已完成并保存到受控输出目录。")
                        : result.message.trimmed();
                    const QString deliveryLocation = result.filename.trimmed().isEmpty()
                        ? result.relativePath.trimmed()
                        : result.filename.trimmed();
                    appendConversationHtml(formatDispatchAssistantMessageHtml(
                        QStringLiteral("## PPT 已生成\n\n%1\n\n"
                                       "文件：`%3`\n\n"
                                       "> 已生成 %2 页可编辑 PPTX，并完成文件回读验证。创作窗口中保留完整计划，"
                                       "任务历史中保留交付记录。")
                            .arg(deliveryMessage.toHtmlEscaped(),
                                 QString::number(result.slideCount),
                                 deliveryLocation.toHtmlEscaped())));
                    updateDispatchActionButtons();
                });
        connect(dialog,
                &PresentationStudioDialog::directGenerationFailed,
                this,
                [this](const QString &message) {
                    if (!currentDispatchPresentationRunning) {
                        return;
                    }
                    currentDispatchPresentationRunning = false;
                    currentDispatchPresentationCompleted = false;
                    ui->dispatchChatStatus->setText(QStringLiteral("PPT 制作未完成"));
                    ui->summaryVal3->setText(QStringLiteral("请查看创作窗口后重试"));
                    setProgressStep(5,
                                    QStringLiteral("5 当前结论 · PPT 制作未完成"),
                                    QStringLiteral("badgeOrange"));
                    setDispatchActivityRunning(false);
                    appendConversationHtml(
                        QStringLiteral("<hr/><h3>AI 调度台</h3>"
                                       "<p style=\"color:#B45309;\"><b>PPT 制作没有完成。</b></p>"
                                       "<p>原有文件没有被修改。请查看创作窗口中的具体原因，修正主题后重新发送。</p>"
                                       "<p style=\"color:#64748B;\">%1</p>")
                            .arg(message.toHtmlEscaped()));
                    updateDispatchActionButtons();
                });
    }
    dialog->open();
    if (directGenerate) {
        dialog->startDirectGeneration(prompt);
    }
}

void MainWindow::beginDocumentPresentationDraft()
{
    const QString documentRef = ui->documentDocumentCombo->currentData().toString().trimmed();
    if (documentRef.isEmpty()) {
        ui->documentRunStatus->setText(QStringLiteral("请先选择用于制作项目方案 PPT 的材料"));
        polishBadge(ui->documentRunStatus, QStringLiteral("badgeOrange"));
        ui->documentDocumentCombo->setFocus();
        return;
    }

    // PPT 渲染器只接受带来源的草稿。这里把草稿生成作为“制作 PPT”的受控前置步骤，
    // 不再将其暴露成一个难以理解的通用下拉选项。
    ui->documentOutputModeCombo->setCurrentIndex(5);
    ui->documentInput->setText(
        QStringLiteral("根据当前材料生成可核验的项目方案草稿，用于制作项目方案 PPT。"
                       "保留已有目标、范围、方案、计划、风险与来源，不要补写材料没有说明的事实。"));
    documentPresentationDraftRequested = true;
    runDocumentAgent();
}

QString MainWindow::selectedDocumentForReview(const QString &dialogTitle, const QString &prompt)
{
    const QString currentDocument = ui->documentDocumentCombo->currentData().toString().trimmed();
    if (!currentDocument.isEmpty()) {
        return currentDocument;
    }

    QStringList resultDocuments;
    for (const QJsonValue &value : currentDocumentResultContext.value(QStringLiteral("documents")).toArray()) {
        const QString documentRef = value.toString().trimmed();
        if (!documentRef.isEmpty() && !resultDocuments.contains(documentRef)) {
            resultDocuments.append(documentRef);
        }
    }
    if (resultDocuments.size() == 1) {
        return resultDocuments.first();
    }
    if (resultDocuments.size() > 1) {
        bool confirmed = false;
        const QString selected = QInputDialog::getItem(
            this, dialogTitle, prompt, resultDocuments, 0, false, &confirmed);
        return confirmed ? selected.trimmed() : QString();
    }
    return QString();
}

void MainWindow::runDocumentAgent()
{
    if (documentAgentRunning || documentDraftMergeLoading || documentPresentationPreviewLoading
        || documentPresentationExporting) {
        if (documentPresentationPreviewLoading || documentPresentationExporting) {
            ui->documentRunStatus->setText(QStringLiteral("项目方案 PPT 正在准备或导出，请等待当前交付流程结束"));
            polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
        }
        return;
    }
    if (documentDraftSaving) {
        ui->documentRunStatus->setText(QStringLiteral("草稿正在保存，请等待当前写入完成"));
        polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
        return;
    }

    const QStringList documentRefs = selectedDocumentAgentReferences();
    const QString outputMode = documentOutputModeValue();
    const bool comparisonMode = outputMode == QStringLiteral("comparison");
    const bool synthesisMode = outputMode == QStringLiteral("synthesis");
    const bool multiDocumentMode = documentOutputUsesMultipleMaterials();
    if (documentRefs.isEmpty() || (multiDocumentMode && documentRefs.size() < 2)) {
        ui->documentRunStatus->setText(
            comparisonMode ? QStringLiteral("多文档对比请勾选至少两份材料")
            : synthesisMode ? QStringLiteral("跨文档整合请勾选至少两份材料")
            : multiDocumentMode ? QStringLiteral("跨文档问答请勾选至少两份材料")
                                : QStringLiteral("请先选择要分析的文档"));
        polishBadge(ui->documentRunStatus, QStringLiteral("badgeOrange"));
        (multiDocumentMode ? static_cast<QWidget *>(ui->documentComparisonList)
                        : static_cast<QWidget *>(ui->documentDocumentCombo))->setFocus();
        return;
    }
    if (multiDocumentMode && documentRefs.size() > DocumentComparisonMaxDocuments) {
        ui->documentRunStatus->setText(
            QStringLiteral("一次跨文档任务最多选择 %1 份材料，请取消多余选择后重试")
                .arg(DocumentComparisonMaxDocuments));
        polishBadge(ui->documentRunStatus, QStringLiteral("badgeOrange"));
        ui->documentComparisonList->setFocus();
        return;
    }

    const QString taskGoal = ui->documentInput->text().trimmed();
    if (taskGoal.isEmpty()) {
        ui->documentRunStatus->setText(QStringLiteral("请说明希望从文档中获得什么结论"));
        polishBadge(ui->documentRunStatus, QStringLiteral("badgeOrange"));
        ui->documentInput->setFocus();
        return;
    }

    documentAgentRunning = true;
    documentSectionDraftRunning = false;
    documentSectionReviewRunning = false;
    documentSectionRevisionRunning = false;
    documentManualRevisionRunning = false;
    documentDraftTemplateRunning = false;
    documentDraftMergeRunning = false;
    documentDraftReviewRunning = false;
    documentDraftRestoreRunning = false;
    activeDocumentAgentTaskId.clear();
    currentDocumentResultTaskId.clear();
    currentDocumentResultContext = QJsonObject{};
    documentDraftSaving = false;
    documentDraftSaved = false;
    documentPresentationPreviewLoading = false;
    documentPresentationExporting = false;
    projectDocumentReviewLoading = false;
    paperReviewLoading = false;
    documentPresentationTaskId.clear();
    documentPresentationPlanId.clear();
    activeProjectDocumentReviewTaskId.clear();
    activePaperReviewTaskId.clear();
    if (documentPresentationDialog) {
        documentPresentationDialog->close();
    }
    if (projectDocumentReviewDialog) {
        projectDocumentReviewDialog->close();
    }
    if (paperReviewDialog) {
        paperReviewDialog->close();
    }
    lastSavedDocumentDraftFilename.clear();
    updateDocumentDraftSaveAction();
    ui->documentRunButton->setEnabled(false);
    ui->documentInput->setEnabled(false);
    ui->documentOutputModeCombo->setEnabled(false);
    ui->documentDocumentCombo->setEnabled(false);
    ui->documentComparisonList->setEnabled(false);
    ui->documentImportButton->setEnabled(false);
    ui->documentRefreshButton->setEnabled(false);
    ui->documentCreatePresentationButton->setEnabled(false);
    ui->documentProjectReviewButton->setEnabled(false);
    ui->documentPaperReviewButton->setEnabled(false);
    ui->documentRunStatus->setText(QStringLiteral("正在提交分析任务"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    setDocumentResultHtml(
        QStringLiteral("<div style=\"padding:8px 4px;color:#475569;line-height:1.6;\">"
                       "<a name=\"conclusion\"></a><p style=\"margin:0 0 6px 0;color:#0F172A;font-weight:700;\">正在建立分析任务</p>"
                       "<p style=\"margin:0;\">任务受理后会显示实际的材料确认、读取、来源校验和完成状态。</p>"
                       "</div>"),
        QStringLiteral("运行中 · 等待可验证结果"),
        false);
    updateDocumentResultDetailSections(QJsonObject{}, false);

    backendClient->runDocumentAgent(taskGoal, documentRefs, documentOutputModeValue());
}

void MainWindow::handleWorkspaceDocumentsReceived(const WorkspaceDocumentListResult &result)
{
    documentWorkspaceLoading = false;
    documentWorkspaceLoaded = true;
    currentWorkspaceDocuments = result.documents;
    if (dispatchMaterialDialog && dispatchMaterialDialog->isVisible()) {
        dispatchMaterialDocumentsPending = false;
        dispatchMaterialDialog->setDocuments(currentWorkspaceDocuments);
        updateDispatchMaterialCatalogStatus();
    }
    ui->documentRefreshButton->setEnabled(!documentAgentRunning);
    ui->documentWorkspaceHint->setText(
        QStringLiteral("可选择材料制作或审查；“智能制作 PPT”可不选材料，从一句需求开始。支持 1MB UTF-8 文本及 10MB PDF/DOCX。"));
    const QString currentSelection = pendingDocumentSelection.isEmpty()
                                         ? ui->documentDocumentCombo->currentData().toString()
                                         : pendingDocumentSelection;
    QStringList previouslyCompared;
    for (QListWidgetItem *item : ui->documentComparisonList->selectedItems()) {
        previouslyCompared.append(item->data(Qt::UserRole).toString());
    }
    const QSignalBlocker comboBlocker(ui->documentDocumentCombo);
    const QSignalBlocker listBlocker(ui->documentComparisonList);
    ui->documentDocumentCombo->clear();
    ui->documentComparisonList->clear();
    for (const WorkspaceDocumentInfo &document : result.documents) {
        const QString documentRef = document.relativePath.isEmpty() ? document.name : document.relativePath;
        const QString typeText = document.documentType == QStringLiteral("pdf") ? QStringLiteral("PDF")
                                 : document.documentType == QStringLiteral("docx") ? QStringLiteral("DOCX")
                                                                                : QStringLiteral("文本");
        const QString displayName = QStringLiteral("%1  ·  %2  ·  %3 KB")
                                        .arg(document.name)
                                        .arg(typeText)
                                        .arg(qMax(1, (document.sizeBytes + 1023) / 1024));
        ui->documentDocumentCombo->addItem(displayName, documentRef);
        auto *comparisonItem = new QListWidgetItem(displayName, ui->documentComparisonList);
        comparisonItem->setData(Qt::UserRole, documentRef);
        comparisonItem->setSelected(previouslyCompared.contains(documentRef));
    }

    if (pdfProcessingDocumentList) {
        QStringList previouslyChecked;
        for (int index = 0; index < pdfProcessingDocumentList->count(); ++index) {
            const QListWidgetItem *item = pdfProcessingDocumentList->item(index);
            if (item && item->checkState() == Qt::Checked) {
                previouslyChecked.append(item->data(Qt::UserRole).toString());
            }
        }

        {
            // 阻断回填过程的 itemChanged，避免每新增一行就触发一次完整状态计算。
            const QSignalBlocker pdfListBlocker(pdfProcessingDocumentList);
            pdfProcessingDocumentList->clear();
            for (const WorkspaceDocumentInfo &document : result.documents) {
                if (document.documentType != QStringLiteral("pdf")) {
                    continue;
                }
                const QString documentRef = document.relativePath.isEmpty() ? document.name : document.relativePath;
                auto *item = new QListWidgetItem(
                    QStringLiteral("%1  ·  %2 KB").arg(document.name).arg(qMax(1, (document.sizeBytes + 1023) / 1024)),
                    pdfProcessingDocumentList);
                item->setData(Qt::UserRole, documentRef);
                item->setFlags(Qt::ItemIsEnabled | Qt::ItemIsUserCheckable);
                item->setCheckState(previouslyChecked.contains(documentRef) ? Qt::Checked : Qt::Unchecked);
            }
            if (pdfProcessingDocumentList->count() == 0) {
                auto *emptyItem = new QListWidgetItem(QStringLiteral("暂无已导入 PDF · 可点击右上角“导入 PDF”"),
                                                      pdfProcessingDocumentList);
                emptyItem->setFlags(Qt::NoItemFlags);
            }
        }
        pdfProcessingWorkspaceLoading = false;
        updatePdfProcessingWorkspaceUi();
    }

    if (result.documents.isEmpty()) {
        ui->documentDocumentCombo->addItem(QStringLiteral("暂无已导入文档"), QString());
        ui->documentDocumentCombo->setEnabled(false);
        ui->documentComparisonList->setEnabled(false);
        pendingDocumentSelection.clear();
        updateDocumentAgentSelectionUi();
        ui->documentRunStatus->setText(QStringLiteral("请先导入一份文档"));
        return;
    }

    int selectedIndex = 0;
    for (int index = 0; index < ui->documentDocumentCombo->count(); ++index) {
        if (ui->documentDocumentCombo->itemData(index).toString() == currentSelection) {
            selectedIndex = index;
            break;
        }
    }
    ui->documentDocumentCombo->setCurrentIndex(selectedIndex);
    ui->documentDocumentCombo->setEnabled(!documentAgentRunning);
    ui->documentComparisonList->setEnabled(!documentAgentRunning);
    pendingDocumentSelection.clear();
    updateDocumentAgentSelectionUi();
}

void MainWindow::handleWorkspaceDocumentsFailed(const QString &message)
{
    documentWorkspaceLoading = false;
    documentWorkspaceLoaded = false;
    if (dispatchMaterialDialog && dispatchMaterialDialog->isVisible()) {
        dispatchMaterialDocumentsPending = false;
        if (dispatchMaterialCatalogError.isEmpty()) {
            dispatchMaterialCatalogError = QStringLiteral("文档：%1").arg(message);
        }
        updateDispatchMaterialCatalogStatus();
    }
    ui->documentRefreshButton->setEnabled(!documentAgentRunning);
    ui->documentDocumentCombo->clear();
    ui->documentDocumentCombo->addItem(QStringLiteral("文档列表加载失败 · 点击刷新重试"), QString());
    ui->documentDocumentCombo->setEnabled(false);
    ui->documentComparisonList->clear();
    ui->documentComparisonList->setEnabled(false);
    ui->documentSelectionState->setText(QStringLiteral("需要重试"));
    polishBadge(ui->documentSelectionState, QStringLiteral("badgeOrange"));
    ui->documentRunStatus->setText(QStringLiteral("文档列表加载失败"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeOrange"));
    ui->documentWorkspaceHint->setText(
        QStringLiteral("无法读取 workspace 文档列表：%1").arg(message.left(120)));
    if (pdfProcessingDialog) {
        pdfProcessingWorkspaceLoading = false;
        if (pdfProcessingStatusLabel) {
            pdfProcessingStatusLabel->setText(QStringLiteral("PDF 列表加载失败"));
            polishBadge(pdfProcessingStatusLabel, QStringLiteral("badgeOrange"));
        }
        if (pdfProcessingResultLabel) {
            pdfProcessingResultLabel->setText(
                QStringLiteral("无法读取已导入的 PDF：%1").arg(message.left(160)));
        }
        updatePdfProcessingWorkspaceUi();
    }
}

QString MainWindow::documentOutputModeValue() const
{
    switch (ui->documentOutputModeCombo->currentIndex()) {
    case 1:
        return QStringLiteral("requirements");
    case 2:
        return QStringLiteral("summary");
    case 3:
        return QStringLiteral("brief");
    case 4:
        return QStringLiteral("outline");
    case 5:
        return QStringLiteral("draft");
    case 6:
        return QStringLiteral("qa");
    case 7:
        return QStringLiteral("cross_qa");
    case 8:
        return QStringLiteral("synthesis");
    case 9:
        return QStringLiteral("comparison");
    default:
        return QStringLiteral("auto");
    }
}

bool MainWindow::documentOutputUsesMultipleMaterials() const
{
    const QString outputMode = documentOutputModeValue();
    return outputMode == QStringLiteral("comparison") || outputMode == QStringLiteral("cross_qa")
           || outputMode == QStringLiteral("synthesis");
}

void MainWindow::updateDocumentAgentTaskHint()
{
    // “常用任务”只帮助用户快速表达意图，不会限制上方输入框的自由问题。
    const QString outputMode = documentOutputModeValue();
    const bool comparisonMode = outputMode == QStringLiteral("comparison");
    const bool synthesisMode = outputMode == QStringLiteral("synthesis");
    const bool multiDocumentMode = documentOutputUsesMultipleMaterials();
    ui->documentComparisonSelectionFrame->setVisible(multiDocumentMode);
    ui->documentDocumentCombo->setVisible(!multiDocumentMode);
    ui->documentSelectionLabel->setText(multiDocumentMode ? QStringLiteral("已选材料") : QStringLiteral("分析材料"));
    ui->documentComparisonSelectionLabel->setText(
        comparisonMode ? QStringLiteral("对比材料")
        : synthesisMode ? QStringLiteral("整合材料") : QStringLiteral("问答材料"));
    ui->documentComparisonHint->setText(
        comparisonMode
            ? QStringLiteral("选择 2 至 4 份材料。系统会逐份连续读取；超出单页范围时会继续分页并如实提示来源覆盖范围。")
            : synthesisMode
                  ? QStringLiteral("选择 2 至 4 份材料。系统会逐份连续读取，归并可兼容内容；冲突只会列为待确认事项。")
                  : QStringLiteral("选择 2 至 4 份材料。系统会逐份连续读取，并只基于多份已读取材料回答你的问题。"));
    switch (ui->documentOutputModeCombo->currentIndex()) {
    case 1:
        ui->documentInput->setPlaceholderText(
            QStringLiteral("例如：提取功能、约束、验收标准与待确认事项"));
        break;
    case 2:
        ui->documentInput->setPlaceholderText(
            QStringLiteral("例如：用 5 个要点概括项目定位、范围与下一步"));
        break;
    case 3:
        ui->documentInput->setPlaceholderText(
            QStringLiteral("例如：按关键信息卡提取项目主题、范围、交付物、节点和风险"));
        break;
    case 4:
        ui->documentInput->setPlaceholderText(
            QStringLiteral("例如：根据材料生成待审阅的项目方案大纲，并标明每章依据"));
        break;
    case 5:
        ui->documentInput->setPlaceholderText(
            QStringLiteral("例如：根据材料起草一份项目说明，先给我可审阅的 Markdown 草稿"));
        break;
    case 6:
        ui->documentInput->setPlaceholderText(
            QStringLiteral("例如：这个项目当前要先完成什么？请附上原文依据"));
        break;
    case 7:
        ui->documentInput->setPlaceholderText(
            QStringLiteral("例如：两份方案对权限确认的共同规定和差异是什么？请附上来源"));
        break;
    case 8:
        ui->documentInput->setPlaceholderText(
            QStringLiteral("例如：整合两份需求材料，合并重复条目，并列出冲突与待确认项"));
        break;
    case 9:
        ui->documentInput->setPlaceholderText(
            QStringLiteral("例如：比较两份方案的共同要求、关键差异和待确认项"));
        break;
    default:
        ui->documentInput->setPlaceholderText(
            QStringLiteral("例如：梳理项目目标、功能、技术约束与待确认事项"));
        break;
    }
}

QString MainWindow::formatDocumentAgentResultHtml(const DocumentAgentRunResult &result) const
{
    const QJsonObject context = result.documentContext;
    const auto richText = [](const QString &text) {
        QString escaped = text.toHtmlEscaped();
        escaped.replace(QStringLiteral("\r\n"), QStringLiteral("\n"));
        escaped.replace(QStringLiteral("\n"), QStringLiteral("<br/>"));
        return escaped;
    };
    const auto sectionTitle = [](const QString &title, const QString &subtitle = QString(), const QString &anchor = QString()) {
        QString value;
        if (!anchor.isEmpty()) {
            value += QStringLiteral("<a name=\"%1\"></a>").arg(anchor.toHtmlEscaped());
        }
        value += QStringLiteral("<div style=\"margin:18px 0 8px 0;color:#0F172A;font-weight:700;font-size:15px;\">%1")
                            .arg(title.toHtmlEscaped());
        if (!subtitle.isEmpty()) {
            value += QStringLiteral("<span style=\"margin-left:7px;color:#94A3B8;font-weight:400;font-size:12px;\">%1</span>")
                         .arg(subtitle.toHtmlEscaped());
        }
        return value + QStringLiteral("</div>");
    };

    QString html = QStringLiteral("<div style=\"padding:2px 4px 14px 4px;line-height:1.65;color:#1E293B;\"><a name=\"conclusion\"></a>");
    html += QStringLiteral(
                "<div style=\"padding:13px 14px;background:#F0F7FF;border:1px solid #D8E8FF;border-radius:8px;\">"
                "<div style=\"margin:0 0 6px 0;color:#1D4ED8;font-weight:700;font-size:15px;\">本次结论</div>"
                "<div style=\"color:#1E293B;\">%1</div></div>")
                .arg(richText(result.reply));

    const QString summary = context.value(QStringLiteral("summary")).toString();
    if (!summary.isEmpty()) {
        html += sectionTitle(QStringLiteral("内容摘要"), QString(), QStringLiteral("summary"));
        html += QStringLiteral("<div style=\"padding:11px 13px;background:#F8FAFC;border-left:3px solid #93C5FD;color:#334155;\">%1</div>")
                    .arg(richText(summary));
    }

    const QJsonObject draftVersion = context.value(QStringLiteral("draft_version")).toObject();
    if (!draftVersion.isEmpty()) {
        const QString versionKind = draftVersion.value(QStringLiteral("kind")).toString();
        const QString versionKindText = versionKind == QStringLiteral("base_draft")
            ? QStringLiteral("草稿初版")
            : versionKind == QStringLiteral("section_preview")
            ? QStringLiteral("章节预览")
            : versionKind == QStringLiteral("fact_review")
            ? QStringLiteral("事实核验")
            : versionKind == QStringLiteral("section_review")
            ? QStringLiteral("本章审校")
            : versionKind == QStringLiteral("revision_batch_preview")
            ? QStringLiteral("多建议修订")
            : versionKind == QStringLiteral("manual_revision_pending_review")
            ? QStringLiteral("手动修订待核验")
            : versionKind == QStringLiteral("restored_preview")
            ? QStringLiteral("历史恢复")
            : versionKind == QStringLiteral("template_preview")
            ? QStringLiteral("模板化交付")
            : versionKind == QStringLiteral("merge_preview")
            ? QStringLiteral("章节合并")
            : QStringLiteral("单建议修订");
        const QString versionLabel = draftVersion.value(QStringLiteral("label")).toString();
        const QString versionId = draftVersion.value(QStringLiteral("version_id")).toString();
        const QString rootTaskId = draftVersion.value(QStringLiteral("root_task_id")).toString();
        const QString parentTaskId = draftVersion.value(QStringLiteral("parent_task_id")).toString();
        const QString changeSummary = draftVersion.value(QStringLiteral("change_summary")).toString();
        html += sectionTitle(
            QStringLiteral("版本链"),
            QStringLiteral("回看旧快照或另存，不自动覆盖"),
            QStringLiteral("version"));
        html += QStringLiteral(
                    "<div style=\"padding:12px 13px;background:#F8FAFC;border:1px solid #DCE7F5;border-radius:8px;\">"
                    "<div style=\"margin-bottom:8px;color:#1D4ED8;font-weight:700;\">%1 <span style=\"margin-left:7px;color:#64748B;font-weight:400;\">%2</span></div>"
                    "<div style=\"color:#334155;line-height:1.8;\">"
                    "当前任务：<code>%3</code><br/>"
                    "根草稿：<code>%4</code>%5%6"
                    "</div></div>")
                    .arg(versionLabel.toHtmlEscaped(),
                         versionKindText.toHtmlEscaped(),
                         versionId.toHtmlEscaped(),
                         rootTaskId.toHtmlEscaped(),
                         parentTaskId.isEmpty()
                             ? QString()
                             : QStringLiteral("<br/>直接父任务：<code>%1</code>")
                                   .arg(parentTaskId.toHtmlEscaped()),
                         changeSummary.isEmpty()
                             ? QString()
                             : QStringLiteral("<br/><span style=\"color:#64748B;\">%1</span>")
                                   .arg(richText(changeSummary)));
    }

    const QJsonObject templatePreview = context.value(QStringLiteral("template_preview")).toObject();
    if (!templatePreview.isEmpty()) {
        const QString templateName = templatePreview.value(QStringLiteral("template_name")).toString();
        const QJsonArray missingSections = templatePreview.value(QStringLiteral("missing_sections")).toArray();
        QStringList missingLabels;
        missingLabels.reserve(missingSections.size());
        for (const QJsonValue &value : missingSections) {
            const QString label = value.toString().trimmed();
            if (!label.isEmpty()) {
                missingLabels.append(label);
            }
        }
        html += sectionTitle(
            QStringLiteral("模板与交付"),
            QStringLiteral("固定结构 · 不补写未知事实"),
            QStringLiteral("template"));
        html += QStringLiteral(
                    "<div style=\"padding:12px 13px;border:1px solid %1;border-radius:8px;background:%2;\">"
                    "<div style=\"margin-bottom:6px;color:%3;font-weight:700;\">%4</div>"
                    "<div style=\"color:#334155;line-height:1.75;\">%5</div></div>")
                    .arg(
                        missingLabels.isEmpty() ? QStringLiteral("#BBE7D0") : QStringLiteral("#F3D7A6"),
                        missingLabels.isEmpty() ? QStringLiteral("#F0FDF4") : QStringLiteral("#FFF9ED"),
                        missingLabels.isEmpty() ? QStringLiteral("#166534") : QStringLiteral("#9A3412"),
                        templateName.toHtmlEscaped(),
                        missingLabels.isEmpty()
                            ? QStringLiteral("当前草稿的章节已全部匹配该模板结构。请继续核对正文与来源，再另存 Markdown 交付版本。")
                            : QStringLiteral("以下模板章节尚未在当前草稿中找到可保守归类的已验证内容：<b>%1</b>。"
                                             "系统没有自动补写，请补充材料或手动修订后再次核验。")
                                  .arg(missingLabels.join(QStringLiteral("、")).toHtmlEscaped()));
    }

    const QJsonObject mergePreview = context.value(QStringLiteral("merge_preview")).toObject();
    if (!mergePreview.isEmpty()) {
        const int automaticSectionCount = mergePreview.value(QStringLiteral("automatic_section_count")).toInt();
        const int resolvedConflictCount = mergePreview.value(QStringLiteral("resolved_conflict_count")).toInt();
        const QJsonArray conflicts = mergePreview.value(QStringLiteral("conflicts")).toArray();
        QStringList conflictLabels;
        conflictLabels.reserve(conflicts.size());
        for (const QJsonValue &value : conflicts) {
            const QJsonObject conflict = value.toObject();
            const QString heading = conflict.value(QStringLiteral("heading")).toString();
            conflictLabels.append(
                conflict.value(QStringLiteral("conflict_id")).toString() == QStringLiteral("title")
                    ? QStringLiteral("文档标题")
                    : heading.isEmpty() ? QStringLiteral("未命名章节") : heading);
        }
        html += sectionTitle(
            QStringLiteral("章节合并"),
            QStringLiteral("三方版本策略 · 原版本未改动"),
            QStringLiteral("merge"));
        html += QStringLiteral(
                    "<div style=\"padding:12px 13px;border:1px solid #C7DDFB;border-radius:8px;background:#F5F9FF;\">"
                    "<div style=\"margin-bottom:6px;color:#1D4ED8;font-weight:700;\">合并预览已建立</div>"
                    "<div style=\"color:#334155;line-height:1.75;\">"
                    "共同祖先：<code>%1</code><br/>"
                    "自动合并章节：%2 个；已确认冲突：%3 项。%4"
                    "</div></div>")
                    .arg(
                        mergePreview.value(QStringLiteral("common_ancestor_task_id")).toString().toHtmlEscaped(),
                        QString::number(automaticSectionCount),
                        QString::number(resolvedConflictCount),
                        conflictLabels.isEmpty()
                            ? QStringLiteral("<br/>两个版本没有双边冲突，结果完全由确定性规则生成。")
                            : QStringLiteral("<br/>已处理冲突：%1。")
                                  .arg(conflictLabels.join(QStringLiteral("、")).toHtmlEscaped()));
    }

    const QJsonObject revisionPreview = context.value(QStringLiteral("revision_preview")).toObject();
    if (!revisionPreview.isEmpty()) {
        const QString heading = revisionPreview.value(QStringLiteral("heading")).toString();
        const QString originalBody = revisionPreview.value(QStringLiteral("original_body")).toString();
        const QString revisedBody = revisionPreview.value(QStringLiteral("revised_body")).toString();
        html += sectionTitle(
            QStringLiteral("本章修订预览"),
            QStringLiteral("独立版本，原草稿未改动"),
            QStringLiteral("revision-preview"));
        const QJsonArray revisionSuggestionIds = revisionPreview
                                                  .value(QStringLiteral("suggestion_ids"))
                                                  .toArray();
        const int revisionCount = revisionSuggestionIds.isEmpty() ? 1 : revisionSuggestionIds.size();
        const QString revisionDescription = revisionCount > 1
            ? QStringLiteral("已将 %1 条不重叠的审校建议按原文位置安全合并，生成前后差异。")
                  .arg(revisionCount)
            : QStringLiteral("已针对“%1”生成精确替换后的前后差异。").arg(heading.toHtmlEscaped());
        html += QStringLiteral(
                    "<div style=\"margin:0 0 10px 0;padding:10px 12px;background:#EEF6FF;"
                    "border-left:3px solid #60A5FA;color:#1E40AF;\">"
                    "%1 此页不会覆盖原草稿或已有文件；"
                    "确认无误后可另存为新的 Markdown 版本。</div>")
                    .arg(revisionDescription);
        html += QStringLiteral(
                    "<table cellspacing=\"0\" cellpadding=\"0\" style=\"width:100%;border-collapse:separate;border-spacing:0 9px;\">"
                    "<tr><td style=\"width:50%;vertical-align:top;padding-right:5px;\">"
                    "<div style=\"padding:12px 13px;border:1px solid #F3D7A6;border-radius:7px;background:#FFF9ED;\">"
                    "<div style=\"margin-bottom:7px;color:#9A3412;font-weight:700;\">修改前</div>"
                    "<div style=\"color:#78350F;line-height:1.7;\">%1</div></div></td>"
                    "<td style=\"width:50%;vertical-align:top;padding-left:5px;\">"
                    "<div style=\"padding:12px 13px;border:1px solid #BBE7D0;border-radius:7px;background:#F0FDF4;\">"
                    "<div style=\"margin-bottom:7px;color:#166534;font-weight:700;\">修改后</div>"
                    "<div style=\"color:#166534;line-height:1.7;\">%2</div></div></td></tr></table>")
                    .arg(richText(originalBody), richText(revisedBody));
    }

    const QJsonObject manualRevisionPreview = context
                                                  .value(QStringLiteral("manual_revision_preview"))
                                                  .toObject();
    if (!manualRevisionPreview.isEmpty()) {
        const QString heading = manualRevisionPreview.value(QStringLiteral("heading")).toString();
        const QString originalBody = manualRevisionPreview.value(QStringLiteral("original_body")).toString();
        const QString revisedBody = manualRevisionPreview.value(QStringLiteral("revised_body")).toString();
        html += sectionTitle(
            QStringLiteral("手动修订预览"),
            QStringLiteral("待重新核验，暂不能保存"),
            QStringLiteral("manual-revision-preview"));
        html += QStringLiteral(
                    "<div style=\"margin:0 0 10px 0;padding:10px 12px;background:#FFF7ED;"
                    "border-left:3px solid #FB923C;color:#9A3412;\">"
                    "“%1”已由用户手动修改。这里保留的历史来源只用于回看，不能证明新正文已核验；"
                    "请点击“核验事实”重新读取材料，且没有待确认问题后才能保存。</div>")
                    .arg(heading.toHtmlEscaped());
        html += QStringLiteral(
                    "<table cellspacing=\"0\" cellpadding=\"0\" style=\"width:100%;border-collapse:separate;border-spacing:0 9px;\">"
                    "<tr><td style=\"width:50%;vertical-align:top;padding-right:5px;\">"
                    "<div style=\"padding:12px 13px;border:1px solid #F3D7A6;border-radius:7px;background:#FFF9ED;\">"
                    "<div style=\"margin-bottom:7px;color:#9A3412;font-weight:700;\">修改前</div>"
                    "<div style=\"color:#78350F;line-height:1.7;\">%1</div></div></td>"
                    "<td style=\"width:50%;vertical-align:top;padding-left:5px;\">"
                    "<div style=\"padding:12px 13px;border:1px solid #F7B267;border-radius:7px;background:#FFF7ED;\">"
                    "<div style=\"margin-bottom:7px;color:#9A3412;font-weight:700;\">手动修订后</div>"
                    "<div style=\"color:#7C2D12;line-height:1.7;\">%2</div></div></td></tr></table>")
                    .arg(richText(originalBody), richText(revisedBody));
    }

    const QJsonArray briefFields = context.value(QStringLiteral("brief_fields")).toArray();
    if (!briefFields.isEmpty()) {
        html += sectionTitle(QStringLiteral("关键信息卡"), QStringLiteral("仅保留材料明确表达的内容"), QStringLiteral("brief"));
        html += QStringLiteral("<table cellspacing=\"0\" cellpadding=\"0\" style=\"width:100%;border-collapse:separate;border-spacing:0 7px;\">");
        for (const QJsonValue &value : briefFields) {
            const QJsonObject field = value.toObject();
            const QString sources = documentSourceRefsText(
                field.value(QStringLiteral("source_refs")).toArray());
            html += QStringLiteral(
                        "<tr><td style=\"width:118px;vertical-align:top;padding:10px 11px;background:#EEF6FF;color:#1D4ED8;font-weight:700;border-radius:7px 0 0 7px;\">%1</td>"
                        "<td style=\"padding:10px 12px;background:#FFFFFF;border:1px solid #DCE7F5;border-left:0;border-radius:0 7px 7px 0;color:#1E293B;\">%2%3</td></tr>")
                        .arg(documentBriefFieldText(field.value(QStringLiteral("key")).toString()).toHtmlEscaped(),
                             richText(field.value(QStringLiteral("value")).toString()),
                             sources.isEmpty()
                                 ? QString()
                                 : QStringLiteral("<div style=\"margin-top:6px;color:#64748B;font-size:12px;\">来源：%1</div>")
                                       .arg(sources.toHtmlEscaped()));
        }
        html += QStringLiteral("</table>");
    }

    const QJsonArray outlineSections = context.value(QStringLiteral("outline_sections")).toArray();
    if (!outlineSections.isEmpty()) {
        html += sectionTitle(
            QStringLiteral("结构化大纲"),
            QStringLiteral("只读蓝图，确认后再进入正式撰写"),
            QStringLiteral("outline"));
        html += QStringLiteral(
            "<div style=\"margin:0 0 10px 0;padding:9px 12px;background:#F0FDF4;"
            "border-left:3px solid #34D399;color:#166534;\">"
            "当前仅整理材料结构与章节依据，不会创建、覆盖或导出文件。</div>");
        for (const QJsonValue &value : outlineSections) {
            const QJsonObject section = value.toObject();
            const QString sources = documentSourceRefsText(
                section.value(QStringLiteral("source_refs")).toArray());
            html += QStringLiteral(
                        "<div style=\"margin:0 0 10px 0;padding:12px 13px;border:1px solid #DCE7F5;"
                        "border-radius:7px;background:#FFFFFF;\"><div style=\"color:#0F172A;font-weight:700;\">%1</div>"
                        "<div style=\"margin-top:4px;color:#64748B;font-size:12px;\">%2</div>"
                        "<ul style=\"margin:8px 0 0 19px;padding:0;color:#334155;\">")
                        .arg(section.value(QStringLiteral("title")).toString().toHtmlEscaped(),
                             richText(section.value(QStringLiteral("intent")).toString()));
            for (const QJsonValue &point : section.value(QStringLiteral("key_points")).toArray()) {
                html += QStringLiteral("<li style=\"margin:0 0 4px 0;\">%1</li>")
                            .arg(richText(point.toString()));
            }
            html += QStringLiteral("</ul>");
            if (!sources.isEmpty()) {
                html += QStringLiteral("<div style=\"margin-top:7px;color:#64748B;font-size:12px;\">章节依据：%1</div>")
                            .arg(sources.toHtmlEscaped());
            }
            html += QStringLiteral("</div>");
        }
    }

    const QString draftTitle = context.value(QStringLiteral("draft_title")).toString();
    const QJsonArray draftSections = context.value(QStringLiteral("draft_sections")).toArray();
    if (!draftSections.isEmpty()) {
        html += sectionTitle(
            QStringLiteral("Markdown 草稿预览"),
            QStringLiteral("审阅通过前不会创建或覆盖文件"),
            QStringLiteral("draft"));
        html += QStringLiteral(
            "<div style=\"margin:0 0 10px 0;padding:9px 12px;background:#F0FDF4;"
            "border-left:3px solid #34D399;color:#166534;\">"
            "草稿正文与章节依据均可复核；确认保存将作为后续独立权限步骤。</div>");
        if (!draftTitle.isEmpty()) {
            html += QStringLiteral(
                        "<div style=\"margin:0 0 10px 0;padding:13px 14px;background:#F8FAFC;"
                        "border:1px solid #DCE7F5;border-radius:7px;color:#0F172A;font-size:18px;font-weight:700;\">%1</div>")
                        .arg(draftTitle.toHtmlEscaped());
        }
        for (const QJsonValue &value : draftSections) {
            const QJsonObject section = value.toObject();
            const QString sources = documentSourceRefsText(
                section.value(QStringLiteral("source_refs")).toArray());
            html += QStringLiteral(
                        "<div style=\"margin:0 0 10px 0;padding:13px 14px;border:1px solid #DCE7F5;"
                        "border-radius:7px;background:#FFFFFF;\"><div style=\"color:#0F172A;font-weight:700;font-size:15px;\">%1</div>"
                        "<div style=\"margin-top:8px;color:#334155;line-height:1.72;\">%2</div>")
                        .arg(section.value(QStringLiteral("heading")).toString().toHtmlEscaped(),
                             richText(section.value(QStringLiteral("body")).toString()));
            if (!sources.isEmpty()) {
                html += QStringLiteral("<div style=\"margin-top:8px;color:#64748B;font-size:12px;\">章节依据：%1</div>")
                            .arg(sources.toHtmlEscaped());
            }
            html += QStringLiteral("</div>");
        }
    }

    const QJsonArray comparisons = context.value(QStringLiteral("comparisons")).toArray();
    if (!comparisons.isEmpty()) {
        html += sectionTitle(QStringLiteral("跨文档对比"), QStringLiteral("每项均关联多份材料"), QStringLiteral("comparison"));
        for (const QJsonValue &value : comparisons) {
            const QJsonObject comparison = value.toObject();
            const QString kind = comparison.value(QStringLiteral("kind")).toString();
            const QString kindText = kind == QStringLiteral("common") ? QStringLiteral("共识")
                                      : kind == QStringLiteral("difference") ? QStringLiteral("差异")
                                      : kind == QStringLiteral("missing") ? QStringLiteral("缺失")
                                                                            : QStringLiteral("待核验");
            const QString kindColor = kind == QStringLiteral("common") ? QStringLiteral("#15803D")
                                       : kind == QStringLiteral("difference") ? QStringLiteral("#2563EB")
                                       : QStringLiteral("#B45309");
            const QString sources = documentSourceRefsText(
                comparison.value(QStringLiteral("source_refs")).toArray());
            html += QStringLiteral(
                        "<div style=\"margin:0 0 9px 0;padding:10px 12px;border:1px solid #DCE7F5;"
                        "border-radius:7px;background:#FFFFFF;\"><span style=\"padding:2px 6px;background:#F8FAFC;"
                        "color:%1;font-weight:700;\">%2</span> <span style=\"color:#0F172A;font-weight:700;\">%3</span>"
                        "<div style=\"margin-top:7px;color:#334155;\">%4</div>")
                        .arg(kindColor, kindText.toHtmlEscaped(),
                             comparison.value(QStringLiteral("dimension")).toString().toHtmlEscaped(),
                             richText(comparison.value(QStringLiteral("summary")).toString()));
            if (!sources.isEmpty()) {
                html += QStringLiteral("<div style=\"margin-top:6px;color:#64748B;font-size:12px;\">对比来源：%1</div>")
                            .arg(sources.toHtmlEscaped());
            }
            html += QStringLiteral("</div>");
        }
    }

    const QJsonArray requirements = context.value(QStringLiteral("requirements")).toArray();
    if (!requirements.isEmpty()) {
        html += sectionTitle(QStringLiteral("需求与约束"), QStringLiteral("可直接用于后续规划"), QStringLiteral("requirements"));
        for (const QJsonValue &value : requirements) {
            const QJsonObject requirement = value.toObject();
            const QString category = documentRequirementCategoryText(
                requirement.value(QStringLiteral("category")).toString());
            const QString priority = documentPriorityText(
                requirement.value(QStringLiteral("priority")).toString());
            const QString sources = documentSourceRefsText(
                requirement.value(QStringLiteral("source_refs")).toArray());
            html += QStringLiteral(
                        "<div style=\"margin:0 0 9px 0;padding:10px 12px;border:1px solid #DCE7F5;"
                        "border-radius:7px;background:#FFFFFF;\"><span style=\"padding:2px 6px;background:#EFF6FF;"
                        "color:#2563EB;font-weight:700;\">%1</span> <span style=\"padding:2px 6px;background:#F0FDF4;"
                        "color:#15803D;font-weight:700;\">%2</span><div style=\"margin-top:7px;color:#1E293B;\">%3</div>")
                        .arg(category.toHtmlEscaped(), priority.toHtmlEscaped(),
                             richText(requirement.value(QStringLiteral("text")).toString()));
            if (!sources.isEmpty()) {
                html += QStringLiteral("<div style=\"margin-top:6px;color:#64748B;font-size:12px;\">来源：%1</div>")
                            .arg(sources.toHtmlEscaped());
            }
            html += QStringLiteral("</div>");
        }
    }

    const QJsonArray questions = context.value(QStringLiteral("open_questions")).toArray();
    const QString reviewTarget = context.value(QStringLiteral("review_target_title")).toString();
    const QJsonArray reviewSupported = context.value(QStringLiteral("constraints")).toArray();
    if (!reviewTarget.isEmpty()) {
        html += sectionTitle(QStringLiteral("草稿事实核验"), QStringLiteral("只读复核，不改写草稿"), QStringLiteral("review"));
        html += QStringLiteral("<div style=\"margin:0 0 9px 0;padding:10px 12px;background:#EEF6FF;border-left:3px solid #60A5FA;color:#1E40AF;\">正在核验：%1</div>")
                    .arg(reviewTarget.toHtmlEscaped());
        if (!reviewSupported.isEmpty()) {
            html += QStringLiteral("<div style=\"padding:10px 12px;background:#F0FDF4;border-left:3px solid #34D399;color:#166534;\"><b>材料可支持的表述</b><ul style=\"margin:7px 0 0 18px;padding:0;\">");
            for (const QJsonValue &value : reviewSupported) {
                const QJsonObject item = value.toObject();
                html += QStringLiteral("<li style=\"margin:0 0 5px 0;\">%1</li>").arg(richText(item.value(QStringLiteral("text")).toString()));
            }
            html += QStringLiteral("</ul></div>");
        }
    }

    const QString sectionReviewTarget = context.value(QStringLiteral("revision_target_title")).toString();
    const QJsonArray revisionSuggestions = context.value(QStringLiteral("revision_suggestions")).toArray();
    if (!sectionReviewTarget.isEmpty()) {
        html += sectionTitle(
            QStringLiteral("本章审校建议"),
            QStringLiteral("只读候选建议，未改写原草稿"),
            QStringLiteral("section-review"));
        html += QStringLiteral(
                    "<div style=\"margin:0 0 10px 0;padding:10px 12px;background:#EEF6FF;"
                    "border-left:3px solid #60A5FA;color:#1E40AF;\">审校章节：%1</div>")
                    .arg(sectionReviewTarget.toHtmlEscaped());
        if (revisionSuggestions.isEmpty()) {
            html += QStringLiteral(
                        "<div style=\"padding:10px 12px;background:#F0FDF4;border-left:3px solid #34D399;"
                        "color:#166534;\">本次未发现需要给出候选改写的明确问题；原草稿未改动。</div>");
        }
        for (const QJsonValue &value : revisionSuggestions) {
            const QJsonObject suggestion = value.toObject();
            const QString severity = suggestion.value(QStringLiteral("severity")).toString();
            const QString category = suggestion.value(QStringLiteral("category")).toString();
            const QString severityText = severity == QStringLiteral("important")
                ? QStringLiteral("重点") : QStringLiteral("建议");
            const QString severityColor = severity == QStringLiteral("important")
                ? QStringLiteral("#B45309") : QStringLiteral("#2563EB");
            const QString categoryText = category == QStringLiteral("accuracy") ? QStringLiteral("准确性")
                : category == QStringLiteral("clarity") ? QStringLiteral("清晰度")
                : category == QStringLiteral("consistency") ? QStringLiteral("一致性")
                : category == QStringLiteral("structure") ? QStringLiteral("结构")
                                                         : QStringLiteral("表达风格");
            const QString sources = documentSourceRefsText(
                suggestion.value(QStringLiteral("source_refs")).toArray());
            html += QStringLiteral(
                        "<div style=\"margin:0 0 10px 0;padding:12px 13px;border:1px solid #DCE7F5;"
                        "border-radius:7px;background:#FFFFFF;\"><span style=\"padding:2px 6px;background:#FFF7ED;"
                        "color:%1;font-weight:700;\">%2</span> <span style=\"padding:2px 6px;background:#EFF6FF;"
                        "color:#2563EB;font-weight:700;\">%3</span>"
                        "<div style=\"margin-top:9px;color:#64748B;font-size:12px;font-weight:700;\">原文片段</div>"
                        "<div style=\"margin-top:3px;padding:8px 10px;background:#F8FAFC;border-left:3px solid #CBD5E1;color:#334155;\">%4</div>"
                        "<div style=\"margin-top:9px;color:#166534;font-size:12px;font-weight:700;\">候选建议</div>"
                        "<div style=\"margin-top:3px;padding:8px 10px;background:#F0FDF4;border-left:3px solid #34D399;color:#166534;\">%5</div>"
                        "<div style=\"margin-top:9px;color:#64748B;font-size:12px;font-weight:700;\">为什么建议这样调整</div>"
                        "<div style=\"margin-top:3px;color:#334155;\">%6</div>")
                        .arg(severityColor, severityText.toHtmlEscaped(), categoryText.toHtmlEscaped(),
                             richText(suggestion.value(QStringLiteral("original_excerpt")).toString()),
                             richText(suggestion.value(QStringLiteral("suggested_text")).toString()),
                             richText(suggestion.value(QStringLiteral("reason")).toString()));
            if (!sources.isEmpty()) {
                html += QStringLiteral("<div style=\"margin-top:8px;color:#64748B;font-size:12px;\">材料依据：%1</div>")
                            .arg(sources.toHtmlEscaped());
            }
            html += QStringLiteral("</div>");
        }
    }
    if (!questions.isEmpty()) {
        html += sectionTitle(QStringLiteral("待确认问题"), QString(), QStringLiteral("questions"));
        html += QStringLiteral("<div style=\"padding:10px 12px;background:#FFF9ED;border-left:3px solid #F59E0B;color:#92400E;\"><ul style=\"margin:0 0 0 18px;padding:0;\">");
        for (const QJsonValue &value : questions) {
            const QJsonObject question = value.toObject();
            html += QStringLiteral("<li style=\"margin:0 0 5px 0;\">%1</li>")
                        .arg(richText(question.value(QStringLiteral("text")).toString()));
        }
        html += QStringLiteral("</ul></div>");
    }

    const QJsonArray sources = context.value(QStringLiteral("sources")).toArray();
    if (!sources.isEmpty()) {
        html += sectionTitle(QStringLiteral("参考来源"), QStringLiteral("结论可回溯"), QStringLiteral("sources"));
        html += QStringLiteral("<div style=\"padding:10px 12px;background:#F8FAFC;border:1px solid #E2E8F0;border-radius:7px;\"><ul style=\"margin:0 0 0 18px;padding:0;color:#475569;\">");
        for (const QJsonValue &value : sources) {
            const QJsonObject source = value.toObject();
            const QString path = source.value(QStringLiteral("relative_path")).toString();
            QString sourceText = QStringLiteral("%1 · %2").arg(path, documentSourceLocationText(source));
            const QString excerpt = source.value(QStringLiteral("excerpt")).toString();
            if (!excerpt.isEmpty()) {
                sourceText += QStringLiteral("：%1").arg(compactPlainPreview(excerpt, 180));
            }
            html += QStringLiteral("<li style=\"margin:0 0 6px 0;\">%1</li>").arg(sourceText.toHtmlEscaped());
        }
        html += QStringLiteral("</ul></div>");
    }

    const QJsonArray warnings = context.value(QStringLiteral("warnings")).toArray();
    const QJsonArray missingContext = context.value(QStringLiteral("missing_context")).toArray();
    if (!warnings.isEmpty() || !missingContext.isEmpty()) {
        html += sectionTitle(QStringLiteral("注意事项"), QString(), QStringLiteral("warnings"));
        html += QStringLiteral("<div style=\"padding:10px 12px;background:#FFF7ED;color:#9A3412;border-left:3px solid #FDBA74;\"><b>需要留意</b>");
        for (const QJsonValue &value : warnings) {
            html += QStringLiteral("<br/>%1").arg(value.toString().toHtmlEscaped());
        }
        for (const QJsonValue &value : missingContext) {
            html += QStringLiteral("<br/>%1").arg(value.toString().toHtmlEscaped());
        }
        html += QStringLiteral("</div>");
    }

    html += QStringLiteral("</div>");
    return html;
}

void MainWindow::setDocumentResultHtml(
    const QString &html,
    const QString &detailStatus,
    bool available)
{
    // 预览与详情共享同一份已校验 HTML，避免两个页面因重复格式化而展示不同事实。
    ui->documentResultText->setHtml(html);
    ui->documentResultDetailText->setHtml(html);
    ui->documentResultDetailText->scrollToAnchor(QStringLiteral("conclusion"));
    ui->documentResultDetailStatus->setText(detailStatus);
    polishBadge(ui->documentResultDetailStatus, available ? QStringLiteral("badgeGreen") : QStringLiteral("badgeGray"));
    documentAnalysisDetailAvailable = available;
    updateDocumentOpenResultAction();
}

void MainWindow::showLatestDocumentWorkbenchResult()
{
    const QString selectedDocument = ui->documentDocumentCombo->currentData().toString().trimmed();
    const bool reviewMatchesSelection = !selectedDocument.isEmpty()
        && selectedDocument == latestDocumentReviewReference;

    if (reviewMatchesSelection) {
        if (latestDocumentReviewKind == DocumentWorkbenchReviewKind::Project
            && !latestProjectReviewResult.taskId.isEmpty()) {
            showProjectReviewDialog(latestProjectReviewResult);
            return;
        }
        if (latestDocumentReviewKind == DocumentWorkbenchReviewKind::Paper
            && !latestPaperReviewResult.taskId.isEmpty()) {
            showPaperReviewDialog(latestPaperReviewResult);
            return;
        }
    }

    if (documentAnalysisDetailAvailable) {
        showDocumentResultDetail();
    }
}

void MainWindow::updateDocumentOpenResultAction()
{
    const bool reviewRunning = projectDocumentReviewLoading || paperReviewLoading;
    const QString selectedDocument = ui->documentDocumentCombo->currentData().toString().trimmed();
    const bool reviewMatchesSelection = !selectedDocument.isEmpty()
        && selectedDocument == latestDocumentReviewReference
        && latestDocumentReviewKind != DocumentWorkbenchReviewKind::None;

    if (reviewRunning) {
        ui->documentOpenResultButton->setText(QStringLiteral("审查中…"));
        ui->documentOpenResultButton->setToolTip(QStringLiteral("当前审查尚未生成可查看的已校验报告"));
        ui->documentOpenResultButton->setEnabled(false);
        return;
    }

    if (reviewMatchesSelection) {
        const bool projectReview = latestDocumentReviewKind == DocumentWorkbenchReviewKind::Project;
        ui->documentOpenResultButton->setText(QStringLiteral("查看审查报告"));
        ui->documentOpenResultButton->setToolTip(
            projectReview ? QStringLiteral("查看当前材料最近一次项目文档审查报告")
                          : QStringLiteral("查看当前材料最近一次论文审查报告"));
        ui->documentOpenResultButton->setEnabled(true);
        return;
    }

    ui->documentOpenResultButton->setText(QStringLiteral("查看详情"));
    ui->documentOpenResultButton->setToolTip(QStringLiteral("在独立结果页查看完整结论、来源和运行记录"));
    ui->documentOpenResultButton->setEnabled(documentAnalysisDetailAvailable);
}

void MainWindow::updateDocumentResultDetailSections(
    const QJsonObject &documentContext,
    bool available,
    const QString &firstSectionText,
    const QString &firstSectionLookup)
{
    // 导航只反映本次 Guardrail 已验证的结构化字段，不能为了界面看起来完整而凭空补空分区。
    const QSignalBlocker blocker(ui->documentResultDetailSectionCombo);
    ui->documentResultDetailSectionCombo->clear();

    if (!available) {
        ui->documentResultDetailSectionCombo->setEnabled(false);
        ui->documentResultDetailMeta->setText(QStringLiteral("完成一次分析后，可按内容分区快速查看。"));
        return;
    }

    ui->documentResultDetailSectionCombo->addItem(
        firstSectionText,
        firstSectionLookup.isEmpty() ? QStringLiteral("conclusion") : firstSectionLookup);
    QStringList summaryParts;
    const QString summary = documentContext.value(QStringLiteral("summary")).toString();
    if (!summary.isEmpty()) {
        ui->documentResultDetailSectionCombo->addItem(QStringLiteral("内容摘要"), QStringLiteral("summary"));
        summaryParts.append(QStringLiteral("摘要"));
    }

    const QJsonArray briefFields = documentContext.value(QStringLiteral("brief_fields")).toArray();
    if (!briefFields.isEmpty()) {
        ui->documentResultDetailSectionCombo->addItem(QStringLiteral("关键信息卡"), QStringLiteral("brief"));
        summaryParts.append(QStringLiteral("%1 项字段").arg(briefFields.size()));
    }

    const QJsonArray outlineSections = documentContext.value(QStringLiteral("outline_sections")).toArray();
    if (!outlineSections.isEmpty()) {
        ui->documentResultDetailSectionCombo->addItem(QStringLiteral("结构化大纲"), QStringLiteral("outline"));
        summaryParts.append(QStringLiteral("%1 个章节").arg(outlineSections.size()));
    }

    const QJsonArray draftSections = documentContext.value(QStringLiteral("draft_sections")).toArray();
    if (documentContext.value(QStringLiteral("draft_version")).isObject()) {
        ui->documentResultDetailSectionCombo->addItem(QStringLiteral("版本链"), QStringLiteral("version"));
        summaryParts.append(QStringLiteral("版本可追溯"));
    }
    if (documentContext.value(QStringLiteral("revision_preview")).isObject()) {
        ui->documentResultDetailSectionCombo->addItem(QStringLiteral("修订差异"), QStringLiteral("revision-preview"));
        summaryParts.append(QStringLiteral("修订预览"));
    }
    if (documentContext.value(QStringLiteral("manual_revision_preview")).isObject()) {
        ui->documentResultDetailSectionCombo->addItem(
            QStringLiteral("手动修订差异"),
            QStringLiteral("manual-revision-preview"));
        summaryParts.append(QStringLiteral("待核验修订"));
    }
    if (documentContext.value(QStringLiteral("template_preview")).isObject()) {
        ui->documentResultDetailSectionCombo->addItem(
            QStringLiteral("模板与交付"),
            QStringLiteral("template"));
        summaryParts.append(QStringLiteral("模板交付"));
    }
    if (documentContext.value(QStringLiteral("merge_preview")).isObject()) {
        ui->documentResultDetailSectionCombo->addItem(
            QStringLiteral("章节合并"),
            QStringLiteral("merge"));
        summaryParts.append(QStringLiteral("合并预览"));
    }
    if (!draftSections.isEmpty()) {
        ui->documentResultDetailSectionCombo->addItem(QStringLiteral("Markdown 草稿"), QStringLiteral("draft"));
        summaryParts.append(QStringLiteral("%1 段草稿").arg(draftSections.size()));
    }

    if (!documentContext.value(QStringLiteral("review_target_title")).toString().isEmpty()) {
        ui->documentResultDetailSectionCombo->addItem(QStringLiteral("事实核验"), QStringLiteral("review"));
        summaryParts.append(QStringLiteral("事实核验"));
    }

    if (!documentContext.value(QStringLiteral("revision_target_title")).toString().isEmpty()) {
        ui->documentResultDetailSectionCombo->addItem(QStringLiteral("本章审校"), QStringLiteral("section-review"));
        summaryParts.append(QStringLiteral("本章审校"));
    }

    const QJsonArray comparisons = documentContext.value(QStringLiteral("comparisons")).toArray();
    if (!comparisons.isEmpty()) {
        ui->documentResultDetailSectionCombo->addItem(QStringLiteral("跨文档对比"), QStringLiteral("comparison"));
        summaryParts.append(QStringLiteral("%1 项对比").arg(comparisons.size()));
    }

    const QJsonArray requirements = documentContext.value(QStringLiteral("requirements")).toArray();
    if (!requirements.isEmpty()) {
        ui->documentResultDetailSectionCombo->addItem(QStringLiteral("需求与约束"), QStringLiteral("requirements"));
        summaryParts.append(QStringLiteral("%1 条需求").arg(requirements.size()));
    }

    const QJsonArray questions = documentContext.value(QStringLiteral("open_questions")).toArray();
    if (!questions.isEmpty()) {
        ui->documentResultDetailSectionCombo->addItem(QStringLiteral("待确认问题"), QStringLiteral("questions"));
        summaryParts.append(QStringLiteral("%1 个待确认项").arg(questions.size()));
    }

    const QJsonArray sources = documentContext.value(QStringLiteral("sources")).toArray();
    if (!sources.isEmpty()) {
        ui->documentResultDetailSectionCombo->addItem(QStringLiteral("参考来源"), QStringLiteral("sources"));
        summaryParts.append(QStringLiteral("%1 条来源").arg(sources.size()));
    }

    const bool hasWarnings = !documentContext.value(QStringLiteral("warnings")).toArray().isEmpty()
                             || !documentContext.value(QStringLiteral("missing_context")).toArray().isEmpty();
    if (hasWarnings) {
        ui->documentResultDetailSectionCombo->addItem(QStringLiteral("注意事项"), QStringLiteral("warnings"));
        summaryParts.append(QStringLiteral("含注意事项"));
    }

    ui->documentResultDetailSectionCombo->setCurrentIndex(0);
    ui->documentResultDetailSectionCombo->setEnabled(ui->documentResultDetailSectionCombo->count() > 1);
    ui->documentResultDetailMeta->setText(
        summaryParts.isEmpty() ? QStringLiteral("本次结果只包含可复核结论。")
                               : QStringLiteral("本次结果：%1").arg(summaryParts.join(QStringLiteral(" · "))));
}

void MainWindow::showDocumentResultDetail()
{
    switchPage(14);
}

void MainWindow::showDocumentWorkbench()
{
    switchPage(4);
}

void MainWindow::updateDocumentDraftSaveAction()
{
    const bool hasValidatedDraft = !currentDocumentResultTaskId.isEmpty()
        && !currentDocumentResultContext.value(QStringLiteral("draft_title")).toString().trimmed().isEmpty()
        && !currentDocumentResultContext.value(QStringLiteral("draft_sections")).toArray().isEmpty();
    // 保存过一次后仍允许用户以新名字另存；后端拒绝同名覆盖，避免“已保存”把用户锁死在单一产物上。
    const QString verificationState = currentDocumentResultContext
                                        .value(QStringLiteral("draft_verification_state"))
                                        .toString();
    const bool verificationComplete = verificationState.isEmpty()
        || verificationState == QStringLiteral("verified");
    const bool canSave = hasValidatedDraft && verificationComplete && !documentDraftSaving
        && !documentDraftMergeLoading && !documentPresentationExporting;

    ui->documentResultDetailSaveButton->setEnabled(canSave);
    // 复制是本地剪贴板操作，不触发后端写入；运行中的结果仍必须等 Guardrail 完成后才能复制。
    ui->documentResultDetailCopyButton->setEnabled(
        hasValidatedDraft && !documentAgentRunning && !documentDraftMergeLoading);
    ui->documentResultDetailCopyButton->setToolTip(
        hasValidatedDraft && !documentAgentRunning && verificationState == QStringLiteral("requires_review")
            ? QStringLiteral("复制当前待核验草稿，不写入文件；请勿将其视为已完成来源核验")
            : hasValidatedDraft && !documentAgentRunning
            ? QStringLiteral("复制当前已验证草稿及章节来源脚注，不写入文件")
            : QStringLiteral("仅已完成且带来源的 Markdown 草稿可以复制"));
    updateDocumentDraftReviewAction();
    if (documentDraftSaving) {
        ui->documentResultDetailSaveButton->setText(QStringLiteral("保存中…"));
        ui->documentResultDetailSaveButton->setToolTip(QStringLiteral("正在把已确认的草稿写入受控输出目录"));
    } else if (documentDraftSaved) {
        ui->documentResultDetailSaveButton->setText(QStringLiteral("另存为 Markdown"));
        ui->documentResultDetailSaveButton->setToolTip(
            QStringLiteral("本次草稿已保存；可用新文件名另存一份，或在任务历史的产物区预览和打开"));
    } else {
        ui->documentResultDetailSaveButton->setText(QStringLiteral("保存 Markdown"));
        ui->documentResultDetailSaveButton->setToolTip(
            hasValidatedDraft && verificationState == QStringLiteral("requires_review")
                ? QStringLiteral("手动修订尚未重新核验来源；请先点击“核验事实”")
                : hasValidatedDraft && verificationState == QStringLiteral("reviewed_with_questions")
                ? QStringLiteral("手动修订仍有待确认事实；补充或修改后再次核验才能保存")
                : hasValidatedDraft
                ? QStringLiteral("保存到 output/document_drafts，需要再次确认，不会覆盖同名文件")
                : QStringLiteral("仅已完成且带来源的 Markdown 草稿可以保存"));
    }
    updateDocumentSectionDraftAction();
    updateDocumentPresentationAction();
    updateProjectReviewAction();
    updateDocumentActivityState();
}

void MainWindow::updateDocumentPresentationAction()
{
    const QString verificationState = currentDocumentResultContext
                                        .value(QStringLiteral("draft_verification_state"))
                                        .toString();
    const bool hasExportableDraft = !currentDocumentResultTaskId.isEmpty()
        && !currentDocumentResultContext.value(QStringLiteral("draft_title")).toString().trimmed().isEmpty()
        && !currentDocumentResultContext.value(QStringLiteral("draft_sections")).toArray().isEmpty()
        && (verificationState.isEmpty() || verificationState == QStringLiteral("verified"));
    const bool enabled = hasExportableDraft && !documentAgentRunning && !documentDraftSaving
        && !documentDraftMergeLoading && !documentPresentationPreviewLoading && !documentPresentationExporting;

    ui->documentResultDetailPresentationButton->setEnabled(enabled);
    if (documentPresentationExporting) {
        ui->documentResultDetailPresentationButton->setText(QStringLiteral("导出中…"));
        ui->documentResultDetailPresentationButton->setToolTip(
            QStringLiteral("正在生成 PPTX、回读验证并写入任务历史，请勿重复提交"));
    } else if (documentPresentationPreviewLoading) {
        ui->documentResultDetailPresentationButton->setText(QStringLiteral("准备计划…"));
        ui->documentResultDetailPresentationButton->setToolTip(
            QStringLiteral("正在从已核验草稿建立只读幻灯片计划，不会调用模型或写入文件"));
    } else {
        ui->documentResultDetailPresentationButton->setText(QStringLiteral("导出项目 PPT"));
        ui->documentResultDetailPresentationButton->setToolTip(
            hasExportableDraft
                ? QStringLiteral("先审阅项目方案幻灯片计划，再确认导出可编辑 PPTX；不会覆盖同名文件")
                : verificationState == QStringLiteral("requires_review")
                ? QStringLiteral("手动修订尚未核验来源，暂不能导出项目方案 PPT")
                : verificationState == QStringLiteral("reviewed_with_questions")
                ? QStringLiteral("草稿仍有待确认事实，暂不能导出项目方案 PPT")
                : QStringLiteral("仅已完成且带来源的 Markdown 草稿可以导出项目方案 PPT"));
    }
    // 幻灯片计划和导出都属于同一条交付链。同步刷新主工作台，避免计划生成期间仍显示可重复点击的入口。
    updateProjectReviewAction();
}

void MainWindow::updateDocumentSectionDraftAction()
{
    const bool hasValidatedDraft = !currentDocumentResultTaskId.isEmpty()
        && !currentDocumentResultContext.value(QStringLiteral("draft_sections")).toArray().isEmpty();
    const bool canCreatePreview = hasValidatedDraft && !documentAgentRunning && !documentDraftSaving
        && !documentDraftMergeLoading && !documentPresentationPreviewLoading && !documentPresentationExporting;
    const bool hasRevisionSuggestions = !currentDocumentResultContext
        .value(QStringLiteral("revision_target_section_id")).toString().isEmpty()
        && !currentDocumentResultContext.value(QStringLiteral("revision_suggestions")).toArray().isEmpty();

    if (documentSectionRevisionAction) {
        documentSectionRevisionAction->setEnabled(hasRevisionSuggestions && canCreatePreview);
        documentSectionRevisionAction->setToolTip(
            hasRevisionSuggestions && canCreatePreview
                ? QStringLiteral("选择一条已审校建议，查看精确替换后的差异；不会覆盖原稿或文件")
                : QStringLiteral("仅完成本章审校并得到候选建议后，才能生成独立修订预览"));
    }
    if (documentSectionBatchRevisionAction) {
        const bool canCreateBatchPreview = hasRevisionSuggestions
            && currentDocumentResultContext.value(QStringLiteral("revision_suggestions")).toArray().size() >= 2
            && canCreatePreview;
        documentSectionBatchRevisionAction->setEnabled(canCreateBatchPreview);
        documentSectionBatchRevisionAction->setToolTip(
            canCreateBatchPreview
                ? QStringLiteral("选择 2 至 6 条同章建议后，后端会校验片段唯一且不重叠再生成预览")
                : QStringLiteral("至少需要两条已完成本章审校的候选建议，才能生成合并预览"));
    }
    if (documentSectionManualRevisionAction) {
        documentSectionManualRevisionAction->setEnabled(canCreatePreview);
        documentSectionManualRevisionAction->setToolTip(
            canCreatePreview
                ? QStringLiteral("在独立编辑窗口修改本章；先建立待核验预览，完成事实核验后才能保存")
                : QStringLiteral("仅已完成且带来源的 Markdown 草稿可以建立手动修订预览"));
    }
    if (documentDraftTemplateAction) {
        const QString verificationState = currentDocumentResultContext
                                            .value(QStringLiteral("draft_verification_state"))
                                            .toString();
        const bool canCreateTemplate = canCreatePreview
            && (verificationState.isEmpty() || verificationState == QStringLiteral("verified"));
        documentDraftTemplateAction->setEnabled(canCreateTemplate);
        documentDraftTemplateAction->setToolTip(
            canCreateTemplate
                ? QStringLiteral("选择项目方案、PRD 或会议纪要模板；只重组已核验章节与来源，不调用模型或写文件")
                : verificationState == QStringLiteral("requires_review")
                ? QStringLiteral("手动修订尚未完成事实核验，暂不能建立交付预览")
                : verificationState == QStringLiteral("reviewed_with_questions")
                ? QStringLiteral("草稿仍有待确认事实，补充或修改后再次核验才能建立交付预览")
                : QStringLiteral("仅已完成且带来源的 Markdown 草稿可以建立交付预览"));
    }
    if (documentDraftMergeAction) {
        const QJsonObject version = currentDocumentResultContext
                                        .value(QStringLiteral("draft_version"))
                                        .toObject();
        const QString verificationState = currentDocumentResultContext
                                            .value(QStringLiteral("draft_verification_state"))
                                            .toString();
        const QString kind = version.value(QStringLiteral("kind")).toString();
        const QSet<QString> mergeableKinds{
            QStringLiteral("base_draft"),
            QStringLiteral("revision_preview"),
            QStringLiteral("revision_batch_preview"),
            QStringLiteral("restored_preview"),
            QStringLiteral("merge_preview"),
        };
        const bool canMerge = canCreatePreview
            && !documentDraftMergeLoading
            && !documentDraftMergeRunning
            && verificationState == QStringLiteral("verified")
            && mergeableKinds.contains(kind);
        documentDraftMergeAction->setEnabled(canMerge);
        documentDraftMergeAction->setText(
            documentDraftMergeLoading ? QStringLiteral("正在加载合并计划…")
            : documentDraftMergeRunning ? QStringLiteral("正在建立合并预览…")
                                        : QStringLiteral("合并其他版本"));
        documentDraftMergeAction->setToolTip(
            canMerge
                ? QStringLiteral("选择同根已核验版本；系统先展示共同祖先与冲突，确认后才建立独立预览")
                : documentDraftMergeLoading || documentDraftMergeRunning
                ? QStringLiteral("章节合并正在处理中，请等待当前操作结束")
                : verificationState != QStringLiteral("verified")
                ? QStringLiteral("只有已完成事实核验的草稿版本可以参与章节合并")
                : QStringLiteral("仅完整草稿、修订预览、恢复预览或既有合并预览可以参与章节合并"));
    }
    if (documentDraftRestoreAction) {
        documentDraftRestoreAction->setEnabled(canCreatePreview);
        documentDraftRestoreAction->setToolTip(
            canCreatePreview
                ? QStringLiteral("从当前版本建立新的独立恢复预览；不调用模型、不覆盖旧任务或文件")
                : QStringLiteral("仅已完成且带来源的 Markdown 草稿可以建立恢复预览"));
    }
    if (documentDraftParentDiffAction) {
        const QJsonObject version = currentDocumentResultContext
                                        .value(QStringLiteral("draft_version"))
                                        .toObject();
        const bool hasParentVersion = !version.value(QStringLiteral("parent_task_id")).toString().trimmed().isEmpty();
        const bool canCompare = hasParentVersion && hasValidatedDraft && !documentAgentRunning
            && !documentDraftParentDiffLoading;
        documentDraftParentDiffAction->setEnabled(canCompare);
        documentDraftParentDiffAction->setText(
            documentDraftParentDiffLoading ? QStringLiteral("正在加载版本差异…") : QStringLiteral("与父版本对比"));
        documentDraftParentDiffAction->setToolTip(
            canCompare
                ? QStringLiteral("只读比较当前快照与直接父版本；不调用模型、不读取工作区、不写文件")
                : hasParentVersion
                ? QStringLiteral("当前草稿仍在运行或版本差异正在加载")
                : QStringLiteral("初版草稿没有直接父版本；派生版本才可对比"));
    }

    ui->documentResultDetailSectionDraftButton->setEnabled(canCreatePreview);
    if (documentDraftMergeRunning) {
        ui->documentResultDetailSectionDraftButton->setText(QStringLiteral("合并中…"));
        ui->documentResultDetailSectionDraftButton->setToolTip(
            QStringLiteral("正在按已确认选择建立章节合并预览；所有旧版本和文件未改动"));
    } else if (documentDraftTemplateRunning) {
        ui->documentResultDetailSectionDraftButton->setText(QStringLiteral("交付中…"));
        ui->documentResultDetailSectionDraftButton->setToolTip(
            QStringLiteral("正在按固定模板重组已核验章节与来源；原草稿和文件未改动"));
    } else if (documentManualRevisionRunning) {
        ui->documentResultDetailSectionDraftButton->setText(QStringLiteral("修订中…"));
        ui->documentResultDetailSectionDraftButton->setToolTip(
            QStringLiteral("正在建立待来源核验的手动修订预览；原草稿和文件未改动"));
    } else if (documentSectionRevisionRunning) {
        ui->documentResultDetailSectionDraftButton->setText(QStringLiteral("生成中…"));
        ui->documentResultDetailSectionDraftButton->setToolTip(
            QStringLiteral("正在校验候选建议并生成独立修订预览；原草稿和文件未改动"));
    } else if (documentSectionReviewRunning) {
        ui->documentResultDetailSectionDraftButton->setText(QStringLiteral("审校中…"));
        ui->documentResultDetailSectionDraftButton->setToolTip(
            QStringLiteral("正在重新读取原材料并生成只读本章审校建议"));
    } else if (documentSectionDraftRunning) {
        ui->documentResultDetailSectionDraftButton->setText(QStringLiteral("撰写中…"));
        ui->documentResultDetailSectionDraftButton->setToolTip(
            QStringLiteral("正在重新读取原材料并生成独立章节预览"));
    } else if (documentAgentRunning) {
        ui->documentResultDetailSectionDraftButton->setText(QStringLiteral("撰写本章"));
        ui->documentResultDetailSectionDraftButton->setToolTip(
            QStringLiteral("当前文档任务尚未结束，完成后可基于已验证草稿撰写本章"));
    } else {
        ui->documentResultDetailSectionDraftButton->setText(QStringLiteral("撰写本章"));
        ui->documentResultDetailSectionDraftButton->setToolTip(
            hasValidatedDraft
                ? QStringLiteral("选择一个草稿章节并生成独立预览，不修改原草稿或已保存文件")
                : QStringLiteral("仅已完成且带来源的 Markdown 草稿可以继续撰写章节"));
    }
}

void MainWindow::updateDocumentDraftReviewAction()
{
    const bool hasValidatedDraft = !currentDocumentResultTaskId.isEmpty()
        && !currentDocumentResultContext.value(QStringLiteral("draft_sections")).toArray().isEmpty();
    const bool enabled = hasValidatedDraft && !documentAgentRunning && !documentDraftSaving
        && !documentDraftMergeLoading;
    ui->documentResultDetailReviewButton->setEnabled(enabled);
    ui->documentResultDetailReviewButton->setText(documentDraftReviewRunning ? QStringLiteral("核验中…") : QStringLiteral("核验事实"));
    ui->documentResultDetailReviewButton->setToolTip(
        enabled ? QStringLiteral("重新读取材料并标出可支持或待复核的草稿事实，不改写或保存文件")
                : QStringLiteral("仅已完成且带来源的 Markdown 草稿可以核验"));
}

void MainWindow::reviewDocumentDraft()
{
    if (documentAgentRunning || currentDocumentResultTaskId.isEmpty()) {
        return;
    }
    documentDraftReviewRunning = true;
    documentSectionDraftRunning = false;
    documentSectionReviewRunning = false;
    documentSectionRevisionRunning = false;
    documentManualRevisionRunning = false;
    documentDraftTemplateRunning = false;
    documentDraftMergeRunning = false;
    documentDraftRestoreRunning = false;
    documentAgentRunning = true;
    ui->documentRunButton->setEnabled(false);
    ui->documentInput->setEnabled(false);
    ui->documentOutputModeCombo->setEnabled(false);
    ui->documentDocumentCombo->setEnabled(false);
    ui->documentComparisonList->setEnabled(false);
    ui->documentImportButton->setEnabled(false);
    ui->documentRefreshButton->setEnabled(false);
    updateDocumentDraftSaveAction();
    ui->documentRunStatus->setText(QStringLiteral("正在重新读取材料并核验草稿事实"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    backendClient->reviewDocumentDraft(currentDocumentResultTaskId);
}

QString MainWindow::suggestedDocumentDraftFilename() const
{
    QString title = documentDraftSaved && !lastSavedDocumentDraftFilename.isEmpty()
        ? lastSavedDocumentDraftFilename
        : currentDocumentResultContext.value(QStringLiteral("draft_title")).toString().simplified();
    if (documentDraftSaved && title.endsWith(QStringLiteral(".md"), Qt::CaseInsensitive)) {
        title.chop(3);
        title += QStringLiteral("-副本");
    } else if (currentDocumentResultContext.value(QStringLiteral("revision_preview")).isObject()) {
        // 修订预览仍沿用原文档标题，但默认文件名明确标识版本，减少用户误把它当成旧稿覆盖。
        const QString versionKind = currentDocumentResultContext
            .value(QStringLiteral("draft_version"))
            .toObject()
            .value(QStringLiteral("kind"))
            .toString();
        title += versionKind == QStringLiteral("revision_batch_preview")
            ? QStringLiteral("-合并修订稿")
            : QStringLiteral("-修订稿");
    } else if (currentDocumentResultContext.value(QStringLiteral("draft_version"))
                   .toObject()
                   .value(QStringLiteral("kind"))
                   .toString() == QStringLiteral("restored_preview")) {
        // 恢复预览与原文件无关，建议名明确标识新版本，提醒用户保存仍是“另存”而非回滚。
        title += QStringLiteral("-恢复稿");
    }
    if (title.isEmpty()) {
        title = QStringLiteral("AgentFlow 文档草稿");
    }

    // 前端先给出 Windows 友好的建议名；后端仍会再次拒绝路径分隔符并做最终目录边界校验。
    const QString forbidden = QStringLiteral("<>:\"|?*/") + QLatin1Char('\\');
    for (QChar &character : title) {
        if (forbidden.contains(character)) {
            character = QChar('-');
        }
    }
    title = title.trimmed();
    while (title.endsWith(QLatin1Char('.')) || title.endsWith(QLatin1Char(' '))) {
        title.chop(1);
    }
    if (title.isEmpty()) {
        title = QStringLiteral("AgentFlow 文档草稿");
    }
    return title.left(96) + QStringLiteral(".md");
}

void MainWindow::copyDocumentDraftToClipboard()
{
    const QString title = currentDocumentResultContext.value(QStringLiteral("draft_title")).toString().trimmed();
    const QJsonArray sections = currentDocumentResultContext.value(QStringLiteral("draft_sections")).toArray();
    if (currentDocumentResultTaskId.isEmpty() || title.isEmpty() || sections.isEmpty()) {
        QMessageBox::information(
            this,
            QStringLiteral("复制 Markdown"),
            QStringLiteral("当前结果没有可复制的已验证 Markdown 草稿。"));
        return;
    }

    QStringList markdown;
    markdown << QStringLiteral("# %1").arg(title);
    for (const QJsonValue &value : sections) {
        const QJsonObject section = value.toObject();
        const QString heading = section.value(QStringLiteral("heading")).toString().trimmed();
        const QString body = section.value(QStringLiteral("body")).toString().trimmed();
        if (heading.isEmpty() || body.isEmpty()) {
            continue;
        }
        markdown << QStringLiteral("## %1").arg(heading) << body;

        QStringList references;
        for (const QJsonValue &sourceValue : section.value(QStringLiteral("source_refs")).toArray()) {
            const QJsonObject source = sourceValue.toObject();
            const QString path = source.value(QStringLiteral("relative_path")).toString().trimmed();
            if (path.isEmpty()) {
                continue;
            }
            references << QStringLiteral("- %1 · %2")
                              .arg(path, documentSourceLocationText(source));
        }
        if (!references.isEmpty()) {
            markdown << QStringLiteral("### 章节来源") << references.join(QLatin1Char('\n'));
        }
    }

    const QString output = markdown.join(QStringLiteral("\n\n")).trimmed();
    if (output.isEmpty()) {
        QMessageBox::warning(
            this,
            QStringLiteral("复制 Markdown"),
            QStringLiteral("草稿章节内容不完整，暂时无法复制。"));
        return;
    }

    // 剪贴板只承载用户当前可见的已验证结果，不触发任何文件写入或后端网络请求。
    QApplication::clipboard()->setText(output);
    ui->documentRunStatus->setText(QStringLiteral("已复制 Markdown 草稿 · 原文件未改动"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeGreen"));
    ui->documentResultDetailStatus->setText(QStringLiteral("草稿已复制 · 仍可继续审阅或保存"));
    polishBadge(ui->documentResultDetailStatus, QStringLiteral("badgeGreen"));
}

void MainWindow::createDocumentDraftSectionPreview()
{
    if (documentAgentRunning || currentDocumentResultTaskId.isEmpty()) {
        return;
    }

    const QJsonArray sections = currentDocumentResultContext.value(QStringLiteral("draft_sections")).toArray();
    if (sections.isEmpty()) {
        QMessageBox::information(
            this,
            QStringLiteral("撰写本章"),
            QStringLiteral("当前结果没有可继续撰写的 Markdown 草稿章节。"));
        return;
    }

    QStringList labels;
    QStringList sectionIds;
    QSet<QString> usedLabels;
    for (const QJsonValue &value : sections) {
        const QJsonObject section = value.toObject();
        const QString sectionId = section.value(QStringLiteral("id")).toString().trimmed();
        const QString heading = section.value(QStringLiteral("heading")).toString().simplified();
        const QString body = section.value(QStringLiteral("body")).toString().trimmed();
        if (sectionId.isEmpty() || heading.isEmpty() || body.isEmpty()) {
            continue;
        }
        QString label = heading;
        int duplicateIndex = 2;
        while (usedLabels.contains(label)) {
            label = QStringLiteral("%1（第 %2 节）").arg(heading).arg(duplicateIndex++);
        }
        usedLabels.insert(label);
        labels.append(label);
        sectionIds.append(sectionId);
    }
    if (labels.isEmpty()) {
        QMessageBox::information(
            this,
            QStringLiteral("撰写本章"),
            QStringLiteral("当前草稿缺少可验证的章节标识或正文，不能安全继续撰写。"));
        return;
    }

    bool sectionAccepted = false;
    const QString selectedLabel = QInputDialog::getItem(
        this,
        QStringLiteral("选择章节"),
        QStringLiteral("基于哪个章节生成独立预览？"),
        labels,
        0,
        false,
        &sectionAccepted);
    if (!sectionAccepted) {
        return;
    }
    const int sectionIndex = labels.indexOf(selectedLabel);
    if (sectionIndex < 0 || sectionIndex >= sectionIds.size()) {
        return;
    }

    bool instructionAccepted = false;
    const QString instruction = QInputDialog::getMultiLineText(
        this,
        QStringLiteral("本章调整要求"),
        QStringLiteral("希望如何撰写或调整本章？"),
        QStringLiteral("请让本章表达更清晰、层次更完整，但只能依据材料中明确的事实。"),
        &instructionAccepted).trimmed();
    if (!instructionAccepted) {
        return;
    }
    if (instruction.isEmpty()) {
        QMessageBox::warning(
            this,
            QStringLiteral("撰写本章"),
            QStringLiteral("请说明希望如何调整本章后再继续。"));
        return;
    }

    const QMessageBox::StandardButton choice = QMessageBox::question(
        this,
        QStringLiteral("确认生成本章预览"),
        QStringLiteral("将重新读取原材料，为“%1”生成一份独立的可追溯预览。\n\n"
                       "不会修改原草稿、不会覆盖已保存文件，也不会写入新的文件。")
            .arg(selectedLabel),
        QMessageBox::Yes | QMessageBox::No,
        QMessageBox::No);
    if (choice != QMessageBox::Yes) {
        return;
    }

    documentSectionDraftRunning = true;
    documentSectionReviewRunning = false;
    documentSectionRevisionRunning = false;
    documentManualRevisionRunning = false;
    documentDraftTemplateRunning = false;
    documentDraftMergeRunning = false;
    documentDraftReviewRunning = false;
    documentDraftRestoreRunning = false;
    documentAgentRunning = true;
    activeDocumentAgentTaskId.clear();
    ui->documentRunButton->setEnabled(false);
    ui->documentInput->setEnabled(false);
    ui->documentOutputModeCombo->setEnabled(false);
    ui->documentDocumentCombo->setEnabled(false);
    ui->documentComparisonList->setEnabled(false);
    ui->documentImportButton->setEnabled(false);
    ui->documentRefreshButton->setEnabled(false);
    updateDocumentDraftSaveAction();
    ui->documentRunStatus->setText(QStringLiteral("正在重新读取材料并撰写本章"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    ui->documentResultDetailStatus->setText(QStringLiteral("本章撰写中 · 原草稿未改动"));
    polishBadge(ui->documentResultDetailStatus, QStringLiteral("badgeBlue"));
    backendClient->expandDocumentDraftSection(
        currentDocumentResultTaskId,
        sectionIds.at(sectionIndex),
        instruction);
}

void MainWindow::reviewDocumentDraftSection()
{
    if (documentAgentRunning || currentDocumentResultTaskId.isEmpty()) {
        return;
    }

    const QJsonArray sections = currentDocumentResultContext.value(QStringLiteral("draft_sections")).toArray();
    if (sections.isEmpty()) {
        QMessageBox::information(
            this,
            QStringLiteral("审校本章"),
            QStringLiteral("当前结果没有可审校的 Markdown 草稿章节。"));
        return;
    }

    QStringList labels;
    QStringList sectionIds;
    QSet<QString> usedLabels;
    for (const QJsonValue &value : sections) {
        const QJsonObject section = value.toObject();
        const QString sectionId = section.value(QStringLiteral("id")).toString().trimmed();
        const QString heading = section.value(QStringLiteral("heading")).toString().simplified();
        const QString body = section.value(QStringLiteral("body")).toString().trimmed();
        if (sectionId.isEmpty() || heading.isEmpty() || body.isEmpty()) {
            continue;
        }
        QString label = heading;
        int duplicateIndex = 2;
        while (usedLabels.contains(label)) {
            label = QStringLiteral("%1（第 %2 节）").arg(heading).arg(duplicateIndex++);
        }
        usedLabels.insert(label);
        labels.append(label);
        sectionIds.append(sectionId);
    }
    if (labels.isEmpty()) {
        QMessageBox::information(
            this,
            QStringLiteral("审校本章"),
            QStringLiteral("当前草稿缺少可验证的章节标识或正文，不能安全审校。"));
        return;
    }

    bool sectionAccepted = false;
    const QString selectedLabel = QInputDialog::getItem(
        this,
        QStringLiteral("选择审校章节"),
        QStringLiteral("希望审校哪一章？"),
        labels,
        0,
        false,
        &sectionAccepted);
    if (!sectionAccepted) {
        return;
    }
    const int sectionIndex = labels.indexOf(selectedLabel);
    if (sectionIndex < 0 || sectionIndex >= sectionIds.size()) {
        return;
    }

    bool focusAccepted = false;
    const QString focus = QInputDialog::getMultiLineText(
        this,
        QStringLiteral("本章审校重点"),
        QStringLiteral("希望重点检查什么？（可留空）"),
        QStringLiteral("检查事实准确性、表述清晰度、前后一致性和结构；只给建议，不改写原文。"),
        &focusAccepted).trimmed();
    if (!focusAccepted) {
        return;
    }

    const QMessageBox::StandardButton choice = QMessageBox::question(
        this,
        QStringLiteral("确认审校本章"),
        QStringLiteral("将重新读取原材料，审校“%1”并返回问题、候选建议和来源。\n\n"
                       "不会修改原草稿、不会覆盖已保存文件，也不会创建新文件。")
            .arg(selectedLabel),
        QMessageBox::Yes | QMessageBox::No,
        QMessageBox::No);
    if (choice != QMessageBox::Yes) {
        return;
    }

    documentSectionReviewRunning = true;
    documentSectionDraftRunning = false;
    documentSectionRevisionRunning = false;
    documentManualRevisionRunning = false;
    documentDraftTemplateRunning = false;
    documentDraftMergeRunning = false;
    documentDraftReviewRunning = false;
    documentDraftRestoreRunning = false;
    documentAgentRunning = true;
    activeDocumentAgentTaskId.clear();
    ui->documentRunButton->setEnabled(false);
    ui->documentInput->setEnabled(false);
    ui->documentOutputModeCombo->setEnabled(false);
    ui->documentDocumentCombo->setEnabled(false);
    ui->documentComparisonList->setEnabled(false);
    ui->documentImportButton->setEnabled(false);
    ui->documentRefreshButton->setEnabled(false);
    updateDocumentDraftSaveAction();
    ui->documentRunStatus->setText(QStringLiteral("正在重新读取材料并审校所选章节"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    ui->documentResultDetailStatus->setText(QStringLiteral("本章审校中 · 原草稿未改动"));
    polishBadge(ui->documentResultDetailStatus, QStringLiteral("badgeBlue"));
    backendClient->reviewDocumentDraftSection(
        currentDocumentResultTaskId,
        sectionIds.at(sectionIndex),
        focus);
}

void MainWindow::createDocumentDraftSectionRevisionPreview()
{
    if (documentAgentRunning || currentDocumentResultTaskId.isEmpty()) {
        return;
    }

    const QString sectionId = currentDocumentResultContext
        .value(QStringLiteral("revision_target_section_id")).toString().trimmed();
    const QJsonArray suggestions = currentDocumentResultContext
        .value(QStringLiteral("revision_suggestions")).toArray();
    if (sectionId.isEmpty() || suggestions.isEmpty()) {
        QMessageBox::information(
            this,
            QStringLiteral("生成修订预览"),
            QStringLiteral("请先完成一次本章审校，并在结果中获得可追溯的候选建议。"));
        return;
    }

    QStringList labels;
    QStringList suggestionIds;
    QSet<QString> usedLabels;
    for (const QJsonValue &value : suggestions) {
        const QJsonObject suggestion = value.toObject();
        const QString suggestionId = suggestion.value(QStringLiteral("id")).toString().trimmed();
        const QString reason = suggestion.value(QStringLiteral("reason")).toString().simplified();
        const QString excerpt = suggestion.value(QStringLiteral("original_excerpt")).toString().simplified();
        if (suggestionId.isEmpty() || excerpt.isEmpty()) {
            continue;
        }
        QString label = reason.isEmpty() ? excerpt.left(54) : reason.left(72);
        if (label.isEmpty()) {
            label = suggestionId;
        }
        int duplicateIndex = 2;
        const QString baseLabel = label;
        while (usedLabels.contains(label)) {
            label = QStringLiteral("%1（建议 %2）").arg(baseLabel).arg(duplicateIndex++);
        }
        usedLabels.insert(label);
        labels.append(label);
        suggestionIds.append(suggestionId);
    }
    if (labels.isEmpty()) {
        QMessageBox::information(
            this,
            QStringLiteral("生成修订预览"),
            QStringLiteral("当前审校结果没有可安全应用的候选建议。"));
        return;
    }

    bool accepted = false;
    const QString selectedLabel = QInputDialog::getItem(
        this,
        QStringLiteral("选择候选建议"),
        QStringLiteral("将哪一条建议生成独立修订预览？"),
        labels,
        0,
        false,
        &accepted);
    if (!accepted) {
        return;
    }
    const int suggestionIndex = labels.indexOf(selectedLabel);
    if (suggestionIndex < 0 || suggestionIndex >= suggestionIds.size()) {
        return;
    }

    const QMessageBox::StandardButton choice = QMessageBox::question(
        this,
        QStringLiteral("确认生成修订预览"),
        QStringLiteral("系统将校验候选原文能否在当前章节中唯一定位，并生成一份前后差异预览。\n\n"
                       "不会调用模型，不会修改原草稿、审校任务或已保存文件。确认无误后，"
                       "你仍需通过“保存 Markdown”另存为一个新版本。"),
        QMessageBox::Yes | QMessageBox::No,
        QMessageBox::No);
    if (choice != QMessageBox::Yes) {
        return;
    }

    beginDocumentSectionRevisionPreview(QStringLiteral("正在校验建议并生成独立修订预览"));
    backendClient->createDocumentDraftSectionRevisionPreview(
        currentDocumentResultTaskId,
        suggestionIds.at(suggestionIndex));
}

void MainWindow::createDocumentDraftSectionBatchRevisionPreview()
{
    if (documentAgentRunning || currentDocumentResultTaskId.isEmpty()) {
        return;
    }

    const QJsonArray suggestions = currentDocumentResultContext
        .value(QStringLiteral("revision_suggestions")).toArray();
    if (suggestions.size() < 2) {
        QMessageBox::information(
            this,
            QStringLiteral("生成多建议预览"),
            QStringLiteral("请先完成一次本章审校，并获得至少两条可追溯的候选建议。"));
        return;
    }

    // 这是一次性、局部的勾选确认，不是主页面的常驻布局；保持工作台主体继续由 Qt Designer 管理。
    QDialog dialog(this);
    dialog.setWindowTitle(QStringLiteral("选择要合并的审校建议"));
    dialog.setMinimumSize(620, 420);
    auto *layout = new QVBoxLayout(&dialog);
    auto *hint = new QLabel(
        QStringLiteral("选择 2 至 6 条建议。系统只会合并能在同一章节中唯一定位、且彼此不重叠的片段；"
                       "出现冲突会安全停止，不会修改原草稿或已保存文件。"),
        &dialog);
    hint->setWordWrap(true);
    hint->setObjectName(QStringLiteral("mutedText"));
    layout->addWidget(hint);

    auto *suggestionList = new QListWidget(&dialog);
    suggestionList->setSelectionMode(QAbstractItemView::NoSelection);
    for (const QJsonValue &value : suggestions) {
        const QJsonObject suggestion = value.toObject();
        const QString suggestionId = suggestion.value(QStringLiteral("id")).toString().trimmed();
        const QString reason = suggestion.value(QStringLiteral("reason")).toString().simplified();
        const QString excerpt = suggestion.value(QStringLiteral("original_excerpt")).toString().simplified();
        if (suggestionId.isEmpty() || excerpt.isEmpty()) {
            continue;
        }
        const QString title = reason.isEmpty() ? excerpt.left(84) : reason.left(112);
        auto *item = new QListWidgetItem(
            QStringLiteral("%1\n原文：%2").arg(title, excerpt.left(120)),
            suggestionList);
        item->setData(Qt::UserRole, suggestionId);
        item->setFlags(item->flags() | Qt::ItemIsUserCheckable);
        item->setCheckState(Qt::Unchecked);
    }
    layout->addWidget(suggestionList, 1);

    auto *buttons = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &dialog);
    buttons->button(QDialogButtonBox::Ok)->setText(QStringLiteral("生成预览"));
    buttons->button(QDialogButtonBox::Cancel)->setText(QStringLiteral("取消"));
    connect(buttons, &QDialogButtonBox::accepted, &dialog, [&dialog, suggestionList]() {
        int checkedCount = 0;
        for (int index = 0; index < suggestionList->count(); ++index) {
            if (suggestionList->item(index)->checkState() == Qt::Checked) {
                ++checkedCount;
            }
        }
        if (checkedCount < 2 || checkedCount > 6) {
            QMessageBox::information(
                &dialog,
                QStringLiteral("选择数量不符合要求"),
                QStringLiteral("请勾选 2 至 6 条建议后再生成预览。"));
            return;
        }
        dialog.accept();
    });
    connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    layout->addWidget(buttons);
    if (dialog.exec() != QDialog::Accepted) {
        return;
    }

    QStringList suggestionIds;
    for (int index = 0; index < suggestionList->count(); ++index) {
        const QListWidgetItem *item = suggestionList->item(index);
        if (item->checkState() == Qt::Checked) {
            suggestionIds.append(item->data(Qt::UserRole).toString());
        }
    }
    if (suggestionIds.size() < 2 || suggestionIds.size() > 6) {
        return;
    }

    const QMessageBox::StandardButton choice = QMessageBox::question(
        this,
        QStringLiteral("确认生成多建议预览"),
        QStringLiteral("系统将校验 %1 条建议在当前章节中能否唯一定位且互不重叠，再按原文位置合并。\n\n"
                       "不会调用模型，不会修改原草稿、审校任务或已保存文件；确认后仍需另存为新版本。"
        ).arg(suggestionIds.size()),
        QMessageBox::Yes | QMessageBox::No,
        QMessageBox::No);
    if (choice != QMessageBox::Yes) {
        return;
    }

    beginDocumentSectionRevisionPreview(
        QStringLiteral("正在校验 %1 条建议并生成合并修订预览").arg(suggestionIds.size()));
    backendClient->createDocumentDraftSectionBatchRevisionPreview(
        currentDocumentResultTaskId,
        suggestionIds);
}

void MainWindow::createDocumentDraftSectionManualRevisionPreview()
{
    if (documentAgentRunning || currentDocumentResultTaskId.isEmpty()) {
        return;
    }

    const QJsonArray sections = currentDocumentResultContext
                                    .value(QStringLiteral("draft_sections"))
                                    .toArray();
    if (sections.isEmpty()) {
        QMessageBox::information(this,
                                 QStringLiteral("手动修订本章"),
                                 QStringLiteral("当前结果没有可编辑的 Markdown 草稿章节。"));
        return;
    }

    QStringList choices;
    QList<QJsonObject> sectionObjects;
    for (const QJsonValue &value : sections) {
        const QJsonObject section = value.toObject();
        const QString sectionId = section.value(QStringLiteral("id")).toString().trimmed();
        const QString heading = section.value(QStringLiteral("heading")).toString().trimmed();
        const QString body = section.value(QStringLiteral("body")).toString().trimmed();
        if (sectionId.isEmpty() || heading.isEmpty() || body.isEmpty()) {
            continue;
        }
        choices.append(QStringLiteral("%1  ·  %2").arg(heading, sectionId));
        sectionObjects.append(section);
    }
    if (sectionObjects.isEmpty()) {
        QMessageBox::information(this,
                                 QStringLiteral("手动修订本章"),
                                 QStringLiteral("当前草稿没有可安全绑定的章节身份。"));
        return;
    }

    bool selected = false;
    const QString choice = QInputDialog::getItem(
        this,
        QStringLiteral("选择章节"),
        QStringLiteral("请选择要手动修订的章节："),
        choices,
        0,
        false,
        &selected);
    if (!selected) {
        return;
    }
    const int selectedIndex = choices.indexOf(choice);
    if (selectedIndex < 0 || selectedIndex >= sectionObjects.size()) {
        return;
    }
    const QJsonObject section = sectionObjects.at(selectedIndex);
    const QString sectionId = section.value(QStringLiteral("id")).toString();
    const QString heading = section.value(QStringLiteral("heading")).toString();
    const QString originalBody = section.value(QStringLiteral("body")).toString();

    // 稳定工作台仍放在 .ui；这里只是一次性编辑器，因此使用独立对话框给正文足够阅读空间。
    QDialog dialog(this);
    dialog.setWindowTitle(QStringLiteral("手动修订本章"));
    dialog.setModal(true);
    dialog.resize(920, 680);
    dialog.setMinimumSize(720, 500);
    auto *layout = new QVBoxLayout(&dialog);
    layout->setContentsMargins(24, 22, 24, 20);
    layout->setSpacing(12);

    auto *title = new QLabel(QStringLiteral("手动修订：%1").arg(heading), &dialog);
    title->setStyleSheet(QStringLiteral("font-size:20px;font-weight:800;color:#0F172A;"));
    layout->addWidget(title);
    auto *hint = new QLabel(
        QStringLiteral("此操作只建立独立预览，不改写原草稿或文件。提交后请重新核验材料来源；"
                       "在核验通过前，保存 Markdown 会保持禁用。"),
        &dialog);
    hint->setWordWrap(true);
    hint->setStyleSheet(QStringLiteral("color:#9A3412;background:#FFF7ED;border-left:3px solid #FB923C;padding:9px 11px;"));
    layout->addWidget(hint);

    auto *editor = new QPlainTextEdit(&dialog);
    editor->setObjectName(QStringLiteral("documentManualRevisionEditor"));
    editor->setPlainText(originalBody);
    editor->setLineWrapMode(QPlainTextEdit::WidgetWidth);
    editor->setPlaceholderText(QStringLiteral("输入修订后的章节正文"));
    layout->addWidget(editor, 1);

    auto *meta = new QLabel(&dialog);
    meta->setObjectName(QStringLiteral("tinyText"));
    layout->addWidget(meta);
    auto *buttons = new QDialogButtonBox(QDialogButtonBox::Cancel, &dialog);
    auto *previewButton = buttons->addButton(QStringLiteral("建立待核验预览"), QDialogButtonBox::AcceptRole);
    layout->addWidget(buttons);
    connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    connect(previewButton, &QPushButton::clicked, &dialog, &QDialog::accept);

    const auto updateEditorState = [editor, meta, previewButton, originalBody]() {
        const QString revised = editor->toPlainText().trimmed();
        const int length = revised.size();
        meta->setText(
            length > 1500
                ? QStringLiteral("%1 / 1500 个字符 · 请缩短后再建立预览").arg(length)
                : revised == originalBody.trimmed()
                ? QStringLiteral("%1 / 1500 个字符 · 修改正文后才能建立预览").arg(length)
                : QStringLiteral("%1 / 1500 个字符 · 提交后需要重新核验来源").arg(length));
        previewButton->setEnabled(!revised.isEmpty() && length <= 1500 && revised != originalBody.trimmed());
    };
    connect(editor, &QPlainTextEdit::textChanged, &dialog, updateEditorState);
    updateEditorState();

    if (dialog.exec() != QDialog::Accepted) {
        return;
    }
    const QString revisedBody = editor->toPlainText().trimmed();
    if (revisedBody.isEmpty() || revisedBody.size() > 1500 || revisedBody == originalBody.trimmed()) {
        return;
    }

    beginDocumentManualRevisionPreview(QStringLiteral("正在建立待来源核验的手动修订预览"));
    backendClient->createDocumentDraftSectionManualRevisionPreview(
        currentDocumentResultTaskId,
        sectionId,
        revisedBody);
}

void MainWindow::createDocumentDraftTemplatePreview()
{
    if (documentAgentRunning || currentDocumentResultTaskId.isEmpty()) {
        return;
    }
    if (currentDocumentResultContext.value(QStringLiteral("draft_sections")).toArray().isEmpty()) {
        QMessageBox::information(
            this,
            QStringLiteral("模板与交付"),
            QStringLiteral("当前结果没有可用于交付的 Markdown 草稿章节。"));
        return;
    }

    const QString verificationState = currentDocumentResultContext
                                        .value(QStringLiteral("draft_verification_state"))
                                        .toString();
    if (verificationState == QStringLiteral("requires_review")
        || verificationState == QStringLiteral("reviewed_with_questions")) {
        QMessageBox::information(
            this,
            QStringLiteral("请先完成事实核验"),
            QStringLiteral("模板交付只能使用已核验草稿。请先处理当前待确认事实并再次核验，"
                           "避免把手动修改的未验证内容包装成正式文档。"));
        return;
    }

    // 模板选择放在独立工作区，而非继续挤压结果详情。第一版只有固定模板和真实边界说明，
    // 用户可以先看清结构与缺失项，再建立不会写盘的交付预览。
    struct TemplateOption {
        QString id;
        QString name;
        QString summary;
        QString sections;
    };
    const QList<TemplateOption> options{
        {QStringLiteral("project_proposal"),
         QStringLiteral("项目方案"),
         QStringLiteral("将已验证内容组织为方案背景、目标、计划、交付与风险。"),
         QStringLiteral("项目背景\n目标与范围\n实施计划\n交付与验收\n风险与依赖")},
        {QStringLiteral("product_requirements"),
         QStringLiteral("产品需求文档"),
         QStringLiteral("面向产品与研发协作，突出场景、功能、非功能和验收边界。"),
         QStringLiteral("背景与目标\n用户与场景\n功能需求\n非功能需求\n验收标准\n风险与待确认")},
        {QStringLiteral("meeting_minutes"),
         QStringLiteral("会议纪要"),
         QStringLiteral("将已有讨论材料整理为主题、结论、行动项与待跟进事项。"),
         QStringLiteral("会议主题与背景\n讨论与结论\n行动项\n待确认与跟进")},
    };

    QDialog dialog(this);
    dialog.setWindowTitle(QStringLiteral("模板与交付"));
    dialog.setModal(true);
    dialog.resize(940, 600);
    dialog.setMinimumSize(760, 480);
    auto *layout = new QVBoxLayout(&dialog);
    layout->setContentsMargins(26, 24, 26, 22);
    layout->setSpacing(14);

    auto *title = new QLabel(QStringLiteral("选择交付模板"), &dialog);
    title->setStyleSheet(QStringLiteral("font-size:22px;font-weight:800;color:#0F172A;"));
    layout->addWidget(title);
    auto *hint = new QLabel(
        QStringLiteral("模板只重组当前已核验草稿的章节与来源，不会调用模型、不补写未知事实，也不会保存文件。"
                       "未匹配的结构会在预览中明确标记，确认后再另存 Markdown。"),
        &dialog);
    hint->setWordWrap(true);
    hint->setStyleSheet(QStringLiteral("color:#475569;"));
    layout->addWidget(hint);

    auto *splitter = new QSplitter(Qt::Horizontal, &dialog);
    splitter->setChildrenCollapsible(false);
    auto *templateList = new QListWidget(splitter);
    templateList->setObjectName(QStringLiteral("documentTemplateList"));
    templateList->setMinimumWidth(260);
    templateList->setSpacing(6);
    for (const TemplateOption &option : options) {
        auto *item = new QListWidgetItem(option.name, templateList);
        item->setData(Qt::UserRole, option.id);
        item->setData(Qt::UserRole + 1, option.summary);
        item->setData(Qt::UserRole + 2, option.sections);
        item->setToolTip(option.summary);
        item->setSizeHint(QSize(238, 54));
    }

    auto *detailPane = new QWidget(splitter);
    auto *detailLayout = new QVBoxLayout(detailPane);
    detailLayout->setContentsMargins(18, 4, 4, 4);
    detailLayout->setSpacing(12);
    auto *templateName = new QLabel(detailPane);
    templateName->setStyleSheet(QStringLiteral("font-size:18px;font-weight:750;color:#0F172A;"));
    detailLayout->addWidget(templateName);
    auto *templateSummary = new QLabel(detailPane);
    templateSummary->setWordWrap(true);
    templateSummary->setStyleSheet(QStringLiteral("color:#475569;line-height:1.5;"));
    detailLayout->addWidget(templateSummary);
    auto *sectionCaption = new QLabel(QStringLiteral("交付结构"), detailPane);
    sectionCaption->setStyleSheet(QStringLiteral("font-size:13px;font-weight:700;color:#334155;"));
    detailLayout->addWidget(sectionCaption);
    auto *templateSections = new QLabel(detailPane);
    templateSections->setWordWrap(true);
    templateSections->setStyleSheet(
        QStringLiteral("padding:14px;background:#F8FAFC;border:1px solid #DCE7F5;border-radius:8px;color:#334155;line-height:1.8;"));
    detailLayout->addWidget(templateSections);
    auto *boundary = new QLabel(
        QStringLiteral("交付边界：本轮只生成 Markdown 交付预览。DOCX / PDF 渲染、企业自定义模板和多草稿合并将在后续独立阶段实现。"),
        detailPane);
    boundary->setWordWrap(true);
    boundary->setStyleSheet(
        QStringLiteral("padding:11px 13px;background:#EEF6FF;border-left:3px solid #60A5FA;color:#1E40AF;"));
    detailLayout->addWidget(boundary);
    detailLayout->addStretch(1);
    splitter->addWidget(templateList);
    splitter->addWidget(detailPane);
    splitter->setSizes({310, 570});
    layout->addWidget(splitter, 1);

    auto *buttons = new QDialogButtonBox(QDialogButtonBox::Cancel, &dialog);
    auto *previewButton = buttons->addButton(QStringLiteral("生成交付预览"), QDialogButtonBox::AcceptRole);
    previewButton->setObjectName(QStringLiteral("primaryButton"));
    layout->addWidget(buttons);
    connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    connect(previewButton, &QPushButton::clicked, &dialog, &QDialog::accept);

    const auto updateTemplateDetail = [templateList, templateName, templateSummary, templateSections, previewButton]() {
        const QListWidgetItem *item = templateList->currentItem();
        const bool available = item != nullptr;
        templateName->setText(available ? item->text() : QStringLiteral("请选择一个模板"));
        templateSummary->setText(available ? item->data(Qt::UserRole + 1).toString() : QString());
        templateSections->setText(
            available ? item->data(Qt::UserRole + 2).toString().replace(QStringLiteral("\n"), QStringLiteral("\n• ")).prepend(QStringLiteral("• "))
                      : QStringLiteral("选择后会展示固定交付结构。"));
        previewButton->setEnabled(available);
    };
    connect(templateList, &QListWidget::currentItemChanged, &dialog, updateTemplateDetail);
    templateList->setCurrentRow(0);
    updateTemplateDetail();

    if (dialog.exec() != QDialog::Accepted || !templateList->currentItem()) {
        return;
    }
    const QString templateId = templateList->currentItem()->data(Qt::UserRole).toString();
    if (templateId.isEmpty()) {
        return;
    }

    beginDocumentDraftTemplatePreview(QStringLiteral("正在按固定模板建立交付预览"));
    backendClient->createDocumentDraftTemplatePreview(currentDocumentResultTaskId, templateId);
}

void MainWindow::beginDocumentDraftTemplatePreview(const QString &statusText)
{
    documentDraftTemplateRunning = true;
    documentDraftMergeRunning = false;
    documentSectionDraftRunning = false;
    documentSectionReviewRunning = false;
    documentSectionRevisionRunning = false;
    documentManualRevisionRunning = false;
    documentDraftReviewRunning = false;
    documentDraftRestoreRunning = false;
    documentAgentRunning = true;
    activeDocumentAgentTaskId.clear();
    ui->documentRunButton->setEnabled(false);
    ui->documentInput->setEnabled(false);
    ui->documentOutputModeCombo->setEnabled(false);
    ui->documentDocumentCombo->setEnabled(false);
    ui->documentComparisonList->setEnabled(false);
    ui->documentImportButton->setEnabled(false);
    ui->documentRefreshButton->setEnabled(false);
    updateDocumentDraftSaveAction();
    ui->documentRunStatus->setText(statusText);
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    ui->documentResultDetailStatus->setText(QStringLiteral("模板化交付预览建立中 · 原草稿未改动"));
    polishBadge(ui->documentResultDetailStatus, QStringLiteral("badgeBlue"));
}

void MainWindow::createDocumentDraftMergePreview()
{
    if (documentAgentRunning || currentDocumentResultTaskId.isEmpty() || documentDraftMergeLoading) {
        return;
    }
    const QJsonObject version = currentDocumentResultContext
                                    .value(QStringLiteral("draft_version"))
                                    .toObject();
    const QString verificationState = currentDocumentResultContext
                                        .value(QStringLiteral("draft_verification_state"))
                                        .toString();
    const QSet<QString> mergeableKinds{
        QStringLiteral("base_draft"),
        QStringLiteral("revision_preview"),
        QStringLiteral("revision_batch_preview"),
        QStringLiteral("restored_preview"),
        QStringLiteral("merge_preview"),
    };
    if (verificationState != QStringLiteral("verified") || !mergeableKinds.contains(version.value(QStringLiteral("kind")).toString())) {
        QMessageBox::information(
            this,
            QStringLiteral("章节合并"),
            QStringLiteral("只有已完成事实核验的完整草稿版本可以参与章节合并。"
                           "单章节、审校和模板重排预览请先恢复为完整版本后再继续。"));
        return;
    }

    // 先请求同根候选而不是让用户输入任务 ID。候选、共同祖先和正文均由后端恢复，
    // Qt 只负责让用户作出产品层面的版本和冲突选择。
    documentDraftMergeLoading = true;
    updateDocumentSectionDraftAction();
    ui->documentRunStatus->setText(QStringLiteral("正在加载同根草稿版本…"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    backendClient->requestDocumentDraftMergeCandidates(currentDocumentResultTaskId);
}

void MainWindow::handleDocumentDraftMergeCandidatesReceived(const QJsonObject &result)
{
    documentDraftMergeLoading = false;
    updateDocumentSectionDraftAction();
    const QJsonArray candidates = result.value(QStringLiteral("candidates")).toArray();
    if (candidates.isEmpty()) {
        ui->documentRunStatus->setText(QStringLiteral("没有可合并的同根版本"));
        polishBadge(ui->documentRunStatus, QStringLiteral("badgeOrange"));
        QMessageBox::information(
            this,
            QStringLiteral("章节合并"),
            QStringLiteral("当前草稿没有其他已核验的完整同根版本。"
                           "完成一次受控修订或从历史版本恢复预览后，可在这里进行章节合并。"));
        return;
    }

    QDialog dialog(this);
    dialog.setWindowTitle(QStringLiteral("选择要合并的版本"));
    dialog.setModal(true);
    dialog.resize(840, 540);
    dialog.setMinimumSize(680, 440);
    auto *layout = new QVBoxLayout(&dialog);
    layout->setContentsMargins(26, 24, 26, 22);
    layout->setSpacing(12);
    auto *title = new QLabel(QStringLiteral("选择同一根草稿的另一个版本"), &dialog);
    title->setStyleSheet(QStringLiteral("font-size:20px;font-weight:800;color:#0F172A;"));
    layout->addWidget(title);
    auto *hint = new QLabel(
        QStringLiteral("下一步会先计算共同祖先与章节冲突。系统只自动采用未发生双边修改的内容，"
                       "冲突必须由你确认，不会直接覆盖当前版本。"),
        &dialog);
    hint->setWordWrap(true);
    hint->setObjectName(QStringLiteral("subText"));
    layout->addWidget(hint);

    auto *splitter = new QSplitter(Qt::Horizontal, &dialog);
    splitter->setChildrenCollapsible(false);
    auto *versionList = new QListWidget(splitter);
    versionList->setMinimumWidth(280);
    auto *detailPane = new QWidget(splitter);
    auto *detailLayout = new QVBoxLayout(detailPane);
    detailLayout->setContentsMargins(18, 4, 4, 4);
    detailLayout->setSpacing(10);
    auto *versionLabel = new QLabel(detailPane);
    versionLabel->setStyleSheet(QStringLiteral("font-size:17px;font-weight:750;color:#0F172A;"));
    detailLayout->addWidget(versionLabel);
    auto *versionTitle = new QLabel(detailPane);
    versionTitle->setWordWrap(true);
    versionTitle->setStyleSheet(QStringLiteral("color:#334155;font-size:14px;"));
    detailLayout->addWidget(versionTitle);
    auto *boundary = new QLabel(
        QStringLiteral("合并边界：只合并同根、已核验的完整草稿快照。不会调用模型、读取材料、"
                       "修改源版本或写入文件；合并完成后仍须另存 Markdown。"),
        detailPane);
    boundary->setWordWrap(true);
    boundary->setStyleSheet(QStringLiteral("padding:12px;background:#EEF6FF;border-left:3px solid #60A5FA;color:#1E40AF;"));
    detailLayout->addWidget(boundary);
    detailLayout->addStretch(1);
    for (const QJsonValue &value : candidates) {
        const QJsonObject candidate = value.toObject();
        const QString taskId = candidate.value(QStringLiteral("task_id")).toString();
        if (taskId.isEmpty()) {
            continue;
        }
        auto *item = new QListWidgetItem(candidate.value(QStringLiteral("label")).toString(), versionList);
        item->setData(Qt::UserRole, taskId);
        item->setData(Qt::UserRole + 1, candidate.value(QStringLiteral("draft_title")).toString());
        item->setData(Qt::UserRole + 2, candidate.value(QStringLiteral("kind")).toString());
        item->setSizeHint(QSize(262, 54));
    }
    splitter->addWidget(versionList);
    splitter->addWidget(detailPane);
    splitter->setSizes({320, 470});
    layout->addWidget(splitter, 1);

    auto *buttons = new QDialogButtonBox(QDialogButtonBox::Cancel, &dialog);
    auto *nextButton = buttons->addButton(QStringLiteral("查看合并计划"), QDialogButtonBox::AcceptRole);
    nextButton->setObjectName(QStringLiteral("primaryButton"));
    layout->addWidget(buttons);
    connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    connect(nextButton, &QPushButton::clicked, &dialog, &QDialog::accept);
    const auto updateCandidateDetail = [versionList, versionLabel, versionTitle, nextButton]() {
        const QListWidgetItem *item = versionList->currentItem();
        const bool available = item != nullptr;
        versionLabel->setText(available ? item->text() : QStringLiteral("请选择一个版本"));
        versionTitle->setText(
            available
                ? QStringLiteral("草稿标题：%1\n版本类型：%2")
                      .arg(item->data(Qt::UserRole + 1).toString(), item->data(Qt::UserRole + 2).toString())
                : QStringLiteral("选择后会计算共同祖先和章节冲突。"));
        nextButton->setEnabled(available);
    };
    connect(versionList, &QListWidget::currentItemChanged, &dialog, updateCandidateDetail);
    versionList->setCurrentRow(0);
    updateCandidateDetail();

    if (dialog.exec() != QDialog::Accepted || !versionList->currentItem()) {
        return;
    }
    const QString otherTaskId = versionList->currentItem()->data(Qt::UserRole).toString();
    if (otherTaskId.isEmpty()) {
        return;
    }
    documentDraftMergeLoading = true;
    updateDocumentSectionDraftAction();
    ui->documentRunStatus->setText(QStringLiteral("正在计算共同祖先与章节冲突…"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    backendClient->requestDocumentDraftMergePlan(currentDocumentResultTaskId, otherTaskId);
}

void MainWindow::handleDocumentDraftMergePlanReceived(const QJsonObject &result)
{
    documentDraftMergeLoading = false;
    updateDocumentSectionDraftAction();
    showDocumentDraftMergePlanDialog(result);
}

void MainWindow::handleDocumentDraftMergeFailed(const QString &message)
{
    documentDraftMergeLoading = false;
    updateDocumentSectionDraftAction();
    ui->documentRunStatus->setText(QStringLiteral("章节合并计划不可用"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeOrange"));
    QMessageBox::warning(
        this,
        QStringLiteral("章节合并暂不可用"),
        message.isEmpty() ? QStringLiteral("后端没有返回可展示的合并计划。") : message);
}

void MainWindow::showDocumentDraftMergePlanDialog(const QJsonObject &result)
{
    const QJsonArray conflicts = result.value(QStringLiteral("conflicts")).toArray();
    const QString primaryTaskId = result.value(QStringLiteral("primary_task_id")).toString();
    const QString secondaryTaskId = result.value(QStringLiteral("secondary_task_id")).toString();
    if (primaryTaskId.isEmpty() || secondaryTaskId.isEmpty()) {
        return;
    }

    QDialog dialog(this);
    dialog.setWindowTitle(QStringLiteral("章节合并计划"));
    dialog.setModal(true);
    dialog.resize(1060, 700);
    dialog.setMinimumSize(820, 540);
    auto *layout = new QVBoxLayout(&dialog);
    layout->setContentsMargins(26, 24, 26, 22);
    layout->setSpacing(12);
    auto *title = new QLabel(QStringLiteral("确认章节合并"), &dialog);
    title->setStyleSheet(QStringLiteral("font-size:21px;font-weight:800;color:#0F172A;"));
    layout->addWidget(title);
    auto *summary = new QLabel(
        QStringLiteral("共同祖先：%1\n可自动合并章节：%2 个；需要你确认的冲突：%3 项。")
            .arg(result.value(QStringLiteral("common_ancestor_task_id")).toString(),
                 QString::number(result.value(QStringLiteral("automatic_section_count")).toInt()),
                 QString::number(conflicts.size())),
        &dialog);
    summary->setWordWrap(true);
    summary->setObjectName(QStringLiteral("subText"));
    layout->addWidget(summary);

    QHash<QString, QString> decisions;
    if (conflicts.isEmpty()) {
        auto *notice = new QLabel(
            QStringLiteral("两个版本没有同时修改同一章节。系统将只采用确定性的自动合并结果，"
                           "仍不会修改源版本或保存文件。"),
            &dialog);
        notice->setWordWrap(true);
        notice->setStyleSheet(QStringLiteral("padding:16px;background:#F0FDF4;border:1px solid #BBE7D0;color:#166534;"));
        layout->addWidget(notice, 1);
    } else {
        auto *splitter = new QSplitter(Qt::Horizontal, &dialog);
        splitter->setChildrenCollapsible(false);
        auto *conflictList = new QListWidget(splitter);
        conflictList->setMinimumWidth(260);
        for (const QJsonValue &value : conflicts) {
            const QJsonObject conflict = value.toObject();
            const QString conflictId = conflict.value(QStringLiteral("conflict_id")).toString();
            const QString heading = conflict.value(QStringLiteral("heading")).toString();
            const QString kind = conflict.value(QStringLiteral("kind")).toString();
            auto *item = new QListWidgetItem(
                conflictId == QStringLiteral("title")
                    ? QStringLiteral("文档标题冲突")
                    : QStringLiteral("%1 · %2").arg(heading, kind),
                conflictList);
            item->setData(Qt::UserRole, conflict);
            item->setData(Qt::UserRole + 1, conflictId);
            item->setSizeHint(QSize(248, 52));
        }

        auto *detailPane = new QWidget(splitter);
        auto *detailLayout = new QVBoxLayout(detailPane);
        detailLayout->setContentsMargins(18, 4, 4, 4);
        detailLayout->setSpacing(10);
        auto *conflictTitle = new QLabel(detailPane);
        conflictTitle->setStyleSheet(QStringLiteral("font-size:17px;font-weight:750;color:#0F172A;"));
        detailLayout->addWidget(conflictTitle);
        auto *conflictHint = new QLabel(detailPane);
        conflictHint->setWordWrap(true);
        conflictHint->setStyleSheet(QStringLiteral("color:#64748B;"));
        detailLayout->addWidget(conflictHint);
        auto *readTabs = new QTabWidget(detailPane);
        const auto addReadTab = [readTabs, detailPane](const QString &caption) {
            auto *reader = new QPlainTextEdit(detailPane);
            reader->setReadOnly(true);
            reader->setUndoRedoEnabled(false);
            reader->setLineWrapMode(QPlainTextEdit::WidgetWidth);
            readTabs->addTab(reader, caption);
            return reader;
        };
        auto *baseReader = addReadTab(QStringLiteral("共同祖先"));
        auto *primaryReader = addReadTab(result.value(QStringLiteral("primary_label")).toString());
        auto *secondaryReader = addReadTab(result.value(QStringLiteral("secondary_label")).toString());
        detailLayout->addWidget(readTabs, 1);
        auto *choiceLabel = new QLabel(QStringLiteral("本项采用"), detailPane);
        choiceLabel->setStyleSheet(QStringLiteral("font-weight:700;color:#334155;"));
        detailLayout->addWidget(choiceLabel);
        auto *choiceCombo = new QComboBox(detailPane);
        detailLayout->addWidget(choiceCombo);
        splitter->addWidget(conflictList);
        splitter->addWidget(detailPane);
        splitter->setSizes({310, 690});
        layout->addWidget(splitter, 1);

        const auto updateConflictDetail = [&]() {
            const QListWidgetItem *item = conflictList->currentItem();
            if (item == nullptr) {
                return;
            }
            const QJsonObject conflict = item->data(Qt::UserRole).toJsonObject();
            const QString conflictId = item->data(Qt::UserRole + 1).toString();
            const QString heading = conflict.value(QStringLiteral("heading")).toString();
            conflictTitle->setText(
                conflictId == QStringLiteral("title")
                    ? QStringLiteral("文档标题冲突")
                    : heading.isEmpty() ? QStringLiteral("章节冲突") : QStringLiteral("章节：%1").arg(heading));
            conflictHint->setText(
                QStringLiteral("两边都对这一项做了不同修改。请明确选择保留当前版本、采用候选版本，"
                               "或回到共同祖先；选择不会修改任何源草稿。"));
            const auto readable = [](const QString &text) {
                return text.isEmpty() ? QStringLiteral("（此版本没有该章节）") : text;
            };
            baseReader->setPlainText(readable(conflict.value(QStringLiteral("base_text")).toString()));
            primaryReader->setPlainText(readable(conflict.value(QStringLiteral("primary_text")).toString()));
            secondaryReader->setPlainText(readable(conflict.value(QStringLiteral("secondary_text")).toString()));
            const QSignalBlocker blocker(choiceCombo);
            choiceCombo->clear();
            choiceCombo->addItem(QStringLiteral("请选择处理方式"), QString());
            choiceCombo->addItem(QStringLiteral("保留当前版本"), QStringLiteral("primary"));
            choiceCombo->addItem(QStringLiteral("采用候选版本"), QStringLiteral("secondary"));
            if (!conflict.value(QStringLiteral("base_text")).toString().isEmpty()) {
                choiceCombo->addItem(QStringLiteral("回到共同祖先"), QStringLiteral("base"));
            }
            const QString selected = decisions.value(conflictId);
            const int selectedIndex = choiceCombo->findData(selected);
            choiceCombo->setCurrentIndex(selectedIndex >= 0 ? selectedIndex : 0);
        };
        connect(conflictList, &QListWidget::currentItemChanged, &dialog, updateConflictDetail);
        connect(choiceCombo,
                qOverload<int>(&QComboBox::currentIndexChanged),
                &dialog,
                [&]() {
                    const QListWidgetItem *item = conflictList->currentItem();
                    if (item == nullptr) {
                        return;
                    }
                    const QString conflictId = item->data(Qt::UserRole + 1).toString();
                    const QString choice = choiceCombo->currentData().toString();
                    if (choice.isEmpty()) {
                        decisions.remove(conflictId);
                    } else {
                        decisions.insert(conflictId, choice);
                    }
                });
        conflictList->setCurrentRow(0);
        updateConflictDetail();
    }

    auto *buttons = new QDialogButtonBox(QDialogButtonBox::Cancel, &dialog);
    auto *mergeButton = buttons->addButton(QStringLiteral("建立合并预览"), QDialogButtonBox::AcceptRole);
    mergeButton->setObjectName(QStringLiteral("primaryButton"));
    layout->addWidget(buttons);
    connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    connect(mergeButton,
            &QPushButton::clicked,
            &dialog,
            [&]() {
                for (const QJsonValue &value : conflicts) {
                    const QString conflictId = value.toObject().value(QStringLiteral("conflict_id")).toString();
                    if (conflictId.isEmpty() || decisions.value(conflictId).isEmpty()) {
                        QMessageBox::information(
                            &dialog,
                            QStringLiteral("请完成冲突选择"),
                            QStringLiteral("每一项冲突都需要明确选择后才能建立合并预览。"));
                        return;
                    }
                }
                dialog.accept();
            });
    if (dialog.exec() != QDialog::Accepted) {
        return;
    }

    QJsonArray resolutions;
    for (const QJsonValue &value : conflicts) {
        const QString conflictId = value.toObject().value(QStringLiteral("conflict_id")).toString();
        QJsonObject resolution;
        resolution.insert(QStringLiteral("conflict_id"), conflictId);
        resolution.insert(QStringLiteral("choice"), decisions.value(conflictId));
        resolutions.append(resolution);
    }
    beginDocumentDraftMergePreview(QStringLiteral("正在建立章节合并预览"));
    backendClient->createDocumentDraftMergePreview(primaryTaskId, secondaryTaskId, resolutions);
}

void MainWindow::beginDocumentDraftMergePreview(const QString &statusText)
{
    documentDraftMergeLoading = false;
    documentDraftMergeRunning = true;
    documentDraftTemplateRunning = false;
    documentSectionDraftRunning = false;
    documentSectionReviewRunning = false;
    documentSectionRevisionRunning = false;
    documentManualRevisionRunning = false;
    documentDraftReviewRunning = false;
    documentDraftRestoreRunning = false;
    documentAgentRunning = true;
    activeDocumentAgentTaskId.clear();
    ui->documentRunButton->setEnabled(false);
    ui->documentInput->setEnabled(false);
    ui->documentOutputModeCombo->setEnabled(false);
    ui->documentDocumentCombo->setEnabled(false);
    ui->documentComparisonList->setEnabled(false);
    ui->documentImportButton->setEnabled(false);
    ui->documentRefreshButton->setEnabled(false);
    updateDocumentDraftSaveAction();
    ui->documentRunStatus->setText(statusText);
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    ui->documentResultDetailStatus->setText(QStringLiteral("章节合并预览建立中 · 原版本未改动"));
    polishBadge(ui->documentResultDetailStatus, QStringLiteral("badgeBlue"));
}

void MainWindow::beginDocumentManualRevisionPreview(const QString &statusText)
{
    documentManualRevisionRunning = true;
    documentDraftTemplateRunning = false;
    documentDraftMergeRunning = false;
    documentSectionDraftRunning = false;
    documentSectionReviewRunning = false;
    documentSectionRevisionRunning = false;
    documentDraftReviewRunning = false;
    documentDraftRestoreRunning = false;
    documentAgentRunning = true;
    activeDocumentAgentTaskId.clear();
    ui->documentRunButton->setEnabled(false);
    ui->documentInput->setEnabled(false);
    ui->documentOutputModeCombo->setEnabled(false);
    ui->documentDocumentCombo->setEnabled(false);
    ui->documentComparisonList->setEnabled(false);
    ui->documentImportButton->setEnabled(false);
    ui->documentRefreshButton->setEnabled(false);
    updateDocumentDraftSaveAction();
    ui->documentRunStatus->setText(statusText);
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    ui->documentResultDetailStatus->setText(QStringLiteral("手动修订预览建立中 · 原草稿未改动"));
    polishBadge(ui->documentResultDetailStatus, QStringLiteral("badgeBlue"));
}

void MainWindow::beginDocumentSectionRevisionPreview(const QString &statusText)
{
    documentSectionRevisionRunning = true;
    documentDraftTemplateRunning = false;
    documentDraftMergeRunning = false;
    documentSectionDraftRunning = false;
    documentSectionReviewRunning = false;
    documentManualRevisionRunning = false;
    documentDraftReviewRunning = false;
    documentDraftRestoreRunning = false;
    documentAgentRunning = true;
    activeDocumentAgentTaskId.clear();
    ui->documentRunButton->setEnabled(false);
    ui->documentInput->setEnabled(false);
    ui->documentOutputModeCombo->setEnabled(false);
    ui->documentDocumentCombo->setEnabled(false);
    ui->documentComparisonList->setEnabled(false);
    ui->documentImportButton->setEnabled(false);
    ui->documentRefreshButton->setEnabled(false);
    updateDocumentDraftSaveAction();
    ui->documentRunStatus->setText(statusText);
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    ui->documentResultDetailStatus->setText(QStringLiteral("修订预览生成中 · 原草稿未改动"));
    polishBadge(ui->documentResultDetailStatus, QStringLiteral("badgeBlue"));
}

void MainWindow::restoreDocumentDraftPreview()
{
    if (documentAgentRunning || currentDocumentResultTaskId.isEmpty()) {
        return;
    }
    if (currentDocumentResultContext.value(QStringLiteral("draft_sections")).toArray().isEmpty()) {
        QMessageBox::information(
            this,
            QStringLiteral("恢复草稿预览"),
            QStringLiteral("当前结果没有可恢复的 Markdown 草稿快照。"));
        return;
    }

    const QJsonObject version = currentDocumentResultContext
                                    .value(QStringLiteral("draft_version"))
                                    .toObject();
    const QString versionLabel = version.value(QStringLiteral("label")).toString();
    const QMessageBox::StandardButton choice = QMessageBox::question(
        this,
        QStringLiteral("确认建立恢复预览"),
        QStringLiteral("将从当前%1建立一份新的独立草稿预览。\n\n"
                       "不会调用模型、不会重新读取材料、不会修改历史任务，也不会覆盖任何已保存文件。"
                       "确认后仍需通过“保存 Markdown”另存为新的版本。")
            .arg(versionLabel.isEmpty() ? QStringLiteral("草稿快照")
                                        : QStringLiteral("版本“%1”").arg(versionLabel)),
        QMessageBox::Yes | QMessageBox::No,
        QMessageBox::No);
    if (choice != QMessageBox::Yes) {
        return;
    }

    beginDocumentDraftRestorePreview(QStringLiteral("正在从当前历史快照建立独立恢复预览"));
    backendClient->restoreDocumentDraftPreview(currentDocumentResultTaskId);
}

void MainWindow::beginDocumentDraftRestorePreview(const QString &statusText)
{
    // 恢复任务独立于当前详情：失败时仍保留用户正在看的旧草稿，完成后才原子切换到新预览。
    documentDraftRestoreRunning = true;
    documentDraftTemplateRunning = false;
    documentDraftMergeRunning = false;
    documentSectionDraftRunning = false;
    documentSectionReviewRunning = false;
    documentSectionRevisionRunning = false;
    documentManualRevisionRunning = false;
    documentDraftReviewRunning = false;
    documentAgentRunning = true;
    activeDocumentAgentTaskId.clear();
    ui->documentRunButton->setEnabled(false);
    ui->documentInput->setEnabled(false);
    ui->documentOutputModeCombo->setEnabled(false);
    ui->documentDocumentCombo->setEnabled(false);
    ui->documentComparisonList->setEnabled(false);
    ui->documentImportButton->setEnabled(false);
    ui->documentRefreshButton->setEnabled(false);
    updateDocumentDraftSaveAction();
    ui->documentRunStatus->setText(statusText);
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    ui->documentResultDetailStatus->setText(QStringLiteral("恢复预览建立中 · 旧草稿未改动"));
    polishBadge(ui->documentResultDetailStatus, QStringLiteral("badgeBlue"));
}

void MainWindow::showDocumentDraftParentDiff()
{
    if (documentDraftParentDiffLoading || documentAgentRunning || currentDocumentResultTaskId.isEmpty()) {
        return;
    }
    const QJsonObject version = currentDocumentResultContext
                                    .value(QStringLiteral("draft_version"))
                                    .toObject();
    if (version.value(QStringLiteral("parent_task_id")).toString().trimmed().isEmpty()) {
        QMessageBox::information(
            this,
            QStringLiteral("版本差异"),
            QStringLiteral("当前是草稿初版，没有直接父版本可比较。"));
        return;
    }

    // 版本对比是普通只读查询，不切换当前任务、不清空详情；只短暂锁住同一菜单动作防连点。
    documentDraftParentDiffLoading = true;
    updateDocumentSectionDraftAction();
    backendClient->requestDocumentDraftParentDiff(currentDocumentResultTaskId);
}

void MainWindow::handleDocumentDraftParentDiffReceived(const QJsonObject &result)
{
    documentDraftParentDiffLoading = false;
    updateDocumentSectionDraftAction();
    showDocumentDraftParentDiffDialog(result);
}

void MainWindow::handleDocumentDraftParentDiffFailed(const QString &message)
{
    documentDraftParentDiffLoading = false;
    updateDocumentSectionDraftAction();
    QMessageBox::warning(
        this,
        QStringLiteral("版本差异暂不可用"),
        message.isEmpty() ? QStringLiteral("后端没有返回可展示的版本差异。") : message);
}

void MainWindow::showDocumentDraftParentDiffDialog(const QJsonObject &result)
{
    QDialog dialog(this);
    dialog.setWindowTitle(QStringLiteral("版本差异"));
    dialog.setModal(true);
    dialog.resize(1120, 720);
    dialog.setMinimumSize(820, 520);

    auto *layout = new QVBoxLayout(&dialog);
    layout->setContentsMargins(24, 22, 24, 20);
    layout->setSpacing(12);

    auto *title = new QLabel(QStringLiteral("当前草稿与父版本的差异"), &dialog);
    title->setStyleSheet(QStringLiteral("font-size:20px;font-weight:800;color:#0F172A;"));
    layout->addWidget(title);

    auto *summary = new QLabel(result.value(QStringLiteral("summary")).toString(), &dialog);
    summary->setObjectName(QStringLiteral("subText"));
    summary->setWordWrap(true);
    layout->addWidget(summary);

    const auto changeKindText = [](const QString &kind) {
        if (kind == QStringLiteral("modified")) {
            return QStringLiteral("已修改");
        }
        if (kind == QStringLiteral("added")) {
            return QStringLiteral("仅当前版本");
        }
        if (kind == QStringLiteral("removed")) {
            return QStringLiteral("仅父版本");
        }
        return QStringLiteral("未修改");
    };
    QString parentText = QStringLiteral("# %1\n\n").arg(result.value(QStringLiteral("parent_title")).toString());
    QString currentText = QStringLiteral("# %1\n\n").arg(result.value(QStringLiteral("current_title")).toString());
    const QJsonArray sections = result.value(QStringLiteral("sections")).toArray();
    for (const QJsonValue &value : sections) {
        const QJsonObject section = value.toObject();
        const QString marker = changeKindText(section.value(QStringLiteral("change_kind")).toString());
        const QString heading = section.value(QStringLiteral("heading")).toString();
        const QString parentBody = section.value(QStringLiteral("parent_body")).toString();
        const QString currentBody = section.value(QStringLiteral("current_body")).toString();
        parentText += QStringLiteral("[%1] %2\n\n%3\n\n")
                          .arg(marker,
                               heading,
                               parentBody.isEmpty() ? QStringLiteral("（父版本没有本章节）") : parentBody);
        currentText += QStringLiteral("[%1] %2\n\n%3\n\n")
                           .arg(marker,
                                heading,
                                currentBody.isEmpty() ? QStringLiteral("（当前版本没有本章节）") : currentBody);
    }

    auto *splitter = new QSplitter(Qt::Horizontal, &dialog);
    splitter->setChildrenCollapsible(false);
    const auto addReadPane = [&splitter](const QString &caption, const QString &text) {
        auto *pane = new QWidget(splitter);
        auto *paneLayout = new QVBoxLayout(pane);
        paneLayout->setContentsMargins(0, 0, 0, 0);
        paneLayout->setSpacing(8);
        auto *label = new QLabel(caption, pane);
        label->setObjectName(QStringLiteral("sectionTitle"));
        paneLayout->addWidget(label);
        auto *reader = new QPlainTextEdit(pane);
        reader->setReadOnly(true);
        reader->setUndoRedoEnabled(false);
        reader->setLineWrapMode(QPlainTextEdit::WidgetWidth);
        reader->setPlainText(text);
        paneLayout->addWidget(reader, 1);
        splitter->addWidget(pane);
    };
    addReadPane(QStringLiteral("父版本"), parentText);
    addReadPane(QStringLiteral("当前版本"), currentText);
    splitter->setSizes({540, 540});
    layout->addWidget(splitter, 1);

    const QJsonArray warnings = result.value(QStringLiteral("warnings")).toArray();
    QStringList warningTexts;
    warningTexts.reserve(warnings.size());
    for (const QJsonValue &warning : warnings) {
        if (!warning.toString().trimmed().isEmpty()) {
            warningTexts.append(warning.toString());
        }
    }
    if (!warningTexts.isEmpty()) {
        auto *hint = new QLabel(warningTexts.join(QStringLiteral("\n")), &dialog);
        hint->setObjectName(QStringLiteral("tinyText"));
        hint->setWordWrap(true);
        layout->addWidget(hint);
    }

    auto *buttons = new QDialogButtonBox(QDialogButtonBox::Close, &dialog);
    connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    layout->addWidget(buttons);
    dialog.exec();
}

void MainWindow::saveDocumentDraft()
{
    if (documentDraftSaving || currentDocumentResultTaskId.isEmpty()) {
        return;
    }
    if (currentDocumentResultContext.value(QStringLiteral("draft_sections")).toArray().isEmpty()) {
        QMessageBox::information(this,
                                 QStringLiteral("保存 Markdown"),
                                 QStringLiteral("当前结果没有可保存的 Markdown 草稿。"));
        return;
    }
    const QString verificationState = currentDocumentResultContext
                                        .value(QStringLiteral("draft_verification_state"))
                                        .toString();
    if (verificationState == QStringLiteral("requires_review")) {
        QMessageBox::information(
            this,
            QStringLiteral("请先核验事实"),
            QStringLiteral("这份草稿包含手动修订。请先点击“核验事实”重新读取材料；"
                           "核验完成且没有待确认问题后，才能保存 Markdown。"));
        return;
    }
    if (verificationState == QStringLiteral("reviewed_with_questions")) {
        QMessageBox::information(
            this,
            QStringLiteral("仍有待确认事实"),
            QStringLiteral("本次手动修订仍有待确认事实。请补充材料或修改正文后再次核验，再保存 Markdown。"));
        return;
    }

    bool accepted = false;
    const QString filename = QInputDialog::getText(
        this,
        QStringLiteral("保存 Markdown 草稿"),
        QStringLiteral("文件名（.md）："),
        QLineEdit::Normal,
        suggestedDocumentDraftFilename(),
        &accepted).trimmed();
    if (!accepted) {
        return;
    }
    if (filename.isEmpty() || !filename.endsWith(QStringLiteral(".md"), Qt::CaseInsensitive)) {
        QMessageBox::warning(this,
                             QStringLiteral("保存 Markdown 草稿"),
                             QStringLiteral("请提供以 .md 结尾的文件名。"));
        return;
    }

    const QMessageBox::StandardButton choice = QMessageBox::question(
        this,
        QStringLiteral("确认保存 Markdown 草稿"),
        QStringLiteral("将把本次已验证的草稿保存到：\noutput/document_drafts/%1\n\n"
                       "不会覆盖同名文件，保存记录会写入本次任务历史。")
            .arg(filename),
        QMessageBox::Yes | QMessageBox::No,
        QMessageBox::No);
    if (choice != QMessageBox::Yes) {
        return;
    }

    documentDraftSaving = true;
    updateDocumentDraftSaveAction();
    ui->documentRunStatus->setText(QStringLiteral("正在保存已确认的 Markdown 草稿"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    backendClient->saveDocumentDraft(currentDocumentResultTaskId, filename);
}

void MainWindow::handleDocumentDraftSaved(const DocumentDraftSaveResult &result)
{
    if (result.taskId != currentDocumentResultTaskId) {
        return;
    }

    documentDraftSaving = false;
    documentDraftSaved = true;
    lastSavedDocumentDraftFilename = result.filename;
    updateDocumentDraftSaveAction();
    ui->documentRunStatus->setText(QStringLiteral("草稿已保存 · 可另存为或在任务历史查看产物"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeGreen"));
    ui->documentResultDetailMeta->setText(
        QStringLiteral("已保存到 %1 · 可另存为新文件名，来源与保存记录已写入任务历史")
            .arg(result.relativePath));
    QMessageBox::information(
        this,
        QStringLiteral("Markdown 草稿已保存"),
        QStringLiteral("%1\n\n可在“历史任务”的产物区预览或打开该文件，也可以使用“另存为 Markdown”保留另一份。")
            .arg(result.relativePath));
}

void MainWindow::handleDocumentDraftSaveFailed(const QString &message)
{
    documentDraftSaving = false;
    updateDocumentDraftSaveAction();
    ui->documentRunStatus->setText(QStringLiteral("草稿保存失败"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeOrange"));
    QMessageBox::warning(
        this,
        QStringLiteral("保存 Markdown 草稿失败"),
        message.isEmpty() ? QStringLiteral("后端没有返回具体错误，请稍后重试。") : message);
}

void MainWindow::requestDocumentPresentationPreview()
{
    if (documentPresentationPreviewLoading || documentPresentationExporting
        || currentDocumentResultTaskId.isEmpty()) {
        return;
    }

    const QString verificationState = currentDocumentResultContext
                                        .value(QStringLiteral("draft_verification_state"))
                                        .toString();
    if (currentDocumentResultContext.value(QStringLiteral("draft_sections")).toArray().isEmpty()
        || (!verificationState.isEmpty() && verificationState != QStringLiteral("verified"))) {
        QMessageBox::information(
            this,
            QStringLiteral("导出项目方案 PPT"),
            QStringLiteral("请先完成带来源的 Markdown 草稿，并确保草稿已通过事实核验。"));
        return;
    }

    documentPresentationPreviewLoading = true;
    documentPresentationTaskId = currentDocumentResultTaskId;
    documentPresentationPlanId.clear();
    updateDocumentPresentationAction();
    ui->documentResultDetailStatus->setText(QStringLiteral("正在自动核验项目材料"));
    ui->documentRunStatus->setText(QStringLiteral("正在自动核验材料并建立项目方案 PPT 计划"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    backendClient->requestProjectProposalPresentationPreview(documentPresentationTaskId);
}

void MainWindow::handlePresentationPreviewReceived(const PresentationPreviewResult &result)
{
    if (result.sourceTaskId != currentDocumentResultTaskId || result.planId.isEmpty()) {
        return;
    }

    documentPresentationPreviewLoading = false;
    documentPresentationTaskId = result.sourceTaskId;
    documentPresentationPlanId = result.planId;
    updateDocumentPresentationAction();
    const int attentionCount = result.preflight.value(QStringLiteral("attention_check_total")).toInt();
    ui->documentResultDetailStatus->setText(
        attentionCount > 0 ? QStringLiteral("PPT 计划已预检 · 有待补充项")
                           : QStringLiteral("PPT 计划已自动核验"));
    ui->documentRunStatus->setText(
        attentionCount > 0
            ? QStringLiteral("项目方案 PPT 已完成自动材料预检 · 请查看待补充项后确认导出")
            : QStringLiteral("项目方案 PPT 已完成自动材料预检 · 请核对后确认导出"));
    polishBadge(ui->documentRunStatus,
                attentionCount > 0 ? QStringLiteral("badgeOrange") : QStringLiteral("badgeGreen"));
    showPresentationPreviewDialog(result);
}

void MainWindow::handlePresentationPreviewFailed(const QString &message)
{
    documentPresentationPreviewLoading = false;
    documentPresentationTaskId.clear();
    documentPresentationPlanId.clear();
    updateDocumentPresentationAction();
    ui->documentResultDetailStatus->setText(QStringLiteral("PPT 计划未生成"));
    ui->documentRunStatus->setText(QStringLiteral("项目方案 PPT 计划生成失败"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeOrange"));
    QMessageBox::warning(
        this,
        QStringLiteral("生成项目方案 PPT 计划失败"),
        message.isEmpty() ? QStringLiteral("后端没有返回具体原因，请确认草稿已完成来源核验后重试。") : message);
}

void MainWindow::handlePresentationExported(const PresentationExportResult &result)
{
    documentPresentationExporting = false;
    updateDocumentPresentationAction();
    if (result.taskId == currentDocumentResultTaskId) {
        ui->documentResultDetailStatus->setText(QStringLiteral("PPT 已导出"));
        ui->documentRunStatus->setText(
            QStringLiteral("项目方案 PPT 已导出 · 已通过回读验证并写入任务历史"));
        polishBadge(ui->documentRunStatus, QStringLiteral("badgeGreen"));
        ui->documentResultDetailMeta->setText(
            QStringLiteral("已导出到 %1 · %2 页 · 可在任务历史查看交付记录")
                .arg(result.relativePath)
                .arg(result.slideCount));
    }

    if (!documentPresentationDialog || result.taskId != documentPresentationTaskId) {
        return;
    }
    auto *filenameInput = documentPresentationDialog->findChild<QLineEdit *>(
        QStringLiteral("documentPresentationFilenameInput"));
    auto *statusLabel = documentPresentationDialog->findChild<QLabel *>(
        QStringLiteral("documentPresentationDialogStatus"));
    auto *buttonBox = documentPresentationDialog->findChild<QDialogButtonBox *>(
        QStringLiteral("documentPresentationDialogButtons"));
    if (filenameInput) {
        filenameInput->setEnabled(false);
    }
    if (statusLabel) {
        statusLabel->setText(
            QStringLiteral("已导出 %1 页，并通过回读验证。文件记录已写入任务历史。")
                .arg(result.slideCount));
        polishBadge(statusLabel, QStringLiteral("badgeGreen"));
    }
    if (buttonBox) {
        buttonBox->clear();
        // PPTX 的本地路径不回传给 Qt 直接打开。统一经本次任务的历史产物入口访问，
        // 可以保留来源、回读验证和文件打开行为的一致审计边界。
        QPushButton *historyButton = buttonBox->addButton(QStringLiteral("查看任务历史"), QDialogButtonBox::ActionRole);
        historyButton->setIcon(style()->standardIcon(QStyle::SP_FileDialogDetailedView));
        historyButton->setToolTip(QStringLiteral("查看本次 PPTX 的回读验证、来源和受控产物。"));
        connect(historyButton, &QPushButton::clicked, this, [this, dialog = documentPresentationDialog, taskId = result.taskId]() {
            if (dialog) {
                dialog->accept();
            }
            openTaskInHistory(taskId);
        });
        QPushButton *closeButton = buttonBox->addButton(QStringLiteral("完成"), QDialogButtonBox::AcceptRole);
        connect(closeButton, &QPushButton::clicked, documentPresentationDialog, &QDialog::accept);
    }
}

void MainWindow::handlePresentationExportFailed(const QString &message)
{
    documentPresentationExporting = false;
    updateDocumentPresentationAction();
    if (documentPresentationDialog) {
        auto *filenameInput = documentPresentationDialog->findChild<QLineEdit *>(
            QStringLiteral("documentPresentationFilenameInput"));
        auto *statusLabel = documentPresentationDialog->findChild<QLabel *>(
            QStringLiteral("documentPresentationDialogStatus"));
        auto *confirmButton = documentPresentationDialog->findChild<QPushButton *>(
            QStringLiteral("documentPresentationDialogConfirmButton"));
        auto *cancelButton = documentPresentationDialog->findChild<QPushButton *>(
            QStringLiteral("documentPresentationDialogCancelButton"));
        if (filenameInput) {
            filenameInput->setEnabled(true);
        }
        if (confirmButton) {
            confirmButton->setEnabled(true);
        }
        if (cancelButton) {
            cancelButton->setEnabled(true);
        }
        if (statusLabel) {
            statusLabel->setText(
                QStringLiteral("导出失败：%1").arg(message.isEmpty() ? QStringLiteral("请修改后重试") : message));
            polishBadge(statusLabel, QStringLiteral("badgeOrange"));
        }
        return;
    }

    ui->documentResultDetailStatus->setText(QStringLiteral("PPT 导出失败"));
    ui->documentRunStatus->setText(QStringLiteral("项目方案 PPT 导出失败"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeOrange"));
    QMessageBox::warning(
        this,
        QStringLiteral("导出项目方案 PPT 失败"),
        message.isEmpty() ? QStringLiteral("后端没有返回具体错误，请重新打开预览后重试。") : message);
}

void MainWindow::showPresentationPreviewDialog(const PresentationPreviewResult &result)
{
    if (documentPresentationDialog) {
        documentPresentationDialog->close();
    }

    auto *dialog = new QDialog(this);
    dialog->setAttribute(Qt::WA_DeleteOnClose);
    dialog->setWindowTitle(QStringLiteral("项目方案 PPT 交付"));
    dialog->setModal(true);
    dialog->setMinimumSize(700, 560);
    dialog->resize(880, 700);
    dialog->setStyleSheet(
        QStringLiteral("QDialog { background:#F4F7FB; }"
                       "QFrame#presentationPreviewCard { background:#FFFFFF; border:1px solid #DDEBFA; border-radius:14px; }"
                       "QLineEdit { background:#FFFFFF; border:1px solid #CBDCF4; border-radius:9px; padding:8px 10px; }"
                       "QTextBrowser { background:#F8FBFF; border:1px solid #DDEBFA; border-radius:10px; padding:10px; }"));
    documentPresentationDialog = dialog;

    auto *layout = new QVBoxLayout(dialog);
    layout->setContentsMargins(22, 20, 22, 20);
    layout->setSpacing(12);

    auto *card = new QFrame(dialog);
    card->setObjectName(QStringLiteral("presentationPreviewCard"));
    auto *cardLayout = new QVBoxLayout(card);
    cardLayout->setContentsMargins(18, 16, 18, 16);
    cardLayout->setSpacing(6);
    auto *title = new QLabel(QStringLiteral("项目方案 PPT"), card);
    title->setStyleSheet(QStringLiteral("font-size:22px; font-weight:700; color:#14224A;"));
    const QJsonObject preflight = result.preflight;
    const int attentionCount = preflight.value(QStringLiteral("attention_check_total")).toInt();
    auto *subtitle = new QLabel(
        QStringLiteral("共 %1 页 · 已自动核验项目材料%2 · 导出后生成可编辑 .pptx")
            .arg(result.slides.size())
            .arg(attentionCount > 0 ? QStringLiteral("（%1 项待补充）").arg(attentionCount)
                                     : QStringLiteral("")),
        card);
    subtitle->setStyleSheet(QStringLiteral("color:#5D6F8C;"));
    subtitle->setWordWrap(true);
    cardLayout->addWidget(title);
    cardLayout->addWidget(subtitle);
    layout->addWidget(card);

    auto *planTitle = new QLabel(QStringLiteral("幻灯片计划"), dialog);
    planTitle->setStyleSheet(QStringLiteral("font-weight:700; color:#1E315A;"));
    layout->addWidget(planTitle);
    auto *previewText = new QTextBrowser(dialog);
    previewText->setOpenExternalLinks(false);
    previewText->setHtml(formatPresentationPreviewHtml(result));
    layout->addWidget(previewText, 1);

    if (!result.warnings.isEmpty()) {
        auto *warning = new QLabel(QStringLiteral("注意：%1").arg(result.warnings.join(QStringLiteral("\n"))), dialog);
        warning->setObjectName(QStringLiteral("badgeOrange"));
        warning->setWordWrap(true);
        layout->addWidget(warning);
    }

    auto *filenameLabel = new QLabel(QStringLiteral("交付文件名"), dialog);
    layout->addWidget(filenameLabel);
    auto *filenameInput = new QLineEdit(suggestedPresentationFilename(), dialog);
    filenameInput->setObjectName(QStringLiteral("documentPresentationFilenameInput"));
    filenameInput->setPlaceholderText(QStringLiteral("例如：项目方案.pptx"));
    layout->addWidget(filenameInput);

    auto *statusLabel = new QLabel(QStringLiteral("确认后会生成新文件，不会覆盖同名文件。"), dialog);
    statusLabel->setObjectName(QStringLiteral("documentPresentationDialogStatus"));
    statusLabel->setWordWrap(true);
    polishBadge(statusLabel, QStringLiteral("badgeGray"));
    layout->addWidget(statusLabel);

    auto *buttonBox = new QDialogButtonBox(dialog);
    buttonBox->setObjectName(QStringLiteral("documentPresentationDialogButtons"));
    QPushButton *cancelButton = buttonBox->addButton(QStringLiteral("取消"), QDialogButtonBox::RejectRole);
    cancelButton->setObjectName(QStringLiteral("documentPresentationDialogCancelButton"));
    QPushButton *confirmButton = buttonBox->addButton(QStringLiteral("确认导出"), QDialogButtonBox::AcceptRole);
    confirmButton->setObjectName(QStringLiteral("documentPresentationDialogConfirmButton"));
    confirmButton->setStyleSheet(QStringLiteral("background:#2563EB; color:white; border-radius:9px; padding:9px 16px; font-weight:700;"));
    connect(cancelButton, &QPushButton::clicked, dialog, &QDialog::reject);
    connect(confirmButton, &QPushButton::clicked, this, [this, filenameInput, statusLabel, confirmButton, cancelButton]() {
        const QString filename = filenameInput->text().trimmed();
        if (filename.isEmpty() || !filename.endsWith(QStringLiteral(".pptx"), Qt::CaseInsensitive)) {
            statusLabel->setText(QStringLiteral("请提供以 .pptx 结尾的文件名。"));
            polishBadge(statusLabel, QStringLiteral("badgeOrange"));
            filenameInput->setFocus();
            return;
        }
        if (documentPresentationTaskId.isEmpty() || documentPresentationPlanId.isEmpty()) {
            statusLabel->setText(QStringLiteral("计划已失效，请关闭后重新打开导出预览。"));
            polishBadge(statusLabel, QStringLiteral("badgeOrange"));
            return;
        }

        documentPresentationExporting = true;
        updateDocumentPresentationAction();
        filenameInput->setEnabled(false);
        confirmButton->setEnabled(false);
        cancelButton->setEnabled(false);
        statusLabel->setText(QStringLiteral("正在生成 PPTX、回读验证并写入任务历史…"));
        polishBadge(statusLabel, QStringLiteral("badgeBlue"));
        backendClient->exportProjectProposalPresentation(
            documentPresentationTaskId,
            documentPresentationPlanId,
            filename);
    });
    layout->addWidget(buttonBox);
    dialog->open();
}

QString MainWindow::formatPresentationPreviewHtml(const PresentationPreviewResult &result) const
{
    QStringList blocks;
    const QJsonObject preflight = result.preflight;
    if (!preflight.isEmpty()) {
        const int checkTotal = preflight.value(QStringLiteral("check_total")).toInt();
        const int passedTotal = preflight.value(QStringLiteral("passed_check_total")).toInt();
        const int attentionTotal = preflight.value(QStringLiteral("attention_check_total")).toInt();
        const int highAttentionTotal = preflight.value(QStringLiteral("high_attention_total")).toInt();
        const bool hasAttention = attentionTotal > 0;
        const QString statusColor = hasAttention ? QStringLiteral("#B45309") : QStringLiteral("#15803D");
        const QString statusText = hasAttention ? QStringLiteral("已完成 · 有待补充项")
                                                : QStringLiteral("已完成 · 材料表述已覆盖");
        blocks.append(
            QStringLiteral("<section style=\"margin:0 0 16px 0;padding:14px 16px;border:1px solid %1;"
                           "border-left:4px solid %1;border-radius:10px;background:%2;\">"
                           "<div style=\"font-size:16px;font-weight:700;color:#14224A;\">自动交付预检"
                           " <span style=\"font-size:12px;color:%1;\">%3</span></div>"
                           "<div style=\"color:#334155;margin-top:6px;\">%4</div>"
                           "<div style=\"color:#64748B;font-size:12px;margin-top:8px;\">"
                           "规则 %5 项 · 已识别 %6 项 · 待补充 %7 项%8</div></section>")
                .arg(statusColor, hasAttention ? QStringLiteral("#FFF7ED") : QStringLiteral("#F0FDF4"),
                     statusText, preflight.value(QStringLiteral("summary")).toString().toHtmlEscaped())
                .arg(checkTotal)
                .arg(passedTotal)
                .arg(attentionTotal)
                .arg(highAttentionTotal > 0 ? QStringLiteral(" · 高优先级 %1 项").arg(highAttentionTotal)
                                            : QStringLiteral("")));

        const QJsonArray findings = preflight.value(QStringLiteral("findings")).toArray();
        for (const QJsonValue &value : findings) {
            const QJsonObject finding = value.toObject();
            const QString severity = finding.value(QStringLiteral("severity")).toString();
            const QString severityText = severity == QStringLiteral("high") ? QStringLiteral("高优先级")
                : severity == QStringLiteral("medium") ? QStringLiteral("中优先级")
                                                         : QStringLiteral("低优先级");
            const QString color = severity == QStringLiteral("high") ? QStringLiteral("#B91C1C")
                : severity == QStringLiteral("medium") ? QStringLiteral("#B45309")
                                                         : QStringLiteral("#1D4ED8");
            QStringList sources;
            for (const QJsonValue &sourceValue : finding.value(QStringLiteral("source_refs")).toArray()) {
                const QJsonObject source = sourceValue.toObject();
                const QString path = source.value(QStringLiteral("relative_path")).toString().toHtmlEscaped();
                const QString locator = source.value(QStringLiteral("source_locator")).toString().toHtmlEscaped();
                if (!path.isEmpty()) {
                    sources.append(locator.isEmpty() ? path : QStringLiteral("%1 · %2").arg(path, locator));
                }
            }
            blocks.append(
                QStringLiteral("<section style=\"margin:0 0 10px 0;padding:11px 14px;border:1px solid #F3D6AA;"
                               "border-radius:9px;background:#FFFBEB;\">"
                               "<div style=\"color:%1;font-size:12px;font-weight:700;\">%2 · %3</div>"
                               "<div style=\"color:#334155;margin-top:5px;\"><b>建议：</b>%4</div>"
                               "%5</section>")
                    .arg(color, severityText, finding.value(QStringLiteral("title")).toString().toHtmlEscaped(),
                         finding.value(QStringLiteral("suggestion")).toString().toHtmlEscaped(),
                         sources.isEmpty()
                             ? QStringLiteral("")
                             : QStringLiteral("<div style=\"color:#64748B;font-size:12px;margin-top:7px;\">来源：%1</div>")
                                   .arg(sources.join(QStringLiteral("；")))));
        }
        blocks.append(QStringLiteral("<h3 style=\"color:#14224A;margin:22px 0 10px 0;\">幻灯片计划</h3>"));
    }
    for (int index = 0; index < result.slides.size(); ++index) {
        const QJsonObject slide = result.slides.at(index).toObject();
        const QString role = slide.value(QStringLiteral("role")).toString();
        const QString roleText = role == QStringLiteral("cover") ? QStringLiteral("封面")
            : role == QStringLiteral("agenda") ? QStringLiteral("目录")
            : role == QStringLiteral("content") ? QStringLiteral("内容")
            : role == QStringLiteral("summary") ? QStringLiteral("核对")
                                              : QStringLiteral("来源");
        QStringList bullets;
        for (const QJsonValue &bullet : slide.value(QStringLiteral("bullets")).toArray()) {
            if (bullet.isString()) {
                bullets.append(QStringLiteral("<li>%1</li>").arg(bullet.toString().toHtmlEscaped()));
            }
        }
        QStringList sources;
        for (const QJsonValue &sourceValue : slide.value(QStringLiteral("source_refs")).toArray()) {
            const QJsonObject source = sourceValue.toObject();
            const QString path = source.value(QStringLiteral("relative_path")).toString().toHtmlEscaped();
            QString locator = source.value(QStringLiteral("source_locator")).toString().toHtmlEscaped();
            if (locator.isEmpty()) {
                const int startLine = source.value(QStringLiteral("start_line")).toInt();
                const int endLine = source.value(QStringLiteral("end_line")).toInt();
                locator = startLine == endLine
                    ? QStringLiteral("第 %1 行").arg(startLine)
                    : QStringLiteral("第 %1-%2 行").arg(startLine).arg(endLine);
            }
            if (!path.isEmpty()) {
                sources.append(QStringLiteral("%1 · %2").arg(path, locator));
            }
        }
        blocks.append(
            QStringLiteral("<section style=\"margin:0 0 14px 0;padding:12px 14px;border:1px solid #DDEBFA;"
                           "border-radius:10px;background:#FFFFFF;\">"
                           "<div style=\"color:#2563EB;font-size:12px;\">第 %1 页 · %2</div>"
                           "<div style=\"font-size:17px;font-weight:700;color:#14224A;margin-top:3px;\">%3</div>%4%5</section>")
                .arg(index + 1)
                .arg(roleText)
                .arg(slide.value(QStringLiteral("title")).toString().toHtmlEscaped())
                .arg(bullets.isEmpty() ? QString() : QStringLiteral("<ul style=\"margin:8px 0 0 18px;\">%1</ul>").arg(bullets.join(QString())))
                .arg(sources.isEmpty() ? QString() : QStringLiteral("<div style=\"color:#64748B;font-size:12px;margin-top:8px;\">来源：%1</div>").arg(sources.join(QStringLiteral("；")))));
    }
    return blocks.join(QString());
}

QString MainWindow::suggestedPresentationFilename() const
{
    QString title = currentDocumentResultContext.value(QStringLiteral("draft_title")).toString().simplified();
    if (title.isEmpty()) {
        title = QStringLiteral("AgentFlow 项目方案");
    }
    const QString forbidden = QStringLiteral("<>:\"|?*/") + QLatin1Char('\\');
    for (QChar &character : title) {
        if (forbidden.contains(character)) {
            character = QChar('-');
        }
    }
    title = title.trimmed();
    while (title.endsWith(QLatin1Char('.')) || title.endsWith(QLatin1Char(' '))) {
        title.chop(1);
    }
    return (title.isEmpty() ? QStringLiteral("AgentFlow 项目方案") : title.left(96))
        + QStringLiteral("-项目方案.pptx");
}

void MainWindow::requestProjectDocumentReview()
{
    if (projectDocumentReviewLoading || paperReviewLoading || documentAgentRunning) {
        return;
    }

    const QString selectedDocument = selectedDocumentForReview(
        QStringLiteral("选择项目材料"),
        QStringLiteral("项目审查一次只检查一份主材料，请选择本次要审查的 PRD、项目方案或计划："));
    if (selectedDocument.isEmpty()) {
        QMessageBox::information(
            this,
            QStringLiteral("项目文档审查"),
            QStringLiteral("请先选择一份已导入的 PRD、项目方案或计划；审查只会读取该受控 workspace 材料。"));
        return;
    }

    projectDocumentReviewLoading = true;
    updateProjectReviewAction();
    updateDocumentOpenResultAction();
    ui->documentResultDetailStatus->setText(QStringLiteral("正在执行项目审查"));
    ui->documentRunStatus->setText(QStringLiteral("正在检查范围、验收、责任、节点、风险依赖与术语口径"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    backendClient->startProjectDocumentReview(selectedDocument, QStringLiteral("auto"));
}

void MainWindow::handleProjectReviewStarted(const QString &taskId)
{
    if (!projectDocumentReviewLoading || taskId.isEmpty()) {
        return;
    }
    activeProjectDocumentReviewTaskId = taskId;
    updateDocumentOpenResultAction();
    ui->documentResultDetailStatus->setText(QStringLiteral("项目审查已受理"));
    ui->documentRunStatus->setText(QStringLiteral("项目审查已受理 · 正在连接真实检查阶段"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    backendClient->connectTaskLog(taskId);
}

void MainWindow::handleProjectReviewStillRunning(const QString &taskId, const QString &status)
{
    if (!projectDocumentReviewLoading || taskId != activeProjectDocumentReviewTaskId) {
        return;
    }
    ui->documentRunStatus->setText(
        status == QStringLiteral("queued") ? QStringLiteral("项目审查任务排队中")
                                             : QStringLiteral("项目审查正在等待已校验报告"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    // 只有事件流提前断开或结果落库稍晚时才走这里。低频重查终态，避免客户端一直停在加载态。
    QTimer::singleShot(500, this, [this, taskId]() {
        if (projectDocumentReviewLoading && taskId == activeProjectDocumentReviewTaskId) {
            backendClient->requestProjectDocumentReviewResult(taskId);
        }
    });
}

void MainWindow::handleProjectReviewReceived(const ProjectReviewResult &result)
{
    if (!activeProjectDocumentReviewTaskId.isEmpty()
        && result.taskId != activeProjectDocumentReviewTaskId) {
        return;
    }
    projectDocumentReviewLoading = false;
    activeProjectDocumentReviewTaskId.clear();
    latestProjectReviewResult = result;
    latestDocumentReviewKind = DocumentWorkbenchReviewKind::Project;
    latestDocumentReviewReference = result.report.value(QStringLiteral("document_ref")).toString().trimmed();
    if (latestDocumentReviewReference.isEmpty()) {
        latestDocumentReviewReference = ui->documentDocumentCombo->currentData().toString().trimmed();
    }
    updateProjectReviewAction();
    updateDocumentOpenResultAction();
    const int findingCount = result.report.value(QStringLiteral("findings")).toArray().size();
    ui->documentResultDetailStatus->setText(
        findingCount > 0 ? QStringLiteral("项目审查完成 · 待处理") : QStringLiteral("项目审查完成 · 通过"));
    ui->documentRunStatus->setText(
        findingCount > 0
            ? QStringLiteral("项目审查完成 · 已将 %1 项待处理问题写入任务历史").arg(findingCount)
            : QStringLiteral("项目审查完成 · 本轮质量规则未发现待处理问题，已写入任务历史"));
    polishBadge(ui->documentRunStatus, findingCount > 0 ? QStringLiteral("badgeOrange") : QStringLiteral("badgeGreen"));
    ui->documentResultDetailMeta->setText(
        QStringLiteral("项目审查任务 %1 · 可在任务历史回看规则、来源与读取审计")
            .arg(result.taskId.right(12)));
    showProjectReviewDialog(result);
}

void MainWindow::handleProjectReviewFailed(const QString &message)
{
    projectDocumentReviewLoading = false;
    activeProjectDocumentReviewTaskId.clear();
    updateProjectReviewAction();
    updateDocumentOpenResultAction();
    ui->documentResultDetailStatus->setText(QStringLiteral("项目审查未完成"));
    ui->documentRunStatus->setText(QStringLiteral("项目审查失败 · 原材料和当前分析结果未修改"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeOrange"));
    QMessageBox::warning(
        this,
        QStringLiteral("项目文档审查失败"),
        message.isEmpty() ? QStringLiteral("后端没有返回具体错误，请确认材料仍在受控 workspace 中后重试。") : message);
}

void MainWindow::requestPaperReview()
{
    if (projectDocumentReviewLoading || paperReviewLoading || documentAgentRunning) {
        return;
    }

    const QString selectedDocument = selectedDocumentForReview(
        QStringLiteral("选择论文材料"),
        QStringLiteral("论文审查一次只检查一份论文或学术报告，请选择本次要审查的主材料："));
    if (selectedDocument.isEmpty()) {
        QMessageBox::information(
            this,
            QStringLiteral("论文审查"),
            QStringLiteral("请先选择一份已导入的论文或学术报告；审查只会读取该受控 workspace 材料。"));
        return;
    }

    paperReviewLoading = true;
    updateProjectReviewAction();
    updateDocumentOpenResultAction();
    ui->documentResultDetailStatus->setText(QStringLiteral("正在执行论文审查"));
    ui->documentRunStatus->setText(QStringLiteral("正在检查论文结构、引用、图表、标题格式与可读性"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    backendClient->startPaperReview(selectedDocument, QStringLiteral("auto"));
}

void MainWindow::handlePaperReviewStarted(const QString &taskId)
{
    if (!paperReviewLoading || taskId.isEmpty()) {
        return;
    }
    activePaperReviewTaskId = taskId;
    updateDocumentOpenResultAction();
    ui->documentResultDetailStatus->setText(QStringLiteral("论文审查已受理"));
    ui->documentRunStatus->setText(QStringLiteral("论文审查已受理 · 正在连接真实检查阶段"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    backendClient->connectTaskLog(taskId);
}

void MainWindow::handlePaperReviewStillRunning(const QString &taskId, const QString &status)
{
    if (!paperReviewLoading || taskId != activePaperReviewTaskId) {
        return;
    }
    ui->documentRunStatus->setText(
        status == QStringLiteral("queued") ? QStringLiteral("论文审查任务排队中")
                                             : QStringLiteral("论文审查正在等待已校验报告"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    // 与项目审查共享相同的终态兜底：流断开不等于任务失败，仍应先确认已写入的报告。
    QTimer::singleShot(500, this, [this, taskId]() {
        if (paperReviewLoading && taskId == activePaperReviewTaskId) {
            backendClient->requestPaperReviewResult(taskId);
        }
    });
}

void MainWindow::handlePaperReviewReceived(const PaperReviewResult &result)
{
    if (!activePaperReviewTaskId.isEmpty() && result.taskId != activePaperReviewTaskId) {
        return;
    }
    paperReviewLoading = false;
    activePaperReviewTaskId.clear();
    latestPaperReviewResult = result;
    latestDocumentReviewKind = DocumentWorkbenchReviewKind::Paper;
    latestDocumentReviewReference = result.report.value(QStringLiteral("document_ref")).toString().trimmed();
    if (latestDocumentReviewReference.isEmpty()) {
        latestDocumentReviewReference = ui->documentDocumentCombo->currentData().toString().trimmed();
    }
    updateProjectReviewAction();
    updateDocumentOpenResultAction();
    const int findingCount = result.report.value(QStringLiteral("findings")).toArray().size();
    ui->documentResultDetailStatus->setText(
        findingCount > 0 ? QStringLiteral("论文审查完成 · 待复核") : QStringLiteral("论文审查完成 · 通过"));
    ui->documentRunStatus->setText(
        findingCount > 0
            ? QStringLiteral("论文审查完成 · 已将 %1 项待复核问题写入任务历史").arg(findingCount)
            : QStringLiteral("论文审查完成 · 本轮形式规则未发现待复核问题，已写入任务历史"));
    polishBadge(ui->documentRunStatus, findingCount > 0 ? QStringLiteral("badgeOrange") : QStringLiteral("badgeGreen"));
    ui->documentResultDetailMeta->setText(
        QStringLiteral("论文审查任务 %1 · 可在任务历史回看规则、来源与读取审计")
            .arg(result.taskId.right(12)));
    showPaperReviewDialog(result);
}

void MainWindow::handlePaperReviewFailed(const QString &message)
{
    paperReviewLoading = false;
    activePaperReviewTaskId.clear();
    updateProjectReviewAction();
    updateDocumentOpenResultAction();
    ui->documentResultDetailStatus->setText(QStringLiteral("论文审查未完成"));
    ui->documentRunStatus->setText(QStringLiteral("论文审查失败 · 原材料和当前分析结果未修改"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeOrange"));
    QMessageBox::warning(
        this,
        QStringLiteral("论文审查失败"),
        message.isEmpty() ? QStringLiteral("后端没有返回具体错误，请确认材料仍在受控 workspace 中后重试。") : message);
}

void MainWindow::showProjectReviewDialog(const ProjectReviewResult &result)
{
    if (projectDocumentReviewDialog) {
        projectDocumentReviewDialog->close();
    }

    auto *dialog = new QDialog(this);
    dialog->setAttribute(Qt::WA_DeleteOnClose);
    dialog->setWindowTitle(QStringLiteral("项目文档审查"));
    dialog->setModal(true);
    dialog->setMinimumSize(760, 590);
    restoreDocumentReviewDialogGeometry(dialog, QStringLiteral("project"));
    dialog->setStyleSheet(
        QStringLiteral("QDialog { background:#F4F7FB; }"
                       "QFrame#projectReviewSummaryCard { background:#FFFFFF; border:1px solid #DDEBFA; border-radius:12px; }"
                       "QFrame#reviewEvidenceInspector { background:#FFFFFF; border:1px solid #DDEBFA; border-radius:10px; }"
                       "QLabel#reviewInspectorTitle { font-size:16px; font-weight:700; color:#14224A; }"
                       "QLabel#reviewInspectorHint { color:#64748B; font-size:12px; }"
                       "QLabel#reviewInspectorMetrics { padding:10px; background:#F8FBFF; border:1px solid #E2EAF5; border-radius:8px; color:#334155; line-height:1.6; }"
                       "QLabel#reviewInspectorCaption { font-weight:700; color:#334155; }"
                       "QTextBrowser#reviewInspectorSources { background:#F8FBFF; border:1px solid #E2EAF5; border-radius:8px; padding:7px; color:#475569; }"
                       "QTextBrowser { background:#F8FBFF; border:1px solid #DDEBFA; border-radius:10px; padding:12px; }"));
    projectDocumentReviewDialog = dialog;

    auto *layout = new QVBoxLayout(dialog);
    layout->setContentsMargins(22, 20, 22, 20);
    layout->setSpacing(12);

    auto *summaryCard = new QFrame(dialog);
    summaryCard->setObjectName(QStringLiteral("projectReviewSummaryCard"));
    auto *summaryLayout = new QVBoxLayout(summaryCard);
    summaryLayout->setContentsMargins(18, 16, 18, 16);
    summaryLayout->setSpacing(5);
    auto *title = new QLabel(QStringLiteral("项目文档审查"), summaryCard);
    title->setStyleSheet(QStringLiteral("font-size:22px; font-weight:700; color:#14224A;"));
    auto *subtitle = new QLabel(
        QStringLiteral("规则化检查 · %1 · %2")
            .arg(result.report.value(QStringLiteral("document_ref")).toString(), result.taskId.right(12)),
        summaryCard);
    subtitle->setStyleSheet(QStringLiteral("color:#5D6F8C;"));
    subtitle->setWordWrap(true);
    auto *summary = new QLabel(result.report.value(QStringLiteral("summary")).toString(), summaryCard);
    summary->setWordWrap(true);
    summary->setStyleSheet(QStringLiteral("color:#1E315A;"));
    summaryLayout->addWidget(title);
    summaryLayout->addWidget(subtitle);
    summaryLayout->addWidget(summary);
    layout->addWidget(summaryCard);

    auto *reportText = new QTextBrowser(dialog);
    reportText->setOpenExternalLinks(false);
    reportText->setHtml(formatProjectReviewHtml(result));
    auto *reviewSplitter = new QSplitter(Qt::Horizontal, dialog);
    reviewSplitter->setChildrenCollapsible(false);
    reviewSplitter->addWidget(reportText);
    reviewSplitter->addWidget(createDocumentReviewInspector(result.report, reviewSplitter));
    reviewSplitter->setStretchFactor(0, 1);
    reviewSplitter->setStretchFactor(1, 0);
    reviewSplitter->setSizes({680, 250});
    layout->addWidget(reviewSplitter, 1);

    const QJsonArray warnings = result.report.value(QStringLiteral("warnings")).toArray();
    if (!warnings.isEmpty()) {
        QStringList warningText;
        for (const QJsonValue &warning : warnings) {
            if (warning.isString()) {
                warningText.append(warning.toString());
            }
        }
        auto *warningLabel = new QLabel(QStringLiteral("审查边界：%1").arg(warningText.join(QStringLiteral("\n"))), dialog);
        warningLabel->setWordWrap(true);
        polishBadge(warningLabel, QStringLiteral("badgeGray"));
        layout->addWidget(warningLabel);
    }

    auto *buttonBox = new QDialogButtonBox(QDialogButtonBox::Close, dialog);
    if (!result.taskId.isEmpty()) {
        auto *historyButton = buttonBox->addButton(QStringLiteral("查看任务历史"), QDialogButtonBox::ActionRole);
        historyButton->setIcon(style()->standardIcon(QStyle::SP_FileDialogDetailedView));
        historyButton->setToolTip(QStringLiteral("打开本次审查的完整执行记录、证据与产物。"));
        connect(historyButton, &QPushButton::clicked, this, [this, dialog, taskId = result.taskId]() {
            dialog->accept();
            openTaskInHistory(taskId);
        });
    }
    connect(buttonBox, &QDialogButtonBox::rejected, dialog, &QDialog::reject);
    layout->addWidget(buttonBox);
    dialog->open();
}

void MainWindow::showPaperReviewDialog(const PaperReviewResult &result)
{
    if (paperReviewDialog) {
        paperReviewDialog->close();
    }

    auto *dialog = new QDialog(this);
    dialog->setAttribute(Qt::WA_DeleteOnClose);
    dialog->setWindowTitle(QStringLiteral("论文审查"));
    dialog->setModal(true);
    dialog->setMinimumSize(760, 590);
    restoreDocumentReviewDialogGeometry(dialog, QStringLiteral("paper"));
    dialog->setStyleSheet(
        QStringLiteral("QDialog { background:#F4F7FB; }"
                       "QFrame#paperReviewSummaryCard { background:#FFFFFF; border:1px solid #DDEBFA; border-radius:12px; }"
                       "QFrame#reviewEvidenceInspector { background:#FFFFFF; border:1px solid #DDEBFA; border-radius:10px; }"
                       "QLabel#reviewInspectorTitle { font-size:16px; font-weight:700; color:#14224A; }"
                       "QLabel#reviewInspectorHint { color:#64748B; font-size:12px; }"
                       "QLabel#reviewInspectorMetrics { padding:10px; background:#F8FBFF; border:1px solid #E2EAF5; border-radius:8px; color:#334155; line-height:1.6; }"
                       "QLabel#reviewInspectorCaption { font-weight:700; color:#334155; }"
                       "QTextBrowser#reviewInspectorSources { background:#F8FBFF; border:1px solid #E2EAF5; border-radius:8px; padding:7px; color:#475569; }"
                       "QTextBrowser { background:#F8FBFF; border:1px solid #DDEBFA; border-radius:10px; padding:12px; }"));
    paperReviewDialog = dialog;

    auto *layout = new QVBoxLayout(dialog);
    layout->setContentsMargins(22, 20, 22, 20);
    layout->setSpacing(12);
    auto *summaryCard = new QFrame(dialog);
    summaryCard->setObjectName(QStringLiteral("paperReviewSummaryCard"));
    auto *summaryLayout = new QVBoxLayout(summaryCard);
    summaryLayout->setContentsMargins(18, 16, 18, 16);
    summaryLayout->setSpacing(5);
    auto *title = new QLabel(QStringLiteral("论文审查"), summaryCard);
    title->setStyleSheet(QStringLiteral("font-size:22px; font-weight:700; color:#14224A;"));
    auto *subtitle = new QLabel(
        QStringLiteral("论文形式规则 · %1 · %2")
            .arg(result.report.value(QStringLiteral("document_ref")).toString(), result.taskId.right(12)),
        summaryCard);
    subtitle->setStyleSheet(QStringLiteral("color:#5D6F8C;"));
    subtitle->setWordWrap(true);
    auto *summary = new QLabel(result.report.value(QStringLiteral("summary")).toString(), summaryCard);
    summary->setWordWrap(true);
    summary->setStyleSheet(QStringLiteral("color:#1E315A;"));
    summaryLayout->addWidget(title);
    summaryLayout->addWidget(subtitle);
    summaryLayout->addWidget(summary);
    layout->addWidget(summaryCard);

    // 两份报告的 finding/check 协议刻意同形，使用同一阅读渲染可保持审查体验一致，避免
    // 因为规则类别不同就产生两套难维护的长文本排版逻辑。
    ProjectReviewResult displayAdapter;
    displayAdapter.report = result.report;
    auto *reportText = new QTextBrowser(dialog);
    reportText->setOpenExternalLinks(false);
    reportText->setHtml(formatProjectReviewHtml(displayAdapter));
    auto *reviewSplitter = new QSplitter(Qt::Horizontal, dialog);
    reviewSplitter->setChildrenCollapsible(false);
    reviewSplitter->addWidget(reportText);
    reviewSplitter->addWidget(createDocumentReviewInspector(result.report, reviewSplitter));
    reviewSplitter->setStretchFactor(0, 1);
    reviewSplitter->setStretchFactor(1, 0);
    reviewSplitter->setSizes({680, 250});
    layout->addWidget(reviewSplitter, 1);

    QStringList warningText;
    for (const QJsonValue &warning : result.report.value(QStringLiteral("warnings")).toArray()) {
        if (warning.isString()) {
            warningText.append(warning.toString());
        }
    }
    if (!warningText.isEmpty()) {
        auto *warningLabel = new QLabel(QStringLiteral("审查边界：%1").arg(warningText.join(QStringLiteral("\n"))), dialog);
        warningLabel->setWordWrap(true);
        polishBadge(warningLabel, QStringLiteral("badgeGray"));
        layout->addWidget(warningLabel);
    }

    auto *buttonBox = new QDialogButtonBox(QDialogButtonBox::Close, dialog);
    if (!result.taskId.isEmpty()) {
        auto *historyButton = buttonBox->addButton(QStringLiteral("查看任务历史"), QDialogButtonBox::ActionRole);
        historyButton->setIcon(style()->standardIcon(QStyle::SP_FileDialogDetailedView));
        historyButton->setToolTip(QStringLiteral("打开本次审查的完整执行记录、证据与产物。"));
        connect(historyButton, &QPushButton::clicked, this, [this, dialog, taskId = result.taskId]() {
            dialog->accept();
            openTaskInHistory(taskId);
        });
    }
    connect(buttonBox, &QDialogButtonBox::rejected, dialog, &QDialog::reject);
    layout->addWidget(buttonBox);
    dialog->open();
}

QString MainWindow::formatProjectReviewHtml(const ProjectReviewResult &result) const
{
    const QJsonObject report = result.report;
    const QJsonArray findings = report.value(QStringLiteral("findings")).toArray();
    const QJsonArray checks = report.value(QStringLiteral("checks")).toArray();
    QStringList blocks;

    auto formatSources = [](const QJsonArray &sources) {
        QStringList labels;
        for (const QJsonValue &value : sources) {
            const QJsonObject source = value.toObject();
            QString locator = source.value(QStringLiteral("source_locator")).toString();
            if (locator.isEmpty()) {
                const int start = source.value(QStringLiteral("start_line")).toInt();
                const int end = source.value(QStringLiteral("end_line")).toInt();
                locator = start == end ? QStringLiteral("第 %1 行").arg(start)
                                       : QStringLiteral("第 %1-%2 行").arg(start).arg(end);
            }
            const QString path = source.value(QStringLiteral("relative_path")).toString();
            if (!path.isEmpty()) {
                labels.append(QStringLiteral("%1 · %2").arg(path.toHtmlEscaped(), locator.toHtmlEscaped()));
            }
        }
        return labels.join(QStringLiteral("；"));
    };

    blocks.append(QStringLiteral("<h3 style=\"color:#14224A;margin:4px 0 10px 0;\">待处理问题</h3>"));
    if (findings.isEmpty()) {
        blocks.append(QStringLiteral("<div style=\"padding:14px;border:1px solid #BBF7D0;border-radius:10px;background:#F0FDF4;color:#166534;\">本轮规则检查未发现需要补充的项目文档问题。请继续人工确认实际执行细节。</div>"));
    } else {
        for (const QJsonValue &value : findings) {
            const QJsonObject finding = value.toObject();
            const QString severity = finding.value(QStringLiteral("severity")).toString();
            const QString severityText = severity == QStringLiteral("high") ? QStringLiteral("高优先级")
                : severity == QStringLiteral("medium") ? QStringLiteral("中优先级")
                                                         : QStringLiteral("低优先级");
            const QString color = severity == QStringLiteral("high") ? QStringLiteral("#B91C1C")
                : severity == QStringLiteral("medium") ? QStringLiteral("#B45309")
                                                           : QStringLiteral("#1D4ED8");
            blocks.append(
                QStringLiteral("<section style=\"margin:0 0 12px 0;padding:13px 14px;border:1px solid #DDEBFA;border-radius:10px;background:#FFFFFF;\">"
                               "<div style=\"color:%1;font-size:12px;font-weight:700;\">%2</div>"
                               "<div style=\"font-size:17px;font-weight:700;color:#14224A;margin-top:3px;\">%3</div>"
                               "<p style=\"color:#334155;margin:8px 0 4px 0;\">%4</p>"
                               "<div style=\"color:#1E40AF;margin-top:6px;\"><b>建议：</b>%5</div>"
                               "<div style=\"color:#64748B;font-size:12px;margin-top:8px;\">依据：%6<br/>来源：%7</div></section>")
                    .arg(color, severityText, finding.value(QStringLiteral("title")).toString().toHtmlEscaped(),
                         finding.value(QStringLiteral("detail")).toString().toHtmlEscaped(),
                         finding.value(QStringLiteral("suggestion")).toString().toHtmlEscaped(),
                         finding.value(QStringLiteral("evidence")).toString().toHtmlEscaped(),
                         formatSources(finding.value(QStringLiteral("source_refs")).toArray())));
        }
    }

    blocks.append(QStringLiteral("<h3 style=\"color:#14224A;margin:20px 0 10px 0;\">质量检查清单</h3>"));
    for (const QJsonValue &value : checks) {
        const QJsonObject check = value.toObject();
        const bool passed = check.value(QStringLiteral("status")).toString() == QStringLiteral("passed");
        const QString color = passed ? QStringLiteral("#15803D") : QStringLiteral("#B45309");
        const QString statusText = passed ? QStringLiteral("已识别") : QStringLiteral("需关注");
        blocks.append(
            QStringLiteral("<div style=\"margin:0 0 8px 0;padding:10px 12px;border:1px solid #E2E8F0;border-radius:8px;background:#FFFFFF;\">"
                           "<span style=\"color:%1;font-weight:700;\">%2</span> · <b style=\"color:#14224A;\">%3</b><br/>"
                           "<span style=\"color:#475569;\">%4</span><br/>"
                           "<span style=\"color:#64748B;font-size:12px;\">%5</span></div>")
                .arg(color, statusText, check.value(QStringLiteral("label")).toString().toHtmlEscaped(),
                     check.value(QStringLiteral("message")).toString().toHtmlEscaped(),
                     formatSources(check.value(QStringLiteral("source_refs")).toArray())));
    }
    return blocks.join(QString());
}

void MainWindow::updateProjectReviewAction()
{
    bool hasResultDocument = false;
    for (const QJsonValue &value : currentDocumentResultContext.value(QStringLiteral("documents")).toArray()) {
        if (!value.toString().trimmed().isEmpty()) {
            hasResultDocument = true;
            break;
        }
    }
    const bool hasSelectedDocument = !ui->documentDocumentCombo->currentData().toString().trimmed().isEmpty();
    const bool hasDocument = hasSelectedDocument || hasResultDocument;
    const bool reviewLoading = projectDocumentReviewLoading || paperReviewLoading;
    const bool enabled = hasDocument && !documentAgentRunning && !reviewLoading;
    const bool presentationEnabled = hasSelectedDocument && !documentAgentRunning && !reviewLoading
        && !documentPresentationPreviewLoading && !documentPresentationExporting;
    ui->documentResultDetailProjectReviewButton->setEnabled(enabled);
    ui->documentCreatePresentationButton->setEnabled(presentationEnabled);
    ui->documentProjectReviewButton->setEnabled(enabled);
    ui->documentPaperReviewButton->setEnabled(enabled);
    if (documentPaperReviewAction) {
        documentPaperReviewAction->setEnabled(enabled);
    }
    if (reviewLoading) {
        // 审查绑定“本次明确选择的一份材料”。执行期间冻结选择和导入，避免用户视觉上切到另一份
        // 文件、后台却仍在审查旧材料；完成或失败后会恢复正常选择。
        ui->documentDocumentCombo->setEnabled(false);
        ui->documentImportButton->setEnabled(false);
        ui->documentRefreshButton->setEnabled(false);
        ui->documentResultDetailProjectReviewButton->setText(QStringLiteral("审查中…"));
        ui->documentResultDetailProjectReviewButton->setToolTip(
            QStringLiteral("正在读取受控材料并运行审查规则，请勿重复提交"));
        ui->documentProjectReviewButton->setText(QStringLiteral("审查中…"));
        ui->documentPaperReviewButton->setText(QStringLiteral("审查中…"));
    } else {
        if (!documentAgentRunning) {
            ui->documentDocumentCombo->setEnabled(ui->documentDocumentCombo->count() > 0);
            ui->documentImportButton->setEnabled(true);
            ui->documentRefreshButton->setEnabled(true);
        }
        ui->documentResultDetailProjectReviewButton->setText(QStringLiteral("审查报告"));
        ui->documentResultDetailProjectReviewButton->setToolTip(
            hasDocument
                ? QStringLiteral("按需生成完整的项目审查报告；制作 PPT 时会自动执行轻量交付预检")
                : QStringLiteral("请先选择一份已导入的项目材料，再生成审查报告"));
        ui->documentProjectReviewButton->setText(QStringLiteral("生成审查报告"));
        ui->documentProjectReviewButton->setToolTip(
            hasDocument ? QStringLiteral("按需生成完整、可追溯的项目审查报告，不修改原文件")
                        : QStringLiteral("请先选择一份已导入的项目材料"));
        ui->documentPaperReviewButton->setText(QStringLiteral("开始论文审查"));
        ui->documentPaperReviewButton->setToolTip(
            hasDocument ? QStringLiteral("只读检查当前材料，不作查重、创新性或学术结论判断")
                        : QStringLiteral("请先选择一份已导入的论文或学术报告"));
    }
    ui->documentCreatePresentationButton->setText(
        documentPresentationDraftRequested ? QStringLiteral("准备中…") : QStringLiteral("开始制作"));
    ui->documentCreatePresentationButton->setToolTip(
        presentationEnabled
            ? QStringLiteral("先建立带来源的方案草稿，再预览并确认导出可编辑 PPTX")
            : QStringLiteral("请先选择一份材料；审查、草稿或导出任务完成后可继续制作"));
    updateDocumentActivityState();
}

void MainWindow::handleDocumentAgentCompleted(const DocumentAgentRunResult &result)
{
    const bool completedPresentationDraft = documentPresentationDraftRequested;
    const bool completedSectionDraft = documentSectionDraftRunning;
    const bool completedSectionReview = documentSectionReviewRunning;
    const bool completedSectionRevision = documentSectionRevisionRunning;
    const bool completedManualRevision = documentManualRevisionRunning;
    const bool completedDraftTemplate = documentDraftTemplateRunning;
    const bool completedDraftMerge = documentDraftMergeRunning;
    const bool completedDraftReview = documentDraftReviewRunning;
    const bool completedDraftRestore = documentDraftRestoreRunning;
    documentAgentRunning = false;
    documentSectionDraftRunning = false;
    documentSectionReviewRunning = false;
    documentSectionRevisionRunning = false;
    documentManualRevisionRunning = false;
    documentDraftTemplateRunning = false;
    documentDraftMergeRunning = false;
    documentDraftReviewRunning = false;
    documentDraftRestoreRunning = false;
    documentPresentationDraftRequested = false;
    activeDocumentAgentTaskId.clear();
    ui->documentInput->setEnabled(true);
    ui->documentOutputModeCombo->setEnabled(true);
    ui->documentDocumentCombo->setEnabled(ui->documentDocumentCombo->count() > 0);
    ui->documentComparisonList->setEnabled(ui->documentComparisonList->count() > 0);
    ui->documentImportButton->setEnabled(true);
    ui->documentRefreshButton->setEnabled(true);
    updateDocumentAgentSelectionUi();
    currentDocumentResultTaskId = result.taskId;
    currentDocumentResultContext = result.documentContext;
    documentDraftSaving = false;
    documentDraftSaved = false;
    documentPresentationPreviewLoading = false;
    documentPresentationExporting = false;
    projectDocumentReviewLoading = false;
    paperReviewLoading = false;
    documentPresentationTaskId.clear();
    documentPresentationPlanId.clear();
    if (documentPresentationDialog) {
        documentPresentationDialog->close();
    }
    if (projectDocumentReviewDialog) {
        projectDocumentReviewDialog->close();
    }
    if (paperReviewDialog) {
        paperReviewDialog->close();
    }
    lastSavedDocumentDraftFilename.clear();
    updateDocumentDraftSaveAction();
    setDocumentResultHtml(
        formatDocumentAgentResultHtml(result),
        result.status == QStringLiteral("completed")
            ? completedDraftReview ? QStringLiteral("草稿核验完成 · 可追溯")
            : completedSectionReview ? QStringLiteral("本章审校完成 · 可追溯")
            : completedSectionRevision ? QStringLiteral("修订预览完成 · 可另存")
            : completedManualRevision ? QStringLiteral("手动修订预览 · 待核验")
            : completedDraftTemplate ? QStringLiteral("模板交付预览 · 可另存")
            : completedDraftMerge ? QStringLiteral("章节合并预览 · 可另存")
            : completedDraftRestore ? QStringLiteral("恢复预览完成 · 可另存")
            : completedSectionDraft ? QStringLiteral("本章预览完成 · 可追溯")
                                    : QStringLiteral("分析完成 · 可追溯")
                                                      : QStringLiteral("结果需要补充或复核"),
        true);
    updateDocumentResultDetailSections(result.documentContext, true);

    if (result.status == QStringLiteral("completed")) {
        ui->documentRunStatus->setText(
            completedPresentationDraft ? QStringLiteral("项目方案草稿已核验 · 正在准备可确认的 PPT 计划")
            : completedDraftReview ? QStringLiteral("草稿核验完成 · 原草稿未改动，已记录到任务历史")
            : completedSectionReview ? QStringLiteral("本章审校完成 · 原草稿未改动，已记录到任务历史")
            : completedSectionRevision ? QStringLiteral("修订预览完成 · 原草稿未改动，可另存为新 Markdown 版本")
            : completedManualRevision ? QStringLiteral("手动修订预览已建立 · 请先核验事实，暂不能保存")
            : completedDraftTemplate ? QStringLiteral("模板交付预览已建立 · 请核对待补充章节后另存 Markdown")
            : completedDraftMerge ? QStringLiteral("章节合并预览已建立 · 已记录冲突选择，可另存 Markdown")
            : completedDraftRestore ? QStringLiteral("恢复预览完成 · 历史草稿未改动，可另存为新 Markdown 版本")
            : completedSectionDraft ? QStringLiteral("本章预览完成 · 原草稿未改动，已记录到任务历史")
                                  : QStringLiteral("分析完成 · 已记录到任务历史"));
        polishBadge(ui->documentRunStatus,
                    completedManualRevision ? QStringLiteral("badgeOrange") : QStringLiteral("badgeGreen"));
        if (completedPresentationDraft && ui->documentResultDetailPresentationButton->isEnabled()) {
            // 草稿已通过同一份来源契约后才建立幻灯片计划；计划仍是只读预览，真正写入必须用户确认。
            QTimer::singleShot(0, this, &MainWindow::requestDocumentPresentationPreview);
        }
        return;
    }

    ui->documentRunStatus->setText(
        result.status == QStringLiteral("needs_clarification")
            ? QStringLiteral("需要补充材料选择或问题描述")
            : QStringLiteral("分析未完成 · 请查看结果中的说明"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeOrange"));
}

void MainWindow::handleDocumentAgentStarted(const DocumentAgentTaskStartResult &result)
{
    activeDocumentAgentTaskId = result.taskId;
    ui->documentRunStatus->setText(
        documentPresentationDraftRequested ? QStringLiteral("正在起草项目方案 · 已连接实时进度")
        : documentDraftReviewRunning ? QStringLiteral("草稿核验中 · 已连接实时进度")
        : documentSectionReviewRunning ? QStringLiteral("本章审校中 · 已连接实时进度")
        : documentSectionRevisionRunning ? QStringLiteral("修订预览中 · 已连接实时进度")
        : documentManualRevisionRunning ? QStringLiteral("手动修订预览中 · 已连接实时进度")
        : documentDraftTemplateRunning ? QStringLiteral("模板化交付预览中 · 已连接实时进度")
        : documentDraftMergeRunning ? QStringLiteral("章节合并预览中 · 已连接实时进度")
        : documentDraftRestoreRunning ? QStringLiteral("恢复预览中 · 已连接实时进度")
        : documentSectionDraftRunning ? QStringLiteral("本章撰写中 · 已连接实时进度")
                                    : QStringLiteral("分析中 · 已连接实时进度"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    backendClient->connectTaskLog(result.taskId);
}

void MainWindow::handleDocumentAgentStillRunning(const QString &taskId, const QString &status)
{
    if (!documentAgentRunning || taskId != activeDocumentAgentTaskId) {
        return;
    }
    ui->documentRunStatus->setText(
        documentPresentationDraftRequested
            ? status == QStringLiteral("queued") ? QStringLiteral("项目方案草稿任务排队中")
                                                : QStringLiteral("正在核对材料并起草项目方案")
        : documentDraftReviewRunning
            ? status == QStringLiteral("queued") ? QStringLiteral("草稿核验任务排队中")
                                             : QStringLiteral("正在核对草稿事实与来源")
        : documentSectionReviewRunning
            ? status == QStringLiteral("queued") ? QStringLiteral("本章审校任务排队中")
                                                 : QStringLiteral("正在核对本章问题与来源")
        : documentSectionRevisionRunning
            ? status == QStringLiteral("queued") ? QStringLiteral("修订预览任务排队中")
                                                 : QStringLiteral("正在校验候选片段并建立前后差异")
        : documentManualRevisionRunning
            ? status == QStringLiteral("queued") ? QStringLiteral("手动修订预览任务排队中")
                                                 : QStringLiteral("正在绑定原草稿快照并建立待核验版本")
        : documentDraftTemplateRunning
            ? status == QStringLiteral("queued") ? QStringLiteral("模板化交付预览任务排队中")
                                                 : QStringLiteral("正在重组已核验章节与来源")
        : documentDraftMergeRunning
            ? status == QStringLiteral("queued") ? QStringLiteral("章节合并预览任务排队中")
                                                 : QStringLiteral("正在校验共同祖先并按确认选择合并章节")
        : documentDraftRestoreRunning
            ? status == QStringLiteral("queued") ? QStringLiteral("恢复预览任务排队中")
                                                 : QStringLiteral("正在校验历史快照并建立独立版本")
        : documentSectionDraftRunning
            ? status == QStringLiteral("queued") ? QStringLiteral("本章任务排队中")
                                                 : QStringLiteral("正在核对本章预览与来源")
            : status == QStringLiteral("queued") ? QStringLiteral("任务排队中")
                                                 : QStringLiteral("正在等待可验证结果"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
    // WebSocket 意外断开时以低频查询补终态，避免用户只能手动刷新；正常流程不会走这里。
    QTimer::singleShot(500, this, [this, taskId]() {
        if (documentAgentRunning && taskId == activeDocumentAgentTaskId) {
            backendClient->requestDocumentAgentResult(taskId);
        }
    });
}

void MainWindow::handleDocumentAgentFailed(const QString &message)
{
    const bool failedPresentationDraft = documentPresentationDraftRequested;
    const bool failedSectionDraft = documentSectionDraftRunning;
    const bool failedSectionReview = documentSectionReviewRunning;
    const bool failedSectionRevision = documentSectionRevisionRunning;
    const bool failedManualRevision = documentManualRevisionRunning;
    const bool failedDraftTemplate = documentDraftTemplateRunning;
    const bool failedDraftMerge = documentDraftMergeRunning;
    const bool failedDraftReview = documentDraftReviewRunning;
    const bool failedDraftRestore = documentDraftRestoreRunning;
    documentAgentRunning = false;
    documentSectionDraftRunning = false;
    documentSectionReviewRunning = false;
    documentSectionRevisionRunning = false;
    documentManualRevisionRunning = false;
    documentDraftTemplateRunning = false;
    documentDraftMergeRunning = false;
    documentDraftReviewRunning = false;
    documentDraftRestoreRunning = false;
    documentPresentationDraftRequested = false;
    activeDocumentAgentTaskId.clear();
    ui->documentInput->setEnabled(true);
    ui->documentOutputModeCombo->setEnabled(true);
    ui->documentDocumentCombo->setEnabled(ui->documentDocumentCombo->count() > 0);
    ui->documentComparisonList->setEnabled(ui->documentComparisonList->count() > 0);
    ui->documentImportButton->setEnabled(true);
    ui->documentRefreshButton->setEnabled(true);
    updateDocumentAgentSelectionUi();
    if (failedSectionDraft || failedSectionReview || failedSectionRevision || failedManualRevision || failedDraftTemplate || failedDraftMerge || failedDraftReview || failedDraftRestore) {
        // 派生章节任务没有权限改动原结果；失败时保留用户正在审阅的草稿而不是清空整个详情页。
        updateDocumentDraftSaveAction();
        ui->documentRunStatus->setText(
            failedDraftReview ? QStringLiteral("草稿核验失败 · 原草稿仍可继续审阅或保存")
            : failedSectionReview ? QStringLiteral("本章审校失败 · 原草稿仍可继续审阅或保存")
            : failedSectionRevision ? QStringLiteral("修订预览失败 · 原草稿仍可继续审阅或保存")
            : failedManualRevision ? QStringLiteral("手动修订预览失败 · 原草稿仍可继续审阅或保存")
            : failedDraftTemplate ? QStringLiteral("模板交付预览失败 · 原草稿仍可继续审阅或保存")
            : failedDraftMerge ? QStringLiteral("章节合并预览失败 · 原草稿仍可继续审阅或保存")
            : failedDraftRestore ? QStringLiteral("恢复预览失败 · 历史草稿仍可继续审阅或保存")
                                  : QStringLiteral("本章创作失败 · 原草稿仍可继续审阅或保存"));
        polishBadge(ui->documentRunStatus, QStringLiteral("badgeOrange"));
        ui->documentResultDetailStatus->setText(
            failedSectionReview ? QStringLiteral("本章审校失败 · 原草稿未改动")
                                : failedSectionRevision ? QStringLiteral("修订预览失败 · 原草稿未改动")
                                : failedManualRevision ? QStringLiteral("手动修订预览失败 · 原草稿未改动")
                                : failedDraftTemplate ? QStringLiteral("模板交付预览失败 · 原草稿未改动")
                                : failedDraftMerge ? QStringLiteral("章节合并预览失败 · 原草稿未改动")
                                : failedDraftRestore ? QStringLiteral("恢复预览失败 · 历史草稿未改动")
                                : QStringLiteral("本章创作失败 · 原草稿未改动"));
        polishBadge(ui->documentResultDetailStatus, QStringLiteral("badgeOrange"));
        QMessageBox::warning(
            this,
            failedDraftReview ? QStringLiteral("草稿事实核验失败")
            : failedSectionReview ? QStringLiteral("本章审校失败")
            : failedSectionRevision ? QStringLiteral("修订预览失败")
            : failedManualRevision ? QStringLiteral("手动修订预览失败")
            : failedDraftTemplate ? QStringLiteral("模板交付预览失败")
            : failedDraftMerge ? QStringLiteral("章节合并预览失败")
            : failedDraftRestore ? QStringLiteral("恢复预览失败")
                                  : QStringLiteral("本章创作失败"),
            message.isEmpty() ? QStringLiteral("后端没有返回具体错误，原草稿未被修改。") : message);
        return;
    }
    currentDocumentResultTaskId.clear();
    currentDocumentResultContext = QJsonObject{};
    documentDraftSaving = false;
    documentDraftSaved = false;
    documentPresentationPreviewLoading = false;
    documentPresentationExporting = false;
    projectDocumentReviewLoading = false;
    paperReviewLoading = false;
    documentPresentationTaskId.clear();
    documentPresentationPlanId.clear();
    if (documentPresentationDialog) {
        documentPresentationDialog->close();
    }
    if (projectDocumentReviewDialog) {
        projectDocumentReviewDialog->close();
    }
    if (paperReviewDialog) {
        paperReviewDialog->close();
    }
    lastSavedDocumentDraftFilename.clear();
    updateDocumentDraftSaveAction();
    ui->documentRunStatus->setText(
        failedPresentationDraft ? QStringLiteral("项目方案草稿未完成 · 请查看说明")
                                : QStringLiteral("分析失败"));
    polishBadge(ui->documentRunStatus, QStringLiteral("badgeOrange"));
    setDocumentResultHtml(
        QStringLiteral("<a name=\"conclusion\"></a><p style=\"color:#B45309;\"><b>无法完成本次分析：</b>%1</p>")
            .arg(message.toHtmlEscaped()),
        QStringLiteral("分析失败 · 请查看说明"),
        true);
    updateDocumentResultDetailSections(
        QJsonObject{},
        true,
        QStringLiteral("失败说明"),
        QStringLiteral("conclusion"));
}

void MainWindow::refreshCurrentDispatchUpdates()
{
    if (currentDispatchTaskId.isEmpty()) {
        return;
    }

    backendClient->requestTaskUpdates(currentDispatchTaskId);
}

void MainWindow::scheduleDispatchUpdatesRefresh(int delayMs)
{
    if (!dispatchUpdateRefreshTimer || currentDispatchTaskId.isEmpty()) {
        return;
    }

    dispatchUpdateRefreshTimer->start(qMax(0, delayMs));
}

bool MainWindow::shouldPollCurrentDispatchUpdates() const
{
    if (currentDispatchTaskId.isEmpty() || currentDispatchNeedsClarification) {
        return false;
    }

    // blocked 需要用户先到历史页处理，不在调度台持续空轮询；waiting_permission 仍轮询，
    // 这样用户在历史页批准后，调度台能自动恢复到 running/completed。
    return currentDispatchRuntimeStatus != QStringLiteral("completed")
        && currentDispatchRuntimeStatus != QStringLiteral("failed")
        && currentDispatchRuntimeStatus != QStringLiteral("cancelled")
        && currentDispatchRuntimeStatus != QStringLiteral("paused")
        && currentDispatchRuntimeStatus != QStringLiteral("blocked");
}

void MainWindow::updateDispatchActionButtons()
{
    const bool hasTask = !currentDispatchTaskId.isEmpty();
    const bool runtimeTask = currentDispatchRuntimeMode == QStringLiteral("runtime")
        || currentDispatchExecutionSubmitted;
    const bool directKnowledgeAnswer = currentDispatchDirectKnowledgeAnswer;
    const bool autoReadOnlyTask = isCurrentDispatchAutoReadOnlyTask();
    const bool directConversation = isCurrentDispatchDirectConversation();
    const bool presentationHandoff = currentDispatchPresentationHandoff;
    const bool compositionRuntimeRequired = currentDispatchPlanSummary.executionReadiness
        == QStringLiteral("requires_composition_runtime");
    if (ui->dispatchPlanButton) {
        // 已进入 Runtime 的版本仍可查看和复盘，但修改入口由详情窗明确切成只读。
        ui->dispatchPlanButton->setEnabled(hasTask);
        ui->dispatchPlanButton->setText(QStringLiteral("查看计划"));
        ui->dispatchPlanButton->setToolTip(
            !hasTask ? QStringLiteral("先在调度台生成一个任务计划。")
                     : (autoReadOnlyTask
                            ? QStringLiteral("查看本次只读任务的材料范围、结论与执行详情。")
                     : (runtimeTask
                            ? QStringLiteral("查看不可变计划版本。真实执行已开始，当前计划不可再修改。")
                            : QStringLiteral("查看当前计划版本，或在执行前修改目标后由总指挥重新规划。"))));
    }
    if (ui->taskSettingButton) {
        ui->taskSettingButton->setEnabled(hasTask);
        if (!hasTask) {
            ui->taskSettingButton->setText(QStringLiteral("查看历史"));
            ui->taskSettingButton->setToolTip(QStringLiteral("先在调度台生成一个任务，再定位到历史详情。"));
        } else if (currentDispatchHasPendingPermission) {
            ui->taskSettingButton->setText(QStringLiteral("处理权限"));
            ui->taskSettingButton->setToolTip(QStringLiteral("打开历史任务页，查看并确认当前 Runtime 的权限请求。"));
        } else if (directKnowledgeAnswer && currentDispatchKnowledgeAnswerDelivered) {
            ui->taskSettingButton->setText(QStringLiteral("查看来源"));
            ui->taskSettingButton->setToolTip(QStringLiteral("打开历史任务，查看本次回答的来源与执行详情。"));
        } else if (currentDispatchArtifactCount > 0) {
            ui->taskSettingButton->setText(QStringLiteral("查看产物"));
            ui->taskSettingButton->setToolTip(QStringLiteral("打开历史任务页，预览或打开当前任务生成的受控产物。"));
        } else {
            ui->taskSettingButton->setText(QStringLiteral("查看历史"));
            ui->taskSettingButton->setToolTip(QStringLiteral("打开历史任务页并定位到当前调度任务。"));
        }
    }
    if (ui->dispatchExecuteButton) {
        if (autoReadOnlyTask) {
            ui->dispatchExecuteButton->setText(
                (currentDispatchKnowledgeAnswerFailed || currentDispatchDataAnalysisFailed)
                    ? (currentDispatchDirectDataAnalysis ? QStringLiteral("分析未完成")
                                                         : QStringLiteral("检索失败"))
                    : ((currentDispatchKnowledgeAnswerDelivered || currentDispatchDataAnalysisDelivered)
                           ? (currentDispatchDirectDataAnalysis ? QStringLiteral("分析已完成")
                                                                : QStringLiteral("回答已完成"))
                           : currentDispatchAutoReadOnlyActivityText()));
        } else if (presentationHandoff) {
            ui->dispatchExecuteButton->setText(
                currentDispatchPresentationRunning
                    ? QStringLiteral("正在制作 PPT")
                    : currentDispatchPresentationCompleted ? QStringLiteral("PPT 已生成")
                                                            : QStringLiteral("PPT 制作已受理"));
        } else if (directConversation) {
            ui->dispatchExecuteButton->setText(QStringLiteral("回答已完成"));
        } else if (currentDispatchGuidedHandoff) {
            ui->dispatchExecuteButton->setText(QStringLiteral("前往数据工作台"));
        } else if (compositionRuntimeRequired) {
            ui->dispatchExecuteButton->setText(QStringLiteral("组合 Runtime 准备中"));
        } else if (currentDispatchExecutionInProgress) {
            ui->dispatchExecuteButton->setText(QStringLiteral("提交中"));
        } else if (runtimeTask && currentDispatchRuntimeStatus == QStringLiteral("waiting_permission")) {
            ui->dispatchExecuteButton->setText(QStringLiteral("等待权限"));
        } else if (runtimeTask
                   && (currentDispatchRuntimeStatus == QStringLiteral("pending")
                       || currentDispatchRuntimeStatus == QStringLiteral("running"))) {
            ui->dispatchExecuteButton->setText(QStringLiteral("执行中"));
        } else if (runtimeTask && currentDispatchRuntimeStatus == QStringLiteral("paused")) {
            ui->dispatchExecuteButton->setText(QStringLiteral("已暂停"));
        } else if (runtimeTask && currentDispatchRuntimeStatus == QStringLiteral("completed")) {
            ui->dispatchExecuteButton->setText(QStringLiteral("执行完成"));
        } else {
            ui->dispatchExecuteButton->setText(QStringLiteral("开始执行"));
        }

        ui->dispatchExecuteButton->setEnabled(!autoReadOnlyTask
                                             && !presentationHandoff
                                             && !directConversation
                                             && hasTask
                                             && !runtimeTask
                                             && !currentDispatchNeedsClarification
                                             && !compositionRuntimeRequired
                                             && !currentDispatchExecutionInProgress);
        if (!hasTask) {
            ui->dispatchExecuteButton->setToolTip(QStringLiteral("先在调度台生成一个任务计划。"));
        } else if (autoReadOnlyTask) {
            ui->dispatchExecuteButton->setToolTip(
                (currentDispatchKnowledgeAnswerFailed || currentDispatchDataAnalysisFailed)
                    ? QStringLiteral("本次只读任务未完成；请在查看历史中确认材料与运行原因。")
                    : ((currentDispatchKnowledgeAnswerDelivered || currentDispatchDataAnalysisDelivered)
                           ? QStringLiteral("本次只读任务已经完成；详情可从右侧入口查看。")
                           : QStringLiteral("已自动执行安全的单材料只读任务，无需额外确认。")));
        } else if (directConversation) {
            ui->dispatchExecuteButton->setToolTip(
                QStringLiteral("这是普通对话，回答已经直接显示；不需要启动 Runtime。"));
        } else if (presentationHandoff) {
            ui->dispatchExecuteButton->setToolTip(
                currentDispatchPresentationRunning
                    ? QStringLiteral("PPT 正在独立创作窗口中生成；主对话不会被切走。")
                    : currentDispatchPresentationCompleted
                          ? QStringLiteral("PPT 已完成，可在创作窗口或任务历史查看交付物。")
                          : QStringLiteral("PPT 制作已受理，可继续发送“开始制作”恢复创作窗口。"));
        } else if (currentDispatchNeedsClarification) {
            ui->dispatchExecuteButton->setToolTip(QStringLiteral("当前计划需要补充信息，暂不适合执行。"));
        } else if (compositionRuntimeRequired) {
            ui->dispatchExecuteButton->setToolTip(
                QStringLiteral("当前已生成多材料依赖图；真实子任务并发与最终汇总将在组合 Runtime 完成后开放。"));
        } else if (currentDispatchGuidedHandoff) {
            ui->dispatchExecuteButton->setToolTip(
                QStringLiteral("当前数据任务需要在数据工作台选择材料并继续，不会创建伪造的自动委派。"));
        } else if (currentDispatchExecutionInProgress) {
            ui->dispatchExecuteButton->setToolTip(QStringLiteral("执行请求正在提交，请稍候。"));
        } else if (runtimeTask && currentDispatchRuntimeStatus == QStringLiteral("waiting_permission")) {
            ui->dispatchExecuteButton->setToolTip(QStringLiteral("真实执行正在等待权限，请点击右侧「处理权限」。"));
        } else if (runtimeTask && currentDispatchRuntimeStatus == QStringLiteral("completed")) {
            ui->dispatchExecuteButton->setToolTip(QStringLiteral("真实执行已经完成，请点击右侧「查看产物」。"));
        } else if (runtimeTask && currentDispatchRuntimeStatus == QStringLiteral("paused")) {
            ui->dispatchExecuteButton->setToolTip(QStringLiteral("任务已暂停，请点击右侧「查看历史」后继续或取消。"));
        } else if (runtimeTask) {
            ui->dispatchExecuteButton->setToolTip(QStringLiteral("真实执行已提交，请在历史任务页查看运行状态。"));
        } else {
            ui->dispatchExecuteButton->setToolTip(
                QStringLiteral("把当前 dry-run 计划转入真实 Runtime；高风险步骤仍会等待权限确认。"));
        }
    }

    updateDispatchPlanRevisionEditor();
}

void MainWindow::openCurrentDispatchTaskInHistory()
{
    if (currentDispatchTaskId.isEmpty()) {
        ui->dispatchChatStatus->setText(QStringLiteral("暂无任务"));
        return;
    }

    openTaskInHistory(currentDispatchTaskId);
}

void MainWindow::openDispatchPlanManager()
{
    if (currentDispatchTaskId.isEmpty() || !backendClient) {
        ui->dispatchChatStatus->setText(QStringLiteral("暂无计划"));
        return;
    }

    if (dispatchPlanDialog) {
        dispatchPlanDialog->showNormal();
        dispatchPlanDialog->raise();
        dispatchPlanDialog->activateWindow();
        refreshDispatchPlanVersions();
        return;
    }

    // 计划修订属于低频的详情操作。把它放进可调整大小的 Inspector，主调度台只保留一个轻入口，
    // 避免用户在每次对话时都面对版本表、说明和确认控件。
    auto *dialog = new QDialog(this);
    dialog->setAttribute(Qt::WA_DeleteOnClose);
    dialog->setWindowTitle(QStringLiteral("任务计划与版本"));
    dialog->setMinimumSize(900, 600);
    dialog->resize(1120, 720);
    dialog->setProperty("dispatchTaskId", currentDispatchTaskId);
    dispatchPlanDialog = dialog;

    auto *rootLayout = new QVBoxLayout(dialog);
    rootLayout->setContentsMargins(24, 22, 24, 22);
    rootLayout->setSpacing(14);

    auto *titleLabel = new QLabel(QStringLiteral("任务计划"), dialog);
    titleLabel->setObjectName(QStringLiteral("sectionTitle"));
    rootLayout->addWidget(titleLabel);
    auto *subtitleLabel = new QLabel(
        QStringLiteral("版本不可覆盖；执行前可修改任务目标，由总指挥重新生成计划。"), dialog);
    subtitleLabel->setObjectName(QStringLiteral("tinyText"));
    subtitleLabel->setWordWrap(true);
    rootLayout->addWidget(subtitleLabel);

    auto *statusLabel = new QLabel(dialog);
    statusLabel->setObjectName(QStringLiteral("tinyText"));
    statusLabel->setWordWrap(true);
    dispatchPlanStatusLabel = statusLabel;
    rootLayout->addWidget(statusLabel);

    auto *splitter = new QSplitter(Qt::Horizontal, dialog);
    splitter->setChildrenCollapsible(false);
    rootLayout->addWidget(splitter, 1);

    auto *versionsPane = new QWidget(splitter);
    auto *versionsLayout = new QVBoxLayout(versionsPane);
    versionsLayout->setContentsMargins(0, 0, 0, 0);
    versionsLayout->setSpacing(8);
    auto *versionsTitle = new QLabel(QStringLiteral("版本记录"), versionsPane);
    versionsTitle->setObjectName(QStringLiteral("cardTitle"));
    versionsLayout->addWidget(versionsTitle);

    auto *versionTable = new QTableWidget(versionsPane);
    versionTable->setColumnCount(4);
    versionTable->setHorizontalHeaderLabels(
        {QStringLiteral("版本"), QStringLiteral("修改说明"), QStringLiteral("状态"), QStringLiteral("时间")});
    versionTable->setSelectionBehavior(QAbstractItemView::SelectRows);
    versionTable->setSelectionMode(QAbstractItemView::SingleSelection);
    versionTable->setEditTriggers(QAbstractItemView::NoEditTriggers);
    versionTable->setAlternatingRowColors(true);
    versionTable->verticalHeader()->setVisible(false);
    versionTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::ResizeToContents);
    versionTable->horizontalHeader()->setSectionResizeMode(1, QHeaderView::Stretch);
    versionTable->horizontalHeader()->setSectionResizeMode(2, QHeaderView::ResizeToContents);
    versionTable->horizontalHeader()->setSectionResizeMode(3, QHeaderView::ResizeToContents);
    versionsLayout->addWidget(versionTable, 1);
    dispatchPlanVersionTable = versionTable;

    auto *previewPane = new QWidget(splitter);
    auto *previewLayout = new QVBoxLayout(previewPane);
    previewLayout->setContentsMargins(0, 0, 0, 0);
    previewLayout->setSpacing(10);
    auto *previewTitle = new QLabel(QStringLiteral("计划详情"), previewPane);
    previewTitle->setObjectName(QStringLiteral("cardTitle"));
    previewLayout->addWidget(previewTitle);
    // 阅读正文与修改计划曾被放在同一个纵向布局中，窗口较矮时会互相挤压。计划是低频
    // Inspector，因此用标签隔离两种任务：默认给阅读足够空间，修改页独立滚动。
    auto *previewTabs = new QTabWidget(previewPane);
    previewTabs->setDocumentMode(true);
    previewTabs->setUsesScrollButtons(true);
    previewLayout->addWidget(previewTabs, 1);

    auto *detailPage = new QWidget(previewTabs);
    auto *detailLayout = new QVBoxLayout(detailPage);
    detailLayout->setContentsMargins(0, 4, 0, 0);
    auto *preview = new QTextBrowser(detailPage);
    preview->setOpenExternalLinks(false);
    preview->setHtml(QStringLiteral("<p style=\"color:#64748B;\">正在读取当前计划...</p>"));
    detailLayout->addWidget(preview, 1);
    previewTabs->addTab(detailPage, QStringLiteral("计划详情"));
    dispatchPlanPreview = preview;

    auto *revisionScroll = new QScrollArea(previewTabs);
    revisionScroll->setWidgetResizable(true);
    revisionScroll->setFrameShape(QFrame::NoFrame);
    auto *revisionFrame = new QFrame(revisionScroll);
    revisionFrame->setObjectName(QStringLiteral("contentCard"));
    auto *revisionLayout = new QVBoxLayout(revisionFrame);
    revisionLayout->setContentsMargins(16, 14, 16, 14);
    revisionLayout->setSpacing(8);
    auto *revisionTitle = new QLabel(QStringLiteral("执行前修改目标"), revisionFrame);
    revisionTitle->setObjectName(QStringLiteral("cardTitle"));
    revisionLayout->addWidget(revisionTitle);
    auto *revisionHint = new QLabel(
        QStringLiteral("不会直接编辑步骤、权限或文件范围；确认后会生成下一版 dry-run 计划。"), revisionFrame);
    revisionHint->setObjectName(QStringLiteral("tinyText"));
    revisionHint->setWordWrap(true);
    revisionLayout->addWidget(revisionHint);

    auto *goalInput = new QPlainTextEdit(revisionFrame);
    goalInput->setPlaceholderText(QStringLiteral("新的任务目标"));
    goalInput->setMaximumBlockCount(30);
    goalInput->setFixedHeight(74);
    goalInput->setPlainText(currentDispatchUserGoal.isEmpty()
                                ? currentDispatchPlanSummary.userGoal
                                : currentDispatchUserGoal);
    revisionLayout->addWidget(goalInput);
    dispatchPlanGoalInput = goalInput;

    auto *changeInput = new QLineEdit(revisionFrame);
    changeInput->setPlaceholderText(QStringLiteral("本次为什么需要调整？例如：改为先提取风险，再生成交付清单"));
    changeInput->setMaxLength(400);
    revisionLayout->addWidget(changeInput);
    dispatchPlanChangeSummaryInput = changeInput;

    auto *revisionActions = new QHBoxLayout();
    auto *revisionState = new QLabel(QStringLiteral("修改会保留当前版本，便于复盘。"), revisionFrame);
    revisionState->setObjectName(QStringLiteral("tinyText"));
    revisionState->setWordWrap(true);
    revisionActions->addWidget(revisionState, 1);
    auto *revisionButton = new QPushButton(QStringLiteral("确认并生成新计划"), revisionFrame);
    revisionButton->setObjectName(QStringLiteral("primaryButton"));
    revisionButton->setMinimumHeight(36);
    revisionActions->addWidget(revisionButton);
    revisionLayout->addLayout(revisionActions);
    dispatchPlanRevisionButton = revisionButton;
    revisionScroll->setWidget(revisionFrame);
    previewTabs->addTab(revisionScroll, QStringLiteral("修改目标"));

    splitter->addWidget(versionsPane);
    splitter->addWidget(previewPane);
    splitter->setStretchFactor(0, 3);
    splitter->setStretchFactor(1, 7);
    splitter->setSizes({330, 730});

    auto *footerLayout = new QHBoxLayout();
    footerLayout->addStretch(1);
    auto *closeButton = new QPushButton(QStringLiteral("关闭"), dialog);
    closeButton->setObjectName(QStringLiteral("ghostButton"));
    footerLayout->addWidget(closeButton);
    rootLayout->addLayout(footerLayout);

    connect(closeButton, &QPushButton::clicked, dialog, &QDialog::close);
    connect(revisionButton, &QPushButton::clicked, this, &MainWindow::submitDispatchPlanRevision);
    connect(versionTable,
            &QTableWidget::currentCellChanged,
            this,
            [this, dialog](int currentRow, int, int, int) {
                if (!dialog || currentRow < 0 || !dispatchPlanVersionTable) {
                    return;
                }
                QTableWidgetItem *item = dispatchPlanVersionTable->item(currentRow, 0);
                const int version = item ? item->data(Qt::UserRole).toInt() : 0;
                if (version > 0 && dialog->property("dispatchTaskId").toString() == currentDispatchTaskId) {
                    backendClient->requestTaskPlanVersion(currentDispatchTaskId, version);
                }
            });
    connect(dialog, &QObject::destroyed, this, [this]() {
        dispatchPlanDialog = nullptr;
        dispatchPlanVersionTable = nullptr;
        dispatchPlanPreview = nullptr;
        dispatchPlanGoalInput = nullptr;
        dispatchPlanChangeSummaryInput = nullptr;
        dispatchPlanRevisionButton = nullptr;
        dispatchPlanStatusLabel = nullptr;
        currentDispatchPlanVersions.clear();
        dispatchPlanVersionsLoading = false;
        dispatchPlanRevisionSubmitting = false;
    });

    updateDispatchPlanRevisionEditor();
    dialog->show();
    refreshDispatchPlanVersions();
}

void MainWindow::refreshDispatchPlanVersions()
{
    if (!dispatchPlanDialog || !backendClient || currentDispatchTaskId.isEmpty()) {
        return;
    }
    if (dispatchPlanDialog->property("dispatchTaskId").toString() != currentDispatchTaskId) {
        return;
    }

    dispatchPlanVersionsLoading = true;
    if (dispatchPlanStatusLabel) {
        dispatchPlanStatusLabel->setText(QStringLiteral("正在读取计划版本..."));
    }
    backendClient->requestTaskPlanVersions(currentDispatchTaskId);
}

void MainWindow::handleDispatchPlanVersionsReceived(const WorkflowPlanVersionListResult &result)
{
    if (!dispatchPlanDialog
        || result.taskId != currentDispatchTaskId
        || dispatchPlanDialog->property("dispatchTaskId").toString() != result.taskId) {
        return;
    }

    dispatchPlanVersionsLoading = false;
    currentDispatchPlanVersions = result.versions;
    if (!dispatchPlanVersionTable) {
        return;
    }

    QSignalBlocker blocker(dispatchPlanVersionTable);
    dispatchPlanVersionTable->setRowCount(currentDispatchPlanVersions.size());
    int currentRow = -1;
    for (int row = 0; row < currentDispatchPlanVersions.size(); ++row) {
        const WorkflowPlanVersionInfo &version = currentDispatchPlanVersions.at(row);
        auto *numberItem = new QTableWidgetItem(QStringLiteral("v%1").arg(version.planVersion));
        numberItem->setData(Qt::UserRole, version.planVersion);
        dispatchPlanVersionTable->setItem(row, 0, numberItem);
        dispatchPlanVersionTable->setItem(
            row,
            1,
            new QTableWidgetItem(version.changeSummary.isEmpty()
                                     ? QStringLiteral("初始计划")
                                     : version.changeSummary));
        dispatchPlanVersionTable->setItem(
            row,
            2,
            new QTableWidgetItem(version.current ? QStringLiteral("当前") : QStringLiteral("历史")));
        dispatchPlanVersionTable->setItem(row, 3, new QTableWidgetItem(version.createdAt));
        if (version.current || version.planId == result.currentPlanId) {
            currentRow = row;
        }
    }

    if (dispatchPlanStatusLabel) {
        dispatchPlanStatusLabel->setText(
            QStringLiteral("已加载 %1 个不可变版本。当前计划可在执行前修改一次目标后重新生成。").arg(
                currentDispatchPlanVersions.size()));
    }
    if (currentRow < 0 && !currentDispatchPlanVersions.isEmpty()) {
        currentRow = 0;
    }
    if (currentRow >= 0) {
        dispatchPlanVersionTable->setCurrentCell(currentRow, 0);
        const int version = currentDispatchPlanVersions.at(currentRow).planVersion;
        backendClient->requestTaskPlanVersion(result.taskId, version);
    }
}

void MainWindow::handleDispatchPlanVersionsFailed(const QString &message)
{
    if (!dispatchPlanDialog) {
        return;
    }
    dispatchPlanVersionsLoading = false;
    if (dispatchPlanStatusLabel) {
        dispatchPlanStatusLabel->setText(QStringLiteral("无法读取计划版本：%1").arg(message));
    }
}

void MainWindow::handleDispatchPlanVersionReceived(const WorkflowPlanDetailResult &result)
{
    if (!dispatchPlanDialog
        || result.taskId != currentDispatchTaskId
        || dispatchPlanDialog->property("dispatchTaskId").toString() != result.taskId) {
        return;
    }
    if (dispatchPlanPreview) {
        dispatchPlanPreview->setHtml(formatDispatchWorkflowPlanHtml(result.planSummary, result.steps));
    }
}

void MainWindow::handleDispatchPlanVersionFailed(const QString &message)
{
    if (!dispatchPlanDialog) {
        return;
    }
    if (dispatchPlanPreview) {
        dispatchPlanPreview->setHtml(
            QStringLiteral("<p style=\"color:#C2410C;\"><b>无法读取该版本：</b>%1</p>")
                .arg(message.toHtmlEscaped()));
    }
}

void MainWindow::submitDispatchPlanRevision()
{
    if (!dispatchPlanDialog || !backendClient || currentDispatchTaskId.isEmpty()) {
        return;
    }
    const bool runtimeTask = currentDispatchRuntimeMode == QStringLiteral("runtime")
        || currentDispatchExecutionSubmitted;
    if (runtimeTask) {
        QMessageBox::information(
            dispatchPlanDialog,
            QStringLiteral("计划已锁定"),
            QStringLiteral("当前任务已经进入真实执行。为保证审计记录一致，只能查看版本，不能修改当前计划。"));
        updateDispatchPlanRevisionEditor();
        return;
    }

    const QString goal = dispatchPlanGoalInput ? dispatchPlanGoalInput->toPlainText().trimmed() : QString();
    const QString changeSummary = dispatchPlanChangeSummaryInput
        ? dispatchPlanChangeSummaryInput->text().trimmed()
        : QString();
    if (goal.size() < 2 || changeSummary.size() < 2) {
        QMessageBox::information(
            dispatchPlanDialog,
            QStringLiteral("补充修改说明"),
            QStringLiteral("请填写新的任务目标和本次修改原因，再确认生成新计划。"));
        return;
    }

    const QMessageBox::StandardButton choice = QMessageBox::question(
        dispatchPlanDialog,
        QStringLiteral("确认修改计划"),
        QStringLiteral("当前版本会永久保留。总指挥会依据新目标生成下一版 dry-run 计划，步骤、权限和范围将重新校验。\n\n继续吗？"),
        QMessageBox::Yes | QMessageBox::No,
        QMessageBox::No);
    if (choice != QMessageBox::Yes) {
        return;
    }

    dispatchPlanRevisionSubmitting = true;
    updateDispatchPlanRevisionEditor();
    if (dispatchPlanStatusLabel) {
        dispatchPlanStatusLabel->setText(QStringLiteral("正在由总指挥重新生成计划..."));
    }
    backendClient->reviseTaskPlan(currentDispatchTaskId, goal, changeSummary);
}

void MainWindow::handleDispatchPlanRevisionCompleted(const WorkflowPlanRevisionResult &result)
{
    if (result.taskId != currentDispatchTaskId) {
        return;
    }

    dispatchPlanRevisionSubmitting = false;
    currentDispatchPlanSummary = result.planSummary;
    currentDispatchPlanSteps = result.steps;
    currentDispatchPlannedStepCount = result.steps.size();
    currentDispatchUserGoal = result.planSummary.userGoal.isEmpty()
        ? currentDispatchUserGoal
        : result.planSummary.userGoal;
    currentDispatchNeedsClarification = result.planSummary.nextAction
        == QStringLiteral("ask_clarifying_questions");
    currentDispatchGuidedHandoff = result.planSummary.nextAction == QStringLiteral("open_data_workspace");
    currentDispatchPresentationHandoff = result.planSummary.nextAction
        == QStringLiteral("open_presentation_studio");
    currentDispatchRuntimeMode = QStringLiteral("dry_run");
    currentDispatchRuntimeStatus = currentDispatchNeedsClarification
        ? QStringLiteral("blocked")
        : QStringLiteral("pending");
    currentDispatchExecutionInProgress = false;
    currentDispatchExecutionSubmitted = false;
    currentDispatchUpdateWatermark = 0;
    currentDispatchUpdates.clear();

    ui->dispatchChatStatus->setText(QStringLiteral("计划已更新"));
    ui->summaryVal3->setText(currentDispatchNeedsClarification
                                  ? QStringLiteral("需要补充信息")
                                  : agentSummary(result.steps));
    appendConversationHtml(
        QStringLiteral("<hr/><h3>AI调度台 · 计划已更新</h3><p>%1</p>%2")
            .arg((result.message.isEmpty() ? QStringLiteral("已生成新的计划版本。") : result.message)
                     .toHtmlEscaped(),
                 formatDispatchChatPlanCardHtml(result.planSummary, result.steps)));
    resetProgressPanel();
    setProgressStep(1, QStringLiteral("1 任务目标 · 已更新"), QStringLiteral("badgeGreen"));
    setProgressStep(2,
                    QStringLiteral("2 Commander 规划 · 已生成 v%1（%2 步）")
                        .arg(result.planSummary.planVersion)
                        .arg(currentDispatchPlannedStepCount),
                    QStringLiteral("badgeGreen"));
    setProgressStep(3,
                    currentDispatchNeedsClarification ? QStringLiteral("3 Workflow 推进 · 等待补充信息")
                                                     : QStringLiteral("3 Workflow 推进 · 等待执行确认"),
                    currentDispatchNeedsClarification ? QStringLiteral("badgeOrange") : QStringLiteral("badgeGray"));
    setProgressStep(4, QStringLiteral("4 权限 / 产物 · 尚未执行"), QStringLiteral("badgeGray"));
    setProgressStep(5,
                    currentDispatchNeedsClarification ? QStringLiteral("5 当前结论 · 需要补充信息")
                                                     : QStringLiteral("5 当前结论 · 请复核计划后执行"),
                    currentDispatchNeedsClarification ? QStringLiteral("badgeOrange") : QStringLiteral("badgeGray"));
    updateDispatchActionButtons();

    if (dispatchPlanGoalInput) {
        dispatchPlanGoalInput->setPlainText(currentDispatchUserGoal);
    }
    if (dispatchPlanChangeSummaryInput) {
        dispatchPlanChangeSummaryInput->clear();
    }
    if (dispatchPlanPreview) {
        dispatchPlanPreview->setHtml(formatDispatchWorkflowPlanHtml(result.planSummary, result.steps));
    }
    if (dispatchPlanStatusLabel) {
        dispatchPlanStatusLabel->setText(
            result.message.isEmpty() ? QStringLiteral("已生成新计划，请复核后再执行。") : result.message);
    }
    refreshDispatchPlanVersions();
    refreshCurrentDispatchUpdates();
}

void MainWindow::handleDispatchPlanRevisionFailed(const QString &message)
{
    dispatchPlanRevisionSubmitting = false;
    updateDispatchPlanRevisionEditor();
    if (dispatchPlanStatusLabel) {
        dispatchPlanStatusLabel->setText(QStringLiteral("计划未修改：%1").arg(message));
    }
    if (dispatchPlanDialog) {
        QMessageBox::warning(dispatchPlanDialog, QStringLiteral("无法修改计划"), message);
    }
}

void MainWindow::updateDispatchPlanRevisionEditor()
{
    if (!dispatchPlanDialog) {
        return;
    }
    const bool runtimeTask = currentDispatchRuntimeMode == QStringLiteral("runtime")
        || currentDispatchExecutionSubmitted;
    const bool editable = !runtimeTask && !currentDispatchExecutionInProgress && !dispatchPlanRevisionSubmitting;
    if (dispatchPlanGoalInput) {
        dispatchPlanGoalInput->setReadOnly(!editable);
    }
    if (dispatchPlanChangeSummaryInput) {
        dispatchPlanChangeSummaryInput->setReadOnly(!editable);
    }
    if (dispatchPlanRevisionButton) {
        dispatchPlanRevisionButton->setEnabled(editable);
        dispatchPlanRevisionButton->setText(
            dispatchPlanRevisionSubmitting ? QStringLiteral("正在生成计划")
                                           : (runtimeTask ? QStringLiteral("计划已锁定")
                                                          : QStringLiteral("确认并生成新计划")));
    }
    if (!editable && dispatchPlanStatusLabel && !dispatchPlanRevisionSubmitting) {
        dispatchPlanStatusLabel->setText(
            runtimeTask
                ? QStringLiteral("当前任务已经进入真实执行；为保证审计一致性，版本仅可查看。")
                : QStringLiteral("当前正在提交执行请求，暂时不能修改计划。"));
    }
}

void MainWindow::openTaskInHistory(const QString &taskId)
{
    if (taskId.isEmpty()) {
        return;
    }

    // “查看本次任务”必须优先于用户上一次的历史筛选；否则刚完成的审查报告可能因旧条件
    // 被隐藏，用户又得手工复制任务 ID 查找。筛选只为这次显式跳转复位，数据仍由后端分页读取。
    const QSignalBlocker statusBlocker(historyStatusFilter);
    const QSignalBlocker modeBlocker(historyModeFilter);
    const QSignalBlocker riskBlocker(historyRiskFilter);
    const QSignalBlocker confirmationBlocker(historyConfirmationFilter);
    const QSignalBlocker keywordBlocker(ui->historyInput);
    if (historyStatusFilter) {
        historyStatusFilter->setCurrentIndex(0);
    }
    if (historyModeFilter) {
        historyModeFilter->setCurrentIndex(0);
    }
    if (historyRiskFilter) {
        historyRiskFilter->setCurrentIndex(0);
    }
    if (historyConfirmationFilter) {
        historyConfirmationFilter->setCurrentIndex(0);
    }
    ui->historyInput->clear();

    pendingHistoryFocusTaskId = taskId;
    historyOffset = 0;
    switchPage(12);
}

bool MainWindow::isCurrentDispatchDirectKnowledgeAnswer() const
{
    if (currentDispatchNeedsClarification
        || currentDispatchPlanSummary.executionReadiness != QStringLiteral("ready")
        || !currentDispatchPlanSummary.workspaceScope.writePaths.isEmpty()
        || !currentDispatchPlanSummary.workspaceScope.externalServices.isEmpty()) {
        return false;
    }

    int specialistCount = 0;
    bool admittedKnowledgeAnswer = false;
    for (const WorkflowStepInfo &step : currentDispatchPlanSteps) {
        if (step.requiresConfirmation) {
            return false;
        }
        if (step.agent == QStringLiteral("commander_agent")) {
            continue;
        }
        ++specialistCount;
        admittedKnowledgeAnswer = step.agent == QStringLiteral("knowledge_agent")
            && step.action == QStringLiteral("answer_question")
            && step.executionMode == QStringLiteral("execute")
            && !step.input.value(QStringLiteral("knowledge_base_id")).toString().trimmed().isEmpty();
    }
    return specialistCount == 1 && admittedKnowledgeAnswer;
}

bool MainWindow::isCurrentDispatchDirectDataAnalysis() const
{
    if (currentDispatchNeedsClarification
        || currentDispatchPlanSummary.executionReadiness != QStringLiteral("ready")
        || !currentDispatchPlanSummary.workspaceScope.writePaths.isEmpty()
        || !currentDispatchPlanSummary.workspaceScope.externalServices.isEmpty()) {
        return false;
    }

    int specialistCount = 0;
    bool admittedDataAnalysis = false;
    for (const WorkflowStepInfo &step : currentDispatchPlanSteps) {
        if (step.requiresConfirmation) {
            return false;
        }
        if (step.agent == QStringLiteral("commander_agent")) {
            continue;
        }
        ++specialistCount;
        admittedDataAnalysis = step.agent == QStringLiteral("data_agent")
            && step.action == QStringLiteral("analyze_dataset")
            && step.executionMode == QStringLiteral("execute")
            && !step.input.value(QStringLiteral("dataset_name")).toString().trimmed().isEmpty();
    }
    return specialistCount == 1 && admittedDataAnalysis;
}

bool MainWindow::isCurrentDispatchAutoReadOnlyTask() const
{
    return currentDispatchDirectKnowledgeAnswer || currentDispatchDirectDataAnalysis;
}

bool MainWindow::isCurrentDispatchDataChartDelivery() const
{
    // 只识别既有的单数据图表写入闭环。它需要客户确认，不能因为含有“图表”一词就把任意
    // 数据分析任务当成已授权写盘；实际动作和文件边界仍由后端 Runtime 二次校验。
    bool hasAnalysis = false;
    bool hasChartExport = false;
    for (const WorkflowStepInfo &step : currentDispatchPlanSteps) {
        hasAnalysis = hasAnalysis || (step.agent == QStringLiteral("data_agent")
                                      && step.action == QStringLiteral("analyze_dataset"));
        hasChartExport = hasChartExport || (step.agent == QStringLiteral("data_agent")
                                            && step.action == QStringLiteral("export_chart_dashboard"));
    }
    return hasAnalysis && hasChartExport;
}

bool MainWindow::isCurrentDispatchDataWorkbookDelivery() const
{
    // 工作簿交付与 PNG 图表同样必须先有已绑定的单数据分析步骤。这里只识别计划形状，
    // 数据版本、文件写入范围和 Excel 回读仍由后端 Runtime 重新验证。
    bool hasAnalysis = false;
    bool hasWorkbookExport = false;
    for (const WorkflowStepInfo &step : currentDispatchPlanSteps) {
        hasAnalysis = hasAnalysis || (step.agent == QStringLiteral("data_agent")
                                      && step.action == QStringLiteral("analyze_dataset"));
        hasWorkbookExport = hasWorkbookExport || (step.agent == QStringLiteral("data_agent")
                                                   && step.action == QStringLiteral("export_analysis_workbook"));
    }
    return hasAnalysis && hasWorkbookExport;
}

bool MainWindow::isCurrentDispatchDirectConversation() const
{
    // 只有没有专业步骤、没有材料副作用也没有澄清问题的普通问答才能直接收束。计划仍会
    // 保存在审计面，Qt 只是不再把一个已经回答的问题强行变成 Runtime 操作。
    return !currentDispatchNeedsClarification
        && !currentDispatchGuidedHandoff
        && !isCurrentDispatchAutoReadOnlyTask()
        && currentDispatchPlanSummary.intent == QStringLiteral("direct_answer");
}

QString MainWindow::currentDispatchAutoReadOnlyActivityText() const
{
    if (currentDispatchDataChartDelivery) {
        return QStringLiteral("正在生成图表");
    }
    if (currentDispatchDataWorkbookDelivery) {
        return QStringLiteral("正在生成分析 Excel");
    }
    return currentDispatchDirectDataAnalysis ? QStringLiteral("正在分析")
                                             : QStringLiteral("正在检索");
}

QString MainWindow::currentDispatchKnowledgeBaseName() const
{
    QString knowledgeBaseId;
    for (const WorkflowStepInfo &step : currentDispatchPlanSteps) {
        if (step.agent == QStringLiteral("knowledge_agent")
            && step.action == QStringLiteral("answer_question")) {
            knowledgeBaseId = step.input.value(QStringLiteral("knowledge_base_id")).toString();
            break;
        }
    }
    for (const KnowledgeBaseInfo &base : currentKnowledgeBases) {
        if (base.knowledgeBaseId == knowledgeBaseId && !base.name.trimmed().isEmpty()) {
            return base.name;
        }
    }
    return QStringLiteral("已选资料库");
}

QString MainWindow::currentDispatchKnowledgeAnswerTaskId() const
{
    // 父 Runtime 只镜像子任务身份与短摘要；完整正文仍留在 K3 子任务。这里从稳定的
    // state snapshot 找 ID，避免 UI 依据日志文本或 artifact 名称猜测关联关系。
    for (auto iterator = currentDispatchUpdates.crbegin(); iterator != currentDispatchUpdates.crend(); ++iterator) {
        const QJsonObject retrospective = iterator->payload.value(QStringLiteral("task_retrospective")).toObject();
        const QJsonArray delegations = retrospective.value(QStringLiteral("delegations")).toArray();
        for (const QJsonValue &value : delegations) {
            const QJsonObject delegation = value.toObject();
            const QString taskId = delegation.value(QStringLiteral("task_id")).toString();
            if (delegation.value(QStringLiteral("agent_id")).toString() == QStringLiteral("knowledge_agent")
                && taskId.startsWith(QStringLiteral("task_kb_"))) {
                return taskId;
            }
        }
    }
    return {};
}

void MainWindow::requestCurrentDispatchKnowledgeAnswerResult()
{
    if (!currentDispatchDirectKnowledgeAnswer || currentDispatchKnowledgeAnswerDelivered
        || currentDispatchKnowledgeAnswerResultRequested || !backendClient) {
        return;
    }
    const QString delegatedTaskId = currentDispatchKnowledgeAnswerTaskId();
    if (delegatedTaskId.isEmpty()) {
        return;
    }

    currentDispatchKnowledgeAnswerChildTaskId = delegatedTaskId;
    currentDispatchKnowledgeAnswerResultRequested = true;
    ui->dispatchChatStatus->setText(QStringLiteral("正在整理答案"));
    ui->summaryVal3->setText(QStringLiteral("正在整理已核验结论"));
    setDispatchActivityRunning(true);
    backendClient->requestKnowledgeAnswerResult(delegatedTaskId);
}

void MainWindow::handleDispatchKnowledgeAnswerCompleted(const KnowledgeAnswerTaskResult &result)
{
    if (!currentDispatchDirectKnowledgeAnswer || result.taskId.isEmpty()
        || result.taskId != currentDispatchKnowledgeAnswerChildTaskId) {
        return;
    }

    currentDispatchKnowledgeAnswerResultRequested = false;
    currentDispatchKnowledgeAnswerDelivered = true;
    currentDispatchKnowledgeAnswerFailed = false;
    currentDispatchRuntimeStatus = result.status;
    const bool completed = result.status == QStringLiteral("completed")
        && result.result.value(QStringLiteral("answer")).isObject();
    ui->dispatchChatStatus->setText(completed ? QStringLiteral("回答完成") : QStringLiteral("需要补充"));
    ui->summaryVal3->setText(completed ? QStringLiteral("已生成带来源回答")
                                       : QStringLiteral("未获得足够依据"));
    setProgressStep(5,
                    completed ? QStringLiteral("5 当前结论 · 已生成带来源回答")
                              : QStringLiteral("5 当前结论 · 当前资料不足以回答"),
                    completed ? QStringLiteral("badgeGreen") : QStringLiteral("badgeOrange"));
    setDispatchActivityRunning(false);
    appendConversationHtml(formatDispatchKnowledgeAnswerHtml(result));
    updateDispatchActionButtons();
}

void MainWindow::handleDispatchKnowledgeAnswerFailed(const QString &message)
{
    if (!currentDispatchDirectKnowledgeAnswer || currentDispatchKnowledgeAnswerChildTaskId.isEmpty()
        || currentDispatchKnowledgeAnswerDelivered) {
        return;
    }

    currentDispatchKnowledgeAnswerResultRequested = false;
    currentDispatchKnowledgeAnswerFailed = true;
    currentDispatchRuntimeStatus = QStringLiteral("failed");
    ui->dispatchChatStatus->setText(QStringLiteral("回答未完成"));
    ui->summaryVal3->setText(QStringLiteral("请查看资料范围或模型状态"));
    setProgressStep(5, QStringLiteral("5 当前结论 · 未取得可验证回答"), QStringLiteral("badgeOrange"));
    setDispatchActivityRunning(false);
    appendConversationHtml(
        QStringLiteral("<hr/><h3>AI 调度台</h3>"
                       "<p style=\"color:#B45309;\"><b>这次没有拿到可验证的回答。</b></p>"
                       "<p>请确认资料库已完成索引、问题足够具体，并在“查看来源”中检查本次执行详情。</p>"
                       "<p style=\"color:#64748B;\">%1</p>")
            .arg(message.left(240).toHtmlEscaped()));
    updateDispatchActionButtons();
}

QString MainWindow::formatDispatchKnowledgeAnswerHtml(const KnowledgeAnswerTaskResult &result) const
{
    const QJsonObject answerResult = result.result;
    const QJsonObject answer = answerResult.value(QStringLiteral("answer")).toObject();
    const QJsonObject evidenceGate = answerResult.value(QStringLiteral("evidence_gate")).toObject();
    const QString message = answerResult.value(QStringLiteral("message")).toString(result.message);
    const QString baseName = currentDispatchKnowledgeBaseName().toHtmlEscaped();
    QString html = QStringLiteral("<hr/><h3>AI 调度台</h3>");
    if (result.status == QStringLiteral("completed") && !answer.isEmpty()) {
        html += QStringLiteral(
                    "<div style=\"margin:6px 0 10px 0;\">"
                    "<h4 style=\"margin:0 0 8px 0;color:#0F172A;\">基于“%1”的回答</h4>%2%3"
                    "<p style=\"margin-top:12px;color:#64748B;\">回答仅依据本次已核验的资料库来源。更多执行细节可从右侧“查看来源”打开。</p>"
                    "</div>")
                    .arg(baseName,
                         formatDispatchAnswerMarkdownHtml(answer.value(QStringLiteral("answer_markdown")).toString()),
                         formatDispatchKnowledgeSourcesHtml(
                             evidenceGate.value(QStringLiteral("sources")).toArray()));
        const QJsonArray warnings = answer.value(QStringLiteral("warnings")).toArray();
        if (!warnings.isEmpty()) {
            QStringList notes;
            for (const QJsonValue &warning : warnings) {
                if (warning.isString() && !warning.toString().trimmed().isEmpty()) {
                    notes.append(warning.toString().toHtmlEscaped());
                }
            }
            if (!notes.isEmpty()) {
                html += QStringLiteral("<p style=\"color:#A15C07;\"><b>说明：</b>%1</p>")
                            .arg(notes.join(QStringLiteral("；")));
            }
        }
        return html;
    }

    return html + QStringLiteral(
                      "<p style=\"color:#B45309;\"><b>当前资料不足以给出可靠回答。</b></p>"
                      "<p>%1</p><p style=\"color:#64748B;\">可以补充相关材料、完成索引，或把问题缩小到一个具体主题后重试。</p>")
                      .arg(message.toHtmlEscaped());
}

QString MainWindow::formatDispatchAnswerMarkdownHtml(const QString &markdown) const
{
    QString html;
    bool unorderedListOpen = false;
    bool orderedListOpen = false;
    const QRegularExpression orderedPattern(QStringLiteral("^\\d+\\.\\s+(.+)$"));
    const auto closeLists = [&html, &unorderedListOpen, &orderedListOpen]() {
        if (unorderedListOpen) {
            html += QStringLiteral("</ul>");
            unorderedListOpen = false;
        }
        if (orderedListOpen) {
            html += QStringLiteral("</ol>");
            orderedListOpen = false;
        }
    };
    const auto inlineHtml = [](const QString &text) {
        QString escaped = text.toHtmlEscaped();
        // 这里始终先转义模型文本，再补有限的 Markdown 样式。这样即使模型返回 HTML，
        // 也只能作为普通文本显示，不能改变客户端富文本内容或注入链接脚本。
        escaped.replace(QRegularExpression(QStringLiteral("\\*\\*([^*\\n]+)\\*\\*")),
                        QStringLiteral("<b>\\1</b>"));
        escaped.replace(QRegularExpression(QStringLiteral("`([^`\\n]+)`")),
                        QStringLiteral("<code style=\"background:#EFF6FF;color:#1D4ED8;\">\\1</code>"));
        return escaped;
    };
    const auto splitTableCells = [](const QString &rawLine) {
        QString line = rawLine.trimmed();
        if (line.startsWith(QLatin1Char('|'))) {
            line.remove(0, 1);
        }
        if (line.endsWith(QLatin1Char('|'))) {
            line.chop(1);
        }

        QStringList cells;
        QString cell;
        bool escapedPipe = false;
        for (const QChar character : line) {
            if (escapedPipe) {
                cell += character;
                escapedPipe = false;
            } else if (character == QLatin1Char('\\')) {
                escapedPipe = true;
            } else if (character == QLatin1Char('|')) {
                cells.append(cell.trimmed());
                cell.clear();
            } else {
                cell += character;
            }
        }
        if (escapedPipe) {
            cell += QLatin1Char('\\');
        }
        cells.append(cell.trimmed());
        return cells;
    };
    const auto isTableDivider = [&splitTableCells](const QString &line) {
        if (!line.contains(QLatin1Char('|'))) {
            return false;
        }
        const QStringList cells = splitTableCells(line);
        if (cells.size() < 2 || cells.size() > 12) {
            return false;
        }
        for (QString cell : cells) {
            cell = cell.trimmed();
            if (cell.startsWith(QLatin1Char(':'))) {
                cell.remove(0, 1);
            }
            if (cell.endsWith(QLatin1Char(':'))) {
                cell.chop(1);
            }
            if (cell.size() < 3 || cell.contains(QRegularExpression(QStringLiteral("[^-]")))) {
                return false;
            }
        }
        return true;
    };

    const QStringList lines = markdown.split('\n');
    for (int lineIndex = 0; lineIndex < lines.size(); ++lineIndex) {
        const QString &rawLine = lines.at(lineIndex);
        const QString line = rawLine.trimmed();
        if (line.isEmpty()) {
            closeLists();
            continue;
        }
        // GitHub 风格表格必须同时满足“表头 + 对齐分隔行”。仅出现竖线的自然语言
        // 不会被误判成表格；不规则行会结束当前表格并回退为普通段落。
        if (lineIndex + 1 < lines.size() && line.contains(QLatin1Char('|'))
            && isTableDivider(lines.at(lineIndex + 1))) {
            const QStringList headerCells = splitTableCells(line);
            const QStringList dividerCells = splitTableCells(lines.at(lineIndex + 1));
            if (headerCells.size() == dividerCells.size() && headerCells.size() <= 12) {
                closeLists();
                html += QStringLiteral(
                    "<table border=\"1\" cellspacing=\"0\" cellpadding=\"7\" "
                    "style=\"border-color:#CBD5E1;margin:10px 0;width:100%;\">"
                    "<thead style=\"background:#EFF6FF;color:#1E3A5F;\"><tr>");
                for (const QString &cell : headerCells) {
                    html += QStringLiteral("<th align=\"left\">%1</th>").arg(inlineHtml(cell));
                }
                html += QStringLiteral("</tr></thead><tbody>");

                int dataLineIndex = lineIndex + 2;
                int dataRowCount = 0;
                while (dataLineIndex < lines.size() && dataRowCount < 60) {
                    const QString dataLine = lines.at(dataLineIndex).trimmed();
                    if (dataLine.isEmpty() || !dataLine.contains(QLatin1Char('|'))) {
                        break;
                    }
                    QStringList cells = splitTableCells(dataLine);
                    if (cells.size() > headerCells.size()) {
                        break;
                    }
                    // Markdown 允许省略末尾空单元格；客户端补齐后保持每行列数一致，
                    // 避免 QTextEdit 因不规则表格把后续答案挤乱。
                    while (cells.size() < headerCells.size()) {
                        cells.append(QString());
                    }
                    html += QStringLiteral("<tr>");
                    for (const QString &cell : cells) {
                        html += QStringLiteral("<td valign=\"top\">%1</td>").arg(inlineHtml(cell));
                    }
                    html += QStringLiteral("</tr>");
                    ++dataRowCount;
                    ++dataLineIndex;
                }
                html += QStringLiteral("</tbody></table>");
                lineIndex = dataLineIndex - 1;
                continue;
            }
        }
        if (line.startsWith(QStringLiteral("### ")) || line.startsWith(QStringLiteral("## "))) {
            closeLists();
            const QString heading = line.mid(line.startsWith(QStringLiteral("### ")) ? 4 : 3);
            html += QStringLiteral("<h4 style=\"margin:14px 0 6px 0;color:#1E3A5F;\">%1</h4>")
                        .arg(inlineHtml(heading));
            continue;
        }
        if (line.startsWith(QStringLiteral("# "))) {
            closeLists();
            html += QStringLiteral("<h4 style=\"margin:14px 0 6px 0;color:#0F172A;\">%1</h4>")
                        .arg(inlineHtml(line.mid(2)));
            continue;
        }
        if (line.startsWith(QStringLiteral("- ")) || line.startsWith(QStringLiteral("* "))) {
            if (orderedListOpen) {
                html += QStringLiteral("</ol>");
                orderedListOpen = false;
            }
            if (!unorderedListOpen) {
                html += QStringLiteral("<ul style=\"margin:4px 0 10px 20px;\">");
                unorderedListOpen = true;
            }
            html += QStringLiteral("<li>%1</li>").arg(inlineHtml(line.mid(2)));
            continue;
        }
        const QRegularExpressionMatch orderedMatch = orderedPattern.match(line);
        if (orderedMatch.hasMatch()) {
            if (unorderedListOpen) {
                html += QStringLiteral("</ul>");
                unorderedListOpen = false;
            }
            if (!orderedListOpen) {
                html += QStringLiteral("<ol style=\"margin:4px 0 10px 20px;\">");
                orderedListOpen = true;
            }
            html += QStringLiteral("<li>%1</li>").arg(inlineHtml(orderedMatch.captured(1)));
            continue;
        }
        closeLists();
        if (line.startsWith(QStringLiteral("> "))) {
            html += QStringLiteral(
                        "<blockquote style=\"margin:8px 0;padding:8px 12px;border-left:3px solid #60A5FA;"
                        "background:#F8FAFC;color:#475569;\">%1</blockquote>")
                        .arg(inlineHtml(line.mid(2)));
            continue;
        }
        html += QStringLiteral("<p style=\"margin:6px 0;line-height:1.65;\">%1</p>").arg(inlineHtml(line));
    }
    closeLists();
    return html.isEmpty() ? QStringLiteral("<p>本次回答没有可展示的正文。</p>") : html;
}

QString MainWindow::formatDispatchAssistantMessageHtml(const QString &markdown) const
{
    // 模型正文必须先走受限 Markdown 渲染器：它解析表格、列表和强调，但不会信任模型返回的
    // 任意 HTML。这样普通对话能像聊天产品一样直接阅读，也不会把规划日志塞回主消息流。
    return QStringLiteral(
               "<hr/><h3>AI 调度台</h3>"
               "<div style=\"margin:6px 0 12px 0;padding:2px 4px;\">%1</div>")
        .arg(formatDispatchAnswerMarkdownHtml(markdown));
}

QString MainWindow::formatDispatchUserMessageHtml(const QString &message) const
{
    // QTextDocument 没有 Web 气泡组件。使用受控的两列表格保持右对齐和稳定最大宽度，
    // 避免长问题把整行文本推到窗口边缘；正文始终转义，不能作为富文本执行。
    const QString escaped = message.toHtmlEscaped().replace(QStringLiteral("\n"), QStringLiteral("<br/>"));
    return QStringLiteral(
               "<table width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"margin:10px 0;\">"
               "<tr><td width=\"30%\"></td><td width=\"70%\" bgcolor=\"#2563EB\">"
               "<p align=\"right\" style=\"margin:8px 12px 2px 12px;color:#DBEAFE;\"><b>我</b></p>"
               "<p style=\"margin:2px 12px 10px 12px;color:white;line-height:1.6;\">%1</p>"
               "</td></tr></table>")
        .arg(escaped);
}

QString MainWindow::formatDispatchKnowledgeSourcesHtml(const QJsonArray &sources) const
{
    if (sources.isEmpty()) {
        return {};
    }
    QString html = QStringLiteral("<h4 style=\"margin:16px 0 6px 0;color:#1E3A5F;\">参考来源</h4><ol style=\"margin:4px 0 8px 20px;\">");
    int displayed = 0;
    for (const QJsonValue &value : sources) {
        const QJsonObject source = value.toObject();
        const QString name = source.value(QStringLiteral("document_name")).toString();
        const QJsonObject anchor = source.value(QStringLiteral("source")).toObject();
        const QString locatorValue = anchor.value(QStringLiteral("source_locator")).toString();
        const QString sourceKind = anchor.value(QStringLiteral("source_kind")).toString();
        const QString locator = sourceKind == QStringLiteral("page")
            ? QStringLiteral("第 %1 页").arg(locatorValue)
            : (sourceKind == QStringLiteral("line")
                   ? QStringLiteral("第 %1 行").arg(locatorValue)
                   : (sourceKind == QStringLiteral("paragraph")
                          ? QStringLiteral("第 %1 段").arg(locatorValue)
                          : locatorValue));
        if (name.isEmpty()) {
            continue;
        }
        html += QStringLiteral("<li><b>%1</b>%2</li>")
                    .arg(name.toHtmlEscaped(),
                         locator.isEmpty() ? QString() : QStringLiteral(" · %1").arg(locator.toHtmlEscaped()));
        if (++displayed >= 4) {
            break;
        }
    }
    return displayed == 0 ? QString() : html + QStringLiteral("</ol>");
}

void MainWindow::setDispatchActivityRunning(bool running)
{
    if (dispatchActivityIndicator) {
        dispatchActivityIndicator->setRunning(running);
    }
}

void MainWindow::executeCurrentDispatchTaskFromDispatch()
{
    if (currentDispatchTaskId.isEmpty() || !backendClient) {
        ui->dispatchChatStatus->setText(QStringLiteral("暂无任务"));
        updateDispatchActionButtons();
        return;
    }
    if (currentDispatchNeedsClarification) {
        QMessageBox::information(this,
                                 QStringLiteral("开始执行"),
                                 QStringLiteral("当前计划还需要补充信息，先完善任务需求后再执行。"));
        updateDispatchActionButtons();
        return;
    }
    if (currentDispatchPlanSummary.executionReadiness
        == QStringLiteral("requires_composition_runtime")) {
        QMessageBox::information(
            this,
            QStringLiteral("组合计划待执行能力"),
            QStringLiteral("当前已完成材料范围、依赖图与并行建议的审阅。真实子任务调度、结果汇总和共享预算将由 C6.4 组合 Runtime 承担，现阶段不会把它伪装成已可执行。"));
        updateDispatchActionButtons();
        return;
    }
    if (currentDispatchGuidedHandoff) {
        // 这不是 Runtime 执行：只携带用户刚才的目标进入已有的数据工作台，客户仍需在那里
        // 明确选择 CSV/XLSX 并启动画像。这样 C1 不会抢跑 D5.4 的正式数据 Agent 委派。
        if (ui->dataAnalysisGoalInput && ui->dataAnalysisGoalInput->text().trimmed().isEmpty()) {
            ui->dataAnalysisGoalInput->setText(currentDispatchUserGoal);
        }
        ui->dispatchChatStatus->setText(QStringLiteral("已转入数据工作台"));
        appendConversationHtml(
            QStringLiteral("<p style=\"color:#0F766E;\"><b>系统</b> · 已带入分析目标，请在数据工作台选择数据文件后继续。</p>"));
        switchPage(6);
        return;
    }

    QStringList stepTitles;
    for (const WorkflowStepInfo &step : currentDispatchPlanSteps) {
        stepTitles.append(step.title.isEmpty() ? step.action : step.title);
    }
    const WorkflowWorkspaceScopeInfo &scope = currentDispatchPlanSummary.workspaceScope;
    const QString readScope = scope.readPaths.isEmpty()
        ? QStringLiteral("未声明读取材料")
        : scope.readPaths.join(QStringLiteral("、"));
    const QString writeScope = scope.writePaths.isEmpty()
        ? QStringLiteral("本计划不写入文件")
        : scope.writePaths.join(QStringLiteral("、"));
    const QString externalScope = scope.externalServices.isEmpty()
        ? QStringLiteral("不访问外部服务")
        : scope.externalServices.join(QStringLiteral("、"));
    const QString details = QStringLiteral(
        "任务 ID：%1\n\n"
        "计划：%2\n"
        "步骤：%3\n"
        "读取范围：%4\n"
        "写入范围：%5\n"
        "外部服务：%6\n\n"
        "确认后会把当前 dry-run 计划转入真实 Runtime。当前 Runtime 只运行后端登记的安全内置工具；"
        "需要文件写入等敏感权限时，任务会停在等待确认状态。真实产物只会写入受控 outputs 目录。")
        .arg(currentDispatchTaskId,
             currentDispatchPlanSummary.summary.isEmpty()
                 ? QStringLiteral("未提供额外摘要")
                 : currentDispatchPlanSummary.summary,
             stepTitles.isEmpty() ? QStringLiteral("未生成可执行步骤") : stepTitles.join(QStringLiteral(" → ")),
             readScope,
             writeScope,
             externalScope);
    const QMessageBox::StandardButton choice = QMessageBox::question(
        this,
        QStringLiteral("开始真实执行"),
        details,
        QMessageBox::Yes | QMessageBox::No,
        QMessageBox::No);
    if (choice != QMessageBox::Yes) {
        return;
    }

    beginCurrentDispatchRuntime(false);
}

void MainWindow::beginCurrentDispatchRuntime(bool automaticallyApproved)
{
    if (currentDispatchTaskId.isEmpty() || !backendClient || currentDispatchExecutionInProgress) {
        return;
    }

    // 仅由已校验的单材料只读任务使用自动路径。其它动作仍先展示范围并由客户确认，
    // 因而不会把“免确认”扩展为文件写入、联网、命令或深度分析的通行证。
    currentDispatchExecutionInProgress = true;
    currentDispatchExecutionSubmitted = false;
    updateDispatchActionButtons();
    ui->dispatchChatStatus->setText(
        automaticallyApproved ? currentDispatchAutoReadOnlyActivityText() : QStringLiteral("请求执行"));
    setDispatchActivityRunning(true);
    setProgressStep(5,
                    automaticallyApproved
                        ? QStringLiteral("5 当前结论 · %1").arg(currentDispatchAutoReadOnlyActivityText())
                        : QStringLiteral("5 当前结论 · 正在提交执行请求"),
                    QStringLiteral("badgeBlue"));
    if (!automaticallyApproved) {
        appendConversationHtml(
            QStringLiteral("<p style=\"color:#2563EB;\"><b>AI调度台</b> · 已收到执行确认，正在进入真实 Runtime。</p>"));
    }
    backendClient->requestTaskExecute(currentDispatchTaskId);
}

void MainWindow::setupCodeWorkshop()
{
    // 代码工坊当前只接入“命令执行前的安全检查”。
    // 这不是正式 Code Agent 执行器，正式功能仍需先和用户确认 Agent 方案。
    codeCommandPolicyBadge = ui->codeCommandPolicyBadge;
    codeCommandPolicyInput = ui->codeCommandPolicyInput;
    codeCommandPolicyCheckButton = ui->codeCommandPolicyCheckButton;
    codeCommandPolicyResultText = ui->codeCommandPolicyResultText;

    polishBadge(codeCommandPolicyBadge, QStringLiteral("badgeGray"));
    if (codeCommandPolicyResultText) {
        codeCommandPolicyResultText->setHtml(
            QStringLiteral("<p style=\"color:#64748B;\">输入一条命令后查看风险级别、当前权限模式下的处理预期、默认超时、输出截断建议和命中原因。</p>"));
    }

    if (codeCommandPolicyInput) {
        connect(codeCommandPolicyInput, &QLineEdit::returnPressed, this, &MainWindow::checkCodeWorkshopCommandPolicy);
    }
    if (codeCommandPolicyCheckButton) {
        connect(codeCommandPolicyCheckButton, &QPushButton::clicked, this, &MainWindow::checkCodeWorkshopCommandPolicy);
    }
}

void MainWindow::checkCodeWorkshopCommandPolicy()
{
    if (!backendClient || !codeCommandPolicyInput || !codeCommandPolicyResultText) {
        return;
    }

    const QString command = codeCommandPolicyInput->text().trimmed();
    if (command.isEmpty()) {
        polishBadge(codeCommandPolicyBadge, QStringLiteral("badgeGray"));
        if (codeCommandPolicyBadge) {
            codeCommandPolicyBadge->setText(QStringLiteral("未检查"));
        }
        codeCommandPolicyResultText->setHtml(
            QStringLiteral("<p style=\"color:#EA580C;\">请输入一条命令。这里只做静态风险检查，不会执行命令。</p>"));
        return;
    }

    codeCommandPolicyCheckInProgress = true;
    if (codeCommandPolicyCheckButton) {
        codeCommandPolicyCheckButton->setEnabled(false);
        codeCommandPolicyCheckButton->setText(QStringLiteral("检查中"));
    }
    polishBadge(codeCommandPolicyBadge, QStringLiteral("badgeBlue"));
    if (codeCommandPolicyBadge) {
        codeCommandPolicyBadge->setText(QStringLiteral("检查中"));
    }
    codeCommandPolicyResultText->setHtml(
        QStringLiteral("<p style=\"color:#2563EB;\">正在请求后端做命令静态风险分类……</p>"));

    backendClient->checkWorkflowCommandPolicy(command);
}

void MainWindow::handleWorkflowCommandPolicyChecked(const WorkflowCommandPolicyCheckResult &result)
{
    codeCommandPolicyCheckInProgress = false;
    if (codeCommandPolicyCheckButton) {
        codeCommandPolicyCheckButton->setEnabled(true);
        codeCommandPolicyCheckButton->setText(QStringLiteral("检查"));
    }

    const QString badgeName = commandPolicyBadgeObjectName(result.riskLevel, result.allowed);
    polishBadge(codeCommandPolicyBadge, badgeName);
    if (codeCommandPolicyBadge) {
        QString badgeText = commandPolicyRiskText(result.riskLevel);
        if (!result.allowed) {
            badgeText += QStringLiteral(" · 默认拒绝");
        } else if (result.effectiveAction == QStringLiteral("block")) {
            badgeText += QStringLiteral(" · 策略阻止");
        } else if (result.effectiveAction == QStringLiteral("confirm")) {
            badgeText += QStringLiteral(" · 策略确认");
        }
        codeCommandPolicyBadge->setText(badgeText);
    }
    if (codeCommandPolicyResultText) {
        codeCommandPolicyResultText->setHtml(formatCommandPolicyResultHtml(result));
    }
}

void MainWindow::handleWorkflowCommandPolicyCheckFailed(const QString &message)
{
    codeCommandPolicyCheckInProgress = false;
    if (codeCommandPolicyCheckButton) {
        codeCommandPolicyCheckButton->setEnabled(true);
        codeCommandPolicyCheckButton->setText(QStringLiteral("检查"));
    }
    polishBadge(codeCommandPolicyBadge, QStringLiteral("badgeOrange"));
    if (codeCommandPolicyBadge) {
        codeCommandPolicyBadge->setText(QStringLiteral("检查失败"));
    }
    if (codeCommandPolicyResultText) {
        codeCommandPolicyResultText->setHtml(
            QStringLiteral("<p style=\"color:#DC2626;\"><b>命令安全检查失败：</b>%1</p>")
                .arg(message.toHtmlEscaped()));
    }
}

QString MainWindow::commandPolicyRiskText(const QString &riskLevel) const
{
    if (riskLevel == QStringLiteral("read_only")) {
        return QStringLiteral("只读定位");
    }
    if (riskLevel == QStringLiteral("diagnostic")) {
        return QStringLiteral("诊断验证");
    }
    if (riskLevel == QStringLiteral("modifying")) {
        return QStringLiteral("修改型");
    }
    if (riskLevel == QStringLiteral("network")) {
        return QStringLiteral("联网");
    }
    if (riskLevel == QStringLiteral("high_risk")) {
        return QStringLiteral("高危");
    }
    if (riskLevel == QStringLiteral("blocked")) {
        return QStringLiteral("平台阻止");
    }
    return riskLevel.isEmpty() || riskLevel == QStringLiteral("none")
        ? QStringLiteral("无明显风险")
        : riskLevel;
}

QString MainWindow::commandExecutionRouteText(const QString &route) const
{
    if (route == QStringLiteral("blocked_by_command_governance")) {
        return QStringLiteral("平台命令治理阻止");
    }
    if (route == QStringLiteral("prefer_agentic_search_or_read_tool")) {
        return QStringLiteral("优先专用搜索/读取工具");
    }
    if (route == QStringLiteral("diagnostic_runner_after_policy_check")) {
        return QStringLiteral("诊断工具封装");
    }
    if (route == QStringLiteral("network_tool_or_shell_after_permission")) {
        return QStringLiteral("联网工具或受限 Shell");
    }
    if (route == QStringLiteral("workspace_action_after_permission")) {
        return QStringLiteral("受控工作区动作");
    }
    if (route == QStringLiteral("no_execution_needed")) {
        return QStringLiteral("无需执行");
    }
    return route.isEmpty() ? QStringLiteral("未定义") : route;
}

QString MainWindow::commandPolicyBadgeObjectName(const QString &riskLevel, bool allowed) const
{
    if (!allowed || riskLevel == QStringLiteral("high_risk") || riskLevel == QStringLiteral("network")) {
        return QStringLiteral("badgeOrange");
    }
    if (riskLevel == QStringLiteral("modifying")) {
        return QStringLiteral("badgePurple");
    }
    if (riskLevel == QStringLiteral("diagnostic")) {
        return QStringLiteral("badgeBlue");
    }
    if (riskLevel == QStringLiteral("read_only")) {
        return QStringLiteral("badgeGreen");
    }
    return QStringLiteral("badgeGray");
}

QString MainWindow::formatCommandPolicyResultHtml(const WorkflowCommandPolicyCheckResult &result) const
{
    QStringList factLines;
    factLines.append(QStringLiteral("<b>风险级别：</b>%1").arg(commandPolicyRiskText(result.riskLevel).toHtmlEscaped()));
    factLines.append(QStringLiteral("<b>执行建议：</b>%1")
                         .arg(result.allowed ? QStringLiteral("可进入后续权限策略判断")
                                             : QStringLiteral("默认拒绝，不应自动执行")));
    factLines.append(QStringLiteral("<b>需要确认：</b>%1").arg(result.requiresConfirmation ? QStringLiteral("是") : QStringLiteral("否")));
    factLines.append(QStringLiteral("<b>需要审计：</b>%1").arg(result.auditRequired ? QStringLiteral("是") : QStringLiteral("否")));
    factLines.append(QStringLiteral("<b>当前权限模式：</b>%1")
                         .arg(runtimePermissionPolicyText(result.effectivePermissionPolicy).toHtmlEscaped()));
    factLines.append(QStringLiteral("<b>策略预期：</b>%1")
                         .arg(permissionPolicyActionText(result.effectiveAction).toHtmlEscaped()));
    factLines.append(QStringLiteral("<b>运行请求：</b>%1")
                         .arg(commandRuntimeRequestStatusText(result.runtimeRequestStatus).toHtmlEscaped()));
    factLines.append(QStringLiteral("<b>需用户批准：</b>%1")
                         .arg(result.permissionRequired ? QStringLiteral("是") : QStringLiteral("否")));
    factLines.append(QStringLiteral("<b>执行范围：</b>%1").arg(commandPolicyRiskText(result.executionScope).toHtmlEscaped()));
    factLines.append(QStringLiteral("<b>执行路线：</b>%1")
                         .arg(commandExecutionRouteText(result.executionRoute).toHtmlEscaped()));
    factLines.append(QStringLiteral("<b>可并发：</b>%1").arg(result.concurrencySafe ? QStringLiteral("是") : QStringLiteral("否")));
    if (result.defaultTimeoutMs > 0) {
        factLines.append(QStringLiteral("<b>默认超时：</b>%1 ms").arg(result.defaultTimeoutMs));
    }
    if (result.maxOutputChars > 0) {
        factLines.append(QStringLiteral("<b>输出截断：</b>%1 字符").arg(result.maxOutputChars));
    }

    QString html;
    html += QStringLiteral("<h3 style=\"margin:0 0 8px 0;color:#0F172A;\">检查结果</h3>");
    html += QStringLiteral("<p style=\"margin:0 0 8px 0;\"><b>原始命令：</b><code>%1</code></p>")
                .arg(result.command.toHtmlEscaped());
    if (!result.normalizedCommand.isEmpty() && result.normalizedCommand != result.command) {
        html += QStringLiteral("<p style=\"margin:0 0 8px 0;\"><b>规范化：</b><code>%1</code></p>")
                    .arg(result.normalizedCommand.toHtmlEscaped());
    }

    html += QStringLiteral("<div style=\"margin:8px 0 10px 0;padding:10px 12px;border:1px solid #DDEBFA;border-radius:10px;background:#FFFFFF;\">");
    html += QStringLiteral("<ul style=\"margin:4px 0 0 18px;padding:0;\">");
    for (const QString &line : factLines) {
        html += QStringLiteral("<li>%1</li>").arg(line);
    }
    html += QStringLiteral("</ul>");
    html += QStringLiteral("</div>");

    if (!result.effectiveReason.isEmpty()) {
        html += QStringLiteral(
                    "<div style=\"margin:0 0 10px 0;padding:9px 11px;border-left:3px solid #22C55E;"
                    "background:#F0FDF4;color:#166534;\"><b>权限策略说明：</b>%1</div>")
                    .arg(result.effectiveReason.toHtmlEscaped());
    }
    if (!result.approvalPrompt.isEmpty()) {
        html += QStringLiteral(
                    "<div style=\"margin:0 0 10px 0;padding:9px 11px;border-left:3px solid #2563EB;"
                    "background:#EFF6FF;color:#1E3A8A;\"><b>运行请求预览：</b>%1</div>")
                    .arg(result.approvalPrompt.toHtmlEscaped());
    }
    if (!result.blockReasonCode.isEmpty()) {
        html += QStringLiteral("<p style=\"margin:6px 0;color:#B45309;\"><b>阻止原因码：</b>%1</p>")
                    .arg(result.blockReasonCode.toHtmlEscaped());
    }

    html += QStringLiteral(
                "<div style=\"margin:0 0 10px 0;padding:10px 12px;border:1px solid #DDEBFA;"
                "border-radius:10px;background:#F8FBFF;\">"
                "<p style=\"margin:0 0 6px 0;color:#0F172A;\"><b>执行预案</b></p>");
    html += QStringLiteral("<p style=\"margin:4px 0;\"><b>cwd 规则：</b>%1</p>")
                .arg(result.cwdPolicy.isEmpty()
                         ? QStringLiteral("未来执行前由 Runtime 固定到受控工作区。")
                         : result.cwdPolicy.toHtmlEscaped());
    html += QStringLiteral("<p style=\"margin:4px 0;\"><b>沙箱提示：</b>%1</p>")
                .arg(result.sandboxHint.isEmpty()
                         ? QStringLiteral("当前仅做静态检查，不执行命令。")
                         : result.sandboxHint.toHtmlEscaped());
    if (!result.auditFields.isEmpty()) {
        html += QStringLiteral("<p style=\"margin:8px 0 4px 0;\"><b>后续审计字段：</b>%1</p>")
                    .arg(result.auditFields.join(QStringLiteral("、")).toHtmlEscaped());
    }
    if (!result.auditRecordPreview.isEmpty()) {
        // 这里只展示审计骨架包含哪些键，避免把完整 JSON 塞满代码工坊页面。
        html += QStringLiteral("<p style=\"margin:8px 0 4px 0;\"><b>审计预览键：</b>%1</p>")
                    .arg(result.auditRecordPreview.keys().join(QStringLiteral("、")).toHtmlEscaped());
    }
    if (!result.executionNotes.isEmpty()) {
        html += QStringLiteral("<p style=\"margin:8px 0 4px 0;\"><b>执行注意事项</b></p>");
        html += dispatchBulletListHtml(result.executionNotes);
    }
    html += QStringLiteral("</div>");

    html += QStringLiteral("<p style=\"margin:6px 0;\"><b>识别到的命令：</b>%1</p>")
                .arg(result.detectedCommands.isEmpty()
                         ? QStringLiteral("无")
                         : result.detectedCommands.join(QStringLiteral("、")).toHtmlEscaped());
    html += QStringLiteral("<p style=\"margin:6px 0;\"><b>分类标签：</b>%1</p>")
                .arg(result.categories.isEmpty()
                         ? QStringLiteral("无")
                         : result.categories.join(QStringLiteral("、")).toHtmlEscaped());
    if (!result.ruleIds.isEmpty()) {
        html += QStringLiteral("<p style=\"margin:6px 0;\"><b>命中规则：</b>%1</p>")
                    .arg(result.ruleIds.join(QStringLiteral("、")).toHtmlEscaped());
    }

    if (!result.reasons.isEmpty()) {
        html += QStringLiteral("<p style=\"margin:10px 0 4px 0;\"><b>判断原因</b></p>");
        html += dispatchBulletListHtml(result.reasons);
    }
    if (!result.destructiveWarnings.isEmpty()) {
        html += QStringLiteral("<p style=\"margin:10px 0 4px 0;color:#B45309;\"><b>破坏性提示</b></p>");
        html += dispatchBulletListHtml(result.destructiveWarnings);
    }
    if (!result.saferAlternatives.isEmpty()) {
        html += QStringLiteral("<p style=\"margin:10px 0 4px 0;color:#2563EB;\"><b>更安全的下一步</b></p>");
        html += dispatchBulletListHtml(result.saferAlternatives);
    }
    if (!result.warnings.isEmpty()) {
        html += QStringLiteral("<p style=\"margin:10px 0 4px 0;color:#EA580C;\"><b>警告</b></p>");
        html += dispatchBulletListHtml(result.warnings);
    }
    if (!result.suggestedTool.isEmpty()) {
        html += QStringLiteral(
                    "<div style=\"margin-top:10px;padding:9px 11px;border-left:3px solid #60A5FA;"
                    "background:#EFF6FF;color:#334155;\"><b>建议：</b>%1</div>")
                    .arg(result.suggestedTool.toHtmlEscaped());
    }
    return html;
}

void MainWindow::setupSettingsPage()
{
    // 设置页结构在 mainwindow.ui 中维护；这里仅绑定运行偏好表单和后端状态。
    settingsRuntimeStatusBadge = ui->settingsRuntimeStatusBadge;
    settingsPermissionPolicyCombo = ui->settingsPermissionPolicyCombo;
    settingsPersonalityCombo = ui->settingsPersonalityCombo;
    settingsMemoryEnabledCheck = ui->settingsMemoryEnabledCheck;
    settingsRefreshPreferencesButton = ui->settingsRefreshPreferencesButton;
    settingsManageMemoriesButton = ui->settingsManageMemoriesButton;
    settingsSavePreferencesButton = ui->settingsSavePreferencesButton;
    settingsRuntimeNotesText = ui->settingsRuntimeNotesText;

    settingsPermissionPolicyCombo->addItem(QStringLiteral("请求批准"), QStringLiteral("always_ask"));
    settingsPermissionPolicyCombo->addItem(QStringLiteral("替我审批"), QStringLiteral("auto_approve"));
    settingsPermissionPolicyCombo->addItem(QStringLiteral("风险操作确认"), QStringLiteral("smart_confirm"));
    settingsPermissionPolicyCombo->addItem(QStringLiteral("完全访问"), QStringLiteral("full_access"));

    settingsPersonalityCombo->addItem(QStringLiteral("专业稳重"), QStringLiteral("professional"));
    settingsPersonalityCombo->addItem(QStringLiteral("简洁直接"), QStringLiteral("concise"));
    settingsPersonalityCombo->addItem(QStringLiteral("温和陪伴"), QStringLiteral("warm"));
    settingsPersonalityCombo->addItem(QStringLiteral("创意活泼"), QStringLiteral("creative"));

    settingsRefreshPreferencesButton->setIcon(style()->standardIcon(QStyle::SP_BrowserReload));
    settingsManageMemoriesButton->setIcon(style()->standardIcon(QStyle::SP_FileDialogDetailedView));
    settingsSavePreferencesButton->setIcon(style()->standardIcon(QStyle::SP_DialogSaveButton));
    connect(settingsRefreshPreferencesButton, &QPushButton::clicked, this, &MainWindow::refreshRuntimePreferences);
    connect(settingsManageMemoriesButton, &QPushButton::clicked, this, &MainWindow::openLongTermMemoryManager);
    connect(settingsSavePreferencesButton, &QPushButton::clicked, this, &MainWindow::saveRuntimePreferencesFromSettings);

    const auto markDirty = [this](int) {
        if (runtimePreferencesLoading || runtimePreferencesSaving) {
            return;
        }
        polishBadge(settingsRuntimeStatusBadge, QStringLiteral("badgeOrange"));
        if (settingsRuntimeStatusBadge) {
            settingsRuntimeStatusBadge->setText(QStringLiteral("未保存"));
        }
    };
    connect(settingsPermissionPolicyCombo, &QComboBox::currentIndexChanged, this, markDirty);
    connect(settingsPersonalityCombo, &QComboBox::currentIndexChanged, this, markDirty);
    connect(settingsMemoryEnabledCheck, &QCheckBox::toggled, this, [markDirty](bool) {
        markDirty(0);
    });

    polishBadge(settingsRuntimeStatusBadge, QStringLiteral("badgeGray"));
    settingsRuntimeNotesText->setHtml(
        QStringLiteral("<p style=\"color:#64748B;\">后端就绪后会读取当前运行偏好。</p>"));
}

void MainWindow::setupMcpConnectionsPage()
{
    // 页面结构与按钮语义保存在 mainwindow.ui；这里只负责状态绑定。首期没有可编辑 URL、
    // 命令、环境变量或密钥，避免“插件管理”意外变成任意 MCP 进程启动入口。
    ui->pluginsConnectionEnableButton->setIcon(style()->standardIcon(QStyle::SP_MediaPlay));
    ui->pluginsConnectionTestButton->setIcon(style()->standardIcon(QStyle::SP_DialogApplyButton));
    ui->pluginsConnectionDisableButton->setIcon(style()->standardIcon(QStyle::SP_DialogCancelButton));
    ui->pluginsConnectionEnableButton->setEnabled(false);
    ui->pluginsConnectionTestButton->setEnabled(false);
    ui->pluginsConnectionDisableButton->setEnabled(false);

    connect(ui->pluginsConnectionEnableButton, &QPushButton::clicked, this, [this]() {
        const auto decision = QMessageBox::question(
            this,
            QStringLiteral("启用公开资料连接"),
            QStringLiteral("启用后，AI 调度台可在你批准联网权限时查询固定的 Wikimedia 公开资料。\n\n"
                           "不会立即联网，也不会开放任意网址、命令或密钥配置。"),
            QMessageBox::Cancel | QMessageBox::Yes,
            QMessageBox::Cancel);
        if (decision == QMessageBox::Yes && backendClient) {
            ui->pluginsConnectionEnableButton->setEnabled(false);
            backendClient->setPublicReferenceMcpEnabled(true);
        }
    });
    connect(ui->pluginsConnectionTestButton, &QPushButton::clicked, this, [this]() {
        if (!backendClient) {
            return;
        }
        ui->pluginsConnectionTestButton->setEnabled(false);
        ui->pluginsConnectionStatus->setText(QStringLiteral("检测中"));
        polishBadge(ui->pluginsConnectionStatus, QStringLiteral("badgeBlue"));
        backendClient->testPublicReferenceMcpConnection();
    });
    connect(ui->pluginsConnectionDisableButton, &QPushButton::clicked, this, [this]() {
        const auto decision = QMessageBox::question(
            this,
            QStringLiteral("停用公开资料连接"),
            QStringLiteral("停用后，AI 调度台不会再为公开资料检索生成 MCP 执行计划。\n\n"
                           "已有任务历史不会被删除。"),
            QMessageBox::Cancel | QMessageBox::Yes,
            QMessageBox::Cancel);
        if (decision == QMessageBox::Yes && backendClient) {
            ui->pluginsConnectionDisableButton->setEnabled(false);
            backendClient->setPublicReferenceMcpEnabled(false);
        }
    });

    polishBadge(ui->pluginsConnectionStatus, QStringLiteral("badgeGray"));
    ui->pluginsConnectionStatus->setText(QStringLiteral("等待后端"));
}

void MainWindow::refreshMcpConnections()
{
    if (!backendClient || !backendManager || !backendManager->isReady()) {
        return;
    }
    ui->pluginsConnectionEnableButton->setEnabled(false);
    ui->pluginsConnectionTestButton->setEnabled(false);
    ui->pluginsConnectionDisableButton->setEnabled(false);
    polishBadge(ui->pluginsConnectionStatus, QStringLiteral("badgeBlue"));
    ui->pluginsConnectionStatus->setText(QStringLiteral("加载中"));
    backendClient->requestMcpConnections();
}

void MainWindow::updatePublicReferenceMcpUi(const McpConnectionInfo &connection, const QString &message)
{
    currentPublicReferenceMcpConnection = connection;
    const bool enabled = connection.enabled;
    const bool ready = connection.status == QStringLiteral("ready");
    const bool degraded = connection.status == QStringLiteral("degraded");
    const bool platformDisabled = connection.status == QStringLiteral("platform_disabled");
    const QString statusText = platformDisabled ? QStringLiteral("平台已关闭")
                             : degraded         ? QStringLiteral("需要处理")
                             : ready            ? QStringLiteral("已就绪")
                             : enabled          ? QStringLiteral("已启用")
                                                : QStringLiteral("未启用");
    const QString badge = (ready ? QStringLiteral("badgeGreen")
                         : degraded || platformDisabled ? QStringLiteral("badgeOrange")
                         : enabled ? QStringLiteral("badgeBlue")
                                   : QStringLiteral("badgeGray"));
    polishBadge(ui->pluginsConnectionStatus, badge);
    ui->pluginsConnectionStatus->setText(statusText);

    QString meta = connection.originSummary.trimmed();
    if (!message.trimmed().isEmpty()) {
        meta = message.trimmed();
    } else if (!connection.lastCheckedAt.trimmed().isEmpty()) {
        meta += QStringLiteral(" · 最近检测 %1 · %2 个 Tool")
                    .arg(connection.lastCheckedAt.left(19).replace(QLatin1Char('T'), QLatin1Char(' ')))
                    .arg(connection.lastToolCount);
    }
    if (degraded && !connection.lastErrorCode.isEmpty()) {
        meta += QStringLiteral(" · 检测失败：%1").arg(connection.lastErrorCode);
    }
    if (meta.isEmpty()) {
        meta = QStringLiteral("启用连接不会联网；实际检索会单独请求联网与受控服务启动权限。");
    }
    ui->pluginsConnectionMeta->setText(meta);
    ui->pluginsConnectionEnableButton->setEnabled(!enabled && !platformDisabled);
    ui->pluginsConnectionTestButton->setEnabled(enabled && !platformDisabled);
    ui->pluginsConnectionDisableButton->setEnabled(enabled);
}

void MainWindow::handleMcpConnectionsReceived(const QList<McpConnectionInfo> &connections)
{
    const auto iterator = std::find_if(
        connections.cbegin(), connections.cend(), [](const McpConnectionInfo &item) {
            return item.connectionId == QStringLiteral("public-reference");
        });
    if (iterator == connections.cend()) {
        handleMcpConnectionsFailed(QStringLiteral("未找到内置公开资料连接。"));
        return;
    }
    updatePublicReferenceMcpUi(*iterator);
}

void MainWindow::handleMcpConnectionsFailed(const QString &message)
{
    polishBadge(ui->pluginsConnectionStatus, QStringLiteral("badgeOrange"));
    ui->pluginsConnectionStatus->setText(QStringLiteral("加载失败"));
    ui->pluginsConnectionMeta->setText(QStringLiteral("无法读取受控连接状态：%1").arg(message.left(180)));
    ui->pluginsConnectionEnableButton->setEnabled(false);
    ui->pluginsConnectionTestButton->setEnabled(false);
    ui->pluginsConnectionDisableButton->setEnabled(false);
}

void MainWindow::handleMcpConnectionUpdated(const McpConnectionInfo &connection, const QString &message)
{
    updatePublicReferenceMcpUi(connection, message);
}

void MainWindow::handleMcpConnectionUpdateFailed(const QString &message)
{
    updatePublicReferenceMcpUi(
        currentPublicReferenceMcpConnection,
        QStringLiteral("操作未完成：%1").arg(message.left(180)));
}

void MainWindow::refreshRuntimePreferences()
{
    if (!backendClient) {
        return;
    }
    runtimePreferencesLoading = true;
    if (settingsRefreshPreferencesButton) {
        settingsRefreshPreferencesButton->setEnabled(false);
    }
    if (settingsSavePreferencesButton) {
        settingsSavePreferencesButton->setEnabled(false);
    }
    polishBadge(settingsRuntimeStatusBadge, QStringLiteral("badgeBlue"));
    if (settingsRuntimeStatusBadge) {
        settingsRuntimeStatusBadge->setText(QStringLiteral("加载中"));
    }
    if (settingsRuntimeNotesText) {
        settingsRuntimeNotesText->setHtml(
            QStringLiteral("<p style=\"color:#2563EB;\">正在读取本地运行偏好……</p>"));
    }
    backendClient->requestRuntimePreferences();
}

void MainWindow::saveRuntimePreferencesFromSettings()
{
    if (!backendClient || !settingsPermissionPolicyCombo || !settingsPersonalityCombo || !settingsMemoryEnabledCheck) {
        return;
    }

    runtimePreferencesSaving = true;
    if (settingsRefreshPreferencesButton) {
        settingsRefreshPreferencesButton->setEnabled(false);
    }
    if (settingsSavePreferencesButton) {
        settingsSavePreferencesButton->setEnabled(false);
        settingsSavePreferencesButton->setText(QStringLiteral("保存中"));
    }
    polishBadge(settingsRuntimeStatusBadge, QStringLiteral("badgeBlue"));
    if (settingsRuntimeStatusBadge) {
        settingsRuntimeStatusBadge->setText(QStringLiteral("保存中"));
    }

    backendClient->saveRuntimePreferences(
        settingsPermissionPolicyCombo->currentData().toString(),
        settingsPersonalityCombo->currentData().toString(),
        settingsMemoryEnabledCheck->isChecked());
}

void MainWindow::handleRuntimePreferencesReceived(const RuntimePreferencesResult &result)
{
    runtimePreferencesLoading = false;
    if (settingsRefreshPreferencesButton) {
        settingsRefreshPreferencesButton->setEnabled(true);
    }
    if (settingsSavePreferencesButton) {
        settingsSavePreferencesButton->setEnabled(true);
        settingsSavePreferencesButton->setText(QStringLiteral("保存设置"));
    }
    applyRuntimePreferencesToSettings(result);
    polishBadge(settingsRuntimeStatusBadge, QStringLiteral("badgeGreen"));
    if (settingsRuntimeStatusBadge) {
        settingsRuntimeStatusBadge->setText(QStringLiteral("已加载"));
    }
}

void MainWindow::handleRuntimePreferencesFailed(const QString &message)
{
    runtimePreferencesLoading = false;
    if (settingsRefreshPreferencesButton) {
        settingsRefreshPreferencesButton->setEnabled(true);
    }
    if (settingsSavePreferencesButton) {
        settingsSavePreferencesButton->setEnabled(true);
    }
    polishBadge(settingsRuntimeStatusBadge, QStringLiteral("badgeOrange"));
    if (settingsRuntimeStatusBadge) {
        settingsRuntimeStatusBadge->setText(QStringLiteral("加载失败"));
    }
    if (settingsRuntimeNotesText) {
        settingsRuntimeNotesText->setHtml(
            QStringLiteral("<p style=\"color:#DC2626;\"><b>运行偏好加载失败：</b>%1</p>")
                .arg(message.toHtmlEscaped()));
    }
}

void MainWindow::handleRuntimePreferencesSaved(const RuntimePreferencesResult &result)
{
    runtimePreferencesSaving = false;
    if (settingsRefreshPreferencesButton) {
        settingsRefreshPreferencesButton->setEnabled(true);
    }
    if (settingsSavePreferencesButton) {
        settingsSavePreferencesButton->setEnabled(true);
        settingsSavePreferencesButton->setText(QStringLiteral("保存设置"));
    }
    applyRuntimePreferencesToSettings(result);
    polishBadge(settingsRuntimeStatusBadge, QStringLiteral("badgeGreen"));
    if (settingsRuntimeStatusBadge) {
        settingsRuntimeStatusBadge->setText(QStringLiteral("已保存"));
    }
}

void MainWindow::handleRuntimePreferencesSaveFailed(const QString &message)
{
    runtimePreferencesSaving = false;
    if (settingsRefreshPreferencesButton) {
        settingsRefreshPreferencesButton->setEnabled(true);
    }
    if (settingsSavePreferencesButton) {
        settingsSavePreferencesButton->setEnabled(true);
        settingsSavePreferencesButton->setText(QStringLiteral("保存设置"));
    }
    polishBadge(settingsRuntimeStatusBadge, QStringLiteral("badgeOrange"));
    if (settingsRuntimeStatusBadge) {
        settingsRuntimeStatusBadge->setText(QStringLiteral("保存失败"));
    }
    if (settingsRuntimeNotesText) {
        settingsRuntimeNotesText->setHtml(
            QStringLiteral("<p style=\"color:#DC2626;\"><b>运行偏好保存失败：</b>%1</p>")
                .arg(message.toHtmlEscaped()));
    }
}

void MainWindow::applyRuntimePreferencesToSettings(const RuntimePreferencesResult &result)
{
    runtimePreferencesLoading = true;
    if (settingsPermissionPolicyCombo) {
        const int index = settingsPermissionPolicyCombo->findData(result.permissionPolicy);
        if (index >= 0) {
            settingsPermissionPolicyCombo->setCurrentIndex(index);
        }
    }
    if (settingsPersonalityCombo) {
        const int index = settingsPersonalityCombo->findData(result.personality);
        if (index >= 0) {
            settingsPersonalityCombo->setCurrentIndex(index);
        }
    }
    if (settingsMemoryEnabledCheck) {
        settingsMemoryEnabledCheck->setChecked(result.memoryEnabled);
    }
    runtimePreferencesLoading = false;
    if (settingsRuntimeNotesText) {
        settingsRuntimeNotesText->setHtml(formatRuntimePreferencesNotesHtml(result));
    }
}

QString MainWindow::runtimePermissionPolicyText(const QString &value) const
{
    if (value == QStringLiteral("always_ask")) {
        return QStringLiteral("请求批准");
    }
    if (value == QStringLiteral("auto_approve")) {
        return QStringLiteral("替我审批");
    }
    if (value == QStringLiteral("smart_confirm")) {
        return QStringLiteral("风险操作确认");
    }
    if (value == QStringLiteral("full_access")) {
        return QStringLiteral("完全访问");
    }
    return value;
}

QString MainWindow::runtimePersonalityText(const QString &value) const
{
    if (value == QStringLiteral("professional")) {
        return QStringLiteral("专业稳重");
    }
    if (value == QStringLiteral("concise")) {
        return QStringLiteral("简洁直接");
    }
    if (value == QStringLiteral("warm")) {
        return QStringLiteral("温和陪伴");
    }
    if (value == QStringLiteral("creative")) {
        return QStringLiteral("创意活泼");
    }
    return value;
}

QString MainWindow::formatRuntimePreferencesNotesHtml(const RuntimePreferencesResult &result) const
{
    QString html = QStringLiteral("<h3 style=\"margin:0 0 8px 0;color:#0F172A;\">当前运行偏好</h3>");
    html += QStringLiteral("<p style=\"margin:4px 0;\"><b>权限确认：</b>%1</p>")
                .arg(runtimePermissionPolicyText(result.permissionPolicy).toHtmlEscaped());
    html += QStringLiteral("<p style=\"margin:4px 0;\"><b>语言风格：</b>%1</p>")
                .arg(runtimePersonalityText(result.personality).toHtmlEscaped());
    html += QStringLiteral("<p style=\"margin:4px 0;\"><b>长期记忆：</b>%1</p>")
                .arg(result.memoryEnabled
                         ? QStringLiteral("已开启，仅读取用户确认的相关短记忆")
                         : QStringLiteral("已关闭，不读取任何长期记忆"));
    html += QStringLiteral("<p style=\"margin:4px 0;\"><b>更新时间：</b>%1</p>")
                .arg(result.updatedAt.isEmpty() ? QStringLiteral("尚未保存") : result.updatedAt.toHtmlEscaped());
    if (!result.notes.isEmpty()) {
        html += QStringLiteral(
                    "<div style=\"margin-top:8px;padding:8px 10px;border-left:3px solid #60A5FA;"
                    "background:#EFF6FF;color:#334155;\">%1</div>")
                    .arg(result.notes.toHtmlEscaped());
    }
    return html;
}

void MainWindow::openLongTermMemoryManager()
{
    if (longTermMemoryDialog) {
        longTermMemoryDialog->show();
        longTermMemoryDialog->raise();
        longTermMemoryDialog->activateWindow();
        refreshLongTermMemoryManager();
        return;
    }

    // 设置主页面只承载开关；低频的查看、编辑与不可逆删除操作在独立详情窗口完成，给摘要和
    // 表单留下充分空间，也让用户明确知道自己正在处理跨任务数据。
    auto *dialog = new QDialog(this);
    dialog->setAttribute(Qt::WA_DeleteOnClose);
    dialog->setWindowTitle(QStringLiteral("长期记忆管理"));
    dialog->setMinimumSize(900, 600);
    dialog->resize(1080, 700);
    longTermMemoryDialog = dialog;

    auto *rootLayout = new QVBoxLayout(dialog);
    rootLayout->setContentsMargins(24, 20, 24, 20);
    rootLayout->setSpacing(14);

    auto *headerLayout = new QHBoxLayout();
    headerLayout->setSpacing(10);
    auto *titleLayout = new QVBoxLayout();
    titleLayout->setSpacing(3);
    auto *title = new QLabel(QStringLiteral("长期记忆"), dialog);
    title->setObjectName(QStringLiteral("sectionTitle"));
    auto *subtitle = new QLabel(
        QStringLiteral("只保存你确认过的偏好、项目约束和已验证经验。不会保存 API Key、原文件内容或完整对话。"),
        dialog);
    subtitle->setObjectName(QStringLiteral("subText"));
    subtitle->setWordWrap(true);
    titleLayout->addWidget(title);
    titleLayout->addWidget(subtitle);
    headerLayout->addLayout(titleLayout, 1);
    auto *statusLabel = new QLabel(QStringLiteral("准备加载"), dialog);
    statusLabel->setObjectName(QStringLiteral("badgeGray"));
    statusLabel->setAlignment(Qt::AlignCenter);
    headerLayout->addWidget(statusLabel, 0, Qt::AlignRight | Qt::AlignTop);
    rootLayout->addLayout(headerLayout);

    auto *splitter = new QSplitter(Qt::Horizontal, dialog);
    splitter->setChildrenCollapsible(false);
    auto *table = new QTableWidget(splitter);
    table->setColumnCount(4);
    table->setHorizontalHeaderLabels({QStringLiteral("类型"), QStringLiteral("标题"), QStringLiteral("范围"), QStringLiteral("状态")});
    table->setSelectionBehavior(QAbstractItemView::SelectRows);
    table->setSelectionMode(QAbstractItemView::SingleSelection);
    table->setEditTriggers(QAbstractItemView::NoEditTriggers);
    table->setAlternatingRowColors(true);
    table->verticalHeader()->setVisible(false);
    table->horizontalHeader()->setStretchLastSection(true);
    table->horizontalHeader()->setSectionResizeMode(0, QHeaderView::ResizeToContents);
    table->horizontalHeader()->setSectionResizeMode(1, QHeaderView::Stretch);
    table->horizontalHeader()->setSectionResizeMode(2, QHeaderView::ResizeToContents);
    table->setMinimumWidth(450);

    auto *editorFrame = new QFrame(splitter);
    editorFrame->setObjectName(QStringLiteral("contentCard"));
    auto *editorLayout = new QVBoxLayout(editorFrame);
    editorLayout->setContentsMargins(18, 16, 18, 16);
    editorLayout->setSpacing(12);
    auto *editorTitle = new QLabel(QStringLiteral("记忆详情"), editorFrame);
    editorTitle->setObjectName(QStringLiteral("sectionTitle"));
    editorLayout->addWidget(editorTitle);
    auto *editorHint = new QLabel(
        QStringLiteral("新建时请只写稳定、可复用的短事实。关闭某条记忆会保留它供你以后查看或重新启用。"),
        editorFrame);
    editorHint->setObjectName(QStringLiteral("subText"));
    editorHint->setWordWrap(true);
    editorLayout->addWidget(editorHint);

    auto *form = new QFormLayout();
    form->setHorizontalSpacing(12);
    form->setVerticalSpacing(10);
    auto *kindCombo = new QComboBox(editorFrame);
    kindCombo->addItem(QStringLiteral("用户偏好"), QStringLiteral("user_preference"));
    kindCombo->addItem(QStringLiteral("项目约束"), QStringLiteral("project_constraint"));
    kindCombo->addItem(QStringLiteral("已验证经验"), QStringLiteral("experience"));
    auto *scopeLabel = new QLabel(QStringLiteral("全局（项目级范围将在项目入口接入后开放）"), editorFrame);
    scopeLabel->setObjectName(QStringLiteral("tinyText"));
    auto *titleInput = new QLineEdit(editorFrame);
    titleInput->setPlaceholderText(QStringLiteral("例如：项目交付约束"));
    auto *summaryInput = new QPlainTextEdit(editorFrame);
    summaryInput->setPlaceholderText(QStringLiteral("例如：项目方案优先给出范围、验收标准和待确认事项。"));
    summaryInput->setMinimumHeight(150);
    auto *tagsInput = new QLineEdit(editorFrame);
    tagsInput->setPlaceholderText(QStringLiteral("标签用逗号分隔，例如：项目, 验收, 交付"));
    auto *enabledCheck = new QCheckBox(QStringLiteral("允许总指挥在开关开启时读取这条记忆"), editorFrame);
    enabledCheck->setChecked(true);
    enabledCheck->setToolTip(QStringLiteral("关闭后不会被计划检索；记录仍可在本窗口查看、编辑或删除。"));
    form->addRow(QStringLiteral("类型"), kindCombo);
    form->addRow(QStringLiteral("范围"), scopeLabel);
    form->addRow(QStringLiteral("标题"), titleInput);
    form->addRow(QStringLiteral("摘要"), summaryInput);
    form->addRow(QStringLiteral("标签"), tagsInput);
    form->addRow(QString(), enabledCheck);
    editorLayout->addLayout(form, 1);

    auto *buttonLayout = new QHBoxLayout();
    auto *newButton = new QPushButton(QStringLiteral("新建"), editorFrame);
    newButton->setObjectName(QStringLiteral("ghostButton"));
    auto *deleteButton = new QPushButton(QStringLiteral("删除"), editorFrame);
    deleteButton->setObjectName(QStringLiteral("ghostButton"));
    auto *clearButton = new QPushButton(QStringLiteral("清空全部"), editorFrame);
    clearButton->setObjectName(QStringLiteral("ghostButton"));
    auto *saveButton = new QPushButton(QStringLiteral("保存记忆"), editorFrame);
    saveButton->setObjectName(QStringLiteral("primaryButton"));
    buttonLayout->addWidget(newButton);
    buttonLayout->addWidget(deleteButton);
    buttonLayout->addWidget(clearButton);
    buttonLayout->addStretch(1);
    buttonLayout->addWidget(saveButton);
    editorLayout->addLayout(buttonLayout);

    splitter->addWidget(table);
    splitter->addWidget(editorFrame);
    splitter->setStretchFactor(0, 1);
    splitter->setStretchFactor(1, 1);
    splitter->setSizes({520, 520});
    rootLayout->addWidget(splitter, 1);

    longTermMemoryTable = table;
    longTermMemoryKindCombo = kindCombo;
    longTermMemoryTitleInput = titleInput;
    longTermMemorySummaryInput = summaryInput;
    longTermMemoryTagsInput = tagsInput;
    longTermMemoryEnabledCheck = enabledCheck;
    longTermMemoryStatusLabel = statusLabel;
    longTermMemorySaveButton = saveButton;
    longTermMemoryDeleteButton = deleteButton;

    connect(table, &QTableWidget::itemSelectionChanged, this, [this]() {
        if (!longTermMemoryTable) {
            return;
        }
        const int row = longTermMemoryTable->currentRow();
        if (row < 0 || row >= currentLongTermMemories.size()) {
            populateLongTermMemoryEditor(nullptr);
            return;
        }
        populateLongTermMemoryEditor(&currentLongTermMemories.at(row));
    });
    connect(newButton, &QPushButton::clicked, this, [this]() {
        if (longTermMemoryTable) {
            longTermMemoryTable->clearSelection();
        }
        populateLongTermMemoryEditor(nullptr);
    });
    connect(saveButton, &QPushButton::clicked, this, [this]() {
        if (!backendClient || !longTermMemoryKindCombo || !longTermMemoryTitleInput
            || !longTermMemorySummaryInput || !longTermMemoryTagsInput || !longTermMemoryEnabledCheck) {
            return;
        }
        const QString titleValue = longTermMemoryTitleInput->text().trimmed();
        const QString summaryValue = longTermMemorySummaryInput->toPlainText().trimmed();
        if (titleValue.size() < 2 || summaryValue.size() < 2) {
            if (longTermMemoryStatusLabel) {
                longTermMemoryStatusLabel->setText(QStringLiteral("请补全标题和摘要"));
            }
            return;
        }
        const QStringList tags = longTermMemoryTagsInput->text().split(
            QRegularExpression(QStringLiteral("[,，]")), Qt::SkipEmptyParts);
        if (longTermMemorySaveButton) {
            longTermMemorySaveButton->setEnabled(false);
        }
        if (longTermMemoryStatusLabel) {
            longTermMemoryStatusLabel->setText(QStringLiteral("正在保存"));
        }
        if (currentLongTermMemoryId.isEmpty()) {
            backendClient->createLongTermMemory(
                longTermMemoryKindCombo->currentData().toString(),
                QStringLiteral("global"),
                titleValue,
                summaryValue,
                tags);
        } else {
            backendClient->updateLongTermMemory(
                currentLongTermMemoryId,
                titleValue,
                summaryValue,
                tags,
                longTermMemoryEnabledCheck->isChecked());
        }
    });
    connect(deleteButton, &QPushButton::clicked, this, [this]() {
        if (!backendClient || currentLongTermMemoryId.isEmpty()) {
            return;
        }
        const auto answer = QMessageBox::warning(
            this,
            QStringLiteral("删除长期记忆"),
            QStringLiteral("删除后总指挥将无法再引用这条记忆，且无法恢复。确定删除吗？"),
            QMessageBox::Yes | QMessageBox::Cancel,
            QMessageBox::Cancel);
        if (answer != QMessageBox::Yes) {
            return;
        }
        // 在刷新列表前先固定当前目标，避免异步回包改变表格选中行后误删其它记忆。
        const QString memoryIdToDelete = currentLongTermMemoryId;
        currentLongTermMemoryId.clear();
        backendClient->deleteLongTermMemory(memoryIdToDelete);
    });
    connect(clearButton, &QPushButton::clicked, this, [this]() {
        if (!backendClient) {
            return;
        }
        const auto answer = QMessageBox::warning(
            this,
            QStringLiteral("清空全局长期记忆"),
            QStringLiteral("这会删除全部全局长期记忆，且无法恢复。确定继续吗？"),
            QMessageBox::Yes | QMessageBox::Cancel,
            QMessageBox::Cancel);
        if (answer != QMessageBox::Yes) {
            return;
        }
        currentLongTermMemoryId.clear();
        backendClient->clearLongTermMemories(QStringLiteral("global"));
    });
    connect(dialog, &QObject::destroyed, this, [this]() {
        currentLongTermMemoryId.clear();
        currentLongTermMemories.clear();
        longTermMemoryLoading = false;
    });

    populateLongTermMemoryEditor(nullptr);
    dialog->show();
    refreshLongTermMemoryManager();
}

void MainWindow::refreshLongTermMemoryManager()
{
    if (!backendClient || !longTermMemoryDialog) {
        return;
    }
    longTermMemoryLoading = true;
    if (longTermMemoryStatusLabel) {
        longTermMemoryStatusLabel->setText(QStringLiteral("加载中"));
    }
    backendClient->requestLongTermMemories();
}

void MainWindow::handleLongTermMemoriesReceived(const QList<LongTermMemoryInfo> &items)
{
    longTermMemoryLoading = false;
    if (!longTermMemoryDialog || !longTermMemoryTable) {
        return;
    }
    int selectedRow = -1;
    currentLongTermMemories = items;
    {
        // 重建表格时抑制 selectionChanged；下方会显式回填编辑器，避免出现列表已刷新但表单仍是旧内容。
        const QSignalBlocker blocker(longTermMemoryTable);
        longTermMemoryTable->setRowCount(items.size());
        for (int row = 0; row < items.size(); ++row) {
            const LongTermMemoryInfo &item = items.at(row);
            auto *kindItem = new QTableWidgetItem(longTermMemoryKindText(item.kind));
            kindItem->setData(Qt::UserRole, item.memoryId);
            longTermMemoryTable->setItem(row, 0, kindItem);
            longTermMemoryTable->setItem(row, 1, new QTableWidgetItem(item.title));
            longTermMemoryTable->setItem(row, 2, new QTableWidgetItem(item.scope));
            longTermMemoryTable->setItem(
                row, 3, new QTableWidgetItem(item.enabled ? QStringLiteral("可引用") : QStringLiteral("已关闭")));
            if (item.memoryId == currentLongTermMemoryId) {
                selectedRow = row;
            }
        }
        if (selectedRow >= 0) {
            longTermMemoryTable->selectRow(selectedRow);
        }
    }
    if (selectedRow >= 0) {
        populateLongTermMemoryEditor(&currentLongTermMemories.at(selectedRow));
    } else {
        currentLongTermMemoryId.clear();
        populateLongTermMemoryEditor(nullptr);
    }
    if (longTermMemoryStatusLabel) {
        longTermMemoryStatusLabel->setText(QStringLiteral("已加载 %1 条").arg(items.size()));
    }
}

void MainWindow::handleLongTermMemoriesFailed(const QString &message)
{
    longTermMemoryLoading = false;
    if (longTermMemoryStatusLabel) {
        longTermMemoryStatusLabel->setText(QStringLiteral("加载失败"));
        longTermMemoryStatusLabel->setToolTip(message);
    }
}

void MainWindow::handleLongTermMemoryMutationCompleted(const QString &message)
{
    if (!longTermMemoryDialog) {
        return;
    }
    if (longTermMemorySaveButton) {
        longTermMemorySaveButton->setEnabled(true);
    }
    if (longTermMemoryStatusLabel) {
        longTermMemoryStatusLabel->setText(message);
    }
    refreshLongTermMemoryManager();
}

void MainWindow::handleLongTermMemoryMutationFailed(const QString &message)
{
    if (longTermMemorySaveButton) {
        longTermMemorySaveButton->setEnabled(true);
    }
    if (longTermMemoryStatusLabel) {
        longTermMemoryStatusLabel->setText(QStringLiteral("保存失败"));
        longTermMemoryStatusLabel->setToolTip(message);
    }
}

void MainWindow::populateLongTermMemoryEditor(const LongTermMemoryInfo *item)
{
    if (!longTermMemoryKindCombo || !longTermMemoryTitleInput || !longTermMemorySummaryInput
        || !longTermMemoryTagsInput || !longTermMemoryEnabledCheck || !longTermMemorySaveButton
        || !longTermMemoryDeleteButton) {
        return;
    }
    const QSignalBlocker kindBlocker(longTermMemoryKindCombo);
    const QSignalBlocker enabledBlocker(longTermMemoryEnabledCheck);
    if (item == nullptr) {
        currentLongTermMemoryId.clear();
        longTermMemoryKindCombo->setEnabled(true);
        longTermMemoryKindCombo->setCurrentIndex(0);
        longTermMemoryTitleInput->clear();
        longTermMemorySummaryInput->clear();
        longTermMemoryTagsInput->clear();
        longTermMemoryEnabledCheck->setChecked(true);
        longTermMemorySaveButton->setText(QStringLiteral("保存记忆"));
        longTermMemoryDeleteButton->setEnabled(false);
        return;
    }
    currentLongTermMemoryId = item->memoryId;
    const int kindIndex = longTermMemoryKindCombo->findData(item->kind);
    if (kindIndex >= 0) {
        longTermMemoryKindCombo->setCurrentIndex(kindIndex);
    }
    // 类型是来源语义的一部分，已有记录若要换类型应新建，避免审计记录被静默改义。
    longTermMemoryKindCombo->setEnabled(false);
    longTermMemoryTitleInput->setText(item->title);
    longTermMemorySummaryInput->setPlainText(item->summary);
    longTermMemoryTagsInput->setText(item->tags.join(QStringLiteral(", ")));
    longTermMemoryEnabledCheck->setChecked(item->enabled);
    longTermMemorySaveButton->setText(QStringLiteral("更新记忆"));
    longTermMemoryDeleteButton->setEnabled(true);
}

QString MainWindow::longTermMemoryKindText(const QString &kind) const
{
    if (kind == QStringLiteral("user_preference")) {
        return QStringLiteral("用户偏好");
    }
    if (kind == QStringLiteral("project_constraint")) {
        return QStringLiteral("项目约束");
    }
    if (kind == QStringLiteral("experience")) {
        return QStringLiteral("已验证经验");
    }
    return kind;
}

void MainWindow::requestHistoryMemoryProposal()
{
    if (!backendClient || currentHistoryTaskId.isEmpty()) {
        return;
    }
    if (historyMemoryButton) {
        historyMemoryButton->setEnabled(false);
    }
    if (historyDetailText) {
        historyDetailText->setHtml(
            QStringLiteral("<p style=\"color:#64748B;\">正在检查本次任务中是否存在可确认的长期约束……</p>"));
    }
    backendClient->requestTaskMemoryProposals(currentHistoryTaskId);
}

void MainWindow::handleTaskMemoryProposalsReceived(const TaskMemoryProposalListResult &result)
{
    if (result.taskId != currentHistoryTaskId) {
        return;
    }
    if (historyMemoryButton) {
        historyMemoryButton->setEnabled(true);
    }
    if (result.items.isEmpty()) {
        if (historyDetailText) {
            historyDetailText->setHtml(
                QStringLiteral("<p><b>本次没有长期记忆候选</b></p><p style=\"color:#64748B;\">%1</p>")
                    .arg(result.note.toHtmlEscaped()));
        }
        return;
    }

    // 当前服务端最多返回一条保守候选。独立对话框让客户能完整检查、编辑并确认，不挤占历史详情。
    activeHistoryMemoryProposal = result.items.first();
    auto *dialog = new QDialog(this);
    dialog->setAttribute(Qt::WA_DeleteOnClose);
    dialog->setWindowTitle(QStringLiteral("记住任务约束"));
    dialog->setMinimumSize(620, 440);
    dialog->resize(720, 520);
    historyMemoryProposalDialog = dialog;

    auto *layout = new QVBoxLayout(dialog);
    auto *intro = new QLabel(
        QStringLiteral("系统仅从客户明确的长期表达中提取候选，尚未保存。请检查后再确认，随时可在系统设置中修改或删除。"),
        dialog);
    intro->setWordWrap(true);
    intro->setObjectName(QStringLiteral("mutedText"));
    layout->addWidget(intro);

    auto *form = new QFormLayout();
    auto *kindCombo = new QComboBox(dialog);
    kindCombo->addItem(QStringLiteral("用户偏好"), QStringLiteral("user_preference"));
    kindCombo->addItem(QStringLiteral("项目约束"), QStringLiteral("project_constraint"));
    kindCombo->addItem(QStringLiteral("已验证经验"), QStringLiteral("experience"));
    const int kindIndex = kindCombo->findData(activeHistoryMemoryProposal.kind);
    kindCombo->setCurrentIndex(kindIndex >= 0 ? kindIndex : 0);
    auto *scopeInput = new QLineEdit(activeHistoryMemoryProposal.suggestedScope, dialog);
    scopeInput->setPlaceholderText(QStringLiteral("global 或 project:项目标识"));
    auto *titleInput = new QLineEdit(activeHistoryMemoryProposal.title, dialog);
    auto *summaryInput = new QPlainTextEdit(activeHistoryMemoryProposal.summary, dialog);
    summaryInput->setMinimumHeight(120);
    auto *tagsInput = new QLineEdit(activeHistoryMemoryProposal.tags.join(QStringLiteral(", ")), dialog);
    tagsInput->setPlaceholderText(QStringLiteral("可选标签，用逗号分隔"));
    form->addRow(QStringLiteral("类型"), kindCombo);
    form->addRow(QStringLiteral("保存范围"), scopeInput);
    form->addRow(QStringLiteral("标题"), titleInput);
    form->addRow(QStringLiteral("摘要"), summaryInput);
    form->addRow(QStringLiteral("标签"), tagsInput);
    layout->addLayout(form, 1);

    auto *reasonLabel = new QLabel(activeHistoryMemoryProposal.reason, dialog);
    reasonLabel->setWordWrap(true);
    reasonLabel->setObjectName(QStringLiteral("tinyText"));
    layout->addWidget(reasonLabel);
    auto *statusLabel = new QLabel(dialog);
    statusLabel->setObjectName(QStringLiteral("tinyText"));
    layout->addWidget(statusLabel);
    auto *buttons = new QDialogButtonBox(QDialogButtonBox::Cancel, dialog);
    auto *confirmButton = buttons->addButton(QStringLiteral("确认保存"), QDialogButtonBox::AcceptRole);
    layout->addWidget(buttons);
    connect(buttons, &QDialogButtonBox::rejected, dialog, &QDialog::close);
    connect(confirmButton, &QPushButton::clicked, dialog, [this, kindCombo, scopeInput, titleInput, summaryInput, tagsInput]() {
        if (!backendClient || !historyMemoryProposalDialog) {
            return;
        }
        const QString scope = scopeInput->text().trimmed().toLower();
        const QString title = titleInput->text().trimmed();
        const QString summary = summaryInput->toPlainText().trimmed();
        if (title.size() < 2 || summary.size() < 2) {
            if (historyMemoryProposalStatusLabel) {
                historyMemoryProposalStatusLabel->setText(QStringLiteral("标题和摘要至少需要 2 个字符。"));
            }
            return;
        }
        static const QRegularExpression scopePattern(
            QStringLiteral("^(global|project:[a-z0-9][a-z0-9_-]{0,63})$"));
        if (!scopePattern.match(scope).hasMatch()) {
            if (historyMemoryProposalStatusLabel) {
                historyMemoryProposalStatusLabel->setText(QStringLiteral("范围只能是 global 或 project:项目标识。"));
            }
            return;
        }
        const QStringList tags = tagsInput->text().split(
            QRegularExpression(QStringLiteral("[,，]")), Qt::SkipEmptyParts);
        if (historyMemoryProposalConfirmButton) {
            historyMemoryProposalConfirmButton->setEnabled(false);
        }
        if (historyMemoryProposalStatusLabel) {
            historyMemoryProposalStatusLabel->setText(QStringLiteral("正在保存已确认的长期记忆……"));
        }
        backendClient->confirmTaskMemoryProposal(
            activeHistoryMemoryProposal.taskId,
            activeHistoryMemoryProposal,
            kindCombo->currentData().toString(),
            scope,
            title,
            summary,
            tags);
    });
    historyMemoryProposalKindCombo = kindCombo;
    historyMemoryProposalScopeInput = scopeInput;
    historyMemoryProposalTitleInput = titleInput;
    historyMemoryProposalSummaryInput = summaryInput;
    historyMemoryProposalTagsInput = tagsInput;
    historyMemoryProposalStatusLabel = statusLabel;
    historyMemoryProposalConfirmButton = confirmButton;
    connect(dialog, &QObject::destroyed, this, [this]() {
        historyMemoryProposalDialog = nullptr;
        historyMemoryProposalKindCombo = nullptr;
        historyMemoryProposalScopeInput = nullptr;
        historyMemoryProposalTitleInput = nullptr;
        historyMemoryProposalSummaryInput = nullptr;
        historyMemoryProposalTagsInput = nullptr;
        historyMemoryProposalStatusLabel = nullptr;
        historyMemoryProposalConfirmButton = nullptr;
        activeHistoryMemoryProposal = TaskMemoryProposalInfo{};
    });
    dialog->open();
}

void MainWindow::handleTaskMemoryProposalsFailed(const QString &taskId, const QString &message)
{
    if (taskId != currentHistoryTaskId) {
        return;
    }
    if (historyMemoryButton) {
        historyMemoryButton->setEnabled(true);
    }
    if (historyDetailText) {
        historyDetailText->setHtml(
            QStringLiteral("<p><b>无法读取长期记忆候选</b></p><p style=\"color:#B91C1C;\">%1</p>")
                .arg(message.toHtmlEscaped()));
    }
}

void MainWindow::handleTaskMemoryProposalConfirmed(const QString &taskId, const QString &message)
{
    if (taskId != currentHistoryTaskId) {
        return;
    }
    if (historyMemoryProposalDialog) {
        historyMemoryProposalDialog->close();
    }
    if (historyMemoryButton) {
        historyMemoryButton->setEnabled(true);
    }
    if (historyDetailText) {
        historyDetailText->setHtml(
            QStringLiteral("<p><b>已记住本次约束</b></p><p style=\"color:#047857;\">%1</p>")
                .arg(message.toHtmlEscaped()));
    }
}

void MainWindow::handleTaskMemoryProposalConfirmFailed(const QString &taskId, const QString &message)
{
    if (taskId != currentHistoryTaskId) {
        return;
    }
    if (historyMemoryProposalConfirmButton) {
        historyMemoryProposalConfirmButton->setEnabled(true);
    }
    if (historyMemoryProposalStatusLabel) {
        historyMemoryProposalStatusLabel->setText(QStringLiteral("保存失败：%1").arg(message));
    }
}

void MainWindow::setupModelPage()
{
    // 模型页的结构在 mainwindow.ui 中维护；这里仅绑定后端 provider 数据和本地筛选状态。
    modelSummaryLabel = ui->modelSummaryLabel;
    modelCurrentProviderBadge = ui->modelCurrentProviderBadge;
    modelCurrentTransportBadge = ui->modelCurrentTransportBadge;
    modelCurrentModelBadge = ui->modelCurrentModelBadge;
    modelCurrentKeyBadge = ui->modelCurrentKeyBadge;
    modelCountLabel = ui->modelCountLabel;
    modelDetailBadge = ui->modelDetailBadge;
    modelHintLabel = ui->modelHintLabel;
    modelConfigStatusBadge = ui->modelConfigStatusBadge;
    modelConfigProviderLabel = ui->modelConfigProviderLabel;
    modelConfigStatusLabel = ui->modelConfigStatusLabel;
    modelSearchInput = ui->modelSearchInput;
    modelConfigBaseUrlInput = ui->modelConfigBaseUrlInput;
    modelConfigModelInput = ui->modelConfigModelInput;
    modelConfigApiKeyInput = ui->modelConfigApiKeyInput;
    modelConfigThinkingCombo = ui->modelConfigThinkingCombo;
    modelRefreshButton = ui->modelRefreshButton;
    modelRoutesButton = ui->modelRoutesButton;
    modelTestConfigButton = ui->modelTestConfigButton;
    modelSaveConfigButton = ui->modelSaveConfigButton;
    modelClearKeyButton = ui->modelClearKeyButton;
    modelProviderTable = ui->modelProviderTable;
    modelDetailText = ui->modelDetailText;

    modelRefreshButton->setIcon(style()->standardIcon(QStyle::SP_BrowserReload));
    modelRefreshButton->setToolTip(QStringLiteral("重新读取后端模型供应商清单，不会触发真实模型调用。"));
    modelRefreshButton->setEnabled(false);
    connect(modelRefreshButton, &QPushButton::clicked, this, &MainWindow::refreshModelProviders);

    modelRoutesButton->setIcon(style()->standardIcon(QStyle::SP_FileDialogDetailedView));
    modelRoutesButton->setToolTip(QStringLiteral("为总指挥和已接入专业任务选择显式模型，不会复制 API Key。"));
    modelRoutesButton->setEnabled(false);
    connect(modelRoutesButton, &QPushButton::clicked, this, &MainWindow::openModelRouteDialog);

    modelSearchInput->setClearButtonEnabled(true);
    connect(modelSearchInput, &QLineEdit::textChanged, this, &MainWindow::applyModelKeywordFilter);

    modelConfigBaseUrlInput->setClearButtonEnabled(true);
    modelConfigModelInput->setClearButtonEnabled(true);
    modelConfigApiKeyInput->setClearButtonEnabled(true);
    connect(modelConfigBaseUrlInput, &QLineEdit::textChanged, this, &MainWindow::updateModelConfigButtons);
    connect(modelConfigModelInput, &QLineEdit::textChanged, this, &MainWindow::updateModelConfigButtons);
    modelConfigThinkingCombo->addItem(QStringLiteral("关闭"), QStringLiteral("disabled"));
    modelConfigThinkingCombo->addItem(QStringLiteral("开启"), QStringLiteral("enabled"));

    modelTestConfigButton->setIcon(style()->standardIcon(QStyle::SP_DialogApplyButton));
    modelTestConfigButton->setToolTip(QStringLiteral("用当前表单内容发起一次小请求；不会保存配置。"));
    connect(modelTestConfigButton, &QPushButton::clicked, this, &MainWindow::testSelectedModelConnection);

    modelSaveConfigButton->setIcon(style()->standardIcon(QStyle::SP_DialogSaveButton));
    modelSaveConfigButton->setToolTip(QStringLiteral("保存当前全局模型配置。Key 只会发送给本地后端安全存储。"));
    connect(modelSaveConfigButton, &QPushButton::clicked, this, [this]() {
        saveSelectedModelConfig(false);
    });

    modelClearKeyButton->setIcon(style()->standardIcon(QStyle::SP_DialogDiscardButton));
    modelClearKeyButton->setToolTip(QStringLiteral("清空当前 provider 已保存的本地 API Key。"));
    connect(modelClearKeyButton, &QPushButton::clicked, this, [this]() {
        if (QMessageBox::question(
                this,
                QStringLiteral("清空 API Key"),
                QStringLiteral("确定要清空当前 provider 的本地 API Key 吗？")) != QMessageBox::Yes) {
            return;
        }
        saveSelectedModelConfig(true);
    });

    modelProviderTable->setColumnCount(5);
    modelProviderTable->setHorizontalHeaderLabels({
        QStringLiteral("Provider"),
        QStringLiteral("名称"),
        QStringLiteral("传输"),
        QStringLiteral("默认模型"),
        QStringLiteral("状态")
    });
    modelProviderTable->setSelectionBehavior(QAbstractItemView::SelectRows);
    modelProviderTable->setSelectionMode(QAbstractItemView::SingleSelection);
    modelProviderTable->setEditTriggers(QAbstractItemView::NoEditTriggers);
    modelProviderTable->setAlternatingRowColors(true);
    modelProviderTable->setSortingEnabled(false);
    modelProviderTable->setWordWrap(false);
    modelProviderTable->setTextElideMode(Qt::ElideRight);
    modelProviderTable->horizontalHeader()->setStretchLastSection(false);
    modelProviderTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::ResizeToContents);
    modelProviderTable->horizontalHeader()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
    modelProviderTable->horizontalHeader()->setSectionResizeMode(2, QHeaderView::ResizeToContents);
    modelProviderTable->horizontalHeader()->setSectionResizeMode(3, QHeaderView::Stretch);
    modelProviderTable->horizontalHeader()->setSectionResizeMode(4, QHeaderView::ResizeToContents);
    modelProviderTable->verticalHeader()->setVisible(false);
    modelProviderTable->verticalHeader()->setDefaultSectionSize(38);
    connect(modelProviderTable, &QTableWidget::itemSelectionChanged, this, &MainWindow::onModelProviderSelectionChanged);

    ui->modelSplitter->setChildrenCollapsible(false);
    ui->modelSplitter->setOpaqueResize(true);
    ui->modelSplitter->setStretchFactor(0, 3);
    ui->modelSplitter->setStretchFactor(1, 2);

    modelDetailText->setReadOnly(true);
    // 右侧详情只是 profile 摘要，配置表单才是主要操作区；限制高度避免两块内容互相挤压。
    modelDetailText->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
    modelDetailText->setMaximumHeight(124);
    showModelEmptyState(QStringLiteral("等待后端就绪后读取模型供应商。"));
}

void MainWindow::refreshModelProviders()
{
    if (!modelProviderTable || !modelDetailText) {
        return;
    }

    if (!backendManager->isReady()) {
        modelProvidersLoading = false;
        if (currentModelProviders.isEmpty()) {
            showModelEmptyState(QStringLiteral("后端尚未就绪，暂时不能读取模型供应商。"));
        }
        if (modelRefreshButton) {
            modelRefreshButton->setEnabled(false);
        }
        return;
    }

    if (modelProvidersLoading) {
        return;
    }

    modelProvidersLoading = true;
    modelRefreshButton->setEnabled(false);
    modelRefreshButton->setText(QStringLiteral("刷新中"));
    modelSummaryLabel->setText(QStringLiteral("正在从后端读取模型供应商清单和当前运行时状态。"));
    if (currentModelProviders.isEmpty()) {
        modelDetailText->setHtml(QStringLiteral("<p style=\"color:#64748B;\">正在加载模型供应商……</p>"));
    }

    // 只读取 profile 元数据和当前配置状态；这个请求不会消耗模型额度。
    backendClient->requestModelProviders();
}

void MainWindow::handleModelProvidersReceived(const ModelProviderListResult &result)
{
    modelProvidersLoading = false;
    modelRefreshButton->setEnabled(backendManager->isReady());
    modelRefreshButton->setText(QStringLiteral("刷新"));

    currentModelStatus = result.current;
    currentModelProviders = result.providers;
    if (modelRoutesButton) {
        modelRoutesButton->setEnabled(backendManager->isReady());
    }
    if (modelRouteDialog) {
        modelRouteDialog->setModelProviders(currentModelProviders);
    }

    modelProviderTable->blockSignals(true);
    modelProviderTable->setRowCount(currentModelProviders.size());

    for (int row = 0; row < currentModelProviders.size(); ++row) {
        const ModelProviderInfo &provider = currentModelProviders.at(row);
        const bool isCurrent = provider.provider == currentModelStatus.provider;
        const bool hasRuntimeError = isCurrent && !currentModelStatus.configurationError.isEmpty();

        auto *providerItem = new QTableWidgetItem(provider.provider);
        providerItem->setData(Qt::UserRole, provider.provider);
        providerItem->setToolTip(provider.provider);

        auto *labelItem = new QTableWidgetItem(provider.label);
        labelItem->setToolTip(provider.notes);

        auto *transportItem = new QTableWidgetItem(modelTransportText(provider.transport));
        transportItem->setData(Qt::UserRole, provider.transport);
        transportItem->setToolTip(provider.transport);

        auto *modelItem = new QTableWidgetItem(provider.defaultModel.isEmpty()
                                                   ? QStringLiteral("未设置")
                                                   : provider.defaultModel);
        modelItem->setToolTip(provider.defaultModel);

        QString stateText = QStringLiteral("已支持");
        if (hasRuntimeError) {
            stateText = QStringLiteral("配置异常");
        } else if (isCurrent && !currentModelStatus.apiKeyConfigured) {
            stateText = QStringLiteral("待配置 Key");
        } else if (isCurrent) {
            stateText = QStringLiteral("当前运行时");
        } else if (provider.apiKeyConfigured) {
            stateText = QStringLiteral("Key 已保存");
        }

        auto *stateItem = new QTableWidgetItem(stateText);
        stateItem->setTextAlignment(Qt::AlignCenter);

        const QList<QTableWidgetItem *> rowItems = {
            providerItem,
            labelItem,
            transportItem,
            modelItem,
            stateItem
        };

        for (QTableWidgetItem *item : rowItems) {
            if (hasRuntimeError) {
                item->setBackground(QBrush(QColor("#FEF2F2")));
                item->setForeground(QBrush(QColor("#DC2626")));
            } else if (isCurrent) {
                item->setBackground(QBrush(QColor("#EAF2FF")));
                item->setForeground(QBrush(QColor("#2563EB")));
            } else {
                item->setForeground(QBrush(QColor("#334155")));
            }
        }

        modelProviderTable->setItem(row, 0, providerItem);
        modelProviderTable->setItem(row, 1, labelItem);
        modelProviderTable->setItem(row, 2, transportItem);
        modelProviderTable->setItem(row, 3, modelItem);
        modelProviderTable->setItem(row, 4, stateItem);
    }

    modelProviderTable->blockSignals(false);
    updateModelSummaryPanel();
    applyModelKeywordFilter();
    if (!selectModelProviderRowById(currentModelStatus.provider)) {
        for (int row = 0; row < modelProviderTable->rowCount(); ++row) {
            if (!modelProviderTable->isRowHidden(row)) {
                modelProviderTable->selectRow(row);
                break;
            }
        }
    }
    updateModelDetailPanel();
}

void MainWindow::openModelRouteDialog()
{
    openModelRouteDialogForRoute(QString());
}

void MainWindow::openModelRouteDialogForRoute(const QString &routeId)
{
    if (!modelRouteDialog) {
        modelRouteDialog = new ModelRouteDialog(this);
        connect(modelRouteDialog, &ModelRouteDialog::refreshRequested, this, [this]() {
            if (backendManager->isReady()) {
                backendClient->requestModelRoutes();
            }
        });
        connect(modelRouteDialog, &ModelRouteDialog::saveRequested, this,
                [this](const QString &routeId,
                       const QString &mode,
                       const QString &provider,
                       const QString &baseUrl,
                       const QString &model,
                       const QString &thinking) {
                    backendClient->saveModelRoute(routeId, mode, provider, baseUrl, model, thinking);
                });
    }

    modelRouteDialog->setModelProviders(currentModelProviders);
    // 使用方只声明稳定 route_id；对话框负责在异步列表返回后选中对应行，避免把路由
    // 编辑逻辑散落到每个 Agent 页面。空值保留模型页“查看全部作用域”的原有入口。
    modelRouteDialog->selectRoute(routeId);
    modelRouteDialog->show();
    modelRouteDialog->raise();
    modelRouteDialog->activateWindow();
    if (!backendManager->isReady()) {
        modelRouteDialog->showRequestError(QStringLiteral("后端尚未就绪，暂时不能读取任务模型路由。"));
        return;
    }
    modelRouteDialog->setLoading(true, QStringLiteral("正在读取任务模型路由…"));
    backendClient->requestModelRoutes();
}

void MainWindow::handleModelRoutesReceived(const ModelRouteListResult &result)
{
    if (modelRouteDialog) {
        modelRouteDialog->setRoutes(result);
    }
    currentModelRoutesById.clear();
    for (const ModelRouteInfo &route : result.routes) {
        currentModelRoutesById.insert(route.routeId, route);
        if (route.routeId == QStringLiteral("commander_planning")) {
            updateDispatchModelRoutePresentation(route);
        }
    }
    updateSpecialistModelRoutePresentations();
}

void MainWindow::handleModelRoutesFailed(const QString &message)
{
    if (modelRouteDialog) {
        modelRouteDialog->showRequestError(message);
    }
}

void MainWindow::handleModelRouteSaved(const ModelRouteInfo &route)
{
    if (modelRouteDialog) {
        modelRouteDialog->applySavedRoute(route);
    }
    currentModelRoutesById.insert(route.routeId, route);
    if (route.routeId == QStringLiteral("commander_planning")) {
        updateDispatchModelRoutePresentation(route);
    }
    updateSpecialistModelRoutePresentations();
}

void MainWindow::updateDispatchModelRoutePresentation(const ModelRouteInfo &route)
{
    if (!ui->dispatchModelRouteButton) {
        return;
    }

    const bool ready = route.availability == QStringLiteral("ready") && route.hasResolved
        && !route.resolvedModel.trimmed().isEmpty();
    if (!ready) {
        ui->dispatchModelRouteButton->setText(QStringLiteral("模型"));
        ui->dispatchModelRouteButton->setToolTip(
            QStringLiteral("总指挥模型当前不可用：%1。点击检查或修改任务模型路由。")
                .arg(route.availabilityMessage.left(120)));
        return;
    }

    const QString provider = !route.resolvedLabel.trimmed().isEmpty()
        ? route.resolvedLabel.trimmed()
        : route.resolvedProvider.trimmed();
    // 输入栏只显示短 Provider 名，把完整 model/thinking 放入 Tooltip。这样客户能一眼确认
    // “不是某个隐式默认”，又不会让一条长模型 ID 挤压自己的自然语言输入。
    ui->dispatchModelRouteButton->setText(provider.left(10));
    const QString thinking = route.resolvedThinking == QStringLiteral("enabled")
        ? QStringLiteral("开启")
        : QStringLiteral("关闭");
    ui->dispatchModelRouteButton->setToolTip(
        QStringLiteral("总指挥本次模型：%1 · %2 · 思考%3。点击调整后续新任务的路由。")
            .arg(provider, route.resolvedModel, thinking));
}

QString MainWindow::modelRoutePresentationText(const QString &routeId) const
{
    const auto iterator = currentModelRoutesById.constFind(routeId);
    if (iterator == currentModelRoutesById.constEnd()) {
        return QStringLiteral("尚未读取；点击后会刷新路由状态");
    }

    const ModelRouteInfo &route = iterator.value();
    if (route.availability != QStringLiteral("ready") || !route.hasResolved
        || route.resolvedModel.trimmed().isEmpty()) {
        const QString reason = route.availabilityMessage.trimmed();
        return reason.isEmpty() ? QStringLiteral("当前不可用，请检查模型配置") : reason.left(160);
    }

    const QString provider = !route.resolvedLabel.trimmed().isEmpty()
        ? route.resolvedLabel.trimmed()
        : route.resolvedProvider.trimmed();
    const QString thinking = route.resolvedThinking == QStringLiteral("enabled")
        ? QStringLiteral("思考开启")
        : QStringLiteral("思考关闭");
    return QStringLiteral("%1 · %2 · %3").arg(provider, route.resolvedModel, thinking);
}

void MainWindow::updateSpecialistModelRoutePresentations()
{
    // 这些是纯展示提示：每个动作仍在后端按 route_id 解析 Profile 并做能力校验，不能因为
    // Qt 已显示一个 Provider 名称就认为任务可执行。
    if (ui->documentModelRouteButton) {
        ui->documentModelRouteButton->setText(QStringLiteral("模型"));
        ui->documentModelRouteButton->setToolTip(
            QStringLiteral("文档分析：%1\nPPT 制作与文档审查：%2\n点击选择需要调整的功能范围。")
                .arg(modelRoutePresentationText(QStringLiteral("document_analysis")),
                     modelRoutePresentationText(QStringLiteral("document_presentation"))));
    }
    if (ui->dataModelRouteButton) {
        const auto iterator = currentModelRoutesById.constFind(QStringLiteral("data_insight"));
        const bool ready = iterator != currentModelRoutesById.constEnd()
            && iterator->availability == QStringLiteral("ready") && iterator->hasResolved
            && !iterator->resolvedModel.trimmed().isEmpty();
        const QString provider = ready
            ? (!iterator->resolvedLabel.trimmed().isEmpty() ? iterator->resolvedLabel
                                                              : iterator->resolvedProvider)
            : QString();
        ui->dataModelRouteButton->setText(ready ? provider.left(10) : QStringLiteral("模型"));
        ui->dataModelRouteButton->setToolTip(
            QStringLiteral("数据洞察：%1\n点击调整后续数据结论任务的模型路由。")
                .arg(modelRoutePresentationText(QStringLiteral("data_insight"))));
    }
    if (ui->knowledgeModelRouteButton) {
        ui->knowledgeModelRouteButton->setText(QStringLiteral("模型"));
        ui->knowledgeModelRouteButton->setToolTip(
            QStringLiteral("知识库问答：%1\n知识库深度分析：%2\n点击选择需要调整的功能范围。")
                .arg(modelRoutePresentationText(QStringLiteral("knowledge_answer")),
                     modelRoutePresentationText(QStringLiteral("knowledge_deep_analysis"))));
    }
}

void MainWindow::handleModelRouteSaveFailed(const QString &message)
{
    if (modelRouteDialog) {
        modelRouteDialog->showRequestError(message);
    }
}

void MainWindow::handleModelProvidersFailed(const QString &message)
{
    modelProvidersLoading = false;
    if (modelRefreshButton) {
        modelRefreshButton->setEnabled(backendManager->isReady());
        modelRefreshButton->setText(QStringLiteral("刷新"));
    }

    if (currentModelProviders.isEmpty()) {
        showModelEmptyState(QStringLiteral("模型供应商加载失败：%1").arg(message));
        return;
    }

    modelSummaryLabel->setText(QStringLiteral("模型供应商刷新失败，当前继续显示上次缓存：%1").arg(message));
}

void MainWindow::applyModelKeywordFilter()
{
    if (!modelProviderTable) {
        return;
    }

    const QString keyword = modelSearchInput ? modelSearchInput->text().trimmed() : QString();
    int visibleCount = 0;

    for (int row = 0; row < modelProviderTable->rowCount(); ++row) {
        QStringList fields;
        for (int column = 0; column < modelProviderTable->columnCount(); ++column) {
            QTableWidgetItem *item = modelProviderTable->item(row, column);
            if (item) {
                fields.append(item->text());
                fields.append(item->toolTip());
            }
        }
        if (row < currentModelProviders.size()) {
            const ModelProviderInfo &provider = currentModelProviders.at(row);
            fields.append(provider.defaultBaseUrl);
            fields.append(provider.notes);
        }

        const bool visible = keyword.isEmpty()
                             || fields.join(QStringLiteral(" ")).contains(keyword, Qt::CaseInsensitive);
        modelProviderTable->setRowHidden(row, !visible);
        if (visible) {
            ++visibleCount;
        }
    }

    if (modelCountLabel) {
        modelCountLabel->setText(QStringLiteral("共 %1 项 · 当前显示 %2 项")
                                     .arg(currentModelProviders.size())
                                     .arg(visibleCount));
    }
    if (modelHintLabel) {
        modelHintLabel->setText(keyword.isEmpty()
                                    ? QStringLiteral("当前页面只读展示 provider profile，不会读取或显示 API Key 明文。")
                                    : QStringLiteral("正在按“%1”筛选当前 provider 列表。").arg(keyword));
    }

    if (visibleCount == 0) {
        modelProviderTable->clearSelection();
        if (modelDetailBadge) {
            polishBadge(modelDetailBadge, QStringLiteral("badgeGray"));
            modelDetailBadge->setText(QStringLiteral("无匹配"));
        }
        if (modelDetailText) {
            modelDetailText->setHtml(QStringLiteral("<p style=\"color:#64748B;\">没有匹配的模型供应商。</p>"));
        }
        return;
    }

    const int currentRow = modelProviderTable->currentRow();
    if (currentRow < 0 || modelProviderTable->isRowHidden(currentRow)) {
        for (int row = 0; row < modelProviderTable->rowCount(); ++row) {
            if (!modelProviderTable->isRowHidden(row)) {
                modelProviderTable->selectRow(row);
                break;
            }
        }
    } else {
        updateModelDetailPanel();
    }
}

void MainWindow::onModelProviderSelectionChanged()
{
    updateModelDetailPanel();
}

bool MainWindow::selectModelProviderRowById(const QString &providerId)
{
    if (!modelProviderTable || providerId.isEmpty()) {
        return false;
    }

    for (int row = 0; row < currentModelProviders.size(); ++row) {
        if (currentModelProviders.at(row).provider == providerId && !modelProviderTable->isRowHidden(row)) {
            modelProviderTable->selectRow(row);
            return true;
        }
    }

    return false;
}

void MainWindow::updateModelSummaryPanel()
{
    if (!modelSummaryLabel) {
        return;
    }

    const bool hasError = !currentModelStatus.configurationError.isEmpty();
    const QString providerText = currentModelStatus.label.isEmpty()
                                     ? (currentModelStatus.provider.isEmpty()
                                            ? QStringLiteral("未解析")
                                            : currentModelStatus.provider)
                                     : currentModelStatus.label;
    const QString transportText = modelTransportText(currentModelStatus.transport);
    const QString modelText = currentModelStatus.model.isEmpty()
                                  ? QStringLiteral("模型未设置")
                                  : currentModelStatus.model;

    if (hasError) {
        modelSummaryLabel->setText(QStringLiteral("模型运行时解析失败：%1。此页仍可查看支持的 provider profile。")
                                       .arg(currentModelStatus.configurationError));
    } else if (currentModelStatus.provider.isEmpty()) {
        modelSummaryLabel->setText(QStringLiteral("后端尚未解析出当前模型运行时，可选择一个 provider 后保存本地配置。"));
    } else {
        modelSummaryLabel->setText(
            QStringLiteral("当前运行时：%1 · %2 · %3。可在右侧保存配置，页面不会回显 API Key。")
                .arg(providerText, modelText, transportText));
    }

    polishBadge(modelCurrentProviderBadge,
                hasError ? QStringLiteral("badgeOrange")
                         : modelProviderBadgeObjectName(currentModelStatus.provider));
    modelCurrentProviderBadge->setText(compactBadgeText(providerText, 20));
    modelCurrentProviderBadge->setToolTip(providerText);

    polishBadge(modelCurrentTransportBadge,
                currentModelStatus.transport.isEmpty() ? QStringLiteral("badgeGray") : QStringLiteral("badgeBlue"));
    modelCurrentTransportBadge->setText(transportText);
    modelCurrentTransportBadge->setToolTip(currentModelStatus.transport);

    polishBadge(modelCurrentModelBadge,
                currentModelStatus.model.isEmpty() ? QStringLiteral("badgeGray") : QStringLiteral("badgePurple"));
    modelCurrentModelBadge->setText(compactBadgeText(modelText, 28));
    modelCurrentModelBadge->setToolTip(modelText);

    polishBadge(modelCurrentKeyBadge,
                hasError ? QStringLiteral("badgeOrange")
                         : (currentModelStatus.apiKeyConfigured ? QStringLiteral("badgeGreen")
                                                                : QStringLiteral("badgeOrange")));
    modelCurrentKeyBadge->setText(hasError
                                      ? QStringLiteral("配置异常")
                                      : (currentModelStatus.apiKeyConfigured ? QStringLiteral("Key 已配置")
                                                                            : QStringLiteral("Key 未配置")));
}

void MainWindow::updateModelDetailPanel()
{
    if (!modelProviderTable || !modelDetailText || currentModelProviders.isEmpty()) {
        return;
    }

    const int row = modelProviderTable->currentRow();
    if (row < 0 || row >= currentModelProviders.size() || modelProviderTable->isRowHidden(row)) {
        if (modelDetailBadge) {
            polishBadge(modelDetailBadge, QStringLiteral("badgeGray"));
            modelDetailBadge->setText(QStringLiteral("未选择"));
        }
        modelDetailText->setHtml(QStringLiteral("<p style=\"color:#64748B;\">选中一个 provider 查看能力和运行时映射。</p>"));
        updateModelConfigForm();
        return;
    }

    const ModelProviderInfo &provider = currentModelProviders.at(row);
    const bool isCurrent = provider.provider == currentModelStatus.provider;
    if (modelDetailBadge) {
        polishBadge(modelDetailBadge, isCurrent ? modelProviderBadgeObjectName(provider.provider)
                                                : QStringLiteral("badgeGray"));
        modelDetailBadge->setText(isCurrent ? QStringLiteral("当前") : QStringLiteral("profile"));
        modelDetailBadge->setToolTip(provider.provider);
    }

    modelDetailText->setHtml(formatModelProviderDetailHtml(provider));
    updateModelConfigForm();
}

void MainWindow::updateModelConfigForm()
{
    if (!modelProviderTable || currentModelProviders.isEmpty()) {
        updateModelConfigButtons();
        return;
    }

    const int row = modelProviderTable->currentRow();
    if (row < 0 || row >= currentModelProviders.size() || modelProviderTable->isRowHidden(row)) {
        if (modelConfigProviderLabel) {
            modelConfigProviderLabel->setText(QStringLiteral("选择左侧供应商后可编辑全局默认模型。"));
        }
        if (modelConfigBaseUrlInput) {
            modelConfigBaseUrlInput->clear();
        }
        if (modelConfigModelInput) {
            modelConfigModelInput->clear();
        }
        if (modelConfigApiKeyInput) {
            modelConfigApiKeyInput->clear();
        }
        if (modelConfigStatusBadge) {
            polishBadge(modelConfigStatusBadge, QStringLiteral("badgeGray"));
            modelConfigStatusBadge->setText(QStringLiteral("未选择"));
        }
        if (modelConfigStatusLabel) {
            modelConfigStatusLabel->setText(QStringLiteral("请选择一个 provider。"));
        }
        updateModelConfigButtons();
        return;
    }

    const ModelProviderInfo &provider = currentModelProviders.at(row);
    const bool isCurrent = provider.provider == currentModelStatus.provider;
    const bool hasError = isCurrent && !currentModelStatus.configurationError.isEmpty();
    const QString baseUrl = isCurrent && !currentModelStatus.baseUrl.isEmpty()
                                ? currentModelStatus.baseUrl
                                : provider.defaultBaseUrl;
    const QString model = isCurrent && !currentModelStatus.model.isEmpty()
                              ? currentModelStatus.model
                              : provider.defaultModel;

    modelConfigBaseUrlInput->blockSignals(true);
    modelConfigModelInput->blockSignals(true);
    modelConfigApiKeyInput->blockSignals(true);
    modelConfigBaseUrlInput->setText(baseUrl);
    modelConfigModelInput->setText(model);
    modelConfigApiKeyInput->clear();
    modelConfigBaseUrlInput->blockSignals(false);
    modelConfigModelInput->blockSignals(false);
    modelConfigApiKeyInput->blockSignals(false);

    const QString providerText = provider.label.isEmpty()
                                     ? provider.provider
                                     : QStringLiteral("%1 (%2)").arg(provider.label, provider.provider);
    if (modelConfigProviderLabel) {
        modelConfigProviderLabel->setText(QStringLiteral("选中：%1 · %2")
                                              .arg(providerText, modelTransportText(provider.transport)));
    }

    if (modelConfigThinkingCombo) {
        modelConfigThinkingCombo->setEnabled(provider.supportsThinking);
        const QString thinking = isCurrent && !currentModelStatus.thinking.isEmpty()
                                     ? currentModelStatus.thinking
                                     : QStringLiteral("disabled");
        const int thinkingIndex = modelConfigThinkingCombo->findData(thinking);
        modelConfigThinkingCombo->setCurrentIndex(thinkingIndex >= 0 ? thinkingIndex : 0);
        modelConfigThinkingCombo->setToolTip(provider.supportsThinking
                                                 ? QStringLiteral("该 provider 支持 thinking 参数。")
                                                 : QStringLiteral("该 provider 当前不支持 thinking 参数。"));
    }

    if (modelConfigStatusBadge) {
        if (hasError) {
            polishBadge(modelConfigStatusBadge, QStringLiteral("badgeOrange"));
            modelConfigStatusBadge->setText(QStringLiteral("异常"));
        } else if (isCurrent && currentModelStatus.apiKeyConfigured) {
            polishBadge(modelConfigStatusBadge, QStringLiteral("badgeGreen"));
            modelConfigStatusBadge->setText(QStringLiteral("已生效"));
        } else if (isCurrent) {
            polishBadge(modelConfigStatusBadge, QStringLiteral("badgeOrange"));
            modelConfigStatusBadge->setText(QStringLiteral("缺 Key"));
        } else if (provider.apiKeyConfigured) {
            polishBadge(modelConfigStatusBadge, QStringLiteral("badgeGreen"));
            modelConfigStatusBadge->setText(QStringLiteral("Key 已保存"));
        } else {
            polishBadge(modelConfigStatusBadge, QStringLiteral("badgeGray"));
            modelConfigStatusBadge->setText(QStringLiteral("待保存"));
        }
    }

    if (modelConfigStatusLabel) {
        if (hasError) {
            modelConfigStatusLabel->setText(QStringLiteral("当前配置异常：%1").arg(currentModelStatus.configurationError));
        } else if (isCurrent && currentModelStatus.apiKeyConfigured) {
            const QString keySource = currentModelStatus.apiKeySource == QStringLiteral("local_config")
                                          ? QStringLiteral("本地安全存储")
                                          : QStringLiteral("环境变量");
            modelConfigStatusLabel->setText(QStringLiteral("当前 provider 已配置 Key，来源：%1。输入新 Key 后保存会替换本地保存值。")
                                                .arg(keySource));
        } else if (isCurrent) {
            modelConfigStatusLabel->setText(QStringLiteral("当前 provider 尚未配置 Key；保存时填写 Key 才能用于真实模型调用。"));
        } else if (provider.apiKeyConfigured) {
            modelConfigStatusLabel->setText(QStringLiteral("该 provider 的 Key 已安全保存但尚未启用；保存配置会将它设为全局默认，Key 留空会继续使用此 provider 已保存的 Key。"));
        } else {
            modelConfigStatusLabel->setText(QStringLiteral("保存后会把该 provider 设为全局默认；Key 留空不会使用其他 provider 的 Key。"));
        }
    }

    updateModelConfigButtons();
}

void MainWindow::updateModelConfigButtons()
{
    const bool hasSelection = modelProviderTable
                              && modelProviderTable->currentRow() >= 0
                              && modelProviderTable->currentRow() < currentModelProviders.size()
                              && !modelProviderTable->isRowHidden(modelProviderTable->currentRow());
    const bool formReady = hasSelection
                           && modelConfigBaseUrlInput
                           && modelConfigModelInput
                           && !modelConfigBaseUrlInput->text().trimmed().isEmpty()
                           && !modelConfigModelInput->text().trimmed().isEmpty();
    const bool canUseFields = backendManager->isReady()
                              && hasSelection
                              && !modelProvidersLoading
                              && !modelConfigSaving
                              && !modelConnectionTesting;
    const bool canEdit = canUseFields && formReady;

    if (modelProviderTable) {
        modelProviderTable->setEnabled(!modelProvidersLoading && !modelConfigSaving && !modelConnectionTesting);
    }
    if (modelSearchInput) {
        modelSearchInput->setEnabled(!modelProvidersLoading && !modelConnectionTesting);
    }
    if (modelConfigBaseUrlInput) {
        modelConfigBaseUrlInput->setEnabled(canUseFields);
    }
    if (modelConfigModelInput) {
        modelConfigModelInput->setEnabled(canUseFields);
    }
    if (modelConfigApiKeyInput) {
        modelConfigApiKeyInput->setEnabled(canUseFields);
    }
    if (modelConfigThinkingCombo) {
        bool supportsThinking = false;
        if (hasSelection) {
            supportsThinking = currentModelProviders.at(modelProviderTable->currentRow()).supportsThinking;
        }
        modelConfigThinkingCombo->setEnabled(canUseFields && supportsThinking);
    }
    if (modelRefreshButton) {
        modelRefreshButton->setEnabled(backendManager->isReady() && !modelProvidersLoading && !modelConfigSaving && !modelConnectionTesting);
    }
    if (modelRoutesButton) {
        modelRoutesButton->setEnabled(backendManager->isReady() && !modelConfigSaving);
    }

    if (modelSaveConfigButton) {
        modelSaveConfigButton->setEnabled(canEdit);
        modelSaveConfigButton->setText(modelConfigSaving ? QStringLiteral("保存中") : QStringLiteral("保存配置"));
    }

    if (modelTestConfigButton) {
        modelTestConfigButton->setEnabled(canEdit);
        modelTestConfigButton->setText(modelConnectionTesting ? QStringLiteral("测试中") : QStringLiteral("测试连接"));
    }

    if (modelClearKeyButton) {
        bool canClear = false;
        if (hasSelection) {
            const ModelProviderInfo &provider = currentModelProviders.at(modelProviderTable->currentRow());
            canClear = provider.provider == currentModelStatus.provider && currentModelStatus.apiKeyConfigured;
        }
        modelClearKeyButton->setEnabled(canEdit && canClear);
    }
}

void MainWindow::saveSelectedModelConfig(bool clearKey)
{
    if (modelConnectionTesting) {
        return;
    }

    if (!modelProviderTable || modelProviderTable->currentRow() < 0
        || modelProviderTable->currentRow() >= currentModelProviders.size()) {
        return;
    }

    const ModelProviderInfo &provider = currentModelProviders.at(modelProviderTable->currentRow());
    const QString baseUrl = modelConfigBaseUrlInput->text().trimmed();
    const QString model = modelConfigModelInput->text().trimmed();
    if (baseUrl.isEmpty() || model.isEmpty()) {
        if (modelConfigStatusLabel) {
            modelConfigStatusLabel->setText(QStringLiteral("Base URL 和模型名称不能为空。"));
        }
        return;
    }

    QString thinking = modelConfigThinkingCombo ? modelConfigThinkingCombo->currentData().toString()
                                                : QString();
    if (thinking.isEmpty()) {
        thinking = QStringLiteral("disabled");
    }
    const QString apiKey = clearKey || !modelConfigApiKeyInput
                               ? QString()
                               : modelConfigApiKeyInput->text().trimmed();

    modelConfigSaving = true;
    if (modelConfigStatusBadge) {
        polishBadge(modelConfigStatusBadge, QStringLiteral("badgeBlue"));
        modelConfigStatusBadge->setText(clearKey ? QStringLiteral("清空中") : QStringLiteral("保存中"));
    }
    if (modelConfigStatusLabel) {
        modelConfigStatusLabel->setText(clearKey
                                            ? QStringLiteral("正在清空本地保存的 API Key。")
                                            : QStringLiteral("正在保存模型配置。"));
    }
    updateModelConfigButtons();

    backendClient->saveModelConfig(provider.provider, baseUrl, model, thinking, apiKey, clearKey);
}

void MainWindow::testSelectedModelConnection()
{
    if (modelConfigSaving || modelConnectionTesting) {
        return;
    }

    if (!modelProviderTable || modelProviderTable->currentRow() < 0
        || modelProviderTable->currentRow() >= currentModelProviders.size()) {
        return;
    }

    const ModelProviderInfo &provider = currentModelProviders.at(modelProviderTable->currentRow());
    const QString baseUrl = modelConfigBaseUrlInput ? modelConfigBaseUrlInput->text().trimmed() : QString();
    const QString model = modelConfigModelInput ? modelConfigModelInput->text().trimmed() : QString();
    if (baseUrl.isEmpty() || model.isEmpty()) {
        if (modelConfigStatusLabel) {
            modelConfigStatusLabel->setText(QStringLiteral("测试前请先填写 Base URL 和模型名称。"));
        }
        return;
    }

    QString thinking = modelConfigThinkingCombo ? modelConfigThinkingCombo->currentData().toString()
                                                : QString();
    if (thinking.isEmpty()) {
        thinking = QStringLiteral("disabled");
    }
    const QString apiKey = modelConfigApiKeyInput ? modelConfigApiKeyInput->text().trimmed() : QString();

    modelConnectionTesting = true;
    if (modelConfigStatusBadge) {
        polishBadge(modelConfigStatusBadge, QStringLiteral("badgeBlue"));
        modelConfigStatusBadge->setText(QStringLiteral("测试中"));
    }
    if (modelConfigStatusLabel) {
        modelConfigStatusLabel->setText(QStringLiteral("正在测试当前表单的模型连接，结果不会写入本地配置。"));
    }
    updateModelConfigButtons();

    backendClient->testModelConnection(provider.provider, baseUrl, model, thinking, apiKey);
}

void MainWindow::handleModelConfigSaved(const ModelProviderStatus &status)
{
    modelConfigSaving = false;
    currentModelStatus = status;
    if (modelConfigApiKeyInput) {
        modelConfigApiKeyInput->clear();
    }
    if (modelConfigStatusBadge) {
        polishBadge(modelConfigStatusBadge, status.apiKeyConfigured ? QStringLiteral("badgeGreen")
                                                                    : QStringLiteral("badgeOrange"));
        modelConfigStatusBadge->setText(status.apiKeyConfigured ? QStringLiteral("已保存")
                                                                : QStringLiteral("Key 空"));
    }
    if (modelConfigStatusLabel) {
        modelConfigStatusLabel->setText(QStringLiteral("配置已保存，正在刷新当前运行时状态。"));
    }
    refreshModelProviders();
}

void MainWindow::handleModelConfigSaveFailed(const QString &message)
{
    modelConfigSaving = false;
    if (modelConfigStatusBadge) {
        polishBadge(modelConfigStatusBadge, QStringLiteral("badgeOrange"));
        modelConfigStatusBadge->setText(QStringLiteral("失败"));
    }
    if (modelConfigStatusLabel) {
        modelConfigStatusLabel->setText(QStringLiteral("模型配置保存失败：%1").arg(message));
    }
    updateModelConfigButtons();
}

void MainWindow::handleModelConnectionTestCompleted(const ModelConnectionTestResult &result)
{
    modelConnectionTesting = false;
    if (modelConfigStatusBadge) {
        polishBadge(modelConfigStatusBadge, result.ok ? QStringLiteral("badgeGreen")
                                                      : QStringLiteral("badgeOrange"));
        modelConfigStatusBadge->setText(result.ok ? QStringLiteral("连接正常") : QStringLiteral("测试失败"));
    }
    if (modelConfigStatusLabel) {
        if (result.ok) {
            const QString providerText = result.label.isEmpty()
                                             ? result.provider
                                             : QStringLiteral("%1 (%2)").arg(result.label, result.provider);
            const QString preview = result.responsePreview.isEmpty()
                                        ? QStringLiteral("模型返回了有效文本。")
                                        : QStringLiteral("返回：%1").arg(result.responsePreview);
            QString keySource = QStringLiteral("未配置");
            if (result.apiKeySource == QStringLiteral("request")) {
                keySource = QStringLiteral("当前表单");
            } else if (result.apiKeySource == QStringLiteral("local_config")) {
                keySource = QStringLiteral("本地安全存储");
            } else if (result.apiKeySource == QStringLiteral("environment")) {
                keySource = QStringLiteral("环境变量");
            }
            modelConfigStatusLabel->setText(QStringLiteral("连接成功：%1 · %2ms · Key 来源：%3。%4")
                                                .arg(providerText,
                                                     QString::number(result.elapsedMs),
                                                     keySource,
                                                     preview));
        } else {
            modelConfigStatusLabel->setText(QStringLiteral("模型连接测试失败：%1").arg(result.message));
        }
    }
    updateModelConfigButtons();
}

void MainWindow::handleModelConnectionTestFailed(const QString &message)
{
    modelConnectionTesting = false;
    if (modelConfigStatusBadge) {
        polishBadge(modelConfigStatusBadge, QStringLiteral("badgeOrange"));
        modelConfigStatusBadge->setText(QStringLiteral("请求失败"));
    }
    if (modelConfigStatusLabel) {
        modelConfigStatusLabel->setText(QStringLiteral("模型连接测试请求失败：%1").arg(message));
    }
    updateModelConfigButtons();
}

void MainWindow::showModelEmptyState(const QString &message)
{
    currentModelProviders.clear();
    currentModelStatus = ModelProviderStatus{};
    modelProvidersLoading = false;
    modelConnectionTesting = false;

    if (modelProviderTable) {
        modelProviderTable->setRowCount(0);
    }
    if (modelSummaryLabel) {
        modelSummaryLabel->setText(message);
    }
    if (modelCountLabel) {
        modelCountLabel->setText(QStringLiteral("共 0 项"));
    }
    if (modelRefreshButton) {
        modelRefreshButton->setText(QStringLiteral("刷新"));
        modelRefreshButton->setEnabled(backendManager->isReady());
    }
    if (modelRoutesButton) {
        modelRoutesButton->setEnabled(backendManager->isReady());
    }
    if (modelHintLabel) {
        modelHintLabel->setText(QStringLiteral("供应商列表用于选择全局默认模型；Key 只显示配置状态，不显示明文。"));
    }

    polishBadge(modelCurrentProviderBadge, QStringLiteral("badgeGray"));
    modelCurrentProviderBadge->setText(QStringLiteral("等待加载"));
    polishBadge(modelCurrentTransportBadge, QStringLiteral("badgeGray"));
    modelCurrentTransportBadge->setText(QStringLiteral("传输未知"));
    polishBadge(modelCurrentModelBadge, QStringLiteral("badgeGray"));
    modelCurrentModelBadge->setText(QStringLiteral("模型未设置"));
    polishBadge(modelCurrentKeyBadge, QStringLiteral("badgeGray"));
    modelCurrentKeyBadge->setText(QStringLiteral("Key 未知"));

    if (modelDetailBadge) {
        polishBadge(modelDetailBadge, QStringLiteral("badgeGray"));
        modelDetailBadge->setText(QStringLiteral("未加载"));
    }
    if (modelDetailText) {
        modelDetailText->setHtml(QStringLiteral("<p style=\"color:#64748B;\">%1</p>").arg(message.toHtmlEscaped()));
    }
    if (modelConfigStatusBadge) {
        polishBadge(modelConfigStatusBadge, QStringLiteral("badgeGray"));
        modelConfigStatusBadge->setText(QStringLiteral("未加载"));
    }
    if (modelConfigProviderLabel) {
        modelConfigProviderLabel->setText(QStringLiteral("等待后端就绪后再编辑模型配置。"));
    }
    if (modelConfigBaseUrlInput) {
        modelConfigBaseUrlInput->clear();
    }
    if (modelConfigModelInput) {
        modelConfigModelInput->clear();
    }
    if (modelConfigApiKeyInput) {
        modelConfigApiKeyInput->clear();
    }
    if (modelConfigStatusLabel) {
        modelConfigStatusLabel->setText(QStringLiteral("后端未连接，暂时无法保存模型配置。"));
    }
    updateModelConfigButtons();
}

QString MainWindow::modelTransportText(const QString &transport) const
{
    if (transport == QStringLiteral("openai_compatible")) {
        return QStringLiteral("OpenAI兼容");
    }
    if (transport == QStringLiteral("anthropic")) {
        return QStringLiteral("Anthropic Messages");
    }
    return transport.isEmpty() ? QStringLiteral("未知") : transport;
}

QString MainWindow::modelProviderBadgeObjectName(const QString &providerId) const
{
    if (providerId == QStringLiteral("deepseek")) {
        return QStringLiteral("badgeBlue");
    }
    if (providerId == QStringLiteral("openai")) {
        return QStringLiteral("badgeGreen");
    }
    if (providerId == QStringLiteral("anthropic")) {
        return QStringLiteral("badgePurple");
    }
    if (providerId == QStringLiteral("qwen")) {
        return QStringLiteral("badgeOrange");
    }
    if (providerId == QStringLiteral("kimi")) {
        return QStringLiteral("badgePurple");
    }
    return QStringLiteral("badgeGray");
}

QString MainWindow::formatModelProviderDetailHtml(const ModelProviderInfo &provider) const
{
    const bool isCurrent = provider.provider == currentModelStatus.provider;
    const QString runtimeProvider = currentModelStatus.label.isEmpty()
                                        ? (currentModelStatus.provider.isEmpty()
                                               ? QStringLiteral("未解析")
                                               : currentModelStatus.provider)
                                        : currentModelStatus.label;
    const QString runtimeModel = currentModelStatus.model.isEmpty()
                                     ? QStringLiteral("未设置")
                                     : currentModelStatus.model;
    const QString runtimeBaseUrl = currentModelStatus.baseUrl.isEmpty()
                                       ? QStringLiteral("未设置")
                                       : currentModelStatus.baseUrl;
    QString keyText = currentModelStatus.apiKeyConfigured ? QStringLiteral("已配置，不显示明文")
                                                          : QStringLiteral("未配置或未解析");
    if (currentModelStatus.apiKeyConfigured) {
        if (currentModelStatus.apiKeySource == QStringLiteral("local_config")) {
            keyText += QStringLiteral("（本地安全存储）");
        } else if (currentModelStatus.apiKeySource == QStringLiteral("environment")) {
            keyText += QStringLiteral("（环境变量）");
        }
    }

    QString html = QStringLiteral(
        "<div style=\"line-height:1.35;\">"
        "<p><b>Provider Profile</b><br/>"
        "%1 · %2 · %3</p>"
        "<p><b>默认入口：</b>%4<br/>"
        "<b>默认模型：</b>%5</p>"
        "<p><b>能力：</b>思考 %6 · JSON %7 · Tool Calls %8</p>")
        .arg(provider.provider.toHtmlEscaped(),
             provider.label.toHtmlEscaped(),
             modelTransportText(provider.transport).toHtmlEscaped(),
             provider.defaultBaseUrl.toHtmlEscaped(),
             (provider.defaultModel.isEmpty() ? QStringLiteral("未设置") : provider.defaultModel).toHtmlEscaped(),
             capabilityText(provider.supportsThinking),
             capabilityText(provider.supportsJsonOutput),
             capabilityText(provider.supportsToolCalls));

    if (!provider.notes.isEmpty()) {
        html += QStringLiteral("<p style=\"color:#64748B;\"><b>说明：</b>%1</p>")
                    .arg(provider.notes.toHtmlEscaped());
    }

    if (!isCurrent) {
        html += QStringLiteral("<p><b>本地 Key：</b>%1</p>")
                    .arg(provider.apiKeyConfigured
                             ? QStringLiteral("已配置，不显示明文（本地安全存储）")
                             : QStringLiteral("未配置"));
    }

    html += QStringLiteral("<p><b>当前运行时</b></p>");
    if (isCurrent) {
        html += QStringLiteral(
            "<p>%1 · %2<br/>"
            "Base URL：%3<br/>"
            "API Key：%4</p>")
                    .arg(runtimeProvider.toHtmlEscaped(),
                         runtimeModel.toHtmlEscaped(),
                         runtimeBaseUrl.toHtmlEscaped(),
                         keyText.toHtmlEscaped());
        if (!currentModelStatus.configurationError.isEmpty()) {
            html += QStringLiteral("<p style=\"color:#DC2626;\"><b>配置异常：</b>%1</p>")
                        .arg(currentModelStatus.configurationError.toHtmlEscaped());
        } else if (!currentModelStatus.notes.isEmpty()) {
            html += QStringLiteral("<p style=\"color:#64748B;\">%1</p>")
                        .arg(currentModelStatus.notes.toHtmlEscaped());
        }
    } else {
        html += QStringLiteral(
            "<p style=\"color:#64748B;\">当前运行时是 %1；选中的 %2 只是已支持的 profile，"
            "可在下方保存为全局默认配置。</p>")
                    .arg(runtimeProvider.toHtmlEscaped(), provider.label.toHtmlEscaped());
    }

    html += QStringLiteral("</div>");
    return html;
}

void MainWindow::setupHistoryPage()
{
    // 历史页的静态结构已经放在 mainwindow.ui 中，C++ 只负责数据绑定和交互逻辑。
    historyStatusFilter = ui->historyStatusFilter;
    historyModeFilter = ui->historyModeFilter;
    historyRiskFilter = ui->historyRiskFilter;
    historyConfirmationFilter = ui->historyConfirmationFilter;
    historyRefreshButton = ui->historyRefreshButton;
    historyExecuteButton = ui->historyExecuteButton;
    historyPrevButton = ui->historyPrevButton;
    historyNextButton = ui->historyNextButton;
    historyArtifactStrip = ui->historyArtifactStrip;
    historyArtifactCombo = ui->historyArtifactCombo;
    historyArtifactPreviewButton = ui->historyArtifactPreviewButton;
    historyArtifactOpenButton = ui->historyArtifactOpenButton;
    historyArtifactCopyButton = ui->historyArtifactCopyButton;
    historyTable = ui->historyTable;
    historyDetailText = ui->historyDetailText;
    historyCountLabel = ui->historyCountLabel;
    historyPageLabel = ui->historyPageLabel;
    historySelectionTitle = ui->historySelectionTitle;
    historySelectionMeta = ui->historySelectionMeta;
    historySelectionBadge = ui->historySelectionBadge;
    historyRuntimeBadge = ui->historyRuntimeBadge;
    historyRuntimeMeta = ui->historyRuntimeMeta;
    historyConfirmationSection = ui->historyConfirmationSection;
    historyConfirmationBody = ui->historyConfirmationBody;
    historyConfirmationIcon = ui->historyConfirmationIcon;
    historyConfirmationText = ui->historyConfirmationText;
    historyConfirmationMeta = ui->historyConfirmationMeta;
    historyConfirmationBadge = ui->historyConfirmationBadge;
    historyConfirmationToggleButton = ui->historyConfirmationToggleButton;
    historyConfirmButton = ui->historyConfirmButton;
    historyPauseButton = ui->historyPauseButton;
    historyCancelButton = ui->historyCancelButton;
    historyRetryButton = ui->historyRetryButton;
    historyRestoreDocumentButton = ui->historyRestoreDocumentButton;
    historyMemoryButton = ui->historyMemoryButton;
    historyModelRoutesButton = ui->historyModelRoutesButton;

    ui->historyInput->setClearButtonEnabled(true);
    ui->historyInput->setPlaceholderText(QStringLiteral("按任务 ID、摘要或状态搜索当前页"));
    connect(ui->historyInput, &QLineEdit::textChanged, this, &MainWindow::applyHistoryKeywordFilter);

    historyRefreshButton->setIcon(style()->standardIcon(QStyle::SP_BrowserReload));
    connect(historyRefreshButton, &QPushButton::clicked, this, &MainWindow::refreshTaskHistory);
    setupHistoryAutoRefresh();
    setupHistoryArtifactToolbar();

    historyExecuteButton->setIcon(style()->standardIcon(QStyle::SP_MediaPlay));
    historyExecuteButton->setToolTip(QStringLiteral("开始执行 dry-run 或继续执行等待中的 runtime 任务。"));
    connect(historyExecuteButton, &QPushButton::clicked, this, [this]() {
        if (currentHistoryTaskId.isEmpty()) {
            return;
        }

        if (!confirmHistoryExecutionRequest()) {
            return;
        }

        const QString effectiveStatus = (currentHistoryRuntimeStateLoaded
                                         && currentHistoryRuntimeStateError.isEmpty()
                                         && !currentHistoryRuntimeState.status.isEmpty())
                                            ? currentHistoryRuntimeState.status
                                            : currentHistoryStatus;
        const QString effectiveMode = (currentHistoryRuntimeStateLoaded
                                       && currentHistoryRuntimeStateError.isEmpty()
                                       && !currentHistoryRuntimeState.mode.isEmpty())
                                          ? currentHistoryRuntimeState.mode
                                          : currentHistoryMode;
        const bool continueRuntime = effectiveMode == QStringLiteral("runtime")
            && (effectiveStatus == QStringLiteral("waiting_permission")
                || effectiveStatus == QStringLiteral("paused"));

        historyExecuteButton->setEnabled(false);
        historyPauseButton->setEnabled(false);
        historyCancelButton->setEnabled(false);
        historyRetryButton->setEnabled(false);
        if (historyRuntimeMeta) {
            historyRuntimeMeta->setText(continueRuntime
                                            ? QStringLiteral("正在请求继续执行任务 %1……").arg(currentHistoryTaskId)
                                            : QStringLiteral("正在请求开始执行任务 %1……").arg(currentHistoryTaskId));
        }
        backendClient->requestTaskExecute(currentHistoryTaskId);
    });

    historyPauseButton->setIcon(style()->standardIcon(QStyle::SP_MediaPause));
    connect(historyPauseButton, &QPushButton::clicked, this, [this]() {
        if (currentHistoryTaskId.isEmpty()) {
            return;
        }

        // 暂停不会强杀正在运行的 Tool；先锁住同一动作，随后由 Runtime 的安全检查点和自动刷新
        // 回填正式 paused 状态，避免界面抢先宣称“已经停止”。
        historyPauseButton->setEnabled(false);
        historyCancelButton->setEnabled(false);
        if (historyDetailText) {
            historyDetailText->setHtml(
                QStringLiteral("<p style=\"color:#64748B;\">正在请求在安全步骤结束后暂停任务 %1……</p>")
                    .arg(currentHistoryTaskId.toHtmlEscaped()));
        }
        backendClient->requestTaskPause(currentHistoryTaskId);
    });

    historyConfirmationText->setReadOnly(true);
    historyConfirmationSection->setVisible(false);
    historyConfirmationBody->setVisible(false);
    // 权限确认只是提醒和审计入口，不能长期占用右侧详情栏的主要空间。
    historyConfirmationSection->setSizePolicy(QSizePolicy::Preferred, QSizePolicy::Maximum);
    historyConfirmationSection->setMaximumHeight(HistoryConfirmationCollapsedHeight);
    historyConfirmationText->setMaximumHeight(HistoryConfirmationTextMaxHeight);
    if (historyConfirmationIcon) {
        historyConfirmationIcon->setPixmap(style()->standardIcon(QStyle::SP_MessageBoxWarning).pixmap(24, 24));
    }
    if (historyRuntimeMeta) {
        // 运行态条只承担“一眼扫过”的状态摘要；完整细节放到下方详情和 tooltip，避免把右侧布局撑高。
        historyRuntimeMeta->setWordWrap(false);
        historyRuntimeMeta->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);
    }
    if (historyConfirmationToggleButton) {
        historyConfirmationToggleButton->setAutoRaise(true);
        historyConfirmationToggleButton->setCheckable(true);
        historyConfirmationToggleButton->setChecked(false);
        historyConfirmationToggleButton->setArrowType(Qt::DownArrow);
        historyConfirmationToggleButton->setToolTip(QStringLiteral("展开或收起权限确认细节"));
        connect(historyConfirmationToggleButton, &QToolButton::toggled, this, [this](bool checked) {
            setHistoryConfirmationExpanded(checked);
        });
    }
    historyConfirmButton->setIcon(style()->standardIcon(QStyle::SP_DialogApplyButton));
    historyConfirmButton->setToolTip(QStringLiteral("写入后端权限确认审计；当前 dry-run 不会触发真实工具执行。"));
    connect(historyConfirmButton, &QPushButton::clicked, this, &MainWindow::markHistoryConfirmationAcknowledged);

    historyCancelButton->setIcon(style()->standardIcon(QStyle::SP_DialogCancelButton));
    historyCancelButton->setToolTip(QStringLiteral("请求取消当前选中的任务"));
    connect(historyCancelButton, &QPushButton::clicked, this, [this]() {
        if (currentHistoryTaskId.isEmpty()) {
            return;
        }

        // 控制请求发出后先禁用按钮，避免用户双击造成重复 retry 或重复取消请求。
        historyCancelButton->setEnabled(false);
        historyRetryButton->setEnabled(false);
        historyDetailText->setHtml(
            QStringLiteral("<p style=\"color:#64748B;\">正在请求取消任务 %1……</p>")
                .arg(currentHistoryTaskId.toHtmlEscaped()));
        backendClient->requestTaskCancel(currentHistoryTaskId);
    });

    historyRetryButton->setIcon(style()->standardIcon(QStyle::SP_BrowserReload));
    historyRetryButton->setToolTip(QStringLiteral("基于缓存计划重新生成 dry-run 任务"));
    connect(historyRetryButton, &QPushButton::clicked, this, [this]() {
        if (currentHistoryTaskId.isEmpty()) {
            return;
        }

        historyCancelButton->setEnabled(false);
        historyRetryButton->setEnabled(false);
        historyDetailText->setHtml(
            QStringLiteral("<p style=\"color:#64748B;\">正在基于任务 %1 生成新的 dry-run……</p>")
                .arg(currentHistoryTaskId.toHtmlEscaped()));
        backendClient->requestTaskRetry(currentHistoryTaskId);
    });

    historyRestoreDocumentButton->setIcon(style()->standardIcon(QStyle::SP_ArrowBack));
    historyRestoreDocumentButton->setToolTip(
        QStringLiteral("从选中的已完成文档草稿建立新的独立预览，不覆盖旧任务或文件"));
    connect(historyRestoreDocumentButton,
            &QPushButton::clicked,
            this,
            &MainWindow::restoreHistoryDocumentDraftPreview);

    historyMemoryButton->setIcon(style()->standardIcon(QStyle::SP_DialogSaveButton));
    connect(historyMemoryButton, &QPushButton::clicked, this, &MainWindow::requestHistoryMemoryProposal);

    if (historyModelRoutesButton) {
        historyModelRoutesButton->setIcon(style()->standardIcon(QStyle::SP_FileDialogInfoView));
        historyModelRoutesButton->setToolTip(QStringLiteral("查看本次任务实际保存的模型路由"));
        historyModelRoutesButton->setVisible(false);
        connect(historyModelRoutesButton, &QToolButton::clicked, this, &MainWindow::showHistoryModelRoutesDialog);
    }

    historyStatusFilter->clear();
    historyStatusFilter->addItem(QStringLiteral("全部状态"), QString());
    historyStatusFilter->addItem(QStringLiteral("待处理"), QStringLiteral("pending"));
    historyStatusFilter->addItem(QStringLiteral("进行中"), QStringLiteral("running"));
    historyStatusFilter->addItem(QStringLiteral("已暂停"), QStringLiteral("paused"));
    historyStatusFilter->addItem(QStringLiteral("待确认"), QStringLiteral("waiting_permission"));
    historyStatusFilter->addItem(QStringLiteral("已完成"), QStringLiteral("completed"));
    historyStatusFilter->addItem(QStringLiteral("已阻塞"), QStringLiteral("blocked"));
    historyStatusFilter->addItem(QStringLiteral("已失败"), QStringLiteral("failed"));
    historyStatusFilter->addItem(QStringLiteral("已取消"), QStringLiteral("cancelled"));
    historyStatusFilter->setMinimumContentsLength(8);

    historyModeFilter->clear();
    historyModeFilter->addItem(QStringLiteral("全部模式"), QString());
    historyModeFilter->addItem(QStringLiteral("dry-run"), QStringLiteral("dry_run"));
    historyModeFilter->addItem(QStringLiteral("runtime"), QStringLiteral("runtime"));
    historyModeFilter->setMinimumContentsLength(8);

    historyRiskFilter->clear();
    historyRiskFilter->addItem(QStringLiteral("全部风险"), QString());
    historyRiskFilter->addItem(QStringLiteral("低风险"), QStringLiteral("low"));
    historyRiskFilter->addItem(QStringLiteral("中风险"), QStringLiteral("medium"));
    historyRiskFilter->addItem(QStringLiteral("高风险"), QStringLiteral("high"));
    historyRiskFilter->setMinimumContentsLength(8);

    historyConfirmationFilter->clear();
    historyConfirmationFilter->addItem(QStringLiteral("全部确认"), QVariant(-1));
    historyConfirmationFilter->addItem(QStringLiteral("需确认"), QVariant(1));
    historyConfirmationFilter->addItem(QStringLiteral("无需确认"), QVariant(0));
    historyConfirmationFilter->setMinimumContentsLength(8);

    for (QComboBox *combo : {historyStatusFilter, historyModeFilter, historyRiskFilter, historyConfirmationFilter}) {
        combo->setEditable(false);
        combo->setSizeAdjustPolicy(QComboBox::AdjustToContents);
    }

    connect(historyStatusFilter, qOverload<int>(&QComboBox::currentIndexChanged), this, &MainWindow::onHistoryFilterChanged);
    connect(historyModeFilter, qOverload<int>(&QComboBox::currentIndexChanged), this, &MainWindow::onHistoryFilterChanged);
    connect(historyRiskFilter, qOverload<int>(&QComboBox::currentIndexChanged), this, &MainWindow::onHistoryFilterChanged);
    connect(historyConfirmationFilter, qOverload<int>(&QComboBox::currentIndexChanged), this, &MainWindow::onHistoryFilterChanged);

    // 筛选和分页都走后端接口，前端这里只做当前页的轻量搜索和展示。
    ui->historySplitter->setChildrenCollapsible(false);
    ui->historySplitter->setOpaqueResize(true);
    ui->historySplitter->setStretchFactor(0, 3);
    ui->historySplitter->setStretchFactor(1, 2);

    historyTable->setColumnCount(8);
    historyTable->setHorizontalHeaderLabels({
        QStringLiteral("状态"),
        QStringLiteral("任务 ID"),
        QStringLiteral("摘要"),
        QStringLiteral("风险"),
        QStringLiteral("确认"),
        QStringLiteral("步骤"),
        QStringLiteral("更新"),
        QStringLiteral("模式")
    });
    historyTable->setSelectionBehavior(QAbstractItemView::SelectRows);
    historyTable->setSelectionMode(QAbstractItemView::SingleSelection);
    historyTable->setEditTriggers(QAbstractItemView::NoEditTriggers);
    historyTable->setAlternatingRowColors(true);
    historyTable->setSortingEnabled(false);
    historyTable->setWordWrap(false);
    historyTable->setTextElideMode(Qt::ElideRight);
    historyTable->horizontalHeader()->setStretchLastSection(false);
    historyTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::ResizeToContents);
    historyTable->horizontalHeader()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
    historyTable->horizontalHeader()->setSectionResizeMode(2, QHeaderView::Stretch);
    historyTable->horizontalHeader()->setSectionResizeMode(3, QHeaderView::ResizeToContents);
    historyTable->horizontalHeader()->setSectionResizeMode(4, QHeaderView::ResizeToContents);
    historyTable->horizontalHeader()->setSectionResizeMode(5, QHeaderView::ResizeToContents);
    historyTable->horizontalHeader()->setSectionResizeMode(6, QHeaderView::ResizeToContents);
    historyTable->horizontalHeader()->setSectionResizeMode(7, QHeaderView::ResizeToContents);
    historyTable->verticalHeader()->setVisible(false);
    historyTable->verticalHeader()->setDefaultSectionSize(36);
    connect(historyTable, &QTableWidget::itemSelectionChanged, this, &MainWindow::onHistoryRowSelectionChanged);

    historyDetailText->setReadOnly(true);
    historyDetailText->setHtml(QStringLiteral("<p>历史任务详情会在这里显示。</p>"));

    connect(historyPrevButton, &QPushButton::clicked, this, [this]() {
        historyOffset = qMax(0, historyOffset - historyLimit);
        refreshTaskHistory();
    });

    connect(historyNextButton, &QPushButton::clicked, this, [this]() {
        historyOffset = historyOffset + historyLimit;
        refreshTaskHistory();
    });

    showHistoryEmptyState(QStringLiteral("等待后端加载历史任务。"));
    updateHistoryPagerControls();
    updateHistoryRuntimePanel();
}

void MainWindow::setupHistoryAutoRefresh()
{
    if (historyRefreshTimer) {
        return;
    }

    // 历史页的自动刷新只服务当前选中的任务，而且只在非终态时工作。
    // 这样用户看到的是“执行中信息会自己更新”，不是整页反复抖动。
    historyRefreshTimer = new QTimer(this);
    historyRefreshTimer->setInterval(HistoryAutoRefreshIntervalMs);
    connect(historyRefreshTimer, &QTimer::timeout, this, &MainWindow::refreshCurrentHistoryDetails);
}

void MainWindow::refreshCurrentHistoryDetails()
{
    if (currentHistoryTaskId.isEmpty() || !backendClient) {
        updateHistoryAutoRefreshState();
        return;
    }

    // 轻刷只针对当前选中任务，且直接复用后端已有的只读查询接口。
    // 这里不刷新历史列表，避免把整页分页和筛选状态也一起打乱。
    backendClient->requestTaskSteps(currentHistoryTaskId);
    if (currentHistoryRequiresConfirmation
        || currentHistoryRuntimeState.status == QStringLiteral("waiting_permission")) {
        backendClient->requestTaskPermissions(currentHistoryTaskId);
    }
    backendClient->requestTaskLogs(currentHistoryTaskId);
    backendClient->requestTaskRuntimeState(currentHistoryTaskId);
    backendClient->requestTaskMetrics(currentHistoryTaskId);
    backendClient->requestTaskModelRoutes(currentHistoryTaskId);
    backendClient->requestTaskEvaluation(currentHistoryTaskId);
    backendClient->requestTaskArtifacts(currentHistoryTaskId);
    backendClient->requestTaskToolCalls(currentHistoryTaskId);
    backendClient->requestTaskUpdates(currentHistoryTaskId);
}

bool MainWindow::shouldAutoRefreshCurrentHistoryTask() const
{
    if (currentHistoryTaskId.isEmpty()) {
        return false;
    }

    const QString delegatedStatus = currentHistoryDelegation().value(QStringLiteral("status")).toString();
    if (delegatedStatus == QStringLiteral("queued") || delegatedStatus == QStringLiteral("pending")
        || delegatedStatus == QStringLiteral("running") || delegatedStatus == QStringLiteral("waiting_permission")) {
        // 父 Runtime 在完成“交接”后会进入 completed；关联 K4 仍在执行时不能因此停掉历史轻刷。
        // 只读 updates 会返回子任务的聚合 checkpoint，不读取章节正文或模型结果。
        return true;
    }

    const QString effectiveStatus = (currentHistoryRuntimeStateLoaded
                                     && currentHistoryRuntimeStateError.isEmpty()
                                     && !currentHistoryRuntimeState.status.isEmpty())
                                        ? currentHistoryRuntimeState.status
                                        : currentHistoryStatus;
    return effectiveStatus == QStringLiteral("pending")
        || effectiveStatus == QStringLiteral("running")
        || effectiveStatus == QStringLiteral("waiting_permission")
        || effectiveStatus == QStringLiteral("blocked");
}

QJsonObject MainWindow::currentHistoryDelegation() const
{
    // updates 的状态快照由后端按当前子任务 checkpoint 重新计算。倒序读取确保不会把早期的
    // “已受理”回执误当成最新状态；这里只取第一条委派，C5.1 当前每个 Commander 计划至多
    // 产生一个 K4 深度总结子任务，未来多委派时可在此扩展为独立列表卡片。
    for (auto iterator = currentHistoryUpdates.crbegin(); iterator != currentHistoryUpdates.crend(); ++iterator) {
        if (iterator->event != QStringLiteral("task_state_snapshot")) {
            continue;
        }
        QJsonObject retrospective = iterator->payload.value(QStringLiteral("task_retrospective")).toObject();
        if (retrospective.isEmpty()) {
            retrospective = iterator->payload.value(QStringLiteral("retrospective")).toObject();
        }
        const QJsonArray delegations = retrospective.value(QStringLiteral("delegations")).toArray();
        if (!delegations.isEmpty() && delegations.first().isObject()) {
            return delegations.first().toObject();
        }
    }
    return {};
}

void MainWindow::updateHistoryAutoRefreshState()
{
    if (!historyRefreshTimer) {
        return;
    }

    if (shouldAutoRefreshCurrentHistoryTask()) {
        if (!historyRefreshTimer->isActive()) {
            historyRefreshTimer->start();
        }
    } else if (historyRefreshTimer->isActive()) {
        historyRefreshTimer->stop();
    }
}

void MainWindow::setupHistoryArtifactToolbar()
{
    if (historyArtifactStrip) {
        // 没有真实产物时整条工具栏不占位；否则“暂无产物”会挤压任务摘要和操作按钮。
        historyArtifactStrip->setVisible(false);
        historyArtifactStrip->setSizePolicy(QSizePolicy::Preferred, QSizePolicy::Maximum);
    }

    if (historyArtifactCombo) {
        historyArtifactCombo->setSizeAdjustPolicy(QComboBox::AdjustToContentsOnFirstShow);
        historyArtifactCombo->setMinimumContentsLength(18);
        historyArtifactCombo->setMaxVisibleItems(8);
        connect(historyArtifactCombo, qOverload<int>(&QComboBox::currentIndexChanged), this,
                &MainWindow::onHistoryArtifactSelectionChanged);
    }

    if (historyArtifactPreviewButton) {
        historyArtifactPreviewButton->setIcon(style()->standardIcon(QStyle::SP_FileDialogContentsView));
        connect(historyArtifactPreviewButton, &QToolButton::clicked, this, &MainWindow::previewSelectedHistoryArtifact);
    }

    if (historyArtifactOpenButton) {
        historyArtifactOpenButton->setIcon(style()->standardIcon(QStyle::SP_DirOpenIcon));
        connect(historyArtifactOpenButton, &QToolButton::clicked, this, &MainWindow::openSelectedHistoryArtifact);
    }

    if (historyArtifactCopyButton) {
        historyArtifactCopyButton->setIcon(style()->standardIcon(QStyle::SP_DialogSaveButton));
        connect(historyArtifactCopyButton, &QToolButton::clicked, this, &MainWindow::copySelectedHistoryArtifactPath);
    }

    refreshHistoryArtifactToolbar();
}

const WorkflowArtifactInfo *MainWindow::selectedHistoryArtifact() const
{
    if (currentHistoryArtifacts.isEmpty()) {
        return nullptr;
    }

    if (currentHistoryArtifactId.isEmpty()) {
        return &currentHistoryArtifacts.first();
    }

    for (const WorkflowArtifactInfo &artifact : currentHistoryArtifacts) {
        if (artifact.artifactId == currentHistoryArtifactId) {
            return &artifact;
        }
    }

    return nullptr;
}

QString MainWindow::historyArtifactLocalPath(const WorkflowArtifactInfo &artifact) const
{
    if (!artifact.uri.startsWith(QStringLiteral("agentflow-output://"))) {
        return QString();
    }

    if (!artifact.metadata.value(QStringLiteral("runtime")).toBool()) {
        return QString();
    }

    const QString outputPath = artifact.metadata.value(QStringLiteral("output_path")).toString();
    if (outputPath.isEmpty()) {
        return QString();
    }

    const QFileInfo info(outputPath);
    if (!info.isAbsolute()) {
        return QString();
    }

    return info.absoluteFilePath();
}

QString MainWindow::historyArtifactDelegatedTaskId(const WorkflowArtifactInfo &artifact) const
{
    // Runtime 只在已审计的关联产物上写入子任务 ID；不从摘要或用户文本猜测，避免跳错任务。
    const QString metadataTaskId = artifact.metadata.value(QStringLiteral("delegated_task_id")).toString().trimmed();
    if (!metadataTaskId.isEmpty()) {
        return metadataTaskId;
    }

    static const QString prefix = QStringLiteral("agentflow-task://");
    if (!artifact.uri.startsWith(prefix)) {
        return QString();
    }
    return artifact.uri.mid(prefix.size()).trimmed();
}

QString MainWindow::historyArtifactPreviewText(const WorkflowArtifactInfo &artifact) const
{
    const QString localPath = historyArtifactLocalPath(artifact);
    QStringList lines;
    lines.append(QStringLiteral("名称：%1").arg(artifact.name.isEmpty() ? QStringLiteral("未命名产物")
                                                                        : artifact.name));
    lines.append(QStringLiteral("类型：%1").arg(artifact.kind.isEmpty() ? QStringLiteral("other")
                                                                      : artifact.kind));
    lines.append(QStringLiteral("MIME：%1").arg(artifact.mimeType.isEmpty() ? QStringLiteral("未提供")
                                                                           : artifact.mimeType));
    lines.append(QStringLiteral("来源：%1").arg(historyArtifactSourceText(artifact, localPath)));
    lines.append(QStringLiteral("Agent：%1").arg(agentDisplayName(artifact.agentId)));
    lines.append(QStringLiteral("步骤：%1").arg(artifact.stepId.isEmpty() ? QStringLiteral("未知步骤")
                                                                         : artifact.stepId));
    lines.append(QStringLiteral("URI：%1").arg(artifact.uri.isEmpty() ? QStringLiteral("未提供") : artifact.uri));
    const QString delegatedTaskId = historyArtifactDelegatedTaskId(artifact);
    if (!delegatedTaskId.isEmpty()) {
        lines.append(QStringLiteral("关联任务：%1").arg(delegatedTaskId));
        lines.append(QStringLiteral("操作提示：点击“打开”可进入关联 Agent 的完整运行记录。"));
    }
    if (!artifact.summary.isEmpty()) {
        lines.append(QStringLiteral("摘要：%1").arg(artifact.summary));
    }
    const QString verificationText = formatHistoryVerificationText(historyVerificationForArtifact(artifact));
    if (!verificationText.isEmpty()) {
        lines.append(QString());
        lines.append(verificationText);
    }
    const QString documentContextText = formatHistoryDocumentContextText(
        historyDocumentContextForArtifact(artifact));
    if (!documentContextText.isEmpty()) {
        lines.append(QString());
        lines.append(documentContextText);
    }

    if (!localPath.isEmpty()) {
        const QFileInfo fileInfo(localPath);
        lines.append(QStringLiteral("本地路径：%1").arg(fileInfo.absoluteFilePath()));
        lines.append(QStringLiteral("文件大小：%1 字节").arg(fileInfo.exists() ? fileInfo.size() : 0));

        if (!fileInfo.exists()) {
            lines.append(QString());
            lines.append(QStringLiteral("预览说明："));
            lines.append(QStringLiteral("后端记录了受控 outputs 路径，但文件当前不存在。可能是产物已被清理、移动，或任务仍在生成中。"));
            return lines.join(QStringLiteral("\n"));
        }

        if (!fileInfo.isFile()) {
            lines.append(QString());
            lines.append(QStringLiteral("预览说明："));
            lines.append(QStringLiteral("当前路径不是普通文件，暂不做文本预览。可以尝试使用“打开”按钮交给系统处理。"));
            return lines.join(QStringLiteral("\n"));
        }

        if (!historyArtifactLooksTextLike(artifact, fileInfo)) {
            lines.append(QString());
            lines.append(QStringLiteral("预览说明："));
            lines.append(QStringLiteral("当前产物不像文本文件，为避免乱码或卡顿，AgentFlow 不在这里强行读取。可以用“打开”按钮交给系统应用处理。"));
            if (!artifact.metadata.isEmpty()) {
                lines.append(QString());
                lines.append(QStringLiteral("元数据："));
                lines.append(QString::fromUtf8(QJsonDocument(artifact.metadata).toJson(QJsonDocument::Indented)));
            }
            return lines.join(QStringLiteral("\n"));
        }

        QFile file(localPath);
        if (file.open(QIODevice::ReadOnly)) {
            QByteArray bytes = file.read(HistoryArtifactPreviewMaxBytes);
            if (bytes.contains('\0')) {
                lines.append(QString());
                lines.append(QStringLiteral("预览说明："));
                lines.append(QStringLiteral("文件包含二进制内容，暂不在文本预览中展开。可以用“打开”按钮交给系统应用处理。"));
                return lines.join(QStringLiteral("\n"));
            }

            QStringDecoder decoder(QStringDecoder::Utf8);
            QString content = decoder.decode(bytes);
            if (!file.atEnd()) {
                content += QStringLiteral("\n\n……内容过长，已截断。");
            }
            if (content.trimmed().isEmpty()) {
                content = QStringLiteral("文件内容为空。");
            }
            lines.append(QString());
            lines.append(decoder.hasError()
                             ? QStringLiteral("文件预览（按 UTF-8 读取，发现少量无法解码字符）：")
                             : QStringLiteral("文件预览："));
            lines.append(content);
            return lines.join(QStringLiteral("\n"));
        }

        lines.append(QString());
        lines.append(QStringLiteral("预览说明："));
        lines.append(QStringLiteral("本地文件存在，但当前无法读取。请检查文件权限，或稍后刷新任务详情。"));
        if (!artifact.metadata.isEmpty()) {
            lines.append(QString());
            lines.append(QStringLiteral("元数据："));
            lines.append(QString::fromUtf8(QJsonDocument(artifact.metadata).toJson(QJsonDocument::Indented)));
        }
        return lines.join(QStringLiteral("\n"));
    }

    lines.append(QString());
    lines.append(QStringLiteral("预览说明："));
    if (artifact.uri.startsWith(QStringLiteral("artifact://dry-run/"))) {
        lines.append(QStringLiteral("这是 dry-run 虚拟产物，没有本地文件。"));
    } else if (artifact.uri.startsWith(QStringLiteral("memory://"))) {
        lines.append(QStringLiteral("这是内存型产物，后端未写入磁盘。"));
    } else if (artifact.uri.startsWith(QStringLiteral("agentflow-output://"))) {
        lines.append(QStringLiteral("这是受控 runtime 输出，但当前文件不可读取或不存在。"));
    } else {
        lines.append(QStringLiteral("后端没有提供可安全打开的本地路径。"));
    }

    if (!artifact.metadata.isEmpty()) {
        lines.append(QString());
        lines.append(QStringLiteral("元数据："));
        lines.append(QString::fromUtf8(QJsonDocument(artifact.metadata).toJson(QJsonDocument::Indented)));
    }

    return lines.join(QStringLiteral("\n"));
}

QString MainWindow::historyArtifactPreviewText(
    const WorkflowArtifactInfo &artifact,
    const WorkflowArtifactPreviewResult &preview) const
{
    QString sourceText = QStringLiteral("后端未提供");
    if (preview.source == QStringLiteral("runtime_output")) {
        sourceText = QStringLiteral("后端受控 runtime outputs");
    } else if (preview.source == QStringLiteral("dry_run")) {
        sourceText = QStringLiteral("dry-run 虚拟产物");
    } else if (preview.source == QStringLiteral("memory")) {
        sourceText = QStringLiteral("内存产物");
    } else if (!preview.source.isEmpty()) {
        sourceText = preview.source;
    }

    QStringList lines;
    lines.append(QStringLiteral("名称：%1").arg(artifact.name.isEmpty() ? preview.name : artifact.name));
    lines.append(QStringLiteral("类型：%1").arg(artifact.kind.isEmpty() ? preview.kind : artifact.kind));
    lines.append(QStringLiteral("MIME：%1").arg(artifact.mimeType.isEmpty() ? preview.mimeType : artifact.mimeType));
    lines.append(QStringLiteral("预览状态：%1").arg(preview.available ? QStringLiteral("可预览")
                                                                      : QStringLiteral("不可预览")));
    lines.append(QStringLiteral("来源：%1").arg(sourceText));
    lines.append(QStringLiteral("Agent：%1").arg(agentDisplayName(artifact.agentId)));
    lines.append(QStringLiteral("步骤：%1").arg(artifact.stepId.isEmpty() ? QStringLiteral("未知步骤")
                                                                         : artifact.stepId));
    lines.append(QStringLiteral("URI：%1").arg(artifact.uri.isEmpty() ? preview.uri : artifact.uri));
    if (!artifact.summary.isEmpty()) {
        lines.append(QStringLiteral("摘要：%1").arg(artifact.summary));
    }

    const QString verificationText = formatHistoryVerificationText(historyVerificationForArtifact(artifact));
    if (!verificationText.isEmpty()) {
        lines.append(QString());
        lines.append(verificationText);
    }
    const QString documentContextText = formatHistoryDocumentContextText(
        historyDocumentContextForArtifact(artifact));
    if (!documentContextText.isEmpty()) {
        lines.append(QString());
        lines.append(documentContextText);
    }

    lines.append(QString());
    if (preview.available) {
        lines.append(QStringLiteral("预览读取：%1 字节 · 编码：%2%3")
                         .arg(preview.bytesRead)
                         .arg(preview.encoding.isEmpty() ? QStringLiteral("utf-8") : preview.encoding)
                         .arg(preview.truncated ? QStringLiteral(" · 已截断") : QString()));
        lines.append(QStringLiteral("文件预览："));
        lines.append(preview.text.trimmed().isEmpty() ? QStringLiteral("文件内容为空。") : preview.text);
        if (preview.truncated) {
            lines.append(QString());
            lines.append(QStringLiteral("……内容过长，后端已按预览上限截断。"));
        }
    } else {
        lines.append(QStringLiteral("预览说明："));
        lines.append(preview.reason.isEmpty()
                         ? QStringLiteral("后端未允许预览该产物。")
                         : preview.reason);
    }

    if (!preview.metadata.isEmpty()) {
        lines.append(QString());
        lines.append(QStringLiteral("后端预览元数据（已脱敏）："));
        lines.append(QString::fromUtf8(QJsonDocument(preview.metadata).toJson(QJsonDocument::Indented)));
    }

    return lines.join(QStringLiteral("\n"));
}

void MainWindow::refreshHistoryArtifactToolbar()
{
    if (!historyArtifactStrip
        && !historyArtifactCombo
        && !historyArtifactPreviewButton
        && !historyArtifactOpenButton
        && !historyArtifactCopyButton) {
        return;
    }

    const bool shouldShowArtifacts = !currentHistoryTaskId.isEmpty()
        && currentHistoryArtifactsLoaded
        && currentHistoryArtifactsError.isEmpty()
        && !currentHistoryArtifacts.isEmpty();

    if (historyArtifactStrip) {
        historyArtifactStrip->setVisible(shouldShowArtifacts);
    }

    if (historyArtifactCombo) {
        historyArtifactCombo->blockSignals(true);
        historyArtifactCombo->clear();

        if (!shouldShowArtifacts) {
            currentHistoryArtifactId.clear();
            historyArtifactCombo->setEnabled(false);
        } else {
            int selectedIndex = -1;
            for (const WorkflowArtifactInfo &artifact : currentHistoryArtifacts) {
                const int index = historyArtifactCombo->count();
                historyArtifactCombo->addItem(historyArtifactDisplayText(artifact), artifact.artifactId);
                historyArtifactCombo->setItemData(index, historyArtifactTooltipText(artifact), Qt::ToolTipRole);
                if (!currentHistoryArtifactId.isEmpty() && artifact.artifactId == currentHistoryArtifactId) {
                    selectedIndex = index;
                }
            }

            if (selectedIndex < 0) {
                selectedIndex = 0;
            }
            historyArtifactCombo->setEnabled(true);
            historyArtifactCombo->setCurrentIndex(selectedIndex);
            currentHistoryArtifactId = historyArtifactCombo->currentData().toString();
        }

        historyArtifactCombo->blockSignals(false);
    }

    updateHistoryArtifactActionState();
}

QString MainWindow::historyModelRouteStageText(const QString &stage) const
{
    if (stage == QStringLiteral("document_analysis")) {
        return QStringLiteral("文档分析");
    }
    if (stage == QStringLiteral("data_insight")) {
        return QStringLiteral("数据洞察");
    }
    if (stage == QStringLiteral("knowledge_answer")) {
        return QStringLiteral("知识库问答");
    }
    if (stage == QStringLiteral("knowledge_deep_map")) {
        return QStringLiteral("深度分析 · Map");
    }
    if (stage == QStringLiteral("knowledge_deep_reduce")) {
        return QStringLiteral("深度分析 · Reduce");
    }
    return stage.isEmpty() ? QStringLiteral("未声明阶段") : stage;
}

QString MainWindow::historyModelRouteSummaryText() const
{
    QStringList entries;
    QSet<QString> seen;
    for (const WorkflowModelRouteAuditInfo &route : currentHistoryModelRoutes) {
        const QString provider = route.label.trimmed().isEmpty() ? route.provider : route.label;
        const QString entry = QStringLiteral("%1：%2 / %3")
                                  .arg(historyModelRouteStageText(route.stage),
                                       provider.isEmpty() ? QStringLiteral("未声明 Provider") : provider,
                                       route.model.isEmpty() ? QStringLiteral("未声明模型") : route.model);
        if (!seen.contains(entry)) {
            seen.insert(entry);
            entries.append(entry);
        }
    }
    return entries.join(QStringLiteral("；"));
}

void MainWindow::updateHistoryModelRoutesButton()
{
    if (!historyModelRoutesButton) {
        return;
    }

    const bool hasAudit = !currentHistoryTaskId.isEmpty()
        && currentHistoryModelRoutesLoaded
        && currentHistoryModelRoutesError.isEmpty()
        && !currentHistoryModelRoutes.isEmpty();
    historyModelRoutesButton->setVisible(hasAudit);
    historyModelRoutesButton->setEnabled(hasAudit);
    if (!hasAudit) {
        return;
    }

    historyModelRoutesButton->setToolTip(
        QStringLiteral("查看本次实际模型：%1").arg(historyModelRouteSummaryText()));
}

void MainWindow::showHistoryModelRoutesDialog()
{
    if (!currentHistoryModelRoutesLoaded || currentHistoryModelRoutes.isEmpty()) {
        return;
    }

    QDialog dialog(this);
    dialog.setWindowTitle(QStringLiteral("本次实际模型"));
    dialog.setMinimumSize(920, 430);

    auto *layout = new QVBoxLayout(&dialog);
    layout->setContentsMargins(18, 18, 18, 18);
    layout->setSpacing(10);

    auto *titleLabel = new QLabel(QStringLiteral("实际执行模型路由"), &dialog);
    titleLabel->setObjectName(QStringLiteral("cardTitle"));
    auto *descriptionLabel = new QLabel(
        QStringLiteral("以下是任务执行时保存的脱敏快照，不会随当前模型配置变化；不包含 API Key、Base URL、提示词或材料内容。"),
        &dialog);
    descriptionLabel->setObjectName(QStringLiteral("subText"));
    descriptionLabel->setWordWrap(true);

    auto *table = new QTableWidget(currentHistoryModelRoutes.size(), 6, &dialog);
    table->setHorizontalHeaderLabels(
        {QStringLiteral("阶段"), QStringLiteral("路由"), QStringLiteral("Profile"),
         QStringLiteral("Provider"), QStringLiteral("模型"), QStringLiteral("思考")});
    table->setEditTriggers(QAbstractItemView::NoEditTriggers);
    table->setSelectionBehavior(QAbstractItemView::SelectRows);
    table->setSelectionMode(QAbstractItemView::SingleSelection);
    table->verticalHeader()->setVisible(false);
    table->setAlternatingRowColors(true);
    table->setWordWrap(false);

    for (int row = 0; row < currentHistoryModelRoutes.size(); ++row) {
        const WorkflowModelRouteAuditInfo &route = currentHistoryModelRoutes.at(row);
        const QString provider = route.label.trimmed().isEmpty()
            ? route.provider
            : QStringLiteral("%1 (%2)").arg(route.label, route.provider);
        const QString thinking = route.thinking == QStringLiteral("enabled")
            ? QStringLiteral("开启")
            : QStringLiteral("关闭");
        const QStringList cells = {
            historyModelRouteStageText(route.stage),
            route.routeId,
            route.profileId,
            provider,
            route.model,
            thinking,
        };
        for (int column = 0; column < cells.size(); ++column) {
            auto *item = new QTableWidgetItem(cells.at(column));
            if (!route.note.trimmed().isEmpty()) {
                item->setToolTip(route.note);
            }
            table->setItem(row, column, item);
        }
    }

    table->horizontalHeader()->setStretchLastSection(false);
    table->horizontalHeader()->setSectionResizeMode(0, QHeaderView::Stretch);
    table->horizontalHeader()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
    table->horizontalHeader()->setSectionResizeMode(2, QHeaderView::ResizeToContents);
    table->horizontalHeader()->setSectionResizeMode(3, QHeaderView::ResizeToContents);
    table->horizontalHeader()->setSectionResizeMode(4, QHeaderView::Stretch);
    table->horizontalHeader()->setSectionResizeMode(5, QHeaderView::ResizeToContents);

    auto *buttonBox = new QDialogButtonBox(QDialogButtonBox::Close, &dialog);
    connect(buttonBox, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);

    layout->addWidget(titleLabel);
    layout->addWidget(descriptionLabel);
    layout->addWidget(table, 1);
    layout->addWidget(buttonBox);
    dialog.exec();
}

void MainWindow::updateHistoryArtifactActionState()
{
    const WorkflowArtifactInfo *artifact = selectedHistoryArtifact();
    const bool hasArtifact = artifact != nullptr;
    const QString localPath = hasArtifact ? historyArtifactLocalPath(*artifact) : QString();
    const QString delegatedTaskId = hasArtifact ? historyArtifactDelegatedTaskId(*artifact) : QString();
    const bool isKnowledgeDeepDelegation = delegatedTaskId.startsWith(QStringLiteral("task_k4_"));
    const bool isRuntimeArtifact = hasArtifact
        && artifact->uri.startsWith(QStringLiteral("agentflow-output://"))
        && artifact->metadata.value(QStringLiteral("runtime")).toBool();
    const bool openingSelected = hasArtifact && historyArtifactOpenInProgress
        && pendingHistoryArtifactOpenTaskId == (artifact->taskId.isEmpty() ? currentHistoryTaskId : artifact->taskId)
        && pendingHistoryArtifactOpenId == artifact->artifactId;
    const bool canOpen = !delegatedTaskId.isEmpty() || isRuntimeArtifact;

    if (historyArtifactPreviewButton) {
        historyArtifactPreviewButton->setEnabled(hasArtifact && !historyArtifactPreviewInProgress);
        historyArtifactPreviewButton->setToolTip(
            historyArtifactPreviewInProgress
                ? QStringLiteral("正在向后端请求受控预览")
                : (!delegatedTaskId.isEmpty()
                       ? QStringLiteral("查看关联 Agent 的说明与子任务 ID")
                       : (hasArtifact ? QStringLiteral("通过后端受控接口预览文本内容、来源说明和元数据")
                               : QStringLiteral("先选择一条任务"))));
    }
    if (historyArtifactOpenButton) {
        historyArtifactOpenButton->setEnabled(canOpen && !openingSelected);
        historyArtifactOpenButton->setIcon(style()->standardIcon(
            delegatedTaskId.isEmpty() ? QStyle::SP_DirOpenIcon : QStyle::SP_ArrowForward));
        historyArtifactOpenButton->setToolTip(!delegatedTaskId.isEmpty()
                                                      ? (isKnowledgeDeepDelegation
                                                             ? QStringLiteral("打开关联深度任务工作台，查看真实进度和控制入口")
                                                             : QStringLiteral("打开关联 Agent 的完整运行记录"))
                                                      : (openingSelected ? QStringLiteral("正在交给系统默认程序打开")
                                                      : (canOpen ? QStringLiteral("通过后端受控边界打开真实交付物")
                                                      : (hasArtifact
                                                             ? historyArtifactOpenUnavailableText(*artifact, localPath)
                                                             : QStringLiteral("先选择一条任务")))));
    }
    if (historyArtifactCopyButton) {
        historyArtifactCopyButton->setEnabled(hasArtifact);
        historyArtifactCopyButton->setToolTip(hasArtifact ? QStringLiteral("复制受控本地路径；没有路径时复制产物 URI")
                                                          : QStringLiteral("先选择一条任务"));
    }
}

void MainWindow::onHistoryArtifactSelectionChanged()
{
    if (!historyArtifactCombo) {
        return;
    }

    currentHistoryArtifactId = historyArtifactCombo->currentData().toString();
    updateHistoryArtifactActionState();
}

void MainWindow::showHistoryArtifactPreviewDialog(const WorkflowArtifactInfo &artifact, const QString &previewText)
{
    QDialog dialog(this);
    dialog.setWindowTitle(QStringLiteral("产物预览 · %1").arg(artifact.name.isEmpty() ? QStringLiteral("未命名产物")
                                                                                    : artifact.name));
    dialog.setMinimumSize(860, 560);

    auto *layout = new QVBoxLayout(&dialog);
    layout->setContentsMargins(18, 18, 18, 18);
    layout->setSpacing(10);

    auto *titleLabel = new QLabel(historyArtifactDisplayText(artifact), &dialog);
    titleLabel->setObjectName(QStringLiteral("cardTitle"));
    titleLabel->setWordWrap(true);

    auto *metaLabel = new QLabel(historyArtifactTooltipText(artifact), &dialog);
    metaLabel->setObjectName(QStringLiteral("subText"));
    metaLabel->setWordWrap(true);

    auto *previewEdit = new QPlainTextEdit(&dialog);
    previewEdit->setReadOnly(true);
    previewEdit->setLineWrapMode(QPlainTextEdit::NoWrap);
    previewEdit->setPlainText(previewText);

    auto *buttonBox = new QDialogButtonBox(QDialogButtonBox::Close, &dialog);
    connect(buttonBox, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);

    layout->addWidget(titleLabel);
    layout->addWidget(metaLabel);
    layout->addWidget(previewEdit, 1);
    layout->addWidget(buttonBox);

    dialog.exec();
}

void MainWindow::previewSelectedHistoryArtifact()
{
    const WorkflowArtifactInfo *artifact = selectedHistoryArtifact();
    if (!artifact) {
        QMessageBox::information(this, QStringLiteral("产物预览"), QStringLiteral("当前没有可预览的产物。"));
        return;
    }

    if (!historyArtifactDelegatedTaskId(*artifact).isEmpty()) {
        showHistoryArtifactPreviewDialog(*artifact, historyArtifactPreviewText(*artifact));
        return;
    }

    pendingHistoryArtifactPreviewTaskId = artifact->taskId.isEmpty() ? currentHistoryTaskId : artifact->taskId;
    pendingHistoryArtifactPreviewId = artifact->artifactId;
    historyArtifactPreviewInProgress = true;
    updateHistoryArtifactActionState();

    // 预览走后端受控接口，不再由 Qt 直接读取 output_path。这样后续打包或远程后端时边界一致。
    backendClient->requestTaskArtifactPreview(
        pendingHistoryArtifactPreviewTaskId,
        pendingHistoryArtifactPreviewId,
        static_cast<int>(HistoryArtifactPreviewMaxBytes));
}

void MainWindow::openSelectedHistoryArtifact()
{
    const WorkflowArtifactInfo *artifact = selectedHistoryArtifact();
    if (!artifact) {
        return;
    }

    const QString delegatedTaskId = historyArtifactDelegatedTaskId(*artifact);
    if (!delegatedTaskId.isEmpty()) {
        if (delegatedTaskId.startsWith(QStringLiteral("task_k4_"))) {
            openKnowledgeDeepTaskDialogForExistingTask(delegatedTaskId);
            return;
        }
        // 新创建的子任务通常就在当前页；不在时回到第一页刷新，历史列表回调会按
        // pendingHistoryFocusTaskId 自动选中它，用户不必手工复制 ID 再筛选。
        if (selectHistoryRowByTaskId(delegatedTaskId)) {
            QToolTip::showText(mapToGlobal(QPoint(width() / 2, height() / 2)),
                               QStringLiteral("已切换到关联 Agent 运行记录。"),
                               this,
                               QRect(),
                               1600);
            return;
        }

        pendingHistoryFocusTaskId = delegatedTaskId;
        historyOffset = 0;
        refreshTaskHistory();
        return;
    }

    const QString taskId = artifact->taskId.isEmpty() ? currentHistoryTaskId : artifact->taskId;
    if (artifact->uri.startsWith(QStringLiteral("agentflow-output://"))
        && artifact->metadata.value(QStringLiteral("runtime")).toBool()) {
        // 公开 artifact 不含本机绝对路径；由后端根据登记的安全 metadata 决定是否能打开。
        pendingHistoryArtifactOpenTaskId = taskId;
        pendingHistoryArtifactOpenId = artifact->artifactId;
        historyArtifactOpenInProgress = true;
        updateHistoryArtifactActionState();
        backendClient->requestTaskArtifactOpen(taskId, artifact->artifactId);
        return;
    }

    const QString localPath = historyArtifactLocalPath(*artifact);
    if (localPath.isEmpty() || !QFileInfo(localPath).exists()) {
        QMessageBox::information(this,
                                 QStringLiteral("打开产物"),
                                 historyArtifactOpenUnavailableText(*artifact, localPath));
        return;
    }

    // 只打开后端声明过的受控 outputs 文件，不从用户输入拼接路径。
    if (!QDesktopServices::openUrl(QUrl::fromLocalFile(localPath))) {
        QMessageBox::warning(this,
                             QStringLiteral("打开产物"),
                             QStringLiteral("系统未能打开该文件：%1").arg(localPath));
    }
}

void MainWindow::copySelectedHistoryArtifactPath()
{
    const WorkflowArtifactInfo *artifact = selectedHistoryArtifact();
    if (!artifact) {
        return;
    }

    const QString localPath = historyArtifactLocalPath(*artifact);
    const QString clipboardText = localPath.isEmpty() ? artifact->uri : localPath;
    QApplication::clipboard()->setText(clipboardText);
    QToolTip::showText(mapToGlobal(QPoint(width() / 2, height() / 2)),
                       localPath.isEmpty() ? QStringLiteral("已复制产物 URI。")
                                           : QStringLiteral("已复制受控产物路径。"),
                       this,
                       QRect(),
                       1800);
}

void MainWindow::refreshTaskHistory()
{
    if (!historyTable || !historyDetailText) {
        return;
    }

    // 每次刷新都先清空详情，避免用户把旧任务日志误认为新结果。
    historyRefreshButton->setEnabled(false);
    historyPrevButton->setEnabled(false);
    historyNextButton->setEnabled(false);

    historyTable->setRowCount(0);
    showHistoryEmptyState(QStringLiteral("正在加载历史任务……"));

    TaskHistoryQuery query;
    query.limit = historyLimit;
    query.offset = historyOffset;
    query.status = historyStatusFilter ? historyStatusFilter->currentData().toString() : QString();
    query.mode = historyModeFilter ? historyModeFilter->currentData().toString() : QString();
    query.maxRiskLevel = historyRiskFilter ? historyRiskFilter->currentData().toString() : QString();
    query.requiresConfirmation = historyConfirmationFilter ? historyConfirmationFilter->currentData().toInt() : -1;

    // 任务历史只拉当前页数据，分页参数交给后端，避免客户端维护全量缓存。
    backendClient->requestTaskHistory(query);
}

void MainWindow::handleTaskHistoryReceived(const TaskHistoryResult &result)
{
    historyRefreshButton->setEnabled(true);

    historyTotal = result.total;
    historyOffset = qBound(0, result.offset, qMax(0, historyTotal));
    historyLimit = qMax(1, result.limit);

    historyTable->blockSignals(true);
    historyTable->setRowCount(result.tasks.size());

    int confirmationCount = 0;
    for (int row = 0; row < result.tasks.size(); ++row) {
        const TaskHistoryItem &item = result.tasks.at(row);
        if (item.requiresConfirmation) {
            ++confirmationCount;
        }

        auto *statusItem = new QTableWidgetItem(historyStatusText(item.status));
        statusItem->setData(Qt::UserRole, item.status);
        statusItem->setTextAlignment(Qt::AlignCenter);
        if (item.status == QStringLiteral("completed")) {
            statusItem->setBackground(QBrush(QColor("#ECFDF5")));
            statusItem->setForeground(QBrush(QColor("#059669")));
        } else if (item.status == QStringLiteral("running") || item.status == QStringLiteral("pending")) {
            statusItem->setBackground(QBrush(QColor("#EAF2FF")));
            statusItem->setForeground(QBrush(QColor("#2563EB")));
        } else if (item.status == QStringLiteral("waiting_permission")) {
            statusItem->setBackground(QBrush(QColor("#FFF7ED")));
            statusItem->setForeground(QBrush(QColor("#C2410C")));
        } else if (item.status == QStringLiteral("blocked")) {
            statusItem->setBackground(QBrush(QColor("#FFF7ED")));
            statusItem->setForeground(QBrush(QColor("#C2410C")));
        } else if (item.status == QStringLiteral("failed") || item.status == QStringLiteral("cancelled")) {
            statusItem->setBackground(QBrush(QColor("#FEF2F2")));
            statusItem->setForeground(QBrush(QColor("#DC2626")));
        }

        auto *taskIdItem = new QTableWidgetItem(item.taskId);
        taskIdItem->setData(Qt::UserRole, item.taskId);
        taskIdItem->setToolTip(item.taskId);

        auto *summaryItem = new QTableWidgetItem(item.summary);
        summaryItem->setToolTip(item.summary);

        auto *riskItem = new QTableWidgetItem(historyRiskText(item.maxRiskLevel));
        riskItem->setData(Qt::UserRole, item.maxRiskLevel);
        riskItem->setTextAlignment(Qt::AlignCenter);

        auto *confirmItem = new QTableWidgetItem(historyConfirmationLabelText(item.requiresConfirmation));
        confirmItem->setData(Qt::UserRole, item.requiresConfirmation);
        confirmItem->setTextAlignment(Qt::AlignCenter);
        if (item.requiresConfirmation) {
            confirmItem->setBackground(QBrush(QColor("#FFF7ED")));
            confirmItem->setForeground(QBrush(QColor("#C2410C")));
        } else {
            confirmItem->setBackground(QBrush(QColor("#F8FAFC")));
            confirmItem->setForeground(QBrush(QColor("#64748B")));
        }

        auto *stepItem = new QTableWidgetItem(QString::number(item.stepCount));
        stepItem->setData(Qt::UserRole, item.stepCount);
        stepItem->setTextAlignment(Qt::AlignCenter);

        auto *updatedItem = new QTableWidgetItem(item.updatedAt);
        updatedItem->setData(Qt::UserRole, item.updatedAt);
        updatedItem->setToolTip(item.updatedAt);

        auto *modeItem = new QTableWidgetItem(historyModeText(item.mode));
        modeItem->setData(Qt::UserRole, item.mode);
        modeItem->setTextAlignment(Qt::AlignCenter);

        historyTable->setItem(row, 0, statusItem);
        historyTable->setItem(row, 1, taskIdItem);
        historyTable->setItem(row, 2, summaryItem);
        historyTable->setItem(row, 3, riskItem);
        historyTable->setItem(row, 4, confirmItem);
        historyTable->setItem(row, 5, stepItem);
        historyTable->setItem(row, 6, updatedItem);
        historyTable->setItem(row, 7, modeItem);
    }
    historyTable->blockSignals(false);

    const int startIndex = historyTotal == 0 ? 0 : historyOffset + 1;
    const int endIndex = historyTotal == 0 ? 0 : historyOffset + result.tasks.size();
    historyCountLabel->setText(QStringLiteral("共 %1 条 · 需确认 %2 条").arg(historyTotal).arg(confirmationCount));
    historyPageLabel->setText(
        historyTotal == 0
            ? QStringLiteral("暂无任务")
            : QStringLiteral("第 %1 页 · 显示 %2-%3 / %4")
                  .arg(historyOffset / historyLimit + 1)
                  .arg(startIndex)
                  .arg(endIndex)
                  .arg(historyTotal));

    // 先按后端分页结果渲染，再做当前页关键词隐藏，这样总数和页码不会被本地搜索污染。
    applyHistoryKeywordFilter();
    updateHistoryPagerControls();

    if (historyTable->rowCount() == 0) {
        showHistoryEmptyState(QStringLiteral("当前筛选没有任务。"));
        return;
    }

    bool selectedVisibleRow = false;
    if (!pendingHistoryFocusTaskId.isEmpty()) {
        selectedVisibleRow = selectHistoryRowByTaskId(pendingHistoryFocusTaskId);
        if (selectedVisibleRow) {
            pendingHistoryFocusTaskId.clear();
        } else {
            pendingHistoryFocusTaskId.clear();
        }
    }

    if (!selectedVisibleRow) {
        for (int row = 0; row < historyTable->rowCount(); ++row) {
            if (!historyTable->isRowHidden(row)) {
                historyTable->selectRow(row);
                selectedVisibleRow = true;
                break;
            }
        }
    }

    if (!selectedVisibleRow) {
        showHistoryEmptyState(QStringLiteral("当前关键词没有匹配到任务。"));
    } else {
        updateHistoryActionButtons();
    }
}

void MainWindow::handleTaskHistoryFailed(const QString &message)
{
    historyRefreshButton->setEnabled(true);
    historyTotal = 0;
    historyOffset = 0;
    historyTable->setRowCount(0);
    showHistoryEmptyState(QStringLiteral("任务历史加载失败：%1").arg(message));
    historyCountLabel->setText(QStringLiteral("加载失败"));
    historyPageLabel->setText(QStringLiteral("请稍后重试"));
    updateHistoryPagerControls();
}

void MainWindow::handleTaskPlanReceived(const WorkflowPlanDetailResult &result)
{
    if (result.taskId.isEmpty() || result.taskId != currentHistoryTaskId) {
        return;
    }

    currentHistoryPlanSummary = result.planSummary;
    currentHistoryPlanSteps = result.steps;
    currentHistoryPlanLoaded = true;
    currentHistoryPlanError.clear();
    refreshHistoryDetailPanel();
}

void MainWindow::handleTaskPlanFailed(const QString &message)
{
    if (currentHistoryTaskId.isEmpty()) {
        return;
    }

    currentHistoryPlanSummary = WorkflowPlanSummaryInfo{};
    currentHistoryPlanSteps.clear();
    currentHistoryPlanLoaded = true;
    currentHistoryPlanError = message;
    refreshHistoryDetailPanel();
}

void MainWindow::handleTaskStepsReceived(const TaskStepListResult &result)
{
    if (result.taskId.isEmpty() || result.taskId != currentHistoryTaskId) {
        return;
    }

    currentHistorySteps = result.steps;
    currentHistoryStepCount = result.total > 0 ? result.total : result.steps.size();
    currentHistoryStepsLoaded = true;
    currentHistoryStepsError.clear();
    refreshHistoryDetailPanel();
    updateHistoryActionButtons();
}

void MainWindow::handleTaskStepsFailed(const QString &message)
{
    if (currentHistoryTaskId.isEmpty()) {
        return;
    }

    currentHistoryStepsLoaded = true;
    currentHistoryStepsError = message;
    refreshHistoryDetailPanel();
    updateHistoryActionButtons();
}

void MainWindow::handleTaskRuntimeStateReceived(const WorkflowRuntimeStateInfo &result)
{
    if (result.taskId.isEmpty() || result.taskId != currentHistoryTaskId) {
        return;
    }

    currentHistoryRuntimeState = result;
    currentHistoryRuntimeStateLoaded = true;
    currentHistoryRuntimeStateError.clear();
    updateHistoryRuntimePanel();
    refreshHistorySelectionBadge();
    refreshHistoryDetailPanel();
}

void MainWindow::handleTaskRuntimeStateFailed(const QString &message)
{
    if (currentHistoryTaskId.isEmpty()) {
        return;
    }

    currentHistoryRuntimeStateLoaded = true;
    currentHistoryRuntimeStateError = message;
    updateHistoryRuntimePanel();
    refreshHistorySelectionBadge();
    refreshHistoryDetailPanel();
}

void MainWindow::handleTaskMetricsReceived(const WorkflowRuntimeMetricsResult &result)
{
    if (result.taskId.isEmpty() || result.taskId != currentHistoryTaskId) {
        return;
    }

    currentHistoryMetrics = result;
    currentHistoryMetricsLoaded = true;
    currentHistoryMetricsError.clear();
    updateHistoryRuntimePanel();
    refreshHistoryDetailPanel();
}

void MainWindow::handleTaskMetricsFailed(const QString &message)
{
    if (currentHistoryTaskId.isEmpty()) {
        return;
    }

    currentHistoryMetricsLoaded = true;
    currentHistoryMetricsError = message;
    updateHistoryRuntimePanel();
    refreshHistoryDetailPanel();
}

void MainWindow::handleTaskModelRoutesReceived(const WorkflowModelRouteAuditResult &result)
{
    if (result.taskId.isEmpty() || result.taskId != currentHistoryTaskId) {
        return;
    }

    currentHistoryModelRoutes = result.modelRoutes;
    currentHistoryModelRoutesLoaded = true;
    currentHistoryModelRoutesError.clear();
    updateHistoryModelRoutesButton();
    refreshHistoryDetailPanel();
}

void MainWindow::handleTaskModelRoutesFailed(const QString &message)
{
    if (currentHistoryTaskId.isEmpty()) {
        return;
    }

    currentHistoryModelRoutes.clear();
    currentHistoryModelRoutesLoaded = true;
    currentHistoryModelRoutesError = message;
    updateHistoryModelRoutesButton();
    refreshHistoryDetailPanel();
}

void MainWindow::handleTaskEvaluationReceived(const WorkflowTaskEvaluationResult &result)
{
    if (result.taskId.isEmpty() || result.taskId != currentHistoryTaskId) {
        return;
    }

    currentHistoryEvaluation = result;
    currentHistoryEvaluationLoaded = true;
    currentHistoryEvaluationError.clear();
    refreshHistoryDetailPanel();
}

void MainWindow::handleTaskEvaluationFailed(const QString &message)
{
    if (currentHistoryTaskId.isEmpty()) {
        return;
    }

    currentHistoryEvaluationLoaded = true;
    currentHistoryEvaluationError = message;
    refreshHistoryDetailPanel();
}

void MainWindow::handleNodeContractsReceived(const WorkflowNodeContractListResult &result)
{
    workflowNodeContractsByStep.clear();
    workflowNodeContractsByTool.clear();

    for (const WorkflowNodeContractInfo &contract : result.contracts) {
        workflowNodeContractsByStep.insert(nodeContractKey(contract.agentId, contract.action), contract);
        if (!contract.toolName.isEmpty()) {
            workflowNodeContractsByTool.insert(contract.toolName, contract);
        }
    }

    workflowNodeContractsLoaded = true;
    workflowNodeContractsError.clear();
    refreshHistoryDetailPanel();
}

void MainWindow::handleNodeContractsFailed(const QString &message)
{
    workflowNodeContractsLoaded = true;
    workflowNodeContractsError = message;
    refreshHistoryDetailPanel();
}

void MainWindow::handleTaskArtifactsReceived(const WorkflowArtifactListResult &result)
{
    if (!pendingAutoOpenArtifactTaskId.isEmpty() && result.taskId == pendingAutoOpenArtifactTaskId) {
        pendingAutoOpenArtifactTaskId.clear();
        for (const WorkflowArtifactInfo &artifact : result.artifacts) {
            if (artifact.uri.startsWith(QStringLiteral("agentflow-output://"))
                && artifact.metadata.value(QStringLiteral("runtime")).toBool()) {
                backendClient->requestTaskArtifactOpen(result.taskId, artifact.artifactId);
                break;
            }
        }
    }

    if (result.taskId.isEmpty() || result.taskId != currentHistoryTaskId) {
        return;
    }

    currentHistoryArtifacts = result.artifacts;
    currentHistoryArtifactsLoaded = true;
    currentHistoryArtifactsError.clear();
    refreshHistoryArtifactToolbar();
    refreshHistoryDetailPanel();
}

void MainWindow::handleTaskArtifactOpened(
    const QString &taskId,
    const QString &artifactId,
    const QString &message)
{
    if (taskId == currentDispatchDeliveryOpenArtifactTaskId
        && artifactId == currentDispatchDeliveryOpenArtifactId) {
        currentDispatchDeliveryOpenInProgress = false;
        ui->dispatchDeliveryOpenButton->setEnabled(true);
        ui->dispatchDeliveryStatus->setText(QStringLiteral("已打开"));
        polishBadge(ui->dispatchDeliveryStatus, QStringLiteral("badgeGreen"));
        if (dispatchDeliveryDialogOpenButton) {
            dispatchDeliveryDialogOpenButton->setEnabled(true);
        }
        if (dispatchDeliveryDialogStatus) {
            dispatchDeliveryDialogStatus->setText(QStringLiteral("已打开"));
            polishBadge(dispatchDeliveryDialogStatus, QStringLiteral("badgeGreen"));
        }
    }

    const bool matchesHistoryOpen = taskId == pendingHistoryArtifactOpenTaskId
        && artifactId == pendingHistoryArtifactOpenId;
    if (matchesHistoryOpen) {
        historyArtifactOpenInProgress = false;
        pendingHistoryArtifactOpenTaskId.clear();
        pendingHistoryArtifactOpenId.clear();
        updateHistoryArtifactActionState();
    }

    if (taskId == lastDataWorkbookExportTaskId || taskId == lastDataTransformationTaskId) {
        ui->dataAnalysisHint->setText(QStringLiteral("%1 已写入任务历史；系统默认程序已打开交付物。").arg(message));
    }

    if (matchesHistoryOpen) {
        QToolTip::showText(mapToGlobal(QPoint(width() / 2, height() / 2)), message, this, QRect(), 1800);
    }
}

void MainWindow::handleTaskArtifactOpenFailed(
    const QString &taskId,
    const QString &artifactId,
    const QString &message)
{
    if (taskId == currentDispatchDeliveryOpenArtifactTaskId
        && artifactId == currentDispatchDeliveryOpenArtifactId) {
        currentDispatchDeliveryOpenInProgress = false;
        ui->dispatchDeliveryOpenButton->setEnabled(true);
        ui->dispatchDeliveryStatus->setText(QStringLiteral("打开失败"));
        polishBadge(ui->dispatchDeliveryStatus, QStringLiteral("badgeOrange"));
        if (dispatchDeliveryDialogOpenButton) {
            dispatchDeliveryDialogOpenButton->setEnabled(true);
        }
        if (dispatchDeliveryDialogStatus) {
            dispatchDeliveryDialogStatus->setText(QStringLiteral("打开失败"));
            polishBadge(dispatchDeliveryDialogStatus, QStringLiteral("badgeOrange"));
        }
    }

    const bool matchesHistoryOpen = taskId == pendingHistoryArtifactOpenTaskId
        && artifactId == pendingHistoryArtifactOpenId;
    if (matchesHistoryOpen) {
        historyArtifactOpenInProgress = false;
        pendingHistoryArtifactOpenTaskId.clear();
        pendingHistoryArtifactOpenId.clear();
        updateHistoryArtifactActionState();
    }

    if (taskId == lastDataWorkbookExportTaskId || taskId == lastDataTransformationTaskId) {
        ui->dataAnalysisHint->setText(QStringLiteral("交付物已生成，但系统未能自动打开：%1。可从任务历史重试。").arg(message.left(180)));
    }

    if (matchesHistoryOpen) {
        QMessageBox::warning(this, QStringLiteral("打开产物失败"), message);
    }
}

void MainWindow::handleTaskArtifactsFailed(const QString &message)
{
    if (currentHistoryTaskId.isEmpty()) {
        return;
    }

    currentHistoryArtifactsLoaded = true;
    currentHistoryArtifactsError = message;
    currentHistoryArtifactId.clear();
    refreshHistoryArtifactToolbar();
    refreshHistoryDetailPanel();
}

void MainWindow::handleTaskArtifactPreviewReceived(const WorkflowArtifactPreviewResult &result)
{
    const bool matchesPending = result.taskId == pendingHistoryArtifactPreviewTaskId
        && result.artifactId == pendingHistoryArtifactPreviewId;
    if (!matchesPending) {
        return;
    }

    historyArtifactPreviewInProgress = false;
    pendingHistoryArtifactPreviewTaskId.clear();
    pendingHistoryArtifactPreviewId.clear();
    updateHistoryArtifactActionState();

    // 用户可能已经切换到别的任务；这种过期响应不再弹窗，避免造成“看错任务”的困惑。
    if (result.taskId != currentHistoryTaskId) {
        return;
    }

    const WorkflowArtifactInfo *artifact = nullptr;
    for (const WorkflowArtifactInfo &item : currentHistoryArtifacts) {
        if (item.artifactId == result.artifactId) {
            artifact = &item;
            break;
        }
    }

    if (!artifact) {
        QMessageBox::information(this,
                                 QStringLiteral("产物预览"),
                                 QStringLiteral("产物列表已刷新，当前预览对象不存在。请重新选择产物。"));
        return;
    }

    showHistoryArtifactPreviewDialog(*artifact, historyArtifactPreviewText(*artifact, result));
}

void MainWindow::handleTaskArtifactPreviewFailed(
    const QString &taskId,
    const QString &artifactId,
    const QString &message)
{
    const bool matchesPending = taskId == pendingHistoryArtifactPreviewTaskId
        && artifactId == pendingHistoryArtifactPreviewId;
    if (!matchesPending) {
        return;
    }

    historyArtifactPreviewInProgress = false;
    pendingHistoryArtifactPreviewTaskId.clear();
    pendingHistoryArtifactPreviewId.clear();
    updateHistoryArtifactActionState();

    if (taskId != currentHistoryTaskId) {
        return;
    }

    QMessageBox::warning(this,
                         QStringLiteral("产物预览失败"),
                         message.isEmpty() ? QStringLiteral("后端没有返回具体错误。") : message);
}

void MainWindow::handleTaskToolCallsReceived(const WorkflowToolCallListResult &result)
{
    if (result.taskId.isEmpty() || result.taskId != currentHistoryTaskId) {
        return;
    }

    currentHistoryToolCalls = result.toolCalls;
    currentHistoryToolCallsLoaded = true;
    currentHistoryToolCallsError.clear();
    refreshHistoryDetailPanel();
}

void MainWindow::handleTaskToolCallsFailed(const QString &message)
{
    if (currentHistoryTaskId.isEmpty()) {
        return;
    }

    currentHistoryToolCallsLoaded = true;
    currentHistoryToolCallsError = message;
    refreshHistoryDetailPanel();
}

void MainWindow::handleTaskUpdatesReceived(const WorkflowTaskUpdateListResult &result)
{
    if (!result.taskId.isEmpty() && result.taskId == currentDispatchTaskId) {
        applyDispatchTaskUpdates(result);
        requestCurrentDispatchDeliveryCardIfTerminal();
        if (shouldPollCurrentDispatchUpdates()) {
            scheduleDispatchUpdatesRefresh(DispatchUpdatesPollIntervalMs);
        } else if (dispatchUpdateRefreshTimer) {
            dispatchUpdateRefreshTimer->stop();
        }
    }

    // updates 是多个后端事实的聚合视图，返回可能晚于用户切换任务；必须先核对 task_id。
    if (result.taskId.isEmpty() || result.taskId != currentHistoryTaskId) {
        return;
    }

    currentHistoryUpdates = result.updates;
    currentHistoryUpdatesLoaded = true;
    currentHistoryUpdatesError.clear();
    refreshHistoryDetailPanel();
}

void MainWindow::handleTaskUpdatesFailed(const QString &taskId, const QString &message)
{
    if (!taskId.isEmpty() && taskId == currentDispatchTaskId && shouldPollCurrentDispatchUpdates()) {
        // WebSocket 断开或短暂网络错误时，HTTP updates 继续承担低频兜底。
        scheduleDispatchUpdatesRefresh(DispatchUpdatesRetryIntervalMs);
    }

    if (!taskId.isEmpty() && taskId == currentHistoryTaskId) {
        currentHistoryUpdatesLoaded = true;
        currentHistoryUpdatesError = message;
        refreshHistoryDetailPanel();
    }
}

void MainWindow::handleTaskLogsReceived(const TaskLogListResult &result)
{
    // WebSocket 或 HTTP 日志可能比用户切换行更晚返回，先核对 task_id，避免串页。
    if (result.taskId.isEmpty() || result.taskId != currentHistoryTaskId) {
        return;
    }

    currentHistoryEvents = result.events;
    currentHistoryLogsLoaded = true;
    currentHistoryLogsError.clear();
    refreshHistoryDetailPanel();

    if (currentHistoryRequiresConfirmation && currentHistoryPermissions.isEmpty()) {
        updateHistoryConfirmationPanelFromLogs(result.events);
    }

    historySelectionTitle->setText(currentHistorySummary.isEmpty() ? currentHistoryTaskId : currentHistorySummary);
    historySelectionMeta->setText(
        QStringLiteral("任务 ID：%1 · 模式：%2 · 更新：%3")
            .arg(currentHistoryTaskId, historyModeText(currentHistoryMode), currentHistoryUpdatedAt));
    refreshHistorySelectionBadge();
}

void MainWindow::handleTaskLogsFailed(const QString &message)
{
    if (currentHistoryTaskId.isEmpty()) {
        return;
    }

    currentHistoryLogsLoaded = true;
    currentHistoryLogsError = message;
    refreshHistoryDetailPanel();
}

void MainWindow::handleTaskPermissionsReceived(const RuntimePermissionListResult &result)
{
    if (result.taskId.isEmpty() || result.taskId != currentHistoryTaskId) {
        return;
    }

    currentHistoryPermissions = result.permissions;
    currentHistoryPermissionsLoaded = true;
    currentHistoryPermissionsError.clear();
    currentHistoryConfirmationAcknowledged = false;
    updateHistoryConfirmationPanel(currentHistoryPermissions);
}

void MainWindow::handleTaskPermissionsFailed(const QString &message)
{
    if (currentHistoryTaskId.isEmpty()) {
        return;
    }

    currentHistoryPermissionsLoaded = true;
    currentHistoryPermissionsError = message;
    if (historyConfirmationSection) {
        historyConfirmationSection->setVisible(currentHistoryRequiresConfirmation);
    }
    if (historyConfirmationMeta) {
        historyConfirmationMeta->setText(QStringLiteral("权限审计读取失败：%1").arg(message));
    }
    if (historyConfirmationBadge) {
        polishBadge(historyConfirmationBadge, QStringLiteral("badgeOrange"));
        historyConfirmationBadge->setText(QStringLiteral("失败"));
    }
    if (historyConfirmationText) {
        historyConfirmationText->setHtml(
            QStringLiteral("<p style=\"color:#DC2626;\">权限请求加载失败：%1</p>").arg(message.toHtmlEscaped()));
    }
    if (historyConfirmButton) {
        historyConfirmButton->setEnabled(false);
    }
    refreshHistorySelectionBadge();
}

void MainWindow::handleTaskPermissionDecisionCompleted(const RuntimePermissionItem &item)
{
    if (item.request.taskId.isEmpty() || item.request.taskId != currentHistoryTaskId) {
        return;
    }

    if (!pendingPermissionApprovalQueue.isEmpty() && pendingPermissionApprovalQueue.first() == item.request.requestId) {
        pendingPermissionApprovalQueue.removeFirst();
    } else {
        pendingPermissionApprovalQueue.removeAll(item.request.requestId);
    }

    for (RuntimePermissionItem &existing : currentHistoryPermissions) {
        if (existing.request.requestId == item.request.requestId) {
            existing = item;
            break;
        }
    }

    if (!pendingPermissionApprovalQueue.isEmpty()) {
        approveNextHistoryPermission();
        return;
    }

    historyPermissionApprovalInProgress = false;
    currentHistoryConfirmationAcknowledged = true;
    updateHistoryConfirmationPanel(currentHistoryPermissions);

    backendClient->requestTaskPermissions(currentHistoryTaskId);
}

void MainWindow::handleTaskPermissionDecisionFailed(const QString &message)
{
    if (currentHistoryTaskId.isEmpty()) {
        return;
    }

    historyPermissionApprovalInProgress = false;
    pendingPermissionApprovalQueue.clear();
    if (historyConfirmButton) {
        historyConfirmButton->setEnabled(true);
        historyConfirmButton->setText(QStringLiteral("确认已阅"));
    }
    if (historyConfirmationMeta) {
        historyConfirmationMeta->setText(QStringLiteral("写入权限审计失败：%1").arg(message));
    }
    if (historyConfirmationText) {
        historyConfirmationText->setHtml(
            QStringLiteral("<p style=\"color:#DC2626;\">权限确认写入失败：%1</p>").arg(message.toHtmlEscaped()));
    }
    refreshHistorySelectionBadge();
    backendClient->requestTaskPermissions(currentHistoryTaskId);
}

void MainWindow::handleTaskControlCompleted(const TaskControlResult &result)
{
    if (result.taskId.isEmpty()) {
        handleTaskControlFailed(QStringLiteral("任务控制响应缺少 task_id。"));
        return;
    }

    const QString actionLabel = result.action == QStringLiteral("retry")
        ? QStringLiteral("重试")
        : (result.action == QStringLiteral("pause") ? QStringLiteral("暂停")
                                                     : QStringLiteral("取消"));
    const QString message = result.message.isEmpty()
                                ? QStringLiteral("%1 请求已完成。").arg(actionLabel)
                                : result.message;

    const bool cancelledActiveDataExport = result.action == QStringLiteral("cancel")
        && result.accepted
        && result.status == QStringLiteral("cancelled")
        && dataWorkbookExportLoading
        && result.taskId == activeDataWorkbookExportTaskId;
    if (cancelledActiveDataExport) {
        // 控制接口先于轮询响应到达时，工作台也要即时恢复，不能让用户在“导出中”状态等待
        // 一个已取消任务的后台线程自然返回。
        handleDataAnalysisWorkbookExportCancelled(message);
    }
    const bool cancelledActiveDataChart = result.action == QStringLiteral("cancel")
        && result.accepted
        && result.status == QStringLiteral("cancelled")
        && dataChartExportLoading
        && result.taskId == activeDataChartExportTaskId;
    if (cancelledActiveDataChart) {
        // 图表渲染与 Excel 一样是后台协作式取消；控制响应先到时也要立即恢复工作台。
        handleDataChartExportCancelled(message);
    }
    const bool cancelledActiveDataTransformation = result.action == QStringLiteral("cancel")
        && result.accepted
        && result.status == QStringLiteral("cancelled")
        && dataTransformationExportLoading
        && result.taskId == activeDataTransformationTaskId;
    if (cancelledActiveDataTransformation) {
        handleDataTransformationExportCancelled(message);
    }

    if (historyDetailText) {
        historyDetailText->setHtml(
            QStringLiteral("<p style=\"color:#2563EB;\"><b>%1请求已提交</b></p>"
                           "<p style=\"color:#64748B;\">%2</p>")
                .arg(actionLabel.toHtmlEscaped(), message.toHtmlEscaped()));
    }

    if (result.action == QStringLiteral("retry") && !result.newTaskId.isEmpty()) {
        pendingHistoryFocusTaskId = result.newTaskId;
        historyOffset = 0;
    } else {
        pendingHistoryFocusTaskId = result.taskId;
    }

    refreshTaskHistory();
}

void MainWindow::handleTaskControlFailed(const QString &message)
{
    if (historyDetailText) {
        historyDetailText->setHtml(
            QStringLiteral("<p style=\"color:#DC2626;\"><b>任务控制失败：</b>%1</p>")
                .arg(message.toHtmlEscaped()));
    }
    updateHistoryActionButtons();
}

void MainWindow::handleTaskExecutionCompleted(const WorkflowExecutionResult &result)
{
    if (result.runtimeTaskId.isEmpty()) {
        handleTaskExecutionFailed(QStringLiteral("执行响应缺少 runtime_task_id。"));
        return;
    }

    const bool fromDispatch = currentDispatchExecutionInProgress
        || (!currentDispatchTaskId.isEmpty() && result.sourceTaskId == currentDispatchTaskId);
    const QString message = result.message.isEmpty()
                                ? QStringLiteral("执行请求已完成。")
                                : result.message;
    if (!result.accepted) {
        // HTTP 成功只表示后端理解了请求；例如仍在等待权限、任务已在后台运行时，Runtime 会明确
        // 拒绝重复入队。此时不能把它显示成“已经提交”，否则客户会误以为又启动了一次任务。
        if (fromDispatch) {
            currentDispatchExecutionInProgress = false;
            currentDispatchRuntimeStatus = result.status;
            ui->dispatchChatStatus->setText(dispatchStatusTextForState(
                currentDispatchRuntimeMode.isEmpty() ? QStringLiteral("runtime") : currentDispatchRuntimeMode,
                result.status));
            if (currentDispatchDirectKnowledgeAnswer) {
                currentDispatchKnowledgeAnswerFailed = true;
                ui->dispatchChatStatus->setText(QStringLiteral("检索未启动"));
                ui->summaryVal3->setText(QStringLiteral("请在历史中确认运行原因"));
                setDispatchActivityRunning(false);
                appendConversationHtml(
                    QStringLiteral("<hr/><h3>AI 调度台</h3>"
                                   "<p style=\"color:#B45309;\"><b>这次检索没有成功启动。</b></p>"
                                   "<p>资料没有被修改。请在“查看历史”中确认资料库索引和运行状态后重试。</p>"));
            } else if (currentDispatchDirectDataAnalysis) {
                currentDispatchDataAnalysisFailed = true;
                ui->dispatchChatStatus->setText(QStringLiteral("分析未启动"));
                ui->summaryVal3->setText(QStringLiteral("请在历史中确认运行原因"));
                setDispatchActivityRunning(false);
                appendConversationHtml(
                    QStringLiteral("<hr/><h3>AI 调度台</h3>"
                                   "<p style=\"color:#B45309;\"><b>这次数据分析没有成功启动。</b></p>"
                                   "<p>数据没有被修改。请在“查看历史”中确认文件状态后重试。</p>"));
            } else {
                appendConversationHtml(
                    QStringLiteral("<p style=\"color:#B45309;\"><b>系统</b> · %1</p>")
                        .arg(message.toHtmlEscaped()));
            }
            updateDispatchActionButtons();
        }
        if (historyRuntimeMeta) {
            historyRuntimeMeta->setText(message);
        }
        if (historyDetailText) {
            historyDetailText->setHtml(
                QStringLiteral("<p style=\"color:#B45309;\"><b>未重复提交任务</b></p><p>%1</p>")
                    .arg(message.toHtmlEscaped()));
        }
        refreshTaskHistory();
        return;
    }
    if (fromDispatch) {
        currentDispatchExecutionInProgress = false;
        currentDispatchExecutionSubmitted = true;
        currentDispatchNeedsClarification = false;
        currentDispatchTaskId = result.runtimeTaskId;
        resetDispatchDeliveryCard();
        currentDispatchUpdateWatermark = 0;
        currentDispatchUpdates.clear();
        currentDispatchRuntimeMode = QStringLiteral("runtime");
        currentDispatchRuntimeStatus = result.status;
        currentDispatchHasPendingPermission = false;
        currentDispatchArtifactCount = 0;
        const QString dispatchStatus = dispatchStatusTextForState(
            currentDispatchRuntimeMode,
            currentDispatchRuntimeStatus);
        ui->dispatchChatStatus->setText(dispatchStatus);
        ui->summaryVal0->setText(result.runtimeTaskId);
        ui->summaryVal3->setText(currentDispatchDirectKnowledgeAnswer
                                     ? QStringLiteral("正在读取已选资料库")
                                     : (currentDispatchDirectDataAnalysis
                                            ? QStringLiteral("正在分析已选数据")
                                            : QStringLiteral("已转入 Runtime")));
        setProgressStep(5,
                        currentDispatchDirectKnowledgeAnswer
                            ? QStringLiteral("5 当前结论 · 正在读取并核验资料库来源")
                            : (currentDispatchDirectDataAnalysis
                                   ? QStringLiteral("5 当前结论 · 正在分析已选数据")
                                   : QStringLiteral("5 当前结论 · 已提交 Runtime：%1").arg(dispatchStatus)),
                        QStringLiteral("badgeBlue"));
        if (isCurrentDispatchAutoReadOnlyTask()) {
            ui->dispatchChatStatus->setText(currentDispatchAutoReadOnlyActivityText());
            setDispatchActivityRunning(true);
        } else {
            appendConversationHtml(
                QStringLiteral("<p style=\"color:#2563EB;\"><b>系统</b> · %1。Runtime 任务：%2</p>")
                    .arg(message.toHtmlEscaped(), result.runtimeTaskId.toHtmlEscaped()));
        }
        backendClient->connectTaskLog(result.runtimeTaskId);
        backendClient->requestTaskDeliveryCard(result.runtimeTaskId);
        refreshCurrentDispatchUpdates();
        updateDispatchActionButtons();
    }

    if (historyRuntimeMeta) {
        historyRuntimeMeta->setText(QStringLiteral("%1 · 当前状态：%2")
                                        .arg(message, historyStatusText(result.status)));
    }
    if (historyDetailText) {
        historyDetailText->setHtml(
            QStringLiteral("<p style=\"color:#2563EB;\"><b>执行请求已提交</b></p>"
                           "<p style=\"color:#64748B;\">%1</p>"
                           "<p style=\"color:#64748B;\">Runtime 任务：%2</p>")
                .arg(message.toHtmlEscaped(), result.runtimeTaskId.toHtmlEscaped()));
    }

    pendingHistoryFocusTaskId = result.runtimeTaskId;
    if (result.runtimeTaskId != currentHistoryTaskId) {
        historyOffset = 0;
    }
    if (fromDispatch && !isCurrentDispatchAutoReadOnlyTask()) {
        switchPage(12);
    } else {
        refreshTaskHistory();
    }
}

void MainWindow::handleTaskExecutionFailed(const QString &message)
{
    if (currentDispatchExecutionInProgress) {
        currentDispatchExecutionInProgress = false;
        currentDispatchExecutionSubmitted = false;
        currentDispatchRuntimeMode.clear();
        currentDispatchRuntimeStatus.clear();
        currentDispatchHasPendingPermission = false;
        currentDispatchArtifactCount = 0;
        if (currentDispatchDirectKnowledgeAnswer) {
            currentDispatchKnowledgeAnswerFailed = true;
            ui->dispatchChatStatus->setText(QStringLiteral("检索未完成"));
            ui->summaryVal3->setText(QStringLiteral("请检查资料库后重试"));
            setProgressStep(5, QStringLiteral("5 当前结论 · 检索请求未完成"), QStringLiteral("badgeOrange"));
            setDispatchActivityRunning(false);
            appendConversationHtml(
                QStringLiteral("<hr/><h3>AI 调度台</h3>"
                               "<p style=\"color:#B45309;\"><b>这次资料库检索没有完成。</b></p>"
                               "<p>资料没有被修改。请确认资料库已经索引完成，再从当前问题重新开始。</p>"));
        } else if (currentDispatchDirectDataAnalysis) {
            currentDispatchDataAnalysisFailed = true;
            ui->dispatchChatStatus->setText(QStringLiteral("分析未启动"));
            ui->summaryVal3->setText(QStringLiteral("请检查数据文件后重试"));
            setProgressStep(5, QStringLiteral("5 当前结论 · 分析请求未完成"), QStringLiteral("badgeOrange"));
            setDispatchActivityRunning(false);
            appendConversationHtml(
                QStringLiteral("<hr/><h3>AI 调度台</h3>"
                               "<p style=\"color:#B45309;\"><b>这次数据分析没有启动。</b></p>"
                               "<p>源数据没有被修改。请确认数据文件可用后重新发送。</p>"));
        } else {
            ui->dispatchChatStatus->setText(QStringLiteral("执行失败"));
            setProgressStep(5, QStringLiteral("5 当前结论 · 执行请求失败"), QStringLiteral("badgeOrange"));
            appendConversationHtml(
                QStringLiteral("<p style=\"color:#DC2626;\"><b>系统</b> · 执行请求失败：%1</p>")
                    .arg(message.toHtmlEscaped()));
        }
        updateDispatchActionButtons();
    }
    if (historyRuntimeMeta) {
        historyRuntimeMeta->setText(QStringLiteral("执行请求失败：%1").arg(message));
    }
    if (historyDetailText) {
        historyDetailText->setHtml(
            QStringLiteral("<p style=\"color:#DC2626;\"><b>执行请求失败：</b>%1</p>")
                .arg(message.toHtmlEscaped()));
    }
    updateHistoryActionButtons();
}

void MainWindow::onHistoryFilterChanged()
{
    historyOffset = 0;
    refreshTaskHistory();
}

void MainWindow::onHistoryRowSelectionChanged()
{
    if (!historyTable) {
        return;
    }

    const QList<QTableWidgetItem *> selectedItems = historyTable->selectedItems();
    if (selectedItems.isEmpty()) {
        return;
    }

    const int row = selectedItems.first()->row();
    if (row < 0 || historyTable->isRowHidden(row)) {
        return;
    }

    QTableWidgetItem *taskIdItem = historyTable->item(row, 1);
    QTableWidgetItem *summaryItem = historyTable->item(row, 2);
    QTableWidgetItem *statusItem = historyTable->item(row, 0);
    QTableWidgetItem *riskItem = historyTable->item(row, 3);
    QTableWidgetItem *confirmItem = historyTable->item(row, 4);
    QTableWidgetItem *stepItem = historyTable->item(row, 5);
    QTableWidgetItem *updatedItem = historyTable->item(row, 6);
    QTableWidgetItem *modeItem = historyTable->item(row, 7);

    currentHistoryTaskId = taskIdItem ? taskIdItem->text() : QString();
    currentHistorySummary = summaryItem ? summaryItem->text() : QString();
    currentHistoryStatus = statusItem ? statusItem->data(Qt::UserRole).toString() : QString();
    currentHistoryRiskLevel = riskItem ? riskItem->data(Qt::UserRole).toString() : QString();
    currentHistoryMode = modeItem ? modeItem->data(Qt::UserRole).toString() : QString();
    currentHistoryRequiresConfirmation = confirmItem ? confirmItem->data(Qt::UserRole).toBool() : false;
    currentHistoryStepCount = stepItem ? stepItem->data(Qt::UserRole).toInt() : 0;
    currentHistoryUpdatedAt = updatedItem ? updatedItem->data(Qt::UserRole).toString() : QString();
    currentHistoryPlanSummary = WorkflowPlanSummaryInfo{};
    currentHistoryPlanSteps.clear();
    currentHistorySteps.clear();
    currentHistoryEvents.clear();
    currentHistoryPermissions.clear();
    currentHistoryRuntimeState = WorkflowRuntimeStateInfo{};
    currentHistoryMetrics = WorkflowRuntimeMetricsResult{};
    currentHistoryModelRoutes.clear();
    currentHistoryEvaluation = WorkflowTaskEvaluationResult{};
    currentHistoryArtifacts.clear();
    currentHistoryToolCalls.clear();
    currentHistoryUpdates.clear();
    currentHistoryArtifactId.clear();
    pendingHistoryArtifactPreviewTaskId.clear();
    pendingHistoryArtifactPreviewId.clear();
    pendingPermissionApprovalQueue.clear();
    historyPermissionApprovalInProgress = false;
    historyArtifactPreviewInProgress = false;
    currentHistoryPlanLoaded = false;
    currentHistoryStepsLoaded = false;
    currentHistoryLogsLoaded = false;
    currentHistoryPermissionsLoaded = false;
    currentHistoryRuntimeStateLoaded = false;
    currentHistoryMetricsLoaded = false;
    currentHistoryModelRoutesLoaded = false;
    currentHistoryEvaluationLoaded = false;
    currentHistoryArtifactsLoaded = false;
    currentHistoryToolCallsLoaded = false;
    currentHistoryUpdatesLoaded = false;
    currentHistoryPlanError.clear();
    currentHistoryStepsError.clear();
    currentHistoryLogsError.clear();
    currentHistoryPermissionsError.clear();
    currentHistoryRuntimeStateError.clear();
    currentHistoryMetricsError.clear();
    currentHistoryModelRoutesError.clear();
    currentHistoryEvaluationError.clear();
    currentHistoryArtifactsError.clear();
    currentHistoryToolCallsError.clear();
    currentHistoryUpdatesError.clear();
    updateHistoryModelRoutesButton();
    refreshHistoryArtifactToolbar();

    // 日志是按需加载的，只有用户点中某条任务时才拉对应 task_id 的历史日志。
    historySelectionTitle->setText(currentHistorySummary.isEmpty() ? currentHistoryTaskId : currentHistorySummary);
    historySelectionMeta->setText(
        QStringLiteral("任务 ID：%1 · 状态：%2 · 模式：%3 · 更新：%4")
            .arg(currentHistoryTaskId,
                 historyStatusText(currentHistoryStatus),
                 historyModeText(currentHistoryMode),
                 currentHistoryUpdatedAt));
    refreshHistorySelectionBadge();

    refreshHistoryDetailPanel();
    updateHistoryRuntimePanel();
    showHistoryConfirmationLoading();
    backendClient->requestTaskPlan(currentHistoryTaskId);
    refreshCurrentHistoryDetails();
    updateHistoryActionButtons();
    updateHistoryAutoRefreshState();
}

void MainWindow::applyHistoryKeywordFilter()
{
    if (!historyTable) {
        return;
    }

    const QString keyword = ui->historyInput->text().trimmed();
    const bool hasKeyword = !keyword.isEmpty();
    int visibleCount = 0;

    for (int row = 0; row < historyTable->rowCount(); ++row) {
        QString haystack;
        for (int column = 0; column < historyTable->columnCount(); ++column) {
            QTableWidgetItem *item = historyTable->item(row, column);
            if (item) {
                haystack += item->text();
                haystack += QLatin1Char(' ');
            }
        }

        const bool visible = !hasKeyword || haystack.contains(keyword, Qt::CaseInsensitive);
        historyTable->setRowHidden(row, !visible);
        if (visible) {
            ++visibleCount;
        }
    }

    if (historyCountLabel) {
        historyCountLabel->setText(
            QStringLiteral("共 %1 条 · 当前页显示 %2 条").arg(historyTotal).arg(visibleCount));
    }

    if (visibleCount == 0) {
        showHistoryEmptyState(
            hasKeyword ? QStringLiteral("当前关键词没有匹配到任务。") : QStringLiteral("当前筛选没有任务。"));
        return;
    }

    if (historyTable->currentRow() >= 0 && !historyTable->isRowHidden(historyTable->currentRow())) {
        return;
    }

    for (int row = 0; row < historyTable->rowCount(); ++row) {
        if (!historyTable->isRowHidden(row)) {
            historyTable->selectRow(row);
            break;
        }
    }
}

void MainWindow::updateHistoryPagerControls()
{
    const bool hasHistory = historyTotal > 0;
    if (historyPrevButton) {
        historyPrevButton->setEnabled(hasHistory && historyOffset > 0);
    }
    if (historyNextButton) {
        historyNextButton->setEnabled(hasHistory && historyOffset + historyLimit < historyTotal);
    }
}

bool MainWindow::selectHistoryRowByTaskId(const QString &taskId)
{
    if (!historyTable || taskId.isEmpty()) {
        return false;
    }

    for (int row = 0; row < historyTable->rowCount(); ++row) {
        if (historyTable->isRowHidden(row)) {
            continue;
        }

        QTableWidgetItem *taskIdItem = historyTable->item(row, 1);
        if (taskIdItem && taskIdItem->text() == taskId) {
            historyTable->selectRow(row);
            return true;
        }
    }

    return false;
}

bool MainWindow::confirmHistoryExecutionRequest()
{
    const QString effectiveStatus = (currentHistoryRuntimeStateLoaded
                                     && currentHistoryRuntimeStateError.isEmpty()
                                     && !currentHistoryRuntimeState.status.isEmpty())
                                        ? currentHistoryRuntimeState.status
                                        : currentHistoryStatus;
    const QString effectiveMode = (currentHistoryRuntimeStateLoaded
                                   && currentHistoryRuntimeStateError.isEmpty()
                                   && !currentHistoryRuntimeState.mode.isEmpty())
                                      ? currentHistoryRuntimeState.mode
                                      : currentHistoryMode;

    if (effectiveMode != QStringLiteral("dry_run")) {
        return true;
    }

    const QString title = currentHistorySummary.isEmpty()
        ? currentHistoryTaskId
        : currentHistorySummary.simplified();

    QStringList details;
    details.append(QStringLiteral("任务 ID：%1").arg(currentHistoryTaskId));
    details.append(QStringLiteral("摘要：%1").arg(title));
    details.append(QStringLiteral("当前模式：%1").arg(historyModeText(effectiveMode)));
    details.append(QStringLiteral("当前状态：%1").arg(historyStatusText(effectiveStatus)));
    details.append(QStringLiteral("最高风险：%1").arg(historyRiskText(currentHistoryRiskLevel)));
    details.append(QStringLiteral("需要权限确认：%1").arg(historyConfirmationLabelText(currentHistoryRequiresConfirmation)));
    if (!currentHistoryPlanSummary.planId.isEmpty() || !currentHistoryPlanSummary.summary.isEmpty()) {
        details.append(
            QStringLiteral("本次权限策略：%1")
                .arg(runtimePermissionPolicyText(
                         currentHistoryPlanSummary.preferences.permissionPolicy)));
    }

    if (currentHistoryRequiresConfirmation) {
        if (!currentHistoryPermissionsLoaded) {
            details.append(QStringLiteral("权限请求：尚在加载；真实执行遇到敏感步骤时仍会等待确认。"));
        } else if (currentHistoryPermissions.isEmpty()) {
            details.append(QStringLiteral("权限请求：后端当前未返回具体请求；真实执行遇到敏感步骤时仍会等待确认。"));
        } else {
            details.append(QStringLiteral("权限请求：%1 项").arg(currentHistoryPermissions.size()));
            const int shownCount = qMin(3, currentHistoryPermissions.size());
            for (int i = 0; i < shownCount; ++i) {
                const RuntimePermissionItem &item = currentHistoryPermissions.at(i);
                const QString permissionSummary = item.request.summary.isEmpty()
                    ? item.request.permissions.join(QStringLiteral("、"))
                    : item.request.summary.simplified();
                details.append(QStringLiteral("  - %1").arg(permissionSummary));
            }
            if (currentHistoryPermissions.size() > shownCount) {
                details.append(QStringLiteral("  - 其余 %1 项可在权限确认区查看。")
                                   .arg(currentHistoryPermissions.size() - shownCount));
            }
        }
    }

    QMessageBox box(this);
    box.setIcon(QMessageBox::Warning);
    box.setWindowTitle(QStringLiteral("开始真实执行"));
    box.setText(QStringLiteral("即将把 dry-run 任务转入真实 runtime 执行。"));
    box.setInformativeText(
        QStringLiteral("当前只运行安全内置工具，文件产物写入受控 outputs 目录；敏感步骤会按本次计划的权限策略自动批准、等待确认或由平台阻止。"));
    box.setDetailedText(details.join(QStringLiteral("\n")));
    QPushButton *startButton = box.addButton(QStringLiteral("开始执行"), QMessageBox::YesRole);
    QPushButton *cancelButton = box.addButton(QStringLiteral("先不执行"), QMessageBox::RejectRole);
    box.setDefaultButton(cancelButton);
    box.setEscapeButton(cancelButton);
    box.exec();

    return box.clickedButton() == startButton;
}

void MainWindow::updateHistoryActionButtons()
{
    const bool hasSelection = !currentHistoryTaskId.isEmpty();
    const QString effectiveStatus = (currentHistoryRuntimeStateLoaded
                                     && currentHistoryRuntimeStateError.isEmpty()
                                     && !currentHistoryRuntimeState.status.isEmpty())
                                        ? currentHistoryRuntimeState.status
                                        : currentHistoryStatus;
    const QString effectiveMode = (currentHistoryRuntimeStateLoaded
                                   && currentHistoryRuntimeStateError.isEmpty()
                                   && !currentHistoryRuntimeState.mode.isEmpty())
                                      ? currentHistoryRuntimeState.mode
                                      : currentHistoryMode;
    const bool canStartRuntime = effectiveMode == QStringLiteral("dry_run")
        && effectiveStatus == QStringLiteral("completed");
    const bool canContinueRuntime = effectiveMode == QStringLiteral("runtime")
        && (effectiveStatus == QStringLiteral("waiting_permission")
            || effectiveStatus == QStringLiteral("paused"));
    const bool canExecute = hasSelection && (canStartRuntime || canContinueRuntime);
    const bool canPause = hasSelection
        && effectiveMode == QStringLiteral("runtime")
        && (effectiveStatus == QStringLiteral("pending")
            || effectiveStatus == QStringLiteral("running")
            || effectiveStatus == QStringLiteral("waiting_permission"));
    const bool canCancel = hasSelection
        && (effectiveStatus == QStringLiteral("pending")
            || effectiveStatus == QStringLiteral("running")
            || effectiveStatus == QStringLiteral("waiting_permission"));
    const bool canRetry = hasSelection
        && !currentHistoryTaskId.startsWith(QStringLiteral("task_data_"))
        && (effectiveStatus == QStringLiteral("completed")
            || effectiveStatus == QStringLiteral("failed")
            || effectiveStatus == QStringLiteral("blocked")
            || effectiveStatus == QStringLiteral("cancelled"));
    const bool canSaveTaskMemory = hasSelection
        && effectiveMode == QStringLiteral("runtime")
        && effectiveStatus == QStringLiteral("completed");
    QJsonObject restorableDocumentContext;
    if (hasSelection && currentHistoryStepsLoaded && currentHistoryStepsError.isEmpty()) {
        for (auto iterator = currentHistorySteps.crbegin(); iterator != currentHistorySteps.crend(); ++iterator) {
            if (iterator->agent != QStringLiteral("document_agent")
                || iterator->stepId != QStringLiteral("document_analysis")
                || iterator->status != QStringLiteral("completed")) {
                continue;
            }
            const QJsonObject context = historyDocumentContextFromStepOutput(iterator->output);
            if (!context.value(QStringLiteral("draft_title")).toString().trimmed().isEmpty()
                && !context.value(QStringLiteral("draft_sections")).toArray().isEmpty()) {
                restorableDocumentContext = context;
            }
            break;
        }
    }
    const bool canRestoreDocumentDraft = !restorableDocumentContext.isEmpty()
        && !documentAgentRunning
        && !documentDraftSaving;
    if (historyExecuteButton) {
        historyExecuteButton->setText(canContinueRuntime ? QStringLiteral("继续执行")
                                                         : QStringLiteral("开始执行"));
        QString executeToolTip;
        if (!hasSelection) {
            executeToolTip = QStringLiteral("先在左侧选择一条历史任务。");
        } else if (canStartRuntime) {
            executeToolTip = QStringLiteral("将已完成的 dry-run 转入真实 runtime 执行，执行前会再次确认。");
        } else if (canContinueRuntime) {
            executeToolTip = QStringLiteral("继续执行等待权限或已阻塞的 runtime 任务。");
        } else if (effectiveMode == QStringLiteral("dry_run")) {
            executeToolTip = QStringLiteral("只有已完成的 dry-run 才能开始真实执行。");
        } else {
            executeToolTip = QStringLiteral("当前任务状态不支持执行操作。");
        }
        historyExecuteButton->setToolTip(executeToolTip);
        historyExecuteButton->setEnabled(canExecute);
    }
    if (historyCancelButton) {
        historyCancelButton->setEnabled(canCancel);
    }
    if (historyPauseButton) {
        // “暂停”只在真实 Runtime 的可运行状态出现；结束、预演或已暂停任务不占用操作栏空间。
        historyPauseButton->setVisible(effectiveMode == QStringLiteral("runtime") && effectiveStatus != QStringLiteral("paused"));
        historyPauseButton->setEnabled(canPause);
        historyPauseButton->setToolTip(
            canPause ? QStringLiteral("当前安全步骤完成后暂停；不会丢失已完成步骤或权限审计。")
                     : QStringLiteral("当前任务不能暂停。"));
    }
    if (historyRetryButton) {
        historyRetryButton->setEnabled(canRetry);
        historyRetryButton->setToolTip(
            currentHistoryTaskId.startsWith(QStringLiteral("task_data_"))
                ? QStringLiteral("数据工作簿不从历史重放；请回数据工作台复核预览后重新确认导出。")
                : QStringLiteral("基于缓存计划重新生成 dry-run 任务"));
    }
    if (historyRestoreDocumentButton) {
        historyRestoreDocumentButton->setEnabled(canRestoreDocumentDraft);
        historyRestoreDocumentButton->setToolTip(
            canRestoreDocumentDraft
                ? QStringLiteral("从该历史草稿建立新的独立预览；不调用模型、不覆盖旧任务或文件")
                : currentHistoryStepsLoaded && currentHistoryStepsError.isEmpty()
                    ? QStringLiteral("当前任务不是已完成且带来源的文档草稿快照")
                    : QStringLiteral("正在读取任务步骤，确认是否存在可恢复的文档草稿"));
    }
    if (historyMemoryButton) {
        historyMemoryButton->setVisible(canSaveTaskMemory);
        historyMemoryButton->setEnabled(canSaveTaskMemory);
        historyMemoryButton->setToolTip(
            canSaveTaskMemory
                ? QStringLiteral("仅查看本次任务中客户明确表达的长期约束；保存前可编辑并确认。")
                : QStringLiteral("只有已完成的真实任务才可检查长期记忆候选。"));
    }
}

void MainWindow::restoreHistoryDocumentDraftPreview()
{
    if (currentHistoryTaskId.isEmpty() || documentAgentRunning) {
        return;
    }
    if (!currentHistoryStepsLoaded || !currentHistoryStepsError.isEmpty()) {
        QMessageBox::information(
            this,
            QStringLiteral("恢复草稿预览"),
            QStringLiteral("历史任务详情仍在加载，请稍候再试。"));
        return;
    }

    QJsonObject context;
    for (auto iterator = currentHistorySteps.crbegin(); iterator != currentHistorySteps.crend(); ++iterator) {
        if (iterator->agent != QStringLiteral("document_agent")
            || iterator->stepId != QStringLiteral("document_analysis")
            || iterator->status != QStringLiteral("completed")) {
            continue;
        }
        context = historyDocumentContextFromStepOutput(iterator->output);
        break;
    }
    if (context.value(QStringLiteral("draft_title")).toString().trimmed().isEmpty()
        || context.value(QStringLiteral("draft_sections")).toArray().isEmpty()) {
        QMessageBox::information(
            this,
            QStringLiteral("恢复草稿预览"),
            QStringLiteral("当前历史任务没有可恢复的已验证 Markdown 草稿。"));
        return;
    }

    const QJsonObject version = context.value(QStringLiteral("draft_version")).toObject();
    const QString versionLabel = version.value(QStringLiteral("label")).toString();
    const QMessageBox::StandardButton choice = QMessageBox::question(
        this,
        QStringLiteral("确认恢复历史草稿"),
        QStringLiteral("将从历史任务中的%1建立一份新的独立预览。\n\n"
                       "不会调用模型、不会读取材料、不会修改该历史任务，也不会覆盖任何已保存文件。"
                       "确认后仍需另存为新的 Markdown 文件。")
            .arg(versionLabel.isEmpty() ? QStringLiteral("草稿快照")
                                        : QStringLiteral("版本“%1”").arg(versionLabel)),
        QMessageBox::Yes | QMessageBox::No,
        QMessageBox::No);
    if (choice != QMessageBox::Yes) {
        return;
    }

    // 恢复任务的进度放回文档工作台展示，历史页仍保留源任务；完成后才把详情切换到新任务结果。
    showDocumentWorkbench();
    beginDocumentDraftRestorePreview(QStringLiteral("正在从历史草稿建立独立恢复预览"));
    backendClient->restoreDocumentDraftPreview(currentHistoryTaskId);
}

void MainWindow::updateHistoryRuntimePanel()
{
    if (!historyRuntimeBadge || !historyRuntimeMeta) {
        updateHistoryActionButtons();
        updateHistoryAutoRefreshState();
        return;
    }

    historyRuntimeBadge->setStyleSheet(QString());
    if (currentHistoryTaskId.isEmpty()) {
        polishBadge(historyRuntimeBadge, QStringLiteral("badgeGray"));
        historyRuntimeBadge->setText(QStringLiteral("未选择"));
        historyRuntimeMeta->setText(QStringLiteral("请选择一条任务查看运行态、产物和工具调用。"));
        historyRuntimeMeta->setToolTip(historyRuntimeMeta->text());
        updateHistoryActionButtons();
        updateHistoryAutoRefreshState();
        return;
    }

    if (!currentHistoryRuntimeStateError.isEmpty()) {
        polishBadge(historyRuntimeBadge, QStringLiteral("badgeOrange"));
        historyRuntimeBadge->setText(QStringLiteral("状态异常"));
        historyRuntimeMeta->setText(QStringLiteral("运行态读取失败：%1").arg(currentHistoryRuntimeStateError));
        historyRuntimeMeta->setToolTip(historyRuntimeMeta->text());
        updateHistoryActionButtons();
        updateHistoryAutoRefreshState();
        return;
    }

    if (!currentHistoryRuntimeStateLoaded) {
        polishBadge(historyRuntimeBadge, QStringLiteral("badgeBlue"));
        historyRuntimeBadge->setText(QStringLiteral("加载中"));
        historyRuntimeMeta->setText(QStringLiteral("正在读取运行态、指标、产物和工具审计。"));
        historyRuntimeMeta->setToolTip(QStringLiteral("正在读取运行态、运行指标、产物和工具调用审计。"));
        updateHistoryActionButtons();
        updateHistoryAutoRefreshState();
        return;
    }

    const QString status = currentHistoryRuntimeState.status.isEmpty()
                               ? currentHistoryStatus
                               : currentHistoryRuntimeState.status;
    const QString mode = currentHistoryRuntimeState.mode.isEmpty()
                             ? currentHistoryMode
                             : currentHistoryRuntimeState.mode;
    const QJsonObject delegation = currentHistoryDelegation();
    const QString delegatedTaskId = delegation.value(QStringLiteral("task_id")).toString();
    const QString delegatedStatus = delegation.value(QStringLiteral("status")).toString();
    const bool hasDelegation = !delegatedTaskId.isEmpty() && !delegatedStatus.isEmpty();
    const bool delegatedActive = delegatedStatus == QStringLiteral("queued")
        || delegatedStatus == QStringLiteral("pending") || delegatedStatus == QStringLiteral("running")
        || delegatedStatus == QStringLiteral("waiting_permission");
    const int mapCompleted = delegation.value(QStringLiteral("map_completed")).toInt();
    const int mapTotal = delegation.value(QStringLiteral("map_total")).toInt();
    const int reduceCompleted = delegation.value(QStringLiteral("reduce_completed")).toInt();
    const int reduceTotal = delegation.value(QStringLiteral("reduce_total")).toInt();
    QStringList actionLabels;
    for (const QString &action : currentHistoryRuntimeState.allowedActions) {
        if (action == QStringLiteral("cancel")) {
            actionLabels.append(QStringLiteral("取消"));
        } else if (action == QStringLiteral("retry")) {
            actionLabels.append(QStringLiteral("重试"));
        } else {
            actionLabels.append(action);
        }
    }
    QStringList nextStatusLabels;
    for (const QString &nextStatus : currentHistoryRuntimeState.allowedNextStatuses) {
        nextStatusLabels.append(historyStatusText(nextStatus));
    }
    const QString actionText = actionLabels.isEmpty() ? QStringLiteral("无") : actionLabels.join(QStringLiteral("、"));
    const QString nextText = nextStatusLabels.isEmpty() ? QStringLiteral("无") : nextStatusLabels.join(QStringLiteral("、"));
    const QString terminalText = currentHistoryRuntimeState.terminal ? QStringLiteral("终态") : QStringLiteral("非终态");
    const QString message = currentHistoryRuntimeState.message.isEmpty()
                                ? QStringLiteral("运行态已记录。")
                                : currentHistoryRuntimeState.message;
    const QString metricsText = formatHistoryMetricsSummaryText();
    const QString stageHint = historyRuntimeStageHint(mode, status, currentHistoryRuntimeState.terminal);

    polishBadge(historyRuntimeBadge, historyRuntimeBadgeObjectName(hasDelegation ? delegatedStatus : status));
    historyRuntimeBadge->setText(
        hasDelegation
            ? (delegatedActive ? QStringLiteral("子任务进行中")
                               : QStringLiteral("子任务：%1").arg(historyStatusText(delegatedStatus)))
            : historyStatusText(status));
    QStringList fullLines;
    fullLines.append(QStringLiteral("阶段提示：%1").arg(stageHint));
    fullLines.append(QStringLiteral("模式：%1 · 状态：%2")
                         .arg(historyModeText(mode), historyStatusText(status)));
    fullLines.append(QStringLiteral("%1 · 可用动作：%2 · 下一状态：%3 · %4")
                         .arg(terminalText, actionText, nextText, message));
    if (hasDelegation) {
        QStringList progressParts;
        if (mapTotal > 0) {
            progressParts.append(QStringLiteral("Map %1/%2").arg(mapCompleted).arg(mapTotal));
        }
        if (reduceTotal > 0) {
            progressParts.append(QStringLiteral("Reduce %1/%2").arg(reduceCompleted).arg(reduceTotal));
        }
        const QString childSummary = delegation.value(QStringLiteral("summary")).toString();
        fullLines.append(
            QStringLiteral("关联子任务：%1 · 状态：%2%3")
                .arg(delegatedTaskId,
                     historyStatusText(delegatedStatus),
                     progressParts.isEmpty() ? QString() : QStringLiteral(" · %1").arg(progressParts.join(QStringLiteral(" · ")))));
        if (!childSummary.isEmpty()) {
            fullLines.append(QStringLiteral("子任务摘要：%1").arg(childSummary));
        }
    }
    if (!currentHistoryMetricsError.isEmpty()) {
        fullLines.append(QStringLiteral("运行指标：读取失败 · %1").arg(currentHistoryMetricsError));
    } else if (!currentHistoryMetricsLoaded) {
        fullLines.append(QStringLiteral("运行指标：加载中"));
    } else if (!metricsText.isEmpty()) {
        fullLines.append(metricsText);
    }
    if (shouldAutoRefreshCurrentHistoryTask()) {
        fullLines.append(QStringLiteral("自动刷新：开启（当前任务轻量刷新）"));
    }

    QString compactMessage = message.simplified();
    if (compactMessage.size() > 44) {
        compactMessage = compactMessage.left(44).trimmed() + QStringLiteral("…");
    }

    QString metaText = QStringLiteral("%1 · %2 · 动作：%3")
                           .arg(stageHint, terminalText, actionText);
    if (hasDelegation) {
        QString delegationMeta = QStringLiteral("子任务：%1").arg(historyStatusText(delegatedStatus));
        if (mapTotal > 0) {
            delegationMeta += QStringLiteral(" · Map %1/%2").arg(mapCompleted).arg(mapTotal);
        }
        if (reduceTotal > 0) {
            delegationMeta += QStringLiteral(" · Reduce %1/%2").arg(reduceCompleted).arg(reduceTotal);
        }
        metaText += QStringLiteral(" · %1").arg(delegationMeta);
    }
    if (!compactMessage.isEmpty() && compactMessage != stageHint) {
        metaText += QStringLiteral(" · %1").arg(compactMessage);
    }
    if (!currentHistoryMetricsError.isEmpty()) {
        metaText += QStringLiteral(" · 指标读取失败");
    } else if (!currentHistoryMetricsLoaded) {
        metaText += QStringLiteral(" · 指标加载中");
    } else if (!metricsText.isEmpty()) {
        metaText += QStringLiteral(" · %1").arg(metricsText.simplified());
    }
    if (shouldAutoRefreshCurrentHistoryTask()) {
        metaText += QStringLiteral(" · 自动刷新");
    }
    historyRuntimeMeta->setText(metaText);
    historyRuntimeMeta->setToolTip(fullLines.join(QStringLiteral("\n")));
    updateHistoryActionButtons();
    updateHistoryAutoRefreshState();
}

void MainWindow::refreshHistoryDetailPanel()
{
    if (!historyDetailText) {
        return;
    }

    if (currentHistoryTaskId.isEmpty()) {
        historyDetailText->setHtml(QStringLiteral("<p style=\"color:#64748B;\">%1</p>")
                                       .arg(QStringLiteral("请选择一条任务查看详情。")));
        return;
    }

    QString html = QStringLiteral("<p><b>任务摘要</b></p>");
    html += QStringLiteral("<p><b>任务 ID：</b>%1</p>").arg(currentHistoryTaskId.toHtmlEscaped());
    const int displayedStepCount = currentHistoryStepsLoaded ? currentHistorySteps.size() : currentHistoryStepCount;
    html += QStringLiteral("<p><b>状态：</b>%1 · <b>模式：</b>%2 · <b>风险：</b>%3 · <b>步骤：</b>%4 · <b>更新：</b>%5</p>")
                .arg(historyStatusText(currentHistoryStatus).toHtmlEscaped(),
                     historyModeText(currentHistoryMode).toHtmlEscaped(),
                     historyRiskText(currentHistoryRiskLevel).toHtmlEscaped(),
                     QString::number(displayedStepCount),
                     currentHistoryUpdatedAt.toHtmlEscaped());
    html += QStringLiteral("<p><b>摘要：</b>%1</p>").arg(currentHistorySummary.toHtmlEscaped());

    if (!currentHistoryModelRoutesError.isEmpty()) {
        html += QStringLiteral("<p style=\"color:#C2410C;\"><b>实际模型：</b>%1</p>")
                    .arg(QStringLiteral("模型审计加载失败：%1").arg(currentHistoryModelRoutesError).toHtmlEscaped());
    } else if (!currentHistoryModelRoutesLoaded) {
        html += QStringLiteral("<p style=\"color:#64748B;\"><b>实际模型：</b>正在读取本次执行快照……</p>");
    } else if (currentHistoryModelRoutes.isEmpty()) {
        html += QStringLiteral(
            "<p style=\"color:#64748B;\"><b>实际模型：</b>历史版本未记录实际模型路由。</p>");
    } else {
        html += QStringLiteral("<p><b>实际模型：</b>%1 <span style=\"color:#64748B;\">可点击标题旁图标查看明细。</span></p>")
                    .arg(historyModelRouteSummaryText().toHtmlEscaped());
    }

    if (currentHistoryRequiresConfirmation) {
        html += QStringLiteral(
            "<p style=\"color:#C2410C;\"><b>确认提醒：</b>该任务包含需要用户确认的敏感步骤，"
            "右侧权限确认区会展示审计记录。</p>");
    }

    html += QStringLiteral("<hr/><p><b>总指挥计划</b></p>");
    if (!currentHistoryPlanError.isEmpty()) {
        html += QStringLiteral("<p style=\"color:#DC2626;\">%1</p>")
                    .arg(QStringLiteral("总指挥计划加载失败：%1").arg(currentHistoryPlanError).toHtmlEscaped());
    } else if (!currentHistoryPlanLoaded) {
        html += QStringLiteral("<p style=\"color:#64748B;\">正在加载总指挥计划摘要……</p>");
    } else {
        html += formatHistoryPlanSummaryHtml();
    }

    html += QStringLiteral("<hr/><p><b>事件流</b></p>");
    if (!currentHistoryUpdatesError.isEmpty()) {
        html += QStringLiteral("<p style=\"color:#DC2626;\">%1</p>")
                    .arg(QStringLiteral("事件流加载失败：%1").arg(currentHistoryUpdatesError).toHtmlEscaped());
    } else if (!currentHistoryUpdatesLoaded) {
        html += QStringLiteral("<p style=\"color:#64748B;\">正在加载任务事件流……</p>");
    } else {
        html += formatHistoryUpdatesHtml();
    }

    html += QStringLiteral("<hr/><p><b>运行态快照</b></p>");
    if (!currentHistoryRuntimeStateError.isEmpty()) {
        html += QStringLiteral("<p style=\"color:#DC2626;\">%1</p>")
                    .arg(QStringLiteral("运行态加载失败：%1").arg(currentHistoryRuntimeStateError).toHtmlEscaped());
    } else if (!currentHistoryRuntimeStateLoaded) {
        html += QStringLiteral("<p style=\"color:#64748B;\">正在加载运行态快照……</p>");
    } else {
        html += formatHistoryRuntimeStateHtml();
    }

    html += QStringLiteral("<hr/><p><b>运行指标</b></p>");
    if (!currentHistoryMetricsError.isEmpty()) {
        html += QStringLiteral("<p style=\"color:#DC2626;\">%1</p>")
                    .arg(QStringLiteral("运行指标加载失败：%1").arg(currentHistoryMetricsError).toHtmlEscaped());
    } else if (!currentHistoryMetricsLoaded) {
        html += QStringLiteral("<p style=\"color:#64748B;\">正在加载执行预算和运行指标……</p>");
    } else {
        html += formatHistoryMetricsHtml();
    }

    html += QStringLiteral("<hr/><p><b>任务评估</b></p>");
    if (!currentHistoryEvaluationError.isEmpty()) {
        html += QStringLiteral("<p style=\"color:#DC2626;\">%1</p>")
                    .arg(QStringLiteral("任务评估加载失败：%1").arg(currentHistoryEvaluationError).toHtmlEscaped());
    } else if (!currentHistoryEvaluationLoaded) {
        html += QStringLiteral("<p style=\"color:#64748B;\">正在加载任务成功率和下一步建议……</p>");
    } else {
        html += formatHistoryEvaluationHtml();
    }

    html += QStringLiteral("<hr/><p><b>步骤概览</b></p>");
    if (!currentHistoryStepsError.isEmpty()) {
        html += QStringLiteral("<p style=\"color:#DC2626;\">%1</p>")
                    .arg(QStringLiteral("步骤加载失败：%1").arg(currentHistoryStepsError).toHtmlEscaped());
    } else if (!currentHistoryStepsLoaded) {
        html += QStringLiteral("<p style=\"color:#64748B;\">正在加载步骤结果……</p>");
    } else if (currentHistorySteps.isEmpty()) {
        html += QStringLiteral("<p style=\"color:#64748B;\">暂无 step 级结果。</p>");
    } else {
        html += formatHistoryStepsHtml();
    }

    html += QStringLiteral("<hr/><p><b>工具调用</b></p>");
    if (!currentHistoryToolCallsError.isEmpty()) {
        html += QStringLiteral("<p style=\"color:#DC2626;\">%1</p>")
                    .arg(QStringLiteral("工具调用加载失败：%1").arg(currentHistoryToolCallsError).toHtmlEscaped());
    } else if (!currentHistoryToolCallsLoaded) {
        html += QStringLiteral("<p style=\"color:#64748B;\">正在加载工具调用审计……</p>");
    } else {
        html += formatHistoryToolCallsHtml();
    }

    html += QStringLiteral("<hr/><p><b>产物</b></p>");
    if (!currentHistoryArtifactsError.isEmpty()) {
        html += QStringLiteral("<p style=\"color:#DC2626;\">%1</p>")
                    .arg(QStringLiteral("产物加载失败：%1").arg(currentHistoryArtifactsError).toHtmlEscaped());
    } else if (!currentHistoryArtifactsLoaded) {
        html += QStringLiteral("<p style=\"color:#64748B;\">正在加载产物目录……</p>");
    } else {
        html += formatHistoryArtifactsHtml();
    }

    html += QStringLiteral("<hr/><p><b>执行日志</b></p>");
    if (!currentHistoryLogsError.isEmpty()) {
        html += QStringLiteral("<p style=\"color:#DC2626;\">%1</p>")
                    .arg(QStringLiteral("日志加载失败：%1").arg(currentHistoryLogsError).toHtmlEscaped());
    } else if (!currentHistoryLogsLoaded) {
        html += QStringLiteral("<p style=\"color:#64748B;\">正在加载执行日志……</p>");
    } else if (currentHistoryEvents.isEmpty()) {
        html += QStringLiteral("<p>暂无日志。</p>");
    } else {
        for (const TaskLogEvent &event : currentHistoryEvents) {
            html += formatHistoryLogHtml(event);
        }
    }

    historyDetailText->setHtml(html);
}

void MainWindow::showHistoryConfirmationLoading()
{
    currentHistoryConfirmationAcknowledged = false;
    currentHistoryPermissions.clear();
    pendingPermissionApprovalQueue.clear();
    historyPermissionApprovalInProgress = false;
    currentHistoryPermissionsLoaded = false;
    currentHistoryPermissionsError.clear();
    if (!historyConfirmationSection) {
        return;
    }

    historyConfirmationSection->setVisible(currentHistoryRequiresConfirmation);
    setHistoryConfirmationExpanded(false);
    if (!currentHistoryRequiresConfirmation) {
        refreshHistorySelectionBadge();
        return;
    }

    polishBadge(historyConfirmationBadge, QStringLiteral("badgeOrange"));
    historyConfirmationBadge->setText(QStringLiteral("待确认"));
    historyConfirmationMeta->setText(QStringLiteral("读取权限审计中，默认折叠。"));
    historyConfirmationText->setHtml(QStringLiteral("<p style=\"color:#64748B;\">权限请求加载中。</p>"));
    historyConfirmButton->setEnabled(false);
    historyConfirmButton->setText(QStringLiteral("确认已阅"));
    refreshHistorySelectionBadge();
}

void MainWindow::updateHistoryConfirmationPanel(const QList<RuntimePermissionItem> &permissions)
{
    if (!historyConfirmationSection) {
        return;
    }

    currentHistoryPermissions = permissions;

    int pendingCount = 0;
    int approvedCount = 0;
    int deniedCount = 0;
    int policyApprovedCount = 0;
    for (const RuntimePermissionItem &item : currentHistoryPermissions) {
        if (item.decision.decision == QStringLiteral("approved")) {
            ++approvedCount;
            if (isPlatformPolicyDecision(item)) {
                ++policyApprovedCount;
            }
        } else if (item.decision.decision == QStringLiteral("denied")) {
            ++deniedCount;
        } else {
            ++pendingCount;
        }
    }

    const int total = currentHistoryPermissions.size();
    const bool shouldShow = currentHistoryRequiresConfirmation || total > 0;
    if (!shouldShow) {
        historyConfirmationSection->setVisible(false);
        refreshHistorySelectionBadge();
        return;
    }

    historyConfirmationSection->setVisible(true);
    setHistoryConfirmationExpanded(false);

    if (total == 0) {
        currentHistoryConfirmationAcknowledged = false;
        polishBadge(historyConfirmationBadge, QStringLiteral("badgeOrange"));
        historyConfirmationBadge->setText(QStringLiteral("待确认"));
        historyConfirmationMeta->setText(QStringLiteral("需要确认，暂未返回具体请求。"));
        historyConfirmationText->setHtml(
            QStringLiteral("<p style=\"color:#C2410C;\">请先查看任务日志。没有 request_id 时，前端不能写入权限决策审计。</p>"));
        historyConfirmButton->setEnabled(false);
        historyConfirmButton->setText(QStringLiteral("确认已阅"));
        refreshHistorySelectionBadge();
        return;
    }

    historyConfirmationText->setHtml(formatHistoryPermissionsHtml());

    if (historyPermissionApprovalInProgress) {
        polishBadge(historyConfirmationBadge, QStringLiteral("badgeOrange"));
        historyConfirmationBadge->setText(QStringLiteral("写入中"));
        historyConfirmationMeta->setText(
            QStringLiteral("写入审计中，剩余 %1 项。").arg(pendingPermissionApprovalQueue.size()));
        historyConfirmButton->setEnabled(false);
        historyConfirmButton->setText(QStringLiteral("写入中"));
        refreshHistorySelectionBadge();
        return;
    }

    if (pendingCount > 0) {
        currentHistoryConfirmationAcknowledged = false;
        polishBadge(historyConfirmationBadge, QStringLiteral("badgeOrange"));
        historyConfirmationBadge->setText(QStringLiteral("待确认"));
        historyConfirmationMeta->setText(
            QStringLiteral("%1 项权限，%2 项待确认。展开查看。").arg(total).arg(pendingCount));
        historyConfirmButton->setEnabled(true);
        historyConfirmButton->setText(QStringLiteral("确认已阅"));
    } else if (deniedCount > 0) {
        currentHistoryConfirmationAcknowledged = false;
        polishBadge(historyConfirmationBadge, QStringLiteral("badgeGray"));
        historyConfirmationBadge->setText(approvedCount > 0 ? QStringLiteral("部分处理") : QStringLiteral("已拒绝"));
        historyConfirmationMeta->setText(
            QStringLiteral("已处理：批准 %1，拒绝 %2。").arg(approvedCount).arg(deniedCount));
        historyConfirmButton->setEnabled(false);
        historyConfirmButton->setText(QStringLiteral("已处理"));
    } else {
        currentHistoryConfirmationAcknowledged = true;
        polishBadge(historyConfirmationBadge, QStringLiteral("badgeGreen"));
        const bool allApprovedByPolicy = policyApprovedCount == approvedCount;
        historyConfirmationBadge->setText(
            allApprovedByPolicy ? QStringLiteral("策略已批准") : QStringLiteral("已确认"));
        historyConfirmationMeta->setText(
            allApprovedByPolicy
                ? QStringLiteral("平台策略自动批准 %1 项，审计已记录。").arg(approvedCount)
                : QStringLiteral("已批准 %1 项，其中策略自动批准 %2 项。")
                      .arg(approvedCount)
                      .arg(policyApprovedCount));
        historyConfirmButton->setEnabled(false);
        historyConfirmButton->setText(
            allApprovedByPolicy ? QStringLiteral("无需操作") : QStringLiteral("已确认"));
    }

    refreshHistorySelectionBadge();
}

void MainWindow::updateHistoryConfirmationPanelFromLogs(const QList<TaskLogEvent> &events)
{
    if (!historyConfirmationSection || !currentHistoryRequiresConfirmation || !currentHistoryPermissions.isEmpty()) {
        return;
    }

    QList<TaskLogEvent> confirmationEvents;
    for (const TaskLogEvent &event : events) {
        if (event.event == QStringLiteral("confirmation_required")) {
            confirmationEvents.append(event);
        }
    }
    if (confirmationEvents.isEmpty()) {
        return;
    }

    historyConfirmationSection->setVisible(true);
    polishBadge(historyConfirmationBadge, QStringLiteral("badgeOrange"));
    historyConfirmationBadge->setText(QStringLiteral("待确认"));
    historyConfirmationMeta->setText(QStringLiteral("审计加载中，先显示敏感步骤。"));

    QString html = QStringLiteral("<ol style=\"margin-top:0;\">");
    for (const TaskLogEvent &event : confirmationEvents) {
        html += QStringLiteral("<li><b>%1</b><br/><span style=\"color:#334155;\">%2</span></li>")
                    .arg(event.stepId.isEmpty() ? QStringLiteral("敏感步骤") : event.stepId.toHtmlEscaped(),
                         event.message.toHtmlEscaped());
    }
    html += QStringLiteral("</ol>");
    historyConfirmationText->setHtml(html);
    historyConfirmButton->setEnabled(false);
    historyConfirmButton->setText(QStringLiteral("确认已阅"));
    refreshHistorySelectionBadge();
}

void MainWindow::setHistoryConfirmationExpanded(bool expanded)
{
    currentHistoryConfirmationExpanded = expanded;

    if (historyConfirmationBody) {
        historyConfirmationBody->setVisible(expanded);
    }
    if (historyConfirmationSection) {
        historyConfirmationSection->setMaximumHeight(expanded ? HistoryConfirmationExpandedHeight
                                                               : HistoryConfirmationCollapsedHeight);
    }
    if (historyConfirmationText) {
        historyConfirmationText->setMaximumHeight(HistoryConfirmationTextMaxHeight);
    }
    if (historyConfirmationToggleButton) {
        historyConfirmationToggleButton->blockSignals(true);
        historyConfirmationToggleButton->setChecked(expanded);
        historyConfirmationToggleButton->setArrowType(expanded ? Qt::UpArrow : Qt::DownArrow);
        historyConfirmationToggleButton->blockSignals(false);
    }
}

void MainWindow::refreshHistorySelectionBadge()
{
    if (!historySelectionBadge) {
        return;
    }

    if (currentHistoryTaskId.isEmpty()) {
        historySelectionBadge->setStyleSheet(QString());
        polishBadge(historySelectionBadge, QStringLiteral("badgeGray"));
        historySelectionBadge->setText(QStringLiteral("空"));
        return;
    }

    if (!currentHistoryRequiresConfirmation) {
        const QString badgeStatus = (currentHistoryRuntimeStateLoaded
                                     && currentHistoryRuntimeStateError.isEmpty()
                                     && !currentHistoryRuntimeState.status.isEmpty())
                                        ? currentHistoryRuntimeState.status
                                        : currentHistoryStatus;
        historySelectionBadge->setStyleSheet(QString());
        polishBadge(historySelectionBadge, historyStatusBadgeObjectName(badgeStatus));
        historySelectionBadge->setText(historyStatusText(badgeStatus));
        return;
    }

    int pendingCount = 0;
    int approvedCount = 0;
    int deniedCount = 0;
    int policyApprovedCount = 0;
    for (const RuntimePermissionItem &item : currentHistoryPermissions) {
        if (item.decision.decision == QStringLiteral("approved")) {
            ++approvedCount;
            if (isPlatformPolicyDecision(item)) {
                ++policyApprovedCount;
            }
        } else if (item.decision.decision == QStringLiteral("denied")) {
            ++deniedCount;
        } else {
            ++pendingCount;
        }
    }

    QString badgeName = QStringLiteral("badgeOrange");
    QString text = QStringLiteral("需确认");
    if (historyPermissionApprovalInProgress) {
        text = QStringLiteral("写入中");
    } else if (!currentHistoryPermissions.isEmpty() && pendingCount == 0) {
        if (deniedCount > 0 && approvedCount == 0) {
            badgeName = QStringLiteral("badgeGray");
            text = QStringLiteral("已拒绝");
        } else if (deniedCount > 0) {
            badgeName = QStringLiteral("badgeGray");
            text = QStringLiteral("部分处理");
        } else {
            badgeName = QStringLiteral("badgeGreen");
            text = policyApprovedCount == approvedCount
                ? QStringLiteral("策略已批准")
                : QStringLiteral("已确认");
        }
    }

    historySelectionBadge->setStyleSheet(QString());
    polishBadge(historySelectionBadge, badgeName);
    historySelectionBadge->setText(text);
}

void MainWindow::markHistoryConfirmationAcknowledged()
{
    if (!historyConfirmationSection || !historyConfirmationSection->isVisible()) {
        return;
    }

    if (historyPermissionApprovalInProgress) {
        return;
    }

    pendingPermissionApprovalQueue.clear();
    for (const RuntimePermissionItem &item : currentHistoryPermissions) {
        if (item.decision.decision == QStringLiteral("pending")) {
            pendingPermissionApprovalQueue.append(item.request.requestId);
        }
    }

    if (pendingPermissionApprovalQueue.isEmpty()) {
        updateHistoryConfirmationPanel(currentHistoryPermissions);
        return;
    }

    // 一个任务可能有多个敏感步骤。UI 保持一个“确认已阅”入口，
    // 但实际审计仍按后端 request_id 逐条写入，后续真实 Runtime 才能逐步读取决策。
    historyPermissionApprovalInProgress = true;
    historyConfirmButton->setEnabled(false);
    historyConfirmButton->setText(QStringLiteral("写入中"));
    polishBadge(historyConfirmationBadge, QStringLiteral("badgeOrange"));
    historyConfirmationBadge->setText(QStringLiteral("写入中"));
    historyConfirmationMeta->setText(
        QStringLiteral("正在写入 %1 项权限确认审计。").arg(pendingPermissionApprovalQueue.size()));
    refreshHistorySelectionBadge();
    approveNextHistoryPermission();
}

void MainWindow::approveNextHistoryPermission()
{
    if (!historyPermissionApprovalInProgress) {
        return;
    }

    if (pendingPermissionApprovalQueue.isEmpty()) {
        historyPermissionApprovalInProgress = false;
        backendClient->requestTaskPermissions(currentHistoryTaskId);
        return;
    }

    const QString requestId = pendingPermissionApprovalQueue.first();
    if (historyConfirmationMeta) {
        historyConfirmationMeta->setText(
            QStringLiteral("正在写入权限确认审计，剩余 %1 项。").arg(pendingPermissionApprovalQueue.size()));
    }
    backendClient->requestTaskPermissionDecision(
        currentHistoryTaskId,
        requestId,
        QStringLiteral("approved"),
        QStringLiteral("local_user"),
        QStringLiteral("通过历史任务页确认已阅。"));
}

QString MainWindow::historyPermissionDecisionText(const QString &decision) const
{
    return permissionDecisionLabel(decision);
}

QString MainWindow::historyPermissionBadgeObjectName(const QString &decision) const
{
    return permissionDecisionBadge(decision);
}

QString MainWindow::formatHistoryPermissionsHtml() const
{
    if (currentHistoryPermissions.isEmpty()) {
        return QStringLiteral("<p style=\"color:#64748B;\">暂无权限请求记录。</p>");
    }

    QString html;
    for (const RuntimePermissionItem &item : currentHistoryPermissions) {
        html += formatHistoryPermissionItemHtml(item);
    }
    return html;
}

QString MainWindow::formatHistoryPermissionItemHtml(const RuntimePermissionItem &item) const
{
    QString decisionColor = QStringLiteral("#C2410C");
    if (item.decision.decision == QStringLiteral("approved")) {
        decisionColor = QStringLiteral("#059669");
    } else if (item.decision.decision == QStringLiteral("denied")) {
        decisionColor = QStringLiteral("#64748B");
    }

    QStringList permissionLabels;
    for (const QString &permission : item.request.permissions) {
        // 产品级预算确认不是底层文件权限；直接展示内部字段会让客户误以为系统在请求未知能力。
        permissionLabels.append(
            permission == QStringLiteral("knowledge_deep_analysis")
                ? QStringLiteral("全库深度分析预算")
                : permission);
    }
    const QString permissionText = permissionLabels.isEmpty()
                                       ? QStringLiteral("未声明")
                                       : permissionLabels.join(QStringLiteral("、"));
    const QString summary = item.request.summary.isEmpty()
                                ? QStringLiteral("敏感能力请求")
                                : item.request.summary;
    const QString permissionSummary = item.request.details.value(QStringLiteral("permission_summary")).toString();
    const QString reason = item.request.details.value(QStringLiteral("reason")).toString();
    const QString action = item.request.details.value(QStringLiteral("action")).toString();

    QStringList detailParts;
    if (!action.isEmpty()) {
        detailParts.append(QStringLiteral("动作：%1").arg(action.toHtmlEscaped()));
    }
    if (!reason.isEmpty()) {
        detailParts.append(QStringLiteral("原因：%1").arg(reason.toHtmlEscaped()));
    }
    if (!permissionSummary.isEmpty()) {
        detailParts.append(QStringLiteral("说明：%1").arg(permissionSummary.toHtmlEscaped()));
    }
    if (!item.request.permissionPolicy.isEmpty()) {
        const QString policyAction = permissionPolicyActionText(item.request.policyAction);
        detailParts.append(QStringLiteral("策略：%1%2")
                               .arg(runtimePermissionPolicyText(item.request.permissionPolicy).toHtmlEscaped(),
                                    policyAction.isEmpty()
                                        ? QString()
                                        : QStringLiteral("（%1）").arg(policyAction.toHtmlEscaped())));
    }
    if (!item.request.policyReason.isEmpty()) {
        detailParts.append(QStringLiteral("策略理由：%1").arg(item.request.policyReason.toHtmlEscaped()));
    }
    if (item.decision.decision != QStringLiteral("pending")) {
        detailParts.append(QStringLiteral("决策来源：%1")
                               .arg(permissionDecisionSourceText(item.decision.decidedBy).toHtmlEscaped()));
    }
    if (!item.decision.note.isEmpty() && item.decision.note != item.request.policyReason) {
        detailParts.append(QStringLiteral("审计备注：%1").arg(item.decision.note.toHtmlEscaped()));
    }

    const QString decidedAt = item.decision.decidedAt.isEmpty()
                                  ? QStringLiteral("未写入")
                                  : item.decision.decidedAt;
    const QString detailHtml = detailParts.isEmpty()
                                   ? QString()
                                   : QStringLiteral("<div style=\"margin-top:4px;color:#64748B;\">%1</div>")
                                         .arg(detailParts.join(QStringLiteral(" · ")));

    QString html = QStringLiteral(
                       "<div style=\"margin-bottom:10px;padding:10px;border:1px solid #F5D7BC;"
                       "border-radius:10px;background:#FFFFFF;\">"
                       "<div><b>%1</b> <span style=\"color:#64748B;\">%2 · %3 · %4</span></div>"
                       "<div style=\"margin-top:4px;color:#334155;\">%5</div>"
                       "<div style=\"margin-top:4px;color:#64748B;\">权限：%6 · 决策："
                       "<span style=\"color:%7;font-weight:800;\">%8</span> · 时间：%9</div>")
        .arg(item.request.stepId.isEmpty() ? QStringLiteral("敏感步骤") : item.request.stepId.toHtmlEscaped(),
             agentDisplayName(item.request.agentId).toHtmlEscaped(),
             historyRiskText(item.request.riskLevel).toHtmlEscaped(),
             item.request.requestId.toHtmlEscaped(),
             summary.toHtmlEscaped(),
             permissionText.toHtmlEscaped(),
             decisionColor,
             historyPermissionDecisionText(item.decision.decision).toHtmlEscaped(),
             decidedAt.toHtmlEscaped());
    html += detailHtml;
    html += QStringLiteral("</div>");
    return html;
}

QJsonObject MainWindow::historyVerificationFromStepOutput(const QJsonObject &output) const
{
    // Runtime 成功时 verification 在 result 下；失败时会被放进 error.details，二者都要兼容。
    QJsonObject verification = output.value(QStringLiteral("result"))
                                   .toObject()
                                   .value(QStringLiteral("verification"))
                                   .toObject();
    if (!verification.isEmpty()) {
        return verification;
    }

    verification = output.value(QStringLiteral("error"))
                       .toObject()
                       .value(QStringLiteral("details"))
                       .toObject()
                       .value(QStringLiteral("verification"))
                       .toObject();
    return verification;
}

QJsonObject MainWindow::historyVerificationFromToolResult(const QJsonObject &result) const
{
    QJsonObject verification = result.value(QStringLiteral("verification")).toObject();
    if (!verification.isEmpty()) {
        return verification;
    }

    verification = result.value(QStringLiteral("error"))
                       .toObject()
                       .value(QStringLiteral("details"))
                       .toObject()
                       .value(QStringLiteral("verification"))
                       .toObject();
    return verification;
}

QJsonObject MainWindow::historyVerificationFromUpdatePayload(const QJsonObject &payload) const
{
    const QJsonObject stepOutput = payload.value(QStringLiteral("step"))
                                       .toObject()
                                       .value(QStringLiteral("output"))
                                       .toObject();
    QJsonObject verification = historyVerificationFromStepOutput(stepOutput);
    if (!verification.isEmpty()) {
        return verification;
    }

    const QJsonArray toolCalls = payload.value(QStringLiteral("tool_calls")).toArray();
    for (const QJsonValue &value : toolCalls) {
        verification = historyVerificationFromToolResult(
            value.toObject().value(QStringLiteral("result")).toObject());
        if (!verification.isEmpty()) {
            return verification;
        }
    }
    return {};
}

QJsonObject MainWindow::historyVerificationForArtifact(const WorkflowArtifactInfo &artifact) const
{
    if (artifact.stepId.isEmpty()) {
        return {};
    }

    // 产物列表本身只保存 artifact 元数据；验证结果来自同 step 的 Runtime 输出或工具审计。
    for (const WorkflowStepRunInfo &step : currentHistorySteps) {
        if (step.stepId == artifact.stepId) {
            const QJsonObject verification = historyVerificationFromStepOutput(step.output);
            if (!verification.isEmpty()) {
                return verification;
            }
        }
    }
    for (const WorkflowToolCallInfo &call : currentHistoryToolCalls) {
        if (call.stepId == artifact.stepId) {
            const QJsonObject verification = historyVerificationFromToolResult(call.result);
            if (!verification.isEmpty()) {
                return verification;
            }
        }
    }
    return {};
}

QJsonObject MainWindow::historyDocumentContextFromStepOutput(const QJsonObject &output) const
{
    // Code / Report 的 Runtime 输出会把前置文档步骤压成 document_context。
    // 这里仅做展示提取，不重新读文件，也不把上下文升级成长期记忆。
    QJsonObject context = output.value(QStringLiteral("result"))
                              .toObject()
                              .value(QStringLiteral("document_context"))
                              .toObject();
    if (context.isEmpty()) {
        context = output.value(QStringLiteral("document_context")).toObject();
    }
    return context;
}

QJsonObject MainWindow::historyDocumentContextFromToolResult(const QJsonObject &result) const
{
    QJsonObject context = result.value(QStringLiteral("document_context")).toObject();
    if (context.isEmpty()) {
        context = result.value(QStringLiteral("result"))
                      .toObject()
                      .value(QStringLiteral("document_context"))
                      .toObject();
    }
    return context;
}

QJsonObject MainWindow::historyDocumentContextFromUpdatePayload(const QJsonObject &payload) const
{
    const QJsonObject stepOutput = payload.value(QStringLiteral("step"))
                                       .toObject()
                                       .value(QStringLiteral("output"))
                                       .toObject();
    QJsonObject context = historyDocumentContextFromStepOutput(stepOutput);
    if (!context.isEmpty()) {
        return context;
    }

    const QJsonArray toolCalls = payload.value(QStringLiteral("tool_calls")).toArray();
    for (const QJsonValue &value : toolCalls) {
        context = historyDocumentContextFromToolResult(
            value.toObject().value(QStringLiteral("result")).toObject());
        if (!context.isEmpty()) {
            return context;
        }
    }
    return {};
}

QJsonObject MainWindow::historyDocumentContextForArtifact(const WorkflowArtifactInfo &artifact) const
{
    if (artifact.stepId.isEmpty()) {
        return {};
    }

    for (const WorkflowStepRunInfo &step : currentHistorySteps) {
        if (step.stepId == artifact.stepId) {
            const QJsonObject context = historyDocumentContextFromStepOutput(step.output);
            if (!context.isEmpty()) {
                return context;
            }
        }
    }
    for (const WorkflowToolCallInfo &call : currentHistoryToolCalls) {
        if (call.stepId == artifact.stepId) {
            const QJsonObject context = historyDocumentContextFromToolResult(call.result);
            if (!context.isEmpty()) {
                return context;
            }
        }
    }
    return {};
}

QString MainWindow::formatHistoryVerificationText(const QJsonObject &verification) const
{
    if (verification.isEmpty()) {
        return QString();
    }

    QStringList lines;
    const bool ok = verification.value(QStringLiteral("ok")).toBool(false);
    const QString reason = verification.value(QStringLiteral("reason")).toString();
    QString reasonText = QStringLiteral("未知");
    if (reason == QStringLiteral("ok")) {
        reasonText = QStringLiteral("关键片段均已命中");
    } else if (reason == QStringLiteral("missing_snippets")) {
        reasonText = QStringLiteral("缺少关键片段");
    } else if (reason == QStringLiteral("read_failed")) {
        reasonText = QStringLiteral("回读失败");
    } else if (!reason.isEmpty()) {
        reasonText = reason;
    }

    lines.append(QStringLiteral("产物验证：%1").arg(ok ? QStringLiteral("已通过") : QStringLiteral("未通过")));
    lines.append(QStringLiteral("原因：%1").arg(reasonText));
    lines.append(QStringLiteral("检查片段：%1").arg(
        verification.value(QStringLiteral("checked_snippets")).toInt()));
    if (verification.contains(QStringLiteral("read_back_bytes"))) {
        lines.append(QStringLiteral("回读字节：%1").arg(
            verification.value(QStringLiteral("read_back_bytes")).toInt()));
    }

    const QJsonArray missing = verification.value(QStringLiteral("missing_snippets")).toArray();
    if (!missing.isEmpty()) {
        QStringList snippets;
        for (const QJsonValue &value : missing) {
            snippets.append(compactBadgeText(value.toString(), 80));
            if (snippets.size() >= 3) {
                break;
            }
        }
        lines.append(QStringLiteral("缺失片段：%1").arg(snippets.join(QStringLiteral("；"))));
    }
    return lines.join(QStringLiteral("\n"));
}

QString MainWindow::formatHistoryVerificationHtml(const QJsonObject &verification) const
{
    if (verification.isEmpty()) {
        return QString();
    }

    const bool ok = verification.value(QStringLiteral("ok")).toBool(false);
    const QString color = ok ? QStringLiteral("#059669") : QStringLiteral("#DC2626");
    const QString background = ok ? QStringLiteral("#ECFDF5") : QStringLiteral("#FEF2F2");
    const QString border = ok ? QStringLiteral("#BBF7D0") : QStringLiteral("#FECACA");
    const QString statusText = ok ? QStringLiteral("已通过") : QStringLiteral("未通过");
    QStringList detailLines = formatHistoryVerificationText(verification).split(QLatin1Char('\n'));
    if (!detailLines.isEmpty()) {
        detailLines.removeFirst();
    }
    const QString plainText = detailLines.join(QStringLiteral("\n"))
                                  .toHtmlEscaped()
                                  .replace(QStringLiteral("\n"), QStringLiteral("<br/>"));

    return QStringLiteral(
               "<div style=\"margin-top:6px;padding:8px;border:1px solid %1;border-radius:8px;"
               "background:%2;color:#334155;\">"
               "<b style=\"color:%3;\">产物验证：%4</b><br/>%5"
               "</div>")
        .arg(border, background, color, statusText.toHtmlEscaped(), plainText);
}

QString MainWindow::formatHistoryDocumentContextHtml(const QJsonObject &context, int displayLimit) const
{
    if (context.isEmpty()) {
        return QString();
    }

    const QJsonArray sourceSteps = context.value(QStringLiteral("source_steps")).toArray();
    const QJsonArray searchMatches = context.value(QStringLiteral("search_matches")).toArray();
    const QJsonArray readPreviews = context.value(QStringLiteral("read_previews")).toArray();
    const int matchTotal = context.value(QStringLiteral("search_match_total")).toInt(searchMatches.size());
    const int previewTotal = context.value(QStringLiteral("read_preview_total")).toInt(readPreviews.size());
    if (sourceSteps.isEmpty() && matchTotal <= 0 && previewTotal <= 0) {
        return QString();
    }

    QStringList sourceStepTexts;
    for (const QJsonValue &value : sourceSteps) {
        const QString stepId = value.toString();
        if (!stepId.isEmpty()) {
            sourceStepTexts.append(stepId.toHtmlEscaped());
        }
        if (sourceStepTexts.size() >= 4) {
            break;
        }
    }
    const QString sourceText = sourceStepTexts.isEmpty()
        ? QStringLiteral("无")
        : sourceStepTexts.join(QStringLiteral("、"));

    QString html = QStringLiteral(
        "<div style=\"margin-top:6px;padding:8px;border:1px solid #C7D2FE;"
        "border-left:4px solid #4F46E5;border-radius:8px;background:#EEF2FF;\">"
        "<div><b>文档上下文</b> <span style=\"color:#64748B;\">来源步骤：%1 · 搜索命中 %2 · 读取预览 %3</span></div>")
        .arg(sourceText, QString::number(matchTotal), QString::number(previewTotal));

    const int safeLimit = qBound(1, displayLimit, 5);
    const int matchDisplayCount = qMin(safeLimit, searchMatches.size());
    if (matchDisplayCount > 0) {
        html += QStringLiteral("<div style=\"margin-top:5px;color:#475569;\"><b>命中片段</b></div><ol style=\"margin-top:4px;margin-bottom:0;padding-left:18px;\">");
        for (int index = 0; index < matchDisplayCount; ++index) {
            const QJsonObject match = searchMatches.at(index).toObject();
            const QString documentName = match.value(QStringLiteral("document_name")).toString(QStringLiteral("未知文档"));
            const int lineNumber = match.value(QStringLiteral("line_number")).toInt();
            const QString preview = match.value(QStringLiteral("preview")).toString();
            html += QStringLiteral("<li style=\"margin-bottom:4px;\"><b>%1:%2</b><br/><span style=\"color:#334155;\">%3</span></li>")
                        .arg(documentName.toHtmlEscaped(),
                             QString::number(lineNumber),
                             preview.toHtmlEscaped());
        }
        html += QStringLiteral("</ol>");
    }

    const int previewDisplayCount = qMin(safeLimit, readPreviews.size());
    if (previewDisplayCount > 0) {
        html += QStringLiteral("<div style=\"margin-top:5px;color:#475569;\"><b>读取预览</b></div><ol style=\"margin-top:4px;margin-bottom:0;padding-left:18px;\">");
        for (int index = 0; index < previewDisplayCount; ++index) {
            const QJsonObject previewItem = readPreviews.at(index).toObject();
            const QString path = previewItem.value(QStringLiteral("relative_path")).toString(
                previewItem.value(QStringLiteral("path")).toString(QStringLiteral("未知路径")));
            const int bytes = previewItem.value(QStringLiteral("bytes")).toInt();
            const QString preview = previewItem.value(QStringLiteral("preview")).toString();
            html += QStringLiteral("<li style=\"margin-bottom:4px;\"><b>%1</b> <span style=\"color:#64748B;\">%2 字节</span><br/><span style=\"color:#334155;\">%3</span></li>")
                        .arg(path.toHtmlEscaped(),
                             QString::number(bytes),
                             preview.toHtmlEscaped());
        }
        html += QStringLiteral("</ol>");
    }

    if (searchMatches.size() > matchDisplayCount || readPreviews.size() > previewDisplayCount) {
        html += QStringLiteral("<div style=\"margin-top:4px;color:#64748B;\">上下文已折叠，完整结构可在工具结果 JSON 中查看。</div>");
    }
    html += QStringLiteral("</div>");
    return html;
}

QString MainWindow::formatHistoryDocumentContextText(const QJsonObject &context, int displayLimit) const
{
    if (context.isEmpty()) {
        return QString();
    }

    const QJsonArray sourceSteps = context.value(QStringLiteral("source_steps")).toArray();
    const QJsonArray searchMatches = context.value(QStringLiteral("search_matches")).toArray();
    const QJsonArray readPreviews = context.value(QStringLiteral("read_previews")).toArray();
    const int matchTotal = context.value(QStringLiteral("search_match_total")).toInt(searchMatches.size());
    const int previewTotal = context.value(QStringLiteral("read_preview_total")).toInt(readPreviews.size());
    if (sourceSteps.isEmpty() && matchTotal <= 0 && previewTotal <= 0) {
        return QString();
    }

    QStringList sourceStepTexts;
    for (const QJsonValue &value : sourceSteps) {
        const QString stepId = value.toString();
        if (!stepId.isEmpty()) {
            sourceStepTexts.append(stepId);
        }
        if (sourceStepTexts.size() >= 4) {
            break;
        }
    }

    QStringList lines;
    lines.append(QStringLiteral("文档上下文："));
    lines.append(QStringLiteral("来源步骤：%1").arg(sourceStepTexts.isEmpty()
                                                   ? QStringLiteral("无")
                                                   : sourceStepTexts.join(QStringLiteral("、"))));
    lines.append(QStringLiteral("搜索命中：%1 · 读取预览：%2").arg(matchTotal).arg(previewTotal));

    const int safeLimit = qBound(1, displayLimit, 6);
    const int matchDisplayCount = qMin(safeLimit, searchMatches.size());
    for (int index = 0; index < matchDisplayCount; ++index) {
        const QJsonObject match = searchMatches.at(index).toObject();
        lines.append(QStringLiteral("- 命中 %1:%2：%3")
                         .arg(match.value(QStringLiteral("document_name")).toString(QStringLiteral("未知文档")))
                         .arg(match.value(QStringLiteral("line_number")).toInt())
                         .arg(match.value(QStringLiteral("preview")).toString()));
    }

    const int previewDisplayCount = qMin(safeLimit, readPreviews.size());
    for (int index = 0; index < previewDisplayCount; ++index) {
        const QJsonObject previewItem = readPreviews.at(index).toObject();
        const QString path = previewItem.value(QStringLiteral("relative_path")).toString(
            previewItem.value(QStringLiteral("path")).toString(QStringLiteral("未知路径")));
        lines.append(QStringLiteral("- 预览 %1（%2 字节）：%3")
                         .arg(path)
                         .arg(previewItem.value(QStringLiteral("bytes")).toInt())
                         .arg(previewItem.value(QStringLiteral("preview")).toString()));
    }
    return lines.join(QStringLiteral("\n"));
}

QString MainWindow::formatHistoryPlanSummaryHtml() const
{
    const bool hasPlanSummary = !currentHistoryPlanSummary.summary.isEmpty()
        || !currentHistoryPlanSummary.intent.isEmpty()
        || !currentHistoryPlanSummary.clarifyingQuestions.isEmpty()
        || !currentHistoryPlanSummary.definitionOfDone.isEmpty()
        || !currentHistoryPlanSteps.isEmpty();
    if (!hasPlanSummary) {
        return QStringLiteral("<p style=\"color:#64748B;\">该任务没有保存可回看的总指挥计划。</p>");
    }

    const QString intent = currentHistoryPlanSummary.intent.isEmpty()
        ? QStringLiteral("general")
        : currentHistoryPlanSummary.intent;
    const QString nextAction = currentHistoryPlanSummary.nextAction.isEmpty()
        ? QStringLiteral("未声明")
        : dispatchNextActionText(currentHistoryPlanSummary.nextAction);
    const QString version = currentHistoryPlanSummary.planVersion > 0
        ? QString::number(currentHistoryPlanSummary.planVersion)
        : QStringLiteral("1");

    QString html = QStringLiteral(
        "<div style=\"margin-bottom:10px;padding:10px;border:1px solid #BFDBFE;"
        "border-left:4px solid #2563EB;border-radius:10px;background:#F8FAFC;\">"
        "<div><b>计划摘要</b> "
        "<span style=\"color:#64748B;\">%1 · v%2 · 下一步：%3</span></div>")
        .arg(intent.toHtmlEscaped(), version.toHtmlEscaped(), nextAction.toHtmlEscaped());

    if (!currentHistoryPlanSummary.summary.isEmpty()) {
        html += QStringLiteral("<div style=\"margin-top:5px;color:#334155;\">%1</div>")
                    .arg(currentHistoryPlanSummary.summary.toHtmlEscaped());
    }

    // 显示计划生成时的偏好快照，避免系统设置看似保存成功却无法从任务侧核对。
    html += QStringLiteral(
        "<div style=\"margin-top:6px;color:#475569;\"><b>本次偏好：</b>%1 · %2</div>")
        .arg(runtimePermissionPolicyText(currentHistoryPlanSummary.preferences.permissionPolicy).toHtmlEscaped(),
             runtimePersonalityText(currentHistoryPlanSummary.preferences.personality).toHtmlEscaped());

    if (!currentHistoryPlanSummary.clarifyingQuestions.isEmpty()) {
        html += QStringLiteral(
            "<div style=\"margin-top:8px;padding:8px;border:1px solid #FDBA74;"
            "border-radius:8px;background:#FFF7ED;color:#9A3412;\">"
            "<b>需要补充信息</b>%1</div>")
            .arg(dispatchBulletListHtml(currentHistoryPlanSummary.clarifyingQuestions));
    }

    if (!currentHistoryPlanSummary.definitionOfDone.isEmpty()) {
        html += QStringLiteral("<div style=\"margin-top:8px;\"><b>完成标准</b>%1</div>")
                    .arg(dispatchBulletListHtml(currentHistoryPlanSummary.definitionOfDone));
    }

    const WorkflowBudgetEstimateInfo budget = currentHistoryPlanSummary.budgetEstimate;
    if (budget.stepCount > 0 || !budget.timeLevel.isEmpty() || !budget.modelCostLevel.isEmpty()) {
        const QString budgetText = QStringLiteral("步骤 %1 · 耗时 %2 · 成本 %3 · 联网 %4 · 命令 %5")
            .arg(budget.stepCount)
            .arg(dispatchBudgetLevelText(budget.timeLevel))
            .arg(dispatchBudgetLevelText(budget.modelCostLevel))
            .arg(budget.requiresNetwork ? QStringLiteral("可能需要") : QStringLiteral("不需要"))
            .arg(budget.requiresCommand ? QStringLiteral("可能需要") : QStringLiteral("不需要"));
        html += QStringLiteral("<div style=\"margin-top:8px;color:#475569;\"><b>预算预估：</b>%1</div>")
                    .arg(budgetText.toHtmlEscaped());
    }

    const WorkflowWorkspaceScopeInfo scope = currentHistoryPlanSummary.workspaceScope;
    const bool hasWorkspaceScope = !scope.readPaths.isEmpty()
        || !scope.writePaths.isEmpty()
        || !scope.externalServices.isEmpty()
        || !scope.notes.isEmpty();
    if (hasWorkspaceScope) {
        html += QStringLiteral(
            "<div style=\"margin-top:6px;color:#475569;\">"
            "<b>工作区边界：</b>读：%1；写：%2；外部：%3</div>")
            .arg(dispatchCompactListText(scope.readPaths),
                 dispatchCompactListText(scope.writePaths),
                 dispatchCompactListText(scope.externalServices));
        if (!scope.notes.isEmpty()) {
            html += QStringLiteral("<div style=\"margin-top:3px;color:#64748B;\">%1</div>")
                        .arg(scope.notes.toHtmlEscaped());
        }
    }

    if (!currentHistoryPlanSteps.isEmpty()) {
        html += QStringLiteral("<div style=\"margin-top:10px;\"><b>计划步骤</b></div><ol style=\"margin-top:6px;\">");
        for (const WorkflowStepInfo &step : currentHistoryPlanSteps) {
            const QString riskText = historyRiskText(step.riskLevel).toHtmlEscaped();
            const QString confirmationText = step.requiresConfirmation ? QStringLiteral("需确认")
                                                                       : QStringLiteral("无需确认");
            const QString reasonText = step.reason.isEmpty()
                ? QStringLiteral("未提供原因")
                : step.reason.toHtmlEscaped();
            const QString expectedText = step.expectedOutput.isEmpty()
                ? QStringLiteral("未声明预期产出")
                : step.expectedOutput.toHtmlEscaped();
            const QString titleText = step.title.isEmpty()
                ? QStringLiteral("%1.%2").arg(step.agent, step.action).toHtmlEscaped()
                : step.title.toHtmlEscaped();
            html += QStringLiteral(
                "<li style=\"margin-bottom:8px;\">"
                "<div><b>%1</b> <span style=\"color:#64748B;\">%2 · 风险：%3 · %4</span></div>"
                "<div style=\"margin-top:3px;color:#475569;\">原因：%5</div>"
                "<div style=\"margin-top:3px;color:#475569;\">预期：%6</div>"
                "%7"
                "</li>")
                .arg(titleText,
                     agentDisplayName(step.agent).toHtmlEscaped(),
                     riskText,
                     confirmationText.toHtmlEscaped(),
                     reasonText,
                     expectedText,
                     formatDispatchStepContractHtml(step));
        }
        html += QStringLiteral("</ol>");
    }

    html += QStringLiteral("</div>");
    return html;
}

QString MainWindow::formatJsonPreview(const QJsonObject &object, int maxLength) const
{
    if (object.isEmpty() || maxLength <= 0) {
        return QString();
    }

    QString text = QString::fromUtf8(QJsonDocument(object).toJson(QJsonDocument::Compact));
    if (text.size() > maxLength) {
        text = text.left(maxLength - 1) + QStringLiteral("…");
    }
    return text.toHtmlEscaped();
}

QString MainWindow::formatWorkspaceSearchResultHtml(const QJsonObject &result, int displayLimit) const
{
    if (result.isEmpty() || !result.contains(QStringLiteral("matches"))) {
        return QString();
    }

    const QString query = result.value(QStringLiteral("query")).toString();
    const QJsonArray matches = result.value(QStringLiteral("matches")).toArray();
    const int total = result.value(QStringLiteral("total")).toInt(matches.size());
    const int searchedDocuments = result.value(QStringLiteral("searched_documents")).toInt();
    const int limit = result.value(QStringLiteral("limit")).toInt();
    const bool limitReached = result.value(QStringLiteral("limit_reached")).toBool(false);

    QString html = QStringLiteral(
        "<div style=\"margin-top:6px;padding:8px;border:1px solid #BAE6FD;"
        "border-left:4px solid #0891B2;border-radius:8px;background:#F0F9FF;\">"
        "<div><b>文档搜索结果</b> <span style=\"color:#64748B;\">关键词：%1 · 命中 %2 处 · 扫描 %3 份文档</span></div>")
        .arg(query.isEmpty() ? QStringLiteral("未提供") : query.toHtmlEscaped(),
             QString::number(total),
             QString::number(searchedDocuments));
    if (limit > 0 || limitReached) {
        html += QStringLiteral("<div style=\"margin-top:3px;color:#64748B;\">显示上限：%1 · 是否截断：%2</div>")
                    .arg(limit > 0 ? QString::number(limit) : QStringLiteral("未提供"),
                         limitReached ? QStringLiteral("是") : QStringLiteral("否"));
    }

    if (matches.isEmpty()) {
        html += QStringLiteral("<div style=\"margin-top:5px;color:#475569;\">没有在受控 workspace 文档中找到匹配内容。</div>");
        html += QStringLiteral("</div>");
        return html;
    }

    const int safeLimit = qBound(1, displayLimit, 8);
    const int displayCount = qMin(safeLimit, matches.size());
    html += QStringLiteral("<ol style=\"margin-top:6px;margin-bottom:0;padding-left:18px;\">");
    for (int index = 0; index < displayCount; ++index) {
        const QJsonObject match = matches.at(index).toObject();
        const QString documentName = match.value(QStringLiteral("document_name")).toString(
            match.value(QStringLiteral("relative_path")).toString(QStringLiteral("未知文档")));
        const int lineNumber = match.value(QStringLiteral("line_number")).toInt();
        const QString preview = match.value(QStringLiteral("preview")).toString(
            match.value(QStringLiteral("line_text")).toString());
        html += QStringLiteral(
            "<li style=\"margin-bottom:5px;\">"
            "<b>%1:%2</b><br/><span style=\"color:#334155;\">%3</span></li>")
            .arg(documentName.toHtmlEscaped(),
                 QString::number(lineNumber),
                 preview.toHtmlEscaped());
    }
    html += QStringLiteral("</ol>");
    if (matches.size() > displayCount) {
        html += QStringLiteral("<div style=\"margin-top:4px;color:#64748B;\">还有 %1 条命中未展开，可在工具调用原始结果中查看。</div>")
                    .arg(matches.size() - displayCount);
    }
    html += QStringLiteral("</div>");
    return html;
}

QString MainWindow::formatWorkspaceSearchResultFromStepOutputHtml(const QJsonObject &output, int displayLimit) const
{
    const QString toolName = output.value(QStringLiteral("tool_name")).toString();
    const QJsonObject result = output.value(QStringLiteral("result")).toObject();
    if (toolName != QStringLiteral("document.search_text")) {
        return QString();
    }
    return formatWorkspaceSearchResultHtml(result, displayLimit);
}

QString MainWindow::formatWorkspaceSearchResultFromUpdatePayloadHtml(const QJsonObject &payload, int displayLimit) const
{
    // updates.payload 会携带 step 和 tool_calls；这里优先从 step.output 读取，
    // 读不到再回退到 tool_calls，避免事件流把搜索结果退化成一行压缩 JSON。
    const QJsonObject stepOutput = payload.value(QStringLiteral("step"))
                                       .toObject()
                                       .value(QStringLiteral("output"))
                                       .toObject();
    QString html = formatWorkspaceSearchResultFromStepOutputHtml(stepOutput, displayLimit);
    if (!html.isEmpty()) {
        return html;
    }

    const QJsonArray toolCalls = payload.value(QStringLiteral("tool_calls")).toArray();
    for (const QJsonValue &value : toolCalls) {
        const QJsonObject toolCall = value.toObject();
        if (toolCall.value(QStringLiteral("tool_name")).toString() == QStringLiteral("document.search_text")) {
            html = formatWorkspaceSearchResultHtml(
                toolCall.value(QStringLiteral("result")).toObject(),
                displayLimit);
            if (!html.isEmpty()) {
                return html;
            }
        }
    }
    return QString();
}

QString MainWindow::formatHistoryRuntimeStateHtml() const
{
    if (currentHistoryRuntimeState.taskId.isEmpty()) {
        return QStringLiteral("<p style=\"color:#64748B;\">暂无运行态快照。</p>");
    }

    const QString statusText = historyStatusText(currentHistoryRuntimeState.status).toHtmlEscaped();
    const QString modeText = historyModeText(currentHistoryRuntimeState.mode).toHtmlEscaped();
    const QString terminalText = currentHistoryRuntimeState.terminal ? QStringLiteral("是") : QStringLiteral("否");
    const QString messageText = currentHistoryRuntimeState.message.isEmpty()
                                    ? QStringLiteral("运行态已记录。")
                                    : currentHistoryRuntimeState.message.toHtmlEscaped();

    QStringList actionLabels;
    for (const QString &action : currentHistoryRuntimeState.allowedActions) {
        if (action == QStringLiteral("cancel")) {
            actionLabels.append(QStringLiteral("取消"));
        } else if (action == QStringLiteral("retry")) {
            actionLabels.append(QStringLiteral("重试"));
        } else {
            actionLabels.append(action.toHtmlEscaped());
        }
    }
    QStringList nextStatusLabels;
    for (const QString &nextStatus : currentHistoryRuntimeState.allowedNextStatuses) {
        nextStatusLabels.append(historyStatusText(nextStatus).toHtmlEscaped());
    }

    const QString actionText = actionLabels.isEmpty() ? QStringLiteral("无") : actionLabels.join(QStringLiteral("、"));
    const QString nextText = nextStatusLabels.isEmpty() ? QStringLiteral("无") : nextStatusLabels.join(QStringLiteral("、"));

    return QStringLiteral(
               "<div style=\"margin-bottom:10px;padding:10px;border:1px solid #DDEBFA;"
               "border-radius:10px;background:#FFFFFF;\">"
               "<div><b>%1</b> <span style=\"color:#64748B;\">%2 · %3</span></div>"
               "<div style=\"margin-top:4px;color:#334155;\">%4</div>"
               "<div style=\"margin-top:4px;color:#64748B;\">终态：%5 · 可用动作：%6 · 下一状态：%7</div>"
               "</div>")
        .arg(statusText,
             modeText,
             currentHistoryRuntimeState.taskId.toHtmlEscaped(),
             messageText,
             terminalText,
             actionText,
             nextText);
}

QString MainWindow::formatHistoryMetricsSummaryText() const
{
    if (!currentHistoryMetricsLoaded
        || !currentHistoryMetricsError.isEmpty()
        || currentHistoryMetrics.taskId.isEmpty()) {
        return QString();
    }

    const RuntimeExecutionMetricsInfo &metrics = currentHistoryMetrics.metrics;
    return QStringLiteral("指标：步骤 %1/%2 · 工具 %3 · 失败 %4 · 重试 %5 · 权限 %6 · 耗时 %7ms · token %8/%9 · 超预算 %10")
        .arg(metrics.stepCompleted)
        .arg(metrics.stepTotal)
        .arg(metrics.toolCallTotal)
        .arg(metrics.toolCallFailed + metrics.stepFailed)
        .arg(metrics.retryTotal)
        .arg(metrics.permissionRequestTotal)
        .arg(metrics.durationMs)
        .arg(metrics.estimatedInputTokens)
        .arg(metrics.estimatedOutputTokens)
        .arg(metrics.budgetExceeded ? QStringLiteral("是") : QStringLiteral("否"));
}

QString MainWindow::formatHistoryMetricsHtml() const
{
    if (currentHistoryMetrics.taskId.isEmpty()) {
        return QStringLiteral("<p style=\"color:#64748B;\">暂无运行指标。</p>");
    }

    const RuntimeExecutionMetricsInfo &metrics = currentHistoryMetrics.metrics;
    const RuntimeExecutionLimitsInfo &limits = currentHistoryMetrics.limits;
    const QString budgetColor = metrics.budgetExceeded ? QStringLiteral("#DC2626") : QStringLiteral("#059669");
    const QString budgetText = metrics.budgetExceeded ? QStringLiteral("是") : QStringLiteral("否");
    const QString tokenBudgetText = limits.tokenBudget >= 0 ? QString::number(limits.tokenBudget)
                                                            : QStringLiteral("未设置");
    const QString startedAt = metrics.startedAt.isEmpty() ? QStringLiteral("未记录")
                                                          : metrics.startedAt.toHtmlEscaped();
    const QString finishedAt = metrics.finishedAt.isEmpty() ? QStringLiteral("未结束")
                                                            : metrics.finishedAt.toHtmlEscaped();

    // metrics 是后续评估 Agent 成功率、效率和成本的核心证据。
    // 详情页按“执行事实”和“预算边界”拆开，用户能看懂，也不把上限误认为实际消耗。
    QString html = QStringLiteral(
        "<div style=\"margin-bottom:10px;padding:10px;border:1px solid #DDEBFA;"
        "border-left:4px solid #2563EB;border-radius:10px;background:#FFFFFF;\">");
    html += QStringLiteral("<div><b>执行事实</b> <span style=\"color:#64748B;\">%1</span></div>")
                .arg(currentHistoryMetrics.taskId.toHtmlEscaped());
    html += QStringLiteral(
                "<div style=\"margin-top:4px;color:#334155;\">步骤：%1/%2 · 步骤失败：%3 · 工具调用：%4 · 工具失败：%5 · 模拟工具：%6</div>")
                .arg(metrics.stepCompleted)
                .arg(metrics.stepTotal)
                .arg(metrics.stepFailed)
                .arg(metrics.toolCallTotal)
                .arg(metrics.toolCallFailed)
                .arg(metrics.toolCallSimulated);
    html += QStringLiteral(
                "<div style=\"margin-top:4px;color:#334155;\">重试：%1 · 权限请求：%2 · 校验错误：%3 · 耗时：%4ms</div>")
                .arg(metrics.retryTotal)
                .arg(metrics.permissionRequestTotal)
                .arg(metrics.validationErrorTotal)
                .arg(metrics.durationMs);
    html += QStringLiteral(
                "<div style=\"margin-top:4px;color:#64748B;\">Token 估算：输入 %1 / 输出 %2 · 成本估算：￥%3 · 超预算："
                "<span style=\"color:%4;font-weight:800;\">%5</span></div>")
                .arg(metrics.estimatedInputTokens)
                .arg(metrics.estimatedOutputTokens)
                .arg(QString::number(metrics.estimatedCostCny, 'f', 4))
                .arg(budgetColor, budgetText);
    html += QStringLiteral("<div style=\"margin-top:4px;color:#64748B;\">开始：%1 · 结束：%2</div>")
                .arg(startedAt, finishedAt);
    html += QStringLiteral("</div>");

    html += QStringLiteral(
        "<div style=\"margin-bottom:10px;padding:10px;border:1px solid #E2E8F0;"
        "border-radius:10px;background:#FFFFFF;\">");
    html += QStringLiteral("<div><b>执行预算</b></div>");
    html += QStringLiteral(
                "<div style=\"margin-top:4px;color:#64748B;\">最大步骤：%1 · 最大工具调用：%2 · 单工具最多重试：%3</div>")
                .arg(limits.maxSteps)
                .arg(limits.maxToolCalls)
                .arg(limits.maxRetriesPerTool);
    html += QStringLiteral(
                "<div style=\"margin-top:4px;color:#64748B;\">工具超时：%1ms · 任务超时：%2ms · Token 预算：%3</div>")
                .arg(limits.toolTimeoutMs)
                .arg(limits.taskTimeoutMs)
                .arg(tokenBudgetText.toHtmlEscaped());
    html += QStringLiteral("</div>");
    return html;
}

QString MainWindow::formatHistoryEvaluationHtml() const
{
    if (currentHistoryEvaluation.taskId.isEmpty()) {
        return QStringLiteral("<p style=\"color:#64748B;\">暂无任务评估。</p>");
    }

    const WorkflowTaskEvaluationResult &evaluation = currentHistoryEvaluation;
    const QString scoreColor = evaluation.overallScore >= 0.8
        ? QStringLiteral("#059669")
        : (evaluation.overallScore >= 0.5 ? QStringLiteral("#C2410C") : QStringLiteral("#DC2626"));
    auto percentText = [](double value) {
        return QStringLiteral("%1%").arg(QString::number(qBound(0.0, value, 1.0) * 100.0, 'f', 1));
    };
    auto renderList = [](const QStringList &items, const QString &emptyText) {
        if (items.isEmpty()) {
            return QStringLiteral("<div style=\"margin-top:4px;color:#64748B;\">%1</div>")
                .arg(emptyText.toHtmlEscaped());
        }

        QString html = QStringLiteral("<ul style=\"margin-top:4px;margin-bottom:0;\">");
        for (const QString &item : items) {
            html += QStringLiteral("<li>%1</li>").arg(item.toHtmlEscaped());
        }
        html += QStringLiteral("</ul>");
        return html;
    };

    QString html = QStringLiteral(
        "<div style=\"margin-bottom:10px;padding:10px;border:1px solid #DDEBFA;"
        "border-left:4px solid %1;border-radius:10px;background:#FFFFFF;\">")
                       .arg(scoreColor);
    html += QStringLiteral("<div><b>%1</b> <span style=\"color:#64748B;\">%2 · %3</span></div>")
                .arg(historyEvaluationOutcomeText(evaluation.outcome).toHtmlEscaped(),
                     historyModeText(evaluation.mode).toHtmlEscaped(),
                     evaluation.taskId.toHtmlEscaped());
    html += QStringLiteral(
                "<div style=\"margin-top:4px;color:#334155;\">综合分：<span style=\"color:%1;font-weight:800;\">%2</span> · 步骤成功率：%3 · 工具成功率：%4 · 效率分：%5</div>")
                .arg(scoreColor,
                     percentText(evaluation.overallScore),
                     percentText(evaluation.stepSuccessRate),
                     percentText(evaluation.toolSuccessRate),
                     percentText(evaluation.efficiencyScore));
    html += QStringLiteral(
                "<div style=\"margin-top:4px;color:#64748B;\">耗时：%1ms · 重试：%2 · 工具失败：%3 · 工具阻塞：%4 · 待确认：%5 · 已拒绝：%6</div>")
                .arg(evaluation.durationMs)
                .arg(evaluation.retryTotal)
                .arg(evaluation.failedToolCalls)
                .arg(evaluation.blockedToolCalls)
                .arg(evaluation.pendingPermissions)
                .arg(evaluation.deniedPermissions);
    html += QStringLiteral("<div style=\"margin-top:6px;color:#334155;\">%1</div>")
                .arg(evaluation.summary.toHtmlEscaped());
    html += QStringLiteral("<div style=\"margin-top:8px;\"><b>风险提示</b></div>");
    html += renderList(evaluation.warnings, QStringLiteral("当前没有额外风险提示。"));
    html += QStringLiteral("<div style=\"margin-top:8px;\"><b>下一步建议</b></div>");
    html += renderList(evaluation.recommendations, QStringLiteral("当前无需额外处理。"));
    html += QStringLiteral("</div>");
    return html;
}

QString MainWindow::nodeContractKey(const QString &agentId, const QString &action) const
{
    return QStringLiteral("%1::%2").arg(agentId, action);
}

const WorkflowNodeContractInfo *MainWindow::nodeContractForStep(const QString &agentId, const QString &action) const
{
    const auto it = workflowNodeContractsByStep.constFind(nodeContractKey(agentId, action));
    if (it == workflowNodeContractsByStep.cend()) {
        return nullptr;
    }
    return &it.value();
}

const WorkflowNodeContractInfo *MainWindow::nodeContractForToolName(const QString &toolName) const
{
    const auto it = workflowNodeContractsByTool.constFind(toolName);
    if (it == workflowNodeContractsByTool.cend()) {
        return nullptr;
    }
    return &it.value();
}

QString MainWindow::formatHistoryNodeContractHtml(const WorkflowNodeContractInfo *contract) const
{
    if (!contract) {
        if (!workflowNodeContractsLoaded && workflowNodeContractsError.isEmpty()) {
            return QStringLiteral(
                "<div style=\"margin-top:6px;color:#64748B;\">节点契约：正在加载。</div>");
        }
        if (!workflowNodeContractsError.isEmpty()) {
            return QStringLiteral(
                "<div style=\"margin-top:6px;color:#C2410C;\">节点契约：加载失败 · %1</div>")
                .arg(workflowNodeContractsError.toHtmlEscaped());
        }
        return QStringLiteral(
            "<div style=\"margin-top:6px;color:#64748B;\">节点契约：未登记。</div>");
    }

    auto joinEscaped = [](const QStringList &values, const QString &emptyText) {
        if (values.isEmpty()) {
            return emptyText;
        }
        QStringList escaped;
        escaped.reserve(values.size());
        for (const QString &value : values) {
            escaped.append(value.toHtmlEscaped());
        }
        return escaped.join(QStringLiteral("、"));
    };

    const QString permissions = joinEscaped(contract->requiredPermissions, QStringLiteral("无额外权限"));
    const QString failures = joinEscaped(contract->failureCodes, QStringLiteral("未声明"));
    const QString evaluationSignalText = joinEscaped(contract->evaluationSignals, QStringLiteral("未声明"));
    const QString stateWrites = joinEscaped(contract->stateWrites, QStringLiteral("未声明"));
    const QString inputSchema = formatJsonPreview(contract->inputSchema, 120);
    const QString outputSchema = formatJsonPreview(contract->outputSchema, 120);

    QString html = QStringLiteral(
        "<div style=\"margin-top:6px;padding:8px;border:1px solid #E2E8F0;"
        "border-radius:8px;background:#F8FAFC;color:#475569;\">"
        "<div><b>节点契约</b> <span style=\"color:#64748B;\">%1 · %2</span></div>"
        "<div style=\"margin-top:3px;\">权限：%3 · 状态写入：%4</div>"
        "<div style=\"margin-top:3px;\">失败码：%5</div>"
        "<div style=\"margin-top:3px;\">评估信号：%6</div>")
        .arg(contract->toolName.toHtmlEscaped(),
             contract->nodeType.toHtmlEscaped(),
             permissions,
             stateWrites,
             failures,
             evaluationSignalText);
    if (!inputSchema.isEmpty() || !outputSchema.isEmpty()) {
        html += QStringLiteral(
            "<div style=\"margin-top:3px;font-family:'Consolas','Courier New',monospace;\">输入：%1 · 输出：%2</div>")
            .arg(inputSchema.isEmpty() ? QStringLiteral("{}") : inputSchema,
                 outputSchema.isEmpty() ? QStringLiteral("{}") : outputSchema);
    }
    html += QStringLiteral("</div>");
    return html;
}

QString MainWindow::formatHistoryArtifactsHtml() const
{
    if (currentHistoryArtifacts.isEmpty()) {
        return QStringLiteral("<p style=\"color:#64748B;\">暂无产物记录。</p>");
    }

    QString html = QStringLiteral(
        "<div style=\"margin-bottom:8px;color:#64748B;\">共 %1 项</div>")
                       .arg(currentHistoryArtifacts.size());

    const int displayLimit = 6;
    const int displayCount = qMin(displayLimit, currentHistoryArtifacts.size());
    for (int index = 0; index < displayCount; ++index) {
        html += formatHistoryArtifactItemHtml(currentHistoryArtifacts.at(index));
    }
    if (currentHistoryArtifacts.size() > displayCount) {
        html += QStringLiteral("<p style=\"color:#64748B;\">还有 %1 项未显示。</p>")
                    .arg(currentHistoryArtifacts.size() - displayCount);
    }
    return html;
}

QString MainWindow::formatHistoryArtifactItemHtml(const WorkflowArtifactInfo &item) const
{
    QString borderColor = QStringLiteral("#DDEBFA");
    QString accentColor = QStringLiteral("#2563EB");
    if (item.kind == QStringLiteral("code")) {
        borderColor = QStringLiteral("#BFDBFE");
        accentColor = QStringLiteral("#2563EB");
    } else if (item.kind == QStringLiteral("report")) {
        borderColor = QStringLiteral("#BBF7D0");
        accentColor = QStringLiteral("#059669");
    } else if (item.kind == QStringLiteral("memory")) {
        borderColor = QStringLiteral("#E9D5FF");
        accentColor = QStringLiteral("#7C3AED");
    }

    const QString summaryText = item.summary.isEmpty() ? QStringLiteral("暂无摘要") : item.summary.toHtmlEscaped();
    const QString metadataText = formatJsonPreview(item.metadata, 180);
    const QString uriText = item.uri.isEmpty() ? QStringLiteral("未提供") : item.uri.toHtmlEscaped();
    const QString mimeText = item.mimeType.isEmpty() ? QStringLiteral("unknown") : item.mimeType.toHtmlEscaped();
    const QString stepText = item.stepId.isEmpty() ? QStringLiteral("未知步骤") : item.stepId.toHtmlEscaped();
    const QString agentText = agentDisplayName(item.agentId).toHtmlEscaped();
    const QString verificationHtml = formatHistoryVerificationHtml(historyVerificationForArtifact(item));
    const QString documentContextHtml = formatHistoryDocumentContextHtml(historyDocumentContextForArtifact(item), 2);
    const QString delegatedTaskId = historyArtifactDelegatedTaskId(item);

    QString html = QStringLiteral(
                       "<div style=\"margin-bottom:10px;padding:10px;border:1px solid %1;border-left:4px solid %2;"
                       "border-radius:10px;background:#FFFFFF;\">"
                       "<div><b>%3</b> <span style=\"color:#64748B;\">%4 · %5 · %6</span></div>"
                       "<div style=\"margin-top:4px;color:#334155;\">%7</div>"
                       "<div style=\"margin-top:4px;color:#64748B;\">URI：%8 · MIME：%9 · 时间：%10</div>")
                           .arg(borderColor,
                                accentColor,
                                item.name.isEmpty() ? QStringLiteral("产物") : item.name.toHtmlEscaped(),
                                item.kind.isEmpty() ? QStringLiteral("other") : item.kind.toHtmlEscaped(),
                                agentText,
                                stepText,
                                summaryText,
                                uriText,
                                mimeText,
                                item.createdAt.toHtmlEscaped());
    html += verificationHtml;
    html += documentContextHtml;
    if (!delegatedTaskId.isEmpty()) {
        html += QStringLiteral(
                    "<div style=\"margin-top:5px;color:#1D4ED8;\">"
                    "关联任务：<b>%1</b> · 可在上方产物栏点击“打开”查看关联 Agent 完整运行记录。</div>")
                    .arg(delegatedTaskId.toHtmlEscaped());
    }
    if (!metadataText.isEmpty()) {
        html += QStringLiteral(
                    "<div style=\"margin-top:4px;color:#64748B;\">元数据："
                    "<span style=\"font-family:'Consolas','Courier New',monospace;\">%1</span></div>")
                    .arg(metadataText);
    }
    html += QStringLiteral("</div>");
    return html;
}

QString MainWindow::formatHistoryToolCallsHtml() const
{
    if (currentHistoryToolCalls.isEmpty()) {
        return QStringLiteral("<p style=\"color:#64748B;\">暂无工具调用记录。</p>");
    }

    QString html = QStringLiteral(
        "<div style=\"margin-bottom:8px;color:#64748B;\">共 %1 条</div>")
                       .arg(currentHistoryToolCalls.size());

    const int displayLimit = 6;
    const int displayCount = qMin(displayLimit, currentHistoryToolCalls.size());
    for (int index = 0; index < displayCount; ++index) {
        html += formatHistoryToolCallItemHtml(currentHistoryToolCalls.at(index));
    }
    if (currentHistoryToolCalls.size() > displayCount) {
        html += QStringLiteral("<p style=\"color:#64748B;\">还有 %1 条未显示。</p>")
                    .arg(currentHistoryToolCalls.size() - displayCount);
    }
    return html;
}

QString MainWindow::formatHistoryToolCallItemHtml(const WorkflowToolCallInfo &item) const
{
    QString borderColor = QStringLiteral("#DDEBFA");
    QString accentColor = QStringLiteral("#2563EB");
    if (item.status == QStringLiteral("completed")) {
        borderColor = QStringLiteral("#BBF7D0");
        accentColor = QStringLiteral("#059669");
    } else if (item.status == QStringLiteral("failed")) {
        borderColor = QStringLiteral("#FECACA");
        accentColor = QStringLiteral("#DC2626");
    } else if (item.status == QStringLiteral("blocked")) {
        borderColor = QStringLiteral("#FCD9B8");
        accentColor = QStringLiteral("#C2410C");
    } else if (item.status == QStringLiteral("simulated")) {
        borderColor = QStringLiteral("#E2E8F0");
        accentColor = QStringLiteral("#64748B");
    }

    QString statusText = item.status;
    if (item.status == QStringLiteral("completed")) {
        statusText = QStringLiteral("已完成");
    } else if (item.status == QStringLiteral("failed")) {
        statusText = QStringLiteral("已失败");
    } else if (item.status == QStringLiteral("blocked")) {
        statusText = QStringLiteral("已阻塞");
    } else if (item.status == QStringLiteral("skipped")) {
        statusText = QStringLiteral("已跳过");
    } else if (item.status == QStringLiteral("simulated")) {
        statusText = QStringLiteral("模拟");
    } else if (item.status == QStringLiteral("pending")) {
        statusText = QStringLiteral("待处理");
    }

    const QString permissionText = item.permissionRequired ? QStringLiteral("需要") : QStringLiteral("无需");
    const QString riskText = historyRiskText(item.riskLevel).toHtmlEscaped();
    const QString requestText = formatJsonPreview(item.request, 160);
    const QString verificationHtml = formatHistoryVerificationHtml(historyVerificationFromToolResult(item.result));
    const QString documentContextHtml = formatHistoryDocumentContextHtml(
        historyDocumentContextFromToolResult(item.result),
        2);
    const QString searchResultHtml = item.toolName == QStringLiteral("document.search_text")
        ? formatWorkspaceSearchResultHtml(item.result, 4)
        : QString();
    const QString resultText = searchResultHtml.isEmpty() ? formatJsonPreview(item.result, 160) : QString();
    const QString errorText = item.error.isEmpty() ? QStringLiteral("无") : item.error.toHtmlEscaped();

    QString html = QStringLiteral(
                       "<div style=\"margin-bottom:10px;padding:10px;border:1px solid %1;border-left:4px solid %2;"
                       "border-radius:10px;background:#FFFFFF;\">"
                       "<div><b>%3</b> <span style=\"color:#64748B;\">%4 · %5 · %6</span></div>"
                       "<div style=\"margin-top:4px;color:#334155;\">%7</div>"
                       "<div style=\"margin-top:4px;color:#64748B;\">状态：<span style=\"color:%2;font-weight:800;\">%8</span>"
                       " · 风险：%9 · 权限：%10 · 尝试：%11/%12 · 耗时：%13ms</div>")
                           .arg(borderColor,
                                accentColor,
                                item.toolName.isEmpty() ? QStringLiteral("工具调用") : item.toolName.toHtmlEscaped(),
                                agentDisplayName(item.agentId).toHtmlEscaped(),
                                item.stepId.isEmpty() ? QStringLiteral("未知步骤") : item.stepId.toHtmlEscaped(),
                                item.callId.toHtmlEscaped(),
                                item.error.isEmpty() ? QStringLiteral("无错误") : QStringLiteral("调用结果需要查看下方详情"),
                                statusText.toHtmlEscaped(),
                                riskText,
                                permissionText,
                                QString::number(item.attempt),
                                QString::number(item.maxAttempts),
                                QString::number(item.durationMs));

    html += QStringLiteral("<div style=\"margin-top:4px;color:#64748B;\">失败次数：%1 · 超时：%2ms</div>")
                .arg(QString::number(item.failureCount), QString::number(item.timeoutMs));
    html += formatHistoryNodeContractHtml(nodeContractForToolName(item.toolName));
    html += verificationHtml;
    html += documentContextHtml;
    if (!requestText.isEmpty()) {
        html += QStringLiteral(
                    "<div style=\"margin-top:4px;color:#475569;\">请求："
                    "<span style=\"font-family:'Consolas','Courier New',monospace;\">%1</span></div>")
                    .arg(requestText);
    }
    if (!resultText.isEmpty()) {
        html += QStringLiteral(
                    "<div style=\"margin-top:4px;color:#475569;\">结果："
                    "<span style=\"font-family:'Consolas','Courier New',monospace;\">%1</span></div>")
                    .arg(resultText);
    }
    html += searchResultHtml;
    html += QStringLiteral(
                "<div style=\"margin-top:4px;color:#64748B;\">错误：%1 · 开始：%2 · 结束：%3</div>")
                .arg(errorText,
                     item.startedAt.toHtmlEscaped().isEmpty() ? QStringLiteral("未开始") : item.startedAt.toHtmlEscaped(),
                     item.finishedAt.toHtmlEscaped().isEmpty() ? QStringLiteral("未结束") : item.finishedAt.toHtmlEscaped());
    html += QStringLiteral("</div>");
    return html;
}

QString MainWindow::formatHistoryUpdatesHtml() const
{
    if (currentHistoryUpdates.isEmpty()) {
        return QStringLiteral("<p style=\"color:#64748B;\">暂无事件流。</p>");
    }

    int warningCount = 0;
    int errorCount = 0;
    int artifactCount = 0;
    for (const WorkflowTaskUpdateInfo &update : currentHistoryUpdates) {
        if (update.level == QStringLiteral("warning")) {
            ++warningCount;
        } else if (update.level == QStringLiteral("error")) {
            ++errorCount;
        }
        if (update.updateType == QStringLiteral("artifact")) {
            ++artifactCount;
        }
    }

    QString html = QStringLiteral(
        "<div style=\"margin-bottom:8px;color:#64748B;\">共 %1 条 · 警告 %2 · 错误 %3 · 产物事件 %4</div>")
                       .arg(currentHistoryUpdates.size())
                       .arg(warningCount)
                       .arg(errorCount)
                       .arg(artifactCount);

    const int displayLimit = 10;

    int retrospectiveIndex = -1;
    for (int index = currentHistoryUpdates.size() - 1; index >= 0; --index) {
        const WorkflowTaskUpdateInfo &update = currentHistoryUpdates.at(index);
        if (update.event == QStringLiteral("task_state_snapshot")
            && update.payload.contains(QStringLiteral("task_retrospective"))) {
            retrospectiveIndex = index;
            break;
        }
    }

    const bool pinRetrospective = currentHistoryUpdates.size() > displayLimit && retrospectiveIndex >= 0;
    const int recentDisplayLimit = pinRetrospective ? displayLimit - 1 : displayLimit;
    QList<int> displayIndexes;
    displayIndexes.reserve(qMin(recentDisplayLimit, currentHistoryUpdates.size()));
    for (int index = currentHistoryUpdates.size() - 1;
         index >= 0 && displayIndexes.size() < recentDisplayLimit;
         --index) {
        if (pinRetrospective && index == retrospectiveIndex) {
            continue;
        }
        displayIndexes.prepend(index);
    }

    if (pinRetrospective) {
        // 状态快照通常排在时间线最后；长任务只展示一小段时，用户最需要先看到复盘。
        // 下面的事件改为“最近事件”，既保留排查线索，也避免旧的 connected/task_started 挤占首屏。
        html += formatTaskRetrospectiveHtml(currentHistoryUpdates.at(retrospectiveIndex).payload);
    }

    if (currentHistoryUpdates.size() > displayLimit) {
        html += QStringLiteral(
                    "<div style=\"margin:6px 0 8px;color:#64748B;\">"
                    "下方显示最近 %1 条事件，早期事件可在执行日志和详情分区继续查看。</div>")
                    .arg(displayIndexes.size());
    }

    for (int index : displayIndexes) {
        html += formatHistoryUpdateItemHtml(currentHistoryUpdates.at(index));
    }
    const int hiddenCount = currentHistoryUpdates.size()
        - displayIndexes.size()
        - (pinRetrospective ? 1 : 0);
    if (hiddenCount > 0) {
        html += QStringLiteral("<p style=\"color:#64748B;\">还有 %1 条早期事件未展开。</p>")
                    .arg(hiddenCount);
    }
    return html;
}

QString MainWindow::formatHistoryUpdateItemHtml(const WorkflowTaskUpdateInfo &item) const
{
    QString borderColor = QStringLiteral("#DDEBFA");
    QString accentColor = QStringLiteral("#2563EB");
    if (item.level == QStringLiteral("warning")) {
        borderColor = QStringLiteral("#FDE68A");
        accentColor = QStringLiteral("#D97706");
    } else if (item.level == QStringLiteral("error")) {
        borderColor = QStringLiteral("#FECACA");
        accentColor = QStringLiteral("#DC2626");
    } else if (item.updateType == QStringLiteral("artifact")) {
        borderColor = QStringLiteral("#BBF7D0");
        accentColor = QStringLiteral("#059669");
    } else if (item.updateType == QStringLiteral("state")) {
        borderColor = QStringLiteral("#C7D2FE");
        accentColor = QStringLiteral("#4F46E5");
    }

    const QString typeText = historyUpdateTypeText(item.updateType).toHtmlEscaped();
    const QString titleText = item.title.isEmpty() ? item.event.toHtmlEscaped() : item.title.toHtmlEscaped();
    const QString agentText = item.agentId.isEmpty() ? QStringLiteral("系统") : agentDisplayName(item.agentId).toHtmlEscaped();
    const QString stepText = item.stepId.isEmpty() ? QStringLiteral("全局") : item.stepId.toHtmlEscaped();
    const QString statusText = item.status.isEmpty() ? QStringLiteral("无状态") : historyStatusText(item.status).toHtmlEscaped();
    const QString timeText = item.occurredAt.isEmpty() ? QStringLiteral("未记录时间") : item.occurredAt.toHtmlEscaped();
    const QString searchResultHtml = formatWorkspaceSearchResultFromUpdatePayloadHtml(item.payload, 2);
    const QString verificationHtml = formatHistoryVerificationHtml(historyVerificationFromUpdatePayload(item.payload));
    const QString documentContextHtml = formatHistoryDocumentContextHtml(
        historyDocumentContextFromUpdatePayload(item.payload),
        2);
    const QString retrospectiveHtml = formatTaskRetrospectiveHtml(item.payload);
    const QString payloadText = (searchResultHtml.isEmpty() && retrospectiveHtml.isEmpty())
        ? formatJsonPreview(item.payload, 120)
        : QString();

    QString html = QStringLiteral(
                       "<div style=\"margin-bottom:8px;padding:9px;border:1px solid %1;border-left:4px solid %2;"
                       "border-radius:8px;background:#FFFFFF;\">"
                       "<div><b>%3</b> <span style=\"color:#64748B;\">#%4 · %5 · %6</span></div>"
                       "<div style=\"margin-top:4px;color:#334155;\">%7</div>"
                       "<div style=\"margin-top:4px;color:#64748B;\">%8 · %9 · %10 · %11</div>")
                           .arg(borderColor,
                                accentColor,
                                titleText,
                                QString::number(item.sequence),
                                typeText,
                                item.event.toHtmlEscaped(),
                                item.message.toHtmlEscaped(),
                                agentText,
                                stepText,
                                statusText,
                                timeText);
    if (!payloadText.isEmpty()) {
        html += QStringLiteral(
                    "<div style=\"margin-top:4px;color:#64748B;\">上下文："
                    "<span style=\"font-family:'Consolas','Courier New',monospace;\">%1</span></div>")
                    .arg(payloadText);
    }
    html += searchResultHtml;
    html += verificationHtml;
    html += documentContextHtml;
    html += retrospectiveHtml;
    html += QStringLiteral("</div>");
    return html;
}

QString MainWindow::formatTaskRetrospectiveHtml(const QJsonObject &payload, bool compact) const
{
    QJsonObject retrospective = payload.value(QStringLiteral("task_retrospective")).toObject();
    if (retrospective.isEmpty()) {
        // 预留兼容键：如果后端未来把复盘从 task_retrospective 改名，旧 UI 仍能读到。
        retrospective = payload.value(QStringLiteral("retrospective")).toObject();
    }
    if (retrospective.isEmpty()) {
        return QString();
    }

    auto percentText = [](double value) {
        if (value < 0.0) {
            return QStringLiteral("未知");
        }
        const int percent = static_cast<int>(value * 100.0 + 0.5);
        return QStringLiteral("%1%").arg(qBound(0, percent, 100));
    };
    auto limitedStringList = [](const QJsonArray &array, int limit) {
        QStringList items;
        const int count = qMin(qMax(0, limit), array.size());
        items.reserve(count);
        for (int index = 0; index < count; ++index) {
            const QString text = array.at(index).toString().trimmed();
            if (!text.isEmpty()) {
                items.append(text);
            }
        }
        return items;
    };

    const QString modeText = historyModeText(retrospective.value(QStringLiteral("mode")).toString()).toHtmlEscaped();
    const QString statusText = historyStatusText(retrospective.value(QStringLiteral("status")).toString()).toHtmlEscaped();
    const QString summary = retrospective.value(QStringLiteral("summary")).toString().toHtmlEscaped();
    const QJsonObject score = retrospective.value(QStringLiteral("score")).toObject();
    const QJsonObject facts = retrospective.value(QStringLiteral("facts")).toObject();
    const QStringList recommendations = limitedStringList(
        retrospective.value(QStringLiteral("recommendations")).toArray(),
        compact ? 2 : 4);
    const QStringList warnings = compact
        ? QStringList()
        : limitedStringList(retrospective.value(QStringLiteral("warnings")).toArray(), 3);
    const QJsonArray delegations = retrospective.value(QStringLiteral("delegations")).toArray();

    QString html = QStringLiteral(
        "<div style=\"margin-top:6px;padding:8px;border:1px solid #C7D2FE;"
        "border-left:4px solid #4F46E5;border-radius:8px;background:#EEF2FF;color:#334155;\">"
        "<div><b>任务复盘</b> <span style=\"color:#64748B;\">%1 · %2 · 综合 %3</span></div>")
        .arg(modeText,
             statusText,
             percentText(score.value(QStringLiteral("overall")).toDouble(-1.0)).toHtmlEscaped());

    if (!summary.isEmpty()) {
        html += QStringLiteral("<div style=\"margin-top:4px;\">%1</div>").arg(summary);
    }

    html += QStringLiteral(
        "<div style=\"margin-top:4px;color:#64748B;\">步骤 %1/%2 · 工具 %3 · 产物 %4 · 权限 %5 · 重试 %6 · 耗时 %7ms</div>")
        .arg(facts.value(QStringLiteral("step_completed")).toInt())
        .arg(facts.value(QStringLiteral("step_total")).toInt())
        .arg(facts.value(QStringLiteral("tool_call_total")).toInt())
        .arg(facts.value(QStringLiteral("artifact_total")).toInt())
        .arg(facts.value(QStringLiteral("permission_total")).toInt())
        .arg(facts.value(QStringLiteral("retry_total")).toInt())
        .arg(facts.value(QStringLiteral("duration_ms")).toInt());

    if (!warnings.isEmpty()) {
        html += QStringLiteral("<div style=\"margin-top:5px;color:#92400E;\"><b>注意</b>%1</div>")
                    .arg(dispatchBulletListHtml(warnings));
    }
    if (!recommendations.isEmpty()) {
        html += QStringLiteral("<div style=\"margin-top:5px;\"><b>建议</b>%1</div>")
                    .arg(dispatchBulletListHtml(recommendations));
    }
    if (!delegations.isEmpty()) {
        html += QStringLiteral("<div style=\"margin-top:6px;padding-top:6px;border-top:1px solid #C7D2FE;\"><b>总指挥交付汇总</b></div>");
        const int delegationLimit = compact ? 1 : 4;
        for (int index = 0; index < delegations.size() && index < delegationLimit; ++index) {
            const QJsonObject delegation = delegations.at(index).toObject();
            const QString agentName = agentDisplayName(delegation.value(QStringLiteral("agent_id")).toString()).toHtmlEscaped();
            const QString delegationStatus = historyStatusText(delegation.value(QStringLiteral("status")).toString()).toHtmlEscaped();
            const QString taskId = delegation.value(QStringLiteral("task_id")).toString().toHtmlEscaped();
            const QString delegationSummary = delegation.value(QStringLiteral("summary")).toString().toHtmlEscaped();
            const QString nextAction = delegation.value(QStringLiteral("next_action")).toString().toHtmlEscaped();
            const int mapCompleted = delegation.value(QStringLiteral("map_completed")).toInt();
            const int mapTotal = delegation.value(QStringLiteral("map_total")).toInt();
            const int reduceCompleted = delegation.value(QStringLiteral("reduce_completed")).toInt();
            const int reduceTotal = delegation.value(QStringLiteral("reduce_total")).toInt();
            QString progressHtml;
            if (mapTotal > 0 || reduceTotal > 0) {
                QStringList progressParts;
                if (mapTotal > 0) {
                    progressParts.append(QStringLiteral("Map %1/%2").arg(mapCompleted).arg(mapTotal));
                }
                if (reduceTotal > 0) {
                    progressParts.append(QStringLiteral("Reduce %1/%2").arg(reduceCompleted).arg(reduceTotal));
                }
                progressHtml = QStringLiteral("<div style=\"margin-top:3px;color:#475569;\">进度：%1</div>")
                    .arg(progressParts.join(QStringLiteral(" · ")));
            }
            html += QStringLiteral(
                        "<div style=\"margin-top:4px;padding:6px;border:1px solid #BFDBFE;border-radius:6px;background:#FFFFFF;\">"
                        "<b>%1</b> · %2 <span style=\"color:#64748B;\">关联任务 %3</span>%4%5%6</div>")
                        .arg(agentName,
                             delegationStatus,
                             taskId,
                             delegationSummary.isEmpty()
                                 ? QString()
                                 : QStringLiteral("<div style=\"margin-top:3px;\">%1</div>").arg(delegationSummary),
                             progressHtml,
                             nextAction.isEmpty()
                                 ? QString()
                                 : QStringLiteral("<div style=\"margin-top:3px;color:#475569;\">%1</div>").arg(nextAction));
        }
    }

    html += QStringLiteral("</div>");
    return html;
}

QString MainWindow::historyUpdateTypeText(const QString &type) const
{
    if (type == QStringLiteral("lifecycle")) {
        return QStringLiteral("生命周期");
    }
    if (type == QStringLiteral("step")) {
        return QStringLiteral("步骤");
    }
    if (type == QStringLiteral("tool_call")) {
        return QStringLiteral("工具");
    }
    if (type == QStringLiteral("permission")) {
        return QStringLiteral("权限");
    }
    if (type == QStringLiteral("artifact")) {
        return QStringLiteral("产物");
    }
    if (type == QStringLiteral("state")) {
        return QStringLiteral("状态");
    }
    return type.isEmpty() ? QStringLiteral("事件") : type;
}

QString MainWindow::formatHistoryStepsHtml() const
{
    if (currentHistorySteps.isEmpty()) {
        return QStringLiteral("<p style=\"color:#64748B;\">暂无 step 级结果。</p>");
    }

    int completedCount = 0;
    int runningCount = 0;
    int blockedCount = 0;
    int failedCount = 0;
    int skippedCount = 0;
    for (const WorkflowStepRunInfo &step : currentHistorySteps) {
        if (step.status == QStringLiteral("completed")) {
            ++completedCount;
        } else if (step.status == QStringLiteral("running")) {
            ++runningCount;
        } else if (step.status == QStringLiteral("blocked")) {
            ++blockedCount;
        } else if (step.status == QStringLiteral("failed")) {
            ++failedCount;
        } else if (step.status == QStringLiteral("skipped")) {
            ++skippedCount;
        }
    }

    QString html = QStringLiteral(
        "<div style=\"margin-bottom:8px;color:#64748B;\">"
        "共 %1 步 · 已完成 %2 · 进行中 %3 · 阻塞 %4 · 失败 %5 · 跳过 %6"
        "</div>")
                       .arg(currentHistorySteps.size())
                       .arg(completedCount)
                       .arg(runningCount)
                       .arg(blockedCount)
                       .arg(failedCount)
                       .arg(skippedCount);

    for (const WorkflowStepRunInfo &step : currentHistorySteps) {
        html += formatHistoryStepItemHtml(step);
    }
    return html;
}

QString MainWindow::formatHistoryStepItemHtml(const WorkflowStepRunInfo &item) const
{
    QString borderColor = QStringLiteral("#C8DEFF");
    QString statusColor = QStringLiteral("#2563EB");
    const QString statusLabel = historyStepStatusText(item.status);
    if (item.status == QStringLiteral("completed")) {
        borderColor = QStringLiteral("#BBF7D0");
        statusColor = QStringLiteral("#059669");
    } else if (item.status == QStringLiteral("running")) {
        borderColor = QStringLiteral("#BFDBFE");
        statusColor = QStringLiteral("#2563EB");
    } else if (item.status == QStringLiteral("blocked")) {
        borderColor = QStringLiteral("#FED7AA");
        statusColor = QStringLiteral("#C2410C");
    } else if (item.status == QStringLiteral("failed")) {
        borderColor = QStringLiteral("#FECACA");
        statusColor = QStringLiteral("#DC2626");
    } else if (item.status == QStringLiteral("skipped")) {
        borderColor = QStringLiteral("#E2E8F0");
        statusColor = QStringLiteral("#64748B");
    }

    const QString agentName = agentDisplayName(item.agent).toHtmlEscaped();
    const QString actionText = item.action.isEmpty() ? QStringLiteral("无动作") : item.action.toHtmlEscaped();
    const QString messageText = item.message.isEmpty() ? QStringLiteral("无额外说明") : item.message.toHtmlEscaped();
    const QString riskText = historyRiskText(item.riskLevel).toHtmlEscaped();
    const QString confirmationText = item.requiresConfirmation ? QStringLiteral("需要确认")
                                                               : QStringLiteral("无需确认");

    QStringList outputParts;
    const QString permissionSummary = item.output.value(QStringLiteral("permission_summary")).toString();
    const QString expectedOutput = item.output.value(QStringLiteral("expected_output")).toString();
    const QString wouldExecute = item.output.value(QStringLiteral("would_execute")).toString();
    const WorkflowNodeContractInfo *contract = nodeContractForStep(item.agent, item.action);
    if (!permissionSummary.isEmpty()) {
        outputParts.append(QStringLiteral("权限：%1").arg(permissionSummary));
    }
    if (!expectedOutput.isEmpty()) {
        outputParts.append(QStringLiteral("预期产出：%1").arg(expectedOutput));
    }
    if (!wouldExecute.isEmpty()) {
        outputParts.append(QStringLiteral("动作：%1").arg(wouldExecute));
    }
    const QString outputText = outputParts.isEmpty() ? QStringLiteral("输出摘要未提供") : outputParts.join(QStringLiteral(" · "));
    const QString searchResultHtml = formatWorkspaceSearchResultFromStepOutputHtml(item.output, 4);
    const QString verificationHtml = formatHistoryVerificationHtml(historyVerificationFromStepOutput(item.output));
    const QString documentContextHtml = formatHistoryDocumentContextHtml(
        historyDocumentContextFromStepOutput(item.output),
        2);

    QString html = QStringLiteral(
               "<div style=\"margin-bottom:10px;padding:10px;border:1px solid %1;border-left:4px solid %2;"
               "border-radius:10px;background:#FFFFFF;\">"
               "<div><b>%3</b> <span style=\"color:#64748B;\">%4 · %5</span></div>"
               "<div style=\"margin-top:4px;color:#334155;\">%6</div>"
               "<div style=\"margin-top:4px;color:#64748B;\">状态：<span style=\"color:%2;font-weight:800;\">%7</span>"
               " · 风险：%8 · 确认：%9</div>"
               "<div style=\"margin-top:4px;color:#475569;\">%10</div>"
               "<div style=\"margin-top:4px;color:#64748B;\">%11</div>")
        .arg(borderColor,
             statusColor,
             item.stepId.isEmpty() ? QStringLiteral("步骤") : item.stepId.toHtmlEscaped(),
             agentName,
             actionText,
             statusLabel.toHtmlEscaped(),
             statusLabel.toHtmlEscaped(),
             riskText,
             confirmationText.toHtmlEscaped(),
             messageText,
             outputText.toHtmlEscaped());
    html += searchResultHtml;
    html += verificationHtml;
    html += documentContextHtml;
    html += formatHistoryNodeContractHtml(contract);
    html += QStringLiteral("</div>");
    return html;
}

QString MainWindow::historyStepStatusText(const QString &status) const
{
    if (status == QStringLiteral("completed")) {
        return QStringLiteral("已完成");
    }
    if (status == QStringLiteral("running")) {
        return QStringLiteral("进行中");
    }
    if (status == QStringLiteral("blocked")) {
        return QStringLiteral("已阻塞");
    }
    if (status == QStringLiteral("failed")) {
        return QStringLiteral("已失败");
    }
    if (status == QStringLiteral("skipped")) {
        return QStringLiteral("已跳过");
    }
    if (status == QStringLiteral("pending")) {
        return QStringLiteral("待处理");
    }
    return status.isEmpty() ? QStringLiteral("未知") : status;
}

QString MainWindow::historyRuntimeBadgeObjectName(const QString &status) const
{
    if (status == QStringLiteral("completed")) {
        return QStringLiteral("badgeGreen");
    }
    if (status == QStringLiteral("waiting_permission")) {
        return QStringLiteral("badgeOrange");
    }
    if (status == QStringLiteral("failed")
        || status == QStringLiteral("blocked")
        || status == QStringLiteral("cancelled")) {
        return QStringLiteral("badgeGray");
    }
    return QStringLiteral("badgeBlue");
}

void MainWindow::showHistoryEmptyState(const QString &message)
{
    currentHistoryTaskId.clear();
    currentHistorySummary.clear();
    currentHistoryStatus.clear();
    currentHistoryMode.clear();
    currentHistoryRiskLevel.clear();
    currentHistoryUpdatedAt.clear();
    currentHistoryRequiresConfirmation = false;
    currentHistoryConfirmationAcknowledged = false;
    currentHistoryPlanSummary = WorkflowPlanSummaryInfo{};
    currentHistoryPlanSteps.clear();
    currentHistorySteps.clear();
    currentHistoryEvents.clear();
    currentHistoryPermissions.clear();
    currentHistoryRuntimeState = WorkflowRuntimeStateInfo{};
    currentHistoryMetrics = WorkflowRuntimeMetricsResult{};
    currentHistoryModelRoutes.clear();
    currentHistoryEvaluation = WorkflowTaskEvaluationResult{};
    currentHistoryArtifacts.clear();
    currentHistoryToolCalls.clear();
    currentHistoryUpdates.clear();
    currentHistoryArtifactId.clear();
    pendingHistoryArtifactPreviewTaskId.clear();
    pendingHistoryArtifactPreviewId.clear();
    pendingPermissionApprovalQueue.clear();
    historyPermissionApprovalInProgress = false;
    historyArtifactPreviewInProgress = false;
    setHistoryConfirmationExpanded(false);
    currentHistoryStepCount = 0;
    currentHistoryPlanLoaded = false;
    currentHistoryStepsLoaded = false;
    currentHistoryLogsLoaded = false;
    currentHistoryPermissionsLoaded = false;
    currentHistoryRuntimeStateLoaded = false;
    currentHistoryMetricsLoaded = false;
    currentHistoryModelRoutesLoaded = false;
    currentHistoryEvaluationLoaded = false;
    currentHistoryArtifactsLoaded = false;
    currentHistoryToolCallsLoaded = false;
    currentHistoryUpdatesLoaded = false;
    currentHistoryPlanError.clear();
    currentHistoryStepsError.clear();
    currentHistoryLogsError.clear();
    currentHistoryPermissionsError.clear();
    currentHistoryRuntimeStateError.clear();
    currentHistoryMetricsError.clear();
    currentHistoryModelRoutesError.clear();
    currentHistoryEvaluationError.clear();
    currentHistoryArtifactsError.clear();
    currentHistoryToolCallsError.clear();
    currentHistoryUpdatesError.clear();

    if (historySelectionTitle) {
        historySelectionTitle->setText(QStringLiteral("暂无选择"));
    }
    if (historySelectionMeta) {
        historySelectionMeta->setText(message);
    }
    if (historySelectionBadge) {
        historySelectionBadge->setStyleSheet(QString());
        polishBadge(historySelectionBadge, QStringLiteral("badgeGray"));
        historySelectionBadge->setText(QStringLiteral("空"));
    }
    if (historyDetailText) {
        historyDetailText->setHtml(
            QStringLiteral("<p style=\"color:#64748B;\">%1</p>").arg(message.toHtmlEscaped()));
    }
    if (historyConfirmationSection) {
        historyConfirmationSection->setVisible(false);
    }
    updateHistoryModelRoutesButton();
    refreshHistoryArtifactToolbar();
    updateHistoryRuntimePanel();
    updateHistoryActionButtons();
}

QString MainWindow::historyStatusBadgeObjectName(const QString &status) const
{
    if (status == QStringLiteral("completed")) {
        return QStringLiteral("badgeGreen");
    }
    if (status == QStringLiteral("waiting_permission")) {
        return QStringLiteral("badgeOrange");
    }
    if (status == QStringLiteral("failed")
        || status == QStringLiteral("blocked")
        || status == QStringLiteral("cancelled")) {
        return QStringLiteral("badgeGray");
    }
    return QStringLiteral("badgeBlue");
}

QString MainWindow::formatHistoryLogHtml(const TaskLogEvent &event) const
{
    // 把日志级别和关键事件统一成一套 HTML 呈现，实时流和历史回放可以共用。
    QString color = QStringLiteral("#2563EB");
    QString levelLabel = QStringLiteral("信息");
    if (event.level == QStringLiteral("warning")) {
        color = QStringLiteral("#F59E0B");
        levelLabel = QStringLiteral("警告");
    } else if (event.level == QStringLiteral("error")) {
        color = QStringLiteral("#DC2626");
        levelLabel = QStringLiteral("错误");
    }

    QString eventLabel = event.event;
    if (event.event == QStringLiteral("confirmation_required")) {
        eventLabel = QStringLiteral("确认提醒");
    } else if (event.event == QStringLiteral("permission_auto_approved")) {
        eventLabel = QStringLiteral("策略自动批准");
    }

    return QStringLiteral(
               "<div style=\"margin-bottom:10px;\">"
               "<div><span style=\"color:%1;font-weight:800;\">%2</span> "
               "<span style=\"color:#64748B;\">#%3 · %4 · %5</span></div>"
               "<div style=\"margin-top:4px;color:#334155;\">%6</div>"
               "</div>")
        .arg(color,
             levelLabel,
             QString::number(event.sequence),
             agentDisplayName(event.agentId).toHtmlEscaped(),
             eventLabel.toHtmlEscaped(),
             event.message.toHtmlEscaped());
}

void MainWindow::switchPage(int index)
{
    ui->contentStack->setCurrentIndex(index);

    switch (index) {
    case 0:
        updateHeader("总览", "快速启动任务、查看智能应用与系统状态");
        setActiveNavButton(ui->navOverviewButton);
        break;
    case 1:
        updateHeader("AI调度台", "统一规划任务、调度应用与结果汇总");
        setActiveNavButton(ui->navDispatchButton);
        break;
    case 2:
        updateHeader("应用中心", "安装、启用与扩展你的智能应用");
        setActiveNavButton(ui->navAppsButton);
        break;
    case 3:
        updateHeader("工作流设计器", "通过节点编排搭建自动化 AI 流程");
        setActiveNavButton(ui->navWorkflowButton);
        break;
    case 4:
        // 主工作台已收束为三项可交付任务；旧的摘要、问答等能力仅作内部步骤或历史兼容，
        // 不再把它们误呈现为客户要在这里选择的产品能力。
        updateHeader("文档助手", "项目方案制作、项目文档审查与论文审查");
        setActiveNavButton(ui->navDocumentButton);
        // 页面第一次打开，或启动期列表请求失败后回到这里，都重新读取轻量清单；不会解析正文、
        // 建索引或调用模型。这样客户不会被陈旧的“等待后端加载文档”占位卡住。
        if (!documentWorkspaceLoaded && !documentWorkspaceLoading) {
            refreshDocumentAgentDocuments();
        }
        break;
    case 5:
        updateHeader("代码工坊", "代码生成、解释、运行诊断与项目结构分析");
        setActiveNavButton(ui->navCodeButton);
        break;
    case 6:
        updateHeader("数据工作台", "导入 Excel/CSV，先完成本地数据画像，再生成可编辑分析交付");
        setActiveNavButton(ui->navDataButton);
        // 首次进入才加载列表；后续由导入成功、刷新按钮或文件选择变化驱动，避免页面切换时
        // 重复扫描本地数据目录和重复解析工作簿。
        if (!dataWorkspaceLoaded && !dataWorkspaceLoading) {
            refreshDataDatasets();
        } else if (dataWorkspaceLoaded && !dataProfileReady && !dataProfileLoading) {
            // 材料选择器可在后台同步数据目录，但不应为此触发整表画像。客户真正进入数据
            // 工作台时才补读当前选择，保持首屏更快且不改变数据页原有的画像交付。
            requestSelectedDataDatasetProfile();
        }
        break;
    case 7:
        updateHeader("视觉工作室", "图像理解、目标检测、风格生成与视觉分析");
        setActiveNavButton(ui->navVisionButton);
        break;
    case 8:
        updateHeader("音视频工坊", "抽帧、转码、字幕、封面和视频分析");
        setActiveNavButton(ui->navVideoButton);
        break;
    case 9:
        updateHeader("知识库", "本机资料入库、索引状态与受控检索准备");
        setActiveNavButton(ui->navKnowledgeButton);
        // 切换页面只刷新轻量状态，不会解析材料或下载模型。真正的索引始终需要用户点击。
        refreshKnowledgeBases();
        break;
    case 10:
        updateHeader("插件管理", "管理受控 MCP 连接与权限边界");
        setActiveNavButton(ui->navPluginsButton);
        if (backendManager && backendManager->isReady()) {
            refreshMcpConnections();
        }
        break;
    case 11:
        updateHeader("模型密钥", "管理全局模型和每个 Agent 的独立 API Key");
        setActiveNavButton(ui->navModelButton);
        if (backendManager->isReady()) {
            if (currentModelProviders.isEmpty() && currentModelStatus.provider.isEmpty()) {
                refreshModelProviders();
            } else {
                updateModelSummaryPanel();
                updateModelDetailPanel();
            }
        } else {
            showModelEmptyState(QStringLiteral("后端尚未就绪，等待连接后自动加载模型供应商。"));
        }
        break;
    case 12:
        updateHeader("历史任务", "查看任务执行记录、生成文件与失败日志");
        setActiveNavButton(ui->navHistoryButton);
        if (backendManager->isReady()) {
            refreshTaskHistory();
        } else {
            showHistoryEmptyState(QStringLiteral("后端尚未就绪，等待连接后自动加载历史任务。"));
        }
        break;
    case 13:
        updateHeader("系统设置", "通用设置、安全策略、存储缓存、日志诊断");
        setActiveNavButton(ui->navSettingsButton);
        break;
    case 14:
        // 结果详情是文档助手的聚焦阅读子页，仍沿用文档助手的导航语义。
        updateHeader("文档结果", "查看可追溯结论、需求、对比与来源");
        setActiveNavButton(ui->navDocumentButton);
        break;
    default:
        break;
    }
}

void MainWindow::updateHeader(const QString &title, const QString &subtitle)
{
    ui->pageTitleLabel->setText(title);
    ui->pageSubtitleLabel->setText(subtitle);
}

void MainWindow::setActiveNavButton(QPushButton *activeButton)
{
    for (QPushButton *button : navigationButtons()) {
        button->setProperty("active", button == activeButton);
        button->style()->unpolish(button);
        button->style()->polish(button);
        button->update();
    }
}

void MainWindow::setBackendConnectingState()
{
    ui->ambientTextLabel->setText(QStringLiteral("AgentFlow · 正在检测或启动本地后端 127.0.0.1:8765 · 本地工作台模式"));
    ui->ambientStatusBadge->setText(QStringLiteral("启动中"));
    ui->systemStatusTitle->setText(QStringLiteral("系统状态"));
    ui->apiUsageLabel->setText(QStringLiteral("后端服务：检测中"));
    ui->taskUsageLabel->setText(QStringLiteral("Agent 注册表：等待加载"));
    ui->apiUsageProgress->setValue(0);
}

void MainWindow::updateBackendHealth(bool ok, const QString &message)
{
    if (!ok) {
        if (backendManager && !backendManager->isReady()) {
            ui->backendRetryButton->setVisible(true);
            ui->backendRetryButton->setEnabled(true);
            ui->backendRetryButton->setText(QStringLiteral("重试后端"));
        }
        ui->ambientTextLabel->setText(QStringLiteral("AgentFlow · 后端未连接 · 可手动启动 FastAPI 服务后重试"));
        ui->ambientStatusBadge->setText(QStringLiteral("后端离线"));
        ui->apiUsageLabel->setText(QStringLiteral("后端服务：未连接 · %1").arg(message));
        ui->taskUsageLabel->setText(QStringLiteral("Agent 注册表：未加载"));
        ui->apiUsageProgress->setValue(0);
        if (ui->contentStack->currentIndex() == 11) {
            if (currentModelProviders.isEmpty()) {
                showModelEmptyState(QStringLiteral("后端未连接，暂时无法读取模型供应商。"));
            } else if (modelSummaryLabel) {
                modelSummaryLabel->setText(QStringLiteral("后端离线，当前保留上次加载的模型供应商缓存。"));
            }
            if (modelRefreshButton) {
                modelRefreshButton->setEnabled(false);
            }
        }
        return;
    }

    ui->backendRetryButton->setVisible(false);
    ui->backendRetryButton->setEnabled(true);
    ui->backendRetryButton->setText(QStringLiteral("重试后端"));
    ui->ambientTextLabel->setText(QStringLiteral("AgentFlow · 后端在线 · 正在加载 Agent 注册表"));
    ui->ambientStatusBadge->setText(QStringLiteral("后端在线"));
    ui->apiUsageLabel->setText(QStringLiteral("后端服务：%1").arg(message));
    ui->taskUsageLabel->setText(QStringLiteral("Agent 注册表：正在加载"));
    ui->apiUsageProgress->setValue(35);

    if (ui->contentStack->currentIndex() == 12) {
        refreshTaskHistory();
    }
    if (ui->contentStack->currentIndex() == 11) {
        if (currentModelProviders.isEmpty() && currentModelStatus.provider.isEmpty()) {
            refreshModelProviders();
        } else {
            if (modelRefreshButton) {
                modelRefreshButton->setEnabled(true);
                modelRefreshButton->setText(QStringLiteral("刷新"));
            }
            updateModelSummaryPanel();
        }
    }
}

void MainWindow::updateAgentCards(const QList<AgentInfo> &agents)
{
    updateAgentCardSet(agents, 0, 8);
    updateAgentCardSet(agents, 100, 12);

    ui->ambientTextLabel->setText(QStringLiteral("AgentFlow · 后端在线 · %1 个 Agent 可用 · 本地工作台模式").arg(agents.size()));
    ui->ambientStatusBadge->setText(QStringLiteral("运行正常"));
    ui->taskUsageLabel->setText(QStringLiteral("Agent 注册表：已加载 %1 个内置 Agent").arg(agents.size()));
    ui->apiUsageProgress->setValue(100);
}

void MainWindow::updateAgentCardSet(const QList<AgentInfo> &agents, int baseIndex, int cardCount)
{
    // baseIndex 0 对应首页常用 Agent 卡片，100 对应应用中心卡片。
    // 复用同一批后端数据，保证两个页面展示一致。
    for (int offset = 0; offset < cardCount; ++offset) {
        const int cardIndex = baseIndex + offset;
        if (offset < agents.size()) {
            const AgentInfo &agent = agents.at(offset);
            const QString subtitle = QStringLiteral("%1 · %2")
                                         .arg(agent.category.isEmpty() ? QStringLiteral("智能应用") : agent.category,
                                              agent.description);
            const QString badge = agent.enabled ? QStringLiteral("后端已启用") : QStringLiteral("已禁用");
            setAgentCard(cardIndex, agent.name, subtitle, badge);
        } else {
            setAgentCard(cardIndex,
                         QStringLiteral("预留插件位"),
                         QStringLiteral("等待 Agent Registry / .afagent 接入"),
                         QStringLiteral("规划中"));
        }
    }
}

void MainWindow::setAgentCard(int index, const QString &title, const QString &subtitle, const QString &badge)
{
    if (QLabel *label = agentTitleLabel(index)) {
        label->setText(title);
    }
    if (QLabel *label = agentSubtitleLabel(index)) {
        label->setText(subtitle);
        label->setWordWrap(true);
    }
    if (QLabel *label = agentBadgeLabel(index)) {
        label->setText(badge);
    }
}

QLabel *MainWindow::agentTitleLabel(int index) const
{
    // UI 文件里这些 QLabel 的 objectName 被 QSS 样式名覆盖过，
    // 因此不能依赖 findChild("agentTitle0")，必须直接用 uic 生成的成员指针。
    const QList<QLabel *> overviewLabels = {
        ui->agentTitle0,
        ui->agentTitle1,
        ui->agentTitle2,
        ui->agentTitle3,
        ui->agentTitle4,
        ui->agentTitle5,
        ui->agentTitle6,
        ui->agentTitle7
    };
    const QList<QLabel *> appLabels = {
        ui->agentTitle100,
        ui->agentTitle101,
        ui->agentTitle102,
        ui->agentTitle103,
        ui->agentTitle104,
        ui->agentTitle105,
        ui->agentTitle106,
        ui->agentTitle107,
        ui->agentTitle108,
        ui->agentTitle109,
        ui->agentTitle110,
        ui->agentTitle111
    };

    if (index >= 0 && index < overviewLabels.size()) {
        return overviewLabels.at(index);
    }
    if (index >= 100 && index < 100 + appLabels.size()) {
        return appLabels.at(index - 100);
    }
    return nullptr;
}

QLabel *MainWindow::agentSubtitleLabel(int index) const
{
    const QList<QLabel *> overviewLabels = {
        ui->agentSub0,
        ui->agentSub1,
        ui->agentSub2,
        ui->agentSub3,
        ui->agentSub4,
        ui->agentSub5,
        ui->agentSub6,
        ui->agentSub7
    };
    const QList<QLabel *> appLabels = {
        ui->agentSub100,
        ui->agentSub101,
        ui->agentSub102,
        ui->agentSub103,
        ui->agentSub104,
        ui->agentSub105,
        ui->agentSub106,
        ui->agentSub107,
        ui->agentSub108,
        ui->agentSub109,
        ui->agentSub110,
        ui->agentSub111
    };

    if (index >= 0 && index < overviewLabels.size()) {
        return overviewLabels.at(index);
    }
    if (index >= 100 && index < 100 + appLabels.size()) {
        return appLabels.at(index - 100);
    }
    return nullptr;
}

QLabel *MainWindow::agentBadgeLabel(int index) const
{
    const QList<QLabel *> overviewLabels = {
        ui->agentBadge0,
        ui->agentBadge1,
        ui->agentBadge2,
        ui->agentBadge3,
        ui->agentBadge4,
        ui->agentBadge5,
        ui->agentBadge6,
        ui->agentBadge7
    };
    const QList<QLabel *> appLabels = {
        ui->agentBadge100,
        ui->agentBadge101,
        ui->agentBadge102,
        ui->agentBadge103,
        ui->agentBadge104,
        ui->agentBadge105,
        ui->agentBadge106,
        ui->agentBadge107,
        ui->agentBadge108,
        ui->agentBadge109,
        ui->agentBadge110,
        ui->agentBadge111
    };

    if (index >= 0 && index < overviewLabels.size()) {
        return overviewLabels.at(index);
    }
    if (index >= 100 && index < 100 + appLabels.size()) {
        return appLabels.at(index - 100);
    }
    return nullptr;
}

void MainWindow::sendDispatchMessage()
{
    if (dispatchSubmissionWaitingForBackend) {
        ui->dispatchChatStatus->setText(QStringLiteral("任务已暂存，正在等待本地后端就绪。"));
        return;
    }

    const QString message = ui->dispatchInputEdit->text().trimmed();
    if (message.isEmpty()) {
        ui->dispatchChatStatus->setText(QStringLiteral("请输入任务"));
        return;
    }

    // 对话式确认只匹配完整、短小的指令，避免把“开始执行后帮我补一份图表”这类新任务
    // 误判为对上一轮计划的确认。真正的权限策略仍在 Runtime 内二次执行，不因这条便利
    // 入口跳过文件写入、联网或命令确认。
    const QString executionCommand = message.toLower().simplified();
    const bool asksToExecute = executionCommand == QStringLiteral("开始执行")
        || executionCommand == QStringLiteral("确认执行")
        || executionCommand == QStringLiteral("开始做")
        || executionCommand == QStringLiteral("继续执行");
    const bool canExecuteCurrentPlan = !currentDispatchTaskId.isEmpty()
        && !currentDispatchNeedsClarification
        && !currentDispatchGuidedHandoff
        && !currentDispatchPresentationHandoff
        && !isCurrentDispatchDirectConversation()
        && currentDispatchRuntimeMode != QStringLiteral("runtime")
        && !currentDispatchExecutionInProgress
        && currentDispatchPlanSummary.executionReadiness != QStringLiteral("requires_composition_runtime");
    if (asksToExecute && canExecuteCurrentPlan) {
        appendConversationHtml(formatDispatchUserMessageHtml(message));
        ui->dispatchInputEdit->clear();
        beginCurrentDispatchRuntime(false);
        return;
    }

    // PPT 已经是当前会话的明确目标时，客户说“开始/继续制作”或“你倒是制作啊”
    // 不是一个新的泛化问题。恢复同一个创作窗口即可，避免又生成一张空的 dry-run 计划卡。
    const bool presentationNudge = currentDispatchPresentationHandoff
        && !currentDispatchPresentationCompleted
        && (executionCommand.contains(QStringLiteral("制作"))
            || executionCommand.contains(QStringLiteral("生成"))
            || executionCommand.contains(QStringLiteral("做")))
        && (executionCommand.contains(QStringLiteral("开始"))
            || executionCommand.contains(QStringLiteral("继续"))
            || executionCommand.contains(QStringLiteral("直接"))
            || executionCommand.contains(QStringLiteral("倒是")));
    if (presentationNudge) {
        appendConversationHtml(formatDispatchUserMessageHtml(message));
        ui->dispatchInputEdit->clear();
        if (currentDispatchPresentationRunning) {
            appendConversationHtml(formatDispatchAssistantMessageHtml(
                QStringLiteral("PPT 正在制作中，创作窗口会持续显示计划、导出和回读验证进度。")));
        } else {
            currentDispatchPresentationRunning = true;
            ui->dispatchChatStatus->setText(QStringLiteral("正在制作 PPT"));
            setDispatchActivityRunning(true);
            openPresentationStudioForPrompt(currentDispatchUserGoal, true);
            appendConversationHtml(formatDispatchAssistantMessageHtml(
                QStringLiteral("已继续制作 PPT，结果完成后会直接回到当前会话。")));
        }
        updateDispatchActionButtons();
        return;
    }

    const QJsonArray materials = buildDispatchMaterialBindings();
    const QJsonArray agentHints = buildDispatchAgentHints();
    const QString projectScope = currentDispatchProjectScope;
    if (!backendManager || !backendManager->isReady()) {
        queueDispatchMessageUntilBackendReady(message, materials, projectScope, agentHints);
        return;
    }

    submitDispatchMessage(message, materials, projectScope, agentHints);
}

QJsonArray MainWindow::buildDispatchMaterialBindings() const
{
    QJsonArray materials;
    if (!dispatchSelectedDocumentRef.isEmpty()) {
        QJsonObject binding;
        binding.insert(QStringLiteral("binding_id"), QStringLiteral("dispatch_document"));
        binding.insert(QStringLiteral("kind"), QStringLiteral("document"));
        binding.insert(QStringLiteral("ref"), dispatchSelectedDocumentRef);
        binding.insert(QStringLiteral("display_name"), QFileInfo(dispatchSelectedDocumentRef).fileName());
        binding.insert(QStringLiteral("origin"), QStringLiteral("client_selected"));
        binding.insert(QStringLiteral("usage"), QStringLiteral("用户在 AI 调度台导入并选择的文档。"));
        materials.append(binding);
    }
    if (!dispatchSelectedKnowledgeBaseId.isEmpty()) {
        QJsonObject binding;
        binding.insert(QStringLiteral("binding_id"), QStringLiteral("dispatch_knowledge_base"));
        binding.insert(QStringLiteral("kind"), QStringLiteral("knowledge_base"));
        binding.insert(QStringLiteral("ref"), dispatchSelectedKnowledgeBaseId);
        binding.insert(QStringLiteral("display_name"), QStringLiteral("当前已选资料库"));
        binding.insert(QStringLiteral("origin"), QStringLiteral("client_selected"));
        binding.insert(QStringLiteral("usage"), QStringLiteral("用户在知识库页明确选择后交给总指挥的资料库。"));
        materials.append(binding);
    }
    if (!dispatchSelectedDatasetRef.isEmpty()) {
        QJsonObject binding;
        binding.insert(QStringLiteral("binding_id"), QStringLiteral("dispatch_dataset"));
        binding.insert(QStringLiteral("kind"), QStringLiteral("dataset"));
        binding.insert(QStringLiteral("ref"), dispatchSelectedDatasetRef);
        binding.insert(QStringLiteral("display_name"), QFileInfo(dispatchSelectedDatasetRef).fileName());
        binding.insert(QStringLiteral("origin"), QStringLiteral("client_selected"));
        binding.insert(QStringLiteral("usage"), QStringLiteral("用户在数据工作台完成画像后明确交给总指挥的数据文件。"));
        materials.append(binding);
    }
    return materials;
}

void MainWindow::queueDispatchMessageUntilBackendReady(const QString &message,
                                                       const QJsonArray &materials,
                                                       const QString &projectScope,
                                                       const QJsonArray &agentHints)
{
    // 只保留一条尚未发出的任务。它在 UI 线程内、当前应用生命周期内有效，不实现隐式
    // 重试队列或跨重启持久化，避免客户误以为离线期间的任务一定已经送达。
    pendingDispatchMessage = message;
    pendingDispatchMaterials = materials;
    pendingDispatchAgentHints = agentHints;
    pendingDispatchProjectScope = projectScope;
    dispatchSubmissionWaitingForBackend = true;

    // 冻结本次材料选择，防止客户切换页面后下一次任务意外继承这次私有材料。
    dispatchSelectedDocumentRef.clear();
    dispatchSelectedKnowledgeBaseId.clear();
    dispatchSelectedDatasetRef.clear();
    updateDispatchMaterialBindingsUi();
    ui->dispatchInputEdit->setReadOnly(true);
    ui->sendTaskButton->setEnabled(false);
    // 暂存内容已经冻结；同时冻结可改变任务范围的入口，避免客户误以为此时重新选材料或
    // 切换项目范围会影响已暂存的请求。后端就绪或启动失败后会统一恢复这些控件。
    ui->attachButton->setEnabled(false);
    ui->dispatchProjectScopeButton->setEnabled(false);
    ui->dispatchChatStatus->setText(QStringLiteral("后端准备中 · 任务已暂存，将在就绪后自动发送"));
    ui->summaryVal0->setText(message.left(24));
    ui->summaryVal1->setText(QStringLiteral("等待本地后端"));
    ui->summaryVal2->setText(QStringLiteral("¥0.00"));
    ui->summaryVal3->setText(QStringLiteral("已暂存"));
    setProgressStep(1, QStringLiteral("1 任务提交 · 已暂存"), QStringLiteral("badgeBlue"));
    setProgressStep(2, QStringLiteral("2 Commander 规划 · 等待后端就绪"), QStringLiteral("badgeGray"));
}

void MainWindow::flushQueuedDispatchMessage()
{
    if (!dispatchSubmissionWaitingForBackend || !backendManager || !backendManager->isReady()) {
        return;
    }

    const QString message = pendingDispatchMessage;
    const QJsonArray materials = pendingDispatchMaterials;
    const QJsonArray agentHints = pendingDispatchAgentHints;
    const QString projectScope = pendingDispatchProjectScope.isEmpty()
        ? QStringLiteral("global")
        : pendingDispatchProjectScope;
    pendingDispatchMessage.clear();
    pendingDispatchMaterials = QJsonArray{};
    pendingDispatchAgentHints = QJsonArray{};
    pendingDispatchProjectScope.clear();
    dispatchSubmissionWaitingForBackend = false;
    ui->dispatchInputEdit->setReadOnly(false);
    ui->attachButton->setEnabled(true);
    ui->dispatchProjectScopeButton->setEnabled(true);
    submitDispatchMessage(message, materials, projectScope, agentHints);
}

void MainWindow::restoreQueuedDispatchMessage(const QString &reason)
{
    if (!dispatchSubmissionWaitingForBackend) {
        return;
    }

    // 启动失败时不丢客户已经输入的目标。还原绑定仅供客户重新发送，不会自动在离线状态
    // 下访问材料或重新发送网络请求。
    ui->dispatchInputEdit->setReadOnly(false);
    ui->dispatchInputEdit->setText(pendingDispatchMessage);
    currentDispatchProjectScope = pendingDispatchProjectScope.isEmpty()
        ? QStringLiteral("global")
        : pendingDispatchProjectScope;
    for (const QJsonValue &value : pendingDispatchMaterials) {
        const QJsonObject binding = value.toObject();
        const QString kind = binding.value(QStringLiteral("kind")).toString();
        const QString ref = binding.value(QStringLiteral("ref")).toString();
        if (kind == QStringLiteral("document")) {
            dispatchSelectedDocumentRef = ref;
        } else if (kind == QStringLiteral("knowledge_base")) {
            dispatchSelectedKnowledgeBaseId = ref;
        } else if (kind == QStringLiteral("dataset")) {
            dispatchSelectedDatasetRef = ref;
        }
    }
    updateDispatchMaterialBindingsUi();
    pendingDispatchMessage.clear();
    pendingDispatchMaterials = QJsonArray{};
    pendingDispatchAgentHints = QJsonArray{};
    pendingDispatchProjectScope.clear();
    dispatchSubmissionWaitingForBackend = false;
    ui->sendTaskButton->setEnabled(true);
    ui->attachButton->setEnabled(true);
    ui->dispatchProjectScopeButton->setEnabled(true);
    ui->dispatchChatStatus->setText(
        QStringLiteral("后端未就绪 · 任务未发送，已保留输入。%1").arg(reason.left(80)));
}

void MainWindow::submitDispatchMessage(const QString &message,
                                       const QJsonArray &materials,
                                       const QString &projectScope,
                                       const QJsonArray &agentHints)
{

    // C6.2 之后同一会话按轮次追加消息。首条发送仅清除 Designer 示例，不清除客户已看到的
    // 前序问答，避免 UI 和后端会话上下文出现“模型记得、客户看不到”的割裂。
    if (!dispatchConversationHasMessages) {
        ui->conversationTextEdit->clear();
        dispatchConversationHasMessages = true;
    }
    resetProgressPanel();
    currentDispatchTaskId.clear();
    resetDispatchDeliveryCard();
    currentDispatchPlannedStepCount = 0;
    currentDispatchPlanSummary = WorkflowPlanSummaryInfo{};
    currentDispatchPlanSteps.clear();
    currentDispatchUpdateWatermark = 0;
    currentDispatchUpdates.clear();
    currentDispatchNeedsClarification = false;
    currentDispatchGuidedHandoff = false;
    currentDispatchPresentationHandoff = false;
    currentDispatchExecutionInProgress = false;
    currentDispatchExecutionSubmitted = false;
    currentDispatchDirectKnowledgeAnswer = false;
    currentDispatchDirectDataAnalysis = false;
    currentDispatchDataChartDelivery = false;
    currentDispatchDataWorkbookDelivery = false;
    currentDispatchAutoExecutePending = false;
    currentDispatchKnowledgeAnswerResultRequested = false;
    currentDispatchKnowledgeAnswerDelivered = false;
    currentDispatchDataAnalysisDelivered = false;
    currentDispatchDataChartDeliveryDelivered = false;
    currentDispatchDataWorkbookDeliveryDelivered = false;
    currentDispatchKnowledgeAnswerFailed = false;
    currentDispatchDataAnalysisFailed = false;
    currentDispatchDataChartDeliveryFailed = false;
    currentDispatchDataWorkbookDeliveryFailed = false;
    currentDispatchKnowledgeAnswerChildTaskId.clear();
    currentDispatchRuntimeMode.clear();
    currentDispatchRuntimeStatus.clear();
    currentDispatchHasPendingPermission = false;
    currentDispatchArtifactCount = 0;
    setDispatchActivityRunning(false);
    if (dispatchUpdateRefreshTimer) {
        dispatchUpdateRefreshTimer->stop();
    }
    updateDispatchActionButtons();
    ui->dispatchInputEdit->clear();
    ui->sendTaskButton->setEnabled(false);
    ui->dispatchChatStatus->setText(QStringLiteral("发送中"));

    ui->summaryVal0->setText(message.left(24));
    ui->summaryVal1->setText(QStringLiteral("等待模型回复"));
    ui->summaryVal2->setText(QStringLiteral("¥0.00"));
    ui->summaryVal3->setText(QStringLiteral("等待规划"));
    setProgressStep(1, QStringLiteral("1 任务提交 · 正在发送"), QStringLiteral("badgeBlue"));
    setProgressStep(2, QStringLiteral("2 Commander 规划 · 等待模型回复"), QStringLiteral("badgeBlue"));

    appendConversationHtml(formatDispatchUserMessageHtml(message));

    currentDispatchUserGoal = message;
    // 材料属于当前会话上下文，而不是单条消息的一次性附件。保留材料条让“分析后生成
    // 图表/Excel”“继续追问”等自然语言后续请求仍能拿到同一份数据；客户可随时点击
    // 材料条上的移除按钮，新建会话时才会整体清空。
    backendClient->sendChatMessage(
        message,
        QString(),
        materials,
        projectScope,
        currentDispatchConversationId,
        agentHints);
}

void MainWindow::handleChatCompleted(const ChatResult &result)
{
    ui->sendTaskButton->setEnabled(true);
    if (!result.conversationId.isEmpty() && result.conversationId != currentDispatchConversationId) {
        // 后端可能因项目范围变化或首次请求创建新会话；以服务端回执为准，Qt 只缓存 ID。
        currentDispatchConversationId = result.conversationId;
        saveDispatchConversationPreference();
    }
    ui->dispatchChatStatus->setText(QStringLiteral("已规划"));
    currentDispatchTaskId = result.taskId;
    resetDispatchDeliveryCard();
    currentDispatchPlannedStepCount = result.steps.size();
    currentDispatchPlanSummary = result.planSummary;
    currentDispatchProjectScope = result.planSummary.projectScope.isEmpty()
        ? QStringLiteral("global")
        : result.planSummary.projectScope;
    updateDispatchProjectScopeButton();
    currentDispatchPlanSteps = result.steps;
    currentDispatchUpdateWatermark = 0;
    currentDispatchUpdates.clear();
    currentDispatchExecutionInProgress = false;
    currentDispatchExecutionSubmitted = false;
    currentDispatchNeedsClarification = result.planSummary.nextAction == QStringLiteral("ask_clarifying_questions");
    currentDispatchGuidedHandoff = result.planSummary.nextAction == QStringLiteral("open_data_workspace");
    currentDispatchPresentationHandoff = result.planSummary.nextAction == QStringLiteral("open_presentation_studio");
    currentDispatchPresentationRunning = currentDispatchPresentationHandoff;
    currentDispatchPresentationCompleted = false;
    // /api/chat 当前生成的是 dry-run 任务；先记录模式，后续日志结束时才能正确区分“预演完成”和“执行完成”。
    currentDispatchRuntimeMode = QStringLiteral("dry_run");
    currentDispatchRuntimeStatus = currentDispatchNeedsClarification
        ? QStringLiteral("blocked")
        : QStringLiteral("pending");
    currentDispatchHasPendingPermission = false;
    currentDispatchArtifactCount = 0;
    currentDispatchKnowledgeAnswerChildTaskId.clear();
    currentDispatchKnowledgeAnswerResultRequested = false;
    currentDispatchKnowledgeAnswerDelivered = false;
    currentDispatchKnowledgeAnswerFailed = false;
    currentDispatchDirectKnowledgeAnswer = isCurrentDispatchDirectKnowledgeAnswer();
    currentDispatchDirectDataAnalysis = isCurrentDispatchDirectDataAnalysis();
    currentDispatchDataChartDelivery = isCurrentDispatchDataChartDelivery();
    currentDispatchDataWorkbookDelivery = isCurrentDispatchDataWorkbookDelivery();
    currentDispatchDataAnalysisDelivered = false;
    currentDispatchDataChartDeliveryDelivered = false;
    currentDispatchDataWorkbookDeliveryDelivered = false;
    currentDispatchDataAnalysisFailed = false;
    currentDispatchDataChartDeliveryFailed = false;
    currentDispatchDataWorkbookDeliveryFailed = false;
    // 纯只读问答和明确要求的本地交付都在 dry-run 收束后自动转 Runtime；后者的
    // 文件写入仍由后端 outputs、参数和回读验证保护，但不再要求客户重复点击确认。
    currentDispatchAutoExecutePending = isCurrentDispatchAutoReadOnlyTask()
        || currentDispatchDataChartDelivery
        || currentDispatchDataWorkbookDelivery;

    // 规划响应到达后先拉取一张轻量结果卡。Runtime 完成时会复用同一入口刷新为最终交付，
    // 让客户始终在调度台看到结果，而不是被迫翻到历史页寻找结论。
    backendClient->requestTaskDeliveryCard(result.taskId);

    ui->summaryVal0->setText(result.taskId);
    ui->summaryVal1->setText(chatModeSummary(result));
    ui->summaryVal2->setText(QStringLiteral("¥0.00"));
    QString taskSummary = agentSummary(result.steps);
    if (currentDispatchNeedsClarification) {
        taskSummary = QStringLiteral("需要补充信息");
    } else if (currentDispatchPresentationHandoff) {
        taskSummary = currentDispatchPresentationRunning
            ? QStringLiteral("正在制作 PPT")
            : currentDispatchPresentationCompleted ? QStringLiteral("PPT 已生成")
                                                    : QStringLiteral("PPT 制作已受理");
    } else if (currentDispatchGuidedHandoff) {
        taskSummary = QStringLiteral("待转入数据工作台");
    } else if (currentDispatchDirectDataAnalysis) {
        taskSummary = QStringLiteral("正在准备数据分析");
    } else if (currentDispatchDataChartDelivery) {
        taskSummary = QStringLiteral("正在生成图表");
    } else if (currentDispatchDataWorkbookDelivery) {
        taskSummary = QStringLiteral("正在生成分析 Excel");
    }
    ui->summaryVal3->setText(taskSummary);

    // 规划、预演和日志是 Harness 的审计面，不是客户需要阅读的“回答”。聊天区只保留
    // 一句当前状态；计划、节点契约、预算与日志继续通过既有详情入口按需查看。
    if (currentDispatchNeedsClarification) {
        const QString questions = result.planSummary.clarifyingQuestions.isEmpty()
            ? QStringLiteral("请补充要处理的材料或希望得到的结果。")
            : result.planSummary.clarifyingQuestions.join(QStringLiteral("<br/>"));
        appendConversationHtml(
            QStringLiteral("<hr/><h3>AI 调度台</h3><p><b>还需要一点信息。</b></p><p>%1</p>")
                .arg(questions.toHtmlEscaped().replace(QStringLiteral("&lt;br/&gt;"), QStringLiteral("<br/>"))));
    } else if (currentDispatchDirectKnowledgeAnswer) {
        appendConversationHtml(
            QStringLiteral("<hr/><h3>AI 调度台</h3><p>正在检索资料库“%1”，完成后会直接给出带来源的回答。</p>")
                .arg(currentDispatchKnowledgeBaseName().toHtmlEscaped()));
    } else if (currentDispatchDirectDataAnalysis) {
        appendConversationHtml(
            QStringLiteral("<hr/><h3>AI 调度台</h3><p>正在分析已选数据，完成后会直接给出主要趋势、差异和图表建议。</p>"));
    } else if (currentDispatchDataChartDelivery) {
        appendConversationHtml(
            QStringLiteral("<hr/><h3>AI 调度台</h3><p><b>正在生成数据图表。</b></p>"
                           "<p>已按你的明确要求复用当前数据并写入新的 PNG；原始数据不会被修改。"
                           "完成后会在本对话展示缩略图和交付入口。</p>"));
    } else if (currentDispatchDataWorkbookDelivery) {
        appendConversationHtml(
            QStringLiteral("<hr/><h3>AI 调度台</h3><p><b>正在生成分析 Excel。</b></p>"
                           "<p>已按你的明确要求新建包含分析表、原生图表和关键指标的工作簿；"
                           "原始数据不会被修改，完成后会在本对话展示交付入口。</p>"));
    } else if (currentDispatchPresentationHandoff) {
        appendConversationHtml(
            QStringLiteral("<hr/><h3>AI 调度台</h3><p><b>已开始制作 PPT。</b></p>"
                           "<p>正在生成创作计划、整理页面并导出可编辑 PPTX；完整进度会显示在独立创作窗口，"
                           "主对话不会被切走。</p>"));
    } else if (currentDispatchGuidedHandoff) {
        appendConversationHtml(
            QStringLiteral("<hr/><h3>AI 调度台</h3><p>已识别到需要在数据工作台继续处理。选择数据文件后，我会基于该文件给出可执行分析。</p>"));
    } else {
        appendConversationHtml(formatDispatchAssistantMessageHtml(result.reply));
    }

    resetProgressPanel();
    for (int index = 0; index < result.steps.size() && index < 5; ++index) {
        const WorkflowStepInfo &step = result.steps.at(index);
        setProgressStep(index + 1,
                        QStringLiteral("%1 %2 · 等待中")
                            .arg(index + 1)
                            .arg(agentDisplayName(step.agent)),
                        QStringLiteral("badgeGray"));
    }

    const bool needsClarification = currentDispatchNeedsClarification;
    const bool autoReadOnlyTask = isCurrentDispatchAutoReadOnlyTask();
    const bool directConversation = isCurrentDispatchDirectConversation();
    QString dispatchStatus = QStringLiteral("预演中");
    if (needsClarification) {
        dispatchStatus = QStringLiteral("待补充");
    } else if (autoReadOnlyTask) {
        dispatchStatus = currentDispatchAutoReadOnlyActivityText();
    } else if (currentDispatchDataChartDelivery) {
        dispatchStatus = QStringLiteral("正在生成图表");
    } else if (currentDispatchDataWorkbookDelivery) {
        dispatchStatus = QStringLiteral("正在生成分析 Excel");
    } else if (currentDispatchPresentationHandoff) {
        dispatchStatus = QStringLiteral("已识别 PPT 制作需求");
    } else if (currentDispatchGuidedHandoff) {
        dispatchStatus = QStringLiteral("待转入工作台");
    } else if (directConversation) {
        dispatchStatus = QStringLiteral("回答完成");
    }
    ui->dispatchChatStatus->setText(dispatchStatus);
    setDispatchActivityRunning(autoReadOnlyTask || currentDispatchPresentationRunning);
    setProgressStep(1, QStringLiteral("1 任务提交 · 已收到任务"), QStringLiteral("badgeGreen"));
    setProgressStep(2,
                    QStringLiteral("2 Commander 规划 · 已生成 %1 个步骤").arg(currentDispatchPlannedStepCount),
                    QStringLiteral("badgeGreen"));
    QString stageThree = QStringLiteral("3 Workflow 推进 · 等待日志");
    QString stageFour = QStringLiteral("4 权限 / 产物 · 等待确认");
    QString stageFive = QStringLiteral("5 当前结论 · 等待更新");
    QString stageThreeBadge = QStringLiteral("badgeBlue");
    QString stageFiveBadge = QStringLiteral("badgeGray");
    if (needsClarification) {
        stageThree = QStringLiteral("3 Workflow 推进 · 等待补充信息");
        stageFour = QStringLiteral("4 权限 / 产物 · 暂不执行");
        stageFive = QStringLiteral("5 当前结论 · 需要补充信息");
        stageThreeBadge = QStringLiteral("badgeOrange");
        stageFiveBadge = QStringLiteral("badgeOrange");
    } else if (currentDispatchDirectKnowledgeAnswer) {
        stageThree = QStringLiteral("3 知识库检索 · 正在准备");
        stageFour = QStringLiteral("4 权限 / 产物 · 无需额外确认");
    } else if (currentDispatchDirectDataAnalysis) {
        stageThree = QStringLiteral("3 数据分析 · 正在准备");
        stageFour = QStringLiteral("4 权限 / 产物 · 无需额外确认");
    } else if (currentDispatchDataChartDelivery) {
        stageThree = QStringLiteral("3 图表交付 · 正在生成 PNG");
        stageFour = QStringLiteral("4 权限 / 产物 · 已按本次请求执行");
        stageFive = QStringLiteral("5 当前结论 · 等待图表回读验证");
    } else if (currentDispatchDataWorkbookDelivery) {
        stageThree = QStringLiteral("3 分析 Excel · 正在生成工作簿");
        stageFour = QStringLiteral("4 权限 / 产物 · 已按本次请求执行");
        stageFive = QStringLiteral("5 当前结论 · 等待工作簿回读验证");
    } else if (currentDispatchPresentationHandoff) {
        stageThree = currentDispatchPresentationRunning
            ? QStringLiteral("3 智能制作 PPT · 正在生成")
            : currentDispatchPresentationCompleted
                  ? QStringLiteral("3 智能制作 PPT · 已完成")
                  : QStringLiteral("3 智能制作 PPT · 已受理");
        stageFour = currentDispatchPresentationRunning
            ? QStringLiteral("4 输出校验 · 等待 PPTX 回读验证")
            : currentDispatchPresentationCompleted
                  ? QStringLiteral("4 输出校验 · 已通过")
                  : QStringLiteral("4 输出校验 · 等待创作窗口");
        stageFive = currentDispatchPresentationRunning
            ? QStringLiteral("5 当前结论 · 正在制作")
            : currentDispatchPresentationCompleted
                  ? QStringLiteral("5 当前结论 · PPT 已交付")
                  : QStringLiteral("5 当前结论 · PPT 制作已受理");
    } else if (currentDispatchGuidedHandoff) {
        stageThree = QStringLiteral("3 工作台交接 · 等待你选择数据");
        stageFour = QStringLiteral("4 权限 / 产物 · 尚未创建数据任务");
        stageFive = QStringLiteral("5 当前结论 · 请前往数据工作台");
    } else if (directConversation) {
        stageThree = QStringLiteral("3 Workflow 推进 · 无需执行");
        stageFour = QStringLiteral("4 权限 / 产物 · 无 Runtime 副作用");
        stageFive = QStringLiteral("5 当前结论 · 已直接回答");
    }
    setProgressStep(3, stageThree, stageThreeBadge);
    setProgressStep(4, stageFour, QStringLiteral("badgeGray"));
    setProgressStep(5, stageFive, stageFiveBadge);
    updateDispatchActionButtons();
    backendClient->connectTaskLog(result.taskId);
    refreshCurrentDispatchUpdates();
    if (currentDispatchPresentationHandoff) {
        // 明确的“制作 PPT”请求直接进入现有创作/导出链；主窗口不切页，也不要求客户
        // 再重复点击“开始执行”。独立窗口负责展示计划、导出和回读验证进度。
        openPresentationStudioForPrompt(currentDispatchUserGoal, true);
    }
}

QString MainWindow::formatDispatchWorkflowPlanHtml(const ChatResult &result) const
{
    return formatDispatchWorkflowPlanHtml(result.planSummary, result.steps);
}

QString MainWindow::formatDispatchWorkflowPlanHtml(
    const WorkflowPlanSummaryInfo &plan,
    const QList<WorkflowStepInfo> &steps) const
{
    if (steps.isEmpty()) {
        return formatDispatchPlanSummaryHtml(plan);
    }

    QString html = formatDispatchPlanSummaryHtml(plan);
    html += QStringLiteral(
        "<p><b>结构化工作流计划</b> "
        "<span style=\"color:#64748B;\">已按 Node Contract 展示工具边界、权限和预期产物。</span></p><ol>");
    for (const WorkflowStepInfo &step : steps) {
        const QString riskText = historyRiskText(step.riskLevel).toHtmlEscaped();
        const QString confirmationText = step.requiresConfirmation ? QStringLiteral("需要确认")
                                                                    : QStringLiteral("无需确认");
        const QString executionText = step.executionMode == QStringLiteral("guided_handoff")
            ? QStringLiteral("需转入专业工作台")
            : (step.executionMode == QStringLiteral("planning_only")
                   ? QStringLiteral("仅规划，不执行工具")
                   : QStringLiteral("已准入，可在确认后执行"));
        const QString reasonText = step.reason.isEmpty() ? QStringLiteral("后端未返回规划原因。")
                                                         : step.reason.toHtmlEscaped();
        const QString expectedText = step.expectedOutput.isEmpty() ? QStringLiteral("后端未返回预期产物。")
                                                                   : step.expectedOutput.toHtmlEscaped();
        QString graphText;
        if (!step.dependsOn.isEmpty()) {
            graphText = QStringLiteral("依赖：%1").arg(step.dependsOn.join(QStringLiteral("、")));
        }
        if (!step.parallelGroup.isEmpty()) {
            const QString groupLabel = step.parallelGroup == QStringLiteral("specialist_read_only")
                ? QStringLiteral("只读专业步骤")
                : step.parallelGroup;
            graphText += (graphText.isEmpty() ? QString() : QStringLiteral(" · "))
                + QStringLiteral("并行组：%1").arg(groupLabel);
        }

        // 调度台只展示计划解释，不在前端执行工具；真实执行仍必须走 runtime 和权限审计。
        QString stepHtml = QStringLiteral(
            "<li style=\"margin-bottom:10px;\">"
            "<div><b>%1</b>：%2 <span style=\"color:#64748B;\">%3 · 风险：%4 · %5 · %6</span></div>"
            "<div style=\"margin-top:3px;color:#475569;\">原因：%7</div>"
            "<div style=\"margin-top:3px;color:#475569;\">预期：%8</div>"
            "<div style=\"margin-top:3px;color:#64748B;\">%9</div>"
            "%10"
            "</li>");
        // 分段 arg 避免依赖特定 Qt 版本对多参数重载数量的差异，也能让占位符对应关系清晰。
        stepHtml = stepHtml.arg(agentDisplayName(step.agent).toHtmlEscaped())
                           .arg(step.title.toHtmlEscaped())
                           .arg(step.action.toHtmlEscaped())
                           .arg(riskText)
                           .arg(confirmationText.toHtmlEscaped())
                           .arg(executionText.toHtmlEscaped())
                           .arg(reasonText)
                           .arg(expectedText)
                           .arg(graphText.toHtmlEscaped())
                           .arg(formatDispatchStepContractHtml(step));
        html += stepHtml;
    }
    html += QStringLiteral("</ol>");
    return html;
}

QString MainWindow::formatDispatchChatPlanCardHtml(
    const WorkflowPlanSummaryInfo &plan,
    const QList<WorkflowStepInfo> &steps) const
{
    // 对话区服务于理解和下一步行动；完整步骤、输入契约、工具与审计信息仍只在计划 Inspector
    // 中按需展开，避免每轮聊天都变成难以阅读的日志墙。
    const QString summary = plan.summary.trimmed().isEmpty()
        ? QStringLiteral("已根据当前目标建立受控任务计划。")
        : plan.summary.trimmed().left(220);
    QStringList agentNames;
    for (const WorkflowStepInfo &step : steps) {
        const QString name = agentDisplayName(step.agent);
        if (!name.isEmpty() && !agentNames.contains(name)) {
            agentNames.append(name);
        }
    }
    const QString involvedAgents = agentNames.isEmpty()
        ? QStringLiteral("总指挥")
        : agentNames.mid(0, 3).join(QStringLiteral("、"));
    QString nextAction = QStringLiteral("低风险只读任务会自动处理；涉及写入时回复“开始执行”。");
    if (plan.nextAction == QStringLiteral("ask_clarifying_questions")) {
        nextAction = QStringLiteral("请补充必要信息后，我会据此更新计划。");
    } else if (plan.nextAction == QStringLiteral("open_data_workspace")) {
        nextAction = QStringLiteral("请前往数据工作台选择数据文件后继续。");
    } else if (plan.nextAction == QStringLiteral("review_combination_plan")) {
        nextAction = QStringLiteral("组合依赖已建立；真实并发与最终汇总将在组合 Runtime 可用后开放。");
    } else if (plan.nextAction == QStringLiteral("review_plan_and_confirm_permissions")) {
        nextAction = QStringLiteral("将写入新的受控产物；确认范围后回复“开始执行”。");
    }
    return QStringLiteral(
               "<div style=\"margin:10px 0 2px 0;padding:10px 12px;border:1px solid #D7E5FF;"
               "border-radius:8px;background:#F8FBFF;\">"
               "<div style=\"color:#1D4ED8;font-weight:700;\">计划已生成 · %1 步</div>"
               "<div style=\"margin-top:4px;color:#334155;\">%2</div>"
               "<div style=\"margin-top:6px;color:#64748B;font-size:12px;\">涉及：%3 · %4</div>"
               "</div>")
        .arg(QString::number(steps.size()),
             summary.toHtmlEscaped(),
             involvedAgents.toHtmlEscaped(),
             nextAction.toHtmlEscaped());
}

QString MainWindow::formatDispatchPlanSummaryHtml(const WorkflowPlanSummaryInfo &plan) const
{
    if (plan.summary.isEmpty()
        && plan.clarifyingQuestions.isEmpty()
        && plan.definitionOfDone.isEmpty()) {
        return QString();
    }

    QString html = QStringLiteral(
        "<div style=\"margin:12px 0;padding:10px;border:1px solid #BFDBFE;"
        "border-radius:8px;background:#EFF6FF;color:#1E3A8A;\">"
        "<div><b>总指挥计划摘要</b> "
        "<span style=\"color:#64748B;\">%1 · v%2 · %3</span></div>")
        .arg(plan.intent.isEmpty() ? QStringLiteral("general") : plan.intent.toHtmlEscaped())
        .arg(plan.planVersion > 0 ? QString::number(plan.planVersion) : QStringLiteral("1"))
        .arg(dispatchNextActionText(plan.nextAction).toHtmlEscaped());

    if (!plan.summary.isEmpty()) {
        html += QStringLiteral("<div style=\"margin-top:5px;color:#334155;\">%1</div>")
                    .arg(plan.summary.toHtmlEscaped());
    }

    html += QStringLiteral(
        "<div style=\"margin-top:6px;color:#475569;\"><b>本次偏好：</b>%1 · %2</div>")
        .arg(runtimePermissionPolicyText(plan.preferences.permissionPolicy).toHtmlEscaped(),
             runtimePersonalityText(plan.preferences.personality).toHtmlEscaped());

    html += QStringLiteral(
        "<div style=\"margin-top:4px;color:#475569;\"><b>记忆范围：</b>%1</div>")
                .arg(plan.projectScope.isEmpty() ? QStringLiteral("global")
                                                  : plan.projectScope.toHtmlEscaped());

    if (!plan.agentHints.isEmpty()) {
        QStringList hintNames;
        for (const QString &agentId : plan.agentHints) {
            hintNames.append(agentDisplayName(agentId));
        }
        html += QStringLiteral(
            "<div style=\"margin-top:4px;color:#475569;\"><b>本轮路由：</b>%1</div>")
                    .arg(hintNames.join(QStringLiteral("、")).toHtmlEscaped());
    }

    if (!plan.clarifyingQuestions.isEmpty()) {
        html += QStringLiteral(
            "<div style=\"margin-top:8px;padding:8px;border:1px solid #FDBA74;"
            "border-radius:8px;background:#FFF7ED;color:#9A3412;\">"
            "<b>需要补充信息</b>%1</div>")
            .arg(dispatchBulletListHtml(plan.clarifyingQuestions));
    }

    if (!plan.definitionOfDone.isEmpty()) {
        html += QStringLiteral(
            "<div style=\"margin-top:8px;\"><b>完成标准</b>%1</div>")
            .arg(dispatchBulletListHtml(plan.definitionOfDone));
    }

    const QString budgetText = QStringLiteral("步骤 %1 · 耗时 %2 · 成本 %3 · 联网 %4 · 命令 %5")
        .arg(plan.budgetEstimate.stepCount)
        .arg(dispatchBudgetLevelText(plan.budgetEstimate.timeLevel))
        .arg(dispatchBudgetLevelText(plan.budgetEstimate.modelCostLevel))
        .arg(plan.budgetEstimate.requiresNetwork ? QStringLiteral("可能需要") : QStringLiteral("不需要"))
        .arg(plan.budgetEstimate.requiresCommand ? QStringLiteral("可能需要") : QStringLiteral("不需要"));
    html += QStringLiteral("<div style=\"margin-top:8px;color:#475569;\"><b>预算预估：</b>%1</div>")
                .arg(budgetText.toHtmlEscaped());

    const bool hasWorkspaceScope = !plan.workspaceScope.readPaths.isEmpty()
        || !plan.workspaceScope.writePaths.isEmpty()
        || !plan.workspaceScope.externalServices.isEmpty();
    if (hasWorkspaceScope) {
        html += QStringLiteral(
            "<div style=\"margin-top:6px;color:#475569;\">"
            "<b>工作区边界：</b>读：%1；写：%2；外部：%3</div>")
            .arg(dispatchCompactListText(plan.workspaceScope.readPaths),
                 dispatchCompactListText(plan.workspaceScope.writePaths),
                 dispatchCompactListText(plan.workspaceScope.externalServices));
    }

    html += QStringLiteral("</div>");
    return html;
}

QString MainWindow::formatDispatchStepContractHtml(const WorkflowStepInfo &step) const
{
    const WorkflowNodeContractInfo *contract = nodeContractForStep(step.agent, step.action);

    QStringList permissions = contract ? contract->requiredPermissions : step.requiredPermissions;
    if (permissions.isEmpty() && step.requiresConfirmation) {
        permissions = step.requiredPermissions;
    }
    const QString permissionText = permissions.isEmpty()
        ? QStringLiteral("无额外权限")
        : permissions.join(QStringLiteral("、")).toHtmlEscaped();
    const QString toolName = contract ? contract->toolName
                                      : (step.toolName.isEmpty()
                                             ? QStringLiteral("%1.%2").arg(step.agent, step.action)
                                             : step.toolName);
    const QString nodeType = contract ? contract->nodeType : QStringLiteral("未登记");
    const QString failureText = (contract && !contract->failureCodes.isEmpty())
        ? contract->failureCodes.join(QStringLiteral("、")).toHtmlEscaped()
        : QStringLiteral("未声明");
    const QString signalText = (contract && !contract->evaluationSignals.isEmpty())
        ? contract->evaluationSignals.join(QStringLiteral("、")).toHtmlEscaped()
        : QStringLiteral("未声明");
    const QString inputText = formatJsonPreview(step.input, 140);

    QString html = QStringLiteral(
        "<div style=\"margin-top:5px;padding:7px;border:1px solid #E2E8F0;"
        "border-radius:8px;background:#F8FAFC;color:#64748B;\">"
        "工具：<b>%1</b> · 类型：%2 · 权限：%3 · 失败码：%4 · 评估：%5")
        .arg(toolName.toHtmlEscaped(),
             nodeType.toHtmlEscaped(),
             permissionText,
             failureText,
             signalText);
    if (!inputText.isEmpty()) {
        html += QStringLiteral(
            "<div style=\"margin-top:3px;font-family:'Consolas','Courier New',monospace;\">输入：%1</div>")
            .arg(inputText);
    }
    if (!step.successCriteria.isEmpty()) {
        html += QStringLiteral("<div style=\"margin-top:3px;\">成功标准：%1</div>")
                    .arg(step.successCriteria.join(QStringLiteral("；")).toHtmlEscaped());
    }
    if (!step.admissionReason.isEmpty()) {
        html += QStringLiteral("<div style=\"margin-top:3px;\">准入：%1</div>")
                    .arg(step.admissionReason.toHtmlEscaped());
    }
    if (!step.verificationScope.isEmpty()) {
        html += QStringLiteral("<div style=\"margin-top:3px;\">验证范围：%1</div>")
                    .arg(step.verificationScope.toHtmlEscaped());
    }
    if (!step.recoveryHint.isEmpty()) {
        html += QStringLiteral("<div style=\"margin-top:3px;color:#0F766E;\">下一步：%1</div>")
                    .arg(step.recoveryHint.toHtmlEscaped());
    }
    const QString commandRisk = step.commandPolicy.value(QStringLiteral("risk_level")).toString();
    if (!commandRisk.isEmpty() && commandRisk != QStringLiteral("none")) {
        const QString commandConfirmation = step.commandPolicy.value(QStringLiteral("requires_confirmation")).toBool()
            ? QStringLiteral("需要确认")
            : QStringLiteral("无需确认");
        const QString commandAllowed = step.commandPolicy.value(QStringLiteral("allowed")).toBool(true)
            ? QStringLiteral("策略允许")
            : QStringLiteral("策略禁止");
        html += QStringLiteral("<div style=\"margin-top:3px;color:#9A3412;\">命令策略：%1 · %2 · %3</div>")
                    .arg(commandRisk.toHtmlEscaped(),
                         commandConfirmation.toHtmlEscaped(),
                         commandAllowed.toHtmlEscaped());
    }
    if (!workflowNodeContractsLoaded && workflowNodeContractsError.isEmpty()) {
        html += QStringLiteral("<div style=\"margin-top:3px;color:#64748B;\">节点契约仍在加载，稍后历史页会显示完整边界。</div>");
    } else if (!workflowNodeContractsError.isEmpty() && !contract) {
        html += QStringLiteral("<div style=\"margin-top:3px;color:#C2410C;\">节点契约加载失败：%1</div>")
                    .arg(workflowNodeContractsError.toHtmlEscaped());
    }
    html += QStringLiteral("</div>");
    return html;
}

void MainWindow::applyDispatchTaskUpdates(const WorkflowTaskUpdateListResult &result)
{
    if (result.taskId.isEmpty() || result.taskId != currentDispatchTaskId) {
        return;
    }

    currentDispatchUpdates = result.updates;
    for (const WorkflowTaskUpdateInfo &item : currentDispatchUpdates) {
        currentDispatchUpdateWatermark = qMax(currentDispatchUpdateWatermark, item.sequence);
    }

    // 先回到稳定的基线阶段，再用最新 updates 覆盖“推进 / 确认 / 结论”。
    setProgressStep(1, QStringLiteral("1 %1 · 已收到任务").arg(dispatchStageTitle(1)), QStringLiteral("badgeGreen"));
    setProgressStep(2,
                    QStringLiteral("2 %1 · 已生成 %2 个步骤")
                        .arg(dispatchStageTitle(2))
                        .arg(currentDispatchPlannedStepCount),
                    QStringLiteral("badgeGreen"));
    setProgressStep(3, QStringLiteral("3 %1 · 等待步骤推进").arg(dispatchStageTitle(3)), QStringLiteral("badgeGray"));
    setProgressStep(4, QStringLiteral("4 %1 · 暂无敏感动作").arg(dispatchStageTitle(4)), QStringLiteral("badgeGray"));
    setProgressStep(5, QStringLiteral("5 %1 · 等待结论").arg(dispatchStageTitle(5)), QStringLiteral("badgeGray"));

    const WorkflowTaskUpdateInfo *latestStep = nullptr;
    const WorkflowTaskUpdateInfo *latestPermission = nullptr;
    const WorkflowTaskUpdateInfo *latestArtifact = nullptr;
    const WorkflowTaskUpdateInfo *latestState = nullptr;
    int completedStepCount = 0;
    int artifactCount = 0;
    int permissionCount = 0;
    for (const WorkflowTaskUpdateInfo &item : currentDispatchUpdates) {
        if (item.updateType == QStringLiteral("step")) {
            latestStep = &item;
            if (item.event == QStringLiteral("step_completed")) {
                ++completedStepCount;
            }
        } else if (item.updateType == QStringLiteral("permission")) {
            latestPermission = &item;
            ++permissionCount;
        } else if (item.updateType == QStringLiteral("artifact")) {
            latestArtifact = &item;
            ++artifactCount;
        } else if (item.updateType == QStringLiteral("state")) {
            latestState = &item;
        }
    }

    const bool latestStateTerminal = latestState
        && (latestState->status == QStringLiteral("completed")
            || latestState->status == QStringLiteral("failed")
            || latestState->status == QStringLiteral("cancelled")
            || latestState->status == QStringLiteral("blocked"));
    const bool pendingPermissionNow = (latestState && latestState->status == QStringLiteral("waiting_permission"))
        || (!latestStateTerminal && latestPermission && latestPermission->status == QStringLiteral("waiting_permission"));

    // 按钮文案依赖的是“当前可操作状态”，不能只看是否有过历史权限事件。
    currentDispatchHasPendingPermission = pendingPermissionNow;
    currentDispatchArtifactCount = artifactCount;
    if (latestState) {
        // 兼容旧任务或降级事件：状态 payload 没有 mode 时保留当前已知模式，
        // 避免一次不完整更新把“预演/真实执行”重新变成未知。
        currentDispatchRuntimeMode = latestState->payload.value(QStringLiteral("mode"))
                                         .toString(currentDispatchRuntimeMode);
        currentDispatchRuntimeStatus = latestState->status;
    } else if (latestPermission && !latestPermission->status.isEmpty()) {
        currentDispatchRuntimeStatus = latestPermission->status;
    }

    if (latestStep) {
        QString badgeName = QStringLiteral("badgeGreen");
        if (latestStep->status == QStringLiteral("running")) {
            badgeName = QStringLiteral("badgeBlue");
        } else if (latestStep->level == QStringLiteral("warning") || latestStep->level == QStringLiteral("error")) {
            badgeName = QStringLiteral("badgeOrange");
        }

        setProgressStep(
            3,
            QStringLiteral("3 %1 · %2/%3 步 · %4")
                .arg(dispatchStageTitle(3))
                .arg(completedStepCount)
                .arg(qMax(1, currentDispatchPlannedStepCount))
                .arg(latestStep->message),
            badgeName);
    }

    if (latestPermission && latestPermission->status == QStringLiteral("waiting_permission")) {
        setProgressStep(
            4,
            QStringLiteral("4 %1 · 等待确认 · %2")
                .arg(dispatchStageTitle(4), latestPermission->message),
            QStringLiteral("badgeOrange"));
    } else if (latestPermission && latestPermission->status == QStringLiteral("blocked")) {
        setProgressStep(
            4,
            QStringLiteral("4 %1 · 权限被拒绝 · %2")
                .arg(dispatchStageTitle(4), latestPermission->message),
            QStringLiteral("badgeOrange"));
    } else if (latestArtifact) {
        setProgressStep(
            4,
            QStringLiteral("4 %1 · 已登记 %2 个产物 · %3")
                .arg(dispatchStageTitle(4))
                .arg(artifactCount)
                .arg(latestArtifact->message),
            QStringLiteral("badgeGreen"));
    } else if (permissionCount > 0) {
        setProgressStep(
            4,
            QStringLiteral("4 %1 · 共有 %2 个敏感步骤待后续确认")
                .arg(dispatchStageTitle(4))
                .arg(permissionCount),
            QStringLiteral("badgeOrange"));
    }

    if (latestState) {
        const QString statusText = dispatchStatusTextForState(currentDispatchRuntimeMode, latestState->status);
        QString badgeName = QStringLiteral("badgeGray");
        if (latestState->status == QStringLiteral("completed")) {
            badgeName = QStringLiteral("badgeGreen");
        } else if (latestState->status == QStringLiteral("running")) {
            badgeName = QStringLiteral("badgeBlue");
        } else if (latestState->status == QStringLiteral("waiting_permission")
                   || latestState->status == QStringLiteral("blocked")
                   || latestState->status == QStringLiteral("failed")
                   || latestState->status == QStringLiteral("cancelled")) {
            badgeName = QStringLiteral("badgeOrange");
        }

        setProgressStep(
            5,
            QStringLiteral("5 %1 · %2").arg(dispatchStageTitle(5), latestState->message),
            badgeName);
        ui->dispatchChatStatus->setText(statusText);
        ui->summaryVal3->setText(statusText);
    }

    if (currentDispatchHasPendingPermission) {
        ui->dispatchChatStatus->setText(QStringLiteral("待确认"));
        ui->summaryVal3->setText(QStringLiteral("等待权限确认"));
    } else if (currentDispatchArtifactCount > 0 && (!latestState || latestState->status != QStringLiteral("running"))) {
        ui->summaryVal3->setText(QStringLiteral("已登记 %1 个产物").arg(currentDispatchArtifactCount));
    }

    if (currentDispatchDirectKnowledgeAnswer && latestState
        && currentDispatchRuntimeMode == QStringLiteral("runtime")) {
        if (latestState->status == QStringLiteral("completed")) {
            // 父任务完成不等于聊天答案已读回。先按关联子任务 ID 读取 K3 的已验证正文，
            // 防止将父任务的短摘要误当成客户答案或把日志拼成回答。
            requestCurrentDispatchKnowledgeAnswerResult();
        } else if (latestState->status == QStringLiteral("failed")
                   || latestState->status == QStringLiteral("blocked")
                   || latestState->status == QStringLiteral("cancelled")) {
            if (!currentDispatchKnowledgeAnswerDelivered && !currentDispatchKnowledgeAnswerFailed) {
                currentDispatchKnowledgeAnswerFailed = true;
                ui->dispatchChatStatus->setText(QStringLiteral("检索未完成"));
                ui->summaryVal3->setText(QStringLiteral("请在历史中确认资料范围与运行原因"));
                setProgressStep(5,
                                QStringLiteral("5 当前结论 · 本次资料库检索未完成"),
                                QStringLiteral("badgeOrange"));
                appendConversationHtml(
                    QStringLiteral("<hr/><h3>AI 调度台</h3>"
                                   "<p style=\"color:#B45309;\"><b>这次资料库检索没有完成。</b></p>"
                                   "<p>资料没有被修改。请确认资料库已完成索引，并在“查看历史”中检查本次运行原因后重试。</p>"));
            }
            setDispatchActivityRunning(false);
        }
    } else if (currentDispatchDirectDataAnalysis && latestState
               && currentDispatchRuntimeMode == QStringLiteral("runtime")) {
        if (latestState->status == QStringLiteral("completed")) {
            if (!currentDispatchDataAnalysisDelivered) {
                const QJsonObject retrospective = latestState->payload
                    .value(QStringLiteral("task_retrospective")).toObject();
                const QJsonArray delegations = retrospective.value(QStringLiteral("delegations")).toArray();
                QString conclusion;
                for (const QJsonValue &value : delegations) {
                    const QJsonObject delegation = value.toObject();
                    if (delegation.value(QStringLiteral("agent_id")).toString() == QStringLiteral("data_agent")
                        && delegation.value(QStringLiteral("status")).toString() == QStringLiteral("completed")) {
                        conclusion = delegation.value(QStringLiteral("summary")).toString().trimmed();
                        break;
                    }
                }
                if (conclusion.isEmpty()) {
                    conclusion = QStringLiteral("数据分析已完成，但当前没有可展示的结论摘要；请在查看历史中打开关联任务。");
                }
                appendConversationHtml(formatDispatchAssistantMessageHtml(
                    QStringLiteral("## 数据分析结果\n\n%1\n\n> 已基于本次受控数据完成只读预览；源文件没有被修改。需要图表时，直接回复“生成图表”即可，我会先给出交付计划。\n")
                        .arg(conclusion)));
                currentDispatchDataAnalysisDelivered = true;
            }
            ui->dispatchChatStatus->setText(QStringLiteral("分析完成"));
            ui->summaryVal3->setText(QStringLiteral("已生成数据结论与图表建议"));
            setProgressStep(5, QStringLiteral("5 当前结论 · 已生成数据分析结果"), QStringLiteral("badgeGreen"));
            setDispatchActivityRunning(false);
        } else if (latestState->status == QStringLiteral("failed")
                   || latestState->status == QStringLiteral("blocked")
                   || latestState->status == QStringLiteral("cancelled")) {
            if (!currentDispatchDataAnalysisFailed) {
                currentDispatchDataAnalysisFailed = true;
                ui->dispatchChatStatus->setText(QStringLiteral("分析未完成"));
                ui->summaryVal3->setText(QStringLiteral("请在历史中确认数据文件与运行原因"));
                setProgressStep(5, QStringLiteral("5 当前结论 · 本次数据分析未完成"), QStringLiteral("badgeOrange"));
                appendConversationHtml(
                    QStringLiteral("<hr/><h3>AI 调度台</h3>"
                                   "<p style=\"color:#B45309;\"><b>这次数据分析没有完成。</b></p>"
                                   "<p>源数据没有被修改。请在“查看历史”中确认文件画像和运行原因后重试。</p>"));
            }
            setDispatchActivityRunning(false);
        }
    } else if (currentDispatchDataChartDelivery && latestState
               && currentDispatchRuntimeMode == QStringLiteral("runtime")) {
        if (latestState->status == QStringLiteral("completed")) {
            if (!currentDispatchDataChartDeliveryDelivered) {
                const QJsonObject retrospective = latestState->payload
                    .value(QStringLiteral("task_retrospective")).toObject();
                const QJsonArray delegations = retrospective.value(QStringLiteral("delegations")).toArray();
                QString chartSummary;
                // QJsonArray 没有 STL 的反向迭代器；按索引逆序读取，确保同一父任务内先拿到
                // 最后完成的图表交付子任务，而不是前一个只读分析子任务。
                for (int index = delegations.size() - 1; index >= 0; --index) {
                    const QJsonObject delegation = delegations.at(index).toObject();
                    const QString delegatedTaskId = delegation.value(QStringLiteral("task_id")).toString();
                    if (delegation.value(QStringLiteral("agent_id")).toString() == QStringLiteral("data_agent")
                        && delegatedTaskId.startsWith(QStringLiteral("task_data_chart_"))
                        && delegation.value(QStringLiteral("status")).toString() == QStringLiteral("completed")) {
                        chartSummary = delegation.value(QStringLiteral("summary")).toString().trimmed();
                        break;
                    }
                }
                if (chartSummary.isEmpty()) {
                    chartSummary = QStringLiteral("已生成并回读验证本次图表 PNG 交付物。");
                }
                appendConversationHtml(formatDispatchAssistantMessageHtml(
                    QStringLiteral("## 图表交付已完成\n\n%1\n\n"
                                   "> 图表已生成到本次受控交付中，源 CSV/XLSX 没有被修改。完整预览与打开入口会保留在任务历史，也会随会话恢复展示。")
                        .arg(chartSummary)));
                currentDispatchDataChartDeliveryDelivered = true;
            }
            ui->dispatchChatStatus->setText(QStringLiteral("图表已交付"));
            ui->summaryVal3->setText(QStringLiteral("已生成并验证图表交付"));
            setProgressStep(5, QStringLiteral("5 当前结论 · 图表已生成并验证"), QStringLiteral("badgeGreen"));
            setDispatchActivityRunning(false);
        } else if (latestState->status == QStringLiteral("failed")
                   || latestState->status == QStringLiteral("blocked")
                   || latestState->status == QStringLiteral("cancelled")) {
            if (!currentDispatchDataChartDeliveryFailed) {
                currentDispatchDataChartDeliveryFailed = true;
                ui->dispatchChatStatus->setText(QStringLiteral("图表交付未完成"));
                ui->summaryVal3->setText(QStringLiteral("源数据未被修改"));
                setProgressStep(5, QStringLiteral("5 当前结论 · 图表交付未完成"), QStringLiteral("badgeOrange"));
                appendConversationHtml(
                    QStringLiteral("<hr/><h3>AI 调度台</h3>"
                                   "<p style=\"color:#B45309;\"><b>这次图表交付没有完成。</b></p>"
                                   "<p>源数据没有被修改。你可以直接调整目标后再次发送，我会重新生成受控交付计划。</p>"));
            }
            setDispatchActivityRunning(false);
        }
    } else if (currentDispatchDataWorkbookDelivery && latestState
               && currentDispatchRuntimeMode == QStringLiteral("runtime")) {
        if (latestState->status == QStringLiteral("completed")) {
            if (!currentDispatchDataWorkbookDeliveryDelivered) {
                const QJsonObject retrospective = latestState->payload
                    .value(QStringLiteral("task_retrospective")).toObject();
                const QJsonArray delegations = retrospective.value(QStringLiteral("delegations")).toArray();
                QString workbookSummary;
                for (int index = delegations.size() - 1; index >= 0; --index) {
                    const QJsonObject delegation = delegations.at(index).toObject();
                    const QString delegatedTaskId = delegation.value(QStringLiteral("task_id")).toString();
                    if (delegation.value(QStringLiteral("agent_id")).toString() == QStringLiteral("data_agent")
                        && delegatedTaskId.startsWith(QStringLiteral("task_data_workbook_"))
                        && delegation.value(QStringLiteral("status")).toString() == QStringLiteral("completed")) {
                        workbookSummary = delegation.value(QStringLiteral("summary")).toString().trimmed();
                        break;
                    }
                }
                if (workbookSummary.isEmpty()) {
                    workbookSummary = QStringLiteral("已生成并回读验证本次分析 Excel 工作簿。");
                }
                appendConversationHtml(formatDispatchAssistantMessageHtml(
                    QStringLiteral("## 分析 Excel 已交付\n\n%1\n\n"
                                   "> 工作簿已生成到本次受控交付中，源 CSV/XLSX 没有被修改。完整打开入口会保留在任务历史，也会随会话恢复展示。")
                        .arg(workbookSummary)));
                currentDispatchDataWorkbookDeliveryDelivered = true;
            }
            ui->dispatchChatStatus->setText(QStringLiteral("分析 Excel 已交付"));
            ui->summaryVal3->setText(QStringLiteral("已生成并验证分析 Excel"));
            setProgressStep(5, QStringLiteral("5 当前结论 · 分析 Excel 已生成并验证"), QStringLiteral("badgeGreen"));
            setDispatchActivityRunning(false);
        } else if (latestState->status == QStringLiteral("failed")
                   || latestState->status == QStringLiteral("blocked")
                   || latestState->status == QStringLiteral("cancelled")) {
            if (!currentDispatchDataWorkbookDeliveryFailed) {
                currentDispatchDataWorkbookDeliveryFailed = true;
                ui->dispatchChatStatus->setText(QStringLiteral("分析 Excel 未完成"));
                ui->summaryVal3->setText(QStringLiteral("源数据未被修改"));
                setProgressStep(5, QStringLiteral("5 当前结论 · 分析 Excel 未完成"), QStringLiteral("badgeOrange"));
                appendConversationHtml(
                    QStringLiteral("<hr/><h3>AI 调度台</h3>"
                                   "<p style=\"color:#B45309;\"><b>这次分析 Excel 交付没有完成。</b></p>"
                                   "<p>源数据没有被修改。你可以直接调整目标后再次发送，我会重新生成受控交付计划。</p>"));
            }
            setDispatchActivityRunning(false);
        }
    }
    updateDispatchActionButtons();
}

void MainWindow::updateDispatchProgressFromLogEvent(const TaskLogEvent &event)
{
    const int stageIndex = dispatchProgressIndexForEvent(event.event);
    QString badgeName = badgeForTaskEvent(event.event);
    if (event.level == QStringLiteral("warning") || event.level == QStringLiteral("error")) {
        badgeName = QStringLiteral("badgeOrange");
    }

    QString text = QStringLiteral("%1 %2 · %3")
                       .arg(stageIndex)
                       .arg(dispatchStageTitle(stageIndex), event.message);

    if (stageIndex == 3 && !event.stepId.isEmpty()) {
        text = QStringLiteral("%1 %2 · %3")
                   .arg(stageIndex)
                   .arg(dispatchStageTitle(stageIndex),
                        QStringLiteral("%1：%2").arg(event.stepId, event.message));
    }

    setProgressStep(stageIndex, text, badgeName);
}

QString MainWindow::formatDispatchUpdateHighlightHtml(const WorkflowTaskUpdateInfo &item) const
{
    QString accentColor = QStringLiteral("#2563EB");
    if (item.level == QStringLiteral("warning")) {
        accentColor = QStringLiteral("#D97706");
    } else if (item.level == QStringLiteral("error")) {
        accentColor = QStringLiteral("#DC2626");
    } else if (item.updateType == QStringLiteral("artifact")) {
        accentColor = QStringLiteral("#059669");
    } else if (item.updateType == QStringLiteral("state")) {
        accentColor = QStringLiteral("#4F46E5");
    }

    const QString typeText = historyUpdateTypeText(item.updateType);
    const QString titleText = item.title.isEmpty() ? item.event : item.title;
    const QString agentText = item.agentId.isEmpty() ? QStringLiteral("系统") : agentDisplayName(item.agentId);
    QString html = QStringLiteral(
                       "<p><span style=\"color:%1;font-weight:700;\">事件流</span> "
                       "<span style=\"color:#64748B;\">[%2 · %3]</span> "
                       "<span style=\"color:#0F172A;\">%4</span> "
                       "<span style=\"color:#64748B;\">(%5)</span></p>")
                       .arg(accentColor,
                            typeText.toHtmlEscaped(),
                            titleText.toHtmlEscaped(),
                            item.message.toHtmlEscaped(),
                            agentText.toHtmlEscaped());
    html += formatWorkspaceSearchResultFromUpdatePayloadHtml(item.payload, 2);
    html += formatHistoryVerificationHtml(historyVerificationFromUpdatePayload(item.payload));
    html += formatHistoryDocumentContextHtml(historyDocumentContextFromUpdatePayload(item.payload), 2);
    html += formatTaskRetrospectiveHtml(item.payload, true);
    if (item.updateType == QStringLiteral("permission") && item.status == QStringLiteral("waiting_permission")) {
        html += QStringLiteral(
            "<p style=\"margin-left:12px;color:#92400E;\">下一步：点击「处理权限」进入历史页确认，"
            "确认前 Runtime 会保持暂停。</p>");
    } else if (item.updateType == QStringLiteral("artifact")) {
        html += QStringLiteral(
            "<p style=\"margin-left:12px;color:#047857;\">下一步：点击「查看产物」进入历史页预览、复制路径或打开受控 outputs 文件。</p>");
    } else if (item.updateType == QStringLiteral("state")) {
        const QString stateMode = item.payload.value(QStringLiteral("mode")).toString(currentDispatchRuntimeMode);
        const QString actionHint = dispatchStateActionHint(stateMode, item.status);
        if (!actionHint.isEmpty()) {
            html += QStringLiteral(
                        "<p style=\"margin-left:12px;color:#334155;\"><b>下一步：</b>%1</p>")
                        .arg(actionHint.toHtmlEscaped());
        }
    }
    return html;
}

QString MainWindow::dispatchStatusTextForState(const QString &mode, const QString &status) const
{
    const bool dryRun = mode == QStringLiteral("dry_run");
    if (status == QStringLiteral("completed")) {
        return dryRun ? QStringLiteral("预演完成") : QStringLiteral("执行完成");
    }
    if (status == QStringLiteral("pending")) {
        return dryRun ? QStringLiteral("等待预演") : QStringLiteral("等待执行");
    }
    if (status == QStringLiteral("running")) {
        return dryRun ? QStringLiteral("预演中") : QStringLiteral("执行中");
    }
    if (status == QStringLiteral("paused")) {
        return QStringLiteral("已暂停");
    }
    if (status == QStringLiteral("waiting_permission")) {
        return QStringLiteral("待确认");
    }
    if (status == QStringLiteral("blocked")) {
        return QStringLiteral("已阻塞");
    }
    if (status == QStringLiteral("failed")) {
        return dryRun ? QStringLiteral("预演失败") : QStringLiteral("执行失败");
    }
    if (status == QStringLiteral("cancelled")) {
        return dryRun ? QStringLiteral("预演取消") : QStringLiteral("已取消");
    }
    return dryRun ? QStringLiteral("预演处理中") : QStringLiteral("处理中");
}

void MainWindow::handleChatFailed(const QString &message)
{
    ui->sendTaskButton->setEnabled(true);
    ui->dispatchChatStatus->setText(QStringLiteral("请求失败"));
    currentDispatchTaskId.clear();
    resetDispatchDeliveryCard();
    currentDispatchPlannedStepCount = 0;
    currentDispatchUpdateWatermark = 0;
    currentDispatchUpdates.clear();
    currentDispatchNeedsClarification = false;
    currentDispatchPresentationHandoff = false;
    currentDispatchPresentationRunning = false;
    currentDispatchPresentationCompleted = false;
    currentDispatchExecutionInProgress = false;
    currentDispatchExecutionSubmitted = false;
    currentDispatchDirectKnowledgeAnswer = false;
    currentDispatchDirectDataAnalysis = false;
    currentDispatchAutoExecutePending = false;
    currentDispatchKnowledgeAnswerResultRequested = false;
    currentDispatchKnowledgeAnswerDelivered = false;
    currentDispatchDataAnalysisDelivered = false;
    currentDispatchKnowledgeAnswerFailed = false;
    currentDispatchDataAnalysisFailed = false;
    currentDispatchKnowledgeAnswerChildTaskId.clear();
    currentDispatchRuntimeMode.clear();
    currentDispatchRuntimeStatus.clear();
    currentDispatchHasPendingPermission = false;
    currentDispatchArtifactCount = 0;
    setDispatchActivityRunning(false);
    if (dispatchUpdateRefreshTimer) {
        dispatchUpdateRefreshTimer->stop();
    }
    updateDispatchActionButtons();
    appendConversationHtml(
        QStringLiteral("<hr/><h3>系统</h3><p style=\"color:#DC2626;\">聊天请求失败：%1</p>")
            .arg(message.toHtmlEscaped()));
}

void MainWindow::resetDispatchDeliveryCard()
{
    // 结果卡只属于当前任务。清空时同时撤销请求状态，避免上一轮慢响应覆盖新会话。
    if (dispatchDeliveryDialog) {
        dispatchDeliveryDialog->close();
    }
    currentDispatchDeliveryCardTaskId.clear();
    currentDispatchDeliveryCardRequestInFlight = false;
    currentDispatchDeliveryCardTerminal = false;
    currentDispatchDeliveryOpenArtifactId.clear();
    currentDispatchDeliveryOpenArtifactTaskId.clear();
    currentDispatchDeliveryPreviewArtifactId.clear();
    currentDispatchDeliveryPreviewArtifactTaskId.clear();
    currentDispatchDeliveryOpenInProgress = false;
    currentDispatchDeliveryImage = QPixmap{};
    if (!ui->dispatchDeliveryCard) {
        return;
    }

    ui->dispatchDeliveryCard->setVisible(false);
    ui->dispatchDeliveryStatus->setText(QStringLiteral("等待结果"));
    polishBadge(ui->dispatchDeliveryStatus, QStringLiteral("badgeGray"));
    ui->dispatchDeliveryText->setHtml(QStringLiteral("<p>任务结果将在这里显示。</p>"));
    ui->dispatchDeliveryImage->clear();
    ui->dispatchDeliveryImage->setVisible(false);
    ui->dispatchDeliveryOpenButton->setVisible(false);
    ui->dispatchDeliveryOpenButton->setEnabled(false);
}

void MainWindow::requestCurrentDispatchDeliveryCardIfTerminal()
{
    if (currentDispatchTaskId.isEmpty()) {
        return;
    }

    const bool terminal = currentDispatchRuntimeStatus == QStringLiteral("completed")
        || currentDispatchRuntimeStatus == QStringLiteral("failed")
        || currentDispatchRuntimeStatus == QStringLiteral("cancelled")
        || currentDispatchRuntimeStatus == QStringLiteral("blocked")
        || currentDispatchRuntimeStatus == QStringLiteral("paused");
    if (!terminal || currentDispatchDeliveryCardRequestInFlight
        || (currentDispatchDeliveryCardTaskId == currentDispatchTaskId
            && currentDispatchDeliveryCardTerminal)) {
        return;
    }

    currentDispatchDeliveryCardTaskId = currentDispatchTaskId;
    currentDispatchDeliveryCardRequestInFlight = true;
    backendClient->requestTaskDeliveryCard(currentDispatchTaskId);
}

QString MainWindow::formatDispatchDeliveryCardHtml(const WorkflowDeliveryCardInfo &card) const
{
    const QString headline = card.headline.trimmed().isEmpty()
        ? QStringLiteral("任务已收到结果")
        : card.headline.trimmed();
    QString html = QStringLiteral(
        "<div style=\"margin:0;color:#0F172A;\"><h3 style=\"margin:0 0 6px 0;\">%1</h3>")
        .arg(headline.toHtmlEscaped());

    if (!card.summaryMarkdown.trimmed().isEmpty()) {
        html += formatDispatchAnswerMarkdownHtml(card.summaryMarkdown);
    }

    if (!card.facts.isEmpty()) {
        html += QStringLiteral(
            "<table border=\"1\" cellspacing=\"0\" cellpadding=\"5\" "
            "style=\"border-color:#D7E5FF;margin:6px 0;width:100%;\">");
        for (const WorkflowDeliveryFactInfo &fact : card.facts) {
            html += QStringLiteral(
                "<tr><th align=\"left\" style=\"background:#F8FBFF;color:#475569;\">%1</th>"
                "<td>%2</td></tr>")
                .arg(fact.label.toHtmlEscaped(), fact.value.toHtmlEscaped());
        }
        html += QStringLiteral("</table>");
    }

    if (card.hasTableSummary) {
        const WorkflowDeliveryTableSummaryInfo &summary = card.tableSummary;
        html += QStringLiteral(
            "<div style=\"margin:6px 0;padding:7px 9px;background:#EEF6FF;color:#174A86;\">"
            "<b>数据交付摘要</b><br/>%1</div>")
            .arg(summary.description.toHtmlEscaped());
    }

    if (!card.artifacts.isEmpty()) {
        html += QStringLiteral("<p style=\"margin:7px 0 3px 0;\"><b>交付物</b></p><ul style=\"margin:2px 0 5px 18px;\">");
        for (const WorkflowDeliveryArtifactInfo &artifact : card.artifacts) {
            const QString name = artifact.name.trimmed().isEmpty()
                ? QStringLiteral("未命名产物")
                : artifact.name.trimmed();
            const QString summary = artifact.summary.trimmed().isEmpty()
                ? QString()
                : QStringLiteral("：%1").arg(artifact.summary.trimmed());
            html += QStringLiteral("<li>%1%2</li>")
                .arg(name.toHtmlEscaped(), summary.toHtmlEscaped());
        }
        html += QStringLiteral("</ul>");
    }

    if (!card.warnings.isEmpty()) {
        html += QStringLiteral(
            "<div style=\"margin-top:6px;padding:6px 8px;background:#FFF7ED;color:#9A3412;\"><b>需要留意</b><ul style=\"margin:3px 0 0 18px;\">");
        for (const QString &warning : card.warnings) {
            html += QStringLiteral("<li>%1</li>").arg(warning.toHtmlEscaped());
        }
        html += QStringLiteral("</ul></div>");
    }

    if (!card.nextActions.isEmpty()) {
        html += QStringLiteral("<p style=\"margin:7px 0 2px 0;color:#475569;\"><b>下一步</b>：%1</p>")
            .arg(card.nextActions.join(QStringLiteral("；")).toHtmlEscaped());
    }
    html += QStringLiteral("</div>");
    return html;
}

void MainWindow::handleTaskDeliveryCardReceived(const WorkflowDeliveryCardInfo &card)
{
    // 任务切换后旧请求可能晚到，必须丢弃，不能把上一轮结果显示到当前会话。
    if (card.taskId.isEmpty() || card.taskId != currentDispatchTaskId) {
        return;
    }

    currentDispatchDeliveryCardTaskId = card.taskId;
    currentDispatchDeliveryCardRequestInFlight = false;
    currentDispatchDeliveryCardTerminal = card.terminal;
    currentDispatchDeliveryOpenArtifactId.clear();
    currentDispatchDeliveryOpenArtifactTaskId.clear();
    currentDispatchDeliveryPreviewArtifactId.clear();
    currentDispatchDeliveryPreviewArtifactTaskId.clear();
    currentDispatchDeliveryOpenInProgress = false;
    currentDispatchDeliveryImage = QPixmap{};
    for (const WorkflowDeliveryArtifactInfo &artifact : card.artifacts) {
        if (currentDispatchDeliveryOpenArtifactId.isEmpty() && artifact.openable) {
            currentDispatchDeliveryOpenArtifactId = artifact.artifactId;
            currentDispatchDeliveryOpenArtifactTaskId = artifact.sourceTaskId.isEmpty()
                ? card.taskId
                : artifact.sourceTaskId;
        }
        if (currentDispatchDeliveryPreviewArtifactId.isEmpty()
            && artifact.previewable
            && artifact.mimeType.startsWith(QStringLiteral("image/"), Qt::CaseInsensitive)
            && artifact.uri.startsWith(QStringLiteral("agentflow-output://data_charts/"))) {
            currentDispatchDeliveryPreviewArtifactId = artifact.artifactId;
            currentDispatchDeliveryPreviewArtifactTaskId = artifact.sourceTaskId.isEmpty()
                ? card.taskId
                : artifact.sourceTaskId;
        }
    }
    // 结果详情放在独立、可移动的知识卡片中；主对话区只保留简短交付摘要。
    ui->dispatchDeliveryCard->setVisible(false);
    ui->dispatchDeliveryText->setHtml(formatDispatchDeliveryCardHtml(card));
    ui->dispatchDeliveryOpenButton->setVisible(!currentDispatchDeliveryOpenArtifactId.isEmpty());
    ui->dispatchDeliveryOpenButton->setEnabled(!currentDispatchDeliveryOpenArtifactId.isEmpty());
    ui->dispatchDeliveryImage->clear();
    ui->dispatchDeliveryImage->setVisible(!currentDispatchDeliveryPreviewArtifactId.isEmpty());
    if (!currentDispatchDeliveryPreviewArtifactId.isEmpty()) {
        ui->dispatchDeliveryImage->setText(QStringLiteral("正在读取已验证图表预览…"));
        backendClient->requestDataChartImage(
            currentDispatchDeliveryPreviewArtifactTaskId.isEmpty()
                ? card.taskId
                : currentDispatchDeliveryPreviewArtifactTaskId,
            currentDispatchDeliveryPreviewArtifactId);
    }

    const bool failed = card.status == QStringLiteral("failed")
        || card.status == QStringLiteral("cancelled")
        || card.status == QStringLiteral("blocked");
    const QString statusText = card.terminal
        ? (failed ? QStringLiteral("未完成") : QStringLiteral("已完成"))
        : QStringLiteral("处理中");
    ui->dispatchDeliveryStatus->setText(statusText);
    polishBadge(ui->dispatchDeliveryStatus,
                card.terminal ? (failed ? QStringLiteral("badgeOrange") : QStringLiteral("badgeGreen"))
                              : QStringLiteral("badgeBlue"));

    // 终态结果不是一条挤在对话底部的小字条：弹出可移动的非模态知识卡片。它只展示已
    // 通过后端交付卡协议的摘要和交付物，客户仍可拖到屏幕边缘查看对话，不会被迫切页。
    if (card.terminal && card.mode != QStringLiteral("dry_run")) {
        showDispatchDeliveryDialog(card);
    }
}

void MainWindow::handleTaskDeliveryCardFailed(const QString &taskId, const QString &message)
{
    if (taskId.isEmpty() || taskId != currentDispatchTaskId) {
        return;
    }

    currentDispatchDeliveryCardRequestInFlight = false;
    currentDispatchDeliveryOpenArtifactId.clear();
    currentDispatchDeliveryOpenArtifactTaskId.clear();
    currentDispatchDeliveryPreviewArtifactId.clear();
    currentDispatchDeliveryPreviewArtifactTaskId.clear();
    currentDispatchDeliveryImage = QPixmap{};
    ui->dispatchDeliveryCard->setVisible(false);
    ui->dispatchDeliveryStatus->setText(QStringLiteral("暂不可用"));
    polishBadge(ui->dispatchDeliveryStatus, QStringLiteral("badgeOrange"));
    ui->dispatchDeliveryText->setHtml(
        QStringLiteral("<p style=\"color:#9A3412;\"><b>结果卡暂时无法读取。</b></p>"
                       "<p>完整结果仍保留在任务历史中。%1</p>")
            .arg(message.toHtmlEscaped()));
    ui->dispatchDeliveryImage->clear();
    ui->dispatchDeliveryImage->setVisible(false);
    ui->dispatchDeliveryOpenButton->setVisible(false);
    ui->dispatchDeliveryOpenButton->setEnabled(false);
}

void MainWindow::openDispatchDeliveryArtifact()
{
    if (currentDispatchTaskId.isEmpty() || currentDispatchDeliveryOpenArtifactId.isEmpty()
        || currentDispatchDeliveryOpenInProgress) {
        return;
    }

    // 只把结果卡中后端已经标记为 openable 的 artifact 交给受控打开接口，Qt 不拼接路径。
    currentDispatchDeliveryOpenInProgress = true;
    ui->dispatchDeliveryOpenButton->setEnabled(false);
    ui->dispatchDeliveryStatus->setText(QStringLiteral("正在打开"));
    polishBadge(ui->dispatchDeliveryStatus, QStringLiteral("badgeBlue"));
    if (dispatchDeliveryDialogStatus) {
        dispatchDeliveryDialogStatus->setText(QStringLiteral("正在打开"));
        polishBadge(dispatchDeliveryDialogStatus, QStringLiteral("badgeBlue"));
    }
    backendClient->requestTaskArtifactOpen(
        currentDispatchDeliveryOpenArtifactTaskId.isEmpty()
            ? currentDispatchTaskId
            : currentDispatchDeliveryOpenArtifactTaskId,
        currentDispatchDeliveryOpenArtifactId);
}

void MainWindow::showDispatchDeliveryDialog(const WorkflowDeliveryCardInfo &card)
{
    if (!dispatchDeliveryDialog) {
        auto *dialog = new QDialog(this, Qt::Tool | Qt::WindowTitleHint | Qt::WindowCloseButtonHint);
        dialog->setAttribute(Qt::WA_DeleteOnClose);
        dialog->setWindowTitle(QStringLiteral("本次结果"));
        dialog->setMinimumSize(620, 440);
        dialog->resize(760, 600);
        dialog->setObjectName(QStringLiteral("dispatchDeliveryDialog"));

        auto *layout = new QVBoxLayout(dialog);
        layout->setContentsMargins(18, 16, 18, 14);
        layout->setSpacing(10);

        auto *header = new QHBoxLayout;
        auto *title = new QLabel(dialog);
        title->setObjectName(QStringLiteral("sectionTitle"));
        title->setMinimumWidth(0);
        header->addWidget(title, 1);
        auto *status = new QLabel(dialog);
        status->setObjectName(QStringLiteral("badgeGreen"));
        header->addWidget(status, 0, Qt::AlignRight | Qt::AlignTop);
        layout->addLayout(header);

        auto *textBrowser = new QTextBrowser(dialog);
        textBrowser->setObjectName(QStringLiteral("subtlePanel"));
        textBrowser->setOpenExternalLinks(false);
        textBrowser->setOpenLinks(false);
        textBrowser->setMinimumHeight(230);
        layout->addWidget(textBrowser, 1);

        auto *image = new QLabel(dialog);
        image->setObjectName(QStringLiteral("subtlePanel"));
        image->setAlignment(Qt::AlignCenter);
        image->setMinimumHeight(90);
        image->setMaximumHeight(190);
        image->setVisible(false);
        layout->addWidget(image);

        auto *actions = new QHBoxLayout;
        actions->addStretch(1);
        auto *openButton = new QPushButton(QStringLiteral("打开交付物"), dialog);
        openButton->setObjectName(QStringLiteral("secondaryButton"));
        openButton->setVisible(false);
        actions->addWidget(openButton);
        auto *historyButton = new QPushButton(QStringLiteral("查看任务详情"), dialog);
        historyButton->setObjectName(QStringLiteral("ghostButton"));
        actions->addWidget(historyButton);
        auto *closeButton = new QPushButton(QStringLiteral("关闭"), dialog);
        closeButton->setObjectName(QStringLiteral("ghostButton"));
        actions->addWidget(closeButton);
        layout->addLayout(actions);

        dispatchDeliveryDialog = dialog;
        dispatchDeliveryDialogText = textBrowser;
        dispatchDeliveryDialogImage = image;
        dispatchDeliveryDialogStatus = status;
        dispatchDeliveryDialogOpenButton = openButton;
        dispatchDeliveryDialogHistoryButton = historyButton;
        connect(openButton, &QPushButton::clicked, this, &MainWindow::openDispatchDeliveryArtifact);
        connect(historyButton, &QPushButton::clicked, this, &MainWindow::openCurrentDispatchTaskInHistory);
        connect(closeButton, &QPushButton::clicked, dialog, &QDialog::close);
    }

    const QString headline = card.headline.trimmed().isEmpty()
        ? QStringLiteral("本次结果")
        : card.headline.trimmed();
    if (dispatchDeliveryDialog) {
        dispatchDeliveryDialog->setWindowTitle(headline);
        dispatchDeliveryDialog->show();
        dispatchDeliveryDialog->raise();
        dispatchDeliveryDialog->activateWindow();
    }
    if (dispatchDeliveryDialogText) {
        dispatchDeliveryDialogText->setHtml(formatDispatchDeliveryCardHtml(card));
    }
    if (dispatchDeliveryDialogStatus) {
        const bool failed = card.status == QStringLiteral("failed")
            || card.status == QStringLiteral("cancelled")
            || card.status == QStringLiteral("blocked");
        dispatchDeliveryDialogStatus->setText(card.terminal
                                                   ? (failed ? QStringLiteral("未完成") : QStringLiteral("已完成"))
                                                   : QStringLiteral("处理中"));
        polishBadge(dispatchDeliveryDialogStatus,
                    card.terminal ? (failed ? QStringLiteral("badgeOrange") : QStringLiteral("badgeGreen"))
                                  : QStringLiteral("badgeBlue"));
    }
    if (dispatchDeliveryDialogOpenButton) {
        const bool available = !currentDispatchDeliveryOpenArtifactId.isEmpty();
        dispatchDeliveryDialogOpenButton->setVisible(available);
        dispatchDeliveryDialogOpenButton->setEnabled(available);
    }
    if (dispatchDeliveryDialogImage) {
        const bool hasPreview = !currentDispatchDeliveryPreviewArtifactId.isEmpty();
        dispatchDeliveryDialogImage->setVisible(hasPreview);
        dispatchDeliveryDialogImage->setPixmap(QPixmap{});
        dispatchDeliveryDialogImage->setText(
            hasPreview ? QStringLiteral("正在读取已验证图表预览…") : QString());
    }
    renderDispatchDeliveryImage();
}

void MainWindow::renderDispatchDeliveryImage()
{
    if (currentDispatchDeliveryImage.isNull()) {
        return;
    }

    if (ui->dispatchDeliveryImage && ui->dispatchDeliveryImage->isVisible()) {
        const int availableWidth = qMax(120, ui->dispatchDeliveryImage->width() - 16);
        const QSize targetSize(availableWidth, 88);
        const QPixmap rendered = currentDispatchDeliveryImage.scaled(
            targetSize, Qt::KeepAspectRatio, Qt::SmoothTransformation);
        ui->dispatchDeliveryImage->setPixmap(rendered);
        ui->dispatchDeliveryImage->setText(QString());
    }

    if (dispatchDeliveryDialogImage && dispatchDeliveryDialogImage->isVisible()) {
        const int dialogWidth = qMax(180, dispatchDeliveryDialogImage->width() - 20);
        const QPixmap dialogRendered = currentDispatchDeliveryImage.scaled(
            QSize(dialogWidth, 170), Qt::KeepAspectRatio, Qt::SmoothTransformation);
        dispatchDeliveryDialogImage->setPixmap(dialogRendered);
        dispatchDeliveryDialogImage->setText(QString());
    }
}

void MainWindow::handleTaskLogReceived(const TaskLogEvent &event)
{
    if (!activeDataChartExportTaskId.isEmpty() && event.taskId == activeDataChartExportTaskId) {
        // 看板页只显示当前阶段，逐条工具日志仍通过统一历史页复盘，避免主工作台被渲染细节挤满。
        ui->dataProfileStatus->setText(event.message.left(90));
        polishBadge(ui->dataProfileStatus,
                    event.level == QStringLiteral("error") || event.level == QStringLiteral("warning")
                        ? QStringLiteral("badgeOrange")
                        : QStringLiteral("badgeBlue"));
        return;
    }
    if (!activeDataWorkbookExportTaskId.isEmpty() && event.taskId == activeDataWorkbookExportTaskId) {
        // 数据工作台只显示后端实际发布的阶段；完整审计仍保留在历史页，避免主工作台变成日志墙。
        ui->dataProfileStatus->setText(event.message.left(90));
        polishBadge(ui->dataProfileStatus,
                    event.level == QStringLiteral("error") || event.level == QStringLiteral("warning")
                        ? QStringLiteral("badgeOrange")
                        : QStringLiteral("badgeBlue"));
        return;
    }
    if (!activePdfProcessingTaskId.isEmpty() && event.taskId == activePdfProcessingTaskId) {
        // PDF 工具只给用户显示确定性的处理阶段，完整事件仍留在统一任务历史中。
        if (pdfProcessingStatusLabel) {
            pdfProcessingStatusLabel->setText(event.message.left(90));
            polishBadge(pdfProcessingStatusLabel,
                        event.level == QStringLiteral("error") || event.level == QStringLiteral("warning")
                            ? QStringLiteral("badgeOrange")
                            : QStringLiteral("badgeBlue"));
        }
        return;
    }
    if (!activeProjectDocumentReviewTaskId.isEmpty()
        && event.taskId == activeProjectDocumentReviewTaskId) {
        // 审查页只显示由后端实际发布的阶段，完整规则和来源仍在完成后的独立阅读窗口与任务历史。
        ui->documentRunStatus->setText(event.message.left(80));
        polishBadge(ui->documentRunStatus,
                    event.level == QStringLiteral("error") || event.level == QStringLiteral("warning")
                        ? QStringLiteral("badgeOrange")
                        : QStringLiteral("badgeBlue"));
        return;
    }
    if (!activePaperReviewTaskId.isEmpty() && event.taskId == activePaperReviewTaskId) {
        ui->documentRunStatus->setText(event.message.left(80));
        polishBadge(ui->documentRunStatus,
                    event.level == QStringLiteral("error") || event.level == QStringLiteral("warning")
                        ? QStringLiteral("badgeOrange")
                        : QStringLiteral("badgeBlue"));
        return;
    }
    if (!activeDocumentAgentTaskId.isEmpty() && event.taskId == activeDocumentAgentTaskId) {
        // 文档页只显示本次真实阶段，不把底层日志混进调度台聊天记录。
        ui->documentRunStatus->setText(event.message.left(80));
        polishBadge(ui->documentRunStatus,
                    event.level == QStringLiteral("error") ? QStringLiteral("badgeOrange")
                    : event.level == QStringLiteral("warning") ? QStringLiteral("badgeOrange")
                                                       : QStringLiteral("badgeBlue"));
        return;
    }

    if (currentDispatchTaskId.isEmpty() || event.taskId != currentDispatchTaskId) {
        return;
    }

    // 聊天面只承担“正在做什么”和最终交付；逐条事件属于可追溯审计，继续保留在任务历史。
    updateDispatchProgressFromLogEvent(event);
    scheduleDispatchUpdatesRefresh(250);
    if (isCurrentDispatchAutoReadOnlyTask()
        || currentDispatchDataChartDelivery
        || currentDispatchDataWorkbookDelivery
        || currentDispatchPresentationRunning) {
        ui->dispatchChatStatus->setText(currentDispatchPresentationRunning
                                            ? QStringLiteral("正在制作 PPT")
                                            : currentDispatchAutoReadOnlyActivityText());
        setDispatchActivityRunning(true);
    }
}

void MainWindow::handleTaskLogFinished(const QString &taskId)
{
    if (!activeDataTransformationTaskId.isEmpty() && taskId == activeDataTransformationTaskId) {
        ui->dataProfileStatus->setText(QStringLiteral("正在读取已验证的字段副本"));
        polishBadge(ui->dataProfileStatus, QStringLiteral("badgeBlue"));
        backendClient->requestDataTransformationExportResult(taskId);
        return;
    }
    if (!activeDataChartExportTaskId.isEmpty() && taskId == activeDataChartExportTaskId) {
        ui->dataProfileStatus->setText(QStringLiteral("正在读取已验证的图表看板"));
        polishBadge(ui->dataProfileStatus, QStringLiteral("badgeBlue"));
        backendClient->requestDataChartExportResult(taskId);
        return;
    }
    if (!activeDataWorkbookExportTaskId.isEmpty() && taskId == activeDataWorkbookExportTaskId) {
        ui->dataProfileStatus->setText(QStringLiteral("正在读取已验证的 Excel 结果"));
        polishBadge(ui->dataProfileStatus, QStringLiteral("badgeBlue"));
        backendClient->requestDataAnalysisWorkbookExportResult(taskId);
        return;
    }
    if (!activePdfProcessingTaskId.isEmpty() && taskId == activePdfProcessingTaskId) {
        if (pdfProcessingStatusLabel) {
            pdfProcessingStatusLabel->setText(QStringLiteral("正在读取已验证的 PDF 结果"));
            polishBadge(pdfProcessingStatusLabel, QStringLiteral("badgeBlue"));
        }
        backendClient->requestPdfProcessingResult(taskId);
        return;
    }
    if (!activeDocumentAgentTaskId.isEmpty() && taskId == activeDocumentAgentTaskId) {
        ui->documentRunStatus->setText(QStringLiteral("正在读取已校验的分析结果"));
        polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
        backendClient->requestDocumentAgentResult(taskId);
        return;
    }
    if (!activeProjectDocumentReviewTaskId.isEmpty() && taskId == activeProjectDocumentReviewTaskId) {
        ui->documentRunStatus->setText(QStringLiteral("正在读取已校验的项目审查报告"));
        polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
        backendClient->requestProjectDocumentReviewResult(taskId);
        return;
    }
    if (!activePaperReviewTaskId.isEmpty() && taskId == activePaperReviewTaskId) {
        ui->documentRunStatus->setText(QStringLiteral("正在读取已校验的论文审查报告"));
        polishBadge(ui->documentRunStatus, QStringLiteral("badgeBlue"));
        backendClient->requestPaperReviewResult(taskId);
        return;
    }

    if (taskId == currentDispatchTaskId) {
        scheduleDispatchUpdatesRefresh(0);
        const bool autoStartAfterDryRun = isCurrentDispatchAutoReadOnlyTask()
            || currentDispatchDataChartDelivery
            || currentDispatchDataWorkbookDelivery;
        if (autoStartAfterDryRun
            && currentDispatchAutoExecutePending
            && currentDispatchRuntimeMode == QStringLiteral("dry_run")) {
            // 计划已完整落库后才转 Runtime，避免 /start 与 dry-run 的异步收束产生竞态。
            // 明确图表/Excel 请求的本地写入由后端专门策略放行；联网、命令和其它写入仍
            // 保留原有确认入口。
            currentDispatchAutoExecutePending = false;
            beginCurrentDispatchRuntime(true);
            return;
        }
        if (currentDispatchPresentationRunning) {
            ui->dispatchChatStatus->setText(QStringLiteral("正在制作 PPT"));
            setDispatchActivityRunning(true);
        } else if (autoStartAfterDryRun) {
            ui->dispatchChatStatus->setText(currentDispatchAutoReadOnlyActivityText());
            setDispatchActivityRunning(true);
        } else if (currentDispatchUpdates.isEmpty()) {
            ui->dispatchChatStatus->setText(QStringLiteral("预演完成"));
        }
    }
}

void MainWindow::handleTaskLogFailed(const QString &message)
{
    Q_UNUSED(message);
    if (dataTransformationExportLoading && !activeDataTransformationTaskId.isEmpty()) {
        ui->dataProfileStatus->setText(QStringLiteral("实时进度连接中断，正在确认字段副本结果"));
        polishBadge(ui->dataProfileStatus, QStringLiteral("badgeOrange"));
        backendClient->requestDataTransformationExportResult(activeDataTransformationTaskId);
        return;
    }
    if (dataChartExportLoading && !activeDataChartExportTaskId.isEmpty()) {
        ui->dataProfileStatus->setText(QStringLiteral("实时进度连接中断，正在确认图表结果"));
        polishBadge(ui->dataProfileStatus, QStringLiteral("badgeOrange"));
        backendClient->requestDataChartExportResult(activeDataChartExportTaskId);
        return;
    }
    if (dataWorkbookExportLoading && !activeDataWorkbookExportTaskId.isEmpty()) {
        ui->dataProfileStatus->setText(QStringLiteral("实时进度连接中断，正在确认 Excel 结果"));
        polishBadge(ui->dataProfileStatus, QStringLiteral("badgeOrange"));
        backendClient->requestDataAnalysisWorkbookExportResult(activeDataWorkbookExportTaskId);
        return;
    }
    if (pdfProcessingRunning && !activePdfProcessingTaskId.isEmpty()) {
        if (pdfProcessingStatusLabel) {
            pdfProcessingStatusLabel->setText(QStringLiteral("实时进度连接中断，正在确认 PDF 结果"));
            polishBadge(pdfProcessingStatusLabel, QStringLiteral("badgeOrange"));
        }
        backendClient->requestPdfProcessingResult(activePdfProcessingTaskId);
        return;
    }
    if (documentAgentRunning && !activeDocumentAgentTaskId.isEmpty()) {
        ui->documentRunStatus->setText(QStringLiteral("实时进度连接中断，正在确认最终结果"));
        polishBadge(ui->documentRunStatus, QStringLiteral("badgeOrange"));
        backendClient->requestDocumentAgentResult(activeDocumentAgentTaskId);
        return;
    }
    if (projectDocumentReviewLoading && !activeProjectDocumentReviewTaskId.isEmpty()) {
        ui->documentRunStatus->setText(QStringLiteral("项目审查进度连接中断，正在确认最终报告"));
        polishBadge(ui->documentRunStatus, QStringLiteral("badgeOrange"));
        backendClient->requestProjectDocumentReviewResult(activeProjectDocumentReviewTaskId);
        return;
    }
    if (paperReviewLoading && !activePaperReviewTaskId.isEmpty()) {
        ui->documentRunStatus->setText(QStringLiteral("论文审查进度连接中断，正在确认最终报告"));
        polishBadge(ui->documentRunStatus, QStringLiteral("badgeOrange"));
        backendClient->requestPaperReviewResult(activePaperReviewTaskId);
        return;
    }

    ui->dispatchChatStatus->setText(isCurrentDispatchAutoReadOnlyTask()
                                        ? QStringLiteral("正在确认只读任务状态")
                                        : QStringLiteral("正在确认任务状态"));
    if (isCurrentDispatchAutoReadOnlyTask()) {
        setDispatchActivityRunning(true);
    }
    if (!currentDispatchTaskId.isEmpty()) {
        scheduleDispatchUpdatesRefresh(0);
    }
}

void MainWindow::appendConversationHtml(const QString &html)
{
    // QTextEdit 使用增量插入，避免每条日志都重建整段 HTML。
    QTextCursor cursor = ui->conversationTextEdit->textCursor();
    cursor.movePosition(QTextCursor::End);
    ui->conversationTextEdit->setTextCursor(cursor);
    ui->conversationTextEdit->insertHtml(html);
    ui->conversationTextEdit->insertHtml(QStringLiteral("<br/>"));
    ui->conversationTextEdit->ensureCursorVisible();
}

void MainWindow::resetProgressPanel()
{
    setProgressStep(1, QStringLiteral("1 任务提交 · 等待中"), QStringLiteral("badgeGray"));
    setProgressStep(2, QStringLiteral("2 Commander 规划 · 等待中"), QStringLiteral("badgeGray"));
    setProgressStep(3, QStringLiteral("3 Workflow 推进 · 等待中"), QStringLiteral("badgeGray"));
    setProgressStep(4, QStringLiteral("4 权限 / 产物 · 等待中"), QStringLiteral("badgeGray"));
    setProgressStep(5, QStringLiteral("5 当前结论 · 等待中"), QStringLiteral("badgeGray"));
}

void MainWindow::setProgressStep(int sequence, const QString &text, const QString &badgeObjectName)
{
    const QList<QLabel *> labels = {
        ui->progress1,
        ui->progress2,
        ui->progress3,
        ui->progress4,
        ui->progress5
    };

    if (sequence < 1 || sequence > labels.size()) {
        return;
    }

    QLabel *label = labels.at(sequence - 1);
    label->setText(text);
    label->setWordWrap(true);
    polishBadge(label, badgeObjectName);
}
