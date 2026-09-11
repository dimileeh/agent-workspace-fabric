"use client";

import { useCallback, type Dispatch, type MutableRefObject, type SetStateAction } from "react";

import {
  parseConsoleCapabilities,
  resolveCapabilityParseFailureClear,
  sameCapabilityNegotiation,
} from "@/lib/console-capabilities";
import { awfPath } from "@/lib/console-urls";
import type { ConsoleCapabilities } from "@/lib/types";
import { apiGet } from "@/components/console-dashboard-shared";

type UseConsoleCapabilitiesArgs = {
  invalidateAuthorizedFeedsIfContextChanged: () => boolean;
  clearAuthorizedConsoleFeeds: (options?: { clearCapabilities?: boolean; authDenied?: boolean }) => void;
  clearCapabilityGatedInventories: () => void;
  clearNewlyUnsupportedCapabilityFeeds: (
    previous: ConsoleCapabilities,
    next: ConsoleCapabilities,
  ) => void;
  loadOverview: () => Promise<void> | void;
  capabilityRequestGenerationRef: MutableRefObject<number>;
  configuredContextFingerprintRef: MutableRefObject<string | null>;
  capabilityLoadInFlightRef: MutableRefObject<boolean>;
  appliedCapabilityGenerationRef: MutableRefObject<number>;
  consoleAuthDeniedRef: MutableRefObject<boolean>;
  revokedCapabilityGenerationRef: MutableRefObject<number>;
  appliedCapabilityFailureGenerationRef: MutableRefObject<number>;
  appliedCapabilitiesRef: MutableRefObject<ConsoleCapabilities | null>;
  lastCapabilityIdentityKeyRef: MutableRefObject<string | null>;
  setCapabilityError: Dispatch<SetStateAction<string | null>>;
  setCapabilities: Dispatch<SetStateAction<ConsoleCapabilities | null>>;
  setCapabilitiesReady: Dispatch<SetStateAction<boolean>>;
};

/**
 * Capability negotiation for the console dashboard.
 * Extracted from console-dashboard.tsx for the first-party 1500-line guard.
 */
