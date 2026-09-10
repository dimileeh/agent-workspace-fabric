import type { ListEnvelope, WorkspaceOverview } from "@/lib/types";
import { awfPath } from "./console-urls.ts";

// Overview list is cursor-paginated (API max 500). Request a small page so the
// dashboard can paint promptly and fetch later pages only as the operator asks.
export const OVERVIEW_LIST_PAGE_SIZE = 100;
// POST /workspaces/overview/batch accepts at most 200 unique IDs.
export const OVERVIEW_REFRESH_BATCH_SIZE = 200;
// Defensive ceiling so a misbehaving cursor cannot loop forever;
// 100 * 50 covers 5k workspaces, beyond typical local/Core fleets.
export const OVERVIEW_LIST_MAX_PAGES = 50;

export type OverviewListFilters = {
  status?: string;
  agent?: string;
  repo_url?: string;
};

export type OverviewListPage = ListEnvelope<WorkspaceOverview>;

/** Why a collected prefix is incomplete. Callers must surface this, not treat it as done. */
export type OverviewTruncationReason = "page_ceiling" | "missing_cursor";

/** Result of accumulating overview pages; never pretend a capped prefix is complete. */
export type OverviewPageCollection = {
  items: WorkspaceOverview[];
  /**
   * True when more rows exist but pagination cannot continue: the page ceiling
   * stopped with has_more, or has_more was set without a continuation cursor.
   */
  truncated: boolean;
  /** Set only when ``truncated`` is true. */
  truncationReason: OverviewTruncationReason | null;
};

function complete(items: WorkspaceOverview[]): OverviewPageCollection {
  return { items, truncated: false, truncationReason: null };
}

function truncated(
  items: WorkspaceOverview[],
  truncationReason: OverviewTruncationReason,
): OverviewPageCollection {
  return { items, truncated: true, truncationReason };
}

/** A continuation cursor must be a non-blank string; null, omitted, and whitespace cannot be followed. */
export function usableContinuationCursor(cursor: string | null | undefined): cursor is string {
  return typeof cursor === "string" && cursor.trim() !== "";
}

export function overviewListPath(
  filters: OverviewListFilters,
  cursor: string | null = null,
): string {
  return awfPath("workspaces/overview", {
    limit: OVERVIEW_LIST_PAGE_SIZE,
    status: filters.status,
    agent: filters.agent,
    repo_url: filters.repo_url,
    cursor: cursor ?? undefined,
  });
}

/** Append one cursor page without replacing existing rows or duplicating IDs. */
export function appendUniqueOverviewItems(
  current: WorkspaceOverview[],
  pageItems: WorkspaceOverview[],
): WorkspaceOverview[] {
  const seen = new Set<string>();
  const merged: WorkspaceOverview[] = [];
  for (const item of [...current, ...pageItems]) {
    if (seen.has(item.workspace_id)) {
      continue;
    }
    seen.add(item.workspace_id);
    merged.push(item);
  }
  return merged;
}

/**
 * Refresh the newest page while retaining an already-loaded history tail.
 * Byte-equivalent rows reuse their previous object so memoized cards stay
 * stable during routine polling.
 */
export function reconcileOverviewFirstPage(
  current: WorkspaceOverview[],
  pageItems: WorkspaceOverview[],
): WorkspaceOverview[] {
  const currentById = new Map(current.map((item) => [item.workspace_id, item]));
  const seen = new Set<string>();
  const reconciled: WorkspaceOverview[] = [];
  for (const item of pageItems) {
    if (seen.has(item.workspace_id)) {
      continue;
    }
    seen.add(item.workspace_id);
    const prior = currentById.get(item.workspace_id);
    reconciled.push(prior && JSON.stringify(prior) === JSON.stringify(item) ? prior : item);
  }
  for (const item of current) {
    if (!seen.has(item.workspace_id)) {
      seen.add(item.workspace_id);
      reconciled.push(item);
    }
  }
  return reconciled;
}

/** Split already-loaded rows outside the refreshed first page into bounded ID batches. */
export function retainedOverviewIdBatches(
  current: WorkspaceOverview[],
  pageItems: WorkspaceOverview[],
): string[][] {
  const seen = new Set(pageItems.map((item) => item.workspace_id));
  const retainedIds: string[] = [];
  for (const item of current) {
    if (seen.has(item.workspace_id)) {
      continue;
    }
    seen.add(item.workspace_id);
    retainedIds.push(item.workspace_id);
  }
  const batches: string[][] = [];
  for (let index = 0; index < retainedIds.length; index += OVERVIEW_REFRESH_BATCH_SIZE) {
    batches.push(retainedIds.slice(index, index + OVERVIEW_REFRESH_BATCH_SIZE));
  }
  return batches;
}

/**
 * Install fresh projections for the retained history without moving its rows.
 * Missing IDs are removed; byte-equivalent rows retain object identity so
 * memoized cards do not rerender on every poll.
 */
export function reconcileOverviewRetainedItems(
  current: WorkspaceOverview[],
  pageItems: WorkspaceOverview[],
  refreshedItems: WorkspaceOverview[],
  missingWorkspaceIds: string[],
): WorkspaceOverview[] {
  const firstPageIds = new Set(pageItems.map((item) => item.workspace_id));
  const refreshedById = new Map(refreshedItems.map((item) => [item.workspace_id, item]));
  const missingIds = new Set(missingWorkspaceIds);
  return reconcileOverviewFirstPage(current, pageItems).flatMap((item) => {
    if (firstPageIds.has(item.workspace_id)) {
      return [item];
    }
    const refreshed = refreshedById.get(item.workspace_id);
    if (refreshed) {
      return JSON.stringify(item) === JSON.stringify(refreshed) ? [item] : [refreshed];
    }
    return missingIds.has(item.workspace_id) ? [] : [item];
  });
}

// Accumulate overview rows across pages until exhaustion or the page ceiling.
// ``fetchPage`` returns ``null`` to signal failure or caller abort; that
// short-circuits to ``null`` so the dashboard can distinguish apply vs discard.
// Hitting the ceiling with ``has_more`` still true, or a contradictory envelope
// (has_more without a continuation cursor), returns ``truncated: true`` so
// callers must surface continuation rather than treat the prefix as complete.
export async function collectOverviewPages(
  fetchPage: (cursor: string | null) => Promise<OverviewListPage | null>,
): Promise<OverviewPageCollection | null> {
  const collected: WorkspaceOverview[] = [];
  let cursor: string | null = null;
  for (let page = 0; page < OVERVIEW_LIST_MAX_PAGES; page += 1) {
    const data = await fetchPage(cursor);
    if (data === null) {
      return null;
    }
    collected.push(...data.items);
    if (!data.has_more) {
      return complete(collected);
    }
    // has_more without a usable cursor cannot be followed. Do not install the
    // prefix as complete — later workspaces would stay unreachable with no warning.
    // Blank or whitespace cursors are the same contradictory envelope as null/omitted.
    if (!usableContinuationCursor(data.next_cursor)) {
      return truncated(collected, "missing_cursor");
    }
    cursor = data.next_cursor;
  }
  return truncated(collected, "page_ceiling");
}
