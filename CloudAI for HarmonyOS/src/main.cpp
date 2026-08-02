#include "CloudAIHarmony.hpp"

#include <cstdlib>
#include <iostream>
#include <memory>

namespace {

class ConsoleHttpClient final : public cloudai::IHttpClient {
public:
    cloudai::HttpResponse send(const cloudai::HttpRequest&) override {
        throw std::runtime_error("no_http_backend: rebuild with CLOUDAI_USE_LIBCURL or inject a HarmonyOS IHttpClient");
    }
};

std::filesystem::path defaultAppDataDir(int argc, char** argv) {
    if (argc > 1) {
        return std::filesystem::path(argv[1]);
    }
    const char* home = std::getenv("HOME");
    if (home && *home) {
        return std::filesystem::path(home) / ".local" / "share" / "LocalAI";
    }
    return std::filesystem::current_path() / "LocalAIData";
}

} // namespace

int main(int argc, char** argv) {
    using namespace cloudai;

    ConfigStore store(defaultAppDataDir(argc, argv));
#ifdef CLOUDAI_USE_LIBCURL
    auto http = std::make_shared<CurlHttpClient>();
#else
    auto http = std::make_shared<ConsoleHttpClient>();
#endif
    CloudAIClient client(store, http);

    LocalConfig local = store.loadLocalConfig();
    CloudConfig cloud = store.loadCloudConfig();
    store.saveCloudConfig(cloud);

    std::cout << "CloudAI " << kAppVersion << " for HarmonyOS C++ core\n";
    std::cout << "Language: " << local.language << "\n";
    std::cout << "Provider: " << cloud.provider << "\n";
    std::cout << "Model: " << cloud.defaultModel << "\n";
    std::cout << "Type a message. /exit to quit.\n";

    std::string line;
    while (true) {
        std::cout << "> ";
        if (!std::getline(std::cin, line)) {
            break;
        }
        if (line == "/exit") {
            break;
        }
        if (line.empty()) {
            continue;
        }
        try {
            std::string answer = client.ask({{"user", line}});
            std::cout << answer << "\n";
        } catch (const std::exception& exc) {
            std::string message = exc.what();
            if (message == "missing_api_key") {
                std::cout << client.text(local, "missing_api_key") << "\n";
            } else {
                std::cout << "CloudAI error: " << message << "\n";
            }
        }
    }

    return 0;
}
