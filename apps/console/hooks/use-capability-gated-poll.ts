"use client";

import { useEffect } from "react";
import { pollMs } from "@/components/console-dashboard-shared";

/** Poll ``load`` on mount and every ``pollMs`` while ``enabled``; no-op when gated off. */
export function useCapabilityGatedPoll(
  enabled: boolean,
  load: () => void | Promise<void>,
): void {
  useEffect(() => {
    if (!enabled) {
      return;
    }
    void load();
    const interval = window.setInterval(() => void load(), pollMs);
    return () => window.clearInterval(interval);
  }, [enabled, load]);
}
