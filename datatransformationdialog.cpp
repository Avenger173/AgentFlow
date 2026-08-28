#include "datatransformationdialog.h"

#include "ui_datatransformationdialog.h"

#include <QIcon>
#include <QJsonDocument>
#include <QListWidgetItem>
#include <QSize>
#include <QSignalBlocker>

namespace {

constexpr int MaximumQueuedOperations = 12;

QString operationTitle(const QString &operationType)
{
    if (operationType == QStringLiteral("arithmetic")) {
        return QStringLiteral("四则计算 / 比率");
    }
    if (operationType == QStringLiteral("date_part")) {
        return QStringLiteral("日期拆分");
    }
    if (operationType == QStringLiteral("round_number")) {
        return QStringLiteral("数值保留位数");
    }
    if (operationType == QStringLiteral("rank")) {
        return QStringLiteral("排名");
    }
    if (operationType == QStringLiteral("share")) {
        return QStringLiteral("占比");
    }
    if (operationType == QStringLiteral("segment")) {
        return QStringLiteral("分段标签");
    }
    if (operationType == QStringLiteral("cumulative")) {
        return QStringLiteral("累计");
    }
    if (operationType == QStringLiteral("period_change")) {
        return QStringLiteral("环比");
    }
    if (operationType == QStringLiteral("period_rate")) {
        return QStringLiteral("环比百分比");
    }
    return QStringLiteral("文本清理");
}

QString operationDescription(const QString &operationType)
{
    if (operationType == QStringLiteral("arithmetic")) {
        return QStringLiteral("将两个数值字段相加、相减、相乘或相除，生成新的数值字段。");
    }
    if (operationType == QStringLiteral("date_part")) {
        return QStringLiteral("从高置信日期字段提取年、月、季度或星期，便于后续分组和图表。");
    }
    if (operationType == QStringLiteral("round_number")) {
        return QStringLiteral("在副本中新建保留指定小数位的数值字段，不改动原数值与分析精度。");
    }
    if (operationType == QStringLiteral("rank")) {
        return QStringLiteral("按整个当前表的数值字段生成降序排名，不隐含按类别分组。");
    }
    if (operationType == QStringLiteral("share")) {
        return QStringLiteral("计算每一行数值占当前字段合计的比例，分母为零会显示为空值。");
    }
    if (operationType == QStringLiteral("segment")) {
        return QStringLiteral("把数值映射为可读标签：成绩优先使用 60 / 80 / 90 档，其它数值采用等宽三档。");
    }
    if (operationType == QStringLiteral("cumulative")) {
        return QStringLiteral("按日期升序生成全表累计值；相同日期维持源文件中的原有顺序。");
    }
    if (operationType == QStringLiteral("period_change")) {
        return QStringLiteral("按日期升序计算相邻行差值；第一行没有上一期，会明确显示为空值。");
    }
    if (operationType == QStringLiteral("period_rate")) {
        return QStringLiteral("按日期升序计算相邻行相对上一期的百分比变化；上一期为 0 或首行会留空。");
    }
    return QStringLiteral("复制文本到新字段并去除首尾空白，不会修改原字段内容。");
}

} // namespace

