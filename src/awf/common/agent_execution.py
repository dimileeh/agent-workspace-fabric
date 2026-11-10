"""Versioned, secret-free agent selection shared by admission and execution.

Only rows without the snapshot use legacy inference. A present invalid snapshot
is an error, never permission to execute with native credentials/defaults.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Literal, cast
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, ValidationError

EXECUTION_CONTRACT_VERSION = 1
EXECUTION_POLICY_KEY = "agent_execution"
LEGACY_CONNECTION_SCOPE = "legacy"
# Managed OpenRouter keys are minted per invocation. Every managed selection
# shares this fixed reference, so connection and cooldown scopes never fragment
# per workspace and no key value or hash enters the snapshot.
MANAGED_OPENROUTER_CREDENTIAL_REF = "managed/openrouter"
PROVIDER_BUDGET_POLICY_KEY = "provider_budget"
# Workers at or above this capability mint, meter and finalize managed keys.
MANAGED_PROVIDER_KEY_WORKER_VERSION = 3
DIAGNOSTIC_PROVIDER_BUDGET_USD = 1.0
CircuitKey = tuple[str, str] | tuple[str, str, str]
_NATIVE = {
    "codex": "openai",
    "claude_code": "anthropic",
    "cursor": "cursor",
    "antigravity": "antigravity",
    "grok": "xai",
}
OLLAMA_ENDPOINT_KEYS = ("AWF_OLLAMA_BASE_URL", "AWF_OPENCODE_OLLAMA_BASE_URL", "OLLAMA_HOST")
_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(?::?-)|(?::?\?[^}]*))?\}$")


class ExecutionSelection(BaseModel):
    """A frozen execution contract; credential_ref contains names, never values."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    agent: str
    provider: str
    model: str | None
    effort: str | None = None
    effort_explicit: bool = False
    model_explicit: bool = False
    endpoint: str | None = None
    credential_ref: str | None = None
    network_service: str | None = None
    compatibility: Literal["native", "legacy", "unverified"] = "native"

    @property
    def third_party(self) -> bool:
        return self.agent in {"codex", "claude_code"} and self.provider != _NATIVE[self.agent]

    @property
    def managed_key(self) -> bool:
        return self.credential_ref == MANAGED_OPENROUTER_CREDENTIAL_REF

    def connection_scope(self, workspace_id: str | None = None) -> str:
        if not self.third_party:
            return LEGACY_CONNECTION_SCOPE
        if self.network_service and not workspace_id:
            raise ValueError("EXECUTION_NETWORK_NAMESPACE_REQUIRED")
        identity = [
            self.endpoint,
            self.credential_ref,
            self.network_service,
            workspace_id if self.network_service else None,
        ]
        return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def normalize_endpoint(raw: str) -> str:
    """Normalize a daemon origin/prefix; reject credential-bearing URLs safely."""
    try:
        parts = urlsplit(raw if "://" in raw else "http://" + raw)
        host = parts.hostname
        port = parts.port
        if (
            not host
            or parts.scheme not in {"http", "https"}
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or any(c.isspace() or ord(c) < 32 for c in raw)
        ):
            raise ValueError
        from awf.service.provider_readiness_helpers import _normalize_host_local_host

        host = _normalize_host_local_host(host.lower())
        authority = f"[{host}]" if ":" in host else host
        if port is None:
            port = 11434
        authority += f":{port}"
        path = parts.path.rstrip("/")
        if path.endswith("/v1"):
            path = path[:-3]
        return urlunsplit((parts.scheme.lower(), authority, path, "", ""))
    except ValueError:
        raise ValueError("PROVIDER_ENDPOINT_INVALID") from None


def provider_for_agent_model(agent: str, model: str | None) -> str | None:
    """Preserve historical model-prefix inference for legacy rows."""
    from awf.adapters.provider_failures import infer_provider

    if agent in {"cursor", "antigravity"}:
        return agent
    return (
        infer_provider(model=model)
        or _NATIVE.get(agent)
        or ("opencode" if agent == "opencode" else None)
    )


