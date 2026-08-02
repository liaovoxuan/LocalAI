#include "CloudAIHarmony.hpp"

#include <algorithm>
#include <cctype>
#include <fstream>
#include <regex>
#include <sstream>
#include <stdexcept>

#ifdef CLOUDAI_USE_LIBCURL
#include <curl/curl.h>
#endif

namespace cloudai {
namespace {

std::string readText(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) {
        return {};
    }
    std::ostringstream stream;
    stream << input.rdbuf();
    return stream.str();
}

void writeText(const std::filesystem::path& path, const std::string& value) {
    std::filesystem::create_directories(path.parent_path());
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    if (!output) {
        throw std::runtime_error("cannot_write_file");
    }
    output << value;
}

std::string trim(std::string value) {
    auto isSpace = [](unsigned char c) { return std::isspace(c) != 0; };
    value.erase(value.begin(), std::find_if(value.begin(), value.end(), [&](char c) { return !isSpace(c); }));
    value.erase(std::find_if(value.rbegin(), value.rend(), [&](char c) { return !isSpace(c); }).base(), value.end());
    return value;
}

std::string lower(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return value;
}

std::string jsonEscape(const std::string& value) {
    std::string out;
    out.reserve(value.size() + 16);
    for (char ch : value) {
        switch (ch) {
            case '\\': out += "\\\\"; break;
            case '"': out += "\\\""; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (static_cast<unsigned char>(ch) < 0x20) {
                    out += " ";
                } else {
                    out += ch;
                }
        }
    }
    return out;
}

std::string jsonString(const std::string& key, const std::string& body) {
    std::regex pattern("\"" + key + "\"\\s*:\\s*\"((?:\\\\.|[^\"])*)\"");
    std::smatch match;
    if (!std::regex_search(body, match, pattern)) {
        return {};
    }
    std::string value = match[1].str();
    value = std::regex_replace(value, std::regex("\\\\n"), "\n");
    value = std::regex_replace(value, std::regex("\\\\r"), "\r");
    value = std::regex_replace(value, std::regex("\\\\t"), "\t");
    value = std::regex_replace(value, std::regex("\\\\\""), "\"");
    value = std::regex_replace(value, std::regex("\\\\\\\\"), "\\");
    return value;
}

bool jsonBool(const std::string& key, const std::string& body, bool fallback) {
    std::regex pattern("\"" + key + "\"\\s*:\\s*(true|false)");
    std::smatch match;
    if (!std::regex_search(body, match, pattern)) {
        return fallback;
    }
    return match[1].str() == "true";
}

std::optional<std::string> objectForKey(const std::string& body, const std::string& key) {
    std::regex startPattern("\"" + key + "\"\\s*:\\s*\\{");
    std::smatch match;
    if (!std::regex_search(body, match, startPattern)) {
        return std::nullopt;
    }
    size_t pos = static_cast<size_t>(match.position() + match.length() - 1);
    int depth = 0;
    bool inString = false;
    bool escaped = false;
    for (size_t i = pos; i < body.size(); ++i) {
        char ch = body[i];
        if (escaped) {
            escaped = false;
            continue;
        }
        if (ch == '\\') {
            escaped = true;
            continue;
        }
        if (ch == '"') {
            inString = !inString;
            continue;
        }
        if (inString) {
            continue;
        }
        if (ch == '{') {
            depth++;
        } else if (ch == '}') {
            depth--;
            if (depth == 0) {
                return body.substr(pos, i - pos + 1);
            }
        }
    }
    return std::nullopt;
}

std::string providerObject(const std::string& body, const std::string& provider) {
    auto providers = objectForKey(body, "providers");
    if (!providers) {
        return {};
    }
    auto providerObj = objectForKey(*providers, provider);
    return providerObj.value_or("");
}

std::string firstChoiceContent(const std::string& body) {
    std::regex pattern("\"choices\"\\s*:\\s*\\[\\s*\\{.*?\"message\"\\s*:\\s*\\{.*?\"content\"\\s*:\\s*\"((?:\\\\.|[^\"])*)\"", std::regex::ECMAScript);
    std::smatch match;
    if (std::regex_search(body, match, pattern)) {
        return jsonString("content", "{\"content\":\"" + match[1].str() + "\"}");
    }
    return {};
}

std::string readSmallTextFile(const std::filesystem::path& path) {
    static const std::uintmax_t maxBytes = 256 * 1024;
    std::error_code ec;
    if (!std::filesystem::exists(path, ec) || !std::filesystem::is_regular_file(path, ec)) {
        return {};
    }
    if (std::filesystem::file_size(path, ec) > maxBytes) {
        return {};
    }
    return readText(path);
}

} // namespace

