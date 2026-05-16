"""Extract concise repair evidence from GitHub Actions failed-step logs."""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass

from awf.common.redaction import redact_secrets

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_PYTEST_NODE_COMPONENT = r"(?:[^\s:\[]+|\[[^\]]*\])+"
_PYTEST_NODE_PATH = r"[^\s:]+\.py"
_PYTEST_NODE_RE = re.compile(
    rf"(?<!\S)(?P<node>{_PYTEST_NODE_PATH}(?:::{_PYTEST_NODE_COMPONENT})+)(?=\s+-\s+|$)"
)
_FAILED_PYTEST_NODE_RE = re.compile(
    rf"\bFAILED\s+(?P<node>{_PYTEST_NODE_PATH}(?:::{_PYTEST_NODE_COMPONENT})+)(?:\s+-\s+|$)"
)
_RUFF_DIAGNOSTIC_RE = re.compile(r"\b(?:src|tests)/[^\s:]+\.py:\d+:\d+:")
_COMMAND_MARKERS = (
    "uv run ",
    "python -m pytest",
    "pytest ",
    "ruff check ",
    "ruff format ",
    "mypy ",
    "npm ",
)
_MAX_TEST_NODES = 20
_MAX_REPRO_NODES = 5
_MAX_COMMANDS = 5
_MAX_SNIPPETS = 8
_MAX_SUMMARIES = 8
_MAX_LINE_CHARS = 500
_DEFAULT_PYTEST_REPRO_COMMAND = "uv run --python 3.12 --extra dev pytest"


@dataclass(frozen=True)
class CiFailureEvidence:
    """Structured repair hints derived from one failed CI check log."""

    failing_commands: tuple[str, ...] = ()
    test_node_ids: tuple[str, ...] = ()
    assertion_snippets: tuple[str, ...] = ()
    error_summaries: tuple[str, ...] = ()
    suggested_repro_commands: tuple[str, ...] = ()
    evidence_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ShellToken:
    value: str
    end_index: int


def extract_ci_failure_evidence(
    log_text: str,
    *,
    check_name: str,
    pytest_fallback_commands: Iterable[str] = (),
) -> CiFailureEvidence:
    """Return focused, redacted repair evidence from a failed CI log."""

    safe_log = redact_secrets(log_text or "")
    if not safe_log.strip():
        return CiFailureEvidence(
            evidence_warnings=(f"GitHub Actions log unavailable for failed check {check_name}.",)
        )

    lines = tuple(_clean_line(line) for line in safe_log.splitlines())
    non_empty_lines = tuple(line for line in lines if line)
    display_lines = tuple(_truncate_line(line) for line in non_empty_lines)
    test_nodes = _dedupe_preserving_values(_extract_test_nodes(non_empty_lines))[:_MAX_TEST_NODES]
    failing_commands = _dedupe(_extract_commands(non_empty_lines))[:_MAX_COMMANDS]
    assertion_snippets = _dedupe(_extract_assertion_snippets(display_lines))[:_MAX_SNIPPETS]
    error_summaries = _dedupe(_extract_error_summaries(display_lines))[:_MAX_SUMMARIES]
    suggested_repro_commands = _suggest_repro_commands(
        test_node_ids=test_nodes,
        failing_commands=failing_commands,
        pytest_fallback_commands=pytest_fallback_commands,
    )
    return CiFailureEvidence(
        failing_commands=tuple(failing_commands),
        test_node_ids=tuple(test_nodes),
        assertion_snippets=tuple(assertion_snippets),
        error_summaries=tuple(error_summaries),
        suggested_repro_commands=tuple(suggested_repro_commands),
    )


def redact_ci_log(log_text: str) -> str:
    """Redact a raw CI log before storage, tailing, or prompt rendering."""

    return redact_secrets(log_text or "")


def _clean_line(line: str) -> str:
    return _ANSI_RE.sub("", line).replace("\r", "").strip()


def _truncate_line(line: str) -> str:
    return line[:_MAX_LINE_CHARS]


