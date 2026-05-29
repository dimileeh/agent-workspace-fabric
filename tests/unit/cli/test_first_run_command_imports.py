"""Regression coverage for first-run command import safety."""

from __future__ import annotations

import importlib
from typing import NoReturn

import pytest

import awf.host_setup.rendering as rendering


def _fail_if_payload_built_at_import(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("placeholder payload was constructed during module import")


@pytest.mark.unit
@pytest.mark.parametrize(
    "module_name",
    (
        "awf.cli.setup_commands",
        "awf.cli.start_commands",
    ),
)
def test_first_run_placeholders_are_not_built_at_import(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
) -> None:
    module = importlib.import_module(module_name)

    try:
        with monkeypatch.context() as patch_context:
            patch_context.setattr(
                rendering,
                "first_run_failure_payload",
                _fail_if_payload_built_at_import,
            )
            importlib.reload(module)
    finally:
        importlib.reload(module)
