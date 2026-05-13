"""Extract concise repair evidence from GitHub Actions failed-step logs."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from awf.common.redaction import redact_secrets

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_PYTEST_NODE_RE = re.compile(r"\b(?P<node>(?:tests|src)/[^\s:]+\.py(?:::[^\s]+)+)")
_FAILED_PYTEST_NODE_RE = re.compile(r"\bFAILED\s+(?P<node>(?:tests|src)/[^\s:]+\.py(?:::[^\s]+)+)")
_RUFF_DIAGNOSTIC_RE = re.compile(r"\b(?:src|tests)/[^\s:]+\.py:\d+:\d+:")
_COMMAND_MARKERS = (
    "uv run ",
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


@dataclass(frozen=True)
class CiFailureEvidence:
    """Structured repair hints derived from one failed CI check log."""

    failing_commands: tuple[str, ...] = ()
    test_node_ids: tuple[str, ...] = ()
    assertion_snippets: tuple[str, ...] = ()
    error_summaries: tuple[str, ...] = ()
    suggested_repro_commands: tuple[str, ...] = ()
    evidence_warnings: tuple[str, ...] = ()


def extract_ci_failure_evidence(
    log_text: str,
    *,
    check_name: str,
) -> CiFailureEvidence:
    """Return focused, redacted repair evidence from a failed CI log."""

    safe_log = redact_secrets(log_text or "")
    if not safe_log.strip():
        return CiFailureEvidence(
            evidence_warnings=(f"GitHub Actions log unavailable for failed check {check_name}.",)
        )

    lines = tuple(_clean_line(line) for line in safe_log.splitlines())
    non_empty_lines = tuple(line for line in lines if line)
    test_nodes = _dedupe(_extract_test_nodes(non_empty_lines))[:_MAX_TEST_NODES]
    failing_commands = _dedupe(_extract_commands(non_empty_lines))[:_MAX_COMMANDS]
    assertion_snippets = _dedupe(_extract_assertion_snippets(non_empty_lines))[:_MAX_SNIPPETS]
    error_summaries = _dedupe(_extract_error_summaries(non_empty_lines))[:_MAX_SUMMARIES]
    suggested_repro_commands = _suggest_repro_commands(
        test_node_ids=test_nodes,
        failing_commands=failing_commands,
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
    cleaned = _ANSI_RE.sub("", line).replace("\r", "").strip()
    return cleaned[:_MAX_LINE_CHARS]


def _extract_test_nodes(lines: Iterable[str]) -> list[str]:
    nodes: list[str] = []
    for line in lines:
        failed_match = _FAILED_PYTEST_NODE_RE.search(line)
        if failed_match:
            nodes.append(_strip_node_suffix(failed_match.group("node")))
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
    for marker in _COMMAND_MARKERS:
        index = line.find(marker)
        if index >= 0:
            return line[index:].strip()
    return None


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
) -> list[str]:
    if test_node_ids:
        selected = test_node_ids[:_MAX_REPRO_NODES]
        return ["uv run --python 3.12 --extra dev pytest " + " ".join(selected) + " -q"]
    return failing_commands[:3]


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
