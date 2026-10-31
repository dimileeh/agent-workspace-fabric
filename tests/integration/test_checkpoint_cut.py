"""Real PostgreSQL locks and managed log/artifact bytes; no provider snapshot."""

import time
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from sqlalchemy import text

from awf.db.base import Base
from awf.runtime.logs import LogStore
from tests.integration.test_checkpoint_marker import checkpoint_request, marker
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import PREFIX, create
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node
from tests.unit.node.test_git_manager import origin_repo as origin_repo


@asynccontextmanager
async def cut(ready_node, tmp_path, *, locks=True):
    from awf.hosted_node.checkpoint_cut import CheckpointRoots, schema_fingerprint
    from awf.hosted_node.checkpoint_schemas import CheckpointMarker

    client, receiver, engine = ready_node
    workspace = await create(receiver.factory)
    root = tmp_path / "work"
    store = LogStore(root=root / "logs", session_factory=receiver.factory)
    sink = await store.open_stream(
        workspace_id=workspace.id,
        stream_id="agent.stdout",
        source="agent",
        name="stdout",
        kind="stdout",
    )
    await sink.write("committed output\n")
    assert (
        await client.post(PREFIX + "/maintenance/begin", json=checkpoint_request())
    ).status_code == 200
    expected = CheckpointMarker.model_validate(await marker(receiver))
    async with engine.connect() as connection, connection.begin():
        if locks:
            tables = sorted([*Base.metadata.tables, "alembic_version"])
            await connection.execute(
                text(
                    "LOCK TABLE "
                    + ",".join('"' + name + '"' for name in tables)
                    + " IN EXCLUSIVE MODE NOWAIT"
                )
            )
        raw = (await connection.get_raw_connection()).driver_connection
        roots = CheckpointRoots(
            work_dir=root,
            device=root.stat().st_dev,
            image_digest="sha256:" + "a" * 64,
            schema_sha256=await schema_fingerprint(raw),
            max_files=100,
            max_bytes=1024 * 1024,
        )
        yield raw, expected, roots, sink.path


async def test_cut_proves_actual_committed_log_bytes_without_writing(ready_node, tmp_path):
    from awf.hosted_node.checkpoint_cut import validate_checkpoint_cut

    async with cut(ready_node, tmp_path) as (connection, expected, roots, path):
        before = path.stat()
        proof = await validate_checkpoint_cut(connection, expected, roots, time.monotonic() + 5)
        assert proof.log_streams == 1 and proof.files == 1
        assert proof.byte_count == len(b"committed output\n")
        assert len(proof.files_sha256) == len(proof.marker_sha256) == 64
        assert proof.schema_sha256 == roots.schema_sha256
        assert path.stat().st_mtime_ns == before.st_mtime_ns
        assert connection.is_in_transaction()


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "short",
        "tail",
        "lines",
        "symlink",
        "hardlink",
        "unknown_root",
        "pending",
        "volume",
        "limit",
        "schema",
        "expired",
        "identity",
    ],
)
async def test_cut_denies_incomplete_or_unsupported_evidence(ready_node, tmp_path, fault):
    from awf.hosted_node.checkpoint_cut import validate_checkpoint_cut
    from awf.hosted_node.identity import MaterialDeniedError

    async with cut(ready_node, tmp_path) as (connection, expected, roots, path):
        deadline = time.monotonic() + 5
        if fault == "missing":
            path.unlink()
        elif fault in {"short", "tail", "lines"}:
            path.write_bytes(
                {"short": b"x", "tail": b"committed output\nextra", "lines": b"committed output "}[
                    fault
                ]
            )
        elif fault == "symlink":
            path.unlink()
            path.symlink_to(tmp_path / "outside")
        elif fault == "hardlink":
            (path.parent / "copy.log").hardlink_to(path)
        elif fault == "unknown_root":
            (roots.work_dir / "unknown").mkdir()
        elif fault == "pending":
            folder = roots.work_dir / "artifacts"
            folder.mkdir()
            (folder / ".plan.md.tmp").write_text("partial")
        elif fault == "volume":
            roots = replace(roots, persistent_volumes=("unsupported-db",))
        elif fault == "limit":
            roots = replace(roots, max_bytes=1)
        elif fault == "schema":
            roots = replace(roots, schema_sha256="b" * 64)
        elif fault == "expired":
            deadline = time.monotonic() - 1
        else:
            expected = expected.model_copy(
                update={"identity": expected.identity.model_copy(update={"boot_epoch": 999})}
            )
        with pytest.raises(MaterialDeniedError, match="CHECKPOINT"):
            await validate_checkpoint_cut(connection, expected, roots, deadline)


