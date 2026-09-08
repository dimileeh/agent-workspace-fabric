"""Antigravity agy 1.1.27 model/effort regressions."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from awf.adapters.antigravity import (
    ANTIGRAVITY_API_KEY_MODE_MODELS,
    AntigravityAdapter,
)
from awf.common.commands import FakeCommandRunner


def _render_cli_script(*, model: str, effort: str | None) -> str:
    adapter = AntigravityAdapter(
        runner=FakeCommandRunner(),
        default_model=model,
        default_effort=effort,
    )
    args = adapter._cli_args(model=model)
    assert args[:2] == ["sh", "-lc"]
    return args[2]


async def _run_with_fake_agy(
    tmp_path: Path,
    *,
    model: str,
    effort: str | None,
    api_key_mode: bool,
) -> tuple[list[str] | None, str, int]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_copy = tmp_path / "argv.json"
    fake_agy = bin_dir / "agy"
    fake_agy.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['AWF_FAKE_AGY_ARGV'], 'w', encoding='utf-8') as fh:\n"
        "    json.dump(sys.argv[1:], fh)\n",
        encoding="utf-8",
    )
    fake_agy.chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env.pop("GEMINI_API_KEY", None)
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "HOME": str(home),
            "AWF_FAKE_AGY_ARGV": str(argv_copy),
        }
    )
    if api_key_mode:
        env["GEMINI_API_KEY"] = "test-key-selects-api-key-mode"

    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        _render_cli_script(model=model, effort=effort),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    _stdout, stderr = await proc.communicate(input=b"prompt\n")
    assert proc.returncode is not None
    captured = json.loads(argv_copy.read_text()) if argv_copy.exists() else None
    return captured, stderr.decode(), proc.returncode


@pytest.mark.unit
def test_api_key_model_effort_allowlist_matches_agy_1_1_27() -> None:
    assert {
        "gemini-3.8-flash": frozenset({"low", "medium", "high"}),
        "gemini-3.7-flash": frozenset({"low", "medium", "high"}),
        "gemini-3.6-flash": frozenset({"low", "medium", "high"}),
        "gemini-3.1-pro": frozenset({"low", "high"}),
    } == ANTIGRAVITY_API_KEY_MODE_MODELS


@pytest.mark.unit
@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        ("low", "low"),
        ("MeDiUm", "medium"),
        ("high", "high"),
        ("xhigh", "high"),
        ("max", "high"),
        (None, "high"),
        ("future-effort", "high"),
    ],
)
async def test_api_key_flash_effort_is_normalized_and_clamped(
    tmp_path: Path,
    effort: str | None,
    expected: str,
) -> None:
    captured, stderr, returncode = await _run_with_fake_agy(
        tmp_path,
        model="gemini-3.8-flash",
        effort=effort,
        api_key_mode=True,
    )

    assert returncode == 0, stderr
    assert captured is not None
    assert captured[captured.index("--model") + 1] == "gemini-3.8-flash"
    assert captured.count("--effort") == 1
    assert captured[captured.index("--effort") + 1] == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        ("low", "low"),
        ("medium", "high"),
        ("high", "high"),
        ("xhigh", "high"),
        ("max", "high"),
    ],
)
async def test_api_key_pro_effort_clamps_to_supported_strength(
    tmp_path: Path,
    effort: str,
    expected: str,
) -> None:
    captured, stderr, returncode = await _run_with_fake_agy(
        tmp_path,
        model="gemini-3.1-pro",
        effort=effort,
        api_key_mode=True,
    )

    assert returncode == 0, stderr
    assert captured is not None
    assert captured.count("--effort") == 1
    assert captured[captured.index("--effort") + 1] == expected


@pytest.mark.unit
async def test_oauth_composite_slug_omits_separate_effort(tmp_path: Path) -> None:
    captured, stderr, returncode = await _run_with_fake_agy(
        tmp_path,
        model="gemini-3.6-flash-high",
        effort="xhigh",
        api_key_mode=False,
    )

    assert returncode == 0, stderr
    assert captured is not None
    assert captured[captured.index("--model") + 1] == "gemini-3.6-flash-high"
    assert "--effort" not in captured


@pytest.mark.unit
@pytest.mark.parametrize("model", ["gemini-3.1-pro-preview", "gemini-3.5-flash"])
async def test_removed_models_reject_in_api_key_mode(
    tmp_path: Path,
    model: str,
) -> None:
    captured, stderr, returncode = await _run_with_fake_agy(
        tmp_path,
        model=model,
        effort="high",
        api_key_mode=True,
    )

    assert returncode == 1
    assert captured is None
    assert model in stderr
    for slug in ANTIGRAVITY_API_KEY_MODE_MODELS:
        assert slug in stderr
