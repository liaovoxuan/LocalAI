// VirtualizationEngineTests — contract tests for the virtualization backend
// layer: architecture normalization, honesty of the support claims, the
// selection order, and the machine-readable report.
//
// These tests are host-agnostic on purpose. They never require a real guest to
// boot; they assert that the layer reports the truth about what it can do.

#include "Platform.hpp"
#include "VirtualizationCapabilities.hpp"
#include "VirtualizationEngine.hpp"
#include "VirtualizationEngineRegistry.hpp"
#include "WindowsHypervisorEngine.hpp"

#include <QCoreApplication>
#include <QDebug>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QStringList>

namespace {

int failures = 0;
int checks = 0;

void check(bool condition, const QString& message) {
    ++checks;
    if (!condition) {
        ++failures;
        qCritical().noquote() << "FAIL:" << message;
    }
}

void checkEqual(const QString& actual, const QString& expected, const QString& what) {
    check(actual == expected,
          QStringLiteral("%1: expected \"%2\", got \"%3\"").arg(what, expected, actual));
}

QString otherArch(const QString& hostArch) {
    return hostArch == QStringLiteral("aarch64") ? QStringLiteral("x86_64") : QStringLiteral("aarch64");
}

// ---------------------------------------------------------------------------
// Architecture normalization
// ---------------------------------------------------------------------------

void testArchitectureNormalization() {
    checkEqual(vw::canonicalGuestArch(QStringLiteral("x86_64")), QStringLiteral("x86_64"), "x86_64");
    checkEqual(vw::canonicalGuestArch(QStringLiteral("amd64")), QStringLiteral("x86_64"), "amd64");
    checkEqual(vw::canonicalGuestArch(QStringLiteral("x64")), QStringLiteral("x86_64"), "x64");
    checkEqual(vw::canonicalGuestArch(QStringLiteral("X86_64")), QStringLiteral("x86_64"), "X86_64 upper");
    checkEqual(vw::canonicalGuestArch(QStringLiteral("arm64")), QStringLiteral("aarch64"), "arm64");
    checkEqual(vw::canonicalGuestArch(QStringLiteral("aarch64")), QStringLiteral("aarch64"), "aarch64");
    checkEqual(vw::canonicalGuestArch(QStringLiteral("ARM64")), QStringLiteral("aarch64"), "ARM64 upper");
    checkEqual(vw::canonicalGuestArch(QStringLiteral("arm64e")), QStringLiteral("aarch64"), "arm64e");
    checkEqual(vw::canonicalGuestArch(QStringLiteral("riscv64")), QStringLiteral("riscv64"), "riscv64");
    checkEqual(vw::canonicalGuestArch(QStringLiteral("riscv64gc")), QStringLiteral("riscv64"), "riscv64gc");
    checkEqual(vw::canonicalGuestArch(QStringLiteral("rv64")), QStringLiteral("riscv64"), "rv64");
    // Narrower ISAs must never be folded into the 64-bit spelling: a native
    // hypervisor cannot expose them.
    checkEqual(vw::canonicalGuestArch(QStringLiteral("i386")), QStringLiteral("i386"), "i386 stays i386");
    checkEqual(vw::canonicalGuestArch(QStringLiteral("arm")), QStringLiteral("arm"), "arm stays arm");
    checkEqual(vw::canonicalGuestArch(QStringLiteral("x86")), QStringLiteral("i386"), "x86 -> i386");
    check(vw::canonicalGuestArch(QString()).isEmpty(), "empty arch stays empty");
}

void testSameIsaRules() {
    check(vw::isSameNativeIsa(QStringLiteral("x86_64"), QStringLiteral("x86_64")), "x86_64 == x86_64");
    check(vw::isSameNativeIsa(QStringLiteral("aarch64"), QStringLiteral("arm64")), "aarch64 == arm64");
    check(!vw::isSameNativeIsa(QStringLiteral("x86_64"), QStringLiteral("aarch64")), "x86_64 != aarch64");
    check(!vw::isSameNativeIsa(QStringLiteral("aarch64"), QStringLiteral("x86_64")), "aarch64 != x86_64");
    check(!vw::isSameNativeIsa(QStringLiteral("x86_64"), QStringLiteral("i386")), "x86_64 != i386");
    check(!vw::isSameNativeIsa(QStringLiteral("aarch64"), QStringLiteral("arm")), "aarch64 != arm");
    check(!vw::isSameNativeIsa(QStringLiteral("riscv64"), QStringLiteral("x86_64")), "riscv64 != x86_64");

    checkEqual(vw::nativeGuestArchForHost(QStringLiteral("x86_64")), QStringLiteral("x86_64"),
               "native guest for x86_64 host");
    checkEqual(vw::nativeGuestArchForHost(QStringLiteral("arm64")), QStringLiteral("aarch64"),
               "native guest for arm64 host");
    check(vw::nativeGuestArchForHost(QStringLiteral("riscv64")).isEmpty(),
          "riscv64 host has no native backend guest");
    check(vw::nativeGuestArchForHost(QStringLiteral("ppc64")).isEmpty(),
          "ppc64 host has no native backend guest");
}

void testTiers() {
    check(vw::tierRank(vw::VirtualizationTier::Native)
              < vw::tierRank(vw::VirtualizationTier::HardwareAccelerated),
          "native ranks before hardware acceleration");
    check(vw::tierRank(vw::VirtualizationTier::HardwareAccelerated)
              < vw::tierRank(vw::VirtualizationTier::SoftwareEmulation),
          "hardware acceleration ranks before TCG");
    checkEqual(vw::tierName(vw::VirtualizationTier::Native), QStringLiteral("native"), "tier name native");
    checkEqual(vw::tierName(vw::VirtualizationTier::HardwareAccelerated), QStringLiteral("hardware"),
               "tier name hardware");
    checkEqual(vw::tierName(vw::VirtualizationTier::SoftwareEmulation), QStringLiteral("emulation"),
               "tier name emulation");
}

// ---------------------------------------------------------------------------
// No cross-architecture hardware acceleration, ever
// ---------------------------------------------------------------------------

void testNoCrossArchHardwareClaims() {
    const vw::HostVirtualizationProfile profile = vw::detectHostVirtualizationProfile();
    const QString hostArch = profile.hostArch;
    const QString foreign = otherArch(hostArch);

    vw::QemuHardwareEngine hardware;
    QString reason;
    check(!hardware.supportsGuestArch(foreign, &reason),
          QStringLiteral("QEMU hardware backend must reject cross-arch guest (%1 host, %2 guest)")
              .arg(hostArch, foreign));
    check(!reason.isEmpty(), "cross-arch rejection carries a reason");

    vw::WindowsHypervisorEngine whpx;
    check(!whpx.supportsGuestArch(foreign, &reason),
          "WHPX backend must reject cross-arch guests");

    // And the matrix must say the same thing for every host platform.
    for (const vw::SupportMatrixRow& row : vw::defaultSupportMatrix()) {
        if (vw::isSameNativeIsa(row.hostArch, row.guestArch)) {
            continue;
        }
        check(row.backendId == vw::backend_id::qemuTcg(),
              QStringLiteral("cross-arch row %1/%2 -> %3 must be TCG")
                  .arg(row.hostPlatform, row.hostArch, row.guestArch));
        check(row.tier == vw::VirtualizationTier::SoftwareEmulation,
              "cross-arch row must sit in the emulation tier");
    }
}

void testWindowsBackendHonesty() {
    const vw::WhpxProbeResult probed = vw::probeWhpx();
#if defined(_WIN32)
    check(probed.hostIsWindows, "Windows build must report a Windows host");
    check(probed.hostArch == QStringLiteral("x86_64") || probed.hostArch == QStringLiteral("aarch64"),
          "Windows host arch must be one of x86_64/aarch64 for WHPX");
#else
    check(!probed.hostIsWindows, "non-Windows build must not claim a Windows host");
    check(!probed.whpxUsable, "non-Windows build must not claim usable WHPX");
    check(!probed.reason.isEmpty(), "non-Windows WHPX rejection carries a reason");

    vw::WindowsHypervisorEngine whpx;
    QString reason;
    check(!whpx.isHostSupported(&reason), "WHPX engine is not host supported outside Windows");
    check(!reason.isEmpty(), "WHPX engine explains why it is unsupported");
    checkEqual(whpx.statusCode(vw::VmConfig()), QStringLiteral("host-unsupported"),
               "WHPX status code reports the host problem first, not the guest arch");
    check(whpx.requirements().size() >= 2, "WHPX documents its requirements");
#endif

    // The engine must exist in the registry on every platform so the CLI can
    // print the full matrix, but it must never claim a false positive.
    vw::WindowsHypervisorEngine engine;
    checkEqual(engine.backendId(), vw::backend_id::windowsHypervisor(), "WHPX backend id");

    // Two distinct facts, and the contract keeps them apart:
    //   * tier()            the mechanism this backend wraps (WHPX -> native).
    //                       It does not depend on the host state.
    //   * executionTierFor()what one guest would really be run with here.
    //                       This is where honest degradation lives.
    const vw::HostVirtualizationProfile profile = vw::detectHostVirtualizationProfile();
    const QString hostArch = profile.hostArch;
    const QString foreign = otherArch(hostArch);

    // A foreign ISA is never handed to WHPX, so it is never native execution.
    check(!vw::tierIsNative(engine.executionTierFor(foreign)),
          "WHPX must not claim native execution for a cross-ISA guest");

#if defined(_WIN32)
    check(engine.tier() == vw::VirtualizationTier::Native
              || engine.tier() == vw::VirtualizationTier::SoftwareEmulation,
          "WHPX reports native or an honest TCG fallback on Windows");
    check(engine.executionTierFor(hostArch) == vw::VirtualizationTier::Native
              || engine.executionTierFor(hostArch) == vw::VirtualizationTier::SoftwareEmulation,
          "WHPX execution tier on Windows is native or an honest TCG fallback");
#else
    // Outside Windows there is no WHPX at all, so no guest — not even the host
    // ISA — may be advertised as native execution.
    check(!vw::tierIsNative(engine.executionTierFor(hostArch)),
          "WHPX must not claim native execution outside Windows");
    check(engine.executionTierFor(hostArch) == vw::VirtualizationTier::SoftwareEmulation,
          "WHPX degrades to software emulation outside Windows");
#endif
}

// ---------------------------------------------------------------------------
// Registry and selection
// ---------------------------------------------------------------------------

void testRegistryBasics() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.registerBuiltinBackends();

