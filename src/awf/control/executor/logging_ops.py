"""Logging operations, validation evidence formatting, and audit/redaction helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from awf.common.audit import redact_audit_value

SETUP_DEPENDENCY_NETWORK_FAILURE = "SETUP_DEPENDENCY_NETWORK_FAILURE"
SETUP_DEPENDENCY_NETWORK_METADATA_KEY = "setup_dependency_network"


def _validation_run_log_stream_refs(
    command_records: list[dict[str, Any]],
) -> dict[str, list[dict[str, str | None]]]:
    refs: list[dict[str, str | None]] = []
    for command in command_records:
        stream_ids = command.get("stream_ids")
        if not isinstance(stream_ids, dict):
            refs.append({"stdout": None, "stderr": None})
            continue
        stdout = stream_ids.get("stdout")
        stderr = stream_ids.get("stderr")
        refs.append(
            {
                "stdout": stdout if isinstance(stdout, str) else None,
                "stderr": stderr if isinstance(stderr, str) else None,
            }
        )
    return {"commands": refs}


def _setup_dependency_network_details(first_fail: object | None) -> dict[str, Any] | None:
    metadata = getattr(first_fail, "metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    details = metadata.get(SETUP_DEPENDENCY_NETWORK_METADATA_KEY)
    if not isinstance(details, Mapping):
        return None
    return cast(dict[str, Any] | None, redact_audit_value(dict(details)))


def _setup_dependency_network_failure_details(
    first_fail: object | None,
) -> dict[str, Any] | None:
    if getattr(first_fail, "reason_code", None) != SETUP_DEPENDENCY_NETWORK_FAILURE:
        return None
    return _setup_dependency_network_details(first_fail)


def _setup_dependency_network_event_payload(
    details: Mapping[str, Any],
    *,
    reason_code: str,
) -> dict[str, Any]:
    payload = dict(details)
    payload["reason_code"] = reason_code
    payload["failure_reason_code"] = SETUP_DEPENDENCY_NETWORK_FAILURE
    return payload


def _format_duration_for_message(value: int | float) -> str:
    return f"{value:g}"


_VALIDATION_EVIDENCE_RETAINED_KEY_COUNT = 5


def _validation_evidence_size_summary(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        retained_keys = [str(key) for key in list(value)[:_VALIDATION_EVIDENCE_RETAINED_KEY_COUNT]]
        return {
            "truncated": True,
            "original_type": "mapping",
            "original_entry_count": len(value),
            "retained_keys": retained_keys,
        }
    if isinstance(value, list):
        return {
            "truncated": True,
            "original_type": "list",
            "original_length": len(value),
        }
    if isinstance(value, str):
        return {
            "truncated": True,
            "original_type": "string",
            "original_length": len(value),
        }
    return {
        "truncated": True,
        "original_type": type(value).__name__,
    }
