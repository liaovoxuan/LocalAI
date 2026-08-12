#include "ConfigManager.hpp"
#include "Platform.hpp"
#include "QemuParser.hpp"
#include "UtmParser.hpp"
#include "VirtualWorldWindow.hpp"
#include "VMManager.hpp"
#include "VmConfig.hpp"

#include <QApplication>
#include <QCoreApplication>
#include <QFile>
#include <QFileInfo>
#include <QTextStream>
#include <stdexcept>

namespace {

QString readSourceText(const QString& value) {
    const QFileInfo info(value);
    if (!info.exists() || !info.isFile()) {
        return value;
    }
    QFile file(value);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
        throw std::runtime_error(QString("Cannot read source file: %1").arg(value).toStdString());
    }
    return QString::fromUtf8(file.readAll());
}

QString optionValue(const QStringList& args, const QString& name, const QString& fallback = QString()) {
    const int index = args.indexOf(name);
    if (index >= 0 && index + 1 < args.size()) {
        if (args.at(index + 1).startsWith("--")) {
            return fallback;
        }
        return args.at(index + 1);
    }
    return fallback;
}

bool hasOption(const QStringList& args, const QString& name) {
    return args.contains(name);
}

void printHelp(QTextStream& out) {
    out << "VirtualWorld Core 1.0\n"
        << "Usage:\n"
        << "  VirtualWorld --check [arch]\n"
        << "  VirtualWorld --test [arch]\n"
        << "  VirtualWorld --create-qcow2 <path> <size-mb>\n"
        << "  VirtualWorld --print-command <vm.json>\n"
        << "  VirtualWorld --start <vm.json>\n"
        << "  VirtualWorld --stop <pid>\n"
        << "  VirtualWorld --export-qemu <vm.json> [--output <script>]\n"
        << "  VirtualWorld --export-utm <vm.json> --output <package.utm> [--guest-os <name>]\n"
        << "  VirtualWorld --import-qemu <command-or-file> --save-config <vm.json>\n"
        << "  VirtualWorld --import-utm <utm-package-or-plist> --save-config <vm.json>\n"
        << "  VirtualWorld --import-virtualworld <vm.json> --save-config <vm.json>\n"
        << "  VirtualWorld --gui\n\n"
        << "QEMU lookup order:\n"
        << "  1. runtime/qemu/<platform>/<host-arch>/bin\n"
        << "  2. runtime/qemu/<platform>/<host-arch>\n"
        << "  3. runtime/qemu/<platform>/bin\n"
        << "  4. runtime/qemu/<platform>\n"
        << "  5. system PATH\n\n"
        << "Hardware acceleration:\n"
        << "  macOS: HVF when guest architecture is compatible\n"
        << "  Windows: WHPX when guest architecture is compatible\n"
        << "  Linux/HarmonyOS/OpenHarmony: KVM when /dev/kvm is available\n"
        << "  Fallback: TCG\n";
}

int saveImportedConfig(const vw::VMManager& manager, const vw::VmConfig& config, const QString& path, QTextStream& out, QTextStream& err) {
    if (path.isEmpty()) {
        err << "Missing --save-config <path>.\n";
        return 2;
    }
    QString error;
    if (!manager.saveConfig(path, manager.normaliseConfig(config), &error)) {
        err << error << "\n";
        return 1;
    }
    out << "True\n";
    return 0;
}

} // namespace

