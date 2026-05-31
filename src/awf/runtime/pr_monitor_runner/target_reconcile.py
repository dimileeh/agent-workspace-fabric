"""Post-merge target reconciliation payload helpers."""

from __future__ import annotations

from collections.abc import Mapping


def _target_reconcile_payload(result: object) -> dict[str, object]:
    if isinstance(result, dict):
        return dict(result)
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, dict):
            return dict(payload)
    return {"result": str(result)}


def _target_reconcile_log_fields(payload: Mapping[str, object]) -> dict[str, object]:
    fields = dict(payload)
    fields.setdefault("resolver_results", [])
    fields.setdefault("commit_sha", None)
    fields.setdefault("pushed", False)
    fields.setdefault("changed_paths", [])
    fields.setdefault("dry_run", None)
    fields.setdefault("commit_allowed", None)
    fields.setdefault("policy_reason_code", None)
    return fields


def _target_reconcile_failure_payload(
    exc: Exception,
    *,
    error_limit: int,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "failed",
        "reason_code": "TARGET_BRANCH_RECONCILE_FAILED",
        "error": str(exc)[:error_limit],
        "error_type": type(exc).__name__,
        "resolver_results": [],
        "commit_sha": None,
        "pushed": False,
        "changed_paths": [],
        "dry_run": None,
        "commit_allowed": None,
        "policy_reason_code": None,
    }

    operation = getattr(exc, "operation", None)
    if isinstance(operation, str):
        payload["operation"] = operation
    result = getattr(exc, "result", None)
    returncode = getattr(result, "returncode", None)
    if isinstance(returncode, int):
        payload["returncode"] = returncode
    reason_code = getattr(result, "reason_code", None)
    if isinstance(reason_code, str):
        payload["command_reason_code"] = reason_code
    stderr = getattr(result, "stderr", None)
    if isinstance(stderr, str) and stderr:
        payload["stderr"] = stderr[:error_limit]
    stdout = getattr(result, "stdout", None)
    if isinstance(stdout, str) and stdout:
        payload["stdout"] = stdout[:error_limit]
    return payload


def _truncate_target_reconcile_failure_payload(
    payload: Mapping[str, object],
    *,
    error_limit: int,
) -> dict[str, object]:
    truncated = dict(payload)
    for key in ("error", "stderr", "stdout"):
        value = truncated.get(key)
        if isinstance(value, str):
            truncated[key] = value[:error_limit]
    return truncated
