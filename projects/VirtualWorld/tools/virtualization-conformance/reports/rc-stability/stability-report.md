# VirtualWorld virtualization — stability, concurrency and mutation gate

- Generated: `2026-10-06T09:43:31+00:00`
- **Gate status: `passed`**
- read-only black-box; only --list-backends/--check are executed; no VM is started

## Pinned artifact

| Field | Value |
| --- | --- |
| Path | `/Users/liaoboxuan/Desktop/各种学习和娱乐资料/开发/LocalAI/projects/VirtualWorld/build-release-candidate/VirtualWorld.app/Contents/MacOS/VirtualWorld` |
| SHA-256 (expected) | `cee56e76a3ebfaee762447788ca373cbee052790b11adfe9fad64797d7c26432` |
| SHA-256 (observed) | `cee56e76a3ebfaee762447788ca373cbee052790b11adfe9fad64797d7c26432` |
| Digest match | **True** |
| Size | 1478896 bytes |
| mtime | `2026-10-06T16:48:14` |

## Settings

- Commands: `['--list-backends', '--check']`
- Architectures: `['x86_64', 'aarch64', 'riscv64']`
- Rounds per variant: sequential **50**, concurrent **50** at concurrency **8**
- Timeout: 90.0s, mutation seed: `20261005`
- Normalized fields: `[]` — schema v3 output was verified byte-identical across runs, so no field is exempted from comparison; any divergence is reported rather than filtered

## Stability

- Invocations: **600** (300 sequential + 300 concurrent)
- Variants OK: 12 / 12
- Crashes: 0, timeouts: 0, non-empty stderr: 0
- Byte-identical variants: 12 / 12
- Duration ms (pooled): min 367.35, p50 1225.3, p95 2441.94, p99 2655.05, max 2822.73
  - sequential: min 367.35, p50 544.89, p95 1156.64, max 2155.09
  - concurrent: min 774.67, p50 1681.81, p95 2524.14, max 2822.73

| Variant | Phase | Runs | Exit codes | p50 ms | p95 ms | Byte-identical | Distinct stdout | Drift | Stderr | Crashes | Timeouts |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `--list-backends x86_64` | sequential | 50 | [0] | 567.43 | 1244.2 | True | 1 | — | 0 | 0 | 0 |
| `--list-backends x86_64` | concurrent | 50 | [0] | 1370.96 | 1933.54 | True | 1 | — | 0 | 0 | 0 |
| `--list-backends aarch64` | sequential | 50 | [0] | 478.09 | 1072.66 | True | 1 | — | 0 | 0 | 0 |
| `--list-backends aarch64` | concurrent | 50 | [0] | 1402.54 | 1580.77 | True | 1 | — | 0 | 0 | 0 |
| `--list-backends riscv64` | sequential | 50 | [0] | 419.39 | 724.11 | True | 1 | — | 0 | 0 | 0 |
| `--list-backends riscv64` | concurrent | 50 | [0] | 1790.0 | 2704.94 | True | 1 | — | 0 | 0 | 0 |
| `--check x86_64` | sequential | 50 | [0] | 632.56 | 1409.24 | True | 1 | — | 0 | 0 | 0 |
| `--check x86_64` | concurrent | 50 | [0] | 1681.81 | 2178.59 | True | 1 | — | 0 | 0 | 0 |
| `--check aarch64` | sequential | 50 | [0] | 694.8 | 1272.53 | True | 1 | — | 0 | 0 | 0 |
| `--check aarch64` | concurrent | 50 | [0] | 1953.45 | 2655.05 | True | 1 | — | 0 | 0 | 0 |
| `--check riscv64` | sequential | 50 | [0] | 582.7 | 1137.63 | True | 1 | — | 0 | 0 | 0 |
| `--check riscv64` | concurrent | 50 | [0] | 2168.73 | 2516.01 | True | 1 | — | 0 | 0 | 0 |

## Mutation suite

- Seed: `20261005` (deterministic; same seed → same suite)
- Mutations: **73**, detectors fired: **73**, gaps: **0**
- Failing samples written to `reports/stability-failures/`

| Category | Detected | Total |
| --- | --- | --- |
| cross-arch | 2 | 2 |
| duplicate-id | 1 | 1 |
| empty-reason | 1 | 1 |
| harmony-honesty | 2 | 2 |
| missing-field | 8 | 8 |
| randomized | 42 | 42 |
| schema | 1 | 1 |
| selection-inconsistency | 4 | 4 |
| selection-ordering | 1 | 1 |
| tcg-ordering | 2 | 2 |
| unknown-status-class | 1 | 1 |
| unknown-tier | 2 | 2 |
| verified-type | 3 | 3 |
| wrong-type | 3 | 3 |

## Failures and attribution

None: every variant was stable and every detector fired as expected.

## Not verified

- No guest was started: only --list-backends and --check were executed.
- Stability is demonstrated only on this macOS/Apple Silicon host; Windows, Linux and HarmonyOS hosts remain covered by synthetic fixtures, not real runs.
- Byte-level determinism across different machines, locales or Qt builds is untested.
- The artifact is pinned by SHA-256; a rebuild invalidates this report by design.
