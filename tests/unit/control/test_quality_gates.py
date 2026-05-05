"""Tests for protected quality-gate file detection."""

from __future__ import annotations

import pytest

from awf.control.quality_gates import (
    changed_paths_are_only_internal_plan_artifacts,
    find_protected_quality_gate_changes,
    plan_only_output_message,
    quality_gate_violation_message,
)


@pytest.mark.unit
def test_unowned_workspace_profile_change_is_protected() -> None:
    violations = find_protected_quality_gate_changes(
        changed_paths=[".awf/workspace.yml", "src/awf/runtime/validation.py"],
        owned_paths=["src/awf/runtime/**"],
    )

    assert [violation.path for violation in violations] == [".awf/workspace.yml"]


@pytest.mark.unit
def test_unowned_pyproject_change_is_protected() -> None:
    violations = find_protected_quality_gate_changes(
        changed_paths=["pyproject.toml"],
        owned_paths=["tests/unit/**"],
    )

    assert [violation.path for violation in violations] == ["pyproject.toml"]


@pytest.mark.unit
def test_explicit_ownership_allows_quality_gate_change() -> None:
    violations = find_protected_quality_gate_changes(
        changed_paths=["pyproject.toml", ".github/workflows/ci.yml"],
        owned_paths=["pyproject.toml", ".github/workflows/**"],
    )

    assert violations == []


@pytest.mark.unit
def test_regular_source_changes_are_not_protected() -> None:
    violations = find_protected_quality_gate_changes(
        changed_paths=[
            "   ",
            "./src/awf/control/executor.py",
            "tests/unit/control/test_executor.py",
        ],
        owned_paths=[],
    )

    assert violations == []


@pytest.mark.unit
def test_plan_only_output_detects_internal_plan_artifacts() -> None:
    assert changed_paths_are_only_internal_plan_artifacts(
        [
            "docs/awf-plans/ws_123.md",
            "./docs/awf-plans/ws_123.conformance.json",
        ]
    )


@pytest.mark.unit
def test_plan_only_output_allows_real_docs_and_source_changes() -> None:
    assert not changed_paths_are_only_internal_plan_artifacts(["docs/PROJECT_ONBOARDING.md"])
    assert not changed_paths_are_only_internal_plan_artifacts(
        ["docs/awf-plans/ws_123.md", "src/awf/control/executor.py"]
    )
    assert not changed_paths_are_only_internal_plan_artifacts([])


@pytest.mark.unit
def test_plan_only_output_message_is_operator_visible() -> None:
    message = plan_only_output_message(
        ["docs/awf-plans/ws_123.md", "docs/awf-plans/ws_123.conformance.json"]
    )

    assert "only AWF plan/conformance artifact" in message
    assert "docs/awf-plans/ws_123.md" in message


@pytest.mark.unit
def test_violation_message_reports_overflow_count() -> None:
    violations = find_protected_quality_gate_changes(
        changed_paths=[
            ".awf/workspace.yml",
            ".coveragerc",
            ".github/workflows/ci.yml",
            "pyproject.toml",
            "pytest.ini",
            "setup.cfg",
            "setup.py",
            "tox.ini",
            ".github/workflows/release.yml",
        ],
        owned_paths=[],
    )

    message = quality_gate_violation_message(violations)

    assert ".awf/workspace.yml" in message
    assert "and 1 more" in message
