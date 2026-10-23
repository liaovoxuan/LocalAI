#include "AppConfig.hpp"
#include "ProviderClient.hpp"

#include <QCoreApplication>
#include <QFile>
#include <QFileInfo>
#include <QProcess>
#include <QTextStream>

#if defined(Q_OS_UNIX)
#include <unistd.h>
#endif

namespace {

QString shellQuote(QString value) {
    return "'" + value.replace("'", "'\\''") + "'";
}

bool hasTerminal() {
#if defined(Q_OS_UNIX)
    return ::isatty(STDIN_FILENO) && ::isatty(STDOUT_FILENO);
#else
    return true;
#endif
}

int launchMacTerminal(const QString& executable) {
#if defined(Q_OS_MACOS)
    const QString command = shellQuote(executable) + " --terminal-child";
    QString script = "tell application \"Terminal\" to do script \"";
    QString escaped = command;
    escaped.replace("\\", "\\\\").replace("\"", "\\\"");
    script += escaped + "\"";
    return QProcess::execute("/usr/bin/osascript", {"-e", script, "-e", "activate application \"Terminal\""});
#else
    Q_UNUSED(executable);
    return 0;
#endif
}

QString readAttachment(const QString& path, QString* error) {
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        if (error) *error = file.errorString();
        return {};
    }
    QByteArray data = file.read(128 * 1024 + 1);
    if (data.size() > 128 * 1024) {
        data.truncate(128 * 1024);
    }
    if (data.contains('\0')) {
        if (error) *error = "Binary attachments are not accepted. Convert the file to text first.";
        return {};
    }
    return QString::fromUtf8(data);
}

void printHelp(QTextStream& out) {
    out << "/agent       toggle Agent mode\n"
        << "/attach PATH attach a text file to the next request\n"
        << "/model PATH  bind a GGUF model and select llama.cpp\n"
        << "/provider ID select ollama or llama_cpp\n"
        << "/new         clear conversation\n"
        << "/status      show runtime status\n"
        << "/quit        exit\n";
}

int runTerminal() {
    QTextStream in(stdin);
    QTextStream out(stdout);
    nativeai::AppConfig config = nativeai::loadConfig(nativeai::AppMode::LocalAISE);
    nativeai::ProviderClient client;
    QVector<nativeai::ChatMessage> history;
    QString attachmentPath;
    bool agentMode = false;

    out << "LocalAI SE 1.0 | C++ terminal | " << config.provider << "\n";
    out << "Type /help for commands.\n\n";
    while (true) {
        out << (agentMode ? "agent> " : "localai> ") << Qt::flush;
        const QString line = in.readLine();
        if (line.isNull() || line == "/quit" || line == "/exit") break;
        if (line == "/help") { printHelp(out); continue; }
        if (line == "/agent") { agentMode = !agentMode; out << "Agent " << (agentMode ? "ON" : "OFF") << "\n"; continue; }
        if (line == "/new") { history.clear(); attachmentPath.clear(); out << "Conversation cleared.\n"; continue; }
        if (line == "/status") {
            out << "provider=" << config.provider << " model=" << config.model
                << " forced_llama_cpp=" << (nativeai::runtimeRequiresLlamaCpp() ? "yes" : "no") << "\n";
            continue;
        }
        if (line.startsWith("/attach ")) {
            const QString path = line.mid(8).trimmed();
            if (!QFileInfo::exists(path)) out << "File not found.\n";
            else { attachmentPath = path; out << "Attached: " << QFileInfo(path).fileName() << "\n"; }
            continue;
        }
        if (line.startsWith("/model ")) {
            const QString path = line.mid(7).trimmed();
            if (!QFileInfo::exists(path) || !path.endsWith(".gguf", Qt::CaseInsensitive)) {
                out << "Select an existing GGUF file.\n";
                continue;
            }
            config.provider = "llama_cpp";
            config.llamaCppModel = QFileInfo(path).absoluteFilePath();
            config.model = config.llamaCppModel;
            QString error;
            out << (nativeai::saveConfig(nativeai::AppMode::LocalAISE, config, &error) ? "Model saved.\n" : error + "\n");
            continue;
        }
        if (line.startsWith("/provider ")) {
            const QString provider = line.mid(10).trimmed().toLower();
            if (nativeai::runtimeRequiresLlamaCpp() && provider != "llama_cpp") {
                out << "This platform requires llama.cpp.\n";
                continue;
            }
            if (!nativeai::supportedProviders(nativeai::AppMode::LocalAISE).contains(provider)) {
                out << "Unsupported provider.\n";
                continue;
            }
            config.provider = provider;
            QString error;
            if (!nativeai::saveConfig(nativeai::AppMode::LocalAISE, config, &error)) out << error << '\n';
            else out << "Provider: " << provider << '\n';
            continue;
        }
        if (line.trimmed().isEmpty()) continue;

        QString request = line;
        if (!attachmentPath.isEmpty()) {
            QString error;
            const QString content = readAttachment(attachmentPath, &error);
            if (!error.isEmpty()) { out << "Attachment error: " << error << '\n'; continue; }
            request += "\n\n[Attached file: " + QFileInfo(attachmentPath).fileName() + "]\n" + content;
            attachmentPath.clear();
        }
        QVector<nativeai::ChatMessage> requestMessages = history;
        if (agentMode) {
            requestMessages.prepend({"system", "Agent mode is enabled. Use only the supplied conversation and attached file contents. Never claim an action that was not performed."});
        }
        requestMessages.append({"user", request});
        out << "Thinking...\n" << Qt::flush;
        QString error;
        const QString answer = client.ask(requestMessages, config, &error);
        if (!error.isEmpty()) { out << "Error: " << error << "\n\n"; continue; }
        out << answer << "\n\n";
        history.append({"user", request});
        history.append({"assistant", answer});
        while (history.size() > 20) history.removeFirst();
    }
    return 0;
}

} // namespace

int main(int argc, char* argv[]) {
    QCoreApplication application(argc, argv);
    QCoreApplication::setApplicationName("LocalAI SE");
    QCoreApplication::setOrganizationName("LocalAI");
    QCoreApplication::setApplicationVersion("1.0");
    const QStringList arguments = application.arguments();
    if (arguments.contains("--self-check")) {
        const nativeai::AppConfig config = nativeai::loadConfig(nativeai::AppMode::LocalAISE);
        QTextStream(stdout) << "app=LocalAI SE\nlanguage=c++17\nui=terminal\nwidgets=not_linked\nprovider="
                            << config.provider << "\nagent=available\nattachments=available\n";
        return 0;
    }
#if defined(Q_OS_MACOS)
    if (!arguments.contains("--terminal-child") && !hasTerminal()) {
        return launchMacTerminal(QCoreApplication::applicationFilePath());
    }
#endif
    return runTerminal();
}
