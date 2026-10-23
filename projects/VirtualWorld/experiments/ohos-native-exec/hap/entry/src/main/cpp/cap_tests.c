/*
 * cap_tests.c — implementation of the shared HarmonyOS capability tests.
 * See cap_tests.h. Builds for aarch64-linux-ohos (musl). Uses only libc:
 * mmap/mprotect, sigaction, pthread + __thread, futex syscall, dlopen/dlsym.
 *
 * IMPORTANT: the only machine code ever executed is the fixed, auditable blob
 *   MOV W0, #42 ; RET   (little-endian: 40 05 80 52  C0 03 5F D6).
 * Nothing else is executed; signal tests use raise(), not crafted code.
 */
#include "cap_tests.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <signal.h>
#include <unistd.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <pthread.h>
#include <dlfcn.h>

#ifndef __NR_futex
#define __NR_futex 98
#endif
#ifndef FUTEX_WAIT
#define FUTEX_WAIT 0
#define FUTEX_WAKE 1
#endif

/* ---- fixed, auditable AArch64 exec blob: MOV W0,#42 ; RET --------------- */
static const unsigned char EXEC_BLOB[] = {
    0x40, 0x05, 0x80, 0x52,   /* MOV  W0, #42  (0x52800540) */
    0xC0, 0x03, 0x5F, 0xD6    /* RET          (0xD65F03C0) */
};

static int g_sigill_caught = 0;
static int g_sigsegv_caught = 0;

static void on_sigill(int s)  { (void)s; g_sigill_caught = 1; }
static void on_sigsegv(int s) { (void)s; g_sigsegv_caught = 1; }

static void emit(FILE *out, const char *tag, const char *name, int pass, int err)
{
    const char *verdict = pass ? "PASS" : "DENIED";
    if (pass)
        fprintf(out, "[CAP][%s] %-20s : %s\n", tag, name, verdict);
    else
        fprintf(out, "[CAP][%s] %-20s : %s (errno=%d %s)\n",
                tag, name, verdict, err, strerror(err));
    fflush(out);
}

/* ---- pthread + TLS test ------------------------------------------------ */
static __thread int g_tls = 7;

static void *tls_thread(void *arg)
{
    (void)arg;
    g_tls = 123;
    return NULL;
}

static int test_pthread_tls(void)
{
    g_tls = 7;
    pthread_t th;
    if (pthread_create(&th, NULL, tls_thread, NULL) != 0)
        return 0;
    void *ret = NULL;
    pthread_join(th, &ret);
    /* After join, in the main thread g_tls must still be 7 (per-thread copy). */
    return (g_tls == 7) ? 1 : 0;
}

/* ---- futex test: FUTEX_WAIT that returns immediately with EAGAIN -------- */
static int test_futex(void)
{
    int u = 1;
    struct timespec ts = {0, 0};
    long r = syscall(__NR_futex, &u, FUTEX_WAIT, 1, &ts, NULL, 0);
    /* Expected: -1, errno=EAGAIN (value matched, no waiter slept). */
    return (r == -1 && errno == EAGAIN) ? 1 : 0;
}

/* ---- dlopen test: open a sibling .so built with the probe -------------- */
static int test_dlopen(const char *so_path)
{
    void *h = dlopen(so_path, RTLD_NOW | RTLD_LOCAL);
    if (!h)
        return 0;
    typedef int (*add_fn)(int, int);
    add_fn f = (add_fn)dlsym(h, "captest_add");
    int ok = 0;
    if (f && f(2, 3) == 5)
        ok = 1;
    dlclose(h);
    return ok;
}

