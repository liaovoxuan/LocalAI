#include "AppConfig.hpp"

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QSaveFile>
#include <QSysInfo>

namespace nativeai {
namespace {

QString dataDirectory(AppMode mode) {
    const QString overrideRoot = qEnvironmentVariable("LOCALAI_APP_DATA_DIR").trimmed();
    QString root;
    if (!overrideRoot.isEmpty()) {
        root = QDir(overrideRoot).filePath(appName(mode));
#if defined(Q_OS_WIN)
    } else if (!qEnvironmentVariable("LOCALAPPDATA").isEmpty()) {
        root = QDir(qEnvironmentVariable("LOCALAPPDATA")).filePath(appName(mode));
#elif defined(Q_OS_MACOS)
    } else {
        root = QDir::home().filePath("Library/Application Support/" + appName(mode));
#else
    } else {
        QString base = qEnvironmentVariable("XDG_DATA_HOME");
        if (base.isEmpty()) base = QDir::home().filePath(".local/share");
        root = QDir(base).filePath(appName(mode));
#endif
    }
    QDir().mkpath(root);
    return root;
}

QJsonObject readObject(const QString& path) {
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) return {};
    const QJsonDocument document = QJsonDocument::fromJson(file.readAll());
    return document.isObject() ? document.object() : QJsonObject{};
}

bool writeObject(const QString& path, const QJsonObject& object, QString* error) {
    const QFileInfo info(path);
    if (!QDir().mkpath(info.dir().absolutePath())) {
        if (error) *error = "Cannot create the configuration directory.";
        return false;
    }
    QSaveFile file(path);
    if (!file.open(QIODevice::WriteOnly)
        || file.write(QJsonDocument(object).toJson(QJsonDocument::Indented)) < 0
        || !file.commit()) {
        if (error) *error = file.errorString();
        return false;
    }
    QFile::setPermissions(path, QFileDevice::ReadOwner | QFileDevice::WriteOwner);
    return true;
}

QString normalizeProvider(QString provider) {
    provider = provider.trimmed().toLower().replace('-', '_');
    if (provider == "openai") provider = "openai_official";
    if (provider == "llamacpp" || provider == "llama.cpp") provider = "llama_cpp";
    if (provider == "lmstudio") provider = "lm_studio";
    if (provider == "compatible") provider = "openai_compatible";
    return provider;
}

QString defaultUrl(const QString& provider) {
    if (provider == "openai_official") return "https://api.openai.com/v1";
    if (provider == "openai_compatible") return {};
    if (provider == "lm_studio") return "http://127.0.0.1:1234/v1";
    return {};
}

QByteArray secretKey() {
    QByteArray key = QSysInfo::machineUniqueId();
    if (key.isEmpty()) key = QSysInfo::machineHostName().toUtf8();
    return key.isEmpty() ? QByteArray("localai-native") : key;
}

QString encodeSecret(const QString& secret) {
    QByteArray bytes = secret.toUtf8();
    const QByteArray key = secretKey();
    for (qsizetype i = 0; i < bytes.size(); ++i) bytes[i] = bytes.at(i) ^ key.at(i % key.size());
    return QString::fromLatin1(bytes.toBase64(QByteArray::Base64UrlEncoding | QByteArray::OmitTrailingEquals));
}

QString decodeSecret(const QString& encoded) {
    QByteArray bytes = QByteArray::fromBase64(encoded.toLatin1(), QByteArray::Base64UrlEncoding);
    const QByteArray key = secretKey();
    for (qsizetype i = 0; i < bytes.size(); ++i) bytes[i] = bytes.at(i) ^ key.at(i % key.size());
    return QString::fromUtf8(bytes);
}

} // namespace

QString appName(AppMode mode) {
    if (mode == AppMode::CloudAI) return "CloudAI";
    return mode == AppMode::LocalAISE ? "LocalAI SE" : "LocalAI";
}

QString versionLabel(AppMode) { return "1.0"; }

QString defaultConfigPath(AppMode mode) {
    return QDir(dataDirectory(mode)).filePath("config.json");
}

QString defaultSecretsPath(AppMode mode) {
    return QDir(dataDirectory(mode)).filePath("secrets.json");
}

bool runtimeRequiresLlamaCpp() {
    const QString os = qEnvironmentVariable("LOCALAI_TARGET_OS", QSysInfo::productType()).toLower();
    const QString arch = qEnvironmentVariable("LOCALAI_TARGET_ARCH", QSysInfo::currentCpuArchitecture()).toLower();
    const QString kernel = QSysInfo::kernelType().toLower();
    const bool windowsArm = os.contains("windows") && (arch == "arm64" || arch == "aarch64");
    return windowsArm || os.contains("harmony") || os.contains("ohos")
        || kernel.contains("harmony") || kernel.contains("ohos")
        || arch.contains("loongarch") || arch.contains("riscv");
}

QStringList supportedProviders(AppMode mode) {
    if (runtimeRequiresLlamaCpp()) return {"llama_cpp"};
    if (mode == AppMode::LocalAISE) return {"ollama", "llama_cpp"};
    if (mode == AppMode::CloudAI) return {"openai_official", "openai_compatible"};
    return {"ollama", "llama_cpp", "lm_studio", "openai_compatible", "openai_official"};
}

