import pytest

from awf.runtime.pr_monitor_models import CheckFailure, CheckFailureLogResult


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