int main(int argc, char* argv[]) {
    QStringList rawArgs;
    for (int i = 1; i < argc; ++i) {
        rawArgs << QString::fromLocal8Bit(argv[i]);
    }

    if (rawArgs.isEmpty() || rawArgs.contains("--gui")) {
        QApplication app(argc, argv);
        app.setApplicationName("VirtualWorld");
        app.setApplicationVersion("1.0");
        vw::VirtualWorldWindow window;
        window.show();
        return app.exec();
    }

    QCoreApplication app(argc, argv);
    app.setApplicationName("VirtualWorld");
    app.setApplicationVersion("1.0");

    QTextStream out(stdout);
    QTextStream err(stderr);
    const QStringList args = rawArgs;
    if (hasOption(args, "--help") || hasOption(args, "-h")) {
        printHelp(out);
        return 0;
    }

    try {
        vw::VMManager manager;
        if (hasOption(args, "--check")) {
            const QString arch = optionValue(args, "--check", "x86_64");
            const vw::QemuEngineInfo info = manager.detectRuntime(arch);
            const vw::HostProfile host = vw::detectHostProfile();
            out << "system=" << (info.systemEmulator.available ? info.systemEmulator.executable : "missing") << "\n";
            out << "system_source=" << info.systemEmulator.source << "\n";
            out << "qemu_img=" << (info.imageTool.available ? info.imageTool.executable : "missing") << "\n";
            out << "qemu_img_source=" << info.imageTool.source << "\n";
            out << "host=" << host.name << "\n";
            out << "host_arch=" << host.arch << "\n";
            out << "guest_arch=" << vw::normalizeArchitecture(arch) << "\n";
            out << "accelerator=" << vw::selectAccelerator(arch, "auto", host) << "\n";
            out << "accelerator_candidates=" << vw::acceleratorCandidates(arch, host).join(',') << "\n";
            return info.canRunVm() ? 0 : 1;
        }

        if (hasOption(args, "--test")) {
            const QString arch = optionValue(args, "--test", "x86_64");
            QString output;
            QString error;
            if (!manager.testRuntime(arch, &output, &error)) {
                err << error << "\n";
                return 1;
            }
            out << output << "\n";
            return 0;
        }

        const int createIndex = args.indexOf("--create-qcow2");
        if (createIndex >= 0) {
            if (createIndex + 2 >= args.size()) {
                err << "Usage: VirtualWorld --create-qcow2 <path> <size-mb>\n";
                return 2;
            }
            bool ok = false;
            const qint64 sizeMb = args.at(createIndex + 2).toLongLong(&ok);
            if (!ok || sizeMb < 1) {
                err << "Invalid qcow2 size.\n";
                return 2;
            }
            QString error;
            if (!manager.createDisk(args.at(createIndex + 1), sizeMb, &error)) {
                err << error << "\n";
                return 1;
            }
            out << "True\n";
            return 0;
        }

        const QString savePath = optionValue(args, "--save-config");
        const QString qemuSource = optionValue(args, "--import-qemu");
        if (!qemuSource.isEmpty()) {
            return saveImportedConfig(manager, vw::parseQemuCommand(readSourceText(qemuSource)), savePath, out, err);
        }
        const QString utmSource = optionValue(args, "--import-utm");
        if (!utmSource.isEmpty()) {
            return saveImportedConfig(manager, vw::parseUtmSource(utmSource), savePath, out, err);
        }
        const QString vwSource = optionValue(args, "--import-virtualworld");
        if (!vwSource.isEmpty()) {
            return saveImportedConfig(manager, manager.loadConfig(vwSource), savePath, out, err);
        }

        const QString printConfig = optionValue(args, "--print-command");
        if (!printConfig.isEmpty()) {
            out << manager.renderQemuCommand(manager.loadConfig(printConfig)) << "\n";
            return 0;
        }

        const QString exportQemuConfig = optionValue(args, "--export-qemu");
        if (!exportQemuConfig.isEmpty()) {
            const QString command = manager.renderQemuCommand(manager.loadConfig(exportQemuConfig));
            const QString outputPath = optionValue(args, "--output");
            if (outputPath.isEmpty()) {
                out << command << "\n";
                return 0;
            }
            QFile file(outputPath);
            if (!file.open(QIODevice::WriteOnly | QIODevice::Text | QIODevice::Truncate)) {
                err << file.errorString() << "\n";
                return 1;
            }
            file.write(command.toUtf8());
            out << "True\n";
            return 0;
        }

        const QString exportUtmConfig = optionValue(args, "--export-utm");
        if (!exportUtmConfig.isEmpty()) {
            const QString outputPath = optionValue(args, "--output");
            if (outputPath.isEmpty()) {
                err << "Missing --output <package.utm>.\n";
                return 2;
            }
            QVector<vw::ValidationIssue> issues;
            const QString plist = vw::renderUtmPlist(manager.loadConfig(exportUtmConfig), optionValue(args, "--guest-os"), &issues);
            QString error;
            if (!vw::writeUtmPackage(outputPath, plist, &error)) {
                err << error << "\n";
                return 1;
            }
            for (const auto& issue : issues) {
                err << issue.level << ": " << issue.message << "\n";
            }
            out << "True\n";
            return 0;
        }

        const QString startConfig = optionValue(args, "--start");
        if (!startConfig.isEmpty()) {
            qint64 pid = 0;
            QString error;
            if (!manager.start(manager.loadConfig(startConfig), &pid, &error)) {
                err << error << "\n";
                return 1;
            }
            out << "True\npid=" << pid << "\n";
            return 0;
        }

        const QString stopPid = optionValue(args, "--stop");
        if (!stopPid.isEmpty()) {
            bool ok = false;
            const qint64 pid = stopPid.toLongLong(&ok);
            if (!ok) {
                err << "Invalid process id.\n";
                return 2;
            }
            QString error;
            if (!manager.stop(pid, &error)) {
                err << error << "\n";
                return 1;
            }
            out << "True\n";
            return 0;
        }
    } catch (const std::exception& exc) {
        err << exc.what() << "\n";
        return 1;
    }

    printHelp(out);
    return 2;
}