    check(registry.hasBackend(vw::backend_id::qemuTcg()), "TCG backend is always registered");
    check(registry.hasBackend(vw::backend_id::qemuHardware()), "hardware backend is always registered");
    check(registry.hasBackend(vw::backend_id::windowsHypervisor()),
          "Windows backend is registered on every platform (reports host-unsupported elsewhere)");

    const QVector<vw::VirtualizationEnginePtr> ordered = registry.backendsInPreferenceOrder();
    check(ordered.size() >= 3, "registry holds at least three backends");
    for (int i = 1; i < ordered.size(); ++i) {
        check(vw::tierRank(ordered.at(i - 1)->tier()) <= vw::tierRank(ordered.at(i)->tier()),
              "backends are returned in tier order");
    }
    // TCG is always registered and always sits in the emulation tier. It is not
    // necessarily the *last* candidate any more: a platform backend that
    // honestly degrades to emulation (Linux KVM without /dev/kvm, WHPX without
    // the API) shares that tier and is ordered by id. What must hold is that no
    // accelerated backend is ever ranked after an emulated one.
    check(registry.hasBackend(vw::backend_id::qemuTcg()), "TCG is always registered");
    const vw::VirtualizationEnginePtr tcg = registry.backend(vw::backend_id::qemuTcg());
    check(!tcg.isNull() && tcg->tier() == vw::VirtualizationTier::SoftwareEmulation,
          "TCG sits in the emulation tier");
    bool sawEmulation = false;
    for (const vw::VirtualizationEnginePtr& engine : ordered) {
        if (engine->tier() == vw::VirtualizationTier::SoftwareEmulation) {
            sawEmulation = true;
        } else if (sawEmulation) {
            check(false, QStringLiteral("\"%1\" is ranked after an emulated backend")
                             .arg(engine->backendId()));
        }
    }
}

