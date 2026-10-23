#include "AppLanguage.hpp"
#include "ConfigManager.hpp"
#include "Platform.hpp"
#include "QemuParser.hpp"
#include "SupportTrust.hpp"
#include "SystemSoftwareEngine.hpp"
#include "SystemSoftwareRomGenerator.hpp"
#include "UtmParser.hpp"
#include "VirtualWorldWindow.hpp"
#include "VMManager.hpp"
#include "VmConfig.hpp"

#include <QApplication>
#include <QCoreApplication>
#include <QFile>
#include <QFileInfo>
#include <QTextStream>
#include <QThread>
#include <memory>
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
        << "  VirtualWorld --check [arch] [--json]\n"
        << "  VirtualWorld --list-backends [arch] [--json]\n"
        << "  VirtualWorld --test [arch]\n"
        << "  VirtualWorld --create-qcow2 <path> <size-mb>\n"
        << "  VirtualWorld --print-command <vm.json>\n"
        << "  VirtualWorld --start <vm.json> [--backend <id>] [--wait]\n"
        << "  VirtualWorld --stop <pid>\n"
        << "  VirtualWorld --verify-support-disk <manifest.json> <support-disk>\n"
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
        << "  Fallback: TCG\n\n"
        << "Virtualization backends:\n"
        << "  --list-backends [arch]        list backends and capabilities (key=value)\n"
        << "  --list-backends [arch] --json machine-readable JSON report\n"
        << "  --check [arch] --json         same JSON report for one guest architecture\n"
        << "  --start <vm.json> --backend <id|auto|qemu> [--wait]\n"
        << "  Backend ids: macos-virtualization, windows-hypervisor, linux-kvm,\n"
        << "               harmony-virtualization, qemu-hardware, qemu-tcg\n"
        << "  Auto order: native (same arch) > QEMU hardware acceleration > TCG\n"
        << "  VIRTUALWORLD_BACKEND overrides the VM config's backend field.\n";
}

