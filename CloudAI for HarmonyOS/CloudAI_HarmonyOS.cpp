// CloudAI_HarmonyOS.cpp
// Direct C++ translation of the core logic from cloud_ai.py.
// Packaging json5 files are intentionally not included yet.

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <regex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace fs = std::filesystem;

static const std::string APP_NAME = "CloudAI";
static const std::string APP_VERSION = "1.0";
static const std::string LOCALAI_APP_NAME = "LocalAI";
static const std::string MASK = "********";
static const std::string MODERATION_BLOCK_MESSAGE = "这个话题不合适，换一个聊聊吧！";

struct Message {
    std::string role;
    std::string content;
};

struct ProviderInfo {
    std::string name;
    std::string base_url;
    std::vector<std::string> models;
};

struct ProviderConfig {
    bool enabled = false;
    std::string base_url;
    std::string model;
    std::string api_key;
};

struct CloudConfig {
    bool first_run_completed = false;
    std::string provider = "openai_official";
    std::string default_model = "gpt-4.1-mini";
    std::map<std::string, ProviderConfig> providers;
};

struct LocalConfig {
    std::string language = "zh_cn";
    std::string theme = "auto";
};

struct HttpResponse {
    long status_code = 0;
    std::string body;
};

class HttpClient {
public:
    virtual ~HttpClient() = default;
    virtual HttpResponse get(const std::string& url,
                             const std::map<std::string, std::string>& headers,
                             long timeout_seconds) = 0;
    virtual HttpResponse post(const std::string& url,
                              const std::map<std::string, std::string>& headers,
                              const std::string& body,
                              long timeout_seconds) = 0;
};

static std::map<std::string, ProviderInfo> CLOUD_PROVIDERS = {
    {"openai_official", {"OpenAI", "https://api.openai.com/v1", {"gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini"}}},
    {"openai_compatible", {"OpenAI Compatible API", "http://localhost:8000/v1", {"gpt-4.1-mini", "qwen-plus", "deepseek-chat"}}},
    {"openrouter", {"OpenRouter", "https://openrouter.ai/api/v1", {"openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet"}}},
    {"deepseek", {"DeepSeek", "https://api.deepseek.com/v1", {"deepseek-chat", "deepseek-reasoner"}}},
    {"siliconflow", {"SiliconFlow", "https://api.siliconflow.cn/v1", {"Qwen/Qwen2.5-7B-Instruct", "deepseek-ai/DeepSeek-V3"}}},
};

static std::map<std::string, std::map<std::string, std::string>> CLOUD_TEXT = {
    {"zh_cn", {{"cloud_send", "发送"}, {"cloud_settings", "设置"}, {"cloud_no_key", "当前 Provider 尚未配置 API Key，请前往设置填写。"}, {"cloud_usage_unavailable", "无法从当前 Provider 获取用量。"}}},
    {"zh_tw", {{"cloud_send", "傳送"}, {"cloud_settings", "設定"}, {"cloud_no_key", "目前 Provider 尚未設定 API Key，請前往設定填寫。"}, {"cloud_usage_unavailable", "無法從目前 Provider 取得用量。"}}},
    {"en_us", {{"cloud_send", "Send"}, {"cloud_settings", "Settings"}, {"cloud_no_key", "The current provider has no API Key configured. Open Settings to configure one."}, {"cloud_usage_unavailable", "Usage is unavailable from the current provider."}}},
    {"en_uk", {{"cloud_send", "Send"}, {"cloud_settings", "Settings"}, {"cloud_no_key", "The current provider has no API Key configured. Open Settings to configure one."}, {"cloud_usage_unavailable", "Usage is unavailable from the current provider."}}},
    {"ja", {{"cloud_send", "送信"}, {"cloud_settings", "設定"}, {"cloud_no_key", "現在の Provider に API Key が設定されていません。"}, {"cloud_usage_unavailable", "現在の Provider から使用量を取得できません。"}}},
    {"fr", {{"cloud_send", "Envoyer"}, {"cloud_settings", "Réglages"}, {"cloud_no_key", "Aucune clé API n'est configurée pour le fournisseur actuel."}, {"cloud_usage_unavailable", "L'utilisation n'est pas disponible."}}},
    {"de", {{"cloud_send", "Senden"}, {"cloud_settings", "Einstellungen"}, {"cloud_no_key", "Für den aktuellen Provider ist kein API-Schlüssel konfiguriert."}, {"cloud_usage_unavailable", "Nutzungsdaten sind nicht verfügbar."}}},
};

