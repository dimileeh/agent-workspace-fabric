"""Real HTTP/PG refresh across active and retained runtime material readers."""

import asyncio
import base64
import copy
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import UTC, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from awf.db.models import Workspace
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.models import Receipt
from awf.hosted_node.receiver import Receiver
from awf.hosted_node.resolver import ProtectedResolver
from awf.hosted_node.runtime_consumers import WorkspaceConsumers
from awf.hosted_node.runtime_views import RuntimeViews, owned_publication
from awf.hosted_node.schemas import Binding, Preparation
from awf.profiles.models import WorkspaceProfile
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_contract import hosted_create

from .test_preparation_renewal import PREFIX
from .test_preparation_renewal import clock as clock
from .test_preparation_renewal_runtime import renewal_workspace as renewal_workspace


@pytest.fixture
async def cutover(renewal_workspace):
    client, runtime, original, _, slots, clock = renewal_workspace
    current = copy.deepcopy(original["credentials"])

    async def refresh(*, generation=False, roles=("agent", "github")):
        for role in roles:
            binding = current[role]
            binding.update(
                idempotency_key=f"cutover-{role}-{binding['revision'] + 1}",
                revision=binding["revision"] + 1,
                fence=binding["fence"] + 1,
                expires_at=(clock.now(UTC) + timedelta(minutes=20)).isoformat(),
            )
            if generation and role == "agent":
                binding["authorization_generation"] += 1
            response = await client.post(
                f"{PREFIX}/materials/refresh",
                json={
                    **binding,
                    "predecessor": slots[role],
                    "material": base64.b64encode(
                        f"synthetic-{role}-{binding['revision']}".encode()
                    ).decode(),
                },
            )
            assert response.status_code == 200 and response.json()["status"] == "current"
            slots[role] = response.json()["receipt_id"]

    return client, runtime, original, current, slots, clock, refresh


@asynccontextmanager
async def consumer(cutover, tmp_path, *, retained=False):
    _, runtime, original, *_ = cutover
    workspace_id = original["workspace_id"]
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.poll_seconds = 0.02
    scope.stop_workspace = AsyncMock()
    entered, leave = asyncio.Event(), asyncio.Event()
    owners = []

    async def execute():
        async with scope.scope(workspace_id):
            owners.append(owned_publication(runtime.views, workspace_id))
            entered.set()
            await leave.wait()

    active = asyncio.create_task(execute())
    descriptor = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if retained:
            leave.set()
            await asyncio.wait_for(active, 5)
            # Placement only: public create supplied all authority/association
            # rows; no terminal state, acknowledgement or material is invented.
            async with runtime.receiver.factory.begin() as session:
                row = await session.get(Workspace, workspace_id)
                row.node_id = runtime.receiver.identity.owner
        path = runtime.receiver.store.root / runtime.views._key(workspace_id) / "view"
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        yield scope, active, owners[0], path, descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not active.done():
            active.cancel()
        await asyncio.wait_for(asyncio.gather(active, return_exceptions=True), 5)


def read_view(descriptor, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
    with os.fdopen(fd, "rb") as stream:
        return stream.read()


def refresh_second_role_at_successor(
    monkeypatch, runtime, original, refresh, *, after_refresh=None
):
    status = runtime._snapshot_status
    observed = []

    async def validate(bindings):
        if (
            not observed
            and bindings["agent"].revision > original["credentials"]["agent"]["revision"]
            and bindings["github"].revision == original["credentials"]["github"]["revision"]
        ):
            observed.append(bindings)
            await refresh(roles=("github",))
            if after_refresh is not None:
                await after_refresh()
        return await status(bindings)

    monkeypatch.setattr(runtime, "_snapshot_status", validate)
    return observed


def hold_boundary(monkeypatch, runtime, boundary, selected):
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_current = runtime._current
    original_resolve = ProtectedResolver.resolve_current
    original_thread = asyncio.to_thread
    original_materials = runtime.materials
    armed = True
    denials = []
    in_call = ContextVar("cutover_call", default=False)
    reads = 0

    async def pause():
        nonlocal armed
        if (selected() or in_call.get()) and armed:
            armed = False
            entered.set()
            await asyncio.wait_for(release.wait(), 5)

    async def current(anchor):
        value = await original_current(anchor)
        if boundary == "capture":
            await pause()
        return value

    async def resolve(*args):
        value = await original_resolve(*args)
        if boundary == "slot-read":
            await pause()
        return value

    async def thread(function, *args, **kwargs):
        nonlocal reads
        value = await original_thread(function, *args, **kwargs)
        if function == runtime.receiver.store.read and in_call.get():
            reads += 1
        if (
            (boundary == "slot-status" and function == runtime.receiver.store.exists)
            or (boundary == "post-read" and function == runtime.receiver.store.read and reads == 2)
            or (
                boundary == "post-publication"
                and function in {runtime.views.publish, runtime.views.refresh}
            )
        ):
            await pause()
        return value

    async def materials(*args, **kwargs):
        token = in_call.set(selected())
        try:
            return await original_materials(*args, **kwargs)
        except (MaterialDeniedError, OSError) as error:
            if selected():
                denials.append(type(error).__name__)
            raise
        finally:
            if selected() and entered.is_set():
                finished.set()
            in_call.reset(token)

    monkeypatch.setattr(runtime, "_current", current)
    monkeypatch.setattr(ProtectedResolver, "resolve_current", resolve)
    monkeypatch.setattr(asyncio, "to_thread", thread)
    monkeypatch.setattr(runtime, "materials", materials)
    return entered, release, finished, denials


@pytest.mark.parametrize("retained", [False, True], ids=["watcher", "retained-sweep"])
@pytest.mark.parametrize("split_roles", [False, True], ids=["together", "successor-cutover"])
@pytest.mark.parametrize(
    "boundary", ["capture", "slot-status", "slot-read", "post-read", "post-publication"]
)
async def test_same_authority_cutover_keeps_consumer_and_held_view(
    cutover, tmp_path, monkeypatch, retained, split_roles, boundary
):
    _, runtime, original, current, _, _, refresh = cutover
    async with consumer(cutover, tmp_path, retained=retained) as state:
        scope, active, owner, path, descriptor = state
        inode = os.fstat(descriptor).st_ino
        selected_task = None

        def selected():
            task = asyncio.current_task()
            return (
                task is selected_task
                if retained
                else task.get_name().startswith("hosted-authority-")
            )

        entered, release, finished, denials = hold_boundary(
            monkeypatch, runtime, boundary, selected
        )
        second_role = (
            refresh_second_role_at_successor(monkeypatch, runtime, original, refresh)
            if split_roles
            else None
        )
        if retained:
            selected_task = asyncio.create_task(scope.admission_check())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await refresh(roles=("agent",) if split_roles else ("agent", "github"))
            release.set()
            await asyncio.wait_for(finished.wait(), 5)
            if retained:
                assert await asyncio.wait_for(selected_task, 5)
            elif denials:
                await asyncio.wait_for(asyncio.gather(active, return_exceptions=True), 5)
            assert not denials, denials
            assert second_role is None or len(second_role) == 1
            scope.stop_workspace.assert_not_awaited()
            assert retained or not active.done()
            publication = await runtime.publication(original["workspace_id"])
            assert publication.owner is owner
            assert path.stat().st_ino == os.fstat(descriptor).st_ino == inode
            for role, filename in [("agent", "openai.api-key"), ("github", "github.api-key")]:
                assert (
                    read_view(descriptor, filename)
                    == f"synthetic-{role}-{current[role]['revision']}".encode()
                )
            assert (await runtime.association(original["workspace_id"]))[0].model_dump(
                mode="json"
            ) == original
        finally:
            release.set()
            if selected_task is not None:
                await asyncio.wait_for(asyncio.gather(selected_task, return_exceptions=True), 5)


@pytest.mark.parametrize("boundary", ["slot-status", "post-publication", "successor-validation"])
@pytest.mark.parametrize(
    "failure",
    ["revoke", "generation", "grant", "boot", "expiry", "missing", "database", "os", "cancel"],
)
async def test_cutover_cannot_mask_real_loss_or_cancellation(
    cutover, tmp_path, monkeypatch, boundary, failure
):
    client, runtime, original, current, slots, clock, refresh = cutover
    successor = None
    async with consumer(cutover, tmp_path) as state:
        scope, active, _, path, descriptor = state
        inode = os.fstat(descriptor).st_ino
        entered, release, finished, _ = hold_boundary(
            monkeypatch,
            runtime,
            "capture" if boundary == "successor-validation" else boundary,
            lambda: asyncio.current_task().get_name().startswith("hosted-authority-"),
        )

        async def lose_authority():
            nonlocal successor
            binding = current["agent"]
            if failure in {"revoke", "grant"}:
                payload = {**binding, "idempotency_key": "loss", "fence": binding["fence"] + 1}
                operation = "revoke"
                if failure == "grant":
                    operation = "install"
                    payload.update(grant_id="foreign-grant", material="c3ludGhldGlj")
                response = await client.post(f"{PREFIX}/materials/{operation}", json=payload)
                assert response.status_code == 200
                assert response.json()["status"] == (
                    "revoked" if failure == "revoke" else "current"
                )
            elif failure == "boot":
                successor = await Receiver.start(runtime.receiver.config, runtime.receiver.factory)
            elif failure == "expiry":
                clock.instant += timedelta(minutes=21)
            elif failure == "missing":
                async with runtime.receiver.factory() as session:
                    preparation = await runtime.receiver.preparation(
                        session, Binding.model_validate(binding)
                    )
                runtime.receiver.store.remove(preparation.id, slots["agent"])
            elif failure == "database":

                async def unavailable(*args):
                    raise SQLAlchemyError("synthetic unavailable authority")

                monkeypatch.setattr(runtime, "_current", unavailable)
            elif failure == "os":

                def unreadable(*args):
                    raise PermissionError("synthetic unreadable material")

                monkeypatch.setattr(runtime.receiver.store, "read", unreadable)
            elif failure == "cancel":
                active.cancel()
            elif failure == "generation" and boundary == "successor-validation":
                await refresh(generation=True, roles=("agent",))

        try:
            await asyncio.wait_for(entered.wait(), 5)
            if boundary == "successor-validation":
                observed = refresh_second_role_at_successor(
                    monkeypatch, runtime, original, refresh, after_refresh=lose_authority
                )
                await refresh(roles=("agent",))
            else:
                observed = None
                await refresh(generation=failure == "generation")
                await lose_authority()
            release.set()
            await asyncio.wait_for(finished.wait(), 5)
            result = (await asyncio.wait_for(asyncio.gather(active, return_exceptions=True), 5))[0]
            expected = {
                "database": SQLAlchemyError,
                "os": PermissionError,
                "cancel": asyncio.CancelledError,
            }.get(failure, MaterialDeniedError)
            assert isinstance(result, expected)
            assert observed is None or len(observed) == 1
            if failure == "cancel":
                scope.stop_workspace.assert_awaited_once_with(
                    original["workspace_id"], authority_lost=False
                )
                assert path.stat().st_ino == inode and not list(path.iterdir())
                assert await runtime.reconcile_workspace(original["workspace_id"]) is False
                assert not list(path.iterdir())
            else:
                scope.stop_workspace.assert_awaited_once_with(original["workspace_id"])
                assert not path.exists()
        finally:
            release.set()
            if successor is not None:
                await successor.close()


@pytest.mark.parametrize("change", ["foreign", "cleared"])
@pytest.mark.parametrize("split_roles", [False, True], ids=["together", "successor-cutover"])
async def test_post_publication_retry_never_adopts_successor_or_resurrects_clear(
    cutover, tmp_path, monkeypatch, change, split_roles
):
    _, runtime, original, _, _, _, refresh = cutover
    async with consumer(cutover, tmp_path, retained=True) as state:
        scope, _, owner, path, descriptor = state
        selected = None
        entered, release, _, denials = hold_boundary(
            monkeypatch, runtime, "post-publication", lambda: asyncio.current_task() is selected
        )
        observed = (
            refresh_second_role_at_successor(monkeypatch, runtime, original, refresh)
            if split_roles
            else None
        )
        selected = asyncio.create_task(scope.admission_check())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await refresh(roles=("agent",) if split_roles else ("agent", "github"))
            if change == "foreign":
                newer = runtime.views.publish(
                    original["workspace_id"],
                    {
                        "openai.api-key": b"synthetic-foreign",
                        "github.api-key": b"synthetic-foreign",
                    },
                )
                assert newer.owner is not owner
            else:
                runtime.views.remove(original["workspace_id"], preserve_directory=True)
            release.set()
            assert await asyncio.wait_for(selected, 5)
            assert not denials
            assert observed is None or len(observed) == 1
            scope.stop_workspace.assert_not_awaited()
            assert path.stat().st_ino == os.fstat(descriptor).st_ino
            if change == "foreign":
                assert (await runtime.publication(original["workspace_id"])).token == newer.token
                assert read_view(descriptor, "openai.api-key") == b"synthetic-foreign"
            else:
                assert await runtime.publication(original["workspace_id"]) is None
                assert not list(path.iterdir())
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(selected, return_exceptions=True), 5)


