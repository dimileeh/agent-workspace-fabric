"""Operator-grant honoring inside ``find_protected_quality_gate_changes``.

All grant logic lives inside the one gate function, so these exercise its
suppression contract directly: a benign edit passes regardless of grants; a real
violation (weakening edit, classifier-less protected file, or unclassifiable
no-diff change) is suppressed only by a matching ``approve_policy_downgrade``
grant — never by a non-acknowledged grant (fail closed).
"""

from __future__ import annotations

import pytest

from awf.control.quality_gates import (
    GrantSpec,
    ProtectedFileDiff,
    find_protected_quality_gate_changes,
)

_LOWER_COVERAGE_OLD = """
[project]
name = "demo"

[tool.coverage.report]
fail_under = 99
""".strip()
_LOWER_COVERAGE_NEW = """
[project]
name = "demo"

[tool.coverage.report]
fail_under = 80
""".strip()
_DEP_ADD_OLD = """
[project]
name = "demo"
dependencies = [
    "fastapi>=0.115.0",
]
""".strip()
_DEP_ADD_NEW = """
[project]
name = "demo"
dependencies = [
    "fastapi>=0.115.0",
    "httpx>=0.27.0",
]
""".strip()
_WORKFLOW_OLD = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
""".strip()
_WORKFLOW_WEAKENED = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
      - name: Post PR comment
        uses: actions/github-script@v7
        with:
          script: |
            await exec.exec("uv", ["run", "pytest"]);
""".strip()


def _pyproject_lower_coverage(grants: tuple[GrantSpec, ...]) -> list:
    return find_protected_quality_gate_changes(
        changed_paths=["pyproject.toml"],
        owned_paths=[],
        protected_file_diffs={
            "pyproject.toml": ProtectedFileDiff(
                path="pyproject.toml",
                old_text=_LOWER_COVERAGE_OLD,
                new_text=_LOWER_COVERAGE_NEW,
            )
        },
        operator_granted_paths=grants,
    )


@pytest.mark.unit
class TestClassifierBackedWeakening:
    """A classifier-detected weakening edit needs an acknowledged grant."""

    def test_blocked_without_grant(self) -> None:
        assert _pyproject_lower_coverage(()) != []

    def test_non_ack_grant_does_not_suppress(self) -> None:
        grants = (GrantSpec(path="pyproject.toml", approve_policy_downgrade=False),)
        assert _pyproject_lower_coverage(grants) != []

    def test_ack_grant_suppresses(self) -> None:
        grants = (GrantSpec(path="pyproject.toml", approve_policy_downgrade=True),)
        assert _pyproject_lower_coverage(grants) == []

    def test_ack_grant_for_other_path_does_not_suppress(self) -> None:
        grants = (GrantSpec(path=".coveragerc", approve_policy_downgrade=True),)
        assert _pyproject_lower_coverage(grants) != []


@pytest.mark.unit
class TestClassifierBackedBenign:
    """A benign classifier-backed edit passes with or without a grant."""

    def test_benign_passes_without_grant(self) -> None:
        violations = find_protected_quality_gate_changes(
            changed_paths=["pyproject.toml"],
            owned_paths=[],
            protected_file_diffs={
                "pyproject.toml": ProtectedFileDiff(
                    path="pyproject.toml", old_text=_DEP_ADD_OLD, new_text=_DEP_ADD_NEW
                )
            },
        )
        assert violations == []

    def test_benign_passes_with_non_ack_grant(self) -> None:
        # A benign granted edit does not require approve_policy_downgrade.
        violations = find_protected_quality_gate_changes(
            changed_paths=["pyproject.toml"],
            owned_paths=[],
            protected_file_diffs={
                "pyproject.toml": ProtectedFileDiff(
                    path="pyproject.toml", old_text=_DEP_ADD_OLD, new_text=_DEP_ADD_NEW
                )
            },
            operator_granted_paths=(
                GrantSpec(path="pyproject.toml", approve_policy_downgrade=False),
            ),
        )
        assert violations == []


