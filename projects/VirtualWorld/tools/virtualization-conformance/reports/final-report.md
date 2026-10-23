# VirtualWorld virtualization — final certification report

- Generated: `2026-10-05T03:20:23+00:00`
- Report version: `1.0.0`
- **Certification status: `conformant`**
- Method: read-only black-box. Only `--list-backends --json` and `--check --json` were executed. No guest was started, nothing was built, and no `--allow-code` was used.

## Certified artifact

| Field | Value |
| --- | --- |
| Build label | `build-final (Release, contrib backends ON)` |
| Binary | `/Users/liaoboxuan/Desktop/各种学习和娱乐资料/开发/LocalAI/projects/VirtualWorld/build-final/VirtualWorld.app/Contents/MacOS/VirtualWorld` |
| SHA-256 | `238f603d3d1d1db3c8b574f9617d0632f0f6bcbfbe4c44d923917b6dda60b80f` |
| Size | 1477872 bytes |
| mtime | `2026-10-05T11:13:19` |
| JSON schema | `virtualworld.virtualization/3` |
| Host | `macos` / `aarch64` (macos-apple-silicon) |
| Compiled backends | `{'harmony_backend': True, 'linux_kvm_backend': True, 'macos_backend': True, 'qemu_fallback': True, 'windows_backend': True}` |
| Registered backend ids | `['macos-virtualization', 'windows-hypervisor', 'qemu-hardware', 'harmony-virtualization', 'linux-kvm', 'qemu-tcg']` |
| Selection order observed | `['macos-virtualization', 'windows-hypervisor', 'qemu-hardware', 'harmony-virtualization', 'linux-kvm', 'qemu-tcg']` |

### Build integrity (artifact vs sources)

- Newest product input: `src/VirtualizationEngine.cpp` (2026-10-05T11:08:57); skew vs binary: `-262.2` s
- Artifact matches newest product input: **True**
- artifact was linked from the newest product inputs

### Product self-tests (informational, read-only)

- Build dir: `build-final`, ctest log `2026-10-05T11:15:20`
- Passed: **7**, failed: **0**
  - `VirtualWorldTranslatorTests`: passed
  - `VirtualWorldVirtualizationEngineTests`: passed (VirtualizationEngineTests: 398 checks, 0 failures)
  - `VirtualWorldVirtualizationIntegrationTests`: passed (VirtualizationIntegrationTests: 590 checks, 0 failures)
  - `VirtualWorldMacVirtualizationEngineTests`: passed
  - `VirtualWorldHarmonyVirtualizationEngineTests`: passed
  - `VirtualWorldLinuxKvmEngineTests`: passed
  - `VirtualWorldWindowsHypervisorEngineTests`: passed

## Self-tests

- `python3 virtualization-conformance/test_run_conformance.py` → **OK**, 68 cases, exit `0`

## Live checks against the certified binary

`<binary>` below is the artifact path recorded above; the exact argv is in the JSON report (`live.targets[].command`).

| # | Command | Exit | Errors | Error codes |
| --- | --- | --- | --- | --- |
| 1 | `<binary> --list-backends x86_64 --json` | 0 | 0 | — |
| 2 | `<binary> --check x86_64 --json` | 0 | 0 | — |
| 3 | `<binary> --list-backends aarch64 --json` | 0 | 0 | — |
| 4 | `<binary> --check aarch64 --json` | 0 | 0 | — |
| 5 | `<binary> --list-backends riscv64 --json` | 0 | 0 | — |
| 6 | `<binary> --check riscv64 --json` | 0 | 0 | — |

Totals: 6 targets, 0 error(s), 0 warning(s).

## Invariants

