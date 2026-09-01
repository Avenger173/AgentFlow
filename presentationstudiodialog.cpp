#include "presentationstudiodialog.h"

#include "ui_presentationstudiodialog.h"

#include <QDialogButtonBox>
#include <QComboBox>
#include <QIcon>
#include <QLabel>
#include <QMessageBox>
#include <QRegularExpression>
#include <QStyle>
#include <QTextBrowser>
#include <QTimer>

namespace {

QString escapeHtml(const QString &value)
{
    return value.toHtmlEscaped().replace(QStringLiteral("\n"), QStringLiteral("<br>"));
}

QString themeLabel(const QString &theme)
{
    if (theme == QStringLiteral("technology_emerald")) {
        return QStringLiteral("技术洞察");
    }
    if (theme == QStringLiteral("narrative_warm")) {
        return QStringLiteral("叙事展示");
    }
    if (theme == QStringLiteral("impact_contrast")) {
        return QStringLiteral("强调对比");
    }
    return QStringLiteral("商务汇报");
}

QString layoutLabel(const QString &layout)
{
    if (layout == QStringLiteral("comparison")) {
        return QStringLiteral("对比表达");
    }
    if (layout == QStringLiteral("process")) {
        return QStringLiteral("流程拆解");
    }
    if (layout == QStringLiteral("timeline")) {
        return QStringLiteral("叙事时间线");
    }
    if (layout == QStringLiteral("metrics")) {
        return QStringLiteral("关键点矩阵");
    }
    if (layout == QStringLiteral("quote")) {
        return QStringLiteral("观点强调");
    }
    if (layout == QStringLiteral("image_statement")) {
        return QStringLiteral("图文陈述");
    }
    if (layout == QStringLiteral("agenda")) {
        return QStringLiteral("目录导航");
    }
    if (layout == QStringLiteral("summary")) {
        return QStringLiteral("行动收束");
    }
    if (layout == QStringLiteral("sources")) {
        return QStringLiteral("事实边界");
    }
    return QStringLiteral("信息卡片");
}

} // namespace

