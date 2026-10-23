/* guestmem.c — sparse guest-physical RAM prototype.
 * See guestmem.h for contract. No signal handlers installed; all allocation
 * and locking happen in normal context.
 */
#if defined(__APPLE__) && !defined(_DARWIN_C_SOURCE)
#  define _DARWIN_C_SOURCE 1
#endif
#include "guestmem.h"

#include <stdlib.h>
#include <string.h>
#include <pthread.h>

#if defined(__unix__) || defined(__APPLE__) || defined(__linux__)
#  include <sys/mman.h>
#  include <unistd.h>
#endif

#ifndef MAP_NORESERVE
#  define MAP_NORESERVE 0   /* absent => kernel default overcommit */
#endif
#ifdef MADV_DONTNEED
#  define GM_HAVE_MADVISE 1
#else
#  define GM_HAVE_MADVISE 0
#endif

typedef struct gm_chunk {
    uint8_t *data;                        /* CHUNK mode backing, NULL in RESERVE */
    uint64_t dirty[GM_PAGES_PER_CHUNK / 64]; /* 512 pages -> 8 words */
    uint32_t dirty_pages;
    uint32_t committed_pages;
    int      discarded;
} gm_chunk_t;

struct vw_guest_mem {
    uint8_t *base;                /* RESERVE mode mapping base; NULL in CHUNK */
    gm_chunk_t **l1;              /* two-level: l1[chunk index] */
    size_t    l1_entries;
    uint64_t  size;
    int       mode;
    int       have_madvise;       /* madvisable AND probe-verified to discard */
    int       discard_path;       /* 0 = MADV_DONTNEED used, 1 = MAP_FIXED re-anon */
    int       mmap_failed;        /* RESERVE attempt failed -> CHUNK in use */

    pthread_mutex_t lock;         /* protects l1 + chunk metadata ONLY.
                                   * never taken in a signal handler. */

    /* atomics for signal-safe stats (read via __atomic loads) */
    uint64_t a_committed_chunks;
    uint64_t a_dirty_pages;
    uint64_t a_oob_rejects;

    /* reserved balloon hooks */
    gm_free_page_report_fn report_fn;
    void *report_ctx;
    uint64_t reported_bytes;
    uint64_t discarded_chunks;
};

/* ---------------- helpers ---------------- */

static void atomic_add(uint64_t *addr, uint64_t v) {
    __atomic_add_fetch(addr, v, __ATOMIC_RELAXED);
}

/* bounds: reject gpa+len wrap and overflow, and empty length */
static int range_check(vw_guest_mem_t *gm, uint64_t gpa, uint64_t len) {
    if (!gm || len == 0) return GM_ERR_ARG;
    if (gpa > gm->size || len > gm->size - gpa) {
        __atomic_add_fetch(&gm->a_oob_rejects, 1, __ATOMIC_RELAXED);
        return GM_ERR_OOB;
    }
    return GM_OK;
}

static uint8_t *chunk_base(vw_guest_mem_t *gm, size_t ci) {
    if (gm->mode == GM_MODE_RESERVE) return gm->base + (uint64_t)ci * GM_CHUNK;
    return gm->l1[ci]->data;
}

/* caller holds the lock */
static int ensure_chunk(vw_guest_mem_t *gm, size_t ci) {
    gm_chunk_t *c = gm->l1[ci];
    if (c) {
        if (c->discarded) { c->discarded = 0; atomic_add(&gm->a_committed_chunks, 1); }
        return GM_OK;
    }
    c = calloc(1, sizeof *c);
    if (!c) return GM_ERR_NOMEM;
    if (gm->mode == GM_MODE_CHUNK) {
        c->data = mmap(NULL, (size_t)GM_CHUNK, PROT_READ | PROT_WRITE,
                       MAP_ANONYMOUS | MAP_PRIVATE | MAP_NORESERVE, -1, 0);
        if (c->data == MAP_FAILED) { free(c); return GM_ERR_NOMEM; }
    }
    gm->l1[ci] = c;
    atomic_add(&gm->a_committed_chunks, 1);
    return GM_OK;
}

/* Runtime probe: does MADV_DONTNEED actually discard on THIS platform?
 * macOS is a known case where MADV_DONTNEED may behave like MADV_FREE (data
 * survives / RSS does not drop). We measure instead of assuming, so the
 * discard path is never claimed to work when it does not. */
