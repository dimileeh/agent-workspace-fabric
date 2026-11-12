"""Hosted profile-secret declaration classification (pure, no DB)."""

import pytest

from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.profile_secrets import (
    HOSTED_OPTIONAL_SECRET_SKIPPED,
    PROTECTED_DECLARATION_MISMATCH,
    PROTECTED_DECLARATION_UNSUPPORTED,
    HostedProfileSecretError,
    partition_hosted_profile_secrets,
)
from awf.profiles.models import ProfileSecret
from awf.profiles.resolver import ProfileResolutionError

BINDINGS = ("agent", "github")
_REF = "env/DO_NOT_LEAK_REF"


def _secret(**fields: object) -> ProfileSecret:
    return ProfileSecret.model_validate(fields)


def test_reason_code_constants_are_stable() -> None:
    assert HOSTED_OPTIONAL_SECRET_SKIPPED == "HOSTED_OPTIONAL_SECRET_SKIPPED"
    assert PROTECTED_DECLARATION_MISMATCH == "PROTECTED_DECLARATION_MISMATCH"
    assert PROTECTED_DECLARATION_UNSUPPORTED == "PROTECTED_DECLARATION_UNSUPPORTED"


def test_optional_unsupported_secrets_are_skipped_by_name_only() -> None:
    secrets = [
        _secret(
            name="db_password", kind="env", provider="env", ref=_REF, target="X", required=False
        ),
        _secret(name="cache_token", target="/run/secrets/cache", required=False),
    ]

    claims, skipped = partition_hosted_profile_secrets(secrets, BINDINGS)

    assert claims == ()
    assert skipped == ("db_password", "cache_token")


def test_exact_protected_claims_are_returned_and_nothing_skipped() -> None:
    secrets = [
        _secret(name="agent", provider="protected", target="/run/awf/secrets/openai.api-key"),
        _secret(name="github", provider="protected", target="/run/awf/secrets/github.api-key"),
    ]

    claims, skipped = partition_hosted_profile_secrets(secrets, BINDINGS)

    assert claims == tuple(secrets)
    assert skipped == ()


@pytest.mark.parametrize(
    "fields",
    [
        # A binding name always claims protected authority, even when optional.
        {"name": "agent", "kind": "env", "provider": "env", "required": False},
        {"name": "github", "provider": "env"},
        # provider: protected always claims protected authority.
        {"name": "unknown", "provider": "protected", "required": False},
    ],
)
def test_protected_claims_are_never_skipped(fields: dict[str, object]) -> None:
    secret = _secret(target="/run/awf/secrets/x", **fields)

    claims, skipped = partition_hosted_profile_secrets([secret], BINDINGS)

    assert claims == (secret,)
    assert skipped == ()


def test_required_unsupported_secret_raises_non_transient_profile_error() -> None:
    secret = _secret(name="db_password", kind="env", provider="env", ref=_REF, target="X")

    with pytest.raises(HostedProfileSecretError) as caught:
        partition_hosted_profile_secrets([secret], BINDINGS)

    exc = caught.value
    assert isinstance(exc, MaterialDeniedError)
    assert isinstance(exc, ProfileResolutionError)
    assert exc.reason == exc.reason_code == PROTECTED_DECLARATION_UNSUPPORTED
    assert exc.status == 422
    assert exc.findings == ()
    assert exc.detail == {"reason_code": PROTECTED_DECLARATION_UNSUPPORTED, "findings": []}
    assert exc.secret_name == "db_password"
    message = str(exc)
    assert message.startswith(f"{PROTECTED_DECLARATION_UNSUPPORTED}:")
    assert "'db_password'" in message
    assert _REF not in message
    assert "required: false" in message


def test_mismatch_error_names_the_secret_and_keeps_both_contracts() -> None:
    exc = HostedProfileSecretError(PROTECTED_DECLARATION_MISMATCH, "agent")

    assert isinstance(exc, MaterialDeniedError)
    assert isinstance(exc, ProfileResolutionError)
    assert exc.reason == exc.reason_code == PROTECTED_DECLARATION_MISMATCH
    assert exc.status == 422
    assert str(exc).startswith(f"{PROTECTED_DECLARATION_MISMATCH}:")
    assert "'agent'" in str(exc)