void testPreferenceParsing() {
    bool ok = false;
    check(vw::parseBackendPreference(QStringLiteral("auto"), &ok) == vw::BackendPreference::Auto,
          "\"auto\" parses to Auto");
    check(vw::parseBackendPreference(QString(), &ok) == vw::BackendPreference::Auto,
          "empty preference parses to Auto");
    check(vw::parseBackendPreference(QStringLiteral("qemu"), &ok) == vw::BackendPreference::QemuOnly,
          "\"qemu\" parses to QemuOnly");
    check(vw::parseBackendPreference(QStringLiteral("qemu-tcg"), &ok) == vw::BackendPreference::Explicit,
          "\"qemu-tcg\" parses to Explicit");
    check(vw::parseBackendPreference(QStringLiteral("macos-virtualization"), &ok)
              == vw::BackendPreference::Explicit,
          "a backend id parses to Explicit");
    check(ok, "parsing reports success");

    vw::VmConfig config;
    config.backend = QStringLiteral("qemu");
    checkEqual(vw::resolveBackendPreference(QString(), config), QStringLiteral("qemu"),
               "config backend field is honoured");
    checkEqual(vw::resolveBackendPreference(QStringLiteral("auto"), config), QStringLiteral("auto"),
               "an explicit request wins over the config field");
}

