from __future__ import annotations

import ast
import json
import re
import shlex
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
import yaml

from awf.cli.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
README_PATH = REPO_ROOT / "README.md"
DOCS_INDEX_CANDIDATES = (README_PATH, REPO_ROOT / "docs" / "README.md")

ROOT_PUBLIC_DOC_NAMES = {
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "RELEASING.md",
}
INTERNAL_DOC_PREFIXES = ("docs/awf-plans/",)
PLANNING_DOC_NAMES = {
    "docs/awf_prd_v2.2.md",
    "docs/PLAN_MVP.md",
    "docs/PLAN_PR_MONITOR.md",
    "docs/PLAN_RELEASE_PR_SYNC.md",
}
OPTIONAL_PUBLIC_GUIDES = {
    "docs/TROUBLESHOOTING.md",
}
COPY_PASTE_DOC_HINTS = {
    "docs/PROJECT_ONBOARDING.md",
}
DOTENV_DOUBLE_QUOTE_DECODE_FUNCTION_LINES = (
    "awf_decode_double_quoted_dotenv() {",
    "  python3 -c 'import re, sys",
    (
        'replacements = {"n": "\\n", "r": "\\r", "t": "\\t", "\\\\": "\\\\", '
        'chr(34): chr(34), "$": "$"}'
    ),
    (
        'print(re.sub(r"\\\\(.)", lambda match: replacements.get(match[1], match[1]), '
        'sys.argv[1]), end="")\' "$1"'
    ),
    "}",
)
DOTENV_UNQUOTED_INLINE_COMMENT_STRIP_FUNCTION_LINES = (
    "awf_strip_unquoted_dotenv_inline_comment() {",
    '  case "$1" in',
    '    \\"*|\\\'*) printf "%s" "$1" ;;',
    '    \\#*) printf "%s" "" ;;',
    '    *) printf "%s" "$1" | sed \'s/[[:space:]]#.*$//; s/[[:space:]]*$//\' ;;',
    "  esac",
    "}",
)
PACKAGE_ENV_READ_LINES = {
    "AWF_API_TOKEN": (
        "AWF_PERSISTED_API_TOKEN=\"$(sed -n 's/^[[:space:]]*"
        "\\(export[[:space:]][[:space:]]*\\)\\{0,1\\}"
        "AWF_API_TOKEN[[:space:]]*=[[:space:]]*//p' .env 2>/dev/null | head -n 1)\""
    ),
    "AWF_POSTGRES_PASSWORD": (
        "AWF_PERSISTED_POSTGRES_PASSWORD=\"$(sed -n 's/^[[:space:]]*"
        "\\(export[[:space:]][[:space:]]*\\)\\{0,1\\}"
        "AWF_POSTGRES_PASSWORD[[:space:]]*=[[:space:]]*//p' .env 2>/dev/null | "
        'head -n 1)"'
    ),
    "AWF_DATABASE_URL": (
        "AWF_PERSISTED_DATABASE_URL=\"$(sed -n 's/^[[:space:]]*"
        "\\(export[[:space:]][[:space:]]*\\)\\{0,1\\}"
        "AWF_DATABASE_URL[[:space:]]*=[[:space:]]*//p' .env 2>/dev/null | "
        'head -n 1)"'
    ),
}
PACKAGE_ENV_INLINE_COMMENT_STRIP_LINES = {
    "AWF_API_TOKEN": (
        'AWF_PERSISTED_API_TOKEN="$(awf_strip_unquoted_dotenv_inline_comment '
        '"$AWF_PERSISTED_API_TOKEN")"'
    ),
    "AWF_POSTGRES_PASSWORD": (
        'AWF_PERSISTED_POSTGRES_PASSWORD="$(awf_strip_unquoted_dotenv_inline_comment '
        '"$AWF_PERSISTED_POSTGRES_PASSWORD")"'
    ),
    "AWF_DATABASE_URL": (
        'AWF_PERSISTED_DATABASE_URL="$(awf_strip_unquoted_dotenv_inline_comment '
        '"$AWF_PERSISTED_DATABASE_URL")"'
    ),
}
PACKAGE_ENV_QUOTE_STRIP_LINES = {
    "AWF_API_TOKEN": (
        'case "$AWF_PERSISTED_API_TOKEN" in',
        '  \\"*\\")',
        '    AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\\"}"',
        '    AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\\"}"',
        '    AWF_PERSISTED_API_TOKEN="$(awf_decode_double_quoted_dotenv "$AWF_PERSISTED_API_TOKEN")"',
        "    ;;",
        "  \\'*\\')",
        '    AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\\\'}"',
        '    AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\\\'}"',
        "    ;;",
        "esac",
    ),
    "AWF_POSTGRES_PASSWORD": (
        'case "$AWF_PERSISTED_POSTGRES_PASSWORD" in',
        '  \\"*\\")',
        '    AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\\"}"',
        '    AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\\"}"',
        (
            '    AWF_PERSISTED_POSTGRES_PASSWORD="$(awf_decode_double_quoted_dotenv '
            '"$AWF_PERSISTED_POSTGRES_PASSWORD")"'
        ),
        "    ;;",
        "  \\'*\\')",
        '    AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\\\'}"',
        '    AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\\\'}"',
        "    ;;",
        "esac",
    ),
    "AWF_DATABASE_URL": (
        'case "$AWF_PERSISTED_DATABASE_URL" in',
        '  \\"*\\")',
        '    AWF_PERSISTED_DATABASE_URL="${AWF_PERSISTED_DATABASE_URL#\\"}"',
        '    AWF_PERSISTED_DATABASE_URL="${AWF_PERSISTED_DATABASE_URL%\\"}"',
        (
            '    AWF_PERSISTED_DATABASE_URL="$(awf_decode_double_quoted_dotenv '
            '"$AWF_PERSISTED_DATABASE_URL")"'
        ),
        "    ;;",
        "  \\'*\\')",
        '    AWF_PERSISTED_DATABASE_URL="${AWF_PERSISTED_DATABASE_URL#\\\'}"',
        '    AWF_PERSISTED_DATABASE_URL="${AWF_PERSISTED_DATABASE_URL%\\\'}"',
        "    ;;",
        "esac",
    ),
}
PACKAGE_POSTGRES_PASSWORD_URLENCODE_LINE = (
    "awf_postgres_password_urlencoded=\"$(python3 -c 'from os import environ; "
    'from urllib.parse import quote; print(quote(environ["AWF_POSTGRES_PASSWORD"], '
    'safe=""))\')"'
)
PACKAGE_DATABASE_URL_ENCODED_EXPORT = (
    'export AWF_DATABASE_URL="postgresql+asyncpg://awf:${awf_postgres_password_urlencoded}'
    '@localhost:${AWF_POSTGRES_HOST_PORT}/awf"'
)
PACKAGE_DATABASE_URL_RAW_PASSWORD_EXPORT = (
    'export AWF_DATABASE_URL="postgresql+asyncpg://awf:${AWF_POSTGRES_PASSWORD}'
    '@localhost:${AWF_POSTGRES_HOST_PORT}/awf"'
)
SOURCE_CHECKOUT_ENV_READ_LINES = {
    "AWF_API_TOKEN": (
        "  AWF_PERSISTED_API_TOKEN=\"$(sed -n 's/^[[:space:]]*"
        "\\(export[[:space:]][[:space:]]*\\)\\{0,1\\}"
        'AWF_API_TOKEN[[:space:]]*=[[:space:]]*//p\' "$env_file" | head -n 1)"'
    ),
    "AWF_POSTGRES_PASSWORD": (
        "  AWF_PERSISTED_POSTGRES_PASSWORD=\"$(sed -n 's/^[[:space:]]*"
        "\\(export[[:space:]][[:space:]]*\\)\\{0,1\\}"
        'AWF_POSTGRES_PASSWORD[[:space:]]*=[[:space:]]*//p\' "$env_file" | head -n 1)"'
    ),
    "AWF_POSTGRES_HOST_PORT": (
        "  AWF_PERSISTED_POSTGRES_HOST_PORT=\"$(sed -n 's/^[[:space:]]*"
        "\\(export[[:space:]][[:space:]]*\\)\\{0,1\\}"
        'AWF_POSTGRES_HOST_PORT[[:space:]]*=[[:space:]]*//p\' "$env_file" | head -n 1)"'
    ),
    "AWF_DATABASE_URL": (
        "  AWF_PERSISTED_DATABASE_URL=\"$(sed -n 's/^[[:space:]]*"
        "\\(export[[:space:]][[:space:]]*\\)\\{0,1\\}"
        'AWF_DATABASE_URL[[:space:]]*=[[:space:]]*//p\' "$env_file" | head -n 1)"'
    ),
}
SOURCE_CHECKOUT_ENV_INLINE_COMMENT_STRIP_LINES = {
    "AWF_API_TOKEN": (
        '  AWF_PERSISTED_API_TOKEN="$(awf_strip_unquoted_dotenv_inline_comment '
        '"$AWF_PERSISTED_API_TOKEN")"'
    ),
    "AWF_POSTGRES_PASSWORD": (
        '  AWF_PERSISTED_POSTGRES_PASSWORD="$(awf_strip_unquoted_dotenv_inline_comment '
        '"$AWF_PERSISTED_POSTGRES_PASSWORD")"'
    ),
    "AWF_POSTGRES_HOST_PORT": (
        '  AWF_PERSISTED_POSTGRES_HOST_PORT="$(awf_strip_unquoted_dotenv_inline_comment '
        '"$AWF_PERSISTED_POSTGRES_HOST_PORT")"'
    ),
    "AWF_DATABASE_URL": (
        '  AWF_PERSISTED_DATABASE_URL="$(awf_strip_unquoted_dotenv_inline_comment '
        '"$AWF_PERSISTED_DATABASE_URL")"'
    ),
}
SOURCE_CHECKOUT_ENV_QUOTE_STRIP_LINES = {
    "AWF_API_TOKEN": (
        '  case "$AWF_PERSISTED_API_TOKEN" in',
        '    \\"*\\")',
        '      AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\\"}"',
        '      AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\\"}"',
        (
            '      AWF_PERSISTED_API_TOKEN="$(awf_decode_double_quoted_dotenv '
            '"$AWF_PERSISTED_API_TOKEN")"'
        ),
        "      ;;",
        "    \\'*\\')",
        '      AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN#\\\'}"',
        '      AWF_PERSISTED_API_TOKEN="${AWF_PERSISTED_API_TOKEN%\\\'}"',
        "      ;;",
        "  esac",
    ),
    "AWF_POSTGRES_PASSWORD": (
        '  case "$AWF_PERSISTED_POSTGRES_PASSWORD" in',
        '    \\"*\\")',
        '      AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\\"}"',
        '      AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\\"}"',
        (
            '      AWF_PERSISTED_POSTGRES_PASSWORD="$(awf_decode_double_quoted_dotenv '
            '"$AWF_PERSISTED_POSTGRES_PASSWORD")"'
        ),
        "      ;;",
        "    \\'*\\')",
        '      AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD#\\\'}"',
        '      AWF_PERSISTED_POSTGRES_PASSWORD="${AWF_PERSISTED_POSTGRES_PASSWORD%\\\'}"',
        "      ;;",
        "  esac",
    ),
    "AWF_POSTGRES_HOST_PORT": (
        '  case "$AWF_PERSISTED_POSTGRES_HOST_PORT" in',
        '    \\"*\\")',
        '      AWF_PERSISTED_POSTGRES_HOST_PORT="${AWF_PERSISTED_POSTGRES_HOST_PORT#\\"}"',
        '      AWF_PERSISTED_POSTGRES_HOST_PORT="${AWF_PERSISTED_POSTGRES_HOST_PORT%\\"}"',
        (
            '      AWF_PERSISTED_POSTGRES_HOST_PORT="$(awf_decode_double_quoted_dotenv '
            '"$AWF_PERSISTED_POSTGRES_HOST_PORT")"'
        ),
        "      ;;",
        "    \\'*\\')",
        '      AWF_PERSISTED_POSTGRES_HOST_PORT="${AWF_PERSISTED_POSTGRES_HOST_PORT#\\\'}"',
        '      AWF_PERSISTED_POSTGRES_HOST_PORT="${AWF_PERSISTED_POSTGRES_HOST_PORT%\\\'}"',
        "      ;;",
        "  esac",
    ),
    "AWF_DATABASE_URL": (
        '  case "$AWF_PERSISTED_DATABASE_URL" in',
        '    \\"*\\")',
        '      AWF_PERSISTED_DATABASE_URL="${AWF_PERSISTED_DATABASE_URL#\\"}"',
        '      AWF_PERSISTED_DATABASE_URL="${AWF_PERSISTED_DATABASE_URL%\\"}"',
        (
            '      AWF_PERSISTED_DATABASE_URL="$(awf_decode_double_quoted_dotenv '
            '"$AWF_PERSISTED_DATABASE_URL")"'
        ),
        "      ;;",
        "    \\'*\\')",
        '      AWF_PERSISTED_DATABASE_URL="${AWF_PERSISTED_DATABASE_URL#\\\'}"',
        '      AWF_PERSISTED_DATABASE_URL="${AWF_PERSISTED_DATABASE_URL%\\\'}"',
        "      ;;",
        "  esac",
    ),
}

LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\((?P<target>[^)]+)\)")
FENCE_DELIMITER_RE = re.compile(r"^ {0,3}```", re.MULTILINE)
OPENING_FENCE_RE = re.compile(
    r"^(?P<indent> {0,3})(?P<delimiter>`{3,})(?P<language>[A-Za-z0-9_+.-]*)[^\n]*$",
)
AWF_COMMAND_RE = re.compile(r"(?<![\w./-])awf(?P<tail>\s+[^`\n|;)]*)")


@dataclass(frozen=True)
class MarkdownFence:
    path: str
    line: int
    language: str
    body: str


@dataclass(frozen=True)
class AwfCommandMention:
    path: str
    command: str
    command_path: tuple[str, ...]


def _markdown_section(text: str, heading: str) -> str:
    """Return the body of the first matching H2 heading up to the next H2.

    Callers must pass an H2 (``##``) heading; H3 or deeper headings are
    intentionally rejected instead of being over-captured by the H2 sentinel.
    """
    if not re.match(r"^##(?!#)(?:[ \t]|$)", heading):
        raise ValueError(f"Only H2 headings are supported: {heading!r}")

    normalized_text = text.replace("\r\n", "\n")
    heading_match = re.search(
        rf"(?m)^{re.escape(heading)}[ \t]*(?:\n|$)",
        normalized_text,
    )
    if heading_match is None:
        raise AssertionError(f"Markdown heading {heading!r} not found")

    start = heading_match.end()
    next_heading = re.search(r"(?m)^## ", normalized_text[start:])
    if next_heading is None:
        return normalized_text[start:]
    return normalized_text[start : start + next_heading.start()]


def _markdown_section_between(text: str, start_heading: str, end_heading: str) -> str:
    """Return text between two required Markdown headings."""
    _, found_start_heading, after_start_heading = text.partition(start_heading)
    assert found_start_heading, f"Markdown heading {start_heading!r} not found"
    section, found_end_heading, _ = after_start_heading.partition(end_heading)
    assert found_end_heading, f"Markdown heading {end_heading!r} not found after {start_heading!r}"
    return section


def _quickstart_upgrade_section(text: str, heading: str) -> str:
    """Return the lane's upgrade block between upgrade and uninstall labels."""
    section = _markdown_section(text, heading)
    upgrade_start = section.find("Upgrade:")
    assert upgrade_start != -1, f"{heading} is missing Upgrade block"
    uninstall_start = section.find("Uninstall:", upgrade_start)
    assert uninstall_start != -1, f"{heading} is missing Uninstall block after Upgrade"
    return section[upgrade_start + len("Upgrade:") : uninstall_start]


def _required_index(text: str, needle: str, label: str, start: int = 0) -> int:
    """Return a required substring index with assertion-style failure output."""
    index = text.find(needle, start)
    assert index != -1, f"{label} is missing required text after offset {start}: {needle!r}"
    return index


