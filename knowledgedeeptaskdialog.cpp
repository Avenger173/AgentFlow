#include "knowledgedeeptaskdialog.h"

#include "taskactivityindicator.h"
#include "ui_knowledgedeeptaskdialog.h"

#include <QIcon>
#include <QJsonArray>
#include <QListWidgetItem>
#include <QMessageBox>
#include <QPushButton>
#include <QStyle>
#include <QTextBrowser>
#include <QTimer>

namespace {

QString markdownEscaped(const QString &value)
{
    // UI 自己拼接的资料名、标题和定位信息来自客户文件元数据，因此转义 Markdown 标记；模型输出
    // 已通过后端结构化契约，只在最终结论区域按 Markdown 读取。
    return value.toHtmlEscaped();
}

QString sourceLocatorText(const QJsonObject &source)
{
    const QString kind = source.value(QStringLiteral("source_kind")).toString();
    const QString locator = source.value(QStringLiteral("source_locator")).toString();
    if (kind == QStringLiteral("page")) {
        return QStringLiteral("第 %1 页").arg(locator);
    }
    if (kind == QStringLiteral("line")) {
        return QStringLiteral("第 %1 行").arg(locator);
    }
    if (kind == QStringLiteral("paragraph")) {
        return QStringLiteral("第 %1 段").arg(locator);
    }
    return locator.isEmpty() ? QStringLiteral("可定位章节") : locator;
}

QString statusText(const QString &status)
{
    if (status == QStringLiteral("queued") || status == QStringLiteral("pending")) {
        return QStringLiteral("等待执行");
    }
    if (status == QStringLiteral("running")) {
        return QStringLiteral("正在分析");
    }
    if (status == QStringLiteral("paused")) {
        return QStringLiteral("已暂停");
    }
    if (status == QStringLiteral("blocked")) {
        return QStringLiteral("需要处理");
    }
    if (status == QStringLiteral("completed")) {
        return QStringLiteral("已完成");
    }
    if (status == QStringLiteral("cancelled")) {
        return QStringLiteral("已取消");
    }
    if (status == QStringLiteral("failed")) {
        return QStringLiteral("未完成");
    }
    return QStringLiteral("等待任务");
}

QString bulletList(const QJsonArray &values)
{
    QString markdown;
    for (const QJsonValue &value : values) {
        if (value.isString() && !value.toString().trimmed().isEmpty()) {
            markdown += QStringLiteral("- %1\n").arg(value.toString());
        }
    }
    return markdown;
}

QString markdownTableCell(const QString &value)
{
    // 模型已通过后端 JSON 契约，但表格仍需避免竖线或换行改变 Markdown 结构。
    QString normalized = value;
    normalized.replace(QStringLiteral("|"), QStringLiteral("\\|"));
    normalized.replace(QChar::LineFeed, QChar::Space);
    normalized.replace(QChar::CarriageReturn, QChar::Space);
    return normalized.simplified().isEmpty() ? QStringLiteral("-") : normalized.simplified();
}

} // namespace

