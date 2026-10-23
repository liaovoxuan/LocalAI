/*
 * cap_shell_probe.c — the SHELL-domain entry point for the HarmonyOS native-exec
 * capability validation. It is pushed to /data/local/tmp and run via hdc shell.
 * It calls the SAME run_cap_tests() routine that the HarmonyPC HAP uses, so the
 * two environments (shell vs third-party app sandbox) are compared apples-to-apples.
 *
 * IMPORTANT: this is the shell-domain probe only. Per task spec, shell results
 * MUST NOT substitute for HAP results; they are a separate, legitimate deliverable
 * (item 1 of the verification task).
 *
 * The only machine code ever executed is the fixed, auditable blob
 *   MOV W0, #42 ; RET   (in cap_tests.c EXEC_BLOB). No unknown guest code runs.
 */
#include "cap_tests.h"

#include <stdio.h>
#include <sys/resource.h>

static long peak_rss_kib(void)
{
    struct rusage ru;
    if (getrusage(RUSAGE_SELF, &ru) != 0)
        return -1;
    /* ru_maxrss is in KiB on Linux/OHOS. */
    return (long)ru.ru_maxrss;
}

int main(void)
{
    fprintf(stdout, "===== HarmonyOS native-exec capability probe (SHELL domain) =====\n");
    fflush(stdout);

    /* Run the shared, auditable test battery. */
    run_cap_tests(stdout, "shell");

    /* Report peak RSS so we can verify the <=1.5 GiB (here ~4 MiB) budget. */
    long rss = peak_rss_kib();
    fprintf(stdout, "[RSS] peak self maxrss = %ld KiB\n", rss);
    fflush(stdout);

    return 0;
}