def _shell_closing_fi_index(section: str, start: int, label: str) -> int:
    """Return the closing fi index after a flat shell guard line."""
    # The docs snippets validated here intentionally use flat if/fi guards. Use
    # a depth-aware parser before reusing this helper for nested shell guards.
    closing_match = re.search(r"(?m)^fi$", section[start:])
    assert closing_match is not None, f"{label} is missing closing shell fi"
    return start + closing_match.start()


def _shell_line_index(section: str, line: str, label: str, start: int = 0) -> int:
    """Return the exact shell line index at or after start."""
    line_match = re.search(rf"(?m)^{re.escape(line)}$", section[start:])
    assert line_match is not None, f"{label} is missing shell line: {line}"
    return start + line_match.start()


def _package_env_restore_script(section: str, label: str) -> str:
    """Return the package-lane restore script before the restart command."""
    restore_index = _shell_line_index(
        section,
        DOTENV_DOUBLE_QUOTE_DECODE_FUNCTION_LINES[0],
        label,
    )
    start_index = _shell_line_index(section, "awf start", label, restore_index)
    return section[restore_index:start_index]


def _assert_dotenv_decode_function(section: str, label: str, start: int = 0) -> tuple[int, int]:
    """Assert the double-quoted dotenv decoder is present in order."""
    indexes: list[int] = []
    current_index = start
    for line in DOTENV_DOUBLE_QUOTE_DECODE_FUNCTION_LINES:
        current_index = _shell_line_index(section, line, label, current_index)
        indexes.append(current_index)
    return indexes[0], indexes[-1]


def _assert_dotenv_inline_comment_strip_function(
    section: str,
    label: str,
    start: int = 0,
) -> tuple[int, int]:
    """Assert the unquoted dotenv inline-comment stripper is present in order."""
    indexes: list[int] = []
    current_index = start
    for line in DOTENV_UNQUOTED_INLINE_COMMENT_STRIP_FUNCTION_LINES:
        current_index = _shell_line_index(section, line, label, current_index)
        indexes.append(current_index)
    return indexes[0], indexes[-1]


def _assert_package_env_quote_strip_lines(
    *,
    label: str,
    section: str,
    lines: tuple[str, ...],
    start: int,
) -> tuple[int, ...]:
    """Assert package-lane env restore snippets strip quoted dotenv values."""
    indexes: list[int] = []
    current_index = start
    for line in lines:
        current_index = _shell_line_index(section, line, label, current_index)
        indexes.append(current_index)
    return tuple(indexes)


def _assert_source_checkout_api_token_restore(
    label: str,
    section: str,
    lifecycle: str,
) -> tuple[int, int]:
    """Assert source-checkout snippets export persisted API tokens before use."""
    allow_default_api_token = lifecycle in {
        "upgrading",
        "rollback",
        "refreshing source-checkout metadata",
    }
    unsafe_default_line = 'export AWF_API_TOKEN="${AWF_API_TOKEN:-$(openssl rand -hex 32)}"'
    unsafe_shared_guard_line = (
        "if ! grep -q '^AWF_API_TOKEN=.' docker/compose/.env .env 2>/dev/null; then"
    )
    token_init_line = 'AWF_PERSISTED_API_TOKEN=""'
    legacy_first_token_loop_line = "for env_file in docker/compose/.env .env; do"
    root_first_token_loop_line = "for env_file in .env docker/compose/.env; do"
    root_token_loop_line = "for env_file in .env; do"
    token_loop_line = (
        root_first_token_loop_line
        if root_first_token_loop_line in section
        else root_token_loop_line
    )
    token_file_guard_line = '  [ -f "$env_file" ] || continue'
    token_read_line = SOURCE_CHECKOUT_ENV_READ_LINES["AWF_API_TOKEN"]
    token_inline_comment_strip_line = SOURCE_CHECKOUT_ENV_INLINE_COMMENT_STRIP_LINES[
        "AWF_API_TOKEN"
    ]
    token_quote_strip_lines = SOURCE_CHECKOUT_ENV_QUOTE_STRIP_LINES["AWF_API_TOKEN"]
    token_break_line = '  [ -n "$AWF_PERSISTED_API_TOKEN" ] && break'
    token_loop_end_line = "done"
    token_guard_line = 'if [ -n "$AWF_PERSISTED_API_TOKEN" ]; then'
    token_persisted_export_line = '  export AWF_API_TOKEN="$AWF_PERSISTED_API_TOKEN"'
    token_default_env_files = (
        ".env docker/compose/.env" if root_first_token_loop_line in section else ".env"
    )
    token_default_guard_line = (
        "elif grep -q '^[[:space:]]*\\(export[[:space:]][[:space:]]*\\)\\{0,1\\}"
        f"AWF_API_TOKEN[[:space:]]*=' {token_default_env_files} 2>/dev/null; then"
    )
    token_default_export_line = '  export AWF_API_TOKEN="${AWF_API_TOKEN:-local-dev-token}"'
    token_else_line = "else"
    legacy_first_token_require_line = (
        '  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core '
        "or persist it in docker/compose/.env or .env before " + lifecycle + '}"'
    )
    root_first_token_require_line = (
        '  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core '
        "or persist it in .env or docker/compose/.env before " + lifecycle + '}"'
    )
    root_token_require_line = (
        '  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core '
        "or persist it in .env before " + lifecycle + '}"'
    )
    token_require_line = (
        root_first_token_require_line
        if root_first_token_require_line in section
        else root_token_require_line
    )
    token_shell_export_line = "  export AWF_API_TOKEN"

    assert unsafe_default_line not in section, f"{label} must not regenerate AWF_API_TOKEN"
    assert unsafe_shared_guard_line not in section, (
        f"{label} must not let root .env satisfy the compose env guard without export"
    )
    assert legacy_first_token_loop_line not in section, (
        f"{label} must prefer root .env over legacy docker/compose/.env"
    )
    assert legacy_first_token_require_line not in section, (
        f"{label} must describe root .env before legacy docker/compose/.env"
    )
    assert token_init_line in section, f"{label} must initialize persisted API token lookup"
    assert token_loop_line in section, f"{label} must inspect source checkout env files"
    assert token_file_guard_line in section, f"{label} must skip absent env files"
    assert token_read_line in section, f"{label} must read persisted AWF_API_TOKEN"
    assert token_inline_comment_strip_line in section, (
        f"{label} must strip unquoted AWF_API_TOKEN inline comments"
    )
    for token_quote_strip_line in token_quote_strip_lines:
        assert token_quote_strip_line in section, (
            f"{label} must strip quoted persisted AWF_API_TOKEN values"
        )
    assert token_break_line in section, f"{label} must prefer the first persisted API token"
    assert token_guard_line in section, f"{label} must branch on persisted API token"
    assert token_persisted_export_line in section, (
        f"{label} must export the persisted AWF_API_TOKEN"
    )
    if allow_default_api_token:
        assert token_default_guard_line in section, (
            f"{label} must recognize empty persisted AWF_API_TOKEN entries"
        )
        assert token_default_export_line in section, (
            f"{label} must fall back to the local default API token"
        )
    else:
        assert token_default_guard_line not in section, (
            f"{label} must require an explicit API token for {lifecycle}"
        )
        assert token_default_export_line not in section, (
            f"{label} must not default API token for {lifecycle}"
        )
    assert token_require_line in section, (
        f"{label} must require AWF_API_TOKEN when no persisted token can be restored"
    )
    assert token_shell_export_line in section, f"{label} must export restored shell AWF_API_TOKEN"

    decode_start_index, decode_end_index = _assert_dotenv_decode_function(section, label)
    strip_start_index, strip_end_index = _assert_dotenv_inline_comment_strip_function(
        section,
        label,
        decode_end_index,
    )
    token_init_index = section.index(token_init_line)
    token_loop_index = _required_index(section, token_loop_line, label, token_init_index)
    token_file_guard_index = _required_index(
        section,
        token_file_guard_line,
        label,
        token_loop_index,
    )
    token_read_index = _required_index(section, token_read_line, label, token_file_guard_index)
    token_inline_comment_strip_index = _shell_line_index(
        section,
        token_inline_comment_strip_line,
        label,
        token_read_index,
    )
    token_quote_strip_indexes: list[int] = []
    token_quote_strip_index = token_inline_comment_strip_index
    for token_quote_strip_line in token_quote_strip_lines:
        token_quote_strip_index = _shell_line_index(
            section,
            token_quote_strip_line,
            label,
            token_quote_strip_index,
        )
        token_quote_strip_indexes.append(token_quote_strip_index)
    token_break_index = _required_index(
        section,
        token_break_line,
        label,
        token_quote_strip_indexes[-1],
    )
    token_loop_end_index = _required_index(
        section,
        token_loop_end_line,
        label,
        token_break_index,
    )
    token_guard_index = _required_index(section, token_guard_line, label, token_loop_end_index)
    token_persisted_export_index = _required_index(
        section,
        token_persisted_export_line,
        label,
        start=token_guard_index,
    )
    token_after_persisted_index = token_persisted_export_index
    if allow_default_api_token:
        token_default_guard_index = _required_index(
            section,
            token_default_guard_line,
            label,
            token_persisted_export_index,
        )
        token_default_export_index = _required_index(
            section,
            token_default_export_line,
            label,
            token_default_guard_index,
        )
        assert (
            token_persisted_export_index < token_default_guard_index < token_default_export_index
        ), f"{label} must check the default API token path after persisted restore"
        token_after_persisted_index = token_default_export_index
    token_else_index = _required_index(
        section,
        token_else_line,
        label,
        token_after_persisted_index,
    )
    token_require_index = _required_index(
        section,
        token_require_line,
        label,
        token_else_index,
    )
    token_shell_export_index = _required_index(
        section,
        token_shell_export_line,
        label,
        token_require_index,
    )
    token_guard_end_index = _shell_closing_fi_index(
        section,
        token_shell_export_index,
        label,
    )

    assert (
        decode_start_index
        < decode_end_index
        < strip_start_index
        < strip_end_index
        < token_init_index
        < token_loop_index
        < token_file_guard_index
        < token_read_index
        < token_inline_comment_strip_index
        < min(token_quote_strip_indexes)
        <= max(token_quote_strip_indexes)
        < token_break_index
        < token_loop_end_index
        < token_guard_index
        < token_persisted_export_index
        <= token_after_persisted_index
        < token_else_index
        < token_require_index
        < token_shell_export_index
        < token_guard_end_index
    ), f"{label} must restore persisted or required API token before continuing"
    return decode_start_index, token_guard_end_index


