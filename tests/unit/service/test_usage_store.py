"""Unit tests for the AWF-owned usage snapshot store and ccusage normalizer."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.db.enums import AgentRuntime
from awf.service import usage_store
from awf.service.usage_store import (
    REASON_INVALID_JSON,
    REASON_NO_RECORDS,
    SCHEMA_VERSION,
    NormalizedUsage,
    UsageSnapshot,
    normalize_ccusage_json,
    provider_ccusage_source,
    read_latest_usage_snapshot,
    subtract_baseline,
    workspace_usage_dir,
    write_usage_snapshot,
)

_ALLOWED_SNAPSHOT_KEYS = {
    "schema_version",
    "workspace_id",
    "provider",
    "source",
    "ccusage_source",
    "model",
    "status",
    "reason",
    "phase",
    "captured_at",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_estimate",
    "currency",
    "baseline",
}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("runtime", "expected"),
    [
        (AgentRuntime.claude_code, "claude"),
        (AgentRuntime.codex, "codex"),
        (AgentRuntime.gemini, "gemini"),
        (AgentRuntime.opencode, "opencode"),
    ],
)
def test_provider_ccusage_source_maps_all_supported_runtimes(
    runtime: AgentRuntime, expected: str
) -> None:
    assert provider_ccusage_source(runtime) == expected


@pytest.mark.unit
def test_provider_ccusage_source_unknown_returns_none() -> None:
    assert provider_ccusage_source("mystery") is None  # type: ignore[arg-type]


@pytest.mark.unit
def test_normalize_ccusage_json_reads_totals() -> None:
    raw = json.dumps(
        {
            "totals": {
                "inputTokens": 100,
                "outputTokens": 40,
                "totalTokens": 140,
                "totalCost": 0.25,
            }
        }
    )
    usage, reason = normalize_ccusage_json(raw)
    assert reason is None
    assert usage == NormalizedUsage(
        input_tokens=100,
        output_tokens=40,
        total_tokens=140,
        cost_estimate=0.25,
        currency="USD",
        model=None,
    )


@pytest.mark.unit
def test_normalize_ccusage_json_sums_daily_when_no_totals() -> None:
    raw = json.dumps(
        {
            "daily": [
                {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15, "totalCost": 0.01},
                {"inputTokens": 20, "outputTokens": 10, "totalTokens": 30, "totalCost": 0.02},
            ]
        }
    )
    usage, reason = normalize_ccusage_json(raw)
    assert reason is None
    assert usage is not None
    assert usage.input_tokens == 30
    assert usage.output_tokens == 15
    assert usage.total_tokens == 45
    assert usage.cost_estimate is not None
    assert abs(usage.cost_estimate - 0.03) < 1e-9


@pytest.mark.unit
def test_normalize_ccusage_json_tolerates_missing_fields_and_synthesizes_total() -> None:
    raw = json.dumps({"totals": {"inputTokens": 100}})
    usage, reason = normalize_ccusage_json(raw)
    assert reason is None
    assert usage is not None
    assert usage.input_tokens == 100
    assert usage.output_tokens is None
    # total synthesized from the available token counts
    assert usage.total_tokens == 100
    assert usage.cost_estimate is None
    assert usage.currency is None


@pytest.mark.unit
def test_normalize_ccusage_json_extracts_model_when_present() -> None:
    raw = json.dumps({"model": "claude-opus", "totals": {"totalTokens": 7}})
    usage, reason = normalize_ccusage_json(raw)
    assert reason is None
    assert usage is not None
    assert usage.model == "claude-opus"


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["", "   ", "not-json{", "[1, 2, 3]"])
def test_normalize_ccusage_json_invalid_returns_invalid_json(raw: str) -> None:
    usage, reason = normalize_ccusage_json(raw)
    assert usage is None
    assert reason == REASON_INVALID_JSON


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    [
        json.dumps({}),
        json.dumps({"daily": []}),
        json.dumps({"totals": {}}),
        json.dumps({"totals": {"note": "no numbers"}, "daily": []}),
    ],
)
def test_normalize_ccusage_json_no_records(raw: str) -> None:
    usage, reason = normalize_ccusage_json(raw)
    assert usage is None
    assert reason == REASON_NO_RECORDS


@pytest.mark.unit
def test_normalize_ccusage_json_ignores_bool_token_values() -> None:
    # JSON booleans must not be treated as integer token counts.
    raw = json.dumps({"totals": {"inputTokens": True, "outputTokens": 5, "totalCost": True}})
    usage, reason = normalize_ccusage_json(raw)
    assert reason is None
    assert usage is not None
    assert usage.input_tokens is None
    assert usage.output_tokens == 5
    assert usage.cost_estimate is None


@pytest.mark.unit
def test_normalize_ccusage_json_ignores_non_numeric_token_values() -> None:
    raw = json.dumps({"totals": {"inputTokens": "abc", "outputTokens": 5, "totalCost": "free"}})
    usage, reason = normalize_ccusage_json(raw)
    assert reason is None
    assert usage is not None
    assert usage.input_tokens is None
    assert usage.output_tokens == 5
    assert usage.cost_estimate is None


@pytest.mark.unit
def test_normalize_ccusage_json_daily_skips_noise_entries() -> None:
    raw = json.dumps(
        {
            "daily": [
                "not-a-dict",
                {"note": "no numbers here"},
                {"inputTokens": 4, "outputTokens": 1, "totalTokens": 5},
            ]
        }
    )
    usage, reason = normalize_ccusage_json(raw)
    assert reason is None
    assert usage is not None
    assert usage.input_tokens == 4
    assert usage.total_tokens == 5


@pytest.mark.unit
def test_normalize_ccusage_json_daily_preserves_unknown_token_field() -> None:
    # When no daily entry reports a token field, the aggregate must stay
    # ``None`` (unknown) rather than collapsing to a misleading ``0``.
    raw = json.dumps(
        {
            "daily": [
                {"outputTokens": 5},
                {"outputTokens": 7},
            ]
        }
    )
    usage, reason = normalize_ccusage_json(raw)
    assert reason is None
    assert usage is not None
    assert usage.input_tokens is None
    assert usage.output_tokens == 12
    assert usage.total_tokens == 12
    assert usage.cost_estimate is None


@pytest.mark.unit
def test_normalized_usage_baseline_dict_round_trip() -> None:
    usage = NormalizedUsage(
        input_tokens=1, output_tokens=2, total_tokens=3, cost_estimate=0.4, currency="USD"
    )
    assert NormalizedUsage.from_baseline_dict(usage.as_baseline_dict()) == usage


@pytest.mark.unit
def test_snapshot_baseline_usage_helper() -> None:
    snapshot = UsageSnapshot(
        workspace_id="ws",
        provider="codex",
        ccusage_source="codex",
        status="available",
        phase="live",
        captured_at="2026-05-22T00:00:00+00:00",
        baseline={"total_tokens": 7},
    )
    baseline = snapshot.baseline_usage()
    assert baseline is not None
    assert baseline.total_tokens == 7
    # No baseline dict -> None.
    assert (
        UsageSnapshot(
            workspace_id="ws",
            provider="codex",
            ccusage_source="codex",
            status="available",
            phase="live",
            captured_at="2026-05-22T00:00:00+00:00",
        ).baseline_usage()
        is None
    )


@pytest.mark.unit
def test_subtract_baseline_with_partial_baseline_fields() -> None:
    current = NormalizedUsage(input_tokens=10, total_tokens=10, cost_estimate=0.5)
    baseline = NormalizedUsage(input_tokens=4)  # only input has a baseline value
    delta = subtract_baseline(current, baseline)
    assert delta.input_tokens == 6
    # baseline missing for these -> current passes through unchanged
    assert delta.total_tokens == 10
    assert delta.cost_estimate == 0.5


@pytest.mark.unit
def test_subtract_baseline_clamps_to_non_negative() -> None:
    current = NormalizedUsage(
        input_tokens=150, output_tokens=60, total_tokens=210, cost_estimate=0.30, currency="USD"
    )
    baseline = NormalizedUsage(
        input_tokens=100, output_tokens=80, total_tokens=180, cost_estimate=0.25, currency="USD"
    )
    delta = subtract_baseline(current, baseline)
    assert delta.input_tokens == 50
    # baseline larger than current -> clamped to 0, never negative
    assert delta.output_tokens == 0
    assert delta.total_tokens == 30
    assert delta.cost_estimate is not None
    assert abs(delta.cost_estimate - 0.05) < 1e-9
    assert delta.currency == "USD"


@pytest.mark.unit
def test_subtract_baseline_none_returns_current() -> None:
    current = NormalizedUsage(input_tokens=10, total_tokens=10)
    assert subtract_baseline(current, None) == current


@pytest.mark.unit
def test_subtract_baseline_preserves_none_current_fields() -> None:
    current = NormalizedUsage(input_tokens=None, total_tokens=5)
    baseline = NormalizedUsage(input_tokens=3, total_tokens=2)
    delta = subtract_baseline(current, baseline)
    assert delta.input_tokens is None
    assert delta.total_tokens == 3


@pytest.mark.unit
def test_usage_snapshot_has_metrics() -> None:
    assert UsageSnapshot(
        workspace_id="ws",
        provider="claude_code",
        ccusage_source="claude",
        status="available",
        phase="final",
        captured_at="2026-05-22T00:00:00+00:00",
        total_tokens=5,
    ).has_metrics
    assert not UsageSnapshot(
        workspace_id="ws",
        provider="claude_code",
        ccusage_source="claude",
        status="unavailable",
        phase="final",
        captured_at="2026-05-22T00:00:00+00:00",
        reason="ccusage_no_records",
    ).has_metrics


@pytest.mark.unit
def test_write_and_read_snapshot_round_trip(tmp_path: Path) -> None:
    snapshot = UsageSnapshot(
        workspace_id="ws_round",
        provider="codex",
        ccusage_source="codex",
        status="available",
        phase="live",
        captured_at="2026-05-22T01:02:03+00:00",
        model="gpt-5",
        input_tokens=12,
        output_tokens=8,
        total_tokens=20,
        cost_estimate=0.02,
        currency="USD",
        baseline={"total_tokens": 4},
    )
    path = write_usage_snapshot(snapshot, work_dir=tmp_path)
    assert path == workspace_usage_dir(tmp_path, "ws_round") / "snapshot.json"

    loaded = read_latest_usage_snapshot("ws_round", work_dir=tmp_path)
    assert loaded == snapshot
    assert loaded is not None
    assert loaded.schema_version == SCHEMA_VERSION


@pytest.mark.unit
def test_write_snapshot_is_atomic_overwrite(tmp_path: Path) -> None:
    base = {
        "workspace_id": "ws_over",
        "provider": "gemini",
        "ccusage_source": "gemini",
        "status": "available",
        "captured_at": "2026-05-22T01:02:03+00:00",
    }
    write_usage_snapshot(UsageSnapshot(phase="live", total_tokens=5, **base), work_dir=tmp_path)
    write_usage_snapshot(UsageSnapshot(phase="final", total_tokens=9, **base), work_dir=tmp_path)
    loaded = read_latest_usage_snapshot("ws_over", work_dir=tmp_path)
    assert loaded is not None
    assert loaded.phase == "final"
    assert loaded.total_tokens == 9
    # No leftover temp files in the usage dir.
    usage_dir = workspace_usage_dir(tmp_path, "ws_over")
    assert [p.name for p in usage_dir.iterdir()] == ["snapshot.json"]


@pytest.mark.unit
def test_snapshot_serialization_contains_only_safe_keys(tmp_path: Path) -> None:
    snapshot = UsageSnapshot(
        workspace_id="ws_safe",
        provider="claude_code",
        ccusage_source="claude",
        status="available",
        phase="final",
        captured_at="2026-05-22T01:02:03+00:00",
        total_tokens=10,
        baseline={"total_tokens": 1},
    )
    path = write_usage_snapshot(snapshot, work_dir=tmp_path)
    payload = json.loads(path.read_text())
    assert set(payload) == _ALLOWED_SNAPSHOT_KEYS
    # No raw transcript paths or jsonl references leak into the durable record.
    text = path.read_text()
    assert ".jsonl" not in text
    assert "/home/" not in text
    assert "projects" not in text


@pytest.mark.unit
def test_read_latest_usage_snapshot_missing_returns_none(tmp_path: Path) -> None:
    assert read_latest_usage_snapshot("ws_absent", work_dir=tmp_path) is None


@pytest.mark.unit
def test_read_latest_usage_snapshot_none_workspace_returns_none(tmp_path: Path) -> None:
    assert read_latest_usage_snapshot(None, work_dir=tmp_path) is None


@pytest.mark.unit
def test_read_latest_usage_snapshot_malformed_file_returns_none(tmp_path: Path) -> None:
    usage_dir = workspace_usage_dir(tmp_path, "ws_bad")
    usage_dir.mkdir(parents=True)
    (usage_dir / "snapshot.json").write_text("{not json")
    assert read_latest_usage_snapshot("ws_bad", work_dir=tmp_path) is None


@pytest.mark.unit
def test_read_latest_usage_snapshot_defaults_to_settings_work_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        usage_store,
        "get_settings",
        lambda: SimpleNamespace(work_dir=str(tmp_path)),
    )
    snapshot = UsageSnapshot(
        workspace_id="ws_default",
        provider="opencode",
        ccusage_source="opencode",
        status="available",
        phase="final",
        captured_at="2026-05-22T01:02:03+00:00",
        total_tokens=3,
    )
    write_usage_snapshot(snapshot, work_dir=tmp_path)
    loaded = read_latest_usage_snapshot("ws_default")
    assert loaded == snapshot


@pytest.mark.unit
def test_read_latest_usage_snapshot_normalizes_user_home_work_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The worker normalizes settings.work_dir with .expanduser().resolve()
    # before the collector writes snapshots. The reader's default path must
    # normalize identically; otherwise the API reads a different directory than
    # the worker wrote and silently falls back to usage_not_reported.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    work_dir_setting = "~/awf-work"
    writer_work_dir = Path(work_dir_setting).expanduser().resolve()
    monkeypatch.setattr(
        usage_store,
        "get_settings",
        lambda: SimpleNamespace(work_dir=work_dir_setting),
    )
    snapshot = UsageSnapshot(
        workspace_id="ws_tilde",
        provider="claude_code",
        ccusage_source="claude",
        status="available",
        phase="final",
        captured_at="2026-05-22T01:02:03+00:00",
        total_tokens=11,
    )
    write_usage_snapshot(snapshot, work_dir=writer_work_dir)

    loaded = read_latest_usage_snapshot("ws_tilde")
    assert loaded == snapshot
