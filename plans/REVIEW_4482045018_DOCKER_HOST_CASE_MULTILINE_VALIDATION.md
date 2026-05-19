# Review 4482045018 Docker Host Case And Multiline Validation

Plan reference:
`plans/REVIEW_4482045018_DOCKER_HOST_CASE_MULTILINE_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Add a regression proving `awf service logs` removes mixed-case `AWF_DOCKER_HOST` variants from the subprocess environment after Compose interpolation. | Complete | Added `tests/unit/service/test_logs.py::test_service_logs_removes_mixed_case_awf_docker_host_after_compose_interpolation`; it failed before implementation because a lower-case shell key remained in the subprocess env. |
| Align service logs cleanup with bootstrap by deleting every environment key whose uppercase form is `AWF_DOCKER_HOST`. | Complete | `src/awf/service/logs.py` now loops over resolved env keys and deletes every case variant matching `AWF_DOCKER_HOST`. |
| Preserve mirroring of the resolved Docker host into `DOCKER_HOST`. | Complete | New regression asserts `DOCKER_HOST` is the resolved service env value; full service logs tests passed. |
| Add a prominent comment near `_merge_env_seed_contents()` `splitlines()` calls documenting unsupported multi-line dotenv values. | Complete | `src/awf/cli/main.py` documents that the merge is line-oriented and requires seed/overlay env entries to stay single-line unless replaced with a dotenv parser. |
| Run focused service logs/init tests and lint for touched files. | Complete | See commands below. |
| Write validation evidence and commit the scoped changes locally. | Complete | This validation file records evidence; local commit follows final diff review. |

## Commands Run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_removes_mixed_case_awf_docker_host_after_compose_interpolation -q
```

Result before implementation: failed as expected because
`awf_docker_host=unix:///stale-awf-docker.sock` remained in the subprocess
environment.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_removes_mixed_case_awf_docker_host_after_compose_interpolation -q
```

Result after implementation: passed, `1 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py -q
```

Result: passed, `28 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_init_without_path_prefers_compose_env_example_over_root tests/unit/cli/test_init.py::test_init_without_path_preserves_context_before_seed_overlay_key -q
```

Result: passed, `2 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py src/awf/cli/main.py tests/unit/service/test_logs.py
```

Result: passed.

Note: an initial init-test command used a stale node id,
`test_init_without_path_merges_root_env_into_compose_env_example`, and failed
with `no tests ran`; the plan was corrected to the existing compose-env merge
test before final validation.

## Remaining Gaps

None.
