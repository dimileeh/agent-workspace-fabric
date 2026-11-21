"""Executor planning iteration and carried-gap publication tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common import github_transport as _github_transport
from awf.common.commands import FakeCommandRunner
from awf.control.executor import WorkspaceExecutor
from awf.control.executor import pr_open_step as _pr_open_step
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.applied_operator_directives import (
    load_applied_operator_directives,
)
from awf.service.artifacts import DEPOSITED_CONFORMANCE_NAME, workspace_artifact_dir
from awf.service.doctor.reasons import reason_text_for_code
from tests.unit.control.executor_paths import _test_worktrees_root
from tests.unit.control.test_executor_parts.test_executor_part_003 import (
    _adapter_prompt_calls,
    _adapter_prompts,
    _created_pr_body,
    _queue_pre_agent_symlink_baseline,
    _queue_pre_push_diagnostics,
    _queue_validation_head,
    _seed_ready_workspace,
    executor,
    factory,
    fake,
)

_IMPORTED_FIXTURES = (executor, factory, fake)


class TestPlanningConformancePublication:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "outcome",
        [
            "satisfied",
            "exhausted",
            "exhausted-with-nul",
            "cancelled-at-compare",
            "reconciled-duplicate",
            "reconciled-transient",
            "reconciled-comment-error",
            "reconciled-tip-mismatch",
            "reconciled-snapshot-error-recovers",
            "reconciled-snapshot-error-persists",
            "reconciled-snapshot-error-tip-mismatch",
            "reconciled-body-error-recovers",
            "reconciled-body-error-already-disclosed",
            "reconciled-body-error-persists",
            "reconciled-body-error-tip-mismatch",
        ],
    )
    async def test_planning_profile_iterates_when_conformance_reports_gaps(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        outcome: str,
    ) -> None:
        ws_id = await _seed_ready_workspace(
            factory,
            resolved_profile={
                "name": "planned",
                "planning": {
                    "required": True,
                    "max_iterations": 1,
                },
                "phases": {"validate": ["pytest -q"]},
            },
        )

        exhausted = outcome != "satisfied"
        reconciled = outcome.startswith("reconciled-")
        failed_read = next(
            (read for read in ("snapshot", "body") if f"-{read}-error-" in outcome), None
        )
        read_failure_persists = outcome.endswith("-persists")
        retry_tip_mismatch = failed_read is not None and outcome.endswith("-tip-mismatch")
        section = (
            "### Unresolved plan-conformance gaps\n\n"
            "- **test_work**: Add a regression test\n"
            r"- **documentation**: Document the behavior ### Note \[redacted\]"
        )
        token = "ghp_" + "a" * 36
        separator = "\0" if outcome == "exhausted-with-nul" else " "
        gaps = [
            {"kind": "test_work", "detail": f"Add a regression{separator}test"},
            {"kind": "documentation", "detail": f"Document the behavior\n### Note {token}"},
        ]
        expected_gaps = [
            {"kind": "test_work", "detail": "Add a regression test"},
            {"kind": "documentation", "detail": "Document the behavior\n### Note [redacted]"},
        ]
        final_report = json.dumps(
            {
                "status": "needs_iteration" if exhausted else "satisfied",
                "summary": "remaining gaps" if exhausted else "done",
                "gaps": gaps if exhausted else [],
            }
        )
        worktree = _test_worktrees_root(factory) / ws_id
        report_path = worktree / "docs" / "awf-plans" / f"{ws_id}.conformance.json"
        original_run = fake.run

        async def run_and_write_final_report(args: list[str], **kwargs: Any) -> Any:
            if args[:3] == ["gh", "pr", "create"] and "\0" in args[args.index("--body") + 1]:
                raise ValueError("embedded null byte")
            result = await original_run(args, **kwargs)
            if result.stdout == final_report:
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(final_report, encoding="utf-8")
                if outcome == "cancelled-at-compare":
                    async with factory() as session:
                        repo = WorkspaceRepository(session)
                        ws = await repo.get(ws_id)
                        assert ws is not None
                        await repo.transition(
                            ws, to=WorkspaceStatus.cancelled, reason_code="TEST_CANCEL"
                        )
                        await session.commit()
            return result

        monkeypatch.setattr(fake, "run", run_and_write_final_report)
        monkeypatch.setattr(_pr_open_step, "_PR_CONFORMANCE_COMMENT_RETRY_DELAY_SECONDS", 0)
        if failed_read:
            # Surface a failed transport read so this exercises the executor retry.
            monkeypatch.setattr(_github_transport, "GITHUB_TRANSPORT_MAX_ATTEMPTS", 1)
        monitor = SimpleNamespace(run=AsyncMock())
        monkeypatch.setattr(executor, "_pr_monitor", monitor)
        _queue_pre_agent_symlink_baseline(fake)
        fake.queue_result(returncode=0, stdout="")  # before planning
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD baseline
        fake.queue_result(returncode=0, stdout="plan written")  # planning
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n")
        fake.queue_result(returncode=0, stdout="")  # committed_paths_since (empty)
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD pre-loop
        fake.queue_result(returncode=0, stdout="implemented")  # initial execute
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(returncode=0, stdout="sha1\n")  # conformance scope HEAD
        fake.queue_result(  # compare says not done
            returncode=0,
            stdout='{"status":"needs_iteration","summary":"gap","gaps":["add tests"]}',
        )
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(returncode=0, stdout="")  # committed paths since conformance HEAD
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 0 post
        fake.queue_result(returncode=0, stdout="fixed gap")  # iteration execute
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(returncode=0, stdout="sha1\n")  # conformance scope HEAD
        fake.queue_result(returncode=0, stdout=final_report)  # final compare
        fake.queue_result(returncode=0, stdout=f"?? docs/awf-plans/{ws_id}.md\n M src/x.py\n")
        fake.queue_result(returncode=0, stdout="")  # committed paths since conformance HEAD
        fake.queue_result(returncode=0, stdout="sha1\n")  # rev-parse HEAD iter 1 post
        if exhausted:
            fake.queue_result(
                returncode=0,
                stdout="docs/awf-plans/workspace.md\ndocs/usage.md\n",
            )  # committed net diff from the provisioned base
        fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="src/x.py\n")
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="1\n")
        fake.queue_result(returncode=0)
        _queue_validation_head(fake)
        if exhausted:
            fake.queue_result(returncode=1, stderr="pytest: failed")
            fake.queue_result(returncode=0, stdout="fixed tests")  # validation fix agent
            fake.queue_result(returncode=0)  # fix git add
            fake.queue_result(returncode=0, stdout="tests/test_x.py\n")
            fake.queue_result(returncode=0)  # fix commit
            _queue_validation_head(fake, head="b" * 40)
        fake.queue_result(returncode=0, stdout="tests ok")
        pushed_tip = "b" * 40
        _queue_pre_push_diagnostics(fake, head=pushed_tip)
        fake.queue_result(returncode=0)  # push
        pr_url = "https://github.com/dimileeh/aira-agent/pull/55"
        if reconciled:
            pr_head = "c" * 40 if outcome == "reconciled-tip-mismatch" else pushed_tip
            head_ref = f"awf/{ws_id}"
            fake.queue_result(
                returncode=1,
                stderr=(
                    'Post "https://api.github.com/graphql": dial tcp: i/o timeout'
                    if outcome == "reconciled-transient"
                    else f'a pull request for branch "{head_ref}" already exists'
                ),
            )
            fake.queue_result(
                stdout=json.dumps(
                    [
                        {
                            "number": 55,
                            "url": pr_url,
                            "headRefName": head_ref,
                            "headRefOid": pr_head,
                            "headRepository": {"nameWithOwner": "dimileeh/aira-agent"},
                        }
                    ]
                ),
            )
            snapshot = json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "merged": False,
                                "closed": False,
                                "headRefName": head_ref,
                                "headRefOid": pr_head,
                            }
                        }
                    }
                }
            )
            if failed_read:
                for attempt in range(2):
                    fail = attempt == 0 or read_failure_persists
                    if failed_read == "snapshot" and fail:
                        fake.queue_result(returncode=1, stderr=f"Snapshot lookup failed: {token}")
                        continue
                    if attempt == 1 and retry_tip_mismatch:
                        fake.queue_result(stdout=snapshot.replace(pushed_tip, "c" * 40))
                        fake.queue_result()  # cat-file finds the reconciled head locally
                        fake.queue_result(returncode=1)  # merge-base rejects containment
                        break
                    fake.queue_result(stdout=snapshot)
                    if fail:
                        fake.queue_result(returncode=1, stderr=f"Body lookup failed: {token}")
                    elif outcome.endswith("-already-disclosed"):
                        fake.queue_result(stdout=f"Existing body\n\n{section}\n")
                    else:
                        fake.queue_result(stdout="Existing body\n")
                        fake.queue_result()  # fallback comment
            else:
                fake.queue_result(stdout=snapshot)
            if outcome == "reconciled-tip-mismatch":
                fake.queue_result()  # cat-file finds the reconciled head locally
                fake.queue_result(returncode=1)  # merge-base rejects containment
            elif not failed_read:
                fake.queue_result(
                    stdout=(
                        f"Original body\n\n{section}\n".replace("\n", "\r\n")
                        if outcome == "reconciled-transient"
                        else "Existing body\n"
                    ),
                )
                if outcome == "reconciled-duplicate":
                    fake.queue_result()  # fallback comment
                elif outcome == "reconciled-comment-error":
                    fake.queue_result(returncode=1, stderr=f"Comment transport failed: {token}")
                    fake.queue_result(stdout=snapshot)
                    fake.queue_result(returncode=1, stderr=f"Comment retry failed: {token}")
        else:
            fake.queue_result(returncode=0, stdout=pr_url)

        await executor.execute(ws_id)

        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None
            events = [
                event for event in ws.events if event.reason_code == "PLAN_CONFORMANCE_GAPS_CARRIED"
            ]
            records = load_applied_operator_directives(
                MonitorState(threads_addressed_ids=ws.monitor_threads_addressed or {})
            )
        if outcome == "cancelled-at-compare":
            assert ws.status == WorkspaceStatus.cancelled.value
            assert events == []
            assert monitor.run.await_count == 0
            assert not any(any("pytest -q" in arg for arg in call.args) for call in fake.calls)
            assert not any("push" in call.args for call in fake.calls)
            return
        if failed_read:
            commands = [call.args for call in fake.calls]
            pr_calls = {
                ("gh", "api", "graphql"): "snapshot",
                ("gh", "pr", "view"): "body",
                ("gh", "pr", "comment"): "comment",
            }
            reads = (
                ["snapshot", "snapshot"]
                if failed_read == "snapshot"
                else ["snapshot", "body", "snapshot", "body"]
            )
            if retry_tip_mismatch and failed_read == "body":
                reads.pop()
            if failed_read == "snapshot" and not (read_failure_persists or retry_tip_mismatch):
                reads.append("body")
            posts_comment = not (
                read_failure_persists
                or retry_tip_mismatch
                or outcome.endswith("-already-disclosed")
            )
            assert [
                pr_calls[tuple(args[:3])] for args in commands if tuple(args[:3]) in pr_calls
            ] == (reads + (["comment"] if posts_comment else []))
            if posts_comment:
                comment = next(args for args in commands if args[:3] == ["gh", "pr", "comment"])
                assert comment[3] == "55"
                assert comment[-1] == section
            assert sum("push" in args for args in commands) == 1
            assert ws.pr_url == pr_url
            assert ws.pr_number == 55
            assert ws.monitor_last_commit_sha == pushed_tip
            pending = next(
                e for e in ws.events if e.event_type == "workspace.audit.pr_conformance_comment"
            )
            assert pending.reason_code == "PR_CONFORMANCE_COMMENT_FAILED"
            assert pending.payload["outcome"] == "pending"
            assert pending.payload["source_head_sha"] == pushed_tip
            error = f"{failed_read.capitalize()} lookup failed: [redacted]"
            assert pending.payload["evidence"]["error_message"].endswith(error)
            created = next(e for e in ws.events if e.event_type == "workspace.audit.pr_created")
            evidence = created.payload["evidence"]
            if retry_tip_mismatch and failed_read == "snapshot":
                assert ws.status == WorkspaceStatus.monitoring_pr.value, ws.failure_message
                assert "conformance_comment_pending" not in evidence
                assert evidence["conformance_comment_error"] == (
                    "Could not verify that the reconciled PR contains the pushed tip"
                )
                monitor.run.assert_awaited_once()
            elif read_failure_persists or retry_tip_mismatch:
                assert ws.status == WorkspaceStatus.failed.value, ws.failure_message
                assert ws.events[-1].reason_code == "PR_CONFORMANCE_COMMENT_FAILED"
                assert evidence["conformance_comment_pending"] is True
                if retry_tip_mismatch:
                    error = "The pull request no longer contains the pushed tip"
                assert evidence["conformance_comment_error"].endswith(error)
                monitor.run.assert_not_awaited()
            else:
                assert ws.status == WorkspaceStatus.monitoring_pr.value, ws.failure_message
                assert evidence["conformance_comment_retried"] is True
                monitor.run.assert_awaited_once()
            return
        if outcome == "reconciled-comment-error":
            assert ws.status == WorkspaceStatus.failed.value, ws.failure_message
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.events[-1].reason_code == "PR_CONFORMANCE_COMMENT_FAILED"
            assert monitor.run.await_count == 0
            assert ws.pr_url == pr_url
            assert ws.pr_number == 55
            assert ws.monitor_last_commit_sha == pushed_tip
            commands = [call.args for call in fake.calls]
            assert sum("push" in args for args in commands) == 1
            comments = [args for args in commands if args[:3] == ["gh", "pr", "comment"]]
            assert [args[3] for args in comments] == ["55", "55"]
            assert [args[-1] for args in comments] == [section, section]
            push = next(e for e in ws.events if e.event_type == "workspace.audit.git_push")
            assert push.payload["outcome"] == "succeeded"
            pending = next(
                e for e in ws.events if e.event_type == "workspace.audit.pr_conformance_comment"
            )
            assert pending.payload["evidence"]["error_message"].endswith(
                "Comment transport failed: [redacted]"
            )
            created = next(e for e in ws.events if e.event_type == "workspace.audit.pr_created")
            assert created.payload["evidence"]["strategy"] == "reconciled_after_duplicate"
            assert created.payload["evidence"]["conformance_comment_error"].endswith(
                "Comment retry failed: [redacted]"
            )
            assert "Comment retry failed: [redacted]" in ws.failure_message
            assert events[0].payload["gaps"] == expected_gaps
            assert records[0].source == "plan_conformance"
            return
        assert ws.status == WorkspaceStatus.monitoring_pr.value, ws.failure_message
        assert monitor.run.await_count == 1
        assert ws.pr_url == pr_url
        assert ws.pr_number == 55
        body = _created_pr_body(fake)
        if reconciled:
            commands = [call.args for call in fake.calls]
            snapshot_index = next(
                index for index, args in enumerate(commands) if args[:3] == ["gh", "api", "graphql"]
            )
            body_reads = [
                index for index, args in enumerate(commands) if args[:3] == ["gh", "pr", "view"]
            ]
            comments = [
                index for index, args in enumerate(commands) if args[:3] == ["gh", "pr", "comment"]
            ]
            if outcome == "reconciled-tip-mismatch":
                assert [args[-2:] for args in commands if "--is-ancestor" in args][-1] == [
                    pushed_tip,
                    pr_head,
                ]
                assert body_reads == comments == []
            else:
                assert len(body_reads) == 1
                assert snapshot_index < body_reads[0]
                if outcome == "reconciled-transient":
                    assert comments == []
                else:
                    assert len(comments) == 1
                    assert body_reads[0] < comments[0]
                    assert commands[comments[0]][3] == "55"
                    assert commands[comments[0]][-1] == section
        adapter_prompts = _adapter_prompts(fake)
        assert len(adapter_prompts) == (6 if exhausted else 5)
        assert "Iteration 1" in adapter_prompts[3]
        assert sum("Conformance phase" in prompt for prompt in adapter_prompts) == 2
        if exhausted:
            assert "Validation failed after your previous pass" in adapter_prompts[-1]
            validation_indexes = [
                index
                for index, call in enumerate(fake.calls)
                if any("pytest -q" in arg for arg in call.args)
            ]
            push_index = next(index for index, call in enumerate(fake.calls) if "push" in call.args)
            prompt_indexes = [index for index, _ in _adapter_prompt_calls(fake)]
            assert prompt_indexes[4] < validation_indexes[0] < prompt_indexes[5]
            assert prompt_indexes[5] < validation_indexes[1] < push_index
            assert len(events) == 1
            assert events[0].payload["gaps"] == expected_gaps
            assert body.endswith(section)
            assert len(records) == 1
            assert records[0].source == "plan_conformance"
            for gap in expected_gaps:
                assert f"{gap['kind']}: {gap['detail']}" in records[0].directive
            assert token not in body and token not in records[0].directive
            assert reason_text_for_code(events[0].reason_code) is not None
            catalog = Path("docs/REASON_CATALOG.md").read_text(encoding="utf-8")
            assert "PLAN_CONFORMANCE_GAPS_CARRIED" in catalog
            artifact = (
                workspace_artifact_dir(executor._config.compose_projects_root.parent, ws_id)
                / DEPOSITED_CONFORMANCE_NAME
            )
            assert artifact.read_text(encoding="utf-8") == final_report
        else:
            assert events == []
            assert records == ()
            assert "Unresolved plan-conformance gaps" not in body
