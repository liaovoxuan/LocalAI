# HarmonyOS / OpenHarmony ARM64 —— 全系统 TCG 高速兜底层

- 项目：`projects/VirtualWorld`
- 审计对象：`qemu-source/`（QEMU **11.1.50**，HEAD `d49f8760`）
- 产物：本报告 + `.deepseek/patches/harmony-tcg-fast-profile.patch`（120 行，配置脚本，已实测可应用）
- 只读约束遵守：`qemu-source/**`、`src/**`、`tests/**`、CMake、`build-qemu-inprocess/**`、`experiments/ohos-native-exec/**`、`docs/harmony-native-exec.md`、任何 ready/blocked 标记**均未修改**。未启动 VM/UTM/guest，未跑 QEMU 全量编译，未启动本地模型，未 commit/push。

> **本报告是设计稿，不是已实现、也不是已优化的声明。**
> 全文**没有任何实测性能数字**。凡未实测处均标注"未实测"。

---

## 0. 先说清楚两件事（避免把前提当既成事实）

### 0.1 当前**不存在** OHOS 的 QEMU configure 产物

任务书提到"现有 OHOS configure 产物"。实测**没有**：

```bash
$ find . -maxdepth 2 -name "*.cross" -o -maxdepth 2 -name "*ohos*"
./build/config-meson.cross            # 这是 macOS 宿主的 cross file，不是 OHOS
$ ls qemu-source/build/config-host.h && grep -E "CONFIG_DARWIN|CONFIG_LINUX" qemu-source/build/config-host.h
#define CONFIG_DARWIN
#undef  CONFIG_LINUX
```

现有的 `build-harmony-inprocess-integration/` 是 **HarmonyOS 侧的 CMake 构建**（一个虚拟化测试二进制），**不是 QEMU configure 产物**。所以本报告的一切 OHOS 相关结论都基于：QEMU 源码 + OHOS SDK 的只读检查 + 已知的交叉编译约束，**没有一份"已经在 OHOS 上 configure 过"的证据**。

### 0.2 宿主侧构建证据**已在审计期间失效**（重要更正）

> **本报告初稿引用的 `qemu-source/build/config-host.h` 内容，在审计期间被并发进程重建而失效。**
> 实测：该目录内容被清空，出现 `# QEMU configure log ...` 与 `# Configured with: '../configure' '--help'`。
> 因此下面这份 macOS 配置快照**当前不可取证**，仅作为"审计早期观察"保留，**不应作为结论依据**。
> 宿主事实改由 `sysctl` + 独立编译探针取得（见 §1.2）。

审计早期观察到的 `qemu-source/build/config-host.h`（**已失效，勿引用**）：

```
#define CONFIG_TCG 1
#undef  CONFIG_TCG_INTERPRETER          # 有 JIT，不是解释器
#define CONFIG_ATOMIC128                # 宿主支持 128 位原子
#define CONFIG_PIXMAN
#define CONFIG_SLIRP
#undef  CONFIG_LINUX / CONFIG_LINUX_AIO / CONFIG_LINUX_IO_URING
```

**它可以用来说明"哪些能力在 aarch64 宿主上已经具备"，但不能代表 OHOS。** OHOS 的差异见 §3.4。

---

## 1. TCG AArch64 后端**已经具备**的能力（防止把已有能力包装成新实现）

| 能力 | 判定 | 证据与说明 |
| --- | --- | --- |
| **MTTCG（多线程 TCG）** | **已有：运行时判定 + 每客体编译期常量** | **不是全局编译期宏**。状态在 `TCGState.mttcg_enabled`（`OnOffAuto`，`accel/tcg/tcg-all.c:53`）；判定在 `tcg_init_machine()`（`tcg-all.c:113-139`）：**AUTO → ON 当且仅当 `mttcg_supported && !icount_enabled()`**，且 `max_threads = ms->smp.max_cpus`。开关是 `-accel tcg,thread=multi\|single`（`tcg_set_thread()` :192-207），**multi + icount 硬报错**（:197-198）。每客体门控位在 `include/accel/tcg/cpu-ops.h:34`，ARM 为 true（`target/arm/cpu.c:2587`）。<br>**更正**：初稿写的 `tcg_cpus_mttcg_enabled()` **在 11.1.50 不存在**（grep 空）；公开名是 **`qemu_tcg_mttcg_enabled()`**（`tcg-all.c:66-70`、`include/system/tcg.h:26`）。 |
| **direct block chaining** | **已有，默认开启** | TB 之间的直接跳转链接是 TCG 的默认行为（`tb_add_jump` 一族），不需要 `CONFIG_*` 开关。**不要把它列为新实现项。** |
| **TB cache** | **已有，可配置** | **更正**：`tcg_ctx->tb_rmap` 在本版本**已不存在**；实际结构是 `tb_ctx.htable`(QHT) + softmmu PageDesc 链 + 每 region 代码树。<br>**更正（关键）**：`-tb-size` **在 QEMU 6.0 已被移除**（`docs/about/removed-features.rst:239-243`），现在必须用 **`-accel tcg,tb-size=N`（单位 MiB）**。<br>默认 = `min(host_physmem/8, 1 GiB)`，对齐宿主页并 clamp 到 `[1 MiB, MAX]`（`tcg/region.c:744-758`）→ 16 GiB 宿主上是 **1 GiB**。aarch64 的 `MAX_CODE_GEN_BUFFER_SIZE = (size_t)-1`（`tcg/aarch64/tcg-target.h:17`）。region 数 = `min(tb_size/2MiB, 8×max_threads)`（`region.c:425-452`）。 |
| **split-W^X** | **已有三条路，但默认关闭** | **更正（关键）**：`splitwx_enabled` 默认 = **0（关闭）**；只有 `CONFIG_DEBUG_TCG && !CONFIG_USER_ONLY` 才是 `-1`(auto)（`tcg-all.c:73-83`）。<br>**默认路径是 anon `PROT_NONE` 再 `mprotect` 到 RWX**（`region.c:803-833`；macOS 额外加 `MAP_JIT`:702-707）。<br>三条实现：memfd（`region.c:560-598`，需 `!CONFIG_TCG_INTERPRETER && CONFIG_POSIX`）、Darwin `mach_vm_remap`（`:600-657`）、TCI。<br>强制 `split-wx=on` 而无 memfd → `"jit split-wx not supported"` → **`error_fatal` 启动失败**；平台禁 `mprotect(PROT_EXEC)` → `error_report` 后 **`exit(1)`**（`:824-833`）。 |
| **host atomics** | **已有（仅 128 位有探测）** | **更正**：`CONFIG_ATOMIC64` 在 11.1.50 **全树不存在**（已删除）——只有 128 位有探测链：`meson.build:2973-3012`（`CONFIG_ATOMIC128` → `_OPT` → `CONFIG_CMPXCHG128`），且 **libatomic 被显式排除**（`meson.build:418-433`）。`include/qemu/atomic.h:23-25` 要求 C11 内建；`ATOMIC_REG_SIZE=8`(:65)；仅 x86_64/s390x 有无锁特化(:206-227)。<br>aarch64 宿主：`host/include/aarch64/host/atomic128-ldst.h.inc:25-26` 的 `HAVE_ATOMIC128_RO=(cpuinfo & CPUINFO_LSE2)`。 |
| **TCG vector ops** | **已有 NEON/AdvSIMD 后端，但无 v256、无 SVE** | **更正**：**不存在 `tcg_out_vec_*` 家族** —— 真名是 `tcg_out_vec_op`（`tcg/aarch64/tcg-target.c.inc:2916`）、`tcg_out_dupi_vec`(:961)、`tcg_out_dup_vec`(:1066)、`tcg_out_dupm_vec`(:1074)。<br>能力位：`tcg-target-has.h:31-54` **v64=1、v128=1、v256=0**；andc/orc/not/neg/abs/shi/shv/mul/sat/minmax/bitsel/tst=1；nand/nor/eqv/roti/rots/rotv/shs/cmpsel=0。`tcg-target.c.inc` 有约 100 个 `INDEX_op_*vec*` case。<br>**后端内无任何 SVE**（grep 空）。>128-bit gvec 在无 v256 宿主被**拆成 128 位**（`tcg/tcg-op-gvec.c:487-508`）。<br>**附带发现**：`have_lse`（`tcg/aarch64/tcg-target-has.h:12`）**定义了但全树未被使用**（只用 `have_lse2`）—— **不要把「LSE 加速」当作已有实现**。 |

