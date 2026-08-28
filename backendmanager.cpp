#include "backendmanager.h"

#include <QCoreApplication>
#include <QDir>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QProcessEnvironment>

namespace {

QString firstExistingDirectory(const QStringList &candidates)
{
    // 按优先级返回第一个真实存在的目录。
    // 空字符串必须跳过，否则 QFileInfo("") 会退化成当前目录，导致 backendDir 误判。
    for (const QString &candidate : candidates) {
        if (candidate.trimmed().isEmpty()) {
            continue;
        }

        const QFileInfo info(candidate);
        if (info.exists() && info.isDir()) {
            return QDir::cleanPath(info.absoluteFilePath());
        }
    }
    return QString();
}

QString cleanOutputLine(QString line)
{
    // Uvicorn 在 Windows 控制台输出里可能带 \r，这里统一清掉再交给 UI。
    line = line.trimmed();
    return line.replace(QLatin1Char('\r'), QString());
}

} // namespace

BackendManager::BackendManager(QObject *parent)
    : BackendManager(QUrl(QStringLiteral("http://127.0.0.1:8765")), parent)
{
}

BackendManager::BackendManager(const QUrl &baseUrl, QObject *parent)
    : QObject(parent)
    , baseUrl_(baseUrl)
{
    probeTimer_.setSingleShot(true);
    // 健康检查是“请求完成后再安排下一次”的节奏，避免多个 /health 并发堆积。
    connect(&probeTimer_, &QTimer::timeout, this, &BackendManager::probeHealth);

    // QProcess 生命周期集中在这里：UI 只接收状态信号，不直接碰进程对象。
    connect(&backendProcess_, &QProcess::started, this, [this]() {
        emit starting(QStringLiteral("后端进程已启动，正在等待健康检查通过。"));
    });
    // stdout/stderr 已合并到同一个通道，所有输出都从 readyRead 统一读取。
    connect(&backendProcess_, &QProcess::readyRead, this, &BackendManager::handleProcessOutput);
    connect(&backendProcess_, &QProcess::errorOccurred, this, [this](QProcess::ProcessError error) {
        // 启动失败时要立即停止轮询；否则 UI 会同时看到“进程失败”和“健康检查超时”两类噪声。
        processStartedByUs_ = false;
        ready_ = false;
        reportUnavailable(processErrorText(error));
    });
    connect(&backendProcess_,
            qOverload<int, QProcess::ExitStatus>(&QProcess::finished),
            this,
            [this](int exitCode, QProcess::ExitStatus exitStatus) {
                // finished 只说明本类持有的 QProcess 退出。
                // 如果 ready_ 曾经为 true，说明服务运行后退出；否则更可能是启动阶段失败。
                processStartedByUs_ = false;
                if (ready_) {
                    emit stopped(QStringLiteral("后端进程已退出：code=%1 status=%2")
                                     .arg(exitCode)
                                     .arg(exitStatus == QProcess::NormalExit ? QStringLiteral("normal")
                                                                             : QStringLiteral("crash")));
                } else {
                    reportUnavailable(QStringLiteral("后端启动失败或提前退出：code=%1").arg(exitCode));
                }
            });
}

BackendManager::~BackendManager()
{
    // 析构时兜底清理，保证用户关闭 Qt 后不会留下本次自动启动的开发后端。
    stopForFastExit();
}

void BackendManager::ensureStarted()
{
    // ready_ 是健康检查结果，不是 QProcess 状态。用户手动后端和自动后端都可能让它为 true。
    if (ready_) {
        emit ready(QStringLiteral("后端已经就绪。"));
        return;
    }

    // 当前 MVP 只允许一次启动尝试，避免按钮/页面刷新导致重复开 uvicorn。
    // 后续做“重新连接”按钮时，可以在失败后重置这个标记。
    if (launchAttempted_) {
        return;
    }

    launchAttempted_ = true;
    startupFailureReported_ = false;
    startupElapsedTimer_.start();
    backendDir_ = resolveBackendDir();
    pythonProgram_ = resolvePythonProgram(backendDir_);

    // 首次探测只需要一次：如果端口已有健康后端，直接复用；
    // 如果失败，probeHealth 会进入 startProcess()。
    remainingProbeAttempts_ = 1;
    emit starting(QStringLiteral("正在检测本地后端 127.0.0.1:8765。"));

    // 先探测端口，避免用户已经手动启动后端时再开一个抢端口的 uvicorn。
    probeHealth();
}

