"""Task-tag helpers for Jira issue keys and Aira task entity keys.

A *task tag* is an optional key an operator attaches to a workspace via
``--task-tag``. AWF accepts two shapes:

- **Jira issue key** (e.g. ``PROJ-123``) — prepended **bare** to branch, PR
  title, and every AWF-authored commit message for BitBucket+Jira auto-linking.
- **Aira task entity key** (e.g. ``PROJ-T123``) — prepended in **brackets**
  on title/commits (``[PROJ-T123] …``) so Aira's PR linker resolves the task;
  the branch always uses the bare key (``PROJ-T123-awf/<id>``) because git
  forbids ``[`` in ref names.

Pass **bare** keys at the CLI (``AIRA-T299``); bracketed ``[AIRA-T299]`` is
accepted and normalized, but bare is recommended because ``[`` is a shell glob
character.

Every helper here is a strict no-op when the tag is falsy, so the feature is
fully backward-compatible when ``--task-tag`` is omitted. Format defaults live as
module constants; a per-workspace configurable separator is a non-goal.
"""

from __future__ import annotations

import re
from typing import Final

TASK_TAG_PATTERN: Final = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")
"""Jira issue key shape: an uppercase project key, a hyphen, then a number."""

ENTITY_KEY_PATTERN: Final = re.compile(r"^[A-Z][A-Z0-9]{1,9}-T[0-9]+$")
"""Aira task entity key shape: alpha-leading project prefix, ``-T``, then digits."""

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


def _strip_balanced_brackets(value: str) -> str:
    """Strip one balanced surrounding ``[...]`` wrapper when present."""
    if value.startswith("[") and value.endswith("]"):
        return value[1:-1].strip()
    return value


def _is_entity_key(tag: str) -> bool:
    """True when ``tag`` matches the Aira task entity key shape."""
    return bool(ENTITY_KEY_PATTERN.match(tag))


def validate_task_tag(value: str | None) -> str | None:
    """Normalize then validate a task tag.

    Returns the canonical bare tag, ``None`` when absent/blank, and raises
    ``ValueError`` with a clear message when the value is non-empty but does not
    match a Jira issue key (``PROJ-123``) or an Aira task entity key
    (``PROJ-T123``).
    """
    normalized = normalize_task_tag(value)
    if normalized is None:
        return None
    candidate = _strip_balanced_brackets(normalized)
    if not candidate:
        raise ValueError(
            "invalid task tag: expected a Jira issue key like 'PROJ-123' "
            "or an Aira task entity key like 'PROJ-T123' "
            "(uppercase project key, hyphen, number; entity keys use -T<number>)"
        )
    if TASK_TAG_PATTERN.match(candidate) or ENTITY_KEY_PATTERN.match(candidate):
        return candidate
    raise ValueError(
        f"invalid task tag {normalized!r}: expected a Jira issue key like "
        "'PROJ-123' or an Aira task entity key like 'PROJ-T123' "
        "(uppercase project key, hyphen, number; entity keys use -T<number>)"
    )


def branch_with_task_tag(branch: str, tag: str | None) -> str:
    """Prepend ``tag`` to ``branch`` with the branch separator; no-op if falsy.

    Idempotent: if ``branch`` already starts with ``"{tag}-"`` it is returned
    unchanged, consistent with :func:`title_with_task_tag` and
    :func:`commit_message_with_task_tag`, so a caller that passes an
    already-tagged branch never accumulates a double prefix. Branches always
    use the bare key — never bracketed — because git forbids ``[`` in ref names.
    """
    if not tag:
        return branch
    prefix = f"{tag}{BRANCH_SEP}"
    if branch.startswith(prefix):
        return branch
    return f"{prefix}{branch}"


def _leads_with_task_key(text: str, tag: str) -> bool:
    """True when ``text`` already begins with ``tag`` as a delimited leading token.

    For entity keys the emitted form is bracketed (``"[{tag}] …"``); for Jira
    keys it is bare (``"{tag} …"`` or ``"{tag}: …"``). The boundary check stops
    a *longer* key (``PROJ-1234`` when ``tag`` is ``PROJ-123``) from being
    mistaken for an already-tagged leading key.
    """
    if _is_entity_key(tag):
        bracketed = f"[{tag}]"
        if not text.startswith(bracketed):
            return False
        rest = text[len(bracketed) :]
        return not rest or not rest[0].isalnum()
    if not text.startswith(tag):
        return False
    rest = text[len(tag) :]
    return not rest or not rest[0].isalnum()


def title_with_task_tag(title: str, tag: str | None) -> str:
    """Prepend ``tag`` to a PR ``title``; no-op if falsy.

    Entity keys are bracketed (``"[{tag}] …"``); Jira keys stay bare
    (``"{tag} …"``). Idempotent on the emitted form so a caller never
    accumulates a duplicated prefix. Consistent with
    :func:`commit_message_with_task_tag`.
    """
    if not tag:
        return title
    if _leads_with_task_key(title, tag):
        return title
    if _is_entity_key(tag):
        return f"[{tag}]{MESSAGE_SEP}{title}"
    return f"{tag}{MESSAGE_SEP}{title}"


def strip_leading_task_tag(text: str, tag: str | None) -> str:
    """Remove a single leading task key from ``text``; no-op if falsy/absent.

    Strips the emitted leading form — bracketed for entity keys, bare (space or
    colon delimiter) for Jira keys — together with the trailing delimiter run.
    The delimited-token check is shared with :func:`title_with_task_tag` /
    :func:`commit_message_with_task_tag`, so a *longer* key is left untouched.

    Use this before embedding a (possibly already-tagged) ``task_title`` inside a
    composed commit subject such as ``f"awf: {title}"``: stripping the leading key
    first means the subsequent :func:`commit_message_with_task_tag` re-application
    yields a single leading key instead of a duplicated prefix.
    """
    if not tag:
        return text
    if not _leads_with_task_key(text, tag):
        return text
    prefix_len = len(f"[{tag}]") if _is_entity_key(tag) else len(tag)
    return re.sub(r"^[^0-9A-Za-z]+", "", text[prefix_len:])


def commit_message_with_task_tag(message: str, tag: str | None) -> str:
    """Prepend ``tag`` to a commit ``message``; no-op if falsy.

    Entity keys are bracketed (``"[{tag}] …"``); Jira keys stay bare. Idempotent
    on the emitted form so monitor re-runs and ``%B``-reusing reparents never
    accumulate a double prefix. Consistent with :func:`title_with_task_tag`.
    """
    if not tag:
        return message
    if _leads_with_task_key(message, tag):
        return message
    if _is_entity_key(tag):
        return f"[{tag}]{MESSAGE_SEP}{message}"
    return f"{tag}{MESSAGE_SEP}{message}"
