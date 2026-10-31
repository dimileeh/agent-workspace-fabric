"""Real HTTP loss/rollback and the ordinary forge client's credential boundary."""

import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from awf.common.commands import AsyncioSubprocessRunner, CommandResult
from awf.db.models import Operation
from tests.integration.test_hosted_lifecycle_parity import fresh_association, source_workspace
from tests.integration.test_hosted_lifecycle_successors import adoption
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import create_application as create_application
from tests.unit.test_hosted_runtime_review_create import create_client as create_client


@pytest.mark.parametrize("action", ["guide", "remonitor", "retry", "adopt-pr"])
async def test_lost_response_replays_committed_effect(
    authority, create_application, create_client, tmp_path, action
):
    app, headers = create_application
    if action == "adopt-pr":
        body = await adoption(create_application, authority)
        path = "/v1/workspaces/adopt-pr"
    else:
        await source_workspace(
            authority,
            create_client,
            tmp_path,
            status="failed" if action == "retry" else "monitoring_pr",
        )
        path = f"/v1/workspaces/ws_cloud_stable/{action}"
        body = {
            "reason": "decision",
            "hosted_authorization": {"namespace": "tenant", "actor": "caller"},
        }
        if action == "retry":
            body = {"hosted": await fresh_association(authority[0], authority[1])}
        elif action == "guide":
            body["directive"] = "Continue review"
    failed = False

    async def drop_reply(scope, receive, send):
        async def lost(message):
            nonlocal failed
            if message["type"] == "http.response.start" and not failed:
                failed = True
                raise ConnectionError("synthetic lost HTTP response")
            await send(message)

        await app(scope, receive, lost)

    async with AsyncClient(
        transport=ASGITransport(app=drop_reply), base_url="http://test"
    ) as client:
        with pytest.raises(ConnectionError, match="lost HTTP"):
            await client.post(path, json=body, headers={**headers, "Idempotency-Key": "lost"})
    async with authority[2]() as session:
        persisted = list(
            await session.scalars(
                select(Operation).where(Operation.type == action.replace("-", "_"))
            )
        )
        assert len(persisted) == 1
        operation_id = persisted[0].id
    replay = await create_client[0].post(
        path, json=body, headers={**headers, "Idempotency-Key": "lost"}
    )
    assert replay.status_code == (200 if action in {"guide", "remonitor"} else 202), replay.text
    async with authority[2]() as session:
        assert await session.get(Operation, operation_id) is not None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Operation)
                .where(Operation.type == action.replace("-", "_"))
            )
            == 1
        )


@pytest.mark.parametrize("failure", [False, True])
async def test_adoption_default_forge_uses_only_fresh_material(
    authority, create_client, monkeypatch, failure
):
    association = await fresh_association(authority[0], authority[1])
    observed = []

    async def subprocess(self, args, **kwargs):
        observed.append((args, kwargs["env"]))
        if failure:
            return CommandResult(returncode=1, stdout="", stderr="refused fresh-github fresh-agent")
        return CommandResult(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "number": 42,
                    "headRefName": "feature/existing",
                    "headRepository": {"nameWithOwner": "example/repo"},
                    "isCrossRepository": False,
                    "baseRefName": "main",
                    "headRefOid": "a" * 40,
                    "baseRefOid": "b" * 40,
                    "state": "OPEN",
                    "isDraft": False,
                    "author": {"login": "caller"},
                    "url": "https://github.com/example/repo/pull/42",
                    "title": "Existing PR",
                }
            ),
        )

    monkeypatch.setattr(AsyncioSubprocessRunner, "run", subprocess)
    monkeypatch.setenv("GH_TOKEN", "wrong-inherited-token")
    monkeypatch.setenv("UNRELATED_SECRET", "never-inherited")
    response = await create_client[0].post(
        "/v1/workspaces/adopt-pr",
        json={"pr_url": "https://github.com/example/repo/pull/42", "hosted": association},
        headers=create_client[1],
    )
    assert response.status_code == (502 if failure else 202), response.text
    assert len(observed) == 1
    args, env = observed[0]
    assert args[:3] == ["gh", "pr", "view"]
    assert env["GH_TOKEN"] == "fresh-github" and env["CODEX_API_KEY"] == "fresh-agent"
    assert "UNRELATED_SECRET" not in env and "wrong-inherited-token" not in env.values()
    assert "fresh-github" not in response.text and "fresh-agent" not in response.text
