# Review 4482045018 Docker Host Case And Multiline Plan

## Problem Statement and Scope

Address the remaining actionable items from PR review-level comment
`issue:4482045018`:

- `awf service logs` removes only the exact uppercase `AWF_DOCKER_HOST` key from
  the Docker Compose subprocess environment, unlike bootstrap's case-insensitive
  cleanup.
- `_merge_env_seed_contents()` is intentionally line-oriented but does not make
  its unsupported multi-line dotenv value limitation clear near `splitlines()`.

Scope is limited to service logs subprocess environment cleanup, the CLI env
seed merge comment, focused tests, and local validation evidence.

## Requirements Checklist

- [ ] Add a regression proving `awf service logs` removes mixed-case
  `AWF_DOCKER_HOST` variants from the subprocess environment after Compose
  interpolation.
- [ ] Align `src/awf/service/logs.py` cleanup with bootstrap by deleting every
  environment key whose uppercase form is `AWF_DOCKER_HOST`.
- [ ] Preserve mirroring of the resolved Docker host into `DOCKER_HOST`.
- [ ] Add a prominent comment near `_merge_env_seed_contents()` `splitlines()`
  calls documenting that multi-line dotenv values are unsupported by the
  line-oriented merge.
- [ ] Run focused service logs/init tests and lint for the touched files.
- [ ] Write validation evidence and commit the scoped changes locally.

## Implementation Steps

1. Add a failing service logs unit test for mixed-case `AWF_DOCKER_HOST`
   interpolation cleanup.
2. Update `_docker_cli_environ()` in `src/awf/service/logs.py` to remove all
   case variants of `AWF_DOCKER_HOST`.
3. Add the multi-line dotenv limitation comment near the `splitlines()` calls in
   `_merge_env_seed_contents()`.
4. Run the targeted regression, the service logs test module, the init env
   merge tests, and ruff for touched files.
5. Create the validation document with requirement-by-requirement evidence.
6. Stage only changed files and commit with the required review-comment message.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_removes_mixed_case_awf_docker_host_after_compose_interpolation -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py -q
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_init_without_path_prefers_compose_env_example_over_root tests/unit/cli/test_init.py::test_init_without_path_preserves_context_before_seed_overlay_key -q
uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py src/awf/cli/main.py tests/unit/service/test_logs.py
```

Pass criteria: the new regression fails before the implementation change, then
all listed commands pass after implementation.