def _extract_test_nodes(lines: Iterable[str]) -> list[str]:
    nodes: list[str] = []
    for line in lines:
        failed_match = _FAILED_PYTEST_NODE_RE.search(line)
        if failed_match:
            nodes.append(failed_match.group("node").strip())
            continue
        for match in _PYTEST_NODE_RE.finditer(line):
            nodes.append(_strip_node_suffix(match.group("node")))
    return nodes


def _strip_node_suffix(node: str) -> str:
    return node.rstrip(",:;)")


def _extract_commands(lines: Iterable[str]) -> list[str]:
    commands: list[str] = []
    for line in lines:
        command = _extract_command_from_line(line)
        if command:
            commands.append(command)
    return commands


def _extract_command_from_line(line: str) -> str | None:
    if not _is_github_run_step_line(line):
        return None
    for marker in _COMMAND_MARKERS:
        index = line.find(marker)
        if index >= 0:
            return line[index:].strip()
    return None


def _is_github_run_step_line(line: str) -> bool:
    parts = [part.strip() for part in line.split("\t") if part.strip()]
    return len(parts) >= 3 and any(part.startswith("Run ") for part in parts[:-1])


def _extract_assertion_snippets(lines: Iterable[str]) -> list[str]:
    snippets: list[str] = []
    for line in lines:
        if "AssertionError" in line or line.startswith(("E   ", ">   ")):
            snippets.append(line)
    return snippets


def _extract_error_summaries(lines: Iterable[str]) -> list[str]:
    summaries: list[str] = []
    for line in lines:
        if (
            line.startswith("FAILED ")
            or "AssertionError" in line
            or line.startswith("Error:")
            or "Process completed with exit code" in line
            or _RUFF_DIAGNOSTIC_RE.search(line)
            or line.lower().startswith(("error ", "error:", "fatal:"))
        ):
            summaries.append(line)
    return summaries


def _suggest_repro_commands(
    *,
    test_node_ids: list[str],
    failing_commands: list[str],
    pytest_fallback_commands: Iterable[str] = (),
) -> list[str]:
    if test_node_ids:
        command = _pytest_repro_command(failing_commands)
        if command is None:
            command = _pytest_repro_command(pytest_fallback_commands)
        if command is None:
            command = _DEFAULT_PYTEST_REPRO_COMMAND
        selected = test_node_ids[:_MAX_REPRO_NODES]
        quoted = " ".join(shlex.quote(node_id) for node_id in selected)
        return [f"{command} {quoted} -q"]
    return []


def _pytest_repro_command(failing_commands: Iterable[str]) -> str | None:
    for command in failing_commands:
        try:
            tokens = _shell_tokens(command)
        except ValueError:
            continue
        for index, token in enumerate(tokens):
            if token.value == "pytest":
                return command[: token.end_index].strip()
            if (
                token.value == "-m"
                and index + 1 < len(tokens)
                and tokens[index + 1].value == "pytest"
            ):
                return command[: tokens[index + 1].end_index].strip()
    return None


def _shell_tokens(command: str) -> list[_ShellToken]:
    tokens: list[_ShellToken] = []
    value: list[str] = []
    token_started = False
    quote: str | None = None
    escape = False
    index = 0
    while index < len(command):
        char = command[index]
        if escape:
            value.append(char)
            escape = False
            index += 1
            continue
        if quote is None and char.isspace():
            if token_started:
                tokens.append(_ShellToken(value="".join(value), end_index=index))
                value = []
                token_started = False
            index += 1
            continue
        if not token_started:
            token_started = True
        if quote is None and char == "\\":
            escape = True
            index += 1
            continue
        if char in ("'", '"') and (quote is None or quote == char):
            quote = None if quote == char else char
            index += 1
            continue
        if quote == '"' and char == "\\":
            escape = True
            index += 1
            continue
        value.append(char)
        index += 1
    if escape or quote is not None:
        raise ValueError
    if token_started:
        tokens.append(_ShellToken(value="".join(value), end_index=len(command)))
    return tokens


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        cleaned = " ".join(item.split()).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        deduped.append(cleaned)
    return deduped


def _dedupe_preserving_values(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        deduped.append(cleaned)
    return deduped
