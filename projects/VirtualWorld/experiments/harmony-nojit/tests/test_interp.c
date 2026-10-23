/* test_interp.c — no-JIT micro-op interpreter tests.
 * NOT product code, NOT QEMU. Host-native only: nothing here boots a guest,
 * spawns a process, or executes generated machine code.
 *
 * Covers: AArch64-subset decode, three dispatch engines (switch,
 * computed-goto, superinstruction-fused computed-goto), basic-block decode
 * cache reuse, software TLB hit rate, engine-to-engine determinism,
 * throughput, and trap behaviour (unsupported opcode / guest OOB).
 */
#include "nojit_cpu.h"
#include "util.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int checks = 0, fails = 0;

#define CHECK(cond, ...) do {                                        \
    checks++;                                                        \
    if (!(cond)) { fails++; printf("  FAIL: %d  ", __LINE__);        \
                   printf(__VA_ARGS__); printf("\n"); }               \
} while (0)

#define INFO(...) do { printf("  .. "); printf(__VA_ARGS__); printf("\n"); } while (0)

/* ---------------- tiny assembler for the supported subset ---------------- */
static uint8_t code[256 * 1024];
static uint32_t ncode;

static void emit(uint32_t w) { memcpy(code + ncode, &w, 4); ncode += 4; }

static uint32_t A_MOVZ(int rd, int hw, uint32_t imm) {
    return 0xD2800000u | ((uint32_t)hw << 21) | ((imm & 0xFFFFu) << 5) | (uint32_t)rd;
}
static uint32_t A_MOVN(int rd, int hw, uint32_t imm) {
    return 0x92800000u | ((uint32_t)hw << 21) | ((imm & 0xFFFFu) << 5) | (uint32_t)rd;
}
static uint32_t A_ADD_I(int rd, int rn, uint32_t imm) {
    return 0x91000000u | ((imm & 0xFFFu) << 10) | ((uint32_t)rn << 5) | (uint32_t)rd;
}
static uint32_t A_SUB_I(int rd, int rn, uint32_t imm) {
    return 0xD1000000u | ((imm & 0xFFFu) << 10) | ((uint32_t)rn << 5) | (uint32_t)rd;
}
static uint32_t A_SUBS_I(int rd, int rn, uint32_t imm) {
    return 0xF1000000u | ((imm & 0xFFFu) << 10) | ((uint32_t)rn << 5) | (uint32_t)rd;
}
static uint32_t A_ADD_R(int rd, int rn, int rm) {
    return 0x8B000000u | ((uint32_t)rm << 16) | ((uint32_t)rn << 5) | (uint32_t)rd;
}
static uint32_t A_SUB_R(int rd, int rn, int rm) {
    return 0xCB000000u | ((uint32_t)rm << 16) | ((uint32_t)rn << 5) | (uint32_t)rd;
}
static uint32_t A_SUBS_R(int rd, int rn, int rm) {
    return 0xEB000000u | ((uint32_t)rm << 16) | ((uint32_t)rn << 5) | (uint32_t)rd;
}
static uint32_t A_AND_R(int rd, int rn, int rm) {
    return 0x8A000000u | ((uint32_t)rm << 16) | ((uint32_t)rn << 5) | (uint32_t)rd;
}
static uint32_t A_MUL(int rd, int rn, int rm) {
    return 0x9B007C00u | ((uint32_t)rm << 16) | ((uint32_t)rn << 5) | (uint32_t)rd;
}
static uint32_t A_LDR(int rt, int rn) {
    return 0xF9400000u | ((uint32_t)rn << 5) | (uint32_t)rt;
}
static uint32_t A_STR(int rt, int rn) {
    return 0xF9000000u | ((uint32_t)rn << 5) | (uint32_t)rt;
}
static uint32_t A_STUR(int rt, int rn, int imm) {
    return 0xF8000000u | (((uint32_t)imm & 0x1FFu) << 12) | ((uint32_t)rn << 5) |
           (uint32_t)rt;
}
static uint32_t A_BC(int cond)   { return 0x54000000u | (uint32_t)cond; }

/* label fixups */
typedef struct { uint32_t at; unsigned char kind; unsigned char lbl; } fix_t;
static fix_t fixes[64];
static int nfix;
static uint32_t lbl_addr[16];

