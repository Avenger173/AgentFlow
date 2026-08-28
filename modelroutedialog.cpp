#include "modelroutedialog.h"

#include "ui_modelroutedialog.h"

#include <QComboBox>
#include <QHeaderView>
#include <QIcon>
#include <QLabel>
#include <QLineEdit>
#include <QPushButton>
#include <QStyle>
#include <QTableWidget>
#include <QTableWidgetItem>

namespace {

QString escapeHtml(const QString &value)
{
    return value.toHtmlEscaped().replace(QStringLiteral("\n"), QStringLiteral("<br>"));
}

} // namespace

ModelRouteDialog::ModelRouteDialog(QWidget *parent)
    : QDialog(parent)
    , ui(new Ui::ModelRouteDialog)
{
    ui->setupUi(this);
    setWindowFlag(Qt::WindowContextHelpButtonHint, false);
    ui->routeIcon->setPixmap(QIcon(QStringLiteral(":/icons/model.svg")).pixmap(30, 30));
    ui->routeTable->setColumnCount(3);
    ui->routeTable->setHorizontalHeaderLabels({QStringLiteral("任务作用域"), QStringLiteral("当前模型"), QStringLiteral("状态")});
    ui->routeTable->setSelectionBehavior(QAbstractItemView::SelectRows);
    ui->routeTable->setSelectionMode(QAbstractItemView::SingleSelection);
    ui->routeTable->setEditTriggers(QAbstractItemView::NoEditTriggers);
    ui->routeTable->setAlternatingRowColors(true);
    ui->routeTable->verticalHeader()->setVisible(false);
    ui->routeTable->verticalHeader()->setDefaultSectionSize(40);
    ui->routeTable->horizontalHeader()->setSectionResizeMode(0, QHeaderView::ResizeToContents);
    ui->routeTable->horizontalHeader()->setSectionResizeMode(1, QHeaderView::Stretch);
    ui->routeTable->horizontalHeader()->setSectionResizeMode(2, QHeaderView::ResizeToContents);

    ui->routeModeCombo->addItem(QStringLiteral("继承全局默认"), QStringLiteral("inherit_global"));
    ui->routeModeCombo->addItem(QStringLiteral("使用独立 Profile"), QStringLiteral("configured"));
    ui->thinkingCombo->addItem(QStringLiteral("关闭"), QStringLiteral("disabled"));
    ui->thinkingCombo->addItem(QStringLiteral("开启"), QStringLiteral("enabled"));

    ui->refreshButton->setIcon(style()->standardIcon(QStyle::SP_BrowserReload));
    ui->refreshButton->setToolTip(QStringLiteral("重新读取任务模型路由，不会调用模型。"));
    ui->saveButton->setIcon(style()->standardIcon(QStyle::SP_DialogSaveButton));
    ui->saveButton->setToolTip(QStringLiteral("保存当前作用域的模型选择。不会复制或显示 API Key。"));
    ui->closeButton->setIcon(style()->standardIcon(QStyle::SP_DialogCloseButton));

    connect(ui->routeTable, &QTableWidget::itemSelectionChanged, this, &ModelRouteDialog::updateEditor);
    connect(ui->routeModeCombo, &QComboBox::currentIndexChanged, this, [this](int) {
        if (!applyingEditorState) {
            updateProviderEditor(false);
            updateActionState();
        }
    });
    connect(ui->providerCombo, &QComboBox::currentIndexChanged, this, [this](int) {
        if (!applyingEditorState) {
            updateProviderEditor(true);
            updateActionState();
        }
    });
    connect(ui->baseUrlInput, &QLineEdit::textChanged, this, &ModelRouteDialog::updateActionState);
    connect(ui->modelInput, &QLineEdit::textChanged, this, &ModelRouteDialog::updateActionState);
    connect(ui->thinkingCombo, &QComboBox::currentIndexChanged, this, [this](int) {
        if (!applyingEditorState) {
            updateActionState();
        }
    });
    connect(ui->refreshButton, &QPushButton::clicked, this, [this]() {
        setLoading(true, QStringLiteral("正在读取任务模型路由…"));
        emit refreshRequested();
    });
    connect(ui->saveButton, &QPushButton::clicked, this, [this]() {
        const ModelRouteInfo *route = currentRoute();
        if (!route) {
            return;
        }
        const QString mode = ui->routeModeCombo->currentData().toString();
        setLoading(true, QStringLiteral("正在保存“%1”的模型路由…").arg(route->label));
        emit saveRequested(
            route->routeId,
            mode,
            ui->providerCombo->currentData().toString(),
            ui->baseUrlInput->text(),
            ui->modelInput->text(),
            ui->thinkingCombo->currentData().toString());
    });
    connect(ui->closeButton, &QPushButton::clicked, this, &QDialog::reject);

    setStatus(QStringLiteral("打开后会读取每个任务作用域的实际模型配置。"));
    updateActionState();
}