| Invariant | Status | Description |
| --- | --- | --- |
| `schema` | PASS | JSON schema is virtualworld.virtualization/<n> with n >= 1 |
| `exit-codes` | PASS | documented exit codes (--list-backends=0, --check in {0,1}) |
| `single-document` | PASS | stdout is exactly one parseable JSON document |
| `backend-id-unique` | PASS | every backend id is non-empty and unique |
| `tier-legal` | PASS | every tier is one of native/hardware/emulation |
| `considered-reasons` | PASS | every candidate carries a non-empty reason |
| `considered-complete` | PASS | selection.considered covers exactly the registered backends |
| `considered-order` | PASS | candidates are ordered native -> hardware -> emulation |
| `best-eligible` | PASS | under preference=auto the best eligible candidate is selected |
| `tcg-last` | PASS | qemu-tcg is present and always the last candidate |
| `tcg-cross-arch` | PASS | cross-architecture guests are served by qemu-tcg |
| `harmony-not-native` | PASS | harmony-virtualization never claims native tier or native execution |
| `kvm-not-native` | PASS | linux-kvm never claims native tier or native execution |
| `whpx-native-tier` | PASS | windows-hypervisor reports the native tier |
| `matrix-complete` | PASS | support_matrix rows are complete and verified is a real boolean |
| `matrix-cross-arch` | PASS | no matrix row advertises acceleration across architectures |
| `legacy-compatibility` | PASS | legacy backend=qemu default and --check text/JSON agreement |

## Defects (report only — not fixed by this tool)

No defects were found on the certified build.

## Fixtures: real captures vs synthetic

- Manifest: `virtualization-conformance/fixtures/index.json`
- Real captures (`synthetic: false`): **2** — these are the only documents that are measurements of a real host.
- Synthetic scenarios (`synthetic: true`): **7** — expectations authored for hosts this machine does not have.
- Negative fixtures (`expect_fail`): **3** — documents that reproduce defects; the checker must reject them.
- Expectations met: 12 / 12

| Fixture | Kind | Expected outcome | Met |
| --- | --- | --- | --- |
| fixture:macOS Apple Silicon, aarch64 guest | real capture | must pass | yes |
| fixture:macOS Apple Silicon, x86_64 guest (cross-architecture) | real capture | must pass | yes |
| fixture:Intel Mac, x86_64 guest | synthetic | must pass | yes |
| fixture:Windows x86_64 host, x86_64 guest | synthetic | must pass | yes |
| fixture:Windows ARM64 host, aarch64 guest | synthetic | must pass | yes |
| fixture:Linux x86_64 host, x86_64 guest | synthetic | must pass | yes |
| fixture:Linux ARM64 host, aarch64 guest | synthetic | must pass | yes |
| fixture:Linux RISC-V64 host, riscv64 guest | synthetic | must pass | yes |
| fixture:HarmonyOS/OpenHarmony ARM64 host, aarch64 guest | synthetic | must pass | yes |
| fixture:HISTORICAL REGRESSION: WHPX reported tier 'emulation', pushing qemu-tcg out of last place | negative | must fail | yes |
| fixture:KNOWN DEFECT: HarmonyOS QEMU compatibility backend claims native | negative | must fail | yes |
| fixture:Malformed JSON must be reported, not crash the tool | negative | must fail | yes |

## Legacy compatibility (`backend=qemu`, `--check`)

'backend=qemu' is the legacy default: resolveBackendPreference() falls back to it, and parseBackendPreference() maps it to the QemuOnly policy, which excludes native backends by policy rather than by failure.

| Check | Result | Detail |
| --- | --- | --- |
| legacy default (no --backend, no VIRTUALWORLD_BACKEND) | PASS | preference=qemu (legacy QemuOnly); 2 backend(s) excluded by policy, 0 without a reason |
| explicit qemu-tcg | PASS | explicit request resolved to 'qemu-tcg' |
| explicit unknown id | PASS | unknown id fell back to 'qemu-tcg' (ok=True) |
| `--check x86_64` text vs --json | PASS | text=0, json=0, selected=qemu-tcg |
| `--check aarch64` text vs --json | PASS | text=0, json=0, selected=qemu-hardware |
| `--check riscv64` text vs --json | PASS | text=0, json=0, selected=qemu-tcg |

## Support matrix (as reported by the certified binary)

