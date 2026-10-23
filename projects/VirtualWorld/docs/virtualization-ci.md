# VirtualWorld 跨平台 CI / 发布验证矩阵

本文件描述 VirtualWorld 虚拟化引擎的**诚实、可维护**跨平台 CI / 发布验证矩阵。
本阶段**只负责 CI 编排层**，不修改引擎、测试实现或现有打包门禁
（详见 `docs/virtualization-release.md`）。

核心原则：**宁可 skip，绝不伪装通过**；**绝不启动真实 VM**；**交叉编译不等于真机运行**。

---

## 1. 矩阵设计（7 个 host/arch 组合）

| Host | Arch | Runner 类型 | 默认触发（push/PR） | 执行模型 |
| --- | --- | --- | --- | --- |
| Windows | x86-64 | 托管 `windows-latest` | ✅ 运行 | native-host |
| Windows | ARM64 | **自托管** `self-hosted,windows,arm64` | ⛔ 仅 `workflow_dispatch` opt-in | native-host |
| macOS | x86-64 | 托管 `macos-13` | ✅ 运行 | native-host |
| macOS | ARM64 | 托管 `macos-latest` | ✅ 运行 | native-host |
| Linux | x86-64 | 托管 `ubuntu-latest` | ✅ 运行 | native-host |
| Linux | ARM64 | 托管 `ubuntu-24.04-arm` | ✅ 运行 | native-host |
| Linux | RISC-V64 | **自托管** `self-hosted,linux,riscv64` | ⛔ 仅 `workflow_dispatch` opt-in | native-host |

**runner 类型判定（诚实）：**
- **托管（hosted）**：GitHub 提供的 runner，可靠可得，默认 push/PR 即跑。
- **自托管（self-hosted）**：GitHub **没有**可靠的托管 runner（Windows ARM64、
  Linux RISC-V64 均无托管 runner）。这类组合**永不**被报告为"通过"——它们在
  push/PR 上被跳过，仅在维护者显式 `workflow_dispatch` 打开 `include_self_hosted`
  且提供匹配的自托管 runner 时才运行。
- **交叉编译 vs 真机**：本矩阵每个条目都在"与 arch 匹配的 runner"上**原生**
  构建/测试；没有任何条目用交叉编译冒充真机运行。即便如此，原生构建也**不**等于
  真机 guest 验证（我们从不启动 VM），因此 `real_hardware_verified` 恒为 `false`。

> Linux ARM64 使用 `ubuntu-24.04-arm`（GitHub 托管 ARM64 Linux，已 GA）。若所在
> 组织没有该托管能力，把它改为自托管即可——校验逻辑仍然通过，只是会变为 opt-in。

---

## 2. 工作流分层

`.github/workflows/virtualization-ci.yml` 分层如下：

1. **静态 / 配置检查（static-checks）**：运行**两套**脚本自测试
   (`generate_matrix_report.py --self-test` 与 `aggregate_results.py --self-test`)，
   生成权威的 `ci-matrix-report.json`，并作为 artifact 上传。这是矩阵的"契约"，
   不构建任何东西。
2. **每平台验证腿（verify）**：对每个 `(host, arch, contrib)` 矩阵腿依次跑
   configure → build → `ctest`（**同时覆盖引擎测试与打包门禁
   `VirtualWorldPackagingGate`**）→ 只读 CLI conformance（白名单守护）。随后**无论
   成功/失败/取消**（`if: always()`）都会产出一条 `virtualworld.ci-result/1` 结果
   JSON，状态由步骤结果**推导**而来（绝不受 artifact 上传成功影响）。
3. **打包门禁**：作为 `ctest` 中的一个测试随 verify 腿运行（macOS 上验证 `.app`
   布局、runtime、QEMU 二进制、固件、support、ad-hoc codesign）。
4. **只读 CLI conformance**：由白名单守护脚本 `conformance_guard.py` 强制只允许
   `--check` / `--list-backends` / `--json` / `--help`，**任何 `--start`（启动 VM）
   或非白名单 flag 一律拒绝**。
5. **聚合门禁（aggregate）**：在 verify 结束后（`needs: verify`，且 `if: always()`
   保证即使有腿失败/取消仍运行）收集所有 per-task 结果，执行"防假绿"校验并产出
   `ci-aggregate-report.json/.md`（详见第 3 节）。

**昂贵 / 真机组合**：Windows ARM64、Linux RISC-V64 通过 `if:` 条件在默认事件上被
GitHub 跳过；只有 `workflow_dispatch` 且 `inputs.include_self_hosted == true` 时
才会尝试在自托管 runner 上运行。普通 push/PR **不会**触发它们，因此这两条在默认
矩阵里**不产生**结果 JSON——聚合门禁也**不要求**它们（它们属于 opt-in，而非必需）。

