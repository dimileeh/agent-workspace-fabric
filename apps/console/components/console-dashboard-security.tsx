"use client";

import {
Activity,
AlertCircle,
Bot,
KeyRound,
Shield
} from "lucide-react";

import {
compactId,
formatDateTime,
statusTone
} from "@/lib/format";
import {
extractProfileSecrets,
extractProfileSecurity,
formatHostHomeMountPolicy,
summarizeEgressStatus,
summarizeProviderCredentialReadiness,
summarizeSecretLeaseReadiness,
} from "@/lib/security-format";
import type {
FailureSummaryResponse,
PolicyFinding,
WorkspaceEgressAudit,
WorkspaceSecretLease
} from "@/lib/types";
import { QueueChip } from "./console-dashboard-capacity";
import {
MutedLine,
Panel,
SmallExternalAnchor,
Td,
Th,
formatPrLinkLabel
} from "./console-dashboard-shared";

export function SecurityEgressPanel({
  resolvedProfile,
  policyFindings,
  egressAudit,
}: {
  resolvedProfile: Record<string, unknown> | null;
  policyFindings: PolicyFinding[] | undefined;
  egressAudit: WorkspaceEgressAudit | null | undefined;
}) {
  const findingsUnavailable = policyFindings === undefined;
  const security = extractProfileSecurity(resolvedProfile);
  const egressStatus = summarizeEgressStatus(security.egress);
  const mountPolicy = formatHostHomeMountPolicy(security.host_home_auth_mounts);
  const secretsResult = extractProfileSecrets(resolvedProfile);
  const secretsUnavailable = !secretsResult.available;
  const secretsAvailable = secretsResult.available ? secretsResult.secrets : [];
  const secretCount = secretsAvailable.length;
  const activeFindings = (policyFindings ?? []).filter((finding) => finding.status === "active");

  return (
    <Panel title="Security & Egress" icon={<Shield size={16} aria-hidden />}>
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
        <QueueChip
          label="Egress"
          value={egressStatus.label}
          detail={egressStatus.detail}
          tone={egressStatus.tone}
        />
        <QueueChip
          label="Host-home mounts"
          value={mountPolicy.label}
          tone={mountPolicy.tone}
        />
        <QueueChip
          label="Secrets declared"
          value={secretsUnavailable ? "unavailable" : secretCount === 0 ? "none" : String(secretCount)}
          tone={secretsUnavailable ? "neutral" : secretCount > 0 ? "info" : "neutral"}
        />
        <QueueChip
          label="Policy findings"
          value={findingsUnavailable ? "unavailable" : activeFindings.length === 0 ? "none" : String(activeFindings.length)}
          detail={activeFindings.length > 0 ? activeFindings.map((f) => f.reason_code).join(", ") : undefined}
          tone={findingsUnavailable ? "neutral" : activeFindings.length > 0 ? "warn" : "neutral"}
        />
        <QueueChip
          label="Audit decision"
          value={egressAudit === undefined ? "unavailable" : egressAudit === null ? "none" : egressAudit.decision}
          detail={
            egressAudit
              ? `enforced ${formatDateTime(egressAudit.enforced_at)}`
              : egressAudit === null
                ? "no egress decision recorded"
                : "audit record not loaded"
          }
          tone={
            egressAudit === undefined || egressAudit === null
              ? "neutral"
              : egressAuditDecisionTone(egressAudit.decision)
          }
        />
        {egressAudit ? (
          <>
            <QueueChip
              label="Audit posture"
              value={egressAudit.policy_posture}
              tone={egressAudit.policy_posture === egressStatus.label ? egressStatus.tone : "warn"}
            />
            <QueueChip
              label="Destination"
              value={egressAudit.destination_category}
              detail={egressAuditDetailsSummary(egressAudit.details)}
              mono
              tone="info"
            />
            <QueueChip
              label="Audit reason"
              value={egressAudit.reason_code}
              mono
              tone="info"
            />
          </>
        ) : null}
      </div>
    </Panel>
  );
}