PresentationStudioDialog::PresentationStudioDialog(BackendClient *backendClient, QWidget *parent)
    : QDialog(parent)
    , ui(new Ui::PresentationStudioDialog)
    , backendClient(backendClient)
    , pollTimer(new QTimer(this))
{
    ui->setupUi(this);
    // Designer 负责控件层级和尺寸；Qt 6 的 uic 不支持把 QBoxLayout stretch 作为 .ui 属性
    // 序列化，因此在创建后明确设定阅读区的可伸缩优先级。
    ui->mainLayout->setStretch(0, 0);
    ui->mainLayout->setStretch(1, 0);
    ui->mainLayout->setStretch(2, 1);
    ui->planLayout->setStretch(1, 1);
    ui->studioIcon->setPixmap(QIcon(QStringLiteral(":/icons/document.svg")).pixmap(34, 34));
    ui->intentEdit->setFocus();
    pollTimer->setInterval(900);

    // .ui 保留输入区域的固定骨架；Provider 清单属于可扩展运行时配置，因此在资料勾选框前
    // 放入“配图来源”控件。内置主题和版式始终生效，这个选择只决定是否再加入图片。
    visualAssetProviderLabel = new QLabel(QStringLiteral("配图来源"), this);
    visualAssetProviderLabel->setObjectName(QStringLiteral("visualAssetProviderLabel"));
    visualAssetProviderCombo = new QComboBox(this);
    visualAssetProviderCombo->setObjectName(QStringLiteral("visualAssetProviderCombo"));
    visualAssetProviderCombo->setMinimumWidth(196);
    visualAssetProviderCombo->addItem(QStringLiteral("不添加额外配图"), QStringLiteral("none"));
    visualAssetProviderCombo->addItem(QStringLiteral("Pexels 摄影图片"), QStringLiteral("pexels"));
    visualAssetProviderCombo->addItem(QStringLiteral("Seedream AI 配图"), QStringLiteral("seedream"));
    // 用户已完成真实生成验收；默认使用 AI 配图，但最终导出仍需在确认框中明确同意本次调用。
    visualAssetProviderCombo->setCurrentIndex(2);
    visualAssetProviderCombo->setToolTip(
        QStringLiteral("内置主题和页面版式始终有效。计划阶段不联网；确认导出后才会调用所选图片来源。Seedream 最多生成 4 张无文字水印的图片。"));
    const int providerIndex = ui->inputActionLayout->indexOf(ui->licensedAssetsCheckBox);
    ui->inputActionLayout->insertWidget(qMax(0, providerIndex), visualAssetProviderLabel);
    ui->inputActionLayout->insertWidget(qMax(0, providerIndex + 1), visualAssetProviderCombo);
    ui->inputHint->setText(
        QStringLiteral("AI 自动判断主题、页数和版式；智能补充只在确认导出后读取固定来源，不会编造事实。"));
    // 复用 Designer 中已有的稳定开关，避免输入区在窄窗口继续横向堆叠；它现在统一控制
    // 公开资料参考与数据图表共用客户可见开关；数据型主题会先建立数量合同，导出后默认
    // 由已配置模型直接生成可编辑图表，联网核验属于后续明确选择的扩展路径。
    ui->licensedAssetsCheckBox->setText(QStringLiteral("导出时智能补充资料与数据"));
    ui->licensedAssetsCheckBox->setToolTip(
        QStringLiteral("计划阶段不联网。确认导出后，数据主题会按已确认计划由模型生成可编辑表格与图表；公开资料仅在明确启用联网核验时读取。"));

    connect(ui->planButton, &QPushButton::clicked, this, &PresentationStudioDialog::startPlanning);
    const auto invalidatePlan = [this](const QString &changedLabel) {
        if (currentPlanId.isEmpty() || planning || exporting) {
            return;
        }
        // 图片与智能补充都是计划身份的一部分。更改后不复用旧 plan_id，防止 UI 选择与实际导出脱节。
        currentPlanId.clear();
        currentPlan = PresentationStudioPlanResult{};
        ui->exportButton->setEnabled(false);
        ui->filenameEdit->setEnabled(false);
        ui->planPreview->setHtml(
            QStringLiteral("<p class='studio-muted'>%1已变更，请重新生成创作计划后再导出。</p>").arg(changedLabel));
        ui->statusLabel->setText(QStringLiteral("%1已更改，当前计划不再可导出。").arg(changedLabel));
    };
    connect(visualAssetProviderCombo, &QComboBox::currentIndexChanged, this, [invalidatePlan](int) {
        invalidatePlan(QStringLiteral("配图来源"));
    });
    connect(ui->licensedAssetsCheckBox, &QCheckBox::checkStateChanged, this, [invalidatePlan](Qt::CheckState) {
        invalidatePlan(QStringLiteral("智能资料与数据补充"));
    });
    connect(ui->exportButton, &QPushButton::clicked, this, &PresentationStudioDialog::exportPresentation);
    connect(ui->historyButton, &QPushButton::clicked, this, [this]() {
        if (!currentTaskId.isEmpty()) {
            emit openTaskHistoryRequested(currentTaskId);
        }
    });
    connect(ui->closeButton, &QPushButton::clicked, this, &QDialog::reject);
    connect(pollTimer, &QTimer::timeout, this, &PresentationStudioDialog::requestLatestPlan);

    connect(backendClient, &BackendClient::presentationStudioStarted, this,
            [this](const PresentationStudioTaskStartResult &result) {
                if (!planning) {
                    return;
                }
                currentTaskId = result.taskId;
                setPlanningState(QStringLiteral("正在生成创作简报与逐页计划…"));
                // 构造参数与成员同名；异步回调必须经 this 访问成员，避免捕获即将离开
                // 构造函数作用域的参数，也明确网络客户端的所有权仍在 MainWindow。
                this->backendClient->connectTaskLog(currentTaskId);
                pollTimer->start();
                requestLatestPlan();
            });
    connect(backendClient, &BackendClient::presentationStudioStillRunning, this,
            [this](const QString &taskId, const QString &) {
                if (taskId == currentTaskId) {
                    setPlanningState(QStringLiteral("正在生成创作简报与逐页计划…"));
                }
            });
    connect(backendClient, &BackendClient::presentationStudioPlanReceived, this,
            [this](const PresentationStudioPlanResult &result) {
                if (result.taskId == currentTaskId) {
                    handlePlanReceived(result);
                }
            });
    connect(backendClient, &BackendClient::presentationStudioFailed, this,
            [this](const QString &message) {
                if (planning) {
                    handlePlanFailed(message);
                }
            });
    connect(backendClient, &BackendClient::presentationStudioExported, this,
            [this](const PresentationExportResult &result) {
                if (result.taskId == currentTaskId) {
                    handlePresentationExported(result);
                }
            });
    connect(backendClient, &BackendClient::presentationStudioExportPrepared, this,
            [this](const QString &taskId) {
                if (!exporting || taskId != currentTaskId) {
                    return;
                }
                this->backendClient->connectTaskLog(currentTaskId);
                dispatchPresentationExport();
            });
    connect(backendClient, &BackendClient::presentationStudioExportFailed, this,
            [this](const QString &message) {
                if (exporting) {
                    handlePresentationExportFailed(message);
                }
            });
    connect(backendClient, &BackendClient::taskLogReceived, this, [this](const TaskLogEvent &event) {
        if (event.taskId != currentTaskId || (!planning && !exporting)
            || event.message.trimmed().isEmpty()) {
            return;
        }
        // 规划和导出共用同一条任务事件流。导出会经历联网检索、数据抽取、渲染与回读，
        // 因此不能只在 planning 时更新，否则客户会以为确认导出后又变成了黑盒等待。
        setPlanningState(event.message);
    });
}

