/*
 * aarch64_rewrite_probe.c — Offline AArch64 ELF + instruction-stream probe.
 *
 * Context (docs/harmony-native-exec.md, design task 7):
 *   A same-ISA "near-native" execution engine for HarmonyOS/OpenHarmony ARM64
 *   must, *before* running a guest, statically identify which instructions
 *   cannot be executed verbatim on the host and must instead be rewritten or
 *   redirected to a controlled trampoline:
 *       * SVC        -> Linux syscall; must be shimmed (Linux -> OHOS).
 *       * HVC / SMC  -> privileged exception generation; never pass through.
 *       * MRS / MSR  -> system-register access (TPIDR_EL0, CNT*_EL0, ...).
 *       * system inst (DC/IC/AT/TLBI, MSR-imm PSTATE) -> privileged.
 *       * branches    -> direct targets (trampoline/cache-boundary sites) and
 *                        indirect (register) targets.
 *
 * This probe PARSES and DECODES only. It never executes, translates, or
 * allocates executable memory for the guest. Memory use is bounded by the
 * mapped file size (we mmap the ELF read-only, MAP_PRIVATE). A getrusage()
 * report is printed so the "low-memory" requirement is demonstrable.
 *
 * Build (host, Apple Silicon aarch64 — used to produce results):
 *     cc -O2 aarch64_rewrite_probe.c -o aarch64_rewrite_probe
 * Cross-build (aarch64-linux-ohos, link-level proof — not run on host):
 *     . ci/ohos/env-ohos.sh && $CC aarch64_rewrite_probe.c \
 *         -o aarch64_rewrite_probe.ohos
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/mman.h>
#include <sys/resource.h>

/* ---- minimal ELF64 ------------------------------------------------ */
typedef uint64_t Elf64_Addr, Elf64_Off, Elf64_Xword;
typedef uint32_t Elf64_Word;
typedef uint16_t Elf64_Half;

#define EM_AARCH64 183
#define PT_LOAD 1
#define PF_X 1
#define SHT_PROGBITS 1
#define SHT_SYMTAB 2
#define SHF_EXECINSTR 0x4

typedef struct {
    unsigned char e_ident[16];
    Elf64_Half e_type;
    Elf64_Half e_machine;
    Elf64_Word e_version;
    Elf64_Addr e_entry;
    Elf64_Off  e_phoff;
    Elf64_Off  e_shoff;
    Elf64_Word e_flags;
    Elf64_Half e_ehsize;
    Elf64_Half e_phentsize;
    Elf64_Half e_phnum;
    Elf64_Half e_shentsize;
    Elf64_Half e_shnum;
    Elf64_Half e_shstrndx;
} Elf64_Ehdr;

typedef struct {
    Elf64_Word p_type, p_flags;
    Elf64_Off  p_offset;
    Elf64_Addr p_vaddr;
    Elf64_Xword p_filesz, p_memsz;
} Elf64_Phdr;

typedef struct {
    Elf64_Word sh_name, sh_type;
    Elf64_Xword sh_flags, sh_addr, sh_offset, sh_size;
    Elf64_Word sh_link, sh_info;
    Elf64_Xword sh_addralign, sh_entsize;
} Elf64_Shdr;

/* ---- counters ----------------------------------------------------- */
static unsigned long cnt_total = 0;
static unsigned long cnt_svc = 0, cnt_hvc = 0, cnt_smc = 0;
static unsigned long cnt_mrs = 0, cnt_msr = 0, cnt_sysinst = 0;
static unsigned long cnt_b = 0, cnt_bl = 0, cnt_bcond = 0;
static unsigned long cnt_cb = 0, cnt_tb = 0, cnt_indirect = 0;
static unsigned long cnt_other = 0;

#define DETAIL_CAP 64
static unsigned long detail_svc[DETAIL_CAP];
static unsigned long detail_sysreg[DETAIL_CAP];
static char sysreg_names[DETAIL_CAP][24];
static unsigned long detail_branch[DETAIL_CAP];
static unsigned long detail_branch_target[DETAIL_CAP];
static int n_detail_svc = 0, n_detail_sysreg = 0, n_detail_branch = 0;