def _assert_source_checkout_postgres_password_restore(
    label: str,
    section: str,
    lifecycle: str,
) -> tuple[int, int]:
    """Assert source-checkout snippets preserve persisted Postgres passwords."""
    unsafe_default_line = 'export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"'
    password_init_line = 'AWF_PERSISTED_POSTGRES_PASSWORD=""'
    legacy_first_password_loop_line = "for env_file in docker/compose/.env .env; do"
    root_first_password_loop_line = "for env_file in .env docker/compose/.env; do"
    root_password_loop_line = "for env_file in .env; do"
    password_loop_line = (
        root_first_password_loop_line
        if root_first_password_loop_line in section
        else root_password_loop_line
    )
    password_file_guard_line = '  [ -f "$env_file" ] || continue'
    password_read_line = SOURCE_CHECKOUT_ENV_READ_LINES["AWF_POSTGRES_PASSWORD"]
    password_inline_comment_strip_line = SOURCE_CHECKOUT_ENV_INLINE_COMMENT_STRIP_LINES[
        "AWF_POSTGRES_PASSWORD"
    ]
    password_quote_strip_lines = SOURCE_CHECKOUT_ENV_QUOTE_STRIP_LINES["AWF_POSTGRES_PASSWORD"]
    password_break_line = '  [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ] && break'
    password_loop_end_line = "done"
    password_guard_line = 'if [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ]; then'
    password_persisted_export_line = (
        '  export AWF_POSTGRES_PASSWORD="$AWF_PERSISTED_POSTGRES_PASSWORD"'
    )
    password_else_line = "else"
    legacy_first_password_require_line = (
        '  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for '
        "the running local Core or persist it in docker/compose/.env or .env before "
        + lifecycle
        + '}"'
    )
    root_first_password_require_line = (
        '  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for '
        "the running local Core or persist it in .env or docker/compose/.env before "
        + lifecycle
        + '}"'
    )
    root_password_require_line = (
        '  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for '
        "the running local Core or persist it in .env before " + lifecycle + '}"'
    )
    password_require_line = (
        root_first_password_require_line
        if root_first_password_require_line in section
        else root_password_require_line
    )
    password_shell_export_line = "  export AWF_POSTGRES_PASSWORD"

    assert unsafe_default_line not in section, f"{label} must not default to awf_dev"
    assert legacy_first_password_loop_line not in section, (
        f"{label} must prefer root .env over legacy docker/compose/.env"
    )
    assert legacy_first_password_require_line not in section, (
        f"{label} must describe root .env before legacy docker/compose/.env"
    )
    assert password_init_line in section, f"{label} must initialize persisted password lookup"
    assert password_loop_line in section, f"{label} must inspect source checkout env files"
    assert password_file_guard_line in section, f"{label} must skip absent env files"
    assert password_read_line in section, f"{label} must read persisted AWF_POSTGRES_PASSWORD"
    assert password_inline_comment_strip_line in section, (
        f"{label} must strip unquoted AWF_POSTGRES_PASSWORD inline comments"
    )
    for password_quote_strip_line in password_quote_strip_lines:
        assert password_quote_strip_line in section, (
            f"{label} must strip quoted persisted AWF_POSTGRES_PASSWORD values"
        )
    assert password_break_line in section, f"{label} must prefer the first persisted password"
    assert password_guard_line in section, f"{label} must branch on persisted password"
    assert password_persisted_export_line in section, (
        f"{label} must export the persisted AWF_POSTGRES_PASSWORD"
    )
    assert password_require_line in section, (
        f"{label} must require AWF_POSTGRES_PASSWORD when no persisted value exists"
    )
    assert password_shell_export_line in section, (
        f"{label} must export restored shell AWF_POSTGRES_PASSWORD"
    )

    password_init_index = section.index(password_init_line)
    password_loop_index = _required_index(section, password_loop_line, label, password_init_index)
    password_file_guard_index = _required_index(
        section,
        password_file_guard_line,
        label,
        password_loop_index,
    )
    password_read_index = _required_index(
        section, password_read_line, label, password_file_guard_index
    )
    password_inline_comment_strip_index = _shell_line_index(
        section,
        password_inline_comment_strip_line,
        label,
        password_read_index,
    )
    password_quote_strip_indexes: list[int] = []
    password_quote_strip_index = password_inline_comment_strip_index
    for password_quote_strip_line in password_quote_strip_lines:
        password_quote_strip_index = _shell_line_index(
            section,
            password_quote_strip_line,
            label,
            password_quote_strip_index,
        )
        password_quote_strip_indexes.append(password_quote_strip_index)
    password_break_index = _required_index(
        section,
        password_break_line,
        label,
        password_quote_strip_indexes[-1],
    )
    password_loop_end_index = _required_index(
        section,
        password_loop_end_line,
        label,
        password_break_index,
    )
    password_guard_index = _required_index(
        section, password_guard_line, label, password_loop_end_index
    )
    password_persisted_export_index = _required_index(
        section,
        password_persisted_export_line,
        label,
        start=password_guard_index,
    )
    password_else_index = _required_index(
        section,
        password_else_line,
        label,
        password_persisted_export_index,
    )
    password_require_index = _required_index(
        section,
        password_require_line,
        label,
        password_else_index,
    )
    password_shell_export_index = _required_index(
        section,
        password_shell_export_line,
        label,
        password_require_index,
    )
    password_guard_end_index = _shell_closing_fi_index(
        section,
        password_shell_export_index,
        label,
    )

    assert (
        password_init_index
        < password_loop_index
        < password_file_guard_index
        < password_read_index
        < password_inline_comment_strip_index
        < min(password_quote_strip_indexes)
        <= max(password_quote_strip_indexes)
        < password_break_index
        < password_loop_end_index
        < password_guard_index
        < password_persisted_export_index
        < password_else_index
        < password_require_index
        < password_shell_export_index
        < password_guard_end_index
    ), f"{label} must restore persisted Postgres password before continuing"
    return password_init_index, password_guard_end_index


