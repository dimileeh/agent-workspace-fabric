type HistoryListener = () => void;

const listeners = new Set<HistoryListener>();

// One patch for every subscriber. Each caller used to save and restore
// history.pushState itself, so the task-details modal and the dashboard
// dropped each other's wrapper on unmount or effect re-run.
let patch: {
  pushState: History["pushState"];
  replaceState: History["replaceState"];
} | null = null;

function browserWindow(): Window | undefined {
  if (typeof window === "undefined") {
    return undefined;
  }
  return window;
}

function notifyListeners(): void {
  for (const listener of [...listeners]) {
    listener();
  }
}

function installPatch(target: Window): void {
  if (patch !== null) {
    return;
  }
  const { history } = target;
  const pushState = history.pushState.bind(history);
  const replaceState = history.replaceState.bind(history);
  patch = { pushState, replaceState };
  history.pushState = ((data: unknown, unused: string, url?: string | URL | null) => {
    pushState(data, unused, url);
    notifyListeners();
  }) as History["pushState"];
  history.replaceState = ((data: unknown, unused: string, url?: string | URL | null) => {
    replaceState(data, unused, url);
    notifyListeners();
  }) as History["replaceState"];
}

function uninstallPatch(target: Window): void {
  if (patch === null) {
    return;
  }
  target.history.pushState = patch.pushState;
  target.history.replaceState = patch.replaceState;
  patch = null;
}

export function subscribeToHistoryNavigation(listener: HistoryListener): () => void {
  listeners.add(listener);
  const target = browserWindow();
  if (target !== undefined) {
    target.addEventListener("popstate", listener);
    installPatch(target);
  }
  return () => {
    listeners.delete(listener);
    const current = browserWindow();
    if (current !== undefined) {
      current.removeEventListener("popstate", listener);
    }
    if (listeners.size === 0 && current !== undefined) {
      uninstallPatch(current);
    }
  };
}
