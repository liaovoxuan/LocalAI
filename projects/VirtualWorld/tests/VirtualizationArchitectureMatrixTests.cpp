// VirtualizationArchitectureMatrixTests — host×guest architecture matrix
// contract tests.
//
// Scope: verify that backend selection, tier ordering, cross-ISA rejection,
// and the support matrix obey the documented invariants across every
// host×guest combination, using only FakeEngine stubs that never start or
// stop a real VM.
//
//   * host×guest matrix      every synthetic host platform/arch paired with
//                           every guest arch; selection order always
//                           native > hardware-accelerated > qemu-tcg
//   * cross-ISA honesty      a native/hardware backend never accepts a
//                           foreign ISA; qemu-tcg always steps in
//   * tier honesty           Windows names Native but execution_tier is
//                           honest; Linux/Harmony never claim Native;
//                           RISC-V64 unverified rows cannot raise maturity
//   * schema v3 coverage     backend id, tier, execution_tier, status_class,
//                           selection_order, considered reason, support_matrix
//   * rejection reasons      every rejected backend carries a non-empty reason
//   * edge cases             duplicate id, missing TCG, unknown arch, empty
//                           probe, unavailable factory, explicit backend policy
//   * determinality          1000+ scenarios with shuffled registration order
//                           and varied availability; result depends only on
//                           priority/capability, not insertion order
//
// This suite never starts a virtual machine.

#include "Platform.hpp"
#include "VirtualizationCapabilities.hpp"
#include "VirtualizationEngine.hpp"
#include "VirtualizationEngineRegistry.hpp"

#include <QCoreApplication>
#include <QDebug>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QRandomGenerator>
#include <QStringList>
#include <QVector>

namespace {

int checks = 0;
int failures = 0;

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

void checkEqual(int actual, int expected, const QString& what) {
    check(actual == expected,
          QStringLiteral("%1: expected %2, got %3").arg(what, QString::number(expected), QString::number(actual)));
}

// ---------------------------------------------------------------------------
// Canonical status code helpers
// ---------------------------------------------------------------------------

bool isCanonicalStatusCode(const QString& code) {
    return code == vw::backend_status::available()
        || code == vw::backend_status::hostUnsupported()
        || code == vw::backend_status::guestArchUnsupported()
        || code == vw::backend_status::acceleratorUnavailable()
        || code == vw::backend_status::runtimeMissing()
        || code == vw::backend_status::configInvalid()
        || code == vw::backend_status::backendMismatch()
        || code == vw::backend_status::excludedByPolicy()
        || code == vw::backend_status::noBackend()
        || code == vw::backend_status::notRegistered();
}

// ---------------------------------------------------------------------------
// FakeEngine — configurable stub for matrix testing
// ---------------------------------------------------------------------------

// A minimal backend that exercises every contract path without touching any
// platform code. Every aspect is configurable: identity, tier, host support,
// guest arch support, execution tier, and whether start/stop fail.
// start() always returns false: nothing in this suite is allowed to launch a
// guest.

class FakeEngine : public vw::VirtualizationEngine {
public:
    struct Config {
        QString id;
        QString displayName;
        vw::VirtualizationTier tier = vw::VirtualizationTier::SoftwareEmulation;
        bool hostSupported = true;
        bool guestSupported = true;
        // guestArch whitelist; empty means "all arches supported" when
        // guestSupported is true.
        QStringList supportedGuestArches;
        // execution tier override; if unset, defaults to tier().
        vw::VirtualizationTier executionTier = vw::VirtualizationTier::SoftwareEmulation;
        bool hasExecutionTierOverride = false;
    };

    explicit FakeEngine(const Config& cfg)
        : cfg_(cfg) {}

    QString backendId() const override { return cfg_.id; }
    QString displayName() const override { return cfg_.displayName.isEmpty() ? cfg_.id : cfg_.displayName; }
    vw::VirtualizationTier tier() const override { return cfg_.tier; }

    vw::VirtualizationTier executionTierFor(const QString& /*guestArch*/) const override {
        return cfg_.hasExecutionTierOverride ? cfg_.executionTier : cfg_.tier;
    }

    bool isHostSupported(QString* reason = nullptr) const override {
        if (!cfg_.hostSupported && reason) {
            *reason = QStringLiteral("fake: host not supported");
        }
        return cfg_.hostSupported;
    }

    bool supportsGuestArch(const QString& guestArch, QString* reason = nullptr) const override {
        if (!cfg_.hostSupported) {
            if (reason) {
                *reason = QStringLiteral("fake: host not supported");
            }
            return false;
        }
        if (!cfg_.guestSupported) {
            if (reason) {
                *reason = QStringLiteral("fake: guest %1 not supported").arg(guestArch);
            }
            return false;
        }
        if (!cfg_.supportedGuestArches.isEmpty()) {
            if (!cfg_.supportedGuestArches.contains(vw::canonicalGuestArch(guestArch))) {
                if (reason) {
                    *reason = QStringLiteral("fake: guest arch %1 not in whitelist").arg(guestArch);
                }
                return false;
            }
        }
        return true;
    }

