"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ReadonlyURLSearchParams } from "next/navigation";
import type { OperatorPreferences, ResolvedOperatorTheme } from "@/lib/operator-preferences";
import {
  DEFAULT_OPERATOR_PREFERENCES,
  normalizeOperatorPreferences,
} from "@/lib/operator-preferences";
import {
  applyOperatorPreferenceAttributes,
  readStoredOperatorPreferences,
  writeStoredOperatorPreferences,
} from "@/components/console-dashboard-shared";

/**
 * Hydrate, persist, and apply operator theme preferences.
 * Extracted from console-dashboard.tsx for the first-party 1500-line guard.
 */
export function useOperatorThemePreferences() {
  const [operatorPreferences, setOperatorPreferences] = useState<OperatorPreferences>(
    DEFAULT_OPERATOR_PREFERENCES,
  );
  const [operatorPreferencesHydrated, setOperatorPreferencesHydrated] = useState(false);
  const [systemTheme, setSystemTheme] = useState<ResolvedOperatorTheme>("light");

  useEffect(() => {
    setOperatorPreferences(readStoredOperatorPreferences());
    setOperatorPreferencesHydrated(true);
  }, []);

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const updateSystemTheme = () => setSystemTheme(media.matches ? "dark" : "light");
    updateSystemTheme();
    media.addEventListener("change", updateSystemTheme);
    return () => media.removeEventListener("change", updateSystemTheme);
  }, []);

  useEffect(() => {
    if (!operatorPreferencesHydrated) {
      return;
    }
    applyOperatorPreferenceAttributes(operatorPreferences, systemTheme);
    writeStoredOperatorPreferences(operatorPreferences);
  }, [operatorPreferences, operatorPreferencesHydrated, systemTheme]);

  const updateOperatorPreferences = useCallback((next: Partial<OperatorPreferences>) => {
    setOperatorPreferences((current) => normalizeOperatorPreferences({ ...current, ...next }));
  }, []);

  return { operatorPreferences, updateOperatorPreferences };
}

/**
 * Keep selected workspace id in sync with the page URL (query ↔ state).
 * Extracted from console-dashboard.tsx for the first-party 1500-line guard.
 */
export function useWorkspaceSelectionUrl(
  searchParams: ReadonlyURLSearchParams,
  initialWorkspaceId: string | null,
) {
  const [selectedId, setSelectedIdState] = useState<string | null>(initialWorkspaceId);
  const selectedIdRef = useRef<string | null>(selectedId);

  const setSelectedId = useCallback((workspaceId: string | null) => {
    selectedIdRef.current = workspaceId;
    setSelectedIdState(workspaceId);
  }, []);

  useEffect(() => {
    const urlWorkspaceId = searchParams.get("workspaceId");
    if (selectedIdRef.current !== urlWorkspaceId) {
      setSelectedId(urlWorkspaceId);
    }
  }, [searchParams, setSelectedId]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const currentParam = params.get("workspaceId");
    if (selectedId !== currentParam) {
      if (selectedId) {
        params.set("workspaceId", selectedId);
      } else {
        params.delete("workspaceId");
      }
      const newQuery = params.toString();
      window.history.replaceState(null, "", newQuery ? `?${newQuery}` : window.location.pathname);
    }
  }, [selectedId]);

  return { selectedId, selectedIdRef, setSelectedId };
}
