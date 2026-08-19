"""On-demand bridge from a replaceable host-home mount to Git-config bundles."""

from __future__ import annotations

import argparse
import asyncio
import os
import stat
import subprocess
from contextlib import suppress
from pathlib import Path

import structlog

from awf.node.git_manager import AGENT_RUNTIME_GID, AGENT_RUNTIME_UID
from awf.service.gitconfig_snapshot import (
    materialize_service_gitconfig,
    release_superseded_service_gitconfig_leases,
)

_CURRENT_SOURCE_NAME = "current"
_EMPTY_SOURCE_NAME = "empty"
_REQUEST = b"refresh\n"
_REQUEST_TIMEOUT_SECONDS = 15.0
_log = structlog.get_logger(__name__)


class GitconfigSourceServer:
    """Publish fresh immutable snapshots from a directory-mounted host home."""

    def __init__(
        self,
        *,
        host_home: Path,
        logical_host_home: Path | None = None,
        work_dir: Path,
        socket_path: Path,
    ) -> None:
        self.host_home = host_home
        self.logical_host_home = logical_host_home or host_home
        self.work_dir = work_dir
        self.socket_path = socket_path
        self.ready = asyncio.Event()
        self._refresh_lock = asyncio.Lock()

    def refresh(self) -> Path | None:
        """Snapshot the currently named host config and publish its source home."""
        snapshot = materialize_service_gitconfig(
            host_home=self.host_home,
            logical_host_home=self.logical_host_home,
            work_dir=self.work_dir,
            owner_uid=AGENT_RUNTIME_UID,
            owner_gid=AGENT_RUNTIME_GID,
        )
        source_home = snapshot.parent if snapshot is not None else self._empty_source_home()
        self._publish_current(source_home)
        release_superseded_service_gitconfig_leases(
            snapshots_root=self.work_dir / "service-auth" / "gitconfig-snapshots",
            protected_configs=(snapshot,),
        )
        return snapshot

    async def serve(self) -> None:
        """Serve refresh requests until cancelled."""
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._remove_stale_socket()
        try:
            await asyncio.to_thread(self.refresh)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            _log.warning("gitconfig_source.initial_refresh_failed", error=str(exc))
            self._publish_current(self._empty_source_home())
        server = await asyncio.start_unix_server(self._handle_request, path=self.socket_path)
        self.ready.set()
        try:
            async with server:
                await server.serve_forever()
        finally:
            server.close()
            await server.wait_closed()
            if self.socket_path.is_socket():
                self.socket_path.unlink()

    async def _handle_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request = await reader.readline()
            if request != _REQUEST:
                writer.write(b"error: invalid request\n")
            else:
                async with self._refresh_lock:
                    snapshot = await asyncio.to_thread(self.refresh)
                response = "-" if snapshot is None else str(snapshot)
                writer.write(f"{response}\n".encode())
            await writer.drain()
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            writer.write(f"error: {exc}\n".encode(errors="replace"))
            with suppress(OSError, ConnectionError):
                await writer.drain()
        finally:
            writer.close()
            with suppress(OSError, ConnectionError):
                await writer.wait_closed()

    def _empty_source_home(self) -> Path:
        empty_home = self.socket_path.parent / _EMPTY_SOURCE_NAME
        empty_home.mkdir(parents=True, exist_ok=True)
        return empty_home

    def _remove_stale_socket(self) -> None:
        try:
            mode = self.socket_path.lstat().st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(mode):
            raise FileExistsError(f"gitconfig source path is not a Unix socket: {self.socket_path}")
        self.socket_path.unlink()

    def _publish_current(self, source_home: Path) -> None:
        current = self.socket_path.parent / _CURRENT_SOURCE_NAME
        pending = self.socket_path.parent / f".{_CURRENT_SOURCE_NAME}.{os.getpid()}"
        with suppress(FileNotFoundError):
            pending.unlink()
        pending.symlink_to(source_home, target_is_directory=True)
        pending.replace(current)


async def request_gitconfig_source_refresh(socket_path: Path) -> Path | None:
    """Request a fresh source snapshot and return its immutable config path."""

    async def _request() -> Path | None:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        try:
            writer.write(_REQUEST)
            await writer.drain()
            response = (await reader.readline()).decode(errors="replace").strip()
        finally:
            writer.close()
            with suppress(OSError, ConnectionError):
                await writer.wait_closed()
        if response == "-":
            return None
        if response.startswith("error:") or not response:
            raise RuntimeError(response or "gitconfig source returned an empty response")
        return Path(response)

    return await asyncio.wait_for(_request(), timeout=_REQUEST_TIMEOUT_SECONDS)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-home-parent", type=Path, required=True)
    parser.add_argument("--logical-host-home", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    """Run the local-service Git-config source bridge."""
    args = _parse_args()
    server = GitconfigSourceServer(
        host_home=args.host_home_parent / args.logical_host_home.name,
        logical_host_home=args.logical_host_home,
        work_dir=args.work_dir,
        socket_path=args.socket,
    )
    asyncio.run(server.serve())


if __name__ == "__main__":  # pragma: no cover - exercised as a Compose process.
    main()


__all__ = ["GitconfigSourceServer", "request_gitconfig_source_refresh"]
