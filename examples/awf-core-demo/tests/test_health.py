from awf_core_demo import health


def test_health_payload() -> None:
    assert health() == {"status": "ok"}
