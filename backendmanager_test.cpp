#include "backendmanager.h"

#include <QSignalSpy>
#include <QHostAddress>
#include <QTcpServer>
#include <QTcpSocket>
#include <QtTest>

#include <utility>

namespace {

class HttpFixtureServer final : public QTcpServer
{
public:
    explicit HttpFixtureServer(QByteArray response, QObject *parent = nullptr)
        : QTcpServer(parent)
        , response_(std::move(response))
    {
        connect(this, &QTcpServer::newConnection, this, [this]() { servePendingConnections(); });
    }

    bool start()
    {
        return listen(QHostAddress::LocalHost, 0);
    }

private:
    void servePendingConnections()
    {
        while (hasPendingConnections()) {
            QTcpSocket *socket = nextPendingConnection();
            connect(socket, &QTcpSocket::readyRead, socket, [socket, response = response_]() {
                // 请求内容只用于确认客户端已连接；夹具从不记录 URL、正文或任何客户数据。
                socket->readAll();
                socket->write(response);
                socket->disconnectFromHost();
            });
            connect(socket, &QTcpSocket::disconnected, socket, &QObject::deleteLater);
        }
    }

    QByteArray response_;
};

QByteArray httpResponse(int statusCode, const QByteArray &body)
{
    const QByteArray reason = statusCode == 200 ? QByteArrayLiteral("OK") : QByteArrayLiteral("Not Found");
    return QByteArrayLiteral("HTTP/1.1 ") + QByteArray::number(statusCode) + QByteArrayLiteral(" ") + reason
        + QByteArrayLiteral("\r\nContent-Type: application/json\r\nContent-Length: ")
        + QByteArray::number(body.size()) + QByteArrayLiteral("\r\nConnection: close\r\n\r\n") + body;
}

QUrl fixtureUrl(const HttpFixtureServer &server)
{
    return QUrl(QStringLiteral("http://127.0.0.1:%1").arg(server.serverPort()));
}

} // namespace

class BackendManagerTest final : public QObject
{
    Q_OBJECT

private slots:
    void acceptsAgentFlowHealthResponse();
    void rejectsForeignHttpServiceAndAllowsExplicitRetry();
};

void BackendManagerTest::acceptsAgentFlowHealthResponse()
{
    HttpFixtureServer server(httpResponse(
        200, QByteArrayLiteral("{\"status\":\"ok\",\"service\":\"AgentFlow Backend\",\"version\":\"test\"}")));
    QVERIFY(server.start());

    BackendManager manager(fixtureUrl(server));
    QSignalSpy readySpy(&manager, &BackendManager::ready);
    QSignalSpy unavailableSpy(&manager, &BackendManager::unavailable);

    manager.ensureStarted();

    QTRY_COMPARE_WITH_TIMEOUT(readySpy.count(), 1, 4'000);
    QCOMPARE(unavailableSpy.count(), 0);
    QVERIFY(manager.isReady());
    QVERIFY(!manager.ownsBackendProcess());
}

void BackendManagerTest::rejectsForeignHttpServiceAndAllowsExplicitRetry()
{
    HttpFixtureServer server(httpResponse(404, QByteArrayLiteral("{\"detail\":\"not found\"}")));
    QVERIFY(server.start());

    BackendManager manager(fixtureUrl(server));
    QSignalSpy unavailableSpy(&manager, &BackendManager::unavailable);

    manager.ensureStarted();
    QTRY_COMPARE_WITH_TIMEOUT(unavailableSpy.count(), 1, 4'000);
    QVERIFY(unavailableSpy.at(0).at(0).toString().contains(QStringLiteral("已有服务")));
    QVERIFY(!manager.ownsBackendProcess());

    // 客户显式重试必须开启一条新的健康检查；非 AgentFlow 服务仍不可被误判为可复用后端。
    manager.retry();
    QTRY_COMPARE_WITH_TIMEOUT(unavailableSpy.count(), 2, 4'000);
    QVERIFY(unavailableSpy.at(1).at(0).toString().contains(QStringLiteral("已有服务")));
    QVERIFY(!manager.isReady());
    QVERIFY(!manager.ownsBackendProcess());
}

QTEST_MAIN(BackendManagerTest)

#include "backendmanager_test.moc"
