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
from pathlib import Path

from awf.cli.watchdog import WatchdogConfig, run_one_scan


def _canned_gh_output(
    *,
    repo: str,
    prs: list[tuple[int, str]],
    head_oid: str = "abc1234567890def",
) -> str:
    """Shape mirrors ``gh pr list --json number,headRefName,baseRefName,headRefOid``."""
    return json.dumps(
        [
            {
                "number": pr[0],
                "headRefName": pr[1],
                "headRefOid": head_oid,
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


class TestWatchdogSkipNotifyHuman:
    """Integration: ``run_one_scan`` must consult the defer-signal
    artifact and skip respawn when the previous monitor exited via
    ``NotifyHuman`` and no new commits have arrived. Without this, the
    watchdog re-spawns the same monitor every poll for a release PR
    that's intentionally awaiting human merge."""

    def test_main_loop_skips_notify_human_pr_with_same_sha(
        self, tmp_path: Path
    ) -> None:
        head = "deadbeefcafe1234"
        gh = _FakeGhLister(
            per_repo={
                "dimileeh/aira-agent": _canned_gh_output(
                    repo="dimileeh/aira-agent",
                    prs=[(355, "awf/release-foo")],
                    head_oid=head,
                ),
            }
        )
        spawn = _FakeSpawn()
        # Drop a NotifyHuman artifact for PR 355 with matching head_sha.
        artifacts_root = tmp_path / "artifacts"
        artifacts_root.mkdir()
        (artifacts_root / "ws_abc.defer-signal.json").write_text(
            json.dumps(
                {
                    "workspace_id": "ws_abc",
                    "repo": "dimileeh/aira-agent",
                    "pr_number": 355,
                    "terminal_action": "NotifyHuman",
                    "head_sha": head,
                    "merged": False,
                    "deferred_bot_items": [],
                    "deferred_human_items": [],
                }
            )
        )
        run_one_scan(
            config=_cfg(["dimileeh/aira-agent"], tmp_path),
            gh_lister=gh,
            process_lister=lambda: "",
            spawn_attach=spawn,
        )
        # The whole point: no respawn for an intentionally-deferred PR.
        assert spawn.calls == []

    def test_main_loop_respawns_notify_human_pr_when_sha_advanced(
        self, tmp_path: Path
    ) -> None:
        """Same artifact, but the PR has new commits → must re-engage."""
        gh = _FakeGhLister(
            per_repo={
                "dimileeh/aira-agent": _canned_gh_output(
                    repo="dimileeh/aira-agent",
                    prs=[(355, "awf/release-foo")],
                    head_oid="newsha2222222222",
                ),
            }
        )
        spawn = _FakeSpawn()
        artifacts_root = tmp_path / "artifacts"
        artifacts_root.mkdir()
        (artifacts_root / "ws_abc.defer-signal.json").write_text(
            json.dumps(
                {
                    "workspace_id": "ws_abc",
                    "repo": "dimileeh/aira-agent",
                    "pr_number": 355,
                    "terminal_action": "NotifyHuman",
                    "head_sha": "oldsha0000000000",
                    "merged": False,
                    "deferred_bot_items": [],
                    "deferred_human_items": [],
                }
            )
        )
        run_one_scan(
            config=_cfg(["dimileeh/aira-agent"], tmp_path),
            gh_lister=gh,
            process_lister=lambda: "",
            spawn_attach=spawn,
        )
        assert len(spawn.calls) == 1
        assert spawn.calls[0]["pr_number"] == 355

    def test_main_loop_respawns_when_no_artifact_exists(
        self, tmp_path: Path
    ) -> None:
        """Crash recovery — if no artifact was ever written for this PR,
        respawn (the watchdog's primary purpose)."""
        gh = _FakeGhLister(
            per_repo={
                "dimileeh/aira-agent": _canned_gh_output(
                    repo="dimileeh/aira-agent",
                    prs=[(355, "awf/release-foo")],
                ),
            }
        )
        spawn = _FakeSpawn()
        run_one_scan(
            config=_cfg(["dimileeh/aira-agent"], tmp_path),
            gh_lister=gh,
            process_lister=lambda: "",
            spawn_attach=spawn,
        )
        assert len(spawn.calls) == 1