KnowledgeDeepTaskDialog::KnowledgeDeepTaskDialog(
    BackendClient *backendClient,
    const QString &knowledgeBaseId,
    const QString &knowledgeBaseName,
    const QList<KnowledgeDocumentInfo> &knowledgeDocuments,
    QWidget *parent)
    : QDialog(parent)
    , ui(new Ui::KnowledgeDeepTaskDialog)
    , backendClient(backendClient)
    , pollTimer(new QTimer(this))
    , knowledgeBaseId(knowledgeBaseId)
    , comparisonDocuments(knowledgeDocuments)
{
    ui->setupUi(this);
    // 长任务的旋转标志和后端终态绑定，不会因旧阶段文案仍在界面上而误导客户。
    activityIndicator = new TaskActivityIndicator(this);
    ui->executionHeaderLayout->insertWidget(1, activityIndicator);
    ui->knowledgeIcon->setPixmap(QIcon(QStringLiteral(":/icons/knowledge.svg")).pixmap(34, 34));
    // K3 问答与 K4 长任务共用一致的视觉语言，但不合并成一张密集页面：前者是一次检索回答，
    // 后者要呈现冻结范围、暂停恢复和交付资格。用更明确的标题避免客户把两者误认成重复功能。
    ui->dialogTitle->setText(QStringLiteral("深度任务工作台"));
    ui->dialogSubtitle->setText(QStringLiteral("完整资料深度总结，或面向指定资料的可恢复对照"));
    ui->requestTitle->setText(QStringLiteral("分析目标与范围"));
    ui->resultTitle->setText(QStringLiteral("任务交付与覆盖"));
    ui->knowledgeBaseLabel->setText(knowledgeBaseName.isEmpty() ? QStringLiteral("当前资料库") : knowledgeBaseName);
    ui->taskKindComboBox->addItem(QStringLiteral("全库深度总结"), QStringLiteral("summary"));
    ui->taskKindComboBox->addItem(QStringLiteral("资料对照表"), QStringLiteral("comparison"));
    for (const KnowledgeDocumentInfo &document : comparisonDocuments) {
        auto *item = new QListWidgetItem(document.displayName, ui->comparisonDocumentList);
        item->setData(Qt::UserRole, document.documentId);
        item->setToolTip(QStringLiteral("只比较该资料当前活动版本的已索引章节"));
    }
    ui->taskGoalEdit->setFocus();
    ui->resultSplitter->setStretchFactor(0, 8);
    ui->resultSplitter->setStretchFactor(1, 3);
    ui->resultSplitter->setSizes({760, 310});
    pollTimer->setInterval(850);
    updateTaskKindUi();
    updateControlState(QString(), false);

    connect(ui->startDeepTaskButton, &QPushButton::clicked, this, &KnowledgeDeepTaskDialog::startTask);
    connect(ui->taskKindComboBox,
            qOverload<int>(&QComboBox::currentIndexChanged),
            this,
            [this](int) { updateTaskKindUi(); });
    connect(ui->pauseButton, &QPushButton::clicked, this, [this]() { requestControl(QStringLiteral("pause")); });
    connect(ui->resumeButton, &QPushButton::clicked, this, [this]() { requestControl(QStringLiteral("resume")); });
    connect(ui->cancelButton, &QPushButton::clicked, this, [this]() { requestControl(QStringLiteral("cancel")); });
    connect(ui->scopeToggleButton, &QPushButton::clicked, this, [this]() {
        setInspectorVisible(!inspectorVisible);
    });
    connect(ui->scopeList, &QListWidget::currentRowChanged, this, [this](int) { showSelectedScopeUnit(); });
    connect(ui->historyButton, &QPushButton::clicked, this, [this]() {
        if (!currentTaskId.isEmpty()) {
            emit openTaskHistoryRequested(currentTaskId);
        }
    });
    connect(ui->exportReportButton, &QPushButton::clicked, this, &KnowledgeDeepTaskDialog::confirmReportExport);
    connect(ui->closeButton, &QPushButton::clicked, this, &QDialog::reject);
    connect(pollTimer, &QTimer::timeout, this, &KnowledgeDeepTaskDialog::requestLatestResult);

    connect(backendClient, &BackendClient::knowledgeDeepTaskStarted, this,
            [this](const KnowledgeDeepTaskStartResult &result) {
                if (!taskRunning || result.taskId.isEmpty()) {
                    return;
                }
                currentTaskId = result.taskId;
                currentStatus = result.status;
                activityIndicator->setRunning(true);
                ui->historyButton->setEnabled(true);
                updateControlState(currentStatus, false);
                updateExecutionState(QStringLiteral("任务已受理，正在冻结当前资料版本并连接真实阶段。"));
                this->backendClient->connectTaskLog(currentTaskId);
                pollTimer->start();
                requestLatestResult();
            });
    connect(backendClient, &BackendClient::knowledgeDeepTaskStillRunning, this,
            [this](const QString &taskId, const QString &status) {
                if (taskRunning && taskId == currentTaskId) {
                    currentStatus = status;
                    updateControlState(currentStatus, false);
                }
            });
    connect(backendClient, &BackendClient::knowledgeDeepTaskResultReceived, this,
            [this](const KnowledgeDeepTaskResult &result) {
                if (!currentTaskId.isEmpty() && result.taskId == currentTaskId) {
                    handleTaskResult(result);
                }
            });
    connect(backendClient, &BackendClient::knowledgeDeepTaskControlCompleted, this,
            [this](const KnowledgeDeepTaskControlResult &result) {
                if (result.taskId != currentTaskId) {
                    return;
                }
                currentStatus = result.status;
                taskRunning = result.status == QStringLiteral("queued") || result.status == QStringLiteral("pending")
                              || result.status == QStringLiteral("running");
                activityIndicator->setRunning(taskRunning);
                if (taskRunning) {
                    pollTimer->start();
                } else {
                    pollTimer->stop();
                }
                updateControlState(currentStatus, false);
                updateExecutionState(
                    result.message.isEmpty() ? QStringLiteral("任务控制状态已更新。") : result.message,
                    result.accepted ? (taskRunning ? QStringLiteral("running") : QStringLiteral("warning"))
                                    : QStringLiteral("warning"));
                requestLatestResult();
            });
    connect(backendClient, &BackendClient::knowledgeDeepTaskReportExported, this,
            [this](const KnowledgeDeepTaskReportExportResult &result) {
                if (result.taskId != currentTaskId) {
                    return;
                }
                const QString message = result.message.isEmpty()
                                            ? QStringLiteral("正式报告已创建，并已追加到本次任务历史。")
                                            : result.message;
                updateExecutionState(message, QStringLiteral("completed"));
                ui->exportReportButton->setEnabled(false);
            });
    connect(backendClient, &BackendClient::knowledgeDeepTaskFailed, this, [this](const QString &message) {
        if (taskRunning && currentTaskId.isEmpty()) {
            handleTaskFailure(message);
        } else if (!currentTaskId.isEmpty()) {
            // HTTP/网络补读失败不等于 Runtime 已失败。保留本地任务身份和轮询，让短暂断连恢复后
            // 继续显示真实状态；客户也始终可以从统一历史打开同一任务。
            updateExecutionState(
                QStringLiteral("客户端暂时无法确认任务状态：%1。后台任务未被客户端停止，将继续尝试刷新。")
                    .arg(message),
                QStringLiteral("warning"));
            if (taskRunning && !pollTimer->isActive()) {
                pollTimer->start();
            }
        }
    });
    connect(backendClient, &BackendClient::taskLogReceived, this, [this](const TaskLogEvent &event) {
        if (event.taskId == currentTaskId && !event.message.trimmed().isEmpty()) {
            updateExecutionState(
                event.message,
                event.level == QStringLiteral("error") ? QStringLiteral("failed")
                                                        : (event.level == QStringLiteral("warning")
                                                               ? QStringLiteral("warning")
                                                               : QStringLiteral("running")));
        }
    });
    connect(backendClient, &BackendClient::taskLogFinished, this, [this](const QString &taskId) {
        if (taskRunning && taskId == currentTaskId) {
            requestLatestResult();
        }
    });
}