export function egressAuditDecisionTone(decision: string): ReturnType<typeof statusTone> {
  const normalized = decision.toLowerCase();
  if (normalized === "allowed" || normalized === "allow") {
    return "good";
  }
  if (normalized === "blocked" || normalized === "denied" || normalized === "deny") {
    return "bad";
  }
  if (normalized === "warn" || normalized === "warning") {
    return "warn";
  }
  return "neutral";
}

export function egressAuditDetailsSummary(details: Record<string, unknown>): string | undefined {
  const hostname = details.hostname;
  if (typeof hostname === "string" && hostname.trim()) {
    return hostname;
  }
  const host = details.host;
  if (typeof host === "string" && host.trim()) {
    return host;
  }
  const url = details.url;
  if (typeof url === "string" && url.trim()) {
    return url;
  }
  return undefined;
}

export function SecretsLeasesPanel({
  resolvedProfile,
  secretLeases,
}: {
  resolvedProfile: Record<string, unknown> | null;
  secretLeases: WorkspaceSecretLease[] | null | undefined;
}) {
  const secretsResult = extractProfileSecrets(resolvedProfile);
  const secretsUnavailable = !secretsResult.available;
  const secrets = secretsResult.available ? secretsResult.secrets : [];
  const leasesUnavailable = secretLeases == null;
  const leaseArray = secretLeases ?? [];
  const readiness = summarizeSecretLeaseReadiness(leaseArray);
  const credentialReadiness = summarizeProviderCredentialReadiness(secrets, leaseArray);
  const mountSecrets = secrets.filter((s) => s.kind === "mount").length;
  const envSecrets = secrets.filter((s) => s.kind === "env").length;

  return (
    <Panel title="Secrets & Leases" icon={<KeyRound size={16} aria-hidden />}>
      {secretsUnavailable && leasesUnavailable ? (
        <MutedLine>No secret policy or leases reported for this workspace.</MutedLine>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
          {secretsUnavailable ? (
            <QueueChip label="Secrets declared" value="unavailable" tone="neutral" />
          ) : secrets.length > 0 ? (
            <>
              <QueueChip
                label="Mount secrets"
                value={String(mountSecrets)}
                tone={mountSecrets > 0 ? "info" : "neutral"}
              />
              <QueueChip
                label="Env secrets"
                value={String(envSecrets)}
                tone={envSecrets > 0 ? "info" : "neutral"}
              />
            </>
          ) : (
            <QueueChip label="Secrets declared" value="none" tone="neutral" />
          )}
          {leaseArray.length > 0 ? (
            <>
              <QueueChip
                label="Leases mounted"
                value={`${readiness.mounted}/${readiness.total}`}
                tone={readiness.allReady ? "good" : "warn"}
                detail={readiness.allReady ? "all mounted" : undefined}
              />
              {readiness.issued > 0 ? (
                <QueueChip label="Issued" value={String(readiness.issued)} tone="info" />
              ) : null}
              {readiness.expired > 0 ? (
                <QueueChip label="Expired" value={String(readiness.expired)} tone="bad" />
              ) : null}
              {readiness.revoked > 0 ? (
                <QueueChip label="Revoked" value={String(readiness.revoked)} tone="bad" />
              ) : null}
            </>
          ) : (
            <QueueChip label="Leases" value={leasesUnavailable ? "unavailable" : "none"} tone="neutral" />
          )}
          {secrets.length > 0 ? (
            <QueueChip
              label="Provider readiness"
              value={leasesUnavailable ? "unavailable" : credentialReadiness.label}
              tone={leasesUnavailable ? "neutral" : credentialReadiness.tone}
              detail={
                leasesUnavailable
                  ? "lease data not yet reported"
                  : credentialReadiness.missingProviders.length > 0
                    ? `missing: ${credentialReadiness.missingProviders.join(", ")}`
                    : undefined
              }
            />
          ) : null}
        </div>
      )}
    </Panel>
  );
}