void testSelectionOrder() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.registerBuiltinBackends();

    const vw::HostVirtualizationProfile profile = vw::detectHostVirtualizationProfile();

    // A cross-architecture guest can only ever land on TCG.
    vw::VmConfig crossArch;
    crossArch.architecture = otherArch(profile.hostArch);
    crossArch.guestOs = QStringLiteral("linux");
    const vw::EngineSelection crossSelection = registry.select(crossArch, vw::BackendPreference::Auto);
    if (crossSelection.ok) {
        checkEqual(crossSelection.backendId, vw::backend_id::qemuTcg(),
                   "cross-arch guest selects TCG");
        check(crossSelection.tier == vw::VirtualizationTier::SoftwareEmulation,
              "cross-arch selection sits in the emulation tier");
    } else {
        // No QEMU runtime on this machine: the failure must still be explained.
        check(!crossSelection.reason.isEmpty(), "a failed selection still explains itself");
        check(!crossSelection.considered.isEmpty(), "a failed selection still lists candidates");
    }

    // QemuOnly must never hand a config to a native backend.
    vw::VmConfig sameArch;
    sameArch.architecture = profile.hostArch;
    sameArch.guestOs = QStringLiteral("linux");
    const vw::EngineSelection qemuOnly = registry.select(sameArch, vw::BackendPreference::QemuOnly);
    if (qemuOnly.ok) {
        check(qemuOnly.tier != vw::VirtualizationTier::Native,
              "QemuOnly never selects a native backend");
    }
    for (const vw::BackendChoice& choice : qemuOnly.considered) {
        if (choice.tier == vw::VirtualizationTier::Native) {
            checkEqual(choice.statusCode, QStringLiteral("excluded-by-policy"),
                       "native backends are excluded by the QemuOnly policy");
        }
    }

    // An explicit unknown backend falls back to the normal chain instead of
    // failing outright.
    const vw::EngineSelection unknown = registry.select(sameArch, QStringLiteral("does-not-exist"));
    check(!unknown.reason.isEmpty(), "unknown backend selection explains itself");
}

