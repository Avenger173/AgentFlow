#ifndef BACKENDMANAGER_H
#define BACKENDMANAGER_H

#include <QElapsedTimer>
#include <QNetworkAccessManager>
#include <QObject>
#include <QPointer>
#include <QProcess>
#include <QTimer>
#include <QUrl>

class QNetworkReply;

// BackendManager 是 Qt 前端和本地 FastAPI 后端之间的“生命周期管家”。
// 它不处理业务 API，也不解析 Agent/聊天数据；这些仍由 BackendClient 负责。
// 这里的边界是：
// 1. 启动前先探测端口，避免和用户手动启动的后端抢占 8765。
// 2. 如果确实由本类启动了后端，Qt 退出时才负责关闭它。
// 3. 所有进程输出、健康状态、失败信息都用信号交给 UI，避免阻塞主线程。
class BackendManager : public QObject
{
    Q_OBJECT

public:
    explicit BackendManager(QObject *parent = nullptr);
    // 仅供本地生命周期测试注入回环服务地址；桌面程序始终使用默认 127.0.0.1:8765。
    BackendManager(const QUrl &baseUrl, QObject *parent = nullptr);
    ~BackendManager() override;

    // 确保后端可用。首次调用会进入：
    // 探测已有服务 -> 不可用则启动 uvicorn -> 轮询 /health。
    // 后续重复调用不会重复启动进程。
    void ensureStarted();

    // 仅由客户明确点击“重试后端”触发。它优先重新探测当前端口；若本类此前启动的
    // 进程仍在运行，不会为了重试而杀掉它；若不存在本类进程，才重新走受控启动链路。
    void retry();

    // 停止本类启动的后端进程。用户手动启动的后端不属于本类，不能在这里关闭。
    void stop();

    // 窗口退出路径使用的快速停止。它只处理本类启动的后端，并避免在 UI 线程长时间等待。
    void stopForFastExit();

    // /health 返回 ok 后才视为 ready；QProcess started 只代表进程已创建，不代表服务可用。
    bool isReady() const;

    // 用于区分“自动启动的后端”和“用户手动启动的后端”，决定退出时是否清理。
    bool ownsBackendProcess() const;

    // 与 BackendClient 保持同一个默认后端地址，后续如支持端口配置，这里要同步扩展。
    QUrl baseUrl() const;

signals:
    // 这些信号都只承载展示信息，不包含业务数据。
    // UI 收到 ready 后，再调用 BackendClient 去拉 /health、/api/agents 等业务接口。
    void starting(const QString &message);
    void ready(const QString &message);
    void unavailable(const QString &message);

    // 后端 stdout/stderr 已合并，按行传出；当前显示在状态区，后续可接日志面板。
    void outputReceived(const QString &line);

    // 只在本类管理的后端进程退出时发出，用于提醒 UI 状态失效。
    void stopped(const QString &message);

private:
    // 路径解析保持在 Qt 侧，方便以后打包时改为 backend exe 或便携目录。
    QString resolveBackendDir() const;
    QString resolvePythonProgram(const QString &backendDir) const;
    QString processErrorText(QProcess::ProcessError error) const;

    // 真正执行 QProcess::start 的位置。调用前应已经确认没有可复用的手动后端。
    void startProcess();

    // 异步访问 /health。这个函数同时承担“首次探测”和“启动后轮询”两种角色。
    void probeHealth();

    // 使用 single-shot timer 做轮询，避免在网络回调里递归触发请求。
    void scheduleProbe();

    // 启动期失败只发出一次客户可见状态，避免 QProcess 和 /health 同时失败时出现重复噪声。
    void reportUnavailable(const QString &message);

    // 读取后端日志并按行发给 UI。这里不做持久化，后续日志系统再接管。
    void handleProcessOutput();

    // stop/stopForFastExit 共用的进程关闭实现；等待时长由调用场景决定。
    void stopOwnedProcess(int gracefulWaitMs, int killWaitMs);

    // 本地后端进程对象。只有 processStartedByUs_ 为 true 时，它才代表需要清理的资源。
    QProcess backendProcess_;

    // 健康检查使用独立 QNetworkAccessManager，避免 BackendClient 未就绪时互相依赖。
    QNetworkAccessManager networkManager_;

    // 同一时刻只允许一条 /health 请求；客户点击重试时会取消旧探测，并忽略其迟到回调。
    QPointer<QNetworkReply> activeHealthReply_;

    // /health 重试定时器。single-shot 方式让每次请求完成后再安排下一次重试。
    QTimer probeTimer_;

    // 当前固定为 127.0.0.1:8765；后续模型、插件或多实例支持会把它变成配置项。
    QUrl baseUrl_;

    // 已解析出的后端目录和 Python 程序。启动失败时 UI 会用这些信息辅助定位。
    QString backendDir_;
    QString pythonProgram_;

    // 记录最近一次健康检查失败原因，最终超时时展示给用户。
    QString lastProbeError_;

    // 仅用于脱敏、面向客户的启动耗时提示；不记录文件、请求正文或凭据。
    QElapsedTimer startupElapsedTimer_;

    // 启动后健康检查最多重试次数；避免后端依赖缺失时无限轮询。
    int remainingProbeAttempts_ = 0;

    // 防止 UI 多次触发 ensureStarted 时重复启动或重复轮询。
    bool launchAttempted_ = false;

    // 同一次启动链路中，QProcess 和网络回调可能先后失败；此标记保证客户只收到一条终态。
    bool startupFailureReported_ = false;

    // 资源归属标记：true 表示后端是本类启动的，stop/destructor 才能关闭。
    bool processStartedByUs_ = false;

    // 健康检查状态，不等同于 QProcess 状态。
    bool ready_ = false;
};

#endif // BACKENDMANAGER_H
