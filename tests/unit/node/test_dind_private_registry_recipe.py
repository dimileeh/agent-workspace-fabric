"""Validate the DinD private-registry auth recipe sample profile.

The recipe doc (``docs/recipes/dind-private-registry-auth.md``) ships a
copy-pasteable ``.awf/workspace.yml`` whose canonical, parseable form lives at
``docs/recipes/examples/dind-private-registry/.awf/workspace.yml``. These tests
keep that sample honest: it must parse as a ``WorkspaceProfile`` and resolve the
declared ``local-file`` lease into the exact read-only Docker config mount the
doc promises, with ``DOCKER_CONFIG`` pointing at the mount's directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.node.compose_manager import AuthMount
from awf.node.secret_mounts import LocalSecretLeaseMountResolver
from awf.profiles.models import DockerMode, WorkspaceProfile

_RECIPE_PROFILE = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "recipes"
    / "examples"
    / "dind-private-registry"
    / ".awf"
    / "workspace.yml"
)


def _load_profile() -> WorkspaceProfile:
    raw = yaml.safe_load(_RECIPE_PROFILE.read_text(encoding="utf-8"))
    return WorkspaceProfile.model_validate(raw)


@pytest.mark.unit
def test_recipe_profile_parses_with_dind_and_docker_config_env() -> None:
    profile = _load_profile()

    assert profile.docker.mode == DockerMode.dind
    # DOCKER_CONFIG must point at the directory that contains config.json.
    assert profile.runtime.environment["DOCKER_CONFIG"] == "/run/awf/secrets/docker"

    (secret,) = profile.secrets
    assert secret.provider == "local-file"
    assert secret.kind == "mount"
    assert secret.mode == "ro"
    assert secret.target == "/run/awf/secrets/docker/config.json"
    # DOCKER_CONFIG (the directory) is the parent of the mount target (the file).
    assert secret.target == f"{profile.runtime.environment['DOCKER_CONFIG']}/config.json"


@pytest.mark.unit
def test_recipe_profile_resolves_config_json_into_readonly_mount(tmp_path: Path) -> None:
    profile = _load_profile()
    (declared,) = profile.secrets

    # The shipped ``ref`` is a placeholder host path; repoint it at a real temp
    # file so the resolver's existence check passes deterministically without
    # depending on a developer's ~/.docker/config.json.
    config_json = tmp_path / ".docker" / "config.json"
    config_json.parent.mkdir(parents=True)
    config_json.write_text('{"auths": {}}', encoding="utf-8")
    resolvable = profile.model_copy(
        update={"secrets": [declared.model_copy(update={"ref": str(config_json)})]}
    )

    resolver = LocalSecretLeaseMountResolver(
        host_home=tmp_path / "host-home",
        work_dir=tmp_path / "work",
        host_env={},
    )
    resolution = resolver.resolve(resolvable, workspace_id="ws_dind_registry")

    assert resolution.mounts == (
        AuthMount(
            source=str(config_json),
            target="/run/awf/secrets/docker/config.json",
            mode="ro",
        ),
    )