@pytest.mark.unit
class TestClassifierLessProtectedFile:
    """A classifier-less protected file fails closed: ack required unconditionally."""

    def _coveragerc(self, grants: tuple[GrantSpec, ...]) -> list:
        return find_protected_quality_gate_changes(
            changed_paths=[".coveragerc"],
            owned_paths=[],
            operator_granted_paths=grants,
        )

    def test_blocked_without_grant(self) -> None:
        assert [v.path for v in self._coveragerc(())] == [".coveragerc"]

    def test_non_ack_grant_does_not_suppress(self) -> None:
        grants = (GrantSpec(path=".coveragerc", approve_policy_downgrade=False),)
        assert self._coveragerc(grants) != []

    def test_ack_grant_suppresses(self) -> None:
        grants = (GrantSpec(path=".coveragerc", approve_policy_downgrade=True),)
        assert self._coveragerc(grants) == []


@pytest.mark.unit
class TestDiffUnavailableFailsClosed:
    """An unclassifiable change with no diff is suppressed only by an ack grant."""

    def test_blocked_without_grant(self) -> None:
        violations = find_protected_quality_gate_changes(
            changed_paths=["pyproject.toml"], owned_paths=[]
        )
        assert [v.path for v in violations] == ["pyproject.toml"]

    def test_ack_grant_suppresses(self) -> None:
        violations = find_protected_quality_gate_changes(
            changed_paths=["pyproject.toml"],
            owned_paths=[],
            operator_granted_paths=(
                GrantSpec(path="pyproject.toml", approve_policy_downgrade=True),
            ),
        )
        assert violations == []


@pytest.mark.unit
class TestDirectoryWideGrant:
    """A directory-wide grant covers nested protected files (glob semantics)."""

    def test_directory_grant_suppresses_nested_weakening_workflow(self) -> None:
        violations = find_protected_quality_gate_changes(
            changed_paths=[".github/workflows/ci.yml"],
            owned_paths=[],
            protected_file_diffs={
                ".github/workflows/ci.yml": ProtectedFileDiff(
                    path=".github/workflows/ci.yml",
                    old_text=_WORKFLOW_OLD,
                    new_text=_WORKFLOW_WEAKENED,
                )
            },
            operator_granted_paths=(
                GrantSpec(path=".github/workflows/", approve_policy_downgrade=True),
            ),
        )
        assert violations == []

    def test_directory_grant_without_ack_does_not_suppress(self) -> None:
        violations = find_protected_quality_gate_changes(
            changed_paths=[".github/workflows/ci.yml"],
            owned_paths=[],
            protected_file_diffs={
                ".github/workflows/ci.yml": ProtectedFileDiff(
                    path=".github/workflows/ci.yml",
                    old_text=_WORKFLOW_OLD,
                    new_text=_WORKFLOW_WEAKENED,
                )
            },
            operator_granted_paths=(
                GrantSpec(path=".github/workflows/", approve_policy_downgrade=False),
            ),
        )
        assert violations != []


@pytest.mark.unit
class TestWildcardGrantMembership:
    """A wildcard grant authorizes only the paths its glob actually matches.

    The grant matcher uses precise glob membership, not the symmetric owned-path
    overlap predicate (which reduced a wildcard to its literal directory prefix
    and over-authorized the whole directory).
    """

    def _deploy_weakening(self, grants: tuple[GrantSpec, ...]) -> list:
        return find_protected_quality_gate_changes(
            changed_paths=[".github/workflows/deploy.yml"],
            owned_paths=[],
            protected_file_diffs={
                ".github/workflows/deploy.yml": ProtectedFileDiff(
                    path=".github/workflows/deploy.yml",
                    old_text=_WORKFLOW_OLD,
                    new_text=_WORKFLOW_WEAKENED,
                )
            },
            operator_granted_paths=grants,
        )

    def test_wildcard_grant_does_not_authorize_non_matching_workflow(self) -> None:
        grants = (GrantSpec(path=".github/workflows/*-lint.yml", approve_policy_downgrade=True),)
        # deploy.yml does not match *-lint.yml, so the ack grant must not suppress.
        assert self._deploy_weakening(grants) != []

    def test_wildcard_grant_authorizes_matching_workflow(self) -> None:
        violations = find_protected_quality_gate_changes(
            changed_paths=[".github/workflows/ci-lint.yml"],
            owned_paths=[],
            protected_file_diffs={
                ".github/workflows/ci-lint.yml": ProtectedFileDiff(
                    path=".github/workflows/ci-lint.yml",
                    old_text=_WORKFLOW_OLD,
                    new_text=_WORKFLOW_WEAKENED,
                )
            },
            operator_granted_paths=(
                GrantSpec(path=".github/workflows/*-lint.yml", approve_policy_downgrade=True),
            ),
        )
        assert violations == []
