/*
 * guest.c — minimal AArch64 "container-type guest" fragment.
 *
 * This is NOT a full program: it is a freestanding code sample compiled to an
 * ELF object (.o) so the offline probe (aarch64_rewrite_probe) has real AArch64
 * instructions to decode. It deliberately contains exactly the instruction
 * classes a near-native engine must intercept:
 *   * mrs  tpidr_el0   (EL0-visible thread-local base  -> TLS shim)
 *   * mrs  cntfrq_el0  (EL0-visible timer frequency    -> clock shim)
 *   * a branchy loop   (direct B / B.cond / CBZ targets)
 *   * svc  #0          (Linux syscall -> Linux->OHOS shim)
 *
 * It is never executed by the probe or by this project (no guest/VM is ever
 * started). It only serves as decode input.
 */

void _start(void)
{
    unsigned long tls = 0;
    unsigned long freq = 0;

    /* EL0 system-register reads — must be shimmed, not executed verbatim. */
    __asm__ volatile ("mrs %0, tpidr_el0"  : "=r"(tls));
    __asm__ volatile ("mrs %0, cntfrq_el0" : "=r"(freq));

    long acc = (long)(tls + freq);
    long i;
    /* Branchy loop: yields B (loop back), B.cond/CBZ (the if), and RET. */
    for (i = 0; i < 7; i++) {
        if (i & 1)
            acc += i;
        else
            acc -= i;
    }

    /* Linux syscall (aarch64 __NR_exit = 93) via SVC #0. */
    register long x0 __asm__("x0") = acc & 0xff;
    register long x8 __asm__("x8") = 93;
    __asm__ volatile ("svc #0" : "+r"(x0) : "r"(x8) : "memory");
}