// Machine-readable, one key=value line per fact. Stable enough for scripts;
// `--json` is available when a nested structure is needed.
void printBackendCatalogue(QTextStream& out, const vw::VMManager& manager, const QString& guestArch) {
    vw::VmConfig probe;
    probe.architecture = guestArch.trimmed().isEmpty() ? QStringLiteral("x86_64") : guestArch.trimmed();
    const QVector<vw::EngineProbeResult> backends = manager.virtualizationRegistry().describeBackends(guestArch);
    for (const vw::EngineProbeResult& probeResult : backends) {
        // `status` may be backend-specific; `status_class` is the canonical
        // fold. Both are printed so scripts can use either.
        const vw::VirtualizationEnginePtr engine =
            manager.virtualizationRegistry().backend(probeResult.backendId);
        const QString status = engine ? engine->statusCode(probe) : vw::backend_status::notRegistered();
        const QString statusClassValue = vw::statusClass(status);
        const QString runsInProcess = engine && engine->runsInProcess() ? "true" : "false";
        // `tier` is the mechanism this backend wraps; `execution_tier` is what
        // this guest would really get here. They diverge when a backend degrades
        // (WHPX with no hypervisor, KVM with no /dev/kvm) — scripts should read
        // `execution_tier`.
        const QString executionTier = vw::tierName(
            engine ? engine->executionTierFor(probe.architecture)
                   : vw::VirtualizationTier::SoftwareEmulation);
        out << "backend=" << probeResult.backendId << "\n"
            << "name=" << probeResult.displayName << "\n"
            << "tier=" << vw::tierName(
                   manager.virtualizationRegistry().backend(probeResult.backendId)
                       ? manager.virtualizationRegistry().backend(probeResult.backendId)->tier()
                       : vw::VirtualizationTier::SoftwareEmulation)
            << "\n"
            << "execution_tier=" << executionTier << "\n"
            << "host_supported=" << (probeResult.capabilities.hostSupported ? "true" : "false") << "\n"
            << "guest_arch_supported=" << (probeResult.capabilities.guestArchSupported ? "true" : "false") << "\n"
            << "hardware_acceleration=" << (probeResult.capabilities.hardwareAcceleration ? "true" : "false") << "\n"
            << "native_execution=" << (probeResult.capabilities.nativeExecution ? "true" : "false") << "\n"
            << "accelerator=" << probeResult.capabilities.accelerator << "\n"
            << "summary=" << probeResult.summary << "\n"
            << "status=" << status << "\n"
            << "status_class=" << statusClassValue << "\n"
            << "runs_in_process=" << runsInProcess << "\n"
            << "--\n";
    }
    const vw::EngineSelection selection = manager.selectBackend(probe);
    const vw::VirtualizationEnginePtr selectedEngine =
        manager.virtualizationRegistry().backend(selection.backendId);
    out << "selected_backend=" << selection.backendId << "\n"
        << "selected_tier=" << vw::tierName(selection.tier) << "\n"
        << "selected_execution_tier=" << vw::tierName(
               selectedEngine ? selectedEngine->executionTierFor(probe.architecture)
                              : vw::VirtualizationTier::SoftwareEmulation)
        << "\n"
        << "selected_ok=" << (selection.ok ? "true" : "false") << "\n"
        << "selected_fell_back=" << (selection.fellBack ? "true" : "false") << "\n"
        << "selected_fallback_from=" << selection.fallbackFrom << "\n"
        << "selected_reason=" << selection.reason << "\n"
        << "selection_order=" << selection.consideredIds().join(',') << "\n";
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
        app.setOrganizationName("VirtualWorld");
        app.setApplicationName("VirtualWorld");
        app.setApplicationVersion("1.0");
        if (!vw::hasSelectedLanguage()) {
            vw::chooseApplicationLanguage(nullptr, true);
        }
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
            if (hasOption(args, "--json")) {
                // Machine-readable mode replaces the key=value listing entirely
                // so the output is a single, parseable JSON document.
                vw::VmConfig probe;
                probe.architecture = arch;
                out << manager.backendReportJson(probe, arch) << "\n";
                return info.canRunVm() ? 0 : 1;
            }
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

        if (hasOption(args, "--list-backends")) {
            const QString arch = optionValue(args, "--list-backends");
            if (hasOption(args, "--json")) {
                vw::VmConfig probe;
                probe.architecture = arch.trimmed().isEmpty() ? QStringLiteral("x86_64") : arch.trimmed();
                out << manager.backendReportJson(probe, arch) << "\n";
            } else {
                printBackendCatalogue(out, manager, arch);
            }
            return 0;
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
        const int verifySupportIndex = args.indexOf("--verify-support-disk");
        if (verifySupportIndex >= 0) {
            if (verifySupportIndex + 2 >= args.size()) {
                err << "Usage: VirtualWorld --verify-support-disk <manifest.json> <support-disk>\n";
                return 2;
            }
            const vw::SupportDiskTrustResult result = vw::verifySupportDiskManifest(
                args.at(verifySupportIndex + 1),
                args.at(verifySupportIndex + 2));
            out << (result.trusted ? "trusted" : "untrusted") << "\n"
                << "reason=" << result.reason << "\n"
                << "product=" << result.product << "\n"
                << "version=" << result.version << "\n"
                << "source=" << result.source << "\n";
            return result.trusted ? 0 : 1;
        }
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
            const vw::VmConfig config = manager.loadConfig(printConfig);
            out << (vw::isSystemSoftwareConfig(config)
                        ? vw::SystemSoftwareEngine().renderCommand(config)
                        : manager.renderQemuCommand(config))
                << "\n";
            return 0;
        }

        const QString exportQemuConfig = optionValue(args, "--export-qemu");
        if (!exportQemuConfig.isEmpty()) {
            const vw::VmConfig config = manager.loadConfig(exportQemuConfig);
            if (vw::isSystemSoftwareConfig(config)) {
                err << "System Software configurations cannot be exported as QEMU scripts.\n";
                return 2;
            }
            const QString command = manager.renderQemuCommand(config);
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
            const vw::VmConfig config = manager.loadConfig(startConfig);
            const QString backendOption = optionValue(args, "--backend");
            bool started = false;
            vw::EngineSelection selection;
            /*
				system-software 走进程内引擎。它必须在 --start 分支的内存活到
				--wait 结束：进程内运行没有 PID，引擎对象一旦析构就等于停机。
				这里用 shared_ptr 持有，而不是像旧的 detached 子进程那样依赖
				父进程退出后子进程独立存活。
			*/
            std::shared_ptr<vw::SystemSoftwareEngine> inProcessSystemSoftware;
            if (vw::isSystemSoftwareConfig(config)) {
                vw::VmConfig launchConfig = config;
                /*
					Mini vMac 现在在本进程内运行：不创建子进程，也就没有 PID。
					严禁在此恢复 QProcess / startDetached / fork / exec 等派生路径
					（HarmonyOS/OpenHarmony 上是硬性禁止，其他平台同样按此契约处理）。
				*/
                inProcessSystemSoftware = std::make_shared<vw::SystemSoftwareEngine>();
                const size_t expectedRomBytes = vw::MiniVmacInProcessCore::romSize();
                if (vw::ensureSystemSoftwareRom(&launchConfig.systemSoftwareRomPath, launchConfig.storageDir, &error,
                                                launchConfig.systemSoftwareRomType)
                    && inProcessSystemSoftware->start(launchConfig, &error)) {
                    started = true;
                }
                if (!started && error.isEmpty()) {
                    error = QStringLiteral(
                                "System Software runtime failed to start in-process; the engine requires a ROM of "
                                "exactly %1 bytes (kROM_Size).")
                                .arg(static_cast<qulonglong>(expectedRomBytes));
                }
            } else if (backendOption.trimmed().isEmpty()
                       && vw::resolveBackendPreference(QString(), config).compare(
                              QStringLiteral("qemu"), Qt::CaseInsensitive) == 0) {
                // Legacy path, byte-for-byte unchanged: detached QEMU with the
                // accelerator chosen by the translator.
                started = manager.start(config, &pid, &error);
            } else {
                started = manager.startWithBackend(config, backendOption, &pid, &error, &selection);
                if (started && !selection.backendId.isEmpty()) {
                    out << "backend=" << selection.backendId << "\n";
                    out << "backend_tier=" << vw::tierName(selection.tier) << "\n";
                    if (selection.fellBack) {
                        out << "backend_fallback_from=" << selection.fallbackFrom << "\n";
                    }
                }
            }
            if (!started) {
                err << error << "\n";
                return 1;
            }
            out << "True\npid=" << pid << "\n";
            if (inProcessSystemSoftware) {
                out << "in_process=true" << "\n";
                out << "backend=system-software" << "\n";
                if (!hasOption(args, "--wait")) {
                    err << "警告：Mini vMac 在 VirtualWorld 进程内运行客户机，进程退出即结束虚拟机。"
                           "加 --wait 可保持进程存活。\n";
                }
            } else if (selection.engine && selection.engine->runsInProcess()) {
                out << "in_process=true" << "\n";
                if (!hasOption(args, "--wait")) {
                    err << "警告：后端 " << selection.backendId
                        << " 在 VirtualWorld 进程内运行客户机，进程退出即结束虚拟机。加 --wait 可保持进程存活。\n";
                }
            }
            if (hasOption(args, "--wait")) {
                if (inProcessSystemSoftware) {
                    while (inProcessSystemSoftware->isRunning()) {
                        QThread::msleep(250);
                    }
                    out << "vm_exit=true" << "\n";
                } else if (selection.engine && selection.engine->runsInProcess()) {
                    while (selection.engine->isRunning()) {
                        QThread::msleep(250);
                    }
                    out << "vm_exit=true" << "\n";
                }
            }
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