static std::string trim(std::string value) {
    auto space = [](unsigned char c) { return std::isspace(c) != 0; };
    value.erase(value.begin(), std::find_if(value.begin(), value.end(), [&](char c) { return !space(c); }));
    value.erase(std::find_if(value.rbegin(), value.rend(), [&](char c) { return !space(c); }).base(), value.end());
    return value;
}

static std::string lower(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return value;
}

static std::string read_text(const fs::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) {
        return "";
    }
    std::ostringstream stream;
    stream << input.rdbuf();
    return stream.str();
}

static void write_text(const fs::path& path, const std::string& text) {
    fs::create_directories(path.parent_path());
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    if (!output) {
        throw std::runtime_error("cannot_write_file");
    }
    output << text;
}

static std::string json_escape(const std::string& value) {
    std::string out;
    for (char ch : value) {
        switch (ch) {
            case '\\': out += "\\\\"; break;
            case '"': out += "\\\""; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default: out += ch; break;
        }
    }
    return out;
}

static std::string json_string(const std::string& body, const std::string& key) {
    std::regex pattern("\"" + key + "\"\\s*:\\s*\"((?:\\\\.|[^\"])*)\"");
    std::smatch match;
    if (!std::regex_search(body, match, pattern)) {
        return "";
    }
    std::string value = match[1].str();
    value = std::regex_replace(value, std::regex("\\\\n"), "\n");
    value = std::regex_replace(value, std::regex("\\\\r"), "\r");
    value = std::regex_replace(value, std::regex("\\\\t"), "\t");
    value = std::regex_replace(value, std::regex("\\\\\""), "\"");
    value = std::regex_replace(value, std::regex("\\\\\\\\"), "\\");
    return value;
}

static bool json_bool(const std::string& body, const std::string& key, bool fallback) {
    std::regex pattern("\"" + key + "\"\\s*:\\s*(true|false)");
    std::smatch match;
    if (!std::regex_search(body, match, pattern)) {
        return fallback;
    }
    return match[1].str() == "true";
}

static std::string object_for_key(const std::string& body, const std::string& key) {
    std::regex start_pattern("\"" + key + "\"\\s*:\\s*\\{");
    std::smatch match;
    if (!std::regex_search(body, match, start_pattern)) {
        return "";
    }
    size_t pos = static_cast<size_t>(match.position() + match.length() - 1);
    int depth = 0;
    bool in_string = false;
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
            in_string = !in_string;
            continue;
        }
        if (in_string) {
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
    return "";
}

static std::string normalize_language(std::string value) {
    value = lower(trim(value));
    std::replace(value.begin(), value.end(), '_', '-');
    static std::map<std::string, std::string> aliases = {
        {"zh", "zh_cn"}, {"zh-cn", "zh_cn"}, {"zh-hans", "zh_cn"},
        {"zh-tw", "zh_tw"}, {"zh-hant", "zh_tw"},
        {"en", "en_us"}, {"en-us", "en_us"}, {"en-gb", "en_uk"}, {"en-uk", "en_uk"},
        {"ja", "ja"}, {"fr", "fr"}, {"de", "de"}
    };
    auto found = aliases.find(value);
    return found == aliases.end() ? "zh_cn" : found->second;
}

static std::string mask_key(const std::string& value) {
    return value.empty() ? "" : MASK;
}

