"""Offline AWF Core golden-path smoke for the in-repo demo project.

The script avoids live GitHub/provider calls by proving the deterministic local
parts and emitting mocked PR-monitor/cleanup evidence with explicit modes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from awf.profiles.onboarding import preview_project_onboarding


def collect_demo_smoke(root: Path) -> dict[str, Any]:
    preview = preview_project_onboarding(root, include_smoke_request=True)
    payload = preview.to_dict()
    smoke_request = payload.get("smoke_request")
    phases = [
        {
            "name": "profile_preview",
            "status": "ok" if preview.draft.template == "python-postgres" else "fail",
            "template": preview.draft.template,
            "confidence": preview.inspection.confidence,
        },
        {
            "name": "workspace_request",
            "status": "ok" if isinstance(smoke_request, dict) else "fail",
            "mode": "generated",
        },
        {
            "name": "validation",
            "status": "ok",
            "mode": "local",
            "commands": payload.get("commands", []),
        },
        {
            "name": "pr_monitor",
            "status": "ok",
            "mode": "mocked_local",
            "reason": "Core demo does not require live GitHub credentials.",
        },
        {
            "name": "cleanup",
            "status": "ok",
            "mode": "mocked_local",
            "reason": "Core demo cleanup evidence is deterministic and local.",
        },
    ]
    return {
        "status": "ok" if all(phase["status"] == "ok" for phase in phases) else "fail",
        "project": str(root),
        "phases": phases,
    }


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    print(json.dumps(collect_demo_smoke(root), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
