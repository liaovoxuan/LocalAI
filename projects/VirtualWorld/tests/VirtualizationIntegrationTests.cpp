// VirtualizationIntegrationTests — integration coverage for the shared
// virtualization layer.
//
// Scope: the parts that only exist once the registry, the factories and the
// platform backends are wired together.
//
//   * backend missing        nothing registered / unknown id requested
//   * factory unavailable    a platform factory that is not compiled in (or not
//                            usable here) must degrade, never crash or lie
//   * cross-arch honesty     no backend may accept a foreign ISA in a native or
//                            hardware-accelerated tier
//   * duplicate registration registering the same id twice replaces, never
//                            duplicates
//   * selection failures     every rejection carries a stable status code and a
//                            human-readable reason
//   * JSON stability         the machine-readable report always exposes the same
//                            key set, on every platform
//   * Mac / Harmony fallback the QEMU chain stays usable when a native backend
//                            cannot run the configuration
//
// This suite never starts a virtual machine. It only inspects capability
// reports, selections and JSON.

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

QString otherArch(const QString& hostArch) {
    return hostArch == QStringLiteral("aarch64") ? QStringLiteral("x86_64") : QStringLiteral("aarch64");
}

// The canonical selector vocabulary plus the documented extended codes the
// platform backends emit. The registry's `considered[]` may only ever use the
// canonical ones; `backends[].status` may use either.
bool isKnownStatusCode(const QString& code) {
    return code == vw::backend_status::available()
        || code == vw::backend_status::hostUnsupported()
        || code == vw::backend_status::guestArchUnsupported()
        || code == vw::backend_status::acceleratorUnavailable()
        || code == vw::backend_status::runtimeMissing()
        || code == vw::backend_status::configInvalid()
        || code == vw::backend_status::backendMismatch()
        || code == vw::backend_status::excludedByPolicy()
        || code == vw::backend_status::noBackend()
        || code == vw::backend_status::notRegistered()
        || code == vw::backend_status::degraded()
        || code == vw::backend_status::runtimeNotRunnable()
        || code == vw::backend_status::whpxApiMissing()
        || code == vw::backend_status::whpxHypervisorNotPresent()
        || code == vw::backend_status::whpxDegraded();
}

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

// Rank of a tier *name* as it appears in the JSON report. Higher is slower, so
// "execution is never better than the mechanism" reads as
// rank(execution) >= rank(mechanism).
int tierNameRank(const QString& tier) {
    if (tier == QStringLiteral("native")) {
        return 0;
    }
    if (tier == QStringLiteral("hardware")) {
        return 1;
    }
    if (tier == QStringLiteral("emulation")) {
        return 2;
    }
    return 3;  // unknown: treated as worse than everything, never as better
}

// A minimal backend used to exercise registration and selection rules without
// touching any platform code. `start()` fails on purpose: nothing in this suite
// is allowed to launch a guest.
class StubEngine : public vw::VirtualizationEngine {
public:
    StubEngine(QString id, vw::VirtualizationTier tier, bool hostOk, bool guestOk)
        : id_(std::move(id)), tier_(tier), hostOk_(hostOk), guestOk_(guestOk) {}

    QString backendId() const override { return id_; }
    QString displayName() const override { return id_; }
    vw::VirtualizationTier tier() const override { return tier_; }

    bool isHostSupported(QString* reason = nullptr) const override {
        if (!hostOk_ && reason) {
            *reason = QStringLiteral("stub: host not supported");
        }
        return hostOk_;
    }

    bool supportsGuestArch(const QString& guestArch, QString* reason = nullptr) const override {
        if (!hostOk_) {
            if (reason) {
                *reason = QStringLiteral("stub: host not supported");
            }
            return false;
        }
        if (!guestOk_) {
            if (reason) {
                *reason = QStringLiteral("stub: guest %1 not supported").arg(guestArch);
            }
            return false;
        }
        return true;
    }

