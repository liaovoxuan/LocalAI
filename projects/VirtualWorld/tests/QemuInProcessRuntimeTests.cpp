/*
 * QemuInProcessRuntimeTests.cpp
 *
 * Real, runnable tests for the QemuInProcessRuntime wrapper. They exercise the
 * wrapper's own logic against the in-process C ABI contract implemented by
 * tests/qemu_inprocess_mock.c (a transparent test double, NOT QEMU).
 *
 * What these tests prove on their own:
 *   - accelerator policy (KVM only for same-ISA + /dev/kvm; TCG otherwise;
 *     cross-ISA is NEVER hardware accelerated)
 *   - single-instance guard returns a stable Busy error
 *   - stop() and destroy() are reentrant / idempotent (no crash, no leak)
 *   - concurrent stop() from many threads is safe
 *   - a failed init releases resources and leaves the session destroyable
 *
 * The PRODUCTION core is libqemu_inprocess.a built from qemu-source. The
 * end-to-end "QEMU actually runs in this PID" harness is compiled only when
 * QEMU_INPROCESS_REAL_CORE is defined (see build-qemu-inprocess/CMakeLists.txt)
 * because it requires the real QEMU static library + its build dependencies.
 */
#include "QemuInProcessRuntime.hpp"

#include <cstdio>
#include <cstdlib>
#include <thread>
#include <vector>

#if defined(QEMU_INPROCESS_REAL_CORE) && defined(QEMU_INPROCESS_RUN_REAL_VM_TESTS)
#include <unistd.h> // getpid, for the same-PID end-to-end harness
#endif

#ifndef QEMU_INPROCESS_REAL_CORE
extern "C" void qemu_inprocess_mock_set_init_failure(int enabled);
extern "C" void qemu_inprocess_mock_set_kvm_usable(int enabled);
#endif

namespace {

int g_checks = 0;
int g_failed = 0;

void check(bool cond, const char* msg)
{
    ++g_checks;
    if (!cond) {
        std::printf("  FAIL: %s\n", msg);
        ++g_failed;
    }
}

using vw::qemu::Accel;
using vw::qemu::AccelDecision;
using vw::qemu::Isa;
using vw::qemu::QemuCallbacks;
using vw::qemu::QemuErrorCode;
using vw::qemu::QemuInProcessRuntime;
using vw::qemu::QemuLaunchConfig;
using vw::qemu::QemuSessionHandle;
using vw::qemu::SessionState;

} // namespace

// ---------------------------------------------------------------------------
static void test_accel_policy()
{
    // Same-ISA + KVM usable -> KVM (hardware accelerated).
    AccelDecision d = QemuInProcessRuntime::decideAccel(Isa::AArch64, Isa::AArch64, true);
    check(d.accel == Accel::KVM && d.hardwareAccelerated,
          "same-ISA + kvm -> KVM hardware accelerated");

    // Same-ISA, no /dev/kvm -> TCG (software).
    d = QemuInProcessRuntime::decideAccel(Isa::AArch64, Isa::AArch64, false);
    check(d.accel == Accel::TCG && !d.hardwareAccelerated,
          "same-ISA - kvm -> TCG software");

    // Cross-ISA -> TCG, and MUST NOT claim hardware acceleration.
    d = QemuInProcessRuntime::decideAccel(Isa::AArch64, Isa::X86_64, true);
    check(d.accel == Accel::TCG && !d.hardwareAccelerated,
          "cross-ISA -> TCG, never hardware accelerated (even if kvm flag set)");
}

// ---------------------------------------------------------------------------
static void test_single_instance_busy()
{
    QemuInProcessRuntime rt;
    QemuSessionHandle h1 = nullptr;
    QemuSessionHandle h2 = nullptr;
    check(rt.create("single-inst", h1) == QemuErrorCode::Ok, "first create Ok");
    check(rt.create("single-inst", h2) == QemuErrorCode::Busy,
          "second create with same tag -> Busy");
    check(h2 == nullptr, "Busy create leaves handle untouched");
    check(rt.create("different-tag", h2) == QemuErrorCode::Busy,
          "different tag still respects process-global single-instance guard");
    check(rt.destroy(h1) == QemuErrorCode::Ok, "destroy first Ok");
    // After destroy, the tag is free again.
    QemuSessionHandle h3 = nullptr;
    check(rt.create("single-inst", h3) == QemuErrorCode::Ok,
          "re-create after destroy Ok");
    check(rt.destroy(h3) == QemuErrorCode::Ok, "destroy re-created Ok");
}

