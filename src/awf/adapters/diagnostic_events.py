"""Bounded, top-level pinned CLI protocol witness for coding diagnostics."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any, Literal

MAX_WITNESS_BYTES = 512_000
MAX_LINE_BYTES = 65_536
MAX_EVENTS = 2000
_NONCE = re.compile(r"(?:^|\n)AWF_NONCE=([0-9a-f]{32})(?:\n|$)")


@dataclass(frozen=True)
class CodingWitness:
    status: Literal["passed", "inconclusive"] = "inconclusive"
    nonce: str | None = None
    tool_ids: tuple[str, ...] = ()
    reason_code: str = "PROVIDER_DIAGNOSTIC_EVIDENCE_MISSING"
    events: tuple[dict[str, Any], ...] = ()


def coding_witness(
    agent: str, output: str, *, prompt: str, challenge_command: str = "awf-provider-challenge"
) -> CodingWitness:
    """Require a fresh challenge result then a distinct completed dependent tool.

    Tool-output strings are never recursively decoded as event envelopes.
    Any malformed/truncated/oversized protocol fails closed, including tail loss.
    """
    if len(output.encode()) > MAX_WITNESS_BYTES:
        return CodingWitness()
    started: dict[str, tuple[int, str]] = {}
    completed: set[str] = set()
    challenge: tuple[str, str, int] | None = None
    proof: CodingWitness | None = None
    try:
        lines = output.splitlines()
        if len(lines) > MAX_EVENTS:
            return CodingWitness()
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            if len(line.encode()) > MAX_LINE_BYTES:
                return CodingWitness()
            event = json.loads(line)
            if (
                not isinstance(event, dict)
                or not isinstance(event.get("type"), str)
                or not event["type"]
            ):
                return CodingWitness()
            actions: list[tuple[str, Any, Any, Any]] = []
            if agent == "codex":
                item = event.get("item", {})
                if isinstance(item, dict) and item.get("type") == "command_execution":
                    kind = event.get("type")
                    if kind in {"item.started", "item.completed"}:
                        actions.append(
                            (
                                "start" if kind == "item.started" else "complete",
                                item.get("id"),
                                item.get("command", "")
                                if kind == "item.started"
                                else item.get("aggregated_output", ""),
                                kind == "item.started"
                                or (
                                    type(item.get("exit_code")) is int
                                    and item.get("exit_code") == 0
                                    and item.get("status") == "completed"
                                ),
                            )
                        )
            elif agent == "claude_code":
                message = event.get("message")
                content = message.get("content", []) if isinstance(message, dict) else []
                if not isinstance(content, list):
                    return CodingWitness()
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if event.get("type") == "assistant" and block.get("type") == "tool_use":
                        actions.append(
                            (
                                "start",
                                block.get("id"),
                                str(block.get("input", {}).get("command", ""))
                                if isinstance(block.get("input"), dict)
                                and block.get("name") == "Bash"
                                else json.dumps(block.get("input", {})),
                                True,
                            )
                        )
                    elif event.get("type") == "user" and block.get("type") == "tool_result":
                        result = block.get("content", "")
                        if isinstance(result, list):
                            result = "\n".join(
                                b.get("text", "")
                                for b in result
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        actions.append(
                            (
                                "complete",
                                block.get("tool_use_id"),
                                result,
                                not block.get("is_error", False),
                            )
                        )
            for kind, ident, text, ok in actions:
                if not isinstance(ident, str) or not ident or not isinstance(text, str):
                    return CodingWitness()
                if kind == "start":
                    if ident in started or ident in completed:
                        return CodingWitness()
                    started[ident] = (index, text)
                else:
                    if ident not in started or ident in completed:
                        return CodingWitness()
                    completed.add(ident)
                    start_index, command = started[ident]
                    if (
                        challenge
                        and ident != challenge[0]
                        and start_index > challenge[2]
                        and challenge[1] in command
                        and ok
                    ):
                        position = command.index(challenge[1])
                        proof = CodingWitness(
                            "passed",
                            challenge[1],
                            (challenge[0], ident),
                            "PROVIDER_DIAGNOSTIC_PROTOCOL_PASSED",
                            (
                                {
                                    "phase": "tool.started",
                                    "call_id": challenge[0],
                                    "event_index": started[challenge[0]][0],
                                    "input": challenge_command,
                                },
                                {
                                    "phase": "tool.completed",
                                    "call_id": challenge[0],
                                    "event_index": challenge[2],
                                    "nonce": challenge[1],
                                },
                                {
                                    "phase": "tool.started",
                                    "call_id": ident,
                                    "event_index": start_index,
                                    "input_excerpt": command[
                                        max(0, position - 128) : position + len(challenge[1]) + 128
                                    ],
                                },
                                {"phase": "tool.completed", "call_id": ident, "event_index": index},
                            ),
                        )
                    match = _NONCE.search(text)
                    if match and _challenge_command(command, challenge_command) and ok:
                        if challenge is not None or match[1] in prompt:
                            return CodingWitness()
                        challenge = (ident, match[1], index)
        return (proof or CodingWitness()) if started.keys() == completed else CodingWitness()
    except (ValueError, TypeError):
        return CodingWitness()


def _challenge_command(command: str, expected: str) -> bool:
    parts = shlex.split(command)
    if (
        len(parts) == 3
        and parts[0] in {"/bin/bash", "bash", "/bin/sh", "sh"}
        and parts[1] in {"-lc", "-c"}
    ):
        parts = shlex.split(parts[2])
    return parts == shlex.split(expected)
