"""Error type, auth, and reason codes for :mod:`awf.common.bitbucket_client`.

Extracted from ``bitbucket_client.py`` so the client module stays under the
first-party file-size guardrail. These symbols form one cohesive unit — the
``ForgeClientError`` peer for BitBucket, the explicit credential mode, and the
stable reason codes that flow end-to-end (exception → log field → event →
policy). ``bitbucket_client`` re-exports them, so existing
``from awf.common.bitbucket_client import BitBucketClientError`` imports keep
working unchanged.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass

from awf.common.audit import redact_audit_text
from awf.common.forge_errors import ForgeClientError

# Reason codes (flow end-to-end: exception → log field → event → policy).
BITBUCKET_AUTH_NOT_CONFIGURED = "BITBUCKET_AUTH_NOT_CONFIGURED"
BITBUCKET_AUTH_FAILED = "BITBUCKET_AUTH_FAILED"
BITBUCKET_API_ERROR = "BITBUCKET_API_ERROR"
# Transport-level failure (connection reset/refused, timeout, DNS) with no HTTP
# status. Distinct from ``BITBUCKET_API_ERROR`` so the PR monitor can tell a
# recoverable network blip apart from a deterministic abort (pagination cap,
# SSRF origin guard) — those also carry ``status=None`` but must fail fast.
BITBUCKET_TRANSPORT_ERROR = "BITBUCKET_TRANSPORT_ERROR"
BITBUCKET_RATE_LIMITED = "BITBUCKET_RATE_LIMITED"
BITBUCKET_PIPELINE_FULL_RERUN = "BITBUCKET_PIPELINE_FULL_RERUN"
BITBUCKET_PIPELINE_NOT_RERUNNABLE = "BITBUCKET_PIPELINE_NOT_RERUNNABLE"
BITBUCKET_ISSUE_TRACKER_DISABLED = "BITBUCKET_ISSUE_TRACKER_DISABLED"
# Distinct from ``BITBUCKET_ISSUE_TRACKER_DISABLED`` (which the catalog documents as
# "note captured on the PR — no action required"). This code means deferred capture
# failed *entirely*: the issue tracker is disabled AND there was no remembered PR
# context to fall back to, so neither an issue nor a PR comment was recorded. Using
# the tracker-disabled code here would mislead operators into thinking the note was
# captured when nothing durable was written.
BITBUCKET_ISSUE_CAPTURE_FAILED = "BITBUCKET_ISSUE_CAPTURE_FAILED"
BITBUCKET_MERGE_METHOD_UNSUPPORTED = "BITBUCKET_MERGE_METHOD_UNSUPPORTED"
# A task-resolution PUT was rejected for lack of permission (403): the token cannot
# resolve reviewer tasks. Distinct from the generic ``BITBUCKET_AUTH_FAILED`` so the
# monitor surfaces it as a stable human blocker (NotifyHuman) — the agent addressing
# the task again cannot fix a missing scope, so a deterministic, reason-coded escalation
# avoids a retry storm. The task stays UNRESOLVED on BitBucket, so the merge gate keeps
# blocking until an operator grants the scope or resolves the task.
BITBUCKET_TASK_RESOLVE_FORBIDDEN = "BITBUCKET_TASK_RESOLVE_FORBIDDEN"
# BitBucket answers 409 Conflict on the merge POST when a merge for this PR is
# already in flight (typically a prior 202 async merge whose task-status poll was
# interrupted by a transient fault and re-issued by the monitor) or when the PR has
# already been merged. Both are recoverable — re-polling ``fetch_pr_status`` observes
# the original merge's eventual MERGED state — so they are classified transient
# rather than mapped to ``BITBUCKET_API_ERROR``. BitBucket also overloads 409 for
# non-recoverable merge failures (merge conflicts, unmet merge checks); those carry
# no in-progress/already-merged signal and must stay deterministic so they surface
# ``BITBUCKET_API_ERROR`` instead of polling forever — see
# ``_is_bitbucket_merge_in_progress_body``.
BITBUCKET_MERGE_IN_PROGRESS = "BITBUCKET_MERGE_IN_PROGRESS"
# The bounded async-merge poll budget was exhausted while the merge task was still
# PENDING. This does *not* mean the merge failed — BitBucket may still complete it
# server-side — so it is recoverable in exactly the same way as
# ``BITBUCKET_MERGE_IN_PROGRESS``: the monitor must wait and re-poll
# ``fetch_pr_status`` (observing the eventual MERGED state) rather than mark the
# operation failed and post a spurious "merge rejected" notification. Distinct from
# ``BITBUCKET_API_ERROR`` (which also carries ``status=None`` but is deterministic)
# so ``_is_transient_bitbucket_client_error`` and ``_attempt_merge_method`` can
# treat the timeout as an in-flight merge instead of a hard failure.
BITBUCKET_MERGE_TASK_TIMEOUT = "BITBUCKET_MERGE_TASK_TIMEOUT"
# The per-commit endpoint used to resolve an abbreviated head SHA to its full
# 40-char form returned a non-dict / missing / too-short ``hash`` (#477).
# Deterministic on purpose: a malformed resolve payload must NOT be retried (that
# would retry-storm the same bad response) and must NOT silently fall back to the
# abbreviated hash (that re-introduces the merge block this fix removes — the
# pre-merge validation gate matches the full 40-char ValidationRun head by exact
# equality). Genuine HTTP/transport faults still propagate from ``_request_json``
# with their existing transient codes, so a network blip routes through retry.
BITBUCKET_COMMIT_RESOLVE_FAILED = "BITBUCKET_COMMIT_RESOLVE_FAILED"
# Lowercase substrings that mark a 409 merge-POST body as the recoverable
# already-merged case rather than a non-recoverable conflict or merge-check
# rejection. Each is merge-specific. Matched case-insensitively against the
# (redacted) response body.
_BITBUCKET_MERGE_IN_PROGRESS_BODY_SIGNALS = (
    "being merged",
    "already merged",
    "already been merged",
)


def _is_bitbucket_merge_in_progress_body(body: str) -> bool:
    """Return whether a 409 merge-POST body indicates a recoverable in-flight merge.

    BitBucket reuses 409 for an in-progress async merge, an already-merged PR, and
    non-recoverable failures (conflicts, unmet merge checks). Only the first two are
    transient; the last must stay deterministic so the monitor notifies a human
    instead of polling indefinitely.

    A bare ``"in progress"`` substring is too broad: BitBucket overloads 409 on the
    merge POST for other concurrent-modification conflicts whose bodies read e.g.
    "another action is in progress" or "operation already in progress", and matching
    those would treat a deterministic conflict as transient and poll forever. Require
    the phrase to be about the *merge* itself ("a merge is already in progress") so a
    genuine in-flight async merge is recovered while unrelated concurrency conflicts
    stay deterministic.
    """
    lowered = body.lower()
    if any(signal in lowered for signal in _BITBUCKET_MERGE_IN_PROGRESS_BODY_SIGNALS):
        return True
    return "in progress" in lowered and "merge" in lowered


class BitBucketClientError(ForgeClientError):
    """Raised when BitBucket Cloud returns an error or an unusable payload.

    Mirrors ``GitHubClientError`` and shares the ``ForgeClientError`` base so a
    single ``except ForgeClientError`` catches both: it carries a redacted body, the
    failing operation, the HTTP status (``None`` for transport errors), and a stable
    ``reason_code`` so the failure flows end-to-end. Catch this specifically (or the
    shared base), never bare ``Exception``.
    """

    def __init__(
        self,
        *,
        operation: str,
        status: int | None,
        body: str,
        reason_code: str = BITBUCKET_API_ERROR,
    ) -> None:
        """Store redacted failure context for monitor diagnostics."""
        self.operation = operation
        self.status = status
        self.reason_code = reason_code
        self.body = redact_audit_text(body)
        status_label = "n/a" if status is None else str(status)
        super().__init__(
            f"{operation} failed (status={status_label}): {self.body.strip() or '<no output>'}"
        )

    def redacted_detail(self) -> str:
        """Return the redacted response body — BitBucket's human-facing detail."""
        return self.body

    @property
    def http_status(self) -> int | None:
        """Return the BitBucket HTTP status (``None`` for transport-level faults)."""
        return self.status


