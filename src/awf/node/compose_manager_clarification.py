"""Persisted clarification-service Compose migration helpers."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

from awf.db.enums import AgentRuntime
from awf.service.environment import compose_expand_value

_CLARIFICATION_BASE_URL_ENV_NAMES = ("OPENAI_BASE_URL", "ANTHROPIC_BASE_URL")
_CLARIFICATION_CREDENTIAL_ENDPOINT_ENV_NAMES = ("AWS_CONTAINER_CREDENTIALS_FULL_URI",)
_CLARIFICATION_PROXY_URL_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
_PERSISTED_CLARIFICATION_MODEL_NETWORK_RECONCILED = (
    "x-awf-persisted-clarification-model-network-reconciled"
)
_PERSISTED_CLARIFICATION_SERVICE_MANAGED = "x-awf-persisted-clarification-service-managed"
_PERSISTED_CLARIFICATION_SERVICE_RUNTIME = "x-awf-persisted-clarification-service-runtime"


def persisted_clarification_runtime_enum(agent_runtime: AgentRuntime | str) -> AgentRuntime:
    """Normalize a live or persisted runtime label to its enum member."""
    if isinstance(agent_runtime, AgentRuntime):
        return agent_runtime
    try:
        return AgentRuntime(agent_runtime)
    except ValueError:
        return AgentRuntime.antigravity


def clarification_runtime_signature(
    agent_runtime: AgentRuntime | str, agent_model: str | None
) -> str:
    """Return the runtime/model identity a clarification service was rendered for.

    Stamped onto the rendered service so a later isolated re-ask can tell that a
    cross-runtime (or cross-model) provider fallback has moved the workspace off
    the runtime whose credentials the persisted clarification container carries.
    """
    return f"{persisted_clarification_runtime_enum(agent_runtime).value}|{agent_model or ''}"


def _persisted_clarification_runtime_is_current(service: object, signature: str) -> bool:
    """Return whether a persisted clarification service targets ``signature``.

    Services rendered before the stamp existed carry no runtime identity; those
    stay a no-op rather than being rewritten on a guess. Only an explicit
    mismatch — a cross-runtime/model fallback moving the workspace off the
    runtime the service was rendered for — reports a stale service.
    """
    if not isinstance(service, Mapping):
        return False
    persisted = service.get(_PERSISTED_CLARIFICATION_SERVICE_RUNTIME)
    return not isinstance(persisted, str) or persisted == signature


def _is_managed_persisted_clarification_service(
    service: object,
    *,
    legacy_agent_image: object = None,
    legacy_agent_working_dir: object = None,
) -> bool:
    """Return whether a persisted service has AWF's clarification signature."""
    if not isinstance(service, Mapping):
        return False
    networks = service.get("networks")
    has_signature = (
        service.get("profiles") == ["awf-clarification"]
        and isinstance(networks, list)
        and "clarification_egress_net" in networks
        and service.get("command") == ["sh", "-c", "sleep infinity"]
        and service.get("restart") == "no"
    )
    if not has_signature:
        return False
    if service.get(_PERSISTED_CLARIFICATION_SERVICE_MANAGED) is True:
        return True
    return (
        _PERSISTED_CLARIFICATION_SERVICE_MANAGED not in service
        and isinstance(legacy_agent_image, str)
        and service.get("image") == legacy_agent_image
        and legacy_agent_working_dir == "/workspace"
        and service.get("working_dir") == legacy_agent_working_dir
    )


def _clarification_model_service_names(
    clarification_environment: Iterable[tuple[str, str]], *, service_names: Iterable[str]
) -> tuple[str, ...]:
    """Return profile services selected by clarification's provider or proxy URL."""

    environment = dict(clarification_environment)
    endpoint_names: tuple[str, ...] = (
        *_CLARIFICATION_BASE_URL_ENV_NAMES,
        *_CLARIFICATION_CREDENTIAL_ENDPOINT_ENV_NAMES,
        *_CLARIFICATION_PROXY_URL_ENV_NAMES,
    )
    if environment.get("AWF_OPENCODE_OLLAMA_BASE_URL"):
        endpoint_names += ("AWF_OPENCODE_OLLAMA_BASE_URL",)
    elif environment.get("OLLAMA_HOST"):
        endpoint_names += ("OLLAMA_HOST",)
    names = tuple(service_names)
    names_by_hostname = {name.lower(): name for name in names}
    selected_names: set[str] = set()
    for endpoint_name in endpoint_names:
        endpoint = environment.get(endpoint_name, "")
        try:
            endpoint = compose_expand_value(endpoint, environ=os.environ)
            if endpoint_name.endswith("OLLAMA_BASE_URL") or endpoint_name == "OLLAMA_HOST":
                endpoint = endpoint if "://" in endpoint else f"//{endpoint}"
            hostname = urlsplit(endpoint).hostname
        except ValueError:
            continue
        if hostname and (service_name := names_by_hostname.get(hostname.lower())):
            selected_names.add(service_name)
    return tuple(name for name in names if name in selected_names)


def _attach_persisted_clarification_model_network(
    services: dict[object, object],
    model_service_names: Iterable[str],
) -> tuple[str, ...]:
    """Attach legacy rendered model services to clarification's dedicated route.

    Idempotent: a re-render (for example after a cross-runtime fallback) must not
    duplicate a network the sidecar already declares, but the name is still
    returned so the caller re-runs the runtime attach and reconciliation.
    """

    attached_names: list[str] = []
    for name in model_service_names:
        service = services.get(name)
        if not isinstance(service, dict):
            continue
        service_networks = service.get("networks")
        if not isinstance(service_networks, list) or "awf_net" not in service_networks:
            continue
        if "clarification_model_net" not in service_networks:
            service["networks"] = [*service_networks, "clarification_model_net"]
        attached_names.append(name)
    return tuple(attached_names)


def _pending_persisted_clarification_model_network_services(
    document: Mapping[object, object], services: Mapping[object, object]
) -> tuple[str, ...]:
    """Return sidecars awaiting a persisted clarification network reconciliation."""
    if document.get(_PERSISTED_CLARIFICATION_MODEL_NETWORK_RECONCILED) is True:
        return ()
    return tuple(
        str(name)
        for name, service in services.items()
        if name not in {"agent", "clarification"}
        and isinstance(service, Mapping)
        and isinstance(service.get("networks"), list)
        and "clarification_model_net" in service["networks"]
    )
