from __future__ import annotations

import pytest

import tests.conftest as shared_conftest
from awf.common.config import get_settings


@pytest.mark.asyncio
async def test_client_fixture_clears_settings_cache_when_app_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AWF_API_TOKEN", raising=False)
    get_settings.cache_clear()

    local_patch = pytest.MonkeyPatch()
    api_token = shared_conftest._API_TEST_TOKEN

    def fail_after_settings_cache(*, use_lifespan: bool) -> object:
        assert use_lifespan is False
        assert get_settings().api_token == api_token
        raise RuntimeError("app setup failed")

    monkeypatch.setattr(shared_conftest, "create_app", fail_after_settings_cache)
    fixture = shared_conftest.client.__wrapped__(object(), local_patch)

    try:
        with pytest.raises(RuntimeError, match="app setup failed"):
            await fixture.__anext__()
        local_patch.undo()

        assert get_settings().api_token != api_token
    finally:
        local_patch.undo()
        get_settings.cache_clear()
