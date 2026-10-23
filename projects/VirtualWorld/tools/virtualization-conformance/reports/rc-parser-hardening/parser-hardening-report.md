# VirtualWorld conformance tools — parser / input robustness hardening

- Generated: `2026-10-06T09:42:57+00:00`
- **Status: `clean`**
- pure parsing/validation entry points only; no product binary executed, no VM command issued; exemptions are never used to convert a failure into a pass

## Run

- Seed: `20261005` (same seed → identical sample set)
- Sample manifest SHA-256: `c7259e901ee3437f0f05d40fbc758f455fcbc190d89eefba9c4f02f086d38916`
- Samples: **5000** across 14 entry points
- Elapsed: 11.69s
- Limits: ≤262144 bytes/sample, depth ≤64, arrays ≤256, batch 40 at 120.0s
- Peak payload: 262158 bytes

## Entry points under test

- `conformance.arch`
- `conformance.check`
- `conformance.strictbool`
- `json.parse`
- `release.classify`
- `release.escapesroot`
- `release.pathsafe`
- `release.resolvebundle`
- `release.verify`
- `report.defects`
- `report.invariants`
- `report.render`
- `stability.canonical`
- `stability.semantics`

## Outcomes

| Outcome | Count |
| --- | --- |
| `accepted` | 1943 |
| `out-of-contract-exception` | 379 |
| `rejected` | 2678 |

## Category coverage

| Category | Samples | Gaps |
| --- | --- | --- |
| `conformance-backend-id-duplicate` | 1 | 0 |
| `conformance-backend-id-empty` | 1 | 0 |
| `conformance-backend-tier-invalid` | 1 | 0 |
| `conformance-build-not-bool` | 1 | 0 |
| `conformance-deep-unknown` | 1 | 0 |
| `conformance-duplicate-array` | 2 | 0 |
| `conformance-execution-tier-invalid` | 1 | 0 |
| `conformance-guest-arch-cross-arch` | 1 | 0 |
| `conformance-guest-arch-cross-arch-real` | 1 | 0 |
| `conformance-host-platform-unknown` | 1 | 0 |
| `conformance-long-string` | 32 | 0 |
| `conformance-missing-section` | 8 | 0 |
| `conformance-not-object` | 6 | 0 |
| `conformance-reason-empty` | 1 | 0 |
| `conformance-schema-unknown` | 1 | 0 |
| `conformance-selection-mismatch` | 1 | 0 |
| `conformance-selection-tier-invalid` | 1 | 0 |
| `conformance-status-class-unknown` | 1 | 0 |
| `conformance-type-swap` | 1080 | 0 |
| `conformance-unknown-field` | 3 | 0 |
| `conformance-verified-not-bool` | 1 | 0 |
| `json-control-char` | 14 | 0 |
| `json-deep-nesting` | 10 | 0 |
| `json-duplicate-array` | 1 | 0 |
| `json-duplicate-key` | 3 | 0 |
| `json-empty` | 7 | 0 |
| `json-fuzz` | 120 | 0 |
| `json-long-array` | 4 | 0 |
| `json-long-string` | 4 | 0 |
| `json-non-object` | 7 | 0 |
| `json-nonstandard` | 5 | 0 |
| `json-number-edge` | 19 | 0 |
| `json-structural-fuzz` | 120 | 0 |
| `json-trailing` | 4 | 0 |
| `json-truncated` | 12 | 0 |
| `json-unicode` | 30 | 0 |
| `json-unknown-field` | 1 | 0 |
| `release-classify` | 1 | 0 |
| `release-contrib-drift` | 1 | 0 |
| `release-contrib-drift-backend` | 1 | 0 |
| `release-count-negative` | 1 | 0 |
| `release-count-type` | 1 | 0 |
| `release-deep-unknown` | 1 | 0 |
| `release-duplicate-array` | 1 | 0 |
| `release-gate-failure-count` | 1 | 0 |
| `release-gate-status-unknown` | 1 | 0 |
| `release-gate-still-passed` | 1 | 0 |
| `release-hash` | 22 | 0 |
| `release-hash-format` | 1 | 0 |
| `release-honesty-realhardware-true` | 1 | 0 |
| `release-honesty-verified-true` | 1 | 0 |
| `release-host-arch-empty` | 1 | 0 |
| `release-host-os-unknown` | 1 | 0 |
| `release-long-string` | 10 | 0 |
| `release-missing-section` | 13 | 0 |
| `release-not-object` | 6 | 0 |
| `release-path` | 52 | 0 |
| `release-path-absolute` | 1 | 0 |
| `release-path-traversal` | 1 | 0 |
| `release-schema-unknown` | 1 | 0 |
| `release-size-wrong` | 1 | 0 |
| `release-timestamp` | 13 | 0 |
| `release-timestamp-impossible` | 1 | 0 |
| `release-timestamp-not-iso` | 1 | 0 |
| `release-type-swap` | 540 | 0 |
| `release-unknown-field` | 2 | 0 |
| `report-findings` | 16 | 0 |
| `report-incontract` | 6 | 0 |
| `scalar-arch` | 58 | 0 |
| `scalar-bool` | 58 | 0 |
| `scalar-canonical` | 58 | 0 |
| `scalar-escapesroot` | 58 | 0 |
| `scalar-pathsafe` | 58 | 0 |
| `structure-report-outofcontract` | 396 | 0 |
| `structure-resolvebundle` | 132 | 0 |
| `structure-semantics` | 132 | 0 |
| `topup-conformance` | 446 | 0 |
| `topup-json-fuzz` | 463 | 0 |
| `topup-release` | 478 | 0 |
| `topup-structure` | 455 | 0 |