DataTransformationDialog::DataTransformationDialog(
    const QJsonObject &analysisPreview,
    const QString &defaultGoalValue,
    QWidget *parent)
    : QDialog(parent)
    , ui(new Ui::DataTransformationDialog)
    , datasetProfile(analysisPreview.value(QStringLiteral("dataset_profile")).toObject())
    , columns(datasetProfile.value(QStringLiteral("columns")).toArray())
    , defaultGoal(defaultGoalValue.trimmed())
{
    ui->setupUi(this);
    ui->dialogIcon->setPixmap(QIcon(QStringLiteral(":/icons/data.svg")).pixmap(32, 32));

    addOperation(QStringLiteral("四则计算 / 比率"), QStringLiteral("单价 × 数量、收入 ÷ 成本"), QStringLiteral("arithmetic"));
    addOperation(QStringLiteral("日期拆分"), QStringLiteral("从日期得到年、月或季度"), QStringLiteral("date_part"));
    addOperation(QStringLiteral("数值保留位数"), QStringLiteral("在副本中保留 0 至 6 位小数"), QStringLiteral("round_number"));
    addOperation(QStringLiteral("排名"), QStringLiteral("按数值生成全表降序位置"), QStringLiteral("rank"));
    addOperation(QStringLiteral("占比"), QStringLiteral("计算每行占字段总量的比例"), QStringLiteral("share"));
    addOperation(QStringLiteral("分段标签"), QStringLiteral("成绩、金额等区间标签"), QStringLiteral("segment"));
    addOperation(QStringLiteral("累计"), QStringLiteral("按日期持续累积数值"), QStringLiteral("cumulative"));
    addOperation(QStringLiteral("环比"), QStringLiteral("按日期比较相邻记录"), QStringLiteral("period_change"));
    addOperation(QStringLiteral("环比百分比"), QStringLiteral("按日期计算相对上一期的变化率"), QStringLiteral("period_rate"));
    addOperation(QStringLiteral("文本清理"), QStringLiteral("复制并去除首尾空白"), QStringLiteral("text_trim"));

    ui->datePartCombo->addItem(QStringLiteral("年份"), QStringLiteral("year"));
    ui->datePartCombo->addItem(QStringLiteral("月份"), QStringLiteral("month"));
    ui->datePartCombo->addItem(QStringLiteral("季度"), QStringLiteral("quarter"));
    ui->datePartCombo->addItem(QStringLiteral("星期"), QStringLiteral("weekday"));
    ui->arithmeticCombo->addItem(QStringLiteral("相加"), QStringLiteral("add"));
    ui->arithmeticCombo->addItem(QStringLiteral("相减"), QStringLiteral("subtract"));
    ui->arithmeticCombo->addItem(QStringLiteral("相乘"), QStringLiteral("multiply"));
    ui->arithmeticCombo->addItem(QStringLiteral("相除 / 比率"), QStringLiteral("divide"));
    for (int digits = 0; digits <= 6; ++digits) {
        ui->roundDigitsCombo->addItem(
            digits == 0 ? QStringLiteral("不保留小数") : QStringLiteral("保留 %1 位小数").arg(digits),
            digits);
    }
    ui->roundDigitsCombo->setCurrentIndex(2);

    const QJsonObject dataset = datasetProfile.value(QStringLiteral("dataset")).toObject();
    const QString datasetName = dataset.value(QStringLiteral("name")).toString();
    const int rowCount = datasetProfile.value(QStringLiteral("row_count")).toInt();
    ui->scopeLabel->setText(
        datasetName.isEmpty()
            ? QStringLiteral("当前数据范围不可用")
            : QStringLiteral("%1 · %2 行 · 仅本机预览").arg(datasetName).arg(rowCount));

    connect(ui->operationList, &QListWidget::currentRowChanged, this, [this](int) { refreshOperationUi(); });
    connect(ui->primaryCombo, qOverload<int>(&QComboBox::currentIndexChanged), this, [this](int) {
        rebuildFieldChoices();
    });
    connect(ui->addQueueButton, &QPushButton::clicked, this, &DataTransformationDialog::addCurrentOperationToQueue);
    connect(ui->removeQueueButton, &QPushButton::clicked, this, &DataTransformationDialog::removeSelectedQueuedOperation);
    connect(ui->previewButton, &QPushButton::clicked, this, [this]() {
        if (queuedOperations.isEmpty()) {
            addCurrentOperationToQueue();
        }
        if (queuedOperations.isEmpty()) {
            return;
        }
        const QJsonObject dataset = datasetProfile.value(QStringLiteral("dataset")).toObject();
        const QString sourceSha256 = datasetProfile.value(QStringLiteral("source_sha256")).toString();
        if (dataset.value(QStringLiteral("name")).toString().isEmpty() || sourceSha256.size() != 64) {
            ui->statusLabel->setText(QStringLiteral("当前数据版本已变化，请关闭后重新生成分析预览。"));
            return;
        }

        const QJsonObject firstOperation = queuedOperations.first().toObject();
        QJsonObject request;
        request.insert(QStringLiteral("dataset_name"), dataset.value(QStringLiteral("name")).toString());
        request.insert(QStringLiteral("source_sha256"), sourceSha256);
        request.insert(QStringLiteral("goal"), defaultGoal);
        // Export contract 兼容首项平铺字段；多字段语义由 operations 队列统一校验。
        for (auto iterator = firstOperation.constBegin(); iterator != firstOperation.constEnd(); ++iterator) {
            request.insert(iterator.key(), iterator.value());
        }
        request.insert(QStringLiteral("operations"), queuedOperations);
        emit previewRequested(request);
        accept();
    });
    connect(ui->cancelButton, &QPushButton::clicked, this, &QDialog::reject);

    ui->operationList->setCurrentRow(0);
    refreshOperationUi();
    refreshQueuedOperationsUi();
}

