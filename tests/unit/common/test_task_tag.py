"""Tests for the task-tag (Jira issue key + Aira entity key) helpers."""

from __future__ import annotations

import subprocess

import pytest

from awf.common.task_tag import (
    BRANCH_SEP,
    MESSAGE_SEP,
    branch_with_task_tag,
    commit_message_with_task_tag,
    normalize_task_tag,
    strip_leading_task_tag,
    title_with_task_tag,
    validate_task_tag,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "value",
    ["PROJ-123", "AB-1", "A1B2-99", "PROJECT-100000"],
)
def test_validate_accepts_well_formed_keys(value: str) -> None:
    assert validate_task_tag(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "proj-123",  # lowercase
        "PROJ123",  # no hyphen / number group
        "PROJ-",  # missing number
        "1AB-2",  # must start with a letter
        "PROJ-12a",  # trailing non-digit
        "PR OJ-1",  # space inside
        "-1",  # no project segment
    ],
)
def test_validate_rejects_malformed_keys(value: str) -> None:
    with pytest.raises(ValueError, match="task tag"):
        validate_task_tag(value)


def test_validate_trims_surrounding_whitespace() -> None:
    assert validate_task_tag("  PROJ-123  ") == "PROJ-123"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_validate_maps_empty_to_none(value: str | None) -> None:
    assert validate_task_tag(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("  PROJ-123 ", "PROJ-123"),
        ("anything", "anything"),  # normalize does not validate the shape
    ],
)
def test_normalize_task_tag(value: str | None, expected: str | None) -> None:
    assert normalize_task_tag(value) == expected


def test_branch_with_task_tag_prepends_with_branch_sep() -> None:
    assert branch_with_task_tag("awf/ws_123", "PROJ-123") == f"PROJ-123{BRANCH_SEP}awf/ws_123"


def test_title_with_task_tag_prepends_with_message_sep() -> None:
    assert title_with_task_tag("Fix the bug", "PROJ-123") == f"PROJ-123{MESSAGE_SEP}Fix the bug"


def test_commit_message_with_task_tag_prepends_with_message_sep() -> None:
    assert (
        commit_message_with_task_tag("awf: do work", "PROJ-123")
        == f"PROJ-123{MESSAGE_SEP}awf: do work"
    )


@pytest.mark.parametrize(
    "helper",
    [
        branch_with_task_tag,
        title_with_task_tag,
        commit_message_with_task_tag,
        strip_leading_task_tag,
    ],
)
@pytest.mark.parametrize("tag", [None, ""])
def test_helpers_are_noop_when_tag_absent(helper, tag: str | None) -> None:
    assert helper("original", tag) == "original"


def test_strip_leading_task_tag_removes_a_single_leading_key() -> None:
    assert strip_leading_task_tag("PROJ-123 Fix the bug", "PROJ-123") == "Fix the bug"


def test_strip_leading_task_tag_is_noop_without_exact_leading_prefix() -> None:
    # A mid-text key, or a different leading key, is left untouched.
    assert strip_leading_task_tag("see PROJ-123 for details", "PROJ-123") == (
        "see PROJ-123 for details"
    )
    assert strip_leading_task_tag("OTHER-1 Fix", "PROJ-123") == "OTHER-1 Fix"


def test_strip_leading_task_tag_removes_colon_form_leading_key() -> None:
    # Regression: the common Jira "PROJ-123: …" colon form is a delimited leading
    # key just like the space form, so it must be stripped too — otherwise the
    # re-applied commit tag duplicates the key (see the colon-form subject test).
    assert strip_leading_task_tag("PROJ-123: Fix the bug", "PROJ-123") == "Fix the bug"


def test_strip_leading_task_tag_is_noop_when_leading_token_is_a_longer_key() -> None:
    # A longer key sharing the prefix is a *different* issue; the boundary check
    # leaves it untouched rather than stripping a partial key, consistent with
    # title_with_task_tag / commit_message_with_task_tag.
    assert strip_leading_task_tag("PROJ-1234: Fix", "PROJ-123") == "PROJ-1234: Fix"


def test_strip_leading_task_tag_when_text_is_exactly_the_key() -> None:
    assert strip_leading_task_tag("PROJ-123", "PROJ-123") == ""


def test_strip_then_commit_tag_yields_single_leading_key() -> None:
    # An already-tagged title embedded in an ``awf:`` subject must not duplicate
    # the issue key once the tag is re-applied.
    title = "PROJ-123 Fix the bug"
    subject = commit_message_with_task_tag(
        f"awf: {strip_leading_task_tag(title, 'PROJ-123')}", "PROJ-123"
    )
    assert subject == "PROJ-123 awf: Fix the bug"


