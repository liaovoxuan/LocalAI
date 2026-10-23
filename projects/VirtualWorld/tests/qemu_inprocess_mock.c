/*
 * qemu_inprocess_mock.c
 *
 * TRANSPARENT TEST DOUBLE for the in-process QEMU C ABI contract
 * (qemu-source/qemu-inprocess/qemu_inprocess.h).
 *
 * This is NOT QEMU. It implements the same C ABI with a trivial state machine
 * so that QemuInProcessRuntime's logic (single-instance guard, reentrant
 * stop/destroy, init-failure cleanup, accelerator policy plumbing) can be
 * exercised and proven correct WITHOUT linking the multi-million-line real
 * QEMU core. The real core is libqemu_inprocess.a built from qemu-source; it
 * is selected by the build when QEMU_INPROCESS_REAL_CORE=ON.
 *
 * The contract it satisfies is byte-for-byte the production contract, so the
 * wrapper cannot tell mock from real at the ABI level.
 */
/* Include the contract header relative to this file so the mock compiles
 * without depending on -I resolution of the (possibly absent) build system.
 * The production core uses the same header via -I qemu-source/qemu-inprocess. */
#include "../qemu-source/qemu-inprocess/qemu_inprocess.h"

#include <pthread.h>
#include <stdlib.h>
#include <string.h>

/* Single-instance guard, mirrors the real core's g_active. */
static int g_active = 0;

/* run()/request_stop() coordination. */
static pthread_mutex_t g_run_mtx = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_run_cv = PTHREAD_COND_INITIALIZER;
static int g_stop_requested = 0;
static int g_init_failure = 0;
static int g_kvm_usable = 1;

struct QemuInProcessSession {
    int dummy;
};

QemuInProcessSession *qemu_inprocess_session_create(void)
{
    return (QemuInProcessSession *)calloc(1, sizeof(QemuInProcessSession));
}

void qemu_inprocess_session_destroy(QemuInProcessSession *session)
{
    free(session);
}

QemuInProcessStatus qemu_inprocess_init(QemuInProcessSession *session,
                                        int argc, char **argv,
                                        const QemuInProcessCallbacks *cb)
{
    (void)session;
    (void)argc;
    (void)argv;
    (void)cb;
    if (g_active) {
        return QEMU_INPROCESS_ERR_BUSY;
    }
    if (g_init_failure) {
        return QEMU_INPROCESS_ERR_INIT_FAILED;
    }
    g_active = 1;
    g_stop_requested = 0;
    return QEMU_INPROCESS_OK;
}

int qemu_inprocess_run(QemuInProcessSession *session)
{
    (void)session;
    pthread_mutex_lock(&g_run_mtx);
    while (!g_stop_requested) {
        pthread_cond_wait(&g_run_cv, &g_run_mtx);
    }
    pthread_mutex_unlock(&g_run_mtx);
    return 0;
}

QemuInProcessStatus qemu_inprocess_request_stop(QemuInProcessSession *session)
{
    (void)session;
    pthread_mutex_lock(&g_run_mtx);
    if (g_stop_requested) {
        pthread_mutex_unlock(&g_run_mtx);
        return QEMU_INPROCESS_ERR_STOPPED; /* idempotent */
    }
    g_stop_requested = 1;
    pthread_cond_signal(&g_run_cv);
    pthread_mutex_unlock(&g_run_mtx);
    return QEMU_INPROCESS_OK;
}

void qemu_inprocess_cleanup(QemuInProcessSession *session)
{
    (void)session;
    g_active = 0;
    g_stop_requested = 0;
}

int qemu_inprocess_kvm_usable(int target_isa_host_match)
{
    return target_isa_host_match && g_kvm_usable ? 1 : 0;
}

int qemu_inprocess_hardware_accel_active(QemuInProcessSession *session)
{
    return session && g_active && g_kvm_usable ? 1 : 0;
}

const QemuInProcessCallbacks *qemu_inprocess_callbacks(
    QemuInProcessSession *session)
{
    (void)session;
    return NULL;
}

/* ---- Non-contract test helper: force the next init to fail. ---- */
void qemu_inprocess_mock_set_init_failure(int enabled)
{
    g_init_failure = enabled ? 1 : 0;
}

void qemu_inprocess_mock_set_kvm_usable(int enabled)
{
    g_kvm_usable = enabled ? 1 : 0;
}