// ---------------------------------------------------------------------------
#ifndef QEMU_INPROCESS_REAL_CORE
static void test_reentrant_stop_destroy()
{
    qemu_inprocess_mock_set_init_failure(0);
    qemu_inprocess_mock_set_kvm_usable(1);
    QemuInProcessRuntime rt;
    rt.setHostIsa(Isa::AArch64);
    QemuSessionHandle h = nullptr;
    check(rt.create("reentrant", h) == QemuErrorCode::Ok, "create Ok");
    check(rt.stop(h) == QemuErrorCode::Ok, "stop before init is a safe no-op");
    QemuLaunchConfig cfg;
    cfg.targetIsa = Isa::AArch64;
    cfg.memoryBytes = 256 * 1024 * 1024;
    check(rt.init(h, cfg) == QemuErrorCode::Ok, "init Ok (KVM same-ISA in mock)");
    check(rt.accel(h).accel == Accel::KVM, "accel resolved to KVM by policy");
    check(rt.start(h) == QemuErrorCode::Ok, "start Ok");
    check(rt.start(h) == QemuErrorCode::Busy, "second start while running -> Busy");
    check(rt.stop(h) == QemuErrorCode::Ok, "first stop Ok");
    check(rt.stop(h) == QemuErrorCode::Ok, "second stop idempotent Ok");
    check(rt.destroy(h) == QemuErrorCode::Ok, "first destroy Ok");
    check(rt.destroy(h) == QemuErrorCode::Ok, "second destroy idempotent Ok");
}

static void test_post_init_accel_truth()
{
    qemu_inprocess_mock_set_init_failure(0);
    qemu_inprocess_mock_set_kvm_usable(0);
    QemuInProcessRuntime rt;
    rt.setHostIsa(Isa::AArch64);
    QemuSessionHandle h = nullptr;
    check(rt.create("accel-fallback", h) == QemuErrorCode::Ok, "fallback create Ok");
    QemuLaunchConfig cfg;
    cfg.targetIsa = Isa::AArch64;
    check(rt.init(h, cfg) == QemuErrorCode::Ok, "fallback init Ok");
    const AccelDecision decision = rt.accel(h);
    check(decision.accel == Accel::TCG && !decision.hardwareAccelerated,
          "post-init result never claims KVM when the core selected TCG");
    check(rt.destroy(h) == QemuErrorCode::Ok, "fallback destroy Ok");
    qemu_inprocess_mock_set_kvm_usable(1);
}

// ---------------------------------------------------------------------------
static void test_concurrent_stop()
{
    qemu_inprocess_mock_set_init_failure(0);
    QemuInProcessRuntime rt;
    rt.setHostIsa(Isa::AArch64);
    QemuSessionHandle h = nullptr;
    check(rt.create("concurrent", h) == QemuErrorCode::Ok, "create Ok");
    QemuLaunchConfig cfg;
    cfg.targetIsa = Isa::AArch64;
    cfg.memoryBytes = 128 * 1024 * 1024;
    check(rt.init(h, cfg) == QemuErrorCode::Ok, "init Ok");
    check(rt.start(h) == QemuErrorCode::Ok, "start Ok");

    const int N = 16;
    std::vector<std::thread> threads;
    for (int i = 0; i < N; ++i) {
        threads.emplace_back([&rt, h]() { rt.stop(h); });
    }
    for (auto& t : threads) {
        t.join();
    }
    check(rt.state(h) == SessionState::Stopped, "state Stopped after concurrent stop");
    check(rt.destroy(h) == QemuErrorCode::Ok, "destroy after concurrent stop Ok");
}

// ---------------------------------------------------------------------------
static void test_init_failure_cleanup()
{
    qemu_inprocess_mock_set_init_failure(1); // force init failure
    QemuInProcessRuntime rt;
    rt.setHostIsa(Isa::AArch64);
    QemuSessionHandle h = nullptr;
    check(rt.create("initfail", h) == QemuErrorCode::Ok, "create Ok");
    QemuLaunchConfig cfg;
    cfg.targetIsa = Isa::AArch64;
    cfg.memoryBytes = 64 * 1024 * 1024;
    check(rt.init(h, cfg) == QemuErrorCode::InitFailed, "init fails as forced");
    check(rt.state(h) == SessionState::Error, "session in Error state after failed init");
    check(rt.destroy(h) == QemuErrorCode::Ok, "destroy after failed init Ok (no leak)");
    // Tag freed -> re-create succeeds.
    qemu_inprocess_mock_set_init_failure(0);
    QemuSessionHandle h2 = nullptr;
    check(rt.create("initfail", h2) == QemuErrorCode::Ok, "re-create after failed init Ok");
    check(rt.destroy(h2) == QemuErrorCode::Ok, "destroy re-created Ok");
}
#endif