def test_strip_then_commit_tag_yields_single_leading_key_for_colon_form() -> None:
    # Regression for the reported bug: a colon-form Jira title must collapse to a
    # single leading key in the composed ``awf:`` commit subject, not
    # "PROJ-123 awf: PROJ-123: …".
    title = "PROJ-123: Fix the bug"
    subject = commit_message_with_task_tag(
        f"awf: {strip_leading_task_tag(title, 'PROJ-123')}", "PROJ-123"
    )
    assert subject == "PROJ-123 awf: Fix the bug"


def test_branch_is_idempotent() -> None:
    once = branch_with_task_tag("awf/ws_123", "PROJ-123")
    twice = branch_with_task_tag(once, "PROJ-123")
    assert once == twice == "PROJ-123-awf/ws_123"


def test_branch_idempotency_only_triggers_on_exact_prefix() -> None:
    # A different leading key (sharing a prefix but not the exact "{tag}-" guard)
    # still gets prefixed rather than being mistaken for an already-tagged branch.
    assert branch_with_task_tag("PROJ-1234-awf/ws_1", "PROJ-123") == ("PROJ-123-PROJ-1234-awf/ws_1")


def test_title_is_idempotent() -> None:
    once = title_with_task_tag("Fix the bug", "PROJ-123")
    twice = title_with_task_tag(once, "PROJ-123")
    assert once == twice == "PROJ-123 Fix the bug"


def test_title_idempotency_only_triggers_on_exact_prefix() -> None:
    # A title that merely contains the tag mid-text still gets prefixed.
    title = "see PROJ-123 for details"
    assert title_with_task_tag(title, "PROJ-123") == "PROJ-123 see PROJ-123 for details"


def test_title_idempotent_on_colon_form_leading_key() -> None:
    # Regression: an operator-supplied Jira title that already leads with the key
    # in the common "PROJ-123: …" colon form must not become a duplicated
    # "PROJ-123 PROJ-123: …" prefix that weakens auto-linking.
    assert title_with_task_tag("PROJ-123: Fix the bug", "PROJ-123") == "PROJ-123: Fix the bug"


def test_title_idempotent_when_title_is_exactly_the_key() -> None:
    # A title that is exactly the key (no trailing text) is already tagged.
    assert title_with_task_tag("PROJ-123", "PROJ-123") == "PROJ-123"


def test_title_prefixes_when_leading_token_is_a_longer_key() -> None:
    # A longer key sharing the prefix is a *different* issue, so the boundary
    # check still prepends rather than treating it as already-tagged.
    assert title_with_task_tag("PROJ-1234: Fix", "PROJ-123") == "PROJ-123 PROJ-1234: Fix"


def test_commit_message_is_idempotent() -> None:
    once = commit_message_with_task_tag("awf: do work", "PROJ-123")
    twice = commit_message_with_task_tag(once, "PROJ-123")
    assert once == twice == "PROJ-123 awf: do work"


def test_commit_message_idempotency_only_triggers_on_exact_prefix() -> None:
    # A message that merely contains the tag mid-text still gets prefixed.
    msg = "see PROJ-123 for details"
    assert commit_message_with_task_tag(msg, "PROJ-123") == "PROJ-123 see PROJ-123 for details"


def test_commit_message_idempotent_on_colon_form_leading_key() -> None:
    # Same colon-form leading-key guard as title_with_task_tag — the helpers are
    # documented as mutually consistent.
    assert commit_message_with_task_tag("PROJ-123: do work", "PROJ-123") == "PROJ-123: do work"


# --- Entity key validation (T1) ---


@pytest.mark.parametrize(
    "value",
    ["AIRA-T299", "PROJ-T1", "AB-T99999"],
)
def test_validate_accepts_entity_task_keys(value: str) -> None:
    assert validate_task_tag(value) == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("[AIRA-T299]", "AIRA-T299"),
        ("  [AIRA-T299]  ", "AIRA-T299"),
        ("[ AIRA-T299 ]", "AIRA-T299"),
    ],
)
def test_validate_normalizes_bracketed_entity_keys(value: str, expected: str) -> None:
    assert validate_task_tag(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "AIRA-F42",  # feature key — task-only
        "12-T3",  # digit-leading prefix
        "AIRA-299X",  # trailing junk
        "aira-t299",  # lowercase
        "[AIRA-T299",  # unbalanced open bracket
        "AIRA-T299]",  # unbalanced close bracket
    ],
)
def test_validate_rejects_invalid_entity_shapes(value: str) -> None:
    with pytest.raises(ValueError, match="task tag"):
        validate_task_tag(value)


def test_validate_error_message_names_both_shapes() -> None:
    with pytest.raises(ValueError, match="PROJ-123") as exc_info:
        validate_task_tag("bad-key")
    message = str(exc_info.value)
    assert "-T" in message or "entity" in message.lower()


