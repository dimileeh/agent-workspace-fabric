"""BitBucketClient pipeline/rerun/merge-method tests (issue #345 Part 2).

Covers the failing-check-log pipeline-lookup chain (status → pipelines → steps →
log) and the external-status pytest fallback, the whole-pipeline rerun (full rerun
vs conservative not-rerunnable), merge-method resolution from remembered PR context
(including ``fast_forward``-only repos), and ``from_env`` construction.
"""

from __future__ import annotations

import json

import pytest

from awf.common.bitbucket_client import (
    BITBUCKET_AUTH_NOT_CONFIGURED,
    BITBUCKET_PIPELINE_NOT_RERUNNABLE,
    BitBucketClient,
    BitBucketClientError,
)

from ._helpers import FakeBitBucket, make_client, pr_payload, repo

pytestmark = pytest.mark.unit

_HEAD = "s" * 40
_REPO = "/2.0/repositories/workspace/repo"
_PR = f"{_REPO}/pullrequests/42"
_PIPELINES = f"{_REPO}/pipelines/"


def _seed_fetch_status(fake: FakeBitBucket, *, pr: dict | None = None) -> None:
    fake.enqueue("GET", _PR, json=pr if pr is not None else pr_payload())
    fake.page("GET", f"{_REPO}/commit/{_HEAD}/statuses", values=[])
    fake.page("GET", f"{_PR}/comments", values=[])
    fake.page("GET", f"{_PR}/diffstat", values=[])
    fake.enqueue("GET", "/2.0/user", json={"account_id": "viewer"})


# ── fetch_failing_check_logs: pipeline-lookup chain ───────────────────────────


async def test_failing_check_logs_pipeline_chain() -> None:
    fake = FakeBitBucket()
    fake.page(
        "GET",
        f"{_REPO}/commit/{_HEAD}/statuses",
        values=[{"state": "FAILED", "name": "Pipeline #5", "key": "PIPELINE"}],
    )
    fake.page("GET", _PIPELINES, values=[{"uuid": "pipe-1", "state": {"name": "COMPLETED"}}])
    fake.page(
        "GET",
        f"{_REPO}/pipelines/pipe-1/steps/",
        values=[
            {"uuid": "step-1", "name": "Build", "state": {"result": {"name": "SUCCESSFUL"}}},
            {"uuid": "step-2", "name": "Test", "state": {"result": {"name": "FAILED"}}},
        ],
    )
    fake.enqueue(
        "GET",
        f"{_REPO}/pipelines/pipe-1/steps/step-2/log",
        text="E   assert 1 == 2\nFAILED tests/test_x.py::test_y\n",
    )
    client = make_client(fake)
    failures = await client.fetch_failing_check_logs(
        repo=repo(), pr_number=42, head_sha=_HEAD, log_tail_chars=3000
    )
    assert len(failures) == 1
    failure = failures[0]
    assert failure.name == "Test"
    assert failure.run_id == "pipe-1"
    assert "FAILED tests/test_x.py::test_y" in failure.log_excerpt


async def test_failing_check_logs_no_failed_statuses_returns_empty() -> None:
    fake = FakeBitBucket()
    fake.page(
        "GET",
        f"{_REPO}/commit/{_HEAD}/statuses",
        values=[{"state": "SUCCESSFUL", "name": "ok"}],
    )
    client = make_client(fake)
    failures = await client.fetch_failing_check_logs(repo=repo(), pr_number=42, head_sha=_HEAD)
    assert failures == ()


async def test_failing_check_logs_external_status_falls_back_to_pytest() -> None:
    fake = FakeBitBucket()
    fake.page(
        "GET",
        f"{_REPO}/commit/{_HEAD}/statuses",
        values=[{"state": "FAILED", "name": "external-linter"}],
    )
    fake.page("GET", _PIPELINES, values=[])  # no pipeline → external status
    client = make_client(fake)
    failures = await client.fetch_failing_check_logs(
        repo=repo(),
        pr_number=42,
        head_sha=_HEAD,
        pytest_fallback_commands=["uv run pytest -q"],
    )
    assert len(failures) == 1
    assert failures[0].name == "external-linter"
    assert failures[0].log_excerpt == ""
    assert failures[0].run_id is None  # external status, no pipeline log
    assert failures[0].evidence_warnings  # surfaced the no-log fallback warning