@pytest.mark.parametrize("current_mode", [False, True])
async def test_snapshot_attempts_are_bounded_and_exact_binding_mode_never_follows(
    cutover, monkeypatch, current_mode
):
    _, runtime, original, _, _, _, refresh = cutover
    association, agent = await runtime.association(original["workspace_id"])
    resolve = ProtectedResolver.resolve_current
    attempts = 0

    async def cutover_before_resolution(*args):
        nonlocal attempts
        attempts += 1
        await refresh()
        return await resolve(*args)

    monkeypatch.setattr(ProtectedResolver, "resolve_current", cutover_before_resolution)
    with pytest.raises(MaterialDeniedError, match="PROTECTED_MATERIAL_UNAVAILABLE"):
        await runtime.materials(association, agent, current=current_mode)
    assert attempts == (3 if current_mode else 1)
    assert await runtime.publication(original["workspace_id"]) is None
    assert (await runtime.association(original["workspace_id"]))[0] == association


@pytest.mark.parametrize("current_mode", [False, True])
async def test_successor_validation_churn_spends_existing_snapshot_budget(
    cutover, monkeypatch, current_mode
):
    _, runtime, original, _, _, _, refresh = cutover
    association, agent = await runtime.association(original["workspace_id"])
    resolve = ProtectedResolver.resolve_current
    status = runtime._snapshot_status
    reads, validations = 0, 0

    async def first_role(*args):
        nonlocal reads
        reads += 1
        if reads == 1:
            await refresh(roles=("agent",))
        return await resolve(*args)

    async def next_role(bindings):
        nonlocal validations
        validations += 1
        await refresh(roles=("github" if validations % 2 else "agent",))
        return await status(bindings)

    monkeypatch.setattr(ProtectedResolver, "resolve_current", first_role)
    monkeypatch.setattr(runtime, "_snapshot_status", next_role)
    with pytest.raises(MaterialDeniedError, match="PROTECTED_MATERIAL_UNAVAILABLE"):
        await runtime.materials(association, agent, current=current_mode)
    assert reads == 1
    assert validations == (2 if current_mode else 0)
    assert await runtime.publication(original["workspace_id"]) is None
    assert (await runtime.association(original["workspace_id"]))[0] == association


