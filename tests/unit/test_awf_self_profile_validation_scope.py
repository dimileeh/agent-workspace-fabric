"""Guard test: the awf-self profile's validate phase must exercise all of src/awf.

Regression guard for #512. The `awf-self` profile in `.awf/workspace.yml` once
scoped its `validate` phase to only the CLI slice (`src/awf/cli`,
`tests/unit/cli`). The post-validation plan-conformance gate
(`_run_post_validation_conformance_check`) feeds the recorded validation
commands to the conformance agent as evidence; a CLI-only scope made every
non-CLI fix (worker, service, gc) look unexercised and falsely failed the
workspace as `agent_failure`.

These assertions trip if the validate phase is ever silently re-narrowed back
to the CLI slice.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml


def _validate_commands() -> list[list[str]]:
    """Tokenized validate-phase commands of the real awf-self profile."""
    profile_path = Path(__file__).resolve().parents[2] / ".awf" / "workspace.yml"
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))["awf"]
    commands: list[list[str]] = []
    for entry in profile["phases"]["validate"]:
        raw = entry["command"] if isinstance(entry, dict) else entry
        commands.append(shlex.split(raw))
    return commands


def _commands_for_tool(tool: str) -> list[list[str]]:
    return [tokens for tokens in _validate_commands() if tool in tokens]


def _targets_path(tokens: list[str], target: str) -> bool:
    """Return True if any token names ``target`` or a parent of it.

    Uses ``pathlib.Path`` comparison so equivalent spellings — ``src/awf`` vs
    ``src/awf/`` — match identically and a trailing slash can never cause a
    false failure. ``Path(str)`` never raises, so no defensive guard is needed.
    """
    target_path = Path(target)
    return any(Path(token) == target_path or Path(token) in target_path.parents for token in tokens)


def _targets_subpath(tokens: list[str], target: str) -> bool:
    """Return True if any token names ``target`` or a path nested under it."""
    target_path = Path(target)
    return any(Path(token) == target_path or target_path in Path(token).parents for token in tokens)


@pytest.mark.unit
def test_validate_ruff_covers_whole_package_not_only_cli() -> None:
    ruff_commands = _commands_for_tool("ruff")
    assert ruff_commands, "validate phase must run ruff"
    # A `src/awf` token (or a parent of it) covers the whole package; a
    # `src/awf/cli` subpath does not, so this does not match the CLI-only scope.
    assert any(_targets_path(tokens, "src/awf") for tokens in ruff_commands), (
        "ruff must lint the whole src/awf package, not only src/awf/cli"
    )
    # The `tests` target must also survive: dropping it from
    # `ruff check src/awf tests` would silently narrow linting coverage of the
    # test tree without tripping the src/awf guard above.
    assert any(_targets_path(tokens, "tests") for tokens in ruff_commands), (
        "ruff must also lint the tests tree, not only src/awf"
    )


@pytest.mark.unit
def test_validate_mypy_covers_whole_package_not_only_cli() -> None:
    mypy_commands = _commands_for_tool("mypy")
    assert mypy_commands, "validate phase must run mypy"
    assert any(_targets_path(tokens, "src/awf") for tokens in mypy_commands), (
        "mypy must type-check the whole src/awf package, not only src/awf/cli"
    )


@pytest.mark.unit
def test_validate_pytest_covers_full_unit_suite_not_only_cli() -> None:
    pytest_commands = _commands_for_tool("pytest")
    assert pytest_commands, "validate phase must run pytest"
    assert any(_targets_path(tokens, "tests/unit") for tokens in pytest_commands), (
        "pytest must run the full tests/unit suite, not only tests/unit/cli"
    )


@pytest.mark.unit
def test_no_validate_command_is_scoped_solely_to_the_cli_slice() -> None:
    """Anti-re-narrowing guard: document what 'too narrow' looks like.

    A command that targets the CLI slice (`src/awf/cli` or `tests/unit/cli`)
    without also covering the broad `src/awf` / `tests/unit` target is exactly
    the regression from #512. Path-based matching means a trailing slash or a
    deeper CLI subpath cannot bypass this guard.
    """
    for tokens in _validate_commands():
        targets_cli = _targets_subpath(tokens, "src/awf/cli") or _targets_subpath(
            tokens, "tests/unit/cli"
        )
        if targets_cli:
            targets_broad = _targets_path(tokens, "src/awf") or _targets_path(tokens, "tests/unit")
            assert targets_broad, (
                f"validate command {tokens!r} is scoped solely to the CLI slice; "
                "it must also cover the broad src/awf / tests/unit target so "
                "conformance exercises non-CLI changes"
            )
