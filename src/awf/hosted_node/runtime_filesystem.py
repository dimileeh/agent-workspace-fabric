"""Group access for the normal managed Git checkout without a root worker."""

import os
import stat
from pathlib import Path

from awf.hosted_node.identity import MaterialDeniedError
from awf.node.git_manager import _agent_writable_git_targets


def share_checkout(root: Path, mirror: Path, worktree: Path, gid: int) -> None:
    for path, parent in ((mirror, root / "mirrors"), (worktree, root / "worktrees")):
        if path.parent != parent or path.resolve() != path or not path.is_dir():
            raise MaterialDeniedError("UNMANAGED_WORKSPACE_PATH")
    targets = _agent_writable_git_targets(layout_mirror=mirror, worktree_path=worktree)
    for target in targets:
        if not target.path.is_relative_to(root) or target.path.resolve() != target.path:
            raise MaterialDeniedError("UNMANAGED_WORKSPACE_PATH")

    def grant(path: Path, *, directories_only: bool = False) -> None:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            return
        directory = stat.S_ISDIR(info.st_mode)
        if directories_only and not directory:
            return
        if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise MaterialDeniedError("UNSAFE_WORKSPACE_FILE")
        desired = (
            0o2770
            if directory
            else (stat.S_IMODE(info.st_mode) & 0o700) | ((info.st_mode & 0o700) >> 3)
        )
        if info.st_uid != os.geteuid():
            if info.st_gid != gid or info.st_mode & 0o060 != 0o060:
                raise MaterialDeniedError("UNWRITABLE_WORKSPACE_FILE")
            return
        os.chown(path, -1, gid, follow_symlinks=False)
        path.chmod(desired, follow_symlinks=False)

    for target in targets:
        if not target.path.exists():
            continue
        grant(target.path, directories_only=target.directories_only)
        if target.recursive:
            for directory, directories, files in os.walk(target.path, followlinks=False):
                for name in directories + files:
                    grant(Path(directory) / name, directories_only=target.directories_only)
