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

QString quoteShell(QString text) { return "'" + text.replace("'", "'\\''") + "'"; }

bool attachedText(const QString& path, QString* result, QString* error) {
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) { if (error) *error = file.errorString(); return false; }
    QByteArray data = file.read(128 * 1024 + 1);
    if (data.contains('\0')) { if (error) *error = "Binary files are not accepted as Agent context."; return false; }
    if (data.size() > 128 * 1024) data.truncate(128 * 1024);
    *result = QString::fromUtf8(data);
    return true;
}

void help(QTextStream& out) {
    out << "/agent       Toggle Agent mode\n"
        << "/attach PATH Attach a text file to the next request\n"
        << "/model PATH  Bind a GGUF model and use llama.cpp\n"
        << "/provider ID Choose ollama or llama_cpp\n"
        << "/new         Clear the conversation\n"
        << "/status      Show runtime state\n"
        << "/quit        Exit\n";
}

int terminalMain() {
    QTextStream in(stdin), out(stdout);
    nativeai::AppConfig config = nativeai::loadConfig(nativeai::AppMode::LocalAISE);
    nativeai::ProviderClient provider;
    QVector<nativeai::ChatMessage> history;
    QString attachment;
    bool agent = false;
    out << "LocalAI SE 1.0 | C++ terminal | provider=" << config.provider << "\nType /help for commands.\n\n";
    while (true) {
        out << (agent ? "agent> " : "localai> ") << Qt::flush;
        const QString line = in.readLine();
        if (line.isNull() || line == "/quit" || line == "/exit") break;
        if (line == "/help") { help(out); continue; }
        if (line == "/agent") { agent = !agent; out << "Agent " << (agent ? "ON" : "OFF") << "\n"; continue; }
        if (line == "/new") { history.clear(); attachment.clear(); out << "Conversation cleared.\n"; continue; }
        if (line == "/status") { out << "provider=" << config.provider << " model=" << config.model << " forced_llama_cpp=" << (nativeai::runtimeRequiresLlamaCpp() ? "yes" : "no") << "\n"; continue; }
        if (line.startsWith("/attach ")) {
            const QString path = line.mid(8).trimmed();
            if (!QFileInfo(path).isFile()) out << "File not found.\n";
            else { attachment = QFileInfo(path).absoluteFilePath(); out << "Attached: " << QFileInfo(path).fileName() << "\n"; }
            continue;
        }
        if (line.startsWith("/model ")) {
            const QString path = line.mid(7).trimmed();
            if (!QFileInfo(path).isFile() || !path.endsWith(".gguf", Qt::CaseInsensitive)) { out << "Select an existing GGUF file.\n"; continue; }
            config.provider = "llama_cpp"; config.llamaCppModel = QFileInfo(path).absoluteFilePath(); config.model = config.llamaCppModel;
            QString error; out << (nativeai::saveConfig(nativeai::AppMode::LocalAISE, config, &error) ? "Model saved.\n" : error + "\n"); continue;
        }
        if (line.startsWith("/provider ")) {
            const QString selected = line.mid(10).trimmed().toLower();
            if (nativeai::runtimeRequiresLlamaCpp() && selected != "llama_cpp") { out << "This platform requires llama.cpp.\n"; continue; }
            if (!nativeai::supportedProviders(nativeai::AppMode::LocalAISE).contains(selected)) { out << "Unsupported provider.\n"; continue; }
            config.provider = selected; QString error; out << (nativeai::saveConfig(nativeai::AppMode::LocalAISE, config, &error) ? "Provider saved.\n" : error + "\n"); continue;
        }
        if (line.trimmed().isEmpty()) continue;
        QString request = line;
        if (!attachment.isEmpty()) {
            QString text, error;
            if (!attachedText(attachment, &text, &error)) { out << "Attachment error: " << error << "\n"; continue; }
            request += "\n\n[Attached file: " + QFileInfo(attachment).fileName() + "]\n" + text;
            attachment.clear();
        }
        QVector<nativeai::ChatMessage> messages = history;
        if (agent) messages.prepend({"system", "Agent mode is enabled. Use only supplied conversation and attached file contents. Never claim an action that was not performed."});
        messages.append({"user", request});
        out << "Thinking...\n" << Qt::flush;
        QString error; const QString answer = provider.ask(messages, config, &error);
        if (!error.isEmpty()) { out << "Error: " << error << "\n\n"; continue; }
        out << answer << "\n\n";
        history.append({"user", request}); history.append({"assistant", answer});
        while (history.size() > 20) history.removeFirst();
    }
    return 0;
}

int launchTerminal(const QString& executable) {
#if defined(Q_OS_MACOS)
    QString command = quoteShell(executable) + " --terminal-child";
    command.replace("\\", "\\\\").replace("\"", "\\\"");
    return QProcess::execute("/usr/bin/osascript", {"-e", "tell application \"Terminal\" to do script \"" + command + "\"", "-e", "activate application \"Terminal\""});
#else
    Q_UNUSED(executable); return 0;
#endif
}

} // namespace

int main(int argc, char* argv[]) {
    QCoreApplication app(argc, argv);
    QCoreApplication::setApplicationName("LocalAI SE"); QCoreApplication::setOrganizationName("LocalAI");
    const QStringList args = app.arguments();
    if (args.contains("--self-check")) {
        const nativeai::AppConfig config = nativeai::loadConfig(nativeai::AppMode::LocalAISE);
        QTextStream(stdout) << "app=LocalAI SE\nlanguage=c++17\nui=terminal\nwidgets=not_linked\nprovider=" << config.provider << "\nagent=available\nattachments=available\n";
        return 0;
    }
#if defined(Q_OS_MACOS) && defined(Q_OS_UNIX)
    if (!args.contains("--terminal-child") && (!isatty(STDIN_FILENO) || !isatty(STDOUT_FILENO))) return launchTerminal(QCoreApplication::applicationFilePath());
#endif
    return terminalMain();
}
