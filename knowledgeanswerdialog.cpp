#include "knowledgeanswerdialog.h"

#include "taskactivityindicator.h"
#include "ui_knowledgeanswerdialog.h"

#include <QIcon>
#include <QJsonArray>
#include <QJsonObject>
#include <QListWidgetItem>
#include <QPushButton>
#include <QTextBrowser>
#include <QTimer>

namespace {

QString markdownEscaped(const QString &value)
{
    // 回答正文已由后端来源契约核验；这里仍把 UI 拼接的资料库名称转义，避免名称中的 Markdown
    // 标记影响阅读层级。模型输出则交给 QTextBrowser 原生 Markdown 渲染。
    return value.toHtmlEscaped();
}

QString sourceLocatorText(const QJsonObject &source)
{
    const QJsonObject anchor = source.value(QStringLiteral("source")).toObject();
    const QString kind = anchor.value(QStringLiteral("source_kind")).toString();
    const QString locator = anchor.value(QStringLiteral("source_locator")).toString();
    if (kind == QStringLiteral("page")) {
        return QStringLiteral("第 %1 页").arg(locator);
    }
    if (kind == QStringLiteral("line")) {
        return QStringLiteral("第 %1 行").arg(locator);
    }
    if (kind == QStringLiteral("paragraph")) {
        return QStringLiteral("第 %1 段").arg(locator);
    }
    return locator.isEmpty() ? QStringLiteral("可定位片段") : locator;
}

QString evidenceStateText(const QString &state)
{
    if (state == QStringLiteral("sufficient")) {
        return QStringLiteral("来源充分");
    }
    if (state == QStringLiteral("partial")) {
        return QStringLiteral("部分覆盖");
    }
    return QStringLiteral("资料不足");
}

} // namespace

KnowledgeAnswerDialog::KnowledgeAnswerDialog(
    BackendClient *backendClient,
    const QString &knowledgeBaseId,
    const QString &knowledgeBaseName,
    QWidget *parent)
    : QDialog(parent)
    , ui(new Ui::KnowledgeAnswerDialog)
    , backendClient(backendClient)
    , pollTimer(new QTimer(this))
    , knowledgeBaseId(knowledgeBaseId)
{
    ui->setupUi(this);
    // 任务指示器只由真实运行状态驱动，客户不必再从不断变化的阶段文本猜测任务是否结束。
    activityIndicator = new TaskActivityIndicator(this);
    ui->resultHeaderLayout->insertWidget(2, activityIndicator);
    ui->knowledgeIcon->setPixmap(QIcon(QStringLiteral(":/icons/knowledge.svg")).pixmap(34, 34));
    ui->knowledgeBaseLabel->setText(knowledgeBaseName.isEmpty() ? QStringLiteral("当前资料库") : knowledgeBaseName);
    ui->questionEdit->setFocus();
    ui->resultSplitter->setStretchFactor(0, 8);
    ui->resultSplitter->setStretchFactor(1, 3);
    ui->resultSplitter->setSizes({760, 310});
    pollTimer->setInterval(850);

    connect(ui->askButton, &QPushButton::clicked, this, &KnowledgeAnswerDialog::startAnswer);
    connect(ui->sourceToggleButton, &QPushButton::clicked, this, [this]() {
        setInspectorVisible(!inspectorVisible);
    });
    connect(ui->sourceList, &QListWidget::currentRowChanged, this, [this](int) { showSelectedSource(); });
    connect(ui->historyButton, &QPushButton::clicked, this, [this]() {
        if (!currentTaskId.isEmpty()) {
            emit openTaskHistoryRequested(currentTaskId);
        }
    });
    connect(ui->closeButton, &QPushButton::clicked, this, &QDialog::reject);
    connect(pollTimer, &QTimer::timeout, this, &KnowledgeAnswerDialog::requestLatestResult);

    connect(backendClient, &BackendClient::knowledgeAnswerStarted, this,
            [this](const KnowledgeAnswerTaskStartResult &result) {
                if (!running || result.taskId.isEmpty()) {
                    return;
                }
                currentTaskId = result.taskId;
                updateExecutionState(QStringLiteral("任务已受理，正在连接实时阶段。"));
                // 回调运行时使用对象持有的客户端，避免依赖构造参数的 lambda 捕获生命周期。
                this->backendClient->connectTaskLog(currentTaskId);
                pollTimer->start();
                requestLatestResult();
            });
    connect(backendClient, &BackendClient::knowledgeAnswerStillRunning, this,
            [this](const QString &taskId, const QString &) {
                if (running && taskId == currentTaskId) {
                    updateExecutionState(QStringLiteral("正在继续处理已选择资料，请稍候。"));
                }
            });
    connect(backendClient, &BackendClient::knowledgeAnswerCompleted, this,
            [this](const KnowledgeAnswerTaskResult &result) {
                if (result.taskId == currentTaskId) {
                    handleAnswerResult(result);
                }
            });
    connect(backendClient, &BackendClient::knowledgeAnswerFailed, this,
            [this](const QString &message) {
                if (running) {
                    handleAnswerFailure(message);
                }
            });
    connect(backendClient, &BackendClient::taskLogReceived, this, [this](const TaskLogEvent &event) {
        if (running && event.taskId == currentTaskId && !event.message.trimmed().isEmpty()) {
            updateExecutionState(event.message, event.level == QStringLiteral("error") ? QStringLiteral("failed") : event.level);
        }
    });
    connect(backendClient, &BackendClient::taskLogFinished, this, [this](const QString &taskId) {
        if (running && taskId == currentTaskId) {
            requestLatestResult();
        }
    });
}