ModelRouteDialog::~ModelRouteDialog()
{
    delete ui;
}

void ModelRouteDialog::setModelProviders(const QList<ModelProviderInfo> &value)
{
    providers = value;
    const QString selectedProvider = ui->providerCombo->currentData().toString();
    ui->providerCombo->blockSignals(true);
    ui->providerCombo->clear();
    for (const ModelProviderInfo &provider : providers) {
        ui->providerCombo->addItem(provider.label, provider.provider);
    }
    const int selectedIndex = ui->providerCombo->findData(selectedProvider);
    if (selectedIndex >= 0) {
        ui->providerCombo->setCurrentIndex(selectedIndex);
    }
    ui->providerCombo->blockSignals(false);
    updateProviderEditor(true);
}

void ModelRouteDialog::setRoutes(const ModelRouteListResult &result)
{
    const QString preferredRouteId = !requestedRouteId.isEmpty()
                                        ? requestedRouteId
                                        : (currentRoute() ? currentRoute()->routeId : QString());
    routes = result.routes;
    loading = false;
    populateRouteTable(preferredRouteId);
    setStatus(QStringLiteral("已读取 %1 个作用域。修改只会影响后续的新任务，不会改写进行中的任务。").arg(routes.size()),
              QStringLiteral("success"));
    updateActionState();
}

void ModelRouteDialog::selectRoute(const QString &routeId)
{
    requestedRouteId = routeId.trimmed();
    if (!requestedRouteId.isEmpty() && !routes.isEmpty()) {
        populateRouteTable(requestedRouteId);
    }
}

void ModelRouteDialog::applySavedRoute(const ModelRouteInfo &route)
{
    bool updated = false;
    for (ModelRouteInfo &item : routes) {
        if (item.routeId == route.routeId) {
            item = route;
            updated = true;
            break;
        }
    }
    if (!updated) {
        routes.append(route);
    }
    loading = false;
    populateRouteTable(route.routeId);
    setStatus(QStringLiteral("已保存。后续“%1”任务将使用这里显示的 Profile。 ").arg(route.label),
              QStringLiteral("success"));
    updateActionState();
}

void ModelRouteDialog::setLoading(bool value, const QString &message)
{
    loading = value;
    if (!message.isEmpty()) {
        setStatus(message, value ? QStringLiteral("running") : QStringLiteral("neutral"));
    }
    updateActionState();
}

void ModelRouteDialog::showRequestError(const QString &message)
{
    loading = false;
    setStatus(QStringLiteral("未能完成操作：%1").arg(message), QStringLiteral("error"));
    updateActionState();
}

void ModelRouteDialog::populateRouteTable(const QString &preferredRouteId)
{
    ui->routeTable->blockSignals(true);
    ui->routeTable->setRowCount(routes.size());
    int targetRow = -1;
    for (int row = 0; row < routes.size(); ++row) {
        const ModelRouteInfo &route = routes.at(row);
        auto *labelItem = new QTableWidgetItem(route.label);
        labelItem->setData(Qt::UserRole, route.routeId);
        labelItem->setToolTip(route.description);
        auto *modelItem = new QTableWidgetItem(resolvedModelLabel(route));
        modelItem->setToolTip(route.availabilityMessage);
        auto *statusItem = new QTableWidgetItem(availabilityLabel(route.availability));
        statusItem->setToolTip(route.availabilityMessage);
        ui->routeTable->setItem(row, 0, labelItem);
        ui->routeTable->setItem(row, 1, modelItem);
        ui->routeTable->setItem(row, 2, statusItem);
        if (route.routeId == preferredRouteId) {
            targetRow = row;
        }
    }
    ui->routeTable->blockSignals(false);
    if (targetRow < 0 && !routes.isEmpty()) {
        targetRow = 0;
    }
    if (targetRow >= 0) {
        ui->routeTable->selectRow(targetRow);
    }
    updateEditor();
}

