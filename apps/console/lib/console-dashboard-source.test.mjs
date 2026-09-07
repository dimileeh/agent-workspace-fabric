import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const dashboardSource = {
  dashboard: readFileSync(new URL("../components/console-dashboard.tsx", import.meta.url), "utf8"),
  liveStream: readFileSync(new URL("../hooks/use-workspace-live-stream.ts", import.meta.url), "utf8"),
  logTails: readFileSync(new URL("../hooks/use-workspace-log-tails.ts", import.meta.url), "utf8"),
  mutatingControls: readFileSync(
    new URL("../hooks/use-workspace-mutating-controls.ts", import.meta.url),
    "utf8",
  ),
  overview: readFileSync(new URL("../components/console-dashboard-overview.tsx", import.meta.url), "utf8"),
  capacity: readFileSync(new URL("../components/console-dashboard-capacity.tsx", import.meta.url), "utf8"),
  shared: readFileSync(new URL("../components/console-dashboard-shared.tsx", import.meta.url), "utf8"),
  logs: readFileSync(new URL("../components/console-dashboard-logs.tsx", import.meta.url), "utf8"),
  inspector: readFileSync(
    new URL("../components/console-dashboard-inspector.tsx", import.meta.url),
    "utf8",
  ),
  fleetPanels: readFileSync(
    new URL("../components/console-dashboard-fleet-panels.tsx", import.meta.url),
    "utf8",
  ),
  overlays: readFileSync(
    new URL("../components/console-dashboard-overlays.tsx", import.meta.url),
    "utf8",
  ),
  rail: readFileSync(
    new URL("../components/console-dashboard-workspace-rail.tsx", import.meta.url),
    "utf8",
  ),
  detail: readFileSync(
    new URL("../components/console-dashboard-workspace-detail.tsx", import.meta.url),
    "utf8",
  ),
};

