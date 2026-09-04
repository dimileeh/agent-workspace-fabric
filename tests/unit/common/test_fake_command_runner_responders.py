"""``FakeCommandRunner.respond_when`` sticky responders."""

from __future__ import annotations

import pytest

from awf.common.commands import FakeCommandRunner


def _is_probe(args: list[str]) -> bool:
    return "ls-files" in args


@pytest.mark.unit
async def test_respond_when_answers_matching_commands_without_consuming_queue() -> None:
    fake = FakeCommandRunner()
    fake.respond_when(_is_probe, returncode=1, stdout="probe out", stderr="probe err")
    fake.queue_result(returncode=0, stdout="queued")

    probe = await fake.run(["git", "ls-files", "-s", "-z"])
    queued = await fake.run(["git", "status"])

    assert (probe.returncode, probe.stdout, probe.stderr) == (1, "probe out", "probe err")
    assert queued.stdout == "queued"
    assert [call.args for call in fake.calls] == [
        ["git", "ls-files", "-s", "-z"],
        ["git", "status"],
    ]


@pytest.mark.unit
async def test_respond_when_first_matching_responder_wins_and_records_call() -> None:
    fake = FakeCommandRunner()
    fake.respond_when(_is_probe, stdout="first", reason_code="FIRST")
    fake.respond_when(lambda _args: True, stdout="second")

    result = await fake.run(["git", "ls-files"], timeout_seconds=1.5, env={"A": "b"})
    other = await fake.run(["git", "status"])

    assert (result.stdout, result.reason_code) == ("first", "FIRST")
    assert other.stdout == "second"
    assert fake.calls[0].timeout_seconds == 1.5
    assert fake.calls[0].env == {"A": "b"}


@pytest.mark.unit
async def test_respond_when_still_validates_timeout_before_answering() -> None:
    fake = FakeCommandRunner()
    fake.respond_when(_is_probe, stdout="probe")

    with pytest.raises(ValueError, match="timeout_seconds"):
        await fake.run(["git", "ls-files"], timeout_seconds=0.0)
    assert fake.calls == []
