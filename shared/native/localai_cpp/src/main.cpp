#include "LocalAI.hpp"

#include <cstdlib>
#include <iostream>
#include <memory>
#include <string>

using namespace localai;

int main(int argc, char** argv) {
    std::filesystem::path appDir;
    if (argc > 1) {
        appDir = std::filesystem::path(argv[1]);
    } else {
        const char* home = std::getenv("HOME");
        if (home && *home) appDir = std::filesystem::path(home) / ".local" / "share" / "LocalAI";
        else appDir = std::filesystem::current_path();
    }

    ConfigStore store(appDir);
    std::shared_ptr<IHttpClient> http;
#ifdef LOCALAI_USE_LIBCURL
    http = makeCurlHttpClient();
#endif

    LocalAIClient client(store, http);

    std::cout << "LocalAI C++ core " << kAppVersion << "\n";
    std::cout << "Provider: " << store.loadLocalConfig().provider << "\n";
    std::string line;
    while (true) {
        std::cout << "> ";
        if (!std::getline(std::cin, line)) break;
        if (line == "/exit") break;
        if (line.empty()) continue;
        try {
            std::vector<Message> msgs{{"user", line}};
            std::string ans = client.ask(msgs, {});
            std::cout << ans << std::endl;
        } catch (const std::exception& e) {
            std::cout << "Error: " << e.what() << std::endl;
        }
    }
    return 0;
}
