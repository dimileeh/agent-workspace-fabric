"""Tests for AWF Reason Catalog synchronization."""

import os
from pathlib import Path

import pytest

from awf.service.doctor.reasons import _REASON_TEXT


def generate_expected_catalog() -> str:
    lines = [
        "# AWF Reason and Error Code Catalog\n",
        "This catalog documents common API/CLI/MCP failures, likely causes, and operator fixes.\n"
    ]
    reasons = {k: v for k, v in _REASON_TEXT.items() if v.likely_cause and v.action and v.message}
    for key in sorted(reasons.keys()):
        val = reasons[key]
        lines.append(f"### {key}")
        lines.append(f"**Problem:** {val.message}")
        lines.append(f"**Likely Cause:** {val.likely_cause}")
        lines.append(f"**Operator Fix:** {val.action}")
        if val.related_command:
            lines.append(f"**Related Command:** `{val.related_command}`")
        if val.docs_link:
            lines.append(f"**Docs Link:** [{val.docs_link}]({val.docs_link})")
        lines.append("")
    return "\n".join(lines)


@pytest.mark.unit
def test_reason_catalog_is_synchronized_with_python_source() -> None:
    repo_root = Path(__file__).parent.parent.parent.parent
    catalog_path = repo_root / "docs" / "REASON_CATALOG.md"
    
    expected_content = generate_expected_catalog()
    actual_content = catalog_path.read_text()
    
    assert actual_content.strip() == expected_content.strip(), (
        "docs/REASON_CATALOG.md is out of sync with src/awf/service/doctor/reasons.py. "
        "Please run `uv run python scripts/generate_reason_catalog.py` to update it."
    )
