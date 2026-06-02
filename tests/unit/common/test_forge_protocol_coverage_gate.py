"""Targeted coverage for the ``ForgeClient`` Protocol method bodies.

The ``ForgeClient`` Protocol (issue #345 Phase 1) declares the provider-neutral
forge surface as ten ``async def`` methods whose bodies are bare ``...``
statements. Those statements are never executed by the concrete ``GitHubClient``
(which overrides every method), so they read as uncovered lines and drop the
combined coverage gate below its threshold.

Mirror the established ``test_stack_launcher_coverage_gate.py`` convention: a
concrete subclass whose overrides defer to ``super().<method>(...)`` executes
each Protocol ``...`` body (each evaluates to ``None``), keeping the structural
contract documented and covered without weakening the gate.
"""

from __future__ import annotations

import pytest

from awf.common.forge import ForgeClient

pytestmark = pytest.mark.unit


class _ConcreteForgeClient(ForgeClient):
    """Concrete ``ForgeClient`` whose methods defer to the Protocol bodies.

    Each override calls ``super().<method>(...)`` so the ``...`` statement in the
    corresponding ``ForgeClient`` Protocol method runs and evaluates to ``None``.
    The forwarded arguments are inert: the Protocol bodies ignore them.
    """

    async def fetch_pr_status(self, *, repo, pr_number, base_behind_count):  # type: ignore[override]
        return await super().fetch_pr_status(
            repo=repo, pr_number=pr_number, base_behind_count=base_behind_count
        )

    async def fetch_failing_check_logs(  # type: ignore[override]
        self,
        *,
        repo,
        pr_number,
        head_sha,
        log_tail_chars=3000,
        pytest_fallback_commands=(),
    ):
        return await super().fetch_failing_check_logs(
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            log_tail_chars=log_tail_chars,
            pytest_fallback_commands=pytest_fallback_commands,
        )

    async def rerun_failed_workflow_jobs(self, *, repo, run_id):  # type: ignore[override]
        return await super().rerun_failed_workflow_jobs(repo=repo, run_id=run_id)

    async def resolve_thread(self, *, thread_id):  # type: ignore[override]
        return await super().resolve_thread(thread_id=thread_id)

    async def post_comment(self, *, repo, pr_number, body):  # type: ignore[override]
        return await super().post_comment(repo=repo, pr_number=pr_number, body=body)

    async def create_issue(self, *, repo, title, body):  # type: ignore[override]
        return await super().create_issue(repo=repo, title=title, body=body)

    async def create_pull_request(self, *, repo, base, head, title, body):  # type: ignore[override]
        return await super().create_pull_request(
            repo=repo, base=base, head=head, title=title, body=body
        )

    async def fetch_repo_merge_methods(self, *, repo):  # type: ignore[override]
        return await super().fetch_repo_merge_methods(repo=repo)

    async def fetch_branch_pull_request_allowed_merge_methods(  # type: ignore[override]
        self, *, repo, branch
    ):
        return await super().fetch_branch_pull_request_allowed_merge_methods(
            repo=repo, branch=branch
        )

    async def merge_pr(self, *, repo, pr_number, method="squash", delete_branch=True):  # type: ignore[override]
        return await super().merge_pr(
            repo=repo, pr_number=pr_number, method=method, delete_branch=delete_branch
        )


async def test_forge_client_protocol_bodies_execute_and_return_none() -> None:
    """Every ``ForgeClient`` Protocol ``...`` body runs and evaluates to ``None``."""
    client = _ConcreteForgeClient()
    sentinel_repo = object()

    assert (
        await client.fetch_pr_status(repo=sentinel_repo, pr_number=1, base_behind_count=0)
    ) is None
    assert (
        await client.fetch_failing_check_logs(repo=sentinel_repo, pr_number=1, head_sha="deadbeef")
    ) is None
    assert (await client.rerun_failed_workflow_jobs(repo=sentinel_repo, run_id="42")) is None
    assert (await client.resolve_thread(thread_id="thread-1")) is None
    assert (await client.post_comment(repo=sentinel_repo, pr_number=1, body="hi")) is None
    assert (await client.create_issue(repo=sentinel_repo, title="t", body="b")) is None
    assert (
        await client.create_pull_request(
            repo=sentinel_repo, base="main", head="feat", title="t", body="b"
        )
    ) is None
    assert (await client.fetch_repo_merge_methods(repo=sentinel_repo)) is None
    assert (
        await client.fetch_branch_pull_request_allowed_merge_methods(
            repo=sentinel_repo, branch="main"
        )
    ) is None
    assert (await client.merge_pr(repo=sentinel_repo, pr_number=1)) is None
