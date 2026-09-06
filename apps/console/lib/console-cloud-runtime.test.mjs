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

test("parseCloudRuntimeSummary rejects unknown schema_version when present", () => {
  assert.equal(parseCloudRuntimeSummary({ ...validRuntime, schema_version: 99 }), null);
});
