"""Classify profile secret declarations against hosted protected bindings."""

from collections.abc import Collection, Iterable

from awf.hosted_node.identity import MaterialDeniedError
from awf.profiles.models import ProfileSecret
from awf.profiles.resolver import ProfileResolutionError

HOSTED_OPTIONAL_SECRET_SKIPPED = "HOSTED_OPTIONAL_SECRET_SKIPPED"
PROTECTED_DECLARATION_MISMATCH = "PROTECTED_DECLARATION_MISMATCH"
PROTECTED_DECLARATION_UNSUPPORTED = "PROTECTED_DECLARATION_UNSUPPORTED"

_MESSAGES = {
    PROTECTED_DECLARATION_MISMATCH: (
        "profile secret {name!r} does not match the hosted protected binding "
        "(expected provider: protected, kind: mount, mode: ro, no ref, and the bound target)"
    ),
    PROTECTED_DECLARATION_UNSUPPORTED: (
        "required profile secret {name!r} cannot be provided by the hosted node; "
        "mark it required: false if only local test containers use it, or remove it"
    ),
}


class HostedProfileSecretError(MaterialDeniedError, ProfileResolutionError):
    """Deterministic hosted profile-secret declaration error (not transient infra).

    Stays a ``MaterialDeniedError`` for hosted callers that read ``reason`` and
    ``status``, and is a ``ProfileResolutionError`` so provisioning classifies it
    as ``profile_resolution_failure`` with its reason code. Only the secret name
    is ever included; never its ref, target, or value.
    """

    def __init__(self, reason: str, secret_name: str) -> None:
        message = f"{reason}: {_MESSAGES[reason].format(name=secret_name)}"
        # The two base initializers do not cooperate; set both contracts explicitly.
        ProfileResolutionError.__init__(self, message, reason_code=reason)
        self.reason = reason
        self.status = 422
        self.secret_name = secret_name


def partition_hosted_profile_secrets(
    secrets: Iterable[ProfileSecret],
    binding_names: Collection[str],
) -> tuple[tuple[ProfileSecret, ...], tuple[str, ...]]:
    """Split declarations into protected claims and skipped optional names.

    A declaration claims protected authority when its name is a binding name or
    its provider is ``protected``; callers validate claims exactly and are never
    allowed to skip them. Any other declaration cannot be provided by the hosted
    node: optional ones are skipped, required ones raise
    ``PROTECTED_DECLARATION_UNSUPPORTED``.
    """
    claims: list[ProfileSecret] = []
    skipped: list[str] = []
    for secret in secrets:
        if secret.name in binding_names or secret.provider == "protected":
            claims.append(secret)
        elif not secret.required:
            skipped.append(secret.name)
        else:
            raise HostedProfileSecretError(PROTECTED_DECLARATION_UNSUPPORTED, secret.name)
    return tuple(claims), tuple(skipped)