export function FailureAnalysisPanel({
  summary,
  status,
  error,
  stale = false,
}: {
  summary: FailureSummaryResponse | null;
  status: "loading" | "success" | "error" | "unavailable";
  error: string | null;
  stale?: boolean;
}) {
  if (status === "unavailable") {
    return (
      <Panel title="Failure Analysis" icon={<AlertCircle size={16} aria-hidden />}>
        <MutedLine>Failure analysis is currently unavailable.</MutedLine>
      </Panel>
    );
  }

  const isLoading = status === "loading";
  const isError = status === "error";

  return (
    <Panel
      title="Failure Analysis"
      icon={<Activity size={16} aria-hidden />}
      stale={stale}
      action={
        <span className="rounded-md border border-slate-200 bg-slate-50 px-2.5 py-1 text-[11px] text-slate-600">
          {isLoading && !summary ? "loading" : isError && !summary ? "error" : `${summary?.total_failures ?? 0} failures in window`}
        </span>
      }
    >
      {isLoading && !summary ? (
        <MutedLine>Failure analysis loading.</MutedLine>
      ) : isError && !summary ? (
        <MutedLine>Unable to load failure analysis: {error}</MutedLine>
      ) : !summary ? (
        <MutedLine>No failure analysis data available.</MutedLine>
      ) : (
        <div className="grid gap-4">
          {error ? (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
              Showing last snapshot. Refresh failed: {error}
            </div>
          ) : null}

          {summary.taxonomy && summary.taxonomy.length > 0 ? (
            <div className="grid gap-2 sm:grid-cols-2 md:grid-cols-3 xl:grid-cols-4">
              {summary.taxonomy.map((tax) => (
                <div key={tax.reason} className="min-w-0 rounded-md border border-slate-200 bg-slate-50 px-3 py-2">
                  <div className="truncate text-[10px] font-medium text-slate-500" title={tax.reason}>{tax.reason}</div>
                  <div className="mono mt-1 text-lg font-semibold text-slate-900">{tax.count}</div>
                </div>
              ))}
            </div>
          ) : null}

          {summary.latest_examples && summary.latest_examples.length > 0 ? (
             <div className="grid gap-2">
               <h3 className="text-xs font-semibold text-slate-700">Latest Examples</h3>
               <div className="max-h-[320px] overflow-auto rounded-md border border-slate-200">
                 <table className="w-full min-w-full table-fixed text-left text-xs md:min-w-[720px]">
                   <thead className="sticky top-0 bg-slate-50 text-slate-600 shadow-[0_1px_0_var(--border)]">
                     <tr>
                       <Th>Workspace</Th>
                       <Th>Context</Th>
                       <Th>Reason</Th>
                       <Th>Message</Th>
                       <Th>Time</Th>
                     </tr>
                   </thead>
                   <tbody>
                     {summary.latest_examples.map((example) => (
                       <tr key={`${example.workspace_id}-${example.timestamp}`} className="border-t border-slate-100 bg-white">
                         <Td>
                           <div className="font-medium text-slate-950 truncate max-w-[200px]" title={example.title}>{example.title}</div>
                           <div className="mono text-[11px] text-slate-500 mt-0.5">{compactId(example.workspace_id, 10)}</div>
                         </Td>
                         <Td>
                            <div className="truncate max-w-[150px]" title={example.repo_url}>{example.repo_url}</div>
                            <div className="text-[11px] text-slate-500 mt-0.5 flex items-center gap-1">
                               <Bot size={10} aria-hidden />
                               {example.agent}
                            </div>
                         </Td>
                         <Td>
                           <span className="inline-flex rounded-md border border-red-200 bg-red-50 px-1.5 py-0.5 text-[10px] font-medium text-red-800">
                             {example.failure_reason}
                           </span>
                         </Td>
                         <Td>
                           <div className="truncate max-w-[300px] text-slate-600" title={example.failure_message}>
                             {example.failure_message}
                           </div>
                         </Td>
                         <Td>
                           <div className="flex min-w-0 flex-wrap items-center gap-2">
                             <span className="text-slate-500">{formatDateTime(example.timestamp)}</span>
                             {example.pr_url ? (
                               <SmallExternalAnchor
                                 href={example.pr_url}
                                 label={formatPrLinkLabel(example.pr_url)}
                               />
                             ) : null}
                           </div>
                         </Td>
                       </tr>
                     ))}
                   </tbody>
                 </table>
               </div>
             </div>
          ) : null}
        </div>
      )}
    </Panel>
  );
}