void ModelRouteDialog::updateEditor()
{
    const ModelRouteInfo *route = currentRoute();
    applyingEditorState = true;
    if (!route) {
        ui->routeTitleLabel->setText(QStringLiteral("选择一个任务作用域"));
        ui->routeDescriptionLabel->setText(QStringLiteral("左侧显示已接入或预留的模型使用位置。"));
        ui->capabilityLabel->setText(QString());
        applyingEditorState = false;
        updateActionState();
        return;
    }

    ui->routeTitleLabel->setText(route->label);
    ui->routeDescriptionLabel->setText(route->description);
    ui->capabilityLabel->setText(capabilityLabel(route->requiredCapabilities));
    const int modeIndex = ui->routeModeCombo->findData(route->mode);
    ui->routeModeCombo->setCurrentIndex(modeIndex >= 0 ? modeIndex : 0);
    const QString provider = route->mode == QStringLiteral("configured") ? route->provider : route->resolvedProvider;
    const int providerIndex = ui->providerCombo->findData(provider);
    if (providerIndex >= 0) {
        ui->providerCombo->setCurrentIndex(providerIndex);
    }
    ui->baseUrlInput->setText(route->mode == QStringLiteral("configured") ? route->baseUrl : QString());
    ui->modelInput->setText(route->mode == QStringLiteral("configured") ? route->model : QString());
    const QString thinking = route->mode == QStringLiteral("configured") ? route->thinking : route->resolvedThinking;
    const int thinkingIndex = ui->thinkingCombo->findData(thinking);
    ui->thinkingCombo->setCurrentIndex(thinkingIndex >= 0 ? thinkingIndex : 0);
    ui->routeRuntimeLabel->setText(route->hasResolved
                                       ? QStringLiteral("当前解析：%1").arg(resolvedModelLabel(*route))
                                       : route->availabilityMessage);
    applyingEditorState = false;
    updateProviderEditor(true);
    updateActionState();
}

void ModelRouteDialog::updateProviderEditor(bool preserveEdits)
{
    const ModelRouteInfo *route = currentRoute();
    const bool configured = ui->routeModeCombo->currentData().toString() == QStringLiteral("configured");
    const bool editable = route && route->availability != QStringLiteral("reserved") && configured && !loading;
    const ModelProviderInfo *provider = providerById(ui->providerCombo->currentData().toString());
    ui->providerCombo->setEnabled(editable);
    ui->baseUrlInput->setEnabled(editable);
    ui->modelInput->setEnabled(editable);
    const bool supportsThinking = provider && provider->supportsThinking;
    ui->thinkingCombo->setEnabled(editable && supportsThinking);
    if (!supportsThinking) {
        const int disabledIndex = ui->thinkingCombo->findData(QStringLiteral("disabled"));
        if (disabledIndex >= 0) {
            ui->thinkingCombo->setCurrentIndex(disabledIndex);
        }
    }
    if (editable && provider && !preserveEdits) {
        ui->baseUrlInput->setText(provider->defaultBaseUrl);
        ui->modelInput->setText(provider->defaultModel);
    }
    const QString modeHint = configured
                                  ? QStringLiteral("独立 Profile 只引用该 Provider 已保存的 Key；不会复制 Key。")
                                  : QStringLiteral("继承全局默认模型。切换为独立 Profile 后才可编辑下方字段。");
    const QString providerHint = provider
                                     ? QStringLiteral("%1 · %2 · %3")
                                           .arg(provider->label, provider->supportsJsonOutput ? QStringLiteral("支持 JSON") : QStringLiteral("不支持 JSON"),
                                                provider->supportsToolCalls ? QStringLiteral("支持 Tool Calls") : QStringLiteral("不支持 Tool Calls"))
                                     : QStringLiteral("请先选择已配置的 Provider。 ");
    ui->profileHintLabel->setText(QStringLiteral("%1\n%2").arg(modeHint, providerHint));
}