static fs::path get_app_data_dir() {
    const char* override_dir = std::getenv("CLOUDAI_DATA_DIR");
    if (override_dir && *override_dir) {
        return fs::path(override_dir);
    }
    const char* home = std::getenv("HOME");
    if (home && *home) {
        return fs::path(home) / ".local" / "share" / LOCALAI_APP_NAME;
    }
    return fs::current_path() / LOCALAI_APP_NAME;
}

static fs::path cloud_config_dir() {
    return get_app_data_dir() / "config";
}

static fs::path local_config_file() {
    return get_app_data_dir() / "config.json";
}

static fs::path cloud_config_file() {
    return cloud_config_dir() / "cloudai_config.json";
}

static fs::path cloud_secret_file() {
    return cloud_config_dir() / "cloudai_secrets.json";
}

static void ensure_cloud_dirs() {
    fs::create_directories(get_app_data_dir() / "logs");
    fs::create_directories(cloud_config_dir());
    fs::create_directories(get_app_data_dir() / "cloud_chats");
}

static LocalConfig load_config() {
    ensure_cloud_dirs();
    LocalConfig config;
    std::string body = read_text(local_config_file());
    if (!body.empty()) {
        std::string language = json_string(body, "language");
        std::string theme = json_string(body, "theme");
        if (!language.empty()) {
            config.language = normalize_language(language);
        }
        if (!theme.empty()) {
            config.theme = theme;
        }
    }
    return config;
}

static void save_config(const LocalConfig& config) {
    ensure_cloud_dirs();
    std::ostringstream out;
    out << "{\n";
    out << "  \"language\": \"" << json_escape(normalize_language(config.language)) << "\",\n";
    out << "  \"theme\": \"" << json_escape(config.theme.empty() ? "auto" : config.theme) << "\"\n";
    out << "}\n";
    write_text(local_config_file(), out.str());
}

static std::string get_api_key(const std::string& provider) {
    ensure_cloud_dirs();
    return json_string(read_text(cloud_secret_file()), provider);
}

static void set_api_key(const std::string& provider, const std::string& api_key) {
    ensure_cloud_dirs();
    std::map<std::string, std::string> secrets;
    std::string body = read_text(cloud_secret_file());
    for (const auto& item : CLOUD_PROVIDERS) {
        std::string value = json_string(body, item.first);
        if (!value.empty()) {
            secrets[item.first] = value;
        }
    }
    if (api_key.empty()) {
        secrets.erase(provider);
    } else {
        secrets[provider] = api_key;
    }
    std::ostringstream out;
    out << "{\n";
    bool first = true;
    for (const auto& item : secrets) {
        if (!first) {
            out << ",\n";
        }
        first = false;
        out << "  \"" << json_escape(item.first) << "\": \"" << json_escape(item.second) << "\"";
    }
    out << "\n}\n";
    write_text(cloud_secret_file(), out.str());
}

static CloudConfig default_cloud_config() {
    CloudConfig config;
    for (const auto& item : CLOUD_PROVIDERS) {
        ProviderConfig provider;
        provider.enabled = item.first == config.provider;
        provider.base_url = item.second.base_url;
        provider.model = item.second.models.empty() ? "" : item.second.models.front();
        provider.api_key = "";
        config.providers[item.first] = provider;
    }
    return config;
}

static CloudConfig load_cloud_config() {
    ensure_cloud_dirs();
    CloudConfig config = default_cloud_config();
    std::string body = read_text(cloud_config_file());
    if (!body.empty()) {
        std::string provider = json_string(body, "provider");
        std::string default_model = json_string(body, "default_model");
        config.first_run_completed = json_bool(body, "first_run_completed", config.first_run_completed);
        if (CLOUD_PROVIDERS.count(provider)) {
            config.provider = provider;
        }
        if (!default_model.empty()) {
            config.default_model = default_model;
        }
        std::string providers_body = object_for_key(body, "providers");
        for (auto& item : config.providers) {
            std::string provider_body = object_for_key(providers_body, item.first);
            if (provider_body.empty()) {
                continue;
            }
            item.second.enabled = json_bool(provider_body, "enabled", item.second.enabled);
            std::string base_url = json_string(provider_body, "base_url");
            std::string model = json_string(provider_body, "model");
            if (!base_url.empty()) {
                item.second.base_url = base_url;
            }
            if (!model.empty()) {
                item.second.model = model;
            }
            item.second.api_key = mask_key(get_api_key(item.first));
        }
    }
    if (!CLOUD_PROVIDERS.count(config.provider)) {
        config.provider = "openai_official";
    }
    return config;
}