async def test_cut_requires_actual_full_table_barrier(ready_node, tmp_path):
    from awf.hosted_node.checkpoint_cut import validate_checkpoint_cut
    from awf.hosted_node.identity import MaterialDeniedError

    async with cut(ready_node, tmp_path, locks=False) as (connection, expected, roots, _):
        with pytest.raises(MaterialDeniedError, match="CHECKPOINT_BARRIER_REQUIRED"):
            await validate_checkpoint_cut(connection, expected, roots, time.monotonic() + 5)


@pytest.mark.parametrize("fault", ["git_link", "artifact_json", "compose_volume"])
async def test_cut_validates_managed_publication_formats(ready_node, tmp_path, fault):
    from awf.hosted_node.checkpoint_cut import validate_checkpoint_cut
    from awf.hosted_node.identity import MaterialDeniedError

    async with cut(ready_node, tmp_path) as (connection, expected, roots, path):
        if fault == "git_link":
            folder = roots.work_dir / "git" / "worktrees" / path.parent.name
            folder.mkdir(parents=True)
            (folder / ".git").write_text("gitdir: /outside/metadata\n")
        elif fault == "artifact_json":
            folder = roots.work_dir / "artifacts"
            folder.mkdir()
            (folder / "result.json").write_text('{"unfinished":')
        else:
            folder = roots.work_dir / "compose" / path.parent.name
            folder.mkdir(parents=True)
            (folder / "compose.yml").write_text(
                "services:\n  db:\n    image: postgres:16\n    volumes: [database:/var/lib/postgresql/data]\nvolumes:\n  database: {}\n"
            )
        with pytest.raises(MaterialDeniedError, match="CHECKPOINT"):
            await validate_checkpoint_cut(connection, expected, roots, time.monotonic() + 5)


async def test_cut_deadline_bounds_sql_before_file_scan(ready_node, tmp_path):
    from awf.hosted_node.checkpoint_cut import validate_checkpoint_cut
    from awf.hosted_node.identity import MaterialDeniedError

    async with cut(ready_node, tmp_path) as (connection, expected, roots, _):

        class SlowConnection:
            def is_in_transaction(self):
                return connection.is_in_transaction()

            async def fetch(self, query, *args):
                await connection.execute("SELECT pg_sleep(0.2)")
                return await connection.fetch(query, *args)

            async def fetchval(self, query, *args):
                return await connection.fetchval(query, *args)

        start = time.monotonic()
        with pytest.raises(MaterialDeniedError, match="CHECKPOINT"):
            await validate_checkpoint_cut(SlowConnection(), expected, roots, start + 0.02)
        assert time.monotonic() - start < 0.15


@pytest.mark.parametrize("fault", [None, "backref", "common", "head", "external"])
async def test_cut_checks_actual_managed_git_links(ready_node, tmp_path, origin_repo, fault):
    from awf.hosted_node.checkpoint_cut import validate_checkpoint_cut
    from awf.hosted_node.identity import MaterialDeniedError
    from awf.node.git_manager import GitManager, linked_worktree_git_dir

    async with cut(ready_node, tmp_path) as (connection, expected, roots, _):
        manager = GitManager(roots.work_dir / "git")
        layout = await manager.add_worktree(
            workspace_id="ws_checkpoint",
            repo_url=origin_repo.as_uri(),
            base_branch="development",
            new_branch="awf/ws_checkpoint",
        )
        git_dir = linked_worktree_git_dir(layout.worktree_path)
        assert git_dir is not None
        if fault == "backref":
            (git_dir / "gitdir").write_text(str(tmp_path / "elsewhere"))
        elif fault == "common":
            (git_dir / "commondir").write_text("../../other")
        elif fault == "head":
            (git_dir / "HEAD").write_text("not a ref")
        elif fault == "external":
            (layout.worktree_path / ".git").write_text("gitdir: /outside/git")
        if fault is not None:
            with pytest.raises(MaterialDeniedError, match="CHECKPOINT"):
                await validate_checkpoint_cut(connection, expected, roots, time.monotonic() + 5)
        else:
            before = {p: p.stat().st_mtime_ns for p in roots.work_dir.rglob("*")}
            proof = await validate_checkpoint_cut(connection, expected, roots, time.monotonic() + 5)
            assert proof.files > 10 and proof.log_streams == 1
            assert before == {p: p.stat().st_mtime_ns for p in roots.work_dir.rglob("*")}
