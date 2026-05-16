# awf-test-quality: ignore[EMPTY_TEST] because documents generated conformance fixture with intentionally empty sentinel test
def test_intentionally_empty():
    pass


# awf-test-quality: ignore[FAKE_ASSERT] because TODO
def test_vague_escape():
    assert True


# awf-test-quality: ignore[EMPTY_TEST]
def test_missing_reason():
    pass