KnowledgeAnswerDialog::~KnowledgeAnswerDialog()
{
    delete ui;
}

void KnowledgeAnswerDialog::startAnswer()
{
    const QString question = ui->questionEdit->toPlainText().trimmed();
    if (question.size() < 2) {
        updateExecutionState(QStringLiteral("请写出一个具体问题，例如“这份方案的验收要求是什么？”"), QStringLiteral("warning"));
        ui->questionEdit->setFocus();
        return;
    }

    currentTaskId.clear();
    currentSources.clear();
    ui->sourceList->clear();
    ui->sourceDetail->setMarkdown(QStringLiteral("选择一条来源后，可查看其文件、位置与短片段。"));
    ui->answerBrowser->setMarkdown(QStringLiteral("正在准备受控检索。答案只会在来源和活动版本核验通过后显示。"));
    running = true;
    activityIndicator->setRunning(true);
    pollTimer->stop();
    ui->askButton->setEnabled(false);
    ui->questionEdit->setEnabled(false);
    ui->historyButton->setEnabled(false);
    updateExecutionState(QStringLiteral("正在提交问题。"));
    backendClient->startKnowledgeAnswer(knowledgeBaseId, question);
}

void KnowledgeAnswerDialog::requestLatestResult()
{
    if (!currentTaskId.isEmpty() && running) {
        backendClient->requestKnowledgeAnswerResult(currentTaskId);
    }
}

void KnowledgeAnswerDialog::handleAnswerResult(const KnowledgeAnswerTaskResult &result)
{
    running = false;
    activityIndicator->setRunning(false);
    pollTimer->stop();
    ui->askButton->setEnabled(true);
    ui->questionEdit->setEnabled(true);
    ui->historyButton->setEnabled(!result.taskId.isEmpty());
    ui->answerBrowser->setMarkdown(resultMarkdown(result));

    const QJsonObject evidenceGate = result.result.value(QStringLiteral("evidence_gate")).toObject();
    showSources(evidenceGate.value(QStringLiteral("sources")).toArray());
    const QString evidenceState = evidenceGate.value(QStringLiteral("evidence_state")).toString();
    ui->evidenceBadge->setText(evidenceStateText(evidenceState));
    const bool completed = result.status == QStringLiteral("completed");
    updateExecutionState(
        result.summary.isEmpty() ? result.message : result.summary,
        completed ? QStringLiteral("completed") : (result.status == QStringLiteral("blocked") ? QStringLiteral("warning") : QStringLiteral("failed")));
}

void KnowledgeAnswerDialog::handleAnswerFailure(const QString &message)
{
    running = false;
    activityIndicator->setRunning(false);
    pollTimer->stop();
    ui->askButton->setEnabled(true);
    ui->questionEdit->setEnabled(true);
    ui->answerBrowser->setMarkdown(
        QStringLiteral("# 本次未完成\n\n%1\n\n资料没有被修改。可检查模型配置、稍后重试，或在任务历史查看阶段记录。")
            .arg(message));
    updateExecutionState(message, QStringLiteral("failed"));
}

void KnowledgeAnswerDialog::showSources(const QJsonArray &sources)
{
    currentSources.clear();
    ui->sourceList->clear();
    for (const QJsonValue &value : sources) {
        if (!value.isObject()) {
            continue;
        }
        const QJsonObject source = value.toObject();
        const QString name = source.value(QStringLiteral("document_name")).toString();
        const QString label = QStringLiteral("%1 · %2").arg(name, sourceLocatorText(source));
        auto *item = new QListWidgetItem(label, ui->sourceList);
        item->setToolTip(label);
        currentSources.append(source);
    }
    ui->sourceCountLabel->setText(QStringLiteral("%1 条").arg(currentSources.size()));
    if (currentSources.isEmpty()) {
        ui->sourceDetail->setMarkdown(QStringLiteral("本次没有可展示的已核验来源。"));
        return;
    }
    ui->sourceList->setCurrentRow(0);
}