def _assert_source_checkout_database_url_restore(
    label: str,
    section: str,
    lifecycle: str,
) -> tuple[int, int]:
    """Assert source-checkout snippets preserve or derive database URLs."""
    host_port_init_line = 'AWF_PERSISTED_POSTGRES_HOST_PORT=""'
    host_port_inline_comment_strip_line = (
        '  AWF_PERSISTED_POSTGRES_HOST_PORT="$(awf_strip_unquoted_dotenv_inline_comment '
        '"$AWF_PERSISTED_POSTGRES_HOST_PORT")"'
    )
    host_port_guard_line = 'if [ -n "$AWF_PERSISTED_POSTGRES_HOST_PORT" ]; then'
    host_port_export_line = '  export AWF_POSTGRES_HOST_PORT="$AWF_PERSISTED_POSTGRES_HOST_PORT"'
    database_url_init_line = 'AWF_PERSISTED_DATABASE_URL=""'
    legacy_first_database_url_loop_line = "for env_file in docker/compose/.env .env; do"
    root_first_database_url_loop_line = "for env_file in .env docker/compose/.env; do"
    root_database_url_loop_line = "for env_file in .env; do"
    database_url_loop_line = (
        root_first_database_url_loop_line
        if root_first_database_url_loop_line in section
        else root_database_url_loop_line
    )
    database_url_inline_comment_strip_line = (
        '  AWF_PERSISTED_DATABASE_URL="$(awf_strip_unquoted_dotenv_inline_comment '
        '"$AWF_PERSISTED_DATABASE_URL")"'
    )
    legacy_first_database_url_require_line = (
        '  : "${AWF_DATABASE_URL:?restore the AWF_DATABASE_URL used for '
        "the running local Core or persist it in docker/compose/.env or .env before "
        + lifecycle
        + '}"'
    )
    root_first_database_url_require_line = (
        '  : "${AWF_DATABASE_URL:?restore the AWF_DATABASE_URL used for '
        "the running local Core or persist it in .env or docker/compose/.env before "
        + lifecycle
        + '}"'
    )
    root_database_url_require_line = (
        '  : "${AWF_DATABASE_URL:?restore the AWF_DATABASE_URL used for '
        "the running local Core or persist it in .env before " + lifecycle + '}"'
    )
    database_url_existing_shell_guard_line = 'elif [ -n "${AWF_DATABASE_URL:-}" ]; then'
    database_url_existing_shell_export_line = "  export AWF_DATABASE_URL"
    requires_inline_comment_strip = (
        DOTENV_UNQUOTED_INLINE_COMMENT_STRIP_FUNCTION_LINES[0] in section
    )

    assert legacy_first_database_url_loop_line not in section, (
        f"{label} must prefer root .env over legacy docker/compose/.env"
    )
    assert legacy_first_database_url_require_line not in section, (
        f"{label} must describe root .env before legacy docker/compose/.env"
    )
    assert root_first_database_url_require_line not in section, (
        f"{label} must allow runtime-derived AWF_DATABASE_URL when none is persisted"
    )
    assert root_database_url_require_line not in section, (
        f"{label} must allow runtime-derived AWF_DATABASE_URL when none is persisted"
    )
    assert host_port_init_line in section, (
        f"{label} must initialize persisted Postgres host port lookup"
    )
    assert database_url_init_line in section, (
        f"{label} must initialize persisted database URL lookup"
    )
    host_port_restore_lines = [
        database_url_loop_line,
        '  [ -f "$env_file" ] || continue',
        SOURCE_CHECKOUT_ENV_READ_LINES["AWF_POSTGRES_HOST_PORT"],
    ]
    if requires_inline_comment_strip:
        host_port_restore_lines.append(host_port_inline_comment_strip_line)
    host_port_restore_lines.extend(
        [
            *SOURCE_CHECKOUT_ENV_QUOTE_STRIP_LINES["AWF_POSTGRES_HOST_PORT"],
            '  [ -n "$AWF_PERSISTED_POSTGRES_HOST_PORT" ] && break',
            "done",
            host_port_guard_line,
            host_port_export_line,
        ]
    )
    database_url_restore_lines = [
        database_url_loop_line,
        '  [ -f "$env_file" ] || continue',
        SOURCE_CHECKOUT_ENV_READ_LINES["AWF_DATABASE_URL"],
    ]
    if requires_inline_comment_strip:
        database_url_restore_lines.append(database_url_inline_comment_strip_line)
    database_url_restore_lines.extend(
        [
            *SOURCE_CHECKOUT_ENV_QUOTE_STRIP_LINES["AWF_DATABASE_URL"],
            '  [ -n "$AWF_PERSISTED_DATABASE_URL" ] && break',
            "done",
            'if [ -n "$AWF_PERSISTED_DATABASE_URL" ]; then',
            '  export AWF_DATABASE_URL="$AWF_PERSISTED_DATABASE_URL"',
            database_url_existing_shell_guard_line,
            database_url_existing_shell_export_line,
        ]
    )

    host_port_init_index = section.index(host_port_init_line)
    current_index = host_port_init_index
    for host_port_restore_line in host_port_restore_lines:
        current_index = _shell_line_index(
            section,
            host_port_restore_line,
            label,
            current_index,
        )
    host_port_guard_end_index = _shell_closing_fi_index(
        section,
        current_index,
        label,
    )

    database_url_init_index = _shell_line_index(
        section,
        database_url_init_line,
        label,
        host_port_guard_end_index,
    )
    current_index = database_url_init_index
    for database_url_restore_line in database_url_restore_lines:
        current_index = _shell_line_index(
            section,
            database_url_restore_line,
            label,
            current_index,
        )
    database_url_guard_end_index = _shell_closing_fi_index(
        section,
        current_index,
        label,
    )
    return host_port_init_index, database_url_guard_end_index


def _assert_source_checkout_stop_prefers_root_env(
    label: str,
    section: str,
    start: int,
    *,
    require_legacy_fallback: bool = False,
) -> tuple[int, int]:
    """Assert source-checkout stop commands use checkout-root .env before legacy env."""
    bare_root_stop_line = "docker compose --env-file .env -f docker/compose/local-service.yml stop"
    root_stop_guard_line = "if [ -f .env ]; then"
    root_stop_line = "  docker compose --env-file .env -f docker/compose/local-service.yml stop"
    legacy_first_stop_guard_line = "if [ -f docker/compose/.env ]; then"
    legacy_stop_guard_line = "elif [ -f docker/compose/.env ]; then"
    legacy_stop_line = (
        "  docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml stop"
    )
    stop_else_line = "else"
    stop_fallback_line = "  docker compose -f docker/compose/local-service.yml stop"

    assert legacy_first_stop_guard_line not in section[start:].splitlines(), (
        f"{label} must prefer checkout root .env over legacy docker/compose/.env"
    )

    bare_root_stop_match = re.search(rf"(?m)^{re.escape(bare_root_stop_line)}$", section[start:])
    if bare_root_stop_match is not None:
        assert not require_legacy_fallback, f"{label} must keep legacy compose env fallback"
        bare_root_stop_index = start + bare_root_stop_match.start()
        return bare_root_stop_index, bare_root_stop_index + len(bare_root_stop_line)

    root_stop_guard_index = _shell_line_index(section, root_stop_guard_line, label, start)
    root_stop_index = _shell_line_index(section, root_stop_line, label, root_stop_guard_index)
    legacy_stop_guard_match = re.search(
        rf"(?m)^{re.escape(legacy_stop_guard_line)}$",
        section[root_stop_index:],
    )
    if legacy_stop_guard_match is None:
        assert not require_legacy_fallback, f"{label} must keep legacy compose env fallback"
        stop_else_index = _shell_line_index(section, stop_else_line, label, root_stop_index)
        stop_fallback_index = _shell_line_index(section, stop_fallback_line, label, stop_else_index)
        stop_guard_end_index = _shell_closing_fi_index(section, stop_fallback_index, label)

        assert (
            root_stop_guard_index
            < root_stop_index
            < stop_else_index
            < stop_fallback_index
            < stop_guard_end_index
        ), f"{label} must stop with root .env before unconfigured compose fallback"
        return root_stop_guard_index, stop_guard_end_index

    legacy_stop_guard_index = root_stop_index + legacy_stop_guard_match.start()
    legacy_stop_index = _shell_line_index(section, legacy_stop_line, label, legacy_stop_guard_index)
    stop_else_index = _shell_line_index(section, stop_else_line, label, legacy_stop_index)
    stop_fallback_index = _shell_line_index(section, stop_fallback_line, label, stop_else_index)
    stop_guard_end_index = _shell_closing_fi_index(section, stop_fallback_index, label)

    assert (
        root_stop_guard_index
        < root_stop_index
        < legacy_stop_guard_index
        < legacy_stop_index
        < stop_else_index
        < stop_fallback_index
        < stop_guard_end_index
    ), f"{label} must stop with root .env before legacy compose env fallback"
    return root_stop_guard_index, stop_guard_end_index


