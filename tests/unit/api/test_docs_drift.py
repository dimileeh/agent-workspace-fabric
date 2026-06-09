"""Docs drift regression tests.

These tests verify that REST_API_REFERENCE.md stays in sync with the OpenAPI
spec by checking:
1. The docs mention all top-level API path prefixes from the spec.
2. Each curl URL path in the docs exists in the current spec.
3. The checked-in openapi.json matches the spec generated from the current app.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from awf.api.app import create_app

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DOCS_PATH = REPO_ROOT / "docs" / "REST_API_REFERENCE.md"
OPENAPI_JSON_PATH = REPO_ROOT / "openapi.json"


@pytest.fixture(scope="module")
def openapi_spec() -> dict:
    app = create_app(use_lifespan=False)
    return app.openapi()


@pytest.fixture(scope="module")
def docs_content() -> str:
    return DOCS_PATH.read_text()


@pytest.mark.unit
def test_docs_mention_all_top_level_api_path_prefixes(
    openapi_spec: dict,
    docs_content: str,
) -> None:
    spec_paths = set(openapi_spec.get("paths", {}).keys())
    top_level_prefixes: set[str] = set()
    for path in spec_paths:
        parts = path.split("/")
        if len(parts) >= 3:
            prefix = "/".join(parts[:3])
            if "{" not in prefix:
                top_level_prefixes.add(prefix)
        elif len(parts) == 2 and parts[1]:
            top_level_prefixes.add(path)
    missing: list[str] = []
    for prefix in sorted(top_level_prefixes):
        if prefix not in docs_content:
            missing.append(prefix)
    assert not missing, (
        f"REST_API_REFERENCE.md is missing these API path prefixes: {missing}. "
        "Add curl examples or path mentions for these endpoints."
    )


@pytest.mark.unit
def test_curl_paths_exist_in_openapi_spec(
    openapi_spec: dict,
    docs_content: str,
) -> None:
    spec_paths = set(openapi_spec.get("paths", {}).keys())
    collapsed = re.sub(r"\\\n\s*", " ", docs_content)
    curl_pattern = re.compile(r"curl\s.*?(?:https?://[^/]+)?(/[^\s'\"]+)")
    matches = curl_pattern.findall(collapsed)
    doc_paths: set[str] = set()
    for match in matches:
        path = match.split("?")[0]
        path = re.sub(r"/[\w-]+_id(?=[/)]|\s|$)", "/{workspace_id}", path)
        path = re.sub(r"/\d+(?=[/)]|\s|$)", "/{id}", path)
        path = re.sub(r"/ws_\w+", "/{workspace_id}", path)
        path = re.sub(r"/op_\w+", "/{operation_id}", path)
        path = re.sub(r"/ls_[\w-]+", "/{stream_id}", path)
        path = re.sub(r"/task_[\w-]+", "/{task_ref}", path)
        path = path.rstrip("/")
        doc_paths.add(path)
    missing: list[str] = []
    for doc_path in sorted(doc_paths):
        if doc_path in spec_paths:
            continue
        templated = _try_template(doc_path, spec_paths)
        if templated is None:
            missing.append(doc_path)
    assert not missing, (
        f"curl URLs in docs reference paths not in the OpenAPI spec: {missing}. "
        "Either update the docs or add the missing endpoint."
    )


def _try_template(doc_path: str, spec_paths: set[str]) -> str | None:
    parts = doc_path.split("/")
    for spec_path in spec_paths:
        spec_parts = spec_path.split("/")
        if len(parts) != len(spec_parts):
            continue
        match = True
        for dp, sp in zip(parts, spec_parts, strict=True):
            if dp == sp:
                continue
            if sp.startswith("{") and sp.endswith("}"):
                continue
            match = False
            break
        if match:
            return spec_path
    return None


@pytest.mark.unit
def test_openapi_json_consistent_with_current_app(openapi_spec: dict) -> None:
    assert OPENAPI_JSON_PATH.exists(), (
        "Checked-in openapi.json is missing. Run: python scripts/generate_openapi.py"
    )
    checked_in = json.loads(OPENAPI_JSON_PATH.read_text())
    current_serialized = json.dumps(openapi_spec, sort_keys=True)
    checked_in_serialized = json.dumps(checked_in, sort_keys=True)
    assert current_serialized == checked_in_serialized, (
        "Checked-in openapi.json has drifted from the current app's OpenAPI spec. "
        "Run: python scripts/generate_openapi.py"
    )
