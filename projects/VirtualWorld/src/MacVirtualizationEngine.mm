// MacVirtualizationEngine.mm — macOS native virtualization backend.
//
// Objective-C++ implementation of the platform layer declared in
// MacVirtualizationEngine.hpp. Everything here is compiled on macOS only; the
// header provides portable inline fallbacks for other platforms.
//
// Availability policy:
//   * All Virtualization.framework usage is guarded by `if (@available(...))`
//     blocks and the framework classes are annotated API_AVAILABLE.
//   * VZVirtualMachine / VZLinuxBootLoader / entropy / storage / network exist
//     from macOS 11.0. Directory sharing and the macOS-guest restore path are
//     12.0+.
//
// Memory policy:
//   * The file is written to compile with or without ARC. `VW_AUTORELEASE` and
//     `vwRetainObjc` / `vwReleaseObjc` hide the difference, and the `-dealloc`
//     override is compiled only when ARC is disabled. Every path is expected to
//     be leak-free under both settings.
//   * The session owns the runner (+1 handle) and the runner strongly owns the
//     VZVirtualMachine. VZVirtualMachine.delegate is a *weak* property, so no
//     cycle exists through the delegate. The only cycle is
//     session -> runner -> machine -> pending start block -> session, and it
//     disappears as soon as the start completion handler has run. `stop()` still
//     detaches and releases the runner so a finished VM cannot be kept alive by
//     a lingering session.
//
// Threading policy:
//   * Every VZVirtualMachine call is issued on the machine's own serial queue
//     (`vwRunOnVirtualizationQueue`), including delegate assignment and
//     start / requestStop / stop, because the framework documents that "every
//     operation on the virtual machine must be done on that queue".
//   * The asynchronous completion handlers always signal a mutex-protected
//     condition variable, and the synchronous `start()` / `stop()` adapters
//     never touch Objective-C objects from the waiting thread.
//   * `startAsync` / `stopAsync` never dereference the engine without holding
//     the shared async control block, so destroying the engine while an async
//     operation is still in flight is safe (see `AsyncControl`).

#include "MacVirtualizationEngine.hpp"

#if VW_MACVIRT_HAS_FRAMEWORK

#import <Foundation/Foundation.h>
#import <Security/Security.h>
#import <Virtualization/Virtualization.h>
#import <dispatch/dispatch.h>

#include <chrono>
#include <condition_variable>
#include <mutex>
#include <string>
#include <sys/sysctl.h>
#include <thread>

// ---------------------------------------------------------------------------
// ARC / MRC compatibility shims
// ---------------------------------------------------------------------------

#if __has_feature(objc_arc)
#define VW_AUTORELEASE(value) (value)
#define VW_UNBRIDGE(type, pointer) ((__bridge type)(pointer))
#else
#define VW_AUTORELEASE(value) [(value) autorelease]
#define VW_UNBRIDGE(type, pointer) ((type)(pointer))
#endif

