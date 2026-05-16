import type { ApiEnvelope, ProviderReadinessPreflight } from "./types";

export type ProviderReadinessTone = "good" | "warn" | "bad";

export interface ProviderReadinessFact {
  label: string;
  value: string;
  detail?: string;
  mono?: boolean;
  tone?: ProviderReadinessTone;
}

export function providerReadinessPreflightTone(
  preflight: ProviderReadinessPreflight,
): ProviderReadinessTone {
  if (preflight.blocks_launch || preflight.readiness_status === "blocked") {
    return "bad";
  }
  if (preflight.override_used) {
    return "warn";
  }
  return "good";
}

export function providerReadinessPreflightFacts(
  preflight: ProviderReadinessPreflight,
  options: { formatCheckedAt?: (value: string) => string } = {},
): ProviderReadinessFact[] {
  const tone = providerReadinessPreflightTone(preflight);
  const checkedAt = options.formatCheckedAt
    ? options.formatCheckedAt(preflight.checked_at)
    : preflight.checked_at;

  return [
    {
      label: "Provider",
      value: preflight.provider,
      detail: preflight.agent,
      mono: true,
    },
    {
      label: "Model",
      value: preflight.model ?? "unavailable",
      mono: true,
    },
    {
      label: "Readiness",
      value: preflight.readiness_status,
      tone,
    },
    {
      label: "Auth",
      value: preflight.auth_status,
      detail: `${preflight.auth_source} / ${preflight.credential_scope ?? "unknown"}`,
      mono: true,
    },
    {
      label: "Probe",
      value: preflight.probe_status,
      mono: true,
    },
    {
      label: "Override",
      value: preflight.override_used ? "used" : "not used",
    },
    {
      label: "Checked",
      value: checkedAt,
    },
    {
      label: "Isolation",
      value: preflight.isolation ?? "unknown",
      mono: true,
    },
  ];
}

export function formatProviderReadinessRetryError(
  result: Extract<ApiEnvelope<unknown>, { ok: false }>,
): string {
  const code = result.errorCode ? `${result.errorCode}: ` : "";
  const preflight = providerReadinessPreflightFromError(result.detail);
  if (preflight) {
    return `${code}${result.message} ${preflight.provider}/${preflight.model ?? "model unavailable"} readiness ${preflight.readiness_status}; auth ${preflight.auth_status} via ${preflight.auth_source}; probe ${preflight.probe_status}; reason ${preflight.reason_code}.`;
  }
  return `${code}${result.message} Confirm the workspace is still failed or cancelled, resolve any reported lock conflict, then retry.`;
}

export function providerReadinessPreflightFromError(
  detail: unknown,
): ProviderReadinessPreflight | null {
  if (!detail || typeof detail !== "object") {
    return null;
  }
  const root = detail as { detail?: unknown; provider_readiness_preflight?: unknown };
  const nested =
    root.provider_readiness_preflight ??
    (root.detail && typeof root.detail === "object"
      ? (root.detail as { provider_readiness_preflight?: unknown }).provider_readiness_preflight
      : null);
  if (!nested || typeof nested !== "object") {
    return null;
  }
  const candidate = nested as Partial<ProviderReadinessPreflight>;
  if (
    typeof candidate.provider === "string" &&
    typeof candidate.readiness_status === "string" &&
    typeof candidate.auth_status === "string" &&
    typeof candidate.auth_source === "string" &&
    typeof candidate.probe_status === "string" &&
    typeof candidate.reason_code === "string" &&
    typeof candidate.blocks_launch === "boolean" &&
    typeof candidate.override_used === "boolean" &&
    typeof candidate.checked_at === "string"
  ) {
    return candidate as ProviderReadinessPreflight;
  }
  return null;
}
