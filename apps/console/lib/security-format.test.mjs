import assert from "node:assert/strict";
import test from "node:test";

import {
  extractHostHomeAuthMountPolicy,
  extractProfileEgress,
  extractProfileSecurity,
  formatHostHomeMountPolicy,
  summarizeEgressStatus,
  summarizeProviderCredentialReadiness,
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

test("extractHostHomeAuthMountPolicy falls back to unavailable for unrecognized mode", () => {
  assert.deepEqual(
    extractHostHomeAuthMountPolicy({ security: { host_home_auth_mounts: { mode: "permissive" } } }),
    { mode: "unavailable" },
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
  assert.deepEqual(extractProfileEgress(null), { mode: "unavailable" });
  assert.deepEqual(extractProfileEgress(undefined), { mode: "unavailable" });
  assert.deepEqual(extractProfileEgress({}), { mode: "unavailable" });
});

test("summarizeEgressStatus treats open as unrestricted and restricted as default", () => {
  assert.deepEqual(summarizeEgressStatus({ mode: "open" }), {
    label: "open",
    tone: "warn",
    detail: "unrestricted internet",
  });
  assert.deepEqual(summarizeEgressStatus({ mode: "restricted" }), {
    label: "restricted",
    tone: "good",
    detail: "default local-only",
  });
});

test("extractProfileSecurity composes egress and host_home_auth_mounts", () => {
  const profile = {
    security: {
      egress: { mode: "open" },
      host_home_auth_mounts: { mode: "block" },
    },
  };
  const result = extractProfileSecurity(profile);
  assert.deepEqual(result.egress, { mode: "open" });
  assert.deepEqual(result.host_home_auth_mounts, { mode: "block" });
});

test("extractProfileSecurity returns unavailable host_home_auth_mounts when missing", () => {
  const result = extractProfileSecurity({});
  assert.equal(result.host_home_auth_mounts.mode, "unavailable");
  assert.equal(result.egress.mode, "unavailable");
});

test("summarizeProviderCredentialReadiness deduplicates missing providers", () => {
  const result = summarizeProviderCredentialReadiness(
    [
      {
        name: "github-token",
        target: "GH_TOKEN",
        kind: "env",
        mode: "ro",
        required: true,
        provider: "github",
      },
      {
        name: "github-cli-config",
        target: "/home/agent/.config/gh",
        kind: "mount",
        mode: "ro",
        required: true,
        provider: "github",
      },
    ],
    [],
  );

  assert.equal(result.declared, 2);
  assert.equal(result.leased, 0);
  assert.deepEqual(result.missingProviders, ["github"]);
  assert.equal(result.label, "0/2 — missing providers");
  assert.equal(result.tone, "warn");
});