    vw::EngineProbeResult probe() const override {
        vw::EngineProbeResult result;
        result.backendId = id_;
        result.displayName = id_;
        result.tier = tier_;
        result.hostSupported = hostOk_;
        result.capabilities.hostSupported = hostOk_;
        result.capabilities.guestArchSupported = hostOk_ && guestOk_;
        result.capabilities.nativeExecution = tier_ == vw::VirtualizationTier::Native;
        result.capabilities.hardwareAcceleration = tier_ != vw::VirtualizationTier::SoftwareEmulation;
        result.capabilities.canStart = hostOk_ && guestOk_;
        result.capabilities.canStop = hostOk_;
        result.summary = QStringLiteral("stub backend %1").arg(id_);
        return result;
    }

    QVector<vw::EngineIssue> validate(const vw::VmConfig& config) const override {
        QVector<vw::EngineIssue> issues;
        if (!hostOk_) {
            issues << vw::engineIssue(vw::EngineIssueLevel::Error,
                                      QStringLiteral("stub-host-unsupported"),
                                      QStringLiteral("stub: host not supported"));
            return issues;
        }
        if (!guestOk_) {
            issues << vw::engineIssue(vw::EngineIssueLevel::Error,
                                      QStringLiteral("stub-guest-unsupported"),
                                      QStringLiteral("stub: guest %1 not supported")
                                          .arg(vw::canonicalGuestArch(config.architecture)));
        }
        return issues;
    }

    bool start(const vw::VmConfig&, qint64*, QString* error) const override {
        if (error) {
            *error = QStringLiteral("stub backends never start a VM");
        }
        return false;
    }

    bool stop(qint64, QString* error) const override {
        if (error) {
            *error = QStringLiteral("stub backends never stop a VM");
        }
        return false;
    }

private:
    QString id_;
    vw::VirtualizationTier tier_;
    bool hostOk_;
    bool guestOk_;
};

// ---------------------------------------------------------------------------
// Backend missing
// ---------------------------------------------------------------------------

void testNoBackendRegistered() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.clear();

    check(!registry.hasBackend(vw::backend_id::qemuTcg()), "cleared registry holds nothing");
    check(!registry.hasBackend(QString()), "an empty id is never a backend");
    check(registry.backend(QString()).isNull(), "looking up an empty id returns null");
    check(registry.backendIds().isEmpty(), "cleared registry reports no ids");
    check(registry.backendsInPreferenceOrder().isEmpty(), "cleared registry orders nothing");

    vw::VmConfig config;
    config.architecture = QStringLiteral("aarch64");
    config.guestOs = QStringLiteral("linux");

    const vw::EngineSelection selection = registry.select(config, vw::BackendPreference::Auto);
    check(!selection.ok, "a registry with no backends cannot select anything");
    check(selection.engine.isNull(), "failed selection hands back no engine");
    check(!selection.reason.isEmpty(), "failure with no backends still explains itself");
    check(selection.considered.isEmpty(), "no backend means nothing was considered");

    // The report must stay machine-readable even in this degenerate state.
    const QString json = registry.toJson(config, QStringLiteral("aarch64"));
    const QJsonDocument document = QJsonDocument::fromJson(json.toUtf8());
    check(!document.isNull() && document.isObject(), "empty registry still emits a JSON object");

    // Restoring the built-ins must work after a clear().
    registry.registerBuiltinBackends();
    check(registry.hasBackend(vw::backend_id::qemuTcg()), "registerBuiltinBackends works after clear()");
}

void testUnknownBackendRequested() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.registerBuiltinBackends();

    vw::VmConfig config;
    config.architecture = QStringLiteral("aarch64");
    config.guestOs = QStringLiteral("linux");

    const vw::EngineSelection selection = registry.select(config, QStringLiteral("no-such-backend"));
    check(selection.reason.contains(QStringLiteral("no-such-backend")),
          "an unknown requested backend is named in the reason");
    for (const vw::BackendChoice& choice : selection.considered) {
        check(!choice.backendId.isEmpty(), "every considered entry has an id");
        // The selector itself only ever emits canonical codes.
        check(isCanonicalStatusCode(choice.statusCode),
              QStringLiteral("\"%1\" uses a canonical selector status code").arg(choice.backendId));
        check(!choice.reason.isEmpty(),
              QStringLiteral("\"%1\" explains itself").arg(choice.backendId));
    }
}