AppConfig loadConfig(AppMode mode) {
    AppConfig config;
    config.appDataDir = dataDirectory(mode);
    config.provider = mode == AppMode::CloudAI ? "openai_official" : "ollama";
    config.model = mode == AppMode::CloudAI ? "gpt-4.1-mini" : "qwen2.5:7b";
    const QJsonObject object = readObject(defaultConfigPath(mode));
    config.language = object.value("language").toString(config.language);
    config.theme = object.value("theme").toString(config.theme);
    config.provider = normalizeProvider(object.value("provider").toString(config.provider));
    config.model = object.value("model").toString(config.model);
    config.apiBaseUrl = object.value("api_base_url").toString(defaultUrl(config.provider));
    config.lmStudioBaseUrl = object.value("lmstudio_base_url").toString(config.lmStudioBaseUrl);
    config.llamaCppBinary = object.value("llamacpp_binary").toString();
    config.llamaCppModel = object.value("llamacpp_model").toString();
    config.wallpaperPath = object.value("wallpaper_path").toString();
    config.apiKey = decodeSecret(readObject(defaultSecretsPath(mode)).value(config.provider).toString());
    if (!supportedProviders(mode).contains(config.provider)) config.provider = supportedProviders(mode).first();
    if (runtimeRequiresLlamaCpp()) config.provider = "llama_cpp";
    return config;
}

bool saveConfig(AppMode mode, const AppConfig& input, QString* error) {
    AppConfig config = input;
    config.provider = runtimeRequiresLlamaCpp() ? "llama_cpp" : normalizeProvider(config.provider);
    if (!supportedProviders(mode).contains(config.provider)) {
        if (error) *error = "Unsupported provider for this application mode.";
        return false;
    }
    QJsonObject object{
        {"language", config.language}, {"theme", config.theme}, {"provider", config.provider},
        {"model", config.model}, {"api_base_url", config.apiBaseUrl},
        {"lmstudio_base_url", config.lmStudioBaseUrl}, {"llamacpp_binary", config.llamaCppBinary},
        {"llamacpp_model", config.llamaCppModel}, {"wallpaper_path", config.wallpaperPath},
    };
    QJsonObject secrets = readObject(defaultSecretsPath(mode));
    if (config.apiKey.trimmed().isEmpty()) secrets.remove(config.provider);
    else secrets.insert(config.provider, encodeSecret(config.apiKey.trimmed()));
    return writeObject(defaultSecretsPath(mode), secrets, error)
        && writeObject(defaultConfigPath(mode), object, error);
}

QString trText(const QString& language, const QString& key) {
    const bool en = language.toLower().startsWith("en");
    const bool ja = language.toLower().startsWith("ja");
    const bool de = language.toLower().startsWith("de");
    const bool fr = language.toLower().startsWith("fr");
    if (key == "send") return en ? "Send" : ja ? "送信" : de ? "Senden" : fr ? "Envoyer" : "发送";
    if (key == "new_chat") return en ? "New Chat" : ja ? "新規チャット" : de ? "Neuer Chat" : fr ? "Nouvelle discussion" : "新对话";
    if (key == "settings") return en ? "Settings" : ja ? "設定" : de ? "Einstellungen" : fr ? "Réglages" : "设置";
    if (key == "thinking") return en ? "Thinking..." : "正在思考...";
    if (key == "error") return en ? "Error" : "错误";
    if (key == "history") return en ? "History" : "历史聊天";
    if (key == "import_file") return en ? "Attach file" : "添加文件";
    if (key == "export") return en ? "Export" : "导出";
    if (key == "export_done") return en ? "Chat exported." : "聊天记录已导出。";
    if (key == "imported_files") return en ? "%1 file(s) attached" : "已添加 %1 个文件";
    if (key == "user") return en ? "You" : "你";
    if (key == "test_connection") return en ? "Test connection" : "测试连接";
    if (key == "missing_key") return en ? "Configure an API key in Settings." : "请在设置中配置 API Key。";
    if (key == "agent_off") return en ? "Agent: Off" : "Agent：关";
    if (key == "agent_on") return en ? "Agent: On" : "Agent：开";
    if (key == "agent_tooltip") return en ? "Toggle Agent mode" : "切换 Agent 模式";
    if (key == "agent_enabled") return en ? "Agent mode enabled" : "Agent 模式已开启";
    if (key == "agent_disabled") return en ? "Agent mode disabled" : "Agent 模式已关闭";
    if (key == "language") return en ? "Language" : "语言";
    if (key == "provider") return en ? "Provider" : "模型提供方";
    if (key == "model") return en ? "Model" : "模型";
    if (key == "theme") return en ? "Theme" : "主题";
    if (key == "choose") return en ? "Choose..." : "选择...";
    return key;
}

QString maskApiKey(const QString& value) { return value.isEmpty() ? QString() : "********"; }
QStringList supportedLanguages() { return {"zh_cn", "zh_tw", "en_us", "en_gb", "ja", "fr", "de"}; }

} // namespace nativeai
