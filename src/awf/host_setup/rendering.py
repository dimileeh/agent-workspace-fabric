"""Shared first-run setup/start rendering helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, ClassVar, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from awf.common.audit import REDACTION_MARKER, redact_audit_text, redact_audit_value
from awf.host_setup.config import (
    HOST_SETUP_CONFIG_CORRUPT,
    HOST_SETUP_CONFIG_SECRET_VALUE,
    HOST_SETUP_CONFIG_WRITE_FAILED,
)
from awf.host_setup.source_assets import (
    SOURCE_CHECKOUT_ASSETS_STALE,
    SOURCE_CHECKOUT_INVALID,
)
from awf.service.doctor.reasons import reason_text_for_code

AWF_SETUP_PLACEHOLDER = "AWF_SETUP_PLACEHOLDER"
AWF_START_PLACEHOLDER = "AWF_START_PLACEHOLDER"

SETUP_READINESS_FAILED = "SETUP_READINESS_FAILED"
SETUP_PROVIDER_UNKNOWN = "SETUP_PROVIDER_UNKNOWN"
INTERACTIVE_INPUT_REQUIRED = "INTERACTIVE_INPUT_REQUIRED"
CREDENTIAL_BACKEND_UNAVAILABLE = "CREDENTIAL_BACKEND_UNAVAILABLE"
CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED = "CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED"
CREDENTIAL_REF_INVALID = "CREDENTIAL_REF_INVALID"
PROVIDER_SETUP_AUTH_INVALID = "PROVIDER_SETUP_AUTH_INVALID"
CLIENT_CONFIG_CONFLICT = "CLIENT_CONFIG_CONFLICT"
CLIENT_CONFIG_WRITE_FAILED = "CLIENT_CONFIG_WRITE_FAILED"
INSTALLER_UNSUPPORTED_PLATFORM = "INSTALLER_UNSUPPORTED_PLATFORM"
INSTALLER_DEPENDENCY_MISSING = "INSTALLER_DEPENDENCY_MISSING"
INSTALLER_CHECKSUM_MISMATCH = "INSTALLER_CHECKSUM_MISMATCH"
INSTALLER_METHOD_FAILED = "INSTALLER_METHOD_FAILED"
INSTALLER_PATH_NOT_REACHABLE = "INSTALLER_PATH_NOT_REACHABLE"
START_COMPOSE_ASSETS_MISSING = "START_COMPOSE_ASSETS_MISSING"
START_PORT_CONFLICT = "START_PORT_CONFLICT"
START_MIGRATION_FAILED = "START_MIGRATION_FAILED"
START_HEALTH_TIMEOUT = "START_HEALTH_TIMEOUT"

FIRST_RUN_SETUP_REASON_CODES: tuple[str, ...] = (
    AWF_SETUP_PLACEHOLDER,
    HOST_SETUP_CONFIG_CORRUPT,
    HOST_SETUP_CONFIG_SECRET_VALUE,
    HOST_SETUP_CONFIG_WRITE_FAILED,
    SETUP_READINESS_FAILED,
    SETUP_PROVIDER_UNKNOWN,
    INTERACTIVE_INPUT_REQUIRED,
)
FIRST_RUN_SOURCE_CHECKOUT_REASON_CODES: tuple[str, ...] = (
    SOURCE_CHECKOUT_INVALID,
    SOURCE_CHECKOUT_ASSETS_STALE,
)
FIRST_RUN_CREDENTIAL_REASON_CODES: tuple[str, ...] = (
    CREDENTIAL_BACKEND_UNAVAILABLE,
    CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED,
    CREDENTIAL_REF_INVALID,
    PROVIDER_SETUP_AUTH_INVALID,
)
FIRST_RUN_CLIENT_REASON_CODES: tuple[str, ...] = (
    CLIENT_CONFIG_CONFLICT,
    CLIENT_CONFIG_WRITE_FAILED,
)
FIRST_RUN_INSTALLER_REASON_CODES: tuple[str, ...] = (
    INSTALLER_UNSUPPORTED_PLATFORM,
    INSTALLER_DEPENDENCY_MISSING,
    INSTALLER_CHECKSUM_MISMATCH,
    INSTALLER_METHOD_FAILED,
    INSTALLER_PATH_NOT_REACHABLE,
)
FIRST_RUN_START_REASON_CODES: tuple[str, ...] = (
    AWF_START_PLACEHOLDER,
    START_COMPOSE_ASSETS_MISSING,
    START_PORT_CONFLICT,
    START_MIGRATION_FAILED,
    START_HEALTH_TIMEOUT,
)
FIRST_RUN_FAILURE_REASON_CODES: tuple[str, ...] = (
    *FIRST_RUN_SETUP_REASON_CODES,
    *FIRST_RUN_SOURCE_CHECKOUT_REASON_CODES,
    *FIRST_RUN_CREDENTIAL_REASON_CODES,
    *FIRST_RUN_CLIENT_REASON_CODES,
    *FIRST_RUN_INSTALLER_REASON_CODES,
    *FIRST_RUN_START_REASON_CODES,
)

FirstRunStatus = Literal["success", "warning", "blocked", "failed"]
FirstRunSeverity = Literal["info", "warning", "blocked", "failed"]

_PROVIDER_REF_RE = re.compile(
    r"\b(?:keyring|env|plain-file)://[^\s\"'`,;)}\]]+",
    re.IGNORECASE,
)
_PROVIDER_REF_KEY_RE = re.compile(
    r"(?:^|[_-])(?:credential|provider)[_-]?refs?(?:$|[_-])",
    re.IGNORECASE,
)


class _FirstRunBaseModel(BaseModel):
    """Shared strict, immutable Pydantic base for first-run payloads."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
        hide_input_in_errors=True,
    )


