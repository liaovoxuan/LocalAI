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

static QJsonArray messagesToJson(const QVector<ChatMessage>& messages) {
    QJsonArray array;
    for (const auto& message : messages) {
        array.append(QJsonObject{{"role", message.role}, {"content", message.content}});
    }
    return array;
}

static QString messagesToPrompt(const QVector<ChatMessage>& messages) {
    QStringList parts;
    for (const auto& message : messages) {
        parts << QString("%1: %2").arg(message.role, message.content);
    }
    return parts.join("\n");
}

static QString providerCode(QString provider) {
    provider = provider.trimmed().toLower().replace('-', '_');
    if (provider == "openai") return "openai_official";
    if (provider == "llamacpp" || provider == "llama.cpp") return "llama_cpp";
    if (provider == "lmstudio") return "lm_studio";
    if (provider == "compatible" || provider == "openai_compatible_api") return "openai_compatible";
    return provider;
}

static QString defaultBaseUrl(const QString& provider) {
    if (provider == "openai_official") return "https://api.openai.com/v1";
    if (provider == "deepseek") return "https://api.deepseek.com/v1";
    if (provider == "openrouter") return "https://openrouter.ai/api/v1";
    if (provider == "siliconflow") return "https://api.siliconflow.cn/v1";
    return {};
}

static QByteArray postJson(const QUrl& url, const QJsonObject& payload, const QString& apiKey, QString* error) {
    if (!url.isValid() || (url.scheme() != "https" && url.scheme() != "http")) {
        if (error) *error = "Invalid API URL.";
        return {};
    }
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
    timer.stop();
    const QByteArray data = reply->readAll();
    const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
    if (status < 200 || status >= 300) {
        QString detail = QString::fromUtf8(data).trimmed();
        if (detail.size() > 600) {
            detail = detail.left(600) + "...";
        }
        if (error) {
            *error = detail.isEmpty()
                ? QString("HTTP %1: %2").arg(status).arg(reply->errorString())
                : QString("HTTP %1: %2").arg(status).arg(detail);
        }
        reply->deleteLater();
        return {};
    }
    if (reply->error() != QNetworkReply::NoError) {
        if (error) *error = reply->errorString();
        reply->deleteLater();
        return {};
    }
    reply->deleteLater();
    return data;
}

