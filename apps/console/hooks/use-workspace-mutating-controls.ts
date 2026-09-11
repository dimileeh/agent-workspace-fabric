"use client";

import {
  useCallback,
  useRef,
  type Dispatch,
  type MutableRefObject,
  type SetStateAction,
} from "react";
import { resolveRetryCapabilityGate } from "@/lib/console-capabilities";
import { formatProviderReadinessRetryError } from "@/lib/provider-readiness-format";
import {
  summarizeWorkspaceOperatorFailure,
  summarizeWorkspaceOperatorSuccess,
} from "@/lib/workspace-operator-controls";
import type {
  ConsoleCapabilities,
  Operation,
  WorkspaceControlResponse,
  WorkspaceOperatorAction,
  WorkspaceOperatorRequest,
  WorkspaceRetryResponse,
} from "@/lib/types";
import {
  type OperatorActionState,
  type RetryActionState,
  apiPostWithDeadline,
  operatorActionPath,
  operatorActionReason,
  operatorIdempotencyKey,
} from "@/components/console-dashboard-shared";
import { awfPath } from "@/lib/console-urls";

type UseWorkspaceMutatingControlsArgs = {
  selectedId: string | null;
  selectedIdRef: MutableRefObject<string | null>;
  authorizedFeedEpochRef: MutableRefObject<number>;
  mutatingCapabilities: ConsoleCapabilities | null;
  capabilitiesReady: boolean;
  workspaceVersion: number | undefined;
  operatorActionState: OperatorActionState;
  setRetryState: Dispatch<SetStateAction<RetryActionState>>;
  setOperatorActionState: Dispatch<SetStateAction<OperatorActionState>>;
  renegotiateAfterMutationAuthorizationDenied: (message: string) => void;
  loadCapabilities: () => Promise<ConsoleCapabilities | null>;
  loadOverview: () => Promise<void>;
  loadWorkspace: (workspaceId: string) => Promise<void>;
  reloadAvailableFeeds: (caps: ConsoleCapabilities) => Promise<void>;
};

/**
 * Retry + operator-action mutations with auth-epoch / selection guards.
 * Extracted from console-dashboard.tsx for the first-party 1500-line guard.
 */