**结论**：§1 的这些能力**没有一项需要在 OHOS 上"重新实现"**。它们要么已默认启用（chaining、atomics、vector），要么是运行时选择（MTTCG），要么是配置开关（tcib size、JIT vs TCI）。**把它们写成"待实现的优化"就是虚报工作量。**

### 1.1 MTTCG 在什么条件下会被静默禁用（必须实测的点）

MTTCG 不是"有就一定用"。可能的抑制因素（源码层面存在，**OHOS 上的实际取值未实测**）：

- **`icount`** —— 唯一在代码里显式强制 single 的条件（`tcg-all.c:113-139`），且 `multi`+icount 会**硬报错**
- **`mttcg_supported`** 为假
- 每客体门控位（ARM 为 true，不构成风险）
- **更正**：初稿说"TCI 会强制单线程"—— **错误**。`CONFIG_TCG_INTERPRETER` 在 `accel/` 下**零命中**，代码**不阻止** TCI + multi。
- **更正**：初稿说"宿主缺 128 位原子会退化"—— 实际上**没有针对 64 位原子的运行时检查**；`CONFIG_ATOMIC128` 的探测只覆盖 128 位。

**验证方法（不需启动 guest）**：

```bash
# 期望：multi（若输出 single，则 MTTCG 被上述某条件抑制）
<qemu-system-aarch64> -accel tcg,thread=multi -machine virt -S -display none \
    -monitor none -serial none -qmp stdio <<< '{"execute":"qmp_capabilities"}' 2>&1 | head
```

---

## 2. ARM64 宿主的可执行优化矩阵

**前提**：宿主 ARM64，无硬件虚拟化（或跨 ISA 必须 TCG）。下表是**配置建议**，不是实测结果。

### 2.1 ARM64 宿主 / **ARM64 客体**（同架构，但仍走 TCG → 性能损失最大）

| 维度 | 建议 | 理由 |
| --- | --- | --- |
| `thread=multi` | **是**（默认） | 同架构 + ARM64 宿主有 atomics ⇒ MTTCG 有效。vCPU 数按宿主大核数，**不要超过物理核数**（超订会加剧 TCG 的 cache 压力） |
| vCPU 数 | 宿主大核数（建议 ≤ 4） | TCG 每 vCPU 一个线程，各自持有 TB cache 写权限 |
| `-tb-size` | 见 §2.4 | TB 越大，TB lookup 的 hash 压力越小，但 cache 局部性变差 |
| 代码缓存回收 | 默认（`tb_flush` 在满时触发） | 回收会丢弃所有已链接 TB，代价高；宁可加大 `-tb-size` 也不要频繁 flush |
| guest/host page size | **重点**：若宿主 16K 页而 guest 期望 4K，softMMU 的映射粒度与 TLB 行为会变化 | 未实测，需在真机核对 |
| 内存序 | ARM64 是弱内存序；TCG 需要插入正确的 barrier | `tcg/aarch64/tcg-target.c.inc` 的 `tcg_out_mb` 已实现；**不要试图"优化掉" barrier** |
| 原子指令 | 由 host atomics + `CONFIG_ATOMIC128` 支撑 | 见 §1 |
| softMMU | **最大开销来源之一** | 见 §5.2 |
| 向量扩展 | guest NEON 走 TCG vector ops | 覆盖度需逐指令核对 |
| 设备模型/显示/音频 | **全部关掉**（headless, `-nographic`） | 见 §3 |

### 2.2 ARM64 宿主 / **x86-64 客体**（跨 ISA，必然 TCG）

| 维度 | 建议 |
| --- | --- |
| `thread=multi` | **是**（跨 ISA 不禁止 MTTCG，只要宿主 atomics 可用） |
| vCPU 数 | 同上，但跨 ISA 的翻译成本更高，**建议 1–2 vCPU**（翻译吞吐是瓶颈，不是并行度） |
| `-tb-size` | **加大**（跨 ISA 的 TB 更多、更大） |
| 内存序 | x86 是强内存序而宿主 ARM64 是弱序 ⇒ TCG 需要**更多** barrier，这是跨 ISA 的主要额外成本 |
| softMMU | 同样是大头；x86 客体的 `CR3`/分页层次更深 |
| 向量 | guest SSE/AVX 需要 TCG vector ops 覆盖；**AVX 在 TCG 下支持有限**，这是已知的跨 ISA 弱项 |

### 2.3 ARM64 宿主 / **RISC-V64 客体**

| 维度 | 建议 |
| --- | --- |
| `thread=multi` | **是** |
| vCPU 数 | 1–2 |
| 向量 | guest RISC-V V 扩展在 TCG 下的支持**需单独核对**（`target/riscv/` 的向量实现与 TCG vector ops 的对接），本报告未验证 |
| 其它 | 同跨 ISA 的通用结论 |

### 2.4 `-tb-size` 上限的现实约束

QEMU 的 `-tb-size` 影响 TB cache 的**容量**，但：

- 它**不是**"越大越好"：TB 结构本身占用 RSS，而任务书规定**单进程 RSS 达 1.5 GiB 即停止** —— 这是本次审计的硬边界
- 因此**不能**给出一个"推荐 TB 大小"的确定值而不测。**测量方法**见 §3.5。
- 一个保守的探索区间（**未实测**）：从默认值起步，按 1.5× 递增，同时观察 RSS 与 TB flush 次数，直到二者之一触界。

---

## 3. 低内存快速配置

### 3.1 只构建 aarch64-softmmu（单一 target）

已写入 patch：`--target-list=aarch64-softmmu`。

**关键理由**：x86_64 / riscv64 必须作为**独立的可执行文件**构建，**不能塞进同一进程** —— 每个 target 有自己的翻译器与 TB 结构，多 target 同进程会让 RSS 与磁盘近似线性增长，且互相争抢 TB cache。

### 3.2 关键裁剪项（全部为**配置即可**，无需改源码）

patch 内已包含：`--without-default-devices`（体积影响最大）、`--disable-gtk/sdl/curses/cocoa/opengl/spice/vnc`（headless，全部不链接）、`--disable-docs/tools/guest-agent/plugins`、`--disable-debug-info/debug-tcg/debug-mutex/debug-graph-lock/debug-stack-usage/debug-remap`、`--disable-multiprocess/seccomp/tpm/smartcard/libudev/libssh/curl/gnutls/nettle/numa/rdma/bpf/capstone/libiscsi/libnfs/glusterfs/zstd/lzo/snappy/bzip2/attr`、`--disable-vhost-*`（全部）、`--disable-kvm/hvf/whpx/xen`、`--disable-tcg-interpreter`（**必须**，否则退化为解释器且强制单线程）。

保留：`--enable-slirp`（用户态网络，避免额外授权）、`--enable-fdt=internal`（virt 机器需要）。

**注意 `--without-default-devices` 的后果**：QEMU 不再自动提供默认设备，**virtio 设备必须在命令行显式给出**。这既是优点（小）也是坑（忘写设备会导致 guest 起不来）。

### 3.3 virtio 优先 / headless

- 存储：`-device virtio-blk-pci` 或 `virtio-blk-device`（**不要** IDE/AHCI/SATA）
- 网络：`-device virtio-net-device` + `-netdev user`（slirp）
- 控制台：`-nographic` / `-display none -serial mon:stdio`
- **绝不要**：图形、音频、USB、TPM、smartcard —— 这些是纯开销

### 3.4 OHOS 特有的三个未知（必须在真机验证，**不得假设**）

1. **`mprotect(PROT_EXEC)` / split-W^X 是否被允许**：若无 JIT 权限，QEMU 会退化到 TCI，性能下降一个数量级。**这是 OHOS 上最大的单一风险。**
2. **128 位原子是否可用**：`CONFIG_ATOMIC128` 的检测结果决定 MTTCG 能否启用。
3. **glib/pixman 的缺口比初稿描述的更窄（重要更正）**：初稿说"当前**无法**交叉编译 QEMU" —— **只对一半**。实测：
   - 仓库里**已有交叉编译好的 OHOS glib 2.78.0 私有前缀**；
   - **OHOS aarch64 的 QEMU meson configure 已经成功过**（`build-qemu-ohos-deps/qemu-config-probe`，含 aarch64-softmmu，TCG 原生后端 YES，pixman 显式 `-Dpixman=disabled`）；
   - **但一个 QEMU 目标文件都没编译过**（`find ... -name '*.o' | wc -l` → 1，仅 config 探测）。

   **准确表述：OHOS 全量编译「从未验证」，而不是「工具链不可能」。** 这两者的差别很大 —— 后者会让人放弃，前者只需排期。

### 3.5 测量方法（不启动 guest 也能做的部分）

```bash
# --- 磁盘占用（构建后，只读）---
du -sh qemu-source/build
du -sh qemu-source/build/qemu-system-aarch64
size -m qemu-source/build/qemu-system-aarch64 2>/dev/null | head   # Mach-O；ELF 用 size -A

# --- 链接后的动态依赖（确认裁掉了什么）---
otool -L qemu-source/build/qemu-system-aarch64     # macOS
readelf -d  qemu-system-aarch64                    # OHOS/ELF

# --- 符号级确认：UI 后端是否真的没链进来 ---
nm -u qemu-system-aarch64 | grep -cE "gtk_|SDL_|vnc_|spice_"   # 期望 0

# --- RSS / TB 行为（需要运行，且不启动 guest：-S 冻结 CPU）---
#   注意：本任务禁止启动 guest，但 -S 是"不启动 guest"，需由设备侧执行者决定
/usr/bin/time -v qemu-system-aarch64 -machine virt -S -display none \
    -monitor none -serial none -accel tcg,thread=multi \
    -m 512M -tb-size <N> 2>&1 | grep -E "Maximum resident"
```

