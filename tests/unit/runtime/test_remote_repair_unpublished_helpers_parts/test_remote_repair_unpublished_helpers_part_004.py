"""Item-provenance chain coverage and legacy subject matching (part 004, #935)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError

from awf.common.commands import CommandResult
from awf.runtime.monitor_state_keys import _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_repair_provenance as _repair_provenance
from awf.runtime.pr_monitor_runner import remote_repair_unpublished_provenance as _provenance

_BASE = "a" * 40
_FIRST = "b" * 40
_SECOND = "c" * 40
_FOREIGN = "d" * 40


def _chain_state(records: list[dict[str, object]]) -> MonitorState:
    state = MonitorState()
    state.mark_addressed(
        _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY,
        json.dumps(records, separators=(",", ":"), sort_keys=True),
    )
    return state


def _record(item_id: str, start: str, head: str) -> dict[str, object]:
    return {
        "item_id": item_id,
        "item_start_head": start,
        "head_sha": head,
        "operation_id": "op_comment_repair",
    }


@pytest.mark.unit
def test_chain_covers_exact_two_item_range() -> None:
    state = _chain_state([_record("PRRT_one", _BASE, _FIRST), _record("PRRT_two", _FIRST, _SECOND)])

    assert (
        _provenance._item_provenance_chain_covers_range(
            state,
            base_head=_BASE.upper(),
            head_sha=_SECOND,
        )
        is True
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("records", "base", "head"),
    [
        pytest.param(
            [_record("PRRT_one", _BASE, _FIRST), _record("PRRT_two", _FOREIGN, _SECOND)],
            _BASE,
            _SECOND,
            id="broken_link",
        ),
        pytest.param(
            [_record("PRRT_one", _FOREIGN, _FIRST)],
            _BASE,
            _FIRST,
            id="wrong_base",
        ),
        pytest.param(
            [_record("PRRT_one", _BASE, _FIRST)],
            _BASE,
            _SECOND,
            id="wrong_tip",
        ),
        pytest.param([], _BASE, _FIRST, id="empty_chain"),
    ],
)
def test_chain_coverage_fails_closed(
    records: list[dict[str, object]],
    base: str,
    head: str,
) -> None:
    state = _chain_state(records)

    assert (
        _provenance._item_provenance_chain_covers_range(state, base_head=base, head_sha=head)
        is False
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    ["{not-json", json.dumps({"item_id": "x"}), json.dumps([{"item_id": "x"}]), "", "   "],
)
def test_chain_coverage_rejects_malformed_marker(raw: str) -> None:
    state = MonitorState()
    state.mark_addressed(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY, raw)

    assert (
        _provenance._item_provenance_chain_covers_range(state, base_head=_BASE, head_sha=_FIRST)
        is False
    )


@pytest.mark.unit
def test_chain_coverage_absent_marker_is_false() -> None:
    assert (
        _provenance._item_provenance_chain_covers_range(
            MonitorState(),
            base_head=_BASE,
            head_sha=_FIRST,
        )
        is False
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "subject",
    [
        "fix: address PR review thread PRRT_kwDOSJAM6s6fjOze",
        "fix: address PR review comment issue:4688598838",
        "fix: address review comment issue:4688598838 — tighten the guard",
        "fix: address PRRT_kwDOSJAM6s6fjOze — tighten the guard",
        "fix: address PR review comment 4688598838",
        "fix: address review comment 4688598838 — tighten the guard",
    ],
)
def test_review_item_commit_subject_matches_awf_shapes(subject: str) -> None:
    assert _provenance._is_review_item_commit_subject(subject) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "subject",
    [
        # The four identifier shapes ``bitbucket_client_parsing.py`` emits.
        "fix: address PR review thread bb:acme/widgets#42:557058",
        "fix: address PR review thread bbtask:acme/widgets#42:99",
        "fix: address PR review comment bbcomment:557058",
        "fix: address bbreview:557058:c7f1 — tighten the guard",
        "fix: address review comment bbreview:Ada Lovelace — tighten the guard",
    ],
)
def test_review_item_commit_subject_matches_bitbucket_shapes(subject: str) -> None:
    assert _provenance._is_review_item_commit_subject(subject) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "subject",
    [
        "fix: address operator hint",
        "fix: address PR #922 CI failure",
        "chore: unrelated local work",
        "fix: address the reviewer feedback",
        # A bare databaseId only counts behind the ``review thread|comment`` words:
        # AWF never emits one unprefixed, and matching it there would preserve (and
        # push) ordinary local commits that merely start with an HTTP status code.
        "fix: address 404 errors",
        "fix: address 500 in the parser",
        "fix: address 4688598838 — tighten the guard",
        "",
    ],
)
def test_review_item_commit_subject_rejects_unrelated_subjects(subject: str) -> None:
    assert _provenance._is_review_item_commit_subject(subject) is False


@pytest.mark.unit
def test_park_reason_names_short_shas_and_subjects() -> None:
    reason = _provenance._unpublished_repair_park_reason(
        (
            ("3195fc8", "fix: address PRRT_kwDOSJAM6s6fjOze — guard"),
            ("aa194c9", "test: cover the guard"),
        )
    )

    assert "3195fc8" in reason
    assert "aa194c9" in reason
    assert "fix: address PRRT_kwDOSJAM6s6fjOze — guard" in reason


@pytest.mark.unit
def test_park_reason_without_commit_log_still_names_the_range() -> None:
    reason = _provenance._unpublished_repair_park_reason(())

    assert "unpushed" in reason.lower()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        pytest.param("", (), id="empty"),
        pytest.param(
            "3195fc8 fix: address PRRT_one — guard\naa194c9 test: cover\n",
            (
                ("3195fc8", "fix: address PRRT_one — guard"),
                ("aa194c9", "test: cover"),
            ),
            id="two_commits",
        ),
        pytest.param("deadbee\n", (("deadbee", ""),), id="subjectless"),
        pytest.param("\n  \nabc1234 fix: x\n", (("abc1234", "fix: x"),), id="blank_lines"),
    ],
)
def test_parse_commit_log_entries(stdout: str, expected: tuple[tuple[str, str], ...]) -> None:
    assert _provenance._parse_commit_log_entries(stdout) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(json.dumps(["not-a-mapping"]), id="non_mapping_entry"),
        pytest.param(
            json.dumps([{"item_id": 7, "item_start_head": _BASE, "head_sha": _FIRST}]),
            id="non_string_item_id",
        ),
        pytest.param(
            json.dumps([{"item_id": "  ", "item_start_head": _BASE, "head_sha": _FIRST}]),
            id="blank_item_id",
        ),
    ],
)
def test_decode_chain_rejects_malformed_records(raw: str) -> None:
    assert _repair_provenance.decode_item_commit_provenance_chain(raw) == ()


@pytest.mark.unit
def test_decode_chain_normalises_a_non_string_operation_id() -> None:
    chain = _repair_provenance.decode_item_commit_provenance_chain(
        json.dumps(
            [
                {
                    "item_id": "PRRT_one",
                    "item_start_head": f" {_BASE} ",
                    "head_sha": _FIRST,
                    "operation_id": 12,
                }
            ]
        )
    )

    assert len(chain) == 1
    assert chain[0].operation_id is None
    assert chain[0].item_start_head == _BASE


@pytest.mark.unit
def test_chain_coverage_accepts_the_suffix_after_a_published_base() -> None:
    """#937: a chain that outlived a push is rooted behind the current PR head.

    The first record's commit is already on the remote, so only the records from
    the fetched head onwards describe ``remote..HEAD`` — and they must be accepted
    instead of defeating the chain path and falling back to subject matching.
    """
    state = _chain_state([_record("PRRT_one", _BASE, _FIRST), _record("PRRT_two", _FIRST, _SECOND)])

    assert (
        _provenance._item_provenance_chain_covers_range(
            state,
            base_head=_FIRST,
            head_sha=_SECOND,
        )
        is True
    )


@pytest.mark.unit
def test_chain_coverage_rejects_a_suffix_that_stops_short_of_head() -> None:
    """The suffix must still end at the current HEAD, not merely start at the base."""
    state = _chain_state([_record("PRRT_one", _BASE, _FIRST), _record("PRRT_two", _FIRST, _SECOND)])

    assert (
        _provenance._item_provenance_chain_covers_range(
            state,
            base_head=_FIRST,
            head_sha=_FOREIGN,
        )
        is False
    )


@pytest.mark.unit
def test_chain_coverage_rejects_a_broken_link_inside_the_suffix() -> None:
    state = _chain_state(
        [
            _record("PRRT_one", _BASE, _FIRST),
            _record("PRRT_two", _FIRST, _SECOND),
            _record("PRRT_three", _FOREIGN, _BASE),
        ]
    )

    assert (
        _provenance._item_provenance_chain_covers_range(
            state,
            base_head=_FIRST,
            head_sha=_BASE,
        )
        is False
    )


@pytest.mark.unit
@pytest.mark.parametrize(("base", "head"), [("", _FIRST), (_BASE, "")])
def test_chain_coverage_rejects_blank_endpoints(base: str, head: str) -> None:
    state = _chain_state([_record("PRRT_one", _BASE, _FIRST)])

    assert (
        _provenance._item_provenance_chain_covers_range(state, base_head=base, head_sha=head)
        is False
    )


@pytest.mark.unit
def test_park_reason_truncates_a_long_commit_list() -> None:
    entries = tuple((f"sha{index:04d}", f"chore: change {index}") for index in range(13))

    reason = _provenance._unpublished_repair_park_reason(entries)

    assert "(+3 more)" in reason
    assert "sha0012" not in reason


@pytest.mark.unit
async def test_disposition_event_without_an_event_sink_is_a_no_op() -> None:
    await _provenance._append_disposition_event(
        SimpleNamespace(),
        workspace_id="ws_hosted",
        event_type="monitor.comment_repair_unpublished_parked",
        reason_code="COMMENT_REPAIR_UNPUBLISHED_PROVENANCE_MISSING",
        payload={"pushed": False},
    )


class _FailingEventSinkRunner:
    """Runner whose audit-event sink fails; ``git log`` still answers."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.append_calls = 0
        self._deps = SimpleNamespace(runner=SimpleNamespace(run=self._run))

    async def _run(self, _args: list[str], **_kwargs: object) -> CommandResult:
        return CommandResult(
            returncode=0,
            stdout="deadbee chore: unrelated local work\n",
            stderr="",
        )

    async def _append_workspace_events(self, **_kwargs: object) -> None:
        self.append_calls += 1
        raise self._error


