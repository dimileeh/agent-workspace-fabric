"""Agent-container Bitbucket git-over-HTTPS auth: rendered/launched boundary.

These tests cover the agent path added for issues #465/#466 (the worker path is
unchanged). The mechanism is leak-proof by construction: a static ``GIT_ASKPASS``
script reads ``$BITBUCKET_API_TOKEN`` from the container runtime env at git call
time, and ``insteadOf`` rewrites carry only the non-secret sentinel username. The
token VALUE must never appear in the rendered compose YAML, the askpass script,
or the resolution metadata — only the ``${BITBUCKET_API_TOKEN}`` lease
placeholder and the runtime ``$BITBUCKET_API_TOKEN`` reference are permitted.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from awf.node.compose_manager import ComposeManager, WorkspaceComposeSpec
from awf.node.secret_mounts import LocalSecretLeaseMountResolver
from awf.profiles.models import WorkspaceProfile

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"
_TOKEN_SENTINEL = "ATATT-LEAK-SENTINEL-do-not-render"
_ASKPASS_TARGET = "/run/awf/secrets/bb-askpass.sh"
_AGENT_HTTPS_URL = "https://x-bitbucket-api-token-auth@bitbucket.org/"


def _bitbucket_profile(*, with_email: bool = True) -> WorkspaceProfile:
    secrets: list[dict[str, object]] = [
        {
            "name": "bitbucket-token",
            "kind": "env",
            "target": "BITBUCKET_API_TOKEN",
            "provider": "bitbucket",
            "ref": "token",
        }
    ]
    if with_email:
        secrets.append(
            {
                "name": "bitbucket-email",
                "kind": "env",
                "target": "BITBUCKET_EMAIL",
                "provider": "bitbucket",
                "ref": "email",
            }
        )
    return WorkspaceProfile.model_validate({"name": "bitbucket-profile", "secrets": secrets})


def _resolve(tmp_path: Path, profile: WorkspaceProfile) -> object:
    resolver = LocalSecretLeaseMountResolver(
        host_home=tmp_path / "host-home",
        work_dir=tmp_path / "work",
        host_env={"BITBUCKET_API_TOKEN": _TOKEN_SENTINEL, "BITBUCKET_EMAIL": "agent@example.com"},
    )
    return resolver.resolve(profile, workspace_id="ws_leak")


def _render(tmp_path: Path, resolution: object) -> tuple[str, dict[str, object]]:
    spec = WorkspaceComposeSpec(
        workspace_id="ws_leak",
        worktree_host_path=tmp_path / "worktree",
        agent_environment=resolution.environment,  # type: ignore[attr-defined]
        auth_mounts=resolution.mounts,  # type: ignore[attr-defined]
    )
    manager = ComposeManager(work_dir=tmp_path / "compose-work", template_path=_TEMPLATE)
    paths = manager.render(spec)
    compose_text = paths.compose_file.read_text(encoding="utf-8")
    parsed = yaml.safe_load(compose_text)
    return compose_text, parsed


@pytest.mark.unit
def test_rendered_compose_never_contains_the_token_value(tmp_path: Path) -> None:
    resolution = _resolve(tmp_path, _bitbucket_profile())
    compose_text, parsed = _render(tmp_path, resolution)

    # (a) The token VALUE appears nowhere at the rendered boundary.
    assert _TOKEN_SENTINEL not in compose_text
    assert _TOKEN_SENTINEL not in json.dumps(parsed, default=str)
    assert _TOKEN_SENTINEL not in json.dumps(
        {"metadata": resolution.metadata, "resolution": repr(resolution)},  # type: ignore[attr-defined]
        default=str,
    )

    # The materialized askpass script also holds no token value.
    askpass_mount = next(
        m
        for m in resolution.mounts
        if m.target == _ASKPASS_TARGET  # type: ignore[attr-defined]
    )
    script_content = Path(askpass_mount.source).read_text(encoding="utf-8")
    assert _TOKEN_SENTINEL not in script_content

    # The only permitted token references: the runtime ``$BITBUCKET_API_TOKEN``
    # inside the mounted script and the ``${BITBUCKET_API_TOKEN}`` lease pair.
    assert '"$BITBUCKET_API_TOKEN"' in script_content
    agent_env = parsed["services"]["agent"]["environment"]
    assert agent_env["BITBUCKET_API_TOKEN"] == "${BITBUCKET_API_TOKEN}"


@pytest.mark.unit
def test_rendered_compose_mounts_askpass_read_only_and_sets_env(tmp_path: Path) -> None:
    resolution = _resolve(tmp_path, _bitbucket_profile())
    _, parsed = _render(tmp_path, resolution)

    agent = parsed["services"]["agent"]
    agent_env = agent["environment"]
    assert agent_env["GIT_ASKPASS"] == _ASKPASS_TARGET
    assert agent_env["GIT_TERMINAL_PROMPT"] == "0"

    # The askpass script is bind-mounted read-only at the askpass target.
    askpass_mount = next(
        m
        for m in resolution.mounts
        if m.target == _ASKPASS_TARGET  # type: ignore[attr-defined]
    )
    expected_volume = f"{askpass_mount.source}:{_ASKPASS_TARGET}:ro"
    assert expected_volume in agent["volumes"]
    # And the host file is executable (so the agent can run the ro mount).
    assert Path(askpass_mount.source).stat().st_mode & 0o111


@pytest.mark.unit
@pytest.mark.parametrize(
    "remote",
    [
        "https://bitbucket.org/ws/repo.git",
        "https://bitbucket.org:443/ws/repo.git",
        "git@bitbucket.org:ws/repo.git",
        "ssh://git@bitbucket.org/ws/repo.git",
        "ssh://git@bitbucket.org:22/ws/repo.git",
    ],
)
def test_insteadof_rewrites_all_remote_forms_to_sentinel_https(
    tmp_path: Path,
    remote: str,
) -> None:
    # (b)+(c): the rendered GIT_CONFIG_* env rewrites every bitbucket.org remote
    # form (HTTPS + both SSH-form shapes, bare and explicit default port) to the
    # sentinel-username HTTPS URL. The explicit-port shapes (``:443`` HTTPS,
    # ``:22`` SSH) are required because insteadOf matches literal prefixes; git
    # leaves them unrewritten without their own source entries (regression).
    # ``git ls-remote --get-url`` expands ``insteadOf`` offline (no network).
    resolution = _resolve(tmp_path, _bitbucket_profile())
    env = dict(resolution.environment)  # type: ignore[attr-defined]
    git_env = {k: v for k, v in env.items() if k.startswith("GIT_CONFIG_")}

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    result = subprocess.run(
        ["git", "ls-remote", "--get-url", remote],
        cwd=repo,
        env={"PATH": _path_env(), **git_env},
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == f"{_AGENT_HTTPS_URL}ws/repo.git"


@pytest.mark.unit
def test_insteadof_does_not_loop_on_already_rewritten_url(tmp_path: Path) -> None:
    # The rewrite target carries the sentinel-username prefix, so it never
    # re-matches the bare ``https://bitbucket.org/`` source rule (no loop).
    resolution = _resolve(tmp_path, _bitbucket_profile())
    env = dict(resolution.environment)  # type: ignore[attr-defined]
    git_env = {k: v for k, v in env.items() if k.startswith("GIT_CONFIG_")}

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    already = f"{_AGENT_HTTPS_URL}ws/repo.git"
    result = subprocess.run(
        ["git", "ls-remote", "--get-url", already],
        cwd=repo,
        env={"PATH": _path_env(), **git_env},
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == already


@pytest.mark.unit
@pytest.mark.parametrize("bad_workspace_id", ["", "../escape", "..", ".", "a/b", "a\\b"])
def test_traversal_workspace_id_is_rejected(tmp_path: Path, bad_workspace_id: str) -> None:
    # Defense in depth: a ``workspace_id`` that is empty or carries a path separator
    # or traversal segment must not be used to build the askpass materialization dir.
    # An empty id would collapse ``work_dir / subdir / ""`` to ``work_dir / subdir``,
    # colliding on a shared parent path across workspaces.
    resolver = LocalSecretLeaseMountResolver(
        host_home=tmp_path / "host-home",
        work_dir=tmp_path / "work",
        host_env={"BITBUCKET_API_TOKEN": _TOKEN_SENTINEL, "BITBUCKET_EMAIL": "agent@example.com"},
    )
    with pytest.raises(ValueError, match="Invalid workspace_id"):
        resolver.resolve(_bitbucket_profile(), workspace_id=bad_workspace_id)


@pytest.mark.unit
def test_non_bitbucket_lease_renders_no_agent_git_auth(tmp_path: Path) -> None:
    # (d): a non-bitbucket lease yields none of GIT_ASKPASS / GIT_CONFIG_* / the
    # askpass mount, and the existing agent env is otherwise unchanged.
    resolver = LocalSecretLeaseMountResolver(
        host_home=tmp_path / "host-home",
        work_dir=tmp_path / "work",
        host_env={"OPENAI_API_KEY": "sk-live-do-not-render"},
    )
    resolution = resolver.resolve(
        WorkspaceProfile.model_validate(
            {
                "name": "env-profile",
                "secrets": [
                    {
                        "name": "openai",
                        "kind": "env",
                        "target": "OPENAI_API_KEY",
                        "provider": "env",
                        "ref": "env/OPENAI_API_KEY",
                    }
                ],
            }
        ),
        workspace_id="ws_leak",
    )

    _, parsed = _render(tmp_path, resolution)
    agent_env = parsed["services"]["agent"]["environment"]
    assert "GIT_ASKPASS" not in agent_env
    assert not any(k.startswith("GIT_CONFIG_") for k in agent_env)
    assert resolution.mounts == ()


def _path_env() -> str:
    return os.environ.get("PATH", "/usr/bin:/bin")