static const char *sysreg_name(unsigned op0, unsigned op1,
                               unsigned crn, unsigned crm, unsigned op2)
{
    /* Tiny lookup of common EL0-visible and a few privileged registers. */
    if (op0 == 3 && op1 == 3 && crn == 13 && crm == 0 && op2 == 2) return "TPIDR_EL0";
    if (op0 == 3 && op1 == 3 && crn == 13 && crm == 0 && op2 == 3) return "TPIDRRO_EL0";
    if (op0 == 3 && op1 == 3 && crn == 13 && crm == 0 && op2 == 1) return "CTPIDR_EL0";
    if (op0 == 3 && op1 == 3 && crn == 14 && crm == 0 && op2 == 0) return "CNTFRQ_EL0";
    if (op0 == 3 && op1 == 3 && crn == 14 && crm == 2 && op2 == 0) return "CNTPCT_EL0";
    if (op0 == 3 && op1 == 3 && crn == 14 && crm == 3 && op2 == 0) return "CNTVCT_EL0";
    if (op0 == 3 && op1 == 0 && crn == 14 && crm == 0 && op2 == 1) return "CNTKCTL_EL1";
    if (op0 == 3 && op1 == 1 && crn == 2  && crm == 0 && op2 == 0) return "TTBR0_EL1";
    if (op0 == 3 && op1 == 1 && crn == 2  && crm == 0 && op2 == 1) return "TTBR1_EL1";
    if (op0 == 3 && op1 == 1 && crn == 10 && crm == 2 && op2 == 0) return "MAIR_EL1";
    if (op0 == 3 && op1 == 1 && crn == 12 && crm == 0 && op2 == 0) return "SCTLR_EL1";
    if (op0 == 3 && op1 == 1 && crn == 4  && crm == 0 && op2 == 0) return "SPR_EL1";
    return "(unknown)";
}

static long sext(long v, int bits)
{
    long shift = 64 - bits;
    return (v << shift) >> shift;
}

/* Decode one 32-bit A64 instruction at file offset `off`, virtual address
 * `addr` (addr = offset of this instruction within the chosen PC base). */
static void decode(uint32_t insn, uint64_t off, uint64_t addr)
{
    cnt_total++;

    /* --- Exception generation: SVC / HVC / SMC --- */
    uint32_t top = insn & 0xFFE00000u;
    if (top == 0xD4000000u) {            /* SVC   (bits[23:21]=000) */
        cnt_svc++;
        if (n_detail_svc < DETAIL_CAP) detail_svc[n_detail_svc++] = off;
        return;
    }
    if (top == 0xD4100000u) {            /* HVC   (bits[23:21]=001) */
        cnt_hvc++; cnt_other++;
        return;
    }
    if (top == 0xD4200000u) {            /* SMC   (bits[23:21]=010) */
        cnt_smc++; cnt_other++;
        return;
    }

    /* --- System register move: MRS (0xD5300000) / MSR reg (0xD5100000) --- */
    if ((insn & 0xFFF00000u) == 0xD5300000u ||
        (insn & 0xFFF00000u) == 0xD5100000u) {
        unsigned op0 = (insn >> 19) & 0x3;
        unsigned op1 = (insn >> 16) & 0x7;
        unsigned crn = (insn >> 12) & 0xf;
        unsigned crm = (insn >> 8)  & 0xf;
        unsigned op2 = (insn >> 5)  & 0x7;
        unsigned is_read = (insn >> 21) & 0x1;
        if (is_read) cnt_mrs++; else cnt_msr++;
        if (n_detail_sysreg < DETAIL_CAP) {
            detail_sysreg[n_detail_sysreg] = off;
            snprintf(sysreg_names[n_detail_sysreg],
                     sizeof(sysreg_names[0]), "%s",
                     sysreg_name(op0, op1, crn, crm, op2));
            n_detail_sysreg++;
        }
        return;
    }

    /* --- Other system instructions (DC/IC/AT/TLBI, MSR-imm PSTATE) --- */
    if ((insn & 0xFFF00000u) == 0xD5000000u) {
        cnt_sysinst++;
        return;
    }

    /* --- Branches --- */
    uint32_t btop = insn & 0xFC000000u;
    long tgt = -1;
    if (btop == 0x14000000u) {           /* B  (imm26) */
        cnt_b++;
        tgt = (long)(addr + 8) + sext((long)(insn & 0x03FFFFFFu) << 2, 28);
    } else if (btop == 0x94000000u) {    /* BL (imm26) */
        cnt_bl++;
        tgt = (long)(addr + 8) + sext((long)(insn & 0x03FFFFFFu) << 2, 28);
    } else if (btop == 0x54000000u) {    /* B.cond (imm19) */
        cnt_bcond++;
        tgt = (long)(addr + 8) + sext((long)((insn >> 5) & 0x7FFFFu) << 2, 21);
    } else if ((insn & 0xFF000000u) == 0x34000000u) {  /* CBZ (top byte 0x34) */
        cnt_cb++;
        tgt = (long)(addr + 8) + sext((long)((insn >> 5) & 0x7FFFFu) << 2, 21);
    } else if ((insn & 0xFF000000u) == 0x35000000u) {  /* CBNZ (top byte 0x35) */
        cnt_cb++;
        tgt = (long)(addr + 8) + sext((long)((insn >> 5) & 0x7FFFFu) << 2, 21);
    } else if ((insn & 0xFF000000u) == 0x36000000u) {  /* TBZ (top byte 0x36) */
        cnt_tb++;
        tgt = (long)(addr + 8) + sext((long)((insn >> 5) & 0x3FFFu) << 2, 16);
    } else if ((insn & 0xFF000000u) == 0x37000000u) {  /* TBNZ (top byte 0x37) */
        cnt_tb++;
        tgt = (long)(addr + 8) + sext((long)((insn >> 5) & 0x3FFFu) << 2, 16);
    }
    if (tgt != -1) {
        if (n_detail_branch < DETAIL_CAP) {
            detail_branch[n_detail_branch] = off;
            detail_branch_target[n_detail_branch] = (unsigned long)tgt;
            n_detail_branch++;
        }
        return;
    }
    if ((insn & 0xFFFFFC00u) == 0xD61F0000u ||   /* BR */
        (insn & 0xFFFFFC00u) == 0xD63F0000u ||   /* BLR */
        (insn & 0xFFFFFC00u) == 0xD65F0000u) {   /* RET */
        cnt_indirect++;
        return;
    }

    cnt_other++;
}