---

## 3. 机器可读报告与结果回填 / 防假绿聚合门禁

本阶段在 CI 编排层新增**单任务结果 schema** 与**聚合门禁**，二者均由
`ci/virtualization/` 下的纯 Python 3 标准库脚本实现，本地与每个 runner 上行为一致。

### 3.1 单任务结果 schema `virtualworld.ci-result/1`

由 `result_schema.py` 定义与校验。每条 per-task 结果必须包含：

| 字段 | 说明 |
| --- | --- |
| `schema` | 固定为 `virtualworld.ci-result/1` |
| `run_id` | GitHub `github.run_id`（聚合层据此拒绝跨 run 混用） |
| `commit_sha` | `github.sha`（聚合层据此拒绝跨提交混用） |
| `host` / `arch` | 目标宿主 / 架构（arch ∈ `x86-64`/`arm64`/`riscv64`） |
| `runner_type` | `hosted` 或 `self-hosted` |
| `job_name` | 产出该结果的 job 名（如 `verify`） |
| `contrib` | `ON` / `OFF` |
| `step_status` | 四个步骤状态：`build`/`test`/`package`/`conformance`，取值 `passed`/`failed`/`skipped`/`cancelled`/`not_run`/`error` |
| `status` | 由步骤推导的总体状态（聚合层校验其不得与推导值冲突，防假绿） |
| `allowed_command_summary` | 该腿实际允许执行的命令摘要（白名单内） |
| `artifact_sha256` | `build` 通过时必须给出构建产物 SHA-256（64 位 hex） |
| `started_at` / `finished_at` | 形如 `2025-06-01T00:00:00Z` 的 UTC 时间戳 |
| `real_hardware_verified` | **恒为 `false`**（任何 `true` 直接被拒） |
| `skip_reason` | 自托管 / opt-in 被 `skipped`/`cancelled` 时必须给出原因 |

校验函数 `validate_result()` 会拦截：schema 不符、必填字段缺失/空、未知 arch、
runner_type 非法、contrib 非法、步骤状态词表外、hash 畸形、`build` 通过但缺 hash、
`real_hardware_verified` 非 `false`、`skipped`/`cancelled` 却无 `skip_reason`、
时间戳缺失/非法、声明状态与推导状态冲突（假绿）。

### 3.2 聚合门禁 `virtualworld.ci-aggregate/1`

由 `aggregate_results.py` 实现，消费所有 per-task 结果并产出
`ci-aggregate-report.json` + `ci-aggregate-report.md`。它**严格拒绝**：

- **跨 run_id 混用**（`AGG_CROSS_RUN_ID`）：所有结果必须同属一个 `run_id`。
- **跨 commit_sha 混用**（`AGG_CROSS_COMMIT`）：所有结果必须同属一个提交。
- **重复 `(host,arch,contrib)`**（`AGG_DUPLICATE_COMBO`）：同一组合不允许两条结果。
- **缺失必需（托管）组合**（`AGG_MISSING_REQUIRED`）：每个 `hosted` × `{ON,OFF}`
  组合都必须有**实际跑过**（状态为 `passed`/`failed`）的结果；自托管 / opt-in
  组合不是必需的，其缺位属诚实预期。
- **过期时间戳**（`AGG_STALE_TIMESTAMP`）：任何结果 `finished_at` 早于其余结果
  最大 `finished_at` 超过 24h，或结束早于开始。
- **伪造 passed**（`AGG_TASK_FAILED` + `RESULT_STATUS_MISMATCH`）：任一必需组合
  以 `failed`/`error` 结束，或声明 `passed` 却与推导状态不符。
- **hash 缺失 / 重复**（`RESULT_HASH_MISSING` / `AGG_HASH_DUPLICATE`）：`build`
  通过必须带 hash；两条结果不得共用同一 hash。
- **未知状态**（`RESULT_UNKNOWN_STATUS`）：步骤/任务状态词表外一律拒绝。

`aggregated_status` 仅在所有校验通过**且**每个必需组合都 `passed` 时才为
`passed`；否则为 `failed`。**`real_hardware_verified` 在聚合报告里也恒为 `false`**。

### 3.3 结果如何产生（emit_result.py）

每个 verify 腿末尾（`if: always()`）调用 `emit_result.py`，读取
`steps.build.outcome` / `steps.test.outcome` / `steps.conformance.outcome`，用
`result_schema.build_result` 推导状态并写入结果 JSON；若 `build` 通过且产物存在则
计算其 SHA-256。随后上传 artifact，其**名称包含 `run_id + host + arch + contrib`**
（`vw-result-<run_id>-<host>-<arch>-<contrib>`），从 artifact 层防止跨 run / 跨
组合串包。聚合 job 用 `download-artifact` 的 `pattern: vw-result-*` +
`merge-multiple: true` 收齐后交给聚合器。

