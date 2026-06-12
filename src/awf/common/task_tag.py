"""Task-tag (Jira issue key) helpers.

A *task tag* is an optional Jira-style issue key (e.g. ``PROJ-123``) that an
operator attaches to a workspace via ``--task-tag``. When present, AWF prepends
it to the branch name, the PR title, and **every commit message AWF authors** so
the resulting branch/PR/commits auto-link to the Jira issue in BitBucket+Jira
orgs (see https://support.atlassian.com/jira-software-cloud/docs/reference-issues-in-your-development-work/).

Every helper here is a strict no-op when the tag is falsy, so the feature is
fully backward-compatible when ``--task-tag`` is omitted. Format defaults live as
module constants; a per-workspace configurable separator is a non-goal.
"""

from __future__ import annotations

import re
from typing import Final

TASK_TAG_PATTERN: Final = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")
"""Jira issue key shape: an uppercase project key, a hyphen, then a number."""

BRANCH_SEP: Final = "-"
"""Separator between the tag and a branch name → ``PROJ-123-awf/<id>``."""

MESSAGE_SEP: Final = " "
"""Separator between the tag and a PR title / commit message → ``PROJ-123 …``."""


def normalize_task_tag(value: str | None) -> str | None:
    """Strip surrounding whitespace; map ``None``/empty/blank to ``None``.

    Does not validate the shape — use :func:`validate_task_tag` for that.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def validate_task_tag(value: str | None) -> str | None:
    """Normalize then validate a task tag.

    Returns the normalized tag, ``None`` when absent/blank, and raises
    ``ValueError`` with a clear message when the value is non-empty but does not
    match the Jira issue-key shape.
    """
    normalized = normalize_task_tag(value)
    if normalized is None:
        return None
    if not TASK_TAG_PATTERN.match(normalized):
        raise ValueError(
            f"invalid task tag {normalized!r}: expected a Jira issue key like "
            "'PROJ-123' (uppercase project key, hyphen, number)"
        )
    return normalized


def branch_with_task_tag(branch: str, tag: str | None) -> str:
    """Prepend ``tag`` to ``branch`` with the branch separator; no-op if falsy.

    Idempotent: if ``branch`` already starts with ``"{tag}-"`` it is returned
    unchanged, consistent with :func:`title_with_task_tag` and
    :func:`commit_message_with_task_tag`, so a caller that passes an
    already-tagged branch never accumulates a double prefix.
    """
    if not tag:
        return branch
    prefix = f"{tag}{BRANCH_SEP}"
    if branch.startswith(prefix):
        return branch
    return f"{prefix}{branch}"


def _leads_with_task_key(text: str, tag: str) -> bool:
    """True when ``text`` already begins with ``tag`` as a delimited leading token.

    "Delimited" means the tag is followed by end-of-string or a non-alphanumeric
    boundary — a space (``"PROJ-123 …"``) or a punctuation form such as a colon
    (``"PROJ-123: …"``, a common Jira title shape). The boundary check is what
    stops a *longer* key (``PROJ-1234`` when ``tag`` is ``PROJ-123``) from being
    mistaken for an already-tagged leading key.
    """
    if not text.startswith(tag):
        return False
    rest = text[len(tag) :]
    return not rest or not rest[0].isalnum()


def title_with_task_tag(title: str, tag: str | None) -> str:
    """Prepend ``tag`` to a PR ``title`` with the message separator; no-op if falsy.

    Idempotent on any already-tagged title: if ``title`` already begins with the
    key as a delimited leading token — the ``"{tag} "`` form *or* a punctuation
    form such as ``"{tag}: …"`` (a common Jira title shape) — it is returned
    unchanged, so a caller never accumulates a duplicated ``"{tag} {tag}: …"``
    prefix that weakens Jira auto-linking. Consistent with
    :func:`commit_message_with_task_tag`.
    """
    if not tag:
        return title
    if _leads_with_task_key(title, tag):
        return title
    return f"{tag}{MESSAGE_SEP}{title}"


def strip_leading_task_tag(text: str, tag: str | None) -> str:
    """Remove a single leading ``"{tag} "`` prefix from ``text``; no-op if falsy/absent.

    Use this before embedding a (possibly already-tagged) ``task_title`` inside a
    composed commit subject such as ``f"awf: {title}"``: stripping the leading key
    first means the subsequent :func:`commit_message_with_task_tag` re-application
    yields a single leading key (``"{tag} awf: …"``) instead of a duplicated
    ``"{tag} awf: {tag} …"`` that weakens Jira auto-linking.
    """
    if not tag:
        return text
    prefix = f"{tag}{MESSAGE_SEP}"
    if text.startswith(prefix):
        return text[len(prefix) :]
    return text


def commit_message_with_task_tag(message: str, tag: str | None) -> str:
    """Prepend ``tag`` to a commit ``message``; no-op if falsy.

    Idempotent on any already-tagged message: if ``message`` already begins with
    the key as a delimited leading token — the ``"{tag} "`` form or a punctuation
    form such as ``"{tag}: …"`` — it is returned unchanged, so monitor re-runs and
    ``%B``-reusing reparents never accumulate a double prefix. Consistent with
    :func:`title_with_task_tag`.
    """
    if not tag:
        return message
    if _leads_with_task_key(message, tag):
        return message
    return f"{tag}{MESSAGE_SEP}{message}"
