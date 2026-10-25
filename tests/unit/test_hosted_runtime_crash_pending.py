"""Actual killed publishers release flock but leave bounded private write remnants."""

import fcntl
import os
import select
import signal
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.runtime_consumers import WorkspaceConsumers
from awf.hosted_node.runtime_views import PublishedView, RuntimeViews
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_retained import deny_agent, retained

_CHILD = r"""
import contextlib, json, os, signal, sys
from pathlib import Path
from awf.hosted_node.material_store import MaterialStore
from awf.hosted_node import runtime_views
from awf.hosted_node.runtime_views import RuntimeViews
from awf.hosted_node.schemas import Identity
root, identity, phase, target, gid, workspace_id = sys.argv[1:]
if sys.platform == "linux":
    store = MaterialStore(Path(root))
else:
    # Same portable mount-probe accommodation as the parent authority fixture.
    store = MaterialStore.__new__(MaterialStore)
    store.root, store.closed = Path(root), False
    fd = store._open_root()
    try:
        info = os.fstat(fd)
        store._device_inode = (info.st_dev, info.st_ino)
    finally:
        os.close(fd)
views = RuntimeViews(store, identity=Identity.model_validate_json(identity), agent_gid=int(gid))
real_open, real_fdopen, real_chown, real_replace = os.open, os.fdopen, os.fchown, os.replace
real_mkdir, real_chmod = os.mkdir, os.fchmod
pending, publications, directories = set(), set(), set()
def pause():
    print("pending-write-held", flush=True)
    while True:
        signal.pause()
def opened(path, *args, **kwargs):
    fd = real_open(path, *args, **kwargs)
    if path == "view":
        directories.add(fd)
    if path == "publication":
        info = os.fstat(fd)
        publications.add((info.st_dev, info.st_ino))
        if phase == "publication_opened":
            pause()
    if path == target + ".pending":
        pending.add(fd)
        if phase == "created":
            pause()
    return fd
class Partial:
    def __init__(self, stream):
        self.stream = stream
    def write(self, value):
        self.stream.write(value[:2])
        self.stream.flush()
        pause()
    def __getattr__(self, name):
        return getattr(self.stream, name)
class Truncated:
    def __init__(self, stream):
        self.stream = stream
    def truncate(self):
        self.stream.truncate()
        pause()
    def __getattr__(self, name):
        return getattr(self.stream, name)
@contextlib.contextmanager
def fdopen(fd, *args, **kwargs):
    with real_fdopen(fd, *args, **kwargs) as stream:
        info = os.fstat(fd)
        publication = (info.st_dev, info.st_ino) in publications
        if (fd in pending and phase == "partial") or (publication and phase == "publication_partial"):
            yield Partial(stream)
        elif publication and phase == "publication_truncated":
            yield Truncated(stream)
        else:
            yield stream
def chown(fd, *args):
    real_chown(fd, *args)
    if fd in directories and phase == "view_chowned":
        pause()
    if fd in pending and phase == "chowned":
        pause()
def replace(source, destination, **kwargs):
    if source == target + ".pending" and phase == "ready":
        pause()
    return real_replace(source, destination, **kwargs)
def mkdir(path, *args, **kwargs):
    real_mkdir(path, *args, **kwargs)
    if path == "view" and phase == "view_created":
        pause()
def chmod(fd, mode):
    real_chmod(fd, mode)
    if fd in directories and phase == "view_ready":
        pause()
if phase.startswith("publication_"):
    from types import SimpleNamespace
    marker = store.root / views._key(workspace_id) / "publication"
    old = marker.read_text() if marker.exists() else "0" * 32
    runtime_views.uuid4 = lambda: SimpleNamespace(hex=("f" if old[0] != "f" else "0") * 32)
os.open, os.fdopen, os.fchown, os.replace = opened, fdopen, chown, replace
os.mkdir, os.fchmod = mkdir, chmod
views.publish(workspace_id, {"openai.api-key": b"synthetic-inflight-agent", "github.api-key": b"synthetic-inflight-github"})
raise AssertionError("crash boundary not reached")
"""


