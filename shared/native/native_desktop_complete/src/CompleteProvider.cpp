#include "ProviderClient.hpp"

#include <QCoreApplication>
#include <QDir>
#include <QEventLoop>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QProcess>
#include <QStandardPaths>
#include <QSysInfo>
#include <QThread>
#include <QTimer>

namespace nativeai {
namespace {

QJsonArray messageArray(const QVector<ChatMessage>& messages) {
    QJsonArray result;
    for (const ChatMessage& message : messages) {
        result.append(QJsonObject{{"role", message.role}, {"content", message.content}});
    }
    return result;
}

QString promptText(const QVector<ChatMessage>& messages) {
    QStringList result;
    for (const ChatMessage& message : messages) result << message.role + ": " + message.content;
    return result.join('\n');
}

QString providerCode(QString provider) {
    provider = provider.trimmed().toLower().replace('-', '_');
    if (provider == "openai") return "openai_official";
    if (provider == "llamacpp" || provider == "llama.cpp") return "llama_cpp";
    if (provider == "lmstudio") return "lm_studio";
    return provider;
}

QString defaultUrl(const QString& provider) {
    if (provider == "openai_official") return "https://api.openai.com/v1";
    if (provider == "lm_studio") return "http://127.0.0.1:1234/v1";
    return {};
}

QByteArray requestJson(const QUrl& url, const QJsonObject& payload, const QString& apiKey,
                       int timeoutMs, QString* error) {
    if (!url.isValid() || (url.scheme() != "http" && url.scheme() != "https")) {
        if (error) *error = "Invalid API URL.";
        return {};
    }
    QNetworkAccessManager manager;
    QNetworkRequest request(url);
    request.setHeader(QNetworkRequest::ContentTypeHeader, "application/json");
    request.setRawHeader("Accept", "application/json");
    request.setRawHeader("User-Agent", "LocalAI-Native/1.0");
    if (!apiKey.isEmpty()) request.setRawHeader("Authorization", ("Bearer " + apiKey).toUtf8());
    QNetworkReply* reply = manager.post(request, QJsonDocument(payload).toJson(QJsonDocument::Compact));
    QEventLoop loop;
    QTimer timer;
    timer.setSingleShot(true);
    QObject::connect(reply, &QNetworkReply::finished, &loop, &QEventLoop::quit);
    QObject::connect(&timer, &QTimer::timeout, &loop, &QEventLoop::quit);
    timer.start(timeoutMs);
    loop.exec();
    if (!timer.isActive()) {
        reply->abort();
        if (error) *error = "Request timeout.";
        reply->deleteLater();
        return {};
    }
    timer.stop();
    const QByteArray data = reply->readAll();
    const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
    const QString networkError = reply->errorString();
    reply->deleteLater();
    if (status < 200 || status >= 300) {
        QString detail = QString::fromUtf8(data).trimmed();
        if (detail.size() > 600) detail = detail.left(600) + "...";
        if (error) *error = detail.isEmpty() ? QString("HTTP %1: %2").arg(status).arg(networkError) : detail;
        return {};
    }
    return data;
}

QByteArray requestGet(const QUrl& url, int timeoutMs, QString* error) {
    QNetworkAccessManager manager;
    QNetworkRequest request(url);
    request.setRawHeader("User-Agent", "LocalAI-Native/1.0");
    QNetworkReply* reply = manager.get(request);
    QEventLoop loop;
    QTimer timer;
    timer.setSingleShot(true);
    QObject::connect(reply, &QNetworkReply::finished, &loop, &QEventLoop::quit);
    QObject::connect(&timer, &QTimer::timeout, &loop, &QEventLoop::quit);
    timer.start(timeoutMs);
    loop.exec();
    if (!timer.isActive()) {
        reply->abort();
        if (error) *error = "Request timeout.";
        reply->deleteLater();
        return {};
    }
    const QByteArray data = reply->readAll();
    const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
    const QString networkError = reply->errorString();
    reply->deleteLater();
    if (status < 200 || status >= 300) {
        if (error) *error = QString("HTTP %1: %2").arg(status).arg(networkError);
        return {};
    }
    return data;
}

QString llamaExecutable(const AppConfig& config) {
    if (QFileInfo(config.llamaCppBinary).isExecutable()) return config.llamaCppBinary;
    QString os = QSysInfo::productType().toLower();
    if (os == "osx") os = "macos";
    else if (os.contains("windows")) os = "windows";
    else if (os.contains("harmony") || os.contains("ohos")) os = "harmonyos";
    else os = "linux";
    QString arch = QSysInfo::currentCpuArchitecture().toLower();
    if (arch == "x86_64" || arch == "amd64") arch = "x64";
    else if (arch == "aarch64") arch = "arm64";
    const QString completion = os == "windows" ? "llama-completion.exe" : "llama-completion";
    const QString legacy = os == "windows" ? "llama-cli.exe" : "llama-cli";
    const QString appDir = QCoreApplication::applicationDirPath();
    const QString resources = QDir(appDir).filePath("../Resources");
    const QStringList candidates = {
        QDir(resources).filePath("runtime/llama.cpp/" + os + "/" + arch + "/bin/" + completion),
        QDir(resources).filePath("runtime/llama.cpp/bin/" + completion),
        QDir(resources).filePath("runtime/llama.cpp/" + os + "/" + arch + "/bin/" + legacy),
        QDir(resources).filePath("runtime/llama.cpp/bin/" + legacy),
        QDir(appDir).filePath(completion),
        QDir(appDir).filePath(legacy),
    };
    for (const QString& candidate : candidates) if (QFileInfo(candidate).isExecutable()) return candidate;
    const QString found = QStandardPaths::findExecutable(completion);
    return found.isEmpty() ? QStandardPaths::findExecutable(legacy) : found;
}

} // namespace

QString ProviderClient::ask(const QVector<ChatMessage>& messages, const AppConfig& config, QString* error) {
    if (messages.isEmpty()) { if (error) *error = "No message to send."; return {}; }
    const QString provider = runtimeRequiresLlamaCpp() ? "llama_cpp" : providerCode(config.provider);
    if (provider == "ollama") return askOllama(messages, config, error);
    if (provider == "llama_cpp") return askLlamaCpp(messages, config, error);
    if (config.apiKey.isEmpty() && provider != "lm_studio") {
        if (error) *error = trText(config.language, "missing_key");
        return {};
    }
    return askOpenAICompatible(messages, config, error);
}

bool ProviderClient::testConnection(const AppConfig& config, QString* message) {
    const QString provider = runtimeRequiresLlamaCpp() ? "llama_cpp" : providerCode(config.provider);
    if (provider == "llama_cpp") {
        const QString model = config.llamaCppModel.isEmpty() ? config.model : config.llamaCppModel;
        const bool okay = !llamaExecutable(config).isEmpty() && QFileInfo::exists(model);
        if (message) *message = okay ? "llama.cpp ready" : "Select a valid llama.cpp executable and GGUF model.";
        return okay;
    }
    if (provider == "ollama") {
        QString error;
        const bool okay = !requestGet(QUrl("http://127.0.0.1:11434/api/tags"), 8000, &error).isEmpty();
        if (message) *message = okay ? "Ollama ready" : error;
        return okay;
    }
    if (config.apiKey.isEmpty() && provider != "lm_studio") {
        if (message) *message = trText(config.language, "missing_key");
        return false;
    }
    if (message) *message = "Configuration is valid; the first chat request will validate the endpoint.";
    return true;
}

QStringList ProviderClient::listModels(const AppConfig& config, QString* error) {
    const QString provider = runtimeRequiresLlamaCpp() ? "llama_cpp" : providerCode(config.provider);
    if (provider == "ollama") {
        const QByteArray data = requestGet(QUrl("http://127.0.0.1:11434/api/tags"), 8000, error);
        QStringList result;
        for (const QJsonValue& value : QJsonDocument::fromJson(data).object().value("models").toArray()) {
            const QString name = value.toObject().value("name").toString();
            if (!name.isEmpty()) result << name;
        }
        return result;
    }
    if (provider == "llama_cpp") return config.llamaCppModel.isEmpty() ? QStringList{} : QStringList{config.llamaCppModel};
    return config.model.isEmpty() ? QStringList{} : QStringList{config.model};
}

QString ProviderClient::askLlamaCpp(const QVector<ChatMessage>& messages, const AppConfig& config, QString* error) {
    const QString executable = llamaExecutable(config);
    const QString model = config.llamaCppModel.isEmpty() ? config.model : config.llamaCppModel;
    if (executable.isEmpty()) { if (error) *error = "Bundled llama.cpp executable was not found."; return {}; }
    if (!QFileInfo::exists(model)) { if (error) *error = "Select a local GGUF model."; return {}; }
    const int idealThreads = QThread::idealThreadCount();
    const int threads = qBound(1, idealThreads > 1 ? idealThreads / 2 : 1, 8);
    QProcess process;
    const QStringList arguments = {
        "-m", model, "-p", promptText(messages), "-c", "4096", "-t", QString::number(threads),
        "-n", "512", "--temp", "0.7", "--no-display-prompt", "-no-cnv",
        "--single-turn", "--simple-io", "--no-warmup",
    };
    process.start(executable, arguments);
    if (!process.waitForStarted(5000)) { if (error) *error = process.errorString(); return {}; }
    if (!process.waitForFinished(180000)) {
        process.kill();
        process.waitForFinished();
        if (error) *error = "llama.cpp request timeout.";
        return {};
    }
    if (process.exitCode() != 0) {
        if (error) *error = QString::fromUtf8(process.readAllStandardError()).trimmed();
        return {};
    }
    return QString::fromUtf8(process.readAllStandardOutput()).trimmed();
}

QString ProviderClient::askOllama(const QVector<ChatMessage>& messages, const AppConfig& config, QString* error) {
    const QJsonObject payload{{"model", config.model}, {"messages", messageArray(messages)}, {"stream", false}};
    const QByteArray data = requestJson(QUrl("http://127.0.0.1:11434/api/chat"), payload, {}, 120000, error);
    const QJsonObject object = QJsonDocument::fromJson(data).object();
    const QString answer = object.value("message").toObject().value("content").toString(
        object.value("response").toString()).trimmed();
    if (answer.isEmpty() && data.size() && error) *error = "Ollama returned an empty response.";
    return answer;
}

QString ProviderClient::askOpenAICompatible(const QVector<ChatMessage>& messages, const AppConfig& config, QString* error) {
    const QString provider = providerCode(config.provider);
    QString base = provider == "lm_studio" ? config.lmStudioBaseUrl : config.apiBaseUrl;
    if (base.isEmpty()) base = defaultUrl(provider);
    if (base.isEmpty()) { if (error) *error = "Configure the API base URL."; return {}; }
    while (base.endsWith('/')) base.chop(1);
    const QUrl endpoint(base.endsWith("/chat/completions") ? base : base + "/chat/completions");
    const QJsonObject payload{{"model", config.model}, {"messages", messageArray(messages)}, {"stream", false}};
    const QByteArray data = requestJson(endpoint, payload, config.apiKey, 120000, error);
    const QJsonArray choices = QJsonDocument::fromJson(data).object().value("choices").toArray();
    if (choices.isEmpty()) { if (!data.isEmpty() && error) *error = "Provider returned no choices."; return {}; }
    const QJsonObject choice = choices.first().toObject();
    const QString answer = choice.value("message").toObject().value("content").toString(
        choice.value("text").toString()).trimmed();
    if (answer.isEmpty() && error) *error = "Provider returned an empty response.";
    return answer;
}

} // namespace nativeai
