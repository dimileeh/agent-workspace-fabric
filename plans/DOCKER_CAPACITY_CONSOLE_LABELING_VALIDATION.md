# Docker Capacity Console Labeling Validation

## Result

Implemented the resource capacity labeling polish without changing scheduling behavior.

## Changes

- Added `local_capacity` source metadata to the resource saturation API payload.
- Updated the console resource panel title to `Resource / Runtime Capacity`.
- Added a scheduler capacity source callout with the detected/runtime CPU and memory limits.
- Renamed CPU and memory meters to `Runtime CPU peak` and `Runtime memory peak`.
- Display memory capacity in GiB because AWF converts Docker byte totals using binary units.

## Validation

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py::test_resource_saturation_endpoint_uses_detected_docker_capacity_when_unset tests/unit/api/test_metrics_capacity.py::test_resource_saturation_endpoint_reports_allocated_capacity_and_queue_pressure tests/unit/service/test_metrics_parts/test_metrics_part_003.py::test_resource_saturation_reports_reserved_disk_dind_and_available_capacity -q` passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/metrics_types.py src/awf/service/metrics_resources.py src/awf/api/routes/metrics.py tests/unit/api/test_metrics_capacity.py tests/unit/service/test_metrics_parts/test_metrics_part_003.py` passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/metrics_types.py src/awf/service/metrics_resources.py src/awf/api/routes/metrics.py` passed.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check` passed.
- `npm --prefix apps/console run lint` passed.
- `npm --prefix apps/console run typecheck` passed.
- `npm --prefix apps/console run build` passed.
- In-app browser validation against `http://localhost:3000/` passed: the panel renders the runtime-capacity heading, source callout, host-hardware note, and runtime CPU/memory labels with no console warnings or errors.

## Notes

- The running AWF API container has not been restarted, so the browser validated the console fallback copy for an older API payload. After AWF is rebuilt/restarted, the same panel can display the explicit `Docker runtime` source from the new API field.
- The Playwright browser spec could not be run directly because Next.js refused to start a second dev server while the existing `localhost:3000` console was active.