void BackendManager::retry()
{
    // 已就绪时不重启已经可用的后端；这里只回报当前状态，避免“重试”意外中断客户任务。
    if (ready_) {
        emit ready(QStringLiteral("后端已经就绪。"));
        return;
    }

    // 旧健康探测的回调可能在 abort 后晚到。清空指针后，回调会识别为过期并直接丢弃。
    probeTimer_.stop();
    if (activeHealthReply_) {
        activeHealthReply_->abort();
        activeHealthReply_.clear();
    }

    lastProbeError_.clear();
    startupFailureReported_ = false;
    startupElapsedTimer_.start();

    if (processStartedByUs_ && backendProcess_.state() != QProcess::NotRunning) {
        // 自动启动的进程有机会只是慢于原超时窗口；先重新探测，不杀掉正在完成初始化的进程。
        launchAttempted_ = true;
        remainingProbeAttempts_ = 30;
        emit starting(QStringLiteral("正在重新检测已启动的本地后端。"));
        probeHealth();
        return;
    }

    // 没有本类管理的存活进程时，重新走“先探测端口，再按需启动”的原始安全路径。
    launchAttempted_ = false;
    ensureStarted();
}

void BackendManager::stop()
{
    stopOwnedProcess(3000, 1000);
}

void BackendManager::stopForFastExit()
{
    // 用户点窗口关闭时最在意的是“应用立刻消失、不要留下后端端口占用”。
    // Uvicorn 在 Windows Debug 环境下优雅 terminate 可能拖几秒；退出路径直接 kill，
    // 并只短等一小会儿让 QProcess 收到 finished，避免卡住主线程。
    stopOwnedProcess(0, 250);
}

void BackendManager::stopOwnedProcess(int gracefulWaitMs, int killWaitMs)
{
    probeTimer_.stop();

    // 关键安全边界：没有资源归属就不关闭进程。
    // 这保护了用户在终端里手动启动的 uvicorn，也保护了未来外部后端模式。
    if (!processStartedByUs_ || backendProcess_.state() == QProcess::NotRunning) {
        return;
    }

    // 只关闭本类启动的后端进程；如果用户手动启动了服务，这里不会触碰。
    // 普通 stop 先给后端正常退出机会；快速退出可把 gracefulWaitMs 设为 0，避免卡 UI。
    if (gracefulWaitMs > 0) {
        backendProcess_.terminate();
    }
    if (gracefulWaitMs <= 0 || !backendProcess_.waitForFinished(gracefulWaitMs)) {
        backendProcess_.kill();
        if (killWaitMs > 0) {
            backendProcess_.waitForFinished(killWaitMs);
        }
    }
    processStartedByUs_ = false;
}

bool BackendManager::isReady() const
{
    return ready_;
}

bool BackendManager::ownsBackendProcess() const
{
    return processStartedByUs_;
}

QUrl BackendManager::baseUrl() const
{
    return baseUrl_;
}

QString BackendManager::resolveBackendDir() const
{
    const QString envPath = QString::fromLocal8Bit(qgetenv("AGENTFLOW_BACKEND_DIR")).trimmed();
    const QString appDir = QCoreApplication::applicationDirPath();
    const QString currentDir = QDir::currentPath();

    // 开发期 exe 通常在 build/<kit> 或 build/codex-debug 下；发布期可能和 backend 同级。
    // 这里按“显式环境变量 -> 当前工作目录 -> exe 周边”逐步查找。
    // 保留 AGENTFLOW_BACKEND_DIR 是为了之后调试外置后端或打包后端目录。
    return firstExistingDirectory({
        envPath,
        QDir(currentDir).absoluteFilePath(QStringLiteral("backend")),
        QDir(appDir).absoluteFilePath(QStringLiteral("backend")),
        QDir(appDir).absoluteFilePath(QStringLiteral("../backend")),
        QDir(appDir).absoluteFilePath(QStringLiteral("../../backend"))
    });
}

QString BackendManager::resolvePythonProgram(const QString &backendDir) const
{
    // Python 选择顺序：
    // 1. AGENTFLOW_PYTHON：用户或打包脚本明确指定。
    // 2. backend/.venv/Scripts/python.exe：开发环境隔离依赖。
    // 3. PATH 中的 python：兜底，适合本机已配置 Python 的情况。
    const QString envPython = QString::fromLocal8Bit(qgetenv("AGENTFLOW_PYTHON")).trimmed();
    if (!envPython.isEmpty()) {
        return envPython;
    }

    const QString venvPython = QDir(backendDir).absoluteFilePath(QStringLiteral(".venv/Scripts/python.exe"));
    if (QFileInfo::exists(venvPython)) {
        return QDir::toNativeSeparators(venvPython);
    }

    return QStringLiteral("python");
}