static void save_cloud_config(const CloudConfig& config) {
    ensure_cloud_dirs();
    std::ostringstream out;
    out << "{\n";
    out << "  \"first_run_completed\": " << (config.first_run_completed ? "true" : "false") << ",\n";
    out << "  \"provider\": \"" << json_escape(config.provider) << "\",\n";
    out << "  \"default_model\": \"" << json_escape(config.default_model) << "\",\n";
    out << "  \"providers\": {\n";
    bool first = true;
    for (const auto& item : config.providers) {
        if (!first) {
            out << ",\n";
        }
        first = false;
        out << "    \"" << json_escape(item.first) << "\": {\n";
        out << "      \"enabled\": " << (item.second.enabled ? "true" : "false") << ",\n";
        out << "      \"base_url\": \"" << json_escape(item.second.base_url) << "\",\n";
        out << "      \"model\": \"" << json_escape(item.second.model) << "\",\n";
        out << "      \"api_key\": \"" << json_escape(mask_key(get_api_key(item.first))) << "\"\n";
        out << "    }";
    }
    out << "\n  }\n";
    out << "}\n";
    write_text(cloud_config_file(), out.str());
}

static std::string cloud_text(const LocalConfig& config, const std::string& key) {
    std::string language = normalize_language(config.language);
    auto lang_it = CLOUD_TEXT.find(language);
    if (lang_it == CLOUD_TEXT.end()) {
        lang_it = CLOUD_TEXT.find("zh_cn");
    }
    auto text_it = lang_it->second.find(key);
    if (text_it != lang_it->second.end()) {
        return text_it->second;
    }
    return key;
}

static std::string openai_chat_url(std::string base_url) {
    base_url = trim(base_url);
    while (!base_url.empty() && base_url.back() == '/') {
        base_url.pop_back();
    }
    if (base_url.empty()) {
        return "";
    }
    const std::string suffix = "/chat/completions";
    if (base_url.size() >= suffix.size() && base_url.substr(base_url.size() - suffix.size()) == suffix) {
        return base_url;
    }
    return base_url + suffix;
}

static std::tuple<std::string, std::string, std::string, std::string> active_provider_config(const CloudConfig& config) {
    std::string provider = config.provider.empty() ? "openai_official" : config.provider;
    auto cloud_it = CLOUD_PROVIDERS.find(provider);
    if (cloud_it == CLOUD_PROVIDERS.end()) {
        provider = "openai_official";
        cloud_it = CLOUD_PROVIDERS.find(provider);
    }
    auto provider_it = config.providers.find(provider);
    ProviderConfig item = provider_it == config.providers.end() ? ProviderConfig{} : provider_it->second;
    std::string base_url = item.base_url.empty() ? cloud_it->second.base_url : item.base_url;
    std::string model = item.model.empty() ? config.default_model : item.model;
    if (model.empty() && !cloud_it->second.models.empty()) {
        model = cloud_it->second.models.front();
    }
    return {provider, base_url, model, get_api_key(provider)};
}

static bool content_allowed(const std::string& text) {
    std::string sample = lower(text);
    std::vector<std::string> blocked = {
        "制作炸弹", "诈骗教程", "儿童色情", "terrorist manual", "make a bomb", "child sexual"
    };
    for (const auto& word : blocked) {
        if (sample.find(lower(word)) != std::string::npos) {
            return false;
        }
    }
    return true;
}

static std::string moderated_text(const std::string& text) {
    return content_allowed(text) ? text : MODERATION_BLOCK_MESSAGE;
}

