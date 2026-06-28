"""Extract concise repair evidence from GitHub Actions failed-step logs."""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass

from awf.common.redaction import redact_secrets

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_PYTHON_DIAGNOSTIC_RE = re.compile(
    r"\b(?:src|tests)/[^\s:]+\.py:\d+(?::\d+)?:\s*(?:error|warning):"
)
_RUFF_DIAGNOSTIC_RE = re.compile(r"\b(?:src|tests)/[^\s:]+\.py:\d+:\d+:\s*[A-Z][A-Z0-9]+\b")
_COMMAND_MARKERS = (
    "uv run ",
    "python -m pytest",
    "pytest ",
    "ruff check ",
    "ruff format ",
    "mypy ",
    "npm ",
)
_PYTEST_NODE_END_DELIMITERS = {",", ";", ")", "`", "'", '"'}
_PYTEST_NODE_PREFIX_DELIMITERS = "([{<"
_PYTEST_NODE_SUFFIX_DELIMITERS = ")]}>"
_PYTEST_NODE_QUOTE_DELIMITERS = {"`", "'", '"'}
_MAX_TEST_NODES = 20
_MAX_REPRO_NODES = 5
_MAX_COMMANDS = 5
_MAX_SNIPPETS = 8
_MAX_SUMMARIES = 8
_MAX_LINE_CHARS = 500
_DEFAULT_PYTEST_REPRO_COMMAND = "uv run --python 3.12 --extra dev pytest"

CI_CODE_FAILURE_MARKERS = (
    "failed test",
    "pytest failed",
    "assertionerror",
    "assert failed",
    "coverage failure",
    "fail-under",
    "typecheck",
    "type check",
    "would reformat:",
    "found lint errors",
    "found type errors",
    "syntaxerror",
    "traceback (most recent call last)",
)


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
    """Shell token plus the source index where that token ends."""

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
    """Strip ANSI control sequences, carriage returns, and edge whitespace."""
    return _ANSI_RE.sub("", line).replace("\r", "").strip()


def _truncate_line(line: str) -> str:
    """Limit a display line to the prompt-safe evidence length."""
    return line[:_MAX_LINE_CHARS]


def _extract_test_nodes(lines: Iterable[str]) -> list[str]:
    """Collect pytest node id candidates from non-empty CI log lines."""
    nodes: list[str] = []
    for line in lines:
        nodes.extend(_pytest_node_candidates(line))
    return nodes


def _pytest_node_candidates(line: str) -> list[str]:
    """Extract pytest node ids with a linear scan.

    CI logs are untrusted text. A previous nested-regex extractor could spend
    unbounded CPU on long GitHub Actions lines before the PR monitor even logged
    its decision. This scanner only looks around ``.py::`` anchors and walks
    each line forward once.
    """
    if ".py::" not in line:
        return []

    nodes: list[str] = []
    search_from = 0
    while True:
        anchor = line.find(".py::", search_from)
        if anchor < 0:
            break

        start = anchor
        while start > 0 and _is_pytest_path_char(line[start - 1]):
            start -= 1

        end = anchor + len(".py::")
        bracket_depth = 0
        while end < len(line):
            char = line[end]
            if char == "[":
                bracket_depth += 1
            elif char == "]" and bracket_depth > 0:
                bracket_depth -= 1
            elif bracket_depth == 0 and (char.isspace() or char in _PYTEST_NODE_END_DELIMITERS):
                break
            end += 1

        candidate = _strip_node_suffix(line[start:end].strip("`'\""))
        if (
            bracket_depth == 0
            and _has_pytest_node_boundary(line, start, end)
            and _looks_like_pytest_node(candidate)
        ):
            nodes.append(candidate)
        search_from = max(end, anchor + len(".py::"))

    return nodes