static void set_label(int l) { lbl_addr[l] = ncode; }
static void emit_b(int l)     { fixes[nfix++] = (fix_t){ ncode, 0, (unsigned char)l }; emit(0x14000000u); }
static void emit_bc(int c, int l) { fixes[nfix++] = (fix_t){ ncode, 1, (unsigned char)l }; emit(A_BC(c)); }
static void emit_cbnz(int r, int l) { fixes[nfix++] = (fix_t){ ncode, 2, (unsigned char)l }; emit(0xB5000000u | (uint32_t)r); }
static void emit_cbz(int r, int l)  { fixes[nfix++] = (fix_t){ ncode, 3, (unsigned char)l }; emit(0xB4000000u | (uint32_t)r); }

static void resolve(void) {
    for (int i = 0; i < nfix; i++) {
        uint32_t w;
        memcpy(&w, code + fixes[i].at, sizeof w);
        int64_t off = (int64_t)lbl_addr[fixes[i].lbl] - (int64_t)fixes[i].at;
        switch (fixes[i].kind) {
        case 0: w = (w & 0xFC000000u) | (uint32_t)((off / 4) & 0x03FFFFFF); break;
        case 1: w = (w & 0xFC00001Fu) | (((uint32_t)((off / 4) & 0x7FFFF)) << 5); break;
        case 2:
        case 3: w = (w & 0xFF00001Fu) | (((uint32_t)((off / 4) & 0x7FFFF)) << 5); break;
        }
        memcpy(code + fixes[i].at, &w, sizeof w);
    }
    nfix = 0;
}

static void prog_reset(void) { ncode = 0; nfix = 0; }

/* ---------------- register/memory snapshot for determinism ---------------- */
typedef struct { uint64_t regs[NJ_REGS]; uint64_t mem_csum; } snap_t;

static uint64_t mem_csum(vw_guest_mem_t *gm, uint64_t gpa, uint64_t len) {
    uint64_t h = 1469598103934665603ULL;
    uint8_t buf[4096];
    for (uint64_t off = 0; off < len;) {
        size_t n = (size_t)((len - off) < sizeof buf ? (len - off) : sizeof buf);
        if (gm_read(gm, gpa + off, buf, n) != GM_OK) {
            h ^= 0x9E3779B9; h *= 1099511628211ULL; off += n; continue;
        }
        for (size_t i = 0; i < n; i++) { h ^= buf[i]; h *= 1099511628211ULL; }
        off += n;
    }
    return h;
}

static void snapshot(nj_cpu_t *cpu, vw_guest_mem_t *gm,
                     uint64_t gpa, uint64_t len, snap_t *s) {
    memcpy(s->regs, cpu->regs, sizeof s->regs);
    s->mem_csum = len ? mem_csum(gm, gpa, len) : 0;
}

static int snaps_equal(const snap_t *a, const snap_t *b) {
    if (a->mem_csum != b->mem_csum) return 0;
    return memcmp(a->regs, b->regs, sizeof a->regs) == 0;
}

static void report_engine(const char *name, nj_cpu_t *cpu, int rc, double secs,
                          uint64_t instrs) {
    printf("  ENGINE %-6s rc=%d exit=%llu steps=%llu blocks=%llu fusions=%llu "
           "tlb=%llu/%llu %.3fs %.2f Mguest-instr/s\n",
           name, rc, (unsigned long long)cpu->exit_code,
           (unsigned long long)cpu->steps,
           (unsigned long long)cpu->block_compiles,
           (unsigned long long)cpu->super_fusions,
           (unsigned long long)cpu->tlb_hits,
           (unsigned long long)cpu->tlb_misses,
           secs,
           secs > 0 ? (double)instrs / secs / 1e6 : 0.0);
}

/* ---------------- programs ---------------- */

/* PROG_A: sum an array of 8-byte elements. Hot pair (LDR + ADD) becomes the
 * S_LOAD_ADD superinstruction under NJ_ENGINE_SUPER. */
#define NA_ELEMS   8192
#define A_BASE     0x00100000ULL     /* 1 MiB */
static void build_prog_a(void) {
    prog_reset();
    emit(A_MOVZ(5, 1, 0x10));      /* x5 = 1 MiB array pointer   */
    emit(A_MOVZ(6, 0, 0x2000));    /* x6 = 8192 elements         */
    emit(A_MOVZ(8, 0, 0));         /* x8 = sum = 0               */
    set_label(0);
    emit(A_LDR(9, 5));             /* x9  = [x5]                 */
    emit(A_ADD_R(8, 8, 9));        /* x8 += x9                   */
    emit(A_ADD_I(5, 5, 8));        /* x5 += 8                    */
    emit(A_SUB_I(6, 6, 1));        /* x6--                       */
    emit_cbnz(6, 0);               /* loop x6 times              */
    emit(A_MOVZ(10, 0, 0));        /* x10 = 0                    */
    emit(A_STUR(8, 10, 0));        /* [0] = sum                  */
    emit(A_ADD_R(0, 8, 31));       /* x0 = x8                    */
    emit(0xD65F0000u);             /* RET (x30)                  */
    resolve();
}