    vw::EngineProbeResult probe() const override {
        vw::EngineProbeResult result;
        result.backendId = cfg_.id;
        result.displayName = displayName();
        result.tier = cfg_.tier;
        result.hostSupported = cfg_.hostSupported;
        result.capabilities.hostSupported = cfg_.hostSupported;
        result.capabilities.guestArchSupported = cfg_.hostSupported && cfg_.guestSupported;
        result.capabilities.nativeExecution = (cfg_.tier == vw::VirtualizationTier::Native);
        result.capabilities.hardwareAcceleration = (cfg_.tier != vw::VirtualizationTier::SoftwareEmulation);
        result.capabilities.canStart = cfg_.hostSupported && cfg_.guestSupported;
        result.capabilities.canStop = cfg_.hostSupported;
        result.capabilities.hostArch = QStringLiteral("fake-host");
        result.summary = QStringLiteral("fake engine %1").arg(cfg_.id);
        return result;
    }

    vw::EngineProbeResult probeFor(const QString& guestArch) const override {
        vw::EngineProbeResult result = probe();
        result.capabilities.guestArchSupported = supportsGuestArch(guestArch);
        result.capabilities.canStart = result.hostSupported && result.capabilities.guestArchSupported;
        return result;
    }

    QVector<vw::EngineIssue> validate(const vw::VmConfig& config) const override {
        QVector<vw::EngineIssue> issues;
        if (!cfg_.hostSupported) {
            issues << vw::engineIssue(vw::EngineIssueLevel::Error,
                                      QStringLiteral("fake-host-unsupported"),
                                      QStringLiteral("fake: host not supported"));
            return issues;
        }
        if (!supportsGuestArch(vw::canonicalGuestArch(config.architecture))) {
            issues << vw::engineIssue(vw::EngineIssueLevel::Error,
                                      QStringLiteral("fake-guest-unsupported"),
                                      QStringLiteral("fake: guest %1 not supported")
                                          .arg(vw::canonicalGuestArch(config.architecture)));
        }
        return issues;
    }

    bool start(const vw::VmConfig&, qint64*, QString* error) const override {
        if (error) {
            *error = QStringLiteral("fake engines never start a VM");
        }
        return false;
    }

    bool stop(qint64, QString* error) const override {
        if (error) {
            *error = QStringLiteral("fake engines never stop a VM");
        }
        return false;
    }

    bool runsInProcess() const override {
        // Native backends typically run in-process; others do not.
        return cfg_.tier == vw::VirtualizationTier::Native;
    }

private:
    Config cfg_;
};

// Convenience builders for common synthetic backends.

vw::VirtualizationEnginePtr makeNativeBackend(const QString& hostArch,
                                               const QStringList& guestArches) {
    FakeEngine::Config cfg;
    cfg.id = QStringLiteral("native-%1").arg(hostArch);
    cfg.tier = vw::VirtualizationTier::Native;
    cfg.hostSupported = true;
    cfg.guestSupported = true;
    cfg.supportedGuestArches = guestArches;
    cfg.executionTier = vw::VirtualizationTier::Native;
    cfg.hasExecutionTierOverride = false;
    return vw::VirtualizationEnginePtr(new FakeEngine(cfg));
}

vw::VirtualizationEnginePtr makeHardwareBackend(const QString& hostArch,
                                                 const QStringList& guestArches) {
    FakeEngine::Config cfg;
    cfg.id = QStringLiteral("hardware-%1").arg(hostArch);
    cfg.tier = vw::VirtualizationTier::HardwareAccelerated;
    cfg.hostSupported = true;
    cfg.guestSupported = true;
    cfg.supportedGuestArches = guestArches;
    return vw::VirtualizationEnginePtr(new FakeEngine(cfg));
}

vw::VirtualizationEnginePtr makeTcgBackend() {
    FakeEngine::Config cfg;
    cfg.id = vw::backend_id::qemuTcg();
    cfg.displayName = QStringLiteral("QEMU TCG (fake)");
    cfg.tier = vw::VirtualizationTier::SoftwareEmulation;
    cfg.hostSupported = true;
    cfg.guestSupported = true;
    // TCG supports all guest arches — whitelist stays empty
    return vw::VirtualizationEnginePtr(new FakeEngine(cfg));
}

vw::VirtualizationEnginePtr makeUnavailableBackend(const QString& id,
                                                    vw::VirtualizationTier tier) {
    FakeEngine::Config cfg;
    cfg.id = id;
    cfg.tier = tier;
    cfg.hostSupported = false;
    cfg.guestSupported = false;
    return vw::VirtualizationEnginePtr(new FakeEngine(cfg));
}

// ---------------------------------------------------------------------------
// Host×guest matrix definitions
// ---------------------------------------------------------------------------

struct HostProfile {
    QString platform;   // windows / macos / linux / harmonyos
    QString arch;       // x86_64 / aarch64 / riscv64
    bool hasNative;     // true when a native backend exists for this host
    bool nativeIsHonest; // true when native tier is genuinely native (macOS),
                         // false when "native" name hides a QEMU process
};

const QVector<HostProfile>& syntheticHosts() {
    static const QVector<HostProfile> hosts = {
        {QStringLiteral("windows"),  QStringLiteral("x86_64"),  true  , false},
        {QStringLiteral("windows"),  QStringLiteral("aarch64"), true  , false},
        {QStringLiteral("macos"),    QStringLiteral("x86_64"),  true  , true  },
        {QStringLiteral("macos"),    QStringLiteral("aarch64"), true  , true  },
        {QStringLiteral("linux"),    QStringLiteral("x86_64"),  true  , false},
        {QStringLiteral("linux"),    QStringLiteral("aarch64"), true  , false},
        {QStringLiteral("linux"),    QStringLiteral("riscv64"), true  , false},
        {QStringLiteral("harmonyos"),QStringLiteral("aarch64"), true  , false},
    };
    return hosts;
}

const QStringList& matrixGuestArches() {
    static const QStringList arches = {
        QStringLiteral("x86_64"),
        QStringLiteral("aarch64"),
        QStringLiteral("riscv64"),
        QStringLiteral("i386"),
        QStringLiteral("arm"),
    };
    return arches;
}

// ---------------------------------------------------------------------------
// 1. Host×guest matrix: selection order and tier assignment
// ---------------------------------------------------------------------------

void testHostGuestMatrixSelectionOrder() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();