// ---------------------------------------------------------------------------
// Factory unavailable
// ---------------------------------------------------------------------------

void testFactoriesNeverLie() {
    // macOS: the factory is only usable when the platform sources are compiled
    // into this build. Either way the contract holds.
    const bool macAvailable = vw::macOSVirtualizationEngineAvailable();
    const vw::VirtualizationEnginePtr macEngine = vw::makeMacOSVirtualizationEngine();
#if defined(VIRTUALWORLD_MACOS_BACKEND)
    check(macAvailable, "macOS backend compiled in: the factory reports itself available");
    check(!macEngine.isNull(), "macOS backend compiled in: the factory builds an engine");
    if (!macEngine.isNull()) {
        checkEqual(macEngine->backendId(), vw::backend_id::macOSVirtualization(),
                   "macOS adapter backend id");
        check(macEngine->tier() == vw::VirtualizationTier::Native,
              "Virtualization.framework is a native-tier backend");
        check(macEngine->runsInProcess(), "Virtualization.framework guests run in-process");
    }
#else
    check(!macAvailable, "macOS backend not compiled in: the factory reports itself unavailable");
    check(macEngine.isNull(), "macOS backend not compiled in: the factory returns no engine");
#endif

    // HarmonyOS/OpenHarmony: the same contract, and additionally the backend
    // must never claim the native tier.
    const bool harmonyCompiled = vw::harmonyBackendCompiledIn();
#if defined(VIRTUALWORLD_HAVE_HARMONY_BACKEND)
    check(harmonyCompiled, "Harmony backend compiled in: harmonyBackendCompiledIn() is true");
#else
    check(!harmonyCompiled, "Harmony backend absent: harmonyBackendCompiledIn() is false");
    check(!vw::harmonyVirtualizationEngineAvailable(),
          "Harmony backend absent: the factory reports itself unavailable");
    check(vw::makeHarmonyVirtualizationEngine().isNull(),
          "Harmony backend absent: the factory returns no engine");
#endif

    // Linux KVM: the same contract as Harmony, and — because it is a QEMU
    // compatibility backend — the same "never native" rule.
    const bool kvmCompiled = vw::linuxKvmBackendCompiledIn();
#if defined(VIRTUALWORLD_HAVE_LINUX_KVM_BACKEND)
    check(kvmCompiled, "Linux KVM backend compiled in: linuxKvmBackendCompiledIn() is true");
#else
    check(!kvmCompiled, "Linux KVM backend absent: linuxKvmBackendCompiledIn() is false");
    check(!vw::linuxKvmEngineAvailable(),
          "Linux KVM backend absent: the factory reports itself unavailable");
    check(vw::makeLinuxKvmEngine().isNull(),
          "Linux KVM backend absent: the factory returns no engine");
#endif

    // A null engine must never enter the catalogue.
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.registerBuiltinBackends();
    const int before = registry.backends().size();
    registry.registerBackend(vw::VirtualizationEnginePtr());
    checkEqual(QString::number(registry.backends().size()), QString::number(before),
               "registering a null engine is a no-op");
}

void testLinuxKvmFallback() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.registerBuiltinBackends();

    const vw::VirtualizationEnginePtr kvm = registry.backend(vw::backend_id::linuxKvm());
    if (!kvm.isNull()) {
        checkEqual(kvm->backendId(), vw::backend_id::linuxKvm(), "the Linux KVM backend uses its stable id");
        // LinuxKvmEngine runs QEMU; even with KVM that is hardware
        // acceleration, not native execution.
        check(kvm->tier() != vw::VirtualizationTier::Native,
              "the Linux KVM backend never reports the native tier");
        check(!vw::tierIsNative(kvm->tier()), "tierIsNative() agrees for the Linux KVM backend");
        const vw::EngineProbeResult probed = kvm->probe();
        check(!probed.capabilities.nativeExecution,
              "the Linux KVM backend never claims native execution");
        if (!probed.capabilities.hostSupported) {
            QString reason;
            check(!kvm->isHostSupported(&reason), "isHostSupported() and probe() agree");
            check(!reason.isEmpty(), "the Linux KVM backend explains why this host is unsupported");
        }
        // "Accelerated" is only claimed when the accelerator is really there.
        if (probed.capabilities.hardwareAcceleration) {
            check(probed.capabilities.hostSupported,
                  "hardware acceleration is only claimed on a host the backend supports");
            check(!probed.capabilities.accelerator.isEmpty()
                      && probed.capabilities.accelerator != QStringLiteral("tcg"),
                  "a hardware-accelerated claim names a real accelerator");
        }
    }

    // The documented matrix must agree: no Linux row is native.
    for (const vw::SupportMatrixRow& row : vw::defaultSupportMatrix()) {
        if (row.hostPlatform != QStringLiteral("linux")) {
            continue;
        }
        check(row.tier != vw::VirtualizationTier::Native,
              QStringLiteral("Linux row %1/%2 is not native").arg(row.hostArch, row.guestArch));
    }

    // QEMU stays reachable no matter what the KVM backend reported.
    check(registry.hasBackend(vw::backend_id::qemuTcg()), "QEMU TCG stays registered");
}

