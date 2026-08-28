#include "taskactivityindicator.h"

#include <QColor>
#include <QPainter>
#include <QPaintEvent>
#include <QTimer>

TaskActivityIndicator::TaskActivityIndicator(QWidget *parent)
    : QWidget(parent)
    , animationTimer(new QTimer(this))
{
    // 固定尺寸避免状态变化时挤动相邻按钮或标签；仅在真实运行阶段启动定时器，不产生空闲重绘。
    setFixedSize(18, 18);
    setAccessibleName(QStringLiteral("任务活动状态"));
    setToolTip(QStringLiteral("当前没有运行中的任务"));
    animationTimer->setInterval(85);
    connect(animationTimer, &QTimer::timeout, this, [this]() {
        phase_ = (phase_ + 1) % 12;
        update();
    });
}

bool TaskActivityIndicator::isRunning() const
{
    return running_;
}

void TaskActivityIndicator::setRunning(bool running)
{
    if (running_ == running) {
        return;
    }

    running_ = running;
    if (running_) {
        animationTimer->start();
        setToolTip(QStringLiteral("任务正在执行"));
    } else {
        animationTimer->stop();
        setToolTip(QStringLiteral("当前没有运行中的任务"));
    }
    update();
}

void TaskActivityIndicator::paintEvent(QPaintEvent *event)
{
    Q_UNUSED(event)

    QPainter painter(this);
    painter.setRenderHint(QPainter::Antialiasing, true);
    painter.translate(width() / 2.0, height() / 2.0);

    constexpr int dotCount = 12;
    constexpr qreal radius = 6.0;
    constexpr qreal dotRadius = 1.45;
    const QColor activeColor(QStringLiteral("#2563EB"));
    const QColor idleColor(QStringLiteral("#A7B8D0"));
    painter.setPen(Qt::NoPen);
    for (int index = 0; index < dotCount; ++index) {
        const int distance = (index - phase_ + dotCount) % dotCount;
        const qreal opacity = running_ ? 0.16 + (1.0 - distance / qreal(dotCount)) * 0.84 : 0.58;
        QColor color = running_ ? activeColor : idleColor;
        color.setAlphaF(opacity);
        painter.setBrush(color);
        painter.save();
        painter.rotate(index * (360.0 / dotCount));
        painter.drawEllipse(QPointF(0, -radius), dotRadius, dotRadius);
        painter.restore();
    }
}
