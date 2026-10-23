# VirtualWorld virtualization — stability, concurrency and mutation gate

- Generated: `2026-10-05T03:40:00+00:00`
- **Gate status: `passed`**
- read-only black-box; only --list-backends/--check are executed; no VM is started

## Pinned artifact

| Field | Value |
| --- | --- |
| Path | `/Users/liaoboxuan/Desktop/各种学习和娱乐资料/开发/LocalAI/projects/VirtualWorld/build-final/VirtualWorld.app/Contents/MacOS/VirtualWorld` |
| SHA-256 (expected) | `238f603d3d1d1db3c8b574f9617d0632f0f6bcbfbe4c44d923917b6dda60b80f` |
| SHA-256 (observed) | `238f603d3d1d1db3c8b574f9617d0632f0f6bcbfbe4c44d923917b6dda60b80f` |
| Digest match | **True** |
| Size | 1477872 bytes |
| mtime | `2026-10-05T11:13:19` |

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
- Duration ms (pooled): min 597.22, p50 1634.54, p95 2903.41, p99 3893.62, max 4890.07
  - sequential: min 597.22, p50 1154.68, p95 3123.94, max 4890.07
  - concurrent: min 1284.79, p50 1696.2, p95 2797.66, max 3108.27

| Variant | Phase | Runs | Exit codes | p50 ms | p95 ms | Byte-identical | Distinct stdout | Drift | Stderr | Crashes | Timeouts |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `--list-backends x86_64` | sequential | 50 | [0] | 1850.71 | 3146.1 | True | 1 | — | 0 | 0 | 0 |
| `--list-backends x86_64` | concurrent | 50 | [0] | 1614.95 | 1837.88 | True | 1 | — | 0 | 0 | 0 |
| `--list-backends aarch64` | sequential | 50 | [0] | 1226.93 | 2893.69 | True | 1 | — | 0 | 0 | 0 |
| `--list-backends aarch64` | concurrent | 50 | [0] | 1809.76 | 3072.8 | True | 1 | — | 0 | 0 | 0 |
| `--list-backends riscv64` | sequential | 50 | [0] | 1495.97 | 4182.91 | True | 1 | — | 0 | 0 | 0 |
| `--list-backends riscv64` | concurrent | 50 | [0] | 1583.95 | 1859.28 | True | 1 | — | 0 | 0 | 0 |
| `--check x86_64` | sequential | 50 | [0] | 1179.24 | 2352.28 | True | 1 | — | 0 | 0 | 0 |
| `--check x86_64` | concurrent | 50 | [0] | 1634.97 | 1775.37 | True | 1 | — | 0 | 0 | 0 |
| `--check aarch64` | sequential | 50 | [0] | 1034.08 | 2095.79 | True | 1 | — | 0 | 0 | 0 |
| `--check aarch64` | concurrent | 50 | [0] | 1984.5 | 2849.29 | True | 1 | — | 0 | 0 | 0 |
| `--check riscv64` | sequential | 50 | [0] | 700.67 | 1211.91 | True | 1 | — | 0 | 0 | 0 |
| `--check riscv64` | concurrent | 50 | [0] | 1814.66 | 2290.18 | True | 1 | — | 0 | 0 | 0 |

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
