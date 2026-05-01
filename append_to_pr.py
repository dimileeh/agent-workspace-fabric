with open("tests/unit/service/test_provider_recovery.py", "a") as f:
    f.write("""
from awf.service.provider_recovery import (
    FallbackTarget,
    provider_cooldown_not_before,
    provider_for_agent_model,
    _fallback_targets,
    _has_existing_provider_recovery_event,
    _latest_failed_state_event,
    _nested_value,
    _nonnegative_int,
    _policy_model,
    _record_provider_circuit_breaker,
    _retry_task_for_source,
    provider_recovery_metadata_from_workspace,
    ProviderRecoveryPolicy,
    ProviderRecoveryState,
    _classification_metadata,
    _decision_payload,
    _select_fallback_target,
    _source_suppression_not_before
)
from awf.db.models import Workspace

def test_fallback_target_to_payload():
    ft = FallbackTarget(agent="codex", provider="openai", model="gpt-4")
    assert ft.to_payload() == {"agent": "codex", "provider": "openai", "model": "gpt-4"}
    
    ft2 = FallbackTarget(agent="gemini", provider=None, model="gemini-1.5")
    assert ft2.to_payload() == {"agent": "gemini", "model": "gemini-1.5"}

def test_provider_recovery_metadata_from_failure_existing():
    metadata = provider_recovery_metadata_from_failure(
        reason_code=None,
        message=None,
        details={"provider_recovery": {"foo": "bar", "retryable": True}},
        task_policy={}
    )
    assert metadata["foo"] == "bar"

def test_provider_recovery_metadata_from_failure_evidence():
    metadata = provider_recovery_metadata_from_failure(
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        message="some long error message with sk-secret-key " * 20,
        details={"provider": "google", "model": "gemini-pro"},
        task_policy={}
    )
    assert "evidence" in metadata
    assert "<redacted>" in metadata["evidence"]

def test_decide_provider_recovery_non_retryable():
    decision = decide_provider_recovery(
        {"retryable": False},
        task_policy={},
        current_agent="gemini",
        current_model="gemini-pro",
        now=datetime.now(UTC)
    )
    assert decision.action == "terminal"
    assert decision.reason_code == "NON_RETRYABLE_PROVIDER_FAILURE"

def test_provider_for_agent_model():
    assert provider_for_agent_model("gemini", None) == "google"
    assert provider_for_agent_model("codex", None) == "openai"
    assert provider_for_agent_model("claude_code", None) == "anthropic"
    assert provider_for_agent_model("opencode", None) == "opencode"
    assert provider_for_agent_model("unknown", None) is None

def test_provider_cooldown_not_before():
    assert provider_cooldown_not_before(None) is None
    assert provider_cooldown_not_before({}) is None
    assert provider_cooldown_not_before({"provider_recovery_state": {"not_before": "invalid"}}) is None
    assert provider_cooldown_not_before({"provider_recovery_state": {"not_before": "2026-05-01T12:00:00Z"}}) == datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

@pytest.mark.unit
async def test_create_provider_recovery_attempt_row_terminal(factory):
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/repo.git",
            branch_base="main",
            task_title="title",
            task_prompt="prompt",
            agent="gemini",
            task_policy={},
            test_commands=[],
            owned_paths=[]
        )
        await session.commit()
        ws_id = ws.id

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            ws_id,
            metadata={"retryable": False, "provider": "google", "model": "gemini-pro"},
            now=datetime.now(UTC)
        )
        assert result == "terminal"

@pytest.mark.unit
async def test_create_provider_recovery_attempt_row_early_returns(factory):
    async with factory() as session:
        result = await create_provider_recovery_attempt_row(session, "invalid_id")
        assert result is None

def test_fallback_targets_edge_cases():
    targets = _fallback_targets(["not_a_mapping", {"agent": "gemini"}, {"agent": "codex", "model": "gpt-4"}, "another_string", True, {"model": "missing_agent"}])
    assert len(targets) == 1
    assert targets[0].agent == "codex"
    
    assert _fallback_targets(None) == []
    assert _fallback_targets("string") == []

def test_policy_model_edge_cases():
    assert _policy_model(None) is None
    assert _policy_model({"agent_model": "gpt-4"}) == "gpt-4"

def test_nonnegative_int_edge_cases():
    assert _nonnegative_int(True, default=5) == 5
    assert _nonnegative_int(False, default=5) == 5
    assert _nonnegative_int(4.0, default=5) == 4
    assert _nonnegative_int(4.5, default=5) == 5

def test_nested_value_edge_cases():
    assert _nested_value({"foo": {"bar": 1}}, "foo", "bar") == 1
    assert _nested_value({"foo": 1}, "foo", "bar") is None
    assert _nested_value({}, "foo", "bar") is None

def test_latest_failed_state_event_edge_cases():
    ws = Workspace()
    ws.events = []
    assert _latest_failed_state_event(ws) is None
    
    ws.events = [WorkspaceEvent(event_type="workspace.state_changed", new_state="running")]
    assert _latest_failed_state_event(ws) is None
    
    ws.events = [WorkspaceEvent(event_type="other_event", new_state="failed")]
    assert _latest_failed_state_event(ws) is None

def test_has_existing_provider_recovery_event_edge_cases():
    ws = Workspace()
    ws.events = [
        WorkspaceEvent(event_type="workspace.provider_recovery_requested", payload=True),
        WorkspaceEvent(event_type="workspace.provider_recovery_requested", payload={"provider_recovery": {"failure_fingerprint": "fingerprint1"}})
    ]
    assert _has_existing_provider_recovery_event(ws, {"failure_fingerprint": "fingerprint1"}) is True
    assert _has_existing_provider_recovery_event(ws, {"failure_fingerprint": "fingerprint2"}) is False
    assert _has_existing_provider_recovery_event(ws, {}) is False

@pytest.mark.unit
async def test_retry_task_for_source_no_attempt(factory):
    async with factory() as session:
        ws = Workspace(id="ws_1", repo_url="url", branch_base="base", task_title="title", task_prompt="prompt", task_class="class", owned_paths=[], task_external_id="ext")
        task = await _retry_task_for_source(session, ws, source_attempt=None)
        assert task is not None
        assert task.idempotency_key == "retry-source-workspace:ws_1"

@pytest.mark.unit
async def test_record_provider_circuit_breaker_edge_cases(factory):
    async with factory() as session:
        ws = Workspace(id="ws_1", task_policy={})
        await _record_provider_circuit_breaker(session, ws, {}, now=datetime.now(UTC))
        await _record_provider_circuit_breaker(session, ws, {"provider": "p", "model": "m", "failure_fingerprint": "f", "reason_code": "OTHER"}, now=datetime.now(UTC))

def test_decide_provider_recovery_attempts_exhausted():
    decision = decide_provider_recovery(
        {"retryable": True, "provider": "google", "model": "gemini"},
        task_policy={"provider_recovery_state": {"retry_attempt_number": 1, "fallback_attempt_number": 0}},
        current_agent="gemini",
        current_model="gemini",
        now=datetime.now(UTC)
    )
    assert decision.action == "terminal"
    assert decision.reason_code == "PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED"

def test_provider_recovery_metadata_from_workspace_details_not_mapping():
    ws = Workspace(failure_reason="reason", failure_message="msg")
    ws.events = [WorkspaceEvent(event_type="workspace.state_changed", new_state="failed", payload={"details": "not_mapping"})]
    provider_recovery_metadata_from_workspace(ws)

def test_provider_cooldown_not_before_more():
    assert provider_cooldown_not_before({"provider_recovery_state": {"not_before": 123}}) is None
    dt = provider_cooldown_not_before({"provider_recovery_state": {"not_before": "2026-05-01T12:00:00"}})
    assert dt is not None
    assert dt.tzinfo == UTC

def test_classification_metadata_recommended_action():
    metadata = _classification_metadata(
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        message="some err",
        details={"provider": "google", "model": "gemini", "recommended_action": "Do something else"}
    )
    if metadata:
        assert metadata.get("recommended_action") == "Do something else"

def test_select_fallback_target_more():
    policy = ProviderRecoveryPolicy(fallbacks=(FallbackTarget("a", "b", "c"),), max_fallback_attempts=1)
    state1 = ProviderRecoveryState(fallback_attempt_number=1)
    assert _select_fallback_target(policy, state1) is None
    policy2 = ProviderRecoveryPolicy(fallbacks=(), max_fallback_attempts=1)
    state2 = ProviderRecoveryState(fallback_attempt_number=0)
    assert _select_fallback_target(policy2, state2) is None

def test_source_suppression_not_before_more():
    decision = ProviderRecoveryDecision(
        action="retry",
        retryable=True,
        not_before=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        target_agent="a",
        target_provider="b",
        target_model="c",
        reason_code="RC",
        terminal_reason=None,
        fallback_attempt_number=0,
        retry_attempt_number=0
    )
    res = _source_suppression_not_before({}, policy=ProviderRecoveryPolicy(), state=ProviderRecoveryState(), decision=decision, now=datetime.now(UTC))
    assert res == decision.not_before

def test_decision_payload_more():
    decision = ProviderRecoveryDecision(
        action="terminal",
        retryable=True,
        not_before=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        target_agent="a",
        target_provider="b",
        target_model="c",
        reason_code="RC",
        terminal_reason=None,
        fallback_attempt_number=0,
        retry_attempt_number=0
    )
    payload = _decision_payload(decision, {}, not_before=datetime(2026, 5, 2, 12, 0, tzinfo=UTC))
    assert payload["not_before"] == "2026-05-02T12:00:00+00:00"

@pytest.mark.unit
async def test_retry_task_for_source_task_not_found(factory):
    async with factory() as session:
        ws = Workspace(id="ws_2", repo_url="url", branch_base="base", task_title="title", task_prompt="prompt", task_class="class", owned_paths=[], task_external_id="ext")
        attempt = TaskAttempt(id="attempt_1", task_id="nonexistent")
        task = await _retry_task_for_source(session, ws, source_attempt=attempt)
        assert task is not None
        assert task.idempotency_key == "retry-source-workspace:ws_2"

@pytest.mark.unit
async def test_create_provider_recovery_attempt_row_immediate_fallback(factory):
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/repo.git",
            branch_base="main",
            task_title="title",
            task_prompt="prompt",
            agent="gemini",
            task_policy={
                "provider_recovery": {
                    "fallbacks": [{"agent": "codex", "model": "gpt-4"}],
                    "max_same_provider_retries": 0,
                    "max_fallback_attempts": 1
                }
            },
            test_commands=[],
            owned_paths=[]
        )
        await session.commit()
        ws_id = ws.id

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            ws_id,
            metadata={"retryable": True, "provider": "google", "model": "gemini"},
            now=datetime.now(UTC)
        )
        assert result is not None
        assert result != "terminal"

@pytest.mark.unit
async def test_create_provider_recovery_attempt_row_no_target_model_and_existing_event(factory):
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/repo.git",
            branch_base="main",
            task_title="title",
            task_prompt="prompt",
            agent="gemini",
            task_policy={},
            test_commands=[],
            owned_paths=[]
        )
        await repo.add_event(ws, event_type="workspace.provider_recovery_requested", payload={"provider_recovery": {"failure_fingerprint": "f1"}})
        await session.commit()
        ws_id = ws.id

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            ws_id,
            metadata={"retryable": True, "provider": "google", "model": None, "failure_fingerprint": "f1"},
            now=datetime.now(UTC)
        )
        assert result is None

@pytest.mark.unit
async def test_create_provider_recovery_attempt_row_no_metadata_from_workspace(factory):
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/repo.git",
            branch_base="main",
            task_title="title",
            task_prompt="prompt",
            agent="gemini",
            task_policy={},
            test_commands=[],
            owned_paths=[]
        )
        await session.commit()
        ws_id = ws.id

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            ws_id,
            metadata=None,
            now=datetime.now(UTC)
        )
        assert result is None

@pytest.mark.unit
async def test_create_provider_recovery_attempt_row_retry_no_model(factory):
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/repo.git",
            branch_base="main",
            task_title="title",
            task_prompt="prompt",
            agent="gemini",
            task_policy={"provider_recovery": {"max_same_provider_retries": 1}},
            test_commands=[],
            owned_paths=[]
        )
        await session.commit()
        ws_id = ws.id

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            ws_id,
            metadata={"retryable": True, "provider": "google"},
            now=datetime.now(UTC)
        )
        assert result is not None
        assert result != "terminal"
        assert result.provider_recovery["action"] == "retry"
""")
