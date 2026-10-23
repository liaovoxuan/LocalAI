/*
 * cap_hap_test.cpp — the HAP (third-party app sandbox) entry point for the
 * HarmonyOS native-exec capability validation. It is compiled into
 * libcaptest.so and called from EntryAbility.ts.
 *
 * It runs the SAME run_cap_tests() routine used by the shell probe, so the two
 * environments (shell vs third-party app sandbox) are compared apples-to-apples.
 * Results are written to (1) the app's private file directory and (2) hilog
 * (the app log), then retrieved via hdc.
 *
 * The only machine code ever executed is the fixed, auditable blob
 *   MOV W0, #42 ; RET   (in cap_tests.c EXEC_BLOB). No unknown guest code runs.
 */
/* Expose POSIX setenv()/dladdr() under musl. */
#define _POSIX_C_SOURCE 200809L

#include "cap_tests.h"

#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <dlfcn.h>
#include <hilog/log.h>

/* Provide captest_add inside this same .so so the dlopen test (which opens this
 * lib via its own path) can resolve it. */
extern "C" int captest_add(int a, int b)
{
    return a + b;
}

static void log_line(const char *line)
{
    OH_LOG_INFO(LOG_APP, "%{public}s", line);
}

extern "C" int RunCapTests(const char *outDir)
{
    char path[1024];
    snprintf(path, sizeof(path), "%s/cap_results.txt", outDir ? outDir : ".");

    FILE *f = fopen(path, "w");
    FILE *out = f ? f : stdout;

    /* Self-locate this .so so the dlopen test opens the app's own lib. */
    Dl_info info;
    if (dladdr(reinterpret_cast<void *>(reinterpret_cast<intptr_t>(RunCapTests)), &info) &&
        info.dli_fname) {
        setenv("CAP_SO_PATH", info.dli_fname, 1);
    }

    /* Run the shared, auditable test battery in the "hap" (app) domain. */
    run_cap_tests(out, "hap");

    if (f) {
        fclose(f);
        OH_LOG_INFO(LOG_APP, "cap results written to %{public}s", path);
    }
    (void)log_line; /* keep helper referenced even if not used above */
    return 0;
}