KnowledgeDeepTaskDialog::~KnowledgeDeepTaskDialog()
{
    delete ui;
}

void KnowledgeDeepTaskDialog::openExistingTask(const QString &taskId)
{
    const QString normalizedTaskId = taskId.trimmed();
    if (normalizedTaskId.isEmpty()) {
        return;
    }

    // 关联入口只恢复已存在任务的观察和控制，不展示会新建 scope 的输入区域；这样历史页不会把
    // “查看子任务”误做成另一次深度分析，也让结论与冻结范围获得完整阅读空间。
    existingTaskMode = true;
    currentTaskId = normalizedTaskId;
    currentStatus = QStringLiteral("pending");
    taskRunning = true;
    currentScopeUnits.clear();
    ui->requestFrame->setVisible(false);
    ui->dialogTitle->setText(QStringLiteral("关联深度任务"));
    ui->dialogSubtitle->setText(QStringLiteral("由总指挥委派；范围、进度与控制权保持在此任务中"));
    ui->knowledgeBaseLabel->setText(QStringLiteral("关联资料库"));
    ui->knowledgeBaseLabel->setToolTip(QStringLiteral("正在读取该任务已冻结的资料范围。"));
    ui->resultTitle->setText(QStringLiteral("任务交付与真实进度"));
    ui->scopeCountLabel->setText(QStringLiteral("正在读取"));
    ui->scopeHint->setText(QStringLiteral("任务加载后显示已冻结的文件、章节和定位信息。"));
    ui->scopeList->clear();
    ui->scopeDetailBrowser->setMarkdown(QStringLiteral("正在读取冻结范围；这里不会展示原始章节正文。"));
    ui->resultBrowser->setMarkdown(
        QStringLiteral("# 正在读取关联深度任务\n\n系统只补读已保存的范围、阶段和检查点，不会重新分析资料。"));
    ui->historyButton->setEnabled(true);
    activityIndicator->setRunning(true);
    updateControlState(currentStatus, false);
    updateExecutionState(QStringLiteral("正在读取已冻结范围和已保存进度。"));
    backendClient->connectTaskLog(currentTaskId);
    pollTimer->start();
    requestLatestResult();
}

