#include "ChatWindow.hpp"
#include "AppConfig.hpp"

#include <QApplication>
#include <QCoreApplication>
#include <QTextStream>
#include <QDir>
#include <QProcess>
#include <QFileInfo>
#include <QFile>
#if !defined(Q_OS_WIN)
#include <unistd.h>
#endif

using namespace nativeai;

#if defined(Q_OS_MACOS)
static QString shellQuote(QString value) {
    return "'" + value.replace("'", "'\\\\''") + "'";
}

static QString appleScriptLiteral(QString value) {
    value.replace("\\", "\\\\");
    value.replace("\"", "\\\"");
    return "\"" + value + "\"";
}
#endif

static QString findPythonTranslator() {
    const QString appDir = QCoreApplication::applicationDirPath();
    const QString name =
#if defined(Q_OS_WIN)
        "PythonMigrationTranslator.exe";
#else
        "PythonMigrationTranslator";
#endif
    const QStringList candidates = {
        QDir(appDir).filePath("../Helpers/" + name),
        QDir(appDir).filePath("helpers/" + name),
        QDir(appDir).filePath(name),
    };
    for (const QString& candidate : candidates) {
        if (QFileInfo(candidate).isExecutable()) {
            return QDir::cleanPath(candidate);
        }
    }
    return {};
}

static QString findPythonRuntime(const QString& edition) {
    const QString overridePath = qEnvironmentVariable("LOCALAI_PYTHON_RUNTIME").trimmed();
    if (!overridePath.isEmpty() && QFileInfo(overridePath).isExecutable()) {
        return QDir::cleanPath(overridePath);
    }
    const QString appDir = QCoreApplication::applicationDirPath();
    const QString executable = edition
#if defined(Q_OS_WIN)
        + ".exe";
#else
        ;
#endif
    const QStringList candidates = {
        QDir(appDir).filePath("../Resources/python-runtime/" + edition + "/" + executable),
        QDir(appDir).filePath("python-runtime/" + edition + "/" + executable),
        QDir(appDir).filePath("../python-runtime/" + edition + "/" + executable),
        QDir(appDir).filePath("../share/LocalAI/python-runtime/" + edition + "/" + executable),
    };
    for (const QString& candidate : candidates) {
        if (QFileInfo(candidate).isExecutable()) {
            return QDir::cleanPath(candidate);
        }
    }
    return {};
}