def resolve_execution(
    *,
    agent: str,
    provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    allow_unverified: bool = False,
    profile: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    managed_openrouter: bool = False,
) -> ExecutionSelection:
    """Resolve once, before persistence; no mutable global provider selection."""
    from awf.adapters.defaults import HISTORICAL_AGENT_DEFAULTS
    from awf.db.enums import AgentRuntime

    explicit_provider = provider
    provider = provider or _NATIVE.get(agent) or provider_for_agent_model(agent, model)
    if provider is None:
        raise ValueError("AGENT_PROVIDER_UNSUPPORTED")
    third_party = agent in {"codex", "claude_code"} and provider != _NATIVE[agent]
    if (
        provider != _NATIVE.get(agent)
        and not (third_party and provider in {"ollama", "openrouter"})
        and agent != "opencode"
    ):
        raise ValueError("AGENT_PROVIDER_UNSUPPORTED")
    if not third_party:
        defaults = HISTORICAL_AGENT_DEFAULTS.get(AgentRuntime(agent))
        from awf.adapters.model_selection import selected_runtime_model_for_defaults

        selected_effort = effort if effort is not None else (defaults.effort if defaults else None)
        selected_model = selected_runtime_model_for_defaults(
            agent=AgentRuntime(agent),
            explicit_model=model,
            default_model=defaults.model if defaults else None,
            effort=selected_effort,
        )
        if agent == "opencode":
            from awf.adapters.opencode import opencode_provider_for_model

            inferred_provider = opencode_provider_for_model(selected_model)
            if explicit_provider is not None and explicit_provider != inferred_provider:
                raise ValueError("AGENT_PROVIDER_UNSUPPORTED")
            provider = inferred_provider
        return ExecutionSelection(
            agent=agent,
            provider=provider,
            model=selected_model,
            effort=effort if effort is not None else (defaults.effort if defaults else None),
            effort_explicit=effort is not None,
            model_explicit=model is not None,
            compatibility="legacy" if agent == "opencode" else "native",
        )
    if not model:
        raise ValueError("AGENT_PROVIDER_MODEL_REQUIRED")
    if any(word in model.lower() for word in ("embed", "rerank")):
        raise ValueError("AGENT_PROVIDER_MODEL_INCOMPATIBLE")
    if effort is not None:
        raise ValueError("AGENT_PROVIDER_EFFORT_UNSUPPORTED")
    if not allow_unverified:
        raise ValueError("AGENT_PROVIDER_MODEL_UNVERIFIED")
    profile = profile or {}
    env = profile.get("runtime", {}).get("environment", {})
    environ = environ or {}
    credential_ref = None
    # Inline model-service credentials would enter requested/resolved profile
    # snapshots. Expanded work accepts only declared references, even for
    # competing credentials that the isolated runtime will remove.
    from awf.adapters.provider_config import competing_provider_env

    for key, value in env.items():
        if (
            competing_provider_env(key)
            and key.endswith(("_KEY", "_TOKEN", "_SECRET"))
            and value
            and not _ENV_REF.fullmatch(value)
        ):
            raise ValueError("PROVIDER_CREDENTIAL_REFERENCE_REQUIRED")
    if provider == "openrouter" and managed_openrouter:
        credential_ref = MANAGED_OPENROUTER_CREDENTIAL_REF
        endpoint = "https://openrouter.ai/api"
    elif provider == "openrouter":
        raw_key = env.get("OPENROUTER_API_KEY")
        if raw_key is not None:
            match = _ENV_REF.fullmatch(raw_key)
            if not match:
                raise ValueError("PROVIDER_CREDENTIAL_REFERENCE_REQUIRED")
            credential_ref = "env/" + match[1]
        else:
            credential_ref = "env/OPENROUTER_API_KEY"
            for secret in profile.get("secrets", []):
                if secret.get("kind") == "env" and secret.get("target") == "OPENROUTER_API_KEY":
                    source = str(secret.get("ref") or "").strip().removeprefix("env/")
                    if str(
                        secret.get("provider") or ""
                    ).strip().lower() != "env" or not re.fullmatch(
                        r"[A-Za-z_][A-Za-z0-9_]*", source
                    ):
                        raise ValueError("PROVIDER_CREDENTIAL_REFERENCE_REQUIRED")
                    credential_ref = "env/" + source
        endpoint = "https://openrouter.ai/api"
    else:
        selected = next((env[k] for k in OLLAMA_ENDPOINT_KEYS if k in env), None)
        if selected is None:
            selected = next(
                (environ[k] for k in OLLAMA_ENDPOINT_KEYS if environ.get(k)),
                "http://host.docker.internal:11434",
            )
        if "${" in selected:
            from awf.service.environment import compose_expand_value

            try:
                selected = compose_expand_value(selected, environ=environ)
            except ValueError:
                raise ValueError("PROVIDER_ENDPOINT_INVALID") from None
        endpoint = normalize_endpoint(selected)
    host = urlsplit(endpoint).hostname
    services = profile.get("services", {})
    service_names = (
        set(services) if isinstance(services, Mapping) else {s.get("name") for s in services}
    )
    return ExecutionSelection(
        agent=agent,
        provider=provider,
        model=model,
        effort=None,
        model_explicit=True,
        endpoint=endpoint,
        credential_ref=credential_ref,
        network_service=host if host in service_names else None,
        compatibility="unverified",
    )