/* PROG_B: write base+i at ptr, ptr += 8. Hot pair (ADD + STR) becomes the
 * S_ADD_STORE superinstruction under NJ_ENGINE_SUPER. */
#define NB_ELEMS   4096
#define B_BASE     0x00200000ULL     /* 2 MiB */
static void build_prog_b(uint32_t iters) {
    prog_reset();
    emit(A_MOVZ(11, 1, 0x20));     /* x11 = 2 MiB output ptr     */
    emit(A_MOVZ(12, 0, 0));        /* x12 = i                    */
    emit(A_MOVZ(13, 0, 0x1111));   /* x13 = base value           */
    emit(A_MOVZ(16, 0, (uint16_t)iters)); /* x16 = inner limit  */
    emit(A_MOVZ(9, 0, 512));       /* x9  = outer rounds         */
    set_label(0);
    emit(A_MOVZ(12, 0, 0));        /* reset i                    */
    set_label(1);
    emit(A_ADD_R(14, 13, 12));     /* val = base + i             */
    emit(A_STR(14, 11));           /* [ptr] = val                */
    emit(A_ADD_I(11, 11, 8));      /* ptr += 8                   */
    emit(A_ADD_I(12, 12, 1));      /* i++                        */
    emit(A_SUBS_R(15, 12, 16));   /* flags from i - limit       */
    emit_bc(3, 1);                 /* B.CC while i < limit       */
    emit(A_SUB_I(9, 9, 1));
    emit_cbnz(9, 0);
    emit(A_ADD_R(0, 12, 31));      /* x0 = i                     */
    emit(0xD65F0000u);
    resolve();
}

/* PROG_C: pure ALU dispatch-bound loop (no guest memory access). */
#define OUTER_C 512
#define INNER_C 4096
static void build_prog_c(void) {
    prog_reset();
    emit(A_MOVZ(8, 0, 0));         /* x8 = acc                   */
    emit(A_MOVZ(9, 0, OUTER_C));   /* outer rounds               */
    set_label(0);
    emit(A_MOVZ(10, 0, INNER_C));  /* inner counter              */
    set_label(1);
    emit(A_ADD_I(8, 8, 1));        /* acc++                      */
    emit(A_SUB_I(10, 10, 1));      /* inner--                    */
    emit_cbnz(10, 1);
    emit(A_SUB_I(9, 9, 1));
    emit_cbnz(9, 0);
    emit(A_ADD_R(0, 8, 31));
    emit(0xD65F0000u);
    resolve();
}

static void build_prog_d(void) {
    prog_reset();
    emit(A_MOVN(1, 0, 0));         /* x1 = all ones              */
    emit(A_MOVZ(2, 0, 0xFF));      /* x2 = 255                   */
    emit(A_AND_R(3, 1, 2));        /* x3 = 255                   */
    emit(A_MOVZ(4, 0, 3));         /* x4 = 3                     */
    emit(A_MUL(5, 3, 4));          /* x5 = 765                   */
    emit(A_SUB_R(6, 5, 2));        /* x6 = 510                   */
    emit(A_SUBS_I(7, 6, 510));      /* x7 = 0, set Z/C            */
    emit_bc(1, 0);                 /* NE false: test fallthrough  */
    emit_b(1);                     /* unconditional forward branch */
    set_label(0);
    emit(A_MOVZ(0, 0, 0xBAD));     /* should be skipped           */
    emit_b(1);
    set_label(1);
    emit_cbz(7, 2);                /* CBZ true                    */
    emit(A_MOVZ(0, 0, 0xBAD));     /* should be skipped           */
    set_label(2);
    emit(A_MOVZ(0, 0, 42));
    emit(0xD65F0000u);
    resolve();
}

