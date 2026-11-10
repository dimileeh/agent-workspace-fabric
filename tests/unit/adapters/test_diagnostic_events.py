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
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 10}},
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


def claude_events():
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
    events.append({"type": "result", "subtype": "success", "is_error": False})
    return events


def test_claude_stream_json_and_string_messages():
    assert (
        coding_witness("claude_code", transcript(claude_events()), prompt="fixed").status
        == "passed"
    )


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
        events.pop(-2)
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
        # Captures contain tool excerpts only, not terminal CLI envelopes.
        assert (
            coding_witness(
                fixture["agent"],
                transcript(fixture["events"]),
                prompt="fixed",
                challenge_command="python /opt/awf-probe/challenge.py",
            ).status
            == "inconclusive"
        )
        terminal = codex_events()[-1] if fixture["agent"] == "codex" else claude_events()[-1]
        # Explicitly synthetic completion tests the captured tool shapes without
        # inventing terminal evidence in the supervisor's original fixtures.
        proof = coding_witness(
            fixture["agent"],
            transcript([*fixture["events"], terminal]),
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


@pytest.mark.parametrize(
    "agent,make_events", [("codex", codex_events), ("claude_code", claude_events)]
)
def test_every_truncated_prefix_is_inconclusive(agent, make_events):
    events = make_events()
    for end in range(len(events)):
        assert (
            coding_witness(agent, transcript(events[:end]), prompt="fixed").status == "inconclusive"
        )


@pytest.mark.parametrize(
    "agent,make_events", [("codex", codex_events), ("claude_code", claude_events)]
)
@pytest.mark.parametrize("case", ["early", "duplicate", "after_terminal", "unfinished", "failure"])
def test_terminal_must_succeed_once_after_all_tools(agent, make_events, case):
    events = make_events()
    if case == "early":
        events.insert(0, events.pop())
    elif case == "duplicate":
        events.append(events[-1])
    elif case == "after_terminal":
        events.append({"type": "assistant", "message": {"content": []}})
    elif case == "unfinished":
        events.pop(-2)
    else:
        failure = {"type": "turn.failed", "error": {"message": "failed"}}
        if agent == "claude_code":
            failure = {"type": "result", "subtype": "error_during_execution", "is_error": True}
        events.insert(-1, failure)
    assert coding_witness(agent, transcript(events), prompt="fixed").status == "inconclusive"


@pytest.mark.parametrize(
    "terminal",
    [
        {"type": "result", "subtype": "error_during_execution", "is_error": False},
        {"type": "result", "subtype": "success", "is_error": True},
        {"type": "result", "subtype": "success"},
        {"type": "result", "subtype": "success", "is_error": 0},
    ],
)
def test_claude_requires_typed_success_envelope(terminal):
    events = claude_events()
    events[-1] = terminal
    assert (
        coding_witness("claude_code", transcript(events), prompt="fixed").status == "inconclusive"
    )


def test_codex_error_before_success_is_inconclusive():
    events = codex_events()
    events.insert(-1, {"type": "error", "message": "stream failed"})
    assert coding_witness("codex", transcript(events), prompt="fixed").status == "inconclusive"


@pytest.mark.parametrize("limit", ["MAX_WITNESS_BYTES", "MAX_LINE_BYTES", "MAX_EVENTS"])
def test_protocol_limits_accept_the_boundary_and_reject_overflow(monkeypatch, limit):
    import awf.adapters.diagnostic_events as module

    output = transcript(codex_events())
    bounds = {
        "MAX_WITNESS_BYTES": len(output.encode()),
        "MAX_LINE_BYTES": max(len(line.encode()) for line in output.splitlines()),
        "MAX_EVENTS": len(output.splitlines()),
    }
    monkeypatch.setattr(module, limit, bounds[limit])
    assert coding_witness("codex", output, prompt="fixed").status == "passed"
    monkeypatch.setattr(module, limit, bounds[limit] - 1)
    assert coding_witness("codex", output, prompt="fixed").status == "inconclusive"


@pytest.mark.parametrize(
    "case",
    [
        "top_level_array",
        "empty_type",
        "bad_id",
        "bad_text",
        "duplicate_start",
        "duplicate_result",
        "orphan_result",
        "tool_failed",
        "repeated_challenge",
        "bad_shell",
    ],
)
def test_malformed_tool_protocol_cannot_establish_a_witness(case):
    events = codex_events()
    if case == "top_level_array":
        events.insert(0, [])
    elif case == "empty_type":
        events.insert(0, {"type": ""})
    elif case == "bad_id":
        events[0]["item"]["id"] = None
    elif case == "bad_text":
        events[1]["item"]["aggregated_output"] = []
    elif case == "duplicate_start":
        events.insert(1, events[0])
    elif case == "duplicate_result":
        events.insert(2, events[1])
    elif case == "orphan_result":
        events.pop(0)
    elif case == "tool_failed":
        events[3]["item"]["exit_code"] = 1
    elif case == "repeated_challenge":
        repeated = codex_events()[:2]
        for event in repeated:
            event["item"]["id"] = "another-challenge"
        events[-1:-1] = repeated
    else:
        events[0]["item"]["command"] = "'unterminated"
    assert coding_witness("codex", transcript(events), prompt="fixed").status == "inconclusive"


def test_claude_structured_results_allow_text_but_reject_malformed_content():
    events = claude_events()
    events[2]["message"]["content"][0]["content"] = [
        {"type": "text", "text": f"AWF_NONCE={NONCE}"},
        {"type": "image"},
        "ignored non-block",
    ]
    output = "\n\n" + transcript(events) + "\n\n"
    assert coding_witness("claude_code", output, prompt="fixed").status == "passed"
    events[1]["message"]["content"].insert(0, "ignored non-block")
    assert coding_witness("claude_code", transcript(events), prompt="fixed").status == "passed"
    events[1]["message"]["content"] = "not a block list"
    assert (
        coding_witness("claude_code", transcript(events), prompt="fixed").status == "inconclusive"
    )


@pytest.mark.parametrize("agent", ["codex", "claude_code", "unsupported-cli"])
def test_unrecognized_envelopes_cannot_supply_tool_evidence(agent):
    events = claude_events() if agent == "claude_code" else codex_events()
    if agent == "codex":
        events[0]["type"] = "item.updated"
    elif agent == "claude_code":
        events[1]["message"]["content"][0]["type"] = "text"
    assert coding_witness(agent, transcript(events), prompt="fixed").status == "inconclusive"
