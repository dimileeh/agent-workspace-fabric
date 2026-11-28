"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ReadonlyURLSearchParams } from "next/navigation";
import type { OperatorPreferences, ResolvedOperatorTheme } from "@/lib/operator-preferences";
import {
  DEFAULT_OPERATOR_PREFERENCES,
  OPERATOR_PREFERENCES_STORAGE_KEY,
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
export function useOperatorThemePreferences(embedded: boolean) {
  const [operatorPreferences, setOperatorPreferences] = useState<OperatorPreferences>(
    DEFAULT_OPERATOR_PREFERENCES,
  );
  const operatorPreferencesRef = useRef(DEFAULT_OPERATOR_PREFERENCES);
  const [operatorPreferencesHydrated, setOperatorPreferencesHydrated] = useState(false);
  const [systemTheme, setSystemTheme] = useState<ResolvedOperatorTheme>("light");

  useEffect(() => {
    const readPreferences = () => {
      const preferences = readStoredOperatorPreferences();
      operatorPreferencesRef.current = preferences;
      setOperatorPreferences(preferences);
    };
    readPreferences();
    setOperatorPreferencesHydrated(true);
    if (!embedded) return;
    const syncPreferences = (event: StorageEvent) => {
      if ((event.key === OPERATOR_PREFERENCES_STORAGE_KEY || event.key === null) &&
          event.storageArea === window.localStorage) {
        readPreferences();
      }
    };
    window.addEventListener("storage", syncPreferences);
    return () => window.removeEventListener("storage", syncPreferences);
  }, [embedded]);

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
    if (!embedded) writeStoredOperatorPreferences(operatorPreferences);
  }, [embedded, operatorPreferences, operatorPreferencesHydrated, systemTheme]);

  const updateOperatorPreferences = useCallback((next: Partial<OperatorPreferences>) => {
    const preferences = normalizeOperatorPreferences({ ...operatorPreferencesRef.current, ...next });
    operatorPreferencesRef.current = preferences;
    setOperatorPreferences(preferences);
    if (embedded) writeStoredOperatorPreferences(preferences);
  }, [embedded]);

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