// ---------------------------------------------------------------------------
// Duplicate registration
// ---------------------------------------------------------------------------

void testDuplicateRegistration() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.clear();

    const QString id = QStringLiteral("stub-duplicate");
    registry.registerBackend(vw::VirtualizationEnginePtr(
        new StubEngine(id, vw::VirtualizationTier::SoftwareEmulation, true, false)));
    registry.registerBackend(vw::VirtualizationEnginePtr(
        new StubEngine(id, vw::VirtualizationTier::SoftwareEmulation, true, true)));

    int occurrences = 0;
    for (const QString& registered : registry.backendIds()) {
        if (registered == id) {
            ++occurrences;
        }
    }
    checkEqual(QString::number(occurrences), QStringLiteral("1"),
               "registering the same id twice replaces instead of duplicating");
    checkEqual(QString::number(registry.backends().size()), QStringLiteral("1"),
               "duplicate registration leaves exactly one entry");

    // The surviving entry is the second one, so the replacement is observable.
    vw::VmConfig config;
    config.architecture = QStringLiteral("aarch64");
    config.guestOs = QStringLiteral("linux");
    const vw::EngineSelection selection = registry.select(config, vw::BackendPreference::Auto);
    check(selection.ok, "the replacement backend accepts the guest architecture");
    checkEqual(selection.backendId, id, "the replacement backend is the one selected");

    // Built-ins re-registering after a clear() must not duplicate either.
    registry.registerBuiltinBackends();
    registry.registerBuiltinBackends();
    const QStringList ids = registry.backendIds();
    int tcg = 0;
    for (const QString& registered : ids) {
        if (registered == vw::backend_id::qemuTcg()) {
            ++tcg;
        }
    }
    checkEqual(QString::number(tcg), QStringLiteral("1"),
               "calling registerBuiltinBackends twice registers TCG once");

    // And the self-registering platform backends must survive a duplicate too.
    int harmony = 0;
    for (const QString& registered : ids) {
        if (registered == vw::backend_id::harmonyVirtualization()) {
            ++harmony;
        }
    }
    check(harmony <= 1, "the Harmony backend appears at most once even with self-registration");
}

// ---------------------------------------------------------------------------
// Cross-architecture honesty
// ---------------------------------------------------------------------------

void testCrossArchIsNeverAccelerated() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.registerBuiltinBackends();

    const vw::HostVirtualizationProfile profile = vw::detectHostVirtualizationProfile();
    const QString foreign = otherArch(profile.hostArch);

    for (const vw::VirtualizationEnginePtr& engine : registry.backends()) {
        if (engine->tier() == vw::VirtualizationTier::SoftwareEmulation) {
            continue;  // TCG is allowed to run anything, slowly
        }
        QString reason;
        check(!engine->supportsGuestArch(foreign, &reason),
              QStringLiteral("\"%1\" (%2) must reject a cross-arch guest")
                  .arg(engine->backendId(), vw::tierName(engine->tier())));
        check(!reason.isEmpty(),
              QStringLiteral("\"%1\" explains the cross-arch rejection").arg(engine->backendId()));
    }

    // And the selection agrees: a foreign ISA never lands on an accelerated
    // backend, whatever the host is.
    vw::VmConfig config;
    config.architecture = foreign;
    config.guestOs = QStringLiteral("linux");
    const vw::EngineSelection selection = registry.select(config, vw::BackendPreference::Auto);
    if (selection.ok) {
        check(selection.tier == vw::VirtualizationTier::SoftwareEmulation,
              QStringLiteral("cross-arch selection lands on TCG, got %1 (%2)")
                  .arg(selection.backendId, vw::tierName(selection.tier)));
        check(!selection.engine.isNull(), "a successful selection hands back an engine");
    } else {
        check(!selection.reason.isEmpty(), "a failed cross-arch selection explains itself");
    }
}

