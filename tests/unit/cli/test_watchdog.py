"""Tests for ``awf.cli.watchdog`` — the stranded-PR watchdog.

The watchdog runs outside any single monitor process. Every poll
interval it:

1. ``gh pr list`` on each watched repo (``--state open --head awf/``).
2. For each open AWF-authored PR, check whether a ``run_awf.py`` process
   is already driving that exact PR's spec file. If not, re-spawn via
   ``scripts/attach_feature_pr_monitor.py``.

Tests inject fake collaborators (``gh_lister`` callable,
``process_lister`` callable, ``spawn_attach`` callable) so no real
subprocess runs. They lock the invariants that matter operationally:

* every open AWF PR in the watched repos gets checked,
* PRs already monitored are skipped (idempotency),
* PRs with no matching process are re-attached,
* a transient ``gh`` failure logs + continues (never crashes the loop),
* multiple repos feed into one flat "PRs to check" list.
"""

from __future__ import annotations

import json
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import awf.cli.watchdog as watchdog
from awf.cli.watchdog import WatchdogConfig, run_one_scan


def _canned_gh_output(*, repo: str, prs: list[tuple[int, str]]) -> str:
    """Shape mirrors ``gh pr list --json number,headRefName,baseRefName``."""
    return json.dumps(
        [
            {
                "number": pr[0],
                "headRefName": pr[1],
                "baseRefName": "development",
                "repository": {"nameWithOwner": repo},
            }
            for pr in prs
        ]
    )


class _FakeGhLister:
    def __init__(self, *, per_repo: dict[str, str]) -> None:
        self._per_repo = per_repo
        self.calls: list[str] = []

    def __call__(self, repo: str) -> str:
        self.calls.append(repo)
        return self._per_repo[repo]


class _FakeSpawn:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        repo: str,
        pr_number: int,
        work_dir: Path,
        agent: str,
    ) -> int:
        self.calls.append(
            {
                "repo": repo,
                "pr_number": pr_number,
                "work_dir": work_dir,
                "agent": agent,
            }
        )
        return 0


def _cfg(repos: list[str], work_dir: Path) -> WatchdogConfig:
    return WatchdogConfig(
        repos=repos,
        work_dir=work_dir,
        agent="claude_code",
        poll_seconds=300,
    )


