#include "CloudAIHarmony.hpp"

#include <napi/native_api.h>

#include <filesystem>
#include <sstream>
#include <stdexcept>
#include <string>

namespace {

using cloudai::CloudAIClient;
using cloudai::ConfigStore;

std::string escapeJson(const std::string& value) {
    std::string result;
    result.reserve(value.size() + 16);
    for (const char ch : value) {
        switch (ch) {
            case '\\': result += "\\\\"; break;
            case '"': result += "\\\""; break;
            case '\n': result += "\\n"; break;
            case '\r': result += "\\r"; break;
            case '\t': result += "\\t"; break;
            default: result += ch; break;
        }
    }
    return result;
}

bool readString(napi_env env, napi_value value, std::string& output) {
    size_t length = 0;
    if (napi_get_value_string_utf8(env, value, nullptr, 0, &length) != napi_ok) {
        return false;
    }
    std::string buffer(length + 1, '\0');
    if (napi_get_value_string_utf8(env, value, buffer.data(), buffer.size(), &length) != napi_ok) {
        return false;
    }
    buffer.resize(length);
    output = std::move(buffer);
    return true;
}

napi_value makeString(napi_env env, const std::string& value) {
    napi_value result = nullptr;
    napi_create_string_utf8(env, value.c_str(), value.size(), &result);
    return result;
}

bool arguments(napi_env env, napi_callback_info info, size_t expected, napi_value* argv) {
    size_t argc = expected;
    return napi_get_cb_info(env, info, &argc, argv, nullptr, nullptr) == napi_ok && argc >= expected;
}

std::filesystem::path appDataPath(const std::string& value) {
    if (value.empty()) {
        throw std::invalid_argument("app_data_dir_required");
    }
    return std::filesystem::path(value);
}

napi_value GetStatus(napi_env env, napi_callback_info info) {
    napi_value argv[1] = {nullptr};
    if (!arguments(env, info, 1, argv)) {
        napi_throw_type_error(env, nullptr, "getStatus(appDataDir) requires one string");
        return nullptr;
    }
    try {
        std::string dataDir;
        if (!readString(env, argv[0], dataDir)) {
            throw std::invalid_argument("app_data_dir_must_be_string");
        }
        ConfigStore store(appDataPath(dataDir));
        const auto local = store.loadLocalConfig();
        const auto cloud = store.loadCloudConfig();
        std::string baseUrl;
        auto provider = cloud.providers.find(cloud.provider);
        if (provider != cloud.providers.end()) {
            baseUrl = provider->second.baseUrl;
        }
        std::ostringstream json;
        json << "{\"app\":\"CloudAI\",\"version\":\"" << escapeJson(cloudai::kAppVersion) << "\",\"language\":\""
             << escapeJson(local.language) << "\",\"provider\":\"" << escapeJson(cloud.provider)
             << "\",\"model\":\"" << escapeJson(cloud.defaultModel)
             << "\",\"base_url\":\"" << escapeJson(baseUrl) << "\"}";
        return makeString(env, json.str());
    } catch (const std::exception& error) {
        napi_throw_error(env, nullptr, error.what());
        return nullptr;
    }
}

napi_value SaveApiKey(napi_env env, napi_callback_info info) {
    napi_value argv[3] = {nullptr};
    if (!arguments(env, info, 3, argv)) {
        napi_throw_type_error(env, nullptr, "setApiKey(appDataDir, provider, apiKey) requires three strings");
        return nullptr;
    }
    try {
        std::string dataDir;
        std::string provider;
        std::string apiKey;
        if (!readString(env, argv[0], dataDir) || !readString(env, argv[1], provider) || !readString(env, argv[2], apiKey)) {
            throw std::invalid_argument("setApiKey arguments must be strings");
        }
        ConfigStore store(appDataPath(dataDir));
        store.setApiKey(provider, apiKey);
        return makeString(env, "ok");
    } catch (const std::exception& error) {
        napi_throw_error(env, nullptr, error.what());
        return nullptr;
    }
}

napi_value ConfigureProvider(napi_env env, napi_callback_info info) {
    napi_value argv[4] = {nullptr};
    if (!arguments(env, info, 4, argv)) {
        napi_throw_type_error(env, nullptr, "configureProvider(appDataDir, provider, baseUrl, model) requires four strings");
        return nullptr;
    }
    try {
        std::string dataDir;
        std::string provider;
        std::string baseUrl;
        std::string model;
        if (!readString(env, argv[0], dataDir) || !readString(env, argv[1], provider)
            || !readString(env, argv[2], baseUrl) || !readString(env, argv[3], model)) {
            throw std::invalid_argument("configureProvider arguments must be strings");
        }
        ConfigStore store(appDataPath(dataDir));
        store.configureProvider(provider, baseUrl, model);
        return makeString(env, "ok");
    } catch (const std::exception& error) {
        napi_throw_error(env, nullptr, error.what());
        return nullptr;
    }
}

napi_value PrepareRequest(napi_env env, napi_callback_info info) {
    napi_value argv[2] = {nullptr};
    if (!arguments(env, info, 2, argv)) {
        napi_throw_type_error(env, nullptr, "prepareRequest(appDataDir, prompt) requires two strings");
        return nullptr;
    }
    try {
        std::string dataDir;
        std::string prompt;
        if (!readString(env, argv[0], dataDir) || !readString(env, argv[1], prompt)) {
            throw std::invalid_argument("prepareRequest arguments must be strings");
        }
        ConfigStore store(appDataPath(dataDir));
        CloudAIClient client(store, nullptr);
        const auto request = client.prepareRequest({{"user", prompt}});
        std::ostringstream json;
        json << "{\"url\":\"" << escapeJson(request.url)
             << "\",\"body\":\"" << escapeJson(request.body)
             << "\",\"authorization\":\"" << escapeJson(request.headers.at("Authorization")) << "\"}";
        return makeString(env, json.str());
    } catch (const std::exception& error) {
        napi_throw_error(env, nullptr, error.what());
        return nullptr;
    }
}

napi_value ResponseText(napi_env env, napi_callback_info info) {
    napi_value argv[1] = {nullptr};
    if (!arguments(env, info, 1, argv)) {
        napi_throw_type_error(env, nullptr, "responseText(body) requires one string");
        return nullptr;
    }
    std::string body;
    if (!readString(env, argv[0], body)) {
        napi_throw_type_error(env, nullptr, "responseText body must be a string");
        return nullptr;
    }
    return makeString(env, cloudai::moderatedText(cloudai::responseTextFromJson(body)));
}

napi_value Init(napi_env env, napi_value exports) {
    napi_property_descriptor properties[] = {
        {"getStatus", nullptr, GetStatus, nullptr, nullptr, nullptr, napi_default, nullptr},
        {"setApiKey", nullptr, SaveApiKey, nullptr, nullptr, nullptr, napi_default, nullptr},
        {"configureProvider", nullptr, ConfigureProvider, nullptr, nullptr, nullptr, napi_default, nullptr},
        {"prepareRequest", nullptr, PrepareRequest, nullptr, nullptr, nullptr, napi_default, nullptr},
        {"responseText", nullptr, ResponseText, nullptr, nullptr, nullptr, napi_default, nullptr},
    };
    napi_define_properties(env, exports, sizeof(properties) / sizeof(properties[0]), properties);
    return exports;
}

napi_module g_module = {};

__attribute__((constructor)) void RegisterCloudAI() {
    g_module.nm_version = 1;
    g_module.nm_flags = 0;
    g_module.nm_filename = nullptr;
    g_module.nm_modname = "cloudai_harmony";
    g_module.nm_register_func = Init;
    g_module.nm_priv = nullptr;
    napi_module_register(&g_module);
}

} // namespace
