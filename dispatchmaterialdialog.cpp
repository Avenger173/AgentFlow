#include "dispatchmaterialdialog.h"

#include "taskactivityindicator.h"
#include "ui_dispatchmaterialdialog.h"

#include <QComboBox>
#include <QIcon>
#include <QLabel>
#include <QPushButton>
#include <QStyle>

namespace {

QString documentTypeLabel(const QString &type)
{
    if (type == QStringLiteral("pdf")) {
        return QStringLiteral("PDF");
    }
    if (type == QStringLiteral("docx")) {
        return QStringLiteral("Word");
    }
    if (type == QStringLiteral("image")) {
        return QStringLiteral("图片");
    }
    return QStringLiteral("文本");
}

} // namespace

DispatchMaterialDialog::DispatchMaterialDialog(QWidget *parent)
    : QDialog(parent)
    , ui(new Ui::DispatchMaterialDialog)
{
    ui->setupUi(this);
    setWindowFlag(Qt::WindowContextHelpButtonHint, false);
    ui->materialIcon->setPixmap(QIcon(QStringLiteral(":/icons/apps.svg")).pixmap(30, 30));
    ui->refreshButton->setIcon(style()->standardIcon(QStyle::SP_BrowserReload));
    ui->applyButton->setIcon(style()->standardIcon(QStyle::SP_DialogApplyButton));
    ui->cancelButton->setIcon(style()->standardIcon(QStyle::SP_DialogCancelButton));
    // 使用项目统一的轻量状态点：目录请求未结束时旋转，成功或失败后立即停下。
    // 它只表达这一个对话框的元数据同步，不会暗示材料正在解析或由模型处理。
    catalogActivityIndicator = new TaskActivityIndicator(ui->catalogStatusFrame);
    ui->catalogStatusLayout->insertWidget(0, catalogActivityIndicator);
    catalogActivityIndicator->setRunning(false);

    connect(ui->refreshButton, &QPushButton::clicked, this, [this]() {
        setCatalogStatus(QStringLiteral("正在同步已导入材料…"), QStringLiteral("running"));
        emit refreshRequested();
    });
    connect(ui->applyButton, &QPushButton::clicked, this, [this]() {
        emit materialsApplied(ui->documentCombo->currentData().toString(),
                              ui->knowledgeCombo->currentData().toString(),
                              ui->datasetCombo->currentData().toString());
        accept();
    });
    connect(ui->cancelButton, &QPushButton::clicked, this, &QDialog::reject);
    connect(ui->documentCombo, qOverload<int>(&QComboBox::currentIndexChanged), this, [this](int) {
        if (!applyingState) {
            updateApplyState();
        }
    });
    connect(ui->knowledgeCombo, qOverload<int>(&QComboBox::currentIndexChanged), this, [this](int) {
        if (!applyingState) {
            updateApplyState();
        }
    });
    connect(ui->datasetCombo, qOverload<int>(&QComboBox::currentIndexChanged), this, [this](int) {
        if (!applyingState) {
            updateApplyState();
        }
    });

    setCatalogStatus(QStringLiteral("可组合选择三类已导入材料；均只会授予本次任务只读范围。"));
    updateApplyState();
}

DispatchMaterialDialog::~DispatchMaterialDialog()
{
    delete ui;
}

void DispatchMaterialDialog::setDocuments(const QList<WorkspaceDocumentInfo> &value)
{
    documents = value;
    populateDocumentCombo();
}

void DispatchMaterialDialog::setKnowledgeBases(const QList<KnowledgeBaseInfo> &value)
{
    knowledgeBases = value;
    populateKnowledgeCombo();
}

void DispatchMaterialDialog::setDatasets(const QList<DataDatasetInfo> &value)
{
    datasets = value;
    populateDatasetCombo();
}

void DispatchMaterialDialog::setSelections(const QString &documentRef,
                                            const QString &knowledgeBaseId,
                                            const QString &datasetRef)
{
    selectedDocumentRef = documentRef;
    selectedKnowledgeBaseId = knowledgeBaseId;
    selectedDatasetRef = datasetRef;
    populateDocumentCombo();
    populateKnowledgeCombo();
    populateDatasetCombo();
}