void testJsonReport() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.registerBuiltinBackends();

    vw::VmConfig config;
    config.architecture = QStringLiteral("aarch64");
    const QString json = registry.toJson(config, QStringLiteral("aarch64"));
    check(!json.isEmpty(), "JSON report is not empty");

    const QJsonDocument document = QJsonDocument::fromJson(json.toUtf8());
    check(!document.isNull() && document.isObject(), "JSON report parses to an object");
    const QJsonObject root = document.object();
    check(root.contains(QStringLiteral("schema")), "JSON report has a schema");
    check(root.contains(QStringLiteral("host")), "JSON report has a host object");
    check(root.contains(QStringLiteral("backends")), "JSON report has backends");
    check(root.contains(QStringLiteral("selection")), "JSON report has a selection");
    check(root.contains(QStringLiteral("support_matrix")), "JSON report has the support matrix");

    const QJsonObject host = root.value(QStringLiteral("host")).toObject();
    check(!host.value(QStringLiteral("arch")).toString().isEmpty(), "host arch is reported");
    const QJsonArray backends = root.value(QStringLiteral("backends")).toArray();
    check(!backends.isEmpty(), "at least one backend is reported");
    for (const QJsonValue& value : backends) {
        const QJsonObject backend = value.toObject();
        check(backend.contains(QStringLiteral("id")) && backend.contains(QStringLiteral("tier")),
              "every backend reports id and tier");
        check(backend.contains(QStringLiteral("host_supported"))
                  && backend.contains(QStringLiteral("guest_arch_supported")),
              "every backend reports host and guest support flags");
    }

    const QJsonArray matrix = root.value(QStringLiteral("support_matrix")).toArray();
    check(!matrix.isEmpty(), "support matrix is populated");
    for (const QJsonValue& value : matrix) {
        const QJsonObject row = value.toObject();
        check(row.contains(QStringLiteral("verified")), "every matrix row states its verification status");
        if (row.value(QStringLiteral("status")).toString() == QStringLiteral("emulation")) {
            check(row.value(QStringLiteral("backend")).toString() == vw::backend_id::qemuTcg(),
                  "emulation rows always use TCG");
        }
    }
}

void testQemuEngines() {
    vw::QemuTcgEngine tcg;
    checkEqual(tcg.backendId(), vw::backend_id::qemuTcg(), "TCG backend id");
    check(tcg.tier() == vw::VirtualizationTier::SoftwareEmulation, "TCG is emulation tier");
    check(tcg.supportsGuestArch(QStringLiteral("aarch64")) || !tcg.isHostSupported(),
          "TCG either supports the arch or the runtime is genuinely missing");
    check(tcg.supportsGuestArch(QStringLiteral("x86_64")) || !tcg.isHostSupported(),
          "TCG either supports x86_64 or the runtime is genuinely missing");
    check(!tcg.requirements().isEmpty(), "TCG documents its requirements");

    vw::QemuHardwareEngine hardware;
    checkEqual(hardware.backendId(), vw::backend_id::qemuHardware(), "hardware backend id");
    check(hardware.tier() == vw::VirtualizationTier::HardwareAccelerated, "hardware tier");
    check(!hardware.requirements().isEmpty(), "hardware backend documents its requirements");

    // System Software configs must never be handed to QEMU backends.
    vw::VmConfig systemSoftware;
    systemSoftware.backend = vw::backend_id::systemSoftware();
    check(vw::hasEngineErrors(tcg.validate(systemSoftware)), "TCG rejects System Software configs");
    check(vw::hasEngineErrors(hardware.validate(systemSoftware)),
          "hardware backend rejects System Software configs");
}