namespace vw {
namespace macvirt {

// ---------------------------------------------------------------------------
// Small Objective-C <-> C++ helpers
// ---------------------------------------------------------------------------

static std::string vwToStd(NSString* value) {
    if (!value) {
        return {};
    }
    const char* utf8 = [value UTF8String];
    return utf8 ? std::string(utf8) : std::string();
}

static NSString* vwToNs(const std::string& value) {
    return [NSString stringWithUTF8String:value.c_str()] ?: @"";
}

static std::string vwErrorString(NSError* error) {
    if (!error) {
        return {};
    }
    return vwToStd(error.localizedDescription) + " (domain=" + vwToStd(error.domain)
        + ", code=" + std::to_string(static_cast<long long>(error.code)) + ")";
}

// ---------------------------------------------------------------------------
// Chaos seam: armed state for the counted stand-in runner.
//
// While armed, start()/startAsync() install VWMacVirtualizationRunnerStub (see
// the Objective-C section below) into Session::runnerRaw instead of the real
// framework runner, and stop() takes the synchronous branch instead of hopping
// onto the machine queue. Everything else — the ownership transfer, the detach,
// the release, the Session destructor — is the production path verbatim.
//
// The default is "not armed", so no production code path changes. The flag is
// atomic because a test may arm it from one thread while another observes the
// counters, and TSan must never be able to call this a data race.
// ---------------------------------------------------------------------------
static std::atomic<bool> g_runnerStubArmed{false};
static std::atomic<int> g_stubInstalled{0};
static std::atomic<int> g_stubDestroyed{0};

static bool vwRunnerStubEnabled() {
    return g_runnerStubArmed.load(std::memory_order_acquire);
}

// Retains an Objective-C object behind a type-erased handle. Works under both
// ARC and MRC; always balanced by vwReleaseObjc().
static void* vwRetainObjc(id object) {
    if (!object) {
        return nullptr;
    }
#if __has_feature(objc_arc)
    return const_cast<void*>(CFBridgingRetain(object));
#else
    return static_cast<void*>([object retain]);
#endif
}

static void vwReleaseObjc(void* handle) {
    if (!handle) {
        return;
    }
#if __has_feature(objc_arc)
    CFRelease(static_cast<CFTypeRef>(handle));
#else
    // MRC: the handle came from -retain, so it must be balanced with -release.
    // CFRelease() would only be valid for real CF types or toll-free-bridged
    // objects; VWMacVirtualizationRunner is a plain NSObject subclass.
    [(id)handle release];
#endif
}

// A single serial queue keeps every Virtualization.framework callback off the
// caller's thread, which is what makes the blocking adapters below safe.
//
// VZVirtualMachine documents that "every operation on the virtual machine must
// be done on that queue". The queue-specific key below lets the helpers detect
// re-entrancy so that hopping onto the queue never self-deadlocks when the code
// is already executing on it.
static char vwQueueKeyStorage;

static dispatch_queue_t vwVirtualizationQueue() {
    static dispatch_queue_t queue = nullptr;
    static std::once_flag flag;
    std::call_once(flag, []() {
        queue = dispatch_queue_create("com.liaoboxuan.virtualworld.macvirtualization",
                                      DISPATCH_QUEUE_SERIAL);
        dispatch_queue_set_specific(queue, &vwQueueKeyStorage, &vwQueueKeyStorage, nullptr);
    });
    return queue;
}

static bool vwOnVirtualizationQueueCurrent() {
    return dispatch_get_specific(&vwQueueKeyStorage) == &vwQueueKeyStorage;
}

// Runs `block` synchronously on the machine's serial queue unless the caller is
// already executing on it, in which case the block must run inline to avoid a
// self-deadlock on that serial queue.
static void vwRunOnVirtualizationQueue(dispatch_block_t block) {
    if (!block) {
        return;
    }
    if (vwOnVirtualizationQueueCurrent()) {
        block();
        return;
    }
    dispatch_sync(vwVirtualizationQueue(), block);
}

// ---------------------------------------------------------------------------
// Host detection
// ---------------------------------------------------------------------------

static bool vwHostIsAppleSilicon() {
#if defined(__arm64__) || defined(__aarch64__)
    return true;
#else
    int value = 0;
    size_t size = sizeof(value);
    if (sysctlbyname("hw.optional.arm64", &value, &size, nullptr, 0) == 0) {
        return value != 0;
    }
    return false;
#endif
}

static bool vwSysctlBool(const char* name, bool* value) {
    int raw = 0;
    size_t size = sizeof(raw);
    if (sysctlbyname(name, &raw, &size, nullptr, 0) != 0) {
        return false;
    }
    if (value) {
        *value = raw != 0;
    }
    return true;
}

// Reports whether the process is sandboxed and whether it declares the
// com.apple.security.virtualization entitlement. Non-sandboxed processes do not
// need the entitlement, but we still surface the result for diagnostics.
static void vwDetectSandboxEntitlement(bool* sandboxed, bool* hasEntitlement) {
    if (sandboxed) {
        *sandboxed = false;
    }
    if (hasEntitlement) {
        *hasEntitlement = false;
    }
    SecTaskRef task = SecTaskCreateFromSelf(kCFAllocatorDefault);
    if (!task) {
        return;
    }
    CFTypeRef sandboxValue =
        SecTaskCopyValueForEntitlement(task, CFSTR("com.apple.security.app-sandbox"), nullptr);
    if (sandboxValue) {
        if (CFGetTypeID(sandboxValue) == CFBooleanGetTypeID() && sandboxed) {
            *sandboxed = CFBooleanGetValue(static_cast<CFBooleanRef>(sandboxValue));
        }
        CFRelease(sandboxValue);
    }
    CFTypeRef virtualizationValue =
        SecTaskCopyValueForEntitlement(task, CFSTR("com.apple.security.virtualization"), nullptr);
    if (virtualizationValue) {
        if (CFGetTypeID(virtualizationValue) == CFBooleanGetTypeID() && hasEntitlement) {
            *hasEntitlement = CFBooleanGetValue(static_cast<CFBooleanRef>(virtualizationValue));
        }
        CFRelease(virtualizationValue);
    }
    CFRelease(task);
}

Host currentHost() {
    Host host;
    host.macOS = true;
    host.arch = vwHostIsAppleSilicon() ? "aarch64" : "x86_64";
    host.appleSilicon = vwHostIsAppleSilicon();

    NSOperatingSystemVersion version = [[NSProcessInfo processInfo] operatingSystemVersion];
    host.macOSMajor = static_cast<int>(version.majorVersion);
    host.macOSMinor = static_cast<int>(version.minorVersion);
    host.osVersion = vwToStd([[NSProcessInfo processInfo] operatingSystemVersionString]);

    host.virtualizationFrameworkAvailable = false;
    if (@available(macOS 11.0, *)) {
        host.virtualizationFrameworkAvailable =
            NSClassFromString(@"VZVirtualMachine") != nil
            && NSClassFromString(@"VZVirtualMachineConfiguration") != nil
            && NSClassFromString(@"VZLinuxBootLoader") != nil;
    }

    NSBundle* frameworkBundle = [NSBundle bundleWithIdentifier:@"com.apple.Virtualization"];
    if (frameworkBundle) {
        host.frameworkVersion =
            vwToStd(frameworkBundle.infoDictionary[@"CFBundleShortVersionString"]);
    }
    if (host.frameworkVersion.empty()) {
        host.frameworkVersion = "unknown";
    }

    bool nested = false;
    if (vwSysctlBool("kern.hv_vmm_present", &nested)) {
        host.virtualizingNested = nested;
    }

    vwDetectSandboxEntitlement(&host.sandboxed, &host.hasVirtualizationEntitlement);
    return host;
}

// ---------------------------------------------------------------------------
// Session bookkeeping shared with the delegate
// ---------------------------------------------------------------------------

struct Session {
    std::mutex mutex;
    std::condition_variable cv;
    bool finished = false;
    RunResult result;
    std::atomic<int> state{static_cast<int>(State::NotStarted)};
    void* runnerRaw = nullptr;  // +1 VWMacVirtualizationRunner, released in dtor

    ~Session() {
        vwReleaseObjc(runnerRaw);
        runnerRaw = nullptr;
    }
};

// Lifetime guard shared between the engine and its async workers.
//
// The engine owns a shared_ptr<AsyncControl>; every async worker takes its own
// copy *before* the thread starts. That means the mutex below stays alive even
// if the engine itself is destroyed, so a worker can always lock it safely.
//
// Protocol (this is what makes destroying the engine during an in-flight async
// operation safe):
//   * A worker only dereferences the engine while holding `mutex` AND after
//     observing `abandoned == false`.
//   * The engine destructor takes `mutex`, sets `abandoned = true`, and only
//     then returns. After that point no worker can reach the engine again.
//   * The engine's own start/stop bookkeeping takes the same mutex, so a worker
//     can never observe a half-updated session_ / stateValue_.
struct AsyncControl {
    std::mutex mutex;
    bool abandoned = false;  // set by ~MacVirtualizationEngine
    bool pending = false;    // an async start is in flight
};

static std::shared_ptr<AsyncControl> vwControl(const std::shared_ptr<void>& handle) {
    return std::static_pointer_cast<AsyncControl>(handle);
}

// Returns the engine's control block, creating it on first use. The constructor
// already installs one, so the lazy path only guards against a default-built
// engine (which the portable fallbacks in the header rely on).
static std::shared_ptr<AsyncControl> vwRequireControl(std::shared_ptr<void>& slot) {
    std::shared_ptr<AsyncControl> control = std::static_pointer_cast<AsyncControl>(slot);
    if (!control) {
        control = std::make_shared<AsyncControl>();
        slot = control;
    }
    return control;
}

static bool vwStateIsBusy(int rawState) {
    const State state = static_cast<State>(rawState);
    return state == State::Starting || state == State::Running || state == State::Paused;
}

} // namespace macvirt
} // namespace vw

