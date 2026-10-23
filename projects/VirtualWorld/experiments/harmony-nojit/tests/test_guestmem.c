/* test_guestmem.c — sparse guest-physical RAM prototype tests.
 * NOT product code, NOT QEMU. Host-native build only (no guest, no VM).
 *
 * Covered: 8 GiB logical / 64 MiB touched (RSS must stay far below 8 GiB),
 * zero-fill of never-committed pages, random read/write consistency,
 * discard accounting + RSS drop, out-of-bounds handling, concurrent writers,
 * explicit CHUNK (two-level) fallback mode, balloon/free-page reporting, and
 * the signal-safe stats path (no handler is installed by this module).
 */
#include "guestmem.h"
#include "util.h"

#include <pthread.h>
#include <stdlib.h>
#include <string.h>

static int checks = 0, fails = 0;

#define CHECK(cond, ...) do {                                        \
    checks++;                                                        \
    if (!(cond)) { fails++; printf("  FAIL: %s:%d  ", __FILE__, __LINE__); \
                   printf(__VA_ARGS__); printf("\n"); }               \
} while (0)

#define INFO(...) do { printf("  .. "); printf(__VA_ARGS__); printf("\n"); } while (0)

/* deterministic PRNG (xorshift64*) */
static uint64_t rng_state;
static uint64_t rng_next(void) {
    uint64_t x = rng_state;
    x ^= x >> 12; x ^= x << 25; x ^= x >> 27;
    rng_state = x;
    return x * 2685821657736338717ULL;
}

#define GIB (1024ULL * 1024ULL * 1024ULL)

/* ---------------- concurrency worker ---------------- */
typedef struct {
    vw_guest_mem_t *gm;
    uint64_t base;        /* chunk-aligned region start */
    uint64_t len;         /* bytes */
    uint8_t  tag;
    int rc;
} tjob_t;

static void *thread_fn(void *arg) {
    tjob_t *j = (tjob_t *)arg;
    uint64_t i;
    j->rc = GM_OK;
    for (i = 0; i + 8 <= j->len; i += 4096) {
        uint8_t buf[8];
        uint64_t v = j->base + i + j->tag * 0x9E3779B97F4A7C15ULL;
        int rc = gm_write(j->gm, j->base + i, &v, 8);
        if (rc != GM_OK) { j->rc = rc; return NULL; }
        memset(buf, 0, sizeof buf);
        rc = gm_read(j->gm, j->base + i, buf, 8);
        if (rc != GM_OK) { j->rc = rc; return NULL; }
        if (memcmp(buf, &v, 8) != 0) { j->rc = -99; return NULL; }
    }
    return NULL;
}

static int report_free_cb(void *ctx, uint64_t gpa, uint64_t len) {
    uint64_t *n = (uint64_t *)ctx;
    (*n) += len;
    (void)gpa;
    return 0;
}

