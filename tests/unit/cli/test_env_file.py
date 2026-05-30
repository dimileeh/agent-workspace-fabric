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
    """Parse basic KEY=VALUE pairs from a .env file."""
    env = tmp_path / ".env"
    env.write_text("DB_HOST=localhost\nDB_PORT=5432\n")
    result = parse_dotenv_file(env)
    assert result == {"DB_HOST": "localhost", "DB_PORT": "5432"}


@pytest.mark.unit
def test_parsed_strips_comments(tmp_path: Path) -> None:
    """Comment lines starting with # are ignored."""
    env = tmp_path / ".env"
    env.write_text("# This is a comment\nKEY=val\n")
    result = parse_dotenv_file(env)
    assert result == {"KEY": "val"}


@pytest.mark.unit
def test_parsed_strips_blank_lines(tmp_path: Path) -> None:
    """Blank lines are ignored during parsing."""
    env = tmp_path / ".env"
    env.write_text("\n\nKEY=val\n\n")
    result = parse_dotenv_file(env)
    assert result == {"KEY": "val"}


@pytest.mark.unit
def test_parsed_strips_export_prefix(tmp_path: Path) -> None:
    """Lines with 'export' prefix are parsed correctly, stripping the export keyword."""
    env = tmp_path / ".env"
    env.write_text("export API_KEY=secret\nexport OTHER=thing\n")
    result = parse_dotenv_file(env)
    assert result == {"API_KEY": "secret", "OTHER": "thing"}


@pytest.mark.unit
def test_parsed_strips_double_quotes(tmp_path: Path) -> None:
    """Double-quoted values are unquoted."""
    env = tmp_path / ".env"
    env.write_text('KEY="hello world"\n')
    result = parse_dotenv_file(env)
    assert result == {"KEY": "hello world"}


@pytest.mark.unit
def test_parsed_strips_single_quotes(tmp_path: Path) -> None:
    """Single-quoted values are unquoted."""
    env = tmp_path / ".env"
    env.write_text("KEY='hello world'\n")
    result = parse_dotenv_file(env)
    assert result == {"KEY": "hello world"}


@pytest.mark.unit
def test_parsed_preserves_unquoted_equal_sign(tmp_path: Path) -> None:
    """Only the first = is treated as the key-value separator; subsequent = are part of the value."""
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
    """Lines without an = sign are skipped."""
    env = tmp_path / ".env"
    env.write_text("JUST_A_WORD\nKEY=val\n")
    result = parse_dotenv_file(env)
    assert result == {"KEY": "val"}


@pytest.mark.unit
def test_parsed_skips_empty_key(tmp_path: Path) -> None:
    """Lines where the key is empty (=value) are skipped."""
    env = tmp_path / ".env"
    env.write_text("=value\nKEY=val\n")
    result = parse_dotenv_file(env)
    assert result == {"KEY": "val"}


@pytest.mark.unit
def test_parsed_empty_value(tmp_path: Path) -> None:
    """Keys with empty values (KEY=) produce an empty string value."""
    env = tmp_path / ".env"
    env.write_text("EMPTY=\nKEY=val\n")
    result = parse_dotenv_file(env)
    assert result == {"EMPTY": "", "KEY": "val"}


@pytest.mark.unit
def test_parsed_empty_quoted_value(tmp_path: Path) -> None:
    """Empty quoted values (KEY="" or KEY='') produce an empty string."""
    env = tmp_path / ".env"
    env.write_text("EMPTY=\"\"\nSINGLE=''\n")
    result = parse_dotenv_file(env)
    assert result == {"EMPTY": "", "SINGLE": ""}


@pytest.mark.unit
def test_parsed_strips_trailing_whitespace(tmp_path: Path) -> None:
    """Trailing whitespace on unquoted values is stripped."""
    env = tmp_path / ".env"
    env.write_text("KEY=value   \n")
    result = parse_dotenv_file(env)
    assert result == {"KEY": "value"}


@pytest.mark.unit
def test_parsed_strips_leading_whitespace_on_key(tmp_path: Path) -> None:
    """Leading whitespace before the key is stripped."""
    env = tmp_path / ".env"
    env.write_text("  KEY=value\n")
    result = parse_dotenv_file(env)
    assert result == {"KEY": "value"}