@dataclass(frozen=True)
class BitBucketAuth:
    """Explicit BitBucket Cloud credential mode (issue #345 decision D6)."""

    mode: str
    api_token: str
    email: str | None = None

    def header_value(self) -> str:
        """Return the ``Authorization`` header value for this credential mode."""
        if self.mode == "basic":
            raw = f"{self.email}:{self.api_token}".encode()
            return "Basic " + base64.b64encode(raw).decode("ascii")
        return f"Bearer {self.api_token}"

    def secret_values(self) -> tuple[str, ...]:
        """Return secret strings that must be redacted from any logged text."""
        secrets = [self.api_token]
        if self.mode == "basic" and self.email:
            encoded = base64.b64encode(f"{self.email}:{self.api_token}".encode()).decode("ascii")
            secrets.append(encoded)
        return tuple(secret for secret in secrets if secret)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> BitBucketAuth:
        """Build auth from the env contract, failing with a reason code if invalid."""
        mode = (env.get("BITBUCKET_AUTH_MODE") or "basic").strip().lower()
        token = (env.get("BITBUCKET_API_TOKEN") or "").strip()
        email = (env.get("BITBUCKET_EMAIL") or "").strip() or None
        if mode not in {"basic", "bearer"}:
            raise BitBucketClientError(
                operation="bitbucket auth",
                status=None,
                body=(
                    f"BITBUCKET_AUTH_MODE must be 'basic' or 'bearer', got {mode!r}. "
                    "App passwords are not supported."
                ),
                reason_code=BITBUCKET_AUTH_NOT_CONFIGURED,
            )
        if not token:
            raise BitBucketClientError(
                operation="bitbucket auth",
                status=None,
                body="BITBUCKET_API_TOKEN is required.",
                reason_code=BITBUCKET_AUTH_NOT_CONFIGURED,
            )
        if mode == "basic" and email is None:
            raise BitBucketClientError(
                operation="bitbucket auth",
                status=None,
                body="BITBUCKET_EMAIL is required for basic auth mode.",
                reason_code=BITBUCKET_AUTH_NOT_CONFIGURED,
            )
        return cls(mode=mode, api_token=token, email=email)