std::map<std::string, ProviderInfo> defaultProviders() {
    return {
        {"openai_official", {"openai_official", "OpenAI", "https://api.openai.com/v1", {"gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini"}}},
        {"openai_compatible", {"openai_compatible", "OpenAI Compatible", "http://localhost:8000/v1", {"gpt-4.1-mini", "qwen-plus", "deepseek-chat"}}},
        {"openrouter", {"openrouter", "OpenRouter", "https://openrouter.ai/api/v1", {"openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet"}}},
        {"deepseek", {"deepseek", "DeepSeek", "https://api.deepseek.com/v1", {"deepseek-chat", "deepseek-reasoner"}}},
        {"siliconflow", {"siliconflow", "SiliconFlow", "https://api.siliconflow.cn/v1", {"Qwen/Qwen2.5-7B-Instruct", "deepseek-ai/DeepSeek-V3"}}},
    };
}

CloudConfig defaultCloudConfig() {
    CloudConfig config;
    auto providers = defaultProviders();
    config.defaultModel = providers["openai_official"].models.front();
    for (const auto& [code, info] : providers) {
        ProviderConfig provider;
        provider.enabled = code == config.provider;
        provider.baseUrl = info.baseUrl;
        provider.model = info.models.empty() ? "" : info.models.front();
        config.providers[code] = provider;
    }
    return config;
}

std::string normalizeLanguage(std::string language) {
    language = lower(trim(language));
    std::replace(language.begin(), language.end(), '_', '-');
    static const std::map<std::string, std::string> aliases = {
        {"zh-cn", "zh_cn"}, {"zh", "zh_cn"}, {"zh-hans", "zh_cn"},
        {"zh-tw", "zh_tw"}, {"zh-hant", "zh_tw"},
        {"en", "en_us"}, {"en-us", "en_us"}, {"en-gb", "en_uk"}, {"en-uk", "en_uk"},
        {"ja", "ja"}, {"fr", "fr"}, {"de", "de"}, {"ko", "ko"}, {"es", "es"},
        {"it", "it"}, {"pt", "pt"}, {"ru", "ru"}, {"nl", "nl"}, {"sv", "sv"},
        {"da", "da"}, {"fi", "fi"}, {"no", "no"}, {"tr", "tr"}, {"pl", "pl"},
        {"cs", "cs"}, {"uk", "uk"}, {"el", "el"}, {"ar", "ar"}, {"mn", "mn"},
        {"th", "th"}, {"vi", "vi"}, {"id", "id"}, {"ms", "ms"}, {"hi", "hi"},
    };
    auto found = aliases.find(language);
    return found == aliases.end() ? "zh_cn" : found->second;
}

std::string maskKey(const std::string& value) {
    return value.empty() ? "" : kMask;
}

std::string sanitizeLogText(std::string text, const std::string& secret) {
    if (secret.empty()) {
        return text;
    }
    size_t pos = 0;
    while ((pos = text.find(secret, pos)) != std::string::npos) {
        text.replace(pos, secret.size(), kMask);
        pos += std::string(kMask).size();
    }
    return text;
}

bool contentAllowed(const std::string& text) {
    const std::string sample = lower(text);
    static const std::vector<std::string> blocked = {
        "制作炸弹", "诈骗教程", "绕过执法", "儿童色情", "terrorist manual",
        "make a bomb", "credit card dump", "child sexual"
    };
    for (const auto& item : blocked) {
        if (sample.find(lower(item)) != std::string::npos) {
            return false;
        }
    }
    return true;
}

