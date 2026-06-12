import type { WorkspaceArtifact } from "@/lib/types";

// Stable names the executor deposits into the served artifact dir. The console
// labels artifacts by filename (the API's ``kind`` stays suffix-based).
export const PLAN_ARTIFACT_NAME = "plan.md";
export const CONFORMANCE_ARTIFACT_NAME = "conformance.json";

export function findArtifactByName(
  items: WorkspaceArtifact[],
  name: string,
): WorkspaceArtifact | undefined {
  return items.find((item) => item.name === name);
}

export function hasPlanArtifact(items: WorkspaceArtifact[]): boolean {
  return findArtifactByName(items, PLAN_ARTIFACT_NAME) !== undefined;
}

export function hasConformanceArtifact(items: WorkspaceArtifact[]): boolean {
  return findArtifactByName(items, CONFORMANCE_ARTIFACT_NAME) !== undefined;
}

export function artifactDownloadPath(workspaceId: string, name: string): string {
  return `/api/awf/workspaces/${encodeURIComponent(workspaceId)}/artifacts/download?path=${encodeURIComponent(name)}`;
}

// Pretty-print a conformance report; fall back to the raw text when it is not
// valid JSON so a malformed report still renders instead of disappearing.
export function formatConformanceJson(rawText: string): string {
  try {
    return JSON.stringify(JSON.parse(rawText), null, 2);
  } catch {
    return rawText;
  }
}