PresentationStudioDialog::~PresentationStudioDialog()
{
    delete ui;
}

void PresentationStudioDialog::setInitialGoal(const QString &goal)
{
    const QString normalizedGoal = goal.trimmed();
    if (normalizedGoal.isEmpty() || planning || exporting) {
        return;
    }

    // 预填只减少客户重复输入；客户仍可在生成计划前修改主题和数据补充选项。
    ui->intentEdit->setPlainText(normalizedGoal);
    ui->intentEdit->setFocus();
    ui->intentEdit->moveCursor(QTextCursor::End);
}

void PresentationStudioDialog::startDirectGeneration(const QString &goal)
{
    const QString normalizedGoal = goal.trimmed();
    if (normalizedGoal.size() < 4 || planning || exporting) {
        return;
    }

    directGeneration = true;
    setInitialGoal(normalizedGoal);
    // 直出默认只使用内置视觉版式，但保留模型数据规划能力；这样明确的“数据 PPT”不会
    // 因没有手工勾选开关而退化成没有图表的普通文本页，也不会暗中联网下载素材。
    if (visualAssetProviderCombo) {
        visualAssetProviderCombo->setCurrentIndex(0);
    }
    ui->licensedAssetsCheckBox->setChecked(true);
    ui->licensedAssetsCheckBox->setEnabled(false);
    ui->intentEdit->setEnabled(false);
    ui->planButton->setEnabled(false);
    ui->closeButton->setText(QStringLiteral("后台继续"));
    ui->statusLabel->setText(QStringLiteral("正在准备 PPT 创作计划…"));
    QTimer::singleShot(0, this, &PresentationStudioDialog::startPlanning);
}

void PresentationStudioDialog::startPlanning()
{
    const QString intent = ui->intentEdit->toPlainText().trimmed();
    if (intent.size() < 4) {
        ui->statusLabel->setText(QStringLiteral("请用一句话说明你想制作什么演示文稿。"));
        return;
    }

    // 每次重新规划都会取得新 taskId；旧计划不再允许误导性地导出到新主题。
    currentTaskId.clear();
    currentPlanId.clear();
    currentPlan = PresentationStudioPlanResult{};
    planning = true;
    exporting = false;
    pollTimer->stop();
    ui->planButton->setEnabled(false);
    ui->exportButton->setEnabled(false);
    ui->historyButton->setEnabled(false);
    ui->filenameEdit->setEnabled(false);
    visualAssetProviderCombo->setEnabled(false);
    ui->licensedAssetsCheckBox->setEnabled(false);
    ui->planPreview->setHtml(QStringLiteral("<p class='studio-muted'>正在准备创作计划…</p>"));
    setPlanningState(QStringLiteral("正在理解主题…"));
    // 计划阶段仍不联网；开关只让后端预先固化可执行的受控资料/数据意图。
    backendClient->startPresentationStudio(
        intent,
        0,
        visualAssetProvider(),
        ui->licensedAssetsCheckBox->isChecked(),
        ui->licensedAssetsCheckBox->isChecked());
}

void PresentationStudioDialog::requestLatestPlan()
{
    if (!currentTaskId.isEmpty() && planning) {
        backendClient->requestPresentationStudioResult(currentTaskId);
    }
}