std::string moderatedText(const std::string& text) {
    return contentAllowed(text) ? text : "这个话题不合适，换一个聊聊吧！";
}

std::string responseTextFromJson(const std::string& body) {
    std::string content = firstChoiceContent(body);
    if (!content.empty()) {
        return content;
    }
    content = jsonString("text", body);
    if (!content.empty()) {
        return content;
    }
    return jsonString("content", body);
}

std::string summarizeUsageData(const std::string& body) {
    for (const auto& key : {"total_usage", "total_credits", "balance", "total_granted", "total_used", "limit", "used", "remaining", "total"}) {
        auto value = jsonString(key, body);
        if (!value.empty()) {
            return std::string(key) + "=" + value;
        }
        std::regex numberPattern("\"" + std::string(key) + "\"\\s*:\\s*([0-9.]+)");
        std::smatch match;
        if (std::regex_search(body, match, numberPattern)) {
            return std::string(key) + "=" + match[1].str();
        }
    }
    if (body.find("\"data\"") != std::string::npos) {
        return "usage data returned";
    }
    return {};
}

ConfigStore::ConfigStore(std::filesystem::path appDataDir) : appDataDir_(std::move(appDataDir)) {}

std::filesystem::path ConfigStore::cloudConfigDir() const {
    return appDataDir_ / "config";
}

LocalConfig ConfigStore::loadLocalConfig() const {
    LocalConfig config;
    const std::string body = readText(appDataDir_ / "config.json");
    if (!body.empty()) {
        auto language = jsonString("language", body);
        auto theme = jsonString("theme", body);
        if (!language.empty()) {
            config.language = normalizeLanguage(language);
        }
        if (!theme.empty()) {
            config.theme = theme;
        }
    }
    return config;
}

void ConfigStore::saveLocalConfig(const LocalConfig& config) const {
    std::ostringstream out;
    out << "{\n"
        << "  \"language\": \"" << jsonEscape(normalizeLanguage(config.language)) << "\",\n"
        << "  \"theme\": \"" << jsonEscape(config.theme.empty() ? "auto" : config.theme) << "\"\n"
        << "}\n";
    writeText(appDataDir_ / "config.json", out.str());
}

CloudConfig ConfigStore::loadCloudConfig() const {
    CloudConfig config = defaultCloudConfig();
    const std::string body = readText(cloudConfigDir() / "cloudai_config.json");
    if (!body.empty()) {
        auto provider = jsonString("provider", body);
        auto defaultModel = jsonString("default_model", body);
        config.firstRunCompleted = jsonBool("first_run_completed", body, config.firstRunCompleted);
        if (!provider.empty() && config.providers.count(provider)) {
            config.provider = provider;
        }
        if (!defaultModel.empty()) {
            config.defaultModel = defaultModel;
        }
        for (auto& [code, item] : config.providers) {
            std::string providerBody = providerObject(body, code);
            if (providerBody.empty()) {
                continue;
            }
            item.enabled = jsonBool("enabled", providerBody, item.enabled);
            auto base = jsonString("base_url", providerBody);
            auto model = jsonString("model", providerBody);
            if (!base.empty()) {
                item.baseUrl = base;
            }
            if (!model.empty()) {
                item.model = model;
            }
            item.maskedApiKey = maskKey(getApiKey(code));
        }
    }
    if (!config.providers.count(config.provider)) {
        config.provider = "openai_official";
    }
    return config;
}

