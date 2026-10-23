# ci/virtualization/result_schema.py
#
# Single-task CI result schema (virtualworld.ci-result/1) and its validation.
#
# Scope: CI orchestration only. This module NEVER builds, never starts a VM, and
# never claims real-hardware verification. Pure Python 3 standard library so it
# runs identically on every runner and locally.
#
# Honesty contract:
#   * real_hardware_verified is ALWAYS false for every result. A result that sets
#     it true is rejected (anti fake-green).
#   * "upload artifact succeeded" is NOT a success signal; success is the
#     step_status values only.
#   * An opt-in / self-hosted result that is skipped MUST carry a skip_reason.
#   * A result that claims build passed MUST carry an artifact SHA-256.

import datetime
import re

RESULT_SCHEMA = "virtualworld.ci-result/1"

# Steps we track per task.
STEPS = ("build", "test", "package", "conformance")

# Allowed per-step / per-task outcome vocabulary.
ALLOWED_STEP_STATUSES = ("passed", "failed", "skipped", "cancelled", "not_run", "error")
ALLOWED_TASK_STATUSES = ("passed", "failed", "skipped", "cancelled")

# Map GitHub Actions step/job outcomes to our vocabulary.
_GH_OUTCOME_MAP = {
    "success": "passed",
    "failure": "failed",
    "cancelled": "cancelled",
    "skipped": "skipped",
    "neutral": "not_run",
    "action_required": "error",
}

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# Stable, machine-readable error / rejection codes.
ERROR_CODES = {
    "RESULT_SCHEMA_MISMATCH": "result.schema is not virtualworld.ci-result/1",
    "RESULT_MISSING_FIELD": "required string field missing or empty",
    "RESULT_UNKNOWN_ARCH": "arch is not in the recognised set",
    "RESULT_RUNNER_TYPE": "runner_type is not hosted|self-hosted",
    "RESULT_CONTRIB": "contrib is not ON|OFF",
    "RESULT_UNKNOWN_STATUS": "step/task status is not in the allowed vocabulary",
    "RESULT_HASH_MALFORMED": "artifact_sha256 is not a 64-char hex string",
    "RESULT_HASH_MISSING": "build passed but artifact_sha256 is missing",
    "RESULT_REAL_HW_MISLABEL": "real_hardware_verified must be false",
    "RESULT_SKIP_WITHOUT_REASON": "self-hosted/opt-in skipped without a skip_reason",
    "RESULT_STATUS_MISMATCH": "declared status disagrees with derived step status",
    "RESULT_STEP_STATUS_MISSING": "step_status missing a required step",
    "RESULT_BAD_TIMESTAMP": "started_at / finished_at missing or invalid",
    "RESULT_COMMAND_SUMMARY": "allowed_command_summary must be a list",
    "AGG_CROSS_RUN_ID": "results mix more than one run_id",
    "AGG_CROSS_COMMIT": "results mix more than one commit_sha",
    "AGG_DUPLICATE_COMBO": "duplicate host/arch/contrib combination",
    "AGG_MISSING_REQUIRED": "a required (hosted) host/arch/contrib result is absent",
    "AGG_STALE_TIMESTAMP": "a result is outside the freshness window",
    "AGG_HASH_DUPLICATE": "two results share the same artifact_sha256",
    "AGG_TASK_FAILED": "a required task ended failed/cancelled/error",
}


def normalize_status(raw):
    """Map a GitHub outcome (or our own vocabulary) to our status token."""
    if raw is None:
        return "error"
    r = str(raw).lower()
    if r in _GH_OUTCOME_MAP:
        return _GH_OUTCOME_MAP[r]
    if r in ALLOWED_STEP_STATUSES:
        return r
    return "error"


def derive_status(step_status):
    """Derive the overall task status from its step outcomes."""
    if not isinstance(step_status, dict):
        return "error"
    values = [step_status.get(s) for s in STEPS]
    if any(v in ("failed", "error") for v in values):
        return "failed"
    if any(v == "cancelled" for v in values):
        return "cancelled"
    if all(v == "skipped" for v in values):
        return "skipped"
    if all(v == "passed" for v in values):
        return "passed"
    return "not_run"