def execution_from_policy(policy: object) -> ExecutionSelection | None:
    if not isinstance(policy, Mapping) or EXECUTION_POLICY_KEY not in policy:
        return None
    try:
        raw = policy[EXECUTION_POLICY_KEY]
        if (
            not isinstance(raw, Mapping)
            or type(raw.get("version")) is not int
            or raw.get("version") != EXECUTION_CONTRACT_VERSION
        ):
            raise ValueError
        selection = ExecutionSelection.model_validate(raw)
        if selection.agent not in {*_NATIVE, "opencode"}:
            raise ValueError
        if not selection.third_party:
            if (
                selection.endpoint is not None
                or selection.credential_ref is not None
                or selection.network_service is not None
            ):
                raise ValueError
            if selection.agent == "opencode":
                from awf.adapters.opencode import opencode_provider_for_model

                if selection.provider != opencode_provider_for_model(selection.model):
                    raise ValueError
            elif selection.provider != _NATIVE[selection.agent]:
                raise ValueError
        if selection.third_party and (
            not selection.endpoint
            or not selection.model
            or selection.effort is not None
            or selection.compatibility != "unverified"
        ):
            raise ValueError
        if selection.third_party and selection.provider not in {"ollama", "openrouter"}:
            raise ValueError
        if (
            selection.provider == "openrouter"
            and selection.third_party
            and not selection.credential_ref
        ):
            raise ValueError
        if (
            selection.endpoint
            and selection.provider == "ollama"
            and normalize_endpoint(selection.endpoint) != selection.endpoint
        ):
            raise ValueError
        if (
            selection.third_party
            and selection.provider == "openrouter"
            and selection.endpoint != "https://openrouter.ai/api"
        ):
            raise ValueError
        if selection.managed_key:
            if not (selection.third_party and selection.provider == "openrouter"):
                raise ValueError
        elif selection.credential_ref and not re.fullmatch(
            r"env/[A-Za-z_][A-Za-z0-9_]*", selection.credential_ref
        ):
            raise ValueError
        return selection
    except (ValidationError, ValueError):
        raise ValueError("EXECUTION_CONTRACT_UNSUPPORTED") from None


def managed_openrouter_frozen(policy: object) -> bool:
    """Return the creation-time managed-key decision frozen beside the selection."""
    budget = policy.get(PROVIDER_BUDGET_POLICY_KEY) if isinstance(policy, Mapping) else None
    return isinstance(budget, Mapping) and budget.get("managed") is True