void testSharedStatusVocabulary() {
    // Every status code is a stable, distinct string; nothing may invent a new
    // spelling, or scripts matching on them break silently.
    const QStringList codes = {
        vw::backend_status::available(),
        vw::backend_status::hostUnsupported(),
        vw::backend_status::guestArchUnsupported(),
        vw::backend_status::acceleratorUnavailable(),
        vw::backend_status::runtimeMissing(),
        vw::backend_status::configInvalid(),
        vw::backend_status::backendMismatch(),
        vw::backend_status::excludedByPolicy(),
        vw::backend_status::noBackend(),
        vw::backend_status::notRegistered(),
    };
    for (int i = 0; i < codes.size(); ++i) {
        for (int j = i + 1; j < codes.size(); ++j) {
            check(codes.at(i) != codes.at(j),
                  QStringLiteral("status codes \"%1\" and \"%2\" are distinct").arg(codes.at(i), codes.at(j)));
        }
    }
    for (const QString& code : codes) {
        check(!code.isEmpty() && !code.contains(QLatin1Char(' ')),
              QStringLiteral("status code \"%1\" is a stable token").arg(code));
    }
    check(vw::statusIsUsable(vw::backend_status::available()), "available is usable");
    check(!vw::statusIsUsable(vw::backend_status::hostUnsupported()),
          "host-unsupported is not usable");
    check(!vw::statusIsUsable(vw::backend_status::guestArchUnsupported()),
          "guest-arch-unsupported is not usable");
    check(!vw::statusIsUsable(vw::backend_status::excludedByPolicy()),
          "excluded-by-policy is not usable");

    check(vw::tierIsNative(vw::VirtualizationTier::Native), "native tier is native");
    check(!vw::tierIsNative(vw::VirtualizationTier::HardwareAccelerated),
          "hardware-accelerated tier is not native");
    check(!vw::tierIsNative(vw::VirtualizationTier::SoftwareEmulation),
          "emulation tier is not native");

    check(vw::backendReportSchema() == QStringLiteral("virtualworld.virtualization/3"),
          "the report schema is versioned");

    // A backend may emit a backend-specific code; statusClass() always folds it
    // back into the canonical vocabulary, and never invents "available".
    checkEqual(vw::statusClass(vw::backend_status::available()), vw::backend_status::available(),
               "available classifies as available");
    checkEqual(vw::statusClass(vw::backend_status::degraded()), vw::backend_status::available(),
               "degraded still classifies as available (it can run the guest, only slower)");
    checkEqual(vw::statusClass(vw::backend_status::whpxDegraded()), vw::backend_status::available(),
               "whpx-degraded still classifies as available");
    checkEqual(vw::statusClass(vw::backend_status::whpxApiMissing()),
               vw::backend_status::acceleratorUnavailable(), "whpx-api-missing is an accelerator problem");
    checkEqual(vw::statusClass(vw::backend_status::whpxHypervisorNotPresent()),
               vw::backend_status::acceleratorUnavailable(), "whpx-hypervisor-not-present is an accelerator problem");
    checkEqual(vw::statusClass(vw::backend_status::runtimeNotRunnable()),
               vw::backend_status::runtimeMissing(), "runtime-not-runnable is a runtime problem");
    checkEqual(vw::statusClass(vw::backend_status::hostUnsupported()),
               vw::backend_status::hostUnsupported(), "host-unsupported classifies as itself");
    // An unknown code must never be trusted to mean "this can run the guest".
    checkEqual(vw::statusClass(QStringLiteral("memory-too-small")), vw::backend_status::configInvalid(),
               "an unknown backend code classifies as config-invalid, never available");
    check(vw::statusClass(QStringLiteral("brand-new-code")) != vw::backend_status::available(),
          "no unknown code can ever classify as available");
    checkEqual(vw::statusClass(QString()), vw::backend_status::notRegistered(),
               "an empty status classifies as not-registered");
}

void testSelectionPutsTcgLast() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.registerBuiltinBackends();

    const QVector<vw::VirtualizationEnginePtr> ordered = registry.backendsInPreferenceOrder();
    check(!ordered.isEmpty(), "the catalogue is not empty");
    for (int i = 1; i < ordered.size(); ++i) {
        check(vw::tierRank(ordered.at(i - 1)->tier()) <= vw::tierRank(ordered.at(i)->tier()),
              "candidates are ordered native -> hardware -> emulation");
    }
    // qemu-tcg is pinned last, even though several platform backends also sit in
    // the emulation tier and would otherwise sort ahead of or behind it by id.
    checkEqual(ordered.last()->backendId(), vw::backend_id::qemuTcg(),
               "qemu-tcg is always the very last candidate");
    check(ordered.last()->tier() == vw::VirtualizationTier::SoftwareEmulation,
          "the last candidate sits in the emulation tier");
}