void ConfigStore::saveCloudConfig(const CloudConfig& config) const {
    std::ostringstream out;
    out << "{\n";
    out << "  \"first_run_completed\": " << (config.firstRunCompleted ? "true" : "false") << ",\n";
    out << "  \"provider\": \"" << jsonEscape(config.provider) << "\",\n";
    out << "  \"default_model\": \"" << jsonEscape(config.defaultModel) << "\",\n";
    out << "  \"providers\": {\n";
    bool first = true;
    for (const auto& [code, item] : config.providers) {
        if (!first) {
            out << ",\n";
        }
        first = false;
        out << "    \"" << jsonEscape(code) << "\": {\n"
            << "      \"enabled\": " << (item.enabled ? "true" : "false") << ",\n"
            << "      \"base_url\": \"" << jsonEscape(item.baseUrl) << "\",\n"
            << "      \"model\": \"" << jsonEscape(item.model) << "\",\n"
            << "      \"api_key\": \"" << jsonEscape(maskKey(getApiKey(code))) << "\"\n"
            << "    }";
    }
    out << "\n  }\n}\n";
    writeText(cloudConfigDir() / "cloudai_config.json", out.str());
}

std::string ConfigStore::getApiKey(const std::string& provider) const {
    const std::string body = readText(cloudConfigDir() / "cloudai_secrets.json");
    return jsonString(provider, body);
}

void ConfigStore::setApiKey(const std::string& provider, const std::string& apiKey) const {
    std::map<std::string, std::string> secrets;
    const std::string body = readText(cloudConfigDir() / "cloudai_secrets.json");
    for (const auto& [code, _] : defaultProviders()) {
        auto value = jsonString(code, body);
        if (!value.empty()) {
            secrets[code] = value;
        }
    }
    if (apiKey.empty()) {
        secrets.erase(provider);
    } else {
        secrets[provider] = apiKey;
    }
    std::ostringstream out;
    out << "{\n";
    bool first = true;
    for (const auto& [code, value] : secrets) {
        if (!first) {
            out << ",\n";
        }
        first = false;
        out << "  \"" << jsonEscape(code) << "\": \"" << jsonEscape(value) << "\"";
    }
    out << "\n}\n";
    writeText(cloudConfigDir() / "cloudai_secrets.json", out.str());
}

CloudAIClient::CloudAIClient(ConfigStore store, std::shared_ptr<IHttpClient> httpClient)
    : store_(std::move(store)), httpClient_(std::move(httpClient)) {}

const std::map<std::string, ProviderInfo>& CloudAIClient::providers() const {
    static const auto providers = defaultProviders();
    return providers;
}

std::string CloudAIClient::text(const LocalConfig& config, const std::string& key) const {
    const std::string lang = normalizeLanguage(config.language);
    static const std::map<std::string, std::map<std::string, std::string>> table = {
        {"zh_cn", {{"send", "发送"}, {"settings", "设置"}, {"missing_api_key", "当前 Provider 尚未配置 API Key，请前往设置填写。"}, {"thinking", "正在思考..."}}},
        {"zh_tw", {{"send", "傳送"}, {"settings", "設定"}, {"missing_api_key", "目前 Provider 尚未設定 API Key，請前往設定填寫。"}, {"thinking", "正在思考..."}}},
        {"en_us", {{"send", "Send"}, {"settings", "Settings"}, {"missing_api_key", "The current provider has no API key configured. Open Settings to add one."}, {"thinking", "Thinking..."}}},
        {"en_uk", {{"send", "Send"}, {"settings", "Settings"}, {"missing_api_key", "The current provider has no API key configured. Open Settings to add one."}, {"thinking", "Thinking..."}}},
        {"ja", {{"send", "送信"}, {"settings", "設定"}, {"missing_api_key", "現在の Provider に API Key が設定されていません。設定で追加してください。"}, {"thinking", "考えています..."}}},
        {"fr", {{"send", "Envoyer"}, {"settings", "Réglages"}, {"missing_api_key", "Aucune clé API n'est configurée pour le fournisseur actuel."}, {"thinking", "Réflexion..."}}},
        {"de", {{"send", "Senden"}, {"settings", "Einstellungen"}, {"missing_api_key", "Für den aktuellen Anbieter ist kein API-Schlüssel konfiguriert."}, {"thinking", "Denke nach..."}}},
    };
    auto langIt = table.find(lang);
    const auto& values = langIt == table.end() ? table.at("zh_cn") : langIt->second;
    auto found = values.find(key);
    if (found != values.end()) {
        return found->second;
    }
    auto fallback = table.at("zh_cn").find(key);
    return fallback == table.at("zh_cn").end() ? key : fallback->second;
}