DataTransformationDialog::~DataTransformationDialog()
{
    delete ui;
}

void DataTransformationDialog::addOperation(
    const QString &title,
    const QString &subtitle,
    const QString &operationType)
{
    auto *item = new QListWidgetItem(title, ui->operationList);
    item->setData(Qt::UserRole, operationType);
    item->setData(Qt::ToolTipRole, subtitle);
    item->setSizeHint(QSize(0, 46));
}

QString DataTransformationDialog::selectedOperationType() const
{
    const QListWidgetItem *item = ui->operationList->currentItem();
    return item ? item->data(Qt::UserRole).toString() : QString();
}

QString DataTransformationDialog::typeDisplayName(const QString &inferredType) const
{
    if (inferredType == QStringLiteral("number")) {
        return QStringLiteral("数值");
    }
    if (inferredType == QStringLiteral("date")) {
        return QStringLiteral("日期");
    }
    if (inferredType == QStringLiteral("boolean")) {
        return QStringLiteral("布尔");
    }
    if (inferredType == QStringLiteral("mixed")) {
        return QStringLiteral("混合");
    }
    return QStringLiteral("文本");
}

bool DataTransformationDialog::columnMatchesType(const QJsonObject &column, const QString &requiredType) const
{
    const QString type = column.value(QStringLiteral("inferred_type")).toString();
    if (requiredType == QStringLiteral("text")) {
        return type == QStringLiteral("text") || type == QStringLiteral("mixed") || type == QStringLiteral("boolean");
    }
    return type == requiredType;
}

void DataTransformationDialog::refreshOperationUi()
{
    const QString operationType = selectedOperationType();
    const bool secondaryRequired = operationType == QStringLiteral("arithmetic")
        || operationType == QStringLiteral("cumulative") || operationType == QStringLiteral("period_change")
        || operationType == QStringLiteral("period_rate");
    const bool datePartVisible = operationType == QStringLiteral("date_part");
    const bool arithmeticVisible = operationType == QStringLiteral("arithmetic");
    const bool roundDigitsVisible = operationType == QStringLiteral("round_number");

    ui->operationTitle->setText(operationTitle(operationType));
    ui->operationDescription->setText(operationDescription(operationType));
    ui->secondaryLabel->setVisible(secondaryRequired);
    ui->secondaryCombo->setVisible(secondaryRequired);
    ui->datePartLabel->setVisible(datePartVisible);
    ui->datePartCombo->setVisible(datePartVisible);
    ui->arithmeticLabel->setVisible(arithmeticVisible);
    ui->arithmeticCombo->setVisible(arithmeticVisible);
    ui->roundDigitsLabel->setVisible(roundDigitsVisible);
    ui->roundDigitsCombo->setVisible(roundDigitsVisible);
    rebuildFieldChoices();
}

