"""Best-effort origin-remote detection used by preview/smoke forge resolution."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common import git_remote


@pytest.mark.unit
def test_returns_origin_url_from_real_checkout(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin", "https://bitbucket.org/o/r.git"],
        check=True,
    )

    assert git_remote.detect_repo_url_from_checkout(tmp_path) == "https://bitbucket.org/o/r.git"


@pytest.mark.unit
def test_returns_none_when_no_origin_remote(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)

    assert git_remote.detect_repo_url_from_checkout(tmp_path) is None


@pytest.mark.unit
def test_returns_none_outside_git_repo(tmp_path: Path) -> None:
    assert git_remote.detect_repo_url_from_checkout(tmp_path) is None


@pytest.mark.unit
def test_returns_none_when_git_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _raise(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("git not installed")

    monkeypatch.setattr(git_remote.subprocess, "run", _raise)

    assert git_remote.detect_repo_url_from_checkout(tmp_path) is None


@pytest.mark.unit
def test_returns_none_for_blank_origin_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        git_remote.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="  \n", stderr=""),
    )

    assert git_remote.detect_repo_url_from_checkout(tmp_path) is None


@pytest.mark.unit
def test_invokes_git_scoped_to_the_checkout_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def _capture(cmd: list[str], **_kwargs: object) -> object:
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="https://github.com/o/r.git\n", stderr="")

    monkeypatch.setattr(git_remote.subprocess, "run", _capture)

    assert git_remote.detect_repo_url_from_checkout(tmp_path) == "https://github.com/o/r.git"
    assert captured["cmd"] == [
        "git",
        "-C",
        str(tmp_path),
        "remote",
        "get-url",
        "origin",
    ]
