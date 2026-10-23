# VirtualWorld virtualization conformance

An **independent, read-only black-box** checker for an already-built VirtualWorld
binary. It answers one question: *does the running binary still honour the
documented virtualization-backend contract?*

It never imports project code, never inspects `src/`, never builds anything and
**never starts a guest**. It only runs the two documented reporting commands:

```
<binary> --list-backends [arch] --json
<binary> --check       [arch] --json
```

Both are pure reporting: enumerate the registered backends, probe the host, print
one JSON document. They are also the only commands this tool is willing to
execute — `run_binary()` raises on anything else, so a typo cannot turn a
conformance run into a VM launch.

## Quick start

```bash
# Against a built binary (runs 4 commands: both commands x aarch64 and x86_64)
python3 tools/virtualization-conformance/run_conformance.py \
    --binary build/VirtualWorld.app/Contents/MacOS/VirtualWorld \
    --output conformance-report.json

# Offline: replay captured + synthetic documents, no binary needed
python3 tools/virtualization-conformance/run_conformance.py \
    --fixtures tools/virtualization-conformance/fixtures

# Exercise the auto-selection invariants (native > hardware > TCG)
python3 tools/virtualization-conformance/run_conformance.py \
    --binary <binary> --backend-env VIRTUALWORLD_BACKEND=auto

# Self-tests (58 cases, standard library only, no network)
python3 tools/virtualization-conformance/test_run_conformance.py
```

Exit codes: `0` conformant, `1` findings, `2` usage/IO error.

Useful flags: `--guest-arch` (repeatable), `--output`, `--format {text,json,both}`,
`--timeout`, `--backend-env NAME=VALUE`, `--allow-code CODE` (downgrade a *known*
finding to informational so CI can stay green while a defect is tracked),
`--quiet`.

## What it checks

**Process level**
* exit code is in the documented set (`--list-backends` → 0; `--check` → 0 or 1);
* stdout is *exactly one* parseable JSON document — progress text around the JSON
  is a finding, because that is not machine-readable;
* the document declares `virtualworld.virtualization/<int>` with version ≥ 1.

**Shape**
* `host` / `guest_arch` / `build` / `backends` / `selection` / `support_matrix`
  present with every required field and the right JSON type;
* every boolean is a *real* JSON boolean — `0`, `1`, `"false"` and `null` are
  rejected, which matters most for `support_matrix[].verified`;
* backend ids are non-empty and unique; tiers ∈ {native, hardware, emulation};
  issue levels ∈ {info, warning, error}.

**Selection invariants**
* exactly one candidate is selected, and it equals `selection.backend`;
* an unsupported candidate is never selected;
* every non-selected candidate carries a **non-empty reason**;
* candidates are ordered native → hardware → emulation;
* `qemu-tcg` is present, supports every guest architecture, and is **always last**;
* a QEMU-compatibility backend (`harmony-virtualization`, `linux-kvm`) never
  reports native tier or `native_execution` — QEMU always owns the device model;
* a backend wrapping a real host hypervisor API (`macos-virtualization`,
  `windows-hypervisor`) *must* report the native tier;
* with `preference=auto` the best eligible candidate wins, and a
  **cross-architecture** guest is always served by `qemu-tcg`;
* a `fell_back` selection must name `fallback_from`.

**Support-matrix hygiene**
* field completeness and strictly-boolean `verified`;
* no row may advertise `native`/`hardware` for a guest architecture that differs
  from the host architecture — cross-architecture hardware acceleration does not
  exist;
* no row may label a QEMU-compatibility backend as `native`.

## Fixtures and the synthetic/real distinction

`fixtures/index.json` lists every document and states, per entry, whether it is a
**real capture** (`synthetic: false`) or **hand-authored** (`synthetic: true`).
The distinction is load-bearing:

* Only real captures may be cited as evidence that a host was exercised. The two
  `macos-apple-silicon-*.json` fixtures are verbatim captures from a built binary
  on a real Apple Silicon Mac.
