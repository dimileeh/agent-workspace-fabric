"""``run_awf.py`` prints a bot-defer summary after its result header.

If any workspace's artifact JSON has non-empty ``deferred_bot_items``,
the summary section is emitted so the orchestrator launching ``run_awf``
has a machine-readable cue to spec follow-up work. An empty artifact
(every task was clean) must NOT add the section.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from scripts import run_awf


def _write_artifact(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


class TestDeferSummary:
    @pytest.mark.unit
    def test_summary_includes_defer_section_when_bot_items_present(self, tmp_path: Path) -> None:
        artifacts_root = tmp_path / "artifacts"
        _write_artifact(
            artifacts_root / "ws_1.defer-signal.json",
            {
                "workspace_id": "ws_1",
                "pr_number": 342,
                "terminal_action": "Merge",
                "merged": True,
                "deferred_bot_items": [
                    {
                        "kind": "thread",
                        "id": "PRRT_xyz",
                        "author": "greptile-apps",
                        "path": "src/foo.py",
                        "line": 42,
                        "body": "rename foo",
                    }
                ],
                "deferred_human_items": [],
            },
        )
        buf = io.StringIO()
        run_awf.print_defer_summary(artifacts_root=artifacts_root, out=buf)
        out = buf.getvalue()
        assert "DEFERRED BOT FEEDBACK" in out
        assert "ws_1" in out
        assert "greptile-apps" in out
        assert "src/foo.py" in out
        assert "42" in out

    @pytest.mark.unit
    def test_summary_omits_defer_section_when_no_items(self, tmp_path: Path) -> None:
        artifacts_root = tmp_path / "artifacts"
        _write_artifact(
            artifacts_root / "ws_clean.defer-signal.json",
            {
                "workspace_id": "ws_clean",
                "pr_number": 1,
                "terminal_action": "Merge",
                "merged": True,
                "deferred_bot_items": [],
                "deferred_human_items": [],
            },
        )
        buf = io.StringIO()
        run_awf.print_defer_summary(artifacts_root=artifacts_root, out=buf)
        assert buf.getvalue() == ""

    @pytest.mark.unit
    def test_summary_skips_missing_artifacts_root(self, tmp_path: Path) -> None:
        """An orchestrator may run with artifacts disabled / the directory
        never created. Don't crash."""
        buf = io.StringIO()
        run_awf.print_defer_summary(artifacts_root=tmp_path / "does-not-exist", out=buf)
        assert buf.getvalue() == ""

    @pytest.mark.unit
    def test_summary_aggregates_across_multiple_workspaces(self, tmp_path: Path) -> None:
        artifacts_root = tmp_path / "artifacts"
        _write_artifact(
            artifacts_root / "ws_1.defer-signal.json",
            {
                "workspace_id": "ws_1",
                "pr_number": 100,
                "terminal_action": "Merge",
                "merged": True,
                "deferred_bot_items": [
                    {
                        "kind": "thread",
                        "id": "T1",
                        "author": "greptile-apps",
                        "path": "a.py",
                        "line": 1,
                        "body": "",
                    }
                ],
                "deferred_human_items": [],
            },
        )
        _write_artifact(
            artifacts_root / "ws_2.defer-signal.json",
            {
                "workspace_id": "ws_2",
                "pr_number": 101,
                "terminal_action": "Merge",
                "merged": True,
                "deferred_bot_items": [
                    {
                        "kind": "review",
                        "id": "C2",
                        "author": "coderabbitai",
                        "path": None,
                        "line": None,
                        "body": "",
                    }
                ],
                "deferred_human_items": [],
            },
        )
        buf = io.StringIO()
        run_awf.print_defer_summary(artifacts_root=artifacts_root, out=buf)
        out = buf.getvalue()
        assert "ws_1" in out
        assert "ws_2" in out
        assert "greptile-apps" in out
        assert "coderabbitai" in out
