#ifndef DISPATCHMATERIALDIALOG_H
#define DISPATCHMATERIALDIALOG_H

#include <QDialog>

#include "backendclient.h"

QT_BEGIN_NAMESPACE
namespace Ui {
class DispatchMaterialDialog;
}
QT_END_NAMESPACE

class TaskActivityIndicator;

// 调度台材料选择器只负责本次任务的只读范围。它不导入文件、不解析正文，也不决定 Agent
// 路由；MainWindow 仍持有稳定引用并在发送后清空，避免材料在会话之间被隐式复用。
class DispatchMaterialDialog : public QDialog
{
    Q_OBJECT

public:
    explicit DispatchMaterialDialog(QWidget *parent = nullptr);
    ~DispatchMaterialDialog() override;

    void setDocuments(const QList<WorkspaceDocumentInfo> &documents);
    void setKnowledgeBases(const QList<KnowledgeBaseInfo> &knowledgeBases);
    void setDatasets(const QList<DataDatasetInfo> &datasets);
    void setSelections(const QString &documentRef,
                       const QString &knowledgeBaseId,
                       const QString &datasetRef);
    void setCatalogStatus(const QString &message, const QString &kind = QStringLiteral("neutral"));

signals:
    void refreshRequested();
    void materialsApplied(const QString &documentRef,
                          const QString &knowledgeBaseId,
                          const QString &datasetRef);

private:
    void populateDocumentCombo();
    void populateKnowledgeCombo();
    void populateDatasetCombo();
    void updateApplyState();

    Ui::DispatchMaterialDialog *ui;
    QList<WorkspaceDocumentInfo> documents;
    QList<KnowledgeBaseInfo> knowledgeBases;
    QList<DataDatasetInfo> datasets;
    QString selectedDocumentRef;
    QString selectedKnowledgeBaseId;
    QString selectedDatasetRef;
    // 目录同步是短异步操作，也要给出真实运行态；不以静态文字猜测任务是否仍在执行。
    TaskActivityIndicator *catalogActivityIndicator = nullptr;
    bool applyingState = false;
};

#endif // DISPATCHMATERIALDIALOG_H
