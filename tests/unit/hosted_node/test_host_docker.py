import asyncio

import httpx
import pytest

from awf.hosted_node.host_docker import DockerObservation
from tests.unit.hosted_node.test_host_config import host_config

CID = "a" * 64
IMAGE = "sha256:" + "b" * 64


def inventory(**changes):
    value = {
        "Id": CID,
        "Image": IMAGE,
        "ExecIDs": None,
        "Config": {"Labels": {"awf.node.role": "control-plane", "awf.node.owner": "org/node"}},
        "State": {
            "Running": True,
            "Paused": False,
            "Restarting": False,
            "Dead": False,
            "Status": "running",
        },
    }
    value.update(changes)
    return value


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "api",
        "worker",
        "exec",
        "paused",
        "unknown",
        "daemon_error",
        "root",
        "swarm",
        "redirect",
        "changed",
    ],
)
def test_read_only_complete_docker_observation(case):
    async def run():
        calls = []

        def respond(request):
            calls.append(request)
            assert request.method == "GET"
            path = request.url.path
            if case == "daemon_error":
                return httpx.Response(503)
            if case == "redirect":
                return httpx.Response(302, headers={"location": "https://untrusted.invalid"})
            if path.endswith("/info"):
                return httpx.Response(
                    200,
                    json={
                        "DockerRootDir": "/elsewhere" if case == "root" else "/var/lib/awf/docker",
                        "Swarm": {"LocalNodeState": "active" if case == "swarm" else "inactive"},
                    },
                )
            if path.endswith("/containers/json"):
                assert request.url.params["all"] == "true"
                if case == "empty":
                    return httpx.Response(200, json=[])
                if case == "changed" and len(calls) > 3:
                    return httpx.Response(200, json=[])
                return httpx.Response(200, json=[{"Id": CID}])
            value = inventory()
            if case == "exec":
                value["ExecIDs"] = ["active-exec"]
            if case == "paused":
                value["State"]["Paused"] = True
            if case == "unknown":
                value.pop("State")
            return httpx.Response(200, json=value)

        config = host_config(
            allowed_containers=[{"id": CID, "image": IMAGE}] if case != "worker" else []
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="http://docker"
        ) as client:
            observed = await DockerObservation(config, client).quiescent("org/node")
        assert observed == (case in ("empty", "api"))

    asyncio.run(run())


@pytest.mark.parametrize("case", ["oversized", "duplicated", "timeout", "malformed"])
async def test_unknown_docker_responses_are_not_quiescent(case):
    def respond(request):
        if case == "timeout":
            raise httpx.ReadTimeout("synthetic")
        if case == "oversized":
            return httpx.Response(200, content=b" " * (1024 * 1024 + 1))
        if case == "malformed":
            return httpx.Response(200, content=b"not-json")
        if request.url.path.endswith("/info"):
            return httpx.Response(
                200,
                json={
                    "DockerRootDir": "/var/lib/awf/docker",
                    "Swarm": {"LocalNodeState": "inactive"},
                },
            )
        if request.url.path.endswith("/containers/json"):
            return httpx.Response(200, json=[{"Id": CID}, {"Id": CID}])
        return httpx.Response(200, json=inventory())

    config = host_config(
        allowed_containers=[{"id": CID, "image": IMAGE}, {"id": "c" * 64, "image": IMAGE}]
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://docker"
    ) as client:
        assert not await DockerObservation(config, client).quiescent("org/node")