void KnowledgeDeepTaskDialog::startTask()
{
    const QString goal = ui->taskGoalEdit->toPlainText().trimmed();
    if (goal.size() < 2) {
        updateExecutionState(QStringLiteral("请用一句话说明目标，例如“比较各资料对交付范围的差异”。"), QStringLiteral("warning"));
        ui->taskGoalEdit->setFocus();
        return;
    }
    const QString kind = taskKind();
    const QStringList documentIds = selectedComparisonDocumentIds();
    if (kind == QStringLiteral("comparison") && documentIds.size() < 2) {
        updateExecutionState(QStringLiteral("资料对照请至少选择两份资料，再开始分析。"), QStringLiteral("warning"));
        ui->comparisonDocumentList->setFocus();
        return;
    }
    if (kind == QStringLiteral("comparison") && documentIds.size() > 12) {
        updateExecutionState(QStringLiteral("一次资料对照最多选择 12 份资料，避免表格在窗口和报告中失去可读性。"),
                             QStringLiteral("warning"));
        ui->comparisonDocumentList->setFocus();
        return;
    }

    currentTaskId.clear();
    currentStatus = QStringLiteral("queued");
    currentScopeUnits.clear();
    ui->scopeList->clear();
    ui->scopeDetailBrowser->setMarkdown(QStringLiteral("范围在任务受理后冻结；这里不会展示原始章节正文。"));
    ui->scopeCountLabel->setText(QStringLiteral("等待冻结"));
    ui->scopeHint->setText(QStringLiteral("范围会显示本次冻结的文件和章节；新任务不会因章节数量静默裁剪。"));
    ui->resultBrowser->setMarkdown(
        QStringLiteral("# 正在创建深度任务\n\n系统将冻结当前活动索引版本，随后按章节执行受控 Map/Reduce 分析。"));
    taskRunning = true;
    activityIndicator->setRunning(true);
    pollTimer->stop();
    ui->taskGoalEdit->setEnabled(false);
    ui->taskKindComboBox->setEnabled(false);
    ui->comparisonDocumentList->setEnabled(false);
    ui->historyButton->setEnabled(false);
    updateControlState(currentStatus, false);
    updateExecutionState(QStringLiteral("正在提交任务；资料内容不会直接写入界面或任务日志。"));
    backendClient->startKnowledgeDeepTask(knowledgeBaseId, kind, goal, documentIds);
}

void KnowledgeDeepTaskDialog::requestLatestResult()
{
    if (!currentTaskId.isEmpty() && taskRunning) {
        backendClient->requestKnowledgeDeepTaskResult(currentTaskId);
    }
}

void KnowledgeDeepTaskDialog::requestControl(const QString &action)
{
    if (currentTaskId.isEmpty()) {
        return;
    }
    if (action == QStringLiteral("pause")) {
        backendClient->pauseKnowledgeDeepTask(currentTaskId);
    } else if (action == QStringLiteral("resume")) {
        backendClient->resumeKnowledgeDeepTask(currentTaskId);
    } else if (action == QStringLiteral("cancel")) {
        backendClient->cancelKnowledgeDeepTask(currentTaskId);
    }
}

void KnowledgeDeepTaskDialog::handleTaskResult(const KnowledgeDeepTaskResult &result)
{
    currentStatus = result.status;
    taskRunning = result.status == QStringLiteral("queued") || result.status == QStringLiteral("pending")
                  || result.status == QStringLiteral("running");
    activityIndicator->setRunning(taskRunning);
    if (taskRunning) {
        pollTimer->start();
    } else {
        pollTimer->stop();
    }
    // 新建模式在任务结束后可再次编辑；关联模式只观察同一任务，不能把它变成新的分析请求。
    if (!existingTaskMode) {
        ui->taskGoalEdit->setEnabled(!taskRunning);
        ui->taskKindComboBox->setEnabled(!taskRunning);
        ui->comparisonDocumentList->setEnabled(!taskRunning && taskKind() == QStringLiteral("comparison"));
    }
    ui->historyButton->setEnabled(!result.taskId.isEmpty());
    showScope(result.scope);
    ui->resultBrowser->setMarkdown(resultMarkdown(result));
    const bool reportAvailable = result.reportReadiness.value(QStringLiteral("can_export")).toBool();
    updateControlState(currentStatus, reportAvailable);

    QString state = taskRunning ? QStringLiteral("running") : QStringLiteral("failed");
    if (result.status == QStringLiteral("completed")) {
        state = QStringLiteral("completed");
    } else if (result.status == QStringLiteral("paused") || result.status == QStringLiteral("blocked")
               || result.status == QStringLiteral("cancelled")) {
        state = QStringLiteral("warning");
    }
    QString executionMessage = result.summary.isEmpty() ? QStringLiteral("深度任务状态已更新。") : result.summary;
    if (result.status == QStringLiteral("blocked")) {
        // “blocked”并不等同于任务已结束：客户能安全地从 checkpoint 重试失败节点，因此把下一步
        // 直接写在状态区，且让同一行的主按钮使用“继续并重试”而不是含糊的“继续”。
        executionMessage += QStringLiteral("\n可点击“继续并重试”从已保存检查点恢复；已完成章节不会重复消耗模型额度。");
    }
    updateExecutionState(executionMessage, state);
}

void KnowledgeDeepTaskDialog::handleTaskFailure(const QString &message)
{
    taskRunning = false;
    activityIndicator->setRunning(false);
    pollTimer->stop();
    if (!existingTaskMode) {
        ui->taskGoalEdit->setEnabled(true);
        ui->taskKindComboBox->setEnabled(true);
        // 提交前的 HTTP/网络失败并不冻结客户的资料选择；恢复当前任务类型对应的控件状态。
        updateTaskKindUi();
    }
    updateControlState(QStringLiteral("failed"), false);
    ui->resultBrowser->setMarkdown(
        QStringLiteral("# 本次深度分析未完成\n\n%1\n\n系统没有显示未经验证的章节结论；已完成的检查点可在任务历史中复盘。")
            .arg(markdownEscaped(message)));
    updateExecutionState(message, QStringLiteral("failed"));
}

