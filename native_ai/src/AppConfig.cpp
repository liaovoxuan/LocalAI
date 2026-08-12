#include "AppConfig.hpp"

#include <QDir>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QStandardPaths>

namespace nativeai {

QString appName(AppMode mode) {
    return mode == AppMode::CloudAI ? "CloudAI" : "LocalAI";
}

QString versionLabel(AppMode mode) {
    return "1.0";
}

static QString appDataDir(AppMode mode) {
    QString root = QStandardPaths::writableLocation(QStandardPaths::AppDataLocation);
    if (root.isEmpty()) {
        root = QDir::homePath() + "/." + appName(mode).toLower();
    }
    QDir().mkpath(root);
    return root;
}

QString defaultConfigPath(AppMode mode) {
    if (mode == AppMode::CloudAI) {
        return QDir(appDataDir(mode)).filePath("cloudai_config.json");
    }
    return QDir(appDataDir(mode)).filePath("config.json");
}

AppConfig loadConfig(AppMode mode) {
    AppConfig config;
    config.appDataDir = appDataDir(mode);
    config.apiBaseUrl = mode == AppMode::CloudAI ? "https://api.deepseek.com/v1" : "";
    config.provider = mode == AppMode::CloudAI ? "openai_compatible" : "ollama";
    config.model = mode == AppMode::CloudAI ? "deepseek-chat" : "llama3.1:8b";
    config.llamaCppModel = "";

    QFile file(defaultConfigPath(mode));
    if (!file.open(QIODevice::ReadOnly)) {
        return config;
    }
    const QJsonDocument document = QJsonDocument::fromJson(file.readAll());
    if (!document.isObject()) {
        return config;
    }
    const QJsonObject object = document.object();
    config.language = object.value("language").toString(config.language);
    config.theme = object.value("theme").toString(config.theme);
    config.provider = object.value("provider").toString(config.provider);
    config.model = object.value("model").toString(object.value("openai_model").toString(config.model));
    config.llamaCppBinary = object.value("llamacpp_binary").toString(config.llamaCppBinary);
    config.llamaCppModel = object.value("llamacpp_model").toString(config.llamaCppModel);
    config.apiBaseUrl = object.value("api_base_url").toString(config.apiBaseUrl);
    config.apiKey = object.value("api_key").toString(config.apiKey);
    config.lmStudioBaseUrl = object.value("lmstudio_base_url").toString(config.lmStudioBaseUrl);
    config.wallpaperPath = object.value("wallpaper").toString(config.wallpaperPath);
    return config;
}

bool saveConfig(AppMode mode, const AppConfig& config, QString* error) {
    QJsonObject object{
        {"language", config.language},
        {"theme", config.theme},
        {"provider", config.provider},
        {"model", config.model},
        {"llamacpp_binary", config.llamaCppBinary},
        {"llamacpp_model", config.llamaCppModel},
        {"api_base_url", config.apiBaseUrl},
        {"api_key", config.apiKey},
        {"lmstudio_base_url", config.lmStudioBaseUrl},
        {"wallpaper", config.wallpaperPath},
    };
    QFile file(defaultConfigPath(mode));
    if (!file.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        if (error) {
            *error = file.errorString();
        }
        return false;
    }
    file.write(QJsonDocument(object).toJson(QJsonDocument::Indented));
    return true;
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
