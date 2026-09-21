#include "mediaimagecanvas.h"

#include <QMouseEvent>
#include <QPainter>
#include <QPalette>

#include <cmath>

MediaImageCanvas::MediaImageCanvas(QWidget *parent)
    : QWidget(parent)
{
    setMinimumSize(360, 240);
    setMouseTracking(true);
    updateCursor();
}

void MediaImageCanvas::setImage(const QPixmap &newImage)
{
    image = newImage;
    placeholderText.clear();
    selection = normalizedSelection(selection);
    updateCursor();
    update();
}

void MediaImageCanvas::clearImage(const QString &message)
{
    image = QPixmap();
    placeholderText = message;
    selection = {};
    selecting = false;
    updateCursor();
    update();
}

void MediaImageCanvas::setSelectionMode(SelectionMode mode)
{
    if (selectionMode == mode) {
        return;
    }
    selectionMode = mode;
    selecting = false;
    updateCursor();
    update();
}

void MediaImageCanvas::setSelection(const QRect &newSelection)
{
    const QRect boundedSelection = normalizedSelection(newSelection);
    if (selection == boundedSelection) {
        return;
    }
    selection = boundedSelection;
    update();
}

void MediaImageCanvas::paintEvent(QPaintEvent *event)
{
    Q_UNUSED(event);

    QPainter painter(this);
    painter.fillRect(rect(), palette().brush(QPalette::Base));

    if (image.isNull()) {
        painter.setPen(palette().color(QPalette::Text));
        painter.drawText(rect(), Qt::AlignCenter | Qt::TextWordWrap, placeholderText);
        return;
    }

    const QRect target = imageDisplayRect();
    painter.drawPixmap(target, image);
    if (selectionMode == SelectionMode::None || selection.isEmpty()) {
        return;
    }

    const qreal scaleX = static_cast<qreal>(target.width()) / image.width();
    const qreal scaleY = static_cast<qreal>(target.height()) / image.height();
    const QRectF displayedSelection(
        target.x() + selection.x() * scaleX,
        target.y() + selection.y() * scaleY,
        selection.width() * scaleX,
        selection.height() * scaleY);
    const QColor accent = selectionMode == SelectionMode::Crop
                              ? QColor(37, 99, 235)
                              : QColor(13, 148, 136);
    QColor fill = accent;
    fill.setAlpha(50);
    painter.fillRect(displayedSelection, fill);
    QPen pen(accent, 2.0);
    pen.setStyle(Qt::DashLine);
    painter.setPen(pen);
    painter.drawRect(displayedSelection);
}

void MediaImageCanvas::mousePressEvent(QMouseEvent *event)
{
    if (event->button() != Qt::LeftButton || image.isNull()
        || selectionMode == SelectionMode::None || !imageDisplayRect().contains(event->position().toPoint())) {
        QWidget::mousePressEvent(event);
        return;
    }

    selecting = true;
    dragStart = clampToImage(event->position().toPoint());
    selection = imageRectForDrag(dragStart, dragStart);
    emit selectionChanged(selection);
    update();
    event->accept();
}

void MediaImageCanvas::mouseMoveEvent(QMouseEvent *event)
{
    if (!selecting) {
        QWidget::mouseMoveEvent(event);
        return;
    }

    const QRect nextSelection = imageRectForDrag(dragStart, clampToImage(event->position().toPoint()));
    if (selection != nextSelection) {
        selection = nextSelection;
        emit selectionChanged(selection);
        update();
    }
    event->accept();
}

void MediaImageCanvas::mouseReleaseEvent(QMouseEvent *event)
{
    if (!selecting || event->button() != Qt::LeftButton) {
        QWidget::mouseReleaseEvent(event);
        return;
    }

    selecting = false;
    const QRect nextSelection = imageRectForDrag(dragStart, clampToImage(event->position().toPoint()));
    if (selection != nextSelection) {
        selection = nextSelection;
        emit selectionChanged(selection);
    }
    update();
    event->accept();
}

QRect MediaImageCanvas::imageDisplayRect() const
{
    if (image.isNull() || contentsRect().isEmpty()) {
        return {};
    }
    const QRect content = contentsRect();
    const QSize scaledSize = image.size().scaled(content.size(), Qt::KeepAspectRatio);
    return QRect(content.left() + (content.width() - scaledSize.width()) / 2,
                 content.top() + (content.height() - scaledSize.height()) / 2,
                 scaledSize.width(),
                 scaledSize.height());
}

QPoint MediaImageCanvas::clampToImage(const QPoint &position) const
{
    const QRect target = imageDisplayRect();
    return QPoint(qBound(target.left(), position.x(), target.right()),
                  qBound(target.top(), position.y(), target.bottom()));
}

QRect MediaImageCanvas::imageRectForDrag(const QPoint &start, const QPoint &end) const
{
    const QRect target = imageDisplayRect();
    if (target.isEmpty() || image.isNull()) {
        return {};
    }

    const int left = qMin(start.x(), end.x());
    const int right = qMax(start.x(), end.x());
    const int top = qMin(start.y(), end.y());
    const int bottom = qMax(start.y(), end.y());
    const auto mapStart = [](int coordinate, int origin, int sourceSize, int displaySize) {
        return static_cast<int>(std::floor(static_cast<double>(coordinate - origin) * sourceSize / displaySize));
    };
    const auto mapEnd = [](int coordinate, int origin, int sourceSize, int displaySize) {
        return static_cast<int>(std::ceil(static_cast<double>(coordinate - origin + 1) * sourceSize / displaySize));
    };
    const int imageLeft = qBound(0, mapStart(left, target.left(), image.width(), target.width()), image.width() - 1);
    const int imageTop = qBound(0, mapStart(top, target.top(), image.height(), target.height()), image.height() - 1);
    const int imageRight = qBound(imageLeft + 1,
                                  mapEnd(right, target.left(), image.width(), target.width()),
                                  image.width());
    const int imageBottom = qBound(imageTop + 1,
                                   mapEnd(bottom, target.top(), image.height(), target.height()),
                                   image.height());
    return QRect(imageLeft, imageTop, imageRight - imageLeft, imageBottom - imageTop);
}

QRect MediaImageCanvas::normalizedSelection(const QRect &newSelection) const
{
    if (image.isNull() || newSelection.isEmpty()) {
        return {};
    }
    const int left = qBound(0, newSelection.x(), image.width() - 1);
    const int top = qBound(0, newSelection.y(), image.height() - 1);
    const int width = qBound(1, newSelection.width(), image.width() - left);
    const int height = qBound(1, newSelection.height(), image.height() - top);
    return QRect(left, top, width, height);
}

void MediaImageCanvas::updateCursor()
{
    const bool canSelect = !image.isNull() && selectionMode != SelectionMode::None;
    setCursor(canSelect ? Qt::CrossCursor : Qt::ArrowCursor);
}
