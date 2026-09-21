#ifndef SPINBOXARROWSTYLE_H
#define SPINBOXARROWSTYLE_H

#include <QAbstractSpinBox>
#include <QPainter>
#include <QProxyStyle>
#include <QStyleFactory>
#include <QStyleOptionSpinBox>

class SpinBoxArrowStyle final : public QProxyStyle
{
public:
    explicit SpinBoxArrowStyle(QStyle *baseStyle = nullptr)
        : QProxyStyle(baseStyle)
    {
    }

    void drawComplexControl(ComplexControl control,
                            const QStyleOptionComplex *option,
                            QPainter *painter,
                            const QWidget *widget = nullptr) const override
    {
        QProxyStyle::drawComplexControl(control, option, painter, widget);
        if (control != CC_SpinBox || !painter) {
            return;
        }

        const auto *spinOption = qstyleoption_cast<const QStyleOptionSpinBox *>(option);
        if (!spinOption) {
            return;
        }

        const QColor background = (spinOption->state & State_Enabled) ? QColor("#F8FBFF")
                                                                        : QColor("#F1F5F9");
        const QColor arrow = (spinOption->state & State_Enabled) ? QColor("#3E5A7B")
                                                                   : QColor("#A7B5C7");
        const auto drawArrow = [this, spinOption, painter, background, arrow, widget](SubControl subControl,
                                                                                        bool pointsUp) {
            const QRect button = subControlRect(CC_SpinBox, spinOption, subControl, widget);
            if (button.width() < 8 || button.height() < 6) {
                return;
            }
            painter->save();
            painter->fillRect(button, background);
            painter->setPen(QPen(QColor("#DCE8F6"), 1));
            painter->drawLine(button.left(), button.top(), button.left(), button.bottom());
            if (subControl == SC_SpinBoxDown) {
                painter->drawLine(button.left(), button.top(), button.right(), button.top());
            }

            const QPoint center = button.center();
            QPolygon triangle;
            if (pointsUp) {
                triangle << QPoint(center.x() - 4, center.y() + 2)
                         << QPoint(center.x() + 4, center.y() + 2)
                         << QPoint(center.x(), center.y() - 3);
            } else {
                triangle << QPoint(center.x() - 4, center.y() - 2)
                         << QPoint(center.x() + 4, center.y() - 2)
                         << QPoint(center.x(), center.y() + 3);
            }
            painter->setPen(Qt::NoPen);
            painter->setBrush(arrow);
            painter->drawPolygon(triangle);
            painter->restore();
        };

        drawArrow(SC_SpinBoxUp, true);
        drawArrow(SC_SpinBoxDown, false);
    }
};

inline void installSpinBoxArrowStyle(QAbstractSpinBox *spinBox)
{
    if (!spinBox) {
        return;
    }

    // QProxyStyle owns its base style.  Never hand it QApplication's shared
    // style returned by spinBox->style(), otherwise destroying a temporary
    // dialog can also destroy the application's active style.
    const QString styleKey = spinBox->style() ? spinBox->style()->objectName() : QString();
    QStyle *baseStyle = styleKey.isEmpty() ? nullptr : QStyleFactory::create(styleKey);
    if (!baseStyle) {
        baseStyle = QStyleFactory::create(QStringLiteral("Fusion"));
    }

    auto *style = new SpinBoxArrowStyle(baseStyle);
    style->setParent(spinBox);
    spinBox->setStyle(style);
}

#endif // SPINBOXARROWSTYLE_H
