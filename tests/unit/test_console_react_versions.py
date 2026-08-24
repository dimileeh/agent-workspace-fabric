"""Regression tests for the console's React version lockstep requirement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_JSON_PATH = REPO_ROOT / "apps" / "console" / "package.json"
PACKAGE_LOCK_PATH = REPO_ROOT / "apps" / "console" / "package-lock.json"


@pytest.mark.unit
def test_console_declares_matching_react_and_react_dom_versions() -> None:
    package_json = json.loads(PACKAGE_JSON_PATH.read_text(encoding="utf-8"))
    dependencies = package_json["dependencies"]

    assert dependencies["react"] == dependencies["react-dom"], (
        f"react={dependencies['react']} react-dom={dependencies['react-dom']}"
    )


@pytest.mark.unit
def test_console_resolves_matching_react_and_react_dom_versions() -> None:
    package_lock = json.loads(PACKAGE_LOCK_PATH.read_text(encoding="utf-8"))
    packages = package_lock["packages"]
    react_version = packages["node_modules/react"]["version"]
    react_dom_version = packages["node_modules/react-dom"]["version"]

    assert react_version == react_dom_version, (
        f"react={react_version} react-dom={react_dom_version}"
    )