@pytest.mark.parametrize("boundary", ["capture", "slot-read"])
@pytest.mark.parametrize("change", ["foreign", "cleared", "removed", "closing"])
@pytest.mark.parametrize("split_roles", [False, True], ids=["together", "successor-cutover"])
async def test_initial_retry_does_not_adopt_foreign_publication_before_first_write(
    cutover, tmp_path, monkeypatch, boundary, change, split_roles
):
    _, runtime, original, _, _, _, refresh = cutover
    workspace_id = original["workspace_id"]
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.stop_workspace = AsyncMock()
    active = None
    entered, release, _, _ = hold_boundary(
        monkeypatch, runtime, boundary, lambda: asyncio.current_task() is active
    )
    observed = (
        refresh_second_role_at_successor(monkeypatch, runtime, original, refresh)
        if split_roles
        else None
    )
    body, leave = asyncio.Event(), asyncio.Event()

    async def execute():
        async with scope.scope(workspace_id):
            body.set()
            await leave.wait()

    active = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        foreign = runtime.views.publish(
            workspace_id,
            {"openai.api-key": b"synthetic-foreign", "github.api-key": b"synthetic-foreign"},
        )
        await refresh(roles=("agent",) if split_roles else ("agent", "github"))
        if change in {"cleared", "removed"}:
            runtime.views.remove(workspace_id, preserve_directory=change == "cleared")
        elif change == "closing":
            active.cancel()
        release.set()
        if change == "closing":
            await asyncio.wait_for(asyncio.gather(active, return_exceptions=True), 5)
        else:
            await asyncio.wait_for(body.wait(), 5)
            assert observed is None or len(observed) == 1
        actual = await runtime.publication(workspace_id)
        if change in {"cleared", "removed"}:
            assert actual is None
            assert not foreign.path.exists() or not list(foreign.path.iterdir())
        else:
            assert actual.token == foreign.token
            assert actual.owner is foreign.owner
            assert (actual.path / "openai.api-key").read_bytes() == b"synthetic-foreign"
        if not active.done():
            active.cancel()
            await asyncio.wait_for(asyncio.gather(active, return_exceptions=True), 5)
        assert await runtime.publication(workspace_id) == actual
    finally:
        release.set()
        if not active.done():
            active.cancel()
        await asyncio.wait_for(asyncio.gather(active, return_exceptions=True), 5)