_SINK_ERRORS = [
    pytest.param(SQLAlchemyError("db down"), id="sqlalchemy_error"),
    pytest.param(OSError("socket closed"), id="os_error"),
]


@pytest.mark.unit
@pytest.mark.parametrize("error", _SINK_ERRORS)
async def test_disposition_event_survives_a_failing_event_sink(error: Exception) -> None:
    runner = _FailingEventSinkRunner(error)

    await _provenance._append_disposition_event(
        runner,
        workspace_id="ws_sink",
        event_type="monitor.comment_repair_unpublished_parked",
        reason_code="COMMENT_REPAIR_UNPUBLISHED_PROVENANCE_MISSING",
        payload={"pushed": False},
    )

    assert runner.append_calls == 1


@pytest.mark.unit
@pytest.mark.parametrize("error", _SINK_ERRORS)
async def test_park_disposition_survives_a_failing_event_sink(error: Exception) -> None:
    runner = _FailingEventSinkRunner(error)

    disposition = await _provenance._resolve_unpublished_comment_repair_disposition(
        runner,
        workspace_id="ws_park",
        worktree_path=Path("/tmp/ws_park"),
        state=MonitorState(),
        current_head=_SECOND,
        fetched_head=_BASE,
        provenance_remote_head=_BASE,
        diff_range=f"{_BASE}..HEAD",
        use_stale_snapshot_diff=False,
        has_comment_repair_provenance=False,
        has_conflicting_repair_provenance=True,
        current_operation_id=None,
    )

    assert disposition is not None
    head, push_result = disposition
    assert head == _SECOND
    assert push_result is not None
    assert push_result.parked_needs_human is True
    assert push_result.details["disposition"] == "conflicting_repair_provenance"


