"""Per-workspace tmpfs views never expose private grant roots or other tenants."""

import os
import stat

import pytest

from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.runtime_views import RuntimeViews
from tests.unit.test_hosted_runtime_authority import authority as authority


async def test_view_is_readonly_group_scoped_and_refreshes_existing_directory(authority):
    runtime, _, _ = authority
    views = RuntimeViews(
        runtime.receiver.store, identity=runtime.receiver.identity, agent_gid=os.getegid()
    )
    path = views.publish(
        "ws_one", {"openai.api-key": b"synthetic-one", "github.api-key": b"synthetic-github"}
    )
    assert path != runtime.receiver.store.root
    assert path.stat().st_gid == os.getegid()
    assert stat.S_IMODE(path.stat().st_mode) == 0o710
    assert stat.S_IMODE((path / "openai.api-key").stat().st_mode) == 0o440
    inode = path.stat().st_ino
    assert (
        views.publish(
            "ws_one", {"openai.api-key": b"synthetic-two", "github.api-key": b"synthetic-github"}
        )
        == path
    )
    assert path.stat().st_ino == inode
    assert (path / "openai.api-key").read_bytes() == b"synthetic-two"
    other = views.publish(
        "ws_other",
        {"openai.api-key": b"synthetic-other", "github.api-key": b"synthetic-other-github"},
    )
    views.remove("ws_one")
    assert not path.exists()
    assert other.is_dir()
    views.remove("ws_one")


async def test_view_rejects_symlink_replacement_and_unknown_targets(authority, tmp_path):
    runtime, _, _ = authority
    views = RuntimeViews(
        runtime.receiver.store, identity=runtime.receiver.identity, agent_gid=os.getegid()
    )
    with pytest.raises(MaterialDeniedError):
        views.publish("ws_one", {"../../escape": b"synthetic"})
    path = views.publish(
        "ws_one", {"openai.api-key": b"synthetic", "github.api-key": b"synthetic-gh"}
    )
    target = path / "openai.api-key"
    target.unlink()
    target.symlink_to(tmp_path / "outside")
    with pytest.raises(MaterialDeniedError):
        views.publish(
            "ws_one", {"openai.api-key": b"synthetic-next", "github.api-key": b"synthetic-gh"}
        )


async def test_old_boot_cleanup_cannot_remove_current_boot_view(authority):
    runtime, _, _ = authority
    identity = runtime.receiver.identity
    old = RuntimeViews(runtime.receiver.store, identity=identity, agent_gid=os.getegid())
    new = RuntimeViews(
        runtime.receiver.store,
        identity=identity.model_copy(update={"boot_epoch": identity.boot_epoch + 1}),
        agent_gid=os.getegid(),
    )
    files = {"openai.api-key": b"synthetic", "github.api-key": b"synthetic-gh"}
    old_path = old.publish("ws_one", files)
    new_path = new.publish("ws_one", files)
    assert old_path != new_path
    old.remove("ws_one")
    assert (new_path / "github.api-key").read_bytes() == b"synthetic-gh"
