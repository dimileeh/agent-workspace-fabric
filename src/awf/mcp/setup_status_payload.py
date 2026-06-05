"""Safe setup-status payload projection helpers for MCP setup tools."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from awf.host_setup.config import ClientIntegrationConfig, HostSetupConfig, ProviderConfig


def _provider_statuses(providers: Mapping[str, ProviderConfig]) -> dict[str, dict[str, Any]]:
    return {name: _provider_status(provider) for name, provider in providers.items()}


def _provider_status(provider: ProviderConfig) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": provider.status}
    if provider.backend is not None:
        payload["backend"] = provider.backend
    if provider.source is not None:
        payload["source"] = provider.source
    payload["credential_ref"] = _credential_ref_metadata(provider.credential_ref)
    return payload


def _credential_ref_metadata(credential_ref: str | None) -> dict[str, Any]:
    if credential_ref is None:
        return {"present": False}
    scheme, separator, _rest = credential_ref.partition("://")
    payload: dict[str, Any] = {"present": True}
    if separator:
        payload["scheme"] = scheme
    return payload


def _client_statuses(clients: Mapping[str, ClientIntegrationConfig]) -> dict[str, dict[str, Any]]:
    statuses: dict[str, dict[str, Any]] = {}
    for name, client in clients.items():
        payload: dict[str, Any] = {"status": client.status}
        if client.updated_at is not None:
            payload["updated_at"] = client.updated_at.isoformat()
        statuses[name] = payload
    return statuses


def _source_checkout_status(config: HostSetupConfig) -> dict[str, Any]:
    if config.source_checkout is None:
        return {"present": False}
    return {
        "present": True,
        "root": str(config.source_checkout.root),
        "verified_at": config.source_checkout.verified_at.isoformat(),
        "marker_count": len(config.source_checkout.markers),
    }


def _setup_status_source_checkout(
    config: HostSetupConfig,
    details: Mapping[str, Any],
    issues: Any,
    *,
    prefer_probed: bool = False,
) -> dict[str, Any]:
    if _has_blocking_source_checkout_issue(issues):
        return {"present": False}

    probed = _probed_source_checkout_status(details)
    if prefer_probed:
        return probed

    persisted = _source_checkout_status(config)
    if persisted["present"]:
        return persisted

    return probed


def _probed_source_checkout_status(details: Mapping[str, Any]) -> dict[str, Any]:
    probed = _mapping(details.get("source_checkout"))
    root = probed.get("root")
    verified_at = probed.get("verified_at")
    if not isinstance(root, str) or not isinstance(verified_at, str):
        return {"present": False}

    payload: dict[str, Any] = {
        "present": True,
        "root": root,
        "verified_at": verified_at,
        # Dry-run probe metadata verifies the checkout but does not enumerate markers.
        "marker_count": None,
    }
    return payload


def _has_blocking_source_checkout_issue(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        item_mapping = _mapping(item)
        severity = item_mapping.get("severity")
        if severity not in ("blocked", "failed"):
            continue
        details = _mapping(item_mapping.get("details"))
        if details.get("check") == "source_checkout":
            return True
    return False


def _safe_setup_checks(value: Any) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    if not isinstance(value, list):
        return checks
    for item in value:
        item_mapping = _mapping(item)
        name = item_mapping.get("name")
        level = item_mapping.get("level")
        if isinstance(name, str) and isinstance(level, str):
            checks.append({"name": name, "level": level})
    return checks


def _setup_status_issues(value: Any) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return issues
    for item in value:
        item_mapping = _mapping(item)
        reason_code = item_mapping.get("reason_code")
        severity = item_mapping.get("severity")
        if not isinstance(reason_code, str) or not isinstance(severity, str):
            continue
        rendered: dict[str, Any] = {"reason_code": reason_code, "severity": severity}
        details = _mapping(item_mapping.get("details"))
        check = details.get("check")
        if isinstance(check, str):
            rendered["check"] = check
        issues.append(rendered)
    return issues


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, Any], value)
    return {}


def _list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
