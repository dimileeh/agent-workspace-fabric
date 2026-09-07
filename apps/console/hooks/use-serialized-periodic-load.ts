"use client";

import { useEffect, type MutableRefObject } from "react";

import { pollMs } from "@/components/console-dashboard-shared";

/**
 * Chain a load after the previous invocation settles, and skip a tick while
 * `inFlightRef` is set.
 *
 * A wall-clock interval that calls `load` every `pollMs` overlaps when a
 * request is slower than the interval. If each call advances a request
 * generation, every completion is discarded and the feed starves.
 *
 * Effect restart (selection, filters, caller identity) always invokes `load`
 * immediately so a newer request can supersede. Do not skip that start when
 * the latch is set — the in-flight call may belong to the previous key, and
 * skipping it would leave stale data until that old call finishes.
 */
export function useSerializedPeriodicLoad(
  enabled: boolean,
  // The result is ignored. Accept any promise so callers such as
  // loadCapabilities (Promise<ConsoleCapabilities | null>) stay chained until
  // settlement. Narrowing to Promise<void> is a type error, and wrapping the
  // call as `() => { void load(); }` would schedule the next tick immediately.
  load: () => void | Promise<unknown>,
  inFlightRef: MutableRefObject<boolean>,
  restartKey: string,
): void {
  useEffect(() => {
    if (!enabled) {
      return;
    }
    let cancelled = false;
    let timer = 0;

    const scheduleNext = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        if (cancelled) {
          return;
        }
        // Still in flight: wait another pollMs. Starting `load` here would
        // advance generation and discard the unfinished request.
        if (inFlightRef.current) {
          scheduleNext();
          return;
        }
        start();
      }, pollMs);
    };

    const start = () => {
      void Promise.resolve(load()).finally(() => {
        if (!cancelled) {
          scheduleNext();
        }
      });
    };

    start();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [enabled, load, inFlightRef, restartKey]);
}
