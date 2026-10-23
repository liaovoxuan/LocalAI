/* guestmem.h — sparse guest-physical RAM prototype (no JIT, no guest exec).
 *
 * Part of experiments/harmony-nojit. NOT product code. NOT QEMU.
 *
 * Goals (see docs/harmony-nojit-runtime.md):
 *   - Fixed-size guest-physical address space (e.g. 8 GiB logical).
 *   - Reserve a large virtual mapping up front; pages become resident only
 *     on FIRST WRITE (first-touch commit). No full memset, no mlock, no
 *     one-shot physical commit.
 *   - Compile-time/runtime fallback: if MAP_NORESERVE or a reliable
 *     reserve/commit path is unavailable, fall back to a two-level page
 *     table with 2 MiB chunks lazily mmap'd on demand.
 *   - Guest read/write/map, out-of-bounds checks, dirty-page accounting,
 *     discard/reclaim (madvise when available, safe munmap reclamation in
 *     chunk mode), and RESERVED (not yet implemented as policy) balloon /
 *     free-page-reporting hooks.
 *   - No malloc and no locking inside any signal handler: this module
 *     installs NO signal handlers, and exposes gm_stats_signal_safe()
 *     which performs plain atomic loads only.
 */
#ifndef VW_GUESTMEM_H
#define VW_GUESTMEM_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define GM_PAGE            ((uint64_t)4096)
#define GM_CHUNK          ((uint64_t)2 * 1024 * 1024)   /* 2 MiB */
#define GM_PAGES_PER_CHUNK (GM_CHUNK / GM_PAGE)          /* 512 */

/* error codes (all failures are reported, never silently success) */
#define GM_OK          0
#define GM_ERR_ARG    (-1)   /* NULL args / len==0 / unaligned callers */
#define GM_ERR_OOB    (-2)   /* guest range outside the address space */
#define GM_ERR_NOMEM  (-3)   /* backing allocation failed */
#define GM_ERR_TRAP   (-4)   /* page could not be resolved */

typedef enum {
    GM_MODE_UNSET = 0,
    GM_MODE_RESERVE,   /* one big MAP_NORESERVE (or equivalent) reservation;
                        * first-touch commit done by the kernel */
    GM_MODE_CHUNK      /* two-level table; 2 MiB chunks mmap'd lazily */
} gm_mode_t;

typedef struct gm_stats {
    uint64_t guest_size;
    uint64_t committed_chunks;      /* chunks with resident backing */
    uint64_t committed_bytes;       /* committed_chunks * GM_CHUNK */
    uint64_t dirty_pages;           /* pages written since last discard */
    uint64_t touched_pages;         /* pages ever made resident */
    uint64_t discarded_chunks;
    uint64_t oob_rejects;
    int      mode;
    int      have_madvise;           /* madvisable AND probe-verified to discard */
    int      discard_path;           /* 0 = MADV_DONTNEED, 1 = MAP_FIXED re-anon,
                                      * -1 = reclamation failed */
} gm_stats_t;

/* signal-safe subset: plain atomic loads, no malloc, no lock.
 * (This module never installs signal handlers; this exists so a signal
 * handler could sample progress without violating the no-malloc/no-lock
 * rule.) */
typedef struct gm_stats_atomic {
    uint64_t committed_chunks;
    uint64_t dirty_pages;
    uint64_t oob_rejects;
} gm_stats_atomic_t;

/* balloon / free-page-reporting: RESERVED interface. An embedding layer may
 * register a callback that receives (gpa, len) for guest pages the guest has
 * DECLARED free. This prototype reports candidate chunks only; it applies no
 * policy of its own. */
typedef int (*gm_free_page_report_fn)(void *ctx, uint64_t gpa, uint64_t len);

typedef struct vw_guest_mem vw_guest_mem_t;

/* create a logical address space of size bytes (rounded up to GM_CHUNK).
 * mode_hint: GM_MODE_RESERVE or GM_MODE_CHUNK; anything else = auto
 * (RESERVE if a reserve mapping is available, else CHUNK). */
vw_guest_mem_t *gm_create_ex(uint64_t size, int mode_hint);
vw_guest_mem_t *gm_create(uint64_t size);           /* auto mode */
void           gm_destroy(vw_guest_mem_t *gm);

/* guest read/write. Bounds-checked; never writes out of range; never zeroes
 * untouched memory. First write to a page commits it (kernel first-touch in
 * RESERVE mode, chunk mmap in CHUNK mode). */
int gm_read (vw_guest_mem_t *gm, uint64_t gpa, void *buf, size_t len);
int gm_write(vw_guest_mem_t *gm, uint64_t gpa, const void *src, size_t len);

/* explicit map (commit backing) without writing; still lazy per-chunk. */
int gm_map(vw_guest_mem_t *gm, uint64_t gpa, uint64_t len);

/* discard/reclaim a range (must be chunk-aligned range or it is handled
 * per covered chunk). In RESERVE mode uses madvise(MADV_DONTNEED) when the
 * platform has it; in CHUNK mode munmaps covered chunks (safe reclamation).
 * Both modes drop dirty-page accounting for fully covered chunks. */
int gm_discard(vw_guest_mem_t *gm, uint64_t gpa, uint64_t len);
int gm_discard_all(vw_guest_mem_t *gm);

/* stats */
int gm_stats(const vw_guest_mem_t *gm, gm_stats_t *out);          /* locked */
int gm_stats_signal_safe(const vw_guest_mem_t *gm,
                         gm_stats_atomic_t *out);                 /* atomics only */

/* resolve the host page containing gpa (creating backing on demand);
 * returns NULL on trap/OOB. Internal use by the CPU TLB; safe to call from
 * normal (non-signal) context. */
void *gm_resolve_page(vw_guest_mem_t *gm, uint64_t gpa);

/* balloon / free-page-reporting hooks (reserved interface) */
int  gm_set_free_page_report(vw_guest_mem_t *gm,
                             gm_free_page_report_fn fn, void *ctx);
/* report clean (non-dirty) committed chunks within [gpa, gpa+len) to the
 * registered callback; returns bytes reported or GM_ERR_* on failure. */
int64_t gm_balloon_report_free(vw_guest_mem_t *gm, uint64_t gpa, uint64_t len);
int64_t gm_balloon_report_free_all(vw_guest_mem_t *gm);

/* diagnostics */
int         gm_mode_of(const vw_guest_mem_t *gm);
const char *gm_mode_name(int mode);
uint64_t    gm_size(const vw_guest_mem_t *gm);

#ifdef __cplusplus
}
#endif
#endif /* VW_GUESTMEM_H */
