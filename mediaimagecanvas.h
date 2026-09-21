#ifndef MEDIAIMAGECANVAS_H
#define MEDIAIMAGECANVAS_H

#include <QPixmap>
#include <QRect>
#include <QWidget>

class QMouseEvent;
class QPaintEvent;

// 预览画布只负责显示与坐标映射；实际像素变换仍由后端版本化接口执行。
class MediaImageCanvas : public QWidget
{
    Q_OBJECT

public:
    enum class SelectionMode {
        None,
        Crop,
        Mask,
    };

    explicit MediaImageCanvas(QWidget *parent = nullptr);

    void setImage(const QPixmap &image);
    void clearImage(const QString &message);
    void setSelectionMode(SelectionMode mode);
    void setSelection(const QRect &selection);

signals:
    void selectionChanged(const QRect &selection);

protected:
    void paintEvent(QPaintEvent *event) override;
    void mousePressEvent(QMouseEvent *event) override;
    void mouseMoveEvent(QMouseEvent *event) override;
    void mouseReleaseEvent(QMouseEvent *event) override;

private:
    QRect imageDisplayRect() const;
    QPoint clampToImage(const QPoint &position) const;
    QRect imageRectForDrag(const QPoint &start, const QPoint &end) const;
    QRect normalizedSelection(const QRect &selection) const;
    void updateCursor();

    QPixmap image;
    QString placeholderText;
    QRect selection;
    QPoint dragStart;
    SelectionMode selectionMode = SelectionMode::None;
    bool selecting = false;
};

#endif // MEDIAIMAGECANVAS_H