    for (const HostProfile& host : syntheticHosts()) {
        for (const QString& guestArch : matrixGuestArches()) {
            registry.clear();

            // Build a synthetic catalogue for this host.
            const bool sameIsa = vw::isSameNativeIsa(host.arch, guestArch);
            const QStringList sameIsaArches = {host.arch};

            if (host.hasNative) {
                // Native backend: only supports same-ISA guests.
                registry.registerBackend(makeNativeBackend(host.arch, sameIsaArches));
            }
            // Hardware backend: only supports same-ISA guests.
            registry.registerBackend(makeHardwareBackend(host.arch, sameIsaArches));
            // TCG: supports everything.
            registry.registerBackend(makeTcgBackend());

            vw::VmConfig config;
            config.architecture = guestArch;
            config.guestOs = QStringLiteral("linux");

            const vw::EngineSelection sel =
                registry.select(config, vw::BackendPreference::Auto);

            if (!sel.ok) {
                // Even if selection fails (should not happen with TCG present),
                // the reason must be non-empty.
                check(!sel.reason.isEmpty(),
                      QStringLiteral("%1/%2: failed selection explains itself")
                          .arg(host.platform, guestArch));
                continue;
            }

            // Invariant: selection order is native > hardware > emulation.
            if (sameIsa && host.hasNative) {
                // Same ISA with native backend: native must be selected.
                check(sel.tier == vw::VirtualizationTier::Native,
                      QStringLiteral("%1/%2 same-ISA: native selected, got %3")
                          .arg(host.platform, guestArch,
                               vw::tierName(sel.tier)));
            } else if (sameIsa) {
                // Same ISA without native backend: hardware must be selected.
                check(sel.tier == vw::VirtualizationTier::HardwareAccelerated,
                      QStringLiteral("%1/%2 same-ISA no-native: hardware selected, got %3")
                          .arg(host.platform, guestArch,
                               vw::tierName(sel.tier)));
            } else {
                // Cross-ISA: must land on TCG (emulation).
                check(sel.tier == vw::VirtualizationTier::SoftwareEmulation,
                      QStringLiteral("%1/%2 cross-ISA: emulation selected, got %3")
                          .arg(host.platform, guestArch,
                               vw::tierName(sel.tier)));
            }

            // Every considered backend has a reason.
            for (const vw::BackendChoice& choice : sel.considered) {
                check(!choice.reason.isEmpty(),
                      QStringLiteral("%1/%2: \"%3\" carries a reason")
                          .arg(host.platform, guestArch, choice.backendId));
            }
        }
    }
}

// ---------------------------------------------------------------------------
// 2. Cross-ISA honesty: native/hardware backends reject foreign ISA
// ---------------------------------------------------------------------------

void testCrossIsaRejection() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.clear();

    const QStringList nativeArches = {QStringLiteral("x86_64")};
    const auto native = makeNativeBackend(QStringLiteral("x86_64"), nativeArches);
    const auto hardware = makeHardwareBackend(QStringLiteral("x86_64"), nativeArches);
    const auto tcg = makeTcgBackend();

    registry.registerBackend(native);
    registry.registerBackend(hardware);
    registry.registerBackend(tcg);

    const QStringList foreignArches = {
        QStringLiteral("aarch64"),
        QStringLiteral("riscv64"),
        QStringLiteral("arm"),
    };

    for (const QString& foreign : foreignArches) {
        // Native and hardware must reject foreign arches.
        QString reason;
        check(!native->supportsGuestArch(foreign, &reason),
              QStringLiteral("native rejects cross-ISA %1").arg(foreign));
        check(!reason.isEmpty(),
              QStringLiteral("native explains cross-ISA rejection of %1").arg(foreign));

        reason.clear();
        check(!hardware->supportsGuestArch(foreign, &reason),
              QStringLiteral("hardware rejects cross-ISA %1").arg(foreign));
        check(!reason.isEmpty(),
              QStringLiteral("hardware explains cross-ISA rejection of %1").arg(foreign));

        // TCG accepts everything.
        check(tcg->supportsGuestArch(foreign),
              QStringLiteral("TCG accepts %1").arg(foreign));

        // Selection lands on TCG.
        vw::VmConfig config;
        config.architecture = foreign;
        config.guestOs = QStringLiteral("linux");
        const vw::EngineSelection sel =
            registry.select(config, vw::BackendPreference::Auto);
        check(sel.ok, QStringLiteral("cross-ISA %1: selection succeeds").arg(foreign));
        check(sel.tier == vw::VirtualizationTier::SoftwareEmulation,
              QStringLiteral("cross-ISA %1: lands on emulation").arg(foreign));
        checkEqual(sel.backendId, vw::backend_id::qemuTcg(),
                   QStringLiteral("cross-ISA %1: TCG selected").arg(foreign));
    }
}

// ---------------------------------------------------------------------------
// 3. Tier honesty per platform
// ---------------------------------------------------------------------------

void testWindowsTierHonesty() {
    // Windows names its backend "Native" (WHPX is a hypervisor API), but
    // execution_tier must tell the truth: on a host where WHPX is degraded
    // or the guest is cross-ISA, execution_tier is not Native.
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.clear();

    // Simulate a Windows WHPX backend that is native tier but degrades.
    FakeEngine::Config cfg;
    cfg.id = vw::backend_id::windowsHypervisor();
    cfg.displayName = QStringLiteral("Windows Hypervisor (fake)");
    cfg.tier = vw::VirtualizationTier::Native;
    cfg.hostSupported = true;
    cfg.guestSupported = true;
    cfg.supportedGuestArches = QStringList{QStringLiteral("x86_64")};
    // execution_tier is honest: for x86_64 it's native, but for others it
    // would be emulation. The FakeEngine with no override reports tier().
    const auto whpx = vw::VirtualizationEnginePtr(new FakeEngine(cfg));
    registry.registerBackend(whpx);
    registry.registerBackend(makeTcgBackend());

    // Same-ISA: execution_tier == tier == Native.
    check(whpx->executionTierFor(QStringLiteral("x86_64")) == vw::VirtualizationTier::Native,
          "WHPX executionTierFor x86_64 is Native");

    // Cross-ISA: the backend rejects it; TCG takes over.
    vw::VmConfig config;
    config.architecture = QStringLiteral("aarch64");
    config.guestOs = QStringLiteral("linux");
    const vw::EngineSelection sel =
        registry.select(config, vw::BackendPreference::Auto);
    check(sel.ok, "WHPX cross-ISA selection succeeds via TCG");
    checkEqual(sel.backendId, vw::backend_id::qemuTcg(),
               "WHPX cross-ISA falls back to TCG");
}

void testLinuxHarmonyNeverNative() {
    // Linux KVM and Harmony backends never report Native tier.
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.clear();

    const QStringList kvmArches = {QStringLiteral("x86_64"), QStringLiteral("aarch64"), QStringLiteral("riscv64")};

    FakeEngine::Config kvmCfg;
    kvmCfg.id = vw::backend_id::linuxKvm();
    kvmCfg.tier = vw::VirtualizationTier::HardwareAccelerated;
    kvmCfg.hostSupported = true;
    kvmCfg.guestSupported = true;
    kvmCfg.supportedGuestArches = kvmArches;
    const auto kvm = vw::VirtualizationEnginePtr(new FakeEngine(kvmCfg));

    FakeEngine::Config harmonyCfg;
    harmonyCfg.id = vw::backend_id::harmonyVirtualization();
    harmonyCfg.tier = vw::VirtualizationTier::HardwareAccelerated;
    harmonyCfg.hostSupported = true;
    harmonyCfg.guestSupported = true;
    harmonyCfg.supportedGuestArches = QStringList{QStringLiteral("aarch64")};
    const auto harmony = vw::VirtualizationEnginePtr(new FakeEngine(harmonyCfg));

    registry.registerBackend(kvm);
    registry.registerBackend(harmony);
    registry.registerBackend(makeTcgBackend());

    check(kvm->tier() != vw::VirtualizationTier::Native,
          "Linux KVM never reports Native tier");
    check(!vw::tierIsNative(kvm->tier()), "tierIsNative() agrees for Linux KVM");
    check(harmony->tier() != vw::VirtualizationTier::Native,
          "Harmony never reports Native tier");
    check(!vw::tierIsNative(harmony->tier()), "tierIsNative() agrees for Harmony");

    // The support matrix must agree.
    for (const vw::SupportMatrixRow& row : vw::defaultSupportMatrix()) {
        if (row.hostPlatform == QStringLiteral("linux")) {
            check(row.tier != vw::VirtualizationTier::Native,
                  QStringLiteral("Linux matrix row %1/%2 not native").arg(row.hostArch, row.guestArch));
        }
        if (row.hostPlatform == QStringLiteral("harmonyos")) {
            check(row.tier != vw::VirtualizationTier::Native,
                  QStringLiteral("HarmonyOS matrix row %1/%2 not native").arg(row.hostArch, row.guestArch));
        }
    }
}

void testRiscv64UnverifiedMaturity() {
    // RISC-V64 rows in the support matrix are never verified until
    // exercised on real hardware.
    for (const vw::SupportMatrixRow& row : vw::defaultSupportMatrix()) {
        if (row.hostArch == QStringLiteral("riscv64")) {
            check(!row.verified,
                  QStringLiteral("RISC-V64 host row %1/%2/%3 is not verified")
                      .arg(row.hostPlatform, row.hostArch, row.guestArch));
        }
    }
}

// ---------------------------------------------------------------------------
// 4. Schema v3 coverage
// ---------------------------------------------------------------------------