* Synthetic fixtures (`make_synthetic_fixtures.py`) let CI on *any* machine cover
  Windows x86_64/ARM64, Linux x86_64/ARM64/RISC-V64, macOS Intel and
  HarmonyOS/OpenHarmony. They are shaped against the shared contract and against
  the project's own documented `support_matrix`.
* A synthetic fixture whose matrix claims `verified: true` is a **finding**
  (`synthetic-fixture-claims-verified`). Hand-written data can never assert real
  hardware verification.

`fixtures/negative/` holds documents that reproduce *known defects*. They are
marked `expect_fail` with the codes the checker must produce; if one of them ever
passes, the harness reports `negative-fixture-unexpectedly-passed` — losing
detector coverage is itself a failure.

## Current status of this build

The Windows tier defect that earlier rounds tracked is **fixed** in the current
`build-final` (schema `virtualworld.virtualization/3`): `windows-hypervisor` now
reports `tier=native`, and consequently `qemu-tcg` is once again the last
candidate. All live targets pass in strict mode with no `--allow-code`.

The defect is still enforced as a *detector* regression by
`fixtures/negative/windows-native-tier-regression.json`: that historical document
must keep failing, so the invariants stay enforced even though the product is now
clean. A negative fixture that starts passing is itself reported
(`negative-fixture-unexpectedly-passed`).

Current state:

```bash
# live binary: 6 targets, 0 errors -> CONFORMANT, exit 0
run_conformance.py --binary build-final/VirtualWorld.app/Contents/MacOS/VirtualWorld \
    --guest-arch x86_64 --guest-arch aarch64 --guest-arch riscv64

# fixtures: 9 positive pass, 3 negative fail as designed -> exit 1 (expected)
run_conformance.py --fixtures fixtures
```

The fixture exit code is 1 *by design*: negative fixtures exist to prove the
detectors still fire, so a fixture run is green only when `expectation_met` is
true for every row (see `reports/final-report.md`), not when the process exits 0.

## Final certification

`make_final_report.py` runs the self-tests, the live checks (strict), the fixture
replay and the legacy-compatibility checks, then writes:

```
reports/final-report.json   machine-readable, per-target commands and exit codes
reports/final-report.md     the same, human-readable
```

It records the certified artifact's SHA-256, its build integrity (whether any
product input is newer than the linked binary), the support matrix, the
real-capture vs synthetic fixture split, defect ownership, and the list of things
this method cannot verify.

## Stability, concurrency and mutation gate

`run_stability_gate.py` is a second, stricter gate bound to one **pinned SHA-256**.
It answers: does the same read-only command always produce the same thing, and
does every contract violation still get caught?

```bash
python3 tools/virtualization-conformance/run_stability_gate.py     --binary build-final/VirtualWorld.app/Contents/MacOS/VirtualWorld     --expected-sha256 238f603d3d1d1db3c8b574f9617d0632f0f6bcbfbe4c44d923917b6dda60b80f     --rounds 50 --concurrency 8
```

* **Artifact pinning.** The digest is checked before anything runs. If the binary
  does not match, the gate writes a **blocked** report and exits 2 — it never
  falls back to another binary and never rebuilds the product.
* **Determinism.** 50 rounds per command/architecture, run **sequentially** and
  under **controlled concurrency** (8 workers). Each invocation records exit code,
  signal, timeout, duration, stdout and stderr. Per variant the gate requires
  identical exit codes, exactly one JSON document on stdout, empty stderr, and
  no semantic drift in selection order, backend ids, `tier`, `execution_tier`,
  `status_class`, or any candidate reason.
* **No hidden normalization.** `NORMALIZED_FIELDS` is empty: schema v3 was
  verified byte-identical across runs, so nothing is filtered. If bytes ever
  diverge the gate reports it. A field may only be added there if it is a
  generation timestamp, and it is then printed in the report.