| host platform | host arch | guest arch | backend | tier | status | verified |
| --- | --- | --- | --- | --- | --- | --- |
| windows | x86_64 | x86_64 | windows-hypervisor | native | native | False |
| windows | x86_64 | x86_64 | qemu-hardware | hardware | hardware | False |
| windows | x86_64 | x86_64 | qemu-tcg | emulation | emulation | False |
| windows | x86_64 | aarch64 | qemu-tcg | emulation | emulation | False |
| windows | x86_64 | riscv64 | qemu-tcg | emulation | emulation | False |
| windows | aarch64 | x86_64 | qemu-tcg | emulation | emulation | False |
| windows | aarch64 | aarch64 | windows-hypervisor | native | native | False |
| windows | aarch64 | aarch64 | qemu-hardware | hardware | hardware | False |
| windows | aarch64 | aarch64 | qemu-tcg | emulation | emulation | False |
| windows | aarch64 | riscv64 | qemu-tcg | emulation | emulation | False |
| macos | x86_64 | x86_64 | macos-virtualization | native | native | False |
| macos | x86_64 | x86_64 | qemu-hardware | hardware | hardware | False |
| macos | x86_64 | x86_64 | qemu-tcg | emulation | emulation | False |
| macos | x86_64 | aarch64 | qemu-tcg | emulation | emulation | False |
| macos | x86_64 | riscv64 | qemu-tcg | emulation | emulation | False |
| macos | aarch64 | x86_64 | qemu-tcg | emulation | emulation | False |
| macos | aarch64 | aarch64 | macos-virtualization | native | native | False |
| macos | aarch64 | aarch64 | qemu-hardware | hardware | hardware | False |
| macos | aarch64 | aarch64 | qemu-tcg | emulation | emulation | False |
| macos | aarch64 | riscv64 | qemu-tcg | emulation | emulation | False |
| linux | x86_64 | x86_64 | linux-kvm | hardware | compat | False |
| linux | x86_64 | x86_64 | qemu-tcg | emulation | emulation | False |
| linux | x86_64 | aarch64 | qemu-tcg | emulation | emulation | False |
| linux | x86_64 | riscv64 | qemu-tcg | emulation | emulation | False |
| linux | aarch64 | x86_64 | qemu-tcg | emulation | emulation | False |
| linux | aarch64 | aarch64 | linux-kvm | hardware | compat | False |
| linux | aarch64 | aarch64 | qemu-tcg | emulation | emulation | False |
| linux | aarch64 | riscv64 | qemu-tcg | emulation | emulation | False |
| linux | riscv64 | x86_64 | qemu-tcg | emulation | emulation | False |
| linux | riscv64 | aarch64 | qemu-tcg | emulation | emulation | False |
| linux | riscv64 | riscv64 | linux-kvm | hardware | compat | False |
| linux | riscv64 | riscv64 | qemu-tcg | emulation | emulation | False |
| harmonyos | x86_64 | x86_64 | harmony-virtualization | hardware | compat | False |
| harmonyos | x86_64 | x86_64 | qemu-tcg | emulation | emulation | False |
| harmonyos | x86_64 | aarch64 | qemu-tcg | emulation | emulation | False |
| harmonyos | x86_64 | riscv64 | qemu-tcg | emulation | emulation | False |
| harmonyos | aarch64 | x86_64 | qemu-tcg | emulation | emulation | False |
| harmonyos | aarch64 | aarch64 | harmony-virtualization | hardware | compat | False |
| harmonyos | aarch64 | aarch64 | qemu-tcg | emulation | emulation | False |
| harmonyos | aarch64 | riscv64 | qemu-tcg | emulation | emulation | False |
| harmonyos | riscv64 | x86_64 | qemu-tcg | emulation | emulation | False |
| harmonyos | riscv64 | aarch64 | qemu-tcg | emulation | emulation | False |
| harmonyos | riscv64 | riscv64 | harmony-virtualization | hardware | compat | False |
| harmonyos | riscv64 | riscv64 | qemu-tcg | emulation | emulation | False |

## Not verified

- No guest was started by this certification: only --list-backends and --check were run.
- Windows, Linux and HarmonyOS hosts were not exercised on real hardware; their documents here are synthetic fixtures labelled synthetic=true.
- Intel Mac behaviour is covered by a synthetic fixture only; this machine is Apple Silicon.
- QEMU/accelerator behaviour beyond capability reporting (an actual accelerated boot) is untested.
- The product binary was produced by another workstream; this tool neither built nor modified it.
