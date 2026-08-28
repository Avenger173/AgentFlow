#ifndef DATATRANSFORMATIONDIALOG_H
#define DATATRANSFORMATIONDIALOG_H

#include <QDialog>
#include <QJsonArray>
#include <QJsonObject>

QT_BEGIN_NAMESPACE
namespace Ui {
class DataTransformationDialog;
}
QT_END_NAMESPACE

// 字段加工采用独立的 Designer 对话框：左侧只选择任务类型，右侧只显示该任务真正需要的字段。
// 它不持有后端、文件路径或 DataFrame；确认“生成预览”时只向 MainWindow 交付受限 JSON 请求。
class DataTransformationDialog final : public QDialog
{
    Q_OBJECT

public:
    explicit DataTransformationDialog(
        const QJsonObject &analysisPreview,
        const QString &defaultGoal,
        QWidget *parent = nullptr);
    ~DataTransformationDialog() override;

signals:
    void previewRequested(const QJsonObject &request);

private:
    void addOperation(const QString &title, const QString &subtitle, const QString &operationType);
    void refreshOperationUi();
    void rebuildFieldChoices();
    void addCurrentOperationToQueue();
    void removeSelectedQueuedOperation();
    void refreshQueuedOperationsUi();
    QJsonObject buildCurrentOperationRequest(QString *errorMessage = nullptr) const;
    QString selectedOperationType() const;
    QString typeDisplayName(const QString &inferredType) const;
    bool columnMatchesType(const QJsonObject &column, const QString &requiredType) const;

    Ui::DataTransformationDialog *ui;
    QJsonObject datasetProfile;
    QJsonArray columns;
    QJsonArray queuedOperations;
    QString defaultGoal;
};

#endif // DATATRANSFORMATIONDIALOG_H