* **Mutation suite.** Deterministic, seeded (same seed → same suite), 73 mutations
  across 14 categories — duplicate ids, missing fields, wrong types, unknown
  `tier`/`status_class`/`execution_tier`, TCG not last, TCG missing, cross-arch
  acceleration, Harmony claiming native, empty reasons, selection
  inconsistencies, and pseudo-true `verified`. Every mutation must make its named
  detector fire; any mutation whose detector does *not* fire is a reported gap.
  The mutated ("failing") samples are written to `reports/stability-failures/`.
* **Safety.** Only `--list-backends --json` and `--check --json` are ever
  executed; `run_conformance.run_binary` refuses anything else, so `--start` is
  unreachable from either tool.

Output: `reports/stability-report.json` and `reports/stability-report.md`. These
never overwrite `final-report.*`.

Self-tests: `test_run_stability_gate.py` covers artifact pinning (including the
blocked path), comparison primitives, every class of instability (exit code,
stderr leak, non-single-JSON, value change, key reordering, per-field semantic
drift, signal, timeout), mutation determinism and coverage, and the CLI.

## Release manifest certification

`verify_release_manifest.py` independently certifies the packaging output
(`schema virtualworld.release/1`) produced by the packaging gate. It re-derives
everything from the filesystem and from the same build's own executable rather
than trusting the document.

```bash
python3 tools/virtualization-conformance/verify_release_manifest.py
```

* **Discovery never guesses.** Continues searching for
  `virtualization-release-manifest.json` and classifies each candidate as contrib
  ON or OFF from its own declared `contrib_backends`. If two candidates claim the
  same role, or a candidate cannot be classified, the run is **blocked** (exit 2)
  and the candidates are listed; `--manifest-on` / `--manifest-off` are then
  required.
* **Honesty flags.** `verified` and `real_hardware_verified` must be exactly
  `false`. A JSON `1` or `"false"` is rejected as a type error, not accepted as
  truthy.
* **Path safety.** QEMU entries must be bundle-relative, free of `..`, free of
  duplicates (including against `main_program`), non-absolute, and must not
  escape the bundle through a symlink. `main_program.path` is absolute by
  construction; it is accepted only while it stays inside *this manifest's* build
  tree, which is also what stops a cross-check from borrowing another build's
  binary.
* **Re-derived values.** Every recorded `size` and `sha256` is recomputed (main
  program + all ten QEMU entries per manifest) and `firmware_count` /
  `support_count` are recounted from the bundle.
* **Cross-checks.** `CMakeCache.txt`'s `VIRTUALWORLD_INCLUDE_CONTRIB_BACKENDS`
  and the executable's own `--list-backends --json` `build` block must agree with
  the manifest's booleans. If a build has no runnable same-host executable the
  cross-check is recorded as **skipped**; another build's binary is never
  substituted (that path is refused explicitly).
* **Mutation suite.** Deterministic (seeded) mutations covering pseudo-true
  honesty flags, wrong hashes and sizes, duplicate/absolute/traversing/symlinked
  paths, backend-flag drift, false-green gates, inconsistent `failure_count`,
  missing QEMU entries and malformed time/host fields. Every one must hit its
  named detector; samples go to `reports/release-manifest-mutations/`.

Output: `reports/release-manifest-report.json` and `.md`, which never overwrite
`final-report.*` or `stability-report.*`.

> Recounting firmware requires replicating the generator's glob exactly: only
> under `runtime/qemu/`, and **case-sensitively** (`*.bin` does not match
> `HIGH0B.BIN`). Getting either wrong invents a `firmware-count-mismatch`.
> `test_firmware_count_matches_generator_scope_and_case` is the regression test.

## Parser and input-robustness hardening

`run_parser_hardening.py` stress-tests the **pure parsing and validation entry
points** of all four tools above. No product binary is executed and no VM command
is reachable.

```bash
python3 tools/virtualization-conformance/run_parser_hardening.py
```

* **Deterministic.** A fixed seed produces a byte-identical sample set in any
  process (`--list-samples` prints the count and manifest digest). Sample payloads
  never embed a temp path, so reproducibility does not depend on the machine.
