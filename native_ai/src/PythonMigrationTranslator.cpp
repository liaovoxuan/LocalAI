#include <algorithm>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <regex>
#include <string>
#include <vector>

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

int main(int argc, char** argv) {
    if (argc < 2) {
        std::cerr << "Usage: PythonMigrationTranslator <python-file-or-directory> [...]\n";
        return 2;
    }
    std::vector<FileReport> reports;
    for (int i = 1; i < argc; ++i) {
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
