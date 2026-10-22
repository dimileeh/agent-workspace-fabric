import base64
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text


async def prepare(hosted):
    client, receiver, _ = hosted
    expires = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    preparation = {
        "namespace": "ns",
        "workspace_id": "ws",
        "preparation_id": "prep",
        "purpose": "credential",
        "target": "/run/awf/secrets/key",
        "expires_at": expires,
    }
    assert (await client.post("/v1/hosted-node/preparations", json=preparation)).status_code == 200
    return dict(
        **preparation,
        identity=receiver.identity.model_dump(),
        idempotency_key="install",
        grant_id="grant",
        revision=1,
        authorization_generation=1,
        fence=1,
    )


async def test_install_replay_refresh_revoke_and_no_disclosure(hosted, caplog):
    client, receiver, _ = hosted
    binding = await prepare(hosted)
    canary = secrets.token_urlsafe(32).encode()
    encoded = base64.b64encode(canary).decode()
    response = await client.post(
        "/v1/hosted-node/materials/install", json={**binding, "material": encoded}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "current"
    replay = await client.post(
        "/v1/hosted-node/materials/install",
        json={**binding, "material": base64.b64encode(b"different").decode()},
    )
    assert replay.json() == response.json()
    status = await client.post("/v1/hosted-node/materials/status", json=binding)
    assert status.json() == response.json()
    newer = {**binding, "idempotency_key": "refresh", "revision": 2, "fence": 2}
    response2 = await client.post(
        "/v1/hosted-node/materials/refresh",
        json={
            **newer,
            "material": encoded,
            "predecessor": response.json()["receipt_id"],
        },
    )
    assert response2.status_code == 200, response2.text
    assert response2.json()["status"] == "current"
    assert (await client.post("/v1/hosted-node/materials/status", json=binding)).json()[
        "status"
    ] == "not_accepted"
    revoke = {**newer, "idempotency_key": "revoke", "fence": 3}
    assert (await client.post("/v1/hosted-node/materials/revoke", json=revoke)).json()[
        "status"
    ] == "revoked"
    assert (await client.post("/v1/hosted-node/materials/status", json=newer)).json()[
        "status"
    ] == "revoked"
    assert (
        await client.post(
            "/v1/hosted-node/materials/install", json={**binding, "material": encoded}
        )
    ).json()["status"] == "revoked"
    assert not list(receiver.store.root.glob("*/*.material"))
    async with receiver.factory() as session:
        for table in (
            "hosted_owners",
            "hosted_preparations",
            "hosted_grants",
            "hosted_receipts",
            "workspace_events",
        ):
            rows = str((await session.execute(text(f"SELECT row_to_json(t) FROM {table} t"))).all())
            assert encoded not in rows
            assert canary.decode() not in rows
    assert encoded not in caplog.text
    assert canary.decode() not in caplog.text
    assert encoded not in response.text + response2.text + status.text


async def test_bad_input_never_echoed(hosted):
    client, _, _ = hosted
    binding = await prepare(hosted)
    canary = secrets.token_urlsafe(32)
    for payload in (
        {**binding, "material": canary},
        {**binding, "material": "YQ==", "extra": canary},
        {**binding, "material": "YQ==", "target": "/run/awf/secrets/../escape"},
    ):
        response = await client.post("/v1/hosted-node/materials/install", json=payload)
        assert response.status_code == 422
        assert canary not in response.text
    response = await client.post("/v1/hosted-node/materials/install", content=canary * 5000)
    assert response.status_code == 413
    assert canary not in response.text


async def test_exact_binding_and_revoke_before_install(hosted):
    client, _, _ = hosted
    binding = await prepare(hosted)
    assert (await client.post("/v1/hosted-node/materials/status", json=binding)).json()[
        "status"
    ] == "not_accepted"
    revoke = {**binding, "idempotency_key": "revoke", "fence": 2}
    assert (await client.post("/v1/hosted-node/materials/revoke", json=revoke)).json()[
        "status"
    ] == "revoked"
    response = await client.post(
        "/v1/hosted-node/materials/install", json={**binding, "material": "YQ=="}
    )
    assert response.status_code == 409
    for field in ("namespace", "workspace_id", "preparation_id"):
        response = await client.post(
            "/v1/hosted-node/materials/status", json={**binding, field: "foreign"}
        )
        assert response.status_code == 404


async def test_lost_response_reconciles_committed_receipt(hosted, monkeypatch):
    client, receiver, _ = hosted
    binding = await prepare(hosted)
    original = receiver.status

    async def response_lost(value):
        raise OSError("injected response failure")

    monkeypatch.setattr(receiver, "status", response_lost)
    first = await client.post(
        "/v1/hosted-node/materials/install", json={**binding, "material": "YQ=="}
    )
    assert first.status_code == 503
    monkeypatch.setattr(receiver, "status", original)
    replay = await client.post(
        "/v1/hosted-node/materials/install", json={**binding, "material": "Yg=="}
    )
    assert replay.json()["status"] == "current"
    files = list(receiver.store.root.glob("*/*.material"))
    assert len(files) == 1 and files[0].read_bytes() == b"a"


async def test_preparation_bounds_and_idempotency_conflicts(hosted):
    client, receiver, _ = hosted
    binding = await prepare(hosted)
    preparation = {
        k: binding[k]
        for k in ("namespace", "workspace_id", "preparation_id", "purpose", "target", "expires_at")
    }
    assert (await client.post("/v1/hosted-node/preparations", json=preparation)).status_code == 200
    assert (
        await client.post(
            "/v1/hosted-node/preparations", json={**preparation, "target": "/run/awf/secrets/other"}
        )
    ).status_code == 409
    far_future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    assert (
        await client.post(
            "/v1/hosted-node/preparations", json={**preparation, "expires_at": far_future}
        )
    ).status_code == 422
    for material in ("", base64.b64encode(b"x" * 65537).decode()):
        assert (
            await client.post(
                "/v1/hosted-node/materials/install", json={**binding, "material": material}
            )
        ).status_code == 422
    assert (
        await client.post("/v1/hosted-node/materials/install", json={**binding, "material": "YQ=="})
    ).status_code == 200
    assert (await client.post("/v1/hosted-node/materials/revoke", json=binding)).status_code == 409
    for field in ("grant_id", "idempotency_key"):
        replacement = {**binding, field: "other", "material": "YQ=="}
        assert (
            await client.post("/v1/hosted-node/materials/install", json=replacement)
        ).status_code == 409


async def test_legacy_reissue_cannot_clear_protected_tombstone(hosted):
    from awf.db.repositories import WorkspaceRepository
    from awf.profiles.models import WorkspaceProfile
    from awf.service.secret_leases import SecretLeaseService

    client, receiver, _ = hosted
    binding = await prepare(hosted)
    revoke = {**binding, "fence": 2, "idempotency_key": "revoke"}
    assert (await client.post("/v1/hosted-node/materials/revoke", json=revoke)).status_code == 200
    async with receiver.factory.begin() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="https://example.invalid/project.git",
            branch_base="main",
            task_title="synthetic",
            task_prompt="synthetic",
            agent="codex",
            test_commands=[],
        )
        profile = WorkspaceProfile.model_validate(
            {
                "name": "synthetic",
                "secrets": [{"name": "key", "target": binding["target"]}],
            }
        )
        service = SecretLeaseService(session)
        await service.issue_profile_secret_leases(workspace, profile)
        await service.revoke_workspace_secret_leases(workspace, reason_code="SYNTHETIC")
        await service.issue_profile_secret_leases(workspace, profile)
    assert (await client.post("/v1/hosted-node/materials/status", json=binding)).json()[
        "status"
    ] == "revoked"


