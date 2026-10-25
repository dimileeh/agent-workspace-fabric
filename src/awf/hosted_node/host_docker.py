"""Bounded read-only Docker inventory on an explicit Unix socket.

All non-allowlisted containers, even stopped ones, deny idle. An exempt control
container must match its exact immutable ID/image/owner and have no exec running.
No workload launch, cleanup or restart is performed by this observer.
"""

import asyncio
import json
from typing import Any

import httpx

from awf.hosted_node.host_config import HostSettings


class DockerObservation:
    def __init__(self, config: HostSettings, client: httpx.AsyncClient):
        self.config, self.client = config, client

    async def _get(self, path: str, *, all_containers: bool = False) -> Any:
        async with self.client.stream(
            "GET",
            "/v1.45/" + path,
            params={"all": "true"} if all_containers else None,
            follow_redirects=False,
            timeout=2,
        ) as response:
            if response.status_code != 200:
                raise ValueError("DOCKER_OBSERVATION_UNAVAILABLE")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 1024 * 1024:
                    raise ValueError("DOCKER_OBSERVATION_UNAVAILABLE")
            return json.loads(body)

    async def quiescent(self, owner: str) -> bool:
        return await self._observe(owner, allow_workloads=False)

    async def healthy(self, owner: str) -> bool:
        """Verify retained Docker/control health without claiming workloads idle."""
        return await self._observe(owner, allow_workloads=True)

    async def _observe(self, owner: str, *, allow_workloads: bool) -> bool:
        try:
            async with asyncio.timeout(3):
                info = await self._get("info")
                if (
                    info["DockerRootDir"] != str(self.config.docker_root)
                    or info["Swarm"]["LocalNodeState"] != "inactive"
                ):
                    return False
                rows = await self._get("containers/json", all_containers=True)
                if not isinstance(rows, list) or len(rows) > (
                    1024 if allow_workloads else len(self.config.allowed_containers)
                ):
                    return False
                allowed = {item.id: item for item in self.config.allowed_containers}
                seen: set[str] = set()
                for row in rows:
                    cid = row["Id"]
                    if cid in seen:
                        return False
                    seen.add(cid)
                    if cid not in allowed:
                        if allow_workloads:
                            continue
                        return False
                    item = await self._get(f"containers/{cid}/json")
                    state, labels = item["State"], item["Config"]["Labels"]
                    if (
                        item["Id"] != cid
                        or item["Image"] != allowed[cid].image
                        or item["ExecIDs"] not in (None, [])
                        or labels.get("awf.node.role") != "control-plane"
                        or labels.get("awf.node.owner") != owner
                        or state["Running"] is not True
                        or state["Status"] != "running"
                        or any(state[key] is not False for key in ("Paused", "Restarting", "Dead"))
                    ):
                        return False
                # Detect inventory change during inspection; Core admission is
                # independently closed before the drain's final DB check.
                again = await self._get("containers/json", all_containers=True)
                return (
                    isinstance(again, list)
                    and len(again) == len(seen)
                    and {row["Id"] for row in again} == seen
                    and (not allow_workloads or set(allowed).issubset(seen))
                )
        except (httpx.HTTPError, OSError, TimeoutError, ValueError, KeyError, TypeError):
            return False