def _crash(views, phase, target, workspace_id="ws_crashed"):
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _CHILD,
            str(views.store.root),
            views.identity.model_dump_json(),
            phase,
            target,
            str(views.gid),
            workspace_id,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    root = os.open(views.store.root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert process.stdout is not None
        assert select.select([process.stdout], [], [], 10)[0], "publisher did not reach boundary"
        assert process.stdout.readline() == "pending-write-held\n"
        with pytest.raises(BlockingIOError):
            fcntl.flock(root, fcntl.LOCK_EX | fcntl.LOCK_NB)
        process.kill()
        assert process.wait(timeout=5) == -signal.SIGKILL
        # The killed interpreter cannot run any Python finally cleanup.
        fcntl.flock(root, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(root, fcntl.LOCK_UN)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
        os.close(root)


@pytest.mark.parametrize("phase", ["created", "partial", "chowned", "ready"])
@pytest.mark.parametrize("target", ["openai.api-key", "github.api-key"])
@pytest.mark.parametrize("operation", ["refresh", "retract"])
async def test_killed_writer_pending_is_recovered_without_touching_successors(
    authority, phase, target, operation
):
    runtime, _, _ = authority
    views = runtime.views
    original = views.publish(
        "ws_crashed", {"openai.api-key": b"synthetic-old", "github.api-key": b"synthetic-old-gh"}
    )
    other = views.publish(
        "ws_other", {"openai.api-key": b"synthetic-other", "github.api-key": b"synthetic-other-gh"}
    )
    _crash(views, phase, target)
    successor = RuntimeViews(
        views.store,
        identity=views.identity.model_copy(update={"boot_epoch": views.identity.boot_epoch + 1}),
        agent_gid=views.gid,
    ).publish(
        "ws_crashed",
        {"openai.api-key": b"synthetic-successor", "github.api-key": b"synthetic-successor-gh"},
    )
    pending = original.path / (target + ".pending")
    assert pending.exists()
    assert pending.stat().st_size == (
        0
        if phase == "created"
        else 2
        if phase == "partial"
        else len(
            b"synthetic-inflight-agent"
            if target == "openai.api-key"
            else b"synthetic-inflight-github"
        )
    )
    views.remove("ws_crashed", publication=original)
    assert pending.exists(), "superseded publication must not clean a newer writer"
    if operation == "refresh":
        files = {"openai.api-key": b"synthetic-current", "github.api-key": b"synthetic-current-gh"}
        current = views.publish("ws_crashed", files)
        assert not pending.exists()
        assert {path.name: path.read_bytes() for path in current.path.iterdir()} == files
        views.remove("ws_crashed", publication=original)
        assert current.path.exists()
        views.remove("ws_crashed", publication=current)
    else:
        views.remove("ws_crashed")
    assert not original.path.parent.exists()
    assert (other.path / "github.api-key").read_bytes() == b"synthetic-other-gh"
    assert (successor.path / "github.api-key").read_bytes() == b"synthetic-successor-gh"


@pytest.mark.parametrize("operation", ["refresh", "retract"])
@pytest.mark.parametrize(
    "fault",
    ["foreign", "symlink", "hardlink", "fifo", "directory", "mode", "large", "owner", "group"],
)
async def test_pending_cleanup_rejects_unsafe_entries_before_unlinking_anything(
    authority, tmp_path, monkeypatch, operation, fault
):
    runtime, _, _ = authority
    views = runtime.views
    files = {"openai.api-key": b"synthetic-agent", "github.api-key": b"synthetic-gh"}
    published = views.publish("ws_crashed", files)
    valid = published.path / "openai.api-key.pending"
    valid.write_bytes(b"partial")
    valid.chmod(0o400)
    bad = published.path / ("foreign.pending" if fault == "foreign" else "github.api-key.pending")
    outside = published.path.parent / "foreign"
    outside.write_bytes(b"preserve-foreign")
    outside.chmod(0o400)
    if fault == "symlink":
        bad.symlink_to(outside)
    elif fault == "hardlink":
        bad.hardlink_to(outside)
    elif fault == "fifo":
        os.mkfifo(bad, 0o400)
    elif fault == "directory":
        bad.mkdir(mode=0o400)
    else:
        bad.write_bytes(b"x" * (65537 if fault == "large" else 1))
        bad.chmod(0o600 if fault == "mode" else 0o400)
    original_stat = os.stat

    def metadata(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == bad.name and kwargs.get("dir_fd") is not None and fault in {"owner", "group"}:
            values = list(result)
            values[4 if fault == "owner" else 5] = 12345
            return os.stat_result(values)
        return result

    monkeypatch.setattr(os, "stat", metadata)
    with pytest.raises(MaterialDeniedError):
        if operation == "refresh":
            views.publish("ws_crashed", files)
        else:
            views.remove("ws_crashed")
    assert valid.read_bytes() == b"partial"
    assert bad.lstat()
    assert outside.read_bytes() == b"preserve-foreign"
    assert (published.path / "openai.api-key").read_bytes() == b"synthetic-agent"
    assert (published.path.parent / "publication").read_text() == published.token
    with pytest.raises(ExceptionGroup, match="Runtime view retraction failed") as first:
        await runtime.receiver.close()
    with pytest.raises(ExceptionGroup) as repeated:
        await runtime.receiver.close()
    assert repeated.value is first.value
    assert valid.read_bytes() == b"partial"
    assert bad.lstat() and outside.read_bytes() == b"preserve-foreign"


@pytest.mark.parametrize("operation", ["refresh", "retract"])
async def test_first_publication_crash_is_recoverable(authority, operation):
    runtime, _, _ = authority
    views = runtime.views
    _crash(views, "partial", "github.api-key")
    path = views.store.root / views._key("ws_crashed") / "view"
    assert (path / "github.api-key.pending").read_bytes() == b"sy"
    if operation == "refresh":
        files = {"openai.api-key": b"synthetic-current", "github.api-key": b"synthetic-gh"}
        current = views.publish("ws_crashed", files)
        assert {entry.name: entry.read_bytes() for entry in path.iterdir()} == files
        views.remove("ws_crashed", publication=current)
    else:
        views.remove("ws_crashed")
    assert not path.parent.exists()


@pytest.mark.parametrize("phase", ["view_created", "view_chowned", "view_ready"])
@pytest.mark.parametrize("operation", ["refresh", "retract"])
async def test_crashed_empty_view_directory_setup_is_recoverable(authority, phase, operation):
    runtime, _, _ = authority
    views = runtime.views
    _crash(views, phase, "openai.api-key")
    path = views.store.root / views._key("ws_crashed") / "view"
    assert path.is_dir() and not list(path.iterdir())
    assert path.stat().st_mode & 0o777 == (0o710 if phase == "view_ready" else 0o700)
    stale = PublishedView(path=path, token="0" * 32)
    views.remove("ws_crashed", publication=stale)
    assert path.exists(), "unpublished directory cannot belong to an older token"
    if operation == "refresh":
        current = views.publish("ws_crashed", {"openai.api-key": b"agent", "github.api-key": b"gh"})
        assert (path / "openai.api-key").read_bytes() == b"agent"
        views.remove("ws_crashed", publication=stale)
        assert path.exists()
        views.remove("ws_crashed", publication=current)
    else:
        views.remove("ws_crashed")
    assert not path.parent.exists()


@pytest.mark.parametrize(
    "phase", ["publication_opened", "publication_partial", "publication_truncated"]
)
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("operation", ["refresh", "retract"])
async def test_crashed_publication_token_has_safe_unconditional_recovery(
    authority, phase, existing, operation
):
    runtime, _, _ = authority
    views = runtime.views
    files = {"openai.api-key": b"agent", "github.api-key": b"gh"}
    original = views.publish("ws_crashed", files) if existing else None
    _crash(views, phase, "openai.api-key")
    path = views.store.root / views._key("ws_crashed") / "view"
    token = (path.parent / "publication").read_text()
    assert not any(entry.name.endswith(".pending") for entry in path.iterdir())
    assert len(token) == (
        32
        if existing or phase == "publication_truncated"
        else 2
        if phase == "publication_partial"
        else 0
    )
    stale = original or PublishedView(path=path, token="0" * 32)
    # Opening an existing token is not yet a new publication; an invalid or
    # changed token must never authorize an older conditional cleanup.
    if existing and phase == "publication_opened":
        assert token == original.token
    elif len(token) != 32:
        with pytest.raises(MaterialDeniedError):
            views.remove("ws_crashed", publication=stale)
        assert path.exists()
    else:
        views.remove("ws_crashed", publication=stale)
        assert path.exists()
    if operation == "refresh":
        current = views.publish("ws_crashed", files)
        assert {entry.name: entry.read_bytes() for entry in path.iterdir()} == files
        views.remove("ws_crashed", publication=stale)
        assert path.exists()
        views.remove("ws_crashed", publication=current)
    else:
        views.remove("ws_crashed")
    assert not path.parent.exists()


@pytest.mark.parametrize("fault", ["public", "group_read", "nonempty", "owner", "group", "symlink"])
@pytest.mark.parametrize("operation", ["refresh", "retract"])
async def test_unfinished_directory_recovery_does_not_accept_unrelated_metadata(
    authority, monkeypatch, fault, operation
):
    runtime, _, _ = authority
    views = runtime.views
    _crash(views, "view_created", "openai.api-key")
    path = views.store.root / views._key("ws_crashed") / "view"
    if fault in {"public", "group_read"}:
        path.chmod(0o755 if fault == "public" else 0o750)
    if fault == "nonempty":
        (path / "github.api-key").write_bytes(b"foreign")
    if fault == "symlink":
        outside = path.parent / "foreign-directory"
        path.rename(outside)
        path.symlink_to(outside, target_is_directory=True)
    real_stat = os.fstat
    original = path.stat()

    def metadata(fd):
        info = real_stat(fd)
        if (info.st_dev, info.st_ino) == (original.st_dev, original.st_ino) and fault in {
            "owner",
            "group",
        }:
            values = list(info)
            values[4 if fault == "owner" else 5] = 12345
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, "fstat", metadata)
    with pytest.raises(NotADirectoryError if fault == "symlink" else MaterialDeniedError):
        if operation == "refresh":
            views.publish("ws_crashed", {"openai.api-key": b"agent", "github.api-key": b"gh"})
        else:
            views.remove("ws_crashed")
    assert path.stat().st_mode == original.st_mode
    assert path.exists()


@pytest.mark.parametrize("operation", ["refresh", "retract"])
async def test_other_agent_pending_name_is_recovered_without_widening_targets(authority, operation):
    runtime, _, _ = authority
    views = runtime.views
    files = {"anthropic.api-key": b"synthetic-agent", "github.api-key": b"synthetic-gh"}
    published = views.publish("ws_crashed", files)
    pending = published.path / "anthropic.api-key.pending"
    pending.write_bytes(b"partial")
    pending.chmod(0o400)
    if operation == "refresh":
        current = views.publish("ws_crashed", files)
        assert not pending.exists()
        views.remove("ws_crashed", publication=published)
        assert current.path.exists()
        views.remove("ws_crashed", publication=current)
    else:
        views.remove("ws_crashed")
    assert not published.path.parent.exists()


@pytest.mark.parametrize("failure", ["revoke", "expiry"])
@pytest.mark.parametrize("unlink_fails", [False, True])
async def test_retained_revoke_after_crash_recovers_admission_only_after_retraction(
    authority, tmp_path, monkeypatch, failure, unlink_fails
):
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    other = await retained(runtime, value, factory, "ws_other", other=True)
    _crash(runtime.views, "partial", "openai.api-key", "ws_cloud_stable")
    pending = path / "openai.api-key.pending"
    assert pending.read_bytes() == b"sy"
    await deny_agent(runtime, value, failure, monkeypatch)
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.stop_workspace = AsyncMock()
    unlink = os.unlink
    view_info = path.stat()

    def deny_pending(name, **kwargs):
        directory = kwargs.get("dir_fd")
        info = os.fstat(directory) if directory is not None else None
        if (
            name == pending.name
            and info is not None
            and (info.st_dev, info.st_ino) == (view_info.st_dev, view_info.st_ino)
        ):
            raise PermissionError("synthetic pending unlink denied")
        return unlink(name, **kwargs)

    if unlink_fails:
        with monkeypatch.context() as patch:
            patch.setattr(os, "unlink", deny_pending)
            assert not await scope.admission_check()
            assert pending.exists()
            scope.stop_workspace.assert_awaited_once_with("ws_cloud_stable")
    assert await scope.admission_check()
    assert not path.exists()
    assert other.exists()
    assert scope.stop_workspace.await_count == (2 if unlink_fails else 1)
    await runtime.workspace("ws_other")
