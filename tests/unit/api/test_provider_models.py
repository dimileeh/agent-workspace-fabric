"""Discovery REST surface uses the same bounded catalog service."""

from awf.api.schemas_providers import ProviderCatalogResponse


async def test_provider_models_endpoint(client, monkeypatch):
    async def query(self, selection, **kwargs):
        assert selection.provider == "openrouter"
        return ProviderCatalogResponse(
            status="unavailable",
            reason_code="PROVIDER_DISCOVERY_UNAVAILABLE",
            checked_at="now",
            connection_scope=selection.connection_scope(),
        )

    monkeypatch.setattr("awf.service.provider_catalog.ProviderCatalog.query", query)
    response = await client.post(
        "/v1/providers/models/query", json={"agent": "codex", "provider": "openrouter"}
    )
    assert response.status_code == 200
    assert response.json()["manual_entry_allowed"] is True
    assert response.json()["coding_compatible"] is False
