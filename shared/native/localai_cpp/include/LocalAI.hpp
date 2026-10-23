#pragma once

#include <filesystem>
#include <map>
#include <memory>
#include <string>
#include <vector>

namespace localai {

constexpr const char* kAppName = "LocalAI";
constexpr const char* kAppVersion = "1.0";

struct Message {
    std::string role;
    std::string content;
};

struct ProviderInfo {
    std::string code;
    std::string name;
    std::string baseUrl;
    std::vector<std::string> models;
};

struct AppConfig {
    std::string language = "zh_cn";
    std::string theme = "auto";
    std::string provider = "ollama";
    std::string lastModel;
};

struct HttpResponse {
    long statusCode = 0;
    std::string body;
};

struct HttpRequest {
    std::string method;
    std::string url;
    std::map<std::string, std::string> headers;
    std::string body;
    long timeoutSeconds = 60;
};

class IHttpClient {
public:
    virtual ~IHttpClient() = default;
    virtual HttpResponse send(const HttpRequest& request) = 0;
};

class ConfigStore {
public:
    explicit ConfigStore(std::filesystem::path appDataDir);

    AppConfig loadLocalConfig() const;
    void saveLocalConfig(const AppConfig& cfg) const;

    const std::filesystem::path& appDataDir() const { return appDataDir_; }

private:
    std::filesystem::path appDataDir_;
};

class LocalAIClient {
public:
    LocalAIClient(ConfigStore store, std::shared_ptr<IHttpClient> httpClient);

    std::string ask(const std::vector<Message>& messages,
                    const std::vector<std::filesystem::path>& filePaths = {});

    const std::map<std::string, ProviderInfo>& providers() const;

private:
    ConfigStore store_;
    std::shared_ptr<IHttpClient> httpClient_;

    std::string buildOllamaPayload(const std::vector<Message>& messages, const std::string& model) const;
};

std::map<std::string, ProviderInfo> defaultProviders();

#ifdef LOCALAI_USE_LIBCURL
std::shared_ptr<IHttpClient> makeCurlHttpClient();
#endif

} // namespace localai
