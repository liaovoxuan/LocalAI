/* nojit_cpu.c — no-JIT guest CPU prototype.
 *
 * Micro-op bytecode interpreter over a self-contained AArch64 instruction
 * SUBSET. No machine code is ever generated; no PROT_EXEC / executable mmap /
 * mprotect RX/RWX appears anywhere in this file (verified by
 * tests/scan_nojit.sh).
 *
 * Engines:
 *   NJ_ENGINE_SWITCH : portable for(;;) switch(op) dispatch
 *   NJ_ENGINE_GOTO   : computed-goto dispatch (GNU labels-as-values)
 *   NJ_ENGINE_SUPER  : computed-goto dispatch over fused blocks
 *                      (superinstructions, shared decode cache)
 */
#include "nojit_cpu.h"
#include "nj_ops.h"

#include <stdlib.h>
#include <string.h>

/* ============ condition codes (NZCV in AArch64 order) ============ */
static int nj_cond(uint64_t nzcv, uint8_t c, uint64_t d) {
    uint64_t N = (nzcv >> 31) & 1, Z = (nzcv >> 30) & 1;
    uint64_t C = (nzcv >> 29) & 1, V = (nzcv >> 28) & 1;
    switch (c) {
        case 0:  return (int)Z;                       /* EQ */
        case 1:  return !Z;                           /* NE */
        case 2:  return (int)C;                       /* CS */
        case 3:  return !C;                           /* CC */
        case 4:  return (int)N;                       /* MI */
        case 5:  return !N;                           /* PL */
        case 6:  return (int)V;                       /* VS */
        case 7:  return !V;                           /* VC */
        case 8:  return (int)(C && !Z);               /* HI */
        case 9:  return (int)(!C || Z);               /* LS */
        case 10: return (int)(N == V);                /* GE */
        case 11: return (int)(N != V);                /* LT */
        case 12: return (int)(!Z && (N == V));        /* GT */
        case 13: return (int)(Z || (N != V));         /* LE */
        case NJOP_CBZ:  return (d == 0);
        case NJOP_CBNZ: return (d != 0);
        default: return 0;
    }
}

static inline void set_nz(uint64_t *nzcv, uint64_t r) {
    *nzcv &= ~((uint64_t)3 << 30);  /* replace N/Z only; preserve C/V */
    *nzcv |= ((r >> 63) & 1u) << 31;
    if (r == 0) *nzcv |= 1u << 30;
}
static inline void set_add_flags(uint64_t *nzcv, uint64_t a, uint64_t b, uint64_t r) {
    uint64_t c = (r < a);
    uint64_t v = (~(a ^ b) & (a ^ r)) >> 63;
    *nzcv &= 0x0FFFFFFFu;
    *nzcv |= c << 29 | v << 28;
    set_nz(nzcv, r);
}
static inline void set_sub_flags(uint64_t *nzcv, uint64_t a, uint64_t b, uint64_t r) {
    uint64_t c = (a >= b);
    uint64_t v = ((a ^ b) & (a ^ r)) >> 63;
    *nzcv &= 0x0FFFFFFFu;
    *nzcv |= c << 29 | v << 28;
    set_nz(nzcv, r);
}

/* ---------------- software TLB ---------------- */
static uint8_t *nj_translate(nj_cpu_t *cpu, uint64_t va) {
    if (cpu->gm) {
        uint64_t size = gm_size(cpu->gm);
        if (va > size || size - va < 8) return NULL;
    } else if (cpu->lin) {
        if (va > cpu->lin_size || cpu->lin_size - va < 8) return NULL;
    }
    uint64_t page = va & ~(uint64_t)4095;
    size_t idx = (size_t)((va >> 12) & (NJ_TLB_ENTRIES - 1));
    if (cpu->tlb[idx].valid && cpu->tlb[idx].tag == page) {
        cpu->tlb_hits++;
        return cpu->tlb[idx].host + (va & 4095);
    }
    cpu->tlb_misses++;
    uint8_t *host = NULL;
    if (cpu->gm) {
        uint8_t *chunk = gm_resolve_page(cpu->gm, va);
        if (!chunk) return NULL;
        host = chunk + (va & (GM_CHUNK - 1));
    } else if (cpu->lin) {
        if (va + 8 > cpu->lin_size) return NULL;
        host = cpu->lin + va;
    } else {
        return NULL;
    }
    cpu->tlb[idx].tag = page;
    cpu->tlb[idx].host = host - (va & 4095);
    cpu->tlb[idx].valid = 1;
    return host;
}

