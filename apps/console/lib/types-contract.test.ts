import type {
  ValidationFreshnessStatus,
  ValidationRunSummary,
  Workspace,
  WorkspaceRecoverySummary,
} from "./types";

type Equal<Left, Right> =
  (<Value>() => Value extends Left ? 1 : 2) extends <Value>() => Value extends Right ? 1 : 2
    ? (<Value>() => Value extends Right ? 1 : 2) extends <Value>() => Value extends Left ? 1 : 2
      ? true
      : false
    : false;

type Expect<Condition extends true> = Condition;

type WorkspaceRecoverySummaryTextIsRequired = Expect<
  Equal<WorkspaceRecoverySummary["summary"], string>
>;

export const workspaceRecoverySummaryTextIsRequired: WorkspaceRecoverySummaryTextIsRequired = true;

type WorkspaceValidationProvenanceIsPresent = Expect<
  Equal<
    Workspace["validation_provenance"],
    {
      required_tier: 1 | 2 | 3 | null;
      latest_satisfied_tier: 1 | 2 | 3 | null;
      freshness_status: ValidationFreshnessStatus;
      reason_code: string | null;
      current_target_head_sha: string | null;
      latest_validation: ValidationRunSummary | null;
    }
  >
>;

type ValidationRunSummaryIdentityFieldsArePresent = Expect<
  Equal<
    Pick<
      ValidationRunSummary,
      | "base_sha"
      | "workspace_head_sha"
      | "profile_name"
      | "profile_version"
      | "profile_source"
      | "resolved_profile_digest"
      | "environment_identity_digest"
      | "environment_identity_inputs"
      | "identity_source"
      | "freshness_status"
      | "freshness_reason_code"
    >,
    {
      base_sha: string | null;
      workspace_head_sha: string | null;
      profile_name: string | null;
      profile_version: number | null;
      profile_source: string | null;
      resolved_profile_digest: string | null;
      environment_identity_digest: string | null;
      environment_identity_inputs: Record<string, unknown>;
      identity_source: "persisted" | "legacy_fallback";
      freshness_status: ValidationFreshnessStatus;
      freshness_reason_code: string | null;
    }
  >
>;

export const workspaceValidationProvenanceIsPresent: WorkspaceValidationProvenanceIsPresent = true;
export const validationRunSummaryIdentityFieldsArePresent: ValidationRunSummaryIdentityFieldsArePresent = true;