def _assert_source_checkout_service_env_restore_and_stop(
    label: str,
    section: str,
    lifecycle: str,
    *,
    require_database_url_restore: bool = False,
    require_legacy_fallback: bool = False,
) -> tuple[int, int, int, int]:
    """Assert source-checkout snippets restore service secrets before stopping Core."""
    api_restore_start_index, api_restore_end_index = _assert_source_checkout_api_token_restore(
        label,
        section,
        lifecycle,
    )
    password_restore_start_index, password_restore_end_index = (
        _assert_source_checkout_postgres_password_restore(
            label,
            section,
            lifecycle,
        )
    )
    env_restore_end_index = password_restore_end_index
    database_url_restore_start_index = password_restore_end_index
    if require_database_url_restore:
        database_url_restore_start_index, env_restore_end_index = (
            _assert_source_checkout_database_url_restore(
                label,
                section,
                lifecycle,
            )
        )
    stop_index, _stop_end_index = _assert_source_checkout_stop_prefers_root_env(
        label,
        section,
        env_restore_end_index,
        require_legacy_fallback=require_legacy_fallback,
    )

    assert (
        api_restore_start_index
        < api_restore_end_index
        < password_restore_start_index
        < password_restore_end_index
        <= database_url_restore_start_index
        <= env_restore_end_index
        < stop_index
    ), f"{label} must restore service environment before stopping Core"
    return api_restore_start_index, env_restore_end_index, stop_index, _stop_end_index


def _assert_source_checkout_service_env_restore_before_stop(
    label: str,
    section: str,
    lifecycle: str,
    *,
    require_legacy_fallback: bool = False,
) -> tuple[int, int]:
    """Assert source-checkout snippets restore service secrets before stopping Core."""
    api_restore_start_index, password_restore_end_index, _stop_index, _stop_end_index = (
        _assert_source_checkout_service_env_restore_and_stop(
            label,
            section,
            lifecycle,
            require_legacy_fallback=require_legacy_fallback,
        )
    )
    return api_restore_start_index, password_restore_end_index