QString BackendManager::processErrorText(QProcess::ProcessError error) const
{
    // QProcess 错误枚举比较底层，这里转成用户能直接理解的中文状态。
    switch (error) {
    case QProcess::FailedToStart:
        return QStringLiteral("后端进程启动失败，请检查 Python 或依赖是否可用。");
    case QProcess::Crashed:
        return QStringLiteral("后端进程异常退出。");
    case QProcess::Timedout:
        return QStringLiteral("后端进程操作超时。");
    case QProcess::WriteError:
        return QStringLiteral("向后端进程写入数据失败。");
    case QProcess::ReadError:
        return QStringLiteral("读取后端进程输出失败。");
    case QProcess::UnknownError:
    default:
        return QStringLiteral("后端进程出现未知错误。");
    }
}

void BackendManager::startProcess()
{
    // 没有后端目录时不要尝试启动 python，否则错误会变成难懂的 uvicorn import 失败。
    if (backendDir_.isEmpty()) {
        reportUnavailable(QStringLiteral("未找到 backend 目录，请确认程序从项目目录或打包目录启动。"));
        return;
    }

    // 如果 QProcess 已经在启动/运行状态，直接返回，避免重复调用 start。
    if (backendProcess_.state() != QProcess::NotRunning) {
        return;
    }

    // 强制 Python 以 UTF-8 输出，配合 Qt 的 fromUtf8 读取，避免中文日志乱码。
    QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
    environment.insert(QStringLiteral("PYTHONUTF8"), QStringLiteral("1"));
    environment.insert(QStringLiteral("PYTHONIOENCODING"), QStringLiteral("utf-8"));

    backendProcess_.setProcessEnvironment(environment);
    // 工作目录必须指向 backend/，这样 uvicorn 才能 import main:app。
    backendProcess_.setWorkingDirectory(backendDir_);
    // 先合并 stdout/stderr，MVP 阶段 UI 只需要一条启动日志流。
    backendProcess_.setProcessChannelMode(QProcess::MergedChannels);

    // MVP 仍启动开发形态的 FastAPI。后续 PyInstaller 打包后，只需要替换这里的 program/args。
    const QStringList arguments = {
        QStringLiteral("-m"),
        QStringLiteral("uvicorn"),
        QStringLiteral("main:app"),
        QStringLiteral("--host"),
        QStringLiteral("127.0.0.1"),
        QStringLiteral("--port"),
        QStringLiteral("8765")
    };

    // 先标记资源归属再 start：即使后续健康检查失败，stop/destructor 也知道它是本类启动的。
    processStartedByUs_ = true;
    // Uvicorn 冷启动可能需要一点时间；30 * 500ms 约 15 秒，足够覆盖普通依赖加载。
    remainingProbeAttempts_ = 30;
    emit starting(QStringLiteral("正在启动后端：%1 -m uvicorn main:app").arg(pythonProgram_));
    backendProcess_.start(pythonProgram_, arguments);
    scheduleProbe();
}