@pytest.mark.parametrize("change", ["present", "cleared", "removed", "absent"])
@pytest.mark.parametrize("same_instance", [False, True])
async def test_absent_publication_fence_is_checked_inside_filesystem_lock(
    authority, monkeypatch, change, same_instance
):
    runtime, _, _ = authority
    views = runtime.views
    workspace_id = "ws_atomic_absence"
    expected = views.absence_token(workspace_id)
    assert expected is not None
    entered, release = threading.Event(), threading.Event()
    directory = views.store._directory

    @contextmanager
    def held(*args, **kwargs):
        if threading.current_thread().name.startswith("cutover-writer"):
            entered.set()
            assert release.wait(5)
        with directory(*args, **kwargs) as parent:
            yield parent

    monkeypatch.setattr(views.store, "_directory", held)
    files = {"openai.api-key": b"synthetic-original", "github.api-key": b"synthetic-original"}
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="cutover-writer") as pool:
        attempt = pool.submit(views.publish, workspace_id, files, expected_absence=expected)
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            other = views if same_instance else RuntimeViews(views.store, identity=views.identity)
            foreign = other.publish(workspace_id, files) if change != "absent" else None
            if change != "present":
                other.remove(workspace_id, preserve_directory=change == "cleared")
            release.set()
            assert await asyncio.to_thread(attempt.result, 5) is None
            actual = views.publication(workspace_id)
            assert actual == (foreign if change == "present" else None)
            if foreign is not None and change != "present":
                assert not foreign.path.exists() or not list(foreign.path.iterdir())
        finally:
            release.set()


@pytest.mark.parametrize("boundary", ["capture", "slot-read"])
@pytest.mark.parametrize("preserve_directory", [False, True])
async def test_absent_retraction_fences_initial_consumer_but_allows_fresh_scope(
    cutover, tmp_path, monkeypatch, boundary, preserve_directory
):
    _, runtime, original, _, _, _, refresh = cutover
    workspace_id = original["workspace_id"]
    key = runtime.views._key(workspace_id)
    path = runtime.receiver.store.root / key
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.stop_workspace = AsyncMock()
    active = None
    entered, release, _, _ = hold_boundary(
        monkeypatch, runtime, boundary, lambda: asyncio.current_task() is active
    )

    async def execute():
        async with scope.scope(workspace_id):
            assert await runtime.publication(workspace_id) is None
            assert not path.exists()

    active = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not path.exists()
        original_absence = runtime.views._absence_tokens.get(key)
        assert original_absence is not None
        await runtime.retract(workspace_id, preserve_directory=preserve_directory)
        await refresh()
        release.set()
        await asyncio.wait_for(active, 5)
        assert not path.exists()
        later_absence = runtime.views.absence_token(workspace_id)
        assert later_absence is not None and later_absence is not original_absence
        async with scope.scope(workspace_id):
            publication = await runtime.publication(workspace_id)
            assert publication is not None
            assert publication.owner is owned_publication(runtime.views, workspace_id)
            assert (publication.path / "openai.api-key").read_bytes() == b"synthetic-agent-2"
        scope.stop_workspace.assert_not_awaited()
    finally:
        release.set()
        if not active.done():
            active.cancel()
        await asyncio.wait_for(asyncio.gather(active, return_exceptions=True), 5)