void KnowledgeDeepTaskDialog::showScope(const QJsonObject &scope)
{
    currentScopeUnits.clear();
    ui->scopeList->clear();
    const QJsonArray units = scope.value(QStringLiteral("map_units")).toArray();
    for (const QJsonValue &value : units) {
        if (!value.isObject()) {
            continue;
        }
        const QJsonObject unit = value.toObject();
        const QString label = scopeUnitLabel(unit);
        auto *item = new QListWidgetItem(label, ui->scopeList);
        item->setToolTip(label);
        currentScopeUnits.append(unit);
    }
    const int documentCount = scope.value(QStringLiteral("covered_document_count")).toInt();
    const int availableMapCount = scope.value(QStringLiteral("available_map_count")).toInt();
    const int availableDocumentCount = scope.value(QStringLiteral("available_document_count")).toInt();
    const QString scopeMode = scope.value(QStringLiteral("scope_mode")).toString();
    if (scopeMode == QStringLiteral("goal_focused") && availableMapCount > currentScopeUnits.size()) {
        ui->scopeCountLabel->setText(
            QStringLiteral("历史聚焦 %1/%2 章节").arg(currentScopeUnits.size()).arg(availableMapCount));
        const QString notice = scope.value(QStringLiteral("scope_notice")).toString();
        ui->scopeHint->setText(
            notice.isEmpty()
                ? QStringLiteral("本次只分析聚焦章节，不代表对整个资料库的逐章审计。")
                : notice);
    } else if (scopeMode == QStringLiteral("selected_documents")) {
        ui->scopeCountLabel->setText(
            QStringLiteral("对照 %1 章节 · %2 份资料").arg(currentScopeUnits.size()).arg(documentCount));
        ui->scopeHint->setText(scope.value(QStringLiteral("scope_notice")).toString());
    } else {
        ui->scopeCountLabel->setText(
            QStringLiteral("%1 个章节 · %2 份资料").arg(currentScopeUnits.size()).arg(documentCount));
        ui->scopeHint->setText(
            QStringLiteral("本次覆盖当前活动索引的全部 %1 份资料；范围只显示文件、章节和定位信息。")
                .arg(availableDocumentCount > 0 ? availableDocumentCount : documentCount));
    }
    if (currentScopeUnits.isEmpty()) {
        ui->scopeDetailBrowser->setMarkdown(QStringLiteral("本次任务没有返回可展示的冻结范围。"));
        return;
    }
    ui->scopeList->setCurrentRow(0);
}

void KnowledgeDeepTaskDialog::showSelectedScopeUnit()
{
    const int row = ui->scopeList->currentRow();
    if (row < 0 || row >= currentScopeUnits.size()) {
        return;
    }
    ui->scopeDetailBrowser->setMarkdown(scopeUnitMarkdown(currentScopeUnits.at(row)));
}

void KnowledgeDeepTaskDialog::updateExecutionState(const QString &message, const QString &state)
{
    ui->stageLabel->setText(message);
    QString color = QStringLiteral("#315A8E");
    QString background = QStringLiteral("#EEF4FC");
    if (state == QStringLiteral("completed")) {
        color = QStringLiteral("#067647");
        background = QStringLiteral("#ECFDF3");
    } else if (state == QStringLiteral("warning")) {
        color = QStringLiteral("#A15C07");
        background = QStringLiteral("#FFF7E8");
    } else if (state == QStringLiteral("failed")) {
        color = QStringLiteral("#B42318");
        background = QStringLiteral("#FFF0EE");
    }
    ui->stageLabel->setStyleSheet(
        QStringLiteral("background:%1; color:%2; border-radius:7px; padding:8px 10px;").arg(background, color));
}

