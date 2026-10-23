# ci/virtualization/aggregate_results.py
#
# Result backfill & anti-fake-green aggregation gate for the VirtualWorld
# cross-platform CI.
#
# Scope: CI orchestration only. This script NEVER builds, never starts a VM,
# and never claims real-hardware verification. It is pure Python 3 standard
# library so it runs identically on every runner and locally.
#
# What it does:
#   * Consumes one JSON file per matrix task (the per-task result emitted by
#     ci/virtualization/emit_result.py), each conforming to
#     virtualworld.ci-result/1 (see result_schema.py).
#   * Strictly rejects: cross run_id mixing, cross commit_sha mixing, duplicate
#     (host,arch,contrib), absent required (hosted) combinations, stale
#     timestamps, faked "passed", missing/duplicate artifact SHA-256, unknown
#     step/task status, and opt-in / self-hosted skipped results without a
#     skip_reason.
#   * Emits machine-readable ci-aggregate-report.json and a ci-aggregate-report.md
#     summary.
#
# Honesty contract:
#   * The aggregated status is DERIVED from step outcomes. "Upload artifact
#     succeeded" is never treated as a success signal.
#   * real_hardware_verified is false for every result and for the aggregate.
#   * A status of "passed" is only produced when every required (hosted)
#     combination actually ran and passed.

import argparse
import datetime
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import result_schema as rs  # noqa: E402
import matrix  # noqa: E402

AGGREGATE_SCHEMA = "virtualworld.ci-aggregate/1"
STALE_WINDOW_HOURS = 24

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _parse(ts):
    return datetime.datetime.strptime(ts, _TS_FMT)


def load_results_from_dir(dir_path):
    """Load every *.json result file in a directory into a list of dicts."""
    results = []
    for path in sorted(glob.glob(os.path.join(dir_path, "*.json"))):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            results.extend(data)
        else:
            results.append(data)
    return results


def _required_combos():
    """The (host, arch, contrib) tuples that MUST have a result that ran.

    These are the hosted entries (GitHub-provided runners) for both contrib
    settings. Self-hosted / opt-in combos are NOT required: their absence is
    honest and expected on ordinary push/PR.
    """
    required = []
    for (host, arch) in matrix.REQUIRED_COMBINATIONS:
        if matrix.RUNNERS[(host, arch)]["type"] == "hosted":
            for contrib in ("ON", "OFF"):
                required.append((host, arch, contrib))
    return required


