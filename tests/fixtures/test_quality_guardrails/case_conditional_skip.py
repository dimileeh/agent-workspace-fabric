import pytest

HAS_DOCKER = False


@pytest.mark.skipif(not HAS_DOCKER, reason="requires docker")
def test_container():
    assert isinstance(HAS_DOCKER, bool)


def has_tool():
    return True


def test_guarded_skip():
    if not has_tool():
        pytest.skip("tool missing")

    result = 1 + 1

    assert result == 2
