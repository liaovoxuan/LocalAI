#include "AppConfig.hpp"

#include <QDir>
#include <QCoreApplication>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QSaveFile>
#include <QStandardPaths>
#include <QSysInfo>

namespace nativeai {

QString appName(AppMode mode) {
    if (mode == AppMode::CloudAI) {
        return "CloudAI";
    }
    return mode == AppMode::LocalAISE ? "LocalAI SE" : "LocalAI";
}

QString versionLabel(AppMode mode) {
    return "1.0";
}

static QString appDataDir(AppMode mode) {
    const QString overrideRoot = qEnvironmentVariable("LOCALAI_APP_DATA_DIR").trimmed();
    QString root;
    if (!overrideRoot.isEmpty()) {
        root = QDir(overrideRoot).filePath(appName(mode));
    } else {
        const bool portable = mode == AppMode::CloudAI
            ? qEnvironmentVariableIsSet("CLOUDAI_PORTABLE")
            : qEnvironmentVariableIsSet("LOCALAI_PORTABLE");
        if (portable) {
            root = QDir(QCoreApplication::applicationDirPath()).filePath(appName(mode));
        }
#if defined(Q_OS_WIN)
        else {
            root = qEnvironmentVariable("LOCALAPPDATA");
            if (root.isEmpty()) {
                root = qEnvironmentVariable("APPDATA");
            }
            root = QDir(root.isEmpty() ? QDir::homePath() : root).filePath(appName(mode));
        }
#elif defined(Q_OS_MACOS)
        else {
            root = QDir(QDir::homePath()).filePath("Library/Application Support/" + appName(mode));
        }
#else
        else {
            root = qEnvironmentVariable("XDG_DATA_HOME");
            if (root.isEmpty()) {
                root = QDir(QDir::homePath()).filePath(".local/share");
            }
            root = QDir(root).filePath(appName(mode));
        }
#endif
    }
    QDir().mkpath(root);
    return root;
}

QString defaultConfigPath(AppMode mode) {
    if (mode == AppMode::CloudAI) {
        return QDir(appDataDir(mode)).filePath("config/cloudai_config.json");
    }
    return QDir(appDataDir(mode)).filePath("config.json");
}

QString defaultSecretsPath(AppMode mode) {
    if (mode == AppMode::CloudAI) {
        return QDir(appDataDir(mode)).filePath("config/cloudai_secrets.json");
    }
    return QDir(appDataDir(mode)).filePath("localai_secrets.json");
}

static QJsonObject readJsonObject(const QString& path) {
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        return {};
    }
    const QJsonDocument document = QJsonDocument::fromJson(file.readAll());
    return document.isObject() ? document.object() : QJsonObject{};
}

static bool writeJsonObject(const QString& path, const QJsonObject& object, QString* error) {
    const QFileInfo info(path);
    if (!QDir().mkpath(info.dir().absolutePath())) {
        if (error) {
            *error = QString("Cannot create configuration directory: %1").arg(info.dir().absolutePath());
        }
        return false;
    }
    QSaveFile file(path);
    if (!file.open(QIODevice::WriteOnly)) {
        if (error) {
            *error = file.errorString();
        }
        return false;
    }
    if (file.write(QJsonDocument(object).toJson(QJsonDocument::Indented)) < 0 || !file.commit()) {
        if (error) {
            *error = file.errorString();
        }
        return false;
    }
    QFile::setPermissions(path, QFileDevice::ReadOwner | QFileDevice::WriteOwner);
    return true;
}

static QString defaultBaseUrl(const QString& provider) {
    if (provider == "openai_official") {
        return "https://api.openai.com/v1";
    }
    if (provider == "deepseek") {
        return "https://api.deepseek.com/v1";
    }
    if (provider == "openrouter") {
        return "https://openrouter.ai/api/v1";
    }
    if (provider == "siliconflow") {
        return "https://api.siliconflow.cn/v1";
    }
    return {};
}

static QString normalizeProvider(QString provider) {
    provider = provider.trimmed().toLower().replace('-', '_');
    if (provider == "openai") {
        return "openai_official";
    }
    if (provider == "llamacpp" || provider == "llama.cpp") {
        return "llama_cpp";
    }
    if (provider == "lmstudio") {
        return "lm_studio";
    }
    if (provider == "compatible" || provider == "openai_compatible_api") {
        return "openai_compatible";
    }
    const QStringList supported = {
        "ollama", "llama_cpp", "lm_studio", "openai_compatible",
        "openai_official", "deepseek", "openrouter", "siliconflow",
    };
    return supported.contains(provider) ? provider : "ollama";
}