static int nj_load64(nj_cpu_t *cpu, uint64_t va, uint64_t *out) {
    if (!cpu || !out) return NJ_TRAP_MALFORM;
    if (cpu->gm && (va & (GM_CHUNK - 1)) > GM_CHUNK - 8)
        return gm_read(cpu->gm, va, out, 8) == GM_OK ? NJ_TRAP_NONE : NJ_TRAP_MEM;
    uint8_t *p = nj_translate(cpu, va);
    if (!p) return NJ_TRAP_MEM;
    memcpy(out, p, 8);
    return NJ_TRAP_NONE;
}

static int nj_store64(nj_cpu_t *cpu, uint64_t va, uint64_t val) {
    if (!cpu) return NJ_TRAP_MALFORM;
    if (cpu->gm && (va & (GM_CHUNK - 1)) > GM_CHUNK - 8)
        return gm_write(cpu->gm, va, &val, 8) == GM_OK ? NJ_TRAP_NONE : NJ_TRAP_MEM;
    uint8_t *p = nj_translate(cpu, va);
    if (!p) return NJ_TRAP_MEM;
    memcpy(p, &val, 8);
    return NJ_TRAP_NONE;
}

int nj_mem_read64(nj_cpu_t *cpu, uint64_t va, uint64_t *out) {
    return nj_load64(cpu, va, out);
}

int nj_mem_write64(nj_cpu_t *cpu, uint64_t va, uint64_t val) {
    return nj_store64(cpu, va, val);
}

void nj_tlb_flush(nj_cpu_t *cpu) {
    if (!cpu) return;
    for (size_t i = 0; i < NJ_TLB_ENTRIES; i++) cpu->tlb[i].valid = 0;
}

/* ============ AArch64 subset decoder (non-branch ops) ============ */

