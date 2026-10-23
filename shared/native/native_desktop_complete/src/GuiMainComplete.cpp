#include "AppConfig.hpp"
#include "CompleteChatWindow.hpp"

#include <QApplication>
#include <QCoreApplication>
#include <QTextStream>

int main(int argc, char* argv[]) {
    bool selfCheck = false;
    for (int index = 1; index < argc; ++index) {
        if (QString::fromLocal8Bit(argv[index]) == "--self-check") selfCheck = true;
    }
    if (selfCheck) {
        QCoreApplication application(argc, argv);
        QCoreApplication::setApplicationName("LocalAI");
        QCoreApplication::setOrganizationName("LocalAI");
        QCoreApplication::setApplicationVersion("1.0");
        const nativeai::AppConfig config = nativeai::loadConfig(nativeai::AppMode::LocalAI);
        QTextStream(stdout) << "app=LocalAI\nlanguage=c++17\nui=qt_widgets\nprovider=" << config.provider
                            << "\nrestricted_platform=" << (nativeai::runtimeRequiresLlamaCpp() ? "yes" : "no")
                            << "\nagent=available\nattachments=available\n";
        return 0;
    }
    QApplication application(argc, argv);
    QCoreApplication::setApplicationName("LocalAI");
    QCoreApplication::setOrganizationName("LocalAI");
    QCoreApplication::setApplicationVersion("1.0");
    nativeai::ChatWindow window(nativeai::AppMode::LocalAI);
    window.show();
    return application.exec();
}
