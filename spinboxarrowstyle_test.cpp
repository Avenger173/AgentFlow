#include "spinboxarrowstyle.h"

#include <QApplication>
#include <QSpinBox>
#include <QtTest>

class SpinBoxArrowStyleTest final : public QObject
{
    Q_OBJECT

private slots:
    void temporarySpinBoxDoesNotDestroyApplicationStyle();
};

void SpinBoxArrowStyleTest::temporarySpinBoxDoesNotDestroyApplicationStyle()
{
    QStyle *const applicationStyle = QApplication::style();
    QVERIFY(applicationStyle);
    const QString applicationStyleName = applicationStyle->objectName();

    {
        QSpinBox temporarySpinBox;
        installSpinBoxArrowStyle(&temporarySpinBox);

        auto *const proxyStyle = dynamic_cast<SpinBoxArrowStyle *>(temporarySpinBox.style());
        QVERIFY(proxyStyle);
        QVERIFY(proxyStyle->baseStyle());
        QVERIFY(proxyStyle->baseStyle() != applicationStyle);
        QCOMPARE(QApplication::style(), applicationStyle);
    }

    QCOMPARE(QApplication::style(), applicationStyle);
    QCOMPARE(QApplication::style()->objectName(), applicationStyleName);

    QSpinBox subsequentSpinBox;
    QCOMPARE(subsequentSpinBox.style(), applicationStyle);
}

QTEST_MAIN(SpinBoxArrowStyleTest)

#include "spinboxarrowstyle_test.moc"
