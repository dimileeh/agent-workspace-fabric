from awf.runtime.pr_monitor_models import CheckFailure, CheckFailureLogResult


def test_check_failure_log_result_empty_tuple_compatibility_is_hash_consistent() -> None:
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
