"""Shared worker node identity resolution."""

from __future__ import annotations

from awf.common.config import Settings
from awf.service.config import DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID, ServiceSettings


def _configured_node_id(value: str | None) -> str:
    return value.strip() if value else ""


def effective_worker_node_id(settings: Settings) -> str:
    """Return the node id used for create-time admission and reservations."""
    return _configured_node_id(settings.worker_node_id) or DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID


def effective_service_node_id(settings: ServiceSettings) -> str:
    """Return the node id used by the local worker/provisioner runtime."""
    return _configured_node_id(settings.node_id) or DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID
