#include "LocalAI.hpp"

#include <algorithm>
#include <cctype>
#include <fstream>
#include <iostream>
#include <sstream>
#include <regex>

namespace localai {

static std::string jsonEscape(const std::string& value) {
    std::ostringstream out;
    for (unsigned char c : value) {
        switch (c) {
        case '"': out << "\\\""; break;
        case '\\': out << "\\\\"; break;
        case '\b': out << "\\b"; break;
        case '\f': out << "\\f"; break;
        case '\n': out << "\\n"; break;
        case '\r': out << "\\r"; break;
        case '\t': out << "\\t"; break;
        default:
            if (c < 0x20) {
                out << "\\u00";
                constexpr char hex[] = "0123456789abcdef";
                out << hex[(c >> 4) & 0x0f] << hex[c & 0x0f];
            } else {
                out << static_cast<char>(c);
            }
        }
    }
    return out.str();
}

static std::string trim(std::string v) {
    auto isSpace = [](unsigned char c) { return std::isspace(c) != 0; };
    v.erase(v.begin(), std::find_if(v.begin(), v.end(), [&](char c) { return !isSpace(c); }));
    v.erase(std::find_if(v.rbegin(), v.rend(), [&](char c) { return !isSpace(c); }).base(), v.end());
    return v;
}

std::map<std::string, ProviderInfo> defaultProviders() {
    return {
        {"ollama", {"ollama", "Ollama (local)", "http://localhost:11434/api/generate", {"llama3.1:8b"}}},
        {"llama_cpp", {"llama_cpp", "llama.cpp", "", {"local-llama"}}},
        {"lm_studio", {"lm_studio", "LM Studio", "http://localhost:1234/v1", {"llama3.1:8b"}}},
        {"openai_compatible", {"openai_compatible", "OpenAI Compatible", "http://localhost:8000/v1", {"gpt-4o-mini"}}},
    };
}

ConfigStore::ConfigStore(std::filesystem::path appDataDir) : appDataDir_(std::move(appDataDir)) {}

AppConfig ConfigStore::loadLocalConfig() const {
    AppConfig cfg;
    std::filesystem::path p = appDataDir_ / "config.json";
    std::ifstream in(p, std::ios::binary);
    if (!in) return cfg;
    std::ostringstream ss;
    ss << in.rdbuf();
    std::string body = ss.str();
    // very small JSON parsing: look for language/provider
    auto findString = [&](const std::string& key){
        std::regex pattern("\"" + key + "\"\\s*:\\s*\"((?:\\\\.|[^\"])*)\"");
        std::smatch m;
        if (std::regex_search(body, m, pattern)) return m[1].str();
        return std::string();
    };
    try {
        std::string lang = findString("language");
        if (!lang.empty()) cfg.language = lang;
        std::string prov = findString("provider");
        if (!prov.empty()) cfg.provider = prov;
    } catch (...) {}
    return cfg;
}

void ConfigStore::saveLocalConfig(const AppConfig& cfg) const {
    std::filesystem::create_directories(appDataDir_);
    std::ofstream out(appDataDir_ / "config.json", std::ios::binary | std::ios::trunc);
    out << "{\n";
    out << "  \"language\": \"" << jsonEscape(trim(cfg.language)) << "\",\n";
    out << "  \"provider\": \"" << jsonEscape(trim(cfg.provider)) << "\"\n";
    out << "}\n";
}

class ConsoleHttpClient : public IHttpClient {
public:
    HttpResponse send(const HttpRequest& req) override {
        throw std::runtime_error("no_http_backend: build with LOCALAI_USE_LIBCURL or provide an IHttpClient");
    }
};

LocalAIClient::LocalAIClient(ConfigStore store, std::shared_ptr<IHttpClient> httpClient)
    : store_(std::move(store)), httpClient_(std::move(httpClient)) {}

const std::map<std::string, ProviderInfo>& LocalAIClient::providers() const {
    static const auto p = defaultProviders();
    return p;
}

std::string LocalAIClient::buildOllamaPayload(const std::vector<Message>& messages, const std::string& model) const {
    std::ostringstream out;
    out << "{\"model\":\"" << jsonEscape(model) << "\",\"prompt\":\"";
    for (const auto& m : messages) {
        out << jsonEscape(m.role) << ": " << jsonEscape(m.content) << "\\n";
    }
    out << "\"}";
    return out.str();
}

std::string LocalAIClient::ask(const std::vector<Message>& messages, const std::vector<std::filesystem::path>&) {
    auto cfg = store_.loadLocalConfig();
    auto prov = cfg.provider.empty() ? std::string("ollama") : cfg.provider;
    auto provs = providers();
    auto it = provs.find(prov);
    if (it == provs.end()) return "Unknown provider";
    const ProviderInfo& info = it->second;
    if (info.code == "ollama") {
        std::string model = info.models.empty() ? "" : info.models.front();
        std::string payload = buildOllamaPayload(messages, model);
        if (!httpClient_) throw std::runtime_error("no_http_client");
        HttpRequest req{"POST", info.baseUrl, {{"Content-Type","application/json"}}, payload, 60};
        HttpResponse resp = httpClient_->send(req);
        if (resp.statusCode >= 200 && resp.statusCode < 300) {
            return resp.body;
        }
        return std::string("HTTP error: ") + std::to_string(resp.statusCode);
    }
    // Fallback: return a stub message indicating provider is not implemented
    return std::string("LocalAI C++: provider '") + prov + "' not implemented in this build.";
}

} // namespace localai
