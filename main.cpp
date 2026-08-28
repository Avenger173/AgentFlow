#include "mainwindow.h"

#include <QApplication>
#include <QStyleFactory>

int main(int argc, char *argv[])
{
    QApplication app(argc, argv);
    // AgentFlow 的视觉主要由 mainwindow.ui 内的 QSS 接管。
    // Qt 6.11 的 modern Windows style 插件在 Debug 环境下曾在窗口初始化期触发崩溃，
    // 固定到 Fusion 可以减少系统样式插件差异，让用户打开/关闭程序的路径更稳定。
    QApplication::setStyle(QStyleFactory::create(QStringLiteral("Fusion")));

    // Debug 构建开启 Run-Time Checks 时，析构庞大的 Qt Designer widget 树曾在退出期触发
    // CRT heap corruption。MainWindow::closeEvent 会先停止本窗口启动的后端并触发 app quit；
    // 主窗口本身交给进程退出时由操作系统回收，优先保证用户关闭程序时不弹运行时错误。
    auto *window = new MainWindow();
    // 调度台、资料库和数据看板都是信息密度较高的工作界面。默认最大化保留 Windows
    // 标题栏与常规窗口行为，同时避免用户每次启动都先手动放大才看得完整页面。
    window->showMaximized();

    return app.exec();
}
