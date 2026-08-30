import pytest

from awf.runtime.pr_monitor_models import (
    CheckFailure,
    CheckFailureLogResult,
    CheckState,
    MergeableState,
    MergeStateStatus,
    PRStatus,
    ReviewThread,
)


def _thread(tid: str, *, body: str = "nit", is_outdated: bool = False) -> ReviewThread:
    return ReviewThread(
        thread_id=tid,
        path="src/x.py",
        line=10,
        body_excerpt=body,
        author=None,
        is_outdated=is_outdated,
    )


@pytest.mark.unit
def test_pr_status_canonical_unresolved_inline_threads_active_wins() -> None:
    """PRStatus exposes the merge-authoritative active-wins unresolved view."""
    active = _thread("D", body="active representation")
    outdated_dup = _thread("D", body="outdated representation", is_outdated=True)
    outdated_only = _thread("B", body="outdated only", is_outdated=True)
    status = PRStatus(
        number=42,
        head_sha="abc123",
        mergeable=MergeableState.MERGEABLE,
        merge_state_status=MergeStateStatus.CLEAN,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(active,),
        unresolved_review_comments=(),
        base_behind_count=0,
        outdated_unresolved_inline_threads=(outdated_dup, outdated_only),
    )
    canonical = status.canonical_unresolved_inline_threads
    assert [t.thread_id for t in canonical] == ["D", "B"]
    assert canonical[0].body_excerpt == "active representation"
    assert canonical[0].is_outdated is False


@pytest.mark.unit
def test_check_failure_log_result_empty_tuple_compatibility_is_hash_consistent() -> None:
    """Verify check failure log result empty tuple compatibility is hash consistent."""
    failure = CheckFailure(name="tests", conclusion="FAILURE", log_excerpt="boom")
    result = CheckFailureLogResult(failures=(failure,), runs_in_progress=False)
    equivalent_result = CheckFailureLogResult(failures=(failure,), runs_in_progress=False)
    empty_result = CheckFailureLogResult()
    empty_result_with_running_check = CheckFailureLogResult(runs_in_progress=True)

    assert result == equivalent_result
    assert hash(result) == hash(equivalent_result)
    assert empty_result == ()
    assert hash(empty_result) == hash(())
    assert empty_result_with_running_check != ()
    assert result != (failure,)
    assert (failure,) != result


@pytest.mark.unit
def test_check_failure_log_result_preserves_tuple_like_access() -> None:
    """Verify check failure log result preserves tuple like access."""
    first = CheckFailure(name="lint", conclusion="FAILURE", log_excerpt="ruff failed")
    second = CheckFailure(name="tests", conclusion="FAILURE", log_excerpt="pytest failed")
    result = CheckFailureLogResult(failures=(first, second), runs_in_progress=True)

    assert bool(result)
    assert len(result) == 2
    assert tuple(result) == (first, second)
    assert result[0] == first
    assert result[1:] == (second,)
