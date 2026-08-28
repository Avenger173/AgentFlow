#ifndef KNOWLEDGEANSWERDIALOG_H
#define KNOWLEDGEANSWERDIALOG_H

#include <QDialog>
#include <QJsonArray>
#include <QJsonObject>
#include <QList>

#include "backendclient.h"

QT_BEGIN_NAMESPACE
namespace Ui {
class KnowledgeAnswerDialog;
}
QT_END_NAMESPACE

class QTimer;
class TaskActivityIndicator;

// K3 的客户阅读工作台。资料库主页仍专注于材料和索引生命周期；这个可伸缩窗口只负责一件事：
// 围绕固定资料库提问、查看真实阶段、阅读已核验结论并按需展开来源。
class KnowledgeAnswerDialog : public QDialog
{
    Q_OBJECT

public:
    KnowledgeAnswerDialog(
        BackendClient *backendClient,
        const QString &knowledgeBaseId,
        const QString &knowledgeBaseName,
        QWidget *parent = nullptr);
    ~KnowledgeAnswerDialog() override;

signals:
    void openTaskHistoryRequested(const QString &taskId);

private:
    void startAnswer();
    void requestLatestResult();
    void handleAnswerResult(const KnowledgeAnswerTaskResult &result);
    void handleAnswerFailure(const QString &message);
    void showSources(const QJsonArray &sources);
    void showSelectedSource();
    void updateExecutionState(const QString &message, const QString &state = QStringLiteral("running"));
    void setInspectorVisible(bool visible);
    QString resultMarkdown(const KnowledgeAnswerTaskResult &result) const;
    QString sourceDetailMarkdown(const QJsonObject &source) const;

    Ui::KnowledgeAnswerDialog *ui;
    BackendClient *backendClient;
    QTimer *pollTimer;
    TaskActivityIndicator *activityIndicator;
    QString knowledgeBaseId;
    QString currentTaskId;
    QList<QJsonObject> currentSources;
    bool running = false;
    bool inspectorVisible = true;
};

#endif // KNOWLEDGEANSWERDIALOG_H
