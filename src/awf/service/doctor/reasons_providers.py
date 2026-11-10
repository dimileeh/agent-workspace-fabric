"""Catalog and diagnostic reason vocabulary shared by operator surfaces."""

from __future__ import annotations

from typing import TYPE_CHECKING

from awf.service.doctor.reasons_helpers import reason_catalog_link

if TYPE_CHECKING:
    from awf.service.doctor.reasons import _ReasonText

_MESSAGES = {
    "PROVIDER_DIAGNOSTIC_CONTRACT_UNSUPPORTED": "The diagnostic version, fixture or frozen identity is unsupported.",
    "PROVIDER_DIAGNOSTIC_CLEANUP_FAILED": "Diagnostic teardown failed; the owned fixture is retained.",
    "PROVIDER_SELECTION_INVALID": "The selected agent/provider/profile is invalid.",
    "PROVIDER_DISCOVERY_AVAILABLE": "A bounded provider catalog is available; capabilities are provider claims.",
    "PROVIDER_DISCOVERY_UNAVAILABLE": "Catalog discovery failed; manual model entry remains available.",
    "PROVIDER_DISCOVERY_UNSUPPORTED": "This provider does not expose supported catalog discovery.",
    "PROVIDER_DISCOVERY_NETWORK_UNSUPPORTED": "Catalog discovery cannot reproduce the selected sidecar network.",
    "PROVIDER_DISCOVERY_RESPONSE_INVALID": "Catalog shape or response bounds are invalid.",
    "EXECUTION_NETWORK_PROBE_PENDING": "Actual runtime-network readiness is deferred until invocation.",
    "EXECUTION_PROFILE_BINDING_REQUIRED": "The selected provider connection must be bound before execution.",
    "AGENT_PROVIDER_MODEL_UNVERIFIED": "No current exact coding-cycle proof matches the selected connection/runtime.",
    "PROVIDER_DIAGNOSTIC_PROFILE_UNSUPPORTED": "The selected model sidecar depends on project resources or credentials.",
    "PROVIDER_DIAGNOSTIC_HOSTED_UNSUPPORTED": "Hosted workers cannot enforce the diagnostic contract.",
    "PROVIDER_DIAGNOSTIC_WORKER_UNSUPPORTED": "All live queue consumers must advertise diagnostic worker capability 2.",
    "PROVIDER_DIAGNOSTIC_RUNTIME_UNSUPPORTED": "The runtime lacks the immutable diagnostic fixture capability.",
    "PROVIDER_DIAGNOSTIC_READY": "The isolated fixture workspace is ready.",
    "PROVIDER_DIAGNOSTIC_VERIFYING": "AWF is independently verifying the fixed fixture.",
    "PROVIDER_DIAGNOSTIC_PASSED": "Ordered dependent CLI tool turns and independent fixture verification passed.",
    "PROVIDER_DIAGNOSTIC_PROTOCOL_PASSED": "The CLI protocol contains two completed dependent tool turns.",
    "PROVIDER_DIAGNOSTIC_EVIDENCE_MISSING": "Required typed tool evidence is missing, malformed or truncated; result is inconclusive.",
    "PROVIDER_DIAGNOSTIC_FIXTURE_FAILED": "The fixture does not satisfy the trusted nonce-dependent oracle.",
    "PROVIDER_DIAGNOSTIC_BOOTSTRAP_FAILED": "The isolated diagnostic runtime could not be provisioned.",
    "PROVIDER_DIAGNOSTIC_EXECUTION_FAILED": "The one-shot diagnostic invocation failed; no replay is scheduled.",
    "PROVIDER_DIAGNOSTIC_TIMEOUT": "The bounded diagnostic deadline expired.",
    "PROVIDER_DIAGNOSTIC_INTERRUPTED": "Worker ownership was lost; the diagnostic is inconclusive and cannot replay.",
    "PROVIDER_DIAGNOSTIC_REPLAY_FORBIDDEN": "This control could replay diagnostic work; create a new connection test instead.",
}


def get_provider_reasons(reason_text_cls: type[_ReasonText]) -> dict[str, _ReasonText]:
    return {
        code: reason_text_cls(
            message,
            "Inspect the diagnostic workspace logs, operation result and selected connection. Keep ordinary admission disabled until all live gates pass. Manual catalog entry still follows ordinary admission policy.",
            "Discovery, coding evidence, runtime capability and endpoint health are separate observations.",
            "awf workspace show <workspace_id>",
            reason_catalog_link(code),
        )
        for code, message in _MESSAGES.items()
    }