std::string CloudAIClient::activeApiKey(const CloudConfig& config) const {
    return store_.getApiKey(config.provider);
}

ProviderConfig CloudAIClient::activeProviderConfig(const CloudConfig& config) const {
    auto found = config.providers.find(config.provider);
    if (found != config.providers.end()) {
        return found->second;
    }
    return defaultCloudConfig().providers.at("openai_official");
}

std::string CloudAIClient::activeBaseUrl(const CloudConfig& config) const {
    auto item = activeProviderConfig(config);
    if (!item.baseUrl.empty()) {
        return item.baseUrl;
    }
    auto found = providers().find(config.provider);
    return found == providers().end() ? "" : found->second.baseUrl;
}

std::string CloudAIClient::activeModel(const CloudConfig& config) const {
    auto item = activeProviderConfig(config);
    if (!item.model.empty()) {
        return item.model;
    }
    if (!config.defaultModel.empty()) {
        return config.defaultModel;
    }
    auto found = providers().find(config.provider);
    return found == providers().end() || found->second.models.empty() ? "" : found->second.models.front();
}

std::string CloudAIClient::openAiChatUrl(const std::string& baseUrl) const {
    std::string base = trim(baseUrl);
    while (!base.empty() && base.back() == '/') {
        base.pop_back();
    }
    if (base.empty()) {
        return {};
    }
    if (base.size() >= 17 && base.substr(base.size() - 17) == "/chat/completions") {
        return base;
    }
    return base + "/chat/completions";
}

std::vector<Message> CloudAIClient::messagesWithFiles(const std::vector<Message>& messages,
                                                      const std::vector<std::filesystem::path>& filePaths,
                                                      const std::string&) const {
    std::vector<Message> result = messages;
    if (result.empty() || filePaths.empty()) {
        return result;
    }
    std::ostringstream context;
    for (const auto& path : filePaths) {
        auto text = readSmallTextFile(path);
        if (!text.empty()) {
            context << "\n\n[File: " << path.filename().string() << "]\n" << text;
        }
    }
    if (!context.str().empty()) {
        result.back().content += "\n\nUse the following file content to answer:" + context.str();
    }
    return result;
}

std::string CloudAIClient::buildChatPayload(const std::vector<Message>& messages,
                                            const std::vector<std::filesystem::path>& filePaths,
                                            const std::string& model) const {
    auto finalMessages = messagesWithFiles(messages, filePaths, model);
    std::ostringstream out;
    out << "{";
    out << "\"model\":\"" << jsonEscape(model) << "\",";
    out << "\"temperature\":0.7,";
    out << "\"stream\":false,";
    out << "\"messages\":[";
    for (size_t i = 0; i < finalMessages.size(); ++i) {
        if (i) {
            out << ",";
        }
        out << "{\"role\":\"" << jsonEscape(finalMessages[i].role)
            << "\",\"content\":\"" << jsonEscape(finalMessages[i].content) << "\"}";
    }
    out << "]}";
    return out.str();
}

std::string CloudAIClient::ask(const std::vector<Message>& messages,
                               const std::vector<std::filesystem::path>& filePaths) {
    std::ostringstream combined;
    for (const auto& message : messages) {
        combined << message.role << ": " << message.content << "\n";
    }
    if (!contentAllowed(combined.str())) {
        return "这个话题不合适，换一个聊聊吧！";
    }
    CloudConfig config = store_.loadCloudConfig();
    std::string apiKey = activeApiKey(config);
    if (apiKey.empty()) {
        throw std::runtime_error("missing_api_key");
    }
    std::string model = activeModel(config);
    std::string url = openAiChatUrl(activeBaseUrl(config));
    if (model.empty() || url.empty()) {
        throw std::runtime_error("invalid_provider_config");
    }
    HttpRequest request;
    request.method = "POST";
    request.url = url;
    request.timeoutSeconds = 180;
    request.headers["Content-Type"] = "application/json";
    request.headers["Authorization"] = "Bearer " + apiKey;
    request.body = buildChatPayload(messages, filePaths, model);
    HttpResponse response = httpClient_->send(request);
    if (response.statusCode < 200 || response.statusCode >= 300) {
        throw std::runtime_error("http_error_" + std::to_string(response.statusCode));
    }
    return moderatedText(responseTextFromJson(response.body));
}

