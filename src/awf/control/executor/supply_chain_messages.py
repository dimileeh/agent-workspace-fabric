"""Supply-chain policy message rendering for executor failures."""

from __future__ import annotations

from collections.abc import Sequence

from awf.service.supply_chain_policy import SupplyChainFinding


def _supply_chain_block_message(findings: Sequence[SupplyChainFinding]) -> str:
    blocking = [finding for finding in findings if finding.severity == "blocking"]
    if not blocking:
        return "Supply-chain policy blocked workspace output."
    lines = ["Supply-chain policy blocked workspace output:"]
    for finding in blocking[:5]:
        guidance = (
            finding.details.get("recovery_guidance") if isinstance(finding.details, dict) else None
        )
        subject = f" ({finding.subject_path})" if finding.subject_path else ""
        lines.append(f"- {finding.reason_code}{subject}: {finding.explanation}")
        if isinstance(guidance, str) and guidance:
            lines.append(f"  Recovery: {guidance}")
    if len(blocking) > 5:
        lines.append(f"- {len(blocking) - 5} additional blocking finding(s).")
    return "\n".join(lines)
