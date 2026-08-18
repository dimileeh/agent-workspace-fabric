"""Persisted-stack clarification upgrade writers.

Resume and monitor paths keep the Compose file they were originally rendered
with rather than re-resolving a profile. These writers migrate such a persisted
file in place — adding (or re-deriving) the isolated clarification service and
recording its model-network reconciliation — while
:mod:`awf.node.compose_manager_clarification` holds the pure inspection helpers
they build on.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

import yaml

from awf.db.enums import AgentRuntime
from awf.node.compose_manager import AuthMount
from awf.node.compose_manager_clarification import (
    _PERSISTED_CLARIFICATION_MODEL_NETWORK_RECONCILED,
    _PERSISTED_CLARIFICATION_SERVICE_MANAGED,
    _PERSISTED_CLARIFICATION_SERVICE_RUNTIME,
    _attach_persisted_clarification_model_network,
    _clarification_model_service_names,
    _is_managed_persisted_clarification_service,
    _pending_persisted_clarification_model_network_services,
    _persisted_clarification_runtime_is_current,
    clarification_runtime_signature,
    persisted_clarification_runtime_enum,
)


def _legacy_bind_mount(value: object) -> AuthMount | None:
    """Parse the string bind syntax emitted by older AWF Compose templates."""
    if not isinstance(value, str):
        return None
    parts = value.rsplit(":", 2)
    if len(parts) == 2:
        source, target = parts
        mode = "ro"
    elif len(parts) == 3:
        source, target, mode = parts
    else:
        return None
    if not source or not target or not target.startswith("/"):
        return None
    return AuthMount(source=source, target=target, mode=mode)


def _legacy_shared_git_mirror_target(auth_mounts: list[AuthMount]) -> str:
    """Return the persisted shared-mirror target, or a harmless sentinel."""
    return next(
        (
            mount.target
            for mount in auth_mounts
            if mount.source == mount.target and mount.target.endswith(".git")
        ),
        "/__awf_legacy_shared_git_mirror__",
    )


def upgrade_persisted_clarification_service(
    *,
    compose_file: Path,
    workspace_id: str,
    agent_runtime: AgentRuntime | str,
    agent_model: str | None = None,
) -> tuple[str, ...] | None:
    """Add clarification safely to a legacy persisted stack, if needed.

    Resume and monitor paths intentionally retain their original rendered stack
    rather than re-resolving a profile. This narrowly augments those generated
    files from the persisted agent inputs, preserving services while denying the
    clarification container the primary worktree and Git credentials. Returns
    the model services whose network declaration changed or whose earlier
    persisted migration remains incomplete; returns ``None`` when clarification
    was already present, targets the current runtime/model, and is fully
    reconciled.

    A monitoring workspace can fall back to a different agent runtime (or model)
    in place, which leaves the persisted clarification service frozen on the
    credentials and model-sidecar route of the runtime that rendered it. The
    service therefore carries the runtime/model it was rendered for, and a
    mismatch re-derives it from the persisted agent service rather than
    short-circuiting.
    """
    runtime_enum = persisted_clarification_runtime_enum(agent_runtime)
    runtime_signature = clarification_runtime_signature(runtime_enum, agent_model)
    try:
        document = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"could not read persisted Compose file {compose_file}") from exc
    if not isinstance(document, dict):
        raise ValueError("persisted Compose file must contain a mapping")
    services = document.get("services")
    if not isinstance(services, dict):
        raise ValueError("persisted Compose file must contain a services mapping")
    if "clarification" in services:
        agent = services.get("agent")
        agent_image = agent.get("image") if isinstance(agent, Mapping) else None
        agent_working_dir = agent.get("working_dir") if isinstance(agent, Mapping) else None
        if not _is_managed_persisted_clarification_service(
            services["clarification"],
            legacy_agent_image=agent_image,
            legacy_agent_working_dir=agent_working_dir,
        ):
            raise ValueError(
                "persisted Compose file clarification service conflicts with the managed "
                "clarification service"
            )
        if _persisted_clarification_runtime_is_current(
            services["clarification"], runtime_signature
        ):
            pending_model_services = _pending_persisted_clarification_model_network_services(
                document, services
            )
            return pending_model_services or None
        # The runtime/model moved under the persisted stack (in-place provider
        # fallback). Fall through and re-derive clarification from the agent
        # service so it carries the selected runtime's credentials and route.
    agent = services.get("agent")
    if not isinstance(agent, dict):
        raise ValueError("persisted Compose file must contain an agent service")
    image = agent.get("image")
    if not isinstance(image, str) or not image:
        raise ValueError("persisted agent service must declare an image")
    # Import lazily: stack launching imports the compose manager, while a
    # compatibility upgrade runs only when an isolated clarification invocation
    # is requested. Reuse the current allowlist so legacy migration cannot drift
    # from the regular clarification service's credential isolation policy.
    from awf.node.stack_launcher import (
        _clarification_agent_environment,
        _clarification_auth_mounts,
    )
    from awf.node.stack_launcher_auth_helpers import (
        aws_profile_path_rewrites as _aws_profile_path_rewrites,
    )
    from awf.node.stack_launcher_auth_helpers import (
        clarification_contained_credential_files as _clarification_contained_credential_files,
    )
    from awf.node.stack_launcher_auth_helpers import (
        external_account_subject_token_file_rewrites as _external_account_subject_token_file_rewrites,
    )
    from awf.node.stack_launcher_auth_helpers import (
        legacy_clarification_entrypoint,
    )

    raw_environment = agent.get("environment", {})
    agent_environment = (
        {str(name): value for name, value in raw_environment.items() if isinstance(value, str)}
        if isinstance(raw_environment, Mapping)
        else {}
    )
    raw_volumes = agent.get("volumes", ())
    auth_mounts = (
        [mount for value in raw_volumes if (mount := _legacy_bind_mount(value)) is not None]
        if isinstance(raw_volumes, list)
        else []
    )
    mirror_target = _legacy_shared_git_mirror_target(auth_mounts)
    agent_environment_items = tuple(agent_environment.items())
    # Never hand the primary worktree to a reason-only invocation, even if a
    # malformed legacy environment happens to name it as a provider setting.
    provider_auth_mounts = [mount for mount in auth_mounts if mount.target != "/workspace"]
    selected_mounts = _clarification_auth_mounts(
        provider_auth_mounts,
        agent_environment=agent_environment_items,
        mirror_target=mirror_target,
        agent_runtime=runtime_enum,
        agent_model=agent_model,
    )
    provider_environment = _clarification_agent_environment(
        agent_environment_items,
        auth_mounts=provider_auth_mounts,
        mirror_target=mirror_target,
        agent_runtime=runtime_enum,
        agent_model=agent_model,
        prefer_file_auth=False,
    )
    external_account_subject_token_file_rewrites = _external_account_subject_token_file_rewrites(
        provider_auth_mounts,
        agent_environment=agent_environment_items,
        mirror_target=mirror_target,
        agent_runtime=runtime_enum,
        agent_model=agent_model,
    )
    aws_profile_rewrites = _aws_profile_path_rewrites(
        provider_auth_mounts,
        agent_environment=agent_environment_items,
        mirror_target=mirror_target,
        agent_runtime=runtime_enum,
        agent_model=agent_model,
    )
    clarification_environment = dict(provider_environment)
    if external_account_subject_token_file_rewrites:
        clarification_environment[
            "AWF_CLARIFICATION_EXTERNAL_ACCOUNT_SUBJECT_TOKEN_FILE_REWRITES"
        ] = json.dumps(external_account_subject_token_file_rewrites).replace("$", "$$")
    if aws_profile_rewrites:
        clarification_environment["AWF_CLARIFICATION_AWS_PROFILE_PATH_REWRITES"] = json.dumps(
            aws_profile_rewrites
        ).replace("$", "$$")
    clarification_volumes: list[str] = []
    for index, mount in enumerate(selected_mounts):
        clarification_environment[f"AWF_CLARIFICATION_AUTH_TARGET_{index}"] = mount.target.replace(
            "$", "$$"
        )
        clarification_volumes.append(f"{mount.source}:/run/awf/clarification-auth/{index}:ro")
    clarification_model_services = _attach_persisted_clarification_model_network(
        services,
        _clarification_model_service_names(
            clarification_environment.items(),
            service_names=(
                str(name)
                for name, service in services.items()
                if name != "agent" and isinstance(service, dict)
            ),
        ),
    )

    clarification: dict[str, object] = {
        "image": image,
        _PERSISTED_CLARIFICATION_SERVICE_MANAGED: True,
        _PERSISTED_CLARIFICATION_SERVICE_RUNTIME: runtime_signature,
        "working_dir": agent.get("working_dir", "/workspace"),
        "networks": [
            "clarification_egress_net",
            *(("clarification_model_net",) if clarification_model_services else ()),
        ],
        "profiles": ["awf-clarification"],
        "command": ["sh", "-c", "sleep infinity"],
        "restart": "no",
    }
    if clarification_environment:
        clarification["environment"] = clarification_environment
    if clarification_volumes:
        clarification["volumes"] = clarification_volumes
        clarification["entrypoint"] = legacy_clarification_entrypoint(
            len(selected_mounts),
            rewrite_external_account_subject_token_file=bool(
                external_account_subject_token_file_rewrites
            ),
            rewrite_aws_profile_paths=bool(aws_profile_rewrites),
            contained_credential_files=_clarification_contained_credential_files(
                provider_auth_mounts,
                agent_environment=agent_environment_items,
                mirror_target=mirror_target,
                agent_runtime=runtime_enum,
                agent_model=agent_model,
            ),
        )
    if "host.docker.internal:host-gateway" in agent.get("extra_hosts", []):
        clarification["extra_hosts"] = ["host.docker.internal:host-gateway"]
    if isinstance(agent.get("deploy"), Mapping):
        clarification["deploy"] = copy.deepcopy(agent["deploy"])
    services["clarification"] = clarification

    raw_networks = document.get("networks")
    if raw_networks is None:
        networks: dict[str, object] = {}
        document["networks"] = networks
    elif isinstance(raw_networks, dict):
        networks = raw_networks
    else:
        raise ValueError("persisted Compose file networks must be a mapping")
    clarification_network: dict[str, object] = {
        "name": f"awf-{workspace_id}-clarification-egress-net"
    }
    awf_network = networks.get("awf_net")
    if isinstance(awf_network, Mapping) and awf_network.get("internal") is True:
        clarification_network["internal"] = True
    networks.setdefault("clarification_egress_net", clarification_network)
    if clarification_model_services:
        networks.setdefault(
            "clarification_model_net",
            {
                "name": f"awf-{workspace_id}-clarification-model-net",
                "internal": True,
            },
        )
    document[_PERSISTED_CLARIFICATION_MODEL_NETWORK_RECONCILED] = not bool(
        clarification_model_services
    )

    temporary_path: Path | None = None
    try:
        file_mode = compose_file.stat().st_mode & 0o777
        fd, raw_temporary_path = tempfile.mkstemp(
            prefix=f".{compose_file.name}.",
            suffix=".tmp",
            dir=compose_file.parent,
        )
        temporary_path = Path(raw_temporary_path)
        with os.fdopen(fd, "w", encoding="utf-8") as temporary_file:
            yaml.safe_dump(document, temporary_file, sort_keys=False)
        temporary_path.chmod(file_mode)
        temporary_path.replace(compose_file)
    except (OSError, yaml.YAMLError) as exc:
        if temporary_path is not None:
            with contextlib.suppress(OSError):
                temporary_path.unlink()
        raise ValueError(f"could not upgrade persisted Compose file {compose_file}") from exc
    return clarification_model_services


def mark_persisted_clarification_model_network_reconciled(*, compose_file: Path) -> None:
    """Durably record that Compose recreated a migrated model sidecar."""
    try:
        document = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"could not read persisted Compose file {compose_file}") from exc
    if not isinstance(document, dict):
        raise ValueError("persisted Compose file must contain a mapping")
    if document.get(_PERSISTED_CLARIFICATION_MODEL_NETWORK_RECONCILED) is True:
        return

    document[_PERSISTED_CLARIFICATION_MODEL_NETWORK_RECONCILED] = True
    temporary_path: Path | None = None
    try:
        file_mode = compose_file.stat().st_mode & 0o777
        fd, raw_temporary_path = tempfile.mkstemp(
            prefix=f".{compose_file.name}.",
            suffix=".tmp",
            dir=compose_file.parent,
        )
        temporary_path = Path(raw_temporary_path)
        with os.fdopen(fd, "w", encoding="utf-8") as temporary_file:
            yaml.safe_dump(document, temporary_file, sort_keys=False)
        temporary_path.chmod(file_mode)
        temporary_path.replace(compose_file)
    except (OSError, yaml.YAMLError) as exc:
        if temporary_path is not None:
            with contextlib.suppress(OSError):
                temporary_path.unlink()
        raise ValueError(
            f"could not mark persisted Compose model network reconciled {compose_file}"
        ) from exc
