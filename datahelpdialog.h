#ifndef DATAHELPDIALOG_H
#define DATAHELPDIALOG_H

#include <QDialog>

QT_BEGIN_NAMESPACE
namespace Ui {
class DataHelpDialog;
}
QT_END_NAMESPACE

// 数据工作台帮助采用独立 Designer 页面，避免把长说明塞回主工作区或依赖临时动态控件。
class DataHelpDialog final : public QDialog
{
    Q_OBJECT

public:
    explicit DataHelpDialog(QWidget *parent = nullptr);
    ~DataHelpDialog() override;

private:
    Ui::DataHelpDialog *ui;
};

#endif // DATAHELPDIALOG_H