def validate_result(r):
    """Validate one result dict. Returns a list of (code, message) errors."""
    errors = []
    if not isinstance(r, dict):
        return [("RESULT_SCHEMA_MISMATCH", "result is not an object")]

    if r.get("schema") != RESULT_SCHEMA:
        errors.append(("RESULT_SCHEMA_MISMATCH", "got %r" % r.get("schema")))

    for f in ("run_id", "commit_sha", "host", "arch",
              "runner_type", "job_name", "contrib"):
        v = r.get(f)
        if not isinstance(v, str) or not v:
            errors.append(("RESULT_MISSING_FIELD", f))

    # arch / runner_type / contrib vocabulary.
    from matrix import KNOWN_ARCHES  # local import avoids a hard cycle at import
    if r.get("arch") not in KNOWN_ARCHES:
        errors.append(("RESULT_UNKNOWN_ARCH", str(r.get("arch"))))
    if r.get("runner_type") not in ("hosted", "self-hosted"):
        errors.append(("RESULT_RUNNER_TYPE", str(r.get("runner_type"))))
    if r.get("contrib") not in ("ON", "OFF"):
        errors.append(("RESULT_CONTRIB", str(r.get("contrib"))))

    # step_status object + vocabulary.
    ss = r.get("step_status")
    if not isinstance(ss, dict):
        errors.append(("RESULT_STEP_STATUS_MISSING", "step_status not an object"))
    else:
        for step in STEPS:
            st = ss.get(step)
            if st not in ALLOWED_STEP_STATUSES:
                errors.append(("RESULT_UNKNOWN_STATUS",
                               "%s=%r" % (step, st)))

    # artifact hash: optional, but must be well-formed when present, and present
    # when build passed.
    ah = r.get("artifact_sha256")
    if ah is not None:
        if not isinstance(ah, str) or not _HEX64.match(ah):
            errors.append(("RESULT_HASH_MALFORMED", str(ah)))
    if isinstance(ss, dict) and ss.get("build") == "passed" and not ah:
        errors.append(("RESULT_HASH_MISSING", "build passed, no artifact hash"))

    # real hardware must never be claimed.
    if r.get("real_hardware_verified") is not False:
        errors.append(("RESULT_REAL_HW_MISLABEL",
                       "real_hardware_verified=%r" % r.get("real_hardware_verified")))

    # opt-in / self-hosted skipped MUST explain why.
    status = r.get("status")
    if (r.get("runner_type") == "self-hosted"
            and status in ("skipped", "cancelled")
            and not r.get("skip_reason")):
        errors.append(("RESULT_SKIP_WITHOUT_REASON", r.get("job_name")))

    # timestamps present and parseable.
    for t in ("started_at", "finished_at"):
        tv = r.get(t)
        if not isinstance(tv, str) or not tv:
            errors.append(("RESULT_BAD_TIMESTAMP", t))
        else:
            try:
                datetime.datetime.strptime(tv, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                errors.append(("RESULT_BAD_TIMESTAMP", "%s=%r" % (t, tv)))

    # allowed_command_summary must be a list.
    if not isinstance(r.get("allowed_command_summary"), list):
        errors.append(("RESULT_COMMAND_SUMMARY", "not a list"))

    # declared status consistency (anti fake-green): if provided, it must agree
    # with the status derived from the steps.
    if status in ALLOWED_TASK_STATUSES:
        derived = derive_status(ss) if isinstance(ss, dict) else "error"
        if status != derived and derived != "not_run":
            errors.append(("RESULT_STATUS_MISMATCH",
                           "declared=%s derived=%s" % (status, derived)))

    return errors


def build_result(schema=RESULT_SCHEMA, run_id="", commit_sha="", host="", arch="",
                runner_type="hosted", job_name="verify", contrib="ON",
                build="passed", test="passed", package=None,
                conformance="passed", artifact_sha256=None,
                started_at="", finished_at="", real_hardware_verified=False,
                skip_reason="", allowed_command_summary=None):
    """Construct a result dict with normalised step statuses (for tests/emitter)."""
    if package is None:
        package = test  # package gate rides with the ctest step
    step_status = {
        "build": normalize_status(build),
        "test": normalize_status(test),
        "package": normalize_status(package),
        "conformance": normalize_status(conformance),
    }
    status = derive_status(step_status)
    if allowed_command_summary is None:
        allowed_command_summary = []
    return {
        "schema": schema,
        "run_id": run_id,
        "commit_sha": commit_sha,
        "host": host,
        "arch": arch,
        "runner_type": runner_type,
        "job_name": job_name,
        "contrib": contrib,
        "step_status": step_status,
        "status": status,
        "allowed_command_summary": allowed_command_summary,
        "artifact_sha256": artifact_sha256,
        "started_at": started_at,
        "finished_at": finished_at,
        "real_hardware_verified": real_hardware_verified,
        "skip_reason": skip_reason,
    }
