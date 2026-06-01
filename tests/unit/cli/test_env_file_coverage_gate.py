"""Coverage-gate unit tests for awf.cli.env_file.

These tests exercise currently-uncovered real behavior in
``awf.cli.env_file.parse_dotenv_file``:

- the ``UnicodeDecodeError`` re-raise path when the .env file is not valid
  UTF-8 (source lines 49-56), and
- the "empty key after stripping" skip branch (source line 69-70), reached
  when the portion before the first ``=`` is whitespace-only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.cli.env_file import parse_dotenv_file


@pytest.mark.unit
def test_parse_dotenv_invalid_utf8_raises_unicode_decode_error(tmp_path: Path) -> None:
    """A non-UTF-8 .env file raises UnicodeDecodeError with an actionable message."""
    env = tmp_path / ".env"
    # 0xff / 0xfe are invalid as a standalone UTF-8 sequence, so read_text
    # (utf-8-sig) raises UnicodeDecodeError, which the parser re-raises with a
    # path-bearing message.
    env.write_bytes(b"FOO=\xff\xfe bar")

    with pytest.raises(UnicodeDecodeError) as excinfo:
        parse_dotenv_file(env)

    exc = excinfo.value
    # The re-raised error preserves the codec but swaps in an actionable reason
    # that names the offending file.
    assert exc.encoding == "utf-8"
    assert "not valid UTF-8" in exc.reason
    assert str(env) in exc.reason


@pytest.mark.unit
def test_parse_dotenv_skips_line_with_whitespace_only_key(tmp_path: Path) -> None:
    """A line whose key is whitespace-only (after export-stripping) is ignored.

    ``export  =value`` -> after stripping the leading ``export `` prefix the
    line is `` =value``; the first ``=`` is at index 1 (so it passes the
    ``eq_index < 1`` guard), but the key slice strips to empty and the line is
    skipped instead of producing an empty-string key.
    """
    env = tmp_path / ".env"
    env.write_text("export  =value\nGOOD=1\n", encoding="utf-8")

    result = parse_dotenv_file(env)

    assert result == {"GOOD": "1"}
    assert "" not in result