static QByteArray getUrl(const QUrl& url, const QString& apiKey, QString* error) {
    if (!url.isValid() || (url.scheme() != "https" && url.scheme() != "http")) {
        if (error) *error = "Invalid API URL.";
        return {};
    }
    QNetworkAccessManager manager;
    QNetworkRequest request(url);
    if (!apiKey.isEmpty()) request.setRawHeader("Authorization", ("Bearer " + apiKey).toUtf8());
    QNetworkReply* reply = manager.get(request);
    QEventLoop loop;
    QTimer timer;
    timer.setSingleShot(true);
    QObject::connect(reply, &QNetworkReply::finished, &loop, &QEventLoop::quit);
    QObject::connect(&timer, &QTimer::timeout, &loop, &QEventLoop::quit);
    timer.start(8000);
    loop.exec();
    if (!timer.isActive()) {
        reply->abort();
        if (error) *error = "Request timeout";
        reply->deleteLater();
        return {};
    }
    const QByteArray data = reply->readAll();
    const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
    const QString replyError = reply->errorString();
    reply->deleteLater();
    if (status < 200 || status >= 300) {
        if (error) *error = QString("HTTP %1: %2").arg(status).arg(replyError);
        return {};
    }
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
    const QString resourcesDir = QDir(appDir).filePath("../Resources");
    const QStringList candidates = {
        QDir(resourcesDir).filePath("runtime/llama.cpp/" + osName + "/" + arch + "/bin/" + exe),
        QDir(resourcesDir).filePath("runtime/llama.cpp/" + osName + "/" + arch + "/" + exe),
        QDir(resourcesDir).filePath("runtime/llama.cpp/bin/" + exe),
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
    const QString provider = runtimeRequiresLlamaCpp() ? "llama_cpp" : providerCode(config.provider);
    if (messages.isEmpty()) {
        if (error) *error = "No chat message to send.";
        return {};
    }
    if (provider == "ollama") {
        return askOllama(messages, config, error);
    }
    if (provider == "llama_cpp") {
        return askLlamaCpp(messages, config, error);
    }
    if (config.apiKey.isEmpty() && provider != "lm_studio") {
        if (error) *error = trText(config.language, "missing_key");
        return {};
    }
    return askOpenAICompatible(messages, config, error);
}

bool ProviderClient::testConnection(const AppConfig& config, QString* message) {
    const QString provider = runtimeRequiresLlamaCpp() ? "llama_cpp" : providerCode(config.provider);
    if (provider == "llama_cpp") {
        const QString binary = findLlamaCppBinary(config);
        const QString model = config.llamaCppModel.trimmed().isEmpty() ? config.model : config.llamaCppModel;
        const bool ok = !binary.isEmpty() && QFileInfo::exists(model);
        if (message) *message = ok ? "llama.cpp ready" : "Configure an executable and local GGUF model.";
        return ok;
    }
    if (provider == "ollama") {
        QString error;
        const QByteArray data = getUrl(QUrl("http://127.0.0.1:11434/api/tags"), {}, &error);
        const bool ok = !data.isEmpty();
        if (message) *message = ok ? "Ollama ready" : error;
        return ok;
    }
    if (config.apiKey.isEmpty() && provider != "lm_studio") {
        if (message) *message = trText(config.language, "missing_key");
        return false;
    }
    if (message) *message = "Provider configuration saved. A request will validate the endpoint.";
    return true;
}

QStringList ProviderClient::listModels(const AppConfig& config, QString* error) {
    const QString provider = runtimeRequiresLlamaCpp() ? "llama_cpp" : providerCode(config.provider);
    if (provider == "ollama") {
        const QByteArray data = getUrl(QUrl("http://127.0.0.1:11434/api/tags"), {}, error);
        if (data.isEmpty()) return {};
        const auto root = QJsonDocument::fromJson(data).object();
        QStringList result;
        for (const auto& value : root.value("models").toArray()) {
            const QString name = value.toObject().value("name").toString();
            if (!name.isEmpty()) result << name;
        }
        return result;
    }
    if (provider == "llama_cpp") return config.llamaCppModel.isEmpty() ? QStringList{} : QStringList{config.llamaCppModel};
    return config.model.isEmpty() ? QStringList{} : QStringList{config.model};
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
    QStringList args{"-m", modelPath, "-p", messagesToPrompt(messages)};
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
    QJsonParseError parseError;
    const QJsonDocument document = QJsonDocument::fromJson(data, &parseError);
    if (!document.isObject()) {
        if (error) *error = "Ollama returned invalid JSON.";
        return {};
    }
    const QJsonObject object = document.object();
    const QString answer = object.value("message").toObject().value("content").toString(
        object.value("response").toString()).trimmed();
    if (answer.isEmpty() && error) *error = "Ollama returned an empty response.";
    return answer;
}

QString ProviderClient::askOpenAICompatible(const QVector<ChatMessage>& messages, const AppConfig& config, QString* error) {
    const QString provider = providerCode(config.provider);
    QString baseUrl = provider == "lm_studio" ? config.lmStudioBaseUrl : config.apiBaseUrl;
    if (baseUrl.isEmpty()) {
        baseUrl = defaultBaseUrl(provider);
    }
    if (baseUrl.isEmpty()) {
        if (error) *error = "API Base URL is not configured.";
        return {};
    }
    if (baseUrl.endsWith('/')) baseUrl.chop(1);
    QJsonObject payload{{"model", config.model}, {"messages", messagesToJson(messages)}};
    const QString endpoint = baseUrl.endsWith("/chat/completions")
        ? baseUrl
        : baseUrl + "/chat/completions";
    const QByteArray data = postJson(QUrl(endpoint), payload, config.apiKey, error);
    if (data.isEmpty()) return {};
    QJsonParseError parseError;
    const QJsonDocument document = QJsonDocument::fromJson(data, &parseError);
    if (!document.isObject()) {
        if (error) *error = "Provider returned invalid JSON.";
        return {};
    }
    const QJsonArray choices = document.object().value("choices").toArray();
    if (choices.isEmpty()) {
        if (error) *error = "No response choices returned";
        return {};
    }
    const QJsonObject choice = choices.at(0).toObject();
    const QString answer = choice.value("message").toObject().value("content").toString(
        choice.value("text").toString()).trimmed();
    if (answer.isEmpty() && error) *error = "Provider returned an empty response.";
    return answer;
}

} // namespace nativeai