// ---------------------------------------------------------------------------
// Selection failure reasons
// ---------------------------------------------------------------------------

void testSelectionFailureReasons() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.clear();

    // Nothing in this catalogue can run anything: both stubs reject the host.
    registry.registerBackend(vw::VirtualizationEnginePtr(
        new StubEngine(QStringLiteral("stub-native"), vw::VirtualizationTier::Native, false, false)));
    registry.registerBackend(vw::VirtualizationEnginePtr(
        new StubEngine(QStringLiteral("stub-tcg"), vw::VirtualizationTier::SoftwareEmulation, false, false)));

    vw::VmConfig config;
    config.architecture = QStringLiteral("aarch64");
    config.guestOs = QStringLiteral("linux");

    const vw::EngineSelection selection = registry.select(config, vw::BackendPreference::Auto);
    check(!selection.ok, "a catalogue where nothing works fails the selection");
    check(selection.engine.isNull(), "a failed selection hands back no engine");
    check(selection.backendId.isEmpty(), "a failed selection names no backend");
    check(!selection.reason.isEmpty(), "a failed selection explains itself");
    check(selection.considered.size() == 2, "both candidates were considered");
    for (const vw::BackendChoice& choice : selection.considered) {
        checkEqual(choice.statusCode, vw::backend_status::hostUnsupported(),
                   QStringLiteral("\"%1\" reports the canonical host-unsupported").arg(choice.backendId));
        check(!choice.reason.isEmpty(),
              QStringLiteral("\"%1\" carries a reason").arg(choice.backendId));
        check(!choice.selected, "a rejected backend is never marked selected");
    }

    // Explicitly requesting a backend that exists but cannot run the config
    // must fall back rather than pretend.
    const vw::EngineSelection explicitSelection = registry.select(config, QStringLiteral("stub-native"));
    check(!explicitSelection.ok, "an explicit but unusable backend does not force success");

    // ...whereas a working backend is honoured and reports no fallback.
    registry.clear();
    registry.registerBackend(vw::VirtualizationEnginePtr(
        new StubEngine(QStringLiteral("stub-works"), vw::VirtualizationTier::SoftwareEmulation, true, true)));
    const vw::EngineSelection good = registry.select(config, vw::BackendPreference::Auto);
    check(good.ok, "a working backend is selected");
    check(!good.fellBack, "selecting the only working backend reports no fallback");
    checkEqual(good.backendId, QStringLiteral("stub-works"), "the working backend is selected");

    registry.registerBuiltinBackends();
}

// ---------------------------------------------------------------------------
// JSON field stability
// ---------------------------------------------------------------------------

void testJsonFieldStability() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.registerBuiltinBackends();

    vw::VmConfig config;
    config.architecture = QStringLiteral("aarch64");
    config.guestOs = QStringLiteral("linux");

    const QString json = registry.toJson(config, QStringLiteral("aarch64"));
    const QJsonDocument document = QJsonDocument::fromJson(json.toUtf8());
    check(!document.isNull() && document.isObject(), "the report parses as a JSON object");
    const QJsonObject root = document.object();

    checkEqual(root.value(QStringLiteral("schema")).toString(), vw::backendReportSchema(),
               "the report declares the current schema");

    // The build block is present on every platform with the same keys, so a
    // consumer can always tell which backends this binary actually contains.
    const QJsonObject build = root.value(QStringLiteral("build")).toObject();
    check(build.contains(QStringLiteral("macos_backend")), "build reports macos_backend");
    check(build.contains(QStringLiteral("windows_backend")), "build reports windows_backend");
    check(build.contains(QStringLiteral("linux_kvm_backend")), "build reports linux_kvm_backend");
    check(build.contains(QStringLiteral("harmony_backend")), "build reports harmony_backend");
    check(build.contains(QStringLiteral("qemu_fallback")), "build reports qemu_fallback");
    check(build.value(QStringLiteral("qemu_fallback")).toBool(), "the QEMU fallback is always present");