class _FlakyEventSinkRunner:
    """Runner whose audit-event sink fails the first ``failures`` writes."""

    def __init__(self, *, failures: int) -> None:
        self._failures = failures
        self.appended: list[str] = []
        self._deps = SimpleNamespace(runner=SimpleNamespace(run=self._run))

    async def _run(self, _args: list[str], **_kwargs: object) -> CommandResult:
        return CommandResult(
            returncode=0,
            stdout="deadbee chore: unrelated local work\n",
            stderr="",
        )

    async def _append_workspace_events(self, *, workspace_id: str, events: list[object]) -> None:
        del workspace_id
        if self._failures > 0:
            self._failures -= 1
            raise SQLAlchemyError("event sink unavailable")
        self.appended.extend(getattr(event, "event_type", "") for event in events)


@pytest.mark.unit
async def test_park_retries_the_audit_event_until_the_sink_accepts_it() -> None:
    """A swallowed event write must not be deduped away by the park marker.

    The marker is persisted by ``_finish_parked_comment_repair_cycle`` whatever
    happens, so marking before the event is durable would make every later poll
    see ``already_parked`` and drop the operator-facing park event permanently
    (PRRT_kwDOSJAM6s6fu_-m).
    """
    runner = _FlakyEventSinkRunner(failures=1)
    state = MonitorState()

    async def _poll() -> tuple[str, object] | None:
        return await _provenance._resolve_unpublished_comment_repair_disposition(
            runner,
            workspace_id="ws_park_retry",
            worktree_path=Path("/tmp/ws_park_retry"),
            state=state,
            current_head=_SECOND,
            fetched_head=_BASE,
            provenance_remote_head=_BASE,
            diff_range=f"{_BASE}..HEAD",
            use_stale_snapshot_diff=False,
            has_comment_repair_provenance=False,
            has_conflicting_repair_provenance=True,
            current_operation_id=None,
        )

    first = await _poll()
    assert first is not None
    assert first[1] is not None
    assert first[1].parked_needs_human is True
    assert runner.appended == []
    # No durable event yet, so no marker: the next poll must retry the audit.
    assert state.parked_unpublished_repair is None

    second = await _poll()
    assert second is not None
    assert second[1] is not None
    assert second[1].parked_needs_human is True
    assert runner.appended == ["monitor.comment_repair_unpublished_parked"]
    assert state.parked_unpublished_repair is not None

    third = await _poll()
    assert third is not None
    assert third[1] is not None
    assert third[1].parked_needs_human is True
    # Durable now — the episode stays audited exactly once.
    assert runner.appended == ["monitor.comment_repair_unpublished_parked"]


