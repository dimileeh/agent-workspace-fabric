"""Validation run payload helpers shared across persistence and API surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def validation_run_coverage_payload(run: object) -> dict[str, Any]:
    """Return structured coverage metadata, preferring the dedicated column.

    Older rows stored coverage under ``log_stream_refs.coverage``. Keep that as
    a read fallback so durable provenance remains readable across migrations.
    """

    coverage = getattr(run, "coverage", None)
    if isinstance(coverage, Mapping):
        return _json_dict(coverage)

    log_stream_refs = getattr(run, "log_stream_refs", None)
    if isinstance(log_stream_refs, Mapping):
        return _json_dict(log_stream_refs.get("coverage"))
    return {}


def _json_dict(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(k): v for k, v in value.items()}
    return {}
