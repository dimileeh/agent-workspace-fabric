"use client";

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useSearchParams } from "next/navigation";

import { apiGet } from "@/components/console-dashboard-shared";
import { awfPath, configuredContextFingerprint } from "@/lib/console-urls";
import { subscribeToHistoryNavigation } from "@/lib/history-navigation";
import type { ApiEnvelope, Workspace } from "@/lib/types";

export type TaskDetailsPromptState =
  | { status: "closed" }
  | { status: "loading" }
  | { status: "ready"; prompt: string }
  | { status: "denied"; message: string }
  | { status: "missing"; message: string }
  | { status: "error"; message: string };

const MISSING_MESSAGE = "Workspace detail was not found.";
const DENIED_FALLBACK = "Task prompt access was denied.";
const ERROR_FALLBACK = "Unable to load the task prompt.";
const MISMATCH_MESSAGE = "Task prompt response did not match this workspace.";

type ResolvedPrompt = Exclude<TaskDetailsPromptState, { status: "closed" } | { status: "loading" }>;

type AppliedPrompt = {
  identity: string;
  state: ResolvedPrompt;
};

function structuredErrorMessage(detail: unknown): string {
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) {
    return "";
  }
  const body = detail as {
    detail?: { message?: unknown } | string;
    message?: unknown;
  };
  if (typeof body.detail === "string" && body.detail.trim()) {
    return body.detail.trim();
  }
  if (
    body.detail &&
    typeof body.detail === "object" &&
    typeof body.detail.message === "string" &&
    body.detail.message.trim()
  ) {
    return body.detail.message.trim();
  }
  if (typeof body.message === "string" && body.message.trim()) {
    return body.message.trim();
  }
  return "";
}

function classifyDetail(result: ApiEnvelope<Workspace>, requestedId: string): ResolvedPrompt {
  if (!result.ok) {
    if (result.status === 401 || result.status === 403) {
      return {
        status: "denied",
        message: structuredErrorMessage(result.detail) || DENIED_FALLBACK,
      };
    }
    if (result.status === 404) {
      return { status: "missing", message: MISSING_MESSAGE };
    }
    if (result.status === 0) {
      return { status: "error", message: result.message.trim() || ERROR_FALLBACK };
    }
    return {
      status: "error",
      message: structuredErrorMessage(result.detail) || ERROR_FALLBACK,
    };
  }

  const data = result.data;
  if (!data || typeof data !== "object") {
    return { status: "ready", prompt: "" };
  }
  if (typeof data.id === "string" && data.id.trim() !== "" && data.id !== requestedId) {
    return { status: "error", message: MISMATCH_MESSAGE };
  }
  const raw = typeof data.task_prompt === "string" ? data.task_prompt : "";
  return { status: "ready", prompt: raw.trim() ? raw : "" };
}

function readLocationFingerprint(): string {
  if (typeof window === "undefined") {
    return "";
  }
  return configuredContextFingerprint(window.location.search);
}

function useTaskDetailsContextFingerprint(): { location: string; router: string } {
  const searchParams = useSearchParams();
  const router = configuredContextFingerprint(`?${searchParams.toString()}`);
  const location = useSyncExternalStore(
    subscribeToHistoryNavigation,
    readLocationFingerprint,
    () => router,
  );
  return { location, router };
}

/**
 * Loads the open task-details modal's authorized prompt.
 * Overview rows deliberately omit task_prompt; this hook does not write it back.
 * A response is applied only for the generation, workspace, and context that started it.
 */
export function useTaskDetailsPrompt(workspaceId: string | null): TaskDetailsPromptState {
  const { location, router } = useTaskDetailsContextFingerprint();
  const identity = workspaceId === null ? null : JSON.stringify([workspaceId, location, router]);
  const [applied, setApplied] = useState<AppliedPrompt | null>(null);
  const generationRef = useRef(0);

  useEffect(() => {
    if (workspaceId === null || identity === null) {
      return;
    }
    const generation = ++generationRef.current;
    const requestedId = workspaceId;
    const requestedIdentity = identity;
    let cancelled = false;
    // Defer past Strict Mode's setup/cleanup/setup so the remount does not
    // issue a second detail read. Closing the modal clears this timer.
    const timer = window.setTimeout(() => {
      void readDetail();
    }, 0);

    async function readDetail() {
      if (cancelled || generation !== generationRef.current) {
        return;
      }
      const requestedFingerprint = readLocationFingerprint();
      const result = await apiGet<Workspace>(awfPath(`workspaces/${encodeURIComponent(requestedId)}`));
      if (
        cancelled ||
        generation !== generationRef.current ||
        readLocationFingerprint() !== requestedFingerprint
      ) {
        return;
      }
      const next = classifyDetail(result, requestedId);
      setApplied((current) => {
        if (cancelled || generation !== generationRef.current) {
          return current;
        }
        return { identity: requestedIdentity, state: next };
      });
    }

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [identity, workspaceId]);

  if (identity === null) {
    return { status: "closed" };
  }
  if (applied !== null && applied.identity === identity) {
    return applied.state;
  }
  return { status: "loading" };
}
