"""Owned-path reservation CLI tests."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from awf.cli.main import app

_runner = CliRunner()


def _mock_response(*, status_code: int = 200, payload: object = None) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.content = b"ok" if payload is not None else b""
    response.text = json.dumps(payload) if payload is not None else ""
    response.json.return_value = payload
    return response


class TestLocksList:
    @pytest.mark.unit
    def test_fetches_locks_with_filters_and_json_default(self) -> None:
        payload = {
            "items": [
                {
                    "workspace_id": "ws_lock",
                    "title": "Lock visibility",
                    "agent": "codex",
                    "status": "running",
                    "repo_url": "git@github.com:example/app.git",
                    "branch_base": "main",
                    "task_class": "test_task",
                    "owned_paths": ["tests/unit/**"],
                    "pr_url": None,
                    "created_at": "2026-04-26T12:00:00Z",
                    "updated_at": "2026-04-26T12:05:00Z",
                }
            ],
            "next_cursor": None,
            "has_more": False,
        }
        response = _mock_response(payload=payload)
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "locks",
                    "list",
                    "--repo-url",
                    "git@github.com:example/app.git",
                    "--task-class",
                    "test_task",
                    "--status",
                    "running",
                    "--limit",
                    "25",
                    "--base-url",
                    "http://awf.local",
                ],
            )

        assert result.exit_code == 0
        assert json.loads(result.stdout) == payload
        assert mock.call_args[0] == ("GET", "http://awf.local/v1/locks")
        assert mock.call_args.kwargs["params"] == {
            "limit": 25,
            "repo_url": "git@github.com:example/app.git",
            "task_class": "test_task",
            "status": "running",
        }

    @pytest.mark.unit
    def test_pretty_format_prints_one_lock_per_block(self) -> None:
        payload = {
            "items": [
                {
                    "workspace_id": "ws_1",
                    "title": "First lock",
                    "status": "ready",
                },
                {
                    "workspace_id": "ws_2",
                    "title": "Second lock",
                    "status": "monitoring_pr",
                },
            ],
            "next_cursor": None,
            "has_more": False,
        }
        response = _mock_response(payload=payload)
        with patch("awf.cli.main.httpx.request", return_value=response):
            result = _runner.invoke(app, ["locks", "list", "--format", "pretty"])

        assert result.exit_code == 0
        assert "--- #1 ---" in result.stdout
        assert "--- #2 ---" in result.stdout
        assert "workspace_id: ws_1" in result.stdout
        assert "workspace_id: ws_2" in result.stdout
