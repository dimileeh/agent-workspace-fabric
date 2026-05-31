"""Tests for ``_host_ports_from_resolved_profile``, ``_host_ports_from_task_policy_companions``, and the ``check_host_port_conflicts`` service helper."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from awf.db.repositories.base import (
    host_ports_from_resolved_profile as _host_ports_from_resolved_profile,
)
from awf.db.repositories.base import (
    host_ports_from_task_policy_companions as _host_ports_from_task_policy_companions,
)
from awf.service.workspaces import (
    WorkspaceCreateDuplicateHostPortError,
    check_host_port_conflicts,
)


class TestHostPortsFromResolvedProfile:
    """Pure-unit tests for extracting host-side ports from a resolved profile."""

    def test_none_profile_returns_empty(self) -> None:
        assert _host_ports_from_resolved_profile(None) == []

    def test_empty_dict_returns_empty(self) -> None:
        assert _host_ports_from_resolved_profile({}) == []

    def test_no_services_key_returns_empty(self) -> None:
        assert _host_ports_from_resolved_profile({"name": "p"}) == []

    def test_services_not_list_returns_empty(self) -> None:
        assert _host_ports_from_resolved_profile({"services": "bad"}) == []

    def test_empty_services_returns_empty(self) -> None:
        assert _host_ports_from_resolved_profile({"services": []}) == []

    def test_service_without_ports_returns_empty(self) -> None:
        assert _host_ports_from_resolved_profile({"services": [{"name": "db"}]}) == []

    def test_service_with_empty_ports_returns_empty(self) -> None:
        assert _host_ports_from_resolved_profile({"services": [{"ports": []}]}) == []

    def test_single_service_single_port(self) -> None:
        profile: dict[str, Any] = {
            "services": [{"name": "postgres", "image": "pg:16", "ports": [[5432, 5432]]}]
        }
        assert _host_ports_from_resolved_profile(profile) == [5432]

    def test_single_service_multiple_ports(self) -> None:
        profile: dict[str, Any] = {
            "services": [{"name": "web", "ports": [[80, 8080], [443, 8443]]}]
        }
        result = _host_ports_from_resolved_profile(profile)
        assert sorted(result) == [8080, 8443]

    def test_multiple_services(self) -> None:
        profile: dict[str, Any] = {
            "services": [
                {"name": "postgres", "ports": [[5432, 5432]]},
                {"name": "redis", "ports": [[6379, 6379]]},
            ]
        }
        result = _host_ports_from_resolved_profile(profile)
        assert sorted(result) == [5432, 6379]

    def test_non_dict_service_entry_skipped(self) -> None:
        profile: dict[str, Any] = {"services": ["not-a-dict", {"ports": [[80, 8080]]}]}
        assert _host_ports_from_resolved_profile(profile) == [8080]

    def test_non_list_tuple_port_mapping_skipped(self) -> None:
        profile: dict[str, Any] = {"services": [{"ports": ["8080"]}]}
        assert _host_ports_from_resolved_profile(profile) == []

    def test_short_port_mapping_skipped(self) -> None:
        profile: dict[str, Any] = {"services": [{"ports": [[80]]}]}
        assert _host_ports_from_resolved_profile(profile) == []

    def test_non_int_host_port_skipped(self) -> None:
        profile: dict[str, Any] = {"services": [{"ports": [[80, "abc"]]}]}
        assert _host_ports_from_resolved_profile(profile) == []

    def test_mix_valid_and_invalid(self) -> None:
        profile: dict[str, Any] = {
            "services": [
                {
                    "ports": [
                        [80, 8080],
                        [443],
                        "bad",
                        [22, "abc"],
                        [9090, 9090],
                    ]
                }
            ]
        }
        result = _host_ports_from_resolved_profile(profile)
        assert sorted(result) == [8080, 9090]

    def test_mixed_companion_and_profile_ports_no_duplicate_dedup(
        self,
    ) -> None:
        """Dedup is not the helper's job; callers (find_host_port_conflicts) handle set membership."""
        profile: dict[str, Any] = {
            "services": [
                {"name": "postgres", "ports": [[5432, 5432]]},
                {"name": "pg2", "ports": [[5432, 5432]]},
            ]
        }
        result = _host_ports_from_resolved_profile(profile)
        assert result == [5432, 5432]