async def test_failing_check_logs_pipeline_without_failing_step_falls_back() -> None:
    fake = FakeBitBucket()
    fake.page(
        "GET",
        f"{_REPO}/commit/{_HEAD}/statuses",
        values=[{"state": "FAILED", "name": "Pipeline #5"}],
    )
    fake.page("GET", _PIPELINES, values=[{"uuid": "pipe-1"}])
    fake.page(
        "GET",
        f"{_REPO}/pipelines/pipe-1/steps/",
        values=[{"uuid": "s1", "name": "Build", "state": {"result": {"name": "SUCCESSFUL"}}}],
    )
    client = make_client(fake)
    failures = await client.fetch_failing_check_logs(repo=repo(), pr_number=42, head_sha=_HEAD)
    assert len(failures) == 1
    assert failures[0].run_id is None  # external fallback path


async def test_failing_check_logs_stopped_pipeline_step_keeps_log_evidence() -> None:
    """A STOPPED/ERROR pipeline step must keep its pipeline UUID and step log.

    Regression for PRRT_kwDOSJAM6s6Hm9af: the commit-status filter accepts
    ``{"FAILED", "STOPPED"}``, but ``_failing_pipeline_steps`` previously matched
    only ``"FAILED"`` step results. A manually-stopped pipeline (steps with result
    ``"STOPPED"``) therefore found a pipeline yet yielded zero failing steps, so it
    fell through to the external fallback with ``run_id=None`` and an empty log,
    silently discarding the real pipeline UUID and partial step output.
    """
    fake = FakeBitBucket()
    fake.page(
        "GET",
        f"{_REPO}/commit/{_HEAD}/statuses",
        values=[{"state": "STOPPED", "name": "Pipeline #5", "key": "PIPELINE"}],
    )
    fake.page("GET", _PIPELINES, values=[{"uuid": "pipe-1", "state": {"name": "STOPPED"}}])
    fake.page(
        "GET",
        f"{_REPO}/pipelines/pipe-1/steps/",
        values=[
            {"uuid": "step-1", "name": "Build", "state": {"result": {"name": "SUCCESSFUL"}}},
            {"uuid": "step-2", "name": "Test", "state": {"result": {"name": "STOPPED"}}},
            {"uuid": "step-3", "name": "Deploy", "state": {"result": {"name": "ERROR"}}},
        ],
    )
    fake.enqueue(
        "GET",
        f"{_REPO}/pipelines/pipe-1/steps/step-2/log",
        text="E   cancelled mid-run\n",
    )
    fake.enqueue(
        "GET",
        f"{_REPO}/pipelines/pipe-1/steps/step-3/log",
        text="E   infrastructure error\n",
    )
    client = make_client(fake)
    failures = await client.fetch_failing_check_logs(
        repo=repo(), pr_number=42, head_sha=_HEAD, log_tail_chars=3000
    )
    by_name = {f.name: f for f in failures}
    assert set(by_name) == {"Test", "Deploy"}  # STOPPED and ERROR steps both surface
    assert by_name["Test"].run_id == "pipe-1"  # pipeline UUID preserved, not None
    assert "cancelled mid-run" in by_name["Test"].log_excerpt
    assert by_name["Deploy"].run_id == "pipe-1"
    assert "infrastructure error" in by_name["Deploy"].log_excerpt


async def test_failing_check_logs_surfaces_external_status_alongside_failing_steps() -> None:
    """A non-Pipelines FAILED status must still surface when a pipeline step fails.

    Regression for PRRT_kwDOSJAM6s6Hm62I: previously the pipeline-step pass
    returned early and dropped every other FAILED/STOPPED commit status (e.g. an
    external linter), so those checks never became CheckFailure rows for triage.
    """
    fake = FakeBitBucket()
    fake.page(
        "GET",
        f"{_REPO}/commit/{_HEAD}/statuses",
        values=[
            {"state": "FAILED", "name": "Pipeline #5", "key": "PIPELINE"},
            {"state": "STOPPED", "name": "external-linter", "key": "lint"},
        ],
    )
    fake.page("GET", _PIPELINES, values=[{"uuid": "pipe-1", "state": {"name": "COMPLETED"}}])
    fake.page(
        "GET",
        f"{_REPO}/pipelines/pipe-1/steps/",
        values=[{"uuid": "step-2", "name": "Test", "state": {"result": {"name": "FAILED"}}}],
    )
    fake.enqueue(
        "GET",
        f"{_REPO}/pipelines/pipe-1/steps/step-2/log",
        text="FAILED tests/test_x.py::test_y\n",
    )
    client = make_client(fake)
    failures = await client.fetch_failing_check_logs(
        repo=repo(), pr_number=42, head_sha=_HEAD, pytest_fallback_commands=["uv run pytest -q"]
    )
    by_name = {f.name: f for f in failures}
    assert set(by_name) == {"Test", "external-linter"}
    assert by_name["Test"].run_id == "pipe-1"  # pipeline step keeps its log evidence
    assert by_name["external-linter"].run_id is None  # external status, no pipeline log
    assert by_name["external-linter"].log_excerpt == ""


