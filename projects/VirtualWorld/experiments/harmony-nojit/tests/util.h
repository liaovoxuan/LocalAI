/* util.h — test-only helpers shared by the harmony-nojit prototype tests.
 * Not product code. Provides a portable CURRENT-RSS reader and a monotonic
 * clock, so every RSS number in the evidence files is reproducible.
 */
#ifndef VW_NOJIT_TESTS_UTIL_H
#define VW_NOJIT_TESTS_UTIL_H

#include <stdint.h>
#include <stdio.h>
#include <time.h>

#if defined(__APPLE__)
#  include <mach/mach.h>
static uint64_t rss_bytes(void) {
    mach_task_basic_info_data_t info;
    mach_msg_type_number_t cnt = MACH_TASK_BASIC_INFO_COUNT;
    if (task_info(mach_task_self(), MACH_TASK_BASIC_INFO,
                  (task_info_t)&info, &cnt) != KERN_SUCCESS)
        return 0;
    return (uint64_t)info.resident_size;   /* macOS: bytes */
}
#  define RSS_UNIT "bytes"
#elif defined(__linux__)
#  include <stdlib.h>
static uint64_t rss_bytes(void) {
    FILE *f = fopen("/proc/self/statm", "r");
    if (!f) return 0;
    unsigned long total = 0, resident = 0;
    if (fscanf(f, "%lu %lu", &total, &resident) != 2) resident = 0;
    fclose(f);
    return (uint64_t)resident * 4096u;      /* Linux: pages -> bytes */
}
#  define RSS_UNIT "bytes (statm pages*4096)"
#else
static uint64_t rss_bytes(void) { return 0; }
#  define RSS_UNIT "unavailable"
#endif

static inline double now_sec(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static const char *host_os(void) {
#if defined(__APPLE__)
    return "macOS";
#elif defined(__linux__)
    return "Linux";
#else
    return "unknown";
#endif
}

#endif /* VW_NOJIT_TESTS_UTIL_H */
