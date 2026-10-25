"""Non-secret controller association for the canonical workspace create API."""

from typing import Annotated

from pydantic import Field, ValidationError, model_validator

from awf.hosted_node.schemas import Binding, StrictModel

POLICY_KEY = "hosted_node"


class RuntimeCredentials(StrictModel):
    agent: Binding
    github: Binding


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
