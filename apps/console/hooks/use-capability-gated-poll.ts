"use client";

import { useEffect } from "react";

import { pollMs } from "@/components/console-dashboard-shared";

/**
 * Poll ``load`` on enable and again only after the previous invocation settles.
 *
 * A wall-clock interval overlaps when an advertised feed is slower than
 * ``pollMs``. Every current caller advances a per-feed request generation and
 * discards responses superseded by the next tick, so a slow success never
 * applies and the panel stays empty or permanently stale.
 *
 * Effect restart (capabilities ready, ``load`` identity) still invokes
 * ``load`` immediately so a newer request can supersede. Explicit refreshes
 * call the loaders directly and are not serialized here.
 */
export function useCapabilityGatedPoll(
  enabled: boolean,
  load: () => void | Promise<void>,
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
        if (!cancelled) {
          start();
        }
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
  }, [enabled, load]);
}