def validate_and_aggregate(results, freshness_hours=STALE_WINDOW_HOURS):
    """Validate a list of per-task results and produce the aggregate report.

    Returns (report_dict, errors) where errors is a list of (code, message).
    """
    errors = []

    # --- per-result validation (reuse result_schema) -----------------------
    for i, r in enumerate(results):
        for (code, msg) in rs.validate_result(r):
            errors.append((code, "result[%d] (%s): %s"
                           % (i, r.get("job_name", "?"), msg)))

    # --- cross run_id / commit_sha mixing -----------------------------------
    run_ids = set(r.get("run_id") for r in results)
    commit_shas = set(r.get("commit_sha") for r in results)
    if len(run_ids) > 1:
        errors.append(("AGG_CROSS_RUN_ID",
                       "multiple run_ids: %s" % sorted(x for x in run_ids if x)))
    if len(commit_shas) > 1:
        errors.append(("AGG_CROSS_COMMIT",
                       "multiple commit_shas: %s"
                       % sorted(x for x in commit_shas if x)))

    # --- duplicate (host, arch, contrib) ------------------------------------
    seen = {}
    for r in results:
        key = (r.get("host"), r.get("arch"), r.get("contrib"))
        seen[key] = seen.get(key, 0) + 1
    for key, cnt in seen.items():
        if cnt > 1:
            errors.append(("AGG_DUPLICATE_COMBO",
                           "%s/%s/%s appears %d times"
                           % (key[0], key[1], key[2], cnt)))

    # --- required (hosted) combos must each have a result that ran ----------
    required = _required_combos()
    present = {}
    for r in results:
        present[(r.get("host"), r.get("arch"), r.get("contrib"))] = r
    missing = []
    failed_required = []
    for key in required:
        r = present.get(key)
        if r is None:
            missing.append(key)
            continue
        st = r.get("status")
        if st in ("skipped", "cancelled", None, "not_run"):
            # A hosted combination must actually run, not be skipped/cancelled.
            missing.append(key)
        elif st in ("failed", "error"):
            failed_required.append(key)
    if missing:
        errors.append(("AGG_MISSING_REQUIRED",
                       "required hosted combos absent or not-run: "
                       + ", ".join("%s/%s/%s" % k for k in missing)))
    if failed_required:
        errors.append(("AGG_TASK_FAILED",
                       "required combos ended failed/error: "
                       + ", ".join("%s/%s/%s" % k for k in failed_required)))

    # --- stale timestamp window ---------------------------------------------
    finished_times = []
    for i, r in enumerate(results):
        sa = r.get("started_at")
        fa = r.get("finished_at")
        if not sa or not fa:
            continue
        try:
            st = _parse(sa)
            ft = _parse(fa)
        except ValueError:
            continue
        if ft < st:
            errors.append(("AGG_STALE_TIMESTAMP",
                           "result[%d] finished before it started" % i))
            continue
        finished_times.append((i, ft))
    if finished_times:
        max_ft = max(ft for _, ft in finished_times)
        cutoff = max_ft - datetime.timedelta(hours=freshness_hours)
        for i, ft in finished_times:
            if ft < cutoff:
                errors.append(("AGG_STALE_TIMESTAMP",
                               "result[%d] finished_at older than %dh window"
                               % (i, freshness_hours)))

    # --- duplicate artifact SHA-256 -----------------------------------------
    hashes = {}
    for i, r in enumerate(results):
        ah = r.get("artifact_sha256")
        if ah:
            hashes.setdefault(ah, []).append(i)
    for ah, idxs in hashes.items():
        if len(idxs) > 1:
            errors.append(("AGG_HASH_DUPLICATE",
                           "artifact_sha256 %s shared by results %s"
                           % (ah[:8], idxs)))

    # --- derived aggregated status ------------------------------------------
    if errors:
        aggregated = "failed"
    else:
        aggregated = "passed"

    report = {
        "schema": AGGREGATE_SCHEMA,
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                        .strftime(_TS_FMT),
        "generated_by": "ci/virtualization/aggregate_results.py",
        "remote_executed": False,
        "run_id": (list(run_ids)[0] if len(run_ids) == 1 else None),
        "commit_sha": (list(commit_shas)[0] if len(commit_shas) == 1 else None),
        "aggregated_status": aggregated,
        "real_hardware_verified": False,
        "result_count": len(results),
        "required_combo_count": len(required),
        "errors": [{"code": c, "message": m} for (c, m) in errors],
        "error_codes": sorted({c for (c, _) in errors}),
        "results": [
            {
                "host": r.get("host"),
                "arch": r.get("arch"),
                "contrib": r.get("contrib"),
                "runner_type": r.get("runner_type"),
                "status": r.get("status"),
                "artifact_sha256_present": bool(r.get("artifact_sha256")),
                "real_hardware_verified": r.get("real_hardware_verified"),
                "skip_reason": r.get("skip_reason", ""),
            }
            for r in results
        ],
        "notes": [
            "Cross run_id / commit_sha mixing is rejected "
            "(AGG_CROSS_RUN_ID / AGG_CROSS_COMMIT).",
            "Each required (hosted) host/arch/contrib must have a result that "
            "actually ran (build/test/package/conformance).",
            "Self-hosted / opt-in combos may be skipped/cancelled WITH a "
            "skip_reason; they are not required and never faked-passed.",
            "real_hardware_verified is false for every result and the aggregate.",
            "Upload-artifact success is NOT a success signal; status is derived "
            "from step outcomes only.",
        ],
    }
    return report, errors