def provider_budget_policy(
    *, limit_usd: float, source: str, requested_usd: float | None = None
) -> dict[str, Any]:
    """The budget frozen beside (never inside) a managed selection."""
    return {
        "version": 1,
        "provider": "openrouter",
        "managed": True,
        "limit_usd": float(limit_usd),
        "requested_usd": requested_usd,
        "source": source,
    }


def require_execution_admission(selection: ExecutionSelection, *, enabled: bool) -> None:
    """Gate new selections; the isolated runtime probes before every invocation."""
    if selection.third_party and not enabled:
        raise ValueError("THIRD_PARTY_AGENT_PROVIDER_ADMISSION_DISABLED")


def circuit_key(
    agent: str, policy: object, *, workspace_id: str | None = None
) -> CircuitKey | None:
    selected = execution_from_policy(policy)
    if (
        isinstance(policy, Mapping)
        and policy.get("agent_execution_profile_pending")
        and selected is None
    ):
        return None
    if selected:
        # Executor defaults can replace a native model before its first launch.
        # A historical placeholder cannot identify the circuit it will execute.
        if selected.model is None or (
            not selected.third_party
            and not selected.model_explicit
            and cast(Mapping[str, Any], policy).get("agent_execution_defaults_pending")
        ):
            return None
        pair = (selected.provider, selected.model)
        return (*pair, selected.connection_scope(workspace_id)) if selected.third_party else pair
    from awf.common.workspace_policy import agent_model_from_task_policy

    model = agent_model_from_task_policy(policy)
    provider = provider_for_agent_model(agent, model)
    return (provider, model) if provider and model else None


def promote_execution(policy: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]:
    """Replace a complete selected fallback, clearing incompatible source policy."""
    selected = execution_from_policy(target)
    if (
        selected is None
        and target.get("agent") in {"codex", "claude_code"}
        and target.get("provider") in {"ollama", "openrouter"}
    ):
        raise ValueError("EXECUTION_FALLBACK_CONTRACT_UNSUPPORTED")
    updated = dict(policy)
    updated.pop("cursor_auto_mode", None)
    updated.pop("agent_effort", None)
    updated.pop(EXECUTION_POLICY_KEY, None)
    updated.pop("coding_compatibility_evidence", None)
    evidence = target.get("coding_compatibility_evidence")
    if selected is not None and evidence is None:
        evidence = next(
            (
                item.get("coding_compatibility_evidence")
                for item in policy.get("provider_recovery", {}).get("fallbacks", [])
                if execution_from_policy(item) == selected
            ),
            None,
        )
    if evidence is not None:
        updated["coding_compatibility_evidence"] = evidence
    updated.pop("agent_provider", None)
    updated["agent_model"] = target["model"]
    if selected:
        updated[EXECUTION_POLICY_KEY] = selected.model_dump(mode="json")
        updated["agent_provider"] = selected.provider
        if selected.effort is not None:
            updated["agent_effort"] = selected.effort
    return updated


def bind_execution_profile(
    policy: Mapping[str, Any],
    *,
    agent: str,
    profile: Mapping[str, Any],
    environ: Mapping[str, str],
) -> dict[str, Any]:
    """Bind deferred connections once the repository profile is authoritative."""
    updated = deepcopy(dict(policy))
    if not updated.pop("agent_execution_profile_pending", False):
        return updated
    targets = [updated, *updated.get("provider_recovery", {}).get("fallbacks", [])]
    for target in targets:
        if EXECUTION_POLICY_KEY in target:
            execution_from_policy(target)
            continue
        primary = target is updated
        selected = resolve_execution(
            agent=agent if primary else target["agent"],
            provider=target.get("agent_provider" if primary else "provider"),
            model=target.get("agent_model" if primary else "model"),
            effort=target.get("agent_effort") if primary else None,
            allow_unverified=bool(
                updated.get("allow_unverified_model")
                or updated.get("require_compatibility_evidence")
            ),
            profile=profile,
            environ=environ,
            managed_openrouter=managed_openrouter_frozen(updated),
        )
        target[EXECUTION_POLICY_KEY] = selected.model_dump(mode="json")
    return updated
