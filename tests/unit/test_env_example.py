from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "template_path",
    [
        Path(".env.example"),
        Path("apps/console/.env.example"),
    ],
)
def test_env_templates_require_operator_api_token(template_path: Path) -> None:
    """The shared template must not ship a predictable bearer token."""

    token_value = _env_value(template_path, "AWF_API_TOKEN")

    assert token_value == ""


def test_root_env_template_seeds_orphan_cleanup_enabled() -> None:
    """First-run copies from .env.example must preserve the runtime default."""

    assert _env_value(Path(".env.example"), "AWF_AUTO_CLEANUP_ORPHANS") == "true"


def _env_value(path: Path, key: str) -> str | None:
    prefix = f"{key}="
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix)
    return None