**RSS 门槛**：任务书规定 1.5 GiB 即停止 —— 上述命令应配合外部监控（如 `ps -o rss=` 轮询）使用，**不要**依赖 `/usr/bin/time` 的单次峰值。

**本节所有数字均为"未实测"**。报告不给出任何预计值。

---

## 4. 编译级优化评估

| 项 | 判定 | 说明 |
| --- | --- | --- |
| **QEMU 默认优化级** | **`optimization=2`（-O2）** | 实测：`meson.build` 默认 options 含 `'optimization=2'`。**不是 -O0，也不是 -O3。**<br>**当前项目基线**（取自 `build-qemu-inprocess/qemu-build/meson-info/intro-buildoptions.json`）：`-O2 -g -Werror -fPIC`、`b_lto=false`、`debug=true`、`b_ndebug=false`。<br>⚠️ **`-Werror` 是硬约束** —— 任何新优化 flag 只要引入一条新告警，构建就直接失败。这会让 §4 中"试一下某个 flag"的成本远高于预期。 |
| **LTO** | **meson 有支持** | 实测 `meson.build:640 if not get_option('b_lto')` —— 存在 `b_lto` 分支。可用 `--enable-lto`（若 configure 暴露）或 `-Db_lto=true`。 |
| **ThinLTO** | **需 OHOS clang 支持** | clang 的 `-flto=thin` 是 Clang 特性，OHOS clang 15.0.4 基于 LLVM 15 ⇒ **很可能支持**，但**我未实测该 SDK 的 `--help`**（本轮未对 SDK 跑命令）。**标记为未验证**。 |
| **PGO 两阶段** | **未验证** | `-fprofile-generate` / `-fprofile-use` 是 clang 标准能力。**但**：QEMU 没有内置 PGO 流程，且 PGO 的 profile 必须来自**真实负载**（即在真机上跑 guest）—— 与"不启动 guest"的约束冲突。**本轮不建议。** |
| **-O3** | **可配置但不建议** | **重要更正：初稿（与任务书）的前提「-O3 挤压 TB cache」不成立。** TB 里的机器码体积由 **QEMU 自己的 TCG 优化器**决定（`tcg/tcg.c:6592 tcg_optimize`、`tcg/optimize.c:3028`），宿主编译器的 `-O` 级别**与 TB 体积没有任何数据通路**；`tb_size` 只来自 `tcg/region.c:748,766-780` 或 `-accel tcg,tb-size=N`。<br>**真实风险**是宿主 text/RSS 膨胀（对本项目 in-process 形态尤其相关）+ 引入新告警（见下 `-Werror`）。 |
| **`-mcpu` 基线 + 运行时多版本** | **`-mcpu` 可用；`target_clones` 实测不可用且不需要** | `--print-supported-cpus` 实测含 **`cortex-a76` / `neoverse-n1` / `generic`**；`-O3 -mcpu=cortex-a76 -mtune=neoverse-n1` 实测 exit=0。<br>**`target_clones` 实测被拒**：`error: function multiversioning is not supported on the current target`；`__attribute__((target("cpu=...")))` 被**静默忽略**。<br>**但不需要** —— QEMU 自带运行时分派：`tcg/aarch64/tcg-target-has.h:13 have_lse2 = cpuinfo & CPUINFO_LSE2`，`util/cpuinfo-aarch64.c:56-71` 用 `getauxval(AT_HWCAP)`。 |
| **隐藏符号** | **需侵入且具破坏性 —— 不要做** | **更正**：初稿建议 `-fvisibility=hidden`，**这是错的**。QEMU 不靠它控导出 —— `meson.build:4012-4016` 生成 `qemu.syms`/`block.syms` + `link_depends`；全局加 `-fvisibility=hidden` 会**破坏 modules/plugins**。<br>可用的只有 **`-Wl,--exclude-libs,ALL`**（`ld.lld --help:71`，只影响 `.a` 归档的自动导出），低风险。 |
| **死代码消除** | **需侵入且高风险 —— 不建议** | **更正**：初稿说"配置即可"，**这是错的**。QOM 类型注册全靠 `__attribute__((constructor))`，本项目 `meson.build:4404+` 的本地注释已点名此坑（"constructors are NOT referenced by any symbol"）。不加 `KEEP`/`-u` 会**静默丢类型**，后果是运行时 `no target-info is available`。 |

**二进制膨胀**：ThinLTO 与 -O3 的方向都是**增大**（LTO 可能因内联而增大，-O3 亦然）。**具体幅度未实测，不给数字。**

---

### 4.1 配置即可 vs 需要侵入修改（明确分级）

**A. 配置即可（上游已有开关，零源码改动）**

| 开关 | 作用 |
| --- | --- |
| `-accel tcg,thread=multi\|single` | MTTCG 选择 |
| **`-accel tcg,tb-size=N`（MiB）** | TB 缓存大小 —— **注意不是已移除的 `-tb-size`** |
| `-accel tcg,split-wx=on` | 强制 split-W^X（需 memfd） |
| `-smp N` | vCPU 数（同时决定 `tcg_ctxs` 与 region 数） |
| `-display none` / `-nodefaults` / `-audiodev none` | 设备与前端开销 |
| `-Dpixman=disabled` | 构建期去掉 pixman |
| `--enable-tcg-interpreter` | 构建期 TCI（无 exec 平台的唯一出路） |
| HMP `info jit` | **观测** TB flush 次数 / 已生成代码量（`tcg-stats.c:150-190`） |
| `-d nochain` / `one-insn-per-tb` | 仅调试，会**禁用直链**，不得用于生产 |

**B. 需要侵入修改（上游确实不存在，不要包装成已有）**

宿主 256-bit 向量执行；SVE 宿主代码生成；**LSE 加速的客体原子**（`have_lse` 已定义但未被使用）；**单 TB / 增量代码回收**（当前只有全量 flush）。

