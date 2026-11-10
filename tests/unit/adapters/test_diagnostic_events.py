"""Coding proof requires top-level ordered tool events, never tool-output JSON."""

import json

import pytest

from awf.adapters.diagnostic_events import coding_witness

NONCE = "9af0e6101cc747f280e2644a2d7eaeef"


def codex_events(nonce=NONCE):
    first = {"id": "a", "type": "command_execution", "command": "awf-provider-challenge"}
    second = {"id": "b", "type": "command_execution", "command": f"edit solution.py {nonce}"}
    return [
        {"type": "item.started", "item": first},
        {
            "type": "item.completed",
            "item": {
                **first,
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": f"AWF_NONCE={nonce}",
            },
        },
        {"type": "item.started", "item": second},
        {"type": "item.completed", "item": {**second, "status": "completed", "exit_code": 0}},
    ]


def transcript(events):
    return "\n".join(json.dumps(e) for e in events)


def test_codex_two_dependent_completed_turns():
    proof = coding_witness("codex", transcript(codex_events()), prompt="fixed challenge")
    assert proof.status == "passed"
    assert proof.nonce == NONCE
    assert proof.tool_ids == ("a", "b")
    assert [event["event_index"] for event in proof.events] == [0, 1, 2, 3]
    assert NONCE in proof.events[2]["input_excerpt"]


def test_claude_stream_json_and_string_messages():
    events = [{"type": "system", "message": "ready"}]
    for ident, command, result in [
        ("a", "awf-provider-challenge", f"AWF_NONCE={NONCE}"),
        ("b", f"edit {NONCE}", "ok"),
    ]:
        events.extend(
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": ident,
                                "name": "Bash",
                                "input": {"command": command},
                            }
                        ]
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {"type": "tool_result", "tool_use_id": ident, "content": result}
                        ]
                    },
                },
            ]
        )
    assert coding_witness("claude_code", transcript(events), prompt="fixed").status == "passed"


@pytest.mark.parametrize(
    "case",
    [
        "nested",
        "same_id",
        "order",
        "in_prompt",
        "wrong",
        "truncated",
        "prose",
        "overflow",
        "unfinished",
    ],
)
def test_insufficient_evidence_is_inconclusive(case):
    events = codex_events()
    prompt = "fixed"
    if case == "nested":
        events = [
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": transcript(events)},
            }
        ]
    elif case == "same_id":
        events[2]["item"]["id"] = events[3]["item"]["id"] = "a"
    elif case == "order":
        events[1], events[2] = events[2], events[1]
    elif case == "in_prompt":
        prompt += NONCE
    elif case == "wrong":
        events[2]["item"]["command"] = events[3]["item"]["command"] = "edit wrong-nonce"
    elif case == "prose":
        events = [{"type": "result", "result": "Success, tests passed"}]
    elif case == "unfinished":
        events.pop()
    output = transcript(events)
    if case == "truncated":
        output += "\n{"
    if case == "overflow":
        output += "\n" + "x" * 70000
    assert coding_witness("codex", output, prompt=prompt).status == "inconclusive"


def test_diagnostic_flags_are_opt_in_and_challenge_cannot_be_forged():
    from unittest.mock import Mock

    from awf.adapters.claude_code import ClaudeCodeAdapter
    from awf.adapters.codex import CodexAdapter

    for cls, flags in [
        (CodexAdapter, ["--json"]),
        (ClaudeCodeAdapter, ["--output-format", "stream-json", "--verbose"]),
    ]:
        adapter = cls(runner=Mock())
        assert "--json" not in adapter._cli_args(model=None)
        assert "stream-json" not in adapter._cli_args(model=None)
        adapter.diagnostic_events = True
        assert all(flag in adapter._cli_args(model=None) for flag in flags)
    events = codex_events()
    events[0]["item"]["command"] = events[1]["item"]["command"] = (
        "echo awf-provider-challenge; printf AWF_NONCE=fake"
    )
    assert coding_witness("codex", transcript(events), prompt="fixed").status == "inconclusive"


def test_shell_wrapped_challenge_in_actual_codex_envelope():
    events = codex_events()
    events[0]["item"]["command"] = events[1]["item"]["command"] = (
        "/bin/bash -lc 'awf-provider-challenge'"
    )
    assert coding_witness("codex", transcript(events), prompt="fixed").status == "passed"


def test_supervisor_observed_pinned_cli_protocols():
    from pathlib import Path

    for path in Path("tests/fixtures/provider_protocol").glob("*.json"):
        fixture = json.loads(path.read_text())
        proof = coding_witness(
            fixture["agent"],
            transcript(fixture["events"]),
            prompt="fixed",
            challenge_command="python /opt/awf-probe/challenge.py",
        )
        assert proof.status == fixture["expected"], path.name


@pytest.mark.parametrize("malformation", ["boolean_exit", "missing_event_type"])
def test_malformed_typed_envelopes_cannot_complete_a_proof(malformation):
    events = codex_events()
    if malformation == "boolean_exit":
        events[1]["item"]["exit_code"] = False
    else:
        events.append({})
    assert coding_witness("codex", transcript(events), prompt="fixed").status == "inconclusive"