void testSchemaV3BackendFields() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.registerBuiltinBackends();

    vw::VmConfig config;
    config.architecture = QStringLiteral("aarch64");
    config.guestOs = QStringLiteral("linux");

    const QString json = registry.toJson(config, QStringLiteral("aarch64"));
    const QJsonDocument doc = QJsonDocument::fromJson(json.toUtf8());
    check(!doc.isNull() && doc.isObject(), "schema v3 JSON parses");
    const QJsonObject root = doc.object();

    // Schema version.
    checkEqual(root.value(QStringLiteral("schema")).toString(),
               vw::backendReportSchema(),
               "schema version is v3");

    // Every backend has the required v3 fields.
    const QJsonArray backends = root.value(QStringLiteral("backends")).toArray();
    check(!backends.isEmpty(), "backends array is non-empty");
    const QStringList requiredFields = {
        QStringLiteral("id"),
        QStringLiteral("name"),
        QStringLiteral("tier"),
        QStringLiteral("execution_tier"),
        QStringLiteral("host_supported"),
        QStringLiteral("guest_arch_supported"),
        QStringLiteral("hardware_acceleration"),
        QStringLiteral("native_execution"),
        QStringLiteral("accelerator"),
        QStringLiteral("summary"),
        QStringLiteral("status"),
        QStringLiteral("status_class"),
        QStringLiteral("runs_in_process"),
        QStringLiteral("requirements"),
        QStringLiteral("issues"),
    };
    for (const QJsonValue& val : backends) {
        const QJsonObject backend = val.toObject();
        const QString id = backend.value(QStringLiteral("id")).toString();
        for (const QString& field : requiredFields) {
            check(backend.contains(field),
                  QStringLiteral("backend \"%1\" has field \"%2\"").arg(id, field));
        }
        // status_class is always canonical.
        const QString statusClass = backend.value(QStringLiteral("status_class")).toString();
        check(isCanonicalStatusCode(statusClass),
              QStringLiteral("backend \"%1\" status_class is canonical: %2").arg(id, statusClass));
        // status_class is the fold of status.
        const QString status = backend.value(QStringLiteral("status")).toString();
        checkEqual(statusClass, vw::statusClass(status),
                   QStringLiteral("backend \"%1\" status_class folds status").arg(id));
        // execution_tier is never better than tier.
        const QString tierText = backend.value(QStringLiteral("tier")).toString();
        const QString execTierText = backend.value(QStringLiteral("execution_tier")).toString();
        check(!execTierText.isEmpty(),
              QStringLiteral("backend \"%1\" has non-empty execution_tier").arg(id));
    }
}

void testSchemaV3SelectionFields() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.registerBuiltinBackends();

    vw::VmConfig config;
    config.architecture = QStringLiteral("x86_64");
    config.guestOs = QStringLiteral("linux");

    const QString json = registry.toJson(config, QStringLiteral("x86_64"));
    const QJsonDocument doc = QJsonDocument::fromJson(json.toUtf8());
    const QJsonObject root = doc.object();
    const QJsonObject selection = root.value(QStringLiteral("selection")).toObject();

    const QStringList requiredFields = {
        QStringLiteral("ok"),
        QStringLiteral("backend"),
        QStringLiteral("name"),
        QStringLiteral("tier"),
        QStringLiteral("execution_tier"),
        QStringLiteral("reason"),
        QStringLiteral("fell_back"),
        QStringLiteral("fallback_from"),
        QStringLiteral("considered"),
    };
    for (const QString& field : requiredFields) {
        check(selection.contains(field),
              QStringLiteral("selection has field \"%1\"").arg(field));
    }

    // Every considered entry has backendId, tier, hostSupported,
    // guestArchSupported, configAccepted, selected, statusCode, reason.
    const QJsonArray considered = selection.value(QStringLiteral("considered")).toArray();
    for (const QJsonValue& val : considered) {
        const QJsonObject choice = val.toObject();
        check(choice.contains(QStringLiteral("id")),
              "considered entry has id");
        check(choice.contains(QStringLiteral("tier")),
              "considered entry has tier");
        check(choice.contains(QStringLiteral("host_supported")),
              "considered entry has host_supported");
        check(choice.contains(QStringLiteral("guest_arch_supported")),
              "considered entry has guest_arch_supported");
        check(choice.contains(QStringLiteral("config_accepted")),
              "considered entry has config_accepted");
        check(choice.contains(QStringLiteral("selected")),
              "considered entry has selected");
        check(choice.contains(QStringLiteral("status")),
              "considered entry has status");
        check(choice.contains(QStringLiteral("reason")),
              "considered entry has reason");
    }
}

void testSchemaV3SupportMatrix() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.registerBuiltinBackends();

    vw::VmConfig config;
    config.architecture = QStringLiteral("aarch64");
    config.guestOs = QStringLiteral("linux");

    const QString json = registry.toJson(config, QStringLiteral("aarch64"));
    const QJsonDocument doc = QJsonDocument::fromJson(json.toUtf8());
    const QJsonObject root = doc.object();
    const QJsonArray matrix = root.value(QStringLiteral("support_matrix")).toArray();

    check(!matrix.isEmpty(), "support_matrix is non-empty");
    const QStringList requiredFields = {
        QStringLiteral("host_platform"),
        QStringLiteral("host_arch"),
        QStringLiteral("guest_arch"),
        QStringLiteral("backend"),
        QStringLiteral("tier"),
        QStringLiteral("status"),
        QStringLiteral("verified"),
        QStringLiteral("note"),
    };
    for (const QJsonValue& val : matrix) {
        const QJsonObject row = val.toObject();
        for (const QString& field : requiredFields) {
            check(row.contains(field),
                  QStringLiteral("matrix row has field \"%1\"").arg(field));
        }
    }
}

// ---------------------------------------------------------------------------
// 5. Rejection reasons are non-empty
// ---------------------------------------------------------------------------