void KnowledgeDeepTaskDialog::updateControlState(const QString &status, bool reportAvailable)
{
    const bool active = status == QStringLiteral("queued") || status == QStringLiteral("pending")
                        || status == QStringLiteral("running");
    const bool resumable = status == QStringLiteral("paused") || status == QStringLiteral("blocked");
    const bool cancellable = active || resumable;
    ui->startDeepTaskButton->setEnabled(!existingTaskMode && !taskRunning);
    ui->pauseButton->setEnabled(active && !currentTaskId.isEmpty());
    ui->resumeButton->setEnabled(resumable && !currentTaskId.isEmpty());
    ui->cancelButton->setEnabled(cancellable && !currentTaskId.isEmpty());
    ui->exportReportButton->setEnabled(reportAvailable && !taskRunning && !currentTaskId.isEmpty());
    ui->pauseButton->setText(QStringLiteral("暂停分析"));
    ui->cancelButton->setText(QStringLiteral("结束任务"));
    if (status == QStringLiteral("blocked")) {
        ui->resumeButton->setText(QStringLiteral("继续并重试"));
        ui->resumeButton->setToolTip(QStringLiteral("从已保存检查点重试失败章节；已完成章节不会重复调用模型"));
    } else {
        ui->resumeButton->setText(QStringLiteral("继续分析"));
        ui->resumeButton->setToolTip(QStringLiteral("从已保存检查点继续，不重复执行已完成章节"));
    }
    ui->statusBadge->setText(statusText(status));
    ui->statusBadge->setProperty("state", status);
    ui->statusBadge->style()->unpolish(ui->statusBadge);
    ui->statusBadge->style()->polish(ui->statusBadge);
}

void KnowledgeDeepTaskDialog::setInspectorVisible(bool visible)
{
    inspectorVisible = visible;
    ui->scopePanel->setVisible(visible);
    ui->scopeToggleButton->setText(visible ? QStringLiteral("隐藏范围") : QStringLiteral("显示范围"));
    if (visible) {
        ui->resultSplitter->setSizes({760, 310});
    }
}

void KnowledgeDeepTaskDialog::updateTaskKindUi()
{
    const bool comparison = taskKind() == QStringLiteral("comparison");
    ui->comparisonSelectionFrame->setVisible(comparison);
    ui->comparisonDocumentList->setEnabled(comparison && !taskRunning);
    if (comparison) {
        ui->taskGoalEdit->setPlaceholderText(
            QStringLiteral("例如：从交付范围、职责、时间节点和待确认事项逐项对照，并给出差异结论。"));
        ui->requestHint->setText(QStringLiteral("先选择要比较的资料；列顺序会写入冻结范围和最终对照表。"));
    } else {
        ui->taskGoalEdit->setPlaceholderText(
            QStringLiteral("例如：总结全部资料的关键结论、约束、待办与需要确认的问题。"));
        ui->requestHint->setText(QStringLiteral("将覆盖当前活动索引的全部资料；开始后资料更新不会混入本次任务。"));
    }
}

void KnowledgeDeepTaskDialog::confirmReportExport()
{
    if (currentTaskId.isEmpty() || !ui->exportReportButton->isEnabled()) {
        return;
    }
    QMessageBox confirmation(
        QMessageBox::Question,
        QStringLiteral("确认导出正式报告"),
        QStringLiteral("将基于本次已完成的冻结范围创建新的 Markdown 正式报告，并写入任务历史。\n\n"
                       "导出不会重新读取资料、重新检索或调用模型。"),
        QMessageBox::NoButton,
        this);
    QPushButton *confirmButton = confirmation.addButton(QStringLiteral("确认导出"), QMessageBox::AcceptRole);
    confirmation.addButton(QStringLiteral("取消"), QMessageBox::RejectRole);
    confirmation.exec();
    if (confirmation.clickedButton() != confirmButton) {
        return;
    }
    ui->exportReportButton->setEnabled(false);
    updateExecutionState(QStringLiteral("正在创建受控正式报告；不会重新调用模型。"));
    backendClient->exportKnowledgeDeepTaskReport(currentTaskId, QString());
}

QString KnowledgeDeepTaskDialog::taskKind() const
{
    return ui->taskKindComboBox->currentData().toString();
}

QStringList KnowledgeDeepTaskDialog::selectedComparisonDocumentIds() const
{
    QStringList documentIds;
    // 选择顺序不依赖 Qt 对 selectedItems() 的返回顺序；最终报告始终按客户看到的列表顺序排列。
    for (int row = 0; row < ui->comparisonDocumentList->count(); ++row) {
        QListWidgetItem *item = ui->comparisonDocumentList->item(row);
        if (item == nullptr || !item->isSelected()) {
            continue;
        }
        const QString documentId = item->data(Qt::UserRole).toString().trimmed();
        if (!documentId.isEmpty()) {
            documentIds.append(documentId);
        }
    }
    return documentIds;
}

QString KnowledgeDeepTaskDialog::scopeUnitLabel(const QJsonObject &unit) const
{
    const QString document = unit.value(QStringLiteral("document_name")).toString();
    const QStringList headings = [&unit]() {
        QStringList values;
        for (const QJsonValue &value : unit.value(QStringLiteral("heading_path")).toArray()) {
            if (value.isString()) {
                values.append(value.toString());
            }
        }
        return values;
    }();
    const QString heading = headings.isEmpty() ? QStringLiteral("未标记章节") : headings.join(QStringLiteral(" / "));
    return QStringLiteral("%1 · %2").arg(document, heading);
}