另有两项非性能类：`docs/devel/multi-thread-tcg.rst:28-33` 与代码行为不一致（文档称按 host memory model 判定，代码实际是 `mttcg_supported && !icount_enabled()`）；以及本仓库自有的 in-process 补丁（见 §5.1）。

---

### 4.2 优化矩阵的关键事实更正

| 维度 | 更正后的实测事实 |
| --- | --- |
| **页大小** | softMMU 粒度与客体 granule 是**两件事**。ARM 系统模式 v7+ 为 **4 KiB** 客体 granule（`target/arm/cpu.c:2125-2139`），i386/riscv 亦 4 KiB。宿主 16 KiB 只影响**代码缓存 region 对齐**与 RAM mmap 粒度 —— **初稿把它说成"映射粒度变化"是不准确的**。 |
| **内存序** | ARM64 与 RISC-V64 客体 `default_memory_order = 0`，宿主 MO = 0 ⇒ **默认路径不额外插屏障**。**x86-64 客体**才是例外：`TCG_MO_ALL & ~TCG_MO_ST_LD` ⇒ 在 arm64 宿主上按需插 `smp_mb()`（`cputlb.c:2297-2380`、`:2698-2778`）。这是**结构性**额外成本，不是配置能消除的。 |
| **softMMU** | 系统模式**恒为 true**（`tcg-internal.h:37-41`），**无开关**。动态 TLB 默认 **2^8**（`tlb-bounds.h:11`），**无 TLB 尺寸开关**。 |
| **代码缓存回收** | 仅 **3 条**全量 flush 路径（缓存耗尽串行/排队、`loadvm`）。**无单 TB 回收**；SMC 失效**不回收代码字节**，要等下一次全量 flush（`tb-maint.c:771-792`）。 |
| **直链** | 系统模式下**跨页 TB 不直链**（`cpu-exec.c:993-995`）；可按 TB 用 `CF_NO_GOTO_TB/CF_NO_GOTO_PTR` 抑制（`translation-block.h:77-78`）。aarch64 宿主用 `B`(±128 MiB)，超出则 `LDR` 字面量间接（`tcg-target.c.inc:2018-2059`）。 |
| **本机宿主探针** | arm64 / 16 GiB / **host page 16 KiB** / LSE=1 LSE2=1 AES=1 PMULL=1 BTI=1 / **无 SVE**。逐字复制 meson 的 atomic 探针用 Apple clang 21 编译链接**均成功（无需 `-latomic`）**，反汇编为 `ldp`/`stp`/`casp`。 |

---

## 5. 热点与可落地 patch 计划

分类口径：**① 上游已有**（不要动）/ **② 配置即可** / **③ 需要侵入修改**。

| 热点 | 位置（QEMU 11.1.50） | 分类 | 说明 |
| --- | --- | --- | --- |
| **翻译循环** | `accel/tcg/translate-all.c:261` `tb_gen_code`、`accel/tcg/cpu-exec.c:1022` `cpu_exec`、`tcg/tcg.c:6556` `tcg_gen_code` | **① 上游已有**（**例外见下**） | ⚠️ **唯一明确可落地的翻译循环优化点**：上游自认的 TODO 在 `translate-all.c:329-337` —— `buffer_overflow` 重试路径**不必要地重做了 `tcg_ops->translate_code` 与 `tcg_gen_code` 内部的 TCG 优化**。修它需要拆 TCG 后端，属**侵入**。 |
| **softMMU TLB** | `accel/tcg/cputlb.c`（`tlb_fill`/`probe_access`/`mmu_lookup`）、`tcg/aarch64/tcg-target.c.inc` 的 softmmu helper | **① 已有 + ③ 有条件** | TLB 快路径已优化。**③ 的诱因**是 guest/host page size 不匹配（16K/4K）——那需要看真机表现，**不要预先改**。 |
| **TB lookup / direct chaining** | **`accel/tcg/cpu-exec.c:198` `tb_htable_lookup`、`:230` `tb_lookup`、`:619` `tb_add_jump`** | **① 已有** | **路径更正**：初稿写的 `tcg/tb-maint.c` **不存在** —— 实际是 **`accel/tcg/tb-maint.c`**；而 `tb_lookup`/`tb_add_jump` **根本不在 tb-maint.c，在 `cpu-exec.c`**。`tb-hash.h` 亦在 `accel/tcg/` 下（不在 `include/exec/`）。<br>上游已有 jmp cache + QHT 两级 + 原子 cmpxchg 占位 + CF_INVALID 防护（`:640` 把宿主绝对地址打进指令流）。**配置旋钮**：`-accel tcg,tb-size=N`（`tcg-all.c:273`）—— 零代码改动的首选。 |
| **helper 调用** | `tcg/tcg.c:2502` `tcg_gen_callN`、`:5874` `tcg_reg_alloc_call`；`include/tcg/tcg.h:218-229` `TCG_CALL_NO_RW*` | **① 已有 + ③ 值得做** | **最值得做的侵入项**：审计热点 helper 是否**漏标** `TCG_CALL_NO_RW*`。这是**机械、可 grep 枚举、低风险**的。<br>⚠️ 附带待核对项：`tcg/aarch64/tcg-target.c.inc:3420-3436` 的 clobber 只 reset 到 `V12`、**未含 V13/V14/V15** —— 审计员**未下判定**，标记为待核对（需确认这些寄存器在该后端是否可用）。 |
| **原子/内存屏障** | `tcg/aarch64/tcg-target.c.inc:1584` `tcg_out_mb`（`:1586-1592` sync 表）、`:1940-1942` LDXP/STXP 回退；`tcg/tcg-op.c:289` `tcg_gen_mb` | **① 已有 —— 不可动** | ⚠️ **`tcg-op.c:297-303` 在 softMMU 下 `parallel = true` 恒真**，注释明确：单 CPU 也有 iothread，去掉屏障会导致 **virtio 队列读错**。**绝对不可动。**<br>可落地项只有"让 `have_lse2` 真正生效"（即 §7.1 的运行时探测）。 |
| **virtio I/O** | `hw/virtio/virtio.c:2034` `virtqueue_pop`、`:1226` `virtqueue_push`、`:2633` `virtio_split_should_notify`；`hw/block/virtio-blk.c:1017` `virtio_blk_handle_vq`（`:1020` `MultiReqBuffer`、`:1021/1026-1028/1039-1041` **通知抑制已内置**、`:1044-1046` `submit_multireq`） | **② 配置即可** | **上游已有批量（MultiReqBuffer）+ 通知抑制** —— **没有需要新增的 batch/抑制配置项**。<br>可落地的全是配置：协商 `VIRTIO_F_EVENT_IDX`、启用 `iothread` + `ioeventfd`（`:1481-1488` 强制要求）、保持 `notification_data=off`（`:3477-3488`）。 |

