#pragma once

#include <filesystem>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace cloudai {

constexpr const char* kAppName = "CloudAI";
constexpr const char* kAppVersion = "v1.0.1";
constexpr const char* kMask = "********";

struct Message {
    std::string role;
    std::string content;
};

// Shared Agent wire protocol. Platform front-ends execute actions only after
// applying their own sandbox/confirmation policy.
struct AgentAction {
    std::string action;
    std::string path;
    std::string content;
    std::string command;
    std::string url;
    std::string query;
};

struct ProviderInfo {
    std::string code;
    std::string name;
    std::string baseUrl;
    std::vector<std::string> models;
};

struct ProviderConfig {
    bool enabled = false;
    std::string baseUrl;
    std::string model;
    std::string maskedApiKey;
};

struct CloudConfig {
    bool firstRunCompleted = false;
    std::string provider = "openai_official";
    std::string defaultModel;
    std::map<std::string, ProviderConfig> providers;
};

struct LocalConfig {
    std::string language = "zh_cn";
    std::string theme = "auto";
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

    LocalConfig loadLocalConfig() const;
    void saveLocalConfig(const LocalConfig& config) const;

    CloudConfig loadCloudConfig() const;
    void saveCloudConfig(const CloudConfig& config) const;

    std::string getApiKey(const std::string& provider) const;
    void setApiKey(const std::string& provider, const std::string& apiKey) const;
    void configureProvider(const std::string& provider,
                           const std::string& baseUrl,
                           const std::string& model) const;

    const std::filesystem::path& appDataDir() const { return appDataDir_; }
    std::filesystem::path cloudConfigDir() const;

private:
    std::filesystem::path appDataDir_;
};

class CloudAIClient {
public:
    CloudAIClient(ConfigStore store, std::shared_ptr<IHttpClient> httpClient);

    const std::map<std::string, ProviderInfo>& providers() const;
    std::string text(const LocalConfig& config, const std::string& key) const;

    std::string ask(const std::vector<Message>& messages,
                    const std::vector<std::filesystem::path>& filePaths = {});

    // HarmonyOS ArkTS performs the platform HTTP request and feeds the response
    // back through responseTextFromJson. This keeps transport permissions and
    // lifecycle handling in the platform layer while sharing request semantics.
    HttpRequest prepareRequest(const std::vector<Message>& messages,
                               const std::vector<std::filesystem::path>& filePaths = {});

    std::string fetchUsage();
    void saveProviderApiKey(const std::string& provider, const std::string& apiKey);

private:
    ConfigStore store_;
    std::shared_ptr<IHttpClient> httpClient_;

    std::string buildChatPayload(const std::vector<Message>& messages,
                                 const std::vector<std::filesystem::path>& filePaths,
                                 const std::string& model) const;
    std::vector<Message> messagesWithFiles(const std::vector<Message>& messages,
                                           const std::vector<std::filesystem::path>& filePaths,
                                           const std::string& model) const;
    std::string activeApiKey(const CloudConfig& config) const;
    ProviderConfig activeProviderConfig(const CloudConfig& config) const;
    std::string activeBaseUrl(const CloudConfig& config) const;
    std::string activeModel(const CloudConfig& config) const;
    std::string openAiChatUrl(const std::string& baseUrl) const;
    std::vector<std::string> usageUrls(const CloudConfig& config) const;
};

std::map<std::string, ProviderInfo> defaultProviders();
CloudConfig defaultCloudConfig();
std::string normalizeLanguage(std::string language);
std::string maskKey(const std::string& value);
std::string sanitizeLogText(std::string text, const std::string& secret);
bool contentAllowed(const std::string& text);
std::string moderatedText(const std::string& text);
std::string responseTextFromJson(const std::string& body);
std::string summarizeUsageData(const std::string& body);
std::optional<AgentAction> parseAgentAction(const std::string& body);
std::string agentSystemPrompt(const std::string& userRequest, const std::string& language = "zh_cn");

#ifdef CLOUDAI_USE_LIBCURL
class CurlHttpClient final : public IHttpClient {
public:
    HttpResponse send(const HttpRequest& request) override;
};
#endif

} // namespace cloudai
