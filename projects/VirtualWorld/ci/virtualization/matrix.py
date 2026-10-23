# ci/virtualization/matrix.py
#
# Canonical, machine-readable definition of the VirtualWorld cross-platform
# CI / release verification matrix.
#
# Scope rule (see docs/virtualization-ci.md): this module ONLY describes CI
# orchestration. It must never import engine code, never start a VM, and never
# claim real-hardware verification. It is pure Python 3 standard library so it
# runs identically on every runner (and locally) without extra dependencies.
#
# The "real" engines / tests / packaging gate live elsewhere; this layer just
# decides WHERE each of them is allowed to run, and records WHY a combination is
# skipped instead of faked-passing.

import datetime

SCHEMA = "virtualworld.ci-matrix/1"

# Architectures we recognise at all. Anything else is rejected by validation.
KNOWN_ARCHES = ("x86-64", "arm64", "riscv64")

# The full set of (host, arch) combinations we must be able to account for.
REQUIRED_COMBINATIONS = [
    ("windows", "x86-64"),
    ("windows", "arm64"),
    ("macos", "x86-64"),
    ("macos", "arm64"),
    ("linux", "x86-64"),
    ("linux", "arm64"),
    ("linux", "riscv64"),
]

# Runner policy per combination.
#   type        : "hosted" (GitHub-provided) or "self-hosted" (must be supplied)
#   on_default  : does this run on ordinary push / pull_request?
#   runs_on     : the literal `runs-on` value used by the workflow YAML
#   label       : a stable identifier for the runner requirement
#
# Honesty rule: a combination with no reliable GitHub-hosted runner is NEVER
# reported as "passing". It is marked opt_in_required and only ever runs when a
# maintainer opts in via workflow_dispatch AND provides a matching self-hosted
# runner. linux/arm64 uses ubuntu-24.04-arm (GA hosted ARM64 Linux); if an org
# does not have it, flip it to self-hosted — the validation will still pass.
RUNNERS = {
    ("windows", "x86-64"): {
        "label": "windows-latest",
        "type": "hosted",
        "on_default": True,
        "runs_on": "windows-latest",
    },
    ("windows", "arm64"): {
        # No GitHub-hosted Windows ARM64 runner exists; require self-hosted.
        "label": "self-hosted-win-arm64",
        "type": "self-hosted",
        "on_default": False,
        "runs_on": ["self-hosted", "windows", "arm64"],
    },
    ("macos", "x86-64"): {
        "label": "macos-13",
        "type": "hosted",
        "on_default": True,
        "runs_on": "macos-13",
    },
    ("macos", "arm64"): {
        "label": "macos-latest",
        "type": "hosted",
        "on_default": True,
        "runs_on": "macos-latest",
    },
    ("linux", "x86-64"): {
        "label": "ubuntu-latest",
        "type": "hosted",
        "on_default": True,
        "runs_on": "ubuntu-latest",
    },
    ("linux", "arm64"): {
        # GitHub-hosted ARM64 Linux (ubuntu-24.04-arm) is generally available.
        "label": "ubuntu-24.04-arm",
        "type": "hosted",
        "on_default": True,
        "runs_on": "ubuntu-24.04-arm",
    },
    ("linux", "riscv64"): {
        # No GitHub-hosted RISC-V64 runner exists; require self-hosted.
        "label": "self-hosted-linux-riscv64",
        "type": "self-hosted",
        "on_default": False,
        "runs_on": ["self-hosted", "linux", "riscv64"],
    },
}

# Conformance command whitelist.
#
# Only read-only inspection of the engine/CLI is ever permitted in CI. Anything
# that could boot a guest VM is forbidden. This is enforced both here (unit
# tested) and at runtime by ci/virtualization/conformance_guard.py.
ALLOWED_CONFORMANCE_TOKENS = (
    "--check",
    "--list-backends",
    "--json",
    "--help",
    "-h",
    "--version",
)
FORBIDDEN_CONFORMANCE_TOKENS = (
    "--start",
    "--launch",
    "--boot",
    "--power-on",
    "--run-vm",
    "--resume",
)