class FirstRunRemediation(_FirstRunBaseModel):
    """Operator remediation text for a reason-coded first-run issue."""

    problem: str = Field(min_length=1)
    cause: str = Field(min_length=1)
    fix: str = Field(min_length=1)
    docs_link: str = Field(min_length=1)
    related_command: str | None = None
    next_steps: tuple[str, ...] = ()


class FirstRunIssue(_FirstRunBaseModel):
    """A single reason-coded first-run issue."""

    reason_code: str = Field(min_length=1)
    severity: FirstRunSeverity
    remediation: FirstRunRemediation
    details: Mapping[str, Any] = Field(default_factory=dict)


class FirstRunPayload(_FirstRunBaseModel):
    """JSON-safe first-run command result."""

    status: FirstRunStatus
    command: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    reason_code: str | None = None
    issues: tuple[FirstRunIssue, ...] = ()
    details: Mapping[str, Any] = Field(default_factory=dict)
    next_steps: tuple[str, ...] = ()


def first_run_remediation_from_reason_code(
    reason_code: str,
    *,
    problem: str | None = None,
    cause: str | None = None,
    fix: str | None = None,
    docs_link: str | None = None,
    related_command: str | None = None,
    next_steps: tuple[str, ...] = (),
) -> FirstRunRemediation:
    """Build remediation from the doctor reason catalog with optional overrides."""
    reason = reason_text_for_code(reason_code)
    if reason is None:
        raise ValueError(f"Unknown first-run reason code: {reason_code}")
    return FirstRunRemediation(
        problem=problem or reason.message,
        cause=cause or reason.likely_cause,
        fix=fix or reason.action,
        docs_link=docs_link or reason.docs_link,
        related_command=related_command if related_command is not None else reason.related_command,
        next_steps=next_steps,
    )


def first_run_issue_from_reason_code(
    reason_code: str,
    *,
    severity: FirstRunSeverity = "failed",
    details: Mapping[str, Any] | None = None,
    problem: str | None = None,
    cause: str | None = None,
    fix: str | None = None,
    docs_link: str | None = None,
    related_command: str | None = None,
    next_steps: tuple[str, ...] = (),
) -> FirstRunIssue:
    """Build a redaction-ready first-run issue from a reason code."""
    return FirstRunIssue(
        reason_code=reason_code,
        severity=severity,
        remediation=first_run_remediation_from_reason_code(
            reason_code,
            problem=problem,
            cause=cause,
            fix=fix,
            docs_link=docs_link,
            related_command=related_command,
            next_steps=next_steps,
        ),
        details=dict(details or {}),
    )