void DataTransformationDialog::rebuildFieldChoices()
{
    const QString operationType = selectedOperationType();
    const QString requiredPrimaryType = operationType == QStringLiteral("date_part")
        ? QStringLiteral("date")
        : operationType == QStringLiteral("text_trim") ? QStringLiteral("text") : QStringLiteral("number");
    const bool secondaryRequired = operationType == QStringLiteral("arithmetic")
        || operationType == QStringLiteral("cumulative") || operationType == QStringLiteral("period_change")
        || operationType == QStringLiteral("period_rate");
    const QString requiredSecondaryType = operationType == QStringLiteral("arithmetic")
        ? QStringLiteral("number") : QStringLiteral("date");
    const QString previousPrimary = ui->primaryCombo->currentData().toString();
    const QString previousSecondary = ui->secondaryCombo->currentData().toString();

    {
        const QSignalBlocker blocker(ui->primaryCombo);
        ui->primaryCombo->clear();
        for (const QJsonValue &value : columns) {
            const QJsonObject column = value.toObject();
            if (!columnMatchesType(column, requiredPrimaryType)) {
                continue;
            }
            const QString name = column.value(QStringLiteral("name")).toString();
            ui->primaryCombo->addItem(
                QStringLiteral("%1 · %2").arg(name, typeDisplayName(column.value(QStringLiteral("inferred_type")).toString())),
                name);
        }
        const int previousIndex = ui->primaryCombo->findData(previousPrimary);
        ui->primaryCombo->setCurrentIndex(previousIndex >= 0 ? previousIndex : 0);
    }
    {
        const QSignalBlocker blocker(ui->secondaryCombo);
        ui->secondaryCombo->clear();
        if (secondaryRequired) {
            const QString primaryName = ui->primaryCombo->currentData().toString();
            for (const QJsonValue &value : columns) {
                const QJsonObject column = value.toObject();
                const QString name = column.value(QStringLiteral("name")).toString();
                if (name == primaryName || !columnMatchesType(column, requiredSecondaryType)) {
                    continue;
                }
                ui->secondaryCombo->addItem(
                    QStringLiteral("%1 · %2").arg(name, typeDisplayName(requiredSecondaryType)), name);
            }
            const int previousIndex = ui->secondaryCombo->findData(previousSecondary);
            ui->secondaryCombo->setCurrentIndex(previousIndex >= 0 ? previousIndex : 0);
        }
    }

    const bool ready = !ui->primaryCombo->currentData().toString().isEmpty()
        && (!secondaryRequired || !ui->secondaryCombo->currentData().toString().isEmpty());
    ui->addQueueButton->setEnabled(ready && queuedOperations.size() < MaximumQueuedOperations);
    ui->previewButton->setEnabled(ready || !queuedOperations.isEmpty());
    if (!queuedOperations.isEmpty()) {
        ui->statusLabel->setText(
            QStringLiteral("已加入 %1 项加工；预览会统一计算，确认后只生成一个新副本。").arg(queuedOperations.size()));
    } else {
        ui->statusLabel->setText(
            ready
                ? QStringLiteral("先加入本次加工；可继续添加最多 %1 项，再统一生成预览。").arg(MaximumQueuedOperations)
                : QStringLiteral("当前画像没有适用字段。请换一种加工方式或返回检查字段类型。"));
    }
}

