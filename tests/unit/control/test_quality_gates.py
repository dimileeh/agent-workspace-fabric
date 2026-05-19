"""Tests for protected quality-gate file detection."""

from __future__ import annotations

import pytest

from awf.control.quality_gates import (
    ProtectedFileDiff,
    changed_paths_are_only_internal_plan_artifacts,
    diff_classified_protected_paths,
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
def test_diff_classified_protected_paths_normalizes_and_deduplicates() -> None:
    paths = diff_classified_protected_paths(
        [
            " ./pyproject.toml ",
            ".\\.github\\workflows\\ci.yml",
            "./.github/workflows/ci.yml",
            "src/awf/control/executor.py",
            " ",
        ]
    )

    assert paths == ("pyproject.toml", ".github/workflows/ci.yml")


@pytest.mark.unit
def test_pyproject_dependency_addition_is_allowed() -> None:
    old_text = """
[project]
name = "demo"
dependencies = [
    "fastapi>=0.115.0",
]
""".strip()
    new_text = """
[project]
name = "demo"
dependencies = [
    "fastapi>=0.115.0",
    "httpx>=0.27.0",
]
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=["pyproject.toml"],
        owned_paths=[],
        protected_file_diffs={
            "pyproject.toml": ProtectedFileDiff(
                path="pyproject.toml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert violations == []


@pytest.mark.unit
def test_pyproject_lower_coverage_fail_under_is_blocked() -> None:
    old_text = """
[project]
name = "demo"

[tool.coverage.report]
fail_under = 99
""".strip()
    new_text = """
[project]
name = "demo"

[tool.coverage.report]
fail_under = 80
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=["pyproject.toml"],
        owned_paths=[],
        protected_file_diffs={
            "pyproject.toml": ProtectedFileDiff(
                path="pyproject.toml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert len(violations) == 1
    violation = violations[0]
    assert violation.path == "pyproject.toml"
    assert violation.section == "tool.coverage.report.fail_under"
    assert violation.line == 5
    assert "lowered from 99 to 80" in violation.reason


@pytest.mark.unit
def test_pyproject_dependency_deletion_is_blocked() -> None:
    old_text = """
[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-cov>=5.0.0",
]
""".strip()
    new_text = """
[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
]
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=["pyproject.toml"],
        owned_paths=[],
        protected_file_diffs={
            "pyproject.toml": ProtectedFileDiff(
                path="pyproject.toml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert len(violations) == 1
    assert violations[0].section == "project.optional-dependencies.dev"
    assert "dependency removed: pytest-cov" in violations[0].reason


@pytest.mark.unit
def test_pyproject_new_dependency_group_is_blocked() -> None:
    old_text = """
[dependency-groups]
dev = [
    "pytest>=8.0.0",
]
""".strip()
    new_text = """
[dependency-groups]
dev = [
    "pytest>=8.0.0",
]
lint = [
    "ruff>=0.9.0",
]
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=["pyproject.toml"],
        owned_paths=[],
        protected_file_diffs={
            "pyproject.toml": ProtectedFileDiff(
                path="pyproject.toml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert len(violations) == 1
    assert violations[0].section == "dependency-groups.lint"
    assert violations[0].line == 5
    assert "dependency group added: dependency-groups.lint" in violations[0].reason


@pytest.mark.unit
def test_workflow_comment_continue_on_error_is_allowed() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post PR comment
        uses: actions/github-script@v7
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post PR comment
        uses: actions/github-script@v7
        continue-on-error: true
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert violations == []


@pytest.mark.unit
def test_workflow_uses_only_comment_continue_on_error_is_allowed() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: peter-evans/create-or-update-comment@v4
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: peter-evans/create-or-update-comment@v4
        continue-on-error: true
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert violations == []


@pytest.mark.unit
def test_workflow_pytest_continue_on_error_is_blocked() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
        continue-on-error: true
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert len(violations) == 1
    assert violations[0].section == "jobs.tests.steps.Run pytest.continue-on-error"
    assert violations[0].line == 9
    assert "continue-on-error is only allowed for comment/notify steps" in violations[0].reason


@pytest.mark.unit
def test_workflow_removing_pytest_continue_on_error_true_is_allowed() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
        continue-on-error: true
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert violations == []


@pytest.mark.unit
def test_workflow_setting_pytest_continue_on_error_false_is_allowed() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
        continue-on-error: true
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
        continue-on-error: false
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert violations == []


@pytest.mark.unit
def test_workflow_continue_on_error_expression_change_is_blocked() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
        continue-on-error: false
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
        continue-on-error: '${{ matrix.allow_failure }}'
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert len(violations) == 1
    violation = violations[0]
    assert violation.section == "jobs.tests.steps.Run pytest.continue-on-error"
    assert "workflow continue-on-error changed outside allowed comment steps" in violation.reason


@pytest.mark.unit
def test_workflow_comment_validation_command_broadening_is_allowed() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post coverage comment
        run: uv run coverage xml
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post coverage comment
        run: uv run coverage xml && uv run coverage html
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert violations == []


@pytest.mark.unit
def test_workflow_comment_step_new_validation_command_is_blocked() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post coverage comment
        run: echo pending
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post coverage comment
        run: uv run coverage xml
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert len(violations) == 1
    assert violations[0].section == "jobs.tests.steps.Post coverage comment.run"
    assert "introducing validation command is blocked" in violations[0].reason


@pytest.mark.unit
def test_workflow_step_key_line_lookup_scans_long_step_block() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
      - name: Post coverage comment
        env:
          KEY_01: value
          KEY_02: value
          KEY_03: value
          KEY_04: value
          KEY_05: value
          KEY_06: value
          KEY_07: value
          KEY_08: value
          KEY_09: value
          KEY_10: value
          KEY_11: value
          KEY_12: value
          KEY_13: value
          KEY_14: value
          KEY_15: value
        run: echo pending
""".strip()
    new_text = old_text.replace("run: echo pending", "run: uv run coverage xml")
    expected_line = new_text.splitlines().index("        run: uv run coverage xml") + 1

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert len(violations) == 1
    violation = violations[0]
    assert violation.section == "jobs.tests.steps.Post coverage comment.run"
    assert violation.line == expected_line
    assert "introducing validation command is blocked" in violation.reason


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "bash scripts/recovery.sh",
        "bash scripts/discover.sh",
        "test -f config.yaml && echo ok",
        "cp tests/fixtures/golden.json /tmp/",
        "ls tests/",
    ],
)
def test_added_informational_job_ignores_non_validation_command_words(command: str) -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
""".strip()
    new_text = f"""
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
  summary:
    name: Summary report
    runs-on: ubuntu-latest
    steps:
      - name: Summary report
        run: {command}
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert violations == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "job_field",
    [
        "permissions:\n      contents: read",
        "needs: tests",
        "if: ${{ always() }}",
        "environment: production",
    ],
)
def test_added_informational_job_with_privileged_fields_is_blocked(job_field: str) -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
""".strip()
    new_text = f"""
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
  notify:
    name: Notify reviewers
    runs-on: ubuntu-latest
    {job_field}
    steps:
      - name: Notify reviewers
        run: echo "heads up"
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert len(violations) == 1
    violation = violations[0]
    assert violation.section == "jobs.notify"
    assert violation.line == 9
    assert "added workflow jobs must be informational/comment/notify only" in violation.reason


@pytest.mark.unit
def test_workflow_removed_job_is_blocked() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - run: uv run pytest
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: uv run ruff check
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - run: uv run pytest
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert len(violations) == 1
    assert violations[0].section == "jobs.lint"
    assert "workflow job removed" in violations[0].reason


@pytest.mark.unit
def test_workflow_existing_step_reorder_is_blocked() -> None:
    old_text = """
name: Release
on: [pull_request]
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - name: Run tests
        run: uv run pytest
      - name: Publish package
        run: python -m build && twine upload dist/*
""".strip()
    new_text = """
name: Release
on: [pull_request]
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - name: Publish package
        run: python -m build && twine upload dist/*
      - name: Run tests
        run: uv run pytest
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/release.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/release.yml": ProtectedFileDiff(
                path=".github/workflows/release.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert len(violations) == 1
    violation = violations[0]
    assert violation.section == "jobs.release.steps.Publish package"
    assert violation.line == 7
    assert "workflow step order changed" in violation.reason


@pytest.mark.unit
def test_workflow_added_informational_step_preserves_existing_step_order() -> None:
    old_text = """
name: Release
on: [pull_request]
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - name: Run tests
        run: uv run pytest
      - name: Publish package
        run: python -m build && twine upload dist/*
""".strip()
    new_text = """
name: Release
on: [pull_request]
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - name: Notify reviewers
        run: echo "workflow started"
      - name: Run tests
        run: uv run pytest
      - name: Publish package
        run: python -m build && twine upload dist/*
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/release.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/release.yml": ProtectedFileDiff(
                path=".github/workflows/release.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert violations == []


@pytest.mark.unit
def test_workflow_pinned_uses_bump_is_allowed() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run pytest
        run: uv run pytest
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
      - name: Run pytest
        run: uv run pytest
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert violations == []


@pytest.mark.unit
def test_workflow_pinned_uses_sha_to_mutable_major_tag_is_blocked() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
      - name: Run pytest
        run: uv run pytest
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run pytest
        run: uv run pytest
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert len(violations) == 1
    violation = violations[0]
    assert violation.section == "jobs.tests.steps.actions/checkout@v4.uses"
    assert "workflow action changed outside pinned ref bump" in violation.reason


@pytest.mark.unit
def test_workflow_pinned_uses_sha_to_full_semver_is_allowed() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
      - name: Run pytest
        run: uv run pytest
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4.2.0
      - name: Run pytest
        run: uv run pytest
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert violations == []


@pytest.mark.unit
def test_workflow_pinned_uses_version_downgrade_is_blocked() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v4.2.0
      - name: Run pytest
        run: uv run pytest
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v3.0.0
      - name: Run pytest
        run: uv run pytest
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert len(violations) == 1
    violation = violations[0]
    assert violation.section == "jobs.tests.steps.actions/setup-python@v3.0.0.uses"
    assert "workflow action changed outside pinned ref bump" in violation.reason


@pytest.mark.unit
def test_workflow_pinned_uses_version_upgrade_is_allowed() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v3.1.0
      - name: Run pytest
        run: uv run pytest
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v4.2.0
      - name: Run pytest
        run: uv run pytest
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert violations == []


@pytest.mark.unit
def test_workflow_uses_bump_to_mutable_branch_is_blocked() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run pytest
        run: uv run pytest
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@main
      - name: Run pytest
        run: uv run pytest
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert len(violations) == 1
    violation = violations[0]
    assert violation.section == "jobs.tests.steps.actions/checkout@main.uses"
    assert "workflow action changed outside pinned ref bump" in violation.reason


@pytest.mark.unit
def test_added_informational_step_with_uses_is_blocked() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
      - name: Notify reviewers
        uses: attacker/action@main
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert len(violations) == 1
    violation = violations[0]
    assert violation.section == "jobs.tests.steps.Notify reviewers"
    assert violation.line == 9
    assert "added workflow steps must be informational/comment/notify only" in violation.reason


@pytest.mark.unit
def test_added_informational_job_with_comment_action_uses_is_allowed() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
  notify-comment:
    name: Notify reviewers
    runs-on: ubuntu-latest
    steps:
      - uses: peter-evans/create-or-update-comment@v4
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert violations == []


@pytest.mark.unit
def test_added_informational_job_with_uses_is_blocked() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
  notify:
    name: Notify reviewers
    runs-on: ubuntu-latest
    steps:
      - name: Notify reviewers
        uses: attacker/action@main
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert len(violations) == 1
    violation = violations[0]
    assert violation.section == "jobs.notify"
    assert violation.line == 9
    assert "added workflow jobs must be informational/comment/notify only" in violation.reason


@pytest.mark.unit
def test_added_informational_reusable_workflow_job_is_blocked() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
  notify-comment:
    name: Notify reviewers
    uses: org/reusable-notify/.github/workflows/comment.yml@v1
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert len(violations) == 1
    violation = violations[0]
    assert violation.section == "jobs.notify-comment"
    assert violation.line == 9
    assert "added workflow jobs must be informational/comment/notify only" in violation.reason


@pytest.mark.unit
@pytest.mark.parametrize(
    ("old_header", "new_header", "section", "line", "reason"),
    [
        (
            "name: CI\non: [pull_request]",
            "name: CI\non: [push, pull_request]",
            "workflow.on",
            2,
            "workflow top-level field changed outside allowed cases: on",
        ),
        (
            "name: CI\non: [pull_request]\npermissions:\n  contents: read",
            (
                "name: CI\non: [pull_request]\npermissions:\n"
                "  contents: read\n  pull-requests: write"
            ),
            "workflow.permissions",
            3,
            "workflow top-level field changed outside allowed cases: permissions",
        ),
    ],
)
def test_workflow_top_level_field_change_is_blocked(
    old_header: str,
    new_header: str,
    section: str,
    line: int,
    reason: str,
) -> None:
    old_text = f"""
{old_header}
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
""".strip()
    new_text = f"""
{new_header}
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    assert len(violations) == 1
    violation = violations[0]
    assert violation.path == ".github/workflows/ci.yml"
    assert violation.section == section
    assert violation.line == line
    assert violation.reason == reason


@pytest.mark.unit
def test_violation_message_includes_file_section_line_and_reason() -> None:
    old_text = """
[tool.coverage.report]
fail_under = 99
""".strip()
    new_text = """
[tool.coverage.report]
fail_under = 80
""".strip()
    violations = find_protected_quality_gate_changes(
        changed_paths=["pyproject.toml"],
        owned_paths=[],
        protected_file_diffs={
            "pyproject.toml": ProtectedFileDiff(
                path="pyproject.toml",
                old_text=old_text,
                new_text=new_text,
            )
        },
    )

    message = quality_gate_violation_message(violations)

    assert "pyproject.toml" in message
    assert "tool.coverage.report.fail_under" in message
    assert "line 2" in message
    assert "lowered from 99 to 80" in message
    assert "lowering or bypassing" not in message


@pytest.mark.unit
def test_missing_protected_file_diff_blocks_conservatively() -> None:
    violations = find_protected_quality_gate_changes(
        changed_paths=["pyproject.toml"],
        owned_paths=[],
    )

    assert len(violations) == 1
    assert violations[0].section == "pyproject.toml"
    assert "diff unavailable" in violations[0].reason


@pytest.mark.unit
def test_identical_pyproject_diff_has_no_quality_gate_violation() -> None:
    text = """
[project]
name = "demo"
dependencies = [
    "fastapi>=0.115.0",
]
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=["pyproject.toml"],
        owned_paths=[],
        protected_file_diffs={
            "pyproject.toml": ProtectedFileDiff(
                path="pyproject.toml",
                old_text=text,
                new_text=text,
            )
        },
    )

    assert violations == []


@pytest.mark.unit
def test_identical_workflow_diff_has_no_quality_gate_violation() -> None:
    text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
""".strip()

    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text=text,
                new_text=text,
            )
        },
    )

    assert violations == []


@pytest.mark.unit
def test_parse_failure_blocks_conservatively() -> None:
    violations = find_protected_quality_gate_changes(
        changed_paths=["pyproject.toml"],
        owned_paths=[],
        protected_file_diffs={
            "pyproject.toml": ProtectedFileDiff(
                path="pyproject.toml",
                old_text="[project]\nname = 'demo'\n",
                new_text="[project\nname = 'demo'\n",
            )
        },
    )

    assert len(violations) == 1
    assert "could not parse pyproject.toml" in violations[0].reason


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