class TestHostPortsFromTaskPolicyCompanions:
    """Pure-unit tests for extracting host-side ports from task-policy companions."""

    def test_none_returns_empty(self) -> None:
        assert _host_ports_from_task_policy_companions(None) == []

    def test_empty_dict_returns_empty(self) -> None:
        assert _host_ports_from_task_policy_companions({}) == []

    def test_no_companions_key_returns_empty(self) -> None:
        assert _host_ports_from_task_policy_companions({"other": 1}) == []

    def test_companions_not_list_returns_empty(self) -> None:
        assert _host_ports_from_task_policy_companions({"companions": "bad"}) == []

    def test_empty_companions_returns_empty(self) -> None:
        assert _host_ports_from_task_policy_companions({"companions": []}) == []

    def test_companion_without_ports_returns_empty(self) -> None:
        assert _host_ports_from_task_policy_companions({"companions": [{"name": "x"}]}) == []

    def test_companion_with_empty_ports_returns_empty(self) -> None:
        assert _host_ports_from_task_policy_companions({"companions": [{"ports": []}]}) == []

    def test_single_companion_single_port(self) -> None:
        policy: dict[str, Any] = {"companions": [{"name": "sidecar", "ports": [[8080, 9090]]}]}
        assert _host_ports_from_task_policy_companions(policy) == [9090]

    def test_single_companion_multiple_ports(self) -> None:
        policy: dict[str, Any] = {
            "companions": [{"name": "web", "ports": [[80, 8080], [443, 8443]]}]
        }
        result = _host_ports_from_task_policy_companions(policy)
        assert sorted(result) == [8080, 8443]

    def test_multiple_companions(self) -> None:
        policy: dict[str, Any] = {
            "companions": [
                {"name": "redis", "ports": [[6379, 6379]]},
                {"name": "pg", "ports": [[5432, 5433]]},
            ]
        }
        result = _host_ports_from_task_policy_companions(policy)
        assert sorted(result) == [5433, 6379]

    def test_non_dict_companion_entry_skipped(self) -> None:
        policy: dict[str, Any] = {"companions": ["not-a-dict", {"ports": [[80, 8080]]}]}
        assert _host_ports_from_task_policy_companions(policy) == [8080]

    def test_non_list_tuple_port_mapping_skipped(self) -> None:
        policy: dict[str, Any] = {"companions": [{"ports": ["9090"]}]}
        assert _host_ports_from_task_policy_companions(policy) == []

    def test_short_port_mapping_skipped(self) -> None:
        policy: dict[str, Any] = {"companions": [{"ports": [[80]]}]}
        assert _host_ports_from_task_policy_companions(policy) == []

    def test_non_int_host_port_skipped(self) -> None:
        policy: dict[str, Any] = {"companions": [{"ports": [[80, "abc"]]}]}
        assert _host_ports_from_task_policy_companions(policy) == []

    def test_duplicate_ports_not_deduped(self) -> None:
        policy: dict[str, Any] = {
            "companions": [
                {"name": "a", "ports": [[80, 8080]]},
                {"name": "b", "ports": [[80, 8080]]},
            ]
        }
        result = _host_ports_from_task_policy_companions(policy)
        assert result == [8080, 8080]


class TestCheckHostPortConflictsIntraRequestDuplicate:
    """Pure-unit tests for intra-request duplicate host-port rejection in check_host_port_conflicts."""

    @staticmethod
    def _make_companion(name: str, ports: list[tuple[int, int]]) -> Any:
        from awf.api.schemas_companions import WorkspaceCompanionRequest

        return WorkspaceCompanionRequest(
            name=name,
            repo_url="https://github.com/example/repo",
            ports=ports,
        )

    @pytest.mark.asyncio
    async def test_no_duplicate_ports_passes(self) -> None:
        repo = AsyncMock()
        repo.find_host_port_conflicts.return_value = []
        companions = [self._make_companion("a", [(5432, 5432)])]
        profile: dict[str, Any] = {"services": [{"name": "redis", "ports": [[6379, 6379]]}]}
        await check_host_port_conflicts(repo, companions, resolved_profile=profile)

    @pytest.mark.asyncio
    async def test_duplicate_between_companion_and_profile(self) -> None:
        repo = AsyncMock()
        companions = [self._make_companion("a", [(5432, 5432)])]
        profile: dict[str, Any] = {"services": [{"name": "pg", "ports": [[5432, 5432]]}]}
        with pytest.raises(WorkspaceCreateDuplicateHostPortError) as exc_info:
            await check_host_port_conflicts(repo, companions, resolved_profile=profile)
        assert exc_info.value.host_port == 5432

    @pytest.mark.asyncio
    async def test_duplicate_within_profile_services(self) -> None:
        repo = AsyncMock()
        profile: dict[str, Any] = {
            "services": [
                {"name": "pg1", "ports": [[5432, 5432]]},
                {"name": "pg2", "ports": [[5432, 5432]]},
            ]
        }
        with pytest.raises(WorkspaceCreateDuplicateHostPortError) as exc_info:
            await check_host_port_conflicts(repo, [], resolved_profile=profile)
        assert exc_info.value.host_port == 5432

    @pytest.mark.asyncio
    async def test_duplicate_between_two_companions(self) -> None:
        repo = AsyncMock()
        companions = [
            self._make_companion("a", [(8080, 8080)]),
            self._make_companion("b", [(9090, 8080)]),
        ]
        with pytest.raises(WorkspaceCreateDuplicateHostPortError) as exc_info:
            await check_host_port_conflicts(repo, companions, resolved_profile=None)
        assert exc_info.value.host_port == 8080

    @pytest.mark.asyncio
    async def test_no_ports_no_error(self) -> None:
        repo = AsyncMock()
        await check_host_port_conflicts(repo, [], resolved_profile=None)
        repo.acquire_host_port_admission_lock.assert_not_called()