void ModelRouteDialog::updateActionState()
{
    const ModelRouteInfo *route = currentRoute();
    const bool reserved = route && route->availability == QStringLiteral("reserved");
    const bool configured = ui->routeModeCombo->currentData().toString() == QStringLiteral("configured");
    const bool complete = !configured
                          || (!ui->providerCombo->currentData().toString().trimmed().isEmpty()
                              && !ui->baseUrlInput->text().trimmed().isEmpty()
                              && !ui->modelInput->text().trimmed().isEmpty());
    ui->routeModeCombo->setEnabled(route && !reserved && !loading);
    ui->saveButton->setEnabled(route && !reserved && !loading && complete);
    ui->refreshButton->setEnabled(!loading);
    if (reserved && route) {
        ui->routeRuntimeLabel->setText(QStringLiteral("预留作用域：%1").arg(route->availabilityMessage));
    }
}

void ModelRouteDialog::setStatus(const QString &message, const QString &kind)
{
    ui->statusLabel->setText(message);
    const QString objectName = kind == QStringLiteral("success") ? QStringLiteral("routeStatusSuccess")
        : kind == QStringLiteral("error") ? QStringLiteral("routeStatusError")
        : kind == QStringLiteral("running") ? QStringLiteral("routeStatusRunning")
                                              : QStringLiteral("routeStatusNeutral");
    ui->statusLabel->setObjectName(objectName);
    ui->statusLabel->style()->unpolish(ui->statusLabel);
    ui->statusLabel->style()->polish(ui->statusLabel);
}

const ModelRouteInfo *ModelRouteDialog::currentRoute() const
{
    const int row = ui->routeTable->currentRow();
    if (row < 0 || row >= routes.size()) {
        return nullptr;
    }
    return &routes.at(row);
}

const ModelProviderInfo *ModelRouteDialog::providerById(const QString &providerId) const
{
    for (const ModelProviderInfo &provider : providers) {
        if (provider.provider == providerId) {
            return &provider;
        }
    }
    return nullptr;
}

QString ModelRouteDialog::routeModeLabel(const QString &mode) const
{
    return mode == QStringLiteral("configured") ? QStringLiteral("独立 Profile") : QStringLiteral("继承全局");
}

QString ModelRouteDialog::availabilityLabel(const QString &availability) const
{
    if (availability == QStringLiteral("ready")) {
        return QStringLiteral("可用");
    }
    if (availability == QStringLiteral("reserved")) {
        return QStringLiteral("预留");
    }
    return QStringLiteral("需配置");
}

QString ModelRouteDialog::capabilityLabel(const QStringList &capabilities) const
{
    if (capabilities.isEmpty()) {
        return QStringLiteral("无额外能力要求");
    }
    QStringList labels;
    for (const QString &capability : capabilities) {
        if (capability == QStringLiteral("json_output")) {
            labels.append(QStringLiteral("需要 JSON Output"));
        } else if (capability == QStringLiteral("tool_calls")) {
            labels.append(QStringLiteral("需要 Tool Calls"));
        } else if (capability == QStringLiteral("visual_generation")) {
            labels.append(QStringLiteral("需要视觉生成"));
        }
    }
    return labels.isEmpty() ? QStringLiteral("能力要求由后端校验") : labels.join(QStringLiteral(" · "));
}

QString ModelRouteDialog::resolvedModelLabel(const ModelRouteInfo &route) const
{
    const QString provider = route.hasResolved ? route.resolvedLabel : route.provider;
    const QString model = route.hasResolved ? route.resolvedModel : route.model;
    if (provider.isEmpty() && model.isEmpty()) {
        return routeModeLabel(route.mode);
    }
    return QStringLiteral("%1 · %2").arg(provider, model);
}
