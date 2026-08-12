#pragma once

#include <QString>
#include <QStringList>

namespace nativeai {

enum class AppMode {
    LocalAI,
    CloudAI,
};

struct AppConfig {
    QString language = "zh_cn";
    QString theme = "auto";
    QString provider = "ollama";
    QString model = "llama3.1:8b";
    QString llamaCppBinary;
    QString llamaCppModel;
    QString apiBaseUrl;
    QString apiKey;
    QString lmStudioBaseUrl = "http://localhost:1234/v1";
    QString wallpaperPath;
    QString appDataDir;
};

QString appName(AppMode mode);
QString versionLabel(AppMode mode);
QString defaultConfigPath(AppMode mode);
AppConfig loadConfig(AppMode mode);
bool saveConfig(AppMode mode, const AppConfig& config, QString* error = nullptr);
QString trText(const QString& language, const QString& key);
QString maskApiKey(const QString& value);
QStringList supportedLanguages();

} // namespace nativeai
