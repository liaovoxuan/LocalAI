#include "CloudAIHarmony.hpp"

#include <filesystem>
#include <fstream>
#include <iterator>
#include <iostream>
#include <memory>
#include <stdexcept>

namespace {

class FakeHttpClient final : public cloudai::IHttpClient {
public:
    cloudai::HttpRequest lastRequest;

    cloudai::HttpResponse send(const cloudai::HttpRequest& request) override {
        lastRequest = request;
        return {200, R"({"choices":[{"message":{"content":"hello from cloud"}}]})"};
    }
};

void require(bool condition, const char* message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}

} // namespace

int main() {
    const auto root = std::filesystem::temp_directory_path() / "cloudai_core_smoke";
    std::error_code error;
    std::filesystem::remove_all(root, error);
    std::filesystem::create_directories(root);

    cloudai::ConfigStore store(root);
    auto config = store.loadCloudConfig();
    config.provider = "openai_official";
    config.providers.at(config.provider).model = "gpt-4.1-mini";
    store.saveCloudConfig(config);
    store.setApiKey(config.provider, "test-key");
    std::ifstream secretFile(root / "config" / "cloudai_secrets.json");
    const std::string secretBody((std::istreambuf_iterator<char>(secretFile)), std::istreambuf_iterator<char>());
    require(secretBody.find("\"openai_official\": \"v1:") != std::string::npos, "secret format mismatch");

    const auto file = root / "context.txt";
    std::ofstream(file) << "project context";

    auto fake = std::make_shared<FakeHttpClient>();
    cloudai::CloudAIClient client(store, fake);
    const auto answer = client.ask({{"user", "summarize"}}, {file});

    require(answer == "hello from cloud", "response parser mismatch");
    require(fake->lastRequest.method == "POST", "request method mismatch");
    require(fake->lastRequest.url == "https://api.openai.com/v1/chat/completions", "request URL mismatch");
    require(fake->lastRequest.body.find("project context") != std::string::npos, "file context missing");
    require(fake->lastRequest.headers.at("Authorization") == "Bearer test-key", "API key missing");
    require(store.getApiKey(config.provider) == "test-key", "API key round trip failed");

    store.configureProvider("custom", "https://example.test/v1", "demo-model");
    auto customConfig = store.loadCloudConfig();
    require(customConfig.provider == "custom", "custom provider selection failed");
    require(customConfig.providers.at("custom").baseUrl == "https://example.test/v1", "custom provider URL failed");
    require(customConfig.providers.at("custom").model == "demo-model", "custom provider model failed");
    store.setApiKey("custom", "custom-key");
    cloudai::CloudAIClient customClient(store, fake);
    const auto customPrepared = customClient.prepareRequest({{"user", "hello"}});
    require(customPrepared.url == "https://example.test/v1/chat/completions", "custom provider request URL mismatch");
    require(customPrepared.headers.at("Authorization") == "Bearer custom-key", "custom provider API key missing");

    config = store.loadCloudConfig();
    config.provider = "openai_official";
    store.saveCloudConfig(config);
    const auto prepared = client.prepareRequest({{"user", "hello"}});
    require(prepared.method == "POST", "prepared request method mismatch");
    require(prepared.url == "https://api.openai.com/v1/chat/completions", "prepared request URL mismatch");
    require(prepared.body.find("hello") != std::string::npos, "prepared request body mismatch");

    const auto action = cloudai::parseAgentAction(
        R"(Model note: {"action":"write_file","path":"/tmp/demo","content":"brace } and newline\n"})");
    require(action.has_value(), "agent action parser failed");
    require(action->action == "write_file", "agent action mismatch");
    require(action->content.find("brace }") != std::string::npos, "agent content parser mismatch");
    const auto commandAction = cloudai::parseAgentAction(R"({"action":"run_command","command":["cmake","--version"]})");
    require(commandAction.has_value() && commandAction->command.find("cmake") != std::string::npos, "agent command parser mismatch");
    require(cloudai::agentSystemPrompt("inspect project").find("Agent Mode") != std::string::npos, "agent prompt missing");

    std::filesystem::remove_all(root, error);
    std::cout << "cloudai_core_smoke: ok\n";
    return 0;
}