def is_allowed_conformance_command(args):
    """Return (ok, reason).

    `args` is the command line AFTER the program name. A command is allowed iff:
      * it contains no forbidden (VM-launch) token, and
      * every token beginning with '-' is in ALLOWED_CONFORMANCE_TOKENS.
    Positional arguments (arch names, vm.json paths) are permitted but only
    when no forbidden token is present (they are ignored for the allow-list
    decision because the forbidden check already rejects VM starts).
    """
    for tok in args:
        if tok in FORBIDDEN_CONFORMANCE_TOKENS:
            return False, "forbidden VM-launch token: %s" % tok
    for tok in args:
        if tok.startswith("-") and tok not in ALLOWED_CONFORMANCE_TOKENS:
            return False, "unknown/non-whitelisted flag: %s" % tok
    return True, "read-only CLI command"


def _entry_status(on_default):
    """Static plan status for a default-gated step."""
    return "scheduled" if on_default else "opt_in_required"


def _skip_reason(host, arch, on_default):
    if on_default:
        return ""
    return (
        "No reliable GitHub-hosted runner for %s/%s. This combination is "
        "opt-in only: it runs solely when a maintainer triggers "
        "workflow_dispatch with include_self_hosted=true AND supplies a matching "
        "self-hosted runner. It is never reported as passing without real "
        "execution." % (host, arch)
    )


def build_entries():
    """Build the list of matrix entries (the canonical plan)."""
    entries = []
    for (host, arch) in REQUIRED_COMBINATIONS:
        r = RUNNERS[(host, arch)]
        on_default = r["on_default"]
        status = _entry_status(on_default)
        entries.append({
            "host": host,
            "arch": arch,
            "runner_label": r["label"],
            "runner_type": r["type"],
            "runs_on": r["runs_on"],
            "on_default": on_default,
            "trigger": "push_and_pr" if on_default else "workflow_dispatch",
            "execution_model": "native-host",
            "cross_compiled": False,
            "real_hardware_run": False,
            "real_hardware_verified": False,
            "conformance_kind": "read-only-cli",
            "build": status,
            "test": status,
            "package": status,
            "conformance": status,
            "skip_reason": _skip_reason(host, arch, on_default),
            "opt_in_available": (not on_default),
        })
    return entries


def _validate_entries(entries):
    """Raise ValueError on any integrity / honesty violation."""
    # Matrix completeness: every required combination appears exactly once.
    seen = {}
    for e in entries:
        key = (e["host"], e["arch"])
        seen[key] = seen.get(key, 0) + 1
    for combo in REQUIRED_COMBINATIONS:
        if combo not in seen:
            raise ValueError("matrix incomplete: missing %s/%s" % combo)
        if seen[combo] != 1:
            raise ValueError("matrix duplicate: %s/%s appears %d times"
                             % (combo[0], combo[1], seen[combo]))

    for e in entries:
        # Unknown architecture.
        if e["arch"] not in KNOWN_ARCHES:
            raise ValueError("unknown arch for %s/%s: %s"
                             % (e["host"], e["arch"], e["arch"]))
        # Missing runner for a default-gated entry.
        if e["on_default"] and (not e["runner_label"] or not e["runs_on"]):
            raise ValueError("missing runner for default entry %s/%s"
                             % (e["host"], e["arch"]))
        # Inconsistent status / skip reason.
        if e["build"] == "scheduled" and e["skip_reason"]:
            raise ValueError("scheduled entry %s/%s must not carry a skip_reason"
                             % (e["host"], e["arch"]))
        if e["build"] in ("opt_in_required", "skipped") and not e["skip_reason"]:
            raise ValueError("skipped entry %s/%s must carry a skip_reason"
                             % (e["host"], e["arch"]))
        # Honesty: never claim real-hardware verification.
        if e["real_hardware_verified"] is not False:
            raise ValueError("real_hardware_verified must be false for %s/%s"
                             % (e["host"], e["arch"]))
        # Honesty: never claim a real (hardware) pass in a plan that has not run.
        if e.get("ci_run_id") is None:
            for step in ("build", "test", "package", "conformance"):
                if e.get(step) in ("passed", "failed"):
                    raise ValueError(
                        "entry %s/%s reports '%s=%s' without a ci_run_id "
                        "(mislabeling a plan as a real result)"
                        % (e["host"], e["arch"], step, e.get(step)))
        # Integrity: default-gated entries must be hosted (we never auto-run
        # self-hosted runners on ordinary push/PR).
        if e["on_default"] and e["runner_type"] != "hosted":
            raise ValueError("on_default entry %s/%s must be hosted, got %s"
                             % (e["host"], e["arch"], e["runner_type"]))
        # Cross-compilation is never real-hardware execution.
        if e["cross_compiled"] and e["real_hardware_verified"] is not False:
            raise ValueError("cross-compiled entry must not claim real hardware")


