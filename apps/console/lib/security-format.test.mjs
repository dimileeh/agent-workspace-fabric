import assert from "node:assert/strict";
import test from "node:test";

import {
  extractProfileSecrets,
  extractProfileEgress,
  extractProfileSecurity,
  extractHostHomeAuthMountPolicy,
  summarizeSecretLeaseReadiness,
  summarizeEgressStatus,
  formatHostHomeMountPolicy,
  summarizeProviderCredentialReadiness,
} from "./security-format.ts";

test("extractProfileSecrets returns [] for null profile", () => {
  assert.deepEqual(extractProfileSecrets(null), []);
});

test("extractProfileSecrets returns [] for undefined profile", () => {
  assert.deepEqual(extractProfileSecrets(undefined), []);
});

test("extractProfileSecrets returns [] for profile without secrets", () => {
  assert.deepEqual(extractProfileSecrets({}), []);
});

test("extractProfileSecrets returns [] for non-array secrets", () => {
  assert.deepEqual(extractProfileSecrets({ secrets: "not-an-array" }), []);
});

test("extractProfileSecrets extracts valid secrets", () => {
  const profile = {
    secrets: [
      { name: "db-password", target: "/run/secrets/db-password", kind: "mount", mode: "ro", required: true, provider: "vault" },
      { name: "api-key", target: "API_KEY", kind: "env", mode: "rw", required: false, provider: null },
    ],
  };
  const result = extractProfileSecrets(profile);
  assert.equal(result.length, 2);
  assert.equal(result[0].name, "db-password");
  assert.equal(result[0].kind, "mount");
  assert.equal(result[0].provider, "vault");
  assert.equal(result[1].name, "api-key");
  assert.equal(result[1].kind, "env");
  assert.equal(result[1].provider, null);
});

test("extractProfileSecrets never includes ref field", () => {
  const profile = {
    secrets: [
      { name: "secret-a", target: "/a", kind: "mount", mode: "ro", required: true, provider: null, ref: "sensitive-value" },
    ],
  };
  const result = extractProfileSecrets(profile);
  assert.equal(result.length, 1);
  assert.equal(result[0].name, "secret-a");
  assert.ok(!("ref" in result[0]), "ref must not be present in output");
});

test("extractProfileSecrets uses defaults for malformed entries", () => {
  const profile = {
    secrets: [
      { name: 123, target: "/a", kind: "invalid", mode: "invalid", required: "yes" },
    ],
  };
  const result = extractProfileSecrets(profile);
  assert.equal(result.length, 1);
  assert.equal(result[0].name, "");
  assert.equal(result[0].kind, "mount");
  assert.equal(result[0].mode, "ro");
  assert.equal(result[0].required, false);
});

test("extractProfileSecrets skips null entries", () => {
  const profile = {
    secrets: [null, { name: "a", target: "/a", kind: "mount", mode: "ro", required: true }, undefined],
  };
  const result = extractProfileSecrets(profile);
  assert.equal(result.length, 1);
  assert.equal(result[0].name, "a");
});

test("extractProfileEgress returns safe default for null profile", () => {
  const result = extractProfileEgress(null);
  assert.deepEqual(result, { mode: "open", allowlist: [] });
});

test("extractProfileEgress returns safe default for undefined profile", () => {
  assert.deepEqual(extractProfileEgress(undefined), { mode: "open", allowlist: [] });
});

test("extractProfileEgress returns safe default for profile without security", () => {
  assert.deepEqual(extractProfileEgress({}), { mode: "open", allowlist: [] });
});

test("extractProfileEgress returns safe default for profile without egress", () => {
  assert.deepEqual(extractProfileEgress({ security: {} }), { mode: "open", allowlist: [] });
});

test("extractProfileEgress returns valid egress", () => {
  const profile = {
    security: {
      egress: { mode: "allowlist", allowlist: ["github.com", "pypi.org"] },
    },
  };
  const result = extractProfileEgress(profile);
  assert.equal(result.mode, "allowlist");
  assert.deepEqual(result.allowlist, ["github.com", "pypi.org"]);
});

test("extractProfileEgress defaults allowlist to [] when missing", () => {
  const profile = {
    security: {
      egress: { mode: "offline" },
    },
  };
  const result = extractProfileEgress(profile);
  assert.equal(result.mode, "offline");
  assert.deepEqual(result.allowlist, []);
});

test("extractProfileEgress falls back to open for unrecognized mode", () => {
  const profile = {
    security: {
      egress: { mode: "unknown-mode", allowlist: [] },
    },
  };
  const result = extractProfileEgress(profile);
  assert.equal(result.mode, "open");
  assert.deepEqual(result.allowlist, []);
});

