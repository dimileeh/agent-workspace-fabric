"""Shared shell-scope helpers for Playwright setup detection."""

from __future__ import annotations

from awf.runtime.node_playwright_setup import (
    _ENV_ASSIGNMENT_RE,
    _SHELL_COMPOUND_CONTROL_TOKENS,
)


def _assignment_preamble_command_index(tokens: list[str], index: int) -> int:
    assignment_start = index
    while index < len(tokens) and _ENV_ASSIGNMENT_RE.fullmatch(tokens[index]):
        index += 1
    if (
        index > assignment_start
        and index < len(tokens)
        and tokens[index] in {"&&", ";"}
        and index + 1 < len(tokens)
    ):
        return index + 1
    return index


def _sequential_command_next_index(tokens: list[str], index: int) -> int | None:
    command_index = index + 1
    while command_index < len(tokens):
        token = tokens[command_index]
        if token in {"&&", ";"}:
            return command_index + 1 if command_index + 1 < len(tokens) else None
        if token in {"||", "|", "|&", "&"}:
            return None
        command_index += 1
    return None


def _leading_cd_package_scope(tokens: list[str], index: int) -> tuple[str, int] | None:
    if index >= len(tokens) or tokens[index] != "cd":
        return None
    package_dir_index = index + 1
    if package_dir_index < len(tokens) and tokens[package_dir_index] == "--":
        package_dir_index += 1
    if package_dir_index >= len(tokens):
        return None
    package_dir = tokens[package_dir_index]
    if (
        package_dir in _SHELL_COMPOUND_CONTROL_TOKENS
        or package_dir.startswith("-")
        or _package_scope_uses_shell_expansion(package_dir)
    ):
        return None
    separator_index = package_dir_index + 1
    if separator_index >= len(tokens) or tokens[separator_index] not in {"&&", ";"}:
        return None
    install_index = separator_index + 1
    install_index = _assignment_preamble_command_index(tokens, install_index)
    if install_index >= len(tokens):
        return None
    return package_dir, install_index


def _package_scope_uses_shell_expansion(package_dir: str) -> bool:
    return "$" in package_dir or "`" in package_dir
