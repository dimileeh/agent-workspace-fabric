"""Unit coverage for ``_persist_console_host_port`` (issue #441).

``awf start --console-port`` must persist ``AWF_CONSOLE_HOST_PORT`` into the active
service env file so later commands resolving from that ``.env`` see the same port.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.cli.init_ops import _persist_console_host_port


@pytest.mark.unit
def test_persist_creates_missing_file_and_parents(tmp_path: Path) -> None:
    """An absent env file (and missing parents) is created with just the assignment."""
    env_file = tmp_path / "nested" / "dir" / ".env"

    _persist_console_host_port(env_file, 4321)

    assert env_file.read_text(encoding="utf-8") == "AWF_CONSOLE_HOST_PORT=4321\n"


@pytest.mark.unit
def test_persist_replaces_existing_assignment_in_place(tmp_path: Path) -> None:
    """An existing assignment is replaced in place; unrelated lines/order preserved."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# header comment\nAWF_API_TOKEN=secret\nAWF_CONSOLE_HOST_PORT=3000\nAWF_OTHER=keep\n",
        encoding="utf-8",
    )

    _persist_console_host_port(env_file, 4321)

    assert env_file.read_text(encoding="utf-8") == (
        "# header comment\nAWF_API_TOKEN=secret\nAWF_CONSOLE_HOST_PORT=4321\nAWF_OTHER=keep\n"
    )


@pytest.mark.unit
def test_persist_appends_when_key_absent(tmp_path: Path) -> None:
    """When the key is absent the assignment is appended and prior content kept."""
    env_file = tmp_path / ".env"
    env_file.write_text("AWF_API_TOKEN=secret\n", encoding="utf-8")

    _persist_console_host_port(env_file, 4321)

    assert env_file.read_text(encoding="utf-8") == (
        "AWF_API_TOKEN=secret\nAWF_CONSOLE_HOST_PORT=4321\n"
    )


@pytest.mark.unit
def test_persist_appends_with_newline_when_file_lacks_trailing_newline(tmp_path: Path) -> None:
    """A file without a trailing newline gets a separator before the appended line."""
    env_file = tmp_path / ".env"
    env_file.write_text("AWF_API_TOKEN=secret", encoding="utf-8")

    _persist_console_host_port(env_file, 4321)

    assert env_file.read_text(encoding="utf-8") == (
        "AWF_API_TOKEN=secret\nAWF_CONSOLE_HOST_PORT=4321\n"
    )


@pytest.mark.unit
def test_persist_preserves_export_prefix_and_replaces_value(tmp_path: Path) -> None:
    """An ``export``-prefixed assignment keeps its prefix and is updated in place."""
    env_file = tmp_path / ".env"
    env_file.write_text("export AWF_CONSOLE_HOST_PORT=3000\n", encoding="utf-8")

    _persist_console_host_port(env_file, 4321)

    assert env_file.read_text(encoding="utf-8") == "export AWF_CONSOLE_HOST_PORT=4321\n"


@pytest.mark.unit
def test_persist_matches_key_case_insensitively_without_duplicating(tmp_path: Path) -> None:
    """A lowercase key spelling is matched case-insensitively; no duplicate appended."""
    env_file = tmp_path / ".env"
    env_file.write_text("awf_console_host_port=3000\n", encoding="utf-8")

    _persist_console_host_port(env_file, 4321)

    assert env_file.read_text(encoding="utf-8") == "awf_console_host_port=4321\n"


@pytest.mark.unit
def test_persist_does_not_touch_console_url(tmp_path: Path) -> None:
    """``AWF_CONSOLE_URL`` is left untouched when persisting the host port."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWF_CONSOLE_URL=http://localhost:9000\nAWF_CONSOLE_HOST_PORT=3000\n",
        encoding="utf-8",
    )

    _persist_console_host_port(env_file, 4321)

    assert env_file.read_text(encoding="utf-8") == (
        "AWF_CONSOLE_URL=http://localhost:9000\nAWF_CONSOLE_HOST_PORT=4321\n"
    )