def _has_pytest_node_boundary(line: str, start: int, end: int) -> bool:
    """Return whether the candidate node id is delimited by log text."""
    if start > 0:
        char = line[start - 1]
        if char in _PYTEST_NODE_PREFIX_DELIMITERS:
            return False
        if not char.isspace() and char not in _PYTEST_NODE_QUOTE_DELIMITERS:
            return False
    if end >= len(line):
        return True
    char = line[end]
    if char in _PYTEST_NODE_SUFFIX_DELIMITERS:
        return False
    if char in _PYTEST_NODE_END_DELIMITERS:
        return True
    if char.isspace():
        rest = line[end:].lstrip()
        return not rest or rest.startswith("- ")
    raise AssertionError(f"unsupported pytest node boundary: {char!r}")


def _is_pytest_path_char(char: str) -> bool:
    """Return whether a character can be part of a pytest file path."""
    return char.isalnum() or char in "/._-"


def _looks_like_pytest_node(candidate: str) -> bool:
    """Return whether a scanned candidate has pytest node-id shape."""
    path, separator, test_part = candidate.partition(".py::")
    if not separator or not path or not test_part:
        return False
    if path and path[0] in _PYTEST_NODE_PREFIX_DELIMITERS:
        return False
    if any(char.isspace() for char in path):
        return False
    return "://" not in path


def _strip_node_suffix(node: str) -> str:
    """Remove trailing punctuation commonly attached to failed node ids."""
    return node.rstrip(",:;)")


def _extract_commands(lines: Iterable[str]) -> list[str]:
    """Extract supported failing commands from GitHub run-step log lines."""
    commands: list[str] = []
    for line in lines:
        command = _extract_command_from_line(line)
        if command:
            commands.append(command)
    return commands


def _extract_command_from_line(line: str) -> str | None:
    """Return the supported command segment from one run-step log line."""
    if not _is_github_run_step_line(line):
        return None
    for marker in _COMMAND_MARKERS:
        index = line.find(marker)
        if index >= 0:
            return line[index:].strip()
    return None


def _is_github_run_step_line(line: str) -> bool:
    """Return whether a log line has GitHub Actions run-step structure."""
    parts = [part.strip() for part in line.split("\t") if part.strip()]
    return len(parts) >= 3 and any(part.startswith("Run ") for part in parts[:-1])


def _extract_assertion_snippets(lines: Iterable[str]) -> list[str]:
    """Collect assertion-oriented lines that help repair pytest failures."""
    snippets: list[str] = []
    for line in lines:
        if "AssertionError" in line or line.startswith(("E   ", ">   ")):
            snippets.append(line)
    return snippets


def _extract_error_summaries(lines: Iterable[str]) -> list[str]:
    """Collect concise error summary lines from displayed CI log output."""
    marker_summaries: list[str] = []
    generic_summaries: list[str] = []
    for line in lines:
        message = _github_log_message_segment(line)
        if _line_has_code_failure_marker(message):
            marker_summaries.append(line)
        elif (
            message.startswith("FAILED ")
            or "AssertionError" in message
            or message.startswith("Error:")
            or "::error" in message
            or "Process completed with exit code" in message
            or _PYTHON_DIAGNOSTIC_RE.search(message)
            or _RUFF_DIAGNOSTIC_RE.search(message)
            or message.lower().startswith(("error ", "error:", "fatal:"))
        ):
            generic_summaries.append(line)
    return [*marker_summaries, *generic_summaries]


def _github_log_message_segment(line: str) -> str:
    """Return the message after GitHub's job and step log prefixes."""
    parts = line.split("\t", 2)
    if len(parts) == 3 and parts[0].strip() and parts[1].strip():
        return parts[2].strip()
    return line


def _line_has_code_failure_marker(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in CI_CODE_FAILURE_MARKERS)


def _suggest_repro_commands(
    *,
    test_node_ids: list[str],
    failing_commands: list[str],
    pytest_fallback_commands: Iterable[str] = (),
) -> list[str]:
    """Build focused pytest reproduction commands from extracted node ids."""
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
    """Find a pytest command prefix suitable for appending node ids."""
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
    """Tokenize enough shell syntax to locate pytest without executing it."""
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
    """Normalize whitespace while preserving first occurrence order."""
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
    """Deduplicate stripped values without collapsing internal whitespace."""
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        deduped.append(cleaned)
    return deduped
