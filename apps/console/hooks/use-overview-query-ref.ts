"use client";

import { useEffect, useRef } from "react";

/**
 * Keep overview list query filters in a ref so `loadOverview` stays referentially
 * stable across filter edits (capability polling must not restart).
 * Extracted from console-dashboard.tsx for the 1500-line maintainability guard
 * and to sync the ref in an effect (react-hooks/refs forbids render assignment).
 */
export function useOverviewQueryRef(
  statusFilters: string[],
  agentFilters: string[],
  repoFilter: string,
) {
  const overviewQueryRef = useRef({ statusFilters, agentFilters, repoFilter });
  useEffect(() => {
    overviewQueryRef.current = { statusFilters, agentFilters, repoFilter };
  }, [statusFilters, agentFilters, repoFilter]);
  return overviewQueryRef;
}
