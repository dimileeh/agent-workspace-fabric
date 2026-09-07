import type { ListEnvelope, WorkspaceOverview } from "@/lib/types";
import { awfPath } from "./console-urls.ts";

// Overview list is cursor-paginated (API max 500). The dashboard rail, client
// search, multi-value filters, and log selection all consume the accumulated
// overview array, so a single page would hide later workspaces while fleet
// summary KPIs still report totals. Request 100 per page and follow next_cursor.
export const OVERVIEW_LIST_PAGE_SIZE = 100;
// Defensive ceiling so a misbehaving cursor cannot loop forever;
// 100 * 50 covers 5k workspaces, beyond typical local/Core fleets.
export const OVERVIEW_LIST_MAX_PAGES = 50;

export type OverviewListFilters = {
  status?: string;
  agent?: string;
  repo_url?: string;
};

export type OverviewListPage = ListEnvelope<WorkspaceOverview>;

/** Result of accumulating overview pages; never pretend a capped prefix is complete. */
export type OverviewPageCollection = {
  items: WorkspaceOverview[];
  /**
   * True when more rows exist but pagination cannot continue: the page ceiling
   * stopped with has_more, or has_more was set without a continuation cursor.
   */
  truncated: boolean;
};

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
      return { items: collected, truncated: false };
    }
    // has_more without a usable cursor cannot be followed. Do not install the
    // prefix as complete — later workspaces would stay unreachable with no warning.
    if (!data.next_cursor) {
      return { items: collected, truncated: true };
    }
    cursor = data.next_cursor;
  }
  return { items: collected, truncated: true };
}
