# Docker Capacity Console Labeling Plan

## Summary

Make the resource capacity panel clearly communicate that CPU and memory limits are the local Docker runtime capacity AWF can schedule against, not necessarily the host machine's physical capacity.

## Scope

- Add local capacity source metadata to the resource saturation payload.
- Show the source in the console resource capacity panel.
- Rename capacity labels to make Docker/runtime capacity explicit.
- Keep scheduling behavior unchanged.

## Validation

- Unit tests for the API payload source metadata.
- Console tests for the updated heading/copy.
- Targeted lint/type/build checks for touched frontend and backend files.
