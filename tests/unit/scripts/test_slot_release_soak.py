"""Pure helpers of the manual slot-release soak (never runs the soak itself)."""

from __future__ import annotations

import pytest

from scripts import slot_release_soak as soak

pytestmark = pytest.mark.unit

_ADOPTED = {"adopted": True, "released_after_exit": True}


def test_clean_run_has_no_violations() -> None:
    assert (
        soak.check_invariants(
            lease_samples=[0, 2, 3, 1],
            slots=3,
            queued_statuses={"ws_a": "ready", "ws_b": "completed"},
            adoption=_ADOPTED,
        )
        == []
    )


def test_flags_leases_above_the_slot_count_and_unadmitted_queued_rows() -> None:
    violations = soak.check_invariants(
        lease_samples=[1, 4, 2, 5],
        slots=3,
        queued_statuses={"ws_b": "requested", "ws_a": "requested", "ws_c": "running"},
        adoption=_ADOPTED,
    )

    assert violations == [
        "leases in use exceeded 3 slots: max 5",
        "queued workspaces never admitted: ['ws_a', 'ws_b']",
    ]


def test_flags_missing_adoption_and_missing_release() -> None:
    not_adopted = soak.check_invariants(
        lease_samples=[], slots=1, queued_statuses={}, adoption={"adopted": False}
    )
    not_released = soak.check_invariants(
        lease_samples=[],
        slots=1,
        queued_statuses={},
        adoption={"adopted": True, "released_after_exit": False},
    )

    assert not_adopted == ["restarted worker did not adopt the in-flight invocation"]
    assert not_released == ["reconciler did not release the adopted lease after its process exited"]


def test_scratch_database_url_only_swaps_the_database_name() -> None:
    url = soak.scratch_database_url(
        "postgresql+asyncpg://awf:secret@postgres:5432/awf", "awf_slot_soak_abc"
    )

    assert url == "postgresql+asyncpg://awf:secret@postgres:5432/awf_slot_soak_abc"


def test_parse_args_requires_a_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWF_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        soak.parse_args([])

    config = soak.parse_args(["--database-url", "postgresql+asyncpg://x/y", "--slots", "2"])
    assert (config.slots, config.monitors, config.queued, config.keep_db) == (2, 7, 3, False)
