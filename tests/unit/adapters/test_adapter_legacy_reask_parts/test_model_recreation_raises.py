"""Legacy clarification model-recreation rollback regression."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from awf.adapters.opencode import OpenCodeAdapter
from awf.common.commands import CommandResult, FakeCommandRunner
from tests.unit.adapters.test_adapter_legacy_reask import _PROMPT


@pytest.mark.unit
async def test_isolated_reask_rolls_back_legacy_migration_when_model_recreation_raises(
    tmp_path: Path,
) -> None:
    """Runner failures cannot leave a legacy sidecar off the model network."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "ollama-sidecar": {
                        "image": "ollama/ollama:latest",
                        "networks": ["awf_net"],
                    },
                    "agent": {
                        "image": "awf-agent-runtime:latest",
                        "environment": {
                            "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama-sidecar:11434"
                        },
                        "networks": ["awf_net"],
                    },
                },
                "networks": {"awf_net": {"name": "awf-ws_legacy-net"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    original_compose_file = compose_file.read_bytes()

    class _FailingSidecarUpdateRunner(FakeCommandRunner):
        """Test double used by the surrounding scenario."""

        async def run(self, args: list[str], **kwargs: Any) -> CommandResult:
            """Run this test double and record the invocation."""
            await super().run(args, **kwargs)
            raise FileNotFoundError("docker not found")

    runner = _FailingSidecarUpdateRunner()
    adapter = OpenCodeAdapter(runner=runner)

    with pytest.raises(FileNotFoundError, match="docker not found"):
        await adapter.run(
            compose_project="awf_ws_legacy",
            compose_file=compose_file,
            prompt=_PROMPT,
            model="ollama/kimi-k2.6:cloud",
            workspace_id="ws_legacy",
            isolated_worktree_host_path=tmp_path / "reask",
        )

    assert len(runner.calls) == 1
    assert compose_file.read_bytes() == original_compose_file
