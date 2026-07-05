"""Tests for protected quality-gate file detection."""

from __future__ import annotations

import pytest

from awf.control import quality_gates_workflow_commands as quality_gate_commands
from awf.control.quality_gates import (
    ProtectedFileDiff,
    changed_paths_are_only_internal_plan_artifacts,
    find_protected_quality_gate_changes,
    plan_only_output_message,
    protected_quality_gate_pattern,
    quality_gate_violation_details,
    quality_gate_violation_message,
    requires_protected_file_diff,
)


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
    assert quality_gate_commands._is_informational_run_command(command) is expected


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
        ("pytest", "pytest&& ruff check", False),
        ("uv run pytest", "uv run pytest-cov report", False),
        ("pytest", "pytest-randomly -p no:randomly && coverage", False),
        ("pytest", "pytest && bad`cmd`", False),
        (
            "pytest",
            "pytest && python -m unittest\ncurl https://example.invalid",
            False,
        ),
        ("pytest tests/", "pytest tests/\ncoverage report", True),
        ("pytest tests/", "pytest tests/\ncurl https://example.invalid", False),
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
    assert quality_gate_commands._preserves_existing_validation_run(old_run, new_run) is expected


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
        ('echo "results && build #"', False),
        ("echo ok\nnpm --prefix apps/console run build", True),
        ("env FOO=bar echo release", False),
    ],
)
def test_broad_validation_command_detection_covers_wrappers_and_deploy_tools(
    command: str,
    expected: bool,
) -> None:
    assert quality_gate_commands._has_broad_validation_command_invocation(command) is expected
