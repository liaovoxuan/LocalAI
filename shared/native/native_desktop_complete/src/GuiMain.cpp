#include "AppConfig.hpp"
#include "ChatWindow.hpp"

#include <QApplication>
#include <QCoreApplication>
#include <QTextStream>

int main(int argc, char* argv[]) {
    QApplication application(argc, argv);
    QCoreApplication::setApplicationName("LocalAI");
    QCoreApplication::setOrganizationName("LocalAI");
    QCoreApplication::setApplicationVersion("1.0");

    if (application.arguments().contains("--self-check")) {
        const nativeai::AppConfig config = nativeai::loadConfig(nativeai::AppMode::LocalAI);
        QTextStream out(stdout);
        out << "app=LocalAI\n"
            << "language=c++17\n"
            << "ui=qt_widgets\n"
            << "provider=" << config.provider << '\n'
            << "restricted_platform=" << (nativeai::runtimeRequiresLlamaCpp() ? "yes" : "no") << '\n'
            << "agent=available\n"
            << "attachments=available\n";
        return 0;
    }

    nativeai::ChatWindow window(nativeai::AppMode::LocalAI);
    window.show();
    return application.exec();
}