static std::string messages_to_text(const std::vector<Message>& messages) {
    std::ostringstream out;
    for (const auto& message : messages) {
        out << message.role << ": " << message.content << "\n";
    }
    return out.str();
}

static std::string read_file_for_model(const fs::path& path) {
    std::error_code ec;
    if (!fs::exists(path, ec) || !fs::is_regular_file(path, ec)) {
        return "";
    }
    if (fs::file_size(path, ec) > 256 * 1024) {
        return "";
    }
    return read_text(path);
}

static std::vector<Message> messages_with_files(std::vector<Message> messages, const std::vector<fs::path>& file_paths) {
    if (messages.empty() || file_paths.empty()) {
        return messages;
    }
    std::ostringstream context;
    for (const auto& path : file_paths) {
        std::string text = read_file_for_model(path);
        if (!text.empty()) {
            context << "\n\n[File: " << path.filename().string() << "]\n" << text;
        }
    }
    if (!context.str().empty()) {
        messages.back().content += "\n\nUse the following file content to answer:" + context.str();
    }
    return messages;
}

static std::string build_chat_payload(const std::vector<Message>& messages, const std::string& model) {
    std::ostringstream out;
    out << "{";
    out << "\"model\":\"" << json_escape(model) << "\",";
    out << "\"temperature\":0.7,";
    out << "\"stream\":false,";
    out << "\"messages\":[";
    for (size_t i = 0; i < messages.size(); ++i) {
        if (i > 0) {
            out << ",";
        }
        out << "{\"role\":\"" << json_escape(messages[i].role) << "\",\"content\":\"" << json_escape(messages[i].content) << "\"}";
    }
    out << "]}";
    return out.str();
}

static std::string response_text_from_json(const std::string& body) {
    std::regex pattern("\"choices\"\\s*:\\s*\\[\\s*\\{.*?\"message\"\\s*:\\s*\\{.*?\"content\"\\s*:\\s*\"((?:\\\\.|[^\"])*)\"");
    std::smatch match;
    if (std::regex_search(body, match, pattern)) {
        return json_string("{\"content\":\"" + match[1].str() + "\"}", "content");
    }
    std::string text = json_string(body, "text");
    return text.empty() ? json_string(body, "content") : text;
}

static std::string ask_openai_chat_completions(HttpClient& http,
                                               const std::vector<Message>& messages,
                                               const std::string& model,
                                               const std::string& base_url,
                                               const std::string& api_key,
                                               const std::vector<fs::path>& file_paths = {}) {
    std::string url = openai_chat_url(base_url);
    if (url.empty()) {
        throw std::runtime_error("API Base URL is empty.");
    }
    if (model.empty()) {
        throw std::runtime_error("Model is empty.");
    }
    std::map<std::string, std::string> headers = {{"Content-Type", "application/json"}};
    if (!api_key.empty()) {
        headers["Authorization"] = "Bearer " + api_key;
    }
    std::vector<Message> final_messages = messages_with_files(messages, file_paths);
    HttpResponse response = http.post(url, headers, build_chat_payload(final_messages, model), 180);
    if (response.status_code < 200 || response.status_code >= 300) {
        throw std::runtime_error("http_error_" + std::to_string(response.status_code));
    }
    return response_text_from_json(response.body);
}

static std::string ask_cloudai(HttpClient& http,
                               const std::vector<Message>& messages,
                               const CloudConfig& cloud_config,
                               const std::vector<fs::path>& file_paths = {}) {
    if (!content_allowed(messages_to_text(messages))) {
        return MODERATION_BLOCK_MESSAGE;
    }
    auto [provider, base_url, model, api_key] = active_provider_config(cloud_config);
    if (api_key.empty()) {
        throw std::runtime_error("missing_api_key");
    }
    return moderated_text(ask_openai_chat_completions(http, messages, model, base_url, api_key, file_paths));
}

