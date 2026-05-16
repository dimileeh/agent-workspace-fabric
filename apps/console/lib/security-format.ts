import type {
  HostHomeAuthMountPolicy,
  ProfileEgress,
  ProfileSecret,
  ProfileSecurity,
  WorkspaceSecretLease,
} from "@/lib/types";

type StatusTone = "neutral" | "info" | "good" | "warn" | "bad";

const VALID_EGRESS_MODES = new Set(["open", "restricted", "offline", "unavailable"]);
const VALID_MOUNT_POLICY_MODES = new Set(["block", "warn", "unavailable"]);

export type ProfileSecretsResult =
  | { available: true; secrets: ProfileSecret[] }
  | { available: false };

export function extractProfileSecrets(
  profile: Record<string, unknown> | null | undefined,
): ProfileSecretsResult {
  if (!profile || !Array.isArray(profile.secrets)) {
    return { available: false };
  }
  const secrets = profile.secrets
    .filter((entry): entry is Record<string, unknown> => entry != null && typeof entry === "object")
    .map((entry) => ({
      name: typeof entry.name === "string" ? entry.name : "",
      target: typeof entry.target === "string" ? entry.target : "",
      kind: entry.kind === "env" ? "env" as const : "mount" as const,
      mode: entry.mode === "rw" ? "rw" as const : "ro" as const,
      required: entry.required === true,
      provider: typeof entry.provider === "string" ? entry.provider : null,
    }));
  return { available: true, secrets };
}

export function extractProfileEgress(
  profile: Record<string, unknown> | null | undefined,
): ProfileEgress {
  const security = (profile as Record<string, unknown> | null | undefined)?.security as Record<string, unknown> | null | undefined;
  const egressObj = security?.egress as Record<string, unknown> | null | undefined;
  if (!egressObj || typeof egressObj !== "object") {
    return { mode: "unavailable" };
  }
  const rawMode = typeof egressObj.mode === "string" ? egressObj.mode : "";
  const mode = VALID_EGRESS_MODES.has(rawMode) ? (rawMode as ProfileEgress["mode"]) : "unavailable";
  return { mode };
}

export function extractHostHomeAuthMountPolicy(
  profile: Record<string, unknown> | null | undefined,
): HostHomeAuthMountPolicy {
  const security = (profile as Record<string, unknown> | null | undefined)?.security as Record<string, unknown> | null | undefined;
  const policy = security?.host_home_auth_mounts as Record<string, unknown> | null | undefined;
  if (!policy || typeof policy !== "object") {
    return { mode: "unavailable" };
  }
  const rawMode = typeof policy.mode === "string" ? policy.mode : "";
  const mode = VALID_MOUNT_POLICY_MODES.has(rawMode)
    ? (rawMode as HostHomeAuthMountPolicy["mode"])
    : "unavailable";
  return { mode };
}

export function extractProfileSecurity(
  profile: Record<string, unknown> | null | undefined,
): ProfileSecurity {
  return {
    egress: extractProfileEgress(profile),
    host_home_auth_mounts: extractHostHomeAuthMountPolicy(profile),
  };
}

export function summarizeSecretLeaseReadiness(leases: WorkspaceSecretLease[]) {
  let issued = 0;
  let mounted = 0;
  let expired = 0;
  let revoked = 0;
  for (const lease of leases) {
    if (lease.status === "mounted") {
      mounted++;
    } else if (lease.status === "issued") {
      issued++;
    } else if (lease.status === "expired") {
      expired++;
    } else if (lease.status === "revoked") {
      revoked++;
    }
  }
  const total = leases.length;
  const allReady = total === 0 || leases.every((lease) => lease.status === "mounted");
  const missingRequired = leases.some(
    (lease) => lease.status !== "mounted" && lease.status !== "issued",
  );
  return { total, issued, mounted, expired, revoked, allReady, missingRequired };
}

export function summarizeEgressStatus(egress: ProfileEgress): {
  label: string;
  tone: StatusTone;
  detail: string;
} {
  switch (egress.mode) {
    case "open":
      return { label: "open", tone: "warn", detail: "unrestricted internet" };
    case "restricted":
      return { label: "restricted", tone: "good", detail: "default local-only" };
    case "offline":
      return { label: "offline", tone: "info", detail: "no external access" };
    case "unavailable":
      return { label: "unavailable", tone: "neutral", detail: "egress config not provided" };
  }
}

export function formatHostHomeMountPolicy(policy: HostHomeAuthMountPolicy): {
  label: string;
  tone: StatusTone;
} {
  if (policy.mode === "block") {
    return { label: "blocked", tone: "good" };
  }
  if (policy.mode === "unavailable") {
    return { label: "unavailable", tone: "neutral" };
  }
  return { label: "allowed (warn)", tone: "warn" };
}

export function summarizeProviderCredentialReadiness(
  secrets: ProfileSecret[],
  leases: WorkspaceSecretLease[],
): { declared: number; leased: number; missingProviders: string[]; label: string; tone: StatusTone } {
  const declared = secrets.length;
  if (declared === 0) {
    return {
      declared: 0,
      leased: 0,
      missingProviders: [],
      label: "no secrets declared",
      tone: "neutral",
    };
  }

  const leaseByKey = new Map<string, WorkspaceSecretLease>();
  for (const lease of leases) {
    const key = `${lease.secret_name}\0${lease.kind}\0${lease.target}`;
    leaseByKey.set(key, lease);
  }

  let leased = 0;
  const missingProvidersSet = new Set<string>();

  for (const secret of secrets) {
    const key = `${secret.name}\0${secret.kind}\0${secret.target}`;
    const matchingLease = leaseByKey.get(key);
    if (matchingLease && (matchingLease.status === "mounted" || matchingLease.status === "issued")) {
      leased++;
    } else {
      const missing = secret.provider ?? secret.name;
      missingProvidersSet.add(missing);
    }
  }
  const missingProviders = Array.from(missingProvidersSet);
  const label =
    missingProviders.length > 0
      ? `${leased}/${declared} — missing providers`
      : leased === declared
        ? "all ready"
        : `${leased}/${declared} ready`;
  const tone = leased === declared && missingProviders.length === 0 ? "good" : "warn";

  return { declared, leased, missingProviders, label, tone };
}