#if defined(VIRTUALWORLD_MACOS_BACKEND)
    check(build.value(QStringLiteral("macos_backend")).toBool(),
          "macos_backend matches the compile definition");
#else
    check(!build.value(QStringLiteral("macos_backend")).toBool(),
          "macos_backend matches the compile definition");
#endif
#if defined(VIRTUALWORLD_HAVE_HARMONY_BACKEND)
    check(build.value(QStringLiteral("harmony_backend")).toBool(),
          "harmony_backend matches the compile definition");
#else
    check(!build.value(QStringLiteral("harmony_backend")).toBool(),
          "harmony_backend matches the compile definition");
#endif

    const QJsonObject host = root.value(QStringLiteral("host")).toObject();
    for (const QString& key : {QStringLiteral("platform"), QStringLiteral("family"),
                               QStringLiteral("arch"), QStringLiteral("name")}) {
        check(host.contains(key), QStringLiteral("host object has \"%1\"").arg(key));
    }
    check(root.contains(QStringLiteral("guest_arch")), "report has guest_arch");
    check(root.contains(QStringLiteral("preference")), "report has preference");

    const QJsonArray backends = root.value(QStringLiteral("backends")).toArray();
    check(!backends.isEmpty(), "report lists backends");
    for (const QJsonValue& value : backends) {
        const QJsonObject backend = value.toObject();
        for (const QString& key : {QStringLiteral("id"), QStringLiteral("name"), QStringLiteral("tier"),
                                   QStringLiteral("execution_tier"),
                                   QStringLiteral("host_supported"), QStringLiteral("guest_arch_supported"),
                                   QStringLiteral("hardware_acceleration"), QStringLiteral("native_execution"),
                                   QStringLiteral("accelerator"), QStringLiteral("summary"),
                                   QStringLiteral("status"), QStringLiteral("status_class"),
                                   QStringLiteral("runs_in_process"),
                                   QStringLiteral("requirements"), QStringLiteral("issues")}) {
            check(backend.contains(key),
                  QStringLiteral("backend \"%1\" has \"%2\"")
                      .arg(backend.value(QStringLiteral("id")).toString(), key));
        }
        const QString status = backend.value(QStringLiteral("status")).toString();
        const QString statusClass = backend.value(QStringLiteral("status_class")).toString();
        check(!status.isEmpty(),
              QStringLiteral("backend \"%1\" reports a status")
                  .arg(backend.value(QStringLiteral("id")).toString()));
        // `status_class` is the stable field: always canonical, and always the
        // fold of `status` — never an independent opinion.
        check(isCanonicalStatusCode(statusClass),
              QStringLiteral("backend \"%1\" reports a canonical status_class")
                  .arg(backend.value(QStringLiteral("id")).toString()));
        checkEqual(statusClass, vw::statusClass(status),
                   QStringLiteral("backend \"%1\" status_class is the fold of its status")
                       .arg(backend.value(QStringLiteral("id")).toString()));
        // `execution_tier` is the honest per-guest answer and can never be a
        // better tier than the mechanism `tier` reports.
        const QString tierText = backend.value(QStringLiteral("tier")).toString();
        const QString executionTierText = backend.value(QStringLiteral("execution_tier")).toString();
        check(!executionTierText.isEmpty(),
              QStringLiteral("backend \"%1\" reports an execution_tier")
                  .arg(backend.value(QStringLiteral("id")).toString()));
        check(tierNameRank(executionTierText) >= tierNameRank(tierText),
              QStringLiteral("backend \"%1\" execution_tier is never better than its tier")
                  .arg(backend.value(QStringLiteral("id")).toString()));
        // A backend that cannot run the guest here must not promise acceleration.
        if (!backend.value(QStringLiteral("host_supported")).toBool()
            || !backend.value(QStringLiteral("guest_arch_supported")).toBool()) {
            check(executionTierText == QStringLiteral("emulation"),
                  QStringLiteral("backend \"%1\" reports emulation when it cannot run the guest")
                      .arg(backend.value(QStringLiteral("id")).toString()));
        }

        // A backend that is not native must never claim native execution.
        if (backend.value(QStringLiteral("tier")).toString() != QStringLiteral("native")) {
            check(!backend.value(QStringLiteral("native_execution")).toBool(),
                  QStringLiteral("non-native backend \"%1\" does not claim native execution")
                      .arg(backend.value(QStringLiteral("id")).toString()));
        }
    }

    const QJsonObject selection = root.value(QStringLiteral("selection")).toObject();
    for (const QString& key : {QStringLiteral("ok"), QStringLiteral("backend"), QStringLiteral("name"),
                               QStringLiteral("tier"), QStringLiteral("execution_tier"),
                               QStringLiteral("reason"), QStringLiteral("fell_back"),
                               QStringLiteral("fallback_from"), QStringLiteral("considered")}) {
        check(selection.contains(key), QStringLiteral("selection object has \"%1\"").arg(key));
    }

    const QJsonArray matrix = root.value(QStringLiteral("support_matrix")).toArray();
    check(!matrix.isEmpty(), "report carries the support matrix");
    for (const QJsonValue& value : matrix) {
        const QJsonObject row = value.toObject();
        for (const QString& key : {QStringLiteral("host_platform"), QStringLiteral("host_arch"),
                                   QStringLiteral("guest_arch"), QStringLiteral("backend"),
                                   QStringLiteral("tier"), QStringLiteral("status"),
                                   QStringLiteral("verified"), QStringLiteral("note")}) {
            check(row.contains(key), QStringLiteral("matrix row has \"%1\"").arg(key));
        }
    }

    // The report must be deterministic across calls.
    checkEqual(registry.toJson(config, QStringLiteral("aarch64")), json,
               "the report is stable across repeated calls");
}

