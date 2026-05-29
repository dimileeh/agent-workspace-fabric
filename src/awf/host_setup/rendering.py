"""Shared first-run setup/start rendering helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, ClassVar, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from awf.common.audit import REDACTION_MARKER, redact_audit_value
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
    # Match from the start of a URI-like scheme token so hyphen-prefixed
    # contexts like x-plain-file:// redact as a whole, while safeplain-file://
    # remains ordinary text.
    r"(?<![A-Za-z0-9_-])"
    r"(?:[A-Za-z][A-Za-z0-9+.-]*-)?(?:keyring|env|plain-file)://"
    r"[^\s\"'`,;)}\]]+",
    re.IGNORECASE,
)
_PROVIDER_REF_KEY_RE = re.compile(
    r"(?:credential|provider)[_-]refs?",
    re.IGNORECASE,
)
_PROVIDER_REF_KEY_SUFFIX_RE = re.compile(
    r"(?P<base>(?:credential|provider)[_-]refs?)(?P<separator>[_:-])(?P<suffix>.+)",
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
    """Build a first-run warning payload with one catalog-backed issue.

    ``details`` are attached to the single issue (``issues[0].details``),
    not to the top-level payload, unlike ``first_run_success_payload``.
    """
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
    """Build a blocked/failed first-run payload with one catalog-backed issue.

    ``details`` are attached to the single issue (``issues[0].details``),
    not to the top-level payload, unlike ``first_run_success_payload``.
    """
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
    raw_payload = payload.model_dump(mode="python", exclude_none=True)
    # Pydantic dumps BaseModel layers into fresh dicts, so optional wrapper-key
    # cleanup below cannot mutate the frozen model. mode="python" can preserve
    # user-supplied details mappings by reference, so this cleanup only removes
    # wrapper keys and never mutates details mapping contents.
    if raw_payload.get("details") == {}:
        raw_payload.pop("details")
    if raw_payload.get("next_steps") in ([], ()):
        raw_payload.pop("next_steps")
    # Keep issues present, even when empty, so success payload consumers can
    # rely on one stable field shape across all first-run statuses.
    issues = raw_payload.get("issues")
    if issues is None:
        issues = ()
    if not isinstance(issues, tuple):
        raise TypeError(
            "FirstRunPayload.model_dump(mode='python') must preserve issues as a tuple."
        )
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        if issue.get("details") == {}:
            issue.pop("details")
        remediation = issue.get("remediation")
        if isinstance(remediation, dict):
            if remediation.get("next_steps") in ([], ()):
                remediation.pop("next_steps")
            # None is already excluded by exclude_none=True above.
            # str_strip_whitespace can normalise whitespace-only values to "".
            if remediation.get("related_command") == "":
                remediation.pop("related_command")
    redacted_payload = redact_first_run_value(raw_payload)
    return cast(dict[str, Any], _json_safe_first_run_value(redacted_payload))


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
    audit_redacted = redact_audit_value(value, preserve_tuples=True)
    return _redact_provider_refs(audit_redacted)


def _json_safe_first_run_value(value: Any) -> Any:
    """Return a JSON-compatible first-run value after redaction."""
    if isinstance(value, Mapping):
        rendered: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            # Provider-ref redaction already preserves sensitive string-key
            # collisions. This final pass covers JSON key coercion collisions
            # for direct helper callers and future non-string upstream shapes.
            rendered_key = _deduplicate_redacted_mapping_key(rendered, key_text)
            rendered[rendered_key] = _json_safe_first_run_value(item)
        return rendered
    if isinstance(value, (list, tuple)):
        return [_json_safe_first_run_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_json_safe_first_run_value(item) for item in sorted(value, key=str)]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return redact_first_run_value(str(value))


def _redact_provider_refs(value: Any) -> Any:
    """Recursively redact provider credential references from arbitrary values."""
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = _redact_first_run_mapping_key(key)
            # This pass preserves collisions introduced by content redaction in
            # mapping keys; JSON-safe rendering has its own coercion guard.
            redacted_key = _deduplicate_redacted_mapping_key(redacted, key_text)
            if _is_provider_ref_key(key_text):
                redacted[redacted_key] = REDACTION_MARKER
            else:
                redacted[redacted_key] = _redact_provider_refs(item)
        return redacted
    if isinstance(value, list):
        return [_redact_provider_refs(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_provider_refs(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return [_redact_provider_refs(item) for item in sorted(value, key=str)]
    if isinstance(value, str):
        return _PROVIDER_REF_RE.sub(REDACTION_MARKER, value)
    return value


def _redact_first_run_mapping_key(key: Any) -> str:
    """Return a string mapping key with sensitive first-run content redacted."""
    key_text = str(key)
    audit_redacted = redact_audit_value(key_text, preserve_tuples=True)
    provider_redacted = _PROVIDER_REF_RE.sub(REDACTION_MARKER, cast(str, audit_redacted))
    return _redact_provider_ref_key_token_suffix(provider_redacted)


def _redact_provider_ref_key_token_suffix(key: str) -> str:
    """Redact token-like suffixes appended to explicit provider-ref keys."""
    match = _PROVIDER_REF_KEY_SUFFIX_RE.fullmatch(key)
    if match is None or not _is_sensitive_provider_ref_key_suffix(match.group("suffix")):
        return key
    return f"{match.group('base')}{match.group('separator')}{REDACTION_MARKER}"


def _is_sensitive_provider_ref_key_suffix(suffix: str) -> bool:
    """Return whether a provider-ref key suffix is already or should be redacted."""
    if suffix == REDACTION_MARKER:
        return True
    audit_redacted = redact_audit_value(suffix, preserve_tuples=True)
    provider_redacted = _PROVIDER_REF_RE.sub(REDACTION_MARKER, cast(str, audit_redacted))
    return provider_redacted == REDACTION_MARKER


def _deduplicate_redacted_mapping_key(mapping: Mapping[str, Any], key: str) -> str:
    """Return a rendered mapping key that preserves redacted-key collisions.

    Suffixes colliding keys as ``key#2``, ``key#3``, skipping any slot already
    occupied by a non-collision entry in the mapping.
    """
    if key not in mapping:
        return key
    suffix = 2
    while f"{key}#{suffix}" in mapping:
        suffix += 1
    return f"{key}#{suffix}"


def _is_provider_ref_key(key: str) -> bool:
    """Return whether a mapping key denotes provider credential references."""
    if _PROVIDER_REF_KEY_RE.fullmatch(key):
        return True
    suffix_match = _PROVIDER_REF_KEY_SUFFIX_RE.fullmatch(key)
    if suffix_match is None:
        return False
    return _is_sensitive_provider_ref_key_suffix(suffix_match.group("suffix"))


def _render_issue_lines(issue: Mapping[str, Any]) -> list[str]:
    """Render one first-run issue as pretty output lines."""
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
            lines.append("Remediation Next:")
            lines.extend(f"  - {step}" for step in issue_next_steps)

    details = issue.get("details")
    if isinstance(details, Mapping) and details:
        lines.append("Details:")
        lines.extend(_render_mapping_lines(details))
    return lines


def _render_mapping_lines(mapping: Mapping[str, Any], *, prefix: str = "  ") -> list[str]:
    """Render mapping details as sorted, nested pretty output lines."""
    lines: list[str] = []
    for key in sorted(mapping):
        value = mapping[key]
        if isinstance(value, Mapping) and value:
            lines.append(f"{prefix}{key}:")
            lines.extend(_render_mapping_lines(value, prefix=f"{prefix}  "))
            continue
        if isinstance(value, (list, tuple)) and value:
            lines.append(f"{prefix}{key}:")
            lines.extend(_render_sequence_lines(value, prefix=f"{prefix}  "))
            continue
        lines.append(f"{prefix}{key}: {_format_pretty_value(value)}")
    return lines


def _render_sequence_lines(sequence: list[Any] | tuple[Any, ...], *, prefix: str) -> list[str]:
    """Render sequence details as nested pretty output lines."""
    lines: list[str] = []
    for item in sequence:
        if isinstance(item, Mapping):
            if item:
                lines.append(f"{prefix}-")
                lines.extend(_render_mapping_lines(item, prefix=f"{prefix}  "))
            else:
                lines.append(f"{prefix}- {{}}")
            continue
        if isinstance(item, (list, tuple)):
            if item:
                lines.append(f"{prefix}-")
                lines.extend(_render_sequence_lines(item, prefix=f"{prefix}  "))
            else:
                lines.append(f"{prefix}- []")
            continue
        lines.append(f"{prefix}- {_format_pretty_value(item)}")
    return lines


def _format_pretty_value(value: Any) -> str:
    """Return a stable scalar representation for pretty output."""
    if isinstance(value, (bool, int, float, list, tuple, dict)) or value is None:
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