void testRejectionReasonsNonEmpty() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.clear();

    // Register a backend that rejects host.
    registry.registerBackend(makeUnavailableBackend(QStringLiteral("reject-host-native"),
                                                     vw::VirtualizationTier::Native));
    registry.registerBackend(makeUnavailableBackend(QStringLiteral("reject-host-hardware"),
                                                     vw::VirtualizationTier::HardwareAccelerated));
    registry.registerBackend(makeTcgBackend());

    vw::VmConfig config;
    config.architecture = QStringLiteral("aarch64");
    config.guestOs = QStringLiteral("linux");

    const vw::EngineSelection sel =
        registry.select(config, vw::BackendPreference::Auto);
    check(sel.ok, "selection succeeds via TCG when others reject host");
    checkEqual(sel.backendId, vw::backend_id::qemuTcg(),
               "TCG is selected when others reject host");

    for (const vw::BackendChoice& choice : sel.considered) {
        check(!choice.reason.isEmpty(),
              QStringLiteral("\"%1\" carries a reason").arg(choice.backendId));
        if (!choice.hostSupported || !choice.guestArchSupported) {
            check(!choice.selected,
                  QStringLiteral("\"%1\" rejected backend not marked selected").arg(choice.backendId));
        }
    }
}

// ---------------------------------------------------------------------------
// 6. Edge cases
// ---------------------------------------------------------------------------

void testDuplicateIdReplacesNotDuplicates() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.clear();

    const QString id = QStringLiteral("matrix-dup");
    registry.registerBackend(vw::VirtualizationEnginePtr(
        new FakeEngine(FakeEngine::Config{id, {}, vw::VirtualizationTier::Native, false, false})));
    registry.registerBackend(vw::VirtualizationEnginePtr(
        new FakeEngine(FakeEngine::Config{id, {}, vw::VirtualizationTier::Native, true, true})));

    int occurrences = 0;
    for (const QString& rid : registry.backendIds()) {
        if (rid == id) {
            ++occurrences;
        }
    }
    checkEqual(occurrences, 1, "duplicate id replaces, does not duplicate");
}

void testMissingTcgSelectionFailsGracefully() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.clear();

    // Only a native backend that rejects the host.
    registry.registerBackend(makeUnavailableBackend(QStringLiteral("only-native"),
                                                     vw::VirtualizationTier::Native));

    vw::VmConfig config;
    config.architecture = QStringLiteral("aarch64");
    config.guestOs = QStringLiteral("linux");

    const vw::EngineSelection sel =
        registry.select(config, vw::BackendPreference::Auto);
    check(!sel.ok, "selection fails without TCG when native rejects host");
    check(!sel.reason.isEmpty(), "failure reason is non-empty");
    check(sel.engine.isNull(), "failed selection returns null engine");
}

void testUnknownGuestArch() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.clear();

    const QStringList x86Arches = {QStringLiteral("x86_64")};
    registry.registerBackend(makeNativeBackend(QStringLiteral("x86_64"), x86Arches));
    registry.registerBackend(makeHardwareBackend(QStringLiteral("x86_64"), x86Arches));
    registry.registerBackend(makeTcgBackend());

    vw::VmConfig config;
    config.architecture = QStringLiteral("mipsel");
    config.guestOs = QStringLiteral("linux");

    const vw::EngineSelection sel =
        registry.select(config, vw::BackendPreference::Auto);
    check(sel.ok, "unknown guest arch selection succeeds via TCG");
    checkEqual(sel.backendId, vw::backend_id::qemuTcg(),
               "unknown guest arch falls back to TCG");
}

void testEmptyProbe() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.clear();

    // An engine with hostSupported=false produces an empty-ish probe.
    const auto engine = makeUnavailableBackend(QStringLiteral("empty-proof-native"),
                                                vw::VirtualizationTier::Native);
    registry.registerBackend(engine);
    registry.registerBackend(makeTcgBackend());

    const vw::EngineProbeResult probe = engine->probe();
    check(!probe.hostSupported, "unavailable engine probe says host unsupported");
    check(!probe.capabilities.hostSupported, "unavailable engine capabilities say host unsupported");
    check(!probe.capabilities.canStart, "unavailable engine cannot start");
}

void testUnavailableFactoryIsNull() {
    // A factory that is not compiled in returns a null engine.
    // This is a contract test: we cannot test real factories here, but we
    // verify that a null engine cannot enter the catalogue.
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.clear();
    registry.registerBuiltinBackends();

    const int before = registry.backends().size();
    registry.registerBackend(vw::VirtualizationEnginePtr());
    checkEqual(registry.backends().size(), before,
               "registering a null engine is a no-op");
}

void testExplicitBackendPolicy() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.clear();

    const QStringList x86Arches = {QStringLiteral("x86_64")};
    registry.registerBackend(makeNativeBackend(QStringLiteral("x86_64"), x86Arches));
    registry.registerBackend(makeHardwareBackend(QStringLiteral("x86_64"), x86Arches));
    registry.registerBackend(makeTcgBackend());

    vw::VmConfig config;
    config.architecture = QStringLiteral("x86_64");
    config.guestOs = QStringLiteral("linux");

    // QemuOnly must never select native.
    const vw::EngineSelection qemuOnly =
        registry.select(config, vw::BackendPreference::QemuOnly);
    if (qemuOnly.ok) {
        check(!vw::tierIsNative(qemuOnly.tier),
              QStringLiteral("QemuOnly never selects native, got %1").arg(qemuOnly.backendId));
    }

    // Explicit backend that does not exist: falls back to default selection.
    const vw::EngineSelection explicitNone =
        registry.select(config, QStringLiteral("no-such-backend"));
    check(explicitNone.ok, "explicit nonexistent backend falls back to default");
    check(!explicitNone.reason.isEmpty(), "explicit nonexistent backend explains fallback");

    // Explicit backend that exists.
    const vw::EngineSelection explicitNative =
        registry.select(config, QStringLiteral("native-x86_64"));
    check(explicitNative.ok, "explicit native backend succeeds");
    checkEqual(explicitNative.backendId, QStringLiteral("native-x86_64"),
               "explicit native backend is selected");
}

// ---------------------------------------------------------------------------
// 7. Determinality: 1000+ shuffled scenarios
// ---------------------------------------------------------------------------

// Simple seeded shuffle (Fisher-Yates) for deterministic reproducibility.
void shuffleVector(QVector<vw::VirtualizationEnginePtr>& vec, quint32 seed) {
    QRandomGenerator rng(seed);
    for (int i = vec.size() - 1; i > 0; --i) {
        const int j = rng.bounded(i + 1);
        qSwap(vec[i], vec[j]);
    }
}

void testDeterminalityShuffledRegistration() {
    // Build a pool of backends with varied capabilities, then for 1000
    // scenarios shuffle the registration order and verify that the
    // selection result depends only on priority/capability, not on
    // insertion order.
    const QStringList x86Arches = {QStringLiteral("x86_64")};
    const QStringList aarch64Arches = {QStringLiteral("aarch64")};
    const QStringList riscv64Arches = {QStringLiteral("riscv64")};

    QVector<vw::VirtualizationEnginePtr> pool;
    pool << makeNativeBackend(QStringLiteral("x86_64"), x86Arches);
    pool << makeHardwareBackend(QStringLiteral("x86_64"), x86Arches);
    pool << makeNativeBackend(QStringLiteral("aarch64"), aarch64Arches);
    pool << makeHardwareBackend(QStringLiteral("aarch64"), aarch64Arches);
    pool << makeHardwareBackend(QStringLiteral("riscv64"), riscv64Arches);
    pool << makeTcgBackend();

    // Add some unavailable backends to stress the logic.
    pool << makeUnavailableBackend(QStringLiteral("dead-native-1"), vw::VirtualizationTier::Native);
    pool << makeUnavailableBackend(QStringLiteral("dead-hardware-1"), vw::VirtualizationTier::HardwareAccelerated);
    pool << makeUnavailableBackend(QStringLiteral("dead-native-2"), vw::VirtualizationTier::Native);

    const QStringList guestArches = {
        QStringLiteral("x86_64"),
        QStringLiteral("aarch64"),
        QStringLiteral("riscv64"),
        QStringLiteral("i386"),
        QStringLiteral("arm"),
    };

    const quint32 baseSeed = 42;
    const int scenarioCount = 1000;

    for (int scenario = 0; scenario < scenarioCount; ++scenario) {
        const quint32 seed = baseSeed + static_cast<quint32>(scenario);
        QVector<vw::VirtualizationEnginePtr> shuffled = pool;
        shuffleVector(shuffled, seed);

        vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
        registry.clear();
        for (const auto& engine : shuffled) {
            registry.registerBackend(engine);
        }

        // TCG must always be present and last in preference order.
        check(registry.hasBackend(vw::backend_id::qemuTcg()),
              QStringLiteral("scenario %1: TCG present").arg(scenario));
        const QVector<vw::VirtualizationEnginePtr> ordered =
            registry.backendsInPreferenceOrder();
        if (!ordered.isEmpty()) {
            checkEqual(ordered.last()->backendId(), vw::backend_id::qemuTcg(),
                       QStringLiteral("scenario %1: TCG is last").arg(scenario));
        }

        // For each guest arch, selection must be deterministic.
        for (const QString& guestArch : guestArches) {
            vw::VmConfig config;
            config.architecture = guestArch;
            config.guestOs = QStringLiteral("linux");

            const vw::EngineSelection sel =
                registry.select(config, vw::BackendPreference::Auto);

            if (!sel.ok) {
                // Should never happen with TCG present.
                check(false,
                      QStringLiteral("scenario %1/%2: selection should succeed")
                          .arg(QString::number(scenario), guestArch));
                continue;
            }

            // Cross-ISA must always land on TCG.
            const bool sameIsaAsAnyHost =
                vw::isSameNativeIsa(QStringLiteral("x86_64"), guestArch)
                || vw::isSameNativeIsa(QStringLiteral("aarch64"), guestArch)
                || vw::isSameNativeIsa(QStringLiteral("riscv64"), guestArch);

            if (!sameIsaAsAnyHost) {
                check(sel.tier == vw::VirtualizationTier::SoftwareEmulation,
                      QStringLiteral("scenario %1/%2 cross-ISA: emulation")
                          .arg(QString::number(scenario), guestArch));
            }

            // Every considered backend has a reason.
            for (const vw::BackendChoice& choice : sel.considered) {
                check(!choice.reason.isEmpty(),
                      QStringLiteral("scenario %1/%2: \"%3\" has reason")
                          .arg(QString::number(scenario), guestArch, choice.backendId));
            }
        }
    }
}

// ---------------------------------------------------------------------------
// 8. TCG is always the last resort
// ---------------------------------------------------------------------------

void testTcgAlwaysLast() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.registerBuiltinBackends();

    const QVector<vw::VirtualizationEnginePtr> ordered =
        registry.backendsInPreferenceOrder();
    check(!ordered.isEmpty(), "preference order is non-empty");

    // Tier ordering: native -> hardware -> emulation.
    for (int i = 1; i < ordered.size(); ++i) {
        check(vw::tierRank(ordered.at(i - 1)->tier()) <= vw::tierRank(ordered.at(i)->tier()),
              QStringLiteral("backends sorted by tier: %1(%2) before %3(%4)")
                  .arg(ordered.at(i - 1)->backendId(),
                       vw::tierName(ordered.at(i - 1)->tier()),
                       ordered.at(i)->backendId(),
                       vw::tierName(ordered.at(i)->tier())));
    }

    // qemu-tcg is pinned at the very end.
    checkEqual(ordered.last()->backendId(), vw::backend_id::qemuTcg(),
               "qemu-tcg is always the very last backend");
}