QString KnowledgeDeepTaskDialog::scopeUnitMarkdown(const QJsonObject &unit) const
{
    QStringList headings;
    for (const QJsonValue &value : unit.value(QStringLiteral("heading_path")).toArray()) {
        if (value.isString()) {
            headings.append(value.toString());
        }
    }
    return QStringLiteral("## %1\n\n**章节**：%2\n\n**位置**：%3\n\n**本次范围**：第 %4 个冻结章节\n\n"
                          "此检查器只展示范围元数据，不展示原始章节正文。")
        .arg(markdownEscaped(unit.value(QStringLiteral("document_name")).toString()),
             markdownEscaped(headings.isEmpty() ? QStringLiteral("未标记章节") : headings.join(QStringLiteral(" / "))),
             markdownEscaped(sourceLocatorText(unit.value(QStringLiteral("source")).toObject())),
             QString::number(unit.value(QStringLiteral("parent_ordinal")).toInt()));
}

QString KnowledgeDeepTaskDialog::mapUnitLabel(const QString &mapUnitId) const
{
    for (const QJsonObject &unit : currentScopeUnits) {
        if (unit.value(QStringLiteral("map_unit_id")).toString() == mapUnitId) {
            return scopeUnitLabel(unit);
        }
    }
    return QStringLiteral("冻结章节");
}

QString KnowledgeDeepTaskDialog::resultMarkdown(const KnowledgeDeepTaskResult &result) const
{
    const QJsonObject deepResult = result.result;
    QString markdown;
    if (result.status == QStringLiteral("completed") && !deepResult.isEmpty()) {
        markdown = QStringLiteral("# 深度结论\n\n%1\n\n## 关键发现\n")
                       .arg(deepResult.value(QStringLiteral("overview")).toString());
        if (deepResult.value(QStringLiteral("task_kind")).toString() == QStringLiteral("comparison")) {
            markdown += QStringLiteral("\n## 资料对照表\n\n%1\n").arg(comparisonTableMarkdown(result));
        }
        const QJsonArray findings = deepResult.value(QStringLiteral("findings")).toArray();
        if (findings.isEmpty()) {
            markdown += QStringLiteral("- 本次没有需要单列的关键发现。\n");
        }
        for (const QJsonValue &value : findings) {
            const QJsonObject finding = value.toObject();
            QStringList sourceLabels;
            for (const QJsonValue &sourceId : finding.value(QStringLiteral("source_ids")).toArray()) {
                if (sourceId.isString()) {
                    sourceLabels.append(mapUnitLabel(sourceId.toString()));
                }
            }
            markdown += QStringLiteral("- %1\n  - 范围：%2\n")
                            .arg(finding.value(QStringLiteral("statement")).toString(), sourceLabels.join(QStringLiteral("；")));
        }
        const QJsonArray conflicts = deepResult.value(QStringLiteral("conflicts")).toArray();
        if (!conflicts.isEmpty()) {
            markdown += QStringLiteral("\n## 需要确认的差异\n");
            for (const QJsonValue &value : conflicts) {
                const QJsonObject conflict = value.toObject();
                QStringList sourceLabels;
                for (const QJsonValue &sourceId : conflict.value(QStringLiteral("source_ids")).toArray()) {
                    if (sourceId.isString()) {
                        sourceLabels.append(mapUnitLabel(sourceId.toString()));
                    }
                }
                markdown += QStringLiteral("- **%1**：%2\n  - 涉及范围：%3\n")
                                .arg(conflict.value(QStringLiteral("topic")).toString(),
                                     conflict.value(QStringLiteral("description")).toString(),
                                     sourceLabels.join(QStringLiteral("；")));
            }
        }
        const QString warnings = bulletList(deepResult.value(QStringLiteral("warnings")).toArray());
        if (!warnings.isEmpty()) {
            markdown += QStringLiteral("\n## 注意事项\n%1").arg(warnings);
        }
    } else {
        markdown = QStringLiteral("# 当前进度\n\n%1\n\n"
                                  "以下内容仅是已安全写入检查点的章节小结，并非完整正式报告。\n")
                       .arg(result.summary.isEmpty() ? QStringLiteral("任务尚未产生可展示的完整结论。") : result.summary);
        const QJsonArray mapResults = result.coverage.value(QStringLiteral("completed_map_results")).toArray();
        if (!mapResults.isEmpty()) {
            markdown += QStringLiteral("\n## 已完成章节\n");
            for (const QJsonValue &value : mapResults) {
                const QJsonObject mapResult = value.toObject();
                markdown += QStringLiteral("### %1\n\n%2\n\n")
                                .arg(markdownEscaped(mapUnitLabel(mapResult.value(QStringLiteral("map_unit_id")).toString())),
                                     mapResult.value(QStringLiteral("summary")).toString());
            }
        }
    }
    markdown += QStringLiteral("\n---\n\n%1").arg(coverageMarkdown(result));
    return markdown;
}