std::vector<std::string> CloudAIClient::usageUrls(const CloudConfig& config) const {
    std::string base = activeBaseUrl(config);
    while (!base.empty() && base.back() == '/') {
        base.pop_back();
    }
    if (base.size() >= 17 && base.substr(base.size() - 17) == "/chat/completions") {
        base = base.substr(0, base.size() - 17);
    }
    std::vector<std::string> urls;
    if (config.provider == "openai_official") {
        urls.push_back("https://api.openai.com/v1/dashboard/billing/usage");
        urls.push_back("https://api.openai.com/v1/usage");
    } else if (config.provider == "openrouter") {
        urls.push_back("https://openrouter.ai/api/v1/credits");
    } else if (config.provider == "deepseek") {
        urls.push_back("https://api.deepseek.com/user/balance");
    } else if (config.provider == "siliconflow") {
        urls.push_back("https://api.siliconflow.cn/v1/user/info");
        urls.push_back("https://api.siliconflow.cn/v1/user/balance");
    }
    if (!base.empty()) {
        urls.push_back(base + "/usage");
        urls.push_back(base + "/dashboard/billing/usage");
        urls.push_back(base + "/user/usage");
    }
    return urls;
}

std::string CloudAIClient::fetchUsage() {
    CloudConfig config = store_.loadCloudConfig();
    std::string apiKey = activeApiKey(config);
    if (apiKey.empty()) {
        throw std::runtime_error("missing_api_key");
    }
    for (const auto& url : usageUrls(config)) {
        HttpRequest request;
        request.method = "GET";
        request.url = url;
        request.timeoutSeconds = 20;
        request.headers["Content-Type"] = "application/json";
        request.headers["Authorization"] = "Bearer " + apiKey;
        HttpResponse response = httpClient_->send(request);
        if (response.statusCode == 404 || response.statusCode == 405) {
            continue;
        }
        if (response.statusCode >= 200 && response.statusCode < 300) {
            auto summary = summarizeUsageData(response.body);
            if (!summary.empty()) {
                return summary;
            }
        }
    }
    return {};
}

void CloudAIClient::saveProviderApiKey(const std::string& provider, const std::string& apiKey) {
    store_.setApiKey(provider, apiKey);
}

#ifdef CLOUDAI_USE_LIBCURL
namespace {
size_t curlWrite(char* ptr, size_t size, size_t nmemb, void* userdata) {
    auto* out = static_cast<std::string*>(userdata);
    out->append(ptr, size * nmemb);
    return size * nmemb;
}
} // namespace

HttpResponse CurlHttpClient::send(const HttpRequest& request) {
    CURL* curl = curl_easy_init();
    if (!curl) {
        throw std::runtime_error("curl_init_failed");
    }
    std::string body;
    struct curl_slist* headers = nullptr;
    for (const auto& [key, value] : request.headers) {
        std::string header = key + ": " + value;
        headers = curl_slist_append(headers, header.c_str());
    }
    curl_easy_setopt(curl, CURLOPT_URL, request.url.c_str());
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, request.timeoutSeconds);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, curlWrite);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &body);
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    if (request.method == "POST") {
        curl_easy_setopt(curl, CURLOPT_POST, 1L);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, request.body.c_str());
    }
    CURLcode code = curl_easy_perform(curl);
    long status = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);
    if (code != CURLE_OK) {
        throw std::runtime_error(curl_easy_strerror(code));
    }
    return {status, body};
}
#endif

} // namespace cloudai