static int probe_madvise_discards(void) {
#if !GM_HAVE_MADVISE
    return 0;
#else
    size_t sz = (size_t)4096;
    uint8_t *p = mmap(NULL, sz, PROT_READ | PROT_WRITE,
                      MAP_ANONYMOUS | MAP_PRIVATE, -1, 0);
    if (p == MAP_FAILED) return 0;
    p[0] = 0x5A;
    int rc = madvise(p, sz, MADV_DONTNEED);
    int ok = (rc == 0 && p[0] == 0);
    munmap(p, sz);
    return ok;
#endif
}

/* Drop the resident backing of a RESERVE-mode range. Preferred: MADV_DONTNEED.
 * Fallback (when the probe above fails): MAP_FIXED re-anonymous mapping, which
 * reliably replaces the range with fresh zero pages and releases the old
 * physical pages. The virtual address is unchanged, so cached host pointers
 * (e.g. the CPU's software TLB) stay valid. */
static void reclaim_range(vw_guest_mem_t *gm, uint8_t *addr, uint64_t len) {
    if (gm->have_madvise) {
#if GM_HAVE_MADVISE
        madvise(addr, (size_t)len, MADV_DONTNEED);
        gm->discard_path = 0;
        return;
#endif
    }
    void *p = mmap(addr, (size_t)len, PROT_READ | PROT_WRITE,
                   MAP_ANONYMOUS | MAP_PRIVATE | MAP_FIXED | MAP_NORESERVE,
                   -1, 0);
    gm->discard_path = (p == MAP_FAILED) ? -1 : 1;
}

/* caller holds the lock.
 * Transitions a chunk BACK to "no resident backing" and keeps the two
 * accounting views (locked list walk and the signal-safe atomics) in exact
 * agreement: exactly one transition == exactly one atomic delta. */
static void drop_chunk(vw_guest_mem_t *gm, size_t ci) {
    gm_chunk_t *c = gm->l1[ci];
    if (!c || c->discarded) return;
    c->discarded = 1;
    atomic_add(&gm->a_committed_chunks, -1);   /* one chunk -> zero backing */
    uint32_t dp = c->dirty_pages;
    if (dp) {
        uint64_t now = __atomic_sub_fetch(&gm->a_dirty_pages, dp, __ATOMIC_RELAXED);
        if (now > ((uint64_t)1 << 63))
            __atomic_store_n(&gm->a_dirty_pages, 0, __ATOMIC_RELAXED);
    }
    memset(c->dirty, 0, sizeof c->dirty);
    c->dirty_pages = 0;
    c->committed_pages = 0;

    if (gm->mode == GM_MODE_CHUNK) {
        if (c->data) munmap(c->data, (size_t)GM_CHUNK);
        free(c);
        gm->l1[ci] = NULL;
    } else {
        reclaim_range(gm, chunk_base(gm, ci), GM_CHUNK);
        gm->discarded_chunks++;
    }
}

/* ---------------- create / destroy ---------------- */

vw_guest_mem_t *gm_create_ex(uint64_t size, int mode_hint) {
    vw_guest_mem_t *gm;
    uint64_t nchunks;

    if (size < GM_PAGE) size = GM_PAGE;
    nchunks = (size + GM_CHUNK - 1) / GM_CHUNK;

    gm = calloc(1, sizeof *gm);
    if (!gm) return NULL;

    gm->size = nchunks * GM_CHUNK;
    gm->l1_entries = (size_t)nchunks;
    gm->l1 = calloc(nchunks, sizeof(gm_chunk_t *));
    if (!gm->l1) { free(gm); return NULL; }

#if GM_HAVE_MADVISE
    /* only trust madvise when the runtime probe proves it discards */
    gm->have_madvise = probe_madvise_discards();
#endif

    if (mode_hint == GM_MODE_CHUNK) {
        gm->mode = GM_MODE_CHUNK;
    } else {
        void *p = mmap(NULL, (size_t)gm->size, PROT_READ | PROT_WRITE,
                       MAP_ANONYMOUS | MAP_PRIVATE | MAP_NORESERVE, -1, 0);
        if (p != MAP_FAILED) {
            gm->base = p;
            gm->mode = GM_MODE_RESERVE;
        } else {
            gm->mmap_failed = 1;
            gm->mode = GM_MODE_CHUNK;   /* runtime fallback */
        }
    }

    pthread_mutex_init(&gm->lock, NULL);
    return gm;
}

vw_guest_mem_t *gm_create(uint64_t size) {
    return gm_create_ex(size, GM_MODE_UNSET);
}

void gm_destroy(vw_guest_mem_t *gm) {
    size_t i;
    if (!gm) return;
    if (gm->base) munmap(gm->base, (size_t)gm->size);
    for (i = 0; i < gm->l1_entries; i++) {
        gm_chunk_t *c = gm->l1[i];
        if (!c) continue;
        if (c->data) munmap(c->data, (size_t)GM_CHUNK);
        free(c);
    }
    pthread_mutex_destroy(&gm->lock);
    free(gm->l1);
    free(gm);
}

