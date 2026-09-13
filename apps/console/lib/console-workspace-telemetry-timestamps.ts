/** Exact timestamp operations shared by telemetry parsing and projection. */

export const RFC3339_DATE_TIME =
  /^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(\.\d+)?([Zz]|[+-](\d{2}):(\d{2}))$/;

/** Epoch ms for a parsed RFC3339 sample timestamp (already validated upstream). */
export function timestampInstantMs(value: string): number {
  return Date.parse(value);
}

/**
 * Fractional digits beyond milliseconds (Date.parse precision). Trailing zeros
 * are stripped so `.000001` and `.000001000` share identity.
 */
function submillisecondFraction(value: string): string {
  const fractionalSeconds = RFC3339_DATE_TIME.exec(value)?.[7] ?? "";
  return fractionalSeconds.slice(4).replace(/0+$/, "");
}

/**
 * Exact instant identity for partitioning: epoch ms + sub-ms fraction.
 * Equates Z / offset spellings of the same UTC moment; keeps distinct
 * sub-millisecond instants that Date.parse would otherwise collapse.
 */
export function timestampInstantKey(value: string): string {
  return `${timestampInstantMs(value)}\0${submillisecondFraction(value)}`;
}

/**
 * Order two validated RFC3339 instants, including sub-millisecond fraction.
 * An optional integer-ms shift of the right instant preserves its fraction.
 */
export function compareTimestampInstants(
  left: string,
  right: string,
  rightOffsetMs = 0,
): number {
  const leftMs = timestampInstantMs(left);
  const rightMs = timestampInstantMs(right) + rightOffsetMs;
  if (leftMs !== rightMs) {
    return leftMs < rightMs ? -1 : 1;
  }
  const leftFrac = submillisecondFraction(left);
  const rightFrac = submillisecondFraction(right);
  const precision = Math.max(leftFrac.length, rightFrac.length);
  const normalizedLeft = leftFrac.padEnd(precision, "0");
  const normalizedRight = rightFrac.padEnd(precision, "0");
  return normalizedLeft === normalizedRight
    ? 0
    : normalizedLeft < normalizedRight
      ? -1
      : 1;
}

/**
 * True when sampleTime falls in [intervalStart, intervalEnd] inclusive.
 * Callers must already reject reversed windows (start > end).
 */
export function isSampleTimeWithinMeasurementInterval(
  sampleTime: string,
  intervalStart: string,
  intervalEnd: string,
): boolean {
  return (
    compareTimestampInstants(sampleTime, intervalStart) >= 0 &&
    compareTimestampInstants(sampleTime, intervalEnd) <= 0
  );
}