int main(int argc, char* argv[]) {
    bool selfCheck = false;
    bool runtimeCheck = false;
    for (int i = 1; i < argc; ++i) {
        const QString argument = QString::fromLocal8Bit(argv[i]);
        if (argument == "--self-check") {
            selfCheck = true;
        } else if (argument == "--runtime-check") {
            runtimeCheck = true;
        }
    }
#if defined(CLOUDAI_BUILD)
    const AppMode mode = AppMode::CloudAI;
#elif defined(LOCALAI_SE_BUILD)
    const AppMode mode = AppMode::LocalAISE;
#else
    AppMode mode = AppMode::LocalAI;
    for (int i = 1; i < argc; ++i) {
        if (QString::fromLocal8Bit(argv[i]) == "--cloud") {
            mode = AppMode::CloudAI;
        }
    }
#endif
    const QString edition = mode == AppMode::LocalAISE ? "LocalAI_SE" : "LocalAI";

    if (runtimeCheck) {
        QCoreApplication app(argc, argv);
        const QString runtime = findPythonRuntime(edition);
        const QString translator = findPythonTranslator();
        const bool runtimeExecutable = QFileInfo(runtime).isExecutable();
#if defined(LOCALAI_ENABLE_PYTHON_RUNTIME)
        const bool translatorExecutable = QFileInfo(translator).isExecutable();
#else
        const bool translatorExecutable = true;
#endif
        QTextStream out(stdout);
        out << "runtime=" << runtime << "\n";
        out << "runtime_executable=" << (runtimeExecutable ? "yes" : "no") << "\n";
        out << "python_translator=" << translator << "\n";
        out << "python_translator_executable=" << (translatorExecutable ? "yes" : "no") << "\n";
        return runtimeExecutable && translatorExecutable ? 0 : 1;
    }

    // The native executable owns packaging and compatibility policy.  Until every
    // Python feature is replaced, it launches the bundled, already-tested feature
    // runtime; LOCALAI_FORCE_NATIVE_UI=1 exposes the in-progress Qt UI directly.
    bool useLegacyRuntime = false;
#if defined(LOCALAI_ENABLE_PYTHON_RUNTIME)
    useLegacyRuntime = !qEnvironmentVariableIsSet("LOCALAI_FORCE_NATIVE_UI");
#endif
    if (!selfCheck && useLegacyRuntime) {
        QCoreApplication probe(argc, argv);
        const QString runtime = findPythonRuntime(edition);
        if (QFileInfo(runtime).isExecutable()) {
            const bool forceLlamaCpp = runtimeRequiresLlamaCpp();
            if (forceLlamaCpp) {
                qputenv("LOCALAI_FORCE_LLAMA_CPP", "1");
            }
            const QString translator = findPythonTranslator();
#if defined(Q_OS_MACOS)
            // Finder launches an .app without a terminal.  LocalAI SE is a CLI,
            // so start it in Terminal only when there is no inherited TTY.
            if (mode == AppMode::LocalAISE && !isatty(STDIN_FILENO)) {
                QString command;
                if (!translator.isEmpty()) {
                    command = "exec " + shellQuote(translator) + " --run " + shellQuote(runtime);
                    if (forceLlamaCpp) command += " --force-llama-cpp";
                } else {
                    command = "exec " + shellQuote(runtime);
                }
                const QString script = "tell application \"Terminal\" to do script "
                    + appleScriptLiteral(command) + "\nactivate application \"Terminal\"";
                if (QProcess::startDetached("/usr/bin/osascript", {"-e", script})) {
                    return 0;
                }
            }
#endif
#if !defined(Q_OS_WIN)
            const QByteArray executable = QFile::encodeName(translator.isEmpty() ? runtime : translator);
            const QByteArray runOption = "--run";
            const QByteArray runtimePath = QFile::encodeName(runtime);
            const QByteArray forceOption = "--force-llama-cpp";
            char* runtimeArgs[] = {
                const_cast<char*>(executable.constData()),
                translator.isEmpty() ? nullptr : const_cast<char*>(runOption.constData()),
                translator.isEmpty() ? nullptr : const_cast<char*>(runtimePath.constData()),
                forceLlamaCpp && !translator.isEmpty() ? const_cast<char*>(forceOption.constData()) : nullptr,
                nullptr,
            };
            ::execv(executable.constData(), runtimeArgs);
#endif
            QStringList bridgeArgs;
            QString bridgeProgram = runtime;
            if (!translator.isEmpty()) {
                bridgeProgram = translator;
                bridgeArgs = {"--run", runtime};
                if (forceLlamaCpp) bridgeArgs << "--force-llama-cpp";
            }
            if (QProcess::startDetached(bridgeProgram, bridgeArgs, QFileInfo(runtime).absolutePath())) {
                return 0;
            }
        }
    }
    if (selfCheck) {
        QCoreApplication app(argc, argv);
        QTextStream out(stdout);
        const AppConfig config = loadConfig(mode);
        out << appName(mode) << " " << versionLabel(mode) << "\n";
        out << "qt_core=ok\n";
        out << "config=" << defaultConfigPath(mode) << "\n";
        out << "secrets=" << defaultSecretsPath(mode) << "\n";
        out << "provider=" << config.provider << "\n";
        out << "ollama_provider=ok\n";
        out << "openai_compatible_provider=ok\n";
        out << "llama_cpp_provider=ok\n";
        out << "cloud_provider_profiles=ok\n";
        out << "runtime_language=" << (useLegacyRuntime ? "cpp+python" : "cpp") << "\n";
        out << "restricted_platform=" << (runtimeRequiresLlamaCpp() ? "yes" : "no") << "\n";
        out << "llama_cpp_binary_configured=" << (!config.llamaCppBinary.isEmpty() ? "yes" : "no") << "\n";
        out << "llama_cpp_model_configured=" << (!config.llamaCppModel.isEmpty() ? "yes" : "no") << "\n";
        out << "ui=not_started\n";
        return 0;
    }
    QApplication app(argc, argv);
    ChatWindow window(mode);
    window.show();
    return app.exec();
}