@pytest.mark.parametrize("preserve_directory", [False, True])
@pytest.mark.parametrize("same_instance", [False, True])
async def test_absent_cleanup_only_invalidates_target_workspace_and_boot(
    authority, preserve_directory, same_instance
):
    runtime, _, _ = authority
    views = runtime.views
    workspace_id = "ws_absent_cleanup"
    peer = views if same_instance else RuntimeViews(views.store, identity=views.identity)
    other_boot = RuntimeViews(
        views.store,
        identity=views.identity.model_copy(update={"boot_epoch": views.identity.boot_epoch + 1}),
    )
    original = views.absence_token(workspace_id)
    sibling = views.absence_token("ws_absent_sibling")
    foreign = other_boot.absence_token(workspace_id)
    assert original is not None and sibling is not None and foreign is not None
    peer.remove(workspace_id, preserve_directory=preserve_directory)
    assert views._absence_tokens.get(views._key(workspace_id)) is None
    assert views.absence_token("ws_absent_sibling") is sibling
    assert other_boot.absence_token(workspace_id) is foreign
    files = {"openai.api-key": b"synthetic-key", "github.api-key": b"synthetic-key"}
    assert views.publish(workspace_id, files, expected_absence=original) is None
    fresh = views.absence_token(workspace_id)
    assert fresh is not None and fresh is not original
    assert views.publish(workspace_id, files, expected_absence=fresh) is not None


@pytest.mark.parametrize("cleanup", ["token-delete", "token-preserve", "owner"])
async def test_conditional_cleanup_does_not_invalidate_later_absence_or_foreign_view(
    authority, cleanup
):
    runtime, _, _ = authority
    views = runtime.views
    workspace_id = "ws_conditional_absence"
    files = {"openai.api-key": b"synthetic-key", "github.api-key": b"synthetic-key"}
    old = views.publish(workspace_id, files)
    views.remove(workspace_id)
    absence = views.absence_token(workspace_id)
    assert absence is not None

    def remove_old():
        if cleanup == "owner":
            views.remove_owned(workspace_id, old)
        else:
            views.remove(
                workspace_id, publication=old, preserve_directory=cleanup == "token-preserve"
            )

    remove_old()
    assert views.absence_token(workspace_id) is absence
    current = views.publish(workspace_id, files, expected_absence=absence)
    assert current is not None and current.owner is not old.owner
    remove_old()
    assert views.publication(workspace_id) == current
    assert (current.path / "openai.api-key").read_bytes() == files["openai.api-key"]


@pytest.mark.parametrize("boundary", ["capture", "slot-read"])
async def test_initial_cutover_can_publish_only_into_unchanged_absence(
    cutover, tmp_path, monkeypatch, boundary
):
    _, runtime, original, _, _, _, refresh = cutover
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.stop_workspace = AsyncMock()
    active = None
    entered, release, _, _ = hold_boundary(
        monkeypatch, runtime, boundary, lambda: asyncio.current_task() is active
    )

    async def execute():
        async with scope.scope(original["workspace_id"]):
            publication = await runtime.publication(original["workspace_id"])
            assert publication.owner is owned_publication(runtime.views, original["workspace_id"])
            assert (publication.path / "openai.api-key").read_bytes() == b"synthetic-agent-2"

    active = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await refresh()
        release.set()
        await asyncio.wait_for(active, 5)
        scope.stop_workspace.assert_not_awaited()
    finally:
        release.set()
        if not active.done():
            active.cancel()
        await asyncio.wait_for(asyncio.gather(active, return_exceptions=True), 5)