void testBackendFactoryContract() {
    // Every factory is honest about whether its backend is in this build: the
    // "compiled in" flag, the availability probe and the returned engine must
    // never disagree in a way that would make the registry lie.
    const bool macCompiled =
#if defined(VIRTUALWORLD_MACOS_BACKEND)
        true;
#else
        false;
#endif
    check(vw::macOSVirtualizationEngineAvailable() == macCompiled
              || !vw::makeMacOSVirtualizationEngine().isNull(),
          "the macOS factory agrees with the build configuration");
    if (!macCompiled) {
        check(!vw::macOSVirtualizationEngineAvailable(), "macOS factory unavailable when not compiled in");
        check(vw::makeMacOSVirtualizationEngine().isNull(), "macOS factory returns nothing when not compiled in");
    }

    check(vw::harmonyBackendCompiledIn() ==
#if defined(VIRTUALWORLD_HAVE_HARMONY_BACKEND)
              true,
#else
              false,
#endif
          "harmonyBackendCompiledIn() matches the build configuration");
    check(vw::linuxKvmBackendCompiledIn() ==
#if defined(VIRTUALWORLD_HAVE_LINUX_KVM_BACKEND)
              true,
#else
              false,
#endif
          "linuxKvmBackendCompiledIn() matches the build configuration");

    // A factory that is compiled in but not usable here must return "no engine"
    // or an engine that reports itself honestly — never a silent fake.
    if (vw::harmonyBackendCompiledIn()) {
        const vw::VirtualizationEnginePtr harmony = vw::makeHarmonyVirtualizationEngine();
        check(!harmony.isNull(), "the Harmony factory builds an engine when compiled in");
        if (!harmony.isNull()) {
            check(harmony->tier() != vw::VirtualizationTier::Native,
                  "the Harmony backend never reports the native tier");
        }
    }
    if (vw::linuxKvmBackendCompiledIn()) {
        const vw::VirtualizationEnginePtr kvm = vw::makeLinuxKvmEngine();
        check(!kvm.isNull(), "the Linux KVM factory builds an engine when compiled in");
        if (!kvm.isNull()) {
            check(kvm->tier() != vw::VirtualizationTier::Native,
                  "the Linux KVM backend never reports the native tier");
        }
    }
}

void testExecutionTierHonesty() {
    // `tier()` is the mechanism a backend wraps; `executionTierFor(arch)` is what
    // one guest would really be run with on this host. The second must never be
    // better than the first, and must fall to emulation for anything the backend
    // cannot run — that is the seam Windows ARM64 (and KVM without /dev/kvm)
    // degrades through.
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.registerBuiltinBackends();

    const vw::HostVirtualizationProfile profile = vw::detectHostVirtualizationProfile();
    const QString hostArch = profile.hostArch;
    const QString foreign = otherArch(hostArch);

    for (const vw::VirtualizationEnginePtr& engine : registry.backends()) {
        const QString id = engine->backendId();
        const vw::VirtualizationTier mechanism = engine->tier();
        const vw::VirtualizationTier sameIsa = engine->executionTierFor(hostArch);
        const vw::VirtualizationTier crossIsa = engine->executionTierFor(foreign);

        check(static_cast<int>(sameIsa) >= static_cast<int>(mechanism),
              QStringLiteral("\"%1\" never executes better than its mechanism tier").arg(id));
        check(static_cast<int>(crossIsa) >= static_cast<int>(mechanism),
              QStringLiteral("\"%1\" never executes a cross-ISA guest better than its mechanism tier")
                  .arg(id));

        // A backend that cannot run a guest does not accelerate it: the registry
        // hands that guest to TCG, so the honest answer is emulation.
        if (!engine->isHostSupported()) {
            check(sameIsa == vw::VirtualizationTier::SoftwareEmulation,
                  QStringLiteral("\"%1\" reports emulation on an unsupported host").arg(id));
        }
        if (!engine->supportsGuestArch(foreign)) {
            check(crossIsa == vw::VirtualizationTier::SoftwareEmulation,
                  QStringLiteral("\"%1\" reports emulation for a guest arch it rejects").arg(id));
        }

        // Cross-architecture hardware acceleration does not exist, so no backend
        // may promise native execution for a foreign ISA.
        check(!vw::tierIsNative(crossIsa),
              QStringLiteral("\"%1\" never claims native execution across ISAs").arg(id));
    }

    // Sanity: the TCG backend is emulation in both directions, whatever the host.
    const vw::VirtualizationEnginePtr tcg = registry.backend(vw::backend_id::qemuTcg());
    if (!tcg.isNull()) {
        check(tcg->executionTierFor(hostArch) == vw::VirtualizationTier::SoftwareEmulation,
              "TCG executes in the emulation tier for the host ISA");
        check(tcg->executionTierFor(foreign) == vw::VirtualizationTier::SoftwareEmulation,
              "TCG executes in the emulation tier for any ISA");
    }
}