def first_run_success_payload(
    *,
    command: str,
    summary: str,
    details: Mapping[str, Any] | None = None,
    next_steps: tuple[str, ...] = (),
) -> FirstRunPayload:
    """Build a successful first-run command payload."""
    return FirstRunPayload(
        status="success",
        command=command,
        summary=summary,
        details=dict(details or {}),
        next_steps=next_steps,
    )


def first_run_warning_payload(
    *,
    command: str,
    reason_code: str,
    summary: str,
    details: Mapping[str, Any] | None = None,
    next_steps: tuple[str, ...] = (),
) -> FirstRunPayload:
    """Build a first-run warning payload with one catalog-backed issue."""
    issue = first_run_issue_from_reason_code(
        reason_code,
        severity="warning",
        details=details,
    )
    return FirstRunPayload(
        status="warning",
        command=command,
        summary=summary,
        reason_code=reason_code,
        issues=(issue,),
        next_steps=next_steps,
    )


def first_run_failure_payload(
    *,
    command: str,
    reason_code: str,
    summary: str,
    status: Literal["blocked", "failed"] = "failed",
    details: Mapping[str, Any] | None = None,
    next_steps: tuple[str, ...] = (),
) -> FirstRunPayload:
    """Build a blocked/failed first-run payload with one catalog-backed issue."""
    issue = first_run_issue_from_reason_code(
        reason_code,
        severity=status,
        details=details,
    )
    return FirstRunPayload(
        status=status,
        command=command,
        summary=summary,
        reason_code=reason_code,
        issues=(issue,),
        next_steps=next_steps,
    )


def render_first_run_json(payload: FirstRunPayload) -> dict[str, Any]:
    """Return a JSON-safe, redacted first-run payload dictionary."""
    raw_payload = payload.model_dump(mode="json", exclude_none=True)
    if raw_payload.get("details") == {}:
        raw_payload.pop("details")
    if raw_payload.get("next_steps") == []:
        raw_payload.pop("next_steps")
    for issue in raw_payload.get("issues", []):
        if not isinstance(issue, dict):
            continue
        if issue.get("details") == {}:
            issue.pop("details")
        remediation = issue.get("remediation")
        if isinstance(remediation, dict) and remediation.get("next_steps") == []:
            remediation.pop("next_steps")
    return cast(dict[str, Any], redact_first_run_value(raw_payload))


def render_first_run_pretty(payload: FirstRunPayload) -> str:
    """Render a concise, redacted operator-facing first-run panel."""
    rendered = render_first_run_json(payload)
    lines = [
        f"Status: {rendered['status']}",
        f"Command: {rendered['command']}",
        f"Summary: {rendered['summary']}",
    ]
    issues = rendered.get("issues")
    reason_code = rendered.get("reason_code")
    # Helper-built warning/failure payloads render reasons per issue below.
    # Preserve this fallback for direct FirstRunPayload callers without issues.
    if reason_code and not issues:
        lines.append(f"Reason: {reason_code}")

    details = rendered.get("details")
    if isinstance(details, Mapping) and details:
        lines.append("Details:")
        lines.extend(_render_mapping_lines(details))

    if isinstance(issues, list):
        for issue in issues:
            if not isinstance(issue, Mapping):
                continue
            lines.extend(_render_issue_lines(issue))

    next_steps = rendered.get("next_steps")
    if isinstance(next_steps, list) and next_steps:
        lines.append("Next:")
        lines.extend(f"  - {step}" for step in next_steps)

    return "\n".join(lines)


def redact_first_run_value(value: Any) -> Any:
    """Recursively redact tokens and provider refs from first-run output values."""
    provider_redacted = _redact_provider_refs(value)
    return redact_audit_value(provider_redacted, preserve_tuples=True)