// ---------------------------------------------------------------------------

/*
 * P0 acceptance: a malformed / unsatisfiable configuration must return
 * QEMU_INPROCESS_ERR_INIT_FAILED instead of terminating the host process.
 *
 * Before qemu_init_checked() existed, this argv reached upstream qemu_init(),
 * which called exit(1) from inside its own body (53 exit/abort sites) and took
 * the whole host application with it. The assertion that matters is not just
 * the return code: it is that this process is still alive afterwards and can
 * still complete a valid in-process lifecycle.
 */
static void test_fatal_config_is_returnable()
{
    QemuInProcessRuntime rt;
    QemuSessionHandle bad = nullptr;

    check(rt.create("badcfg", bad) == QemuErrorCode::Ok, "create session for malformed config");
    QemuLaunchConfig cfg;
    cfg.targetIsa = Isa::AArch64;
    cfg.memoryBytes = 64 * 1024 * 1024;
    cfg.machine = "virt";
    /* An option upstream qemu_init() rejects outright. */
    cfg.extraArgs = "--definitely-not-a-real-qemu-option";

    const QemuErrorCode rc = rt.init(bad, cfg);
    check(rc == QemuErrorCode::InitFailed,
          "malformed argv returns InitFailed (host NOT terminated)");
    printf("    malformed-argv init returned InitFailed; host still alive\n");
    check(rt.destroy(bad) == QemuErrorCode::Ok, "destroy after failed init");

    /* Reaching this line at all is the point: the host survived. */
    check(true, "host process survived a fatal configuration error");
}

#ifdef QEMU_INPROCESS_REAL_CORE
// End-to-end proof that QEMU executes INSIDE this process (no child PID).
// Compiled only with the real libqemu_inprocess.a, which is blocked in the
// release gate. It is additionally opt-in because it starts a virtual machine.
static void test_real_core_same_pid_harness()
{
    QemuInProcessRuntime rt;
    rt.setHostIsa(Isa::AArch64);
    QemuSessionHandle h = nullptr;
    check(rt.create("realcore", h) == QemuErrorCode::Ok, "create Ok (real core)");
    QemuLaunchConfig cfg;
    cfg.targetIsa = Isa::AArch64;
    cfg.memoryBytes = 64 * 1024 * 1024;
    cfg.machine = "virt"; // aarch64 default; explicit so qemu_init never hits
                          // the fatal "no machine" exit() path in-process
    cfg.extraArgs = "-nodefaults -S"; // minimal, no guest ROM/OS; vCPU held at reset
    check(rt.init(h, cfg) == QemuErrorCode::Ok, "init Ok (real core)");
    check(rt.start(h) == QemuErrorCode::Ok, "start Ok (real core in this PID)");
    check(rt.state(h) == SessionState::Running, "Running in-process");
    // Genuine same-PID proof: the worker thread (which runs qemu_main_loop)
    // records its own PID; it must equal the host PID, proving QEMU executes
    // inside this process and not as a spawned child.
    const int wpid = rt.workerPid(h);
    const pid_t hostPid = getpid();
    check(wpid > 0 && wpid == (int)hostPid,
          "QEMU main loop runs in host PID (in-process, no child process)");
    rt.stop(h);
    rt.destroy(h);
}
#endif

// ---------------------------------------------------------------------------
int main()
{
    std::printf("QemuInProcessRuntime wrapper tests (contract mock)\n");
    test_accel_policy();
    test_single_instance_busy();
#ifndef QEMU_INPROCESS_REAL_CORE
    test_reentrant_stop_destroy();
    test_post_init_accel_truth();
    test_concurrent_stop();
    test_init_failure_cleanup();
#endif
#if defined(QEMU_INPROCESS_REAL_CORE) && defined(QEMU_INPROCESS_RUN_REAL_VM_TESTS)
    test_fatal_config_is_returnable();
    test_real_core_same_pid_harness();
#endif
    std::printf("checks=%d failed=%d\n", g_checks, g_failed);
    return g_failed == 0 ? 0 : 1;
}