@pytest.mark.unit
@pytest.mark.parametrize("error", _SINK_ERRORS)
async def test_preserve_disposition_survives_a_failing_event_sink(error: Exception) -> None:
    runner = _FailingEventSinkRunner(error)
    state = _chain_state([_record("PRRT_one", _BASE, _FIRST), _record("PRRT_two", _FIRST, _SECOND)])

    disposition = await _provenance._resolve_unpublished_comment_repair_disposition(
        runner,
        workspace_id="ws_preserve",
        worktree_path=Path("/tmp/ws_preserve"),
        state=state,
        current_head=_SECOND,
        fetched_head=_BASE,
        provenance_remote_head=_BASE,
        diff_range=f"{_BASE}..HEAD",
        use_stale_snapshot_diff=False,
        has_comment_repair_provenance=False,
        has_conflicting_repair_provenance=False,
        current_operation_id=None,
    )

    assert disposition == (_SECOND, None)


_SECRET_SUBJECT = "chore: wire token=ghp_abcdefghijklmnopqrstuvwxyz012345"


@pytest.mark.unit
def test_park_reason_redacts_secrets_in_commit_subjects() -> None:
    reason = _provenance._unpublished_repair_park_reason((("deadbee", _SECRET_SUBJECT),))

    assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in reason
    assert "[redacted]" in reason
    assert "deadbee" in reason


class _CapturingEventSinkRunner:
    """Runner whose ``git log`` returns a secret-bearing subject; records events."""

    def __init__(self, stdout: str) -> None:
        self._stdout = stdout
        self.payloads: list[dict[str, object]] = []
        self._deps = SimpleNamespace(runner=SimpleNamespace(run=self._run))

    async def _run(self, _args: list[str], **_kwargs: object) -> CommandResult:
        return CommandResult(returncode=0, stdout=self._stdout, stderr="")

    async def _append_workspace_events(self, **kwargs: object) -> None:
        events = kwargs["events"]
        assert isinstance(events, list)
        for event in events:
            self.payloads.append(dict(event.payload))


@pytest.mark.unit
async def test_park_disposition_redacts_secrets_it_persists() -> None:
    runner = _CapturingEventSinkRunner(f"deadbee {_SECRET_SUBJECT}\n")

    disposition = await _provenance._resolve_unpublished_comment_repair_disposition(
        runner,
        workspace_id="ws_secret",
        worktree_path=Path("/tmp/ws_secret"),
        state=MonitorState(),
        current_head=_SECOND,
        fetched_head=_BASE,
        provenance_remote_head=_BASE,
        diff_range=f"{_BASE}..HEAD",
        use_stale_snapshot_diff=False,
        has_comment_repair_provenance=False,
        has_conflicting_repair_provenance=True,
        current_operation_id=None,
    )

    assert disposition is not None
    push_result = disposition[1]
    assert push_result is not None
    # The park reason becomes ``awaiting_human_reason``; the payload reaches consumers.
    assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in push_result.stderr
    preserved = push_result.details["preserved_commits"]
    assert preserved == ["deadbee chore: wire token=[redacted]"]
    assert runner.payloads
    assert runner.payloads[0]["preserved_commits"] == preserved