async def test_profile_mount_reader_uses_the_same_cutover_validation(
    cutover, tmp_path, monkeypatch
):
    _, runtime, original, _, _, _, refresh = cutover
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.stop_workspace = AsyncMock()
    selected = None
    ready = False
    workspace = runtime.workspace

    async def first_validated(*args):
        nonlocal ready
        value = await workspace(*args)
        if asyncio.current_task() is selected and scope.workspace_id.get() is not None:
            ready = True
        return value

    monkeypatch.setattr(runtime, "workspace", first_validated)
    entered, release, _, _ = hold_boundary(
        monkeypatch, runtime, "capture", lambda: ready and asyncio.current_task() is selected
    )

    async def execute():
        async with scope.scope(original["workspace_id"]):
            publication = await runtime.publication(original["workspace_id"])
            descriptor = os.open(publication.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                result = await runtime.profile_mounts(
                    WorkspaceProfile(name="synthetic"), original["workspace_id"]
                )
                assert len(result.mounts) == 1 and result.mounts[0].source == str(publication.path)
                assert publication.path.stat().st_ino == os.fstat(descriptor).st_ino
                assert (
                    await runtime.publication(original["workspace_id"])
                ).owner is publication.owner
                assert read_view(descriptor, "openai.api-key") == b"synthetic-agent-2"
            finally:
                os.close(descriptor)

    selected = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await refresh()
        release.set()
        await asyncio.wait_for(selected, 5)
        scope.stop_workspace.assert_not_awaited()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(selected, return_exceptions=True), 5)


@pytest.mark.parametrize("boundary", ["capture", "slot-read"])
async def test_unrelated_public_workspace_cannot_invalidate_initial_retry(
    cutover, tmp_path, monkeypatch, boundary
):
    client, runtime, original, _, _, clock, refresh = cutover
    other = hosted_create()
    other["hosted"] = copy.deepcopy(original)
    other["hosted"]["workspace_id"] = "ws_other_normal"
    for role, binding in other["hosted"]["credentials"].items():
        binding.update(
            workspace_id="ws_other_normal",
            preparation_id=f"other-{role}",
            grant_id=f"other-{role}",
            idempotency_key=f"other-{role}",
        )
        preparation = {key: binding[key] for key in Preparation.model_fields}
        preparation["expires_at"] = (clock.now(UTC) + timedelta(minutes=50)).isoformat()
        assert (await client.post(f"{PREFIX}/preparations", json=preparation)).status_code == 200
        result = await client.post(
            f"{PREFIX}/materials/install", json={**binding, "material": "c3ludGhldGljLW90aGVy"}
        )
        assert result.status_code == 200 and result.json()["status"] == "current"
    result = await client.post(
        "/v1/workspaces", json=other, headers={"Idempotency-Key": "other-normal-create"}
    )
    assert result.status_code == 202
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.stop_workspace = AsyncMock()
    active = None
    entered, release, _, _ = hold_boundary(
        monkeypatch, runtime, boundary, lambda: asyncio.current_task() is active
    )

    async def execute():
        async with scope.scope(original["workspace_id"]):
            publication = await runtime.publication(original["workspace_id"])
            assert publication is not None, "unrelated publication suppressed an authorized view"
            assert publication.owner is owned_publication(runtime.views, original["workspace_id"])
            assert (publication.path / "openai.api-key").read_bytes() == b"synthetic-agent-2"

    active = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        other_scope = WorkspaceConsumers(runtime, tmp_path / "other")
        async with other_scope.scope("ws_other_normal"):
            other_publication = await runtime.publication("ws_other_normal")
            assert other_publication.owner is owned_publication(runtime.views, "ws_other_normal")
        other_token = (await runtime.publication("ws_other_normal")).token
        await refresh()
        release.set()
        await asyncio.wait_for(active, 5)
        scope.stop_workspace.assert_not_awaited()
        assert (await runtime.publication("ws_other_normal")).token == other_token
        assert (other_publication.path / "openai.api-key").read_bytes() == b"synthetic-other"
    finally:
        release.set()
        if not active.done():
            active.cancel()
        await asyncio.wait_for(asyncio.gather(active, return_exceptions=True), 5)


