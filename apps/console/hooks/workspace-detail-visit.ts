import type { MutableRefObject } from "react";

import type { DetailFeedName } from "@/hooks/workspace-detail-feed-settlement";

type SettledDetailOutage = { generation: number; message: string };

export type WorkspaceDetailVisitRefs = {
  workspaceDetailVisitSelectionRef: MutableRefObject<string | null | undefined>;
  workspaceDetailVisitRef: MutableRefObject<number>;
  workspaceDetailVisitGenerationFloorRef: MutableRefObject<number>;
  workspaceDetailRequestGenerationRef: MutableRefObject<number>;
  workspaceStreamAuthDeniedRef: MutableRefObject<boolean>;
  workspaceBaseDetailAuthDeniedRef: MutableRefObject<boolean>;
  revokedWorkspaceDetailGenerationRef: MutableRefObject<number>;
  appliedWorkspaceDetailGenerationRef: MutableRefObject<number>;
  revokedEventFeedGenerationRef: MutableRefObject<number>;
  appliedEventFeedGenerationRef: MutableRefObject<number>;
  revokedRuntimeGenerationRef: MutableRefObject<number>;
  appliedRuntimeGenerationRef: MutableRefObject<number>;
  runtimeDenialReleasedThroughRef: MutableRefObject<number>;
  revokedOperationsGenerationRef: MutableRefObject<number>;
  appliedOperationsGenerationRef: MutableRefObject<number>;
  operationsDenialReleasedThroughRef: MutableRefObject<number>;
  eventsOutageReleasedThroughRef: MutableRefObject<number>;
  appliedDetailFailureGenerationRef: MutableRefObject<number>;
  settledDetailOutagesRef: MutableRefObject<Partial<Record<DetailFeedName, SettledDetailOutage>>>;
  revokedLogListingGenerationRef: MutableRefObject<number>;
  appliedLogListingGenerationRef: MutableRefObject<number>;
};

/**
 * Start a new inspector visit when the selected workspace changes.
 *
 * The previous visit's in-flight GET is still the latest request generation
 * until this visit starts its own load. Advance past it so a late 401/403
 * cannot stamp revoked onto the generation the re-opened GET will take,
 * including the window before that GET begins. Previous denial and success
 * watermarks must not cover this visit: a new visit may retry /stream, and
 * the previous route denial must not keep that retry closed or survive onto
 * the re-opened GET.
 *
 * Extracted from use-workspace-detail-loader.ts for the first-party 1500-line guard.
 */
export function beginWorkspaceDetailVisit(
  selectedId: string | null,
  refs: WorkspaceDetailVisitRefs,
): void {
  if (refs.workspaceDetailVisitSelectionRef.current === selectedId) {
    return;
  }
  refs.workspaceDetailVisitSelectionRef.current = selectedId;
  refs.workspaceDetailVisitRef.current += 1;
  refs.workspaceDetailVisitGenerationFloorRef.current =
    ++refs.workspaceDetailRequestGenerationRef.current;
  refs.workspaceStreamAuthDeniedRef.current = false;
  refs.workspaceBaseDetailAuthDeniedRef.current = false;
  refs.revokedWorkspaceDetailGenerationRef.current = 0;
  refs.appliedWorkspaceDetailGenerationRef.current = 0;
  refs.revokedEventFeedGenerationRef.current = 0;
  refs.appliedEventFeedGenerationRef.current = 0;
  refs.revokedRuntimeGenerationRef.current = 0;
  refs.appliedRuntimeGenerationRef.current = 0;
  refs.runtimeDenialReleasedThroughRef.current = 0;
  refs.revokedOperationsGenerationRef.current = 0;
  refs.appliedOperationsGenerationRef.current = 0;
  refs.operationsDenialReleasedThroughRef.current = 0;
  refs.eventsOutageReleasedThroughRef.current = 0;
  refs.appliedDetailFailureGenerationRef.current = 0;
  refs.settledDetailOutagesRef.current = {};
  refs.revokedLogListingGenerationRef.current = 0;
  refs.appliedLogListingGenerationRef.current = 0;
}
