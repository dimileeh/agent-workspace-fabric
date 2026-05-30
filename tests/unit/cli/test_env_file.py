"""Tests for .env file parsing (parse_dotenv_file, parse_env_from_arg, parse_env_exclude_arg)."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.cli.env_file import parse_dotenv_file, parse_env_exclude_arg, parse_env_from_arg

# ---------------------------------------------------------------------------
# parse_dotenv_file
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parsed_basic_key_value(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("DB_HOST=localhost\nDB_PORT=5432\n")
    result = parse_dotenv_file(env)
    assert result == {"DB_HOST": "localhost", "DB_PORT": "5432"}


@pytest.mark.unit
def test_parsed_strips_comments(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# This is a comment\nKEY=val\n")
    result = parse_dotenv_file(env)
    assert result == {"KEY": "val"}


@pytest.mark.unit
def test_parsed_strips_blank_lines(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("\n\nKEY=val\n\n")
    result = parse_dotenv_file(env)
    assert result == {"KEY": "val"}


@pytest.mark.unit
def test_parsed_strips_export_prefix(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("export API_KEY=secret\nexport OTHER=thing\n")
    result = parse_dotenv_file(env)
    assert result == {"API_KEY": "secret", "OTHER": "thing"}


@pytest.mark.unit
def test_parsed_strips_double_quotes(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text('KEY="hello world"\n')
    result = parse_dotenv_file(env)
    assert result == {"KEY": "hello world"}


@pytest.mark.unit
def test_parsed_strips_single_quotes(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("KEY='hello world'\n")
    result = parse_dotenv_file(env)
    assert result == {"KEY": "hello world"}


@pytest.mark.unit
def test_parsed_preserves_unquoted_equal_sign(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("KEY=value=with=equals\n")
    result = parse_dotenv_file(env)
    assert result == {"KEY": "value=with=equals"}


@pytest.mark.unit
def test_parsed_strips_inline_comment_after_value(tmp_path: Path) -> None:
    """Inline comments are NOT supported — the # is part of the value."""
    env = tmp_path / ".env"
    env.write_text("KEY=value # this is NOT an inline comment\n")
    result = parse_dotenv_file(env)
    assert result == {"KEY": "value # this is NOT an inline comment"}


@pytest.mark.unit
def test_parsed_skips_line_without_equals(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("JUST_A_WORD\nKEY=val\n")
    result = parse_dotenv_file(env)
    assert result == {"KEY": "val"}


@pytest.mark.unit
def test_parsed_skips_empty_key(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("=value\nKEY=val\n")
    result = parse_dotenv_file(env)
    assert result == {"KEY": "val"}


@pytest.mark.unit
def test_parsed_empty_value(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("EMPTY=\nKEY=val\n")
    result = parse_dotenv_file(env)
    assert result == {"EMPTY": "", "KEY": "val"}


@pytest.mark.unit
def test_parsed_empty_quoted_value(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("EMPTY=\"\"\nSINGLE=''\n")
    result = parse_dotenv_file(env)
    assert result == {"EMPTY": "", "SINGLE": ""}


@pytest.mark.unit
def test_parsed_strips_trailing_whitespace(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("KEY=value   \n")
    result = parse_dotenv_file(env)
    assert result == {"KEY": "value"}


@pytest.mark.unit
def test_parsed_strips_leading_whitespace_on_key(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("  KEY=value\n")
    result = parse_dotenv_file(env)
    assert result == {"KEY": "value"}


@pytest.mark.unit
def test_parsed_file_not_found_raises() -> None:
    with pytest.raises(FileNotFoundError, match="--companion-env-from file not found"):
        parse_dotenv_file(Path("/nonexistent/path/.env"))


@pytest.mark.unit
def test_parsed_file_permission_denied_raises(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("KEY=val\n")
    env.chmod(0o000)
    try:
        with pytest.raises(PermissionError, match="unreadable"):
            parse_dotenv_file(env)
    finally:
        env.chmod(0o644)


# ---------------------------------------------------------------------------
# parse_env_from_arg
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_env_from_arg_basic() -> None:
    name, path = parse_env_from_arg("my-app=./config/.env")
    assert name == "my-app"
    assert path == "config/.env"


@pytest.mark.unit
def test_parse_env_from_arg_expands_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    name, path = parse_env_from_arg("my-app=~/.env")
    assert name == "my-app"
    assert path == str(tmp_path / ".env")


@pytest.mark.unit
def test_parse_env_from_arg_expands_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_DIR", "/custom/dir")
    name, path = parse_env_from_arg("my-app=$MY_DIR/.env")
    assert name == "my-app"
    assert path == "/custom/dir/.env"


@pytest.mark.unit
def test_parse_env_from_arg_no_equals_raises() -> None:
    with pytest.raises(ValueError, match="--companion-env-from.*malformed"):
        parse_env_from_arg("no_equals_sign")


@pytest.mark.unit
def test_parse_env_from_arg_empty_name_raises() -> None:
    with pytest.raises(ValueError, match="--companion-env-from.*malformed"):
        parse_env_from_arg("=.env")


@pytest.mark.unit
def test_parse_env_from_arg_empty_path_raises() -> None:
    with pytest.raises(ValueError, match="--companion-env-from.*malformed"):
        parse_env_from_arg("my-app=")


# ---------------------------------------------------------------------------
# parse_env_exclude_arg
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_env_exclude_arg_single() -> None:
    name, keys = parse_env_exclude_arg("my-app=PROD_ONLY_KEY")
    assert name == "my-app"
    assert keys == {"PROD_ONLY_KEY"}


@pytest.mark.unit
def test_parse_env_exclude_arg_multiple() -> None:
    name, keys = parse_env_exclude_arg("my-app=KEY1,KEY2,KEY3")
    assert name == "my-app"
    assert keys == {"KEY1", "KEY2", "KEY3"}


@pytest.mark.unit
def test_parse_env_exclude_arg_no_equals_raises() -> None:
    with pytest.raises(ValueError, match="--companion-env-exclude.*malformed"):
        parse_env_exclude_arg("no_equals")


@pytest.mark.unit
def test_parse_env_exclude_arg_empty_name_raises() -> None:
    with pytest.raises(ValueError, match="--companion-env-exclude.*malformed"):
        parse_env_exclude_arg("=KEY1")


@pytest.mark.unit
def test_parse_env_exclude_arg_empty_keys_raises() -> None:
    with pytest.raises(ValueError, match="--companion-env-exclude.*malformed"):
        parse_env_exclude_arg("my-app=")