def _assert_package_upgrade_restores_service_env(
    label: str,
    section: str,
    upgrade_line: str | None,
    lifecycle: str = "upgrading",
) -> None:
    """Assert package upgrade snippets restore service environment before restart."""
    api_read_line = PACKAGE_ENV_READ_LINES["AWF_API_TOKEN"]
    api_inline_comment_strip_line = PACKAGE_ENV_INLINE_COMMENT_STRIP_LINES["AWF_API_TOKEN"]
    api_quote_strip_lines = PACKAGE_ENV_QUOTE_STRIP_LINES["AWF_API_TOKEN"]
    api_guard_line = 'if [ -n "$AWF_PERSISTED_API_TOKEN" ]; then'
    api_persisted_export_line = '  export AWF_API_TOKEN="$AWF_PERSISTED_API_TOKEN"'
    api_else_line = "else"
    api_require_line = (
        '  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core '
        f'or persist it in .env before {lifecycle}}}"'
    )
    api_shell_export_line = "  export AWF_API_TOKEN"
    unsafe_api_grep_guard_line = (
        "if ! grep -q '^[[:space:]]*\\(export[[:space:]][[:space:]]*\\)\\{0,1\\}"
        "AWF_API_TOKEN[[:space:]]*=[[:space:]]*[^[:space:]]' .env 2>/dev/null; then"
    )
    unsafe_api_generation_line = (
        '  export AWF_API_TOKEN="${AWF_API_TOKEN:-$(openssl rand -hex 32)}"'
    )
    password_read_line = PACKAGE_ENV_READ_LINES["AWF_POSTGRES_PASSWORD"]
    password_inline_comment_strip_line = PACKAGE_ENV_INLINE_COMMENT_STRIP_LINES[
        "AWF_POSTGRES_PASSWORD"
    ]
    password_quote_strip_lines = PACKAGE_ENV_QUOTE_STRIP_LINES["AWF_POSTGRES_PASSWORD"]
    password_guard_line = 'if [ -n "$AWF_PERSISTED_POSTGRES_PASSWORD" ]; then'
    password_persisted_export_line = (
        '  export AWF_POSTGRES_PASSWORD="$AWF_PERSISTED_POSTGRES_PASSWORD"'
    )
    password_else_line = "else"
    password_require_line = (
        '  : "${AWF_POSTGRES_PASSWORD:?restore the AWF_POSTGRES_PASSWORD used for '
        f'the running local Core or persist it in .env before {lifecycle}}}"'
    )
    password_shell_export_line = "  export AWF_POSTGRES_PASSWORD"
    unsafe_password_grep_guard_line = (
        "if ! grep -q '^[[:space:]]*\\(export[[:space:]][[:space:]]*\\)\\{0,1\\}"
        "AWF_POSTGRES_PASSWORD[[:space:]]*=[[:space:]]*[^[:space:]]' .env "
        "2>/dev/null; then"
    )
    unsafe_password_default_line = (
        'export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"'
    )
    database_url_read_line = PACKAGE_ENV_READ_LINES["AWF_DATABASE_URL"]
    database_url_inline_comment_strip_line = PACKAGE_ENV_INLINE_COMMENT_STRIP_LINES[
        "AWF_DATABASE_URL"
    ]
    database_url_quote_strip_lines = PACKAGE_ENV_QUOTE_STRIP_LINES["AWF_DATABASE_URL"]
    database_url_guard_line = 'if [ -n "$AWF_PERSISTED_DATABASE_URL" ]; then'
    database_url_persisted_export_line = '  export AWF_DATABASE_URL="$AWF_PERSISTED_DATABASE_URL"'
    database_url_else_line = "else"
    database_url_require_line = (
        '  : "${AWF_DATABASE_URL:?restore the AWF_DATABASE_URL used for '
        f'the running local Core or persist it in .env before {lifecycle}}}"'
    )
    database_url_shell_export_line = "  export AWF_DATABASE_URL"
    start_line = "\nawf start\n"

    if upgrade_line is not None:
        assert upgrade_line in section, f"{label} is missing upgrade command"
    for decode_line in DOTENV_DOUBLE_QUOTE_DECODE_FUNCTION_LINES:
        assert decode_line in section, f"{label} must decode double-quoted dotenv escapes"
    for strip_function_line in DOTENV_UNQUOTED_INLINE_COMMENT_STRIP_FUNCTION_LINES:
        assert strip_function_line in section, (
            f"{label} must define unquoted dotenv inline-comment stripping"
        )
    assert unsafe_api_grep_guard_line not in section, (
        f"{label} must not let stale shell AWF_API_TOKEN override persisted .env"
    )
    assert api_read_line in section, f"{label} must read persisted AWF_API_TOKEN"
    assert api_inline_comment_strip_line in section, (
        f"{label} must strip unquoted AWF_API_TOKEN inline comments"
    )
    for api_quote_strip_line in api_quote_strip_lines:
        assert api_quote_strip_line in section, f"{label} must strip quoted AWF_API_TOKEN"
    assert api_guard_line in section, f"{label} must branch on persisted AWF_API_TOKEN"
    assert api_persisted_export_line in section, f"{label} must export persisted AWF_API_TOKEN"
    assert api_require_line in section, f"{label} must require the existing AWF_API_TOKEN"
    assert api_shell_export_line in section, f"{label} must export restored AWF_API_TOKEN"
    assert unsafe_api_generation_line not in section, f"{label} must not regenerate AWF_API_TOKEN"
    assert unsafe_password_grep_guard_line not in section, (
        f"{label} must not let stale shell AWF_POSTGRES_PASSWORD override persisted .env"
    )
    assert password_read_line in section, f"{label} must read persisted AWF_POSTGRES_PASSWORD"
    assert password_inline_comment_strip_line in section, (
        f"{label} must strip unquoted AWF_POSTGRES_PASSWORD inline comments"
    )
    for password_quote_strip_line in password_quote_strip_lines:
        assert password_quote_strip_line in section, (
            f"{label} must strip quoted AWF_POSTGRES_PASSWORD"
        )
    assert password_guard_line in section, f"{label} must branch on persisted AWF_POSTGRES_PASSWORD"
    assert password_persisted_export_line in section, (
        f"{label} must export persisted AWF_POSTGRES_PASSWORD"
    )
    assert unsafe_password_default_line not in section, (
        f"{label} must not default AWF_POSTGRES_PASSWORD"
    )
    assert password_require_line in section, (
        f"{label} must require the existing AWF_POSTGRES_PASSWORD"
    )
    assert password_shell_export_line in section, (
        f"{label} must export restored AWF_POSTGRES_PASSWORD"
    )
    assert database_url_read_line in section, f"{label} must read persisted AWF_DATABASE_URL"
    assert database_url_inline_comment_strip_line in section, (
        f"{label} must strip unquoted AWF_DATABASE_URL inline comments"
    )
    for database_url_quote_strip_line in database_url_quote_strip_lines:
        assert database_url_quote_strip_line in section, (
            f"{label} must strip quoted AWF_DATABASE_URL"
        )
    assert database_url_guard_line in section, f"{label} must branch on persisted AWF_DATABASE_URL"
    assert database_url_persisted_export_line in section, (
        f"{label} must export persisted AWF_DATABASE_URL"
    )
    assert database_url_require_line in section, (
        f"{label} must require the existing AWF_DATABASE_URL"
    )
    assert database_url_shell_export_line in section, (
        f"{label} must export restored AWF_DATABASE_URL"
    )
    assert start_line in section, f"{label} is missing restart command"

    upgrade_index = -1 if upgrade_line is None else _required_index(section, upgrade_line, label)
    search_start_index = 0 if upgrade_line is None else upgrade_index
    decode_start_index, decode_end_index = _assert_dotenv_decode_function(
        section,
        label,
        search_start_index,
    )
    strip_start_index, strip_end_index = _assert_dotenv_inline_comment_strip_function(
        section,
        label,
        decode_end_index,
    )
    api_read_index = _shell_line_index(section, api_read_line, label, strip_end_index)
    api_inline_comment_strip_index = _shell_line_index(
        section,
        api_inline_comment_strip_line,
        label,
        api_read_index,
    )
    api_quote_strip_indexes = _assert_package_env_quote_strip_lines(
        label=label,
        section=section,
        lines=api_quote_strip_lines,
        start=api_inline_comment_strip_index,
    )
    api_guard_index = _shell_line_index(
        section,
        api_guard_line,
        label,
        api_quote_strip_indexes[-1],
    )
    api_persisted_export_index = _shell_line_index(
        section,
        api_persisted_export_line,
        label,
        api_guard_index,
    )
    api_else_index = _shell_line_index(section, api_else_line, label, api_persisted_export_index)
    api_require_index = _shell_line_index(section, api_require_line, label, api_else_index)
    api_shell_export_index = _shell_line_index(
        section,
        api_shell_export_line,
        label,
        api_require_index,
    )
    api_guard_end_index = _shell_closing_fi_index(section, api_shell_export_index, label)
    password_read_index = _shell_line_index(section, password_read_line, label, api_guard_end_index)
    password_inline_comment_strip_index = _shell_line_index(
        section,
        password_inline_comment_strip_line,
        label,
        password_read_index,
    )
    password_quote_strip_indexes = _assert_package_env_quote_strip_lines(
        label=label,
        section=section,
        lines=password_quote_strip_lines,
        start=password_inline_comment_strip_index,
    )
    password_guard_index = _shell_line_index(
        section,
        password_guard_line,
        label,
        password_quote_strip_indexes[-1],
    )
    password_persisted_export_index = _shell_line_index(
        section,
        password_persisted_export_line,
        label,
        password_guard_index,
    )
    password_else_index = _shell_line_index(
        section,
        password_else_line,
        label,
        password_persisted_export_index,
    )
    password_require_index = _shell_line_index(
        section,
        password_require_line,
        label,
        password_else_index,
    )
    password_shell_export_index = _shell_line_index(
        section,
        password_shell_export_line,
        label,
        password_require_index,
    )
    password_guard_end_index = _shell_closing_fi_index(
        section,
        password_shell_export_index,
        label,
    )
    database_url_read_index = _shell_line_index(
        section,
        database_url_read_line,
        label,
        password_guard_end_index,
    )
    database_url_inline_comment_strip_index = _shell_line_index(
        section,
        database_url_inline_comment_strip_line,
        label,
        database_url_read_index,
    )
    database_url_quote_strip_indexes = _assert_package_env_quote_strip_lines(
        label=label,
        section=section,
        lines=database_url_quote_strip_lines,
        start=database_url_inline_comment_strip_index,
    )
    database_url_guard_index = _shell_line_index(
        section,
        database_url_guard_line,
        label,
        database_url_quote_strip_indexes[-1],
    )
    database_url_persisted_export_index = _shell_line_index(
        section,
        database_url_persisted_export_line,
        label,
        database_url_guard_index,
    )
    database_url_else_index = _shell_line_index(
        section,
        database_url_else_line,
        label,
        database_url_persisted_export_index,
    )
    database_url_require_index = _shell_line_index(
        section,
        database_url_require_line,
        label,
        database_url_else_index,
    )
    database_url_shell_export_index = _shell_line_index(
        section,
        database_url_shell_export_line,
        label,
        database_url_require_index,
    )
    database_url_guard_end_index = _shell_closing_fi_index(
        section,
        database_url_shell_export_index,
        label,
    )
    start_index = _required_index(section, start_line, label, start=database_url_guard_end_index)
    assert (
        upgrade_index
        < decode_start_index
        < decode_end_index
        < strip_start_index
        < strip_end_index
        < api_read_index
        < api_inline_comment_strip_index
        < min(api_quote_strip_indexes)
        <= max(api_quote_strip_indexes)
        < api_guard_index
        < api_persisted_export_index
        < api_else_index
        < api_require_index
        < api_shell_export_index
        < api_guard_end_index
        < password_read_index
        < password_inline_comment_strip_index
        < min(password_quote_strip_indexes)
        <= max(password_quote_strip_indexes)
        < password_guard_index
        < password_persisted_export_index
        < password_else_index
        < password_require_index
        < password_shell_export_index
        < password_guard_end_index
        < database_url_read_index
        < database_url_inline_comment_strip_index
        < min(database_url_quote_strip_indexes)
        <= max(database_url_quote_strip_indexes)
        < database_url_guard_index
        < database_url_persisted_export_index
        < database_url_else_index
        < database_url_require_index
        < database_url_shell_export_index
        < database_url_guard_end_index
        < start_index
    ), f"{label} must restore missing service env before restart"