def test_validate_jira_regression_aira_299() -> None:
    assert validate_task_tag("AIRA-299") == "AIRA-299"


@pytest.mark.parametrize(
    "value",
    [
        "[]",
        "[   ]",
    ],
)
def test_validate_rejects_empty_bracket_wrapper(value: str) -> None:
    with pytest.raises(ValueError, match="task tag") as exc_info:
        validate_task_tag(value)
    assert repr(value) in str(exc_info.value)


# --- Entity key title / commit (T2) ---


def test_title_with_entity_task_tag_brackets_key() -> None:
    assert title_with_task_tag("Fix the bug", "AIRA-T299") == "[AIRA-T299] Fix the bug"


def test_commit_message_with_entity_task_tag_brackets_key() -> None:
    assert commit_message_with_task_tag("awf: do work", "AIRA-T299") == "[AIRA-T299] awf: do work"


def test_title_jira_regression_stays_bare() -> None:
    assert title_with_task_tag("Fix the bug", "AIRA-299") == "AIRA-299 Fix the bug"


def test_commit_message_jira_regression_stays_bare() -> None:
    assert commit_message_with_task_tag("awf: do work", "AIRA-299") == "AIRA-299 awf: do work"


def test_title_entity_idempotent_on_bracketed_form() -> None:
    once = title_with_task_tag("Fix the bug", "AIRA-T299")
    twice = title_with_task_tag(once, "AIRA-T299")
    assert once == twice == "[AIRA-T299] Fix the bug"


def test_commit_message_entity_idempotent_on_bracketed_form() -> None:
    once = commit_message_with_task_tag("awf: do work", "AIRA-T299")
    twice = commit_message_with_task_tag(once, "AIRA-T299")
    assert once == twice == "[AIRA-T299] awf: do work"


def test_title_entity_longer_key_boundary_not_treated_as_tagged() -> None:
    assert title_with_task_tag("[AIRA-T2999] Fix", "AIRA-T299") == ("[AIRA-T299] [AIRA-T2999] Fix")
    assert title_with_task_tag("AIRA-T2999 Fix", "AIRA-T299") == ("[AIRA-T299] AIRA-T2999 Fix")


def test_commit_message_entity_longer_key_boundary_not_treated_as_tagged() -> None:
    assert commit_message_with_task_tag("[AIRA-T2999] msg", "AIRA-T299") == (
        "[AIRA-T299] [AIRA-T2999] msg"
    )


# --- Entity key branch (T2) ---


def test_branch_with_entity_task_tag_stays_bare() -> None:
    assert branch_with_task_tag("awf/ws_123", "AIRA-T299") == "AIRA-T299-awf/ws_123"


def test_branch_jira_regression_aira_299() -> None:
    assert branch_with_task_tag("awf/ws_123", "AIRA-299") == "AIRA-299-awf/ws_123"


def _assert_git_ref_valid(branch: str) -> None:
    result = subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.parametrize("tag", ["AIRA-T299", "AIRA-299", "PROJ-123"])
def test_branch_with_task_tag_passes_git_check_ref_format(tag: str) -> None:
    branch = branch_with_task_tag("awf/ws_627d325e3d6144899eef4624", tag)
    assert "[" not in branch
    _assert_git_ref_valid(branch)


# --- Entity key strip / leads-with (T2) ---


def test_strip_leading_task_tag_entity_bracketed_form() -> None:
    assert strip_leading_task_tag("[AIRA-T299] Fix the bug", "AIRA-T299") == "Fix the bug"


def test_strip_leading_task_tag_entity_longer_key_untouched() -> None:
    assert strip_leading_task_tag("[AIRA-T2999] Fix", "AIRA-T299") == "[AIRA-T2999] Fix"


def test_strip_then_commit_tag_entity_yields_single_bracketed_key() -> None:
    title = "[AIRA-T299] Fix the bug"
    subject = commit_message_with_task_tag(
        f"awf: {strip_leading_task_tag(title, 'AIRA-T299')}", "AIRA-T299"
    )
    assert subject == "[AIRA-T299] awf: Fix the bug"


# --- Integration: all three formatters for entity key (T2) ---


def test_entity_key_integration_all_formatters() -> None:
    tag = "AIRA-T299"
    title = title_with_task_tag("Async workspace support", tag)
    commit = commit_message_with_task_tag("awf: initial commit", tag)
    branch = branch_with_task_tag("awf/ws_627d325e3d6144899eef4624", tag)

    assert title == "[AIRA-T299] Async workspace support"
    assert commit == "[AIRA-T299] awf: initial commit"
    assert branch == "AIRA-T299-awf/ws_627d325e3d6144899eef4624"
    assert "[" not in branch
    _assert_git_ref_valid(branch)