void PresentationStudioDialog::handlePlanReceived(const PresentationStudioPlanResult &result)
{
    planning = false;
    pollTimer->stop();
    currentPlan = result;
    currentPlanId = result.planId;
    ui->planButton->setEnabled(true);
    ui->exportButton->setEnabled(true);
    ui->historyButton->setEnabled(true);
    ui->filenameEdit->setEnabled(true);
    // 修改配图来源会主动使当前计划失效；用户可以立即重新生成，而不会把新选择误用到旧计划。
    visualAssetProviderCombo->setEnabled(true);
    ui->licensedAssetsCheckBox->setEnabled(true);
    visualAssetProviderCombo->setToolTip(
        QStringLiteral("内置主题和页面版式始终有效。更换图片来源后需要重新生成创作计划。"));
    ui->filenameEdit->setText(suggestedFilename());
    updatePlanPreview();
    setReadyState(
        result.warnings.isEmpty()
            ? QStringLiteral("创作计划已完成。确认内容与事实边界后即可导出 PPTX。")
            : QStringLiteral("创作计划已完成，包含需要留意的事项。请阅读后再导出。"));
    if (directGeneration) {
        // 用户已经在调度台明确提出“制作”，这里不再把同一份确认重复交给客户；
        // 外部素材仍保持关闭，文件只会写入既有受控 outputs 目录。
        QTimer::singleShot(0, this, &PresentationStudioDialog::exportPresentation);
    }
}

void PresentationStudioDialog::handlePlanFailed(const QString &message)
{
    planning = false;
    pollTimer->stop();
    ui->planButton->setEnabled(true);
    visualAssetProviderCombo->setEnabled(true);
    ui->licensedAssetsCheckBox->setEnabled(true);
    ui->statusLabel->setText(
        message.isEmpty() ? QStringLiteral("创作计划未能生成，请稍后重试。") : message);
    if (directGeneration) {
        emit directGenerationFailed(message);
    }
}