test("extractProfileEgress handles non-string mode", () => {
  const profile = {
    security: {
      egress: { mode: 42, allowlist: [] },
    },
  };
  const result = extractProfileEgress(profile);
  assert.equal(result.mode, "open");
});

test("extractHostHomeAuthMountPolicy returns block for null profile", () => {
  assert.deepEqual(extractHostHomeAuthMountPolicy(null), { mode: "block" });
});

test("extractHostHomeAuthMountPolicy returns block for undefined profile", () => {
  assert.deepEqual(extractHostHomeAuthMountPolicy(undefined), { mode: "block" });
});

test("extractHostHomeAuthMountPolicy returns block for profile without security", () => {
  assert.deepEqual(extractHostHomeAuthMountPolicy({}), { mode: "block" });
});

test("extractHostHomeAuthMountPolicy returns block for profile without host_home_auth_mounts", () => {
  assert.deepEqual(extractHostHomeAuthMountPolicy({ security: {} }), { mode: "block" });
});

test("extractHostHomeAuthMountPolicy returns valid policy", () => {
  const profile = { security: { host_home_auth_mounts: { mode: "warn" } } };
  assert.deepEqual(extractHostHomeAuthMountPolicy(profile), { mode: "warn" });
});

test("extractHostHomeAuthMountPolicy falls back to block for unrecognized mode", () => {
  const profile = { security: { host_home_auth_mounts: { mode: "allow" } } };
  assert.deepEqual(extractHostHomeAuthMountPolicy(profile), { mode: "block" });
});

test("extractProfileSecurity combines egress and host_home_auth_mounts", () => {
  const profile = {
    security: {
      egress: { mode: "offline", allowlist: [] },
      host_home_auth_mounts: { mode: "warn" },
    },
  };
  const result = extractProfileSecurity(profile);
  assert.equal(result.egress.mode, "offline");
  assert.equal(result.host_home_auth_mounts.mode, "warn");
});

test("extractProfileSecurity returns safe defaults for null profile", () => {
  const result = extractProfileSecurity(null);
  assert.equal(result.egress.mode, "open");
  assert.deepEqual(result.egress.allowlist, []);
  assert.equal(result.host_home_auth_mounts.mode, "block");
});

test("summarizeSecretLeaseReadiness with empty array", () => {
  const result = summarizeSecretLeaseReadiness([]);
  assert.equal(result.total, 0);
  assert.equal(result.issued, 0);
  assert.equal(result.mounted, 0);
  assert.equal(result.expired, 0);
  assert.equal(result.revoked, 0);
  assert.equal(result.allReady, true);
  assert.equal(result.missingRequired, false);
});

test("summarizeSecretLeaseReadiness with all-mounted leases", () => {
  const leases = [
    { lease_id: "a", secret_name: "s1", kind: "mount", target: "/a", status: "mounted", provider: "vault", ref_digest: "h1", issued_at: "2025-01-01", mounted_at: "2025-01-01", expires_at: null, revoked_at: null },
  ];
  const result = summarizeSecretLeaseReadiness(leases);
  assert.equal(result.total, 1);
  assert.equal(result.mounted, 1);
  assert.equal(result.allReady, true);
  assert.equal(result.missingRequired, false);
});

test("summarizeSecretLeaseReadiness with mixed statuses", () => {
  const leases = [
    { lease_id: "a", secret_name: "s1", kind: "mount", target: "/a", status: "mounted", provider: "vault", ref_digest: null, issued_at: "2025-01-01", mounted_at: "2025-01-01", expires_at: null, revoked_at: null },
    { lease_id: "b", secret_name: "s2", kind: "env", target: "ENV_KEY", status: "issued", provider: null, ref_digest: null, issued_at: "2025-01-01", mounted_at: null, expires_at: null, revoked_at: null },
    { lease_id: "c", secret_name: "s3", kind: "mount", target: "/c", status: "expired", provider: "aws", ref_digest: null, issued_at: "2025-01-01", mounted_at: null, expires_at: "2025-01-02", revoked_at: null },
    { lease_id: "d", secret_name: "s4", kind: "mount", target: "/d", status: "revoked", provider: null, ref_digest: null, issued_at: "2025-01-01", mounted_at: null, expires_at: null, revoked_at: "2025-01-02" },
  ];
  const result = summarizeSecretLeaseReadiness(leases);
  assert.equal(result.total, 4);
  assert.equal(result.mounted, 1);
  assert.equal(result.issued, 1);
  assert.equal(result.expired, 1);
  assert.equal(result.revoked, 1);
  assert.equal(result.allReady, false);
});

