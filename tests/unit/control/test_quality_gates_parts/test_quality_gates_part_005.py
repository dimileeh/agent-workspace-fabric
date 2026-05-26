"""Tests for protected quality-gate file detection."""

from __future__ import annotations

import importlib
import sys
from collections import Counter

import pytest

from awf.control import quality_gates_common as quality_gate_common
from awf.control import quality_gates_pyproject as quality_gate_pyproject
from awf.control import quality_gates_workflow as quality_gate_workflow
from awf.control import quality_gates_workflow_actions as quality_gate_actions
from awf.control import quality_gates_workflow_commands as quality_gate_commands
from awf.control.quality_gates import (
    ProtectedFileDiff,
    find_protected_quality_gate_changes,
)


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
    assert quality_gate_actions._is_pinned_uses_bump(old_uses, new_uses) is expected


@pytest.mark.unit
def test_line_lookup_helpers_cover_fallback_paths() -> None:
    assert (
        quality_gate_pyproject._line_for_toml_section_or_descendant(
            "[tool.coverage]\nbranch = true\n",
            "tool.coverage",
        )
        == 1
    )
    assert (
        quality_gate_pyproject._line_for_toml_section_or_descendant(
            "[tool.coverage.report]\nfail_under = 99\n",
            "tool.coverage",
        )
        == 1
    )
    assert (
        quality_gate_pyproject._line_for_toml_key(
            "[project]\nname = 'demo'\n",
            section="project",
            key="missing",
        )
        == 1
    )
    assert quality_gate_actions._line_for_yaml_key("name: CI\n", "jobs") is None
    assert (
        quality_gate_workflow._line_for_workflow_step(
            "steps:\n  - run: |\n      echo hi\n",
            {"name": "missing"},
        )
        is None
    )
    assert (
        quality_gate_workflow._line_for_workflow_step_key(
            "run: echo hi\n",
            {"run": "echo hi"},
            key="uses",
        )
        is None
    )
    assert (
        quality_gate_workflow._line_for_workflow_step_key(
            "run: echo hi\nuses: actions/checkout@v4\n",
            {"run": "echo hi"},
            key="uses",
        )
        == 2
    )
    assert (
        quality_gate_workflow._line_for_workflow_step_key(
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
        quality_gate_workflow._line_for_workflow_step(
            workflow_with_multiline_run,
            multiline_run_step,
        )
        == 5
    )
    assert (
        quality_gate_workflow._line_for_workflow_step_key(
            workflow_with_multiline_run,
            multiline_run_step,
            key="continue-on-error",
        )
        == 8
    )


def test_quality_gates_workflow_actions_import_has_no_circular_dependency() -> None:
    module_names = (
        "awf.control.quality_gates_workflow",
        "awf.control.quality_gates_workflow_actions",
        "awf.control.quality_gates_workflow_commands",
    )
    originals = {name: sys.modules.get(name) for name in module_names}
    try:
        for name in module_names:
            sys.modules.pop(name, None)
        imported = importlib.import_module("awf.control.quality_gates_workflow_actions")
        assert imported is not None
        assert imported._is_default_false_continue_on_error is not None
        assert imported._line_for_yaml_key("jobs:\\n  test: {}\\n", "jobs") == 1
    finally:
        for name, module in originals.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


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

    class _MarkedYamlError(quality_gate_workflow.yaml.YAMLError):
        problem_mark = _ProblemMark()

    def _raise_marked_error(_text: str) -> object:
        raise _MarkedYamlError("bad yaml")

    monkeypatch.setattr(quality_gate_workflow.yaml, "safe_load", _raise_marked_error)

    workflow, violation = quality_gate_workflow._parse_workflow_yaml(
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
    assert quality_gate_pyproject._coverage_policy_without_fail_under("strict") == "strict"
    assert quality_gate_pyproject._coverage_policy_without_fail_under(
        {"run": {"branch": True}, "report": {"fail_under": 99}}
    ) == {"run": {"branch": True}}
    assert quality_gate_pyproject._coverage_policy_without_fail_under(
        {"report": {"fail_under": 99, "show_missing": True}}
    ) == {"report": {"show_missing": True}}


@pytest.mark.unit
def test_quality_gate_common_formats_non_string_policy_values() -> None:
    assert quality_gate_common._format_toml_policy_value(None) == "unset"  # noqa: SLF001
    assert quality_gate_common._format_toml_policy_value(99.0) == "99"  # noqa: SLF001
    assert quality_gate_common._format_toml_policy_value("strict") == "'strict'"  # noqa: SLF001
    assert quality_gate_common._format_toml_policy_value(["a", "b"]) == "['a', 'b']"  # noqa: SLF001


@pytest.mark.unit
def test_private_dependency_replacement_helper_falls_back_to_existing_raw_entry() -> None:
    assert (
        quality_gate_pyproject._replacement_dependency_raw(
            old_entries=Counter({"fastapi>=1": 1}),
            new_entries=Counter({"fastapi>=1": 1}),
        )
        == "fastapi>=1"
    )


@pytest.mark.unit
def test_private_workflow_shape_helpers_cover_empty_and_invalid_edges() -> None:
    assert quality_gate_workflow._workflow_steps({}) == []
    assert quality_gate_workflow._is_informational_job("tests", {"steps": []}) is False
    assert (
        quality_gate_workflow._is_informational_job(
            "summary",
            {"name": "Summary report", "steps": "echo ok"},
        )
        is False
    )
    assert (
        quality_gate_workflow._is_informational_job(
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
        quality_gate_workflow._is_informational_job(
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
        quality_gate_workflow._is_informational_job(
            "summary",
            {
                "name": "Summary report",
                "permissions": {"packages": "write"},
                "steps": [{"name": "Summary report", "run": "echo ok"}],
            },
        )
        is False
    )
    assert (
        quality_gate_workflow._is_informational_step({"name": "Deploy", "run": "echo ok"}) is False
    )


@pytest.mark.unit
def test_private_shell_and_validation_helpers_cover_remaining_parser_edges() -> None:
    assert quality_gate_commands._informational_shell_command_is_safe(()) is True
    assert quality_gate_commands._validation_run_append_commands("; ruff check") is None
    assert quality_gate_commands._preserves_existing_validation_run(
        "pytest",
        "pytest && ruff check && pytest tests/unit",
    )
    assert not quality_gate_commands._preserves_existing_validation_run(
        "pytest",
        "pytest && env CI=true",
    )
    assert not quality_gate_commands._preserves_existing_validation_run(
        "pytest",
        "pytest && npm --prefix apps/console",
    )
    assert not quality_gate_commands._preserves_existing_validation_run(
        "pytest",
        "pytest && npm run --",
    )
    assert quality_gate_commands._has_broad_validation_command_invocation("build")
    assert not quality_gate_commands._has_broad_validation_command_invocation(
        "npm --prefix apps/console"
    )
    assert not quality_gate_commands._has_broad_validation_command_invocation("python build.py")
    assert not quality_gate_commands._docker_runs_broad_validation_command(())


@pytest.mark.unit
def test_private_uses_ref_helpers_cover_invalid_and_short_version_edges() -> None:
    assert not quality_gate_commands._is_comment_or_notify_capable_step_uses(
        {},
        "actions/github-script",
    )
    assert not quality_gate_actions._is_workflow_version_ref_non_downgrade("v1", "main")
    assert not quality_gate_actions._is_full_workflow_version_ref("main")
    assert quality_gate_actions._workflow_version_ref_sort_key("main") is None
    assert quality_gate_actions._workflow_version_ref_sort_key("v1")[0] == (1, 0, 0)
    assert quality_gate_actions._uses_action("actions/checkout") is None


@pytest.mark.unit
def test_private_pyproject_policy_helpers_cover_generic_policy_edges() -> None:
    old_text = """
[tool.ruff]
line-length = 100
""".strip()
    new_text = """
[tool.ruff]
line-length = 88
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
    assert violations[0].section == "tool.ruff"
    assert violations[0].line == 1

    coverage_violations = find_protected_quality_gate_changes(
        changed_paths=["pyproject.toml"],
        owned_paths=[],
        protected_file_diffs={
            "pyproject.toml": ProtectedFileDiff(
                path="pyproject.toml",
                old_text="[tool.coverage.run]\nbranch = true\n",
                new_text="[tool.coverage.run]\nbranch = false\n",
            )
        },
    )

    assert len(coverage_violations) == 1
    assert coverage_violations[0].section == "tool.coverage"
    assert quality_gate_common._format_toml_policy_value(None) == "unset"
    assert quality_gate_pyproject._coverage_policy_without_fail_under({"report": "strict"}) == {
        "report": "strict"
    }
    assert (
        quality_gate_pyproject._dependency_group_entry_unsupported_reason(
            section="dependency-groups.dev",
            value=[{"include-group": "test"}],
        )
        == "dependency section contains PEP 735 include-group entries that "
        "require ownership of pyproject.toml for evaluation: dependency-groups.dev"
    )


@pytest.mark.unit
def test_private_workflow_job_key_helpers_cover_fail_closed_edges() -> None:
    class _DuplicateStringKey:
        def __str__(self) -> str:
            return "duplicate"

    assert (
        quality_gate_workflow._workflow_jobs(
            {"jobs": {_DuplicateStringKey(): {}, _DuplicateStringKey(): {}}},
            "jobs: {}\n",
        )
        is None
    )
    assert quality_gate_workflow._workflow_jobs({"jobs": []}, "jobs: []\n") is None
    assert quality_gate_workflow._workflow_job_id(object(), "jobs: {}\n").startswith("<object")
    assert quality_gate_workflow._workflow_source_scalar_key_name(True, ("yes", "on")) is None
    assert quality_gate_workflow._workflow_job_key_source_names("name: CI\n") == ()
    assert quality_gate_workflow._workflow_jobs_mapping_node("jobs:\n  - test\n") is None
    assert quality_gate_workflow._workflow_jobs_mapping_node("- test\n") is None
    assert not quality_gate_workflow._yaml_scalar_key_matches_loaded_key("[bad", object())
    assert not quality_gate_workflow._yaml_scalar_key_matches_loaded_key("a: b", "a")


@pytest.mark.unit
def test_private_informational_safety_helpers_cover_rejected_edges() -> None:
    assert not quality_gate_workflow._informational_job_permissions_are_safe(["read"])
    assert quality_gate_workflow._safe_informational_env_names("bad") is None
    assert quality_gate_workflow._safe_informational_env_names({1: "value"}) is None
    assert quality_gate_workflow._safe_informational_env_names({"PATH": "value"}) is None
    assert quality_gate_workflow._safe_informational_env_names({"OK": object()}) is None
    assert not quality_gate_workflow._is_safe_informational_env_value(object())
    assert quality_gate_commands._informational_shell_tokens_are_safe(()) is True
    assert quality_gate_commands._informational_shell_tokens_are_safe(("echo", "ok")) is True

    safe_env_names: set[str] = set()
    quality_gate_commands._remember_safe_informational_assignments(
        ("SUMMARY=value",),
        safe_env_names,
    )
    quality_gate_commands._remember_safe_informational_assignments(
        ("TOKEN=value",),
        safe_env_names,
    )
    assert safe_env_names == {"SUMMARY"}
    assert quality_gate_commands._has_unsafe_informational_parameter_expansion(
        ("$SUMMARY",),
        {"PATH"},
    )
    assert quality_gate_commands._has_unsafe_github_actions_expression(
        ('echo "${{ github.sha }} ${{"',)
    )
    assert quality_gate_commands._is_validation_command("'unterminated pytest")
    assert not quality_gate_commands._is_validation_command("'unterminated docs")
    assert quality_gate_commands._raw_validation_command_match("python -m unittest")


@pytest.mark.unit
def test_private_validation_command_helpers_cover_wrapper_edges() -> None:
    assert quality_gate_commands._shell_command_tokens_are_validation(("env", "CI=true", "pytest"))
    assert not quality_gate_commands._run_wrapper_runs_validation_command(("--python", "3.12"))
    assert not quality_gate_commands._command_words_start_validation_command(())
    assert quality_gate_commands._command_words_start_validation_command(
        ("python", "-I", "tests/unit")
    )
    assert quality_gate_commands._command_words_start_validation_command(
        ("npm", "--prefix", "apps/console", "run", "test")
    )
    assert quality_gate_commands._command_words_start_validation_command(("make", "lint"))
    assert quality_gate_commands._python_runs_validation_command(("-I", "-m", "unittest"))
    assert not quality_gate_commands._python_runs_validation_command(("-I", "script.py"))
    assert not quality_gate_commands._package_manager_runs_any_validation_command(
        ("--prefix", "apps/console", "run")
    )
    assert not quality_gate_commands._package_manager_runs_any_validation_command(
        ("--prefix", "apps/console")
    )
    assert quality_gate_commands._package_manager_runs_any_validation_command(("exec", "pytest"))
    assert quality_gate_commands._validation_run_append_commands("\\") is None
    assert quality_gate_commands._validation_run_append_commands("&& 'unterminated") is None
    assert quality_gate_commands._validation_run_append_commands("ruff check") is None
    assert quality_gate_commands._validation_run_append_commands("") is None
    assert quality_gate_commands._validation_run_append_commands(
        "&& ruff check\npytest tests/unit"
    ) == (("ruff", "check"), ("pytest", "tests/unit"))
    assert not quality_gate_commands._validation_run_append_command_is_safe(())
    assert not quality_gate_commands._validation_run_append_command_is_safe(("echo", "ok"))
    assert not quality_gate_commands._validation_run_append_command_is_safe(("python", "script.py"))
    assert quality_gate_commands._validation_run_append_command_is_safe(("tox", "test"))
    assert not quality_gate_commands._package_manager_runs_validation_command(("exec", "--prefix"))
    assert quality_gate_commands._package_manager_runs_validation_command(
        ("exec", "coverage", "xml")
    )
    assert quality_gate_commands._package_manager_runs_validation_command(("exec", "pytest"))
    assert not quality_gate_commands._coverage_runs_safe_report_command(())
    assert quality_gate_commands._coverage_runs_safe_report_command(
        ("--rcfile", "pyproject.toml", "xml")
    )
    assert quality_gate_commands._coverage_runs_safe_report_command(
        ("--rcfile=pyproject.toml", "xml")
    )
    assert quality_gate_commands._strip_run_wrapper_options(("--extra=dev", "pytest")) == (
        "pytest",
    )


@pytest.mark.unit
def test_private_broad_command_helpers_cover_fallback_and_negative_edges() -> None:
    assert quality_gate_commands._has_broad_validation_command_invocation("build 'unterminated")
    assert quality_gate_commands._shell_command_word_segments("'unterminated") is None
    assert quality_gate_commands._shell_words("   ") == ()
    assert quality_gate_commands._shell_words("'unterminated build") == ("'unterminated", "build")
    assert not quality_gate_commands._words_start_broad_validation_command(())
    assert not quality_gate_commands._words_start_broad_validation_command(
        ("python", "-I", "noop.py")
    )
    assert not quality_gate_commands._words_start_broad_validation_command(("bash",))
    assert quality_gate_commands._strip_shell_command_prefixes(
        (
            "env",
            "-i",
            "CI=true",
            "uv",
            "run",
            "--python",
            "3.12",
            "pytest",
        )
    ) == ("pytest",)
    assert not quality_gate_commands._package_manager_runs_broad_validation_command(
        ("run", "--prefix", "apps/console")
    )
    assert not quality_gate_commands._package_manager_runs_broad_validation_command(("run",))
    assert not quality_gate_commands._python_runs_broad_validation_module(("-I", "script.py"))
    assert not quality_gate_commands._docker_runs_broad_validation_command(("run", "image"))
    assert not quality_gate_commands._known_broad_validation_command_pair("gh", ())
    assert not quality_gate_commands._known_broad_validation_command_pair("gcloud", ("run",))
    assert not quality_gate_commands._known_broad_validation_command_pair(
        "netlify",
        ("build",),
    )
    assert not quality_gate_commands._known_broad_validation_command_pair("twine", ("check",))
    assert not quality_gate_commands._known_broad_validation_command_pair("custom", ("deploy",))


@pytest.mark.unit
def test_private_comment_notify_input_helpers_cover_invalid_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = "peter-evans/create-or-update-comment"
    assert not quality_gate_actions._comment_notify_action_with_inputs_are_safe(action, "bad")
    assert not quality_gate_actions._comment_notify_action_with_inputs_are_safe(action, {1: "body"})
    assert not quality_gate_actions._comment_notify_action_with_inputs_are_safe(
        action,
        {"unknown": "body"},
    )
    assert not quality_gate_actions._comment_notify_action_with_inputs_are_safe(
        action,
        {"body": "${{ secrets.GITHUB_TOKEN }}"},
    )
    assert quality_gate_actions._comment_notify_action_with_inputs_are_safe(
        action,
        {"body": "ok", "comment-id": 123},
    )
    assert quality_gate_commands._is_comment_or_notify_capable_step_uses(
        {"name": "Post PR comment", "uses": "peter-evans/create-or-update-comment@v4"},
        "peter-evans/create-or-update-comment@v4",
    )
    monkeypatch.setattr(
        quality_gate_commands,
        "_COMMENT_NOTIFY_CAPABLE_ACTION_USES",
        frozenset({"example/commenter"}),
    )
    assert quality_gate_commands._is_comment_or_notify_capable_step_uses(
        {"name": "Post PR comment", "uses": "example/commenter@v1"},
        "example/commenter@v1",
    )

    assert not quality_gate_actions._github_script_comment_notify_inputs_are_safe(None)
    assert not quality_gate_actions._github_script_comment_notify_inputs_are_safe("bad")
    assert not quality_gate_actions._github_script_comment_notify_inputs_are_safe({1: "script"})
    assert not quality_gate_actions._github_script_comment_notify_inputs_are_safe(
        {"unknown": "script"}
    )
    assert not quality_gate_actions._github_script_comment_notify_inputs_are_safe({"script": 1})
    assert not quality_gate_actions._github_script_comment_notify_inputs_are_safe(
        {"script": "${{ secrets.GITHUB_TOKEN }}"}
    )
    assert not quality_gate_actions._github_script_comment_notify_inputs_are_safe(
        {
            "debug": "${{ secrets.GITHUB_TOKEN }}",
            "script": "github.rest.issues.createComment({})",
        }
    )
    assert not quality_gate_actions._github_script_comment_notify_script_is_safe("core.info('x')")
    assert not quality_gate_actions._github_script_comment_notify_script_is_safe("console.log('x')")
    assert not quality_gate_actions._github_script_comment_notify_script_is_safe(
        "github.rest.repos.listForOrg({})"
    )
    assert quality_gate_actions._comment_notify_action_with_value_is_safe(None)
    assert quality_gate_actions._comment_notify_action_with_value_is_safe(True)


@pytest.mark.unit
def test_private_workflow_with_input_helpers_cover_invalid_and_safe_shapes() -> None:
    assert not quality_gate_actions._workflow_pinned_bump_with_inputs_are_safe(
        new_uses=None,
        old_inputs={},
        new_inputs={},
    )
    assert not quality_gate_actions._workflow_pinned_bump_with_inputs_are_safe(
        new_uses="actions/setup-python@v5",
        old_inputs="bad",
        new_inputs={},
    )
    assert quality_gate_actions._workflow_pinned_bump_with_inputs_are_safe(
        new_uses="actions/setup-python@v5",
        old_inputs={"python-version": "3.12"},
        new_inputs={"python-version": "3.12"},
    )
    assert not quality_gate_actions._workflow_pinned_bump_with_inputs_are_safe(
        new_uses="actions/setup-python@v5",
        old_inputs={"cache": "pip"},
        new_inputs={"cache": "uv"},
    )
    assert not quality_gate_actions._workflow_pinned_bump_with_inputs_are_safe(
        new_uses="actions/setup-python@v5",
        old_inputs={},
        new_inputs={"python-version": "3.12"},
    )
    assert not quality_gate_actions._workflow_pinned_bump_with_inputs_are_safe(
        new_uses="actions/setup-python@v5",
        old_inputs={"python-version": "3.11"},
        new_inputs={"python-version": "${{ secrets.PYTHON_VERSION }}"},
    )
    assert quality_gate_actions._workflow_pinned_bump_allowed_with_keys("bad") == frozenset()
    assert quality_gate_actions._normalized_workflow_with_inputs(None) == {}
    assert quality_gate_actions._normalized_workflow_with_inputs("bad") is None
    assert quality_gate_actions._normalized_workflow_with_inputs({1: "value"}) is None
    assert quality_gate_actions._normalized_workflow_with_inputs({" ": "value"}) is None
    assert (
        quality_gate_actions._normalized_workflow_with_inputs(
            {"python_version": "3.12", "python-version": "3.13"}
        )
        is None
    )
    assert quality_gate_actions._workflow_with_inputs_have_safe_names_and_values(None)
    assert not quality_gate_actions._workflow_with_inputs_have_safe_names_and_values("bad")
    assert not quality_gate_actions._workflow_with_inputs_have_safe_names_and_values({1: "value"})
    assert not quality_gate_actions._workflow_with_inputs_have_safe_names_and_values(
        {"token": "value"}
    )
    assert not quality_gate_actions._workflow_with_inputs_have_safe_names_and_values(
        {"body": "${{ secrets.GITHUB_TOKEN }}"}
    )
    assert not quality_gate_actions._workflow_with_inputs_have_safe_names_and_values(
        {"count": object()}
    )
    assert quality_gate_actions._workflow_with_input_value_is_safe(None)
    assert quality_gate_actions._workflow_with_input_value_is_safe(1)
    assert not quality_gate_actions._workflow_with_input_value_is_safe(object())
    assert not quality_gate_actions._workflow_with_input_value_is_safe(
        "${{ secrets.GITHUB_TOKEN }}"
    )


@pytest.mark.unit
def test_private_line_lookup_helpers_cover_marker_scan_and_yaml_errors() -> None:
    workflow_text = """
steps:
  - name: Build
    id: actual
    run: pytest
  - name: Comment
    run: echo ok
""".strip()

    assert (
        quality_gate_workflow._line_for_workflow_step_key(
            workflow_text,
            {"name": "Build", "id": "expected"},
            key="run",
        )
        == 4
    )
    assert (
        quality_gate_workflow._line_for_workflow_step_key_from_yaml_nodes(
            "name: [\n",
            {"name": "Build"},
            key="run",
        )
        is None
    )
    assert quality_gate_workflow._compose_workflow_yaml_document("name: [\n") is None


@pytest.mark.unit
def test_private_policy_and_expression_helpers_cover_base_branch_edges() -> None:
    old_text = "[tool.ruff]\nline-length = 100\n"
    new_text = "[tool.ruff]\nline-length = 120\n"

    violations = quality_gate_pyproject._pyproject_policy_section_violations(
        path="pyproject.toml",
        protected_pattern="pyproject.toml",
        old_doc={"tool": {"ruff": {"line-length": 100}}},
        new_doc={"tool": {"ruff": {"line-length": 120}}},
        old_text=old_text,
        new_text=new_text,
    )

    assert [violation.section for violation in violations] == ["tool.ruff"]
    assert quality_gate_commands._has_unsafe_informational_parameter_expansion(("echo", "${TOKEN}"))
    assert quality_gate_commands._has_unsafe_informational_parameter_expansion(
        ("echo", "$AWS_SECRET_ACCESS_KEY")
    )
    assert quality_gate_commands._has_unsafe_informational_parameter_expansion(
        ("echo", "$CUSTOM"),
        safe_env_names=set(),
    )
    assert not quality_gate_commands._has_unsafe_informational_parameter_expansion(
        ("echo", "$CUSTOM"),
        safe_env_names={"CUSTOM"},
    )
    assert quality_gate_commands._has_unsafe_github_actions_expression(
        ("echo", "${{ github.sha }} ${{")
    )
    assert quality_gate_commands._has_unsafe_github_actions_expression(
        ("echo", "${{ secrets.TOKEN }}")
    )
    assert quality_gate_commands._is_validation_command("pytest 'unterminated")
    assert quality_gate_commands._shell_tokens_include_validation_command(
        ("ruff", "check", "&&", "echo", "done")
    )
    assert not quality_gate_commands._run_wrapper_runs_validation_command(())
    assert not quality_gate_commands._package_manager_runs_any_validation_command(())
    assert not quality_gate_commands._package_manager_runs_any_validation_command(("run",))
    assert (
        quality_gate_workflow._workflow_jobs(
            {"jobs": {1: {}, "1": {}}},
            'jobs:\n  1: {}\n  "1": {}\n',
        )
        is None
    )
    assert quality_gate_workflow._workflow_job_id(object(), "") is not None
    assert quality_gate_workflow._workflow_source_scalar_key_name(True, ("true", "True")) is None
    assert quality_gate_workflow._workflow_job_key_source_names("name: CI\n") == ()
    assert quality_gate_workflow._workflow_jobs_mapping_node("jobs: []\n") is None
    assert not quality_gate_workflow._yaml_scalar_key_matches_loaded_key("[bad", "bad")
    assert not quality_gate_workflow._informational_job_permissions_are_safe([])
    assert quality_gate_workflow._safe_informational_env_names([]) is None
    assert quality_gate_workflow._safe_informational_env_names({1: "value"}) is None
    assert not quality_gate_workflow._is_safe_informational_env_value(object())
    assert not quality_gate_commands._is_validation_command("echo 'unterminated")
    assert quality_gate_commands._strip_validation_command_prefixes(
        ("env", "FOO=bar", "pytest")
    ) == ("pytest",)
    assert quality_gate_commands._command_words_start_validation_command(("npm", "test"))
    assert quality_gate_commands._command_words_start_validation_command(("pytest", "tests/unit"))
    assert not quality_gate_commands._package_manager_runs_validation_command(("exec",))
    assert not quality_gate_commands._coverage_runs_safe_report_command(())
    assert quality_gate_commands._strip_coverage_options(
        ("--data-file", ".coverage", "report")
    ) == ("report",)
    assert not quality_gate_commands._has_broad_validation_command_invocation(
        "pytest 'unterminated"
    )
    assert quality_gate_commands._shell_words(" 'unterminated") == ("'unterminated",)
    assert not quality_gate_actions._comment_notify_action_with_inputs_are_safe(
        "peter-evans/create-or-update-comment",
        [],
    )
    assert not quality_gate_actions._comment_notify_action_with_inputs_are_safe(
        "peter-evans/create-or-update-comment",
        {1: "body"},
    )
    assert not quality_gate_actions._github_script_comment_notify_inputs_are_safe(None)
    assert not quality_gate_actions._github_script_comment_notify_inputs_are_safe([])
    assert not quality_gate_actions._github_script_comment_notify_inputs_are_safe({1: "value"})
    assert not quality_gate_actions._github_script_comment_notify_inputs_are_safe({"script": 1})
    assert not quality_gate_actions._github_script_comment_notify_inputs_are_safe(
        {"script": "${{ secrets.TOKEN }}"}
    )
    assert not quality_gate_actions._github_script_comment_notify_inputs_are_safe(
        {"script": "console.log('done')"}
    )
    assert not quality_gate_actions._comment_notify_action_with_value_is_safe(object())
    assert not quality_gate_actions._workflow_pinned_bump_with_inputs_are_safe(
        new_uses="actions/setup-node@v4",
        old_inputs=[],
        new_inputs={},
    )
    assert quality_gate_actions._workflow_pinned_bump_with_inputs_are_safe(
        new_uses="actions/setup-node@v4",
        old_inputs={"node-version": "20"},
        new_inputs={"node-version": "20"},
    )
    assert not quality_gate_actions._workflow_pinned_bump_with_inputs_are_safe(
        new_uses="actions/setup-node@v4",
        old_inputs={"node-version": "20"},
        new_inputs={"cache-dependency-path": "package-lock.json"},
    )
    assert quality_gate_actions._workflow_pinned_bump_allowed_with_keys("not-an-action") == (
        frozenset()
    )
    assert quality_gate_actions._normalized_workflow_with_inputs([]) is None
    assert quality_gate_actions._normalized_workflow_with_inputs({1: "value"}) is None
    assert (
        quality_gate_actions._normalized_workflow_with_inputs({"foo_bar": 1, "foo-bar": 2}) is None
    )
    assert not quality_gate_actions._workflow_with_inputs_have_safe_names_and_values([])
    assert not quality_gate_actions._workflow_with_inputs_have_safe_names_and_values(
        {"name": object()}
    )
    assert quality_gate_actions._workflow_with_input_value_is_safe(None)
    assert not quality_gate_actions._workflow_with_input_value_is_safe(object())
    assert (
        "PEP 735 include-group"
        in quality_gate_pyproject._dependency_group_entry_unsupported_reason(
            section="dependency-groups.dev",
            value=[{"include-group": "test"}],
        )
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(quality_gate_workflow.yaml, "safe_load", lambda _text: [])
        assert not quality_gate_workflow._yaml_scalar_key_matches_loaded_key("unused", "key")
    assert quality_gate_commands._informational_shell_tokens_are_safe(()) is True
    assert not quality_gate_commands._command_words_start_validation_command(("make", "build"))
    assert quality_gate_commands._command_words_start_validation_command(("python", "-m", "pytest"))
    assert quality_gate_commands._validation_run_append_commands(" && echo \\") is None
    assert quality_gate_commands._validation_run_append_commands(" && echo 'unterminated") is None
    assert quality_gate_commands._validation_run_append_commands(" ruff check") is None
    assert quality_gate_commands._validation_run_append_commands("") is None
    assert quality_gate_commands._package_manager_runs_validation_command(
        ("exec", "coverage", "xml")
    )
    assert quality_gate_commands._package_manager_runs_validation_command(("exec", "check"))
    assert quality_gate_commands._shell_words("   ") == ()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            quality_gate_commands,
            "_COMMENT_NOTIFY_CAPABLE_ACTION_USES",
            frozenset({"custom/action"}),
        )
        assert quality_gate_commands._is_comment_or_notify_capable_step_uses(
            {
                "name": "Notify",
                "uses": "custom/action@0123456789abcdef0123456789abcdef01234567",
            },
            "custom/action@0123456789abcdef0123456789abcdef01234567",
        )
    assert not quality_gate_actions._github_script_comment_notify_inputs_are_safe(
        {
            "script": "github.rest.issues.createComment({})",
            "debug": object(),
        }
    )
    assert quality_gate_actions._comment_notify_action_with_value_is_safe(None)
    assert not quality_gate_actions._workflow_pinned_bump_with_inputs_are_safe(
        new_uses="actions/cache@v4",
        old_inputs={},
        new_inputs={"key": "cache-key"},
    )
    assert quality_gate_actions._workflow_with_inputs_have_safe_names_and_values(None)
    assert quality_gate_common._format_toml_policy_value(None) == "unset"
    assert (
        quality_gate_workflow._line_for_workflow_step_key_from_yaml_nodes(
            "jobs: [",
            {},
            key="run",
        )
        is None
    )
    assert quality_gate_workflow._compose_workflow_yaml_document("jobs: [") is None
    workflow_text = "\n".join(
        (
            "jobs: [",
            "  test:",
            "    steps:",
            "      - name: Build",
            "        run: make build",
            "      - name: Test",
            "        run: make test",
        )
    )
    assert (
        quality_gate_workflow._line_for_workflow_step_key(
            workflow_text,
            {"name": "Build", "run": "make build"},
            key="run",
        )
        == 5
    )


@pytest.mark.unit
def test_workflow_action_policy_formatting_helpers_cover_edge_shapes() -> None:
    assert quality_gate_actions._nested_value({"outer": {"inner": 1}}, ("outer", "inner")) == 1
    assert quality_gate_actions._nested_value({"outer": "leaf"}, ("outer", "inner")) is None
    assert quality_gate_actions._nested_value({}, ("missing",)) is None

    assert quality_gate_actions._is_number(1)
    assert quality_gate_actions._is_number(1.5)
    assert not quality_gate_actions._is_number(True)
    assert not quality_gate_actions._is_number("1")

    assert quality_gate_actions._format_number(3.0) == "3"
    assert quality_gate_actions._format_number(3.25) == "3.25"
    assert quality_gate_actions._format_toml_policy_value(None) == "unset"
    assert quality_gate_actions._format_toml_policy_value(4) == "4"
    assert quality_gate_actions._format_toml_policy_value("strict") == "'strict'"