// ---------------------------------------------------------------------------
// Chaos seam: the counted stand-in for VWMacVirtualizationRunner.
//
// It is instantiated only while MacVirtualizationEngine::setRunnerStubForTesting()
// has armed the seam, and never in production. Because it is an ordinary
// Objective-C object it goes through exactly the same vwRetainObjc() /
// vwReleaseObjc() pair as the real runner, and its dealloc is counted — yet it
// is never handed to Virtualization.framework and no guest is ever booted. That
// combination is what lets the runnerRaw ownership contract be asserted exactly:
// installs must equal destructions, always.
// ---------------------------------------------------------------------------

// Forward-declared at file scope: an Objective-C implementation cannot be nested
// in a C++ namespace, and this one has to reach the counter above.
static void vwNoteStubDestroyed();

static void vwNoteStubDestroyed() {
    vw::macvirt::g_stubDestroyed.fetch_add(1, std::memory_order_relaxed);
}

@interface VWMacVirtualizationRunnerStub : NSObject
@end

@implementation VWMacVirtualizationRunnerStub
- (void)dealloc {
    vwNoteStubDestroyed();
#if !__has_feature(objc_arc)
    // MRC: the object was retained by vwRetainObjc(), so the chain must still
    // reach NSObject. Under ARC, calling [super dealloc] is a compile error.
    [super dealloc];
#endif
}
@end

// ---------------------------------------------------------------------------
// Objective-C runner / delegate
// ---------------------------------------------------------------------------

API_AVAILABLE(macos(11.0))
@interface VWMacVirtualizationRunner : NSObject <VZVirtualMachineDelegate>
@property (nonatomic, strong) VZVirtualMachine* machine;
- (void)bindSession:(vw::macvirt::Session*)session;
- (void)attachMachine:(VZVirtualMachine*)machine;
- (void)detachMachine;
- (void)requestStopWithCompletion:(void (^)(NSError* _Nullable error))completion;
@end

API_AVAILABLE(macos(11.0))
@implementation VWMacVirtualizationRunner {
    vw::macvirt::Session* _session;  // non-owning; lifetime owned by the C++ side
}

- (void)bindSession:(vw::macvirt::Session*)session {
    _session = session;
}

- (void)attachMachine:(VZVirtualMachine*)machine {
    self.machine = machine;
}

- (void)detachMachine {
    self.machine.delegate = nil;
    self.machine = nil;
}

- (void)requestStopWithCompletion:(void (^)(NSError* _Nullable error))completion {
    VZVirtualMachine* machine = self.machine;
    if (!machine) {
        if (completion) {
            completion(nil);
        }
        return;
    }
    if (@available(macOS 12.0, *)) {
        [machine stopWithCompletionHandler:completion];
        return;
    }
    NSError* error = nil;
    if (![machine requestStopWithError:&error]) {
        if (completion) {
            completion(error);
        }
    } else if (completion) {
        completion(nil);
    }
}

- (void)virtualMachine:(VZVirtualMachine*)virtualMachine didStopWithError:(NSError*)error {
    (void)virtualMachine;
    vw::macvirt::Session* session = _session;
    if (!session) {
        return;
    }
    std::lock_guard<std::mutex> lock(session->mutex);
    session->state.store(static_cast<int>(vw::macvirt::State::Error));
    session->result = vw::macvirt::RunResult::failure(
        "guest-stopped-with-error", "客户机异常停止。", vw::macvirt::vwErrorString(error));
    session->finished = true;
    session->cv.notify_all();
}

- (void)guestDidStopVirtualMachine:(VZVirtualMachine*)virtualMachine {
    (void)virtualMachine;
    vw::macvirt::Session* session = _session;
    if (!session) {
        return;
    }
    std::lock_guard<std::mutex> lock(session->mutex);
    session->state.store(static_cast<int>(vw::macvirt::State::Stopped));
    session->result = vw::macvirt::RunResult::success("客户机已停止。");
    session->finished = true;
    session->cv.notify_all();
}

#if !__has_feature(objc_arc)
- (void)dealloc {
    [_machine release];
    [super dealloc];
}
#endif

@end