static uint32_t le32dec(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

int nj_decode_instr(uint32_t insn, nj_uop_t *u, int *is_term) {
    *is_term = 0;
    uint32_t op8 = insn & 0xFF000000;

    /* MOVZ / MOVN (64-bit). MOVK is NOT a pure functional micro-op (it merges
     * into the previous register value), so it is rejected as unsupported
     * instead of silently executing wrong code. */
    {
        uint32_t cls = insn & 0xFF800000;
        if (cls == 0xD2800000 || cls == 0x92800000) {
            uint32_t hw = (insn >> 21) & 3;
            uint32_t imm16 = (insn >> 5) & 0xFFFF;
            uint32_t rd = insn & 31;
            uint64_t val = (uint64_t)imm16 << (16 * hw);
            if (cls == 0x92800000) val = ~val;          /* MOVN */
            u->op = NJOP_MOVI; u->a = rd; u->imm = (int64_t)val;
            return 1;
        }
    }

    /* ADD/ADDS/SUB/SUBS immediate */
    if (op8 == 0x91000000 || op8 == 0xB1000000 ||
        op8 == 0xD1000000 || op8 == 0xF1000000) {
        uint32_t sh = (insn >> 22) & 1;
        uint64_t imm = (insn >> 10) & 0xFFF;
        if (sh) imm <<= 12;
        uint32_t rn = (insn >> 5) & 31, rd = insn & 31;
        if (op8 == 0x91000000) { u->op = NJOP_ADDI;  u->a = rd; u->b = rn; u->imm = (int64_t)imm; }
        else if (op8 == 0xB1000000) { u->op = NJOP_ADDS_I; u->a = rd; u->b = rn; u->imm = (int64_t)imm; }
        else if (op8 == 0xD1000000) { u->op = NJOP_SUBI; u->a = rd; u->b = rn; u->imm = (int64_t)imm; }
        else { u->op = NJOP_SUBS_I; u->a = rd; u->b = rn; u->imm = (int64_t)imm; }
        return 1;
    }

    /* ADD/ADDS/SUB/SUBS shifted register (LSL #0 only) */
    {
        uint32_t lg = insn & 0xFF200000;
        if (lg == 0x8B000000 || lg == 0xAB000000 ||
            lg == 0xCB000000 || lg == 0xEB000000) {
            if (((insn >> 22) & 3) != 0 || ((insn >> 10) & 63) != 0) return 0;
            uint32_t rm = (insn >> 16) & 31, rn = (insn >> 5) & 31, rd = insn & 31;
            if (lg == 0x8B000000) { u->op = NJOP_ADD;    u->a = rd; u->b = rn; u->c = rm; }
            else if (lg == 0xAB000000) { u->op = NJOP_ADDS_R; u->a = rd; u->b = rn; u->c = rm; }
            else if (lg == 0xCB000000) { u->op = NJOP_SUB;   u->a = rd; u->b = rn; u->c = rm; }
            else { u->op = NJOP_SUBS_R; u->a = rd; u->b = rn; u->c = rm; }
            return 1;
        }
    }

    /* AND/ORR/EOR shifted register (N=0, LSL #0 only) */
    {
        uint32_t lg = insn & 0xFF200000;
        if (lg == 0x8A000000 || lg == 0xAA000000 || lg == 0xCA000000) {
            if (insn & 0x00200000) return 0;             /* N=1 unsupported */
            if (((insn >> 22) & 3) != 0 || ((insn >> 10) & 63) != 0) return 0;
            uint32_t rm = (insn >> 16) & 31, rn = (insn >> 5) & 31, rd = insn & 31;
            if (lg == 0x8A000000) { u->op = NJOP_AND; u->a = rd; u->b = rn; u->c = rm; }
            else if (lg == 0xAA000000) { u->op = NJOP_ORR; u->a = rd; u->b = rn; u->c = rm; }
            else { u->op = NJOP_EOR; u->a = rd; u->b = rn; u->c = rm; }
            return 1;
        }
    }

    /* MUL = MADD Rd,Rn,Rm,XZR (64-bit) */
    if ((insn & 0xFFE0FC00) == 0x9B007C00) {
        u->op = NJOP_MUL; u->a = insn & 31; u->b = (insn >> 5) & 31; u->c = (insn >> 16) & 31;
        return 1;
    }

    /* LSLV / LSRV / ASRV */
    if ((insn & 0xFFE0FC00) == 0x9AC02000) { u->op = NJOP_LSLV; u->a = insn & 31; u->b = (insn >> 5) & 31; u->c = (insn >> 16) & 31; return 1; }
    if ((insn & 0xFFE0FC00) == 0x9AC02400) { u->op = NJOP_LSRV; u->a = insn & 31; u->b = (insn >> 5) & 31; u->c = (insn >> 16) & 31; return 1; }
    if ((insn & 0xFFE0FC00) == 0x9AC02800) { u->op = NJOP_ASRV; u->a = insn & 31; u->b = (insn >> 5) & 31; u->c = (insn >> 16) & 31; return 1; }

    /* LDR/STR unsigned offset (64-bit) */
    if ((insn & 0xFFC00000) == 0xF9400000 || (insn & 0xFFC00000) == 0xF9000000) {
        uint64_t imm = ((uint64_t)((insn >> 10) & 0xFFF)) * 8;
        uint32_t rn = (insn >> 5) & 31, rt = insn & 31;
        u->imm = (int64_t)imm; u->a = rt; u->b = rn;
        u->op = ((insn & 0xFFC00000) == 0xF9400000) ? NJOP_LOAD : NJOP_STORE;
        return 1;
    }

    /* LDUR/STUR unscaled (64-bit) */
    if ((insn & 0xFFC00000) == 0xF8400000 || (insn & 0xFFC00000) == 0xF8000000) {
        int64_t imm = (int64_t)((insn >> 12) & 0x1FF);
        if (imm & 0x100) imm -= 0x200;
        uint32_t rn = (insn >> 5) & 31, rt = insn & 31;
        u->imm = imm; u->a = rt; u->b = rn;
        u->op = ((insn & 0xFFC00000) == 0xF8400000) ? NJOP_LOAD : NJOP_STORE;
        return 1;
    }

    /* NOP */
    if (insn == 0xD503201F) { u->op = NJOP_NOP; return 1; }

    return 0;
}

/* ============ basic-block compile (branch ops included) ============ */

static size_t block_index(uint64_t pc) {
    return (size_t)((pc >> 2) & (NJ_MAX_BLOCKS - 1));
}

static nj_block_t *get_block(nj_cpu_t *cpu, uint64_t pc) {
    size_t idx = block_index(pc);
    nj_block_t *b = cpu->blocks[idx];
    if (b && b->guest_pc == pc && b->fused == cpu->fuse) return b;
    if (!b) {
        b = calloc(1, sizeof *b);
        if (!b) return NULL;
        cpu->blocks[idx] = b;
    }

    b->guest_pc = pc;
    b->fused = cpu->fuse;
    b->nuops = 0;
    b->ninstr = 0;
    cpu->block_compiles++;

    uint64_t at = pc;
    while (at + 4 <= cpu->code_len && b->nuops < NJ_MAX_UOPS - 2) {
        uint32_t insn = le32dec(cpu->code + at);
        uint32_t op8 = insn & 0xFC000000;
        at += 4;

        if (op8 == 0x14000000) {                          /* B (PC-relative) */
            int64_t off = (int64_t)(insn & 0x03FFFFFF);
            if (off & 0x02000000) off -= 0x04000000;
            off *= 4;
            b->uops[b->nuops].op = NJOP_BRIMM;
            b->uops[b->nuops].imm = (int64_t)(at - 4) + off;
            b->nuops++; b->ninstr++;
            return b;
        }
        if (op8 == 0x54000000) {                          /* B.cond (PC-relative) */
            int64_t off = (int64_t)((insn >> 5) & 0x7FFFF);
            if (off & 0x40000) off -= 0x80000;
            off *= 4;
            b->uops[b->nuops].op = NJOP_BRCOND;
            b->uops[b->nuops].c = (uint8_t)(insn & 15);
            b->uops[b->nuops].imm = (int64_t)(at - 4) + off;
            b->nuops++; b->ninstr++;
            return b;
        }
        if ((insn & 0x7E000000u) == 0x34000000u) {        /* CBZ / CBNZ */
            int64_t off = (int64_t)((insn >> 5) & 0x7FFFF);
            if (off & 0x40000) off -= 0x80000;
            off *= 4;
            b->uops[b->nuops].op = NJOP_BRCOND;
            b->uops[b->nuops].c = (insn & 0x01000000u) ? NJOP_CBNZ : NJOP_CBZ;
            b->uops[b->nuops].d = (uint8_t)(insn & 31);
            b->uops[b->nuops].imm = (int64_t)(at - 4) + off;
            b->nuops++; b->ninstr++;
            return b;
        }
        if ((insn & 0xFFFFFC1F) == 0xD65F0000) {          /* RET */
            b->uops[b->nuops].op = NJOP_EXIT;
            b->nuops++; b->ninstr++;
            return b;
        }

        nj_uop_t u; int term = 0;
        if (!nj_decode_instr(insn, &u, &term)) {
            b->uops[b->nuops].op = NJOP_TRAP_BADOP;
            b->uops[b->nuops].imm = (int64_t)(int32_t)insn;
            b->nuops++;
            return b;
        }
        b->uops[b->nuops++] = u;
        b->ninstr++;
    }
    /* fell off the end of guest code: clean exit */
    b->uops[b->nuops].op = NJOP_EXIT;
    b->nuops++;
    return b;
}

/* superinstruction fusion pass (only for NJ_ENGINE_SUPER blocks).
 * Fuses:
 *   LOAD a,[b+imm] ; ADD a,a,c        -> S_LOAD_ADD
 *   ADD a,d,c      ; STORE a,[b+imm]  -> S_ADD_STORE
 * Sequential semantics preserved (value computed before address/store).
 */
static int fuse_block(nj_cpu_t *cpu, nj_block_t *b) {
    if (!cpu->fuse || b->nuops < 2) return 0;
    nj_uop_t out[NJ_MAX_UOPS];
    int n = 0, i = 0, fused = 0;
    while (i < b->nuops) {
        nj_uop_t *u = &b->uops[i];
        if (u->op == NJOP_LOAD && i + 1 < b->nuops) {
            nj_uop_t *v = &b->uops[i + 1];
            int add_uses_loaded = v->op == NJOP_ADD && v->a != 31 &&
                ((v->b == v->a && v->c == u->a) ||
                 (v->c == v->a && v->b == u->a));
            if (add_uses_loaded) {
                nj_uop_t s; memset(&s, 0, sizeof s);
                s.op = NJOP_S_LOAD_ADD;
                s.a = v->a;                              /* ADD destination */
                s.b = u->b;                              /* load base */
                s.c = (v->b == v->a) ? v->c : v->b;     /* other ADD source */
                s.d = u->a;                              /* loaded register */
                s.imm = u->imm;
                out[n++] = s; i += 2; fused++;
                continue;
            }
        }
        if (u->op == NJOP_ADD && u->a != 31 && i + 1 < b->nuops) {
            nj_uop_t *v = &b->uops[i + 1];
            if (v->op == NJOP_STORE && v->a == u->a) {
                nj_uop_t s; memset(&s, 0, sizeof s);
                s.op = NJOP_S_ADD_STORE;
                s.a = u->a;      /* dest / stored value */
                s.b = v->b;      /* store address reg */
                s.c = u->c;      /* add src2 */
                s.d = u->b;      /* add src1 */
                s.imm = v->imm;  /* store offset */
                out[n++] = s; i += 2; fused++;
                continue;
            }
        }
        out[n++] = *u; i++;
    }
    if (fused) {
        memcpy(b->uops, out, (size_t)n * sizeof(nj_uop_t));
        b->nuops = (uint16_t)n;
        cpu->super_fusions += (uint64_t)fused;
    }
    return fused;
}

/* ============ engines ============ */

static int run_switch(nj_cpu_t *cpu, nj_block_t *b) {
    uint64_t *R = cpu->regs;
    uint16_t ip;
    for (ip = 0; ip < b->nuops; ip++) {
        const nj_uop_t *u = &b->uops[ip];
        switch (u->op) {
        case NJOP_NOP: break;
        case NJOP_MOVI: if (u->a != 31) R[u->a] = (uint64_t)u->imm; break;
        case NJOP_ADD:  if (u->a != 31) R[u->a] = R[u->b] + R[u->c]; break;
        case NJOP_ADDI: if (u->a != 31) R[u->a] = R[u->b] + (uint64_t)u->imm; break;
        case NJOP_SUB:  if (u->a != 31) R[u->a] = R[u->b] - R[u->c]; break;
        case NJOP_SUBI: if (u->a != 31) R[u->a] = R[u->b] - (uint64_t)u->imm; break;
        case NJOP_MUL:  if (u->a != 31) R[u->a] = R[u->b] * R[u->c]; break;
        case NJOP_AND:  if (u->a != 31) R[u->a] = R[u->b] & R[u->c]; break;
        case NJOP_ORR:  if (u->a != 31) R[u->a] = R[u->b] | R[u->c]; break;
        case NJOP_EOR:  if (u->a != 31) R[u->a] = R[u->b] ^ R[u->c]; break;
        case NJOP_LSLV: if (u->a != 31) R[u->a] = R[u->b] << (R[u->c] & 63); break;
        case NJOP_LSRV: if (u->a != 31) R[u->a] = R[u->b] >> (R[u->c] & 63); break;
        case NJOP_ASRV: if (u->a != 31) R[u->a] = (uint64_t)((int64_t)R[u->b] >> (R[u->c] & 63)); break;
        case NJOP_ADDS_I: {
            uint64_t r = R[u->b] + (uint64_t)u->imm;
            if (u->a != 31) R[u->a] = r;
            set_add_flags(&cpu->nzcv, R[u->b], (uint64_t)u->imm, r);
            break;
        }
        case NJOP_ADDS_R: {
            uint64_t r = R[u->b] + R[u->c];
            if (u->a != 31) R[u->a] = r;
            set_add_flags(&cpu->nzcv, R[u->b], R[u->c], r);
            break;
        }
        case NJOP_SUBS_I: {
            uint64_t r = R[u->b] - (uint64_t)u->imm;
            if (u->a != 31) R[u->a] = r;
            set_sub_flags(&cpu->nzcv, R[u->b], (uint64_t)u->imm, r);
            break;
        }
        case NJOP_SUBS_R: {
            uint64_t r = R[u->b] - R[u->c];
            if (u->a != 31) R[u->a] = r;
            set_sub_flags(&cpu->nzcv, R[u->b], R[u->c], r);
            break;
        }
        case NJOP_LOAD: {
            uint64_t v;
            int mrc = nj_load64(cpu, R[u->b] + (uint64_t)u->imm, &v);
            if (mrc != NJ_TRAP_NONE) { cpu->pc = b->guest_pc; return mrc; }
            if (u->a != 31) R[u->a] = v;
            break;
        }
        case NJOP_STORE: {
            int mrc = nj_store64(cpu, R[u->b] + (uint64_t)u->imm, R[u->a]);
            if (mrc != NJ_TRAP_NONE) { cpu->pc = b->guest_pc; return mrc; }
            break;
        }
        case NJOP_S_LOAD_ADD: {
            uint64_t v;
            int mrc = nj_load64(cpu, R[u->b] + (uint64_t)u->imm, &v);
            if (mrc != NJ_TRAP_NONE) { cpu->pc = b->guest_pc; return mrc; }
            if (u->d != 31) R[u->d] = v;
            if (u->a != 31) R[u->a] = R[u->a] + R[u->c];
            break;
        }
        case NJOP_S_ADD_STORE: {
            uint64_t v = R[u->d] + R[u->c];
            if (u->a != 31) R[u->a] = v;
            int mrc = nj_store64(cpu, R[u->b] + (uint64_t)u->imm, v);
            if (mrc != NJ_TRAP_NONE) { cpu->pc = b->guest_pc; return mrc; }
            break;
        }
        case NJOP_BRIMM:
            cpu->pc = (uint64_t)u->imm;
            return NJ_TRAP_NONE;
        case NJOP_BRCOND: {
            int take;
            if (u->c == NJOP_CBZ)       take = (R[u->d] == 0);
            else if (u->c == NJOP_CBNZ) take = (R[u->d] != 0);
            else                        take = nj_cond(cpu->nzcv, u->c, 0);
            if (take) { cpu->pc = (uint64_t)u->imm; return NJ_TRAP_NONE; }
            cpu->pc = b->guest_pc + (uint64_t)b->ninstr * 4;
            return NJ_TRAP_NONE;
        }
        case NJOP_EXIT:
            cpu->halted = 1;
            return NJ_TRAP_NONE;
        case NJOP_TRAP_BADOP:
            cpu->pc = b->guest_pc;
            return NJ_TRAP_BADOP;
        default:
            cpu->pc = b->guest_pc;
            return NJ_TRAP_BADOP;
        }
    }
    return NJ_TRAP_NONE;
}

#if defined(__GNUC__) && !defined(NJ_NO_COMPUTED_GOTO)
#define NJ_HAVE_COMPUTED_GOTO 1
static int run_goto(nj_cpu_t *cpu, nj_block_t *b) {
    static const void *const tab[] = {
        [NJOP_NOP]         = &&L_nop,
        [NJOP_MOVI]        = &&L_movi,
        [NJOP_ADD]         = &&L_add,
        [NJOP_ADDI]        = &&L_addi,
        [NJOP_SUB]         = &&L_sub,
        [NJOP_SUBI]        = &&L_subi,
        [NJOP_MUL]         = &&L_mul,
        [NJOP_AND]         = &&L_and,
        [NJOP_ORR]         = &&L_orr,
        [NJOP_EOR]         = &&L_eor,
        [NJOP_LSLV]        = &&L_lslv,
        [NJOP_LSRV]        = &&L_lsrv,
        [NJOP_ASRV]        = &&L_asrv,
        [NJOP_ADDS_I]      = &&L_adds_i,
        [NJOP_ADDS_R]      = &&L_adds_r,
        [NJOP_SUBS_I]      = &&L_subs_i,
        [NJOP_SUBS_R]      = &&L_subs_r,
        [NJOP_LOAD]        = &&L_load,
        [NJOP_STORE]       = &&L_store,
        [NJOP_BRIMM]       = &&L_brimm,
        [NJOP_BRCOND]      = &&L_brcond,
        [NJOP_EXIT]        = &&L_exit,
        [NJOP_TRAP_BADOP]  = &&L_badop,
        [NJOP_S_LOAD_ADD]  = &&L_s_load_add,
        [NJOP_S_ADD_STORE] = &&L_s_add_store,
    };
    uint64_t *R = cpu->regs;
    uint16_t ip = 0;
    const nj_uop_t *u = &b->uops[0];
    if (u->op > NJ_MAX_TAB || !tab[u->op]) { cpu->pc = b->guest_pc; return NJ_TRAP_BADOP; }
    goto *tab[u->op];

#define NJ_NEXT() do { if (++ip >= b->nuops) return NJ_TRAP_NONE; \
                       u = &b->uops[ip]; \
                       if (u->op > NJ_MAX_TAB || !tab[u->op]) { cpu->pc = b->guest_pc; return NJ_TRAP_BADOP; } \
                       goto *tab[u->op]; } while (0)

L_nop:   NJ_NEXT();
L_movi:  if (u->a != 31) R[u->a] = (uint64_t)u->imm; NJ_NEXT();
L_add:   if (u->a != 31) R[u->a] = R[u->b] + R[u->c]; NJ_NEXT();
L_addi:  if (u->a != 31) R[u->a] = R[u->b] + (uint64_t)u->imm; NJ_NEXT();
L_sub:   if (u->a != 31) R[u->a] = R[u->b] - R[u->c]; NJ_NEXT();
L_subi:  if (u->a != 31) R[u->a] = R[u->b] - (uint64_t)u->imm; NJ_NEXT();
L_mul:   if (u->a != 31) R[u->a] = R[u->b] * R[u->c]; NJ_NEXT();
L_and:   if (u->a != 31) R[u->a] = R[u->b] & R[u->c]; NJ_NEXT();
L_orr:   if (u->a != 31) R[u->a] = R[u->b] | R[u->c]; NJ_NEXT();
L_eor:   if (u->a != 31) R[u->a] = R[u->b] ^ R[u->c]; NJ_NEXT();
L_lslv:  if (u->a != 31) R[u->a] = R[u->b] << (R[u->c] & 63); NJ_NEXT();
L_lsrv:  if (u->a != 31) R[u->a] = R[u->b] >> (R[u->c] & 63); NJ_NEXT();
L_asrv:  if (u->a != 31) R[u->a] = (uint64_t)((int64_t)R[u->b] >> (R[u->c] & 63)); NJ_NEXT();
L_adds_i: {
    uint64_t r = R[u->b] + (uint64_t)u->imm;
    if (u->a != 31) R[u->a] = r;
    set_add_flags(&cpu->nzcv, R[u->b], (uint64_t)u->imm, r);
    NJ_NEXT();
}
L_adds_r: {
    uint64_t r = R[u->b] + R[u->c];
    if (u->a != 31) R[u->a] = r;
    set_add_flags(&cpu->nzcv, R[u->b], R[u->c], r);
    NJ_NEXT();
}
L_subs_i: {
    uint64_t r = R[u->b] - (uint64_t)u->imm;
    if (u->a != 31) R[u->a] = r;
    set_sub_flags(&cpu->nzcv, R[u->b], (uint64_t)u->imm, r);
    NJ_NEXT();
}
L_subs_r: {
    uint64_t r = R[u->b] - R[u->c];
    if (u->a != 31) R[u->a] = r;
    set_sub_flags(&cpu->nzcv, R[u->b], R[u->c], r);
    NJ_NEXT();
}
L_load: {
    uint64_t v;
    int mrc = nj_load64(cpu, R[u->b] + (uint64_t)u->imm, &v);
    if (mrc != NJ_TRAP_NONE) { cpu->pc = b->guest_pc; return mrc; }
    if (u->a != 31) R[u->a] = v;
    NJ_NEXT();
}
L_store: {
    int mrc = nj_store64(cpu, R[u->b] + (uint64_t)u->imm, R[u->a]);
    if (mrc != NJ_TRAP_NONE) { cpu->pc = b->guest_pc; return mrc; }
    NJ_NEXT();
}
L_s_load_add: {
    uint64_t v;
    int mrc = nj_load64(cpu, R[u->b] + (uint64_t)u->imm, &v);
    if (mrc != NJ_TRAP_NONE) { cpu->pc = b->guest_pc; return mrc; }
    if (u->d != 31) R[u->d] = v;
    if (u->a != 31) R[u->a] = R[u->a] + R[u->c];
    NJ_NEXT();
}
L_s_add_store: {
    uint64_t v = R[u->d] + R[u->c];
    if (u->a != 31) R[u->a] = v;
    int mrc = nj_store64(cpu, R[u->b] + (uint64_t)u->imm, v);
    if (mrc != NJ_TRAP_NONE) { cpu->pc = b->guest_pc; return mrc; }
    NJ_NEXT();
}
L_brimm:
    cpu->pc = (uint64_t)u->imm;
    return NJ_TRAP_NONE;
L_brcond: {
    int take;
    if (u->c == NJOP_CBZ)       take = (R[u->d] == 0);
    else if (u->c == NJOP_CBNZ) take = (R[u->d] != 0);
    else                        take = nj_cond(cpu->nzcv, u->c, 0);
    if (take) { cpu->pc = (uint64_t)u->imm; return NJ_TRAP_NONE; }
    cpu->pc = b->guest_pc + (uint64_t)b->ninstr * 4;
    return NJ_TRAP_NONE;
}
L_exit:
    cpu->halted = 1;
    return NJ_TRAP_NONE;
L_badop:
    cpu->pc = b->guest_pc;
    return NJ_TRAP_BADOP;

#undef NJ_NEXT
}
#else
#define run_goto run_switch
#endif