class TestWatchdogScan:
    def test_watchdog_scans_open_awf_prs(self, tmp_path: Path) -> None:
        gh = _FakeGhLister(
            per_repo={
                "dimileeh/aira-web": _canned_gh_output(
                    repo="dimileeh/aira-web",
                    prs=[(100, "awf/ws1"), (101, "awf/ws2"), (102, "awf/ws3")],
                ),
            }
        )
        spawn = _FakeSpawn()

        run_one_scan(
            config=_cfg(["dimileeh/aira-web"], tmp_path),
            gh_lister=gh,
            process_lister=lambda: "",
            spawn_attach=spawn,
        )

        assert gh.calls == ["dimileeh/aira-web"]
        # All three PRs were "not monitored" in the process list → all
        # three got re-attached.
        assert len(spawn.calls) == 3
        assert {c["pr_number"] for c in spawn.calls} == {100, 101, 102}

    def test_watchdog_skips_already_monitored_pr(self, tmp_path: Path) -> None:
        """Process lister returns a ``run_awf.py`` line whose --config
        points at the PR-100 spec file. Watchdog must NOT re-spawn."""
        gh = _FakeGhLister(
            per_repo={
                "dimileeh/aira-web": _canned_gh_output(
                    repo="dimileeh/aira-web",
                    prs=[(100, "awf/ws1")],
                ),
            }
        )
        spawn = _FakeSpawn()
        # The ps line contains the deterministic spec filename that the
        # attach script writes.
        ps_output = (
            "12345 python run_awf.py --config "
            f"{tmp_path}/feature-pr-specs/dimileeh__aira-web-feature-pr100.json\n"
        )

        run_one_scan(
            config=_cfg(["dimileeh/aira-web"], tmp_path),
            gh_lister=gh,
            process_lister=lambda: ps_output,
            spawn_attach=spawn,
        )

        assert spawn.calls == []

    def test_watchdog_respawns_for_stranded_pr(self, tmp_path: Path) -> None:
        """PR 100 has no matching process → attach script invoked with
        repo + PR + work_dir + agent."""
        gh = _FakeGhLister(
            per_repo={
                "dimileeh/aira-web": _canned_gh_output(
                    repo="dimileeh/aira-web",
                    prs=[(100, "awf/ws1")],
                ),
            }
        )
        spawn = _FakeSpawn()

        run_one_scan(
            config=_cfg(["dimileeh/aira-web"], tmp_path),
            gh_lister=gh,
            process_lister=lambda: "",
            spawn_attach=spawn,
        )

        assert len(spawn.calls) == 1
        call = spawn.calls[0]
        assert call["repo"] == "dimileeh/aira-web"
        assert call["pr_number"] == 100
        assert call["work_dir"] == tmp_path
        assert call["agent"] == "claude_code"

    def test_watchdog_handles_multiple_repos(self, tmp_path: Path) -> None:
        gh = _FakeGhLister(
            per_repo={
                "dimileeh/aira-web": _canned_gh_output(
                    repo="dimileeh/aira-web",
                    prs=[(100, "awf/a"), (101, "awf/b")],
                ),
                "dimileeh/aira-agent": _canned_gh_output(
                    repo="dimileeh/aira-agent",
                    prs=[(200, "awf/c"), (201, "awf/d")],
                ),
                "dimileeh/aira-agent-workspace-fabric": _canned_gh_output(
                    repo="dimileeh/aira-agent-workspace-fabric",
                    prs=[(300, "awf/e"), (301, "awf/f")],
                ),
            }
        )
        spawn = _FakeSpawn()

        run_one_scan(
            config=_cfg(
                [
                    "dimileeh/aira-web",
                    "dimileeh/aira-agent",
                    "dimileeh/aira-agent-workspace-fabric",
                ],
                tmp_path,
            ),
            gh_lister=gh,
            process_lister=lambda: "",
            spawn_attach=spawn,
        )

        assert len(gh.calls) == 3
        assert len(spawn.calls) == 6
        assert {c["pr_number"] for c in spawn.calls} == {100, 101, 200, 201, 300, 301}

    def test_watchdog_survives_gh_lookup_failure(self, tmp_path: Path) -> None:
        """``gh pr list`` raising must not crash the scan — the watchdog
        logs and moves on to the next repo. The second repo in the list
        must still be processed."""
        call_log: list[str] = []

        def flaky_lister(repo: str) -> str:
            call_log.append(repo)
            if repo == "dimileeh/aira-web":
                raise RuntimeError("gh exploded")
            return _canned_gh_output(repo=repo, prs=[(300, "awf/e")])

        spawn = _FakeSpawn()

        # Must not raise.
        run_one_scan(
            config=_cfg(
                [
                    "dimileeh/aira-web",
                    "dimileeh/aira-agent-workspace-fabric",
                ],
                tmp_path,
            ),
            gh_lister=flaky_lister,
            process_lister=lambda: "",
            spawn_attach=spawn,
        )

        # Both repos were attempted (first failed, second succeeded).
        assert call_log == [
            "dimileeh/aira-web",
            "dimileeh/aira-agent-workspace-fabric",
        ]
        # Only the second repo's PR got re-attached.
        assert len(spawn.calls) == 1
        assert spawn.calls[0]["pr_number"] == 300

    def test_watchdog_survives_bad_json_output(self, tmp_path: Path) -> None:
        """A corrupt gh response is treated like a failed lookup — log,
        continue to the next repo, no spawn."""
        gh = _FakeGhLister(
            per_repo={
                "dimileeh/aira-web": "not valid json {{{",
            }
        )
        spawn = _FakeSpawn()

        run_one_scan(
            config=_cfg(["dimileeh/aira-web"], tmp_path),
            gh_lister=gh,
            process_lister=lambda: "",
            spawn_attach=spawn,
        )

        assert spawn.calls == []

    def test_watchdog_survives_wrong_json_shape_and_missing_pr_number(
        self,
        tmp_path: Path,
    ) -> None:
        spawn = _FakeSpawn()

        run_one_scan(
            config=_cfg(["dimileeh/aira-web", "dimileeh/aira-agent"], tmp_path),
            gh_lister=lambda repo: json.dumps({"not": "a list"})
            if repo == "dimileeh/aira-web"
            else json.dumps([{"headRefName": "awf/ws"}]),
            process_lister=lambda: "",
            spawn_attach=spawn,
        )

        assert spawn.calls == []

    def test_watchdog_survives_attach_spawn_exception(self, tmp_path: Path) -> None:
        def boom(**_kwargs: object) -> int:
            raise RuntimeError("spawn exploded")

        run_one_scan(
            config=_cfg(["dimileeh/aira-web"], tmp_path),
            gh_lister=lambda _repo: _canned_gh_output(
                repo="dimileeh/aira-web",
                prs=[(100, "awf/ws1")],
            ),
            process_lister=lambda: "",
            spawn_attach=boom,
        )


