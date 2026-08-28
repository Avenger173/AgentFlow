#ifndef MODELROUTEDIALOG_H
#define MODELROUTEDIALOG_H

#include <QDialog>

#include "backendclient.h"

QT_BEGIN_NAMESPACE
namespace Ui {
class ModelRouteDialog;
}
QT_END_NAMESPACE

class QTableWidgetItem;

// 任务模型路由是低频检查器：左侧只用于快速定位作用域，右侧仅编辑当前一项。它不复制
// API Key、不发起连接测试，也不把高级模型选择挤进供应商总配置页。
class ModelRouteDialog : public QDialog
{
    Q_OBJECT

public:
    explicit ModelRouteDialog(QWidget *parent = nullptr);
    ~ModelRouteDialog() override;

    void setModelProviders(const QList<ModelProviderInfo> &providers);
    void setRoutes(const ModelRouteListResult &result);
    // 具体工作台打开设置时预选其对应作用域；路由尚未返回时先记住请求，避免客户在
    // 全量列表里二次定位“总指挥规划”“知识库问答”等低频配置。
    void selectRoute(const QString &routeId);
    void applySavedRoute(const ModelRouteInfo &route);
    void setLoading(bool loading, const QString &message = QString());
    void showRequestError(const QString &message);

signals:
    void refreshRequested();
    void saveRequested(
        const QString &routeId,
        const QString &mode,
        const QString &provider,
        const QString &baseUrl,
        const QString &model,
        const QString &thinking);

private:
    void populateRouteTable(const QString &preferredRouteId = QString());
    void updateEditor();
    void updateProviderEditor(bool preserveEdits = true);
    void updateActionState();
    void setStatus(const QString &message, const QString &kind = QStringLiteral("neutral"));
    const ModelRouteInfo *currentRoute() const;
    const ModelProviderInfo *providerById(const QString &providerId) const;
    QString routeModeLabel(const QString &mode) const;
    QString availabilityLabel(const QString &availability) const;
    QString capabilityLabel(const QStringList &capabilities) const;
    QString resolvedModelLabel(const ModelRouteInfo &route) const;

    Ui::ModelRouteDialog *ui;
    QList<ModelProviderInfo> providers;
    QList<ModelRouteInfo> routes;
    QString requestedRouteId;
    bool loading = false;
    bool applyingEditorState = false;
};

#endif // MODELROUTEDIALOG_H
