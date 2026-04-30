import assert from "node:assert/strict";
import test from "node:test";

import {
  extractHostHomeAuthMountPolicy,
  extractProfileEgress,
  extractProfileSecurity,
  extractProfileSecrets,
  formatHostHomeMountPolicy,
  summarizeEgressStatus,
  summarizeProviderCredentialReadiness,
  summarizeSecretLeaseReadiness,
} from "./security-format.ts";

test("extractHostHomeAuthMountPolicy returns unavailable when policy is missing", () => {
  assert.deepEqual(extractHostHomeAuthMountPolicy(null), { mode: "unavailable" });
  assert.deepEqual(extractHostHomeAuthMountPolicy(undefined), { mode: "unavailable" });
  assert.deepEqual(extractHostHomeAuthMountPolicy({}), { mode: "unavailable" });
  assert.deepEqual(extractHostHomeAuthMountPolicy({ security: null }), { mode: "unavailable" });
  assert.deepEqual(extractHostHomeAuthMountPolicy({ security: {} }), { mode: "unavailable" });
  assert.deepEqual(
    extractHostHomeAuthMountPolicy({ security: { host_home_auth_mounts: null } }),
    { mode: "unavailable" },
  );
  assert.deepEqual(
    extractHostHomeAuthMountPolicy({ security: { host_home_auth_mounts: "bad" } }),
    { mode: "unavailable" },
  );
});

test("extractHostHomeAuthMountPolicy returns block when policy explicitly set to block", () => {
  assert.deepEqual(
    extractHostHomeAuthMountPolicy({ security: { host_home_auth_mounts: { mode: "block" } } }),
    { mode: "block" },
  );
});

test("extractHostHomeAuthMountPolicy returns warn when policy explicitly set to warn", () => {
  assert.deepEqual(
    extractHostHomeAuthMountPolicy({ security: { host_home_auth_mounts: { mode: "warn" } } }),
    { mode: "warn" },
  );
});

test("extractHostHomeAuthMountPolicy falls back to block for unrecognized mode", () => {
  assert.deepEqual(
    extractHostHomeAuthMountPolicy({ security: { host_home_auth_mounts: { mode: "permissive" } } }),
    { mode: "block" },
  );
});

test("formatHostHomeMountPolicy renders block as good", () => {
  assert.deepEqual(formatHostHomeMountPolicy({ mode: "block" }), {
    label: "blocked",
    tone: "good",
  });
});

test("formatHostHomeMountPolicy renders warn as warn tone", () => {
  assert.deepEqual(formatHostHomeMountPolicy({ mode: "warn" }), {
    label: "allowed (warn)",
    tone: "warn",
  });
});

test("formatHostHomeMountPolicy renders unavailable as neutral", () => {
  assert.deepEqual(formatHostHomeMountPolicy({ mode: "unavailable" }), {
    label: "unavailable",
    tone: "neutral",
  });
});

test("extractProfileEgress returns unavailable when egress is missing", () => {
  assert.deepEqual(extractProfileEgress(null), { mode: "unavailable", allowlist: [] });
  assert.deepEqual(extractProfileEgress(undefined), { mode: "unavailable", allowlist: [] });
  assert.deepEqual(extractProfileEgress({}), { mode: "unavailable", allowlist: [] });
});

test("extractProfileSecurity composes egress and host_home_auth_mounts", () => {
  const profile = {
    security: {
      egress: { mode: "open" },
      host_home_auth_mounts: { mode: "block" },
    },
  };
  const result = extractProfileSecurity(profile);
  assert.deepEqual(result.egress, { mode: "open", allowlist: [] });
  assert.deepEqual(result.host_home_auth_mounts, { mode: "block" });
});

test("extractProfileSecurity returns unavailable host_home_auth_mounts when missing", () => {
  const result = extractProfileSecurity({});
  assert.equal(result.host_home_auth_mounts.mode, "unavailable");
  assert.equal(result.egress.mode, "unavailable");
});