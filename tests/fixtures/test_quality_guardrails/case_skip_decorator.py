import pytest


@pytest.mark.skip(reason="TODO")
def test_skipped():
    assert 1 == 1


@pytest.mark.skipif(True, reason="TODO")
def test_skipif_true():
    assert 1 == 1