def render_markdown(report):
    lines = []
    lines.append("# VirtualWorld CI 聚合报告 (`%s`)" % report["schema"])
    lines.append("")
    lines.append("- 生成时间: %s (UTC)" % report["generated_at"])
    lines.append("- run_id: `%s`" % report["run_id"])
    lines.append("- commit_sha: `%s`" % report["commit_sha"])
    lines.append("- 聚合状态: **%s**" % report["aggregated_status"].upper())
    lines.append("- 真机验证: `%s` (恒为 false)" % report["real_hardware_verified"])
    lines.append("- 结果条数: %d" % report["result_count"])
    lines.append("- 必需组合: %d" % report["required_combo_count"])
    lines.append("")
    if report["errors"]:
        lines.append("## 校验错误")
        lines.append("")
        for e in report["errors"]:
            lines.append("- `%s`: %s" % (e["code"], e["message"]))
        lines.append("")
    lines.append("## 各任务结果")
    lines.append("")
    lines.append("| Host | Arch | Contrib | Runner | Status | Artifact | Real HW |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for r in report["results"]:
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            r["host"], r["arch"], r["contrib"], r["runner_type"], r["status"],
            "yes" if r["artifact_sha256_present"] else "no",
            r["real_hardware_verified"]))
    lines.append("")
    lines.append("## 说明")
    lines.append("")
    for n in report["notes"]:
        lines.append("- %s" % n)
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Deterministic synthetic self-tests (fixed time + fixed seed, no randomness)
# ---------------------------------------------------------------------------

def _hash_for(host, arch, contrib):
    import hashlib
    return hashlib.sha256(
        ("%s-%s-%s" % (host, arch, contrib)).encode("utf-8")).hexdigest()


def _mk(host, arch, contrib, runner_type="hosted",
        build="passed", test="passed", package=None, conformance="passed",
        artifact_sha256=None, real_hw=False, skip_reason="",
        started_min=0, finished_min=5, run_id="RUN-1", commit_sha="COMMIT-AAA",
        job_name=None):
    base = datetime.datetime(2025, 6, 1, 0, 0, 0)
    started = (base + datetime.timedelta(minutes=started_min)).strftime(_TS_FMT)
    finished = (base + datetime.timedelta(minutes=finished_min)).strftime(_TS_FMT)
    if artifact_sha256 is None and build == "passed":
        artifact_sha256 = _hash_for(host, arch, contrib)
    r = rs.build_result(
        run_id=run_id, commit_sha=commit_sha, host=host, arch=arch,
        runner_type=runner_type, contrib=contrib,
        build=build, test=test, package=package, conformance=conformance,
        artifact_sha256=artifact_sha256, started_at=started, finished_at=finished,
        real_hardware_verified=real_hw, skip_reason=skip_reason,
        allowed_command_summary=["--check --json", "--list-backends --json"])
    if job_name is not None:
        r["job_name"] = job_name
    return r