// ---------------------------------------------------------------------------
// Mac / Harmony fallback
// ---------------------------------------------------------------------------

void testMacFallback() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.registerBuiltinBackends();

    const bool hasMac = registry.hasBackend(vw::backend_id::macOSVirtualization());
#if defined(VIRTUALWORLD_MACOS_BACKEND)
    check(hasMac, "macOS backend compiled in: it is in the registry");
    const vw::VirtualizationEnginePtr mac = registry.backend(vw::backend_id::macOSVirtualization());
    if (!mac.isNull()) {
        check(mac->tier() == vw::VirtualizationTier::Native, "the macOS backend is native tier");
        const vw::EngineProbeResult probed = mac->probe();
        if (!probed.capabilities.hostSupported) {
            check(!probed.summary.isEmpty(), "an unsupported macOS backend says why");
        }
        // Even when Virtualization.framework is present, a macOS guest restore
        // is not wired up yet: start() must refuse instead of pretending.
        check(mac->runsInProcess(), "the macOS backend owns the guest in-process");
    }
#else
    check(!hasMac, "macOS backend not compiled in: it is absent from the registry");
#endif

    // The QEMU chain must survive whatever happened above.
    check(registry.hasBackend(vw::backend_id::qemuHardware()), "QEMU hardware acceleration stays registered");
    check(registry.hasBackend(vw::backend_id::qemuTcg()), "QEMU TCG stays registered as the last resort");
}

