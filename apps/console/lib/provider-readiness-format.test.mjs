import assert from "node:assert/strict";
import test from "node:test";

import {
  formatProviderReadinessRetryError,
  providerReadinessPreflightFacts,
  providerReadinessPreflightFromError,
  providerReadinessPreflightTone,
} from "./provider-readiness-format.ts";

const blockedPreflight = {
  provider: "claude_code",
  agent: "claude_code",
  model: "claude-opus-4-7",
  model_source: "default",
  readiness_status: "blocked",
  auth_status: "fail",
  auth_source: "not_observed",
  credential_scope: "not_observed",
  isolation: "none",
  probe_status: "skipped",
  reason_code: "CLAUDE_AUTH_MISSING",
  message: "No Claude Code auth signal was visible.",
  override_required: true,
  override_requested: false,
  override_used: false,
  override_reason: null,
  blocks_launch: true,
  checked_at: "2026-05-03T12:00:00+00:00",
  credential_sources: [],
};

test("providerReadinessPreflightFacts maps persisted preflight into dashboard chips", () => {
  const overridePreflight = {
    ...blockedPreflight,
    readiness_status: "admitted_with_override",
    override_requested: true,
    override_used: true,
    override_reason: "operator checked auth",
    blocks_launch: false,
  };

  assert.equal(providerReadinessPreflightTone(blockedPreflight), "bad");
  assert.equal(providerReadinessPreflightTone(overridePreflight), "warn");

  const facts = providerReadinessPreflightFacts(overridePreflight, {
    formatCheckedAt: () => "2m ago",
  });

  assert.deepEqual(
    facts.map(({ label, value, detail, mono, tone }) => ({ label, value, detail, mono, tone })),
    [
      {
        label: "Provider",
        value: "claude_code",
        detail: "claude_code",
        mono: true,
        tone: undefined,
      },
      {
        label: "Model",
        value: "claude-opus-4-7",
        detail: undefined,
        mono: true,
        tone: undefined,
      },
      {
        label: "Readiness",
        value: "admitted_with_override",
        detail: undefined,
        mono: undefined,
        tone: "warn",
      },
      {
        label: "Auth",
        value: "fail",
        detail: "not_observed / not_observed",
        mono: true,
        tone: undefined,
      },
      {
        label: "Probe",
        value: "skipped",
        detail: undefined,
        mono: true,
        tone: undefined,
      },
      {
        label: "Override",
        value: "used",
        detail: undefined,
        mono: undefined,
        tone: undefined,
      },
      {
        label: "Checked",
        value: "2m ago",
        detail: undefined,
        mono: undefined,
        tone: undefined,
      },
      {
        label: "Isolation",
        value: "none",
        detail: undefined,
        mono: true,
        tone: undefined,
      },
    ],
  );
});

test("formatProviderReadinessRetryError includes blocked provider model and reason", () => {
  const detail = {
    detail: {
      provider_readiness_preflight: blockedPreflight,
    },
  };

  assert.equal(providerReadinessPreflightFromError(detail), blockedPreflight);
  assert.equal(
    formatProviderReadinessRetryError({
      ok: false,
      status: 409,
      errorCode: "PROVIDER_READINESS_PRECHECK_FAILED",
      message: "Selected provider readiness blocked workspace launch.",
      detail,
    }),
    "PROVIDER_READINESS_PRECHECK_FAILED: Selected provider readiness blocked workspace launch. claude_code/claude-opus-4-7 readiness blocked; auth fail via not_observed; probe skipped; reason CLAUDE_AUTH_MISSING.",
  );
});

test("providerReadinessPreflightFromError rejects partial preflight payloads", () => {
  assert.equal(
    providerReadinessPreflightFromError({
      provider_readiness_preflight: {
        provider: "claude_code",
        readiness_status: "blocked",
        auth_status: "fail",
        auth_source: "not_observed",
        probe_status: "skipped",
        reason_code: "CLAUDE_AUTH_MISSING",
      },
    }),
    null,
  );
});
