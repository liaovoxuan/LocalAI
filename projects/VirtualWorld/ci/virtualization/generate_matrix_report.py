# ci/virtualization/generate_matrix_report.py
#
# Generates the machine-readable CI matrix report (ci-matrix-report.json) and
# runs the built-in self-tests. Pure standard library; safe to run anywhere
# (locally, or as the `static-checks` job in the workflow).
#
# Usage:
#   python3 generate_matrix_report.py [--out PATH] [--yaml PATH]
#                                    [--yaml-method text|pyyaml]
#   python3 generate_matrix_report.py --self-test
#
# This script never builds anything, never starts a VM, and never contacts the
# network.

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import matrix  # noqa: E402


def run_self_tests():
    """Return (passed, failed, details). Never raises on a *test* failure."""
    passed = 0
    failed = 0
    details = []

    def check(name, cond, info=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            details.append("PASS  %s" % name)
        else:
            failed += 1
            details.append("FAIL  %s  %s" % (name, info))

    # 1) Happy path: the canonical report validates cleanly.
    try:
        rep = matrix.build_report()
        check("canonical-report-validates", True)
    except Exception as exc:  # pragma: no cover
        check("canonical-report-validates", False, str(exc))
        details.append("self-test aborted: canonical report invalid")
        return passed, failed, details

    # 2) Matrix completeness: removing a required combination must fail.
    entries = matrix.build_entries()
    entries_missing = entries[:-1]  # drop the last required combination
    try:
        matrix._validate_entries(entries_missing)
        check("matrix-completeness-rejects-missing", False,
              "validation should have rejected a missing combination")
    except ValueError:
        check("matrix-completeness-rejects-missing", True)

    # 3) Unknown architecture is rejected.
    entries_bad = matrix.build_entries()
    entries_bad[0] = dict(entries_bad[0])
    entries_bad[0]["arch"] = "sparc"
    # keep uniqueness by overriding host too so the missing-check doesn't trip
    entries_bad[0]["host"] = "windows"
    try:
        matrix._validate_entries(entries_bad)
        check("unknown-arch-rejected", False,
              "validation should reject arch 'sparc'")
    except ValueError:
        check("unknown-arch-rejected", True)

    # 4) Missing runner for a default entry is rejected.
    entries_bad = matrix.build_entries()
    entries_bad[0] = dict(entries_bad[0])
    entries_bad[0]["on_default"] = True
    entries_bad[0]["runner_label"] = ""
    entries_bad[0]["runs_on"] = ""
    try:
        matrix._validate_entries(entries_bad)
        check("missing-runner-rejected", False,
              "validation should reject a default entry with no runner")
    except ValueError:
        check("missing-runner-rejected", True)

    # 5) Mislabeling real hardware (real_hardware_verified=true) is rejected.
    entries_bad = matrix.build_entries()
    entries_bad[0] = dict(entries_bad[0])
    entries_bad[0]["real_hardware_verified"] = True
    try:
        matrix._validate_entries(entries_bad)
        check("real-hardware-mislabel-rejected", False,
              "validation should reject real_hardware_verified=true")
    except ValueError:
        check("real-hardware-mislabel-rejected", True)

    # 6) Mislabeling a plan as a real pass (status passed without ci_run_id).
    entries_bad = matrix.build_entries()
    entries_bad[0] = dict(entries_bad[0])
    entries_bad[0]["build"] = "passed"
    try:
        matrix._validate_entries(entries_bad)
        check("fake-pass-rejected", False,
              "validation should reject 'passed' without a ci_run_id")
    except ValueError:
        check("fake-pass-rejected", True)

    # 7) Whitelist: forbidden VM-launch token rejected.
    ok, _ = matrix.is_allowed_conformance_command(["--start", "vm.json"])
    check("whitelist-blocks-start", ok is False)
    ok, _ = matrix.is_allowed_conformance_command(
        ["--start", "--backend", "qemu", "--wait"])
    check("whitelist-blocks-start-with-flags", ok is False)

    # 8) Whitelist: read-only commands accepted.
    ok, _ = matrix.is_allowed_conformance_command(["--check", "--json"])
    check("whitelist-allows-check-json", ok is True)
    ok, _ = matrix.is_allowed_conformance_command(["--list-backends"])
    check("whitelist-allows-list-backends", ok is True)

    # 9) Whitelist: unknown flag rejected.
    ok, _ = matrix.is_allowed_conformance_command(["--frobnicate"])
    check("whitelist-blocks-unknown-flag", ok is False)

    # 10) Whitelist: harmless positional arg (arch) accepted when read-only.
    ok, _ = matrix.is_allowed_conformance_command(["--check", "arm64", "--json"])
    check("whitelist-allows-positional-arch", ok is True)

    # 11) Summary sanity.
    s = rep["summary"]
    check("summary-total-7", s["total"] == 7,
          "total=%s" % s["total"])
    check("summary-real-hardware-false-all", s["real_hardware_verified_false_count"] == 7,
          "count=%s" % s["real_hardware_verified_false_count"])
    check("summary-scheduled-5", s["scheduled"] == 5,
          "scheduled=%s" % s["scheduled"])
    check("summary-opt-in-2", s["opt_in_required"] == 2,
          "opt_in=%s" % s["opt_in_required"])

    return passed, failed, details


def main(argv=None):
    parser = argparse.ArgumentParser(description="VirtualWorld CI matrix report")
    parser.add_argument("--out", default="ci-matrix-report.json",
                        help="output JSON path")
    parser.add_argument("--yaml", default=None,
                        help="path to .github/workflows/virtualization-ci.yml "
                             "for a lightweight structural check")
    parser.add_argument("--yaml-method", default=None,
                        choices=["text", "pyyaml"],
                        help="force the YAML check method (default: auto)")
    parser.add_argument("--self-test", action="store_true",
                        help="run the built-in self-tests and exit")
    args = parser.parse_args(argv)

    if args.self_test:
        passed, failed, details = run_self_tests()
        for line in details:
            print(line)
        print("self-tests: %d passed, %d failed" % (passed, failed))
        return 0 if failed == 0 else 1

    report = matrix.build_report(yaml_path=args.yaml,
                                 yaml_method=args.yaml_method)
    out_path = args.out
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print("wrote %s (schema=%s, entries=%d, scheduled=%d, opt_in=%d)"
          % (out_path, report["schema"], report["summary"]["total"],
             report["summary"]["scheduled"],
             report["summary"]["opt_in_required"]))
    if report["yaml_validation"].get("ok") is False:
        print("WARNING: yaml validation reported issues:")
        for issue in report["yaml_validation"]["issues"]:
            print("  - %s" % issue)
        # Do not hard-fail the build on a yaml check warning; surface it.
    return 0


if __name__ == "__main__":
    sys.exit(main())
