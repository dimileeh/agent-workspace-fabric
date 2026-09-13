/** Telemetry SVG geometry and resource-meter formatting. */

import type { TelemetryQuality } from "./console-workspace-telemetry.ts";

/** Max SVG points drawn for a telemetry sparkline after downsampling. */
export const MAX_SPARKLINE_POINTS = 64;

/**
 * Evenly downsample a sorted series for sparkline rendering.
 * Always preserves first and last points when maxPoints >= 2.
 */
export function downsampleSeriesForSparkline<T>(
  points: readonly T[],
  maxPoints: number = MAX_SPARKLINE_POINTS,
): T[] {
  if (points.length <= maxPoints) {
    return points.slice();
  }
  if (maxPoints <= 0) {
    return [];
  }
  if (maxPoints === 1) {
    return [points[points.length - 1]!];
  }
  const out: T[] = [];
  const last = points.length - 1;
  let prevIdx = -1;
  for (let i = 0; i < maxPoints; i++) {
    const idx = Math.round((i * last) / (maxPoints - 1));
    if (idx === prevIdx) {
      continue;
    }
    out.push(points[idx]!);
    prevIdx = idx;
  }
  return out;
}

type SparklineDownsamplePoint = {
  value: number;
  quality: TelemetryQuality;
  /** Index in the pre-downsample series; locates timestamps and quality breaks. */
  originalIndex: number;
};

/**
 * Downsample sparkline samples for SVG while preferring non-ok representation.
 * Plain even sampling can drop partial/stale samples and erase quality gaps;
 * when non-ok + endpoints exceed maxPoints, keep a bounded representative
 * subset of non-ok indices (never more than maxPoints total).
 * Reserves series value min/max before non-ok / even fill so a sole peak
 * skipped by sampling cannot flatten the chart (geometry scales from
 * retained points only), even when non-ok samples would otherwise saturate
 * the point budget.
 * Returned points carry originalIndex so geometry can keep the time grid after
 * ok anchors are evicted.
 */
function downsampleSparklinePreservingQuality(
  points: readonly SparklineInputPoint[],
  maxPoints: number = MAX_SPARKLINE_POINTS,
): SparklineDownsamplePoint[] {
  const asDownsamplePoint = (idx: number): SparklineDownsamplePoint => ({
    value: points[idx]!.value,
    quality: sparklinePointQuality(points[idx]!),
    originalIndex: idx,
  });

  if (points.length <= maxPoints) {
    return points.map((_, idx) => asDownsamplePoint(idx));
  }
  if (maxPoints <= 0) {
    return [];
  }
  if (maxPoints === 1) {
    return [asDownsamplePoint(points.length - 1)];
  }

  const last = points.length - 1;
  const keep = new Set<number>([0, last]);

  // Even sampling (and non-ok representative fill) can skip a sole peak
  // (e.g. n=65, max=64 skips index 32). Reserve extrema before consuming the
  // interior budget so saturation events stay visible; geometry min/max are
  // derived from retained points only.
  if (points.length > 0 && keep.size < maxPoints) {
    let minIdx = 0;
    let maxIdx = 0;
    for (let i = 1; i < points.length; i++) {
      const v = points[i]!.value;
      if (v < points[minIdx]!.value) {
        minIdx = i;
      }
      if (v > points[maxIdx]!.value) {
        maxIdx = i;
      }
    }
    // Prefer the peak when the remaining budget allows only one extrema slot.
    keep.add(maxIdx);
    if (keep.size < maxPoints) {
      keep.add(minIdx);
    }
  }

  const nonOkInterior: number[] = [];
  for (let i = 1; i < last; i++) {
    if (sparklinePointQuality(points[i]!) !== "ok" && !keep.has(i)) {
      nonOkInterior.push(i);
    }
  }

  const interiorBudget = maxPoints - keep.size;
  if (nonOkInterior.length <= interiorBudget) {
    for (const idx of nonOkInterior) {
      keep.add(idx);
    }
  } else if (interiorBudget > 0) {
    for (const idx of downsampleSeriesForSparkline(nonOkInterior, interiorBudget)) {
      keep.add(idx);
    }
  }

  if (keep.size < maxPoints) {
    const evenIdx = downsampleSeriesForSparkline(
      Array.from({ length: points.length }, (_, i) => i),
      maxPoints,
    );
    for (const idx of evenIdx) {
      if (keep.size >= maxPoints) {
        break;
      }
      keep.add(idx);
    }
  }

  return [...keep].sort((a, b) => a - b).map(asDownsamplePoint);
}

export type SparklinePath = {
  d: string;
  quality: TelemetryQuality;
};

export type SparklineMarker = {
  x: number;
  y: number;
  quality: TelemetryQuality;
};

export type SparklineGeometry = {
  w: number;
  h: number;
  /** Contiguous same-quality runs with 2+ samples (gaps between quality changes). */
  paths: SparklinePath[];
  /** Single-sample runs (including a lone series point). */
  markers: SparklineMarker[];
  /** Worst non-ok quality present, or null when every point is ok. */
  qualification: "partial" | "stale" | null;
};

type SparklineInputPoint = {
  value: number;
  sampleTime?: string;
  quality?: TelemetryQuality;
};

function sparklinePointQuality(point: SparklineInputPoint): TelemetryQuality {
  return point.quality ?? "ok";
}