def _redact_provider_refs(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_provider_ref_key(key_text):
                redacted[key_text] = REDACTION_MARKER
            else:
                redacted[key_text] = _redact_provider_refs(item)
        return redacted
    if isinstance(value, list):
        return [_redact_provider_refs(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_provider_refs(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return [_redact_provider_refs(item) for item in sorted(value, key=str)]
    if isinstance(value, str):
        return redact_audit_text(_PROVIDER_REF_RE.sub(REDACTION_MARKER, value))
    return value


def _is_provider_ref_key(key: str) -> bool:
    return bool(_PROVIDER_REF_KEY_RE.search(key))


def _render_issue_lines(issue: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    reason_code = issue.get("reason_code")
    if reason_code:
        lines.append(f"Reason: {reason_code}")
    severity = issue.get("severity")
    if severity:
        lines.append(f"Severity: {severity}")

    remediation = issue.get("remediation")
    if isinstance(remediation, Mapping):
        if problem := remediation.get("problem"):
            lines.append(f"Problem: {problem}")
        if cause := remediation.get("cause"):
            lines.append(f"Cause: {cause}")
        if fix := remediation.get("fix"):
            lines.append(f"Fix: {fix}")
        if docs_link := remediation.get("docs_link"):
            lines.append(f"Docs: {docs_link}")
        if related := remediation.get("related_command"):
            lines.append(f"Related Command: {related}")
        issue_next_steps = remediation.get("next_steps")
        if isinstance(issue_next_steps, list) and issue_next_steps:
            lines.append("Next:")
            lines.extend(f"  - {step}" for step in issue_next_steps)

    details = issue.get("details")
    if isinstance(details, Mapping) and details:
        lines.append("Details:")
        lines.extend(_render_mapping_lines(details))
    return lines


def _render_mapping_lines(mapping: Mapping[str, Any], *, prefix: str = "  ") -> list[str]:
    lines: list[str] = []
    for key in sorted(mapping):
        value = mapping[key]
        if isinstance(value, Mapping):
            lines.append(f"{prefix}{key}:")
            lines.extend(_render_mapping_lines(value, prefix=f"{prefix}  "))
            continue
        lines.append(f"{prefix}{key}: {_format_pretty_value(value)}")
    return lines


def _format_pretty_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return json.dumps(value, sort_keys=True)
    return str(value)


__all__ = [
    "AWF_SETUP_PLACEHOLDER",
    "AWF_START_PLACEHOLDER",
    "CLIENT_CONFIG_CONFLICT",
    "CLIENT_CONFIG_WRITE_FAILED",
    "CREDENTIAL_BACKEND_UNAVAILABLE",
    "CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED",
    "CREDENTIAL_REF_INVALID",
    "FIRST_RUN_CLIENT_REASON_CODES",
    "FIRST_RUN_CREDENTIAL_REASON_CODES",
    "FIRST_RUN_FAILURE_REASON_CODES",
    "FIRST_RUN_INSTALLER_REASON_CODES",
    "FIRST_RUN_SETUP_REASON_CODES",
    "FIRST_RUN_SOURCE_CHECKOUT_REASON_CODES",
    "FIRST_RUN_START_REASON_CODES",
    "INSTALLER_CHECKSUM_MISMATCH",
    "INSTALLER_DEPENDENCY_MISSING",
    "INSTALLER_METHOD_FAILED",
    "INSTALLER_PATH_NOT_REACHABLE",
    "INSTALLER_UNSUPPORTED_PLATFORM",
    "INTERACTIVE_INPUT_REQUIRED",
    "PROVIDER_SETUP_AUTH_INVALID",
    "SETUP_PROVIDER_UNKNOWN",
    "SETUP_READINESS_FAILED",
    "START_COMPOSE_ASSETS_MISSING",
    "START_HEALTH_TIMEOUT",
    "START_MIGRATION_FAILED",
    "START_PORT_CONFLICT",
    "FirstRunIssue",
    "FirstRunPayload",
    "FirstRunRemediation",
    "first_run_failure_payload",
    "first_run_issue_from_reason_code",
    "first_run_remediation_from_reason_code",
    "first_run_success_payload",
    "first_run_warning_payload",
    "redact_first_run_value",
    "render_first_run_json",
    "render_first_run_pretty",
]