def _public_docs() -> set[str]:
    public_docs = _all_public_markdown_docs()
    public_docs.update(_readme_public_doc_links())
    public_docs.update(_present_docs(OPTIONAL_PUBLIC_GUIDES))
    return {doc for doc in public_docs if _is_public_doc_path(doc)}


def _all_public_markdown_docs() -> set[str]:
    docs_dir = REPO_ROOT / "docs"
    if not docs_dir.exists():
        return set()

    return {
        path.relative_to(REPO_ROOT).as_posix()
        for path in docs_dir.rglob("*.md")
        if _is_public_doc_path(path.relative_to(REPO_ROOT).as_posix())
    }


def _docs_index_links() -> set[str]:
    links: set[str] = set()
    for index_path in DOCS_INDEX_CANDIDATES:
        if index_path.exists():
            links.update(_markdown_doc_links(index_path))
    return {link for link in links if _is_public_doc_path(link)}


def _readme_public_doc_links() -> set[str]:
    return {link for link in _markdown_doc_links(README_PATH) if _is_public_doc_path(link)}


def _markdown_doc_links(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return {
        resolved
        for target in LINK_RE.findall(text)
        if (resolved := _resolve_markdown_link(path, target)) is not None
    }


def _resolve_markdown_link(source_path: Path, target: str) -> str | None:
    target = target.strip()
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return None
    raw_path = unquote(parsed.path)
    if not raw_path or raw_path.startswith("#") or not raw_path.endswith(".md"):
        return None

    base_dir = source_path.parent
    resolved = (base_dir / raw_path).resolve()
    try:
        rel_path = resolved.relative_to(REPO_ROOT)
    except ValueError:
        return None
    return rel_path.as_posix()


def _is_public_doc_path(rel_path: str) -> bool:
    if rel_path in ROOT_PUBLIC_DOC_NAMES:
        return True
    if not rel_path.startswith("docs/"):
        return False
    if any(rel_path.startswith(prefix) for prefix in INTERNAL_DOC_PREFIXES):
        return False
    return rel_path not in PLANNING_DOC_NAMES


def _present_docs(candidates: Iterable[str]) -> set[str]:
    return {rel_path for rel_path in candidates if (REPO_ROOT / rel_path).exists()}


def _typer_command_tree(typer_app: object) -> set[tuple[str, ...]]:
    command_paths: set[tuple[str, ...]] = set()
    for command in typer_app.registered_commands:
        command_paths.add((_command_name(command),))
    for group in typer_app.registered_groups:
        group_name = group.name
        for command in group.typer_instance.registered_commands:
            command_paths.add((group_name, _command_name(command)))
    return command_paths


def _command_name(command: object) -> str:
    explicit_name = command.name
    if explicit_name:
        return explicit_name
    return command.callback.__name__.replace("_", "-")


def _awf_command_mentions(paths: Iterable[Path]) -> list[AwfCommandMention]:
    root_groups = {path[0] for path in _typer_command_tree(app) if len(path) == 2}
    mentions: list[AwfCommandMention] = []
    for rel_path in paths:
        doc_path = REPO_ROOT / rel_path
        if not doc_path.exists():
            continue
        text = doc_path.read_text(encoding="utf-8")
        collapsed = re.sub(r"\\\n\s*", " ", text)
        for line in collapsed.splitlines():
            if _ignore_awf_command_line(line):
                continue
            for match in AWF_COMMAND_RE.finditer(line):
                raw_command = f"awf{match.group('tail')}".strip()
                command_path = _awf_command_path(raw_command, root_groups)
                if command_path is None:
                    continue
                mentions.append(
                    AwfCommandMention(
                        path=rel_path.as_posix(),
                        command=raw_command,
                        command_path=command_path,
                    )
                )
    return mentions


def _ignore_awf_command_line(line: str) -> bool:
    lowered = line.lower()
    return "currently not implemented" in lowered or "future" in lowered


def _awf_command_path(
    raw_command: str,
    root_groups: set[str],
) -> tuple[str, ...] | None:
    try:
        tokens = shlex.split(raw_command, comments=True)
    except ValueError:
        tokens = raw_command.split()
    tokens = [_clean_token(token) for token in tokens]
    if not tokens or tokens[0] != "awf":
        return None
    if len(tokens) == 1:
        return None

    first = tokens[1]
    if not _looks_like_command_token(first):
        return None
    if first not in root_groups:
        return (first,)
    if len(tokens) < 3:
        return None

    second = tokens[2]
    if not _looks_like_command_token(second):
        return None
    return (first, second)


def _clean_token(token: str) -> str:
    return token.strip("`'\".,: ")


def _looks_like_command_token(token: str) -> bool:
    if not token or token.startswith("-"):
        return False
    if token.startswith(("<", "{", "$")):
        return False
    if token in {".", "...", "&&", "||", "|", ";"}:
        return False
    return not ("/" in token or "=" in token)


def _copy_paste_docs() -> set[str]:
    docs = _present_docs(COPY_PASTE_DOC_HINTS)
    docs.update(
        rel_path
        for rel_path in _present_docs(_public_docs())
        if "copy-paste" in (REPO_ROOT / rel_path).read_text(encoding="utf-8").lower()
        and rel_path.endswith(".md")
    )
    return docs


def _fence_delimiter_count_is_even(text: str) -> bool:
    return len(FENCE_DELIMITER_RE.findall(text)) % 2 == 0


def _markdown_fences(rel_path: str, text: str) -> list[MarkdownFence]:
    fences: list[MarkdownFence] = []
    lines = text.splitlines()
    line_index = 0

    while line_index < len(lines):
        opening_match = OPENING_FENCE_RE.match(lines[line_index])
        if opening_match is None:
            line_index += 1
            continue

        opening_line = line_index + 1
        indent_width = len(opening_match.group("indent"))
        delimiter_width = len(opening_match.group("delimiter"))
        body_lines: list[str] = []
        line_index += 1

        while line_index < len(lines):
            line = lines[line_index]
            if _is_closing_fence(line, delimiter_width):
                fences.append(
                    MarkdownFence(
                        path=rel_path,
                        line=opening_line,
                        language=opening_match.group("language").lower(),
                        body="\n".join(body_lines).strip("\n"),
                    )
                )
                break

            body_lines.append(_strip_fence_body_indent(line, indent_width))
            line_index += 1
        line_index += 1
    return fences


def _is_closing_fence(line: str, delimiter_width: int) -> bool:
    match = re.match(r"^ {0,3}(?P<delimiter>`{3,})[ \t]*$", line)
    return match is not None and len(match.group("delimiter")) >= delimiter_width


def _strip_fence_body_indent(line: str, indent_width: int) -> str:
    leading_spaces = len(line) - len(line.lstrip(" "))
    return line[min(indent_width, leading_spaces) :]


def _assert_snippet_syntax(fence: MarkdownFence) -> None:
    if not fence.body.strip():
        pytest.fail(f"{fence.path}:{fence.line} has an empty copy-paste snippet")

    language = fence.language
    if language == "json":
        json.loads(fence.body)
    elif language in {"yaml", "yml"}:
        yaml.safe_load(fence.body)
    elif language == "python":
        ast.parse(fence.body)
    elif language in {"bash", "sh", "shell"}:
        script = _strip_shell_prompts(fence.body)
        try:
            result = subprocess.run(
                ["bash", "-n"],
                input=script,
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
        except FileNotFoundError:
            pytest.fail(
                f"{fence.path}:{fence.line} cannot validate shell snippet because "
                "bash is not available on PATH"
            )
        assert result.returncode == 0, (
            f"{fence.path}:{fence.line} has invalid shell syntax: {result.stderr.strip()}"
        )
    else:
        assert fence.body.strip(), f"{fence.path}:{fence.line} has empty text snippet"


def _strip_shell_prompts(script: str) -> str:
    stripped_lines: list[str] = []
    for line in script.splitlines():
        if line.startswith(("$ ", "> ")):
            stripped_lines.append(line[2:])
        else:
            stripped_lines.append(line)
    return "\n".join(stripped_lines)
