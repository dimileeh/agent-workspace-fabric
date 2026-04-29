def test_do_work(monkeypatch):
    from service import do_work

    monkeypatch.setattr("service.do_work", lambda: "fake")

    assert do_work() == "fake"
