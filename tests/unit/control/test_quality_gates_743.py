"""Quality-gate tests for issue #743: unowned-protected path detection and the
`.awf/workspace.yml` tier/final_gate no-op note in the block message."""

from __future__ import annotations

import pytest

from awf.control.quality_gates import (
    find_protected_quality_gate_changes,
    quality_gate_violation_message,
    unowned_protected_paths,
)


@pytest.mark.unit
def test_unowned_protected_paths_covers_all_patterns_and_normalizes() -> None:
    """unowned_protected_paths spans every protected pattern (not just pyproject/workflow).

    It underpins the conformance-salvage quarantine (#743): a changed protected
    file the source did not own is what a retry must not replay.
    """
    paths = unowned_protected_paths(
        [
            " ./.awf/workspace.yml ",
            ".coveragerc",
            ".\\.github\\workflows\\ci.yml",
            "src/awf/runtime/validation.py",
            "pytest.ini",
            " ",
        ]
    )

    assert paths == (
        ".awf/workspace.yml",
        ".coveragerc",
        ".github/workflows/ci.yml",
        "pytest.ini",
    )


@pytest.mark.unit
def test_unowned_protected_paths_excludes_owned() -> None:
    """A protected path covered by owned_paths is not returned."""
    paths = unowned_protected_paths(
        [".awf/workspace.yml", "pyproject.toml"],
        owned_paths=[".awf/workspace.yml"],
    )

    assert paths == ("pyproject.toml",)


@pytest.mark.unit
def test_violation_message_flags_workspace_yml_tier_and_final_gate_noop() -> None:
    """A .awf/workspace.yml edit must warn the tier/final_gate edit is a no-op (#743).

    ``requested_tier`` is set at dispatch and ``final_gate`` is fixed at provision
    time, so editing them in the protected file has no effect. The message must
    say so, so agents stop editing the file to try to escalate AWF validation.
    """
    violations = find_protected_quality_gate_changes(
        changed_paths=[".awf/workspace.yml"],
        owned_paths=[],
    )

    message = quality_gate_violation_message(violations)

    assert ".awf/workspace.yml" in message
    assert "requested_tier" in message
    assert "final_gate" in message
    assert "dispatch" in message


@pytest.mark.unit
def test_violation_message_omits_workspace_yml_note_for_other_files() -> None:
    """The tier/final_gate note only appears for .awf/workspace.yml edits."""
    violations = find_protected_quality_gate_changes(
        changed_paths=["pytest.ini"],
        owned_paths=[],
    )

    message = quality_gate_violation_message(violations)

    assert "pytest.ini" in message
    assert "requested_tier" not in message