## Error-code stability

- Distinct codes observed: 84
- Scanned vocabulary size: 135
- Codes outside the vocabulary: none
- Malformed codes: none

| Code | Occurrences |
| --- | --- |
| `array-item-not-string` | 3 |
| `backend-flag-drift` | 12 |
| `backend-flag-not-bool` | 78 |
| `backend-id-duplicate` | 2 |
| `backend-id-empty` | 3 |
| `backend-id-missing` | 16 |
| `backend-issue-not-object` | 3 |
| `backend-issues-invalid` | 15 |
| `backend-not-object` | 33 |
| `backend-tier-invalid` | 8 |
| `backends-empty` | 1 |
| `backends-key-missing` | 8 |
| `build-key-missing` | 15 |
| `build-value-not-bool` | 104 |
| `considered-empty` | 1 |
| `considered-id-missing` | 14 |
| `considered-missing-backend` | 48 |
| `considered-none-selected` | 50 |
| `considered-not-array` | 21 |
| `considered-not-object` | 21 |
| `considered-tier-invalid` | 7 |
| `considered-unknown-backend` | 59 |
| `count-not-int` | 106 |
| `cross-arch-not-tcg` | 25 |
| `document-not-object` | 12 |
| `empty-field` | 77 |
| `execution-tier-invalid` | 43 |
| `fallback-from-missing` | 1 |
| `field-not-array` | 15 |
| `firmware-count-mismatch` | 5 |
| `gate-check-not-object` | 3 |
| `gate-failure-count-mismatch` | 6 |
| `gate-failure-count-not-int` | 24 |
| `gate-false-green` | 6 |
| `gate-status-invalid` | 28 |
| `generated-at-invalid` | 38 |
| `harness-candidate-ambiguous` | 1 |
| `host-arch-differs-from-runner` | 10 |
| `host-arch-empty` | 2 |
| `host-arch-invalid` | 4 |
| `host-os-invalid` | 11 |
| `host-platform-unknown` | 9 |
| `json-unparseable` | 144 |
| `main-program-missing` | 30 |
| `main-program-path-absolute-ok` | 1061 |
| `main-program-path-escape` | 4 |
| `main-program-path-unsafe` | 6 |
| `main-program-sha256-format` | 41 |
| `main-program-size-mismatch` | 5 |
| `matrix-cross-arch-acceleration` | 11 |
| `matrix-empty` | 2 |
| `matrix-platform-unknown` | 8 |
| `matrix-row-not-object` | 34 |
| `matrix-tier-invalid` | 7 |
| `matrix-verified-not-bool` | 17 |
| `missing-field` | 290 |
| `native-execution-tier-mismatch` | 8 |
| `platform-native-tier-not-native` | 8 |
| `qemu-entry-not-object` | 28 |
| `qemu-fallback-not-built` | 1 |

