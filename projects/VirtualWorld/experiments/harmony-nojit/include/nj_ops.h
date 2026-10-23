/* nj_ops.h — micro-op opcode set for the no-JIT interpreter.
 * Self-contained IR (QEMU-TCI *style*, but NOT QEMU TCI and NOT part of any
 * upstream project). No opcode corresponds to machine code.
 */
#ifndef VW_NJ_OPS_H
#define VW_NJ_OPS_H

#define NJOP_NOP          0
#define NJOP_MOVI         1   /* a = imm                                   */
#define NJOP_ADD          2   /* a = b + c                                 */
#define NJOP_ADDI         3   /* a = b + imm                               */
#define NJOP_SUB          4   /* a = b - c                                 */
#define NJOP_SUBI         5   /* a = b - imm                               */
#define NJOP_MUL          6   /* a = b * c                                 */
#define NJOP_AND          7
#define NJOP_ORR          8
#define NJOP_EOR          9
#define NJOP_LSLV        10   /* a = b <<  (c & 63)                        */
#define NJOP_LSRV        11   /* a = b >>  (c & 63)                        */
#define NJOP_ASRV        12   /* a = b >>  (c & 63), arithmetic            */
#define NJOP_ADDS_I      13   /* a = b + imm, set NZCV                     */
#define NJOP_ADDS_R      14   /* a = b + c,   set NZCV                     */
#define NJOP_SUBS_I      15   /* a = b - imm, set NZCV                     */
#define NJOP_SUBS_R      16   /* a = b - c,   set NZCV                     */
#define NJOP_LOAD        17   /* a = mem64(regs[b] + imm)                  */
#define NJOP_STORE       18   /* mem64(regs[b] + imm) = regs[a]            */
#define NJOP_BRIMM       19   /* pc = imm                    (terminator)  */
#define NJOP_BRCOND      20   /* if cond -> pc = imm         (terminator)  */
#define NJOP_EXIT        21   /* halt; exit_code = regs[0]  (terminator)   */
#define NJOP_TRAP_BADOP  22   /* decode failure            (terminator)    */
#define NJOP_MAX_BASE    23   /* base opcodes end here                    */

/* superinstruction zone */
#define NJOP_S_LOAD_ADD  32   /* a = a + mem64(regs[b] + imm)             */
#define NJOP_S_ADD_STORE 33   /* mem64(regs[b]+imm) = regs[d] + regs[c];  *
                              * regs[a] = regs[d] + regs[c]               */

/* highest opcode that has a computed-goto table slot. Ops above this (or a
 * NULL slot) are treated as decode failures by the engines. */
#define NJ_MAX_TAB       NJOP_S_ADD_STORE

/* conditional-branch special codes (stored in uop.c) */
#define NJOP_CBZ         16
#define NJOP_CBNZ        17

#endif /* VW_NJ_OPS_H */