void DispatchMaterialDialog::setCatalogStatus(const QString &message, const QString &kind)
{
    ui->catalogStatusLabel->setText(message);
    if (catalogActivityIndicator) {
        catalogActivityIndicator->setRunning(kind == QStringLiteral("running"));
    }
    if (kind == QStringLiteral("running")) {
        ui->catalogStatusLabel->setObjectName(QStringLiteral("statusRunning"));
    } else if (kind == QStringLiteral("error")) {
        ui->catalogStatusLabel->setObjectName(QStringLiteral("statusError"));
    } else {
        ui->catalogStatusLabel->setObjectName(QStringLiteral("statusNeutral"));
    }
    ui->catalogStatusLabel->style()->unpolish(ui->catalogStatusLabel);
    ui->catalogStatusLabel->style()->polish(ui->catalogStatusLabel);
}

void DispatchMaterialDialog::populateDocumentCombo()
{
    applyingState = true;
    const QString current = selectedDocumentRef.isEmpty() ? ui->documentCombo->currentData().toString()
                                                            : selectedDocumentRef;
    ui->documentCombo->clear();
    ui->documentCombo->addItem(QStringLiteral("不选择文档"), QString());
    for (const WorkspaceDocumentInfo &document : documents) {
        const QString ref = document.relativePath.isEmpty() ? document.name : document.relativePath;
        ui->documentCombo->addItem(
            QStringLiteral("%1 · %2 · %3 KB")
                .arg(document.name, documentTypeLabel(document.documentType))
                .arg(qMax(1, (document.sizeBytes + 1023) / 1024)),
            ref);
    }
    const int index = ui->documentCombo->findData(current);
    ui->documentCombo->setCurrentIndex(index >= 0 ? index : 0);
    applyingState = false;
    updateApplyState();
}

void DispatchMaterialDialog::populateKnowledgeCombo()
{
    applyingState = true;
    const QString current = selectedKnowledgeBaseId.isEmpty() ? ui->knowledgeCombo->currentData().toString()
                                                                : selectedKnowledgeBaseId;
    ui->knowledgeCombo->clear();
    ui->knowledgeCombo->addItem(QStringLiteral("不选择资料库"), QString());
    for (const KnowledgeBaseInfo &base : knowledgeBases) {
        QString state = QStringLiteral("待索引");
        if (base.status == QStringLiteral("ready")) {
            state = QStringLiteral("已索引");
        } else if (base.status == QStringLiteral("partial_failure")) {
            state = QStringLiteral("部分可用");
        } else if (base.status == QStringLiteral("indexing")) {
            state = QStringLiteral("索引中");
        }
        ui->knowledgeCombo->addItem(QStringLiteral("%1 · %2").arg(base.name, state), base.knowledgeBaseId);
    }
    const int index = ui->knowledgeCombo->findData(current);
    ui->knowledgeCombo->setCurrentIndex(index >= 0 ? index : 0);
    applyingState = false;
    updateApplyState();
}

void DispatchMaterialDialog::populateDatasetCombo()
{
    applyingState = true;
    const QString current = selectedDatasetRef.isEmpty() ? ui->datasetCombo->currentData().toString()
                                                           : selectedDatasetRef;
    ui->datasetCombo->clear();
    ui->datasetCombo->addItem(QStringLiteral("不选择数据集"), QString());
    for (const DataDatasetInfo &dataset : datasets) {
        const QString ref = dataset.relativePath.isEmpty() ? dataset.name : dataset.relativePath;
        ui->datasetCombo->addItem(
            QStringLiteral("%1 · %2 · %3 KB")
                .arg(dataset.name, dataset.datasetType.toUpper())
                .arg(qMax(1, (dataset.sizeBytes + 1023) / 1024)),
            ref);
    }
    const int index = ui->datasetCombo->findData(current);
    ui->datasetCombo->setCurrentIndex(index >= 0 ? index : 0);
    applyingState = false;
    updateApplyState();
}

void DispatchMaterialDialog::updateApplyState()
{
    if (!ui) {
        return;
    }
    const bool hasMaterial = !ui->documentCombo->currentData().toString().isEmpty()
                             || !ui->knowledgeCombo->currentData().toString().isEmpty()
                             || !ui->datasetCombo->currentData().toString().isEmpty();
    ui->applyButton->setEnabled(hasMaterial);
    ui->applyButton->setToolTip(hasMaterial
                                    ? QStringLiteral("应用所选材料到本次任务。")
                                    : QStringLiteral("至少选择一份材料后才能应用。"));
}
