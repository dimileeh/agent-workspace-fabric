import type { Metadata, Viewport } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import {
  DEFAULT_OPERATOR_PREFERENCES,
  OPERATOR_PREFERENCES_STORAGE_KEY,
} from "@/lib/operator-preferences";
import "./globals.css";

const plexSans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-plex-sans",
  display: "swap",
});

const plexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-plex-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "AWF Console",
  description: "Local operator console for Agent Workspace Fabric.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

const preferenceBootScript = `
(() => {
  const defaults = ${JSON.stringify(DEFAULT_OPERATOR_PREFERENCES)};
  const validTheme = (value) => value === "light" || value === "dark" || value === "system";
  const validContrast = (value) => value === "normal" || value === "high";
  const validFontSize = (value) => value === "standard" || value === "large";
  const resolveTheme = (theme) => {
    if (theme !== "system") return theme;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  };
  try {
    const parsed = JSON.parse(window.localStorage.getItem(${JSON.stringify(OPERATOR_PREFERENCES_STORAGE_KEY)}) || "null") || {};
    const theme = validTheme(parsed.theme) ? parsed.theme : defaults.theme;
    const contrast = validContrast(parsed.contrast) ? parsed.contrast : defaults.contrast;
    const fontSize = validFontSize(parsed.fontSize) ? parsed.fontSize : defaults.fontSize;
    const root = document.documentElement;
    root.setAttribute("data-awf-theme", resolveTheme(theme));
    root.setAttribute("data-awf-theme-mode", theme);
    root.setAttribute("data-awf-contrast", contrast);
    root.setAttribute("data-awf-font-size", fontSize);
  } catch {
    const root = document.documentElement;
    root.setAttribute("data-awf-theme", defaults.theme);
    root.setAttribute("data-awf-theme-mode", defaults.theme);
    root.setAttribute("data-awf-contrast", defaults.contrast);
    root.setAttribute("data-awf-font-size", defaults.fontSize);
  }
})();
`;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${plexSans.variable} ${plexMono.variable}`}
      data-awf-theme={DEFAULT_OPERATOR_PREFERENCES.theme}
      data-awf-theme-mode={DEFAULT_OPERATOR_PREFERENCES.theme}
      data-awf-contrast={DEFAULT_OPERATOR_PREFERENCES.contrast}
      data-awf-font-size={DEFAULT_OPERATOR_PREFERENCES.fontSize}
      suppressHydrationWarning
    >
      <body>
        <script dangerouslySetInnerHTML={{ __html: preferenceBootScript }} />
        {children}
      </body>
    </html>
  );
}
