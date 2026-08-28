#pragma once

#include <QWidget>

class QTimer;

// 统一的轻量任务活动指示器：运行时旋转，进入暂停/完成/失败等终态后停止。
// 它不根据阶段文案猜测状态，调用方只能用后端返回的真实任务状态驱动 setRunning。
class TaskActivityIndicator : public QWidget
{
    Q_OBJECT
    Q_PROPERTY(bool running READ isRunning WRITE setRunning)

public:
    explicit TaskActivityIndicator(QWidget *parent = nullptr);

    bool isRunning() const;
    void setRunning(bool running);

protected:
    void paintEvent(QPaintEvent *event) override;

private:
    QTimer *animationTimer;
    bool running_ = false;
    int phase_ = 0;
};
