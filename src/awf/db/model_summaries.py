"""Queue and reservation projections used by workspace ORM properties."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from awf.db.models import QueueDecision, ResourceReservation


def _queue_decision_summary(decision: QueueDecision) -> dict[str, Any]:
    return {
        "id": decision.id,
        "decision": decision.decision,
        "reason_code": decision.reason_code,
        "class_priority": decision.class_priority,
        "computed_priority": decision.computed_priority,
        "age_boost": decision.age_boost,
        "retry_bonus": decision.retry_bonus,
        "resource_summary": decision.resource_summary,
        "overlap_risk_summary": decision.overlap_risk_summary,
        "score_summary": decision.score_summary,
        "decided_at": decision.decided_at,
    }


def _resource_reservation_summary(reservation: ResourceReservation) -> dict[str, Any]:
    return {
        "id": reservation.id,
        "node_id": reservation.node_id,
        "steady_cpu": reservation.steady_cpu,
        "steady_memory_gb": reservation.steady_memory_gb,
        "peak_cpu": reservation.peak_cpu,
        "peak_memory_gb": reservation.peak_memory_gb,
        "disk_mb": reservation.disk_mb,
        "dind_slots": reservation.dind_slots,
        "phase": reservation.phase,
        "reserved_at": reservation.reserved_at,
        "released_at": reservation.released_at,
    }