void testHarmonyFallback() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.registerBuiltinBackends();

    const vw::VirtualizationEnginePtr harmony = registry.backend(vw::backend_id::harmonyVirtualization());
    if (!harmony.isNull()) {
        checkEqual(harmony->backendId(), vw::backend_id::harmonyVirtualization(),
                   "the Harmony backend uses its stable id");
        // The honesty invariant: HarmonyOS/OpenHarmony exposes no public
        // hypervisor API, so a QEMU process accelerated by KVM is hardware
        // tier, never native.
        check(harmony->tier() != vw::VirtualizationTier::Native,
              "the Harmony backend never reports the native tier");
        check(!vw::tierIsNative(harmony->tier()), "tierIsNative() agrees for the Harmony backend");
        const vw::EngineProbeResult probed = harmony->probe();
        check(!probed.capabilities.nativeExecution,
              "the Harmony backend never claims native execution");
        // On a non-Harmony host it must say so rather than fall through silently.
        if (!probed.capabilities.hostSupported) {
            QString reason;
            check(!harmony->isHostSupported(&reason), "isHostSupported() and probe() agree");
            check(!reason.isEmpty(), "the Harmony backend explains why this host is unsupported");
        }
    }

    // The documented matrix must agree with the code: no HarmonyOS row is native.
    for (const vw::SupportMatrixRow& row : vw::defaultSupportMatrix()) {
        if (row.hostPlatform != QStringLiteral("harmonyos")) {
            continue;
        }
        check(row.tier != vw::VirtualizationTier::Native,
              QStringLiteral("HarmonyOS row %1/%2 is not native").arg(row.hostArch, row.guestArch));
        if (!vw::isSameNativeIsa(row.hostArch, row.guestArch)) {
            check(row.tier == vw::VirtualizationTier::SoftwareEmulation,
                  QStringLiteral("cross-arch HarmonyOS row %1/%2 is emulation").arg(row.hostArch, row.guestArch));
        }
    }

    // QEMU stays reachable no matter what the Harmony backend reported.
    check(registry.hasBackend(vw::backend_id::qemuTcg()), "QEMU TCG stays registered");
}

void testTcgIsAlwaysLastResort() {
    vw::VirtualizationEngineRegistry& registry = vw::VirtualizationEngineRegistry::instance();
    registry.registerBuiltinBackends();

    const QVector<vw::VirtualizationEnginePtr> ordered = registry.backendsInPreferenceOrder();
    check(!ordered.isEmpty(), "the catalogue is not empty");
    for (int i = 1; i < ordered.size(); ++i) {
        check(vw::tierRank(ordered.at(i - 1)->tier()) <= vw::tierRank(ordered.at(i)->tier()),
              "candidates are ordered native -> hardware -> emulation");
    }
    check(vw::tierRank(ordered.last()->tier()) == vw::tierRank(vw::VirtualizationTier::SoftwareEmulation),
          "the last candidate sits in the emulation tier");
    // Several platform backends degrade into the emulation tier on a host that
    // cannot accelerate them. That is fine — but qemu-tcg is pinned last, so
    // the guaranteed fallback is never pre-empted.
    checkEqual(ordered.last()->backendId(), vw::backend_id::qemuTcg(),
               "qemu-tcg is always the very last candidate, even behind degraded platform backends");

    // QemuOnly must never produce a native selection.
    vw::VmConfig config;
    config.architecture = vw::detectHostVirtualizationProfile().hostArch;
    config.guestOs = QStringLiteral("linux");
    const vw::EngineSelection qemuOnly = registry.select(config, vw::BackendPreference::QemuOnly);
    if (qemuOnly.ok) {
        check(!vw::tierIsNative(qemuOnly.tier),
              QStringLiteral("QemuOnly never selects a native backend, got %1").arg(qemuOnly.backendId));
    }
}

} // namespace

int main(int argc, char* argv[]) {
    // RuntimeManager resolves paths through QCoreApplication; instantiating one
    // keeps the runtime probe quiet.
    QCoreApplication app(argc, argv);

    testNoBackendRegistered();
    testUnknownBackendRequested();
    testFactoriesNeverLie();
    testDuplicateRegistration();
    testCrossArchIsNeverAccelerated();
    testSelectionFailureReasons();
    testJsonFieldStability();
    testMacFallback();
    testHarmonyFallback();
    testLinuxKvmFallback();
    testTcgIsAlwaysLastResort();

    qInfo().noquote() << QStringLiteral("VirtualizationIntegrationTests: %1 checks, %2 failures")
                             .arg(checks)
                             .arg(failures);
    return failures == 0 ? 0 : 1;
}
