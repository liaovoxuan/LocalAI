#include <algorithm>
#include <cerrno>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <regex>
#include <string>
#include <vector>

#if defined(_WIN32)
#include <process.h>
#include <cstdlib>
#include <shellapi.h>
#include <windows.h>
#else
#include <unistd.h>
#endif

namespace fs = std::filesystem;

struct FileReport {
    fs::path path;
    int lines = 0;
    int functions = 0;
    int classes = 0;
    int imports = 0;
};

static FileReport scanFile(const fs::path& path) {
    FileReport report;
    report.path = path;
    std::ifstream in(path);
    std::string line;
    const std::regex defRe("^\\s*def\\s+[A-Za-z_][A-Za-z0-9_]*\\s*\\(");
    const std::regex classRe("^\\s*class\\s+[A-Za-z_][A-Za-z0-9_]*");
    const std::regex importRe("^\\s*(import|from)\\s+");
    while (std::getline(in, line)) {
        ++report.lines;
        if (std::regex_search(line, defRe)) ++report.functions;
        if (std::regex_search(line, classRe)) ++report.classes;
        if (std::regex_search(line, importRe)) ++report.imports;
    }
    return report;
}

#if defined(_WIN32)
static int runWindowsRuntime() {
    int wideArgc = 0;
    LPWSTR* wideArgv = CommandLineToArgvW(GetCommandLineW(), &wideArgc);
    if (!wideArgv || wideArgc < 3) {
        if (wideArgv) LocalFree(wideArgv);
        std::cerr << "Unable to read the Windows Unicode command line.\n";
        return 2;
    }
    const fs::path runtime = fs::absolute(fs::path(wideArgv[2])).lexically_normal();
    if (!fs::is_regular_file(runtime)) {
        std::wcerr << L"Python runtime not found: " << runtime.wstring() << L"\n";
        LocalFree(wideArgv);
        return 3;
    }
    bool forceLlamaCpp = false;
    bool passArguments = false;
    std::vector<std::wstring> arguments{runtime.wstring()};
    for (int i = 3; i < wideArgc; ++i) {
        const std::wstring argument = wideArgv[i];
        if (!passArguments && argument == L"--force-llama-cpp") {
            forceLlamaCpp = true;
        } else if (!passArguments && argument == L"--") {
            passArguments = true;
        } else if (passArguments) {
            arguments.push_back(argument);
        } else {
            std::wcerr << L"Unknown bridge option: " << argument << L"\n";
            LocalFree(wideArgv);
            return 2;
        }
    }
    LocalFree(wideArgv);
    _wputenv_s(L"LOCALAI_RUNTIME_BRIDGE", L"cpp");
    if (forceLlamaCpp) _wputenv_s(L"LOCALAI_FORCE_LLAMA_CPP", L"1");
    std::vector<const wchar_t*> childArgv;
    childArgv.reserve(arguments.size() + 1);
    for (const std::wstring& argument : arguments) childArgv.push_back(argument.c_str());
    childArgv.push_back(nullptr);
    const intptr_t status = _wspawnv(_P_WAIT, runtime.c_str(), childArgv.data());
    if (status == -1) {
        std::cerr << "Unable to start Python runtime: " << std::strerror(errno) << "\n";
        return 4;
    }
    return static_cast<int>(status);
}
#endif

static int runPythonRuntime(int argc, char** argv) {
    if (argc < 3) {
        std::cerr << "Usage: PythonMigrationTranslator --run <runtime> [--force-llama-cpp] [-- <args...>]\n";
        return 2;
    }
#if defined(_WIN32)
    return runWindowsRuntime();
#else
    const fs::path runtime = fs::absolute(fs::path(argv[2])).lexically_normal();
    if (!fs::is_regular_file(runtime)) {
        std::cerr << "Python runtime not found: " << runtime << "\n";
        return 3;
    }

    bool forceLlamaCpp = false;
    bool passArguments = false;
    std::vector<std::string> arguments{runtime.string()};
    for (int i = 3; i < argc; ++i) {
        const std::string argument = argv[i];
        if (!passArguments && argument == "--force-llama-cpp") {
            forceLlamaCpp = true;
        } else if (!passArguments && argument == "--") {
            passArguments = true;
        } else if (passArguments) {
            arguments.push_back(argument);
        } else {
            std::cerr << "Unknown bridge option: " << argument << "\n";
            return 2;
        }
    }

    setenv("LOCALAI_RUNTIME_BRIDGE", "cpp", 1);
    if (forceLlamaCpp) setenv("LOCALAI_FORCE_LLAMA_CPP", "1", 1);

    std::vector<char*> childArgv;
    childArgv.reserve(arguments.size() + 1);
    for (std::string& argument : arguments) {
        childArgv.push_back(argument.data());
    }
    childArgv.push_back(nullptr);

    execv(runtime.c_str(), childArgv.data());
    std::cerr << "Unable to start Python runtime: " << std::strerror(errno) << "\n";
    return 4;
#endif
}

static int printSelfCheck() {
    std::cout << "translator=ok\n";
    std::cout << "language=c++17\n";
#if defined(_WIN32)
    std::cout << "process_backend=windows_unicode_spawn\n";
#elif defined(__OHOS__)
    std::cout << "process_backend=ohos_posix_exec\n";
#elif defined(__APPLE__)
    std::cout << "process_backend=apple_posix_exec\n";
#elif defined(__linux__)
    std::cout << "process_backend=linux_posix_exec\n";
#else
    std::cout << "process_backend=posix_exec\n";
#endif
    return 0;
}

int main(int argc, char** argv) {
    if (argc < 2) {
        std::cerr << "Usage:\n"
                  << "  PythonMigrationTranslator --run <runtime> [--force-llama-cpp] [-- <args...>]\n"
                  << "  PythonMigrationTranslator --scan <python-file-or-directory> [...]\n";
        return 2;
    }
    if (std::string(argv[1]) == "--run") {
        return runPythonRuntime(argc, argv);
    }
    if (std::string(argv[1]) == "--self-check") {
        return printSelfCheck();
    }
    const int firstInput = std::string(argv[1]) == "--scan" ? 2 : 1;
    if (firstInput >= argc) {
        std::cerr << "No Python source path was provided.\n";
        return 2;
    }
    std::vector<FileReport> reports;
    for (int i = firstInput; i < argc; ++i) {
        fs::path input(argv[i]);
        if (!fs::exists(input)) continue;
        if (fs::is_regular_file(input) && input.extension() == ".py") {
            reports.push_back(scanFile(input));
        } else if (fs::is_directory(input)) {
            for (const auto& entry : fs::recursive_directory_iterator(input)) {
                if (entry.is_regular_file() && entry.path().extension() == ".py") {
                    const auto text = entry.path().string();
                    if (text.find("qemu-master") != std::string::npos) continue;
                    reports.push_back(scanFile(entry.path()));
                }
            }
        }
    }
    std::sort(reports.begin(), reports.end(), [](const FileReport& a, const FileReport& b) {
        return a.path.string() < b.path.string();
    });
    std::cout << "{\n  \"schema\": \"localai.python-migration.v1\",\n  \"files\": [\n";
    for (std::size_t i = 0; i < reports.size(); ++i) {
        const auto& r = reports[i];
        std::cout << "    {\"path\": \"" << r.path.string() << "\", \"lines\": " << r.lines
                  << ", \"functions\": " << r.functions << ", \"classes\": " << r.classes
                  << ", \"imports\": " << r.imports << "}";
        if (i + 1 < reports.size()) std::cout << ",";
        std::cout << "\n";
    }
    std::cout << "  ]\n}\n";
    return 0;
}