async def test_failing_check_logs_skips_pipeline_status_identified_by_url() -> None:
    """The pipeline's own commit status (recognised by its /pipelines/ url) is not
    double-counted on top of the per-step failures, even without a ``PIPELINE`` key."""
    fake = FakeBitBucket()
    fake.page(
        "GET",
        f"{_REPO}/commit/{_HEAD}/statuses",
        values=[
            {
                "state": "FAILED",
                "name": "Pipeline #7",
                "url": "https://bitbucket.org/workspace/repo/pipelines/results/7",
            }
        ],
    )
    fake.page("GET", _PIPELINES, values=[{"uuid": "pipe-1", "state": {"name": "COMPLETED"}}])
    fake.page(
        "GET",
        f"{_REPO}/pipelines/pipe-1/steps/",
        values=[{"uuid": "step-2", "name": "Test", "state": {"result": {"name": "FAILED"}}}],
    )
    fake.enqueue(
        "GET",
        f"{_REPO}/pipelines/pipe-1/steps/step-2/log",
        text="FAILED tests/test_x.py::test_y\n",
    )
    client = make_client(fake)
    failures = await client.fetch_failing_check_logs(repo=repo(), pr_number=42, head_sha=_HEAD)
    assert [f.name for f in failures] == ["Test"]  # no duplicate external row for the pipeline


async def test_failing_check_logs_scopes_statuses_by_refname() -> None:
    """Scope the commit-statuses fetch by the PR source branch (refname).

    BitBucket statuses are ref-scoped; ``fetch_pr_status`` already filters by the
    source branch, so ``fetch_failing_check_logs`` must use the same refname (from
    remembered PR context) or the two calls can disagree about which statuses exist
    for the head commit.
    """
    fake = FakeBitBucket()
    _seed_fetch_status(fake)  # primes _pr_context with source branch "feature/head"
    fake.page(
        "GET",
        f"{_REPO}/commit/{_HEAD}/statuses",
        values=[{"state": "SUCCESSFUL", "name": "ok"}],
    )
    client = make_client(fake)
    await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    await client.fetch_failing_check_logs(repo=repo(), pr_number=42, head_sha=_HEAD)
    statuses_calls = [
        r for r in fake.calls("GET") if r.url.path == f"{_REPO}/commit/{_HEAD}/statuses"
    ]
    assert len(statuses_calls) == 2
    assert all(r.url.params.get("refname") == "feature/head" for r in statuses_calls)


async def test_failing_check_logs_omits_refname_without_pr_context() -> None:
    """Without remembered PR context, fall back to an unscoped statuses fetch."""
    fake = FakeBitBucket()
    fake.page(
        "GET",
        f"{_REPO}/commit/{_HEAD}/statuses",
        values=[{"state": "SUCCESSFUL", "name": "ok"}],
    )
    client = make_client(fake)
    failures = await client.fetch_failing_check_logs(repo=repo(), pr_number=42, head_sha=_HEAD)
    assert failures == ()
    statuses_call = next(
        r for r in fake.calls("GET") if r.url.path == f"{_REPO}/commit/{_HEAD}/statuses"
    )
    assert "refname" not in statuses_call.url.params


