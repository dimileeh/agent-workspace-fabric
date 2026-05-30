"""Tests for companion env merge logic (merge_companion_env)."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.cli.companion_env import merge_companion_env


def _c(*names_envs: tuple[str, dict[str, str]]) -> list[dict]:
    return [{"name": n, "repo_url": f"git@x:{n}.git", "environment": e} for n, e in names_envs]


# ---------------------------------------------------------------------------
# Merge precedence: payload wins over file
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_payload_wins_over_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DB_HOST=localhost\nDB_PORT=5432\n")
    companions = _c(("aira-agent", {"DB_HOST": "postgres:5432"}))
    result = merge_companion_env(
        companions,
        env_from=[("aira-agent", str(env_file))],
        env_exclude=[],
    )
    assert result[0]["environment"]["DB_HOST"] == "postgres:5432"
    assert result[0]["environment"]["DB_PORT"] == "5432"


@pytest.mark.unit
def test_file_fills_gaps(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\nB=2\n")
    companions = _c(("app", {"A": "from-payload"}))
    result = merge_companion_env(
        companions,
        env_from=[("app", str(env_file))],
        env_exclude=[],
    )
    assert result[0]["environment"]["A"] == "from-payload"
    assert result[0]["environment"]["B"] == "2"


# ---------------------------------------------------------------------------
# Exclude drops keys
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_exclude_drops_keys(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("KEEP=this\nDROP=that\n")
    companions = _c(("app", {}))
    result = merge_companion_env(
        companions,
        env_from=[("app", str(env_file))],
        env_exclude=[("app", {"DROP"})],
    )
    assert result[0]["environment"] == {"KEEP": "this"}


@pytest.mark.unit
def test_exclude_applies_after_merge(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OVERRIDE=val\nDROP=dropped\n")
    companions = _c(("app", {"OVERRIDE": "from-payload"}))
    result = merge_companion_env(
        companions,
        env_from=[("app", str(env_file))],
        env_exclude=[("app", {"DROP"})],
    )
    assert result[0]["environment"] == {"OVERRIDE": "from-payload"}


# ---------------------------------------------------------------------------
# Missing companion name error
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_companion_raises(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\n")
    companions = _c(("other-app", {}))
    with pytest.raises(ValueError, match="--companion-env-from names companion"):
        merge_companion_env(
            companions,
            env_from=[("nonexistent", str(env_file))],
            env_exclude=[],
        )


# ---------------------------------------------------------------------------
# Missing / unreadable file error
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_env_file_raises() -> None:
    companions = _c(("app", {}))
    with pytest.raises(FileNotFoundError, match="--companion-env-from"):
        merge_companion_env(
            companions,
            env_from=[("app", "/nonexistent/.env")],
            env_exclude=[],
        )


@pytest.mark.unit
def test_unreadable_env_file_raises(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("KEY=val\n")
    env_file.chmod(0o000)
    try:
        companions = _c(("app", {}))
        with pytest.raises(PermissionError, match="unreadable"):
            merge_companion_env(
                companions,
                env_from=[("app", str(env_file))],
                env_exclude=[],
            )
    finally:
        env_file.chmod(0o644)


# ---------------------------------------------------------------------------
# Validation: bad key name → warn and skip
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bad_key_name_warned_and_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GOOD_KEY=val\n1bad-start=num\n")
    companions = _c(("app", {}))
    result = merge_companion_env(
        companions,
        env_from=[("app", str(env_file))],
        env_exclude=[],
    )
    assert result[0]["environment"] == {"GOOD_KEY": "val"}
    captured = capsys.readouterr()
    assert "1bad-start" in captured.err
    assert "1bad-start" not in captured.out


# ---------------------------------------------------------------------------
# Validation: interpolation in value → warn and skip
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_interpolation_value_warned_and_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GOOD=val\nINTERP=${OTHER_VAR}\n")
    companions = _c(("app", {}))
    result = merge_companion_env(
        companions,
        env_from=[("app", str(env_file))],
        env_exclude=[],
    )
    assert result[0]["environment"] == {"GOOD": "val"}
    captured = capsys.readouterr()
    assert "INTERP" in captured.err


# ---------------------------------------------------------------------------
# Validation: value never leaked in warnings
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_warning_never_leaks_value(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET_KEY=super-secret-value-12345\n")
    companions = _c(("app", {}))
    merge_companion_env(
        companions,
        env_from=[("app", str(env_file))],
        env_exclude=[],
    )
    captured = capsys.readouterr()
    assert "super-secret-value-12345" not in captured.err
    assert "super-secret-value-12345" not in captured.out


# ---------------------------------------------------------------------------
# Multiple companions
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_merge_targets_correct_companion(tmp_path: Path) -> None:
    env_a = tmp_path / "a.env"
    env_a.write_text("X=1\n")
    env_b = tmp_path / "b.env"
    env_b.write_text("Y=2\n")
    companions = [
        {"name": "alpha", "repo_url": "git@x:a.git", "environment": {}},
        {"name": "beta", "repo_url": "git@x:b.git", "environment": {}},
    ]
    result = merge_companion_env(
        companions,
        env_from=[("alpha", str(env_a)), ("beta", str(env_b))],
        env_exclude=[],
    )
    assert result[0]["environment"] == {"X": "1"}
    assert result[1]["environment"] == {"Y": "2"}


# ---------------------------------------------------------------------------
# Key too long → warn and skip
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_key_too_long_warned_and_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    long_key = "A" * 300
    env_file = tmp_path / ".env"
    env_file.write_text(f"GOOD=val\n{long_key}=val2\n")
    companions = _c(("app", {}))
    result = merge_companion_env(
        companions,
        env_from=[("app", str(env_file))],
        env_exclude=[],
    )
    assert result[0]["environment"] == {"GOOD": "val"}
    captured = capsys.readouterr()
    assert long_key[:20] in captured.err


# ---------------------------------------------------------------------------
# Escaped dollar ($$) is allowed in values
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_escaped_dollar_in_value(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PRICE=$$10\n")
    companions = _c(("app", {}))
    result = merge_companion_env(
        companions,
        env_from=[("app", str(env_file))],
        env_exclude=[],
    )
    assert result[0]["environment"] == {"PRICE": "$$10"}


# ---------------------------------------------------------------------------
# exclude on non-existent companion
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_exclude_for_missing_companion_raises() -> None:
    companions = _c(("app", {}))
    with pytest.raises(ValueError, match="--companion-env-exclude names companion"):
        merge_companion_env(
            companions,
            env_from=[],
            env_exclude=[("nonexistent", {"KEY"})],
        )


# ---------------------------------------------------------------------------
# Non-object environment crashes pre-validated
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_env",
    [
        "not-a-dict",
        ["list", "of", "strings"],
        True,
        42,
    ],
    ids=["string", "list", "bool-true", "int"],
)
def test_non_object_environment_raises(bad_env: object) -> None:
    companions = [{"name": "app", "repo_url": "git@x:app.git", "environment": bad_env}]
    with pytest.raises(ValueError, match="non-object 'environment' field"):
        merge_companion_env(companions, env_from=[], env_exclude=[])


@pytest.mark.unit
def test_false_environment_also_raises() -> None:
    companions = [{"name": "app", "repo_url": "git@x:app.git", "environment": False}]
    with pytest.raises(ValueError, match="non-object 'environment' field"):
        merge_companion_env(companions, env_from=[], env_exclude=[])


@pytest.mark.unit
def test_none_environment_treated_as_empty() -> None:
    companions = [{"name": "app", "repo_url": "git@x:app.git", "environment": None}]
    result = merge_companion_env(companions, env_from=[], env_exclude=[])
    assert result[0]["environment"] == {}


# ---------------------------------------------------------------------------
# Overlap with environment_secrets → warn and skip
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_env_from_skips_keys_overlapping_environment_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DB_PASSWORD=hunter2\nAPI_KEY=fromfile\nSAFE_KEY=yes\n")
    companions = [
        {
            "name": "app",
            "repo_url": "git@x:app.git",
            "environment": {},
            "environment_secrets": {
                "DB_PASSWORD": {"value_from": "secret_ref"},
                "API_KEY": {"value_from": "another_ref"},
            },
        },
    ]
    result = merge_companion_env(
        companions,
        env_from=[("app", str(env_file))],
        env_exclude=[],
    )
    assert "DB_PASSWORD" not in result[0]["environment"]
    assert "API_KEY" not in result[0]["environment"]
    assert result[0]["environment"] == {"SAFE_KEY": "yes"}
    captured = capsys.readouterr()
    assert "DB_PASSWORD" in captured.err
    assert "API_KEY" in captured.err


@pytest.mark.unit
def test_env_from_overlap_value_never_leaked_in_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET_KEY=top-secret-value-999\n")
    companions = [
        {
            "name": "app",
            "repo_url": "git@x:app.git",
            "environment": {},
            "environment_secrets": {"SECRET_KEY": {"value_from": "ref"}},
        },
    ]
    merge_companion_env(
        companions,
        env_from=[("app", str(env_file))],
        env_exclude=[],
    )
    captured = capsys.readouterr()
    assert "top-secret-value-999" not in captured.err
    assert "top-secret-value-999" not in captured.out
