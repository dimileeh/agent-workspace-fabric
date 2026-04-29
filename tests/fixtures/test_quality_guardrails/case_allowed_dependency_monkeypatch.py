def test_dependency_monkeypatch(monkeypatch):
    import service

    monkeypatch.setattr(service, "_run_command", lambda: "ok")

    result = service.orchestrate()

    assert result == "ok"
