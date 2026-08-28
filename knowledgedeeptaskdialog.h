#ifndef KNOWLEDGEEPTASKDIALOG_H
#define KNOWLEDGEEPTASKDIALOG_H

#include <QDialog>
#include <QJsonObject>
#include <QList>

#include "backendclient.h"

QT_BEGIN_NAMESPACE
namespace Ui {
class KnowledgeDeepTaskDialog;
}
QT_END_NAMESPACE

class QTimer;
class TaskActivityIndicator;

// K4 深度任务的独立阅读工作台。资料库主页只管理材料和索引；本窗口负责一个冻结范围内的
// 长任务生命周期、可读部分结果和客户确认后的正式报告，不展示父块正文或本机绝对路径。
class KnowledgeDeepTaskDialog : public QDialog
{
    Q_OBJECT

public:
    KnowledgeDeepTaskDialog(
        BackendClient *backendClient,
        const QString &knowledgeBaseId,
        const QString &knowledgeBaseName,
        const QList<KnowledgeDocumentInfo> &knowledgeDocuments,
        QWidget *parent = nullptr);
    ~KnowledgeDeepTaskDialog() override;

    // 从总指挥的关联产物进入时复用同一工作台，只读取既有 K4 任务；不会重新创建范围或任务。
    void openExistingTask(const QString &taskId);

signals:
    void openTaskHistoryRequested(const QString &taskId);

private:
    void startTask();
    void requestLatestResult();
    void requestControl(const QString &action);
    void handleTaskResult(const KnowledgeDeepTaskResult &result);
    void handleTaskFailure(const QString &message);
    void showScope(const QJsonObject &scope);
    void showSelectedScopeUnit();
    void updateExecutionState(const QString &message, const QString &state = QStringLiteral("running"));
    void updateControlState(const QString &status, bool reportAvailable);
    void setInspectorVisible(bool visible);
    void updateTaskKindUi();
    void confirmReportExport();
    QString taskKind() const;
    QStringList selectedComparisonDocumentIds() const;
    QString scopeUnitLabel(const QJsonObject &unit) const;
    QString scopeUnitMarkdown(const QJsonObject &unit) const;
    QString mapUnitLabel(const QString &mapUnitId) const;
    QString resultMarkdown(const KnowledgeDeepTaskResult &result) const;
    QString coverageMarkdown(const KnowledgeDeepTaskResult &result) const;
    QString comparisonTableMarkdown(const KnowledgeDeepTaskResult &result) const;

    Ui::KnowledgeDeepTaskDialog *ui;
    BackendClient *backendClient;
    QTimer *pollTimer;
    TaskActivityIndicator *activityIndicator;
    QString knowledgeBaseId;
    QString currentTaskId;
    QString currentStatus;
    QList<KnowledgeDocumentInfo> comparisonDocuments;
    QList<QJsonObject> currentScopeUnits;
    bool taskRunning = false;
    bool inspectorVisible = true;
    bool existingTaskMode = false;
};

#endif // KNOWLEDGEEPTASKDIALOG_H