void PresentationStudioDialog::exportPresentation()
{
    const QString filename = ui->filenameEdit->text().trimmed();
    if (currentTaskId.isEmpty() || currentPlanId.isEmpty()) {
        ui->statusLabel->setText(QStringLiteral("当前没有可确认的创作计划，请先生成方案。"));
        return;
    }
    if (filename.isEmpty() || !filename.endsWith(QStringLiteral(".pptx"), Qt::CaseInsensitive)) {
        ui->statusLabel->setText(QStringLiteral("请提供以 .pptx 结尾的交付文件名。"));
        ui->filenameEdit->setFocus();
        return;
    }
    const QString planAssetProvider = currentPlan.assetPlan.value(QStringLiteral("provider")).toString();
    const bool useExternalAssets = currentPlan.assetPlan.value(QStringLiteral("state")).toString()
        == QStringLiteral("planned") && !planAssetProvider.isEmpty();
    const int assetSlotCount = currentPlan.assetPlan.value(QStringLiteral("slots")).toArray().size();
    const int assetQueryCount = currentPlan.assetPlan.value(QStringLiteral("queries")).toArray().size();
    const int assetCount = qMax(assetSlotCount, assetQueryCount);
    const bool usePublicResearch = currentPlan.researchPlan.value(QStringLiteral("state")).toString()
        == QStringLiteral("planned")
        && currentPlan.researchPlan.value(QStringLiteral("provider")).toString() == QStringLiteral("wikimedia");
    const QString dataPlanState = currentPlan.dataPlan.value(QStringLiteral("state")).toString();
    const QString dataProvider = currentPlan.dataPlan.value(QStringLiteral("provider")).toString();
    // ``planned`` 兼容旧快照。普通创作的 research_gateway 计划在确认导出后由已配置模型
    // 直接生成数据，不会再因为网页取证或来源校验阻断图表交付。
    const bool useWorldBankData = (dataPlanState == QStringLiteral("planned")
                                   || dataPlanState == QStringLiteral("provider_planned"))
        && dataProvider == QStringLiteral("world_bank");
    const bool hasResearchBlueprint = dataPlanState == QStringLiteral("research_planned")
        && dataProvider == QStringLiteral("research_gateway");
    const bool useStructuredData = useWorldBankData || hasResearchBlueprint;
    QString assetNotice = QStringLiteral("\n本次不会调用外部视觉服务，将使用内置多版式设计。\n");
    if (useExternalAssets && planAssetProvider == QStringLiteral("pexels")) {
        assetNotice = QStringLiteral("\n本次会联网检索并按逐页视觉意图嵌入最多 %1 张 Pexels 授权图片，"
                                     "并在 PPT 与任务历史保留摄影师和来源。")
                          .arg(assetCount);
    } else if (useExternalAssets && planAssetProvider == QStringLiteral("seedream")) {
        assetNotice = QStringLiteral("\n本次会调用 Seedream 图像生成服务，按逐页视觉意图最多生成 %1 张图片；"
                                     "图片不加文字水印，任务历史仍会保留模型和提示词摘要。")
                          .arg(qMin(4, assetCount));
    }
    const QString researchNotice = usePublicResearch
        ? QStringLiteral("\n本次还会联网读取固定 Wikimedia 接口的最多 %1 条公开资料参考，"
                         "只写入来源页和任务历史，不自动作为统计数据或结论。")
              .arg(currentPlan.researchPlan.value(QStringLiteral("max_sources")).toInt())
        : QString();
    QString dataNotice;
    if (useWorldBankData) {
        dataNotice = QStringLiteral("\n本次还会联网读取固定 World Bank 指标接口：%1（%2）；仅生成 1 张可编辑、"
                                    "带年份和来源的%3图，数据不足或年份不一致时会跳过，不会编造。")
                         .arg(currentPlan.dataPlan.value(QStringLiteral("indicator_name")).toString(),
                              currentPlan.dataPlan.value(QStringLiteral("indicator_code")).toString(),
                              currentPlan.dataPlan.value(QStringLiteral("chart_type")).toString() == QStringLiteral("comparison_bar")
                                  ? QStringLiteral("同年对比")
                                  : QStringLiteral("年度趋势"));
    } else if (hasResearchBlueprint) {
        const int visualCount = currentPlan.dataPlan.value(QStringLiteral("required_visual_count")).toInt();
        const QJsonArray requestedVisuals = currentPlan.dataPlan.value(QStringLiteral("requested_visuals")).toArray();
        dataNotice = QStringLiteral("\n本次会由已配置模型按创作计划直接生成可编辑数据与图表；"
                                    "默认不联网、不等待来源核验。当前需交付 %1 个数据视图（%2）。")
                         .arg(qMax(visualCount, requestedVisuals.size()))
                         .arg(requestedVisuals.size());
    }
    if (!directGeneration) {
        const auto decision = QMessageBox::question(
            this,
            QStringLiteral("确认导出 PPTX"),
            QStringLiteral("将基于当前创作计划生成新的可编辑 PPTX。\n"
                           "本次不会覆盖同名文件；数据图表会按当前计划生成，并可在 PowerPoint 中编辑。%1\n\n"
                           "是否继续？")
                .arg(assetNotice + researchNotice + dataNotice),
            QMessageBox::Cancel | QMessageBox::Yes,
            QMessageBox::Cancel);
        if (decision != QMessageBox::Yes) {
            return;
        }
    }

    exporting = true;
    ui->exportButton->setEnabled(false);
    ui->planButton->setEnabled(false);
    ui->filenameEdit->setEnabled(false);
    ui->licensedAssetsCheckBox->setEnabled(false);
    ui->statusLabel->setText(useExternalAssets && planAssetProvider == QStringLiteral("seedream")
        ? QStringLiteral("正在生成视觉素材、写入 PPTX 并回读验证…")
        : (usePublicResearch || useStructuredData)
            ? QStringLiteral("正在由模型生成数据图表、写入 PPTX 并回读验证…")
            : QStringLiteral("正在生成 PPTX、回读验证并写入任务历史…"));
    // 先创建并订阅本次导出的新事件通道，再提交真正的导出请求。否则同一 taskId 的旧计划
    // 日志流可能已经结束并抢先关闭 WebSocket，让客户错过联网研究与回读阶段。
    backendClient->preparePresentationStudioExport(currentTaskId);
}