async def test_fake_serves_distinct_responses_by_query_params() -> None:
    """The fake transport keys canned responses by query string when asked.

    Regression for the helper limitation flagged on PR #443: two calls to the same
    path with different query strings (e.g. an unscoped vs a ``refname``-scoped
    statuses fetch) must be able to return independent responses, so a test can
    prove that different params yield different observable behavior — not merely
    that a param was sent. The path-only enqueue still resolves the unscoped call.
    """
    fake = FakeBitBucket()
    path = f"{_REPO}/commit/{_HEAD}/statuses"
    fake.page("GET", path, values=[{"state": "SUCCESSFUL", "name": "unscoped"}])
    fake.page(
        "GET",
        path,
        values=[{"state": "FAILED", "name": "scoped"}],
        params={"refname": "feature/head"},
    )
    async with fake.client() as http:
        unscoped = await http.get(path)
        scoped = await http.get(path, params={"refname": "feature/head"})
    assert unscoped.json()["values"][0]["name"] == "unscoped"
    assert scoped.json()["values"][0]["name"] == "scoped"


async def test_failing_check_logs_pipeline_scoped_to_pr_ref() -> None:
    """Pick the pipeline whose ref matches the PR source branch, not just newest.

    Regression for PRRT_kwDOSJAM6s6HnDUF: the commit-status fetch is scoped by
    refname to the PR source branch, so the pipeline lookup must apply the same
    ref context. With both a branch and a PR pipeline on one commit, choosing the
    newest by ``target.commit.hash`` alone could tail steps from a different ref.
    """
    fake = FakeBitBucket()
    _seed_fetch_status(fake)  # primes _pr_context with source branch "feature/head"
    fake.page(
        "GET",
        f"{_REPO}/commit/{_HEAD}/statuses",
        values=[{"state": "FAILED", "name": "Pipeline #5", "key": "PIPELINE"}],
    )
    fake.page(
        "GET",
        _PIPELINES,
        values=[
            # newest, but for a different ref → must be skipped
            {"uuid": "other-ref", "target": {"ref_name": "main"}},
            # older, but a PR pipeline whose source is this PR's branch → selected
            {"uuid": "pipe-1", "target": {"source": "feature/head"}},
        ],
    )
    fake.page(
        "GET",
        f"{_REPO}/pipelines/pipe-1/steps/",
        values=[{"uuid": "step-2", "name": "Test", "state": {"result": {"name": "FAILED"}}}],
    )
    fake.enqueue(
        "GET",
        f"{_REPO}/pipelines/pipe-1/steps/step-2/log",
        text="FAILED tests/test_x.py::test_y\n",
    )
    client = make_client(fake)
    await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    failures = await client.fetch_failing_check_logs(repo=repo(), pr_number=42, head_sha=_HEAD)
    assert [f.name for f in failures] == ["Test"]
    assert failures[0].run_id == "pipe-1"  # matched the PR ref, not the newer other-ref pipeline


async def test_failing_check_logs_pipeline_wrong_ref_falls_back_to_external() -> None:
    """When every candidate pipeline targets a different ref, don't tail its log.

    Returning a wrong-ref pipeline would attach an unrelated step log to the PR's
    failing status, so the lookup yields no pipeline and the status falls back to
    the external (pytest) evidence path instead.
    """
    fake = FakeBitBucket()
    _seed_fetch_status(fake)
    fake.page(
        "GET",
        f"{_REPO}/commit/{_HEAD}/statuses",
        values=[{"state": "FAILED", "name": "Pipeline #5", "key": "PIPELINE"}],
    )
    fake.page("GET", _PIPELINES, values=[{"uuid": "other-ref", "target": {"ref_name": "main"}}])
    client = make_client(fake)
    await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    failures = await client.fetch_failing_check_logs(
        repo=repo(), pr_number=42, head_sha=_HEAD, pytest_fallback_commands=["uv run pytest -q"]
    )
    assert len(failures) == 1
    assert failures[0].run_id is None  # no matching pipeline → external fallback, no wrong log


