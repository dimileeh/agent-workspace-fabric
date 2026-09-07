import assert from "node:assert/strict";
import test from "node:test";

import { parseCloudRuntimeSummary } from "./console-cloud-runtime.ts";

const validRuntime = {
  schema_version: 1,
  generated_at: "2026-09-06T17:00:00Z",
  as_of: "2026-09-06T17:00:00Z",
  queue: {
    queued_count: 4,
    oldest_wait_seconds: 120,
    oldest_workspace_id: "ws_oldest",
  },
  provisioning: {
    in_progress: 2,
    pending: 1,
  },
  admission: {
    ok: true,
    status: "ok",
    reason: "within_quota",
    detail: null,
    quota: {
      limit: 50,
      in_use: 12,
      available: 38,
    },
  },
};

test("parseCloudRuntimeSummary accepts hosted fixture shape", () => {
  const parsed = parseCloudRuntimeSummary(validRuntime);
  assert.ok(parsed);
  assert.equal(parsed.queue.queued_count, 4);
  assert.equal(parsed.admission.status, "ok");
  assert.equal(parsed.provisioning.in_progress, 2);
});

test("parseCloudRuntimeSummary rejects non-objects", () => {
  assert.equal(parseCloudRuntimeSummary(null), null);
  assert.equal(parseCloudRuntimeSummary(undefined), null);
  assert.equal(parseCloudRuntimeSummary([]), null);
  assert.equal(parseCloudRuntimeSummary("x"), null);
});

test("parseCloudRuntimeSummary rejects missing nested objects required by the panel", () => {
  assert.equal(parseCloudRuntimeSummary({ ...validRuntime, queue: null }), null);
  assert.equal(parseCloudRuntimeSummary({ ...validRuntime, admission: undefined }), null);
  assert.equal(
    parseCloudRuntimeSummary({
      schema_version: 1,
      generated_at: "2026-09-06T17:00:00Z",
      queue: validRuntime.queue,
      provisioning: validRuntime.provisioning,
    }),
    null,
  );
  assert.equal(parseCloudRuntimeSummary({ ...validRuntime, provisioning: [] }), null);
});

test("parseCloudRuntimeSummary rejects missing generated_at", () => {
  const { generated_at: _drop, ...rest } = validRuntime;
  assert.equal(parseCloudRuntimeSummary(rest), null);
});

test("parseCloudRuntimeSummary rejects empty or unparseable generated_at", () => {
  // Nonempty but invalid values must fail closed: relativeTime would otherwise
  // feed NaN into Intl.RelativeTimeFormat.format and throw RangeError.
  for (const generated_at of [
    "",
    "not-a-date",
    "   ",
    "Invalid Date",
    // Date.parse accepts these, but they are not OpenAPI date-time / RFC 3339.
    "09/07/2026",
    "2026-09-07",
    "2026-09-07 08:43:57Z",
    "2026-09-07T08:43:57",
    // Date.parse normalizes impossible calendar values; reject them explicitly.
    "2026-02-29T12:00:00Z",
    "2026-04-31T12:00:00Z",
    "2026-13-01T12:00:00Z",
    "2026-01-01T25:00:00Z",
    "2026-09-07T08:43:57+99:00",
  ]) {
    assert.equal(parseCloudRuntimeSummary({ ...validRuntime, generated_at }), null);
  }
});

test("parseCloudRuntimeSummary accepts leap-day RFC 3339 generated_at", () => {
  assert.ok(parseCloudRuntimeSummary({ ...validRuntime, generated_at: "2024-02-29T12:00:00Z" }));
});

test("parseCloudRuntimeSummary rejects unknown schema_version when present", () => {
  assert.equal(parseCloudRuntimeSummary({ ...validRuntime, schema_version: 99 }), null);
});

test("parseCloudRuntimeSummary rejects empty nested objects and invalid field types", () => {
  assert.equal(parseCloudRuntimeSummary({ ...validRuntime, queue: {} }), null);
  assert.equal(parseCloudRuntimeSummary({ ...validRuntime, provisioning: {} }), null);
  assert.equal(parseCloudRuntimeSummary({ ...validRuntime, admission: {} }), null);
  assert.equal(
    parseCloudRuntimeSummary({
      ...validRuntime,
      admission: { ...validRuntime.admission, status: { nested: true } },
    }),
    null,
  );
  assert.equal(
    parseCloudRuntimeSummary({
      ...validRuntime,
      admission: { ...validRuntime.admission, detail: { nested: true } },
    }),
    null,
  );
  assert.equal(
    parseCloudRuntimeSummary({
      ...validRuntime,
      queue: { ...validRuntime.queue, queued_count: "4" },
    }),
    null,
  );
  assert.equal(
    parseCloudRuntimeSummary({
      ...validRuntime,
      admission: {
        ...validRuntime.admission,
        quota: { limit: "50", in_use: 12, available: 38 },
      },
    }),
    null,
  );
});

test("parseCloudRuntimeSummary accepts null counts and omitted optional fields", () => {
  const parsed = parseCloudRuntimeSummary({
    schema_version: 1,
    generated_at: "2026-09-06T17:00:00Z",
    queue: {
      queued_count: null,
      oldest_wait_seconds: null,
    },
    provisioning: {
      in_progress: null,
      pending: null,
    },
    admission: {
      ok: false,
      status: "denied",
      reason: "quota_exceeded",
      detail: null,
      quota: null,
    },
  });
  assert.ok(parsed);
  assert.equal(parsed.queue.queued_count, null);
  assert.equal(parsed.admission.status, "denied");
  assert.equal(parsed.admission.quota, null);
});

test("parseCloudRuntimeSummary rejects negative or fractional numeric evidence", () => {
  assert.equal(
    parseCloudRuntimeSummary({
      ...validRuntime,
      queue: { ...validRuntime.queue, queued_count: -1 },
    }),
    null,
  );
  assert.equal(
    parseCloudRuntimeSummary({
      ...validRuntime,
      queue: { ...validRuntime.queue, oldest_wait_seconds: -0.5 },
    }),
    null,
  );
  assert.equal(
    parseCloudRuntimeSummary({
      ...validRuntime,
      provisioning: { ...validRuntime.provisioning, in_progress: 1.5 },
    }),
    null,
  );
  assert.equal(
    parseCloudRuntimeSummary({
      ...validRuntime,
      admission: {
        ...validRuntime.admission,
        quota: { limit: -1, in_use: 0, available: 0 },
      },
    }),
    null,
  );
});
