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
    """Prepend ``tag`` to ``branch`` with the branch separator; no-op if falsy."""
    if not tag:
        return branch
    return f"{tag}{BRANCH_SEP}{branch}"


def title_with_task_tag(title: str, tag: str | None) -> str:
    """Prepend ``tag`` to a PR ``title`` with the message separator; no-op if falsy."""
    if not tag:
        return title
    return f"{tag}{MESSAGE_SEP}{title}"


def commit_message_with_task_tag(message: str, tag: str | None) -> str:
    """Prepend ``tag`` to a commit ``message``; no-op if falsy.

    Idempotent: if ``message`` already starts with ``"{tag} "`` it is returned
    unchanged, so monitor re-runs and ``%B``-reusing reparents never accumulate
    a double prefix.
    """
    if not tag:
        return message
    prefix = f"{tag}{MESSAGE_SEP}"
    if message.startswith(prefix):
        return message
    return f"{prefix}{message}"