namespace vw {
namespace macvirt {

// ---------------------------------------------------------------------------
// Configuration construction
// ---------------------------------------------------------------------------

// Builds a VZVirtualMachineConfiguration for a Linux guest. Returns nil and
// fills `error` when a device cannot be created. Caller must be inside an
// @autoreleasepool and the macOS 11.0 availability domain.
static VZVirtualMachineConfiguration* vwBuildLinuxConfiguration(const Config& config,
                                                                NSError** error) API_AVAILABLE(macos(11.0));
static VZVirtualMachineConfiguration* vwBuildLinuxConfiguration(const Config& config,
                                                                NSError** error) {
    VZVirtualMachineConfiguration* configuration =
        VW_AUTORELEASE([[VZVirtualMachineConfiguration alloc] init]);

    VZLinuxBootLoader* bootLoader = VW_AUTORELEASE(
        [[VZLinuxBootLoader alloc] initWithKernelURL:[NSURL fileURLWithPath:vwToNs(config.kernelPath)]]);
    if (!config.initrdPath.empty()) {
        bootLoader.initialRamdiskURL = [NSURL fileURLWithPath:vwToNs(config.initrdPath)];
    }
    if (!config.kernelCommandLine.empty()) {
        bootLoader.commandLine = vwToNs(config.kernelCommandLine);
    }
    configuration.bootLoader = bootLoader;
    configuration.CPUCount = static_cast<NSUInteger>(config.cpuCount);
    configuration.memorySize = static_cast<uint64_t>(config.memoryMb) * 1024ULL * 1024ULL;
    configuration.entropyDevices = @[ VW_AUTORELEASE([[VZVirtioEntropyDeviceConfiguration alloc] init]) ];

    NSMutableArray<VZStorageDeviceConfiguration*>* storage = [NSMutableArray array];
    for (const std::string& diskPath : config.diskPaths) {
        NSError* attachmentError = nil;
        VZDiskImageStorageDeviceAttachment* attachment = VW_AUTORELEASE(
            [[VZDiskImageStorageDeviceAttachment alloc] initWithURL:[NSURL fileURLWithPath:vwToNs(diskPath)]
                                                          readOnly:NO
                                                             error:&attachmentError]);
        if (!attachment) {
            if (error) {
                *error = attachmentError;
            }
            return nil;
        }
        [storage addObject:VW_AUTORELEASE([[VZVirtioBlockDeviceConfiguration alloc] initWithAttachment:attachment])];
    }
    configuration.storageDevices = storage;

    if (config.networkEnabled) {
        VZVirtioNetworkDeviceConfiguration* network =
            VW_AUTORELEASE([[VZVirtioNetworkDeviceConfiguration alloc] init]);
        network.attachment = VW_AUTORELEASE([[VZNATNetworkDeviceAttachment alloc] init]);
        network.MACAddress = [VZMACAddress randomLocallyAdministeredAddress];
        configuration.networkDevices = @[ network ];
    }

    if (@available(macOS 12.0, *)) {
        if (config.directorySharingEnabled && !config.sharedDirectoryPath.empty()) {
            VZSharedDirectory* shared = VW_AUTORELEASE(
                [[VZSharedDirectory alloc] initWithURL:[NSURL fileURLWithPath:vwToNs(config.sharedDirectoryPath)]
                                              readOnly:NO]);
            VZSingleDirectoryShare* share =
                VW_AUTORELEASE([[VZSingleDirectoryShare alloc] initWithDirectory:shared]);
            VZVirtioFileSystemDeviceConfiguration* fileSystem = VW_AUTORELEASE(
                [[VZVirtioFileSystemDeviceConfiguration alloc] initWithTag:@"share"]);
            fileSystem.share = share;
            configuration.directorySharingDevices = @[ fileSystem ];
        }
    }

    return configuration;
}

// ---------------------------------------------------------------------------
// Validation helpers (filesystem existence)
// ---------------------------------------------------------------------------

static void vwRequireRegularFile(std::vector<Issue>& issues,
                                 const std::string& path,
                                 const char* code,
                                 const char* label) {
    if (path.empty()) {
        return;
    }
    NSFileManager* manager = [NSFileManager defaultManager];
    NSString* nsPath = vwToNs(path);
    BOOL isDirectory = NO;
    const BOOL exists = [manager fileExistsAtPath:nsPath isDirectory:&isDirectory];
    if (!exists || isDirectory) {
        issues.push_back({IssueLevel::Error,
                          code,
                          std::string(label) + "不存在或不是普通文件：" + path});
    }
}

static void vwRequireDirectory(std::vector<Issue>& issues,
                               const std::string& path,
                               const char* code,
                               const char* label) {
    if (path.empty()) {
        return;
    }
    NSFileManager* manager = [NSFileManager defaultManager];
    BOOL isDirectory = NO;
    const BOOL exists = [manager fileExistsAtPath:vwToNs(path) isDirectory:&isDirectory];
    if (!exists || !isDirectory) {
        issues.push_back({IssueLevel::Error,
                          code,
                          std::string(label) + "不存在或不是目录：" + path});
    }
}

// ---------------------------------------------------------------------------
// Validation: policy + filesystem existence
// ---------------------------------------------------------------------------

static std::vector<Issue> vwValidateWithFilesystem(const Config& config, const Host& host) {
    std::vector<Issue> issues = validateConfig(config, host);
    vwRequireRegularFile(issues, config.kernelPath, "kernel-missing", "内核镜像");
    vwRequireRegularFile(issues, config.initrdPath, "initrd-missing", "initrd 镜像");
    vwRequireRegularFile(issues, config.ipswPath, "ipsw-missing", "IPSW 恢复镜像");
    for (const std::string& diskPath : config.diskPaths) {
        vwRequireRegularFile(issues, diskPath, "disk-missing", "磁盘镜像");
    }
    if (config.directorySharingEnabled) {
        vwRequireDirectory(issues, config.sharedDirectoryPath, "shared-directory-missing", "共享目录");
    }
    return issues;
}

// ---------------------------------------------------------------------------
// Preflight (shared by start and startAsync)
// ---------------------------------------------------------------------------
//
// Order matters: host-level and capability failures are reported with their own
// specific codes (so the upper layer can fall back to QEMU), while field-level
// problems fall through to the generic `config-invalid` result with issues.

struct Preflight {
    bool proceed = false;
    RunResult failure;
    Host host;
};

static Preflight vwPreflight(const Config& config) {
    Preflight result;
    result.host = currentHost();

    if (!result.host.macOS) {
        result.failure = RunResult::failure("host-not-macos",
                                            "Apple Virtualization.framework 后端仅在 macOS 上可用。");
        return result;
    }
    if (result.host.macOSMajor < 11) {
        result.failure = RunResult::failure("macos-11-required",
                                            "Virtualization.framework 需要 macOS 11 或更高版本。");
        return result;
    }
    if (!result.host.virtualizationFrameworkAvailable) {
        result.failure = RunResult::failure("framework-unavailable",
                                            "当前进程无法加载 Virtualization.framework。");
        return result;
    }
    if (result.host.sandboxed && !result.host.hasVirtualizationEntitlement) {
        result.failure = RunResult::failure(
            "sandbox-entitlement-missing",
            "进程已沙盒化但缺少 com.apple.security.virtualization 权限，无法创建虚拟机。");
        return result;
    }

    const GuestKind kind = classifyGuest(config);
    const Capabilities caps = evaluateCapabilities(result.host);

    if (kind == GuestKind::Unknown) {
        result.failure = RunResult::failure(
            "guest-os-unsupported",
            "Virtualization.framework 后端不支持 guestOs=\"" + config.guestOs + "\"；请回退到 QEMU。");
        return result;
    }
    if (kind == GuestKind::Linux) {
        if (!caps.linuxGuest.supported) {
            result.failure = RunResult::failure("linux-guest-unsupported",
                                                caps.linuxGuest.reason,
                                                "capability=" + caps.linuxGuest.code);
            return result;
        }
        const std::string guestArch = normalizeArch(config.architecture);
        if (!guestArch.empty() && !hostSupportsGuestArch(guestArch, result.host)) {
            result.failure = RunResult::failure(
                "guest-arch-unsupported",
                "宿主 " + normalizeArch(result.host.arch) + " 无法通过 Virtualization.framework 原生虚拟化 "
                    + guestArch + " 客户机；请回退到 QEMU TCG。",
                "host=" + normalizeArch(result.host.arch) + ", guest=" + guestArch);
            return result;
        }
    } else if (kind == GuestKind::MacOSIPSW && !caps.macOSGuestIPSW.supported) {
        result.failure = RunResult::failure("macos-guest-unsupported",
                                            caps.macOSGuestIPSW.reason,
                                            "capability=" + caps.macOSGuestIPSW.code);
        return result;
    }

    std::vector<Issue> issues = vwValidateWithFilesystem(config, result.host);
    if (hasErrors(issues)) {
        result.failure = RunResult::failure("config-invalid",
                                            "配置校验失败。",
                                            joinIssueDetails(issues));
        result.failure.issues = std::move(issues);
        return result;
    }

    if (kind == GuestKind::MacOSIPSW) {
        // Honest unsupported: validation and capability probing are complete, but
        // the restore/install pipeline is intentionally not faked. The upper
        // layer must fall back to QEMU.
        result.failure = RunResult::failure(
            "macos-ipsw-restore-unsupported",
            "macOS 客户机 IPSW 恢复/安装流程尚未在本后端接线，请回退到 QEMU。",
            "capability=" + caps.macOSGuestIPSW.code);
        return result;
    }

    result.proceed = true;
    return result;
}

// ---------------------------------------------------------------------------
// Real Linux boot
// ---------------------------------------------------------------------------

static RunResult vwWaitForSession(const std::shared_ptr<Session>& session,
                                  std::chrono::seconds timeout) {
    std::unique_lock<std::mutex> lock(session->mutex);
    const bool signaled = session->cv.wait_for(lock, timeout, [&session]() {
        return session->finished;
    });
    if (!signaled) {
        return RunResult::failure("timeout", "等待 Virtualization.framework 回调超时。");
    }
    return session->result;
}

// Defined further down with the rest of the runner helpers; declared here because
// both vwStartLinuxVm() and the async workers need them.
API_AVAILABLE(macos(11.0))
static void vwDetachRunnerHandle(void* handle);
static RunResult vwStopSession(const std::shared_ptr<Session>& session);

static RunResult vwStartLinuxVm(const Config& config, std::shared_ptr<Session>* outSession) {
    std::shared_ptr<Session> session = std::make_shared<Session>();
    session->state.store(static_cast<int>(State::Starting));

    // Chaos path. While the stub seam is armed, install the counted stand-in and
    // report success without touching Virtualization.framework at all. That makes
    // the ownership contract around Session::runnerRaw — the field whose unlocked
    // read/write caused a double release and a dangling pointer — exercisable
    // deterministically and in bounded time, with no guest and no framework.
    //
    // The install mirrors the real path exactly: the +1 reference lives in the
    // field, and everything after this point (stop transfer, detach, release,
    // ~Session) is unmodified production code.
    if (vwRunnerStubEnabled()) {
        static const char kStubStarted[] = "macOS 原生虚拟机已启动（测试替身，未接触 Virtualization.framework）。";
        // Unlike the real runner, the stand-in carries no autorelease: its +1 is
        // exactly the explicit ownership that Session::runnerRaw holds, so the
        // installed/destroyed counters stay in lock-step with the release. Under
        // ARC the local is released by the compiler at scope end; under MRC we
        // balance the alloc's +1 by hand. Either way the only remaining +1 is the
        // CFRetain stored in runnerRaw, released by vwReleaseObjc() exactly once.
        VWMacVirtualizationRunnerStub* stub =
            [[VWMacVirtualizationRunnerStub alloc] init];
        {
            std::lock_guard<std::mutex> lock(session->mutex);
            session->runnerRaw = vwRetainObjc(stub);
        }
#if !__has_feature(objc_arc)
        [stub release];
#endif
        session->state.store(static_cast<int>(State::Running));
        session->result = RunResult::success(kStubStarted);
        session->finished = true;
        session->cv.notify_all();
        g_stubInstalled.fetch_add(1, std::memory_order_relaxed);
        if (outSession) {
            *outSession = session;
        }
        return RunResult::success(kStubStarted);
    }

    RunResult result;
    if (@available(macOS 11.0, *)) {
        @autoreleasepool {
            NSError* buildError = nil;
            VZVirtualMachineConfiguration* configuration =
                vwBuildLinuxConfiguration(config, &buildError);
            if (!configuration) {
                session->state.store(static_cast<int>(State::Error));
                return RunResult::failure("configuration-build-failed",
                                          "无法构建 Virtualization.framework 配置。",
                                          vwErrorString(buildError));
            }

            NSError* validationError = nil;
            if (![configuration validateWithError:&validationError]) {
                session->state.store(static_cast<int>(State::Error));
                return RunResult::failure("framework-validation-failed",
                                          "Virtualization.framework 拒绝了该配置。",
                                          vwErrorString(validationError));
            }

            VWMacVirtualizationRunner* runner =
                VW_AUTORELEASE([[VWMacVirtualizationRunner alloc] init]);
            [runner bindSession:session.get()];
            VZVirtualMachine* machine = VW_AUTORELEASE(
                [[VZVirtualMachine alloc] initWithConfiguration:configuration
                                                          queue:vwVirtualizationQueue()]);
            [runner attachMachine:machine];
            // Published under the session mutex: vwStopSession() and ~Session()
            // are the only other readers of this field and both take the lock.
            {
                std::lock_guard<std::mutex> lock(session->mutex);
                session->runnerRaw = vwRetainObjc(runner);
            }

            // The framework requires delegate assignment and start to be issued
            // on the machine's queue. Both calls return immediately, so running
            // them synchronously on that queue costs nothing beyond the hand-off.
            vwRunOnVirtualizationQueue(^{
              machine.delegate = runner;
              [machine startWithCompletionHandler:^(NSError* error) {
                std::lock_guard<std::mutex> lock(session->mutex);
                if (error) {
                    session->state.store(static_cast<int>(State::Error));
                    session->result = RunResult::failure("start-failed",
                                                         "虚拟机启动失败。",
                                                         vwErrorString(error));
                } else {
                    session->state.store(static_cast<int>(State::Running));
                    session->result = RunResult::success("macOS 原生虚拟机已启动。");
                }
                session->finished = true;
                session->cv.notify_all();
              }];
            });

            result = vwWaitForSession(session, std::chrono::seconds(120));
            if (!result.ok) {
                session->state.store(static_cast<int>(State::Error));
                if (result.code == "timeout") {
                    // A pending framework callback may still be in flight. Do not
                    // release the runner/machine here: the block keeps the session
                    // alive and unwinds once the callback finally fires. This is
                    // an intentionally bounded leak on a pathological hang.
                    return result;
                }
                // Same hand-off rule as vwStopSession(): take a +1 local copy of
                // the runner under the session mutex and clear the field, so the
                // Session destructor cannot release it while the framework is
                // still unwinding from the rejected start.
                void* owned = nullptr;
                {
                    std::lock_guard<std::mutex> lock(session->mutex);
                    // Ownership transfer, not a copy: the field already carries
                    // the +1 reference and clearing it makes `owned` the only
                    // holder, which is exactly what keeps the runner alive for
                    // the detach below.
                    owned = session->runnerRaw;
                    session->runnerRaw = nullptr;
                }
                vwDetachRunnerHandle(owned);
                if (owned) {
                    vwReleaseObjc(owned);
                }
                return result;
            }
        }
    } else {
        session->state.store(static_cast<int>(State::Error));
        return RunResult::failure("framework-unavailable",
                                  "Virtualization.framework 需要 macOS 11 或更高版本。");
    }

    if (outSession) {
        *outSession = session;
    }
    return result;
}

// ---------------------------------------------------------------------------
// Engine implementation
// ---------------------------------------------------------------------------

MacVirtualizationEngine::MacVirtualizationEngine()
    : asyncControl_(std::make_shared<AsyncControl>()) {}

MacVirtualizationEngine::~MacVirtualizationEngine() {
    // Block every async worker from touching this engine from here on. Workers
    // hold their own reference to the same control block, so they can still take
    // the mutex safely; they just observe `abandoned` and stay away.
    const std::shared_ptr<AsyncControl> control = vwControl(asyncControl_);
    if (control) {
        std::lock_guard<std::mutex> lock(control->mutex);
        control->abandoned = true;
    }

    // With the workers excluded, session_ can be inspected and torn down without
    // the control lock.
    if (session_) {
        (void)stop();
    }
}

const char* MacVirtualizationEngine::backendIdentifier() {
    return "macos-virtualization";
}

const char* MacVirtualizationEngine::backendDisplayName() {
    return "Apple Virtualization.framework";
}

bool MacVirtualizationEngine::isHostSupported() {
    return probeHost(currentHost()).hostSupported;
}

bool MacVirtualizationEngine::supportsGuestArch(const std::string& guestArch) {
    return hostSupportsGuestArch(guestArch, currentHost());
}

bool MacVirtualizationEngine::isHostSupported(std::string* reason) {
    return describeHostSupport(currentHost(), reason);
}

bool MacVirtualizationEngine::supportsGuestArch(const std::string& guestArch,
                                                std::string* reason) {
    return describeGuestArchSupport(guestArch, currentHost(), reason);
}

ProbeResult MacVirtualizationEngine::probe() {
    return probeHost(currentHost());
}

Capabilities MacVirtualizationEngine::capabilities() const {
    return evaluateCapabilities(currentHost());
}

Host MacVirtualizationEngine::hostProfile() const {
    return currentHost();
}

State MacVirtualizationEngine::state() const {
    return static_cast<State>(stateValue_.load());
}

std::vector<Issue> MacVirtualizationEngine::validate(const Config& config) const {
    return vwValidateWithFilesystem(config, currentHost());
}

RunResult MacVirtualizationEngine::start(const Config& config) {
    Preflight preflight = vwPreflight(config);
    if (!preflight.proceed) {
        return preflight.failure;
    }

    // Claim the single VM slot. This also rejects a start that races with an
    // in-flight startAsync(), which the bare stateValue_ check cannot see yet.
    const std::shared_ptr<AsyncControl> control = vwRequireControl(asyncControl_);
    StartGate gate;
    {
        std::lock_guard<std::mutex> lock(control->mutex);
        if (control->pending || vwStateIsBusy(stateValue_.load()) || session_) {
            return RunResult::failure("already-running", "已有 macOS 原生虚拟机在运行。");
        }
        control->pending = true;
        gate = startGate_;
    }

    std::shared_ptr<Session> session;
    RunResult result;
    RunResult aborted;
    if (gate && !gate(config, &aborted)) {
        result = aborted.code.empty()
            ? RunResult::failure("start-gate-aborted", "启动被测试闸门中止，未接触 Virtualization.framework。")
            : aborted;
    } else {
        result = vwStartLinuxVm(config, &session);
    }
    const bool started = result.ok && session != nullptr;

    {
        std::lock_guard<std::mutex> lock(control->mutex);
        control->pending = false;
        if (started) {
            session_ = session;
        }
        stateValue_.store(static_cast<int>(started ? State::Running : State::Error));
    }
    return result;
}

void MacVirtualizationEngine::setStartGateForTesting(StartGate gate) {
    const std::shared_ptr<AsyncControl> control = vwRequireControl(asyncControl_);
    std::lock_guard<std::mutex> lock(control->mutex);
    startGate_ = std::move(gate);
}

void MacVirtualizationEngine::setRunnerStubForTesting(bool enabled) {
    g_runnerStubArmed.store(enabled, std::memory_order_release);
}

int MacVirtualizationEngine::runnerStubInstalled() const {
    return g_stubInstalled.load(std::memory_order_acquire);
}

int MacVirtualizationEngine::runnerStubDestroyed() const {
    return g_stubDestroyed.load(std::memory_order_acquire);
}

void MacVirtualizationEngine::resetRunnerStubCountersForTesting() {
    g_stubInstalled.store(0, std::memory_order_release);
    g_stubDestroyed.store(0, std::memory_order_release);
}

void MacVirtualizationEngine::startAsync(const Config& config,
                                         std::function<void(const RunResult&)> completion) {
    Preflight preflight = vwPreflight(config);
    if (!preflight.proceed) {
        // Validation/capability failures are reported synchronously: they carry
        // actionable diagnostics and never touch the framework.
        if (completion) {
            completion(preflight.failure);
        }
        return;
    }

    // Claim the async slot *before* spawning the worker, so two concurrent
    // startAsync() calls cannot both build a VM.
    const std::shared_ptr<AsyncControl> control = vwRequireControl(asyncControl_);
    StartGate gate;
    {
        std::lock_guard<std::mutex> lock(control->mutex);
        if (control->pending || vwStateIsBusy(stateValue_.load()) || session_) {
            if (completion) {
                completion(RunResult::failure("already-running", "已有 macOS 原生虚拟机在运行。"));
            }
            return;
        }
        control->pending = true;
        // Copied out deliberately: the worker must be able to run the gate without
        // dereferencing the engine, because the engine may be destroyed while the
        // gate is still executing.
        gate = startGate_;
    }

    // Only the blocking framework handshake is moved off the caller's thread.
    // The worker owns an AsyncControl reference, so it can lock the guard even if
    // the engine dies first, and it only touches `this` while holding that lock
    // with `abandoned` observed as false.
    std::thread([this, control, config, completion, gate]() {
        std::shared_ptr<Session> session;
        RunResult result;
        RunResult aborted;
        if (gate && !gate(config, &aborted)) {
            result = aborted.code.empty()
                ? RunResult::failure("start-gate-aborted",
                                     "启动被测试闸门中止，未接触 Virtualization.framework。")
                : aborted;
        } else {
            result = vwStartLinuxVm(config, &session);
        }

        bool abandoned = false;
        {
            std::lock_guard<std::mutex> lock(control->mutex);
            control->pending = false;
            abandoned = control->abandoned;
            if (!abandoned) {
                const bool started = result.ok && session != nullptr;
                if (started) {
                    session_ = session;
                }
                stateValue_.store(static_cast<int>(started ? State::Running : State::Error));
            }
        }

        if (abandoned) {
            // The engine was destroyed while this start was in flight. Its owner
            // can no longer observe or stop the guest, so roll the VM back here
            // rather than leaking a running VM, and say so explicitly.
            if (session) {
                (void)vwStopSession(session);
            }
            if (completion) {
                completion(RunResult::failure("engine-destroyed",
                                              "引擎在启动完成前被销毁，已回滚该虚拟机。"));
            }
            return;
        }

        if (completion) {
            completion(result);
        }
    }).detach();
}

// Availability-isolated helpers for the runner handle. VWMacVirtualizationRunner
// itself is macOS 11+, so these helpers are annotated and their callers must sit
// inside an @available(macOS 11.0, *) block.

API_AVAILABLE(macos(11.0))
static bool vwRequestRunnerStop(void* handle, const std::shared_ptr<Session>& session) {
    // While the chaos seam is armed the handle is a test stand-in that does not
    // implement the framework's stop protocol. Reporting "not requested" makes
    // vwStopSession() take its synchronous branch instead, which keeps the stress
    // loop bounded and deterministic; the ownership arithmetic below is unchanged.
    if (vwRunnerStubEnabled()) {
        return false;
    }
    VWMacVirtualizationRunner* runner = VW_UNBRIDGE(VWMacVirtualizationRunner*, handle);
    if (!runner) {
        return false;
    }
    // VZVirtualMachine operations must be issued on the machine's own queue, so
    // the runner hops onto it. stopWithCompletionHandler: returns immediately, so
    // the hand-off does not block this thread until the VM has actually stopped.
    vwRunOnVirtualizationQueue(^{
      [runner requestStopWithCompletion:^(NSError* error) {
        std::lock_guard<std::mutex> lock(session->mutex);
        if (error) {
            session->state.store(static_cast<int>(State::Error));
            session->result = RunResult::failure("stop-failed",
                                                 "虚拟机停止失败。",
                                                 vwErrorString(error));
        } else {
            session->state.store(static_cast<int>(State::Stopped));
            session->result = RunResult::success("macOS 原生虚拟机已停止。");
        }
        session->finished = true;
        session->cv.notify_all();
      }];
    });
    return true;
}

API_AVAILABLE(macos(11.0))
static void vwDetachRunnerHandle(void* handle) {
    if (!handle) {
        return;
    }
    // The stand-in has no machine to detach. Skipping the hop keeps the seam from
    // dereferencing an object the framework never saw.
    if (vwRunnerStubEnabled()) {
        return;
    }
    VWMacVirtualizationRunner* runner = VW_UNBRIDGE(VWMacVirtualizationRunner*, handle);
    vwRunOnVirtualizationQueue(^{
      [runner detachMachine];
    });
}

// Stops `session` and returns the outcome. This never touches the engine, which
// is what makes it safe to call from an async worker whose engine may be gone.
//
// Ownership rule for `Session::runnerRaw`, now enforced at every access site:
//   * It is written, cleared or read only while holding `session->mutex`.
//   * A function that must hand the runner to the framework (or to the dispatch
//     queue) *transfers* the +1 reference into a local under that mutex and
//     immediately clears the field. The local becomes the sole owner, so ~Session
//     cannot release the same reference a second time and a concurrent framework
//     callback can never see a torn pointer.
//
// Before this, vwStopSession() read runnerRaw unlocked while vwStartLinuxVm()
// wrote it and ~Session() released it, so a stop racing a failed start could
// over-release the Objective-C runner (refcount underflow) and a stop racing
// session teardown could hand a dangling pointer to vwDetachRunnerHandle().
//
// The reference in `owned` is deliberately not released when vwWaitForSession()
// times out: the stop callback may still be in flight on the machine's queue, so
// releasing here could race it. That is the same intentionally bounded leak on a
// pathological hang that vwStartLinuxVm() documents, and it is what keeps the
// session -> runner -> machine chain from being torn down underneath the
// framework.
static RunResult vwStopSession(const std::shared_ptr<Session>& session) {
    void* owned = nullptr;
    {
        std::lock_guard<std::mutex> lock(session->mutex);
        const State current = static_cast<State>(session->state.load());
        if (current == State::NotStarted || current == State::Stopped || current == State::Error) {
            return RunResult::failure("not-running", "没有正在运行的 macOS 原生虚拟机。");
        }
        session->finished = false;
        session->state.store(static_cast<int>(State::Stopping));
        // Ownership transfer of the +1 reference (see below), so the runner
        // stays alive even though the Session may still be observed by a late
        // framework callback.
        owned = session->runnerRaw;
        session->runnerRaw = nullptr;
    }

    bool stopRequested = false;
    if (@available(macOS 11.0, *)) {
        stopRequested = vwRequestRunnerStop(owned, session);
    }
    if (!stopRequested) {
        std::lock_guard<std::mutex> lock(session->mutex);
        session->state.store(static_cast<int>(State::Stopped));
        session->result = RunResult::success("macOS 原生虚拟机已停止。");
        session->finished = true;
        session->cv.notify_all();
    }

    const RunResult result = vwWaitForSession(session, std::chrono::seconds(30));

    // Break the session -> runner -> machine chain. On timeout the stop callback
    // may still be in flight, so releasing the machine here could race it; the
    // completion block keeps the session alive and unwinds on its own.
    if (result.code != "timeout") {
        if (@available(macOS 11.0, *)) {
            vwDetachRunnerHandle(owned);
        }
        if (owned) {
            vwReleaseObjc(owned);
            owned = nullptr;
        }
    }
    return result;
}

RunResult MacVirtualizationEngine::stop() {
    const std::shared_ptr<AsyncControl> control = vwRequireControl(asyncControl_);
    std::shared_ptr<Session> session;
    {
        std::lock_guard<std::mutex> lock(control->mutex);
        session = std::static_pointer_cast<Session>(session_);
    }
    if (!session) {
        return RunResult::failure("not-running", "没有正在运行的 macOS 原生虚拟机。");
    }

    const RunResult result = vwStopSession(session);

    {
        std::lock_guard<std::mutex> lock(control->mutex);
        if (std::static_pointer_cast<Session>(session_) == session) {
            session_.reset();
        }
        if (result.code == "not-running") {
            // The guest may simply have shut itself down; keep the state it
            // reported instead of turning that into an error.
            stateValue_.store(session->state.load());
        } else {
            stateValue_.store(static_cast<int>(result.ok ? State::Stopped : State::Error));
        }
    }
    return result;
}

void MacVirtualizationEngine::stopAsync(std::function<void(const RunResult&)> completion) {
    const std::shared_ptr<AsyncControl> control = vwRequireControl(asyncControl_);
    std::shared_ptr<Session> session;
    {
        std::lock_guard<std::mutex> lock(control->mutex);
        session = std::static_pointer_cast<Session>(session_);
    }
    if (!session) {
        if (completion) {
            completion(RunResult::failure("not-running", "没有正在运行的 macOS 原生虚拟机。"));
        }
        return;
    }

    // Same lifetime rule as startAsync: the worker only dereferences the engine
    // while holding control->mutex with `abandoned` observed as false.
    std::thread([this, control, session, completion]() {
        const RunResult result = vwStopSession(session);
        {
            std::lock_guard<std::mutex> lock(control->mutex);
            if (!control->abandoned) {
                if (std::static_pointer_cast<Session>(session_) == session) {
                    session_.reset();
                }
                if (result.code == "not-running") {
                    stateValue_.store(session->state.load());
                } else {
                    stateValue_.store(static_cast<int>(result.ok ? State::Stopped : State::Error));
                }
            }
        }
        if (completion) {
            completion(result);
        }
    }).detach();
}

const BackendDescriptor& backendDescriptor() {
    static const BackendDescriptor descriptor = {
        backendId(),
        backendName(),
        &MacVirtualizationEngine::isHostSupported,
        &MacVirtualizationEngine::supportsGuestArch,
        &MacVirtualizationEngine::probe,
        []() { return MacVirtualizationEngine().capabilities(); },
        [](const Config& config) { return MacVirtualizationEngine().validate(config); },
        [](const Config& config) { return MacVirtualizationEngine().start(config); },
        []() { return MacVirtualizationEngine().stop(); },
    };
    return descriptor;
}

} // namespace macvirt
} // namespace vw

#endif // VW_MACVIRT_HAS_FRAMEWORK
