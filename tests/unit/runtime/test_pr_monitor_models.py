from awf.runtime.pr_monitor_models import CheckFailure, CheckFailureLogResult


def test_check_failure_log_result_does_not_compare_equal_to_tuple() -> None:
    failure = CheckFailure(name="tests", conclusion="FAILURE", log_excerpt="boom")
    result = CheckFailureLogResult(failures=(failure,), runs_in_progress=False)
    equivalent_result = CheckFailureLogResult(failures=(failure,), runs_in_progress=False)
    empty_result = CheckFailureLogResult()

    assert result == equivalent_result
    assert hash(result) == hash(equivalent_result)
    assert empty_result == ()
    assert result != (failure,)
    assert (failure,) != result