async def test_revoke_records_cleanup_after_durable_denial(hosted):
    from sqlalchemy import select

    from awf.hosted_node.models import Receipt

    client, receiver, _ = hosted
    binding = await prepare(hosted)
    revoke = {**binding, "fence": 2, "idempotency_key": "revoke"}
    assert (await client.post("/v1/hosted-node/materials/revoke", json=revoke)).status_code == 200
    async with receiver.factory() as session:
        receipt = (await session.scalars(select(Receipt))).one()
        assert receipt.state == "cleaned"


async def test_exact_node_and_refresh_high_water_marks(hosted):
    client, _, _ = hosted
    binding = await prepare(hosted)
    first = await client.post(
        "/v1/hosted-node/materials/install", json={**binding, "material": "YQ=="}
    )
    for field, value in (
        ("owner", "foreign"),
        ("incarnation", "foreign"),
        ("boot_epoch", 99),
        ("recovery_generation", 99),
    ):
        response = await client.post(
            "/v1/hosted-node/materials/status",
            json={
                **binding,
                "identity": {**binding["identity"], field: value},
            },
        )
        assert response.status_code == 403
    newer = {
        **binding,
        "idempotency_key": "refresh",
        "revision": 2,
        "authorization_generation": 2,
        "fence": 2,
        "material": "Yg==",
        "predecessor": first.json()["receipt_id"],
    }
    for changes in ({"revision": 1}, {"fence": 1}, {"predecessor": "wrong"}):
        assert (
            await client.post("/v1/hosted-node/materials/refresh", json={**newer, **changes})
        ).status_code == 409
    assert (await client.post("/v1/hosted-node/materials/refresh", json=newer)).json()[
        "status"
    ] == "current"
    third = {
        **newer,
        "idempotency_key": "third",
        "revision": 3,
        "fence": 3,
        "authorization_generation": 1,
    }
    assert (await client.post("/v1/hosted-node/materials/refresh", json=third)).status_code == 409


