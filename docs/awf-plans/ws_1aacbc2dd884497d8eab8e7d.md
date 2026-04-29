# Plan: P0 Test Quality Guardrails

## Objective

Add a focused static checker for AWF's own Python tests so obviously false-green
tests are caught during validation. The first slice should block:

- empty test functions/methods;
- `assert True` / `assert False` placeholders;
- unconditional skip-only tests with no real runtime condition;
- narrow, obvious cases where `monkeypatch` replaces the exact behavior the
  same test then claims to exercise.

The checker should be AST-based, lightweight, deterministic, and conservative.
It should fail loudly for high-confidence placeholders while avoiding a broad
"test style linter" that would create noise across the existing suite.

## Current Code Context

- Existing quality policy code lives in `src/awf/control/quality_gates.py` and
  currently protects quality-gate configuration paths from unowned edits.
- Existing quality-gate tests live in `tests/unit/control/test_quality_gates.py`.
- Shared pytest fixtures live in `tests/conftest.py`; no custom collection hook
  currently enforces test-body quality.
- `.awf/workspace.yml` currently validates a narrow CLI slice in its self-profile
  phase commands and has a separate 99% Python coverage command. Any self-profile
  wiring must be added only after proving the checker is stable and low-noise.
- A quick repository scan found no current `assert True` / `assert False`
  placeholders and only conditional integration skips. The suite has many valid
  `monkeypatch` uses, so the broad-monkeypatch rule must stay intentionally
  narrow.

## Intended Files And Modules To Touch

Production code:

- `src/awf/control/test_quality_guardrails.py` (new)
  - Define `TestQualityViolation`.
  - Define `scan_test_quality(paths: Sequence[Path]) -> list[TestQualityViolation]`.
  - Implement AST visitors and comment-based escape-hatch parsing.

Tests and fixtures:

- `tests/unit/control/test_test_quality_guardrails.py` (new)
  - Unit tests for individual checker rules using small fixture files.
- `tests/unit/control/test_test_quality_guardrails_self.py` (new)
  - A self-test that runs the checker against AWF's checked-in test suite.
- `tests/fixtures/test_quality_guardrails/*.py` (new)
  - Small positive and negative fixture files. Name files like `case_empty.py`,
    not `test_*.py`, so pytest does not collect intentionally bad examples.

Documentation:

- `docs/test-quality-guardrails.md` (new)
  - Document rule scope and allowed escape hatches with explicit rationale
    requirements.

Conditional self-profile wiring:

- `.awf/workspace.yml`
  - Add a narrow validate command for the checker only if the self-scan passes
    cleanly without broad allowlisting and remains fast. If it is noisy, leave
    this profile unchanged and document why in the PR.

No database migrations, API schemas, console files, package lockfiles, or
coverage thresholds are expected.

## Tests To Write First

1. `test_flags_empty_test_function`
   - Fixture: a file with `def test_placeholder(): pass`.
   - Expected violation: `EMPTY_TEST`.

2. `test_flags_ellipsis_only_test_method`
   - Fixture: a `Test*` class with `def test_placeholder(self): ...`.
   - Expected violation: `EMPTY_TEST`.

3. `test_flags_assert_true_and_assert_false`
   - Fixture: tests containing exact `assert True` and `assert False`.
   - Expected violations: `FAKE_ASSERT` on each placeholder line.

4. `test_flags_unconditional_pytest_skip_only_test`
   - Fixture: `def test_later(): pytest.skip("TODO")`.
   - Expected violation: `SKIP_ONLY_TEST`.

5. `test_flags_unconditional_skip_decorator`
   - Fixture: `@pytest.mark.skip(reason="TODO")` on a test.
   - Expected violation: `SKIP_ONLY_TEST`.

6. `test_allows_conditional_skipif_and_guarded_pytest_skip`
   - Fixture: `@pytest.mark.skipif(not HAS_DOCKER, reason=...)` and
     `if not has_tool(): pytest.skip(...)` followed by a real assertion.
   - Expected: no violations.

7. `test_flags_directly_exercised_monkeypatched_behavior`
   - Fixture: patch `service.do_work` to a fake, then call
     `service.do_work()` as the only exercised behavior.
   - Expected violation: `BROAD_MONKEYPATCH`.

8. `test_allows_monkeypatch_of_dependency_when_production_entrypoint_is_called`
   - Fixture: patch `service._run_command`, then call `service.orchestrate()`
     and assert on the result or recorded call.
   - Expected: no violations.

9. `test_escape_hatch_requires_specific_rationale`
   - Fixture: one violation with
     `# awf-test-quality: ignore[EMPTY_TEST] because ...` and one with a vague
     or missing reason.
   - Expected: the documented rationale suppresses the first violation; the
     missing/vague rationale is reported as `INVALID_ESCAPE_HATCH`.

10. `test_awf_test_suite_has_no_test_quality_guardrail_violations`
    - Run the checker over `tests/unit` and `tests/integration`, excluding
      `tests/fixtures`.
    - Expected: no unsuppressed violations after implementation. If this fails
      because a real intentional exception exists, add the smallest inline
      escape hatch with a concrete rationale.

After adding the first tests, run the focused checker test command and confirm
the expected failures before implementing the production module.

## Implementation Steps

1. Add the new fixture files and failing unit tests.