async def test_failing_check_logs_other_pr_pipeline_falls_back_to_external() -> None:
    """A PR-only pipeline for a *different* PR must not be mis-attributed.

    Regression for PRRT_kwDOSJAM6s6HoN62: when candidate pipelines carry only a
    ``target.pullrequest.id`` (no ``ref_name``/``source``) and it belongs to
    another PR, neither the PR-id nor the branch match fires. The wrong-ref guard
    must still recognise the pipeline as ref-bearing and bail to the external
    (pytest) fallback instead of attaching the other PR's step log via
    ``pipelines[0]``.
    """
    fake = FakeBitBucket()
    _seed_fetch_status(fake)  # this monitor is PR 42 on branch "feature/head"
    fake.page(
        "GET",
        f"{_REPO}/commit/{_HEAD}/statuses",
        values=[{"state": "FAILED", "name": "Pipeline #5", "key": "PIPELINE"}],
    )
    fake.page(
        "GET",
        _PIPELINES,
        values=[{"uuid": "other-pr", "target": {"pullrequest": {"id": 99}}}],
    )
    client = make_client(fake)
    await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    failures = await client.fetch_failing_check_logs(
        repo=repo(), pr_number=42, head_sha=_HEAD, pytest_fallback_commands=["uv run pytest -q"]
    )
    assert len(failures) == 1
    assert failures[0].run_id is None  # other PR's pipeline → external fallback, no wrong log


async def test_failing_check_logs_pipeline_without_ref_metadata_keeps_newest() -> None:
    """When pipelines expose no ref identity, keep the newest (legacy behavior).

    Older pipeline payloads may omit target ref metadata; rather than drop the
    log evidence, fall back to the most recent pipeline by commit.
    """
    fake = FakeBitBucket()
    _seed_fetch_status(fake)
    fake.page(
        "GET",
        f"{_REPO}/commit/{_HEAD}/statuses",
        values=[{"state": "FAILED", "name": "Pipeline #5", "key": "PIPELINE"}],
    )
    fake.page("GET", _PIPELINES, values=[{"uuid": "pipe-1", "state": {"name": "COMPLETED"}}])
    fake.page(
        "GET",
        f"{_REPO}/pipelines/pipe-1/steps/",
        values=[{"uuid": "step-2", "name": "Test", "state": {"result": {"name": "FAILED"}}}],
    )
    fake.enqueue(
        "GET",
        f"{_REPO}/pipelines/pipe-1/steps/step-2/log",
        text="FAILED tests/test_x.py::test_y\n",
    )
    client = make_client(fake)
    await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    failures = await client.fetch_failing_check_logs(repo=repo(), pr_number=42, head_sha=_HEAD)
    assert [f.name for f in failures] == ["Test"]
    assert failures[0].run_id == "pipe-1"


async def test_failing_check_logs_prefers_pr_pipeline_over_branch_pipeline() -> None:
    """Prefer the PR pipeline over a same-branch branch pipeline on one commit.

    Regression for PRRT_kwDOSJAM6s6HnnXW: Bitbucket can record both a branch
    pipeline and a pull-request pipeline for the same commit on the same source
    branch. Both match the branch ref, so picking the newest branch match could
    tail the unrelated branch pipeline's steps. The lookup must prefer the
    pipeline whose ``target.pullrequest.id`` is this PR.
    """
    fake = FakeBitBucket()
    _seed_fetch_status(fake)  # primes _pr_context with source branch "feature/head"
    fake.page(
        "GET",
        f"{_REPO}/commit/{_HEAD}/statuses",
        values=[{"state": "FAILED", "name": "Pipeline #5", "key": "PIPELINE"}],
    )
    fake.page(
        "GET",
        _PIPELINES,
        values=[
            # newest, a branch pipeline on the same source branch → must be skipped
            {"uuid": "branch-pipe", "target": {"ref_name": "feature/head"}},
            # older, the PR pipeline for this PR → selected by pullrequest id
            {
                "uuid": "pr-pipe",
                "target": {"source": "feature/head", "pullrequest": {"id": 42}},
            },
        ],
    )
    fake.page(
        "GET",
        f"{_REPO}/pipelines/pr-pipe/steps/",
        values=[{"uuid": "step-2", "name": "Test", "state": {"result": {"name": "FAILED"}}}],
    )
    fake.enqueue(
        "GET",
        f"{_REPO}/pipelines/pr-pipe/steps/step-2/log",
        text="FAILED tests/test_x.py::test_y\n",
    )
    client = make_client(fake)
    await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    failures = await client.fetch_failing_check_logs(repo=repo(), pr_number=42, head_sha=_HEAD)
    assert [f.name for f in failures] == ["Test"]
    assert failures[0].run_id == "pr-pipe"  # PR pipeline, not the newer branch pipeline


# ── rerun_failed_workflow_jobs ────────────────────────────────────────────────


