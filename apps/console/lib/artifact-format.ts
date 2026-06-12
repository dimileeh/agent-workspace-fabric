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

// The artifact list API is cursor-paginated (default page 50, max 500) and
// sorted by relative_path, so plan.md / conformance.json can land on a later
// page whenever enough artifacts sort before them. Request large pages and
// follow next_cursor (below) so the presence decision sees every artifact
// instead of hiding the Plan/Validation controls for an artifact-heavy task.
export const ARTIFACT_LIST_PAGE_SIZE = 200;
// Defensive ceiling on pages followed so a misbehaving cursor cannot loop
// forever; 200 * 50 covers 10k artifacts, far beyond any real deposit.
export const ARTIFACT_LIST_MAX_PAGES = 50;

export function artifactListPath(workspaceId: string, cursor: string | null): string {
  const query = new URLSearchParams({ limit: String(ARTIFACT_LIST_PAGE_SIZE) });
  if (cursor) {
    query.set("cursor", cursor);
  }
  return `/api/awf/workspaces/${encodeURIComponent(workspaceId)}/artifacts?${query.toString()}`;
}

export interface ArtifactListPage {
  items: WorkspaceArtifact[];
  next_cursor: string | null;
  has_more: boolean;
}

// Accumulate artifacts across pages until both named artifacts are present (the
// only thing the presence check needs) or the list is exhausted. ``fetchPage``
// returns ``null`` to signal a failed request, which short-circuits to ``null``
// so the caller can surface the error instead of a truncated list.
export async function collectArtifactsForPresence(
  fetchPage: (cursor: string | null) => Promise<ArtifactListPage | null>,
): Promise<WorkspaceArtifact[] | null> {
  const collected: WorkspaceArtifact[] = [];
  let cursor: string | null = null;
  for (let page = 0; page < ARTIFACT_LIST_MAX_PAGES; page += 1) {
    const data = await fetchPage(cursor);
    if (data === null) {
      return null;
    }
    collected.push(...data.items);
    if (hasPlanArtifact(collected) && hasConformanceArtifact(collected)) {
      break;
    }
    if (!data.has_more || !data.next_cursor) {
      break;
    }
    cursor = data.next_cursor;
  }
  return collected;
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
