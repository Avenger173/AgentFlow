#include "datahelpdialog.h"

#include "ui_datahelpdialog.h"

#include <QDialogButtonBox>
#include <QIcon>

DataHelpDialog::DataHelpDialog(QWidget *parent)
    : QDialog(parent)
    , ui(new Ui::DataHelpDialog)
{
    ui->setupUi(this);
    ui->helpIcon->setPixmap(QIcon(QStringLiteral(":/icons/data.svg")).pixmap(34, 34));
    connect(ui->buttonBox, &QDialogButtonBox::rejected, this, &QDialog::reject);
}

DataHelpDialog::~DataHelpDialog()
{
    delete ui;
}