QString KnowledgeDeepTaskDialog::comparisonTableMarkdown(const KnowledgeDeepTaskResult &result) const
{
    const QJsonObject scope = result.scope;
    const QJsonArray selectedDocumentIds = scope.value(QStringLiteral("selected_document_ids")).toArray();
    QStringList documentNames;
    for (const QJsonValue &documentIdValue : selectedDocumentIds) {
        const QString documentId = documentIdValue.toString();
        QString name = QStringLiteral("已选资料");
        for (const QJsonObject &unit : currentScopeUnits) {
            if (unit.value(QStringLiteral("document_id")).toString() == documentId) {
                name = unit.value(QStringLiteral("document_name")).toString();
                break;
            }
        }
        documentNames.append(markdownTableCell(name));
    }
    if (documentNames.size() < 2) {
        return QStringLiteral("当前对照范围尚未返回至少两份资料。\n");
    }
    QString markdown = QStringLiteral("| 对照维度 | %1 | 结论 |\n")
                           .arg(documentNames.join(QStringLiteral(" | ")));
    QStringList dividers{QStringLiteral("---")};
    for (int index = 0; index < documentNames.size(); ++index) {
        dividers.append(QStringLiteral("---"));
    }
    dividers.append(QStringLiteral("---"));
    markdown += QStringLiteral("| %1 |\n").arg(dividers.join(QStringLiteral(" | ")));
    const QJsonArray rows = result.result.value(QStringLiteral("comparison_rows")).toArray();
    if (rows.isEmpty()) {
        return markdown + QStringLiteral("| 当前未形成可对照行 | - | - |\n");
    }
    for (const QJsonValue &value : rows) {
        const QJsonObject row = value.toObject();
        QStringList cells{markdownTableCell(row.value(QStringLiteral("dimension")).toString())};
        const QJsonArray values = row.value(QStringLiteral("values")).toArray();
        for (int index = 0; index < documentNames.size(); ++index) {
            cells.append(markdownTableCell(index < values.size() ? values.at(index).toString()
                                                                  : QStringLiteral("当前汇总未明确说明")));
        }
        cells.append(markdownTableCell(row.value(QStringLiteral("conclusion")).toString()));
        markdown += QStringLiteral("| %1 |\n").arg(cells.join(QStringLiteral(" | ")));
    }
    return markdown;
}

QString KnowledgeDeepTaskDialog::coverageMarkdown(const KnowledgeDeepTaskResult &result) const
{
    const QJsonObject coverage = result.coverage;
    const QJsonObject readiness = result.reportReadiness;
    QString markdown = QStringLiteral("## 覆盖与交付状态\n\n- 当前状态：%1\n- Map：%2 / %3 个章节已完成\n- Reduce：%4 / %5 个节点已完成\n")
                           .arg(statusText(result.status),
                                QString::number(coverage.value(QStringLiteral("completed_map_unit_ids")).toArray().size()),
                                QString::number(coverage.value(QStringLiteral("total_map_count")).toInt()),
                                QString::number(coverage.value(QStringLiteral("completed_reduce_count")).toInt()),
                                QString::number(coverage.value(QStringLiteral("total_reduce_count")).toInt()));
    const QJsonObject scope = result.scope;
    if (scope.value(QStringLiteral("scope_mode")).toString() == QStringLiteral("goal_focused")) {
        const int selected = coverage.value(QStringLiteral("total_map_count")).toInt();
        const int available = scope.value(QStringLiteral("available_map_count")).toInt();
        markdown += QStringLiteral("- 分析范围：目标聚焦 %1/%2 个章节，不代表整库逐章审计\n")
                        .arg(selected)
                        .arg(available > 0 ? available : selected);
        const QString notice = scope.value(QStringLiteral("scope_notice")).toString();
        if (!notice.isEmpty()) {
            markdown += QStringLiteral("\n> 覆盖边界：%1\n").arg(markdownEscaped(notice));
        }
    }
    const QJsonArray missing = readiness.value(QStringLiteral("missing_map_unit_ids")).toArray();
    if (!missing.isEmpty()) {
        markdown += QStringLiteral("- 尚未纳入正式结论：%1 个冻结章节\n").arg(missing.size());
    }
    const QString readinessMessage = readiness.value(QStringLiteral("message")).toString();
    if (!readinessMessage.isEmpty()) {
        markdown += QStringLiteral("\n**正式报告**：%1\n").arg(readinessMessage);
    }
    const QString warnings = bulletList(coverage.value(QStringLiteral("warnings")).toArray())
                             + bulletList(readiness.value(QStringLiteral("warnings")).toArray());
    if (!warnings.isEmpty()) {
        markdown += QStringLiteral("\n### 需要留意\n%1").arg(warnings);
    }
    return markdown;
}
