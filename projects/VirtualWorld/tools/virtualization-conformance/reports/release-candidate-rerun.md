# VirtualWorld release candidate — final independent acceptance

- Generated: `2026-10-06T09:42:27+00:00`
- Report schema: `virtualworld.release-candidate-final/1`
- **Verdict: `certified`**
- independent black-box acceptance; no build, no rebuild, no product modification; only --check/--list-backends/--json are issued to the candidate and no VM is started

## Certified candidate

| Field | Value |
| --- | --- |
| Binary | `/Users/liaoboxuan/Desktop/各种学习和娱乐资料/开发/LocalAI/projects/VirtualWorld/build-release-candidate/VirtualWorld.app/Contents/MacOS/VirtualWorld` |
| Binary SHA-256 (marker) | `cee56e76a3ebfaee762447788ca373cbee052790b11adfe9fad64797d7c26432` |
| Binary SHA-256 (recomputed) | `cee56e76a3ebfaee762447788ca373cbee052790b11adfe9fad64797d7c26432` |
| Size | 1478896 bytes |
| mtime | `2026-10-06T16:48:14` |
| release-ready marker SHA-256 | `cffff4a42da2c2cebc26ef59a472581a8a598ef5f864c67ca90e57d05b85da72` |
| Platform freeze marker | `/Users/liaoboxuan/Desktop/各种学习和娱乐资料/开发/LocalAI/projects/VirtualWorld/build-mac-harmony-chaos/.platform-freeze-ready` |
| Freeze marker SHA-256 | `199ba93c8ab2d90b8828a1ae5ad52d3eef998870b34ceb328e3b4965818ca1b1` |
| contrib backends | True |

## Attestations

- `real_hardware_verified`: **False** (a Debian UTM guest is virtual hardware, not real hardware)
- `vm_started`: **False**
- This tool launched no VM: **True**
- No Debian VM was exercised; recorded reason: 'Safe, deterministic automation (start -> capability probe -> guaranteed graceful stop) is NOT provable on this host: utmctl is absent (no CLI start/stop), AppleScript stop is not provably graceful, and pkill/killall on QEMU is strictly forbidden (no guaranteed cleanup). Running state of the specific VM also cannot be reliably determined without utmctl. Per the convergence rule, the smoke is intentionally skipped rather than faked. The software release is still declared ready; only real-hardware verification is withheld.'
- VM-launching flags reachable: **False** (allowed flags: `['--check', '--help', '--json', '--list-backends', '--version', '-h']`)

### Candidate invocations issued by this tool

| Flags | Exit | ms |
| --- | --- | --- |
| `['--help']` | 0 | 98.8 |

## Verifier battery

| Verifier | Exit | OK | Detail |
| --- | --- | --- | --- |
| `self_tests` | 0 | True | 317 tests (baseline 214) |
| `parser_hardening` | 0 | True | 5000 samples |
| `conformance` | 0 | True | arches ['x86_64', 'aarch64', 'riscv64'] |
| `release_manifests` | 0 | True | roots ['build-release-candidate', 'build-release-candidate-nocontrib'] |
| `stability` | 0 | True | 50 rounds/phase, 600 invocations |

### conformance

```
rpt": ""
    },
    {
      "source": "VirtualWorld --list-backends riscv64",
      "command": [
        "/Users/liaoboxuan/Desktop/各种学习和娱乐资料/开发/LocalAI/projects/VirtualWorld/build-release-candidate/VirtualWorld.app/Contents/MacOS/VirtualWorld",
        "--list-backends",
        "riscv64",
        "--json"
      ],
      "exit_code": 0,
      "ok": true,
      "error_count": 0,
      "warning_count": 0,
      "findings": [],
      "stderr_excerpt": ""
    },
    {
      "source": "VirtualWorld --check riscv64",
      "command": [
        "/Users/liaoboxuan/Desktop/各种学习和娱乐资料/开发/LocalAI/projects/VirtualWorld/build-release-candidate/VirtualWorld.app/Contents/MacOS/VirtualWorld",
        "--check",
        "riscv64",
        "--json"
      ],
      "exit_code": 0,
      "ok": true,
      "error_count": 0,
      "warning_count": 0,
      "findings": [],
      "stderr_excerpt": ""
    }
  ]
}
```

