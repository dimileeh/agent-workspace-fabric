"""Structured control-plane audit payload helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

AUDIT_SCHEMA = "control_audit.v1"
REDACTION_MARKER = "[redacted]"

_MAX_STRING_LENGTH = 1000
_SENSITIVE_NON_TOKEN_KEY_RE = re.compile(
    r"(authorization|bearer|password|passwd|secret|api[_-]?key|access[_-]?key)",
    re.IGNORECASE,
)
_SENSITIVE_TOKEN_KEY_RE = re.compile(r"token", re.IGNORECASE)
_TOKEN_USAGE_METADATA_KEY_RE = re.compile(
    r"(?:^|[_-])(?:input|output|total|prompt|completion|cached|reasoning)"
    r"[_-]?tokens?(?:[_-](?:count|used|usage))?$"
    r"|(?:^|[_-])tokens?[_-](?:count|used|usage)$",
    re.IGNORECASE,
)
_URL_CREDENTIAL_RE = re.compile(
    r"(\b[a-z][a-z0-9+.-]*://)([^/\s:@]+(?::[^/\s@]+)?@)",
    re.IGNORECASE,
)
_AUTHORIZATION_RE = re.compile(
    r"(\bAuthorization\s*:\s*(?:Bearer|Basic)\s+)([A-Za-z0-9._~+/=\-]{8,})",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(\bBearer\s+)([A-Za-z0-9._~+/=\-]{8,})", re.IGNORECASE)
_TOKEN_ASSIGNMENT_RE = re.compile(
    r"\b(?P<key>"
    r"(?:[A-Za-z][A-Za-z0-9_]*_)?TOKEN"
    r"|(?:[A-Za-z][A-Za-z0-9_]*_)?(?:API[_-]?KEY|ACCESS[_-]?KEY)"
    r"|(?:AUTH|GITHUB|GH)[_-]?TOKEN"
    r"|PASSWORD|PASSWD|SECRET"
    r")\b"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<quote>[\"']?)"
    r"(?P<value>[^\s\"'`,;)}\]]+)"
    r"(?P=quote)",
    re.IGNORECASE,
)
_KNOWN_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])("
    r"gh[apousr]_[A-Za-z0-9_]{8,}|"
    r"github_pat_[A-Za-z0-9_]{8,}|"
    r"glpat-[A-Za-z0-9_-]{8,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|"
    r"sk-ant-[A-Za-z0-9_-]{8,}|"
    r"sk-proj-[A-Za-z0-9_-]{8,}|"
    r"sk-[A-Za-z0-9_-]{8,}|"
    r"AIza[A-Za-z0-9_-]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{8,}"
    r")(?![A-Za-z0-9_])"
)


def build_audit_payload(
    *,
    actor: str,
    action: str,
    outcome: str,
    reason_code: str,
    source: str | None = None,
    operation_id: str | None = None,
    operation_type: str | None = None,
    pr_number: int | None = None,
    pr_url: str | None = None,
    source_head_sha: str | None = None,
    source_base_sha: str | None = None,
    target_branch: str | None = None,
    remote_branch: str | None = None,
    branch_name: str | None = None,
    evidence: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the compact JSON payload stored on audit workspace events."""

    payload: dict[str, Any] = {
        "schema": AUDIT_SCHEMA,
        "actor": actor,
        "source": source or actor,
        "action": action,
        "outcome": outcome,
        "reason_code": reason_code,
        "operation_id": operation_id,
        "operation_type": operation_type,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "source_head_sha": source_head_sha,
        "source_base_sha": source_base_sha,
        "target_branch": target_branch,
        "remote_branch": remote_branch,
        "branch_name": branch_name,
    }
    if extra is not None:
        payload.update(dict(extra))
    if evidence is not None:
        payload["evidence"] = dict(evidence)
    return cast(dict[str, Any], _drop_none(redact_audit_value(payload)))


def redact_audit_value(value: Any, *, preserve_tuples: bool = False) -> Any:
    """Recursively redact token-like values while preserving usage metadata."""

    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                redacted[key_text] = REDACTION_MARKER
            else:
                redacted[key_text] = redact_audit_value(
                    item,
                    preserve_tuples=preserve_tuples,
                )
        return redacted
    if isinstance(value, list):
        return [redact_audit_value(item, preserve_tuples=preserve_tuples) for item in value]
    if isinstance(value, tuple):
        redacted_items = tuple(
            redact_audit_value(item, preserve_tuples=preserve_tuples) for item in value
        )
        if preserve_tuples:
            return redacted_items
        return list(redacted_items)
    if isinstance(value, (set, frozenset)):
        return [
            redact_audit_value(item, preserve_tuples=preserve_tuples)
            for item in sorted(value, key=str)
        ]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def redact_audit_text(value: str, *, limit: int = _MAX_STRING_LENGTH) -> str:
    """Redact token-like content from a durable diagnostic string."""

    return _redact_string(value, limit=limit)


def _is_sensitive_key(key: str) -> bool:
    if _SENSITIVE_NON_TOKEN_KEY_RE.search(key):
        return True
    return bool(
        _SENSITIVE_TOKEN_KEY_RE.search(key) and not _TOKEN_USAGE_METADATA_KEY_RE.search(key)
    )


def _redact_string(value: str, *, limit: int = _MAX_STRING_LENGTH) -> str:
    redacted = _URL_CREDENTIAL_RE.sub(r"\1" + REDACTION_MARKER + "@", value)
    redacted = _AUTHORIZATION_RE.sub(r"\1" + REDACTION_MARKER, redacted)
    redacted = _TOKEN_ASSIGNMENT_RE.sub(_redact_assignment, redacted)
    redacted = _BEARER_RE.sub(r"\1" + REDACTION_MARKER, redacted)
    redacted = _KNOWN_TOKEN_RE.sub(REDACTION_MARKER, redacted)
    if len(redacted) > limit:
        return f"{redacted[:limit]}...[truncated]"
    return redacted


def _redact_assignment(match: re.Match[str]) -> str:
    quote = match.group("quote")
    return f"{match.group('key')}{match.group('separator')}{quote}{REDACTION_MARKER}{quote}"


def _drop_none(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): cleaned
            for key, item in value.items()
            if (cleaned := _drop_none(item)) is not None
        }
    if isinstance(value, list):
        return [cleaned for item in value if (cleaned := _drop_none(item)) is not None]
    return value
