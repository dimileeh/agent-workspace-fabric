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


def _env_value(path: Path, key: str) -> str | None:
    prefix = f"{key}="
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix)
    return None
