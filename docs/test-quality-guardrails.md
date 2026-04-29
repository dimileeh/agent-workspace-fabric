# Test Quality Guardrails

AWF runs a lightweight static checker over its Python tests to block obvious
false-green placeholders. The checker is intentionally narrow: it catches
high-confidence cases that do not prove behavior, and it avoids becoming a
general test style linter.

## Rules

`EMPTY_TEST`

Flags pytest-collected test functions and `Test*` methods whose effective body
is only a docstring, `pass`, `...`, bare `return`, or `return None`.

`FAKE_ASSERT`

Flags exact boolean placeholder assertions:

```python
assert True
assert False
```

It does not flag comparisons, truthiness checks, identity checks, or assertion
helpers.

`SKIP_ONLY_TEST`

Flags tests that only call `pytest.skip(...)`, including through an
unconditional branch wrapper, and tests with unconditional skip markers:

```python
@pytest.mark.skip(reason="TODO")
def test_later():
    assert real_behavior()

def test_later():
    pytest.skip("TODO")

def test_later():
    if True:
        pytest.skip("TODO")
```

Conditional skips are allowed when they preserve a real test body:

```python
@pytest.mark.skipif(not HAS_DOCKER, reason="requires Docker")
def test_docker_flow():
    assert run_flow()

def test_tool_flow():
    if not has_tool():
        pytest.skip("tool is unavailable")

    assert run_flow()
```

`BROAD_MONKEYPATCH`

Flags the narrow high-confidence pattern where a test monkeypatches a behavior
and then directly calls that same behavior as the only exercised production
entrypoint:

```python
def test_do_work(monkeypatch):
    monkeypatch.setattr(service, "do_work", lambda: "fake")

    assert service.do_work() == "fake"
```

Dependency seams are allowed when the test calls a different production
entrypoint:

```python
def test_orchestrate(monkeypatch):
    monkeypatch.setattr(service, "_run_command", lambda: "ok")

    assert service.orchestrate() == "ok"
```

## Escape Hatches

Escape hatches are for rare cases where a test is intentionally shaped like a
placeholder but still proves something meaningful. Use an inline or immediately
preceding comment:

```python
# awf-test-quality: ignore[EMPTY_TEST] because generated compatibility sentinel must remain import-only
def test_generated_sentinel():
    pass
```

The rationale must explain why the test still proves behavior. Vague reasons
such as `TODO`, `temporary`, or `false positive` are rejected as
`INVALID_ESCAPE_HATCH`. Prefer rewriting the test unless the exception is
clearer and lower risk than changing the fixture.
