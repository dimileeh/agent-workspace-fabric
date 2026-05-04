export const OPERATOR_PREFERENCES_STORAGE_KEY = "awf.operator.preferences.v1";
export const OPERATOR_PREFERENCES_VERSION = 1;

export type OperatorTheme = "light" | "dark" | "system";
export type ResolvedOperatorTheme = "light" | "dark";
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
  "data-awf-theme": ResolvedOperatorTheme;
  "data-awf-theme-mode": OperatorTheme;
  "data-awf-contrast": OperatorContrast;
  "data-awf-font-size": OperatorFontSize;
};

export function normalizeOperatorPreferences(value: unknown): OperatorPreferences {
  if (!isRecord(value)) {
    return { ...DEFAULT_OPERATOR_PREFERENCES };
  }

  const theme = value.theme;
  const contrast = value.contrast;
  const fontSize = value.fontSize;

  return {
    theme: isOperatorTheme(theme) ? theme : DEFAULT_OPERATOR_PREFERENCES.theme,
    contrast: isOperatorContrast(contrast)
      ? contrast
      : DEFAULT_OPERATOR_PREFERENCES.contrast,
    fontSize: isOperatorFontSize(fontSize)
      ? fontSize
      : DEFAULT_OPERATOR_PREFERENCES.fontSize,
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
  systemTheme: ResolvedOperatorTheme = DEFAULT_OPERATOR_PREFERENCES.theme === "dark" ? "dark" : "light",
): OperatorPreferenceAttributes {
  const normalized = normalizeOperatorPreferences(preferences);
  return {
    "data-awf-theme": resolveOperatorTheme(normalized, systemTheme),
    "data-awf-theme-mode": normalized.theme,
    "data-awf-contrast": normalized.contrast,
    "data-awf-font-size": normalized.fontSize,
  };
}

export function resolveOperatorTheme(
  preferences: OperatorPreferences,
  systemTheme: ResolvedOperatorTheme,
): ResolvedOperatorTheme {
  const normalized = normalizeOperatorPreferences(preferences);
  return normalized.theme === "system" ? systemTheme : normalized.theme;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isOperatorTheme(value: unknown): value is OperatorTheme {
  return value === "dark" || value === "light" || value === "system";
}

function isOperatorContrast(value: unknown): value is OperatorContrast {
  return value === "high" || value === "normal";
}

function isOperatorFontSize(value: unknown): value is OperatorFontSize {
  return value === "large" || value === "standard";
}