static int run_all_engines(vw_guest_mem_t *gm, const char *prog,
                           uint64_t expect_exit, snap_t snaps[3]) {
    int rc[3];
    double t[3];
    uint64_t ninstr[3];
    const char *names[3] = { "switch", "goto", "super" };
    memset(snaps, 0, 3 * sizeof *snaps);
    uint64_t checksum_gpa = 0, checksum_len = 8;
    if (strcmp(prog, "prog_b") == 0) {
        checksum_gpa = B_BASE;
        checksum_len = (uint64_t)NB_ELEMS * 512 * 8;
    }

    for (int e = 0; e < 3; e++) {
        if (strcmp(prog, "prog_b") == 0)
            CHECK(gm_discard(gm, B_BASE, checksum_len) == GM_OK,
                  "%s/%s discard output before run", prog, names[e]);
        nj_cpu_t *cpu = nj_create(code, ncode, gm);
        CHECK(cpu != NULL, "nj_create(%s)", names[e]);
        if (!cpu) { rc[e] = -1; t[e] = 0; ninstr[e] = 0; continue; }
        cpu->engine = e;
        cpu->fuse   = (e == 2);
        double t0 = now_sec();
        rc[e] = nj_run(cpu, 0);
        t[e] = now_sec() - t0;
        ninstr[e] = cpu->steps;
        CHECK(cpu->exit_code == expect_exit,
              "%s/%s exit=%llu want %llu", prog, names[e],
              (unsigned long long)cpu->exit_code,
              (unsigned long long)expect_exit);
        CHECK(rc[e] == NJ_TRAP_NONE, "%s/%s rc=%d", prog, names[e], rc[e]);
        if (e == NJ_ENGINE_SUPER &&
            (strcmp(prog, "prog_a") == 0 || strcmp(prog, "prog_b") == 0))
            CHECK(cpu->super_fusions > 0, "%s super engine performed fusion", prog);
        snapshot(cpu, gm, checksum_gpa, checksum_len, &snaps[e]);
        report_engine(names[e], cpu, rc[e], t[e], ninstr[e]);
        nj_destroy(cpu);
    }
    CHECK(snaps_equal(&snaps[0], &snaps[1]),
          "%s: switch and computed-goto disagree", prog);
    CHECK(snaps_equal(&snaps[0], &snaps[2]),
          "%s: switch and superinstruction disagree", prog);
    return 0;
}

static void init_prog_a_data(vw_guest_mem_t *gm) {
    uint64_t vals[NA_ELEMS];
    for (uint32_t i = 0; i < NA_ELEMS; i++) vals[i] = (uint64_t)i * 3 + 1;
    CHECK(gm_write(gm, A_BASE, vals, sizeof vals) == GM_OK,
          "initialize prog_a array data");
}

static void test_tlb_reclaim(void) {
    printf("[T4] software TLB hit, reclaim and explicit invalidation\n");
    vw_guest_mem_t *gm = gm_create_ex(4ULL * 1024 * 1024, GM_MODE_CHUNK);
    CHECK(gm != NULL, "create TLB test memory");
    if (!gm) return;
    uint8_t tiny[] = { 0x1F, 0x20, 0x03, 0xD5, 0xC0, 0x03, 0x5F, 0xD6 };
    nj_cpu_t *cpu = nj_create(tiny, sizeof tiny, gm);
    CHECK(cpu != NULL, "create TLB test CPU");
    if (!cpu) { gm_destroy(gm); return; }
    uint64_t got = 0;
    CHECK(nj_mem_write64(cpu, 0x1000, 0x123456789ABCDEF0ULL) == NJ_TRAP_NONE,
          "TLB initial write");
    CHECK(nj_mem_read64(cpu, 0x1000, &got) == NJ_TRAP_NONE &&
          got == 0x123456789ABCDEF0ULL, "TLB read hit/value");
    CHECK(cpu->tlb_misses >= 1 && cpu->tlb_hits >= 1,
          "TLB counters miss=%llu hit=%llu",
          (unsigned long long)cpu->tlb_misses,
          (unsigned long long)cpu->tlb_hits);
    CHECK(gm_discard(gm, 0, GM_CHUNK) == GM_OK, "discard mapped TLB chunk");
    nj_tlb_flush(cpu);
    got = UINT64_MAX;
    CHECK(nj_mem_read64(cpu, 0x1000, &got) == NJ_TRAP_NONE && got == 0,
          "TLB flush after discard observes zero-filled reclaimed page");
    nj_destroy(cpu);
    gm_destroy(gm);
}