/* ---------------- core paths ---------------- */

int gm_read(vw_guest_mem_t *gm, uint64_t gpa, void *buf, size_t len) {
    int rc;
    if ((rc = range_check(gm, gpa, len)) != GM_OK) return rc;
    if (!buf) return GM_ERR_ARG;

    size_t ci = (size_t)(gpa / GM_CHUNK);
    uint64_t off = gpa - (uint64_t)ci * GM_CHUNK;
    uint8_t *dst = (uint8_t *)buf, *src;
    size_t left = (size_t)len;

    while (left) {
        size_t n = (size_t)(GM_CHUNK - off);
        if (n > left) n = left;
        pthread_mutex_lock(&gm->lock);
        if (!gm->l1[ci]) {           /* never-committed chunk reads as zero */
            pthread_mutex_unlock(&gm->lock);
            memset(dst, 0, n);
        } else {
            src = chunk_base(gm, ci) + off;
            pthread_mutex_unlock(&gm->lock);
            memcpy(dst, src, n);
        }
        dst += n; left -= n;
        ci++; off = 0;
    }
    return GM_OK;
}

int gm_write(vw_guest_mem_t *gm, uint64_t gpa, const void *src, size_t len) {
    int rc;
    if ((rc = range_check(gm, gpa, len)) != GM_OK) return rc;
    if (!src) return GM_ERR_ARG;

    size_t ci = (size_t)(gpa / GM_CHUNK);
    uint64_t off = gpa - (uint64_t)ci * GM_CHUNK;
    const uint8_t *sp = (const uint8_t *)src;
    size_t left = (size_t)len;

    while (left) {
        size_t n = (size_t)(GM_CHUNK - off);
        if (n > left) n = left;

        pthread_mutex_lock(&gm->lock);
        rc = ensure_chunk(gm, ci);
        if (rc != GM_OK) { pthread_mutex_unlock(&gm->lock); return rc; }
        gm_chunk_t *c = gm->l1[ci];
        uint8_t *dst = chunk_base(gm, ci) + off;

        /* first-touch commit is implicit: in RESERVE mode the kernel backs
         * the page on this memcpy; in CHUNK mode the chunk mapping above is
         * the commitment. No memset, no mlock, no one-shot commit. */
        uint64_t p0 = off / GM_PAGE;
        uint64_t p1 = (off + n - 1) / GM_PAGE;
        for (uint64_t p = p0; p <= p1; p++) {
            uint64_t w = p >> 6, b = p & 63;
            if (!((c->dirty[w] >> b) & 1)) {
                c->dirty[w] |= (uint64_t)1 << b;
                c->dirty_pages++;
                c->committed_pages++;
                atomic_add(&gm->a_dirty_pages, 1);
            }
        }
        pthread_mutex_unlock(&gm->lock);

        memcpy(dst, sp, n);
        sp += n; left -= n;
        ci++; off = 0;
    }
    return GM_OK;
}

int gm_map(vw_guest_mem_t *gm, uint64_t gpa, uint64_t len) {
    int rc;
    if ((rc = range_check(gm, gpa, len)) != GM_OK) return rc;

    size_t ci = (size_t)(gpa / GM_CHUNK);
    size_t clast = (size_t)((gpa + len - 1) / GM_CHUNK);

    pthread_mutex_lock(&gm->lock);
    for (size_t i = ci; i <= clast; i++) {
        rc = ensure_chunk(gm, i);
        if (rc != GM_OK) { pthread_mutex_unlock(&gm->lock); return rc; }
    }
    pthread_mutex_unlock(&gm->lock);
    return GM_OK;
}

void *gm_resolve_page(vw_guest_mem_t *gm, uint64_t gpa) {
    void *p = NULL;
    if (!gm || gpa >= gm->size) return NULL;
    pthread_mutex_lock(&gm->lock);
    size_t ci = (size_t)(gpa / GM_CHUNK);
    if (ensure_chunk(gm, ci) == GM_OK) p = chunk_base(gm, ci);
    pthread_mutex_unlock(&gm->lock);
    return p;
}

int gm_discard(vw_guest_mem_t *gm, uint64_t gpa, uint64_t len) {
    int rc;
    if ((rc = range_check(gm, gpa, len)) != GM_OK) return rc;

    size_t ci = (size_t)(gpa / GM_CHUNK);
    size_t clast = (size_t)((gpa + len - 1) / GM_CHUNK);

    pthread_mutex_lock(&gm->lock);
    for (size_t i = ci; i <= clast; i++) {
        if (!gm->l1[i]) continue;
        drop_chunk(gm, i);
    }
    pthread_mutex_unlock(&gm->lock);
    return GM_OK;
}