void PresentationStudioDialog::dispatchPresentationExport()
{
    if (!exporting || currentTaskId.isEmpty() || currentPlanId.isEmpty()) {
        return;
    }
    const QString planAssetProvider = currentPlan.assetPlan.value(QStringLiteral("provider")).toString();
    const bool useExternalAssets = currentPlan.assetPlan.value(QStringLiteral("state")).toString()
        == QStringLiteral("planned") && !planAssetProvider.isEmpty();
    const bool usePublicResearch = currentPlan.researchPlan.value(QStringLiteral("state")).toString()
        == QStringLiteral("planned")
        && currentPlan.researchPlan.value(QStringLiteral("provider")).toString() == QStringLiteral("wikimedia");
    const QString dataPlanState = currentPlan.dataPlan.value(QStringLiteral("state")).toString();
    const QString dataProvider = currentPlan.dataPlan.value(QStringLiteral("provider")).toString();
    const bool useWorldBankData = (dataPlanState == QStringLiteral("planned")
                                   || dataPlanState == QStringLiteral("provider_planned"))
        && dataProvider == QStringLiteral("world_bank");
    const bool hasResearchBlueprint = dataPlanState == QStringLiteral("research_planned")
        && dataProvider == QStringLiteral("research_gateway");
    const bool useStructuredData = useWorldBankData || hasResearchBlueprint;
    backendClient->exportPresentationStudio(
        currentTaskId,
        currentPlanId,
        ui->filenameEdit->text().trimmed(),
        useExternalAssets,
        usePublicResearch,
        useStructuredData,
        // 数据直出只使用已配置模型，不读网页；只有图片、公开资料或固定联网 Provider 才需要联网确认。
        useExternalAssets || usePublicResearch || useWorldBankData);
}

void PresentationStudioDialog::handlePresentationExported(const PresentationExportResult &result)
{
    exporting = false;
    ui->planButton->setEnabled(true);
    ui->historyButton->setEnabled(true);
    ui->licensedAssetsCheckBox->setEnabled(true);
    ui->statusLabel->setText(
        result.message.isEmpty()
            ? QStringLiteral("已导出 %1 页 PPTX，并通过回读验证。可在任务历史打开交付物。").arg(result.slideCount)
            : result.message);
    if (directGeneration) {
        emit directGenerationCompleted(result);
    }
}

void PresentationStudioDialog::handlePresentationExportFailed(const QString &message)
{
    exporting = false;
    ui->planButton->setEnabled(true);
    ui->exportButton->setEnabled(true);
    ui->filenameEdit->setEnabled(true);
    ui->licensedAssetsCheckBox->setEnabled(true);
    ui->statusLabel->setText(
        message.isEmpty() ? QStringLiteral("PPTX 导出失败，请修改后重试。") : message);
    if (directGeneration) {
        emit directGenerationFailed(message);
    }
}

QString PresentationStudioDialog::visualAssetProvider() const
{
    return visualAssetProviderCombo
        ? visualAssetProviderCombo->currentData().toString()
        : QStringLiteral("none");
}

void PresentationStudioDialog::setPlanningState(const QString &message)
{
    ui->statusLabel->setProperty("class", QStringLiteral("studio-status-running"));
    ui->statusLabel->setText(message);
    if (directGeneration) {
        emit directGenerationProgress(message);
    }
}

void PresentationStudioDialog::setReadyState(const QString &message)
{
    ui->statusLabel->setProperty("class", QStringLiteral("studio-status-ready"));
    ui->statusLabel->setText(message);
    if (directGeneration) {
        emit directGenerationProgress(message);
    }
}

void PresentationStudioDialog::updatePlanPreview()
{
    ui->planPreview->setHtml(formatPlanHtml());
}