@pytest.mark.unit
def test_parsed_file_not_found_raises() -> None:
    """A nonexistent file path raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError, match="--companion-env-from file not found"):
        parse_dotenv_file(Path("/nonexistent/path/.env"))


@pytest.mark.unit
def test_parsed_file_permission_denied_raises(tmp_path: Path) -> None:
    """An unreadable file (chmod 0) raises PermissionError."""
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
    """Basic --companion-env-from argument parses name=path correctly."""
    name, path = parse_env_from_arg("my-app=./config/.env")
    assert name == "my-app"
    assert path == "config/.env"


@pytest.mark.unit
def test_parse_env_from_arg_expands_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """~ in the path is expanded to the user's home directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    name, path = parse_env_from_arg("my-app=~/.env")
    assert name == "my-app"
    assert path == str(tmp_path / ".env")


@pytest.mark.unit
def test_parse_env_from_arg_expands_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """$VAR environment variables in the path are expanded."""
    monkeypatch.setenv("MY_DIR", "/custom/dir")
    name, path = parse_env_from_arg("my-app=$MY_DIR/.env")
    assert name == "my-app"
    assert path == "/custom/dir/.env"


@pytest.mark.unit
def test_parse_env_from_arg_no_equals_raises() -> None:
    """An argument without an = sign raises ValueError."""
    with pytest.raises(ValueError, match="--companion-env-from.*malformed"):
        parse_env_from_arg("no_equals_sign")


@pytest.mark.unit
def test_parse_env_from_arg_empty_name_raises() -> None:
    """An argument with an empty name (=path) raises ValueError."""
    with pytest.raises(ValueError, match="--companion-env-from.*malformed"):
        parse_env_from_arg("=.env")


@pytest.mark.unit
def test_parse_env_from_arg_empty_path_raises() -> None:
    """An argument with an empty path (name=) raises ValueError."""
    with pytest.raises(ValueError, match="--companion-env-from.*malformed"):
        parse_env_from_arg("my-app=")


# ---------------------------------------------------------------------------
# parse_env_exclude_arg
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_env_exclude_arg_single() -> None:
    """A single exclude key is parsed correctly."""
    name, keys = parse_env_exclude_arg("my-app=PROD_ONLY_KEY")
    assert name == "my-app"
    assert keys == {"PROD_ONLY_KEY"}


@pytest.mark.unit
def test_parse_env_exclude_arg_multiple() -> None:
    """Multiple comma-separated exclude keys are parsed into a set."""
    name, keys = parse_env_exclude_arg("my-app=KEY1,KEY2,KEY3")
    assert name == "my-app"
    assert keys == {"KEY1", "KEY2", "KEY3"}


@pytest.mark.unit
def test_parse_env_exclude_arg_no_equals_raises() -> None:
    """An exclude argument without an = sign raises ValueError."""
    with pytest.raises(ValueError, match="--companion-env-exclude.*malformed"):
        parse_env_exclude_arg("no_equals")


@pytest.mark.unit
def test_parse_env_exclude_arg_empty_name_raises() -> None:
    """An exclude argument with an empty name (=KEY) raises ValueError."""
    with pytest.raises(ValueError, match="--companion-env-exclude.*malformed"):
        parse_env_exclude_arg("=KEY1")


@pytest.mark.unit
def test_parse_env_exclude_arg_empty_keys_raises() -> None:
    """An exclude argument with an empty key list (name=) raises ValueError."""
    with pytest.raises(ValueError, match="--companion-env-exclude.*malformed"):
        parse_env_exclude_arg("my-app=")


@pytest.mark.unit
def test_parse_env_exclude_arg_comma_only_raises() -> None:
    """An exclude argument with only commas (name=,) raises ValueError."""
    with pytest.raises(ValueError, match="--companion-env-exclude.*malformed"):
        parse_env_exclude_arg("my-app=,")


@pytest.mark.unit
def test_parse_env_exclude_arg_multiple_commas_only_raises() -> None:
    """An exclude argument with only commas (name=,,) raises ValueError."""
    with pytest.raises(ValueError, match="--companion-env-exclude.*malformed"):
        parse_env_exclude_arg("my-app=,,")
