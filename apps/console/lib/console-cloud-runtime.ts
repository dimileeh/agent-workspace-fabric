import type { CloudRuntimeSummary } from "./types.ts";

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value != null && typeof value === "object" && !Array.isArray(value);
}

/**
 * Fail closed on hosted cloud-runtime payloads that lack the nested objects
 * CloudRuntimePanel reads. Callers keep the last good snapshot and surface an error.
 */
export function parseCloudRuntimeSummary(payload: unknown): CloudRuntimeSummary | null {
  if (!isPlainObject(payload)) {
    return null;
  }
  if (payload.schema_version !== 1) {
    return null;
  }
  if (typeof payload.generated_at !== "string" || payload.generated_at.length === 0) {
    return null;
  }
  if (
    !isPlainObject(payload.queue) ||
    !isPlainObject(payload.provisioning) ||
    !isPlainObject(payload.admission)
  ) {
    return null;
  }
  return payload as CloudRuntimeSummary;
}
