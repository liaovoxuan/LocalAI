#include "ProviderClient.hpp"

#include <QEventLoop>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QCoreApplication>
#include <QDir>
#include <QFileInfo>
#include <QProcess>
#include <QSysInfo>
#include <QStandardPaths>
#include <QThread>
#include <QTimer>

namespace nativeai {

ProviderClient::ProviderClient(QObject* parent) : QObject(parent) {}

static QJsonArray messagesToJson(const QVector<ChatMessage>& messages) {
    QJsonArray array;
    for (const auto& message : messages) {
        array.append(QJsonObject{{"role", message.role}, {"content", message.content}});
    }
    return array;
}

static QByteArray postJson(const QUrl& url, const QJsonObject& payload, const QString& apiKey, QString* error) {
    QNetworkAccessManager manager;
    QNetworkRequest request(url);
    request.setHeader(QNetworkRequest::ContentTypeHeader, "application/json");
    if (!apiKey.isEmpty()) {
        request.setRawHeader("Authorization", ("Bearer " + apiKey).toUtf8());
    }
    QNetworkReply* reply = manager.post(request, QJsonDocument(payload).toJson(QJsonDocument::Compact));
    QEventLoop loop;
    QTimer timer;
    timer.setSingleShot(true);
    QObject::connect(reply, &QNetworkReply::finished, &loop, &QEventLoop::quit);
    QObject::connect(&timer, &QTimer::timeout, &loop, &QEventLoop::quit);
    timer.start(120000);
    loop.exec();
    if (!timer.isActive()) {
        reply->abort();
        if (error) *error = "Request timeout";
        reply->deleteLater();
        return {};
    }
    if (reply->error() != QNetworkReply::NoError) {
        if (error) *error = reply->errorString();
        reply->deleteLater();
        return {};
    }
    const QByteArray data = reply->readAll();
    reply->deleteLater();
    return data;
}

static QString findLlamaCppBinary(const AppConfig& config) {
    if (!config.llamaCppBinary.trimmed().isEmpty() && QFileInfo::exists(config.llamaCppBinary)) {
        return config.llamaCppBinary;
    }
    const QString appDir = QCoreApplication::applicationDirPath();
    QString osName = QSysInfo::productType().toLower();
    if (osName == "osx" || osName == "macos") osName = "macos";
    else if (osName.contains("windows")) osName = "windows";
    else if (osName.contains("harmony") || osName.contains("ohos")) osName = "harmonyos";
    else osName = "linux";
    QString arch = QSysInfo::currentCpuArchitecture().toLower();
    if (arch == "x86_64" || arch == "amd64") arch = "x64";
    else if (arch == "arm64" || arch == "aarch64") arch = "arm64";
    const QString exe = osName == "windows" ? "llama-cli.exe" : "llama-cli";
    const QStringList candidates = {
        QDir(appDir).filePath("runtime/llama.cpp/" + osName + "/" + arch + "/bin/" + exe),
        QDir(appDir).filePath("runtime/llama.cpp/" + osName + "/" + arch + "/" + exe),
        QDir(appDir).filePath("runtime/llama.cpp/bin/llama-cli"),
        QDir(appDir).filePath("runtime/llama.cpp/llama-cli"),
        QDir(appDir).filePath("llama-cli"),
        QDir::homePath() + "/Downloads/llama.cpp-master/build/bin/llama-cli",
        QDir::homePath() + "/Downloads/llama.cpp-master/build/bin/Release/llama-cli",
        QDir::homePath() + "/Downloads/llama.cpp-master/bin/llama-cli",
    };
    for (const QString& path : candidates) {
        if (QFileInfo(path).isExecutable()) return path;
    }
    QString found = QStandardPaths::findExecutable("llama-cli");
    if (!found.isEmpty()) return found;
    return QStandardPaths::findExecutable("main");
}

static QStringList llamaCppOptions(const AppConfig& config) {
    int threads = qBound(1, QThread::idealThreadCount() > 0 ? QThread::idealThreadCount() / 2 : 4, 8);
    Q_UNUSED(config);
    return {
        "-c", "4096",
        "-t", QString::number(threads),
        "-n", "512",
        "--temp", "0.7",
        "--no-display-prompt",
        "--no-conversation",
        "--single-turn",
        "--simple-io",
        "--no-warmup",
        "--no-show-timings",
    };
}

QString ProviderClient::ask(const QVector<ChatMessage>& messages, const AppConfig& config, QString* error) {
    if (config.provider == "ollama") {
        return askOllama(messages, config, error);
    }
    if (config.provider == "llama_cpp") {
        return askLlamaCpp(messages, config, error);
    }
    if (config.apiKey.isEmpty() && config.provider != "lm_studio") {
        if (error) *error = trText(config.language, "missing_key");
        return {};
    }
    return askOpenAICompatible(messages, config, error);
}

QString ProviderClient::askLlamaCpp(const QVector<ChatMessage>& messages, const AppConfig& config, QString* error) {
    const QString binary = findLlamaCppBinary(config);
    const QString modelPath = config.llamaCppModel.trimmed().isEmpty() ? config.model : config.llamaCppModel.trimmed();
    if (binary.isEmpty()) {
        if (error) *error = "llama.cpp executable was not found. Put llama-cli in runtime/llama.cpp/bin or add it to PATH.";
        return {};
    }
    if (modelPath.isEmpty() || !QFileInfo::exists(modelPath)) {
        if (error) *error = "llama.cpp requires a local GGUF model path.";
        return {};
    }
    QProcess process;
    QStringList args{"-m", modelPath, "-p", messagesToJson(messages).last().toObject().value("content").toString()};
    args.append(llamaCppOptions(config));
    process.start(binary, args);
    if (!process.waitForStarted(5000)) {
        if (error) *error = process.errorString();
        return {};
    }
    if (!process.waitForFinished(180000)) {
        process.kill();
        if (error) *error = "llama.cpp request timeout";
        return {};
    }
    if (process.exitCode() != 0) {
        if (error) *error = QString::fromUtf8(process.readAllStandardError()).trimmed();
        return {};
    }
    return QString::fromUtf8(process.readAllStandardOutput()).trimmed();
}

QString ProviderClient::askOllama(const QVector<ChatMessage>& messages, const AppConfig& config, QString* error) {
    QJsonObject payload{{"model", config.model}, {"messages", messagesToJson(messages)}, {"stream", false}};
    const QByteArray data = postJson(QUrl("http://127.0.0.1:11434/api/chat"), payload, {}, error);
    if (data.isEmpty()) return {};
    const QJsonDocument document = QJsonDocument::fromJson(data);
    const QJsonObject object = document.object();
    return object.value("message").toObject().value("content").toString();
}

QString ProviderClient::askOpenAICompatible(const QVector<ChatMessage>& messages, const AppConfig& config, QString* error) {
    QString baseUrl = config.provider == "lm_studio" ? config.lmStudioBaseUrl : config.apiBaseUrl;
    if (baseUrl.endsWith('/')) baseUrl.chop(1);
    QJsonObject payload{{"model", config.model}, {"messages", messagesToJson(messages)}};
    const QByteArray data = postJson(QUrl(baseUrl + "/chat/completions"), payload, config.apiKey, error);
    if (data.isEmpty()) return {};
    const QJsonDocument document = QJsonDocument::fromJson(data);
    const QJsonArray choices = document.object().value("choices").toArray();
    if (choices.isEmpty()) {
        if (error) *error = "No response choices returned";
        return {};
    }
    return choices.at(0).toObject().value("message").toObject().value("content").toString();
}

} // namespace nativeai