* **≥5000 bounded samples** across empty/truncated JSON, duplicate keys, deep
  nesting, very long strings, NUL and control characters, Unicode normalisation
  and confusables, `NaN`/`Infinity`/huge integers, boolean-vs-integer confusion,
  unknown fields, duplicated arrays, absolute/relative/`..` paths, symlink escape,
  and corrupt timestamps and hashes. Bounds: 256 KiB per sample, depth 64,
  arrays ≤256, each batch under a wall-clock timeout.
* **Two contract classes.** *Untrusted* validators (the JSON parser, the document
  validators, the path and scalar helpers) must **never** raise: any exception is
  a defect. *Internal* consumers (`make_final_report`'s renderers) only ever
  receive structures this toolchain builds, so an exception on out-of-contract
  garbage is recorded as informational — while a structurally in-contract payload
  that raises is still a defect.
* **Nothing is hidden.** Exceptions are never swallowed into a pass; they are
  caught only to be classified as failures. Samples that must be rejected are
  checked for *silent acceptance*, samples that must be tolerated for
  *over-rejection*, and each targeted contract violation must produce its
  **specific documented code** — "rejected somehow" is not accepted.
* **Crash and hang attribution.** Batches run in a subprocess; a batch that dies
  or times out is bisected down to the single offending sample.
* **Minimisation.** Only real gaps are minimised (delta-debugged) and written to
  `reports/parser-hardening-failures/`. A clean run writes none.

Output: `reports/parser-hardening-report.json` and `.md`. Recent runs found and
fixed seven real defects across `run_conformance.py`,
`verify_release_manifest.py` and `run_stability_gate.py`; they are listed in the
report's *Defects found by this harness and fixed in the tools* section, and each
has a regression test in `TestFixedDefects`.

## Release candidate acceptance

`verify_release_candidate.py` is the final, gated acceptance run. It consumes
`build-release-candidate/release-ready.json` (schema `virtualworld.release-ready/1`),
published by the release gate only after the shared integration and both Release
builds pass.

```bash
python3 tools/virtualization-conformance/verify_release_candidate.py
```

* **Gated.** If `release-blocked.json` exists, or the readiness marker never
  appears, the run writes a `blocked` report and stops. It never builds, rebuilds,
  substitutes `build-final`, or repairs the product.
* **Pinned to one candidate.** Every check binds to the SHA-256 in the marker. The
  digest is recomputed at *every* candidate invocation and again after the battery;
  if it moves, the verdict is blocked. Candidate paths must be absolute and inside
  the marker's own `build-release-candidate*` directory.
* **No silent passes.** Required facts (schema, candidate path, binary digest,
  platform freeze marker digest, source aggregate, contrib toggle, honesty flags)
  are located by documented alias and reported with the exact JSON path used. A
  fact that cannot be located — including a source aggregate with no source list
  to recompute — makes the run `blocked` rather than assumed true. The marker must
  not be older than the artifacts it describes.
* **`vm_started` is a branch, not a constant.** `real_hardware_verified` must
  still be exactly `false` (a Debian UTM guest is virtual hardware, not real
  hardware). `vm_started: false` is acceptable only with a non-empty
  `skip_reason`. `vm_started: true` switches the run onto a Debian-only evidence
  path and blocks unless **all** of the following hold:
  - `guest_os` is `debian` (a Kali or any other guest is rejected, including a
    Kali mention anywhere in the evidence subtree or in the VM name);
  - a UTM VM identifier/name is recorded;
  - the pre-start state proves the VM was **not already running** (an already
    running state is rejected, and an unrecognised state is rejected as
    unprovable — otherwise "only this instance was stopped" cannot be shown);
  - the evidence is bound to the candidate path and binary SHA-256;
  - the test medium is recorded as an **absolute** path, lies inside the
    `Start Boot Option` root or inside an explicitly declared *Debian* UTM
    configuration (a non-Debian configuration does not widen the allowance), and
    its recomputed SHA-256 matches;
  - `started_at` / `ready_at` / `stopped_at` are present, parseable, and in
    order;
  - every recorded log/evidence path exists on disk;
  - the evidence asserts that **only the instance this run started** was stopped.

  This tool never contacts a VM: it certifies the evidence WorkBuddy AI recorded
  and does not reproduce the boot. All of the above are hard blocks — the battery
  is not run and the verdict is `blocked`.
* **Command allowlist.** The candidate binary is executed only through
  `CandidateRunner`, which refuses anything outside
  `--check`/`--list-backends`/`--json`/`--help`/`--version`. A static reachability
  scan independently proves no `--start`/`--launch`/`--boot` flag appears in
  executable code across the whole toolchain. No VM is ever started.
* **Battery.** Re-runs everything against the pinned candidate: the full verifier
  self-test suite, the 5000-sample parser hardening run, strict live conformance
  on three architectures, release-manifest certification for both candidates, and
  the stability/concurrency gate (600 read-only invocations, fixed seed). Each
  sub-verifier writes into its own `rc-*` subdirectory, so the earlier
  `final-report.*`, `stability-report.*`, `release-manifest-report.*` and
  `parser-hardening-report.*` files are never overwritten.
* **Independent schema.** Output is `reports/release-candidate-final.json` and
  `.md` under `virtualworld.release-candidate-final/1`.

A marker-only run (`--skip-heavy`) reports `incomplete`, never `certified`: a
verdict of certified always means the battery actually ran.

## Rerun after a block (history vs. active gate)

A blocked release gate leaves `release-blocked.json` behind. When the gate later
consumes that block it renames the file to `release-blocked.<timestamp>.json`;
those renamed files are **history**, not an active gate, and must not keep
blocking every subsequent run. `active_blocked_marker()` therefore treats only
the canonical `release-blocked.json` as active, records the historical ones in the
report, and a *new* active block still blocks normally.

```bash
# the rerun writes its own files; the first-round report is never overwritten
python3 tools/virtualization-conformance/verify_release_candidate.py \
    --report-basename release-candidate-rerun
```

When a rerun follows a block, the readiness marker must additionally prove it
supersedes that specific block:

* its `generated_at` must be **later** than the freeze marker's `freeze_utc`;
* it must name the error it supersedes, and that name must be
  `PLATFORM_FREEZE_HASH_MISMATCH`;
* its candidate and freeze-marker digests must be the refreshed ones — re-checked
  against disk like every other hash.

These three conditions apply only when a historical block is present, so a clean
first-round readiness is unaffected.

### Platform freeze verification

`verify_freeze_marker()` parses the text-format `.platform-freeze-ready` marker and
re-derives every frozen file hash from disk. It requires exactly **8** owned files,
all matching, and refuses a marker that claims `real_hardware_verified` anything
other than false. A repaired marker is checked for internal consistency too: the
`freeze_repaired_from_hash` must be the hash the superseded block complained about,
and `freeze_repaired_to_hash` must equal what is on disk now.

## Layout

```
tools/virtualization-conformance/
├── run_conformance.py              the checker (stdlib only)
├── test_run_conformance.py         58 self-tests (unittest)
├── make_synthetic_fixtures.py      regenerates the synthetic fixtures
├── README.md                       this file
└── fixtures/
    ├── index.json                  manifest: real vs synthetic, expectations
    ├── macos-apple-silicon-aarch64.json          (real capture)
    ├── macos-apple-silicon-x86_64-guest.json     (real capture, cross-arch)
    ├── macos-intel-x86_64.json                   (synthetic)
    ├── windows-x86_64.json / windows-aarch64.json
    ├── linux-x86_64.json / linux-aarch64.json / linux-riscv64.json
    ├── harmonyos-aarch64.json
    └── negative/
        ├── windows-native-tier-regression.json   reproduces the live defect
        ├── harmony-claims-native.json
        └── broken-json.json
```

## Limitations

* It validates the *reported* contract, not the guest behaviour behind it. A
  backend that reports honestly but boots nothing would pass.
* It cannot see per-config validation outcomes beyond what the two reporting
  commands surface for one probe configuration.
* Synthetic fixtures encode the *intended* contract for hosts this machine does
  not have; they are expectations, not measurements. Only the marked real
  captures are measurements.
