import pytest
from pydantic import SecretStr, ValidationError

from awf.hosted_node.lifecycle_schemas import RecoveryAcknowledgement
from tests.unit.hosted_node.test_host_config import host_config

AUTHORITY = {
    "url": "https://authority.example/internal/v1/vm-nodes/org/node/physical-fence",
    "bearer": "synthetic-node-fence-only",
}


def test_authority_is_optional_frozen_secret_server_configuration():
    assert host_config().physical_fence_authority is None
    config = host_config(physical_fence_authority=AUTHORITY)
    authority = config.physical_fence_authority
    assert authority.url == AUTHORITY["url"]
    assert isinstance(authority.bearer, SecretStr)
    assert authority.bearer.get_secret_value() == AUTHORITY["bearer"]
    assert AUTHORITY["bearer"] not in repr(config)
    assert "bearer" not in config.model_dump()["physical_fence_authority"]
    assert AUTHORITY["bearer"] not in config.model_dump_json()
    with pytest.raises(ValidationError):
        authority.url = "https://other.example/fence"


@pytest.mark.parametrize(
    "url",
    [
        "http://authority.example/fence",
        "//authority.example/fence",
        "/internal/fence",
        "https:///fence",
        "https://user:password@authority.example/fence",
        "https://@authority.example/fence",
        "https://authority.example/fence?token=secret",
        "https://authority.example/fence?",
        "https://authority.example/fence#anchor",
        "https://authority.example/fence#",
        "https://authority.example:invalid/fence",
        "https://authority.example:0/fence",
        "https://authority.example:65536/fence",
        " https://authority.example/fence",
        "https://authority.example/\nfence",
        "https://authority.example/\x7ffence",
        "https://authority.example\\other/fence",
        "https://authority.example/" + "x" * 2048,
        123,
    ],
)
def test_authority_url_cannot_redirect_trust(url):
    with pytest.raises(ValidationError):
        host_config(physical_fence_authority={**AUTHORITY, "url": url})


@pytest.mark.parametrize("bearer", ["", "short", "x" * 4097, "x" * 16 + "\r\nInjected: yes"])
def test_authority_rejects_invalid_bearer_without_echo(bearer):
    with pytest.raises(ValidationError) as failure:
        host_config(physical_fence_authority={**AUTHORITY, "bearer": bearer})
    assert "Injected" not in str(failure.value)


@pytest.mark.parametrize(
    "extra", [{"verify": False}, {"owner": "other/node"}, {"proxy": "http://x"}]
)
def test_authority_unknown_settings_are_forbidden(extra):
    with pytest.raises(ValidationError):
        host_config(physical_fence_authority={**AUTHORITY, **extra})


@pytest.mark.parametrize("field", ["physical_fence_authority", "host", "evidence", "expires_at"])
def test_recovery_request_cannot_supply_authority_or_evidence(field):
    with pytest.raises(ValidationError):
        RecoveryAcknowledgement.model_validate(
            {"idempotency_key": "ack", "receipt_ref": "receipt", field: AUTHORITY}
        )