static void decode_range(const unsigned char *base, uint64_t off, uint64_t addr,
                         uint64_t size)
{
    uint64_t i;
    for (i = 0; i + 4 <= size; i += 4) {
        uint32_t insn = (uint32_t)base[off + i]
                      | ((uint32_t)base[off + i + 1] << 8)
                      | ((uint32_t)base[off + i + 2] << 16)
                      | ((uint32_t)base[off + i + 3] << 24);
        decode(insn, off + i, addr + i);
    }
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: %s <aarch64-elf>\n", argv[0]);
        return 2;
    }
    int fd = open(argv[1], O_RDONLY);
    if (fd < 0) { perror("open"); return 1; }
    struct stat st;
    if (fstat(fd, &st) != 0) { perror("fstat"); close(fd); return 1; }
    if (st.st_size < (off_t)sizeof(Elf64_Ehdr)) {
        fprintf(stderr, "file too small\n"); close(fd); return 1;
    }
    const unsigned char *map = mmap(NULL, (size_t)st.st_size, PROT_READ,
                                   MAP_PRIVATE, fd, 0);
    if (map == MAP_FAILED) { perror("mmap"); close(fd); return 1; }

    const Elf64_Ehdr *eh = (const Elf64_Ehdr *)map;
    if (!(eh->e_ident[0] == 0x7f && eh->e_ident[1] == 'E' &&
          eh->e_ident[2] == 'L' && eh->e_ident[3] == 'F')) {
        fprintf(stderr, "not an ELF\n"); munmap((void*)map, st.st_size); close(fd); return 1;
    }
    int is64 = eh->e_ident[4] == 2;
    printf("# aarch64_rewrite_probe\n");
    printf("file            : %s\n", argv[1]);
    printf("EI_CLASS        : %d (1=32,2=64)\n", eh->e_ident[4]);
    printf("e_machine       : %u (183=AArch64)  is_aarch64=%s\n",
           eh->e_machine, eh->e_machine == EM_AARCH64 ? "YES" : "NO");
    printf("e_type          : %u (1=REL,2=EXEC,3=DYN)\n", eh->e_type);

    if (!is64 || eh->e_machine != EM_AARCH64) {
        printf("SKIP: not a 64-bit AArch64 ELF.\n");
        munmap((void*)map, st.st_size); close(fd); return 0;
    }

    /* Collect executable ranges: prefer sections (.text / SHF_EXECINSTR),
     * fall back to PT_LOAD PF_X program headers. */
    unsigned ranges = 0;
    struct { uint64_t off, addr, size; } r[256];

    if (eh->e_shoff && eh->e_shnum) {
        const Elf64_Shdr *sh = (const Elf64_Shdr *)(map + eh->e_shoff);
        for (Elf64_Half s = 0; s < eh->e_shnum && ranges < 256; s++) {
            if ((sh[s].sh_type == SHT_PROGBITS) &&
                (sh[s].sh_flags & SHF_EXECINSTR) &&
                sh[s].sh_size) {
                r[ranges].off  = sh[s].sh_offset;
                r[ranges].addr = sh[s].sh_addr ? sh[s].sh_addr : sh[s].sh_offset;
                r[ranges].size = sh[s].sh_size;
                ranges++;
            }
        }
    }
    if (ranges == 0 && eh->e_phoff && eh->e_phnum) {
        const Elf64_Phdr *ph = (const Elf64_Phdr *)(map + eh->e_phoff);
        for (Elf64_Half p = 0; p < eh->e_phnum && ranges < 256; p++) {
            if (ph[p].p_type == PT_LOAD && (ph[p].p_flags & PF_X) &&
                ph[p].p_filesz) {
                r[ranges].off  = ph[p].p_offset;
                r[ranges].addr = ph[p].p_vaddr;
                r[ranges].size = ph[p].p_filesz;
                ranges++;
            }
        }
    }

    printf("code_ranges     : %u\n", ranges);
    for (unsigned k = 0; k < ranges; k++) {
        printf("  range[%u] off=0x%llx vaddr=0x%llx size=0x%llx\n",
               k, (unsigned long long)r[k].off,
               (unsigned long long)r[k].addr,
               (unsigned long long)r[k].size);
        decode_range(map, r[k].off, r[k].addr, r[k].size);
    }

    /* ---- summary ---- */
    printf("\n# instruction summary (total decoded: %lu)\n", cnt_total);
    printf("SVC (syscall)   : %lu\n", cnt_svc);
    printf("HVC / SMC       : %lu / %lu  (privileged; must never pass through)\n", cnt_hvc, cnt_smc);
    printf("MRS (sysreg rd) : %lu\n", cnt_mrs);
    printf("MSR (sysreg wr) : %lu\n", cnt_msr);
    printf("sys-inst(DC/IC..): %lu  (privileged)\n", cnt_sysinst);
    printf("B / BL          : %lu / %lu\n", cnt_b, cnt_bl);
    printf("B.cond          : %lu\n", cnt_bcond);
    printf("CBZ/CBNZ        : %lu\n", cnt_cb);
    printf("TBZ/TBNZ        : %lu\n", cnt_tb);
    printf("indirect BR/BLR/RET: %lu\n", cnt_indirect);
    printf("other/arith     : %lu\n", cnt_other);

    printf("\n# sensitive-instruction detail (first %d each)\n", DETAIL_CAP);
    printf("SVC offsets     :");
    for (int i = 0; i < n_detail_svc; i++) printf(" 0x%lx", detail_svc[i]);
    printf("\nMRS/MSR (off: reg):");
    for (int i = 0; i < n_detail_sysreg; i++)
        printf(" 0x%lx:%s", detail_sysreg[i], sysreg_names[i]);
    printf("\ndirect-branch (off -> target):");
    for (int i = 0; i < n_detail_branch; i++)
        printf(" 0x%lx->0x%lx", detail_branch[i], detail_branch_target[i]);
    printf("\n");

    /* ---- low-memory evidence ---- */
    struct rusage ru;
    if (getrusage(RUSAGE_SELF, &ru) == 0) {
        long kb = ru.ru_maxrss;            /* macOS: bytes; Linux: kB */
#ifdef __APPLE__
        kb = kb / 1024;                    /* -> KiB */
#endif
        printf("\n# resource: max RSS observed = %ld KiB (cap 1536 MiB = 1572864 KiB)\n", kb);
    }

    munmap((void*)map, st.st_size);
    close(fd);
    return 0;
}
