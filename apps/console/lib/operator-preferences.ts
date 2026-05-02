export const OPERATOR_PREFERENCES_STORAGE_KEY = "awf.operator.preferences.v1";
export const OPERATOR_PREFERENCES_VERSION = 1;

export type OperatorTheme = "light" | "dark";
export type OperatorContrast = "normal" | "high";
export type OperatorFontSize = "standard" | "large";

export type OperatorPreferences = {
  theme: OperatorTheme;
  contrast: OperatorContrast;
  fontSize: OperatorFontSize;
};

export const DEFAULT_OPERATOR_PREFERENCES: OperatorPreferences = {
  theme: "light",
  contrast: "normal",
  fontSize: "standard",
};

export type OperatorPreferenceAttributes = {
  "data-awf-theme": OperatorTheme;
  "data-awf-contrast": OperatorContrast;
  "data-awf-font-size": OperatorFontSize;
};

export function normalizeOperatorPreferences(value: unknown): OperatorPreferences {
  if (!isRecord(value)) {
    return { ...DEFAULT_OPERATOR_PREFERENCES };
  }

  return {
    theme: value.theme === "dark" || value.theme === "light" ? value.theme : "light",
    contrast: value.contrast === "high" || value.contrast === "normal" ? value.contrast : "normal",
    fontSize: value.fontSize === "large" || value.fontSize === "standard" ? value.fontSize : "standard",
  };
}

export function decodeOperatorPreferences(raw: string | null | undefined): OperatorPreferences {
  if (!raw) {
    return { ...DEFAULT_OPERATOR_PREFERENCES };
  }
  try {
    return normalizeOperatorPreferences(JSON.parse(raw) as unknown);
  } catch {
    return { ...DEFAULT_OPERATOR_PREFERENCES };
  }
}

export function encodeOperatorPreferences(preferences: OperatorPreferences): string {
  const normalized = normalizeOperatorPreferences(preferences);
  return JSON.stringify({
    version: OPERATOR_PREFERENCES_VERSION,
    theme: normalized.theme,
    contrast: normalized.contrast,
    fontSize: normalized.fontSize,
  });
}

export function operatorPreferenceAttributes(
  preferences: OperatorPreferences,
): OperatorPreferenceAttributes {
  const normalized = normalizeOperatorPreferences(preferences);
  return {
    "data-awf-theme": normalized.theme,
    "data-awf-contrast": normalized.contrast,
    "data-awf-font-size": normalized.fontSize,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