async def test_rerun_reconstructs_pr_pipeline_target() -> None:
    fake = FakeBitBucket()
    _seed_fetch_status(fake)
    fake.enqueue("POST", _PIPELINES, json={"uuid": "new-pipe"})
    client = make_client(fake)
    await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    await client.rerun_failed_workflow_jobs(repo=repo(), run_id="pipe-1")
    payload = json.loads(fake.calls("POST")[0].content)
    target = payload["target"]
    assert target["type"] == "pipeline_pullrequest_target"
    assert target["source"] == "feature/head"
    assert target["destination"] == "development"
    assert target["commit"]["hash"] == "s" * 40
    assert target["pullrequest"]["id"] == 42
    # BitBucket's PR pipeline selector type is the hyphenated "pull-requests"
    # (verified against the live POST /pipelines docs); an underscore would not
    # match the pull-requests pipeline definition.
    assert target["selector"] == {"type": "pull-requests", "pattern": "**"}


async def test_rerun_without_pr_context_is_not_rerunnable() -> None:
    fake = FakeBitBucket()
    client = make_client(fake)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client.rerun_failed_workflow_jobs(repo=repo(), run_id="x")
    assert excinfo.value.reason_code == BITBUCKET_PIPELINE_NOT_RERUNNABLE
    assert fake.calls("POST") == []  # never triggered a wrong pipeline


async def test_rerun_incomplete_context_is_not_rerunnable() -> None:
    fake = FakeBitBucket()
    pr = pr_payload()
    del pr["destination"]["commit"]  # no destination commit → cannot reconstruct safely
    _seed_fetch_status(fake, pr=pr)
    client = make_client(fake)
    await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client.rerun_failed_workflow_jobs(repo=repo(), run_id="x")
    assert excinfo.value.reason_code == BITBUCKET_PIPELINE_NOT_RERUNNABLE
    assert fake.calls("POST") == []


# ── merge-method resolution from PR context (D5) ──────────────────────────────


async def test_fetch_repo_merge_methods_maps_from_pr_context() -> None:
    fake = FakeBitBucket()
    _seed_fetch_status(
        fake, pr=pr_payload(merge_strategies=["merge_commit", "squash", "fast_forward"])
    )
    client = make_client(fake)
    await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    methods = await client.fetch_repo_merge_methods(repo=repo())
    assert methods == ("merge", "squash", "fast_forward")


async def test_fetch_repo_merge_methods_without_context_is_empty() -> None:
    fake = FakeBitBucket()
    client = make_client(fake)
    methods = await client.fetch_repo_merge_methods(repo=repo())
    assert methods == ()


async def test_fetch_branch_merge_methods_fast_forward_only() -> None:
    fake = FakeBitBucket()
    _seed_fetch_status(fake, pr=pr_payload(merge_strategies=["fast_forward"]))
    client = make_client(fake)
    await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    methods = await client.fetch_branch_pull_request_allowed_merge_methods(
        repo=repo(), branch="development"
    )
    assert methods == ("fast_forward",)


async def test_fetch_branch_merge_methods_absent_returns_none() -> None:
    fake = FakeBitBucket()
    _seed_fetch_status(fake, pr=pr_payload())  # no merge_strategies on dest branch
    client = make_client(fake)
    await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    methods = await client.fetch_branch_pull_request_allowed_merge_methods(
        repo=repo(), branch="development"
    )
    assert methods is None


# ── from_env ──────────────────────────────────────────────────────────────────


def test_from_env_builds_client() -> None:
    client = BitBucketClient.from_env(
        {"BITBUCKET_AUTH_MODE": "bearer", "BITBUCKET_API_TOKEN": "tok"}
    )
    assert isinstance(client, BitBucketClient)


def test_from_env_invalid_config_raises() -> None:
    with pytest.raises(BitBucketClientError) as excinfo:
        BitBucketClient.from_env({})
    assert excinfo.value.reason_code == BITBUCKET_AUTH_NOT_CONFIGURED


# ── lifecycle ───────────────────────────────────────────────────────────────────


async def test_aclose_closes_underlying_httpx_client() -> None:
    fake = FakeBitBucket()
    client = make_client(fake)
    await client.aclose()
    assert client._client.is_closed


async def test_async_context_manager_closes_on_exit() -> None:
    fake = FakeBitBucket()
    client = make_client(fake)
    async with client as entered:
        assert entered is client
        assert not client._client.is_closed
    assert client._client.is_closed
