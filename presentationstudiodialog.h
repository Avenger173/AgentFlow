#ifndef PRESENTATIONSTUDIODIALOG_H
#define PRESENTATIONSTUDIODIALOG_H

#include <QDialog>

#include "backendclient.h"

QT_BEGIN_NAMESPACE
namespace Ui {
class PresentationStudioDialog;
}
QT_END_NAMESPACE

class QTimer;
class QComboBox;
class QLabel;

// PPT 制作 V2 的独立工作台。主文档页只负责入口；这里负责最小输入、真实阶段、计划阅读、
// 明确导出和历史定位，避免把长计划与交付操作挤进材料选择区。
class PresentationStudioDialog : public QDialog
{
    Q_OBJECT

public:
    explicit PresentationStudioDialog(BackendClient *backendClient, QWidget *parent = nullptr);
    ~PresentationStudioDialog() override;
    // 调度台的主题创作引导只预填客户已经发送的文本，不自动发起模型请求或导出文件。
    void setInitialGoal(const QString &goal);
    // 调度台对“帮我制作 PPT”的明确请求走低摩擦直出：自动生成计划并导出内置版式，
    // 外部图片与联网资料仍不会被静默打开；独立窗口只展示真实进度和最终交付。
    void startDirectGeneration(const QString &goal);

signals:
    void openTaskHistoryRequested(const QString &taskId);
    void directGenerationProgress(const QString &message);
    void directGenerationCompleted(const PresentationExportResult &result);
    void directGenerationFailed(const QString &message);

private:
    void startPlanning();
    void requestLatestPlan();
    void handlePlanReceived(const PresentationStudioPlanResult &result);
    void handlePlanFailed(const QString &message);
    void exportPresentation();
    void handlePresentationExported(const PresentationExportResult &result);
    void handlePresentationExportFailed(const QString &message);
    void dispatchPresentationExport();
    void setPlanningState(const QString &message);
    void setReadyState(const QString &message);
    void updatePlanPreview();
    QString formatPlanHtml() const;
    QString suggestedFilename() const;
    QString visualAssetProvider() const;

    Ui::PresentationStudioDialog *ui;
    BackendClient *backendClient;
    QTimer *pollTimer;
    QLabel *visualAssetProviderLabel = nullptr;
    QComboBox *visualAssetProviderCombo = nullptr;
    QString currentTaskId;
    QString currentPlanId;
    PresentationStudioPlanResult currentPlan;
    bool planning = false;
    bool exporting = false;
    bool directGeneration = false;
};

#endif // PRESENTATIONSTUDIODIALOG_H