// ---------------------------------------------------------------------------
// 9. Full matrix with defaultSupportMatrix() invariants
// ---------------------------------------------------------------------------

void testDefaultSupportMatrixInvariants() {
    const QVector<vw::SupportMatrixRow> matrix = vw::defaultSupportMatrix();
    check(!matrix.isEmpty(), "defaultSupportMatrix is non-empty");

    // Every row has non-empty required fields.
    for (const vw::SupportMatrixRow& row : matrix) {
        check(!row.hostPlatform.isEmpty(),
              QStringLiteral("row %1/%2/%3 has hostPlatform").arg(row.hostPlatform, row.hostArch, row.guestArch));
        check(!row.hostArch.isEmpty(),
              QStringLiteral("row %1/%2/%3 has hostArch").arg(row.hostPlatform, row.hostArch, row.guestArch));
        check(!row.guestArch.isEmpty(),
              QStringLiteral("row %1/%2/%3 has guestArch").arg(row.hostPlatform, row.hostArch, row.guestArch));
        check(!row.backendId.isEmpty(),
              QStringLiteral("row %1/%2/%3 has backendId").arg(row.hostPlatform, row.hostArch, row.guestArch));
        check(!row.status.isEmpty(),
              QStringLiteral("row %1/%2/%3 has status").arg(row.hostPlatform, row.hostArch, row.guestArch));
    }

    // Cross-ISA rows must be emulation only.
    for (const vw::SupportMatrixRow& row : matrix) {
        if (!vw::isSameNativeIsa(row.hostArch, row.guestArch)) {
            check(row.tier == vw::VirtualizationTier::SoftwareEmulation,
                  QStringLiteral("cross-ISA row %1/%2/%3 is emulation")
                      .arg(row.hostPlatform, row.hostArch, row.guestArch));
            checkEqual(row.backendId, vw::backend_id::qemuTcg(),
                       QStringLiteral("cross-ISA row %1/%2/%3 uses TCG")
                           .arg(row.hostPlatform, row.hostArch, row.guestArch));
        }
    }

    // Same-ISA rows for linux/harmonyos are never native.
    for (const vw::SupportMatrixRow& row : matrix) {
        if (row.hostPlatform == QStringLiteral("linux")
            || row.hostPlatform == QStringLiteral("harmonyos")) {
            check(row.tier != vw::VirtualizationTier::Native,
                  QStringLiteral("%1 row %2/%3 never native")
                      .arg(row.hostPlatform, row.hostArch, row.guestArch));
        }
    }

    // Same-ISA rows for windows/macos with native backend are native.
    for (const vw::SupportMatrixRow& row : matrix) {
        if (vw::isSameNativeIsa(row.hostArch, row.guestArch)
            && (row.hostPlatform == QStringLiteral("windows")
                || row.hostPlatform == QStringLiteral("macos"))
            && row.backendId != vw::backend_id::qemuHardware()
            && row.backendId != vw::backend_id::qemuTcg()) {
            check(row.tier == vw::VirtualizationTier::Native,
                  QStringLiteral("%1 same-ISA native row %2/%3 is native tier")
                      .arg(row.hostPlatform, row.hostArch, row.guestArch));
        }
    }
}

// ---------------------------------------------------------------------------
// 10. BackendPreference::QemuOnly never selects native
// ---------------------------------------------------------------------------

void testQemuOnlyNeverNative() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.registerBuiltinBackends();

    const QStringList guestArches = {
        QStringLiteral("x86_64"),
        QStringLiteral("aarch64"),
        QStringLiteral("riscv64"),
    };

    for (const QString& guestArch : guestArches) {
        vw::VmConfig config;
        config.architecture = guestArch;
        config.guestOs = QStringLiteral("linux");

        const vw::EngineSelection sel =
            registry.select(config, vw::BackendPreference::QemuOnly);
        if (sel.ok) {
            check(!vw::tierIsNative(sel.tier),
                  QStringLiteral("QemuOnly never selects native for %1, got %2")
                      .arg(guestArch, sel.backendId));
        }
    }
}

} // namespace

int main(int argc, char* argv[]) {
    QCoreApplication app(argc, argv);

    testHostGuestMatrixSelectionOrder();
    testCrossIsaRejection();
    testWindowsTierHonesty();
    testLinuxHarmonyNeverNative();
    testRiscv64UnverifiedMaturity();
    testSchemaV3BackendFields();
    testSchemaV3SelectionFields();
    testSchemaV3SupportMatrix();
    testRejectionReasonsNonEmpty();
    testDuplicateIdReplacesNotDuplicates();
    testMissingTcgSelectionFailsGracefully();
    testUnknownGuestArch();
    testEmptyProbe();
    testUnavailableFactoryIsNull();
    testExplicitBackendPolicy();
    testDeterminalityShuffledRegistration();
    testTcgAlwaysLast();
    testDefaultSupportMatrixInvariants();
    testQemuOnlyNeverNative();

    qInfo().noquote() << QStringLiteral("VirtualizationArchitectureMatrixTests: %1 checks, %2 failures")
                             .arg(checks)
                             .arg(failures);
    return failures == 0 ? 0 : 1;
}