/* ============ lifecycle ============ */

nj_cpu_t *nj_create(const uint8_t *code, uint64_t code_len, vw_guest_mem_t *gm) {
    nj_cpu_t *cpu = calloc(1, sizeof *cpu);
    if (!cpu) return NULL;
    cpu->code = code;
    cpu->code_len = code_len;
    cpu->gm = gm;
    cpu->engine = NJ_ENGINE_SWITCH;
    cpu->fuse = 0;
    cpu->regs[31] = 0;
    return cpu;
}

void nj_destroy(nj_cpu_t *cpu) {
    if (!cpu) return;
    for (size_t i = 0; i < NJ_MAX_BLOCKS; i++)
        if (cpu->blocks[i]) free(cpu->blocks[i]);
    free(cpu);
}

void nj_reset(nj_cpu_t *cpu) {
    if (!cpu) return;
    memset(cpu->regs, 0, sizeof cpu->regs);
    cpu->regs[31] = 0;
    cpu->pc = 0;
    cpu->exit_code = 0;
    cpu->nzcv = 0;
    cpu->steps = 0;
    cpu->halted = 0;
    cpu->block_compiles = 0;
    cpu->super_fusions = 0;
    cpu->tlb_hits = 0;
    cpu->tlb_misses = 0;
    cpu->guest_instr = 0;
    cpu->guest_op_count = 0;
}

int nj_run(nj_cpu_t *cpu, uint64_t entry_pc) {
    if (!cpu) return NJ_TRAP_MALFORM;
    cpu->halted = 0;
    cpu->pc = entry_pc;
    const uint64_t RUNAWAY = 500000000000ull;  /* guest-instruction ceiling */

    for (;;) {
        nj_block_t *b = get_block(cpu, cpu->pc);
        if (!b) return NJ_TRAP_MALFORM;
        if (cpu->fuse) { fuse_block(cpu, b); b->fused = 1; }
        int rc = (cpu->engine == NJ_ENGINE_SWITCH) ? run_switch(cpu, b)
                                                   : run_goto(cpu, b);
        cpu->steps += b->ninstr;
        cpu->guest_instr += b->nuops;
        cpu->guest_op_count += b->ninstr;
        if (rc != NJ_TRAP_NONE) return rc;
        if (cpu->halted) { cpu->exit_code = cpu->regs[0]; return NJ_TRAP_NONE; }
        if (cpu->steps > RUNAWAY) return NJ_TRAP_BADOP;  /* runaway guard */
    }
}
