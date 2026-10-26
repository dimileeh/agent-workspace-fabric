"""Schema and create-hash contract for optional repo.expected_base_sha."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from awf.api.routes.workspaces import _workspace_create_request_hash
from awf.api.schemas import WorkspaceCreateRequest, WorkspaceRepo

_SHA = "a" * 40
_OTHER = "b" * 40
_BODY = {
    "repo": {"url": "git@github.com:example/app.git", "base_branch": "development"},
    "task": {
        "title": "Pin the base",
        "prompt": "Implement the pinned task.",
        "agent": "codex",
        "kind": "feature_branch_pr",
    },
    "workspace": {"profile_ref": "auto", "profile": None},
    "validation": {"commands": ["pytest -q"], "requested_tier": 1},
    "resources": {},
}


def _request(repo: dict[str, object]) -> WorkspaceCreateRequest:
    body = {**_BODY, "repo": repo}
    return WorkspaceCreateRequest.model_validate(body)


def _pre_field_digest(payload: WorkspaceCreateRequest) -> str:
    """Hash a dump that does not contain the pin key, matching pre-field bodies."""
    dumped = payload.model_dump(mode="json", by_alias=True)
    dumped["repo"].pop("expected_base_sha", None)
    assert "expected_base_sha" not in dumped["repo"]
    assert dumped["task"]["model"] is None
    normalized = {"api_version": "v1", "payload": dumped}
    serialized = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode()).hexdigest()


@pytest.mark.unit
def test_workspace_repo_accepts_full_sha_and_defaults_to_none() -> None:
    omitted = WorkspaceRepo.model_validate({"url": "git@github.com:example/app.git"})
    assert omitted.expected_base_sha is None
    lowered = WorkspaceRepo.model_validate(
        {
            "url": "git@github.com:example/app.git",
            "expected_base_sha": _SHA.upper(),
        }
    )
    padded = WorkspaceRepo.model_validate(
        {
            "url": "git@github.com:example/app.git",
            "expected_base_sha": f"  {_SHA.upper()}  ",
        }
    )
    assert lowered.expected_base_sha == _SHA
    assert padded.expected_base_sha == _SHA


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    ["a" * 39, "g" * 40, "development", "v1.2.3", "   ", ""],
)
def test_workspace_repo_rejects_non_commit_pins(value: str) -> None:
    with pytest.raises(ValidationError):
        WorkspaceRepo.model_validate(
            {"url": "git@github.com:example/app.git", "expected_base_sha": value}
        )


@pytest.mark.unit
def test_unpinned_create_hash_matches_pre_field_digest_and_pins_differ() -> None:
    omitted = _request({"url": "git@github.com:example/app.git", "base_branch": "development"})
    explicit_null = _request(
        {
            "url": "git@github.com:example/app.git",
            "base_branch": "development",
            "expected_base_sha": None,
        }
    )
    pinned = _request(
        {
            "url": "git@github.com:example/app.git",
            "base_branch": "development",
            "expected_base_sha": _SHA,
        }
    )
    other = _request(
        {
            "url": "git@github.com:example/app.git",
            "base_branch": "development",
            "expected_base_sha": _OTHER,
        }
    )
    upper = _request(
        {
            "url": "git@github.com:example/app.git",
            "base_branch": "development",
            "expected_base_sha": _SHA.upper(),
        }
    )
    legacy = _pre_field_digest(omitted)
    omitted_hash = _workspace_create_request_hash(omitted, api_version="v1")
    assert omitted_hash == legacy
    assert _workspace_create_request_hash(explicit_null, api_version="v1") == omitted_hash
    assert _workspace_create_request_hash(pinned, api_version="v1") != omitted_hash
    assert _workspace_create_request_hash(
        other, api_version="v1"
    ) != _workspace_create_request_hash(pinned, api_version="v1")
    assert _workspace_create_request_hash(
        upper, api_version="v1"
    ) == _workspace_create_request_hash(pinned, api_version="v1")
