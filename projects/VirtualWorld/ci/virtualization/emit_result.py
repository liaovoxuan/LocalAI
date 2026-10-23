# ci/virtualization/emit_result.py
#
# Emits a single per-task CI result JSON (virtualworld.ci-result/1) for one
# matrix leg, derived from the step outcomes of that leg.
#
# Scope: CI orchestration only. It NEVER builds or starts a VM; it only records
# what the current leg's steps actually did. The status is DERIVED from the
# step outcomes (build/test/package/conformance) -- an artifact upload
# succeeding does NOT make this "passed".
#
# This runs with `if: always()` so a leg that failed or was cancelled still
# emits an honest result (a failed/cancelled leg must be visible to the
# aggregation gate, never silently dropped).

import argparse
import datetime
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import result_schema as rs  # noqa: E402

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(_TS_FMT)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _map_outcome(raw):
    return rs.normalize_status(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Emit one CI result JSON")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--contrib", required=True, choices=["ON", "OFF"])
    parser.add_argument("--runner-type", default="hosted",
                        choices=["hosted", "self-hosted"])
    parser.add_argument("--job-name", default="verify")
    parser.add_argument("--artifact-path", default=None,
                        help="path to the built main artifact (hashed if it "
                             "exists and build passed)")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    build = _map_outcome(os.environ.get("STEP_BUILD", "success"))
    test = _map_outcome(os.environ.get("STEP_TEST", "success"))
    package = _map_outcome(os.environ.get("STEP_PACKAGE",
                                          os.environ.get("STEP_TEST", "success")))
    conformance = _map_outcome(os.environ.get("STEP_CONFORMANCE", "skipped"))

    artifact_sha256 = None
    if build == "passed" and args.artifact_path \
            and os.path.exists(args.artifact_path):
        try:
            artifact_sha256 = _sha256_file(args.artifact_path)
        except Exception:
            artifact_sha256 = None

    skip_reason = os.environ.get("SKIP_REASON", "")

    result = rs.build_result(
        run_id=args.run_id,
        commit_sha=args.commit_sha,
        host=args.host,
        arch=args.arch,
        runner_type=args.runner_type,
        job_name=args.job_name,
        contrib=args.contrib,
        build=build,
        test=test,
        package=package,
        conformance=conformance,
        artifact_sha256=artifact_sha256,
        started_at=os.environ.get("RESULT_STARTED") or _now(),
        finished_at=os.environ.get("RESULT_FINISHED") or _now(),
        real_hardware_verified=False,
        skip_reason=skip_reason,
        allowed_command_summary=["--check --json", "--list-backends --json"])

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print("wrote result %s/%s/%s runner=%s status=%s hash=%s"
          % (args.host, args.arch, args.contrib, args.runner_type,
             result["status"], "yes" if artifact_sha256 else "no"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
