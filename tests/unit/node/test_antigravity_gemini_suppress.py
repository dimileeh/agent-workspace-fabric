"""Verify gcloud host credentials are mounted while gemini and ADC are not staged."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.node.auth_mounts import resolve_service_auth_mounts


@pytest.mark.unit
def test_gcloud_host_credentials_mounted_and_gemini_and_adc_not_mounted(
    tmp_path: Path,
) -> None:
    """~/.config/gcloud is mounted; ~/.gemini and ambient GOOGLE_APPLICATION_CREDENTIALS are not."""
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    (host_home / ".gemini").mkdir(parents=True)
    (host_home / ".gemini" / "settings.json").write_text("{}")
    (host_home / ".config" / "gcloud").mkdir(parents=True)
    adc_file = tmp_path / "adc.json"
    adc_file.write_text("{}")

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_test",
        host_env={"GOOGLE_APPLICATION_CREDENTIALS": str(adc_file)},
    )

    sources = {m.source for m in mounts}
    targets = {m.target for m in mounts}
    assert "/home/agent/.gemini" not in targets
    assert "/home/agent/.config/gcloud" in targets
    assert str(adc_file) not in sources