function worstSparklineQualification(
  qualities: readonly TelemetryQuality[],
): "partial" | "stale" | null {
  let worst: "partial" | "stale" | null = null;
  for (const quality of qualities) {
    if (quality === "stale") {
      return "stale";
    }
    if (quality === "partial") {
      worst = "partial";
    }
  }
  return worst;
}

/** True when any original sample strictly between fromIdx and toIdx differs in quality. */
function hasOriginalQualityBreak(
  points: readonly SparklineInputPoint[],
  fromIdx: number,
  toIdx: number,
  runQuality: TelemetryQuality,
): boolean {
  for (let j = fromIdx + 1; j < toIdx; j++) {
    if (sparklinePointQuality(points[j]!) !== runQuality) {
      return true;
    }
  }
  return false;
}

function pathDFromCoords(coords: readonly string[]): string {
  return `M ${coords.join(" L ")}`;
}

/**
 * Map a sorted value series into SVG sparkline geometry.
 * Contiguous same-quality runs stay connected; quality transitions leave gaps.
 * One-sample runs become markers. Non-ok history is never a single unqualified path.
 * Qualification uses the full series; SVG downsampling prefers non-ok samples
 * within the point budget. X uses elapsed time across the original series;
 * untimed inputs retain index spacing. Original indices preserve timestamps
 * and prevent joining previously gapped non-ok runs after downsampling.
 */
export function buildSparklineGeometry(
  points: readonly SparklineInputPoint[],
  width = 120,
  height = 28,
): SparklineGeometry | null {
  if (points.length === 0) {
    return null;
  }
  const qualification = worstSparklineQualification(points.map(sparklinePointQuality));
  const series = downsampleSparklinePreservingQuality(points);
  const lastOrig = points.length - 1;

  if (series.length === 1) {
    return {
      w: width,
      h: height,
      paths: [],
      markers: [{ x: width / 2, y: height / 2, quality: series[0]!.quality }],
      qualification,
    };
  }

  const times = points.map((p) => Date.parse(p.sampleTime ?? ""));
  const hasTimes = times.every(Number.isFinite);
  const startTime = times[0]!;
  const timeSpan = times[lastOrig]! - startTime;
  let min = series[0]!.value;
  let max = series[0]!.value;
  for (let i = 1; i < series.length; i++) {
    const v = series[i]!.value;
    if (v < min) {
      min = v;
    }
    if (v > max) {
      max = v;
    }
  }
  const span = max - min || 1;
  const coords = series.map((p) => {
    const fraction = hasTimes
      ? timeSpan === 0 ? 0.5 : (times[p.originalIndex]! - startTime) / timeSpan
      : p.originalIndex / lastOrig;
    const x = fraction * width;
    const y = height - ((p.value - min) / span) * (height - 4) - 2;
    return {
      x,
      y,
      text: `${x.toFixed(1)},${y.toFixed(1)}`,
      quality: p.quality,
      originalIndex: p.originalIndex,
    };
  });

  const paths: SparklinePath[] = [];
  const markers: SparklineMarker[] = [];
  let runStart = 0;
  for (let i = 1; i <= coords.length; i++) {
    const qualityBreak = i === coords.length || coords[i]!.quality !== coords[runStart]!.quality;
    // Index gaps from downsampling must not join distinct original quality runs
    // (e.g. two partial clusters separated by ok samples).
    const originalRunBreak =
      !qualityBreak &&
      hasOriginalQualityBreak(
        points,
        coords[i - 1]!.originalIndex,
        coords[i]!.originalIndex,
        coords[runStart]!.quality,
      );
    if (!qualityBreak && !originalRunBreak) {
      continue;
    }
    const run = coords.slice(runStart, i);
    const quality = run[0]!.quality;
    if (run.length === 1) {
      markers.push({ x: run[0]!.x, y: run[0]!.y, quality });
    } else {
      paths.push({
        d: pathDFromCoords(run.map((c) => c.text)),
        quality,
      });
    }
    runStart = i;
  }

  return {
    w: width,
    h: height,
    paths,
    markers,
    qualification,
  };
}

/**
 * Format CPU cores for the resource meter.
 * Sub-centicore samples (e.g. 0.004) must not collapse to "0 cores" via toFixed(2).
 * Sub-0.005 millicore samples must not collapse to "0 millicores" either.
 */
export function formatCores(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  const abs = Math.abs(value);
  if (abs > 0 && abs < 0.01) {
    return `${formatScaledDecimal(value * 1000)} millicores`;
  }
  return `${formatScaledDecimal(value)} cores`;
}

/** Compact decimal text; never round a nonzero finite value to a literal "0". */
function formatScaledDecimal(value: number): string {
  if (Number.isInteger(value)) {
    return String(value);
  }
  const fixed2 = value.toFixed(2).replace(/\.?0+$/, "");
  if (fixed2 !== "" && Number(fixed2) !== 0) {
    return fixed2;
  }
  // toFixed(2) collapsed a nonzero reading (e.g. 0.001 → "0.00").
  // Keep enough fractional digits that the display stays nonzero.
  // toFixed rejects digits > 100; values below that fixed-point range
  // need an exponential fallback so the meter does not throw.
  const abs = Math.abs(value);
  const fractionDigits = Math.max(2, Math.ceil(-Math.log10(abs)) + 1);
  if (fractionDigits > 100) {
    return value.toExponential(2).replace(/\.?0+e/, "e");
  }
  return value.toFixed(fractionDigits).replace(/\.?0+$/, "");
}