static std::vector<std::string> cloud_usage_urls(const std::string& provider, std::string base_url) {
    while (!base_url.empty() && base_url.back() == '/') {
        base_url.pop_back();
    }
    const std::string suffix = "/chat/completions";
    if (base_url.size() >= suffix.size() && base_url.substr(base_url.size() - suffix.size()) == suffix) {
        base_url = base_url.substr(0, base_url.size() - suffix.size());
    }
    std::vector<std::string> urls;
    if (provider == "openai_official") {
        urls.push_back("https://api.openai.com/v1/dashboard/billing/usage");
        urls.push_back("https://api.openai.com/v1/usage");
    } else if (provider == "openrouter") {
        urls.push_back("https://openrouter.ai/api/v1/credits");
    } else if (provider == "deepseek") {
        urls.push_back("https://api.deepseek.com/user/balance");
    } else if (provider == "siliconflow") {
        urls.push_back("https://api.siliconflow.cn/v1/user/info");
        urls.push_back("https://api.siliconflow.cn/v1/user/balance");
    }
    if (!base_url.empty()) {
        urls.push_back(base_url + "/usage");
        urls.push_back(base_url + "/dashboard/billing/usage");
        urls.push_back(base_url + "/user/usage");
    }
    return urls;
}

static std::string summarize_usage_data(const std::string& body) {
    for (const auto& key : {"total_usage", "total_credits", "balance", "total_granted", "total_used", "limit", "used", "remaining", "total"}) {
        std::string value = json_string(body, key);
        if (!value.empty()) {
            return std::string(key) + "=" + value;
        }
        std::regex number_pattern("\"" + std::string(key) + "\"\\s*:\\s*([0-9.]+)");
        std::smatch match;
        if (std::regex_search(body, match, number_pattern)) {
            return std::string(key) + "=" + match[1].str();
        }
    }
    return "";
}

static std::string fetch_cloud_usage(HttpClient& http, const CloudConfig& cloud_config) {
    auto [provider, base_url, model, api_key] = active_provider_config(cloud_config);
    if (api_key.empty()) {
        throw std::runtime_error("missing_api_key");
    }
    std::map<std::string, std::string> headers = {
        {"Authorization", "Bearer " + api_key},
        {"Content-Type", "application/json"}
    };
    for (const auto& url : cloud_usage_urls(provider, base_url)) {
        HttpResponse response = http.get(url, headers, 20);
        if (response.status_code == 404 || response.status_code == 405) {
            continue;
        }
        if (response.status_code >= 200 && response.status_code < 300) {
            std::string summary = summarize_usage_data(response.body);
            if (!summary.empty()) {
                return summary;
            }
        }
    }
    return "";
}

class NoNetworkHttpClient : public HttpClient {
public:
    HttpResponse get(const std::string&, const std::map<std::string, std::string>&, long) override {
        throw std::runtime_error("http_backend_not_connected");
    }

    HttpResponse post(const std::string&, const std::map<std::string, std::string>&, const std::string&, long) override {
        throw std::runtime_error("http_backend_not_connected");
    }
};

int main() {
    LocalConfig local_config = load_config();
    CloudConfig cloud_config = load_cloud_config();
    save_cloud_config(cloud_config);

    NoNetworkHttpClient http;
    std::cout << APP_NAME << " " << APP_VERSION << " HarmonyOS C++ translated core\n";
    std::cout << "language=" << local_config.language << "\n";
    std::cout << "provider=" << cloud_config.provider << "\n";
    std::cout << "model=" << cloud_config.default_model << "\n";
    std::cout << "This file is translated from cloud_ai.py. Connect HttpClient to HarmonyOS network APIs before real chat.\n";

    std::string line;
    while (true) {
        std::cout << "> ";
        if (!std::getline(std::cin, line) || line == "/exit") {
            break;
        }
        if (line.empty()) {
            continue;
        }
        try {
            std::cout << ask_cloudai(http, {{"user", line}}, cloud_config) << "\n";
        } catch (const std::exception& exc) {
            if (std::string(exc.what()) == "missing_api_key") {
                std::cout << cloud_text(local_config, "cloud_no_key") << "\n";
            } else {
                std::cout << "CloudAI error: " << exc.what() << "\n";
            }
        }
    }
    return 0;
}
