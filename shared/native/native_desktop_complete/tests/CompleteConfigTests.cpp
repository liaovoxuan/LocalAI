#include "AppConfig.hpp"

#include <QCoreApplication>
#include <QTemporaryDir>
#include <iostream>

int main(int argc, char* argv[]) {
    QCoreApplication app(argc, argv);
    QTemporaryDir temporary;
    if (!temporary.isValid()) return 1;
    qputenv("LOCALAI_APP_DATA_DIR", temporary.path().toUtf8());
    nativeai::AppConfig config;
    config.provider = "llama_cpp";
    config.model = "example.gguf";
    config.llamaCppModel = "/tmp/example.gguf";
    config.apiKey = "secret";
    QString error;
    if (!nativeai::saveConfig(nativeai::AppMode::LocalAI, config, &error)) { std::cerr << error.toStdString() << '\n'; return 1; }
    const nativeai::AppConfig loaded = nativeai::loadConfig(nativeai::AppMode::LocalAI);
    if (loaded.provider != config.provider || loaded.llamaCppModel != config.llamaCppModel || loaded.apiKey != config.apiKey) return 1;
    if (!nativeai::supportedProviders(nativeai::AppMode::LocalAISE).contains("llama_cpp")) return 1;
    if (!nativeai::runtimeRequiresLlamaCpp() && nativeai::supportedProviders(nativeai::AppMode::LocalAISE).contains("openai_official")) return 1;
    return 0;
}