@pytest.mark.parametrize("prior", ["install", "revoke", "tombstone", "pending"])
async def test_preparation_authority_survives_grant_replacement(hosted, peer, prior, monkeypatch):
    client, receiver, _ = hosted
    other, _, _ = peer
    binding = await prepare(hosted)
    authority = {**binding, "fence": 9, "authorization_generation": 10}
    if prior != "tombstone":
        with monkeypatch.context() as patch:
            if prior == "pending":

                def fail_install(*args):
                    raise OSError("interrupted publication")

                patch.setattr(receiver.store, "install", fail_install)
            initial = await client.post(
                "/v1/hosted-node/materials/install", json={**authority, "material": "YQ=="}
            )
        assert initial.json()["status"] == ("unavailable" if prior == "pending" else "current")
    if prior in ("revoke", "tombstone"):
        authority = {**authority, "fence": 10, "idempotency_key": "revoke"}
        assert (await client.post("/v1/hosted-node/materials/revoke", json=authority)).json()[
            "status"
        ] == "revoked"
    files = set(receiver.store.root.glob("*/*.material"))
    # Independent handlers must observe durable maxima, not process-local state.
    for fence, generation in ((1, 1), (authority["fence"], 11), (11, 1)):
        stale = {
            **binding,
            "grant_id": "replacement",
            "idempotency_key": f"stale-{fence}-{generation}",
            "fence": fence,
            "authorization_generation": generation,
            "material": "Yg==",
        }
        response = await other.post("/v1/hosted-node/materials/install", json=stale)
        assert response.status_code == 409, response.text
        assert "GRANT_CONFLICT" in response.text
        assert set(receiver.store.root.glob("*/*.material")) == files
    replacement = {
        **binding,
        "grant_id": "replacement",
        "idempotency_key": "replacement",
        "fence": 11,
        "authorization_generation": 10,
        "material": "Yg==",
    }
    accepted = await other.post("/v1/hosted-node/materials/install", json=replacement)
    assert accepted.json()["status"] == "current"
    assert (
        await client.post("/v1/hosted-node/materials/install", json=replacement)
    ).json() == accepted.json()
    if prior == "install":
        # The old grant also cannot refresh below the replacement's fence.
        assert (
            await client.post(
                "/v1/hosted-node/materials/refresh",
                json={
                    **authority,
                    "fence": 10,
                    "revision": 2,
                    "idempotency_key": "delayed-refresh",
                    "predecessor": initial.json()["receipt_id"],
                    "material": "Yw==",
                },
            )
        ).status_code == 409
    # A separate preparation has its own authority sequence.
    preparation = {
        key: binding[key]
        for key in ("namespace", "workspace_id", "purpose", "target", "expires_at")
    }
    preparation["preparation_id"] = "independent"
    assert (await client.post("/v1/hosted-node/preparations", json=preparation)).status_code == 200
    assert (
        await client.post(
            "/v1/hosted-node/materials/install",
            json={**binding, **preparation, "idempotency_key": "independent", "material": "YQ=="},
        )
    ).json()["status"] == "current"