**关键诚实点**：聚合 job 的 `if: always()` 保证即便有腿失败/取消也会运行；聚合状态
完全由 per-task 步骤结果推导，"上传 artifact 成功"**绝不**等价于构建/测试成功。

### 3.4 静态计划报告 `ci-matrix-report.json`

由 `generate_matrix_report.py` 生成（`--yaml` 可附轻量 YAML 结构检查），至少包含：
每个条目的 `host`/`arch`、`runner_type`、`on_default`、`execution_model`、
`cross_compiled`、`real_hardware_run`、`real_hardware_verified`、`build`/`test`/
`package`/`conformance` 状态、以及 `skip_reason`。

**强制诚实字段**：
- `real_hardware_verified`：每个条目恒为 `false`。
- `real_hardware_run`：每个条目恒为 `false`（从不启动 guest VM）。
- 状态只有 `scheduled`（默认会跑）/ `opt_in_required`（需 opt-in + 自托管）/ 其它
  ；在 `ci_run_id` 为 `null` 的静态计划里，绝不允许出现 `passed`/`failed`（防止把
  计划伪装成真实结果——自测试会拦截这种"错误标真"）。

`summary` 给出总数、scheduled、opt_in_required、hosted/self-hosted 计数，以及
`real_hardware_verified_false_count`。

---

## 4. 命令行白名单（禁止 VM 启动）

`ci/virtualization/matrix.py` 定义并单测：
- **允许**：`--check` / `--list-backends` / `--json` / `--help` / `-h` / `--version`
  （以及无害的位置参数，如 arch 名）。
- **禁止**：`--start` / `--launch` / `--boot` / `--power-on` / `--run-vm` /
  `--resume`（任何能启动 guest VM 的 flag）。
- 任何以 `-` 开头但不在允许集合中的 flag 也拒绝。

`conformance_guard.py` 是运行期强制：CI 的 conformance 步骤经它执行命令，命中禁止
flag 直接以退出码 2 拒绝，绝不退让。

---

## 5. 自测试覆盖（`--self-test`）

`generate_matrix_report.py --self-test`（矩阵计划，纯标准库，**16 项全绿**）：
- **未知架构**：出现 `sparc` 等未知 arch → 校验必须拒绝。
- **缺 runner**：默认条目缺少 runner 标签 → 校验必须拒绝。
- **错误标真**：任意条目 `real_hardware_verified=true` → 校验必须拒绝。
- **伪造通过**：计划里（`ci_run_id=null`）出现 `passed` → 校验必须拒绝。
- **白名单**：`--start` 被拒；`--check --json` / `--list-backends` 被允许；
  未知 flag 被拒；带 arch 位置参数的只读命令被允许。
- **矩阵完整性**：缺少任一必需 (host,arch) 组合 → 校验必须拒绝；且总数为 7。

`aggregate_results.py --self-test`（聚合门禁，纯标准库，**17 项全绿**，确定性
固定时间/种子）：
- **完整 7 组合通过**：10 个必需 hosted 组合全 `passed` + 2 个合法 opt-in skip →
  聚合 `passed`、零错误。
- **任一必需组合缺失** → `AGG_MISSING_REQUIRED`。
- **Windows ARM64 / Linux RISC-V64 合法 opt-in skip**（带 `skip_reason`）→ 仍零错误。
- **跨提交污染** → `AGG_CROSS_COMMIT`。
- **跨 run_id 污染** → `AGG_CROSS_RUN_ID`。
- **重复 `(host,arch,contrib)`** → `AGG_DUPLICATE_COMBO`。
- **假绿**（声明 `passed` 但 build `failed`）→ `RESULT_STATUS_MISMATCH`。
- **错误标真**（`real_hardware_verified=true`）→ `RESULT_REAL_HW_MISLABEL`。
- **哈希篡改**（两条结果共用同一 hash）→ `AGG_HASH_DUPLICATE`。
- **build 通过却缺 hash** → `RESULT_HASH_MISSING`。
- **未知状态** → `RESULT_UNKNOWN_STATUS`。
- **必需组合 cancelled / failed** → `AGG_MISSING_REQUIRED` / `AGG_TASK_FAILED`。
- **过期时间戳**（早于窗口 48h）→ `AGG_STALE_TIMESTAMP`。
- **自托管 skipped 无 `skip_reason`** → `RESULT_SKIP_WITHOUT_REASON`。