2. Implement `src/awf/control/test_quality_guardrails.py` with small public
   helpers:
   - `ViolationCode = Literal["EMPTY_TEST", "FAKE_ASSERT", "SKIP_ONLY_TEST",
     "BROAD_MONKEYPATCH", "INVALID_ESCAPE_HATCH"]`
   - `TestQualityViolation(path, line, code, message)`
   - `scan_test_quality(paths, *, exclude_globs=...)`.

3. Use `ast.parse` for Python files and visit only pytest-collected shapes:
   - top-level functions named `test_*`;
   - async test functions named `test_*`;
   - methods named `test_*` inside classes named `Test*`.

4. Empty-test rule:
   - Ignore an optional docstring.
   - Treat `pass`, `...`, and bare `return` / `return None` as no-op bodies.
   - Flag only when every effective statement is a no-op.

5. Fake-assert rule:
   - Flag exact boolean constant assertions: `assert True` and `assert False`.
   - Do not flag comparisons, truthiness checks, sentinel identity checks, or
     assertion helpers. This keeps the rule focused.

6. Skip-only rule:
   - Flag tests whose only effective behavior is an unconditional
     `pytest.skip(...)`.
   - Flag `@pytest.mark.skip(...)` and `@pytest.mark.skipif(True, ...)`.
   - Allow `pytest.mark.skipif(<non-constant condition>, reason=...)`.
   - Allow guarded runtime skips inside an `if` block when the test continues to
     perform real behavior/assertions after the guard.

7. Broad-monkeypatch rule:
   - Keep this deliberately narrow for the P0 slice.
   - Detect `monkeypatch.setattr(...)` calls and extract the patched target leaf
     from either string targets (`"pkg.mod.symbol"`) or object/name pairs
     (`module, "symbol"`).
   - Flag only high-confidence cases where the same test directly calls the
     patched target as the exercised behavior, or where the patched target leaf
     exactly matches the test subject and no other production entrypoint is
     called.
   - Do not flag ordinary dependency seams, environment patching, time patching,
     filesystem error injection, subprocess stubs, or constructor fakes used
     while a different production entrypoint is exercised.

8. Escape hatches:
   - Support inline or immediately preceding comments:
     `# awf-test-quality: ignore[CODE] because <specific rationale>`.
   - Apply suppressions to the nearest violation line or enclosing test.
   - Require a non-empty, specific rationale with a minimum length and reject
     vague values such as `TODO`, `temporary`, or `false positive`.
   - Report malformed escape hatches as `INVALID_ESCAPE_HATCH`.

9. Add `docs/test-quality-guardrails.md`:
   - List each rule and examples.
   - Explain that escape hatches are for rare cases only and must say why the
     test still proves behavior.
   - Include examples of allowed conditional skips and dependency monkeypatches.

10. Add the self-scan test:
    - Default paths: `tests/unit`, `tests/integration`.
    - Excludes: `tests/fixtures/**`, caches, virtualenvs, and generated files.
    - Format failure output as file:line plus rule code so agents can fix tests
      without hunting.

11. Conditional `.awf/workspace.yml` wiring:
    - After the full unit suite and focused self-scan pass cleanly, add a fast
      validate command such as:
      `uv run --python 3.12 --extra dev pytest tests/unit/control/test_test_quality_guardrails_self.py -q`
    - Do not add this command if it requires broad suppressions or produces
      unstable/noisy failures. In that case, keep enforcement through the normal
      unit suite and document the deferral.

## Validation Commands

Focused red/green commands:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_test_quality_guardrails.py -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_test_quality_guardrails_self.py -q
```

Required task validation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit -q
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```

Optional broader confidence if existing tests need nontrivial suppressions or
self-profile wiring changes:

```bash
uv run --python 3.12 --extra dev pytest --cov=awf --cov-report=term-missing
```

## Risks And Assumptions

- Assumption: AWF only needs Python test-quality checking in this slice.
- Assumption: pytest's default naming conventions are sufficient for this repo:
  `test_*` functions/methods and `Test*` classes.
- Assumption: a normal unit test that invokes the scanner is preferable to a
  pytest collection hook for the first slice because it is easier to scope,
  test, and debug.
- Risk: broad monkeypatch detection is the hardest rule to make precise. The
  mitigation is to flag only direct self-faking patterns and document the
  limitation instead of guessing across dependency-injection-heavy tests.
- Risk: fixture files containing intentionally bad tests could be collected by
  pytest. Mitigation: place them under `tests/fixtures/test_quality_guardrails`
  and avoid `test_*.py` filenames.
- Risk: escape hatches could become a bypass. Mitigation: require rule-specific
  inline rationale and have the checker reject vague or malformed suppressions.
- Risk: adding a command to `.awf/workspace.yml` touches protected validation
  policy. Only do this after stability is demonstrated; do not lower or remove
  any existing validation command or coverage requirement.

## Explicit Non-Goals

- Do not implement mutation testing or semantic proof of assertion quality.
- Do not ban all `monkeypatch`; valid dependency fakes remain allowed.
- Do not add a pytest plugin or collection hook in this slice.
- Do not change public API responses, database models, migrations, console UI,
  package dependencies, lockfiles, coverage thresholds, or workspace coverage
  requirements.
- Do not scan non-Python tests or frontend tests in this slice.
- Do not switch branches, push, rebase, merge PRs, or manually intervene in AWF
  PR automation.
