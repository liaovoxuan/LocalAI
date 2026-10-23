#include "AppConfig.hpp"

#include <QCoreApplication>
#include <QFile>
#include <QJsonDocument>
#include <QTemporaryDir>

#include <iostream>

namespace {

bool require(bool condition, const char* message) {
    if (!condition) {
        std::cerr << "FAIL: " << message << '\n';
        return false;
    }
    return true;
}

bool fileContains(const QString& path, const QString& text) {
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        return false;
    }
    return QString::fromUtf8(file.readAll()).contains(text);
}

} // namespace

int main(int argc, char* argv[]) {
    QCoreApplication app(argc, argv);
    QTemporaryDir temporary;
    if (!require(temporary.isValid(), "temporary directory is available")) {
        return 1;
    }
    qputenv("LOCALAI_APP_DATA_DIR", temporary.path().toUtf8());

    if (!require(nativeai::defaultConfigPath(nativeai::AppMode::LocalAI)
                     != nativeai::defaultConfigPath(nativeai::AppMode::LocalAISE),
                 "LocalAI and LocalAI SE use separate configuration paths")) {
        return 1;
    }

    nativeai::AppConfig local;
    local.language = "en_us";
    local.provider = "openai_compatible";
    local.model = "example-model";
    local.apiBaseUrl = "https://example.invalid/v1";
    local.apiKey = "test-local-secret";
    local.llamaCppBinary = "/tmp/llama-cli";
    QString error;
    {
        QFile legacy(nativeai::defaultConfigPath(nativeai::AppMode::LocalAI));
        if (!require(legacy.open(QIODevice::WriteOnly), "legacy config opens")) {
            return 1;
        }
        legacy.write("{\"first_welcome_done\":true,\"api_key\":\"old-secret\"}");
    }
    if (!require(nativeai::saveConfig(nativeai::AppMode::LocalAI, local, &error), "local config saves")) {
        std::cerr << error.toStdString() << '\n';
        return 1;
    }
    if (!require(!fileContains(nativeai::defaultConfigPath(nativeai::AppMode::LocalAI), local.apiKey),
                 "local API key is absent from public config")) {
        return 1;
    }
    if (!require(fileContains(nativeai::defaultConfigPath(nativeai::AppMode::LocalAI), "\"first_welcome_done\": true"),
                 "unmanaged local config remains")) {
        return 1;
    }
    const nativeai::AppConfig loadedLocal = nativeai::loadConfig(nativeai::AppMode::LocalAI);
    if (!require(loadedLocal.provider == local.provider, "local provider round trips")
        || !require(loadedLocal.model == local.model, "local model round trips")
        || !require(loadedLocal.apiKey == local.apiKey, "local API key round trips")) {
        return 1;
    }

    nativeai::AppConfig cloud;
    cloud.language = "zh_cn";
    cloud.provider = "deepseek";
    cloud.model = "deepseek-chat";
    cloud.apiBaseUrl = "https://api.deepseek.com/v1";
    cloud.apiKey = "test-cloud-secret";
    if (!require(nativeai::saveConfig(nativeai::AppMode::CloudAI, cloud, &error), "cloud config saves")) {
        std::cerr << error.toStdString() << '\n';
        return 1;
    }
    if (!require(!fileContains(nativeai::defaultConfigPath(nativeai::AppMode::CloudAI), cloud.apiKey),
                 "cloud API key is absent from public config")) {
        return 1;
    }
    const nativeai::AppConfig loadedCloud = nativeai::loadConfig(nativeai::AppMode::CloudAI);
    if (!require(loadedCloud.provider == cloud.provider, "cloud provider round trips")
        || !require(loadedCloud.model == cloud.model, "cloud model round trips")
        || !require(loadedCloud.apiKey == cloud.apiKey, "cloud API key round trips")) {
        return 1;
    }
    return 0;
}