def build_report(yaml_path=None, yaml_method=None, ci_run_id=None):
    """Assemble the full machine-readable report (does not execute anything)."""
    entries = build_entries()
    # ci_run_id (if provided) lets entries carry real pass/fail later; it is
    # None in a static plan, which validation treats as "not yet executed".
    for e in entries:
        e["ci_run_id"] = ci_run_id
    _validate_entries(entries)

    hosted = sum(1 for e in entries if e["runner_type"] == "hosted")
    self_hosted = sum(1 for e in entries if e["runner_type"] == "self-hosted")
    scheduled = sum(1 for e in entries if e["build"] == "scheduled")
    opt_in = sum(1 for e in entries if e["build"] == "opt_in_required")

    yaml_validation = _validate_yaml(yaml_path, yaml_method)

    report = {
        "schema": SCHEMA,
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                          .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_by": "ci/virtualization/generate_matrix_report.py",
        "ci_run_id": ci_run_id,
        "remote_executed": False,
        "hosts": sorted({e["host"] for e in entries}),
        "arches_known": list(KNOWN_ARCHES),
        "entries": entries,
        "summary": {
            "total": len(entries),
            "scheduled": scheduled,
            "opt_in_required": opt_in,
            "hosted": hosted,
            "self_hosted": self_hosted,
            "real_hardware_verified_false_count":
                sum(1 for e in entries if e["real_hardware_verified"] is False),
        },
        "yaml_validation": yaml_validation,
        "notes": [
            "Cross-compilation is never counted as real-hardware execution.",
            "real_hardware_verified is false for every entry: the CI matrix "
            "never starts a guest VM (see --start exclusion).",
            "opt_in_required entries are skipped on push/PR and only run via "
            "workflow_dispatch include_self_hosted=true on a supplied runner.",
            "Conformance in CI is read-only CLI only (whitelisted flags).",
        ],
    }
    return report


def _validate_yaml(path, forced_method=None):
    """Lightweight, dependency-free check of the workflow YAML.

    Prefers PyYAML if present; otherwise falls back to explicit textual checks
    and records which method was used. Never installs software or touches the
    network.
    """
    result = {"checked": False, "method": None, "issues": [], "ok": True}
    if not path:
        result["issues"].append("no yaml path provided")
        result["ok"] = False
        return result
    import os
    if not os.path.exists(path):
        result["issues"].append("yaml file not found: %s" % path)
        result["ok"] = False
        return result

    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    result["checked"] = True

    # Connection-level: every runner label must be referenced by the workflow.
    missing = []
    for combo, r in RUNNERS.items():
        label = r["label"]
        if isinstance(r["runs_on"], list):
            needles = r["runs_on"]
        else:
            needles = [r["runs_on"]]
        if not any(n in text for n in needles):
            missing.append("%s/%s (%s)" % (combo[0], combo[1], label))
    if missing:
        result["issues"].append(
            "runner labels not found in workflow: " + "; ".join(missing))
        result["ok"] = False

    # Structural presence checks (textual, dependency-free).
    required_tokens = [
        "workflow_dispatch",
        "pull_request",
        "concurrency",
        "permissions",
        "timeout-minutes",
        "include_self_hosted",
    ]
    for tok in required_tokens:
        if tok not in text:
            result["issues"].append("missing expected token: %s" % tok)
            result["ok"] = False

    # If PyYAML is available, corroborate structurally; otherwise rely on text.
    if forced_method == "text" or forced_method is None:
        try:
            import yaml  # noqa: F401
            result["method"] = "pyyaml"
            _yaml_struct_checks(text, result)
        except ImportError:
            result["method"] = "text"
    else:
        result["method"] = forced_method
    return result


def _yaml_struct_checks(text, result):
    try:
        import yaml
        doc = yaml.safe_load(text)
        on = doc.get("on", doc.get(True, None))
        if isinstance(on, dict):
            if "workflow_dispatch" not in on and "pull_request" not in on \
                    and "push" not in on:
                result["issues"].append("on: missing trigger keys")
                result["ok"] = False
        jobs = doc.get("jobs", {})
        if "static-checks" not in jobs:
            result["issues"].append("jobs: missing static-checks")
            result["ok"] = False
    except Exception as exc:  # pragma: no cover - defensive
        result["issues"].append("yaml parse warning: %s" % exc)