### 5.1 审计中发现的真问题：工作树 `tcg/region.c` 的 in-process 补丁不完整

这不是 TCG 性能问题，而是**当前工作树里已存在的缺陷**，且与生命周期工作直接相关：

```bash
$ git --no-optional-locks diff HEAD -- tcg/region.c    # 仅 2 个 hunk / 39 行
```

- **hunk 2** 在 `tcg_region_init()` 顶部加了 `bool first_init = (region.n == 0);`（`region.c:763`），但 **`first_init` 在整份文件里只出现这一次、从未被使用**。注释声称"一次性资源全部由它守卫"，**该守卫并未实现**。
- 后果：**第二次 `qemu_init()` 仍会再次 `alloc_code_gen_buffer()`**（再 mmap 一次并覆盖 `region.start_aligned`，**旧映射未 munmap**）、再次 `qemu_mutex_init(&region.lock)`、再次 `tcg_region_trees_init()`。
- **`hunk 1`（在 `tcg_region_initial_alloc()` 里重置 `region.current`/`agg_size_full`）是有效的**，能消掉 `g_assert(!err)`。
- 附带影响：**region 几何被首次 init 冻结** —— 之后的 `tb_size` / `splitwx` / `max_threads` 不再生效。

**建议**：在继续 in-process 生命周期工作前先补完这个守卫，否则每周期泄漏一次代码缓存映射。（该文件归生命周期所有者，本报告不修改它。）

---

**结论（六热点汇总，实测）**：**六个热点里没有任何一个是"上游尚未优化、躺着等我们改"的。** 六项中 3 项是纯"上游已有"，配置即可的是 `-accel tcg,tb-size=N` 与 helper flag 审计；**真正值得做的侵入项只有两个**：

1. **B-4**：审计热点 helper 是否漏标 `TCG_CALL_NO_RW*`（机械、可 grep、低风险）
2. **B-1**：`translate-all.c:329-337` 的**上游自认 TODO**（`buffer_overflow` 重试路径重复翻译，需拆 TCG 后端）

除此之外的一切"改 TCG 核心求加速"都是无的放矢。真正能立刻做的是 **② 配置类**（§3 的裁剪 + §4 的死代码消除）。任何 ③ 类改动都应先有**真机 profile 证据**，否则是无的放矢。

---

## 6. 持久化 TB / AOT 热块缓存 —— **明确否决**

**结论：在当前架构下不可行，不建议投入。** 理由逐条如下（这也是任务书要求讨论的全部维度）：

| 维度 | 问题 |
| --- | --- |
| **ASLR** | TB 内包含**绝对分支目标**与指向宿主代码/数据的地址。持久化后重载，地址空间布局变了 → 所有内嵌地址失效。要做就必须"持久化时记录、加载时全部重定位"，而 TCG 的 TB 没有为可重定位而设计。 |
| **guest physical mapping** | TB 里的 softMMU 访问依赖于**运行期建立**的 guest→host 物理映射。同一 guest 内核在不同 boot 下物理布局可能不同，TB 无法跨 boot 复用。 |
| **页面权限** | TB 的**正确性依赖**"被翻译的 guest 页面在翻译后没有被改写"。加载一份旧的 TB 会绕过这个不变式 —— 必须重新校验所有相关页，成本接近重新翻译。 |
| **自修改代码（SMC）** | TCG 已有 SMC 检测（TB invalidation）。持久化 TB 与 SMC 检测**根本冲突**：旧 TB 的失效信息不存在于新进程。 |
| **代码页哈希与失效** | 要安全复用，必须对"翻译时的 guest 内存内容"做哈希并在加载时比对。这需要保存**每一份 TB 对应的 guest 页内容**，其体积与 TB 同量级 —— 收益被存储成本抵消。 |
| **版本键** | 一份可复用的 TB 缓存必须同时绑定：QEMU 版本、TCG 内部 IR/ABI 版本、**helper 集合与编号**、guest CPU feature 位、宿主 ISA 与 CPU feature、以及**编译选项**（-O2 与 -O3 生成的 helper 不同）。任一变化都必须整体失效。这不是一个"键"，而是一个易碎的矩阵。 |

**穷举证据：QEMU 完全没有持久化/AOT 机制。**

`tcg_tb_save` / `tb_save` / `CONFIG_TCG_AOT` / `aot` **全 grep 无匹配**（唯一命中是 PowerPC timebase 的 `trace_ppc_tb_load` 与 FDT 的 `dtb_load_data`，无关）。`include/exec/tb-flush.h` 只有 flush 接口。`tb_flush__exclusive_or_serial` 的全部调用点只有 4 类：`translate-all.c:294/297`（buffer 满）、`tcg-all.c:99`（loadvm）、plugins、linux-user。`tcg-all.c:273` 的 `-accel tcg` 只有 `tb-size` / `split-wx` / `one-insn-per-tx`，**无 cache-file 属性**。

**四条原理性不可行**（任一条即足够）：