QString PresentationStudioDialog::formatPlanHtml() const
{
    if (currentPlan.planId.isEmpty()) {
        return QString();
    }
    const QString title = escapeHtml(currentPlan.brief.value(QStringLiteral("title")).toString());
    const QString purpose = escapeHtml(currentPlan.brief.value(QStringLiteral("purpose")).toString());
    const QString audience = escapeHtml(currentPlan.brief.value(QStringLiteral("audience")).toString());
    const QString coreMessage = escapeHtml(currentPlan.brief.value(QStringLiteral("core_message")).toString());
    const QString theme = themeLabel(currentPlan.brief.value(QStringLiteral("theme")).toString());
    const QString themeReason = escapeHtml(currentPlan.brief.value(QStringLiteral("theme_reason")).toString());
    const QString factNotice = escapeHtml(currentPlan.brief.value(QStringLiteral("fact_check_notice")).toString());
    const QString assetNotice = escapeHtml(currentPlan.assetPlan.value(QStringLiteral("notice")).toString());
    const QString researchNotice = escapeHtml(currentPlan.researchPlan.value(QStringLiteral("notice")).toString());
    const QString dataNotice = escapeHtml(currentPlan.dataPlan.value(QStringLiteral("notice")).toString());
    QStringList blocks;
    blocks.append(QStringLiteral("<h2>%1</h2><p><b>目的</b>：%2<br><b>受众</b>：%3<br><b>核心信息</b>：%4</p>"
                                      "<p class='studio-chip'>视觉方向：%5</p><p>%6</p>")
                      .arg(title, purpose, audience, coreMessage, escapeHtml(theme), themeReason));
    blocks.append(QStringLiteral("<h3>逐页计划</h3>"));
    for (int index = 0; index < currentPlan.slides.size(); ++index) {
        const QJsonObject slide = currentPlan.slides.at(index).toObject();
        const QString slideTitle = escapeHtml(slide.value(QStringLiteral("title")).toString());
        const QString layout = escapeHtml(layoutLabel(slide.value(QStringLiteral("layout")).toString()));
        const QJsonArray bullets = slide.value(QStringLiteral("bullets")).toArray();
        QStringList items;
        for (const QJsonValue &value : bullets) {
            items.append(QStringLiteral("<li>%1</li>").arg(escapeHtml(value.toString())));
        }
        blocks.append(QStringLiteral("<section><h4>%1. %2 <span class='studio-chip'>%3</span></h4><ul>%4</ul></section>")
                          .arg(index + 1)
                          .arg(slideTitle)
                          .arg(layout)
                          .arg(items.join(QString())));
    }
    blocks.append(QStringLiteral("<h3>事实边界</h3><p class='studio-warning'>%1</p>").arg(factNotice));
    if (!assetNotice.isEmpty()) {
        blocks.append(QStringLiteral("<h3>视觉素材</h3><p class='studio-chip'>%1</p>").arg(assetNotice));
    }
    if (!researchNotice.isEmpty()) {
        blocks.append(QStringLiteral("<h3>公开资料参考</h3><p class='studio-chip'>%1</p>").arg(researchNotice));
    }
    if (!dataNotice.isEmpty()) {
        blocks.append(QStringLiteral("<h3>数据图表计划</h3><p class='studio-chip'>%1</p>").arg(dataNotice));
        const QJsonArray requestedVisuals = currentPlan.dataPlan.value(QStringLiteral("requested_visuals")).toArray();
        const QJsonArray visualMetrics = currentPlan.dataPlan.value(QStringLiteral("visual_metrics")).toArray();
        QStringList visualItems;
        const auto visualLabel = [](const QString &value) {
            if (value == QStringLiteral("comparison_table")) return QStringLiteral("横向数据表");
            if (value == QStringLiteral("trend_table")) return QStringLiteral("趋势明细表");
            if (value == QStringLiteral("comparison_bar")) return QStringLiteral("柱状图");
            if (value == QStringLiteral("grouped_bar")) return QStringLiteral("分组柱状图");
            if (value == QStringLiteral("horizontal_bar")) return QStringLiteral("横向条形图");
            if (value == QStringLiteral("trend_line")) return QStringLiteral("折线图");
            if (value == QStringLiteral("trend_area")) return QStringLiteral("趋势面积图");
            if (value == QStringLiteral("share_pie")) return QStringLiteral("构成饼图");
            if (value == QStringLiteral("share_doughnut")) return QStringLiteral("构成环形图");
            return QStringLiteral("数据视图");
        };
        for (int index = 0; index < requestedVisuals.size(); ++index) {
            QStringList metricLabels;
            if (index < visualMetrics.size()) {
                const QJsonArray metrics = visualMetrics.at(index).toArray();
                for (const QJsonValue &metric : metrics) {
                    if (!metric.toString().trimmed().isEmpty()) {
                        metricLabels.append(escapeHtml(metric.toString()));
                    }
                }
            }
            const QString metricText = metricLabels.isEmpty()
                ? QStringLiteral("由规划模型按数据形态选择")
                : metricLabels.join(QStringLiteral("、"));
            visualItems.append(
                QStringLiteral("<li><b>%1</b>：%2</li>")
                    .arg(escapeHtml(visualLabel(requestedVisuals.at(index).toString())), metricText));
        }
        if (!visualItems.isEmpty()) {
            blocks.append(QStringLiteral("<h3>数据交付清单</h3><ul>%1</ul>").arg(visualItems.join(QString())));
        }
    }

    // 计划阶段刻意不联网，避免客户刚输入一句主题就触发搜索、图片生成或数据读取。此前预览
    // 只写“尚未联网”容易让人误以为模型 Key 没有生效；这里把“尚未执行”和“确认后将执行”
    // 分开呈现，且直接说明确认导出才是外部调用的起点。
    const QString visualProvider = currentPlan.assetPlan.value(QStringLiteral("provider")).toString();
    const bool visualPlanned = currentPlan.assetPlan.value(QStringLiteral("state")).toString()
        == QStringLiteral("planned") && !visualProvider.isEmpty();
    const bool publicResearchPlanned = currentPlan.researchPlan.value(QStringLiteral("state")).toString()
        == QStringLiteral("planned")
        && currentPlan.researchPlan.value(QStringLiteral("provider")).toString() == QStringLiteral("wikimedia");
    const bool dataResearchPlanned = currentPlan.dataPlan.value(QStringLiteral("state")).toString()
        == QStringLiteral("research_planned")
        && currentPlan.dataPlan.value(QStringLiteral("provider")).toString() == QStringLiteral("research_gateway");
    const bool worldBankPlanned = (currentPlan.dataPlan.value(QStringLiteral("state")).toString()
                                      == QStringLiteral("planned")
                                  || currentPlan.dataPlan.value(QStringLiteral("state")).toString()
                                      == QStringLiteral("provider_planned"))
        && currentPlan.dataPlan.value(QStringLiteral("provider")).toString() == QStringLiteral("world_bank");
    QStringList pendingActions;
    if (visualPlanned) {
        pendingActions.append(
            visualProvider == QStringLiteral("seedream")
                ? QStringLiteral("按页面意图调用 Seedream 生成配图。")
                : QStringLiteral("按页面意图检索 Pexels 授权图片。"));
    }
    if (publicResearchPlanned) {
        pendingActions.append(QStringLiteral("读取最多 3 条公开资料，仅作为来源页和人工复核线索。"));
    }
    if (dataResearchPlanned) {
        pendingActions.append(
            QStringLiteral("使用已配置模型按数据蓝图直接生成可编辑表格和图表，不执行网页搜索或来源核验。"));
    } else if (worldBankPlanned) {
        pendingActions.append(QStringLiteral("读取已固化的 World Bank 指标并校验统一年份后生成图表。"));
    }
    if (!pendingActions.isEmpty()) {
        QStringList actionItems;
        for (const QString &action : pendingActions) {
            actionItems.append(QStringLiteral("<li>%1</li>").arg(escapeHtml(action)));
        }
        blocks.append(
            QStringLiteral("<h3>确认导出后才会执行</h3>"
                           "<p class='studio-action'>当前是创作计划预览。点击“确认导出”后才会执行下列模型、素材或文件写入步骤。</p>"
                           "<ul>%1</ul>")
                .arg(actionItems.join(QString())));
    }
    const QJsonArray assetSlots = currentPlan.assetPlan.value(QStringLiteral("slots")).toArray();
    if (!assetSlots.isEmpty()) {
        // slots 是 Qt 的元对象宏，局部变量不能复用该名称，否则会在预处理阶段破坏 C++ 语法。
        QStringList slotItems;
        for (const QJsonValue &value : assetSlots) {
            const QJsonObject slot = value.toObject();
            slotItems.append(QStringLiteral("<li><b>%1</b> - %2</li>")
                             .arg(escapeHtml(slot.value(QStringLiteral("slide_title")).toString()),
                                  escapeHtml(slot.value(QStringLiteral("purpose")).toString())));
        }
        const QString provider = currentPlan.assetPlan.value(QStringLiteral("provider")).toString();
        const QString providerText = provider == QStringLiteral("seedream")
            ? QStringLiteral("若确认导出，将按以下页面意图调用 Seedream 生成图片：")
            : QStringLiteral("若确认导出，将按以下页面意图请求 Pexels 授权图片：");
        blocks.append(QStringLiteral("<p class='studio-muted'>%1</p><ul>%2</ul>")
                          .arg(providerText, slotItems.join(QString())));
    }
    if (!currentPlan.warnings.isEmpty()) {
        blocks.append(QStringLiteral("<h3>注意事项</h3><ul><li>%1</li></ul>")
                          .arg(escapeHtml(currentPlan.warnings.join(QStringLiteral("\n"))).replace(
                              QStringLiteral("<br>"), QStringLiteral("</li><li>"))));
    }
    return blocks.join(QString());
}

QString PresentationStudioDialog::suggestedFilename() const
{
    QString title = currentPlan.brief.value(QStringLiteral("title")).toString().trimmed();
    title.replace(QRegularExpression(QStringLiteral("[\\\\/:*?\"<>|]+")), QStringLiteral("-"));
    title = title.simplified();
    if (title.isEmpty()) {
        title = QStringLiteral("AgentFlow 演示方案");
    }
    return QStringLiteral("%1.pptx").arg(title.left(64));
}