## Findings

| Severity | Code | Message | Path |
| --- | --- | --- | --- |
| warning | `source-aggregate-formula-unreproduced` | the source file set matches exactly, but the aggregate digest formula belongs to the release gate and could not be independently reproduced, so the digest value itself is not certified by this run | `$.aggregate_source_sha256.value` |

## Located marker facts

| Fact | JSON path | Value |
| --- | --- | --- |
| `candidate_path` | `$.candidate_builds[0].build_dir` | build-release-candidate |
| `binary_path` | `$.candidate_builds[0].binary.path` | build-release-candidate/VirtualWorld.app/Contents/MacOS/VirtualWorld |
| `binary_sha256` | `$.candidate_builds[0].binary.sha256` | cee56e76a3ebfaee762447788ca373cbee052790b11adfe9fad64797d7c26432 |
| `freeze_marker_path` | `$.platform_freeze_marker.path` | build-mac-harmony-chaos/.platform-freeze-ready |
| `freeze_marker_sha256` | `$.platform_freeze_marker.file_sha256` | 199ba93c8ab2d90b8828a1ae5ad52d3eef998870b34ceb328e3b4965818ca1b1 |
| `source_aggregate` | `$.aggregate_source_sha256.value` | a6a1b6f12dbe075f5f0d28af611c58e711fe08c2a821228e99c4f5bde67b153c |
| `sources` | `(not found)` | None |
| `aggregate_file_count` | `$.aggregate_source_sha256.file_count` | 72 |
| `aggregate_scope` | `$.aggregate_source_sha256.scope` | CMakeLists.txt + src/ + tests/ + cmake/ + assets/VirtualWorld.icns (full build-input fingerprint; identical source tree ... |
| `contrib_backends` | `$.candidate_builds[0].packaging_manifest.contrib_backends` | True |
| `real_hardware_verified` | `$.candidate_builds[0].packaging_manifest.real_hardware_verified` | False |
| `vm_started` | `$.vm_real_hardware_smoke.vm_started` | False |
| `skip_reason` | `$.vm_real_hardware_smoke.skip_reason` | Safe, deterministic automation (start -> capability probe -> guaranteed graceful stop) is NOT provable on this host: utm... |
| `guest_os` | `$.vm_real_hardware_smoke.guest_os` | None |
| `vm_name` | `$.vm_real_hardware_smoke.utm_vm_id` | None |
| `pre_start_state` | `(not found)` | None |
| `image_path` | `$.vm_real_hardware_smoke.target_identity_recorded_readonly.disk_image` | /Volumes/MacD/UTM/Linux.utm/Data/B90D4890-732D-4A27-A02A-431D9523E7EC.qcow2 |
| `image_sha256` | `(not found)` | None |
| `started_at` | `(not found)` | None |
| `ready_at` | `(not found)` | None |
| `stopped_at` | `(not found)` | None |
| `evidence_paths` | `(not found)` | None |
| `utm_config` | `(not found)` | None |
| `instance_scope` | `(not found)` | None |
| `generated_at` | `$.generated_at_utc` | 2026-10-06T09:29:16Z |
| `gate_status` | `$.candidate_builds[0].ctest.tests[0].result` | passed |
| `supersedes_error` | `$.supersedes` | build-release-candidate/release-blocked.2026-10-06T02-50Z.json |
| `freeze_utc` | `$.platform_freeze_marker.freeze_utc` | 2026-10-06T08:15:02Z |

## Not verified

- This tool started no VM: only --check/--list-backends/--json were issued to the candidate. VM evidence is WorkBuddy AI's and is certified as recorded, not reproduced here.
- real_hardware_verified is false in the marker and is not claimed here; a Debian UTM guest is virtual hardware, not real hardware.
- Windows, Linux and HarmonyOS hosts are not exercised on real hardware; any such evidence is absent rather than inferred.
- Cross-platform packaging beyond the macOS candidates present on this host is not covered by this acceptance run.