static void test_traps(vw_guest_mem_t *gm) {
    printf("[T5] unsupported opcode, memory OOB and cross-chunk accesses\n");
    uint8_t bad[4] = { 0, 0, 0, 0 };
    for (int engine = NJ_ENGINE_SWITCH; engine <= NJ_ENGINE_SUPER; engine++) {
        nj_cpu_t *cpu = nj_create(bad, sizeof bad, gm);
        CHECK(cpu != NULL, "create bad-op CPU engine=%d", engine);
        if (!cpu) continue;
        cpu->engine = engine;
        cpu->fuse = (engine == NJ_ENGINE_SUPER);
        CHECK(nj_run(cpu, 0) == NJ_TRAP_BADOP,
              "unsupported opcode traps engine=%d", engine);
        nj_destroy(cpu);
    }

    uint64_t got = 0;
    CHECK(nj_mem_read64(NULL, 0, &got) == NJ_TRAP_MALFORM,
          "NULL CPU read is malformed request");

    nj_cpu_t *probe = nj_create(bad, sizeof bad, gm);
    CHECK(probe != NULL, "create direct OOB probe CPU");
    if (probe) {
        CHECK(nj_mem_read64(probe, gm_size(gm), &got) == NJ_TRAP_MEM,
              "read at guest_size traps");
        CHECK(nj_mem_read64(probe, gm_size(gm) - 4, &got) == NJ_TRAP_MEM,
              "8-byte read straddling guest_size traps");
        CHECK(nj_mem_write64(probe, gm_size(gm) - 4, 1) == NJ_TRAP_MEM,
              "8-byte write straddling guest_size traps");
        nj_destroy(probe);
    }

    uint32_t oob_words[3] = {
        A_MOVZ(1, 1, (uint32_t)(gm_size(gm) >> 16)),
        A_LDR(2, 1),
        0xD65F0000u
    };
    for (int engine = NJ_ENGINE_SWITCH; engine <= NJ_ENGINE_SUPER; engine++) {
        nj_cpu_t *cpu = nj_create((const uint8_t *)oob_words, sizeof oob_words, gm);
        CHECK(cpu != NULL, "create instruction OOB CPU engine=%d", engine);
        if (!cpu) continue;
        cpu->engine = engine;
        cpu->fuse = (engine == NJ_ENGINE_SUPER);
        CHECK(nj_run(cpu, 0) == NJ_TRAP_MEM,
              "instruction OOB traps engine=%d", engine);
        nj_destroy(cpu);
    }

    /* A valid 64-bit access straddling a 2 MiB chunk must use the safe slow
     * path rather than dereference across independently mapped chunks. */
    const uint64_t edge = GM_CHUNK - 4;
    uint32_t lo = 0xA1B2C3D4u, hi = 0xE5F60718u;
    CHECK(gm_write(gm, edge, &lo, sizeof lo) == GM_OK, "seed cross-chunk low half");
    CHECK(gm_write(gm, edge + 4, &hi, sizeof hi) == GM_OK, "seed cross-chunk high half");
    probe = nj_create(bad, sizeof bad, gm);
    if (probe) {
        CHECK(nj_mem_read64(probe, edge, &got) == NJ_TRAP_NONE,
              "cross-chunk 64-bit read succeeds");
        uint64_t want = (uint64_t)lo | ((uint64_t)hi << 32);
        CHECK(got == want, "cross-chunk value=0x%016llx want 0x%016llx",
              (unsigned long long)got, (unsigned long long)want);
        CHECK(nj_mem_write64(probe, edge, 0x1122334455667788ULL) == NJ_TRAP_NONE,
              "cross-chunk 64-bit write succeeds");
        CHECK(nj_mem_read64(probe, edge, &got) == NJ_TRAP_NONE &&
              got == 0x1122334455667788ULL, "cross-chunk write/read roundtrip");
        nj_destroy(probe);
    }
}