@pytest.mark.parametrize("field", ["revision", "fence"])
async def test_corrupt_nonadvancing_successor_cannot_authorize_a_retry(
    cutover, tmp_path, monkeypatch, field
):
    _, runtime, original, _, slots, _, refresh = cutover
    async with consumer(cutover, tmp_path) as state:
        scope, active, _, path, _ = state
        entered, release, _, _ = hold_boundary(
            monkeypatch,
            runtime,
            "capture",
            lambda: asyncio.current_task().get_name().startswith("hosted-authority-"),
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await refresh()
            async with runtime.receiver.factory.begin() as session:
                row = await session.get(Receipt, slots["agent"])
                row.binding = {**row.binding, field: original["credentials"]["agent"][field]}
            release.set()
            result = (await asyncio.wait_for(asyncio.gather(active, return_exceptions=True), 5))[0]
            assert isinstance(result, MaterialDeniedError)
            scope.stop_workspace.assert_awaited_once_with(original["workspace_id"])
            assert not path.exists()
        finally:
            release.set()


async def test_retry_cannot_reinterpret_a_preexisting_cleared_view_as_absence(
    cutover, tmp_path, monkeypatch
):
    _, runtime, original, _, _, _, refresh = cutover
    workspace_id = original["workspace_id"]
    await runtime.workspace(workspace_id)
    path = (await runtime.publication(workspace_id)).path
    inode = path.stat().st_ino
    await runtime.retract(workspace_id, preserve_directory=True)
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.stop_workspace = AsyncMock()
    active = None
    entered, release, _, _ = hold_boundary(
        monkeypatch, runtime, "capture", lambda: asyncio.current_task() is active
    )

    async def execute():
        async with scope.scope(workspace_id):
            assert await runtime.publication(workspace_id) is None
            assert path.stat().st_ino == inode and not list(path.iterdir())

    active = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await refresh()
        release.set()
        await asyncio.wait_for(active, 5)
        assert await runtime.publication(workspace_id) is None
        assert path.stat().st_ino == inode and not list(path.iterdir())
        scope.stop_workspace.assert_not_awaited()
    finally:
        release.set()
        if not active.done():
            active.cancel()
        await asyncio.wait_for(asyncio.gather(active, return_exceptions=True), 5)


async def test_expiry_during_final_association_read_remains_terminal(cutover, monkeypatch):
    _, runtime, original, _, _, clock, _ = cutover
    association = runtime.association
    reads = 0
    instant = clock.instant

    async def cross_expiry(*args):
        nonlocal reads
        result = await association(*args)
        reads += 1
        if reads == 2:
            clock.instant += timedelta(minutes=21)
        return result

    monkeypatch.setattr(runtime, "association", cross_expiry)
    with pytest.raises(MaterialDeniedError, match="PROTECTED_MATERIAL_UNAVAILABLE"):
        await runtime.workspace(original["workspace_id"])
    assert await runtime.publication(original["workspace_id"]) is None
    clock.instant = instant
    with pytest.raises(MaterialDeniedError, match="PROTECTED_MATERIAL_UNAVAILABLE"):
        await runtime.workspace(original["workspace_id"])


async def test_absence_witnesses_share_workspace_scope_without_retaining_history(authority):
    runtime, _, _ = authority
    views = runtime.views
    other = RuntimeViews(views.store, identity=views.identity)
    before = set(views.store.root.iterdir())
    token = views.absence_token("ws_shared_absence")
    assert other.absence_token("ws_shared_absence") is token
    assert views.absence_token("ws_other_absence") is not token
    del token
    assert not views._absence_tokens
    for index in range(1025):
        token = views.absence_token(f"ws_absent_{index}")
        assert token is not None
        assert len(views._absence_tokens) == 1
    del token
    assert not views._absence_tokens
    assert set(views.store.root.iterdir()) == before