QJsonObject DataTransformationDialog::buildCurrentOperationRequest(QString *errorMessage) const
{
    const QString operationType = selectedOperationType();
    const QString primaryColumn = ui->primaryCombo->currentData().toString().trimmed();
    const QString secondaryColumn = ui->secondaryCombo->currentData().toString().trimmed();
    const bool secondaryRequired = operationType == QStringLiteral("arithmetic")
        || operationType == QStringLiteral("cumulative") || operationType == QStringLiteral("period_change")
        || operationType == QStringLiteral("period_rate");
    if (operationType.isEmpty() || primaryColumn.isEmpty() || (secondaryRequired && secondaryColumn.isEmpty())) {
        if (errorMessage) {
            *errorMessage = QStringLiteral("当前操作缺少可用字段；请换一种方式或检查数据画像。");
        }
        return QJsonObject{};
    }

    QJsonObject request;
    request.insert(QStringLiteral("operation_type"), operationType);
    request.insert(QStringLiteral("primary_column"), primaryColumn);
    if (secondaryRequired) {
        request.insert(QStringLiteral("secondary_column"), secondaryColumn);
    }
    if (!ui->resultNameEdit->text().trimmed().isEmpty()) {
        request.insert(QStringLiteral("result_column"), ui->resultNameEdit->text().trimmed());
    }
    request.insert(QStringLiteral("date_part"), ui->datePartCombo->currentData().toString());
    request.insert(QStringLiteral("arithmetic_operator"), ui->arithmeticCombo->currentData().toString());
    request.insert(QStringLiteral("round_digits"), ui->roundDigitsCombo->currentData().toInt());
    return request;
}

void DataTransformationDialog::addCurrentOperationToQueue()
{
    if (queuedOperations.size() >= MaximumQueuedOperations) {
        ui->statusLabel->setText(QStringLiteral("一次最多新增 %1 个字段；请先预览当前队列。").arg(MaximumQueuedOperations));
        return;
    }
    QString error;
    const QJsonObject operation = buildCurrentOperationRequest(&error);
    if (operation.isEmpty()) {
        ui->statusLabel->setText(error);
        return;
    }

    const QString requestedResult = operation.value(QStringLiteral("result_column")).toString().trimmed();
    if (!requestedResult.isEmpty()) {
        for (const QJsonValue &value : queuedOperations) {
            if (value.toObject().value(QStringLiteral("result_column")).toString().trimmed() == requestedResult) {
                ui->statusLabel->setText(
                    QStringLiteral("新字段名称“%1”已在队列中，请换一个名称。").arg(requestedResult));
                return;
            }
        }
    }
    queuedOperations.append(operation);
    refreshQueuedOperationsUi();
    rebuildFieldChoices();
}

void DataTransformationDialog::removeSelectedQueuedOperation()
{
    const int index = ui->queueList->currentRow();
    if (index < 0 || index >= queuedOperations.size()) {
        return;
    }
    queuedOperations.removeAt(index);
    refreshQueuedOperationsUi();
    rebuildFieldChoices();
}

void DataTransformationDialog::refreshQueuedOperationsUi()
{
    ui->queueList->clear();
    for (int index = 0; index < queuedOperations.size(); ++index) {
        const QJsonObject operation = queuedOperations.at(index).toObject();
        const QString type = operationTitle(operation.value(QStringLiteral("operation_type")).toString());
        const QString primary = operation.value(QStringLiteral("primary_column")).toString();
        const QString result = operation.value(QStringLiteral("result_column")).toString();
        const QString summary = result.isEmpty()
            ? QStringLiteral("%1 · %2 · 自动命名").arg(type, primary)
            : QStringLiteral("%1 · %2 → %3").arg(type, primary, result);
        auto *item = new QListWidgetItem(QStringLiteral("%1. %2").arg(index + 1).arg(summary), ui->queueList);
        item->setData(Qt::UserRole, QJsonDocument(operation).toJson(QJsonDocument::Compact));
        item->setToolTip(QStringLiteral("预览与导出时会和其它项目一起新增到同一份副本。"));
    }
    ui->removeQueueButton->setEnabled(!queuedOperations.isEmpty());
    ui->queueHint->setText(
        queuedOperations.isEmpty()
            ? QStringLiteral("尚未加入加工项。选择一种操作和字段后加入队列；原字段不会被覆盖。")
            : QStringLiteral("已选 %1 / %2 项。确认后会在一份原格式副本中统一追加这些字段。")
                  .arg(queuedOperations.size())
                  .arg(MaximumQueuedOperations));
}
