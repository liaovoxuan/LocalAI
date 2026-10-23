#include <cstdlib>
#include <iostream>
#include <string>

static std::string environmentValue(const char* name) {
    const char* value = std::getenv(name);
    return value ? value : "";
}

int main() {
    const bool bridgeOk = environmentValue("LOCALAI_RUNTIME_BRIDGE") == "cpp";
    const bool providerOk = environmentValue("LOCALAI_FORCE_LLAMA_CPP") == "1";
    if (!bridgeOk || !providerOk) {
        std::cerr << "bridge=" << environmentValue("LOCALAI_RUNTIME_BRIDGE")
                  << " provider=" << environmentValue("LOCALAI_FORCE_LLAMA_CPP") << "\n";
        return 1;
    }
    std::cout << "translator_environment=ok\n";
    return 0;
}
