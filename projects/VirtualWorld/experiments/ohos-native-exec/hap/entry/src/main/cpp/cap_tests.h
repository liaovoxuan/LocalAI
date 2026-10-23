/*
 * cap_tests.h — shared capability-test routines for the HarmonyOS native-exec
 * validation. Both the shell probe (run via hdc in the `shell` domain) and the
 * HarmonyPC HAP (run in the normal third-party app sandbox) call the SAME
 * run_cap_tests() so the two environments are compared apples-to-apples.
 *
 * Every test is deterministic, auditable, and uses ONLY fixed/known AArch64
 * instructions (the exec blob is `MOV W0,#42; RET`). No unknown guest code is
 * ever executed. Results are written to the caller-supplied FILE* (stdout for
 * the shell probe, a private app file for the HAP).
 */
#ifndef CAP_TESTS_H
#define CAP_TESTS_H

#include <stdio.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Run all capability tests against the current process and emit one line per
 * check. `tag` labels the environment (e.g. "shell" or "hap"). */
void run_cap_tests(FILE *out, const char *tag);

#ifdef __cplusplus
}
#endif

#endif /* CAP_TESTS_H */
