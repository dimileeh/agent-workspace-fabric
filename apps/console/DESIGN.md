# Design System — AWF Console

Authored via a design consultation (`/gstack-design-consultation`). This is the source of truth
for the console's look and feel. Read it before any UI change; do not hardcode palette values and
never convey status by color alone.

## Product Context

- **What this is:** local operator control room for Agent Workspace Fabric — monitoring fleets of
  AI coding agents across isolated workspaces (lifecycle, validation freshness, merge queue,
  capacity, failures, live logs).
- **Who it is for:** operators triaging many concurrent agent workspaces.
- **Project type:** real-time monitoring / observability console (Next.js 16, React 19, Tailwind v4).
- **Peers:** Grafana, Datadog, Temporal Web, k9s/Lens.

## Memorable Thing

A serious, trustworthy control room. Calm, dense, information-first, never flashy.

## Aesthetic Direction

- Industrial / high-performance HMI (ISA-101 inspired). Decoration does no work; type, spacing,
  surface elevation, and semantic color do.
- No gradients, glow, or imagery. Sharp, precise, instrument-grade.

## Principles

- **Three layers:** Status (is the fleet ok? <5s glance, 5–7 KPIs, top-left priority) → Diagnosis
  (why?) → Action (what do I do?). Progressive disclosure: detail lives in the inspector drawer.
- **Semantic color carries meaning, never decoration**; never color-alone — pair every status with
  a glyph + label (`statusGlyph`/`toneGlyph` in `lib/format.ts`).
- **Dim/flag stale data** so operators never act on outdated readings (`data-awf-stale`).
- WCAG 2.2 AA baseline; keep high-contrast + large-font + reduced-motion modes.

## Typography

- UI/body: **IBM Plex Sans** (self-hosted via `next/font`, `--font-plex-sans`).
- Mono (IDs, SHAs, logs, numerics): **IBM Plex Mono** (`--font-plex-mono`, `.mono`).
- All metrics use `font-variant-numeric: tabular-nums` (`.mono`, `.tnum`, `.kpi-value`).
- Root font-size 100% (112.5% in large-font mode). Dense scale: labels 10–11px caps
  (`.label-caps`), body 12–14px, KPI values 22px (`.kpi-value`), titles 16px.

## Color (semantic tokens — one source of truth in `app/globals.css`)

Every theme (light / dark / high-contrast, ×2) re-declares the same variables; Tailwind utilities
are generated from them via `@theme inline` (`bg-surface`, `text-fg-muted`, `border-line`,
`text-info`, `bg-pr-soft`, …). Legacy aliases (`--background`, `--panel`, `--border`, …) are kept
so existing arbitrary-value utilities resolve against the same palette.

Roles: `canvas`, `surface`, `surface-2`, `elevated`; `fg`, `fg-strong`, `fg-muted`, `fg-faint`;
`line`, `line-strong`; `accent` (+soft); status `info`/`healthy`/`attention`/`danger`/`pr`/`idle`,
each with `main` / `soft` / `border` / `text`.

Status uses vivid, solid tints (not faint overlays) so green/blue/amber/red read clearly. Each
status keeps `main` (dots/sparklines/values) + `soft` (chip fill) + `border` + `text`.

Dark (default-quality, calm low-glare canvas; vivid status):

- canvas `#0b0e14`, surface `#111722`, surface-2 `#161d2b`, elevated `#1b2433`
- fg `rgb(226 232 240)` (desaturated, not pure white), muted 66%, faint 42%
- line `rgb(226 232 240 / 14%)`, line-strong 28%
- accent `#5b9dff` · info/`running`/`monitoring_pr` blue `#5b9dff` (soft `#102b47`) ·
  healthy/`completed` green `#34d399` (soft `#0d2f24`) · attention/`cancelled` amber `#f4c86a`
  (soft `#322507`) · danger/`failed` red `#fb8b83` (soft `#3a1111`) · idle `#8896a8`

Light: canvas `#f5f7fa`, surface `#fff`, surface-2 `#eef2f7`; fg `#16202b`, muted `#5a6675`;
accent/info `#2f6fe0` · healthy `#15875a` · attention `#9a6400` · danger `#c0392f`.

High-contrast: per-role overrides (yellow accent on dark, etc.) — preserve existing behavior.

## Status Glyphs (color + glyph + label)

`running ●` · `requested/ready ◷` · `monitoring_pr ◆` · `completed ✓` · `failed/destroyed ✕` ·
`cancelled ⊘` · `destroying ◌` · `stale/attention ⚠`. Source: `statusGlyph` in `lib/format.ts`.

## Spacing & Radius

Base 4px (Tailwind scale). Radius: controls/chips `--radius-control` 4px, panels
`--radius-panel` 8px, pills/dots full.

## Layout (Status → Diagnosis → Action)

- **Top (Status):** `TopBar` (`<header>` with brand, API/Stream pills, refresh, preferences) plus
  the `FleetHealthStrip` KPI band.
- **Section nav:** `SectionNav` is a sticky jump bar shown **only on narrow screens** (`xl:hidden`),
  where panels stack into one column. Wide screens lay panels out side by side and need none.
- **Main (Diagnosis):** workspace list + capacity / merge-queue / failure panels.
- **Inspector drawer (Action):** operator controls, lifecycle rail, validation freshness, logs.

## Motion

Minimal-functional. ~120ms ease-out transitions; the only "alive" motion is the live-stream pulse.
Respect `prefers-reduced-motion` (already enforced globally).

## Components

`KpiStat` · `Badge` (glyph + color + label) · `Panel` (elevation + stale state) ·
`Fact` · `Chip` / `QueueChip` · `LifecycleRail`.

## Decisions Log

- 2026-06 Initial system via `/gstack-design-consultation`. System-default theme with dark-first
  quality, IBM Plex, semantic tokens replacing hardcoded slate hex, three-layer IA.
- 2026-06 Revision: vivid solid status tints (green completed / amber cancelled / red failed /
  blue running + monitoring_pr); `monitoring_pr` stays blue with a distinct `◆` glyph rather than
  a separate hue. Section navigation is a narrow-screen-only sticky jump bar (the wide layout shows
  panels side by side and needs none).
- 2026-06 Removed KPI/capacity sparklines (sparse client-side history made them noisy) and the
  comfortable/compact density toggle (negligible visual effect).
