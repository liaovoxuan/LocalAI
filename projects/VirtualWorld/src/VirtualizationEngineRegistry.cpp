#include "VirtualizationEngineRegistry.hpp"

#include "Platform.hpp"
#include "VirtualizationCapabilities.hpp"
#include "WindowsHypervisorEngine.hpp"

// Optional HarmonyOS/OpenHarmony backend. The guard is set by CMake only when
// both of its sources exist, so this translation unit never references the
// factory in a build that does not link it.
#if defined(VIRTUALWORLD_HAVE_HARMONY_BACKEND)
#include "HarmonyVirtualizationEngine.hpp"
#endif

#if defined(VIRTUALWORLD_HAVE_LINUX_KVM_BACKEND)
#include "LinuxKvmEngine.hpp"
#endif

#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonValue>
#include <QtGlobal>

#include <algorithm>

namespace vw {

namespace {

EngineProbeResult probeBackend(const VirtualizationEnginePtr& engine, const QString& guestArch) {
    if (guestArch.trimmed().isEmpty()) {
        return engine->probe();
    }
    return engine->probeFor(guestArch);
}

BackendChoice describeChoice(const VirtualizationEnginePtr& engine,
                             const VmConfig& config,
                             const QString& guestArch) {
    BackendChoice choice;
    choice.backendId = engine->backendId();
    choice.displayName = engine->displayName();
    choice.tier = engine->tier();

    const EngineProbeResult probed = probeBackend(engine, guestArch);
    choice.hostSupported = probed.capabilities.hostSupported;
    choice.guestArchSupported = probed.capabilities.guestArchSupported;

    if (!choice.hostSupported) {
        choice.statusCode = backend_status::hostUnsupported();
        choice.reason = probed.summary.isEmpty()
            ? QStringLiteral("宿主不支持该后端。")
            : probed.summary;
        return choice;
    }
    if (!choice.guestArchSupported) {
        choice.statusCode = backend_status::guestArchUnsupported();
        QString reason;
        engine->supportsGuestArch(guestArch.isEmpty() ? config.architecture : guestArch, &reason);
        choice.reason = reason.isEmpty() ? QStringLiteral("后端不支持该客户机架构。") : reason;
        return choice;
    }

    const QVector<EngineIssue> issues = engine->validate(config);
    choice.configAccepted = !hasEngineErrors(issues);
    if (!choice.configAccepted) {
        choice.statusCode = backend_status::configInvalid();
        choice.reason = joinEngineIssues(issues);
        if (choice.reason.isEmpty()) {
            choice.reason = QStringLiteral("配置校验失败。");
        }
        return choice;
    }

    choice.statusCode = backend_status::available();
    choice.reason = QStringLiteral("可用：%1").arg(tierDescription(engine->tier()));
    return choice;
}

} // namespace

QStringList EngineSelection::consideredIds() const {
    QStringList ids;
    for (const BackendChoice& choice : considered) {
        ids << choice.backendId;
    }
    return ids;
}

VirtualizationEngineRegistry& VirtualizationEngineRegistry::instance() {
    static VirtualizationEngineRegistry registry;
    return registry;
}

void VirtualizationEngineRegistry::registerBackend(VirtualizationEnginePtr engine) {
    if (!engine) {
        return;
    }
    for (int i = 0; i < backends_.size(); ++i) {
        if (backends_.at(i)->backendId() == engine->backendId()) {
            backends_[i] = engine;
            return;
        }
    }
    backends_.append(engine);
}

bool VirtualizationEngineRegistry::hasBackend(const QString& backendId) const {
    return !backend(backendId).isNull();
}

QVector<VirtualizationEnginePtr> VirtualizationEngineRegistry::backends() const {
    return backends_;
}

QStringList VirtualizationEngineRegistry::backendIds() const {
    QStringList ids;
    for (const VirtualizationEnginePtr& engine : backends_) {
        ids << engine->backendId();
    }
    return ids;
}

VirtualizationEnginePtr VirtualizationEngineRegistry::backend(const QString& backendId) const {
    const QString wanted = backendId.trimmed().toLower();
    for (const VirtualizationEnginePtr& engine : backends_) {
        if (engine->backendId().compare(wanted, Qt::CaseInsensitive) == 0) {
            return engine;
        }
    }
    return VirtualizationEnginePtr();
}

void VirtualizationEngineRegistry::registerBuiltinBackends() {
    if (builtinsRegistered_) {
        return;
    }
    builtinsRegistered_ = true;

    // The Windows backend is registered on every platform on purpose: it is
    // plain C++ outside `_WIN32` and reports `host-unsupported` honestly, which
    // lets the CLI document the whole matrix instead of hiding rows.
    registerBackend(VirtualizationEnginePtr(new WindowsHypervisorEngine()));

    if (macOSVirtualizationEngineAvailable()) {
        const VirtualizationEnginePtr engine = makeMacOSVirtualizationEngine();
        if (engine) {
            registerBackend(engine);
        }
    }

    // HarmonyOS/OpenHarmony. Explicit registration wins over the backend's own
    // static self-registration when both happen; registerBackend() replaces by
    // id, so the catalogue never ends up with two entries. The factory is only
    // consulted when the platform sources are actually linked in.
#if defined(VIRTUALWORLD_HAVE_HARMONY_BACKEND)
    if (harmonyVirtualizationEngineAvailable()) {
        const VirtualizationEnginePtr engine = makeHarmonyVirtualizationEngine();
        if (engine) {
            registerBackend(engine);
        }
    }
#endif

    // Linux KVM. Same id-replacing rule as Harmony: the backend also
    // self-registers from its own translation unit, and whichever runs second
    // simply replaces the first.
#if defined(VIRTUALWORLD_HAVE_LINUX_KVM_BACKEND)
    if (linuxKvmEngineAvailable()) {
        const VirtualizationEnginePtr engine = makeLinuxKvmEngine();
        if (engine) {
            registerBackend(engine);
        }
    }
#endif

    registerBackend(VirtualizationEnginePtr(new QemuHardwareEngine()));
    registerBackend(VirtualizationEnginePtr(new QemuTcgEngine()));
}

void VirtualizationEngineRegistry::clear() {
    backends_.clear();
    builtinsRegistered_ = false;
}

QVector<VirtualizationEnginePtr> VirtualizationEngineRegistry::backendsInPreferenceOrder() const {
    QVector<VirtualizationEnginePtr> ordered = backends_;
    std::stable_sort(ordered.begin(), ordered.end(),
                     [](const VirtualizationEnginePtr& left, const VirtualizationEnginePtr& right) {
                         if (tierRank(left->tier()) != tierRank(right->tier())) {
                             return tierRank(left->tier()) < tierRank(right->tier());
                         }
                         return left->backendId() < right->backendId();
                     });

    // `qemu-tcg` is the guaranteed last resort, so it is pinned to the very end
    // rather than being sorted by id inside the emulation tier. Several
    // platform backends honestly degrade to SoftwareEmulation on a host that
    // cannot accelerate them (linux-kvm without /dev/kvm, windows-hypervisor
    // without WHPX, harmony-virtualization without KVM), and those legitimately
    // run before the generic TCG backend — but nothing may run after it.
    const QString tcgId = backend_id::qemuTcg();
    for (int i = 0; i < ordered.size(); ++i) {
        if (ordered.at(i)->backendId() == tcgId) {
            const VirtualizationEnginePtr tcg = ordered.takeAt(i);
            ordered.append(tcg);
            break;
        }
    }
    return ordered;
}

BackendPreference parseBackendPreference(const QString& value, bool* ok) {
    const QString text = value.trimmed().toLower();
    if (ok) {
        *ok = true;
    }
    if (text.isEmpty() || text == backend_id::autoSelect()) {
        return BackendPreference::Auto;
    }
    if (text == backend_id::qemuLegacy()) {
        return BackendPreference::QemuOnly;
    }
    return BackendPreference::Explicit;
}

QString resolveBackendPreference(const QString& requested, const VmConfig& config) {
    if (!requested.trimmed().isEmpty()) {
        return requested.trimmed();
    }
    const QString fromEnv = QString::fromLocal8Bit(qgetenv("VIRTUALWORLD_BACKEND")).trimmed();
    if (!fromEnv.isEmpty()) {
        return fromEnv;
    }
    return config.backend.trimmed().isEmpty() ? backend_id::qemuLegacy() : config.backend.trimmed();
}

EngineSelection VirtualizationEngineRegistry::select(const VmConfig& config,
                                                     const QString& preferenceText) const {
    const QString resolved = resolveBackendPreference(preferenceText, config);
    const BackendPreference preference = parseBackendPreference(resolved);
    if (preference == BackendPreference::Explicit) {
        return select(config, preference, resolved);
    }
    return select(config, preference);
}

EngineSelection VirtualizationEngineRegistry::select(const VmConfig& config,
                                                     BackendPreference preference,
                                                     const QString& explicitBackendId) const {
    const QString guestArch = canonicalGuestArch(config.architecture);

    QVector<VirtualizationEnginePtr> ordered = backendsInPreferenceOrder();

    EngineSelection selection;
    QString explicitReason;

    if (preference == BackendPreference::QemuOnly) {
        QVector<VirtualizationEnginePtr> filtered;
        for (const VirtualizationEnginePtr& engine : ordered) {
            if (engine->tier() != VirtualizationTier::Native) {
                filtered.append(engine);
            } else {
                BackendChoice skipped;
                skipped.backendId = engine->backendId();
                skipped.displayName = engine->displayName();
                skipped.tier = engine->tier();
                skipped.statusCode = backend_status::excludedByPolicy();
                skipped.reason = QStringLiteral("策略要求只使用 QEMU 后端，跳过原生后端。");
                selection.considered.append(skipped);
            }
        }
        ordered = filtered;
    } else if (preference == BackendPreference::Explicit) {
        const VirtualizationEnginePtr requested = backend(explicitBackendId);
        if (requested.isNull()) {
            explicitReason = QStringLiteral("未找到后端 “%1”，按默认顺序选择。").arg(explicitBackendId);
        } else {
            ordered.removeAll(requested);
            ordered.prepend(requested);
        }
    }

    // Index in `selection.considered` where the evaluated candidates start.
    const int evaluatedOffset = selection.considered.size();

    int chosenIndex = -1;
    for (int i = 0; i < ordered.size(); ++i) {
        const VirtualizationEnginePtr& engine = ordered.at(i);
        BackendChoice choice = describeChoice(engine, config, guestArch);
        if (chosenIndex < 0 && choice.configAccepted && choice.hostSupported
            && choice.guestArchSupported) {
            choice.selected = true;
            chosenIndex = i;
        }
        selection.considered.append(choice);
    }

    if (chosenIndex < 0) {
        selection.ok = false;
        QStringList reasons;
        for (int i = evaluatedOffset; i < selection.considered.size(); ++i) {
            reasons << QStringLiteral("%1: %2").arg(selection.considered.at(i).backendId,
                                                    selection.considered.at(i).reason);
        }
        selection.reason = reasons.isEmpty()
            ? QStringLiteral("没有注册任何可用后端。")
            : QStringLiteral("所有候选后端都无法运行该配置：%1").arg(reasons.join(QStringLiteral("; ")));
        if (!explicitReason.isEmpty()) {
            selection.reason = explicitReason + QStringLiteral(" ") + selection.reason;
        }
        return selection;
    }

    const BackendChoice& chosen = selection.considered.at(evaluatedOffset + chosenIndex);
    const VirtualizationEnginePtr& engine = ordered.at(chosenIndex);

    selection.ok = true;
    selection.engine = engine;
    selection.backendId = chosen.backendId;
    selection.displayName = chosen.displayName;
    selection.tier = chosen.tier;
    selection.reason = chosen.reason;

    // `considered` may start with policy-excluded entries; the fallback flag is
    // computed against the candidates that were actually evaluated.
    if (chosenIndex > 0) {
        selection.fellBack = true;
        selection.fallbackFrom = selection.considered.at(evaluatedOffset).backendId;
    }
    if (selection.fellBack) {
        selection.reason = QStringLiteral("首选后端 %1 不可用，回退：%2")
                               .arg(selection.fallbackFrom, selection.reason);
    }
    if (!explicitReason.isEmpty()) {
        selection.reason = explicitReason + QStringLiteral(" ") + selection.reason;
    }
    return selection;
}

QVector<EngineProbeResult> VirtualizationEngineRegistry::describeBackends(const QString& guestArch) const {
    QVector<EngineProbeResult> results;
    for (const VirtualizationEnginePtr& engine : backendsInPreferenceOrder()) {
        results.append(probeBackend(engine, guestArch));
    }
    return results;
}

namespace {

QJsonArray issuesToJson(const QVector<EngineIssue>& issues) {
    QJsonArray array;
    for (const EngineIssue& issue : issues) {
        QJsonObject object;
        object.insert(QStringLiteral("level"), engineIssueLevelName(issue.level));
        object.insert(QStringLiteral("code"), issue.code);
        object.insert(QStringLiteral("message"), issue.message);
        array.append(object);
    }
    return array;
}

QJsonArray stringListToJson(const QStringList& values) {
    QJsonArray array;
    for (const QString& value : values) {
        array.append(value);
    }
    return array;
}

} // namespace

QString VirtualizationEngineRegistry::toJson(const VmConfig& config, const QString& guestArch) const {
    const HostProfile host = detectHostProfile();
    const QString resolvedGuestArch = guestArch.trimmed().isEmpty()
        ? canonicalGuestArch(config.architecture)
        : canonicalGuestArch(guestArch);

    QJsonObject root;
    root.insert(QStringLiteral("schema"), backendReportSchema());

    // Which platform backends are actually linked into this binary. Every field
    // is present on every platform so consumers can rely on a stable key set;
    // only the boolean values change.
    QJsonObject buildObject;
#if defined(VIRTUALWORLD_MACOS_BACKEND)
    buildObject.insert(QStringLiteral("macos_backend"), true);
#else
    buildObject.insert(QStringLiteral("macos_backend"), false);
#endif
#if defined(VIRTUALWORLD_HAVE_WINDOWS_BACKEND)
    buildObject.insert(QStringLiteral("windows_backend"), true);
#else
    buildObject.insert(QStringLiteral("windows_backend"), false);
#endif
#if defined(VIRTUALWORLD_HAVE_LINUX_KVM_BACKEND)
    buildObject.insert(QStringLiteral("linux_kvm_backend"), true);
#else
    buildObject.insert(QStringLiteral("linux_kvm_backend"), false);
#endif
#if defined(VIRTUALWORLD_HAVE_HARMONY_BACKEND)
    buildObject.insert(QStringLiteral("harmony_backend"), true);
#else
    buildObject.insert(QStringLiteral("harmony_backend"), false);
#endif
    buildObject.insert(QStringLiteral("qemu_fallback"), true);
    root.insert(QStringLiteral("build"), buildObject);

    QJsonObject hostObject;
    hostObject.insert(QStringLiteral("platform"), platformName(host.platform));
    hostObject.insert(QStringLiteral("family"), host.family);
    hostObject.insert(QStringLiteral("arch"), canonicalHostArch(host.arch));
    hostObject.insert(QStringLiteral("name"), host.name);
    root.insert(QStringLiteral("host"), hostObject);
    root.insert(QStringLiteral("guest_arch"), resolvedGuestArch);

    const QString preferenceText = resolveBackendPreference(QString(), config);
    root.insert(QStringLiteral("preference"), preferenceText);

    QJsonArray backendsArray;
    for (const VirtualizationEnginePtr& engine : backendsInPreferenceOrder()) {
        const EngineProbeResult probed = probeBackend(engine, resolvedGuestArch);
        QJsonObject object;
        object.insert(QStringLiteral("id"), probed.backendId.isEmpty() ? engine->backendId() : probed.backendId);
        object.insert(QStringLiteral("name"), probed.displayName.isEmpty() ? engine->displayName() : probed.displayName);
        object.insert(QStringLiteral("tier"), tierName(engine->tier()));
        // `tier` names the mechanism, `execution_tier` names what this guest
        // would really get on this host. They differ exactly where a backend
        // degrades (WHPX without a hypervisor, KVM without /dev/kvm), and that
        // difference is the honest answer a script should act on.
        object.insert(QStringLiteral("execution_tier"),
                      tierName(engine->executionTierFor(resolvedGuestArch)));
        object.insert(QStringLiteral("host_supported"), probed.capabilities.hostSupported);
        object.insert(QStringLiteral("guest_arch_supported"), probed.capabilities.guestArchSupported);
        object.insert(QStringLiteral("hardware_acceleration"), probed.capabilities.hardwareAcceleration);
        object.insert(QStringLiteral("native_execution"), probed.capabilities.nativeExecution);
        object.insert(QStringLiteral("accelerator"), probed.capabilities.accelerator);
        object.insert(QStringLiteral("summary"), probed.summary);
        const QString status = engine->statusCode(config);
        object.insert(QStringLiteral("status"), status);
        // `status` may be a backend-specific code (whpx-api-missing, degraded,
        // a validation issue code...). `status_class` always folds it back into
        // the canonical vocabulary, and is the field scripts should branch on.
        object.insert(QStringLiteral("status_class"), statusClass(status));
        object.insert(QStringLiteral("runs_in_process"), engine->runsInProcess());
        object.insert(QStringLiteral("requirements"), stringListToJson(engine->requirements()));
        object.insert(QStringLiteral("issues"), issuesToJson(probed.diagnostics));
        backendsArray.append(object);
    }
    root.insert(QStringLiteral("backends"), backendsArray);

    const EngineSelection selection = select(config, preferenceText);
    QJsonObject selectionObject;
    selectionObject.insert(QStringLiteral("ok"), selection.ok);
    selectionObject.insert(QStringLiteral("backend"), selection.backendId);
    selectionObject.insert(QStringLiteral("name"), selection.displayName);
    selectionObject.insert(QStringLiteral("tier"), tierName(selection.tier));
    // Same mechanism/achieved split as `backends[]`: the winning backend's
    // mechanism tier is `tier`, the tier this guest really gets on this host is
    // `execution_tier`.
    const VirtualizationEnginePtr selected = selection.ok ? backend(selection.backendId)
                                                          : VirtualizationEnginePtr();
    selectionObject.insert(QStringLiteral("execution_tier"),
                           selected ? tierName(selected->executionTierFor(resolvedGuestArch))
                                    : QString());
    selectionObject.insert(QStringLiteral("reason"), selection.reason);
    selectionObject.insert(QStringLiteral("fell_back"), selection.fellBack);
    selectionObject.insert(QStringLiteral("fallback_from"), selection.fallbackFrom);

    QJsonArray consideredArray;
    for (const BackendChoice& choice : selection.considered) {
        QJsonObject object;
        object.insert(QStringLiteral("id"), choice.backendId);
        object.insert(QStringLiteral("tier"), tierName(choice.tier));
        object.insert(QStringLiteral("host_supported"), choice.hostSupported);
        object.insert(QStringLiteral("guest_arch_supported"), choice.guestArchSupported);
        object.insert(QStringLiteral("config_accepted"), choice.configAccepted);
        object.insert(QStringLiteral("selected"), choice.selected);
        object.insert(QStringLiteral("status"), choice.statusCode);
        object.insert(QStringLiteral("reason"), choice.reason);
        consideredArray.append(object);
    }
    selectionObject.insert(QStringLiteral("considered"), consideredArray);
    root.insert(QStringLiteral("selection"), selectionObject);

    QJsonArray matrixArray;
    for (const SupportMatrixRow& row : defaultSupportMatrix()) {
        QJsonObject object;
        object.insert(QStringLiteral("host_platform"), row.hostPlatform);
        object.insert(QStringLiteral("host_arch"), row.hostArch);
        object.insert(QStringLiteral("guest_arch"), row.guestArch);
        object.insert(QStringLiteral("backend"), row.backendId);
        object.insert(QStringLiteral("tier"), tierName(row.tier));
        object.insert(QStringLiteral("status"), row.status);
        object.insert(QStringLiteral("verified"), row.verified);
        object.insert(QStringLiteral("note"), row.note);
        matrixArray.append(object);
    }
    root.insert(QStringLiteral("support_matrix"), matrixArray);

    QJsonDocument document(root);
    return QString::fromUtf8(document.toJson(QJsonDocument::Indented));
}

} // namespace vw