---

## 6. 严谨性处理

- **shell / PowerShell 路径**：跨平台二进制路径由 `Locate built binary` 步骤按
  `host` 显式给出（`VirtualWorld.exe` / `.app/Contents/MacOS/VirtualWorld` /
  `VirtualWorld`），避免隐式相对路径在 Windows 上出错。
- **缓存键**：`vw-<host>-<arch>-<contrib>-<CMakeLists hash>`，按平台和 contrib
  配置隔离，避免串味。
- **并发取消**：`concurrency.cancel-in-progress` 仅对 `pull_request` 为真，显式
  `workflow_dispatch`（昂贵/自托管）不被取消；分组键含 `event_name`。
- **超时**：static-checks 10 分钟；build-and-test 90 分钟；conformance 30 分钟。
- **最小权限**：工作流顶层 `permissions: {}`，各 job 仅 `contents: read`。
- **artifact 名称**：`virtualization-ci-matrix-report`，语义明确。
- **失败归属**：`strategy.fail-fast: false` 保证一个平台失败不连累其它；job 名与
  步骤名带 `host/arch/contrib` 上下文，便于定位。
- **artifact 名称防串包**：per-task 结果 artifact 名含
  `run_id + host + arch + contrib`（`vw-result-<run_id>-<host>-<arch>-<contrib>`）；
  聚合报告 artifact 名含 `run_id`（`vw-aggregate-<run_id>`）。跨 run / 跨组合的
  产物不会混进同一次聚合。
- **聚合门禁运行语义**：`aggregate` job `needs: verify` 且 `if: always()`，保证在
  verify 结束后运行（即便有腿失败/取消）；能区分 `failed`/`cancelled`/`skipped`。
- **上传 ≠ 成功**：per-task 状态由步骤结果推导，`emit_result.py` 末步 `if: always()`
  仍产出结果；"上传 artifact 成功"绝不替代构建/测试成功。
- **交叉编译 ≠ 真机**：`cross_compiled` 恒为 `false`，且即便未来加入交叉编译条目，
  `real_hardware_verified` 仍必须 `false`，其状态不得标记为真实硬件通过。

---

## 7. 本地验证（不触发远程 CI）

本阶段只做本地 YAML / 脚本静态验证与自测试，不安装软件、不联网、不推送、不发布：

```bash
# 运行两套自测试
python3 ci/virtualization/generate_matrix_report.py --self-test
python3 ci/virtualization/aggregate_results.py --self-test

# 生成矩阵计划报告（含轻量 YAML 结构检查；无 PyYAML 时自动退化为文本检查）
python3 ci/virtualization/generate_matrix_report.py \
    --yaml .github/workflows/virtualization-ci.yml \
    --out ci-matrix-report.json

# 端到端演练聚合门禁（用一组合成结果目录喂给聚合器）
mkdir -p /tmp/vw-results
# ... 写入若干 virtualworld.ci-result/1 结果 JSON ...
python3 ci/virtualization/aggregate_results.py \
    --dir /tmp/vw-results \
    --out ci-aggregate-report.json \
    --md ci-aggregate-report.md

# 单独验证一条命令是否被白名单允许
python3 ci/virtualization/conformance_guard.py -- VirtualWorld --check --json
python3 ci/virtualization/conformance_guard.py -- VirtualWorld --start vm.json   # 拒绝
```

> 所有"远程行为"（实际构建、CTest、conformance、artifact 上传、真机执行）在本地下
> 仅以**静态检查 + 确定性自测试**覆盖，从未真正执行；`remote_executed` 字段恒为
> `false`。

---

## 8. 允许改动范围与文件清单

本 CI 阶段允许改动 / 新建：
- `.github/workflows/virtualization-ci.yml`（本工作流）
- `ci/virtualization/matrix.py`（矩阵定义 / 校验 / 白名单 / 报告构建）
- `ci/virtualization/generate_matrix_report.py`（矩阵计划报告生成 + 自测试）
- `ci/virtualization/aggregate_results.py`（结果聚合门禁 + 确定性自测试）
- `ci/virtualization/result_schema.py`（单任务结果 schema `virtualworld.ci-result/1` + 校验）
- `ci/virtualization/emit_result.py`（verify 腿末步产出 per-task 结果 JSON）
- `ci/virtualization/conformance_guard.py`（运行期白名单守护）
- `docs/virtualization-ci.md`（本文件）

**不在**范围（不得修改）：`CMakeLists.txt`、`cmake/`、`src/`、`tests/`、其它 docs、
`README.md`、`tools/virtualization-conformance`、任何平台文件、`qemu-source`、
`qemu-master`、所有 `build*`（既有构建目录）。