- **ASLR**：TB 结构体含宿主绝对地址（`translation-block.h:98 tc.ptr`、`:128 jmp_target_addr`、`:147-149 jmp_list_*`），而**已生成的机器码本身**也被 `cpu-exec.c:640 tb_set_jmp_target(..., (uintptr_t)tb_next->tc.ptr)` **打成宿主绝对跳转**。缓冲区是 `mmap(NULL, ...)`（`region.c:559`），每次地址不同。QEMU 这些字段**没有任何重定位信息** —— 要做就得在 TCG 之外再造一层链接器。
- **SMC 自修改代码**：softMMU **不做 mprotect**（`tb-internal.h:27-40` 注释：只有 user-only 才 mprotect）。检测完全靠运行时 `DIRTY_MEMORY_CODE` 脏位（`cputlb.c:1344-1346` → `tb_invalidate_phys_range_fast`）。**冷加载时脏位根本不存在。**
- **guest 物理映射变化**：键是 `phys_pc`，而本版本内存区域变更**不 flush TB**；唯一补偿是跨页禁直连（`cpu-exec.c:993-995`）。
- **版本键（无法可靠构造）**：六项**全部缺失** —— QEMU 版本无代码级指纹；**`tcg/tcg.h` 没有 TCG ABI/opcode 版本常量**；helper 表是编译期生成的（`include/exec/helper-info.c.inc`，参数 layout 由 `tcg.c:2513-2516 init_call_layout` 算出）；`cpuinfo` 是**运行时值**未入指纹；`TCG_TARGET_*` 是编译期常量；`-O/-mcpu/-flto` **不留任何痕迹**。任一项不一致 → 生成码调用错误 helper 地址或错误参数 layout = **直接内存破坏，不是性能退化**。

**最锋利的一条否决理由**：**即使把代码页哈希做对**，SMC 与映射两轴都会退化为"**每次加载全量校验**" —— 省下的只是翻译（`tcg_optimize` + liveness + regalloc + 编码），**省不掉校验**（读 + 哈希 guest 页 + 重建 `PageDesc`/`page_next` 失效链）。**收支是否为正没有任何实测数据，绝不能当作"白捡的加速"。**

**上游自己的选择也印证了这一点**：`tcg-all.c:88-101` 的注释是设计者的结论 —— 连 `loadvm`（QEMU 全控状态）都因为"绕过常规 flush 机制"而必须 `tb_flush__exclusive_or_serial()`。持久化 TB 的前提恰恰是"绕过常规 flush 且跨进程"，**比 loadvm 更弱**。

**因此：否决。**

**唯一诚实的受限替代**（若一定要投入）：**持久化"热度统计"而非机器码** —— 导出 guest 页/PC 的执行频次 profile，用于下次启动时指导 `-tb-size` 取值、预翻译决策、翻译粒度调优。它不触碰上述任何一轴（持久化的是统计量，不是代码），加载后仍由 QEMU 正常翻译。**收益未实测。** 如果追求"冷启动更快"，正确方向是**减少需要翻译的代码量**（`--without-default-devices`、极小 guest 镜像、把热路径放进 guest 侧的一次性初始化），而不是持久化 TB。

---

## 7. Patch 与未来 Benchmark 方案

### 7.1 Patch 内容与可应用性（实测）

**文件**：`.deepseek/patches/harmony-tcg-fast-profile.patch`（120 行）

**内容**：**仅新增一个配置文件** `scripts/ohos-tcg-fast-configure.sh`（候选配置脚本，115 行）。**不含任何对 QEMU 源码的修改。**

**验证（实测，非声称）**：

```bash
# 在临时副本上检查与真实应用
$ cd /tmp/vwtcg/verify && git init -q . && <基准提交>
$ git apply --check  .../harmony-tcg-fast-profile.patch     # CHECK EXIT=0
$ git apply          .../harmony-tcg-fast-profile.patch     # Applied cleanly
$ sh -n scripts/ohos-tcg-fast-configure.sh                  # 语法 OK
```

**未落入源码树** ✓。`qemu-source` 的 `git status` 在本任务前后**逐字一致**（无新增改动）。

**注意**：patch 只提供**候选 profile**。它**未在 OHOS 上跑过**（且当前**不可能**跑 —— sysroot 缺 glib/pixman）。

### 7.2 未来在真实 HarmonyOS 设备上的 Benchmark 方案

**前置**：先在 OHOS sysroot 里交叉构建 glib-2.0 与 pixman-1（否则无法编译 QEMU）。

| # | 项目 | 方法 | 门槛 |
| --- | --- | --- | --- |
| 1 | **启动到 shell** | 最小 guest 镜像（如 buildroot 的 aarch64 minimal），`-machine virt -nographic`，测量"从 QEMU 进程启动到 guest shell 出现"的墙钟时间 | 与基线对比；**绝对值随镜像不同，必须固定镜像** |
| 2 | **CoreMark** | 在 guest 内跑 CoreMark（单核与多核），对照**宿主机原生 CoreMark** | 不要报"绝对分数"，报**TCG/原生比值** —— 这才可比 |
| 3 | **内存** | guest 内 `memcpy`/`memset` 带宽 + 宿主侧 RSS（1.5 GiB 门槛） | RSS ≤ 1.5 GiB；带宽与原生比值 |
| 4 | **virtio-block** | guest 内 `dd`/`fio` 顺序与随机读写，`virtio-blk-device` | 与原生比值；确认无 IDE/SATA 回退 |
| 5 | **不同 ISA** | 同一硬件上跑 **ARM64 客体**与 **x86-64 客体**（后者需独立构建的 x86_64-softmmu 二进制） | 量化跨 ISA 的额外成本；**不得**在同一进程内混跑 |
| 6 | **温度/功耗** | 设备侧热区与电流采样（持续负载 ≥ 10 分钟） | 设定**热节流阈值**与**功耗上限**；出现节流则该配置不可用于长时运行 |

**门槛设置原则**：
- 每项都要求**基线对比**（同设备原生 or 上一版配置），**不接受单点数字**
- 温度/功耗必须有**硬阈值 + 超限即判失败**，否则"性能提升"可能只是把功耗推高
- 所有测试固定 guest 镜像、固定 QEMU 版本、固定 `-tb-size`，否则数字不可比

---

## 8. 给总监的决策

### 短期最值得做的 3 项

