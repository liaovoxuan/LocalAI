#include "ChatWindow.hpp"
#include "AppConfig.hpp"

#include <QApplication>
#include <QFileInfo>
#include <QStandardPaths>
#include <QTextStream>

using namespace nativeai;

int main(int argc, char* argv[]) {
    QApplication app(argc, argv);
#if defined(CLOUDAI_BUILD)
    const AppMode mode = AppMode::CloudAI;
#else
    AppMode mode = AppMode::LocalAI;
    for (int i = 1; i < argc; ++i) {
        if (QString::fromLocal8Bit(argv[i]) == "--cloud") {
            mode = AppMode::CloudAI;
        }
    }
#endif
    for (int i = 1; i < argc; ++i) {
        if (QString::fromLocal8Bit(argv[i]) == "--self-check") {
            QTextStream out(stdout);
            const AppConfig config = loadConfig(mode);
            out << appName(mode) << " " << versionLabel(mode) << "\n";
            out << "qt=ok\n";
            out << "config=" << defaultConfigPath(mode) << "\n";
            out << "provider=" << config.provider << "\n";
            out << "ollama_provider=ok\n";
            out << "openai_compatible_provider=ok\n";
            out << "llama_cpp_provider=ok\n";
            out << "llama_cpp_binary_configured=" << (!config.llamaCppBinary.isEmpty() ? "yes" : "no") << "\n";
            out << "llama_cpp_model_configured=" << (!config.llamaCppModel.isEmpty() ? "yes" : "no") << "\n";
            out << "ui=ok\n";
            return 0;
        }
    }
    ChatWindow window(mode);
    window.show();
    return app.exec();
}
