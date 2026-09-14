/**
 * OpenAPI `format: date-time` / RFC 3339 profile: full date-time with `T`/`t`
 * and a timezone (`Z`/`z` or ±HH:mm). Rejects Date.parse-permissive forms like
 * `09/07/2026` or date-only `2026-09-07`.
 * Capturing groups let callers reject impossible calendar values that Date.parse
 * would normalize (e.g. 2026-02-29 → March 1).
 */
export const RFC3339_DATE_TIME =
  /^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(\.\d+)?([Zz]|[+-](\d{2}):(\d{2}))$/;
