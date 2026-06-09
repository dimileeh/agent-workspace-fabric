def test_directly_exercised_monkeypatch(monkeypatch):
    import service

    monkeypatch.setattr(service, "do_work", lambda: "fake")

    assert service.do_work() == "fake"