bool runtimeRequiresLlamaCpp() {
    const QString targetOs = qEnvironmentVariable("LOCALAI_TARGET_OS", QSysInfo::productType())
        .trimmed().toLower();
    const QString targetArch = qEnvironmentVariable("LOCALAI_TARGET_ARCH", QSysInfo::currentCpuArchitecture())
        .trimmed().toLower();
    const QString kernel = QSysInfo::kernelType().trimmed().toLower();
    const bool windowsOnArm = targetOs.contains("windows")
        && (targetArch == "arm64" || targetArch == "aarch64");
    return windowsOnArm
        || targetOs.contains("harmony")
        || targetOs.contains("ohos")
        || kernel.contains("harmony")
        || kernel.contains("ohos")
        || targetArch.contains("loongarch")
        || targetArch.contains("riscv");
}

QStringList supportedProviders(AppMode mode) {
    if (runtimeRequiresLlamaCpp()) {
        return {"llama_cpp"};
    }
    if (mode == AppMode::CloudAI) {
        return {"openai_official", "openai_compatible", "deepseek", "openrouter", "siliconflow"};
    }
    if (mode == AppMode::LocalAISE) {
        return {"ollama", "llama_cpp"};
    }
    return {"ollama", "llama_cpp", "lm_studio", "openai_compatible", "openai_official"};
}

static QByteArray machineSalt() {
    const QByteArray salt = QSysInfo::machineHostName().toUtf8();
    return salt.isEmpty() ? QByteArray("cloudai") : salt;
}

static QString encodeSecret(const QString& value) {
    if (value.isEmpty()) {
        return {};
    }
    QByteArray bytes = value.toUtf8();
    const QByteArray salt = machineSalt();
    for (int i = 0; i < bytes.size(); ++i) {
        bytes[i] = bytes.at(i) ^ salt.at(i % salt.size());
    }
    return "v1:" + QString::fromLatin1(
        bytes.toBase64(QByteArray::Base64UrlEncoding | QByteArray::OmitTrailingEquals));
}

static QString decodeSecret(QString value) {
    value = value.trimmed();
    if (value.isEmpty() || value == "********") {
        return {};
    }
    if (value.startsWith("localai-secret-v1:")) {
        value.remove(0, QString("localai-secret-v1:").size());
    } else if (value.startsWith("v1:")) {
        value.remove(0, 3);
    } else if (!value.contains(':')) {
        return value;
    } else {
        return {};
    }
    QByteArray bytes = QByteArray::fromBase64(value.toLatin1(), QByteArray::Base64UrlEncoding);
    if (bytes.isEmpty()) {
        return {};
    }
    const QByteArray salt = machineSalt();
    for (int i = 0; i < bytes.size(); ++i) {
        bytes[i] = bytes.at(i) ^ salt.at(i % salt.size());
    }
    const QString decoded = QString::fromUtf8(bytes);
    return decoded.contains(QChar::ReplacementCharacter) ? QString() : decoded;
}

static QString secretForProvider(const QJsonObject& secrets, const QString& provider) {
    return decodeSecret(secrets.value(provider).toString());
}

AppConfig loadConfig(AppMode mode) {
    AppConfig config;
    config.appDataDir = appDataDir(mode);
    config.provider = mode == AppMode::CloudAI ? "openai_official" : "ollama";
    config.model = mode == AppMode::CloudAI ? "gpt-4.1-mini" : "qwen2.5:7b";
    config.apiBaseUrl = defaultBaseUrl(config.provider);

    QJsonObject object;
    if (mode == AppMode::CloudAI) {
        const QJsonObject shared = readJsonObject(QDir(config.appDataDir).filePath("config.json"));
        config.language = shared.value("language").toString(config.language);
        config.theme = shared.value("theme").toString(config.theme);
        object = readJsonObject(defaultConfigPath(mode));
        config.provider = normalizeProvider(object.value("provider").toString(config.provider));
        const QJsonObject providers = object.value("providers").toObject();
        const QJsonObject provider = providers.value(config.provider).toObject();
        config.model = provider.value("model").toString(
            object.value("default_model").toString(config.model));
        config.apiBaseUrl = provider.value("base_url").toString(defaultBaseUrl(config.provider));
        config.apiKey = secretForProvider(readJsonObject(defaultSecretsPath(mode)), config.provider);
        if (config.apiKey.isEmpty()) {
            config.apiKey = decodeSecret(provider.value("api_key").toString());
        }
    } else {
        object = readJsonObject(defaultConfigPath(mode));
        config.language = object.value("language").toString(config.language);
        config.theme = object.value("theme").toString(config.theme);
        config.provider = normalizeProvider(object.value("provider").toString(config.provider));
        config.model = object.value("last_model").toString(
            object.value("model").toString(object.value("openai_model").toString(config.model)));
        config.llamaCppBinary = object.value("llamacpp_binary").toString();
        config.llamaCppModel = object.value("llamacpp_model").toString();
        config.apiBaseUrl = object.value("api_base_url").toString(defaultBaseUrl(config.provider));
        config.lmStudioBaseUrl = object.value("lmstudio_base_url").toString(config.lmStudioBaseUrl);
        config.wallpaperPath = object.value("wallpaper_path").toString(
            object.value("wallpaper").toString());
        config.apiKey = secretForProvider(readJsonObject(defaultSecretsPath(mode)), config.provider);
        if (config.apiKey.isEmpty()) {
            config.apiKey = decodeSecret(object.value("api_key").toString());
        }
    }
    if (config.provider == "lm_studio") {
        config.apiBaseUrl = config.lmStudioBaseUrl;
    } else if (config.apiBaseUrl.isEmpty()) {
        config.apiBaseUrl = defaultBaseUrl(config.provider);
    }
    if (mode == AppMode::LocalAISE
        && config.provider != "ollama"
        && config.provider != "llama_cpp") {
        config.provider = "ollama";
    }
    if (runtimeRequiresLlamaCpp()) {
        config.provider = "llama_cpp";
    }
    return config;
}