test("task details modal locks body scroll in a layout effect", () => {
  const modalSource = extractFunctionSource("TaskDetailsModal");
  const scrollLockEffect = modalSource.match(
    /use(?:Isomorphic)?(?:Layout)?Effect\(\(\) => \{\s*const scrollY = window\.scrollY;[\s\S]*?document\.body\.style\.overflow = "hidden";[\s\S]*?\}, \[\]\);/,
  );

  assert.ok(scrollLockEffect, "Expected TaskDetailsModal to lock and restore body scroll");
  assert.match(
    scrollLockEffect[0],
    /^use(?:Layout|IsomorphicLayout)Effect\(/,
    "Expected the modal scroll lock to use layout timing (useLayoutEffect / useIsomorphicLayoutEffect) so it runs before paint",
  );
  // The isomorphic alias must resolve to useLayoutEffect on the client; otherwise
  // the scroll lock would run after paint and the position would visibly jump.
  assert.match(
    dashboardSource.detail,
    /useIsomorphicLayoutEffect =\s*typeof window !== "undefined" \? useLayoutEffect : useEffect;/,
  );
});

test("capacity panel only shows oldest queued fact when the queue is populated", () => {
  const panelSource = extractFunctionSource("ResourceCapacityPanel");

  assert.match(
    panelSource,
    /saturation\.capacity_queue\.queued_workspace_count > 0 &&\s*saturation\.capacity_queue\.oldest_wait_seconds !== null/,
  );
});

test("capacity panel falls back to full reserved pressure reasons", () => {
  const panelSource = extractFunctionSource("ResourceCapacityPanel");

  assert.match(
    panelSource,
    /saturation\.allocated_capacity\.pressure_reasons\.length > 0\s*\?\s*saturation\.allocated_capacity\.pressure_reasons\s*:\s*saturation\.capacity\.pressure_reasons/,
  );
  assert.match(panelSource, /pressureReasons\.map\(\(reason\) =>/);
});

test("reliability panel renders independently of resource capacity", () => {
  assert.match(dashboardSource.capacity, /export function ReliabilityPanel\(/);
  // Split maintainability extraction mounts ReliabilityPanel from fleet-panels.
  assert.match(
    dashboardSource.fleetPanels,
    /<ReliabilityPanel[\s\S]*workspaceSummary=\{workspaceSummary\}/,
  );
  assert.match(
    dashboardSource.fleetPanels,
    /showReliability \? \(\s*<ReliabilityPanel/,
  );
  assert.match(
    dashboardSource.dashboard,
    /<ConsoleDashboardFleetPanels[\s\S]*showReliability=\{showReliability\}[\s\S]*workspaceSummary=\{workspaceSummary\}/,
  );
  assert.doesNotMatch(
    extractFunctionSource("ResourceCapacityPanel"),
    /workspaceSummary/,
  );
});

test("clearAuthorizedConsoleFeeds resets agent, model, repo, and search filter selections", () => {
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /const clearAuthorizedConsoleFeeds = useCallback\([\s\S]*?setRetainedAgents\(\[\]\);\s*setRetainedModels\(\[\]\);[\s\S]*?setAgentFilters\(\[\]\);\s*setModelFilters\(\[\]\);\s*setRepoFilter\(""\);\s*setSearchText\(""\);/,
    "Expected auth/tenant clear to reset agent/model/repo/search filters with retained metadata",
  );
});

test("authorized feed loaders discard responses after clear epoch advances", () => {
  const dashboard = dashboardSource.dashboard;
  for (const loader of [
    "loadOverview",
    "loadResourceSaturation",
    "loadDashboardSummary",
    "loadCloudRuntime",
    "loadWorkspaceSummary",
    "loadMergeQueue",
    "loadFailureSummary",
  ]) {
    assert.match(
      dashboard,
      new RegExp(
        `const ${loader} = useCallback\\([\\s\\S]*?const epoch = authorizedFeedEpochRef\\.current;[\\s\\S]*?if \\(\\s*epoch !== authorizedFeedEpochRef\\.current`,
      ),
      `Expected ${loader} to capture and discard on authorizedFeedEpochRef advance`,
    );
  }
  assert.match(
    dashboard,
    /const consoleAuthDeniedRef = useRef\(false\);/,
    "Expected a synchronous consoleAuthDeniedRef latch for auth revocation",
  );
  assert.match(
    dashboard,
    /if \(options\?\.authDenied\) \{\s*consoleAuthDeniedRef\.current = true;/,
    "Expected clearAuthorizedConsoleFeeds to latch auth denial synchronously",
  );
  assert.match(
    dashboard,
    /const loadOverview = useCallback\([\s\S]*?if \(consoleAuthDeniedRef\.current\) \{\s*setOverview\(\[\]\);\s*return;/,
    "Expected loadOverview to refuse refill while auth denial is latched",
  );
  assert.match(
    dashboard,
    /const loadOverview = useCallback\([\s\S]*?if \(\s*epoch !== authorizedFeedEpochRef\.current \|\|\s*consoleAuthDeniedRef\.current \|\|\s*generation !== overviewRequestGenerationRef\.current \|\|\s*overviewQueryRef\.current !== capturedQuery\s*\)/,
    "Expected loadOverview to re-check auth denial, request generation, and captured query after awaits",
  );
  assert.match(
    dashboard,
    /const workspaceDetailRequestGenerationRef = useRef\(0\);/,
    "Expected a workspace-detail request-generation ref so overlapping polls stay monotonic",
  );
  assert.match(
    dashboard,
    /const loadWorkspace = useCallback\([\s\S]*?const epoch = authorizedFeedEpochRef\.current;[\s\S]*?const gatedGeneration = gatedDetailFeedGenerationRef\.current;[\s\S]*?const generation = \+\+workspaceDetailRequestGenerationRef\.current;[\s\S]*?if \(\s*epoch !== authorizedFeedEpochRef\.current \|\|\s*gatedGeneration !== gatedDetailFeedGenerationRef\.current \|\|\s*generation !== workspaceDetailRequestGenerationRef\.current \|\|\s*selectedIdRef\.current !== workspaceId\s*\)/,
    "Expected loadWorkspace to discard after epoch/gated-detail/request generation advance or selection change",
  );
  assert.match(
    dashboardSource.logTails,
    /const loadLogTail = useCallback\([\s\S]*?const epoch = authorizedFeedEpochRef\.current;[\s\S]*?const gatedGeneration = gatedDetailFeedGenerationRef\.current;[\s\S]*?if \(\s*epoch !== authorizedFeedEpochRef\.current \|\|\s*gatedGeneration !== gatedDetailFeedGenerationRef\.current \|\|\s*selectedIdRef\.current !== workspaceId\s*\)/,
    "Expected loadLogTail to discard after epoch/gated-detail generation advance or selection change",
  );
  for (const loader of [
    "loadResourceSaturation",
    "loadWorkspaceSummary",
    "loadFailureSummary",
  ]) {
    assert.match(
      dashboard,
      new RegExp(
        `const ${loader} = useCallback\\([\\s\\S]*?const epoch = authorizedFeedEpochRef\\.current;[\\s\\S]*?const gatedGeneration = gatedDetailFeedGenerationRef\\.current;[\\s\\S]*?const generation = \\+\\+\\w+RequestGenerationRef\\.current;[\\s\\S]*?if \\(\\s*epoch !== authorizedFeedEpochRef\\.current \\|\\|\\s*gatedGeneration !== gatedDetailFeedGenerationRef\\.current \\|\\|\\s*generation !== \\w+RequestGenerationRef\\.current\\s*\\)`,
      ),
      `Expected ${loader} to discard after authorized epoch, gated-detail, or feed request generation advance`,
    );
  }
  assert.match(
    dashboard,
    /const loadMergeQueue = useCallback\([\s\S]*?const epoch = authorizedFeedEpochRef\.current;[\s\S]*?const gatedGeneration = gatedDetailFeedGenerationRef\.current;[\s\S]*?const generation = \+\+mergeQueueRequestGenerationRef\.current;[\s\S]*?if \(\s*epoch !== authorizedFeedEpochRef\.current \|\|\s*gatedGeneration !== gatedDetailFeedGenerationRef\.current \|\|\s*generation !== mergeQueueRequestGenerationRef\.current\s*\)/,
    "Expected loadMergeQueue to discard after authorized epoch, gated-detail, or merge-queue request generation advance",
  );
});

test("loadOverview discards responses superseded by a newer filter query", () => {
  const dashboard = dashboardSource.dashboard;
  // repoFilter is applied server-side only (filterAndSortOverview does not reapply it),
  // so overlapping paginated overview loads must stamp a generation and capture the
  // query snapshot like other feeds — ignore responses for a superseded filter.
  assert.match(
    dashboard,
    /const overviewRequestGenerationRef = useRef\(0\);/,
    "Expected an overview request generation ref for overlapping filter/poll loads",
  );
  assert.match(
    dashboard,
    /const loadOverview = useCallback\([\s\S]*?const generation = \+\+overviewRequestGenerationRef\.current;[\s\S]*?const capturedQuery = overviewQueryRef\.current;[\s\S]*?generation !== overviewRequestGenerationRef\.current \|\|\s*overviewQueryRef\.current !== capturedQuery[\s\S]*?setOverview\(/,
    "Expected loadOverview to stamp generation, capture the query, and discard superseded responses before apply",
  );
  assert.match(
    dashboard,
    /const loadOverview = useCallback\([\s\S]*?collectOverviewPages\(async \(cursor\) => \{[\s\S]*?generation !== overviewRequestGenerationRef\.current \|\|\s*overviewQueryRef\.current !== capturedQuery/,
    "Expected paginated overview page fetches to abort when a newer request or query supersedes them",
  );
  assert.match(
    dashboard,
    /const loadOverview = useCallback\([\s\S]*?const \{ statusFilters: statuses, agentFilters: agents, repoFilter: repo \} =\s*capturedQuery;/,
    "Expected overview list filters to come from the captured query snapshot, not a live re-read",
  );
});

test("loadOverview reads filters via ref so capability polling stays filter-independent", () => {
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /const overviewQueryRef = useOverviewQueryRef\(statusFilters, agentFilters, repoFilter\);/,
    "Expected overview filter values to live in useOverviewQueryRef (effect-synced, no render write)",
  );
  assert.doesNotMatch(
    dashboard,
    /overviewQueryRef\.current\s*=/,
    "Expected overviewQueryRef not to be assigned during render (react-hooks/refs)",
  );
  assert.match(
    dashboard,
    /const loadOverview = useCallback\([\s\S]*?overviewQueryRef\.current;[\s\S]*?\}, \[setSelectedId\]\);/,
    "Expected loadOverview deps to exclude status/agent/repo filters",
  );
  assert.doesNotMatch(
    dashboard,
    /const loadOverview = useCallback\([\s\S]*?\}, \[agentFilters, repoFilter, setSelectedId, statusFilters\]\);/,
    "Expected loadOverview not to recreate when only overview filters change",
  );
});

test("loadOverview follows overview pagination beyond the first page", () => {
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /import \{ collectOverviewPages, overviewListPath \} from "@\/lib\/overview-list";/,
    "Expected loadOverview to import overview cursor helpers",
  );
  assert.match(
    dashboard,
    /const loadOverview = useCallback\([\s\S]*?collectOverviewPages\(async \(cursor\) => \{[\s\S]*?overviewListPath\(filters, cursor\)/,
    "Expected loadOverview to accumulate pages via collectOverviewPages + overviewListPath",
  );
  assert.doesNotMatch(
    dashboard,
    /const loadOverview = useCallback\([\s\S]*?awfPath\("workspaces\/overview"/,
    "Expected loadOverview not to issue a single non-cursor overview request",
  );
  assert.match(
    dashboard,
    /collected\.truncated[\s\S]*?Workspace list truncated/,
    "Expected loadOverview to surface truncation when the page ceiling stops with has_more",
  );
  assert.match(
    dashboard,
    /setOverviewTruncationWarning\(\s*collected\.truncated\s*\?[\s\S]*?Workspace list truncated/,
    "Expected truncation to use a dedicated overview warning state, not the shared error slot",
  );
  assert.doesNotMatch(
    dashboard,
    /const loadOverview = useCallback\([\s\S]*?setError\(\s*collected\.truncated/,
    "Expected loadOverview not to write truncation into the shared error state wiped by loadWorkspace",
  );
  assert.match(
    dashboard,
    /collected\.items\.map\(/,
    "Expected loadOverview to consume OverviewPageCollection.items rather than a bare array",
  );
});

test("loadWorkspace success clears shared error without clearing overview truncation", () => {
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /const \[overviewTruncationWarning, setOverviewTruncationWarning\] = useState<string \| null>\(null\);/,
    "Expected a dedicated overview truncation warning state",
  );
  assert.match(
    dashboard,
    /const loadWorkspace = useCallback\([\s\S]*?\} else \{\s*setError\(null\);\s*\}/,
    "Expected loadWorkspace success to clear only the shared error slot",
  );
  assert.doesNotMatch(
    dashboard,
    /const loadWorkspace = useCallback\([\s\S]*?setOverviewTruncationWarning/,
    "Expected loadWorkspace not to touch overview truncation warning",
  );
  assert.match(
    dashboard,
    /overviewTruncationWarning \? <ErrorBanner message=\{overviewTruncationWarning\} \/> : null/,
    "Expected truncation warning to render independently of the shared error banner",
  );
});

test("loadWorkspace retains last-good diagnostics on transient feed failure; clears on gated-off or 401/403", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6f9g8g: inspector
  // runtime/events/operations/logs must not wipe last-successful snapshots on
  // polling blips; gated-off feeds still clear, and feed-level 401/403 drops cache.
  const dashboard = dashboardSource.dashboard;
  const loadIdx = dashboard.indexOf("const loadWorkspace = useCallback");
  assert.ok(loadIdx > 0, "Expected loadWorkspace callback");
  const loadEnd = dashboard.indexOf("}, [capabilities]);", loadIdx);
  assert.ok(loadEnd > loadIdx, "Expected loadWorkspace dependency list");
  const body = dashboard.slice(loadIdx, loadEnd);
  assert.match(
    body,
    /setDetail\(\(current\) =>/,
    "Expected loadWorkspace to merge into the current detail snapshot",
  );
  assert.match(
    body,
    /feedAuthDenied/,
    "Expected loadWorkspace to distinguish feed-level 401/403 from transient outages",
  );
  assert.match(
    body,
    /!allowRuntime\s*\?\s*null[\s\S]*?current\.runtime/,
    "Expected gated-off runtime to clear while transient runtime failure retains current.runtime",
  );
  assert.match(
    body,
    /!allowEvents\s*\?\s*\[\][\s\S]*?current\.events/,
    "Expected gated-off events to clear while transient events failure retains current.events",
  );
  assert.match(
    body,
    /!allowOperations\s*\?\s*\[\][\s\S]*?current\.operations/,
    "Expected gated-off operations to clear while transient operations failure retains current.operations",
  );
  assert.match(
    body,
    /!allowLogs\s*\?\s*\[\][\s\S]*?current\.streams/,
    "Expected gated-off logs to clear while transient log-list failure retains current.streams",
  );
  assert.doesNotMatch(
    body,
    /runtime: runtime\?\.ok \? runtime\.data : null,\s*events: events\?\.ok \? events\.data\.items : \[\],/,
    "Expected loadWorkspace not to replace successful diagnostic snapshots with null/[] on every non-ok result",
  );
});

test("authorized feed clear and overview auth denial wipe truncation with the overview", () => {
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /const clearAuthorizedConsoleFeeds = useCallback\([\s\S]*?setOverview\(\[\]\);\s*setOverviewTruncationWarning\(null\);/,
    "Expected clearAuthorizedConsoleFeeds to wipe truncation with the overview",
  );
  assert.match(
    dashboard,
    /const loadOverview = useCallback\([\s\S]*?if \(pageAuthDenied\) \{[\s\S]*?setOverview\(\[\]\);\s*setOverviewTruncationWarning\(null\);/,
    "Expected overview auth denial to wipe truncation with the overview",
  );
});

test("loadOverview auth denial closes dependent workspace surfaces", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6f9g8d: overview
  // feed-level 401/403 must close selection/inspector/logs/fullscreen rather
  // than clearing only the rail while capabilities still advertise logs.
  const dashboard = dashboardSource.dashboard;
  const authDeniedIdx = dashboard.indexOf("if (pageAuthDenied) {");
  assert.ok(authDeniedIdx > 0, "Expected pageAuthDenied clear path in loadOverview");
  const loadOverviewEnd = dashboard.indexOf("}, [setSelectedId]);", authDeniedIdx);
  assert.ok(loadOverviewEnd > authDeniedIdx, "Expected loadOverview callback end after pageAuthDenied");
  const authDeniedBody = dashboard.slice(authDeniedIdx, loadOverviewEnd);
  assert.match(
    authDeniedBody,
    /gatedDetailFeedGenerationRef\.current \+= 1;[\s\S]*?setOverview\(\[\]\);[\s\S]*?setOverviewTruncationWarning\(null\);[\s\S]*?setSelectedId\(null\);[\s\S]*?setDetail\(emptyDetail\);[\s\S]*?setLogsFullscreen\(false\);[\s\S]*?setFullscreenWorkspaceIds\(\[\]\);/,
    "Expected overview auth denial to invalidate detail generation and close selection/inspector/fullscreen logs",
  );
  assert.equal(
    authDeniedBody.includes("authorizedFeedEpochRef.current +="),
    false,
    "Expected overview auth denial not to bump authorizedFeedEpochRef (would thrash unrelated feeds while overview stays denied)",
  );
  assert.equal(
    authDeniedBody.includes("clearAuthorizedConsoleFeeds("),
    false,
    "Expected overview auth denial not to call clearAuthorizedConsoleFeeds (capabilities may still succeed)",
  );
});

test("loadOverview retains last-good snapshot on transient page failure; clears on 401/403", () => {
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /const loadOverview = useCallback\([\s\S]*?if \(!result\.ok\) \{[\s\S]*?if \(result\.status === 401 \|\| result\.status === 403\) \{\s*pageAuthDenied = true;\s*\}/,
    "Expected loadOverview to mark feed-level 401/403 as auth denial rather than a transient outage",
  );
  assert.match(
    dashboard,
    /const loadOverview = useCallback\([\s\S]*?if \(collected === null\) \{[\s\S]*?if \(pageError !== null\) \{\s*setError\(pageError\);\s*\}\s*if \(pageAuthDenied\) \{[\s\S]*?gatedDetailFeedGenerationRef\.current \+= 1;[\s\S]*?setOverview\(\[\]\);\s*setOverviewTruncationWarning\(null\);/,
    "Expected loadOverview to clear overview and dependent surfaces on page auth denial and retain last-good on other page failures",
  );
  assert.doesNotMatch(
    dashboard,
    /const loadOverview = useCallback\([\s\S]*?if \(collected === null\) \{[\s\S]*?setError\(pageError\);[\s\S]*?setOverview\(\[\]\);\s*return;/,
    "Expected loadOverview not to blank the authorized overview on every collectOverviewPages null",
  );
});

test("loadDashboardSummary discards stale success and error via request generation", () => {
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /const dashboardSummaryRequestGenerationRef = useRef\(0\);/,
    "Expected a dashboard-summary request-generation ref so overlapping polls stay monotonic",
  );
  assert.match(
    dashboard,
    /const loadDashboardSummary = useCallback\([\s\S]*?const generation = \+\+dashboardSummaryRequestGenerationRef\.current;[\s\S]*?generation !== dashboardSummaryRequestGenerationRef\.current[\s\S]*?setDashboardSummaryError/,
    "Expected loadDashboardSummary to bump generation before fetch and discard mismatched responses before success or error setters",
  );
});

test("loadDashboardSummary clears last-good snapshot on feed-level 401 or 403", () => {
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /const loadDashboardSummary = useCallback\([\s\S]*?if \(!result\.ok\) \{[\s\S]*?if \(result\.status === 401 \|\| result\.status === 403\) \{\s*setDashboardSummary\(null\);\s*setDashboardSummaryError\(result\.message\);\s*return;\s*\}[\s\S]*?setDashboardSummaryError\(result\.message\);/,
    "Expected loadDashboardSummary to drop authorized counters on 401/403 rather than retain last-good as a transient outage",
  );
});

test("loadMergeQueue clears last-good snapshot on feed-level 401 or 403", () => {
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /const loadMergeQueue = useCallback\([\s\S]*?if \(!result\.ok\) \{[\s\S]*?if \(result\.status === 401 \|\| result\.status === 403\) \{\s*setMergeQueue\(\[\]\);\s*setMergeQueueHasMore\(false\);\s*setMergeQueueError\(result\.message\);\s*setMergeQueueStatus\("error"\);\s*return;\s*\}[\s\S]*?setMergeQueueError\(result\.message\);\s*setMergeQueueStatus\("error"\);/,
    "Expected loadMergeQueue to drop authorized queue rows on 401/403 rather than retain last-good as a transient outage",
  );
});

test("loadResourceSaturation clears last-good snapshot on feed-level 401 or 403", () => {
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /const loadResourceSaturation = useCallback\([\s\S]*?if \(!result\.ok\) \{[\s\S]*?if \(result\.status === 401 \|\| result\.status === 403\) \{\s*setResourceSaturation\(null\);\s*setResourceError\(result\.message\);\s*return;\s*\}[\s\S]*?setResourceError\(result\.message\);/,
    "Expected loadResourceSaturation to drop authorized saturation on 401/403 rather than retain last-good as a transient outage",
  );
  assert.match(
    dashboard,
    /const resourceSaturationRequestGenerationRef = useRef\(0\);/,
    "Expected a resource-saturation request-generation ref so overlapping polls stay monotonic",
  );
  assert.match(
    dashboard,
    /const loadResourceSaturation = useCallback\([\s\S]*?const generation = \+\+resourceSaturationRequestGenerationRef\.current;[\s\S]*?generation !== resourceSaturationRequestGenerationRef\.current[\s\S]*?setResourceError/,
    "Expected loadResourceSaturation to bump generation before fetch and discard mismatched responses before success or error setters",
  );
});

test("loadWorkspaceSummary clears last-good snapshot on feed-level 401 or 403", () => {
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /const loadWorkspaceSummary = useCallback\([\s\S]*?if \(!result\.ok\) \{[\s\S]*?if \(result\.status === 401 \|\| result\.status === 403\) \{\s*setWorkspaceSummary\(null\);\s*setWorkspaceSummaryError\(result\.message\);\s*return;\s*\}[\s\S]*?setWorkspaceSummaryError\(result\.message\);/,
    "Expected loadWorkspaceSummary to drop authorized reliability on 401/403 rather than retain last-good as a transient outage",
  );
  assert.match(
    dashboard,
    /const workspaceSummaryRequestGenerationRef = useRef\(0\);/,
    "Expected a workspace-summary request-generation ref so overlapping polls stay monotonic",
  );
  assert.match(
    dashboard,
    /const loadWorkspaceSummary = useCallback\([\s\S]*?const generation = \+\+workspaceSummaryRequestGenerationRef\.current;[\s\S]*?generation !== workspaceSummaryRequestGenerationRef\.current[\s\S]*?setWorkspaceSummaryError/,
    "Expected loadWorkspaceSummary to bump generation before fetch and discard mismatched responses before success or error setters",
  );
});

test("loadFailureSummary clears last-good snapshot on feed-level 401 or 403", () => {
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /const loadFailureSummary = useCallback\([\s\S]*?if \(!result\.ok\) \{[\s\S]*?if \(result\.status === 401 \|\| result\.status === 403\) \{\s*setFailureSummary\(null\);\s*setFailureSummaryStatus\("error"\);\s*setFailureSummaryError\(result\.message\);\s*return;\s*\}/,
    "Expected loadFailureSummary to drop authorized failure examples on 401/403 rather than retain last-good as a transient outage",
  );
  assert.match(
    dashboard,
    /const failureSummaryRequestGenerationRef = useRef\(0\);/,
    "Expected a failure-summary request-generation ref so overlapping polls stay monotonic",
  );
  assert.match(
    dashboard,
    /const loadFailureSummary = useCallback\([\s\S]*?const generation = \+\+failureSummaryRequestGenerationRef\.current;[\s\S]*?generation !== failureSummaryRequestGenerationRef\.current[\s\S]*?setFailureSummaryError/,
    "Expected loadFailureSummary to bump generation before fetch and discard mismatched responses before success or error setters",
  );
});

test("loadMergeQueue discards stale success and error via request generation", () => {
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /const mergeQueueRequestGenerationRef = useRef\(0\);/,
    "Expected a merge-queue request-generation ref so overlapping polls stay monotonic",
  );
  assert.match(
    dashboard,
    /const loadMergeQueue = useCallback\([\s\S]*?const generation = \+\+mergeQueueRequestGenerationRef\.current;[\s\S]*?generation !== mergeQueueRequestGenerationRef\.current[\s\S]*?setMergeQueueError/,
    "Expected loadMergeQueue to bump generation before fetch and discard mismatched responses before success or error setters",
  );
});

test("loadCloudRuntime discards stale success and error via request generation", () => {
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /const cloudRuntimeRequestGenerationRef = useRef\(0\);/,
    "Expected a cloud-runtime request-generation ref so overlapping polls stay monotonic",
  );
  assert.match(
    dashboard,
    /const loadCloudRuntime = useCallback\([\s\S]*?const generation = \+\+cloudRuntimeRequestGenerationRef\.current;[\s\S]*?generation !== cloudRuntimeRequestGenerationRef\.current[\s\S]*?setCloudRuntimeError/,
    "Expected loadCloudRuntime to bump generation before fetch and discard mismatched responses before success or error setters",
  );
});

test("loadCapabilities discards stale responses via request generation", () => {
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /const capabilityRequestGenerationRef = useRef\(0\);/,
    "Expected a capability request-generation ref so overlapping polls can be ordered",
  );
  assert.match(
    dashboard,
    /const loadCapabilities = useCallback\([\s\S]*?const generation = \+\+capabilityRequestGenerationRef\.current;[\s\S]*?if \(generation !== capabilityRequestGenerationRef\.current\) \{\s*return null;\s*\}/,
    "Expected loadCapabilities to bump generation before fetch and discard mismatched responses",
  );
  assert.match(
    dashboard,
    /const loadCapabilities = useCallback\([\s\S]*?if \(generation !== capabilityRequestGenerationRef\.current\) \{\s*return null;\s*\}[\s\S]*?consoleAuthDeniedRef\.current = false;/,
    "Expected success-path denial-latch clear to run only after the generation freshness check",
  );
});

test("loadCapabilities reloads overview after clearing a latched auth denial", () => {
  const dashboard = dashboardSource.dashboard;
  // Successful negotiation must not leave the workspace list empty until the
  // next poll tick: clear the latch and immediately refill overview when
  // recovery follows a prior 401/403.
  assert.match(
    dashboard,
    /const loadCapabilities = useCallback\([\s\S]*?const wasAuthDenied = consoleAuthDeniedRef\.current;[\s\S]*?consoleAuthDeniedRef\.current = false;[\s\S]*?if \(wasAuthDenied \|\| identityChanged\) \{\s*void loadOverview\(\);\s*\}/,
    "Expected loadCapabilities to refill overview immediately after clearing a latched auth denial",
  );
  assert.match(
    dashboard,
    /const loadCapabilities = useCallback\([\s\S]*?\}, \[[\s\S]*?loadOverview[\s\S]*?\]\);/,
    "Expected loadCapabilities to depend on loadOverview for auth-recovery refill",
  );
});

test("loadCapabilities reloads overview after identity-change feed clear", () => {
  const dashboard = dashboardSource.dashboard;
  // Concurrent loadOverview can capture an epoch that identity clear advances;
  // restart overview immediately so the new tenant list is not blank until poll.
  // Compare against lastCapabilityIdentityKeyRef so a 404 gap (null React state)
  // cannot disguise a backend/tenant switch as bootstrap.
  assert.match(
    dashboard,
    /const loadCapabilities = useCallback\([\s\S]*?let identityChanged = false;[\s\S]*?const priorIdentityKey = lastCapabilityIdentityKeyRef\.current;[\s\S]*?if \(priorIdentityKey !== null && parsed\.identityKey !== priorIdentityKey\) \{[\s\S]*?clearAuthorizedConsoleFeeds\(\);[\s\S]*?identityChanged = true;[\s\S]*?if \(wasAuthDenied \|\| identityChanged\) \{\s*void loadOverview\(\);\s*\}/,
    "Expected loadCapabilities to refill overview immediately after identity-change clear",
  );
});

test("loadCapabilities preserves nav only for trusted unchanged identity on parse failure", () => {
  const dashboard = dashboardSource.dashboard;
  // Inventory malformations can still carry a different/missing identity; clear
  // authorized feeds unless trustedIdentityKey matches the prior key.
  assert.match(
    dashboard,
    /const loadCapabilities = useCallback\([\s\S]*?const parsed = parseConsoleCapabilities\(result\.data\);[\s\S]*?if \(!parsed\.ok\) \{[\s\S]*?const clearAction = resolveCapabilityParseFailureClear\(\{\s*priorIdentityKey:\s*lastCapabilityIdentityKeyRef\.current,\s*trustedIdentityKey:\s*parsed\.trustedIdentityKey,\s*\}\);[\s\S]*?if \(clearAction === "clear_authorized"\) \{\s*clearAuthorizedConsoleFeeds\(\{\s*clearCapabilities:\s*true,?\s*\}\);[\s\S]*?\} else \{\s*clearCapabilityGatedInventories\(\);/,
    "Expected parse failures to clear authorized feeds unless trusted identity is unchanged",
  );
  assert.match(
    dashboard,
    /resolveCapabilityParseFailureClear/,
    "Expected loadCapabilities to use resolveCapabilityParseFailureClear",
  );
});

test("capabilities 404 retains last identity key for recovery comparison", () => {
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /const lastCapabilityIdentityKeyRef = useRef<string \| null>\(null\);/,
    "Expected a ref that retains the last successful capability identity across 404 gaps",
  );
  const gatedClearStart = dashboard.indexOf("const clearCapabilityGatedInventories = useCallback");
  assert.ok(gatedClearStart > 0, "Expected clearCapabilityGatedInventories helper");
  const gatedClearEnd = dashboard.indexOf(
    "// Same-identity inventory can withdraw a feed without changing the epoch key.",
    gatedClearStart,
  );
  const gatedClearBody = dashboard.slice(gatedClearStart, gatedClearEnd);
  assert.match(
    gatedClearBody,
    /appliedCapabilitiesRef\.current = null;[\s\S]*?setCapabilities\(null\);/,
    "Expected 404 gated clear to drop negotiation inventory/capabilities",
  );
  assert.equal(
    gatedClearBody.includes("lastCapabilityIdentityKeyRef.current = null"),
    false,
    "Expected 404 gated clear not to wipe lastCapabilityIdentityKeyRef (needed for post-404 identity change)",
  );
  assert.equal(
    gatedClearBody.includes("setCapabilityIdentityKey"),
    false,
    "Expected identity tracking via lastCapabilityIdentityKeyRef only (no React identity state)",
  );
  const authClearStart = dashboard.indexOf("const clearAuthorizedConsoleFeeds = useCallback");
  const authClearEnd = gatedClearStart;
  const authClearBody = dashboard.slice(authClearStart, authClearEnd);
  assert.match(
    authClearBody,
    /if \(options\?\.clearCapabilities\) \{[\s\S]*?lastCapabilityIdentityKeyRef\.current = null;[\s\S]*?setCapabilities\(null\);/,
    "Expected clearCapabilities path to drop the retained identity key",
  );
  assert.match(
    dashboard,
    /lastCapabilityIdentityKeyRef\.current = parsed\.identityKey;[\s\S]*?setCapabilities\(nextCapabilities\);/,
    "Expected successful negotiation to update the retained identity key",
  );
});
test("loadCapabilities outage retains last-successful negotiation", () => {
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /if \(result\.status === 401 \|\| result\.status === 403\) \{[\s\S]*?setCapabilities\(null\);[\s\S]*?if \(result\.status === 404\) \{[\s\S]*?clearCapabilityGatedInventories\(\);[\s\S]*?const retained = appliedCapabilitiesRef\.current;[\s\S]*?if \(retained === null\) \{[\s\S]*?setCapabilities\(null\);[\s\S]*?return null;[\s\S]*?return retained;/,
    "Expected 5xx/network capability outages to keep appliedCapabilitiesRef rather than nulling negotiated feeds",
  );
  assert.match(
    dashboard,
    /capabilitiesForMutatingControls\(capabilities, capabilityError\)/,
    "Expected mutating controls to fail closed while capabilityError is set during retained outages",
  );
  assert.match(
    dashboard,
    /getWorkspaceOperatorControls\(\{[\s\S]*?capabilities:\s*mutatingCapabilities,/,
    "Expected operator controls to use mutatingCapabilities, not retained feed capabilities",
  );
  assert.match(
    dashboardSource.mutatingControls,
    /resolveRetryCapabilityGate\(\{\s*capabilities:\s*mutatingCapabilities,\s*capabilitiesReady,/,
    "Expected Retry gate to use mutatingCapabilities during capability outages",
  );
  assert.match(
    dashboardSource.liveStream,
    /frame\.type === "log"\) \{[\s\S]*?if \(!allowStreamLogs\) \{\s*return;\s*\}/,
    "Expected SSE log frames to be ignored when workspace_logs listing is unavailable",
  );
});

test("loadCapabilities 404 clears gated inventories without wiping overview navigation", () => {
  const dashboard = dashboardSource.dashboard;
  const gatedClearStart = dashboard.indexOf("const clearCapabilityGatedInventories = useCallback");
  assert.ok(gatedClearStart > 0, "Expected clearCapabilityGatedInventories helper");
  const gatedClearEnd = dashboard.indexOf(
    "// Same-identity inventory can withdraw a feed without changing the epoch key.",
    gatedClearStart,
  );
  assert.ok(gatedClearEnd > gatedClearStart, "Expected gated-clear helper before feed-withdrawal helper");
  const gatedClearBody = dashboard.slice(gatedClearStart, gatedClearEnd);
  assert.match(
    gatedClearBody,
    /setDashboardSummary\(null\);[\s\S]*?setCloudRuntime\(null\);[\s\S]*?setCapabilities\(null\);/,
    "Expected clearCapabilityGatedInventories to drop optional inventories and negotiation state",
  );
  assert.equal(
    gatedClearBody.includes("authorizedFeedEpochRef.current +="),
    false,
    "Expected 404 gated clear not to bump authorizedFeedEpochRef (would invalidate overview loads)",
  );
  assert.match(
    gatedClearBody,
    /dashboardSummaryRequestGenerationRef\.current \+= 1;[\s\S]*?cloudRuntimeRequestGenerationRef\.current \+= 1;[\s\S]*?mergeQueueRequestGenerationRef\.current \+= 1;[\s\S]*?resourceSaturationRequestGenerationRef\.current \+= 1;[\s\S]*?workspaceSummaryRequestGenerationRef\.current \+= 1;[\s\S]*?failureSummaryRequestGenerationRef\.current \+= 1;[\s\S]*?gatedDetailFeedGenerationRef\.current \+= 1;/,
    "Expected 404 gated clear to bump summary/cloud-runtime/merge-queue/diagnostic request generations and gatedDetailFeedGenerationRef so in-flight feeds cannot restore cleared inventories",
  );
  assert.match(
    dashboard,
    /const gatedDetailFeedGenerationRef = useRef\(0\);/,
    "Expected a gated-detail generation ref separate from authorizedFeedEpochRef",
  );
  assert.equal(
    gatedClearBody.includes("setOverview([])"),
    false,
    "Expected 404 gated clear to preserve overview workspace navigation",
  );
  assert.equal(
    gatedClearBody.includes("setSelectedId(null)"),
    false,
    "Expected 404 gated clear to preserve workspace selection",
  );
  assert.match(
    dashboard,
    /if \(result\.status === 404\) \{[\s\S]*?clearCapabilityGatedInventories\(\)[\s\S]*?setCapabilityError\(result\.message\)[\s\S]*?setCapabilitiesReady\(true\)[\s\S]*?return null;/,
    "Expected capabilities 404 to clear gated inventories only (legacy-safe navigation)",
  );
  assert.match(
    dashboard,
    /\/\/ Transient capability-endpoint outage \(5xx\/network\):/,
    "Expected retention comments to name 5xx/network only, not 404",
  );
  // 404 branch must appear before the retain path.
  const idx404 = dashboard.indexOf("if (result.status === 404)");
  const idxRetain = dashboard.indexOf("const retained = appliedCapabilitiesRef.current");
  assert.ok(idx404 > 0 && idxRetain > idx404, "Expected 404 clear before 5xx/network retain");
});
test("workspace rail omits log actions when showWorkspaceLogs is false", () => {
  const dashboard = dashboardSource.dashboard;
  const rail = dashboardSource.rail;
  const overview = dashboardSource.overview;
  assert.match(
    dashboard,
    /showWorkspaceLogs=\{showWorkspaceLogs\}/,
    "Expected dashboard to pass showWorkspaceLogs into ConsoleDashboardWorkspaceRail",
  );
  assert.match(
    rail,
    /\{props\.showWorkspaceLogs \? \(\s*<WorkspaceSelectionToolbar/,
    "Expected rail to omit WorkspaceSelectionToolbar when workspace_logs is unsupported",
  );
  assert.match(
    rail,
    /showWorkspaceLogs=\{props\.showWorkspaceLogs\}/,
    "Expected rail to forward showWorkspaceLogs into WorkspaceList",
  );
  assert.match(
    overview,
    /\{showWorkspaceLogs \? \(\s*<input[\s\S]*?Select \$\{item\.title\} for fullscreen logs/,
    "Expected WorkspaceList to omit log-selection checkboxes when logs are unsupported",
  );
  assert.match(
    overview,
    /\{showWorkspaceLogs \? \(\s*<button[\s\S]*?>\s*<Terminal[\s\S]*?>\s*Logs\s*<\/button>/,
    "Expected WorkspaceList to omit per-row Logs buttons when logs are unsupported",
  );
});

test("fullscreen log stream requires listing capability via allowStreamLogs", () => {
  const dashboard = dashboardSource.dashboard;
  const overlays = dashboardSource.overlays;
  const logs = dashboardSource.logs;
  // Split maintainability extraction wires fullscreen props through overlays.
  assert.match(
    dashboard,
    /allowFullscreenStreamLogs=\{allowFullscreenStreamLogs\}/,
    "Expected dashboard to pass allowFullscreenStreamLogs into ConsoleDashboardOverlays",
  );
  assert.match(
    overlays,
    /props\.logsFullscreen &&\s*props\.allowFullscreenLogs &&\s*props\.fullscreenWorkspaces\.length > 0/,
    "Expected overlays to unmount fullscreen logs when workspace_logs is withdrawn (not leave unsupported columns mounted)",
  );
  // Same-identity workspace_logs withdrawal must close fullscreen state — omit alone
  // leaves logsFullscreen true so the viewer remounts when listing is re-advertised.
  assert.match(
    dashboard,
    /if \(plan\.clearLogs\) \{[\s\S]*?setLogsFullscreen\(false\);[\s\S]*?setFullscreenWorkspaceIds\(\[\]\);/,
    "Expected clearLogs withdrawal to close the fullscreen log viewer, not only clear inspector tails",
  );
  // Missing/malformed negotiation (404 gated clear) also drops allowFullscreenLogs;
  // close open fullscreen so it does not remount when capabilities recover.
  const gatedClearStart = dashboard.indexOf("const clearCapabilityGatedInventories = useCallback");
  assert.ok(gatedClearStart > 0, "Expected clearCapabilityGatedInventories helper");
  const gatedClearEnd = dashboard.indexOf(
    "// Same-identity inventory can withdraw a feed without changing the epoch key.",
    gatedClearStart,
  );
  const gatedClearBody = dashboard.slice(gatedClearStart, gatedClearEnd);
  assert.match(
    gatedClearBody,
    /setLogsFullscreen\(false\);[\s\S]*?setFullscreenWorkspaceIds\(\[\]\);/,
    "Expected missing/malformed capability clear to close an open fullscreen log viewer",
  );
  assert.match(
    overlays,
    /allowStreamLogs=\{props\.allowFullscreenStreamLogs\}/,
    "Expected overlays to pass allowStreamLogs, not bare allowStream, so stream-only caps do not buffer hidden log frames",
  );
  assert.match(
    logs,
    /allowStreamLogs,\s*$/m,
    "Expected MultiWorkspaceLogsFullscreen/WorkspaceLogColumn to take allowStreamLogs (not bare allowStream)",
  );
  assert.match(
    logs,
    /if \(!allowStreamLogs\) \{\s*setStreamState\("idle"\);\s*return;\s*\}/,
    "Expected WorkspaceLogColumn to open /stream only when listing+stream (allowStreamLogs) is allowed",
  );
});

test("same-identity feed withdrawal invalidates gated reads without advancing auth epoch", () => {
  const dashboard = dashboardSource.dashboard;
  const withdrawStart = dashboard.indexOf(
    "const clearNewlyUnsupportedCapabilityFeeds = useCallback",
  );
  assert.ok(withdrawStart > 0, "Expected clearNewlyUnsupportedCapabilityFeeds helper");
  const withdrawEnd = dashboard.indexOf(
    "const invalidateAuthorizedFeedsIfContextChanged = useCallback",
    withdrawStart,
  );
  assert.ok(withdrawEnd > withdrawStart, "Expected withdrawal helper before context invalidation");
  const withdrawBody = dashboard.slice(withdrawStart, withdrawEnd);
  assert.equal(
    withdrawBody.includes("authorizedFeedEpochRef.current +="),
    false,
    "Expected same-identity withdrawal not to bump authorizedFeedEpochRef (would strand retry/operator mutations in submitting)",
  );
  assert.match(
    withdrawBody,
    /if \(capabilityFeedWithdrawalCleared\(plan\)\) \{\s*gatedDetailFeedGenerationRef\.current \+= 1;\s*\}/,
    "Expected same-identity withdrawal to bump gatedDetailFeedGenerationRef so in-flight detail/log reads cannot restore withdrawn data",
  );
  assert.match(
    withdrawBody,
    /if \(plan\.clearDashboardSummary\) \{[\s\S]*?dashboardSummaryRequestGenerationRef\.current \+= 1;/,
    "Expected fleet_summary withdrawal to bump dashboard-summary request generation",
  );
  assert.match(
    withdrawBody,
    /if \(plan\.clearFailures\) \{[\s\S]*?failureSummaryRequestGenerationRef\.current \+= 1;/,
    "Expected failures withdrawal to bump failure-summary request generation",
  );
  assert.match(
    withdrawBody,
    /if \(plan\.clearMergeQueue\) \{[\s\S]*?mergeQueueRequestGenerationRef\.current \+= 1;/,
    "Expected merge_queue withdrawal to bump merge-queue request generation",
  );
});

test("configured context query changes clear authorized state before capability response", () => {
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /configuredContextFingerprint/,
    "Expected dashboard to fingerprint configured hosted context query keys",
  );
  assert.match(
    dashboard,
    /configuredContextFingerprintRef/,
    "Expected a ref tracking the last observed context fingerprint",
  );
  assert.match(
    dashboard,
    /clearAuthorizedConsoleFeeds\(\{\s*clearCapabilities:\s*true\s*\}\)/,
    "Expected context-change invalidation to clear capabilities immediately",
  );
  assert.match(
    dashboard,
    /history\.(?:replace|push)State/,
    "Expected soft history URL changes to be observed for context invalidation",
  );
  assert.match(
    dashboard,
    /const loadCapabilities = useCallback\([\s\S]*?invalidateAuthorizedFeedsIfContextChanged\([\s\S]*?const generation = \+\+capabilityRequestGenerationRef\.current;/,
    "Expected loadCapabilities to invalidate on context change before starting the capability fetch",
  );
  // Soft tenant switches must not start overview concurrent with capabilities:
  // an identity clear advances the overview epoch and would blank the list.
  assert.match(
    dashboard,
    /const syncConfiguredContext = \(\) => \{[\s\S]*?await loadCapabilities\(\);\s*await loadOverview\(\);/,
    "Expected tenant rebootstrap to sequence overview after capability identity is applied",
  );
  assert.doesNotMatch(
    dashboard,
    /const syncConfiguredContext = \(\) => \{[\s\S]*?void loadCapabilities\(\);\s*void loadOverview\(\);/,
    "Expected tenant rebootstrap not to start overview concurrent with capabilities",
  );
});

test("operator controls block renders success warnings", () => {
  const blockSource = extractFunctionSource("OperatorControlsBlock");

  assert.match(blockSource, /state\.status === "success" && state\.warnings\.length > 0/);
  assert.match(blockSource, /state\.warnings\.map\(\(warning\) =>/);
  assert.match(blockSource, /warning\.message \|\| warning\.warning_code/);
});

test("operator controls block keeps inactive reasons in hover tooltips", () => {
  const blockSource = extractFunctionSource("OperatorControlsBlock");

  assert.match(blockSource, /const tooltip = reason \? `\$\{control\.label\}: \${reason}` : null;/);
  assert.match(blockSource, /role="tooltip"/);
  assert.match(blockSource, /group-hover:not-sr-only/);
  assert.match(blockSource, /group-focus-within:not-sr-only/);
  assert.doesNotMatch(blockSource, />\{reason\}<\/span>/);
});

test("operator control tooltip-describedby target follows disabled focus state", () => {
  const blockSource = extractFunctionSource("OperatorControlsBlock");

  assert.match(blockSource, /tabIndex=\{disabled && reason \? 0 : undefined\}/);
  assert.match(
    blockSource,
    /aria-describedby=\{disabled && reason \? `operator-control-tip-\$\{workspaceId\}-\$\{control\.action\}` : undefined\}/,
  );
});

test("workspace retry button gates on negotiated control capabilities", () => {
  const summarySource = extractFunctionSource("WorkspaceSummary");
  const dashboard = dashboardSource.dashboard;
  const mutating = dashboardSource.mutatingControls;

  assert.match(summarySource, /resolveRetryCapabilityGate/);
  assert.match(summarySource, /capabilitiesReady/);
  assert.match(summarySource, /retryDisabled = retrySubmitting \|\| !retryGate\.enabled/);
  assert.match(
    mutating,
    /resolveRetryCapabilityGate\(\{\s*capabilities:\s*mutatingCapabilities,\s*capabilitiesReady,/,
  );
  assert.match(mutating, /if \(!retryGate\.enabled\) \{\s*return;\s*\}/);
  assert.match(dashboard, /useWorkspaceMutatingControls\(/);
});

test("workspace retry is guarded by authorized feed epoch", () => {
  const mutating = dashboardSource.mutatingControls;
  const retryStart = mutating.indexOf("const retrySelectedWorkspace = useCallback");
  const operatorStart = mutating.indexOf("const runWorkspaceOperatorAction = useCallback");
  assert.ok(retryStart >= 0, "Expected retrySelectedWorkspace callback");
  assert.ok(operatorStart > retryStart, "Expected runWorkspaceOperatorAction after retrySelectedWorkspace");
  const retrySource = mutating.slice(retryStart, operatorStart);

  assert.match(
    retrySource,
    /if \(!retryGate\.enabled\) \{\s*return;\s*\}[\s\S]*?const epoch = authorizedFeedEpochRef\.current;\s*setRetryState\(\{ status: "submitting" \}\);/,
    "Expected retrySelectedWorkspace to capture authorizedFeedEpochRef when the capability gate succeeds, before submitting",
  );
  assert.match(
    retrySource,
    /epoch !== authorizedFeedEpochRef\.current/,
    "Expected retrySelectedWorkspace to discard responses when authorizedFeedEpochRef advances",
  );
  const epochMismatchReturn = retrySource.match(
    /if \(epoch !== authorizedFeedEpochRef\.current\) \{\s*return;\s*\}/,
  );
  assert.ok(
    epochMismatchReturn,
    "Expected retrySelectedWorkspace to return without follow-up refreshes when the auth epoch advances",
  );
  const afterFirstEpochReturn = retrySource.slice(
    retrySource.indexOf(epochMismatchReturn[0]) + epochMismatchReturn[0].length,
  );
  assert.match(
    afterFirstEpochReturn,
    /const caps = await loadCapabilities\(\);\s*if \(epoch !== authorizedFeedEpochRef\.current\) \{\s*return;\s*\}/,
    "Expected retrySelectedWorkspace to re-check the auth epoch before follow-up refreshes",
  );
});

test("operator action state is guarded by current workspace selection", () => {
  const dashboard = dashboardSource.dashboard;
  const mutating = dashboardSource.mutatingControls;
  const preferencesHook = readFileSync(
    new URL("../hooks/use-operator-theme-preferences.ts", import.meta.url),
    "utf8",
  );

  assert.match(preferencesHook, /const selectedIdRef = useRef<string \| null>\(selectedId\);/);
  assert.match(dashboard, /const \{ selectedId, selectedIdRef, setSelectedId \} = useWorkspaceSelectionUrl\(/);
  assert.match(mutating, /const workspaceId = selectedId;/);
  assert.match(mutating, /operatorIdempotencyKey\(action, workspaceId\)/);
  assert.match(mutating, /operatorActionPath\(action, workspaceId\)/);
  assert.match(mutating, /selectedIdRef\.current !== workspaceId/);
  assert.match(
    mutating,
    /const runWorkspaceOperatorAction = useCallback\([\s\S]*?const epoch = authorizedFeedEpochRef\.current;[\s\S]*?epoch !== authorizedFeedEpochRef\.current/,
    "Expected operator actions to capture and discard on authorizedFeedEpochRef advance",
  );
});

test("inspector omits unsupported diagnostic panels instead of empty shells", () => {
  const inspector = dashboardSource.inspector;
  assert.match(inspector, /\{showWorkspaceRuntime \? <RuntimePanel runtime=\{detail\.runtime\} \/> : null\}/);
  assert.match(
    inspector,
    /\{showWorkspaceOperations \? \(\s*<OperationsPanel operations=\{detail\.operations\} \/>\s*\) : null\}/,
  );
  assert.match(inspector, /\{showWorkspaceEvents \? <EventsPanel events=\{detail\.events\} \/> : null\}/);
  assert.match(inspector, /\{showWorkspaceLogs \? \(\s*<LogsPanel/);
  assert.doesNotMatch(
    inspector,
    /<RuntimePanel runtime=\{showWorkspaceRuntime \? detail\.runtime : null\} \/>/,
  );
});

test("fleet health strip omits unsupported fleet_summary KPIs", () => {
  assert.match(dashboardSource.dashboard, /includeSummary:\s*fleetSummaryAvailable/);
  assert.match(dashboardSource.dashboard, /showFleetHealthStrip/);
  assert.match(
    dashboardSource.dashboard,
    /\{showFleetHealthStrip \? \(\s*<FleetHealthStrip/,
  );
});

test("dashboard paths go through the console URL builder", () => {
  assert.match(dashboardSource.dashboard, /from "@\/lib\/console-urls"/);
  assert.match(dashboardSource.dashboard, /awfPath\(/);
  assert.doesNotMatch(dashboardSource.dashboard, /["'`]\/api\/awf/);
  assert.match(dashboardSource.shared, /from "@\/lib\/console-urls"/);
  assert.match(dashboardSource.shared, /operatorPath\(/);
  assert.match(dashboardSource.shared, /awfPath\(/);
  assert.doesNotMatch(dashboardSource.shared, /["'`]\/api\/(?:awf|operator)/);
  assert.match(dashboardSource.logs, /from "@\/lib\/console-urls"/);
  assert.match(dashboardSource.logs, /awfPath\(/);
  assert.doesNotMatch(dashboardSource.logs, /["'`]\/api\/awf/);
});

test("extractPrNumberFromHref regex is forge-neutral (GitHub + Bitbucket)", () => {
  assert.match(dashboardSource.shared, /pull\(\?:-requests\)\?/);
});

// Plain-JS mirror of extractPrNumberFromHref (console-dashboard-shared.tsx) for runtime
// extraction tests. The source-text test above keeps the regex pattern in sync.
function extractPrNumberFromHref(href) {
  const match = href.match(/\/pull(?:-requests)?\/(\d+)(?:[/?#]|$)/);
  if (!match) return null;
  const number = Number(match[1]);
  return Number.isSafeInteger(number) && number > 0 ? number : null;
}

test("extractPrNumberFromHref extracts PR number from GitHub and Bitbucket URLs", () => {
  assert.equal(extractPrNumberFromHref("https://github.com/org/repo/pull/42"), 42);
  assert.equal(extractPrNumberFromHref("https://github.com/org/repo/pull/42/"), 42);
  assert.equal(extractPrNumberFromHref("https://github.com/org/repo/pull/42?foo=bar"), 42);
  assert.equal(extractPrNumberFromHref("https://github.com/org/repo/pull/42#comment-1"), 42);
  assert.equal(extractPrNumberFromHref("https://bitbucket.org/org/repo/pull-requests/42"), 42);
  assert.equal(extractPrNumberFromHref("https://bitbucket.org/org/repo/pull-requests/42/"), 42);
});

test("extractPrNumberFromHref returns null for non-PR URLs and edge cases", () => {
  assert.equal(extractPrNumberFromHref("https://github.com/org/repo/issues/42"), null);
  assert.equal(extractPrNumberFromHref("https://github.com/org/repo/pull/"), null);
  assert.equal(extractPrNumberFromHref("https://github.com/org/repo/pull/0"), null);
  assert.equal(extractPrNumberFromHref(""), null);
});

test("formatPrLinkLabel in logs view passes pr_number", () => {
  assert.match(dashboardSource.logs, /formatPrLinkLabel\(workspace\.pr_url,\s*workspace\.pr_number\)/);
});

test("formatPrLinkLabel in detail view passes pr_number", () => {
  assert.match(dashboardSource.detail, /formatPrLinkLabel\(overview\.pr_url,\s*overview\.pr_number\)/);
});

function extractFunctionSource(functionName) {
  const markers = [`export function ${functionName}(`, `function ${functionName}(`];
  let matchSource = null;
  let start = -1;

  for (const source of Object.values(dashboardSource)) {
    for (const marker of markers) {
      const found = source.indexOf(marker);
      if (found >= 0) {
        start = found;
        matchSource = source;
        break;
      }
    }
    if (matchSource) {
      break;
    }
  }

  assert.notEqual(start, -1, `Expected ${functionName} to exist`);

  const nextFunction = matchSource.indexOf("\nexport function ", start + 1);
  const nextPrivateFunction = matchSource.indexOf("\nfunction ", start + 1);
  const next = [nextFunction, nextPrivateFunction]
    .filter((idx) => idx > start)
    .reduce((a, b) => (a === -1 || (b !== -1 && b < a) ? b : a), -1);
  const end = next === -1 ? matchSource.length : next;
  return matchSource.slice(start, end);
}