def _full_hosted_pass():
    """All 10 required hosted (host,arch,contrib) results, all passing."""
    out = []
    for (host, arch) in matrix.REQUIRED_COMBINATIONS:
        if matrix.RUNNERS[(host, arch)]["type"] != "hosted":
            continue
        for contrib in ("ON", "OFF"):
            out.append(_mk(host, arch, contrib))
    return out


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

    # 1) Full pass: 10 hosted + 2 legal opt-in skips -> aggregated passed.
    results = _full_hosted_pass()
    results.append(_mk("windows", "arm64", "ON", runner_type="self-hosted",
                       build="skipped", test="skipped", conformance="skipped",
                       skip_reason="no hosted win-arm64 runner; opt-in only"))
    results.append(_mk("linux", "riscv64", "ON", runner_type="self-hosted",
                       build="skipped", test="skipped", conformance="skipped",
                       skip_reason="no hosted riscv64 runner; opt-in only"))
    rep, errs = validate_and_aggregate(results)
    codes = {c for (c, _) in errs}
    check("full-pass-aggregates-passed", rep["aggregated_status"] == "passed"
          and not errs, "errors=%s" % codes)
    check("full-pass-10-required", rep["required_combo_count"] == 10,
          "required=%s" % rep["required_combo_count"])
    check("full-pass-opt-in-skips-ok",
          "AGG_MISSING_REQUIRED" not in codes,
          "codes=%s" % codes)

    # 2) Any required combo missing -> AGG_MISSING_REQUIRED.
    results = _full_hosted_pass()
    results = [r for r in results
               if not (r["host"] == "linux" and r["arch"] == "x86-64"
                       and r["contrib"] == "ON")]
    rep, errs = validate_and_aggregate(results)
    check("missing-required-rejected",
          "AGG_MISSING_REQUIRED" in {c for (c, _) in errs},
          "codes=%s" % {c for (c, _) in errs})

    # 3) Legal opt-in skip (windows/arm64 + linux/riscv64 self-hosted skipped
    #    WITH skip_reason) is accepted; no error.
    results = _full_hosted_pass()
    results.append(_mk("windows", "arm64", "OFF", runner_type="self-hosted",
                       build="skipped", test="skipped", conformance="skipped",
                       skip_reason="no hosted win-arm64 runner; opt-in only"))
    results.append(_mk("linux", "riscv64", "OFF", runner_type="self-hosted",
                       build="skipped", test="skipped", conformance="skipped",
                       skip_reason="no hosted riscv64 runner; opt-in only"))
    rep, errs = validate_and_aggregate(results)
    check("opt-in-skip-no-error",
          "AGG_MISSING_REQUIRED" not in {c for (c, _) in errs},
          "codes=%s" % {c for (c, _) in errs})

    # 4) Cross-commit contamination -> AGG_CROSS_COMMIT.
    results = _full_hosted_pass()
    results[0] = dict(results[0])
    results[0]["commit_sha"] = "COMMIT-BBB"
    rep, errs = validate_and_aggregate(results)
    check("cross-commit-rejected",
          "AGG_CROSS_COMMIT" in {c for (c, _) in errs},
          "codes=%s" % {c for (c, _) in errs})

    # 5) Cross-run-id contamination -> AGG_CROSS_RUN_ID.
    results = _full_hosted_pass()
    results[0] = dict(results[0])
    results[0]["run_id"] = "RUN-2"
    rep, errs = validate_and_aggregate(results)
    check("cross-run-id-rejected",
          "AGG_CROSS_RUN_ID" in {c for (c, _) in errs},
          "codes=%s" % {c for (c, _) in errs})

    # 6) Duplicate (host,arch,contrib) -> AGG_DUPLICATE_COMBO.
    results = _full_hosted_pass()
    results.append(dict(results[0]))  # exact duplicate key
    rep, errs = validate_and_aggregate(results)
    check("duplicate-combo-rejected",
          "AGG_DUPLICATE_COMBO" in {c for (c, _) in errs},
          "codes=%s" % {c for (c, _) in errs})

    # 7) Fake-green: build failed but declared status "passed".
    fake = {
        "schema": rs.RESULT_SCHEMA,
        "run_id": "RUN-1", "commit_sha": "COMMIT-AAA",
        "host": "linux", "arch": "x86-64", "runner_type": "hosted",
        "job_name": "verify", "contrib": "ON",
        "step_status": {"build": "failed", "test": "passed",
                        "package": "passed", "conformance": "passed"},
        "status": "passed",
        "allowed_command_summary": [],
        "artifact_sha256": None,
        "started_at": "2025-06-01T00:00:00Z",
        "finished_at": "2025-06-01T00:05:00Z",
        "real_hardware_verified": False,
        "skip_reason": "",
    }
    rep, errs = validate_and_aggregate([fake])
    check("fake-green-rejected",
          "RESULT_STATUS_MISMATCH" in {c for (c, _) in errs},
          "codes=%s" % {c for (c, _) in errs})

    # 8) False hardware label -> RESULT_REAL_HW_MISLABEL.
    bad = _mk("linux", "x86-64", "ON")
    bad["real_hardware_verified"] = True
    rep, errs = validate_and_aggregate([bad])
    check("false-hw-label-rejected",
          "RESULT_REAL_HW_MISLABEL" in {c for (c, _) in errs},
          "codes=%s" % {c for (c, _) in errs})

    # 9) Hash tampering: two results share an artifact SHA-256.
    a = _mk("linux", "x86-64", "ON")
    b = _mk("linux", "x86-64", "OFF")
    a["artifact_sha256"] = "a" * 64
    b["artifact_sha256"] = "a" * 64
    rep, errs = validate_and_aggregate([a, b])
    check("hash-duplicate-rejected",
          "AGG_HASH_DUPLICATE" in {c for (c, _) in errs},
          "codes=%s" % {c for (c, _) in errs})

    # 10) Build passed but artifact hash missing -> RESULT_HASH_MISSING.
    bad = _mk("linux", "x86-64", "ON", artifact_sha256=None)
    # force build passed yet no hash
    bad["step_status"]["build"] = "passed"
    bad["artifact_sha256"] = None
    rep, errs = validate_and_aggregate([bad])
    check("hash-missing-rejected",
          "RESULT_HASH_MISSING" in {c for (c, _) in errs},
          "codes=%s" % {c for (c, _) in errs})

    # 11) Unknown step status -> RESULT_UNKNOWN_STATUS.
    bad = _mk("linux", "x86-64", "ON")
    bad["step_status"]["build"] = "bogus"
    rep, errs = validate_and_aggregate([bad])
    check("unknown-status-rejected",
          "RESULT_UNKNOWN_STATUS" in {c for (c, _) in errs},
          "codes=%s" % {c for (c, _) in errs})

    # 12) Required combo cancelled -> AGG_MISSING_REQUIRED (hosted must run).
    results = _full_hosted_pass()
    results = [r for r in results
               if not (r["host"] == "macos" and r["arch"] == "arm64"
                       and r["contrib"] == "ON")]
    results.append(_mk("macos", "arm64", "ON",
                       build="cancelled", test="cancelled",
                       conformance="cancelled"))
    rep, errs = validate_and_aggregate(results)
    check("required-cancelled-rejected",
          "AGG_MISSING_REQUIRED" in {c for (c, _) in errs},
          "codes=%s" % {c for (c, _) in errs})

    # 13) Required combo failed -> AGG_TASK_FAILED.
    results = _full_hosted_pass()
    results = [r for r in results
               if not (r["host"] == "macos" and r["arch"] == "arm64"
                       and r["contrib"] == "ON")]
    results.append(_mk("macos", "arm64", "ON",
                       build="failed", test="failed", conformance="failed"))
    rep, errs = validate_and_aggregate(results)
    check("required-failed-rejected",
          "AGG_TASK_FAILED" in {c for (c, _) in errs},
          "codes=%s" % {c for (c, _) in errs})

    # 14) Stale timestamp: one result 48h older than the rest.
    results = _full_hosted_pass()
    results[0]["finished_at"] = "2025-05-30T00:00:00Z"
    results[0]["started_at"] = "2025-05-30T00:00:00Z"
    rep, errs = validate_and_aggregate(results)
    check("stale-timestamp-rejected",
          "AGG_STALE_TIMESTAMP" in {c for (c, _) in errs},
          "codes=%s" % {c for (c, _) in errs})

    # 15) Self-hosted skipped WITHOUT skip_reason -> RESULT_SKIP_WITHOUT_REASON.
    bad = _mk("windows", "arm64", "ON", runner_type="self-hosted",
              build="skipped", test="skipped", conformance="skipped",
              skip_reason="")
    rep, errs = validate_and_aggregate([bad])
    check("skip-without-reason-rejected",
          "RESULT_SKIP_WITHOUT_REASON" in {c for (c, _) in errs},
          "codes=%s" % {c for (c, _) in errs})

    return passed, failed, details


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="VirtualWorld CI result aggregation gate")
    parser.add_argument("--dir", default=None,
                        help="directory containing per-task result JSON files")
    parser.add_argument("--out", default="ci-aggregate-report.json",
                        help="output JSON path")
    parser.add_argument("--md", default=None,
                        help="optional Markdown report path")
    parser.add_argument("--self-test", action="store_true",
                        help="run the built-in self-tests and exit")
    args = parser.parse_args(argv)

    if args.self_test:
        passed, failed, details = run_self_tests()
        for line in details:
            print(line)
        print("aggregate self-tests: %d passed, %d failed" % (passed, failed))
        return 0 if failed == 0 else 1

    if not args.dir:
        print("ERROR: --dir is required (or use --self-test)", file=sys.stderr)
        return 2
    results = load_results_from_dir(args.dir)
    report, errors = validate_and_aggregate(results)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    if args.md:
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(report))
    print("aggregated_status=%s errors=%d results=%d required=%d"
          % (report["aggregated_status"], len(errors),
             report["result_count"], report["required_combo_count"]))
    if errors:
        for e in report["errors"]:
            print("  %s: %s" % (e["code"], e["message"]))
    return 0 if report["aggregated_status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