0. **在 OHOS 设备上打印 `getauxval(AT_HWCAP)` / `cpuinfo`，确认 `have_lse2` 是否生效。**
   这是**优先于一切编译 flag 的第一优先级**，理由：`CONFIG_GETAUXVAL` 已在 OHOS configure 探测中定义、musl 也有声明，**编译期路径正确**；但 `getauxval(AT_HWCAP)` 在 **OHOS App 沙箱下运行时是否返回真实值 —— 未验证**。若返回 0：`have_lse2` 关闭 ⇒ 16B 原子退化为 **LDXP/STXP 循环**（`tcg-target.c.inc:1891-1948`），AES/PMULL 也关闭。这是**可运行时可观测、可修、收益机制明确**的项，比任何编译选项都更值得先做。

2. **落地 §3 的裁剪配置**（`--without-default-devices` + headless + 单 target）。这是**唯一确定有收益且零源码风险**的动作，已写成可应用 patch。收益方向明确（更小的二进制、更少的常驻代码、更少的设备开销）。
2. **在真机上先确认两件事**：**MTTCG 是否真的启用**、以及 **JIT 是否被允许**。
   - MTTCG：`-accel tcg,thread=multi` 是否被接受、是否退化为 single。注意禁用条件只有三条 —— `!mttcg_supported`、**icount**、显式 `thread=single`；**TCI 并不阻止 multi**（初稿的相反说法已更正）。
   - JIT / W^X：**默认 `splitwx_enabled = 0`，即默认走 anon `PROT_NONE` → `mprotect` 到 RWX**。这是需要重点确认的路径。若平台禁止 `mprotect(PROT_EXEC)`，QEMU 会在 `tcg/region.c:824-833` **`exit(1)`**。
   - **OHOS 的配置级解法**：项目走 `aarch64-linux-musl` ⇒ `CONFIG_LINUX` ⇒ **memfd 可用**（内核 ≥ 3.17），因此 **`-accel tcg,split-wx=on` 就能拿到真正的 W^X**，无需 TCI。只有连 memfd + `PROT_EXEC` 都被禁时，才需要构建期为 `--enable-tcg-interpreter`（代价是性能降一个数量级）。
3. **建立 §7.2 的测量基线**（哪怕只有第 1、2 项）。**没有基线就无法判断任何优化是否有效** —— 这是当前最大的方法论缺口。

### 短期应立刻处理的缺陷（不属于"优化"，但优先于优化）

**工作树 `tcg/region.c` 的 in-process 补丁不完整**（详见 §5.1）：`first_init` 变量**定义了但从未被使用**，注释声称的守卫并未实现。后果是**第二次 `qemu_init()` 仍会再次 `alloc_code_gen_buffer()`**（再 mmap 一次并覆盖 `region.start_aligned`，**旧映射未 munmap**）、再次 `qemu_mutex_init`、再次 `tcg_region_trees_init()` —— **每个周期泄漏一次代码缓存映射**。此外 **region 几何（`tb-size`/`splitwx`/`max_threads`）被首次 init 冻结**，后续周期不再生效。

这直接削弱 §3、§4 所有 `tb-size` 相关结论在**第二周期之后**的有效性。**建议在继续生命周期工作前先补完该守卫。**（该文件归生命周期所有者，本报告未修改它。）

### 中期 3 项

1. **交叉构建 glib-2.0 + pixman-1 到 aarch64-ohos**（解锁真正的 OHOS 构建）。这是**目标依赖缺口**，不是 SDK 缺失。
2. **在真机上做 §3.5 的 RSS / TB 行为测量**，据此决定 `-tb-size` 与 vCPU 数，而不是照搬桌面参数。
3. **按 §4 试编译级选项**（先 `--gc-sections`，再 ThinLTO），**每项都要用 §7.2 的基线对比**，不可只报体积变化。

### 应放弃的 3 项

1. **持久化 TB / AOT 热块缓存** —— §6 已逐维度否决。与 SMC、ASLR、版本键根本冲突，且需与上游分叉。
2. **`-O3` 作为默认优化级** —— QEMU 上游选 -O2 有 TB/I-cache 容量的理由；-O3 对 TCG 这种"翻译器是热点"的场景可能反向。要试也必须实测，**不应默认启用**。
3. **修改 TCG 的翻译循环 / softMMU / barrier 以求"加速"** —— §5 已阐明这些都是**上游已优化**或**正确性关键**的路径。在没有真机 profile 证据前改动它们，风险远大于收益。

> **注意区分**：§5.1 那个 `tcg/region.c` 的 `first_init` 补全属于**修复既有缺陷**，不属于本项"为加速而改核心"。前者应做，后者应放弃。

### 最重要的一句话

**即使上述全部完成，TCG 仍然不等于硬件虚拟化。**

TCG 是**指令级动态翻译**：每条 guest 指令都要经过"查 TB → 未命中则翻译 → 执行宿主代码"的路径，且需要模拟特权级、MMU、设备。硬件虚拟化（KVM/HVF/WHPX）让 guest 指令**直接在 CPU 上执行**，开销接近于零。

因此：
- TCG 的**绝对性能上限**远低于硬件虚拟化，这是架构性的，不是调优能弥补的
- §1–§5 的一切工作，目标应是"**把 TCG 的损失压到可接受**"，而不是"让 TCG 追上硬件虚拟化"
- 若产品需要接近原生的性能，**唯一出路是硬件虚拟化**（而这正是 HarmonyOS 上可能不可用的前提）

**不要向客户承诺"TCG 方案能达到原生性能"。**

---

## 9. 本报告未做/未验证的事

1. **未实测任何性能数字** —— 全文无一个实测性能值。
2. **未在 OHOS 上 configure 或编译 QEMU** —— 因为 **OHOS sysroot 缺 glib-2.0 与 pixman-1**（前一轮审计已证实），且任务禁止全量编译。
3. **未对 OHOS clang 跑任何命令** —— 因此 §4 中 ThinLTO / `-mcpu` 支持情况标为**未验证**。
4. **未启动任何 guest/VM/UTM**，未提交或推送。
5. **未逐条核对 `tcg/aarch64/tcg-target.c.inc` 的 vector 指令覆盖度** —— §1、§2 中相关结论标为"未逐条验证"。
6. 本报告**不是**"已实现"或"已优化"的声明。patch 只是一个**候选配置**。
