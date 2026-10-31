"""Non-secret controller association for the canonical workspace create API."""

from typing import Annotated

from pydantic import BeforeValidator, Field, ValidationError, model_validator

from awf.hosted_node.schemas import Binding, Opaque, StrictModel

POLICY_KEY = "hosted_node"


class RuntimeCredentials(StrictModel):
    agent: Binding
    github: Binding


class HostedControlAuthorization(StrictModel):
    """Controller attestation derived from the caller's current authorization."""

    namespace: Opaque
    actor: Annotated[str, Field(min_length=1, max_length=256, pattern=r"^\S(?:.*\S)?$")]
    allow_grants: Annotated[bool, Field(strict=True)] = False


class HostedRetryRequest(StrictModel):
    hosted: "HostedWorkspace | None" = None


class HostedWorkspace(StrictModel):
    workspace_id: Annotated[str, Field(pattern=r"^ws_[A-Za-z0-9_-]+$", max_length=128)]
    credentials: RuntimeCredentials

    @model_validator(mode="after")
    def exact_workspace(self) -> "HostedWorkspace":
        agent, github = self.credentials.agent, self.credentials.github
        if (
            agent.workspace_id != self.workspace_id
            or github.workspace_id != self.workspace_id
            or agent.identity != github.identity
            or agent.namespace != github.namespace
            or agent.authorization_generation != github.authorization_generation
            or agent.preparation_id == github.preparation_id
            or agent.target == github.target
        ):
            raise ValueError("HOSTED_WORKSPACE_BINDING_MISMATCH")
        return self


class HostedReauthorization(StrictModel):
    expected_association: HostedWorkspace
    replacement_association: HostedWorkspace

    @model_validator(mode="after")
    def fixed_scope_fresh_authority(self) -> "HostedReauthorization":
        old, new = self.expected_association, self.replacement_association
        if old.workspace_id != new.workspace_id:
            raise ValueError("HOSTED_WORKSPACE_BINDING_MISMATCH")
        for role in ("agent", "github"):
            before, after = getattr(old.credentials, role), getattr(new.credentials, role)
            if any(
                getattr(before, key) != getattr(after, key)
                for key in ("namespace", "workspace_id", "purpose", "target")
            ) or any(
                getattr(before, key) == getattr(after, key)
                for key in ("preparation_id", "grant_id", "idempotency_key")
            ):
                raise ValueError("HOSTED_REAUTHORIZATION_SCOPE_MISMATCH")
        return self


def _non_null_reauthorization(value: object) -> object:
    if value is None:
        raise ValueError("HOSTED_REAUTHORIZATION_REQUIRED")
    return value


OptionalReauthorization = Annotated[
    HostedReauthorization | None, BeforeValidator(_non_null_reauthorization)
]


def association_matches(stored: object, incoming: HostedWorkspace | None) -> bool:
    if incoming is None:
        return stored is None
    try:
        anchor = HostedWorkspace.model_validate(stored)
    except ValidationError:
        return False
    # Admission verifies each supplied receipt and its current grant. Refresh
    # metadata may advance; the durable workspace authority must not rebind.
    refreshed = {"revision", "fence", "expires_at", "idempotency_key"}
    return anchor.workspace_id == incoming.workspace_id and all(
        getattr(anchor.credentials, role).model_dump(exclude=refreshed)
        == getattr(incoming.credentials, role).model_dump(exclude=refreshed)
        for role in ("agent", "github")
    )