export function useWorkspaceMutatingControls({
  selectedId,
  selectedIdRef,
  authorizedFeedEpochRef,
  mutatingCapabilities,
  capabilitiesReady,
  workspaceVersion,
  operatorActionState,
  setRetryState,
  setOperatorActionState,
  renegotiateAfterMutationAuthorizationDenied,
  loadCapabilities,
  loadOverview,
  loadWorkspace,
  reloadAvailableFeeds,
}: UseWorkspaceMutatingControlsArgs) {
  const retryIdempotencyKeysRef = useRef(new Map<string, string>());
  const operatorActionIdempotencyKeysRef = useRef(new Map<string, string>());

  const retrySelectedWorkspace = useCallback(async () => {
    const workspaceId = selectedId;
    if (!workspaceId) {
      return;
    }
    const retryGate = resolveRetryCapabilityGate({
      capabilities: mutatingCapabilities,
      capabilitiesReady,
    });
    if (!retryGate.enabled) {
      return;
    }
    // Capture at gate success so a tenant/auth epoch bump during the POST (or
    // during follow-up refreshes) cannot apply the prior tenant's retry result.
    const epoch = authorizedFeedEpochRef.current;
    setRetryState({ status: "submitting" });
    const retryIdentityScope = `${epoch}:${workspaceId}`;
    const idempotencyKey =
      retryIdempotencyKeysRef.current.get(retryIdentityScope) ??
      operatorIdempotencyKey("retry", workspaceId);
    retryIdempotencyKeysRef.current.set(retryIdentityScope, idempotencyKey);
    const result = await apiPostWithDeadline<WorkspaceRetryResponse>(
      awfPath(`workspaces/${encodeURIComponent(workspaceId)}/retry`),
      { idempotency_key: idempotencyKey },
    );
    if (
      result.ok ||
      (result.status !== 0 && result.status !== 502 && result.status !== 504)
    ) {
      if (retryIdempotencyKeysRef.current.get(retryIdentityScope) === idempotencyKey) {
        retryIdempotencyKeysRef.current.delete(retryIdentityScope);
      }
    }
    if (
      epoch === authorizedFeedEpochRef.current &&
      !result.ok &&
      (result.status === 401 || result.status === 403)
    ) {
      renegotiateAfterMutationAuthorizationDenied(result.message);
    }
    if (
      epoch !== authorizedFeedEpochRef.current ||
      selectedIdRef.current !== workspaceId
    ) {
      // Auth/tenant epoch advanced or selection changed — do not paint prior
      // tenant retry state into the current inspector.
      if (epoch !== authorizedFeedEpochRef.current) {
        return;
      }
      if (!result.ok) {
        return;
      }
      const caps = await loadCapabilities();
      if (epoch !== authorizedFeedEpochRef.current) {
        return;
      }
      // Capability outages must not hide mutation results from the workspace list.
      await loadOverview();
      if (epoch !== authorizedFeedEpochRef.current) {
        return;
      }
      if (caps) {
        await reloadAvailableFeeds(caps);
      }
      return;
    }
    if (!result.ok) {
      setRetryState({ status: "error", message: formatProviderReadinessRetryError(result) });
      return;
    }
    setRetryState({
      status: "success",
      newWorkspaceId: result.data.new_workspace_id,
      operationId: result.data.operation_id,
    });
    {
      const caps = await loadCapabilities();
      if (epoch !== authorizedFeedEpochRef.current) {
        return;
      }
      await loadOverview();
      if (epoch !== authorizedFeedEpochRef.current) {
        return;
      }
      if (caps) {
        await reloadAvailableFeeds(caps);
      }
    }
  }, [
    authorizedFeedEpochRef,
    capabilitiesReady,
    loadCapabilities,
    loadOverview,
    mutatingCapabilities,
    renegotiateAfterMutationAuthorizationDenied,
    reloadAvailableFeeds,
    selectedId,
    selectedIdRef,
    setRetryState,
  ]);

  const runWorkspaceOperatorAction = useCallback(
    async (action: WorkspaceOperatorAction, requestedTier?: number) => {
      const workspaceId = selectedId;
      if (!workspaceId || operatorActionState.status === "submitting") {
        return;
      }
      setOperatorActionState({ status: "submitting", action });
      const epoch = authorizedFeedEpochRef.current;
      const operatorActionIdentityScope = `${epoch}:${workspaceId}:${action}`;
      const idempotencyKey =
        operatorActionIdempotencyKeysRef.current.get(operatorActionIdentityScope) ??
        operatorIdempotencyKey(action, workspaceId);
      operatorActionIdempotencyKeysRef.current.set(
        operatorActionIdentityScope,
        idempotencyKey,
      );
      const payload: WorkspaceOperatorRequest = {
        reason: operatorActionReason(action),
        workspace_version: workspaceVersion,
        idempotency_key: idempotencyKey,
      };
      if (action === "revalidate") {
        payload.requested_tier = requestedTier === 1 || requestedTier === 2 || requestedTier === 3 ? requestedTier : 1;
      }

      const result = await apiPostWithDeadline<WorkspaceControlResponse | Operation>(
        operatorActionPath(action, workspaceId),
        payload,
      );
      if (
        result.ok ||
        (result.status !== 0 && result.status !== 502 && result.status !== 504)
      ) {
        if (
          operatorActionIdempotencyKeysRef.current.get(operatorActionIdentityScope) ===
          idempotencyKey
        ) {
          operatorActionIdempotencyKeysRef.current.delete(operatorActionIdentityScope);
        }
      }
      if (
        epoch === authorizedFeedEpochRef.current &&
        !result.ok &&
        (result.status === 401 || result.status === 403)
      ) {
        renegotiateAfterMutationAuthorizationDenied(result.message);
      }
      if (
        epoch !== authorizedFeedEpochRef.current ||
        selectedIdRef.current !== workspaceId
      ) {
        // Auth/tenant epoch advanced or selection changed — do not paint prior
        // tenant operation state into the current inspector.
        if (epoch !== authorizedFeedEpochRef.current) {
          return;
        }
        if (!result.ok) {
          return;
        }
        const caps = await loadCapabilities();
        if (epoch !== authorizedFeedEpochRef.current) {
          return;
        }
        await loadOverview();
        if (epoch !== authorizedFeedEpochRef.current) {
          return;
        }
        if (caps) {
          await reloadAvailableFeeds(caps);
        }
        return;
      }
      if (!result.ok) {
        const failure = summarizeWorkspaceOperatorFailure(result);
        setOperatorActionState({
          status: "error",
          action,
          errorCode: failure.errorCode,
          message: failure.message,
        });
        return;
      }

      const success = summarizeWorkspaceOperatorSuccess(action, result.data);
      setOperatorActionState({
        status: "success",
        action,
        operationId: success.operationId,
        operationStatus: success.status,
        message: success.message,
        warnings: success.warnings,
      });
      {
        const caps = await loadCapabilities();
        if (epoch !== authorizedFeedEpochRef.current) {
          return;
        }
        await Promise.all([
          loadOverview(),
          loadWorkspace(workspaceId),
          ...(caps ? [reloadAvailableFeeds(caps)] : []),
        ]);
      }
    },
    [
      authorizedFeedEpochRef,
      loadCapabilities,
      loadOverview,
      loadWorkspace,
      operatorActionState.status,
      renegotiateAfterMutationAuthorizationDenied,
      reloadAvailableFeeds,
      selectedId,
      selectedIdRef,
      setOperatorActionState,
      workspaceVersion,
    ],
  );

  return { retrySelectedWorkspace, runWorkspaceOperatorAction };
}
