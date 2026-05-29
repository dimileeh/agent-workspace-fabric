# PRRT_kwDOSJAM6s6FhfXJ Docker Source Markers Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6FhfXJ_DOCKER_SOURCE_MARKERS_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Missing Docker build/package bootstrap inputs copied by `docker/control-plane.Dockerfile` fail source validation with `SOURCE_CHECKOUT_INVALID`. | Complete | Added `test_source_checkout_requires_control_plane_docker_build_inputs`; it failed before implementation with five `DID NOT RAISE` failures and passes after updating markers. |
| Missing Docker build/package bootstrap inputs are reported through `SourceCheckoutError.missing_markers` using root-relative marker paths. | Complete | The regression asserts exact `missing_markers` and serialized `to_dict()["missing_markers"]` for `uv.lock`, `alembic.ini`, `.env.example`, `openapi.json`, and `migrations`. |
| Existing valid checkout metadata records the expanded marker contract so stale metadata detection can detect the new contract. | Complete | `SOURCE_CHECKOUT_MARKERS` now includes the new inputs, so `SOURCE_CHECKOUT_REQUIRED_MARKER_PATHS`, `VerifiedSourceCheckout.markers`, and persisted metadata all use the expanded tuple. Existing metadata tests passed. |
| Validation remains focused; full AWF/GitHub validation is left to the AWF post-agent pipeline. | Complete | Ran only targeted host setup tests and focused ruff checks. Full AWF/GitHub validation was not run in the agent phase. |

## Files Changed

- `src/awf/host_setup/source_assets.py`
- `tests/unit/service/test_host_setup_config.py`
- `plans/PRRT_kwDOSJAM6s6FhfXJ_DOCKER_SOURCE_MARKERS_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6FhfXJ_DOCKER_SOURCE_MARKERS_VALIDATION.md`

## Verification Evidence

- Initial failing regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k control_plane_docker_build_inputs`
  failed with five missing-error failures before implementation.
- Focused regression after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k control_plane_docker_build_inputs`
  passed: `5 passed, 31 deselected`.
- Containing unit file:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q`
  passed: `36 passed`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/source_assets.py tests/unit/service/test_host_setup_config.py`
  passed.

## Remaining Gaps

None for this review thread. Full AWF/GitHub validation remains managed by AWF
after agent completion.