class TestWatchdogConfig:
    def test_default_repos_list_matches_brief(self) -> None:
        """Policy: the three AWF-managed repos. Hard-coded defaults are
        fine — this is an operator tool, not a library."""
        cfg = WatchdogConfig(work_dir=Path("/tmp/x"))
        assert cfg.repos == [
            "dimileeh/aira-agent",
            "dimileeh/aira-web",
            "dimileeh/aira-agent-workspace-fabric",
        ]


class TestWatchdogRealSeams:
    def test_default_gh_lister_calls_gh_with_repo_head_and_limit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[dict[str, object]] = []

        def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append({"args": args, **kwargs})
            return SimpleNamespace(stdout="[]")

        monkeypatch.setattr(watchdog.subprocess, "run", fake_run)

        assert watchdog._default_gh_lister("dimileeh/aira-web", limit=17) == "[]"

        assert calls == [
            {
                "args": [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    "dimileeh/aira-web",
                    "--state",
                    "open",
                    "--head",
                    "awf/",
                    "--json",
                    "number,headRefName,baseRefName",
                    "--limit",
                    "17",
                ],
                "capture_output": True,
                "text": True,
                "check": True,
            }
        ]

    def test_default_process_lister_returns_ps_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            watchdog.subprocess,
            "check_output",
            lambda args, **kwargs: f"{args} {kwargs}",
        )

        output = watchdog._default_process_lister()

        assert "ps" in output
        assert "stderr" in output

    @pytest.mark.parametrize(
        "exc",
        [
            subprocess.CalledProcessError(1, ["ps", "-ef"]),
            FileNotFoundError("ps"),
            OSError("process table unavailable"),
        ],
    )
    def test_default_process_lister_returns_empty_on_process_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        exc: Exception,
    ) -> None:
        def fake_check_output(*_args: object, **_kwargs: object) -> str:
            raise exc

        monkeypatch.setattr(watchdog.subprocess, "check_output", fake_check_output)

        assert watchdog._default_process_lister() == ""

    def test_default_spawn_attach_invokes_attach_script(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(watchdog, "_project_root", lambda: Path("/repo"))

        def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append({"args": args, **kwargs})
            return SimpleNamespace(returncode=0, stderr="")

        monkeypatch.setattr(watchdog.subprocess, "run", fake_run)

        returncode = watchdog._default_spawn_attach(
            repo="dimileeh/aira-web",
            pr_number=123,
            work_dir=tmp_path,
            agent="codex",
        )

        assert returncode == 0
        assert calls[0]["args"] == [
            watchdog.sys.executable,
            "/repo/scripts/attach_feature_pr_monitor.py",
            "--repo",
            "git@github.com:dimileeh/aira-web.git",
            "--pr",
            "123",
            "--work-dir",
            str(tmp_path),
            "--agent",
            "codex",
        ]
        assert calls[0]["capture_output"] is True
        assert calls[0]["text"] is True
        assert calls[0]["check"] is False

    def test_default_spawn_attach_returns_nonzero_exit_code(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(watchdog, "_project_root", lambda: Path("/repo"))
        monkeypatch.setattr(
            watchdog.subprocess,
            "run",
            lambda *_args, **_kwargs: SimpleNamespace(returncode=17, stderr="bad auth"),
        )

        assert (
            watchdog._default_spawn_attach(
                repo="dimileeh/aira-web",
                pr_number=123,
                work_dir=tmp_path,
                agent="codex",
            )
            == 17
        )

    def test_project_root_raises_when_pyproject_is_not_found(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(watchdog.Path, "is_file", lambda _self: False)

        with pytest.raises(RuntimeError, match="could not locate pyproject.toml"):
            watchdog._project_root()

    def test_project_root_finds_checkout_pyproject(self) -> None:
        assert (watchdog._project_root() / "pyproject.toml").is_file()


class TestWatchdogLoopAndCli:
    def test_shutdown_flag_handle_sets_stop(self) -> None:
        flag = watchdog._ShutdownFlag()

        flag.handle(signal.SIGTERM, None)

        assert flag.stop is True

    def test_run_loop_exits_after_signal(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        handlers: dict[int, object] = {}
        scans: list[str] = []

        def fake_signal(signum: int, handler: object) -> None:
            handlers[signum] = handler

        def gh_lister(_repo: str) -> str:
            scans.append("scan")
            handler = handlers[signal.SIGTERM]
            assert callable(handler)
            handler(signal.SIGTERM, None)
            return "[]"

        monkeypatch.setattr(watchdog.signal, "signal", fake_signal)

        watchdog._run_loop(
            config=WatchdogConfig(work_dir=tmp_path, repos=["dimileeh/aira-web"], poll_seconds=2),
            gh_lister=gh_lister,
            process_lister=lambda: "",
            spawn_attach=lambda **_kwargs: 0,
            sleep=lambda _seconds: None,
        )

        assert scans == ["scan"]
        assert signal.SIGINT in handlers
        assert signal.SIGTERM in handlers

    def test_run_loop_sleep_slice_observes_shutdown_signal(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        handlers: dict[int, object] = {}
        sleeps: list[float] = []

        def fake_signal(signum: int, handler: object) -> None:
            handlers[signum] = handler

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            handler = handlers[signal.SIGTERM]
            assert callable(handler)
            handler(signal.SIGTERM, None)

        monkeypatch.setattr(watchdog.signal, "signal", fake_signal)

        watchdog._run_loop(
            config=WatchdogConfig(work_dir=tmp_path, repos=["dimileeh/aira-web"], poll_seconds=2),
            gh_lister=lambda _repo: "[]",
            process_lister=lambda: "",
            spawn_attach=lambda **_kwargs: 0,
            sleep=sleep,
        )

        assert sleeps == [1.0]

    def test_start_command_wires_options_into_run_loop(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        calls: list[dict[str, object]] = []

        def fake_run_loop(**kwargs: object) -> None:
            calls.append(kwargs)

        monkeypatch.setattr(watchdog, "_run_loop", fake_run_loop)

        result = CliRunner().invoke(
            watchdog.app,
            [
                "--work-dir",
                str(tmp_path),
                "--poll-seconds",
                "7",
                "--repo",
                "dimileeh/aira-web",
                "--agent",
                "codex",
                "--gh-pr-limit",
                "11",
            ],
        )

        assert result.exit_code == 0
        config = calls[0]["config"]
        assert isinstance(config, WatchdogConfig)
        assert config.work_dir == tmp_path
        assert config.poll_seconds == 7
        assert config.repos == ["dimileeh/aira-web"]
        assert config.agent == "codex"
        assert config.gh_pr_limit == 11