void KnowledgeAnswerDialog::showSelectedSource()
{
    const int row = ui->sourceList->currentRow();
    if (row < 0 || row >= currentSources.size()) {
        return;
    }
    ui->sourceDetail->setMarkdown(sourceDetailMarkdown(currentSources.at(row)));
}

void KnowledgeAnswerDialog::updateExecutionState(const QString &message, const QString &state)
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

void KnowledgeAnswerDialog::setInspectorVisible(bool visible)
{
    inspectorVisible = visible;
    ui->sourcePanel->setVisible(visible);
    ui->sourceToggleButton->setText(visible ? QStringLiteral("隐藏来源") : QStringLiteral("显示来源"));
    if (visible) {
        ui->resultSplitter->setSizes({760, 310});
    }
}

QString KnowledgeAnswerDialog::resultMarkdown(const KnowledgeAnswerTaskResult &result) const
{
    const QJsonObject answerResult = result.result;
    const QString resultStatus = answerResult.value(QStringLiteral("status")).toString();
    const QJsonObject answer = answerResult.value(QStringLiteral("answer")).toObject();
    const QJsonObject gate = answerResult.value(QStringLiteral("evidence_gate")).toObject();
    const QJsonObject diagnostics = answerResult.value(QStringLiteral("retrieval_diagnostics")).toObject();
    const QString message = answerResult.value(QStringLiteral("message")).toString(result.message);
    QString markdown;
    if (resultStatus == QStringLiteral("completed") && !answer.isEmpty()) {
        const QString cacheState = diagnostics.value(QStringLiteral("local_cache_state")).toString();
        const int cacheAgeMs = diagnostics.value(QStringLiteral("local_cache_age_ms")).toInt(-1);
        const QString cacheText = cacheState == QStringLiteral("hit")
            ? (cacheAgeMs >= 0
                   ? QStringLiteral("已命中（约 %1 ms 前的本地检索结果）").arg(cacheAgeMs)
                   : QStringLiteral("已命中"))
            : QStringLiteral("未命中，本次已重新检索");
        markdown = QStringLiteral("# 本次结论\n\n%1\n\n---\n\n## 证据范围\n\n"
                                   "- 资料覆盖：%2\n- 检索方式：%3\n- 当前活动索引：第 %4 代\n- 本地检索缓存：%5\n")
                       .arg(answer.value(QStringLiteral("answer_markdown")).toString(),
                            evidenceStateText(gate.value(QStringLiteral("evidence_state")).toString()),
                            diagnostics.value(QStringLiteral("mode")).toString(QStringLiteral("关键词检索")),
                            QString::number(diagnostics.value(QStringLiteral("active_index_generation")).toInt()),
                            cacheText);
    } else if (result.status == QStringLiteral("blocked")) {
        markdown = QStringLiteral("# 资料不足，暂不回答\n\n%1\n\n"
                                   "系统没有把空白或不完整来源交给模型补写。补充材料、建立索引或缩小问题范围后可重新提问。")
                       .arg(message);
    } else {
        markdown = QStringLiteral("# 本次未完成\n\n%1\n\n"
                                   "系统未展示未经来源校验的模型内容。你可以稍后重试，或从任务历史查看阶段与失败原因。")
                       .arg(message);
    }
    const QJsonArray warnings = answer.value(QStringLiteral("warnings")).toArray();
    if (!warnings.isEmpty()) {
        markdown += QStringLiteral("\n\n## 需要留意\n");
        for (const QJsonValue &warning : warnings) {
            if (warning.isString()) {
                markdown += QStringLiteral("- %1\n").arg(warning.toString());
            }
        }
    }
    return markdown;
}

QString KnowledgeAnswerDialog::sourceDetailMarkdown(const QJsonObject &source) const
{
    const QJsonArray headings = source.value(QStringLiteral("heading_path")).toArray();
    QStringList headingParts;
    for (const QJsonValue &heading : headings) {
        if (heading.isString()) {
            headingParts.append(heading.toString());
        }
    }
    return QStringLiteral("## %1\n\n**位置**：%2\n\n**章节**：%3\n\n### 片段\n\n%4")
        .arg(markdownEscaped(source.value(QStringLiteral("document_name")).toString()),
             markdownEscaped(sourceLocatorText(source)),
             markdownEscaped(headingParts.isEmpty() ? QStringLiteral("未标记章节") : headingParts.join(QStringLiteral(" / "))),
             markdownEscaped(source.value(QStringLiteral("excerpt")).toString()));
}