int gm_discard_all(vw_guest_mem_t *gm) {
    if (!gm) return GM_ERR_ARG;
    return gm_discard(gm, 0, gm->size);
}

/* ---------------- stats ---------------- */

int gm_stats(const vw_guest_mem_t *gm, gm_stats_t *out) {
    if (!gm || !out) return GM_ERR_ARG;
    memset(out, 0, sizeof *out);
    pthread_mutex_lock((pthread_mutex_t *)&gm->lock);
    uint64_t dirty = 0, committed = 0;
    for (size_t i = 0; i < gm->l1_entries; i++) {
        gm_chunk_t *c = gm->l1[i];
        /* a discarded chunk has no resident pages (MADV_DONTNEED) or no
         * backing at all (munmap), so it must not count as committed. */
        if (!c || c->discarded) continue;
        committed++;
        dirty += c->dirty_pages;
    }
    pthread_mutex_unlock((pthread_mutex_t *)&gm->lock);
    out->guest_size = gm->size;
    out->mode = gm->mode;
    out->have_madvise = gm->have_madvise;
    out->discard_path = gm->discard_path;
    out->committed_chunks = committed;
    out->committed_bytes = committed * GM_CHUNK;
    out->dirty_pages = dirty;
    out->discarded_chunks = gm->discarded_chunks;
    out->oob_rejects = __atomic_load_n(&gm->a_oob_rejects, __ATOMIC_RELAXED);
    out->touched_pages = __atomic_load_n(&gm->a_dirty_pages, __ATOMIC_RELAXED);
    return GM_OK;
}

int gm_stats_signal_safe(const vw_guest_mem_t *gm, gm_stats_atomic_t *out) {
    /* atomics only: no malloc, no lock, signal-handler legal */
    if (!gm || !out) return GM_ERR_ARG;
    out->committed_chunks = __atomic_load_n(&gm->a_committed_chunks, __ATOMIC_RELAXED);
    out->dirty_pages = __atomic_load_n(&gm->a_dirty_pages, __ATOMIC_RELAXED);
    out->oob_rejects = __atomic_load_n(&gm->a_oob_rejects, __ATOMIC_RELAXED);
    return GM_OK;
}

/* ---------------- balloon / free-page reporting (reserved) ---------------- */

int gm_set_free_page_report(vw_guest_mem_t *gm, gm_free_page_report_fn fn, void *ctx) {
    if (!gm) return GM_ERR_ARG;
    gm->report_fn = fn;
    gm->report_ctx = ctx;
    return GM_OK;
}

int64_t gm_balloon_report_free(vw_guest_mem_t *gm, uint64_t gpa, uint64_t len) {
    if (!gm || !gm->report_fn || len == 0) return GM_ERR_ARG;
    if (gpa >= gm->size || len > gm->size - gpa) return GM_ERR_OOB;

    uint64_t reported = 0;
    size_t ci = (size_t)(gpa / GM_CHUNK);
    size_t clast = (size_t)((gpa + len - 1) / GM_CHUNK);

    pthread_mutex_lock(&gm->lock);
    for (size_t i = ci; i <= clast; i++) {
        gm_chunk_t *c = gm->l1[i];
        if (!c || c->dirty_pages) continue;      /* report clean chunks only */
        uint64_t base = (uint64_t)i * GM_CHUNK;
        if (gm->report_fn(gm->report_ctx, base, GM_CHUNK) == 0)
            reported += GM_CHUNK;
    }
    pthread_mutex_unlock((pthread_mutex_t *)&gm->lock);
    gm->reported_bytes += reported;
    return (int64_t)reported;
}

int64_t gm_balloon_report_free_all(vw_guest_mem_t *gm) {
    if (!gm) return GM_ERR_ARG;
    return gm_balloon_report_free(gm, 0, gm->size);
}

/* ---------------- diagnostics ---------------- */

int gm_mode_of(const vw_guest_mem_t *gm) { return gm ? gm->mode : GM_MODE_UNSET; }

const char *gm_mode_name(int mode) {
    switch (mode) {
        case GM_MODE_RESERVE: return "RESERVE(MAP_NORESERVE big-VA + first-touch)";
        case GM_MODE_CHUNK:   return "CHUNK(two-level table + 2MiB lazy mmap)";
        default: return "UNSET";
    }
}

uint64_t gm_size(const vw_guest_mem_t *gm) { return gm ? gm->size : 0; }
