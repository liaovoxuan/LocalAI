# VirtualWorld release manifest — independent certification

- Generated: `2026-10-06T09:43:14+00:00`
- **Certification status: `certified`**
- independent black-box re-derivation: hashes, sizes, counts, path safety and backend flags are recomputed from the bundle and the same build's executable

## Discovery

- Mode: **auto-discovered** (never guesses when ambiguous)
- contrib ON: `['/Users/liaoboxuan/Desktop/各种学习和娱乐资料/开发/LocalAI/projects/VirtualWorld/build-release-candidate/virtualization-release-manifest.json']`
- contrib OFF: `['/Users/liaoboxuan/Desktop/各种学习和娱乐资料/开发/LocalAI/projects/VirtualWorld/build-release-candidate-nocontrib/virtualization-release-manifest.json']`

## Manifest summaries

| Role | Manifest | Contrib | Backends | QEMU entries | Firmware | Support | Gate | Hashes recomputed | Errors |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| contrib ON | `build-release-candidate/virtualization-release-manifest.json` | True | `{'macos': True, 'windows': True, 'linux_kvm': True, 'harmony': True}` | 10 | 55 | 6 | passed | 11 | 0 |
| contrib OFF | `build-release-candidate-nocontrib/virtualization-release-manifest.json` | False | `{'macos': True, 'windows': True, 'linux_kvm': False, 'harmony': False}` | 10 | 55 | 6 | passed | 11 | 0 |

### contrib ON

- Bundle root: `build-release-candidate/VirtualWorld.app` (resolved from VirtualWorld.app)
- Bytes re-hashed: 216,667,072
- CMakeCache cross-check: `checked` (cache=ON, manifest=True)
- Executable cross-check: `checked` — observed `{'macos': True, 'windows': True, 'linux_kvm': True, 'harmony': True}` vs manifest `{'macos': True, 'windows': True, 'linux_kvm': True, 'harmony': True}`
- No findings.

### contrib OFF

- Bundle root: `build-release-candidate-nocontrib/VirtualWorld.app` (resolved from VirtualWorld.app)
- Bytes re-hashed: 216,567,920
- CMakeCache cross-check: `checked` (cache=OFF, manifest=False)
- Executable cross-check: `checked` — observed `{'macos': True, 'windows': True, 'linux_kvm': False, 'harmony': False}` vs manifest `{'macos': True, 'windows': True, 'linux_kvm': False, 'harmony': False}`
- No findings.

## Mutation suite

- Seed: `20261005` (deterministic)
- Mutations: **47**, detectors fired: **47**, gaps: **0**
- Failing samples: `reports/release-manifest-mutations/`

| Category | Detected | Total |
| --- | --- | --- |
| backend-drift | 3 | 3 |
| counts | 3 | 3 |
| gate | 4 | 4 |
| hash-size | 6 | 6 |
| missing-field | 5 | 5 |
| missing-qemu | 1 | 1 |
| path-safety | 6 | 6 |
| randomized | 8 | 8 |
| schema | 1 | 1 |
| time-host | 5 | 5 |
| verified-flag | 5 | 5 |

## Failures and attribution

None: both manifests re-derived cleanly and every detector fired.

## Not verified

- No VM command was run: only --list-backends --json was executed, and never --start.
- Real-hardware verification is explicitly false in both manifests and is not claimed here.
- Windows and Linux packaging is not covered: only the macOS bundles present on this host were certified.
- Code signature validity beyond the recorded ad-hoc gate result was not re-verified.
- Manifests are certified as documents plus their referenced local files; a manifest produced on another machine cannot be compared against this host's filesystem.
