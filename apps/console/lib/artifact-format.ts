import type { ArtifactPresenceItem, ArtifactPresencePage } from "@/lib/types";
import { awfPath } from "./console-urls.ts";

// Stable names the executor deposits into the served artifact dir. The console
// labels artifacts by filename (the API's ``kind`` stays suffix-based).
export const PLAN_ARTIFACT_NAME = "plan.md";
export const CONFORMANCE_ARTIFACT_NAME = "conformance.json";

// Match on ``relative_path``, not ``name``: the artifacts API exposes nested
// files with ``name`` set to the basename only, so a nested
// ``validation_worktree/plan.md`` would otherwise satisfy a basename match for
// ``plan.md`` even though no root artifact exists. The download endpoint is
// then called with ``path=plan.md`` (the stable root path) and 404s. The
// deposited plan/conformance artifacts live at the root, where
// ``relative_path`` equals the stable filename.
export function findArtifactByRelativePath(
  items: readonly ArtifactPresenceItem[],
  relativePath: string,
): ArtifactPresenceItem | undefined {
  return items.find((item) => item.relative_path === relativePath);
}

export function hasPlanArtifact(items: readonly ArtifactPresenceItem[]): boolean {
  return findArtifactByRelativePath(items, PLAN_ARTIFACT_NAME) !== undefined;
}

export function hasConformanceArtifact(items: readonly ArtifactPresenceItem[]): boolean {
  return findArtifactByRelativePath(items, CONFORMANCE_ARTIFACT_NAME) !== undefined;
}

export function artifactDownloadPath(workspaceId: string, name: string): string {
  return awfPath(`workspaces/${encodeURIComponent(workspaceId)}/artifacts/download`, {
    path: name,
  });
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
  return awfPath(`workspaces/${encodeURIComponent(workspaceId)}/artifacts`, {
    limit: ARTIFACT_LIST_PAGE_SIZE,
    cursor: cursor ?? undefined,
  });
}

function isArtifactPresenceItem(value: unknown): value is ArtifactPresenceItem {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const relativePath = (value as { relative_path?: unknown }).relative_path;
  return typeof relativePath === "string" && relativePath.length > 0;
}

// Accept a page only when every item has a usable relative_path and the
// pagination envelope is well typed. ``has_more`` advertises another page, so
// it requires a non-empty cursor; a null cursor cannot be followed and must
// not be treated as a complete list. Extra fields are left in place. One bad
// item rejects the whole page so presence is never decided from a partial list.
export function parseArtifactPresencePage(value: unknown): ArtifactPresencePage | null {
  if (typeof value !== "object" || value === null) {
    return null;
  }
  const page = value as {
    items?: unknown;
    next_cursor?: unknown;
    has_more?: unknown;
  };
  if (!Array.isArray(page.items) || !page.items.every(isArtifactPresenceItem)) {
    return null;
  }
  if (typeof page.has_more !== "boolean") {
    return null;
  }
  const nextCursor = page.next_cursor;
  const usableCursor = typeof nextCursor === "string" && nextCursor.length > 0;
  // Reject a non-string cursor, an empty cursor, and has_more with no cursor.
  // The last case advertises a later page that cannot be requested; returning
  // null here is what stops collectArtifactsForPresence from keeping the prefix.
  if (!usableCursor && (nextCursor !== null || page.has_more)) return null;
  return {
    items: page.items,
    next_cursor: nextCursor,
    has_more: page.has_more,
  };
}

// Accumulate artifacts across pages until both named artifacts are present (the
// only thing the presence check needs) or the list is exhausted. ``fetchPage``
// returns ``null`` to signal a failed request, which short-circuits to ``null``
// so the caller can surface the error instead of a truncated list. A thrown
// fetch or a page that is not an ArtifactPresencePage does the same.
export async function collectArtifactsForPresence(
  fetchPage: (cursor: string | null) => Promise<unknown>,
): Promise<ArtifactPresenceItem[] | null> {
  const collected: ArtifactPresenceItem[] = [];
  let cursor: string | null = null;
  for (let page = 0; page < ARTIFACT_LIST_MAX_PAGES; page += 1) {
    let raw: unknown;
    try {
      raw = await fetchPage(cursor);
    } catch {
      return null;
    }
    if (raw === null) {
      return null;
    }
    const data = parseArtifactPresencePage(raw);
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
