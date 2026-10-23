#include "Translator.hpp"
#include "QemuParser.hpp"
#include "RecoveryGuard.hpp"
#include "SupportTrust.hpp"
#include "SystemSoftwareEngine.hpp"
#include "SystemSoftwareRomGenerator.hpp"

#include <QDebug>
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QTemporaryDir>

namespace {

bool containsPair(const QStringList& values, const QString& first, const QString& second) {
    for (int index = 0; index + 1 < values.size(); ++index) {
        if (values.at(index) == first && values.at(index + 1) == second) {
            return true;
        }
    }
    return false;
}

bool containsValueWith(const QStringList& values, const QString& option, const QString& fragment) {
    for (int index = 0; index + 1 < values.size(); ++index) {
        if (values.at(index) == option && values.at(index + 1).contains(fragment)) {
            return true;
        }
    }
    return false;
}

int fail(const QString& message) {
    qCritical().noquote() << message;
    return 1;
}

} // namespace

int main(int argc, char** argv) {
    QCoreApplication application(argc, argv);
    if (!vw::isDisklessSystemSoftwareVersion("0")
        || !vw::isDisklessSystemSoftwareVersion("1")
        || !vw::isDisklessSystemSoftwareVersion("2")
        || !vw::isDisklessSystemSoftwareVersion("3")
        || vw::isDisklessSystemSoftwareVersion("4")
        || vw::isDisklessSystemSoftwareVersion("6")) {
        return fail("System Software diskless version policy is incorrect.");
    }
    vw::VmConfig systemSoftware;
    systemSoftware.backend = "system-software";
    systemSoftware.systemSoftwareVersion = "0";
    systemSoftware.systemSoftwareRomPath = "/tmp/system-software-rom.rom";
    const QStringList disklessArgs = vw::SystemSoftwareEngine().buildArguments(systemSoftware);
    if (disklessArgs != QStringList{QStringLiteral("rom:/tmp/system-software-rom.rom")}) {
        return fail("diskless System Software inputs should contain only the in-process ROM descriptor.");
    }
    if (vw::systemSoftwareRuntimeVariant(systemSoftware) != "twiggy") {
        return fail("System Software 0 must select the Twiggy runtime.");
    }
    vw::VmConfig legacy128;
    legacy128.systemSoftwareVersion = "6";
    legacy128.systemSoftwareRomPath = "/tmp/Mac128k.rom";
    if (vw::systemSoftwareRuntimeVariant(legacy128) != "128k") {
        return fail("Legacy configurations must infer the 128K runtime from the ROM name.");
    }
    vw::VmConfig mac512;
    mac512.systemSoftwareMachine = "Macintosh 512K";
    mac512.systemSoftwareRomType = "mac128k";
    if (vw::systemSoftwareRuntimeVariant(mac512) != "512ke") {
        return fail("Macintosh 512K must select its machine-specific runtime.");
    }
    vw::VmConfig macPlus;
    macPlus.systemSoftwareMachine = "Mac Plus";
    if (vw::systemSoftwareRuntimeVariant(macPlus) != "plus") {
        return fail("Mac Plus must select the Plus runtime.");
    }
    vw::VmConfig classic;
    classic.backend = "system-software";
    classic.systemSoftwareVersion = "2";
    classic.systemSoftwareRomPath = "/tmp/classic.rom";
    classic.disks.append({"/tmp/floppy-b.dsk", "floppy", "dsk", false, "floppy", 0, "floppy-b"});
    classic.disks.append({"/tmp/floppy-a.dsk", "floppy", "dsk", false, "floppy", 0, "floppy-a"});
    const QStringList classicArgs = vw::SystemSoftwareEngine().buildArguments(classic);
    if (classicArgs != QStringList{QStringLiteral("rom:/tmp/classic.rom"),
                                  QStringLiteral("disk:/tmp/floppy-a.dsk"),
                                  QStringLiteral("disk:/tmp/floppy-b.dsk")}) {
        return fail("System Software must pass Floppy A before Floppy B.");
    }
    vw::VmConfig tiny;
    vw::setMemoryKb(tiny, 128);
    if (vw::effectiveMemoryKb(tiny) != 128 || vw::formatQemuMemory(128) != "128K") {
        return fail("128 KB memory configuration was not preserved.");
    }

    const vw::VmConfig importedAudio = vw::parseQemuCommand(
        "qemu-system-x86_64 -audiodev driver=pa,id=audio0 -device AC97,audiodev=audio0");
    if (!importedAudio.audio.enabled || importedAudio.audio.driver != "pa"
        || importedAudio.audio.model.compare("AC97", Qt::CaseInsensitive) != 0
        || importedAudio.audio.id != "audio0") {
        return fail("QEMU audio options were not parsed into AudioConfig.");
    }
    const QStringList audioArgs = vw::buildQemuCommandParts(importedAudio, vw::HostPlatform::Linux);
    if (!containsValueWith(audioArgs, "-audiodev", "driver=pa,id=audio0")
        || !containsValueWith(audioArgs, "-device", "AC97,audiodev=audio0")) {
        return fail("AudioConfig was not rendered as QEMU audio devices.");
    }

    vw::VmConfig m68kAudio = importedAudio;
    m68kAudio.guestOs = "Classic MacOS";
    m68kAudio.architecture = "m68k";
    m68kAudio.machine = "q800";
    const QStringList m68kAudioArgs = vw::buildQemuCommandParts(
        vw::convertToQemu(m68kAudio, vw::HostPlatform::Linux).config, vw::HostPlatform::Linux);
    if (!containsValueWith(m68kAudioArgs, "-audiodev", "driver=pa,id=audio0")
        || !containsValueWith(m68kAudioArgs, "-machine", "audiodev=audio0,easc=off")
        || containsPair(m68kAudioArgs, "-device", "AC97,audiodev=audio0")
        || containsPair(m68kAudioArgs, "-audio", "none")) {
        return fail("Classic m68k audio must use the q800 ASC compatibility path.");
    }

    vw::VmConfig m68kNetwork;
    m68kNetwork.guestOs = "Classic MacOS";
    m68kNetwork.architecture = "m68k";
    m68kNetwork.machine = "q800";
    const vw::ConversionResult m68kNetworkConverted =
        vw::convertToQemu(m68kNetwork, vw::HostPlatform::Linux);
    const QStringList m68kNetworkArgs =
        vw::buildQemuCommandParts(m68kNetworkConverted.config, vw::HostPlatform::Linux);
    if (!containsValueWith(m68kNetworkArgs, "-nic", "user,id=net0")
        || !containsValueWith(m68kNetworkArgs, "-nic", "model=dp83932")
        || containsPair(m68kNetworkArgs, "-device", "dp83932")) {
        return fail("Classic m68k must use the q800 SONIC network through -nic.");
    }

    vw::VmConfig config;
    config.guestOs = "macOS";
    config.architecture = "aarch64";
    config.machine = "virt";
    config.accelerator = "tcg";
    config.cpuCores = 4;
    config.memoryMb = 8192;
    config.graphics.adapter = "virtio-gpu-pci";
    config.graphics.ramMb = 1536;
    config.usb.controller = "usb";
    config.usb.devices = QStringList{"usb-kbd", "usb-tablet"};
    config.disks.append({"/tmp/virtualworld-usb-test.img", "usb", "raw", false, "disk"});

    const vw::ConversionResult converted = vw::convertToQemu(config, vw::HostPlatform::MacOS);
    if (converted.config.usb.controller != "qemu-xhci") {
        return fail("virt machines must use qemu-xhci for USB input devices.");
    }

    const QStringList args = vw::buildQemuCommandParts(converted.config, vw::HostPlatform::MacOS);
    if (!containsPair(args, "-device", "qemu-xhci") || args.contains("-usb")) {
        return fail("ARM virt command must add qemu-xhci instead of legacy -usb.");
    }

    const QString command = args.join(' ');
    if (!command.contains("hostmem=1073741824,max_hostmem=1073741824")) {
        return fail("Virtio GPU host memory must be a power of two.");
    }
    if (command.contains("hostmem=1610612736")) {
        return fail("Virtio GPU host memory must not keep the invalid 1536 MiB value.");
    }

    vw::VmConfig ppcConfig;
    ppcConfig.guestOs = "Classic MacOS";
    ppcConfig.architecture = "ppc";
    ppcConfig.machine = "mac99";
    ppcConfig.graphics.adapter = "VGA";
    ppcConfig.graphics.vga = "VGA";

    const vw::ConversionResult ppcConverted = vw::convertToQemu(ppcConfig, vw::HostPlatform::MacOS);
    if (!ppcConverted.config.graphics.adapter.isEmpty() || ppcConverted.config.graphics.vga != "std") {
        return fail("Classic PPC Mac configurations must use -vga std without a duplicate VGA device.");
    }

    const QStringList ppcArgs = vw::buildQemuCommandParts(ppcConverted.config, vw::HostPlatform::MacOS);
    if (!containsPair(ppcArgs, "-vga", "std") || containsPair(ppcArgs, "-vga", "VGA")
        || containsPair(ppcArgs, "-device", "VGA")) {
        return fail("Classic PPC command must not emit the unsupported -vga VGA argument.");
    }
    if (!containsValueWith(ppcArgs, "-nic", "model=sungem")
        || containsPair(ppcArgs, "-device", "sungem")) {
        return fail("Classic mac99 must use its built-in Sun GEM network controller.");
    }

    vw::VmConfig stalePpcConfig;
    stalePpcConfig.guestOs = "Classic MacOS";
    stalePpcConfig.architecture = "ppc";
    stalePpcConfig.machine = "mac99";
    stalePpcConfig.graphics.adapter = "std";
    const vw::ConversionResult stalePpcConverted =
        vw::convertToQemu(stalePpcConfig, vw::HostPlatform::MacOS);
    const QStringList stalePpcArgs =
        vw::buildQemuCommandParts(stalePpcConverted.config, vw::HostPlatform::MacOS);
    if (!stalePpcConverted.config.graphics.adapter.isEmpty()
        || stalePpcConverted.config.graphics.vga != "std"
        || !containsPair(stalePpcArgs, "-vga", "std")
        || containsPair(stalePpcArgs, "-device", "std")) {
        return fail("Stale PPC graphics adapter 'std' must be normalized to -vga std.");
    }

    const vw::VmConfig importedPpc = vw::parseQemuCommand(
        "qemu-system-ppc -M mac99 -device std");
    const vw::ConversionResult importedPpcConverted =
        vw::convertToQemu(importedPpc, vw::HostPlatform::MacOS);
    const QStringList importedPpcArgs =
        vw::buildQemuCommandParts(importedPpcConverted.config, vw::HostPlatform::MacOS);
    if (!containsPair(importedPpcArgs, "-vga", "std")
        || containsPair(importedPpcArgs, "-device", "std")) {
        return fail("Imported legacy '-device std' must be converted to -vga std.");
    }

    vw::VmConfig persistedPpc;
    persistedPpc.guestOs = "Classic MacOS";
    persistedPpc.architecture = "ppc";
    persistedPpc.machine = "mac99";
    persistedPpc.unsupportedArgs = QStringList{"-device", "std"};
    const vw::ConversionResult persistedPpcConverted =
        vw::convertToQemu(persistedPpc, vw::HostPlatform::MacOS);
    const QStringList persistedPpcArgs =
        vw::buildQemuCommandParts(persistedPpcConverted.config, vw::HostPlatform::MacOS);
    if (!containsPair(persistedPpcArgs, "-vga", "std")
        || containsPair(persistedPpcArgs, "-device", "std")) {
        return fail("Persisted legacy '-device std' must be removed before QEMU launch.");
    }

    vw::VmConfig ppcStorageConfig;
    ppcStorageConfig.guestOs = "Classic MacOS";
    ppcStorageConfig.architecture = "ppc";
    ppcStorageConfig.machine = "mac99";
    ppcStorageConfig.usb.controller = "usb";
    ppcStorageConfig.usb.devices = QStringList{"usb-kbd", "usb-mouse", "usb-tablet"};
    ppcStorageConfig.disks.append({"/tmp/install.iso", "cdrom", "raw", true, "cdrom"});
    ppcStorageConfig.disks.append({"/tmp/system.qcow2", "virtio", "qcow2", false, "disk", 2048});
    const vw::ConversionResult ppcStorageConverted =
        vw::convertToQemu(ppcStorageConfig, vw::HostPlatform::MacOS);
    const QStringList ppcStorageArgs =
        vw::buildQemuCommandParts(ppcStorageConverted.config, vw::HostPlatform::MacOS);
    if (!containsValueWith(ppcStorageArgs, "-drive", "file=/tmp/install.iso,if=ide,media=cdrom,format=raw,readonly=on,index=2")
        || !containsValueWith(ppcStorageArgs, "-drive", "file=/tmp/system.qcow2,if=ide,format=qcow2,index=0")
        || containsValueWith(ppcStorageArgs, "-device", "ide-hd,drive=vw_ide")
        || containsPair(ppcStorageArgs, "-device", "usb-kbd")
        || containsPair(ppcStorageArgs, "-device", "usb-mouse")
        || containsPair(ppcStorageArgs, "-device", "usb-tablet")
        || !ppcStorageConverted.config.usb.controller.isEmpty()
        || !ppcStorageConverted.config.usb.devices.isEmpty()) {
        return fail("mac99 must use its built-in IDE storage and ADB input, without USB input devices.");
    }

    vw::VmConfig bootOrderConfig;
    bootOrderConfig.guestOs = "generic";
    bootOrderConfig.architecture = "x86_64";
    bootOrderConfig.machine = "q35";
    bootOrderConfig.disks.append({"/tmp/system.qcow2", "sata", "qcow2", false, "disk"});
    bootOrderConfig.disks.append({"/tmp/install.iso", "sata", "raw", true, "cdrom"});
    const QStringList bootOrderArgs =
        vw::buildQemuCommandParts(bootOrderConfig, vw::HostPlatform::MacOS);
    if (!containsPair(bootOrderArgs, "-boot", "order=c,menu=on,strict=on")
        || !containsPair(bootOrderArgs, "-device", "ide-hd,drive=vw_sata0,bus=vw_ahci0.0,bootindex=1")
        || !containsPair(bootOrderArgs, "-device", "ide-cd,drive=vw_sata1,bus=vw_ahci0.1,bootindex=2")) {
        return fail("storage list order must remain the QEMU boot order.");
    }

    vw::VmConfig oldWindowsConfig;
    oldWindowsConfig.guestOs = "Old Windows";
    oldWindowsConfig.architecture = "i386";
    oldWindowsConfig.machine = "pc";
    vw::setMemoryKb(oldWindowsConfig, 640);
    oldWindowsConfig.disks.append({"/tmp/old-windows.qcow2", "virtio", "qcow2", false, "disk"});
    const vw::ConversionResult oldWindowsConverted =
        vw::convertToQemu(oldWindowsConfig, vw::HostPlatform::MacOS);
    const QStringList oldWindowsArgs =
        vw::buildQemuCommandParts(oldWindowsConverted.config, vw::HostPlatform::MacOS);
    if (vw::effectiveMemoryKb(oldWindowsConverted.config) != 640
        || !containsPair(oldWindowsArgs, "-m", "1M")
        || !containsValueWith(oldWindowsArgs, "-drive", "file=/tmp/old-windows.qcow2,if=ide,format=qcow2,index=0")
        || !containsPair(oldWindowsArgs, "-vga", "std")) {
        return fail("Old Windows must preserve 640 KB configuration while using QEMU's 1 MB PC/VGA minimum.");
    }
    const vw::VmConfig parsedLowMemory =
        vw::parseQemuCommand("qemu-system-i386 -m 640K");
    if (vw::effectiveMemoryKb(parsedLowMemory) != 640) {
        return fail("QEMU memory imports must preserve kilobyte-sized values.");
    }

    vw::VmConfig floppyConfig;
    floppyConfig.guestOs = "Old Windows";
    floppyConfig.architecture = "i386";
    floppyConfig.machine = "pc";
    floppyConfig.disks.append({"/tmp/windows-boot.img", "floppy", "img", false, "floppy"});
    floppyConfig.disks.append({"/tmp/old-windows.qcow2", "ide", "qcow2", false, "disk"});
    const QStringList floppyArgs =
        vw::buildQemuCommandParts(floppyConfig, vw::HostPlatform::MacOS);
    if (!containsPair(floppyArgs, "-boot", "order=a,menu=on,strict=on")
        || !containsValueWith(floppyArgs, "-drive", "file=/tmp/windows-boot.img,if=floppy,media=disk,format=raw")
        || !containsValueWith(floppyArgs, "-drive", "file=/tmp/old-windows.qcow2,if=ide,format=qcow2,index=1")) {
        return fail("Old Windows floppy images must be attached as A: and boot before the hard disk.");
    }
    const vw::VmConfig parsedFloppy =
        vw::parseQemuCommand("qemu-system-i386 -fda /tmp/windows-boot.img");
    if (parsedFloppy.disks.size() != 1
        || parsedFloppy.disks.first().media != "floppy"
        || parsedFloppy.disks.first().interfaceName != "floppy") {
        return fail("QEMU floppy options must import as floppy media.");
    }
    const vw::VmConfig parsedDriveFloppy =
        vw::parseQemuCommand("qemu-system-i386 -drive file=/tmp/windows-boot.img,if=floppy,format=raw");
    if (parsedDriveFloppy.disks.size() != 1
        || parsedDriveFloppy.disks.first().media != "floppy") {
        return fail("QEMU if=floppy drives must import as floppy media.");
    }

    vw::VmConfig windowsConfig;
    windowsConfig.guestOs = "Windows XP";
    if (!vw::isSupportedWindowsGuest(windowsConfig)) {
        return fail("Windows XP must be eligible for the recovery guard.");
    }
    windowsConfig.guestOs = "Windows 98";
    if (vw::isSupportedWindowsGuest(windowsConfig)) {
        return fail("Windows 98 must remain outside the recovery guard scope.");
    }
    vw::VmConfig linuxConfig;
    linuxConfig.guestOs = "Linux";
    if (!vw::isSupportedLinuxGuest(linuxConfig)) {
        return fail("Linux must be eligible for the Linux Support Disk.");
    }
    linuxConfig.guestOs = "Linux Mint";
    if (!vw::isSupportedLinuxGuest(linuxConfig)) {
        return fail("Linux distributions must be eligible for the Linux Support Disk.");
    }
    vw::VmConfig macConfig;
    macConfig.guestOs = "macOS";
    if (vw::detectHostProfile().platform == vw::HostPlatform::MacOS
        && !vw::isSupportedMacosGuest(macConfig)) {
        return fail("macOS guests must be eligible for the macOS Support Disk on macOS hosts.");
    }
    if (vw::detectHostProfile().platform != vw::HostPlatform::MacOS
        && vw::isSupportedMacosGuest(macConfig)) {
        return fail("macOS Support Disk must be unavailable on non-macOS hosts.");
    }
    vw::DiskConfig macGuard;
    macGuard.role = "macos-recovery-guard";
    macGuard.readonly = true;
    if (!vw::isRecoveryGuardDisk(macGuard)
        || !vw::isMacosRecoveryGuardDisk(macGuard)) {
        return fail("macOS Support Disk role must be protected.");
    }
    vw::DiskConfig linuxGuard;
    linuxGuard.role = "linux-recovery-guard";
    linuxGuard.readonly = true;
    if (!vw::isRecoveryGuardDisk(linuxGuard)
        || !vw::isLinuxRecoveryGuardDisk(linuxGuard)) {
        return fail("Linux Support Disk role must be protected.");
    }

    const QByteArray ed25519PublicKey =
        "xgE/qmi93u/wcw90+fLLrm7H4/mfLh3lMg0VTZtaoRI=";
    const QByteArray ed25519Signature =
        "okbGDpwG35ApbgVvhDsefy7R+mP/axUTmBAktD5gOVzqXMBu4G3AAkPKD0bm7jsIn0q+VJDrAo6t6n4nkvDYBg==";
    if (!vw::verifyEd25519(ed25519PublicKey,
                           "virtualworld-ed25519-test",
                           QByteArray::fromBase64(ed25519Signature))) {
        return fail("Ed25519 verification vector failed.");
    }
    QString authorizationError;
    if (vw::authorizeHighRiskOperation(QJsonObject(), "delete-host-data", "test", false, &authorizationError)
        || authorizationError.isEmpty()) {
        return fail("high-risk operations must require explicit confirmation and a signed token.");
    }

    vw::VmConfig guardedWindows;
    guardedWindows.guestOs = "Windows 11";
    guardedWindows.architecture = "x86_64";
    guardedWindows.machine = "q35";
    guardedWindows.disks.append({"/tmp/windows.qcow2", "sata", "qcow2", false, "disk"});
    vw::DiskConfig guardDisk;
    guardDisk.path = "/tmp/VirtualWorld-RecoveryGuard.qcow2";
    guardDisk.interfaceName = "ide";
    guardDisk.format = "qcow2";
    guardDisk.readonly = true;
    guardDisk.media = "disk";
    guardDisk.sizeMb = 64;
    guardDisk.role = "recovery-guard";
    guardedWindows.disks.append(guardDisk);
    const QStringList guardedArgs =
        vw::buildQemuCommandParts(guardedWindows, vw::HostPlatform::MacOS);
    if (!containsPair(guardedArgs, "-device", "ide-hd,drive=vw_sata0,bus=vw_ahci0.0,bootindex=1")
        || !containsValueWith(guardedArgs, "-drive",
                              "file=/tmp/VirtualWorld-RecoveryGuard.qcow2,if=ide,format=qcow2,readonly=on")
        || containsValueWith(guardedArgs, "-drive",
                             "file=/tmp/VirtualWorld-RecoveryGuard.qcow2,if=ide,format=qcow2,readonly=on,bootindex=")) {
        return fail("the recovery guard disk must be read-only and excluded from boot order.");
    }

    QTemporaryDir romSource;
    QTemporaryDir romStorage;
    if (!romSource.isValid() || !romStorage.isValid()) {
        return fail("temporary directories for System Software ROM generation are unavailable.");
    }
    const QList<QPair<QString, char>> romParts = {
        {"HIGH0B.BIN", '\x10'},
        {"HIGH1B.BIN", '\x30'},
        {"LOW0B.BIN", '\x20'},
        {"LOW1B.BIN", '\x40'},
    };
    for (const auto& part : romParts) {
        QFile file(QDir(romSource.path()).filePath(part.first));
        if (!file.open(QIODevice::WriteOnly)
            || file.write(QByteArray(16 * 1024, part.second)) != 16 * 1024) {
            return fail("could not create temporary System Software ROM parts.");
        }
    }
    qputenv("VIRTUALWORLD_SYSTEM_SOFTWARE_ROMS", romSource.path().toLocal8Bit());
    QString generatedRom;
    QString romError;
    if (!vw::ensureSystemSoftwareRom(&generatedRom, romStorage.path(), &romError,
                                     QStringLiteral("macplus"))) {
        qunsetenv("VIRTUALWORLD_SYSTEM_SOFTWARE_ROMS");
        return fail("System Software ROM generation failed: " + romError);
    }
    QFile generatedFile(generatedRom);
    if (!generatedFile.open(QIODevice::ReadOnly)) {
        qunsetenv("VIRTUALWORLD_SYSTEM_SOFTWARE_ROMS");
        return fail("generated System Software ROM cannot be opened.");
    }
    const QByteArray generatedBytes = generatedFile.readAll();
    if (generatedBytes.size() != static_cast<qsizetype>(vw::MiniVmacInProcessCore::romSize())) {
        qunsetenv("VIRTUALWORLD_SYSTEM_SOFTWARE_ROMS");
        return fail("generated System Software ROM does not match the linked in-process engine size.");
    }
    QString reusedRom = generatedRom;
    if (!vw::ensureSystemSoftwareRom(&reusedRom, romStorage.path(), &romError,
                                     QStringLiteral("macplus"))
        || reusedRom != generatedRom) {
        qunsetenv("VIRTUALWORLD_SYSTEM_SOFTWARE_ROMS");
        return fail("generated System Software ROM path was not reused.");
    }
    QString existingRom = "/tmp/already-selected-system-software.rom";
    if (!vw::ensureSystemSoftwareRom(&existingRom, romStorage.path(), &romError)
        || existingRom != "/tmp/already-selected-system-software.rom") {
        qunsetenv("VIRTUALWORLD_SYSTEM_SOFTWARE_ROMS");
        return fail("an explicitly selected System Software ROM must remain unchanged.");
    }
    qunsetenv("VIRTUALWORLD_SYSTEM_SOFTWARE_ROMS");

    return 0;
}
