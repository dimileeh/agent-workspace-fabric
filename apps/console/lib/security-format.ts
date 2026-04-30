import type {
  HostHomeAuthMountPolicy,
  ProfileEgress,
  ProfileSecret,
  ProfileSecurity,
  WorkspaceSecretLease,
} from "@/lib/types";

type StatusTone = "neutral" | "info" | "good" | "warn" | "bad";

const VALID_EGRESS_MODES = new Set(["open", "allowlist", "offline", "mirrored", "unavailable"]);
const VALID_MOUNT_POLICY_MODES = new Set(["block", "warn"]);

export function extractProfileSecrets(
  profile: Record<string, unknown> | null | undefined,
): ProfileSecret[] {
  if (!profile || !Array.isArray(profile.secrets)) {
    return [];
  }
  return profile.secrets
    .filter((entry): entry is Record<string, unknown> => entry != null && typeof entry === "object")
    .map((entry) => ({
      name: typeof entry.name === "string" ? entry.name : "",
      target: typeof entry.target === "string" ? entry.target : "",
      kind: entry.kind === "env" ? "env" as const : "mount" as const,
      mode: entry.mode === "rw" ? "rw" as const : "ro" as const,
      required: entry.required === true,
      provider: typeof entry.provider === "string" ? entry.provider : null,
    }));
}

export function extractProfileEgress(
  profile: Record<string, unknown> | null | undefined,
): ProfileEgress {
  const security = (profile as Record<string, unknown> | null | undefined)?.security as Record<string, unknown> | null | undefined;
  const egressObj = security?.egress as Record<string, unknown> | null | undefined;
  if (!egressObj || typeof egressObj !== "object") {
    return { mode: "unavailable", allowlist: [] };
  }
  const rawMode = typeof egressObj.mode === "string" ? egressObj.mode : "";
  const mode = VALID_EGRESS_MODES.has(rawMode) ? (rawMode as ProfileEgress["mode"]) : "unavailable";
  const allowlist = Array.isArray(egressObj.allowlist)
    ? egressObj.allowlist.filter((item): item is string => typeof item === "string")
    : [];
  return { mode, allowlist };
}

export function extractHostHomeAuthMountPolicy(
  profile: Record<string, unknown> | null | undefined,
): HostHomeAuthMountPolicy {
  const security = (profile as Record<string, unknown> | null | undefined)?.security as Record<string, unknown> | null | undefined;
  const policy = security?.host_home_auth_mounts as Record<string, unknown> | null | undefined;
  if (!policy || typeof policy !== "object") {
    return { mode: "block" };
  }
  const rawMode = typeof policy.mode === "string" ? policy.mode : "";
  const mode = VALID_MOUNT_POLICY_MODES.has(rawMode)
    ? (rawMode as HostHomeAuthMountPolicy["mode"])
    : "block";
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
      return { label: "open", tone: "good", detail: "no restrictions" };
    case "allowlist":
      return {
        label: "allowlist",
        tone: "warn",
        detail: `${egress.allowlist.length} allowed host${egress.allowlist.length === 1 ? "" : "s"}`,
      };
    case "offline":
      return { label: "offline", tone: "info", detail: "no external access" };
    case "mirrored":
      return {
        label: "mirrored",
        tone: "neutral",
        detail: `${egress.allowlist.length} mirror host${egress.allowlist.length === 1 ? "" : "s"}`,
      };
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

  const leaseByName = new Map<string, WorkspaceSecretLease>();
  for (const lease of leases) {
    leaseByName.set(lease.secret_name, lease);
  }

  const leasedProviders = new Set<string>();
  const missingProviders: string[] = [];

  for (const secret of secrets) {
    const matchingLease = leaseByName.get(secret.name);
    if (matchingLease && (matchingLease.status === "mounted" || matchingLease.status === "issued")) {
      leasedProviders.add(secret.name);
    } else {
      const missing = secret.provider ?? secret.name;
      if (!missingProviders.includes(missing)) {
        missingProviders.push(missing);
      }
    }
  }

  const leased = leasedProviders.size;
  const label =
    missingProviders.length > 0
      ? `${leased}/${declared} — missing providers`
      : leased === declared
        ? "all ready"
        : `${leased}/${declared} ready`;
  const tone = leased === declared && missingProviders.length === 0 ? "good" : "warn";

  return { declared, leased, missingProviders, label, tone };
}