export function useConsoleCapabilities({
  invalidateAuthorizedFeedsIfContextChanged,
  clearAuthorizedConsoleFeeds,
  clearCapabilityGatedInventories,
  clearNewlyUnsupportedCapabilityFeeds,
  loadOverview,
  capabilityRequestGenerationRef,
  configuredContextFingerprintRef,
  capabilityLoadInFlightRef,
  appliedCapabilityGenerationRef,
  consoleAuthDeniedRef,
  revokedCapabilityGenerationRef,
  appliedCapabilityFailureGenerationRef,
  appliedCapabilitiesRef,
  lastCapabilityIdentityKeyRef,
  setCapabilityError,
  setCapabilities,
  setCapabilitiesReady,
}: UseConsoleCapabilitiesArgs) {
  const loadCapabilities = useCallback(async (): Promise<ConsoleCapabilities | null> => {
    // Soft tenant switches update the URL before capabilities return; clear
    // authorized surfaces immediately so prior-tenant rows/controls cannot linger.
    invalidateAuthorizedFeedsIfContextChanged();
    const generation = ++capabilityRequestGenerationRef.current;
    const contextFingerprint = configuredContextFingerprintRef.current;
    capabilityLoadInFlightRef.current = true;
    try {
      const result = await apiGet<ConsoleCapabilities>(awfPath("console/capabilities"));
      const applyAuthoritativeCapabilityDenial = (deniedGeneration: number, message: string) => {
        // A soft tenant switch already owns the console. An older context's
        // 401/403 must not latch denial onto the new fingerprint.
        if (contextFingerprint !== configuredContextFingerprintRef.current) {
          return;
        }
        // A newer successful negotiation already owns the console. A late
        // 401/403 from an older request must not clear it.
        if (deniedGeneration < appliedCapabilityGenerationRef.current) {
          return;
        }
        // This request started inside an already-applied denial window.
        // Raising the watermark here would reject a recovery request that
        // started after the original denial.
        if (
          consoleAuthDeniedRef.current &&
          deniedGeneration <= revokedCapabilityGenerationRef.current
        ) {
          return;
        }
        // Cover every capability request that has already started so an
        // in-flight refresh cannot restore cleared feeds. A request that
        // starts after this watermark may recover.
        revokedCapabilityGenerationRef.current = Math.max(
          revokedCapabilityGenerationRef.current,
          capabilityRequestGenerationRef.current,
        );
        clearAuthorizedConsoleFeeds({ clearCapabilities: true, authDenied: true });
        setCapabilityError(message);
        setCapabilities(null);
        setCapabilitiesReady(true);
      };
      const applyTransientCapabilityOutage = (failedGeneration: number, message: string) => {
        // A soft tenant switch already owns the console. An older context's
        // network/5xx must not latch an outage onto the new fingerprint.
        if (contextFingerprint !== configuredContextFingerprintRef.current) {
          return false;
        }
        // A newer successful negotiation already owns the console. A late
        // 5xx from an older request must not re-latch capabilityError.
        if (failedGeneration < appliedCapabilityGenerationRef.current) {
          return false;
        }
        // A newer outage already owns the warning.
        if (failedGeneration < appliedCapabilityFailureGenerationRef.current) {
          return false;
        }
        // A 401/403 already covers this generation. Do not replace the
        // authorization reason or restore cleared capabilities.
        if (
          failedGeneration <= revokedCapabilityGenerationRef.current ||
          consoleAuthDeniedRef.current
        ) {
          return false;
        }
        appliedCapabilityFailureGenerationRef.current = Math.max(
          appliedCapabilityFailureGenerationRef.current,
          failedGeneration,
        );
        // Re-check in the updater: a newer success or denial can settle after
        // this outage is queued.
        setCapabilityError((current) =>
          failedGeneration < appliedCapabilityGenerationRef.current ||
          failedGeneration < appliedCapabilityFailureGenerationRef.current ||
          consoleAuthDeniedRef.current
            ? current
            : message,
        );
        setCapabilitiesReady(true);
        // Transient capability-endpoint outage (5xx/network): keep the last successful
        // negotiation so fleet KPIs and inspector detail retain last-good snapshots
        // while the error is shown. Mutating controls fail closed via
        // capabilitiesForMutatingControls(capabilities, capabilityError) until
        // negotiation succeeds again. Auth denial and never-negotiated stay fail-closed.
        const retained = appliedCapabilitiesRef.current;
        if (retained === null) {
          setCapabilities(null);
        }
        return true;
      };
      const applyMissingCapabilityContract = (
        failedGeneration: number,
        applyClear: () => void,
        message: string,
      ): boolean => {
        // A soft tenant switch already owns the console. An older context's
        // 404 or malformed payload must not clear the new fingerprint.
        if (contextFingerprint !== configuredContextFingerprintRef.current) {
          return false;
        }
        // A newer successful negotiation already owns the console. A late
        // missing/invalid contract from an older request must not clear it.
        if (failedGeneration < appliedCapabilityGenerationRef.current) {
          return false;
        }
        // A newer outage or missing-contract response already owns the warning.
        if (failedGeneration < appliedCapabilityFailureGenerationRef.current) {
          return false;
        }
        // A 401/403 already covers this generation. Do not replace the
        // authorization reason or restore cleared capabilities.
        if (
          failedGeneration <= revokedCapabilityGenerationRef.current ||
          consoleAuthDeniedRef.current
        ) {
          return false;
        }
        appliedCapabilityFailureGenerationRef.current = Math.max(
          appliedCapabilityFailureGenerationRef.current,
          failedGeneration,
        );
        applyClear();
        // Re-check in the updater: a newer success or denial can settle after
        // this missing-contract response is queued.
        setCapabilityError((current) =>
          contextFingerprint !== configuredContextFingerprintRef.current ||
          failedGeneration < appliedCapabilityGenerationRef.current ||
          failedGeneration < appliedCapabilityFailureGenerationRef.current ||
          consoleAuthDeniedRef.current
            ? current
            : message,
        );
        setCapabilitiesReady(true);
        return true;
      };
      // Apply even if a newer request has started but has not yet established
      // recovery. A newer request merely starting, hanging, or failing
      // transiently is not recovery.
      if (!result.ok && (result.status === 401 || result.status === 403)) {
        applyAuthoritativeCapabilityDenial(generation, result.message);
        return null;
      }
      // Transient failures follow the same rule as 401/403: suppress only after
      // a newer successful negotiation has applied. Discarding a completed
      // network/5xx only because Refresh started a newer request leaves
      // capabilityError null, so retained capabilities keep enabling mutating
      // controls if that newer request hangs.
      if (!result.ok && result.status !== 404) {
        const applied = applyTransientCapabilityOutage(generation, result.message);
        return applied ? appliedCapabilitiesRef.current : null;
      }
      // Missing/rolled-back negotiation: clear gated inventories so optional
      // feeds stop polling, without wiping legacy-safe workspace navigation
      // (CONSOLE_BACKEND_CONTRACT — no inferred privileges). Apply even if
      // Refresh has only started a newer request. Suppress only after a newer
      // successful negotiation has applied — a hang is not recovery, and
      // discarding this 404 leaves capabilityError null so retained capabilities
      // keep enabling mutating controls.
      if (!result.ok && result.status === 404) {
        applyMissingCapabilityContract(generation, () => {
          clearCapabilityGatedInventories();
        }, result.message);
        return null;
      }
      // The branches above cover every !ok status. This guard is the
      // discriminant narrow so result.data is only read on a successful envelope.
      if (!result.ok) {
        return null;
      }
      const parsed = parseConsoleCapabilities(result.data);
      if (!parsed.ok) {
        // Identity is extracted independently of inventory malformations. Preserve
        // legacy-safe overview nav only when the failed payload still carries the
        // same trusted identity; otherwise wipe authorized feeds and advance the
        // epoch so late prior-tenant overview rows cannot apply
        // (CONSOLE_BACKEND_CONTRACT — malformed ≡ missing/404 only for unchanged
        // trusted identity). A malformed 200 is a completed invalid contract,
        // not a stale success: apply it unless a newer negotiation already recovered.
        applyMissingCapabilityContract(generation, () => {
          const clearAction = resolveCapabilityParseFailureClear({
            priorIdentityKey: lastCapabilityIdentityKeyRef.current,
            trustedIdentityKey: parsed.trustedIdentityKey,
          });
          if (clearAction === "clear_authorized") {
            clearAuthorizedConsoleFeeds({ clearCapabilities: true });
          } else {
            clearCapabilityGatedInventories();
          }
        }, parsed.message);
        return null;
      }
      if (
        generation !== capabilityRequestGenerationRef.current ||
        generation <= revokedCapabilityGenerationRef.current ||
        generation < appliedCapabilityGenerationRef.current
      ) {
        return null;
      }
      // A newer network/5xx or missing-contract response already applied.
      // This older success must not clear it, or last-good capabilities stay
      // current with no error (or a cleared contract is restored).
      if (generation < appliedCapabilityFailureGenerationRef.current) {
        return null;
      }

      // Keep the prior object when only generated_at (or equivalent) changed so
      // effects that depend on `capabilities` do not restart every poll cycle
      // (dashboard feeds + selected workspace SSE reconnect / missed events).
      const previous = appliedCapabilitiesRef.current;
      const nextCapabilities =
        previous !== null && sameCapabilityNegotiation(previous, parsed.capabilities)
          ? previous
          : parsed.capabilities;

      // Skip bootstrap (null → first key) so the parallel overview fetch is not wiped.
      // Identity clear advances the feed epoch; a concurrent loadOverview that
      // captured the prior epoch must be restarted or the new tenant list stays
      // blank until the next poll tick. Compare the retained ref — not React state —
      // so a 404 gap cannot disguise a different backend/tenant as bootstrap.
      let identityChanged = false;
      const priorIdentityKey = lastCapabilityIdentityKeyRef.current;
      if (priorIdentityKey !== null && parsed.identityKey !== priorIdentityKey) {
        clearAuthorizedConsoleFeeds();
        identityChanged = true;
      } else if (previous !== null && nextCapabilities !== previous) {
        clearNewlyUnsupportedCapabilityFeeds(previous, nextCapabilities);
      }
      // Capture before clear: a concurrent loadOverview (context sync / poll) may
      // still have refused while the latch was set; refill immediately so recovery
      // does not wait for the next overview poll tick.
      const wasAuthDenied = consoleAuthDeniedRef.current;
      consoleAuthDeniedRef.current = false;
      appliedCapabilityGenerationRef.current = Math.max(
        appliedCapabilityGenerationRef.current,
        generation,
      );
      appliedCapabilitiesRef.current = nextCapabilities;
      lastCapabilityIdentityKeyRef.current = parsed.identityKey;
      setCapabilities(nextCapabilities);
      setCapabilityError((current) =>
        generation < appliedCapabilityFailureGenerationRef.current ? current : null,
      );
      setCapabilitiesReady(true);
      if (wasAuthDenied || identityChanged) {
        void loadOverview();
      }
      return nextCapabilities;
    } finally {
      // A superseded explicit refresh or context sync must not clear the latch
      // while that newer request is still in flight; periodic polls skip while
      // this stays true.
      if (generation === capabilityRequestGenerationRef.current) {
        capabilityLoadInFlightRef.current = false;
      }
    }
  }, [
    clearAuthorizedConsoleFeeds,
    clearCapabilityGatedInventories,
    clearNewlyUnsupportedCapabilityFeeds,
    invalidateAuthorizedFeedsIfContextChanged,
    loadOverview,
    capabilityRequestGenerationRef,
    configuredContextFingerprintRef,
    capabilityLoadInFlightRef,
    appliedCapabilityGenerationRef,
    consoleAuthDeniedRef,
    revokedCapabilityGenerationRef,
    appliedCapabilityFailureGenerationRef,
    appliedCapabilitiesRef,
    lastCapabilityIdentityKeyRef,
    setCapabilityError,
    setCapabilities,
    setCapabilitiesReady,
  ]);

  return { loadCapabilities };
}