## Harness incidents (crash / timeout bisection)

None: no batch timed out or died.

## Out-of-contract exceptions (informational)

- Count: **379** on targets ['report.defects', 'report.invariants', 'report.render']
- Internal consumers (`make_final_report` renderers) were fed arbitrary JSON, which is outside their contract: they only ever receive report structures this toolchain builds. Exceptions there are recorded as informational; a structurally in-contract payload that raises is still counted as a gap, and no untrusted-input validator may appear here.

## Gaps (minimized samples)

None: every sample either parsed/validated cleanly or was rejected with a stable code. No unhandled exception, hang, crash or silent acceptance.

## Attribution

No outstanding gaps to attribute.

## Defects found by this harness and fixed in the tools

7 defect(s) were found by this hardening run and fixed inside `tools/virtualization-conformance/`. Each has a regression test, so the same input cannot silently start crashing again.

| Module | Site | Symptom | Fix | Regression test |
| --- | --- | --- | --- | --- |
| `run_conformance.py` | `_check_selection_policy` | AttributeError: 'bool' object has no attribute 'get' when host was a truthy non-mapping (e.g. JSON true) | added _as_dict() and used it for the host lookup | `TestFixedDefects.test_as_dict_guards_truthy_non_mappings` |
| `verify_release_manifest.py` | `resolve_bundle_root` | AttributeError: 'bool' object has no attribute 'get' when main_program was a truthy non-mapping | added _as_dict() and applied it at all five (x or {}).get(...) sites | `TestFixedDefects.test_release_resolve_bundle_root_survives_non_mapping_main_program` |
| `verify_release_manifest.py` | `escapes_root` | ValueError: lstat: embedded null character in path | fails closed: an unresolvable candidate is reported as escaping rather than raising | `TestFixedDefects.test_escapes_root_fails_closed_on_unresolvable_path` |
| `verify_release_manifest.py` | `_check_qemu` | ValueError: no path specified | require a non-empty main path before relativising it | `TestFixedDefects.test_release_verify_survives_empty_main_program_path` |
| `verify_release_manifest.py` | `_check_qemu` | ValueError: lstat: embedded null character in path | added _safe_realpath() and used it in the check and the message | `TestFixedDefects.test_release_verify_survives_nul_in_qemu_path` |
| `run_stability_gate.py` | `project_semantics` | TypeError: 'NoneType' object is not iterable | added _as_list()/_as_dict() and used them for backends/selection/matrix | `TestFixedDefects.test_project_semantics_survives_null_containers` |
| `run_conformance.py` | `_check_guest_arch` | guest-arch-invalid was unreachable: unknown-but-non-empty architectures are normalised, not rejected | documented as deliberate -- rejecting unknown architectures would fail the bundled loongarch64 target; bogus acceleration claims are still caught by cross-arch-not-tcg | `TestFixedDefects.test_guest_arch_invalid_is_documented_dead_branch` |

## Not verified

- Only pure parsing/validation entry points are exercised; live binary runs and VM behaviour are out of scope.
- Exhaustive input-space coverage is impossible; coverage is bounded by the generator and its documented resource limits.
- Resource limits are enforced per sample; aggregate memory pressure across a whole batch is not measured.
