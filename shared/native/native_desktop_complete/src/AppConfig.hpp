#pragma once

#include <QString>
#include <QStringList>

namespace nativeai {

enum class AppMode {
    LocalAI,
    LocalAISE,
    CloudAI,
};

struct AppConfig {
    QString language = "zh_cn";
    QString theme = "auto";
    QString provider;
    QString model;
    QString apiBaseUrl;
    QString lmStudioBaseUrl = "http://127.0.0.1:1234/v1";
    QString apiKey;
    QString llamaCppBinary;
    QString llamaCppModel;
    QString wallpaperPath;
    QString appDataDir;
};

QString appName(AppMode mode);
QString versionLabel(AppMode mode);
QString defaultConfigPath(AppMode mode);
QString defaultSecretsPath(AppMode mode);
AppConfig loadConfig(AppMode mode);
bool saveConfig(AppMode mode, const AppConfig& config, QString* error = nullptr);
bool runtimeRequiresLlamaCpp();
QStringList supportedProviders(AppMode mode);
QStringList supportedLanguages();
QString trText(const QString& language, const QString& key);
QString maskApiKey(const QString& value);

} // namespace nativeai