int main(void) {
    printf("== harmony-nojit: sparse guest RAM tests [%s, rss unit: %s] ==\n",
           host_os(), RSS_UNIT);
    printf("rss_start = %llu bytes\n",
           (unsigned long long)rss_bytes());

    /* ---------- T1: 8 GiB logical, 64 MiB touched ---------- */
    printf("[T1] 8GiB logical address space, touch 64MiB\n");
    uint64_t rss_before = rss_bytes();
    vw_guest_mem_t *gm = gm_create(8ULL * GIB);
    CHECK(gm != NULL, "gm_create(8GiB)");
    if (!gm) { printf("FATAL: cannot create guest space\n"); return 1; }
    CHECK(gm_size(gm) == 8ULL * GIB, "gm_size=%llu want %llu",
          (unsigned long long)gm_size(gm), (unsigned long long)(8ULL * GIB));
    INFO("mode = %s (MAP_NORESERVE big-VA + kernel first-touch commit)",
         gm_mode_name(gm_mode_of(gm)));
    CHECK(gm_mode_of(gm) == GM_MODE_RESERVE,
          "auto mode selected RESERVE (got %d)", gm_mode_of(gm));

    /* read-untouched must be zero and must NOT commit */
    {
        uint8_t buf[64];
        memset(buf, 0xAB, sizeof buf);
        int rc = gm_read(gm, 4ULL * GIB, buf, sizeof buf);
        CHECK(rc == GM_OK, "read untouched rc=%d", rc);
        int allzero = 1;
        for (size_t i = 0; i < sizeof buf; i++) if (buf[i]) allzero = 0;
        CHECK(allzero, "untouched read is zero");
    }

    /* Touch exactly 64 MiB (chunk-aligned) with first-touch page commits: one
     * 8-byte write per 4 KiB page. Deliberately NOT a 64 MiB host buffer, so
     * the measured RSS delta is attributable to the guest pages alone. */
    {
        const uint64_t base = 0x200000ULL;      /* chunk aligned */
        const uint64_t pages = 16384;           /* 64 MiB */
        int bad = 0;
        for (uint64_t p = 0; p < pages; p++) {
            uint64_t gpa = base + p * 4096ULL;
            uint64_t v = 0xC0FFEE0000000000ULL + p;
            if (gm_write(gm, gpa, &v, 8) != GM_OK) { bad++; break; }
        }
        CHECK(bad == 0, "first-touch commit loop over 16384 pages");
    }
    gm_stats_t st;
    memset(&st, 0, sizeof st);
    CHECK(gm_stats(gm, &st) == GM_OK, "gm_stats");
    INFO("guest_size=%llu committed_chunks=%llu committed_bytes=%llu "
         "dirty_pages=%llu touched=%llu discarded=%llu",
         (unsigned long long)st.guest_size,
         (unsigned long long)st.committed_chunks,
         (unsigned long long)st.committed_bytes,
         (unsigned long long)st.dirty_pages,
         (unsigned long long)st.touched_pages,
         (unsigned long long)st.discarded_chunks);
    CHECK(st.committed_chunks == 32,
          "committed_chunks=%llu want 32 (64MiB/2MiB)",
          (unsigned long long)st.committed_chunks);
    CHECK(st.committed_bytes == 64ULL * 1024ULL * 1024ULL,
          "committed_bytes=%llu want %llu",
          (unsigned long long)st.committed_bytes,
          (unsigned long long)(64ULL * 1024ULL * 1024ULL));
    CHECK(st.dirty_pages == 16384, "dirty_pages=%llu want 16384",
          (unsigned long long)st.dirty_pages);

    uint64_t rss_after = rss_bytes();
    printf("  RESULT rss_before=%llu rss_after=%llu delta=%llu "
           "(logical 8GiB, touched 64MiB)\n",
           (unsigned long long)rss_before, (unsigned long long)rss_after,
           (unsigned long long)(rss_after - rss_before));
    CHECK(rss_after < 1ULL * GIB,
          "RSS after 64MiB touch is %llu bytes (must stay far below 8GiB)",
          (unsigned long long)rss_after);

    /* verify the data is really there (first and last touched page) */
    {
        uint64_t v0 = 0, vlast = 0;
        uint64_t w0 = 0xC0FFEE0000000000ULL + 0;
        uint64_t wl = 0xC0FFEE0000000000ULL + 16383;
        CHECK(gm_read(gm, 0x200000ULL, &v0, 8) == GM_OK, "readback page 0");
        CHECK(gm_read(gm, 0x200000ULL + 16383ULL * 4096ULL, &vlast, 8) == GM_OK,
              "readback page 16383");
        CHECK(v0 == w0, "page0 value=%llu want %llu",
              (unsigned long long)v0, (unsigned long long)w0);
        CHECK(vlast == wl, "last page value=%llu want %llu",
              (unsigned long long)vlast, (unsigned long long)wl);
    }

    /* ---------- T2: random read/write consistency ---------- */
    printf("[T2] random read/write consistency (256MiB window)\n");
    rng_state = 0x123456789ABCDEFULL;
    {
        const uint64_t win = 256ULL * 1024ULL * 1024ULL;
        const int ops = 100000;
        int mism = 0;
        for (int i = 0; i < ops; i++) {
            uint64_t off = (rng_next() % (win / 8)) * 8;
            uint64_t val = rng_next();
            uint64_t got = 0;
            if (gm_write(gm, 0x40000000ULL + off, &val, 8) != GM_OK) { mism++; continue; }
            if (gm_read(gm, 0x40000000ULL + off, &got, 8) != GM_OK) { mism++; continue; }
            if (got != val) mism++;
        }
        CHECK(mism == 0, "random RW mismatches=%d of %d", mism, ops);
        gm_stats(gm, &st);
        INFO("after random phase: committed_chunks=%llu dirty_pages=%llu",
             (unsigned long long)st.committed_chunks,
             (unsigned long long)st.dirty_pages);
        /* 64MiB + 256MiB window => 320MiB => 160 chunks */
        CHECK(st.committed_chunks == 160,
              "committed_chunks=%llu want 160 (320MiB/2MiB)",
              (unsigned long long)st.committed_chunks);
    }

    /* ---------- T3: discard / reclaim ---------- */
    printf("[T3] discard / reclaim accounting + RSS\n");
    {
        uint64_t rss_pre = rss_bytes();
        gm_stats(gm, &st);
        uint64_t cc_pre = st.committed_chunks, dp_pre = st.dirty_pages;
        int rc = gm_discard_all(gm);
        CHECK(rc == GM_OK, "gm_discard_all rc=%d", rc);
        /* give the kernel a moment to honour MADV_DONTNEED / MAP_FIXED paths */
        struct timespec ts = { 0, 50 * 1000 * 1000 };
        nanosleep(&ts, NULL);
        gm_stats(gm, &st);
        uint64_t rss_post = rss_bytes();
        printf("  RESULT discard_all: committed_chunks %llu -> %llu, "
               "dirty_pages %llu -> %llu, discarded_chunks=%llu, "
               "rss %llu -> %llu\n",
               (unsigned long long)cc_pre,
               (unsigned long long)st.committed_chunks,
               (unsigned long long)dp_pre,
               (unsigned long long)st.dirty_pages,
               (unsigned long long)st.discarded_chunks,
               (unsigned long long)rss_pre,
               (unsigned long long)rss_post);
        CHECK(st.committed_chunks == 0,
              "committed_chunks after discard=%llu want 0",
              (unsigned long long)st.committed_chunks);
        CHECK(st.dirty_pages == 0,
              "dirty_pages after discard=%llu want 0",
              (unsigned long long)st.dirty_pages);
        CHECK(rss_post <= rss_pre,
              "RSS grew across discard (%llu -> %llu)",
              (unsigned long long)rss_pre, (unsigned long long)rss_post);
        INFO("reclaim path actually used: %s (have_madvise=%d, discard_path=%d)",
             st.discard_path == 0 ? "MADV_DONTNEED" :
             st.discard_path == 1 ? "MAP_FIXED re-anon (madvise not effective)"
                                  : "REANON FAILED",
             st.have_madvise, st.discard_path);
        CHECK(st.discard_path >= 0, "reclamation path actually applied");
        /* data really must be gone (zero) — proves the reclaim dropped pages */
        uint64_t v = 0;
        gm_read(gm, 0x200000ULL, &v, 8);
        CHECK(v == 0, "discarded guest page reads back zero (got %llu)",
              (unsigned long long)v);
    }

    /* ---------- T4: out-of-bounds ---------- */
    printf("[T4] out-of-bounds handling\n");
    {
        uint8_t buf[16];
        int rc = gm_write(gm, gm_size(gm), buf, 8);
        CHECK(rc == GM_ERR_OOB, "write at size -> rc=%d want %d", rc, GM_ERR_OOB);
        rc = gm_write(gm, gm_size(gm) - 4, buf, 8);
        CHECK(rc == GM_ERR_OOB, "straddling write -> rc=%d want %d", rc, GM_ERR_OOB);
        rc = gm_read(gm, gm_size(gm) - 8, buf, 16);
        CHECK(rc == GM_ERR_OOB, "straddling read -> rc=%d want %d", rc, GM_ERR_OOB);
        rc = gm_write(gm, 0, buf, 0);
        CHECK(rc == GM_ERR_ARG, "zero length -> rc=%d want %d", rc, GM_ERR_ARG);
        rc = gm_discard(gm, gm_size(gm), 4096);
        CHECK(rc == GM_ERR_OOB, "discard OOB -> rc=%d want %d", rc, GM_ERR_OOB);
        gm_stats(gm, &st);
        CHECK(st.oob_rejects >= 4, "oob_rejects=%llu want >=4",
              (unsigned long long)st.oob_rejects);
        /* after all the rejects the space must still be usable */
        uint64_t v = 0xDEADBEEFCAFEBABEULL, got = 0;
        CHECK(gm_write(gm, 0x1000ULL, &v, 8) == GM_OK, "write after OOB rejects");
        CHECK(gm_read(gm, 0x1000ULL, &got, 8) == GM_OK, "read after OOB rejects");
        CHECK(got == v, "value after OOB rejects");
    }

    /* ---------- T5: concurrency ---------- */
    printf("[T5] concurrent writers (4 threads, disjoint regions)\n");
    {
        const int NT = 4;
        pthread_t th[4];
        tjob_t jobs[4];
        for (int i = 0; i < NT; i++) {
            jobs[i].gm = gm;
            jobs[i].base = 0x10000000ULL + (uint64_t)i * 0x200000ULL; /* 2MiB each */
            jobs[i].len = 0x200000ULL;
            jobs[i].tag = (uint8_t)(i + 1);
            jobs[i].rc = 0;
            pthread_create(&th[i], NULL, thread_fn, &jobs[i]);
        }
        int all_ok = 1;
        for (int i = 0; i < NT; i++) {
            pthread_join(th[i], NULL);
            if (jobs[i].rc != 0) { all_ok = 0; INFO("thread %d rc=%d", i, jobs[i].rc); }
        }
        CHECK(all_ok, "concurrent writers");
    }

    /* ---------- T6: balloon / free-page reporting ---------- */
    printf("[T6] balloon / free-page reporting (reserved interface)\n");
    {
        uint64_t reported = 0;
        CHECK(gm_set_free_page_report(gm, report_free_cb, &reported) == GM_OK,
              "gm_set_free_page_report");
        int64_t n = gm_balloon_report_free_all(gm);
        CHECK(n >= 0, "gm_balloon_report_free_all rc=%lld", (long long)n);
        INFO("reported free bytes = %lld", (long long)n);
        /* no handler => clean error, no crash */
        vw_guest_mem_t *g2 = gm_create(8ULL * GIB);
        CHECK(g2 != NULL, "second guest space");
        if (g2) {
            CHECK(gm_balloon_report_free_all(g2) < 0,
                  "report with no callback must fail cleanly");
            gm_destroy(g2);
        }
    }

    /* ---------- T7: signal-safe stats ---------- */
    printf("[T7] signal-safe stats (atomics only, no handler installed)\n");
    {
        gm_stats_t s7;
        CHECK(gm_stats(gm, &s7) == GM_OK, "gm_stats refresh for comparison");
        gm_stats_atomic_t a;
        CHECK(gm_stats_signal_safe(gm, &a) == GM_OK, "gm_stats_signal_safe");
        INFO("atomic committed_chunks=%llu dirty_pages=%llu oob_rejects=%llu",
             (unsigned long long)a.committed_chunks,
             (unsigned long long)a.dirty_pages,
             (unsigned long long)a.oob_rejects);
        CHECK(a.committed_chunks == s7.committed_chunks,
              "atomic committed_chunks=%llu agrees with locked=%llu",
              (unsigned long long)a.committed_chunks,
              (unsigned long long)s7.committed_chunks);
        CHECK(a.dirty_pages == s7.dirty_pages,
              "atomic dirty_pages=%llu agrees with locked=%llu",
              (unsigned long long)a.dirty_pages,
              (unsigned long long)s7.dirty_pages);
        CHECK(a.oob_rejects == s7.oob_rejects,
              "atomic oob_rejects=%llu agrees with locked=%llu",
              (unsigned long long)a.oob_rejects,
              (unsigned long long)s7.oob_rejects);
    }

    /* ---------- T8: explicit CHUNK fallback mode ---------- */
    printf("[T8] explicit two-level CHUNK mode (no big reservation)\n");
    {
        vw_guest_mem_t *gc = gm_create_ex(64ULL * 1024ULL * 1024ULL,
                                          GM_MODE_CHUNK);
        CHECK(gc != NULL, "gm_create_ex CHUNK");
        if (gc) {
            CHECK(gm_mode_of(gc) == GM_MODE_CHUNK, "mode is CHUNK");
            uint8_t buf[16];
            memset(buf, 0x33, sizeof buf);
            CHECK(gm_write(gc, 0, buf, sizeof buf) == GM_OK, "chunk write");
            memset(buf, 0, sizeof buf);
            CHECK(gm_read(gc, 0, buf, sizeof buf) == GM_OK, "chunk read");
            for (size_t i = 0; i < 16; i++)
                CHECK(buf[i] == 0x33, "chunk value[%zu]=0x%02X", i, buf[i]);
            gm_stats_t s2;
            gm_stats(gc, &s2);
            INFO("chunk mode: committed_chunks=%llu committed_bytes=%llu have_madvise=%d",
                 (unsigned long long)s2.committed_chunks,
                 (unsigned long long)s2.committed_bytes, s2.have_madvise);
            CHECK(s2.committed_chunks == 1,
                  "chunk committed_chunks=%llu want 1",
                  (unsigned long long)s2.committed_chunks);
            CHECK(gm_discard_all(gc) == GM_OK, "chunk discard_all");
            gm_stats(gc, &s2);
            CHECK(s2.committed_chunks == 0,
                  "chunk committed_chunks after discard=%llu want 0",
                  (unsigned long long)s2.committed_chunks);
            /* reuse after reclaim */
            memset(buf, 0x77, sizeof buf);
            CHECK(gm_write(gc, 4ULL * 1024ULL, buf, sizeof buf) == GM_OK,
                  "chunk write after discard");
            memset(buf, 0, sizeof buf);
            CHECK(gm_read(gc, 4ULL * 1024ULL, buf, sizeof buf) == GM_OK,
                  "chunk read after discard");
            for (size_t i = 0; i < 16; i++)
                CHECK(buf[i] == 0x77, "chunk reuse value[%zu]=0x%02X", i, buf[i]);
            gm_destroy(gc);
        }
    }

    gm_destroy(gm);
    uint64_t rss_end = rss_bytes();
    printf("rss_end = %llu bytes\n", (unsigned long long)rss_end);

    printf("== summary: %d checks, %d failures ==\n", checks, fails);
    return fails == 0 ? 0 : 1;
}
