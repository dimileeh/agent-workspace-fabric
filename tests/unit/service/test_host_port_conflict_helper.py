"""Tests for ``_host_ports_from_resolved_profile`` and the ``check_host_port_conflicts`` service helper."""

from __future__ import annotations

from typing import Any

from awf.service.workspaces import _host_ports_from_resolved_profile


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
