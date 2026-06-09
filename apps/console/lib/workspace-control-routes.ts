import { randomUUID } from "node:crypto";

import { proxyAwf } from "./awf-server.ts";
import type { ValidationTier, WorkspaceOperatorAction } from "./types.ts";

type WorkspaceControlRoutePayload = {
  reason?: string;
  workspace_version?: number;
  requested_tier?: number;
  idempotency_key?: string;
};

type AwfControlAction = WorkspaceOperatorAction;

const actionPaths: Record<AwfControlAction, string> = {
  remonitor: "remonitor",
  refresh: "refresh",
  revalidate: "validate",
  cancel: "cancel",
};

const invalidRequestStatus = 400;
const maxReasonLength = 1024;
const maxIdempotencyKeyLength = 128;

export async function handleWorkspaceControlRoute(
  action: AwfControlAction,
  workspaceId: string,
  request: Request,
): Promise<Response> {
  const parsed = await parsePayload(request);
  if (!parsed.ok) {
    return invalidRequest(parsed.message);
  }

  const payload = parsed.value;
  const tier = payload.requested_tier;
  if (action === "revalidate" && tier !== undefined && !isValidationTier(tier)) {
    return invalidRequest("requested_tier must be 1, 2, or 3.");
  }
  if (action !== "revalidate" && tier !== undefined) {
    return invalidRequest("requested_tier is only supported for revalidate.");
  }
  if (payload.workspace_version !== undefined && !isNonNegativeInteger(payload.workspace_version)) {
    return invalidRequest("workspace_version must be a non-negative integer.");
  }

  const idempotencyKeyResult = idempotencyKey(payload.idempotency_key, request.headers);
  if (!idempotencyKeyResult.ok) {
    return invalidRequest(idempotencyKeyResult.message);
  }

  const body = JSON.stringify(awfBody(action, payload));
  return proxyAwf(`/v1/workspaces/${encodeURIComponent(workspaceId)}/${actionPaths[action]}`, {
    method: "POST",
    body,
    contentType: "application/json",
    headers: {
      "Idempotency-Key": idempotencyKeyResult.value,
      "If-Match": ifMatchHeader(payload.workspace_version, request.headers),
    },
  });
}

async function parsePayload(
  request: Request,
): Promise<{ ok: true; value: WorkspaceControlRoutePayload } | { ok: false; message: string }> {
  const text = await request.text();
  if (!text.trim()) {
    return { ok: true, value: {} };
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(text) as unknown;
  } catch {
    return { ok: false, message: "Request body must be valid JSON." };
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { ok: false, message: "Request body must be a JSON object." };
  }

  const record = parsed as Record<string, unknown>;
  const payload: WorkspaceControlRoutePayload = {};
  if (record.reason !== undefined) {
    if (typeof record.reason !== "string") {
      return { ok: false, message: "reason must be a string." };
    }
    const reason = record.reason.trim();
    if (reason.length > maxReasonLength) {
      return { ok: false, message: "reason must be 1024 characters or fewer." };
    }
    if (reason) {
      payload.reason = reason;
    }
  }
  if (record.workspace_version !== undefined) {
    if (typeof record.workspace_version !== "number") {
      return { ok: false, message: "workspace_version must be a number." };
    }
    payload.workspace_version = record.workspace_version;
  }
  if (record.requested_tier !== undefined) {
    if (typeof record.requested_tier !== "number") {
      return { ok: false, message: "requested_tier must be a number." };
    }
    payload.requested_tier = record.requested_tier;
  }
  if (record.idempotency_key !== undefined) {
    if (typeof record.idempotency_key !== "string") {
      return { ok: false, message: "idempotency_key must be a string." };
    }
    payload.idempotency_key = record.idempotency_key;
  }
  return { ok: true, value: payload };
}

function awfBody(action: AwfControlAction, payload: WorkspaceControlRoutePayload): Record<string, string | ValidationTier | boolean> {
  const body: Record<string, string | ValidationTier | boolean> = {};
  if (payload.reason) {
    body.reason = payload.reason;
  }
  if (action === "revalidate" && isValidationTier(payload.requested_tier)) {
    body.requested_tier = payload.requested_tier;
  }
  if (action === "cancel") {
    body.stop_stack = true;
  }
  return body;
}

function ifMatchHeader(workspaceVersion: number | undefined, headers: Headers): string | undefined {
  if (workspaceVersion !== undefined) {
    return String(workspaceVersion);
  }
  return cleanHeader(headers.get("if-match"));
}

function idempotencyKey(
  rawKey: string | undefined,
  headers: Headers,
): { ok: true; value: string } | { ok: false; message: string } {
  const candidate = cleanHeader(rawKey) ?? cleanHeader(headers.get("idempotency-key"));
  if (!candidate) {
    return { ok: true, value: `console:${randomUUID()}` };
  }
  if (candidate.length > maxIdempotencyKeyLength) {
    return { ok: false, message: "idempotency_key must be 128 characters or fewer." };
  }
  return { ok: true, value: candidate };
}

function cleanHeader(value: string | null | undefined): string | undefined {
  const cleaned = value?.trim();
  return cleaned ? cleaned : undefined;
}

function isValidationTier(value: number | undefined): value is ValidationTier {
  return value === 1 || value === 2 || value === 3;
}

function isNonNegativeInteger(value: number): boolean {
  return Number.isInteger(value) && value >= 0;
}

function invalidRequest(message: string): Response {
  return Response.json(
    {
      ok: false,
      error_code: "INVALID_REQUEST",
      message,
    },
    { status: invalidRequestStatus },
  );
}