void BackendManager::probeHealth()
{
    // 这个函数同时服务两种场景：
    // 1. 首次探测：判断用户是否已经手动启动后端。
    // 2. 自动启动后轮询：等待 uvicorn 真正开始监听端口。
    // 单飞行保护：启动、材料选择和客户重试都可能调用本方法，但同一时刻不需要多条 /health。
    if (activeHealthReply_) {
        return;
    }

    QNetworkRequest request(QUrl(baseUrl_.toString() + QStringLiteral("/health")));
    request.setTransferTimeout(1500);

    QNetworkReply *reply = networkManager_.get(request);
    activeHealthReply_ = reply;
    connect(reply, &QNetworkReply::finished, this, [this, reply]() {
        // QNetworkReply 必须在 finished 后 deleteLater，避免异步对象泄漏。
        reply->deleteLater();

        // retry() 可能已经取消并替换了这一轮探测；旧回调不能继续启动进程或覆盖新状态。
        if (activeHealthReply_.data() != reply) {
            return;
        }
        activeHealthReply_.clear();

        // QProcess 的失败有可能先一步到达。后续的网络回调只做资源收尾，不能复活启动链路。
        if (startupFailureReported_) {
            return;
        }

        const QVariant statusAttribute = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute);
        const int httpStatus = statusAttribute.isValid() ? statusAttribute.toInt() : 0;
        const bool receivedHttpResponse = httpStatus >= 100;

        if (reply->error() == QNetworkReply::NoError) {
            const QJsonDocument document = QJsonDocument::fromJson(reply->readAll());
            const QJsonObject payload = document.object();
            if (payload.value(QStringLiteral("status")).toString() == QStringLiteral("ok")) {
                // 只有 /health 明确返回 ok，才通知 UI 后端可用。
                // 单纯 QProcess::started 不能证明 FastAPI 已完成启动。
                ready_ = true;
                probeTimer_.stop();

                const QString service = payload.value(QStringLiteral("service")).toString(QStringLiteral("AgentFlow Backend"));
                const QString version = payload.value(QStringLiteral("version")).toString(QStringLiteral("unknown"));
                const qint64 elapsedMs = startupElapsedTimer_.isValid() ? startupElapsedTimer_.elapsed() : -1;
                startupElapsedTimer_.invalidate();
                emit ready(elapsedMs >= 0
                               ? QStringLiteral("%1 %2 已就绪（启动检查 %3 秒）")
                                     .arg(service, version)
                                     .arg(QString::number(static_cast<double>(elapsedMs) / 1000.0, 'f', 1))
                               : QStringLiteral("%1 %2 已就绪").arg(service, version));
                return;
            }
            lastProbeError_ = QStringLiteral("健康检查返回非 ok 状态。");
        } else {
            lastProbeError_ = reply->errorString();
        }

        // 收到任何 HTTP 响应都说明 8765 确实有服务在监听。若它没有返回 AgentFlow 的健康协议，
        // 再启动一个 uvicorn 只会制造端口冲突；应把处理权交还给客户，而不是盲目重试。
        if (receivedHttpResponse) {
            reportUnavailable(
                QStringLiteral("端口 127.0.0.1:8765 已有服务，但 /health 未返回 AgentFlow 就绪状态（HTTP %1）。"
                               "请关闭占用端口的服务后点击“重试后端”。")
                    .arg(httpStatus));
            return;
        }

        // 如果不是本类启动的后端，说明这是首次探测失败；
        // 现在才进入自动启动，避免和用户已有后端抢端口。
        if (!processStartedByUs_) {
            startProcess();
            return;
        }

        // 已经启动过后端但 /health 未就绪：继续按固定间隔轮询。
        if (remainingProbeAttempts_ > 0) {
            --remainingProbeAttempts_;
            scheduleProbe();
            return;
        }

        // 到这里说明进程可能启动了，但 FastAPI 没有在预期时间内对外服务。
        reportUnavailable(QStringLiteral("后端健康检查超时：%1").arg(lastProbeError_));
    });
}

void BackendManager::scheduleProbe()
{
    // single-shot 定时器只保留一个待执行探测，避免网络慢时叠出多条请求。
    if (!probeTimer_.isActive()) {
        probeTimer_.start(500);
    }
}

void BackendManager::reportUnavailable(const QString &message)
{
    // 同一次启动里 QProcess 的启动错误、端口冲突和 /health 超时可能同时发生；客户只需知道
    // 可操作的第一条终态。下一次明确 retry() 会重置该标记并重新开始一条独立检查链路。
    if (startupFailureReported_) {
        return;
    }

    startupFailureReported_ = true;
    ready_ = false;
    probeTimer_.stop();

    const qint64 elapsedMs = startupElapsedTimer_.isValid() ? startupElapsedTimer_.elapsed() : -1;
    startupElapsedTimer_.invalidate();
    const QString elapsedSuffix = elapsedMs >= 0
        ? QStringLiteral("（启动检查 %1 秒）")
              .arg(QString::number(static_cast<double>(elapsedMs) / 1000.0, 'f', 1))
        : QString();
    emit unavailable(message + elapsedSuffix);
}

void BackendManager::handleProcessOutput()
{
    // 这里一次可能读到多行 uvicorn 输出。按行拆分后发给 UI，
    // 可以避免状态区显示一整块不可读的日志。
    const QString output = QString::fromUtf8(backendProcess_.readAll());
    const QStringList lines = output.split(QLatin1Char('\n'), Qt::SkipEmptyParts);
    for (const QString &line : lines) {
        const QString cleaned = cleanOutputLine(line);
        if (!cleaned.isEmpty()) {
            emit outputReceived(cleaned);
        }
    }
}
