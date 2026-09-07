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
export async function collectOverviewPages(
  fetchPage: (cursor: string | null) => Promise<OverviewListPage | null>,
): Promise<WorkspaceOverview[] | null> {
  const collected: WorkspaceOverview[] = [];
  let cursor: string | null = null;
  for (let page = 0; page < OVERVIEW_LIST_MAX_PAGES; page += 1) {
    const data = await fetchPage(cursor);
    if (data === null) {
      return null;
    }
    collected.push(...data.items);
    if (!data.has_more || !data.next_cursor) {
      break;
    }
    cursor = data.next_cursor;
  }
  return collected;
}
