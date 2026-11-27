import { isEmbeddedConsole, isEmbeddedConsolePage } from "./console-urls.ts";

export const consoleSessionExpiredMessage = "Console access denied because the session expired.";

// The browser module instance keeps the same signal across client navigation.
const session = new AbortController();

export function isConsoleSessionExpired(): boolean {
  return session.signal.aborted && isEmbeddedConsolePage();
}

export function subscribeToConsoleSessionExpiry(onExpiry: () => void): () => void {
  session.signal.addEventListener("abort", onExpiry);
  return () => session.signal.removeEventListener("abort", onExpiry);
}

export function notifyEmbeddedParent(browser: Window): void {
  const { location, parent } = browser;
  if (parent === browser || location.origin === "null" ||
      !isEmbeddedConsole(new URLSearchParams(location.search))) {
    return;
  }
  try {
    if (parent.location.origin === location.origin) {
      parent.postMessage({ type: "awf-console:unauthenticated" }, location.origin);
    }
  } catch {
    // A cross-origin or newly navigated parent may deny access.
  }
}

/** Returns whether the embedded session handled the denial. */
export function expireConsoleSession(): boolean {
  if (!isEmbeddedConsolePage()) return false;
  if (!session.signal.aborted) {
    session.abort(new DOMException(consoleSessionExpiredMessage, "AbortError"));
    notifyEmbeddedParent(window);
  }
  return true;
}

export async function consoleApiFetch(path: string, init: RequestInit): Promise<Response> {
  if (!isEmbeddedConsolePage()) return fetch(path, init);

  session.signal.throwIfAborted();
  const signal = init.signal
    ? AbortSignal.any([init.signal, session.signal])
    : session.signal;
  const response = await fetch(path, { ...init, signal });
  if (response.status === 401) expireConsoleSession();
  // A denial invalidates this response and all other in-flight responses.
  signal.throwIfAborted();
  return response;
}