bool saveConfig(AppMode mode, const AppConfig& config, QString* error) {
    const QString provider = runtimeRequiresLlamaCpp() ? "llama_cpp" : normalizeProvider(config.provider);
    QJsonObject secrets = readJsonObject(defaultSecretsPath(mode));
    if (config.apiKey.trimmed().isEmpty()) {
        secrets.remove(provider);
    } else {
        secrets.insert(provider, encodeSecret(config.apiKey.trimmed()));
    }
    if (!writeJsonObject(defaultSecretsPath(mode), secrets, error)) {
        return false;
    }

    if (mode == AppMode::CloudAI) {
        const QJsonObject previous = readJsonObject(defaultConfigPath(mode));
        QJsonObject providers = previous.value("providers").toObject();
        QJsonObject providerObject = providers.value(provider).toObject();
        providerObject.insert("enabled", true);
        providerObject.insert("base_url", config.apiBaseUrl.trimmed());
        providerObject.insert("model", config.model.trimmed());
        providerObject.insert("api_key", maskApiKey(config.apiKey));
        providers.insert(provider, providerObject);
        QJsonObject cloudConfig = previous;
        cloudConfig.insert("first_run_completed", true);
        cloudConfig.insert("provider", provider);
        cloudConfig.insert("default_model", config.model.trimmed());
        cloudConfig.insert("providers", providers);
        QJsonObject sharedConfig = readJsonObject(QDir(appDataDir(mode)).filePath("config.json"));
        sharedConfig.insert("language", config.language);
        sharedConfig.insert("theme", config.theme);
        sharedConfig.insert("wallpaper_path", config.wallpaperPath);
        if (!writeJsonObject(QDir(appDataDir(mode)).filePath("config.json"), sharedConfig, error)) {
            return false;
        }
        return writeJsonObject(defaultConfigPath(mode), cloudConfig, error);
    }

    QJsonObject object = readJsonObject(defaultConfigPath(mode));
    object.insert("language", config.language);
    object.insert("theme", config.theme);
    object.insert("provider", provider);
    object.insert("last_model", config.model.trimmed());
    object.insert("openai_model", config.model.trimmed());
    object.insert("llamacpp_binary", config.llamaCppBinary.trimmed());
    object.insert("llamacpp_model", config.llamaCppModel.trimmed());
    object.insert("api_base_url", config.apiBaseUrl.trimmed());
    object.insert("lmstudio_base_url", config.lmStudioBaseUrl.trimmed());
    object.insert("wallpaper_path", config.wallpaperPath);
    object.remove("api_key");
    object.remove("cloud_api_key");
    return writeJsonObject(defaultConfigPath(mode), object, error);
}

QString trText(const QString& language, const QString& key) {
    const QString lang = language.toLower();
    const bool en = lang.startsWith("en");
    const bool ja = lang.startsWith("ja");
    const bool de = lang.startsWith("de");
    const bool fr = lang.startsWith("fr");
    if (key == "send") return en ? "Send" : ja ? "送信" : de ? "Senden" : fr ? "Envoyer" : "发送";
    if (key == "new_chat") return en ? "New Chat" : ja ? "新規チャット" : de ? "Neuer Chat" : fr ? "Nouvelle discussion" : "新对话";
    if (key == "settings") return en ? "Settings" : ja ? "設定" : de ? "Einstellungen" : fr ? "Réglages" : "设置";
    if (key == "model") return en ? "Model" : ja ? "モデル" : de ? "Modell" : fr ? "Modèle" : "模型";
    if (key == "thinking") return en ? "Thinking..." : ja ? "考え中..." : de ? "Denke nach..." : fr ? "Réflexion..." : "正在思考...";
    if (key == "missing_key") return en ? "API Key is not configured. Please configure it in Settings." : "当前 Provider 未配置 API Key，请前往设置填写。";
    if (key == "error") return en ? "Error" : ja ? "エラー" : de ? "Fehler" : fr ? "Erreur" : "错误";
    return key;
}

QString maskApiKey(const QString& value) {
    return value.isEmpty() ? QString() : "********";
}

QStringList supportedLanguages() {
    return {"zh_cn", "zh_tw", "en_us", "en_gb", "ja", "fr", "de"};
}

} // namespace nativeai
