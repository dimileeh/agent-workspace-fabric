# LLM Usage Console Accuracy Plan

## Summary

Fix two operator-facing LLM usage display issues observed in a live PR monitor:

- Do not show "pricing not configured" when ccusage already supplied a concrete
  cost estimate.
- Expose cached input and reasoning output token buckets so ccusage totals are
  understandable when `totalTokens` is greater than input plus output.

## Scope

- Add normalized `cached_input_tokens` and `reasoning_output_tokens` fields to
  ccusage parsing, usage snapshots, workspace observability, API schemas, and
  console types.
- Render the extra token buckets in the console when present.
- Treat an existing `cost_estimate` as already priced for display purposes.

## Validation

- Focused Python tests for ccusage normalization, baseline subtraction, and usage
  payload propagation.
- Focused console tests for the LLM usage block.
- Targeted lint/type checks for touched files.
