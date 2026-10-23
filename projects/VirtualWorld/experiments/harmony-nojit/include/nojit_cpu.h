/* nojit_cpu.h — no-JIT guest CPU prototype (micro-op bytecode interpreter).
 *
 * Part of experiments/harmony-nojit. NOT product code. NOT QEMU, NOT TCI.
 *
 * Hard properties (enforced by tests/scan_nojit.sh):
 *   - NEVER generates machine code.
 *   - NEVER requests PROT_EXEC, never mmaps executable memory, never calls
 *     mprotect RX/RWX, never emits JIT code of any kind.
 *   - Dispatch engines: portable switch, direct-threaded computed-goto
 *     (when the compiler supports GNU labels-as-values), and a fused
 *     superinstruction variant of computed-goto.
 *   - Basic-block decode cache: guest code is decoded to a compact micro-op
 *     array once per basic block and cached.
 *   - Software TLB in front of guest memory.
 *   - Guest ISA: a self-contained AArch64 INSTRUCTION SUBSET (see the decoder
 *     list in nojit_cpu.c). This is not a complete AArch64 CPU and makes no
 *     claim of near-native performance.
 */
#ifndef VW_NOJIT_CPU_H
#define VW_NOJIT_CPU_H

#include <stdint.h>
#include <stddef.h>
#include "guestmem.h"

#ifdef __cplusplus
extern "C" {
#endif

#define NJ_REGS          32
#define NJ_TLB_ENTRIES   512
#define NJ_MAX_BLOCKS    1024      /* direct-mapped decode cache */
#define NJ_MAX_UOPS      64        /* micro-ops per basic block */

/* guest traps / outcomes */
#define NJ_TRAP_NONE     0
#define NJ_TRAP_MEM      1         /* guest memory fault (OOB etc.) */
#define NJ_TRAP_BADOP    2         /* instruction outside the supported subset */
#define NJ_TRAP_MALFORM  3         /* block decode failure */

/* dispatch engines */
typedef enum {
    NJ_ENGINE_SWITCH = 0,   /* portable switch dispatch                 */
    NJ_ENGINE_GOTO,         /* computed-goto (labels-as-values) dispatch */
    NJ_ENGINE_SUPER         /* computed-goto + superinstruction fusion   */
} nj_engine_t;

/* micro-op */
typedef struct nj_uop {
    uint8_t  op;
    uint8_t  a, b, c, d;    /* register operands */
    int64_t  imm;           /* immediate / branch target */
} nj_uop_t;

typedef struct nj_block {
    uint64_t guest_pc;
    uint32_t seq;            /* recompile generation */
    uint32_t ninstr;
    uint16_t nuops;
    int      fused;
    nj_uop_t  uops[NJ_MAX_UOPS];
} nj_block_t;

typedef struct nj_cpu {
    uint64_t regs[NJ_REGS];
    uint64_t pc;
    uint64_t exit_code;
    uint64_t nzcv;           /* N Z C V in bits 31..28 (AArch64 layout) */
    uint64_t steps;         /* guest instructions retired (proxy count) */
    int      halted;         /* set by NJOP_EXIT */

    /* guest code */
    const uint8_t *code;
    uint64_t code_len;

    /* memory: guestmem (may be NULL) or flat scratch */
    vw_guest_mem_t *gm;
    uint8_t *lin;            /* optional flat backing (test-only) */
    uint64_t lin_size;

    /* decode cache (direct-mapped by pc) */
    nj_block_t *blocks[NJ_MAX_BLOCKS];
    uint32_t    block_seq;
    uint64_t    block_compiles;

    /* superinstruction fusion counter */
    uint64_t    super_fusions;

    /* software TLB */
    struct { uint64_t tag; uint8_t *host; int valid; } tlb[NJ_TLB_ENTRIES];
    uint64_t tlb_hits, tlb_misses;

    /* stats */
    uint64_t guest_instr;    /* executed micro-ops / guest instrs */
    uint64_t guest_op_count; /* decoded guest instructions */

    int engine;
    int fuse;
} nj_cpu_t;

/* --- lifecycle --- */
nj_cpu_t *nj_create(const uint8_t *code, uint64_t code_len, vw_guest_mem_t *gm);
void      nj_destroy(nj_cpu_t *cpu);
void      nj_reset(nj_cpu_t *cpu);

/* run from entry_pc until a terminator; returns NJ_TRAP_* (0 = clean exit).
 * exit_code is returned via cpu->exit_code. */
int       nj_run(nj_cpu_t *cpu, uint64_t entry_pc);

/* memory accessors used by tests (bounds-checked) */
int       nj_mem_read64 (nj_cpu_t *cpu, uint64_t va, uint64_t *out);
int       nj_mem_write64(nj_cpu_t *cpu, uint64_t va, uint64_t val);

/* Invalidate the software TLB. MUST be called by the embedder after any
 * gm_discard()/reclaim that could unmap or decommit a guest range: in CHUNK
 * mode a stale entry would point at an unmapped host page. */
void      nj_tlb_flush(nj_cpu_t *cpu);

/* decode a single guest instruction subset entry (exposed for tests) */
int       nj_decode_instr(uint32_t insn, nj_uop_t *out, int *is_terminator);

#ifdef __cplusplus
}
#endif
#endif /* VW_NOJIT_CPU_H */