int main(void) {
    printf("== harmony-nojit: interpreter tests [%s] ==\n", host_os());
    printf("rss_start = %llu bytes\n", (unsigned long long)rss_bytes());
    vw_guest_mem_t *gm = gm_create(32ULL * 1024 * 1024);
    CHECK(gm != NULL, "create 32MiB interpreter guest RAM");
    if (!gm) return 1;

    snap_t snaps[3];
    printf("[T1] array sum + LOAD/ADD fusion\n");
    init_prog_a_data(gm);
    build_prog_a();
    uint64_t sum_a = (uint64_t)NA_ELEMS * (NA_ELEMS - 1) * 3 / 2 + NA_ELEMS;
    run_all_engines(gm, "prog_a", sum_a, snaps);
    uint64_t got = 0;
    CHECK(gm_read(gm, 0, &got, 8) == GM_OK && got == sum_a,
          "prog_a stored sum=%llu want %llu",
          (unsigned long long)got, (unsigned long long)sum_a);
    CHECK(snaps[2].mem_csum == mem_csum(gm, 0, 8),
          "prog_a result memory checksum captured");
    CHECK(snaps[2].regs[8] == sum_a, "prog_a x8 sum=%llu want %llu",
          (unsigned long long)snaps[2].regs[8], (unsigned long long)sum_a);
    CHECK(snaps[2].regs[0] == sum_a, "prog_a x0 return value");

    printf("[T2] repeated stores + ADD/STORE fusion + CC branch\n");
    build_prog_b(NB_ELEMS);
    uint64_t rss_b_before = rss_bytes();
    run_all_engines(gm, "prog_b", NB_ELEMS, snaps);
    uint64_t rss_b_after = rss_bytes();
    printf("  RESULT interpreter_workload_rss_before=%llu after=%llu delta=%lld bytes\n",
           (unsigned long long)rss_b_before,
           (unsigned long long)rss_b_after,
           (long long)rss_b_after - (long long)rss_b_before);
    CHECK(rss_b_after < 1536ULL * 1024 * 1024,
          "interpreter workload RSS below 1.5GiB (got %llu)",
          (unsigned long long)rss_b_after);
    uint64_t first = 0, last = 0;
    uint64_t total_b = (uint64_t)NB_ELEMS * 512;
    CHECK(gm_read(gm, B_BASE, &first, 8) == GM_OK && first == 0x1111,
          "prog_b first output=%llu want %u",
          (unsigned long long)first, 0x1111u);
    CHECK(gm_read(gm, B_BASE + (total_b - 1) * 8, &last, 8) == GM_OK &&
          last == 0x1111ULL + NB_ELEMS - 1,
          "prog_b last output=%llu want %llu",
          (unsigned long long)last,
          (unsigned long long)(0x1111ULL + NB_ELEMS - 1));
    CHECK(snaps[2].mem_csum == mem_csum(gm, B_BASE, total_b * 8),
          "prog_b final output checksum captured");

    printf("[T3] pure ALU dispatch workload + reset semantics\n");
    build_prog_c();
    uint64_t expect_c = (uint64_t)OUTER_C * INNER_C;
    run_all_engines(gm, "prog_c", expect_c, snaps);
    CHECK(snaps[0].regs[8] == expect_c && snaps[1].regs[8] == expect_c &&
          snaps[2].regs[8] == expect_c, "prog_c accumulator across all engines");
    nj_cpu_t *reset_cpu = nj_create(code, ncode, gm);
    CHECK(reset_cpu != NULL, "create reset test CPU");
    if (reset_cpu) {
        reset_cpu->engine = NJ_ENGINE_SWITCH;
        CHECK(nj_run(reset_cpu, 0) == NJ_TRAP_NONE && reset_cpu->exit_code == expect_c,
              "run before reset");
        nj_reset(reset_cpu);
        CHECK(nj_run(reset_cpu, 0) == NJ_TRAP_NONE && reset_cpu->exit_code == expect_c,
              "run after reset");
        CHECK(reset_cpu->block_compiles < reset_cpu->steps,
              "decode cache reused across reset");
        nj_destroy(reset_cpu);
    }

    printf("[T4] MOVN/AND/MUL/SUB, NZCV, B.cond/B/CBZ\n");
    build_prog_d();
    run_all_engines(gm, "prog_d", 42, snaps);
    for (int e = 0; e < 3; e++) {
        CHECK(snaps[e].regs[1] == UINT64_MAX, "prog_d x1 MOVN engine=%d", e);
        CHECK(snaps[e].regs[3] == 255, "prog_d x3 AND engine=%d", e);
        CHECK(snaps[e].regs[5] == 765, "prog_d x5 MUL engine=%d", e);
        CHECK(snaps[e].regs[6] == 510, "prog_d x6 SUB engine=%d", e);
        CHECK(snaps[e].regs[7] == 0, "prog_d x7 SUBS engine=%d", e);
    }

    test_tlb_reclaim();
    test_traps(gm);

    gm_destroy(gm);
    printf("rss_end = %llu bytes\n", (unsigned long long)rss_bytes());
    printf("== summary: %d checks, %d failures ==\n", checks, fails);
    return fails == 0 ? 0 : 1;
}