test("summarizeEgressStatus for open mode", () => {
  const result = summarizeEgressStatus({ mode: "open", allowlist: [] });
  assert.equal(result.label, "open");
  assert.equal(result.tone, "good");
  assert.equal(result.detail, "no restrictions");
});

test("summarizeEgressStatus for allowlist mode", () => {
  const result = summarizeEgressStatus({ mode: "allowlist", allowlist: ["github.com", "pypi.org"] });
  assert.equal(result.label, "allowlist");
  assert.equal(result.tone, "warn");
  assert.equal(result.detail, "2 allowed hosts");
});

test("summarizeEgressStatus for offline mode", () => {
  const result = summarizeEgressStatus({ mode: "offline", allowlist: [] });
  assert.equal(result.label, "offline");
  assert.equal(result.tone, "info");
  assert.equal(result.detail, "no external access");
});

test("summarizeEgressStatus for mirrored mode", () => {
  const result = summarizeEgressStatus({ mode: "mirrored", allowlist: ["internal.registry"] });
  assert.equal(result.label, "mirrored");
  assert.equal(result.tone, "neutral");
  assert.equal(result.detail, "1 mirror host");
});

test("formatHostHomeMountPolicy for block", () => {
  const result = formatHostHomeMountPolicy({ mode: "block" });
  assert.equal(result.label, "blocked");
  assert.equal(result.tone, "good");
});

test("formatHostHomeMountPolicy for warn", () => {
  const result = formatHostHomeMountPolicy({ mode: "warn" });
  assert.equal(result.label, "allowed (warn)");
  assert.equal(result.tone, "warn");
});

test("summarizeProviderCredentialReadiness with no secrets or leases", () => {
  const result = summarizeProviderCredentialReadiness([], []);
  assert.equal(result.declared, 0);
  assert.equal(result.leased, 0);
  assert.deepEqual(result.missingProviders, []);
  assert.equal(result.label, "no secrets declared");
  assert.equal(result.tone, "neutral");
});

test("summarizeProviderCredentialReadiness matching providers", () => {
  const secrets = [
    { name: "db-password", target: "/a", kind: "mount", mode: "ro", required: true, provider: "vault" },
    { name: "api-key", target: "KEY", kind: "env", mode: "ro", required: false, provider: "aws" },
  ];
  const leases = [
    { lease_id: "l1", secret_name: "db-password", kind: "mount", target: "/a", status: "mounted", provider: "vault", ref_digest: null, issued_at: "", mounted_at: "", expires_at: null, revoked_at: null },
    { lease_id: "l2", secret_name: "api-key", kind: "env", target: "KEY", status: "issued", provider: "aws", ref_digest: null, issued_at: "", mounted_at: null, expires_at: null, revoked_at: null },
  ];
  const result = summarizeProviderCredentialReadiness(secrets, leases);
  assert.equal(result.declared, 2);
  assert.equal(result.leased, 2);
  assert.deepEqual(result.missingProviders, []);
  assert.equal(result.tone, "good");
});

test("summarizeProviderCredentialReadiness with missing providers", () => {
  const secrets = [
    { name: "db-password", target: "/a", kind: "mount", mode: "ro", required: true, provider: "vault" },
    { name: "api-key", target: "KEY", kind: "env", mode: "ro", required: false, provider: "aws" },
  ];
  const leases = [
    { lease_id: "l1", secret_name: "db-password", kind: "mount", target: "/a", status: "mounted", provider: "vault", ref_digest: null, issued_at: "", mounted_at: "", expires_at: null, revoked_at: null },
  ];
  const result = summarizeProviderCredentialReadiness(secrets, leases);
  assert.equal(result.declared, 2);
  assert.equal(result.leased, 1);
  assert.deepEqual(result.missingProviders, ["aws"]);
  assert.equal(result.tone, "warn");
});

test("summarizeProviderCredentialReadiness secrets with null provider matched by name", () => {
  const secrets = [
    { name: "local-secret", target: "/a", kind: "mount", mode: "ro", required: true, provider: null },
  ];
  const leases = [
    { lease_id: "l1", secret_name: "local-secret", kind: "mount", target: "/a", status: "mounted", provider: null, ref_digest: null, issued_at: "", mounted_at: "", expires_at: null, revoked_at: null },
  ];
  const result = summarizeProviderCredentialReadiness(secrets, leases);
  assert.equal(result.declared, 1);
  assert.equal(result.leased, 1);
  assert.equal(result.tone, "good");
});