void testQemuCompatBackendsAreNeverNative() {
    // HarmonyOS/OpenHarmony exposes no public hypervisor API, and
    // LinuxKvmEngine documents itself as a QEMU compatibility backend that is
    // "never native". A QEMU process accelerated by KVM is hardware tier; only
    // a backend that drives the host hypervisor API directly is native.
    for (const vw::SupportMatrixRow& row : vw::defaultSupportMatrix()) {
        const bool qemuCompat = row.hostPlatform == QStringLiteral("harmonyos")
            || row.hostPlatform == QStringLiteral("linux");
        if (!qemuCompat) {
            continue;
        }
        check(row.tier != vw::VirtualizationTier::Native,
              QStringLiteral("%1 row %2/%3 is never native")
                  .arg(row.hostPlatform, row.hostArch, row.guestArch));
        check(row.status != QStringLiteral("native"),
              QStringLiteral("%1 row %2/%3 never claims the native status")
                  .arg(row.hostPlatform, row.hostArch, row.guestArch));
        if (!vw::isSameNativeIsa(row.hostArch, row.guestArch)) {
            check(row.tier == vw::VirtualizationTier::SoftwareEmulation,
                  QStringLiteral("cross-arch %1 row %2/%3 is emulation")
                      .arg(row.hostPlatform, row.hostArch, row.guestArch));
        }
    }
    check(!vw::tierIsNative(vw::VirtualizationTier::HardwareAccelerated),
          "QEMU+KVM is hardware acceleration, not native execution");
}

void testSupportMatrixCoverage() {
    const QVector<vw::SupportMatrixRow> rows = vw::defaultSupportMatrix();
    check(!rows.isEmpty(), "support matrix is not empty");

    bool hasWindowsArm64 = false;
    bool hasLinuxRiscv64 = false;
    bool hasHarmony = false;
    bool hasMacOSAppleSilicon = false;
    for (const vw::SupportMatrixRow& row : rows) {
        if (row.hostPlatform == QStringLiteral("windows") && row.hostArch == QStringLiteral("aarch64")
            && row.guestArch == QStringLiteral("aarch64")) {
            hasWindowsArm64 = true;
        }
        if (row.hostPlatform == QStringLiteral("linux") && row.hostArch == QStringLiteral("riscv64")
            && row.guestArch == QStringLiteral("riscv64")) {
            hasLinuxRiscv64 = true;
        }
        if (row.hostPlatform == QStringLiteral("harmonyos")) {
            hasHarmony = true;
        }
        if (row.hostPlatform == QStringLiteral("macos") && row.hostArch == QStringLiteral("aarch64")
            && row.guestArch == QStringLiteral("aarch64")) {
            hasMacOSAppleSilicon = true;
        }
    }
    check(hasWindowsArm64, "matrix covers Windows ARM64");
    check(hasLinuxRiscv64, "matrix covers Linux RISC-V64");
    check(hasHarmony, "matrix covers HarmonyOS/OpenHarmony");
    check(hasMacOSAppleSilicon, "matrix covers Apple Silicon");
}

} // namespace

int main(int argc, char* argv[]) {
    // RuntimeManager resolves paths through QCoreApplication; instantiating one
    // keeps the runtime probe quiet.
    QCoreApplication app(argc, argv);

    testArchitectureNormalization();
    testSameIsaRules();
    testTiers();
    testNoCrossArchHardwareClaims();
    testWindowsBackendHonesty();
    testRegistryBasics();
    testPreferenceParsing();
    testSelectionOrder();
    testJsonReport();
    testQemuEngines();
    testSharedStatusVocabulary();
    testSelectionPutsTcgLast();
    testBackendFactoryContract();
    testExecutionTierHonesty();
    testQemuCompatBackendsAreNeverNative();
    testSupportMatrixCoverage();

    qInfo().noquote() << QStringLiteral("VirtualizationEngineTests: %1 checks, %2 failures")
                             .arg(checks)
                             .arg(failures);
    return failures == 0 ? 0 : 1;
}
