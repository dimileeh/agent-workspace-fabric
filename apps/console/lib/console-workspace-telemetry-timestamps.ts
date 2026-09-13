/** Exact timestamp operations shared by telemetry parsing and projection. */

export const RFC3339_DATE_TIME =
  /^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(\.\d+)?([Zz]|[+-](\d{2}):(\d{2}))$/;

/**
 * Bound timestamps before regex/Date.parse and fractional-second processing.
 * Nanosecond timestamps with offsets need ~35 chars; unlimited fractions would
 * multiply parsing and identity-key work across MAX_TELEMETRY_SAMPLES rows.
 */
export const MAX_RFC3339_TIMESTAMP_LENGTH = 64;

export function isFiniteTimestampString(value: string): boolean {
  // Reject before regex/Date.parse so a single overlong fractional-second
  // field cannot burn UI-thread time scanning an unbounded producer string.
  if (value.length > MAX_RFC3339_TIMESTAMP_LENGTH) {
    return false;
  }
  const match = RFC3339_DATE_TIME.exec(value);
  if (!match) {
    return false;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  const dt = new Date(Date.UTC(year, month - 1, day, hour, minute, second));
  if (
    dt.getUTCFullYear() !== year ||
    dt.getUTCMonth() !== month - 1 ||
    dt.getUTCDate() !== day ||
    dt.getUTCHours() !== hour ||
    dt.getUTCMinutes() !== minute ||
    dt.getUTCSeconds() !== second
  ) {
    return false;
  }
  const tzDesignator = match[8];
  if (tzDesignator !== "Z" && tzDesignator !== "z") {
    const tzHour = Number(match[9]);
    const tzMinute = Number(match[10]);
    if (tzHour > 23 || tzMinute > 59) {
      return false;
    }
  }
  return Number.isFinite(Date.parse(value));
}

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