void run_cap_tests(FILE *out, const char *tag)
{
    fprintf(out, "===== cap_tests environment=%s =====\n", tag);
    fflush(out);

    /* 1) mmap RW */
    {
        void *p = mmap(NULL, 4096, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        int pass = (p != MAP_FAILED);
        emit(out, tag, "mmap_rw", pass, pass ? 0 : errno);
        if (pass) munmap(p, 4096);
    }

    /* 2) mmap RX (allocate an executable page directly) */
    {
        void *p = mmap(NULL, 4096, PROT_READ | PROT_EXEC,
                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        int pass = (p != MAP_FAILED);
        emit(out, tag, "mmap_rx", pass, pass ? 0 : errno);
        if (pass) munmap(p, 4096);
    }

    /* 3) mmap RWX */
    {
        void *p = mmap(NULL, 4096, PROT_READ | PROT_WRITE | PROT_EXEC,
                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        int pass = (p != MAP_FAILED);
        emit(out, tag, "mmap_rwx", pass, pass ? 0 : errno);
        if (pass) munmap(p, 4096);
    }

    /* 4) RW -> mprotect RX */
    {
        void *p = mmap(NULL, 4096, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        int pass = 0, e = 0;
        if (p != MAP_FAILED) {
            if (mprotect(p, 4096, PROT_READ | PROT_EXEC) == 0) {
                pass = 1;
            } else {
                e = errno;
            }
            munmap(p, 4096);
        } else {
            e = errno;
        }
        emit(out, tag, "rw_to_rx_mprotect", pass, e);
    }

    /* 5) file-backed MAP_EXEC (map THIS binary's own text as RX) */
    {
        /* Use /proc/self/exe to obtain a file-backed RX mapping. */
        void *p = mmap(NULL, 4096, PROT_READ | PROT_EXEC,
                       MAP_PRIVATE, 0 /* special: /proc/self/exe via fd below */, 0);
        /* The above fd=0 is wrong on purpose-safe; redo with real fd. */
        (void)p; /* placeholder; real test below using a temp file */
        int pass = 0, e = 0;
        FILE *tf = tmpfile();
        if (tf) {
            int fd = fileno(tf);
            void *q = mmap(NULL, 4096, PROT_READ | PROT_EXEC,
                           MAP_PRIVATE, fd, 0);
            if (q != MAP_FAILED) { pass = 1; munmap(q, 4096); }
            else e = errno;
            fclose(tf);
        } else { e = errno; }
        emit(out, tag, "file_map_rx", pass, e);
    }

    /* 6) anonymous executable page + execute fixed blob */
    {
        void *p = mmap(NULL, 4096, PROT_READ | PROT_WRITE | PROT_EXEC,
                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        int pass = 0, e = 0;
        if (p == MAP_FAILED) {
            /* Try RW then mprotect RX as a fallback. */
            p = mmap(NULL, 4096, PROT_READ | PROT_WRITE,
                     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
            if (p != MAP_FAILED) {
                if (mprotect(p, 4096, PROT_READ | PROT_EXEC) != 0) {
                    e = errno; p = MAP_FAILED;
                }
            } else e = errno;
        }
        if (p != MAP_FAILED) {
            memcpy(p, EXEC_BLOB, sizeof(EXEC_BLOB));
            __builtin___clear_cache((char *)p, (char *)p + sizeof(EXEC_BLOB));
            typedef int (*fn)(void);
            int rv = ((fn)p)();
            if (rv == 42) pass = 1; else e = -1;
            munmap(p, 4096);
        }
        emit(out, tag, "exec_fixed_blob", pass, e);
    }

    /* 7) SIGILL / SIGSEGV handler install + catch */
    {
        struct sigaction sa_i, sa_s, old_i, old_s;
        memset(&sa_i, 0, sizeof sa_i); memset(&sa_s, 0, sizeof sa_s);
        sa_i.sa_handler = on_sigill;  sa_s.sa_handler = on_sigsegv;
        sigemptyset(&sa_i.sa_mask);   sigemptyset(&sa_s.sa_mask);
        int ok_i = (sigaction(SIGILL, &sa_i, &old_i) == 0);
        int ok_s = (sigaction(SIGSEGV, &sa_s, &old_s) == 0);
        g_sigill_caught = 0; g_sigsegv_caught = 0;
        if (ok_i) raise(SIGILL);
        if (ok_s) raise(SIGSEGV);
        int pass_i = (ok_i && g_sigill_caught);
        int pass_s = (ok_s && g_sigsegv_caught);
        emit(out, tag, "sigill_handler", pass_i, pass_i ? 0 : EINVAL);
        emit(out, tag, "sigsegv_handler", pass_s, pass_s ? 0 : EINVAL);
        /* restore */
        if (ok_i) sigaction(SIGILL, &old_i, NULL);
        if (ok_s) sigaction(SIGSEGV, &old_s, NULL);
    }

    /* 8) pthread + TLS */
    emit(out, tag, "pthread_tls", test_pthread_tls(), EINVAL);

    /* 9) futex */
    emit(out, tag, "futex", test_futex(), EINVAL);

    /* 10) dlopen sibling .so (path provided by caller via env CAP_SO_PATH) */
    {
        const char *so = getenv("CAP_SO_PATH");
        int pass = (so && *so) ? test_dlopen(so) : 0;
        emit(out, tag, "dlopen_so", pass, pass ? 0 : ENOENT);
    }

    fprintf(out, "===== end =====\n");
    fflush(out);
}
