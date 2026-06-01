"""Scoped coverage-gate tests for awf.cli.companion_env.

These exercise two code paths that the broader ``test_companion_env.py`` suite
does not reach:

* ``_build_name_index`` skipping a companion whose ``name`` is ``None`` /
  absent (the false side of ``if name is not None``), driven through the public
  ``merge_companion_env`` entry point.
* ``_shallow_copy_companion`` rejecting a non-dict ``environment`` field. The
  public ``merge_companion_env`` pre-validates ``environment`` and raises before
  the helper runs, so this defensive guard is verified by calling the helper
  directly with the same contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.cli.companion_env import (
    _shallow_copy_companion,
    merge_companion_env,
)


def _write_env(path: Path, content: str) -> str:
    path.write_text(content, encoding="utf-8")
    return str(path)


@pytest.mark.unit
def test_nameless_companion_is_skipped_by_name_index(tmp_path: Path) -> None:
    """A companion with no ``name`` takes the ``name is None`` branch (170->exit).

    ``_build_name_index`` must not register a name-less companion, and the
    targeted companion that follows must still be merged correctly. This proves
    the false side of ``if name is not None`` is reached without crashing.
    """
    env_file = _write_env(tmp_path / "a.env", "FOO=bar\n")
    companions: list[dict[str, object]] = [
        {"image": "sidecar"},  # no "name" key -> name is None -> skipped
        {"name": "db", "image": "postgres"},
    ]

    result = merge_companion_env(
        companions,
        env_from=[("db", env_file)],
        env_exclude=[],
    )

    # The name-less companion passes through untouched (never indexed, never
    # mutated, no environment injected because it was not targeted).
    assert result[0] == {"image": "sidecar"}
    assert "environment" not in result[0]
    # The named companion got the merged env from the file.
    assert result[1]["environment"] == {"FOO": "bar"}


@pytest.mark.unit
def test_nameless_companion_cannot_be_targeted(tmp_path: Path) -> None:
    """A name-less companion is absent from the index, so targeting it raises.

    This drives the same ``name is None`` skip (170->exit) and then confirms the
    observable consequence: the companion is not addressable by env-from.
    """
    env_file = _write_env(tmp_path / "a.env", "FOO=bar\n")
    with pytest.raises(ValueError, match="no companion with that name"):
        merge_companion_env(
            [{"image": "sidecar"}],  # no name -> not registered in the index
            env_from=[("sidecar", env_file)],
            env_exclude=[],
        )


@pytest.mark.unit
def test_shallow_copy_rejects_non_dict_environment() -> None:
    """``_shallow_copy_companion`` raises on a non-dict ``environment`` (253-255).

    The error message must name the companion and report the offending type.
    """
    with pytest.raises(ValueError, match="non-object 'environment'") as excinfo:
        _shallow_copy_companion({"name": "db", "environment": ["not", "a", "dict"]})

    message = str(excinfo.value)
    assert "'db'" in message
    assert "list" in message  # reports type(env).__name__


@pytest.mark.unit
def test_shallow_copy_non_dict_environment_uses_unknown_fallback() -> None:
    """Without a ``name``, the guard falls back to ``<unknown>`` (line 254)."""
    with pytest.raises(ValueError, match="non-object 'environment'") as excinfo:
        _shallow_copy_companion({"environment": "scalar-string"})

    message = str(excinfo.value)
    assert "<unknown>" in message
    assert "str" in message


@pytest.mark.unit
def test_shallow_copy_isolates_environment_dict() -> None:
    """Sanity: a valid environment dict is deep-copied (mutations do not leak)."""
    original: dict[str, object] = {"name": "db", "environment": {"FOO": "bar"}}
    copy = _shallow_copy_companion(original)

    env = copy["environment"]
    assert isinstance(env, dict)
    env["FOO"] = "mutated"
    # Original env dict is untouched.
    assert original["environment"] == {"FOO": "bar"}
