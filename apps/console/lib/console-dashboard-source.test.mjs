import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const dashboardSource = {
  dashboard: readFileSync(new URL("../components/console-dashboard.tsx", import.meta.url), "utf8"),
  liveStream: readFileSync(new URL("../hooks/use-workspace-live-stream.ts", import.meta.url), "utf8"),
  logTails: readFileSync(new URL("../hooks/use-workspace-log-tails.ts", import.meta.url), "utf8"),
  detailLoader: readFileSync(
    new URL("../hooks/use-workspace-detail-loader.ts", import.meta.url),
    "utf8",
  ),
  serializedPoll: readFileSync(
    new URL("../hooks/use-serialized-periodic-load.ts", import.meta.url),
    "utf8",
  ),
  gatedPoll: readFileSync(
    new URL("../hooks/use-capability-gated-poll.ts", import.meta.url),
    "utf8",
  ),
  mutatingControls: readFileSync(
    new URL("../hooks/use-workspace-mutating-controls.ts", import.meta.url),
    "utf8",
  ),
  fleetFeeds: readFileSync(
    new URL("../hooks/use-console-fleet-feeds.ts", import.meta.url),
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

test("task details modal does not render the legacy Effort fact", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6f_-KV: requested and
  // confirmed facts already distinguish policy/default/auto from execution
  // evidence. A leftover formatAgentEffort "Effort" row restates policy effort
  // beside the requested value and recreates that mix-up.
  const modalSource = extractFunctionSource("TaskDetailsModal");

  assert.match(modalSource, /label="Requested effort"/);
  assert.match(modalSource, /label="Confirmed model"/);
  assert.doesNotMatch(modalSource, /formatAgentEffort/);
  assert.doesNotMatch(modalSource, /label="Effort"/);
  assert.match(
    modalSource,
    /formatAgentLabel\(\{\s*\.\.\.workspace,\s*agent_effort:\s*null\s*\}\)/,
    "Expected TaskDetailsModal Agent fact to omit legacy policy/default/auto effort",
  );
});

test("workspace summary does not embed effort in the Agent fact", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6gApGD: Requested effort
  // already shows policy/default/auto effort. Leaving agent_effort in the Agent
  // label repeats it and can be mistaken for confirmed execution metadata.
  const summarySource = extractFunctionSource("WorkspaceSummary");

  assert.match(summarySource, /label="Requested effort"/);
  assert.match(
    summarySource,
    /formatAgentIdentityLabel\(/,
    "Expected WorkspaceSummary Agent fact to omit legacy policy/default/auto effort",
  );
  assert.doesNotMatch(summarySource, /formatAgentLabel\(/);
});

test("task details modal shows duration when duration_seconds is recorded", () => {
  const modalSource = extractFunctionSource("TaskDetailsModal");

  assert.match(
    modalSource,
    /recordedDurationLabel\(workspace\.duration_seconds\)/,
    "Expected TaskDetailsModal to read duration_seconds from the hosted overview",
  );
  assert.match(
    modalSource,
    /recordedDuration != null \? \(\s*<Fact label="Duration" value=\{recordedDuration\} \/>\s*\) : null/,
    "Expected TaskDetailsModal to render a Duration fact when duration_seconds is recorded",
  );
});

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

test("fleet grid stays one column when capacity section is absent", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6gApGB: the 2xl
  // two-track template is only for capacity beside merge-queue. Applying it
  // when showCapacitySection is false leaves the advertised queue in the
  // first track and an empty second column.
  const fleet = extractFunctionSource("ConsoleDashboardFleetPanels");

  assert.match(
    fleet,
    /className=\{[\s\S]*?showCapacitySection[\s\S]*?2xl:grid-cols-\[minmax\(0,1fr\)_minmax\(460px,0\.85fr\)\]/,
    "Expected the 2xl two-column fleet template to be gated on showCapacitySection",
  );
  assert.doesNotMatch(
    fleet,
    /className="[^"]*2xl:grid-cols-\[minmax\(0,1fr\)_minmax\(460px,0\.85fr\)\]"/,
    "Expected the two-column fleet template not to be an unconditional class string",
  );
  assert.match(
    fleet,
    /: "grid min-w-0 grid-cols-1 gap-4 p-4 pb-0"/,
    "Expected a capacity-absent fleet grid to stay one explicit column",
  );
  assert.match(
    fleet,
    /showCapacitySection \? "scroll-mt-14 2xl:col-span-2" : "scroll-mt-14"/,
    "Expected failures to span both tracks only when the capacity column exists",
  );
  assert.doesNotMatch(
    fleet,
    /className="scroll-mt-14 2xl:col-span-2"/,
    "Expected failures col-span-2 not to force an implicit second track without capacity",
  );
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
  const fleetFeeds = dashboardSource.fleetFeeds;
  assert.match(
    dashboard,
    /useConsoleFleetFeeds\(\{/,
    "Expected fleet snapshot loaders to live in useConsoleFleetFeeds",
  );
  assert.match(
    dashboard,
    /const loadOverview = useCallback\([\s\S]*?const epoch = authorizedFeedEpochRef\.current;[\s\S]*?if \(\s*epoch !== authorizedFeedEpochRef\.current/,
    "Expected loadOverview to capture and discard on authorizedFeedEpochRef advance",
  );
  for (const loader of [
    "loadResourceSaturation",
    "loadDashboardSummary",
    "loadCloudRuntime",
    "loadWorkspaceSummary",
    "loadMergeQueue",
    "loadFailureSummary",
  ]) {
    assert.match(
      fleetFeeds,
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
    dashboardSource.detailLoader,
    /const workspaceDetailRequestGenerationRef = useRef\(0\);/,
    "Expected a workspace-detail request-generation ref so overlapping polls stay monotonic",
  );
  assert.match(
    dashboardSource.detailLoader,
    /const loadWorkspace = useCallback\([\s\S]*?const epoch = authorizedFeedEpochRef\.current;[\s\S]*?const gatedGeneration = gatedDetailFeedGenerationRef\.current;[\s\S]*?const generation = \+\+workspaceDetailRequestGenerationRef\.current;[\s\S]*?if \(\s*epoch !== authorizedFeedEpochRef\.current \|\|\s*generation !== workspaceDetailRequestGenerationRef\.current \|\|\s*selectedIdRef\.current !== workspaceId\s*\)/,
    "Expected loadWorkspace to discard after epoch/request generation advance or selection change",
  );
  assert.match(
    dashboardSource.detailLoader,
    /workspaceDetailVisitRef\.current \+= 1;[\s\S]*?workspaceDetailVisitGenerationFloorRef\.current = \+\+workspaceDetailRequestGenerationRef\.current;[\s\S]*?revokedWorkspaceDetailGenerationRef\.current = 0;[\s\S]*?appliedWorkspaceDetailGenerationRef\.current = 0;/,
    "Expected a selection change to start a new detail visit, advance request generation, and drop denial watermarks",
  );
  assert.match(
    dashboardSource.detailLoader,
    /visit !== workspaceDetailVisitRef\.current/,
    "Expected a late base-detail 401/403 to be ignored after the operator re-opens the workspace",
  );
  assert.match(
    dashboardSource.detailLoader,
    /deniedGeneration <= workspaceDetailVisitGenerationFloorRef\.current/,
    "Expected a previous-visit 401/403 to be dropped even when selectedId matches the re-opened workspace",
  );
  assert.match(
    dashboardSource.detailLoader,
    /if \(gatedGeneration !== gatedDetailFeedGenerationRef\.current\) \{[\s\S]*?const stampsForExternalDrop =[\s\S]*?const dropped = gatedDetailDropsSince\(stampsForExternalDrop, gatedGeneration\);[\s\S]*?if \(allGatedDetailFeedsDropped\(dropped\)\) \{[\s\S]*?setDetail\(\(current\) => \(\{[\s\S]*?workspace: workspaceFromDetailResult\(current\.workspace, workspace\),[\s\S]*?\}\)\)[\s\S]*?return;/,
    "Expected a gated-detail generation bump to union external drops since capture, then apply the basic workspace GET and skip optional feeds",
  );
  assert.match(
    dashboardSource.logTails,
    /const logTailRequestGenerationRef = useRef<Record<string, number>>\(\{\}\);/,
    "Expected a per-stream log-tail request-generation ref so overlapping tails stay monotonic",
  );
  assert.match(
    dashboardSource.logTails,
    /const loadLogTail = useCallback\([\s\S]*?const epoch = authorizedFeedEpochRef\.current;[\s\S]*?const gatedGeneration = gatedDetailFeedGenerationRef\.current;[\s\S]*?const generationKey = `\$\{workspaceId\}:\$\{stream\.stream_id\}`;[\s\S]*?const generation = \(logTailRequestGenerationRef\.current\[generationKey\] \?\? 0\) \+ 1;[\s\S]*?logTailRequestGenerationRef\.current\[generationKey\] = generation;[\s\S]*?if \(\s*epoch !== authorizedFeedEpochRef\.current \|\|\s*generation !== logTailRequestGenerationRef\.current\[generationKey\] \|\|\s*selectedIdRef\.current !== workspaceId \|\|\s*logListingAuthDeniedRef\.current \|\|\s*workspaceDetailAuthDeniedRef\.current\s*\)/,
    "Expected loadLogTail to discard after epoch/per-stream generation advance, selection change, listing denial, or base-detail denial",
  );
  assert.match(
    dashboardSource.logTails,
    /const gatedGenerationAdvanced =\s*gatedGeneration !== gatedDetailFeedGenerationRef\.current;[\s\S]*?if \(gatedGenerationAdvanced && \(result\.ok \|\| !isLogTailAuthFailure\(result\.status\)\)\) \{\s*settleInFlight\(\);\s*return;\s*\}/,
    "Expected a gated-detail generation bump to discard non-auth tail results without dropping sibling 401/403s",
  );
  for (const loader of [
    "loadResourceSaturation",
    "loadWorkspaceSummary",
    "loadFailureSummary",
  ]) {
    assert.match(
      fleetFeeds,
      new RegExp(
        `const ${loader} = useCallback\\([\\s\\S]*?const epoch = authorizedFeedEpochRef\\.current;[\\s\\S]*?const gatedGeneration = gatedDetailFeedGenerationRef\\.current;[\\s\\S]*?const generation = \\+\\+\\w+RequestGenerationRef\\.current;[\\s\\S]*?if \\(\\s*epoch !== authorizedFeedEpochRef\\.current \\|\\|\\s*gatedGeneration !== gatedDetailFeedGenerationRef\\.current\\s*\\)`,
      ),
      `Expected ${loader} to discard after authorized epoch or gated-detail advance`,
    );
  }
  assert.match(
    fleetFeeds,
    /const loadMergeQueue = useCallback\([\s\S]*?const epoch = authorizedFeedEpochRef\.current;[\s\S]*?const gatedGeneration = gatedDetailFeedGenerationRef\.current;[\s\S]*?const generation = \+\+mergeQueueRequestGenerationRef\.current;[\s\S]*?if \(\s*epoch !== authorizedFeedEpochRef\.current \|\|\s*gatedGeneration !== gatedDetailFeedGenerationRef\.current\s*\)/,
    "Expected loadMergeQueue to discard after authorized epoch or gated-detail advance",
  );
});

test("periodic overview polls skip while a collection is still in flight", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6f-kK6: a slow
  // multi-page overview must not be cancelled by the next pollMs tick.
  // Bumping overviewRequestGenerationRef on every interval tick makes the
  // unfinished collector return null; if every collection exceeds the interval,
  // the rail stays empty or permanently stale. Periodic loads serialize:
  // the next poll is scheduled only after the previous invocation settles,
  // and a still-paging collection is not replaced.
  // Filter changes and explicit refreshes still call loadOverview directly
  // and advance generation so a newer query can supersede an in-flight load.
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /const overviewLoadInFlightRef = useRef\(false\);/,
    "Expected an overview in-flight latch so periodic polls can serialize",
  );
  assert.match(
    dashboard,
    /const loadOverview = useCallback\([\s\S]*?const generation = \+\+overviewRequestGenerationRef\.current;[\s\S]*?overviewLoadInFlightRef\.current = true;[\s\S]*?finally \{[\s\S]*?if \(generation === overviewRequestGenerationRef\.current\) \{\s*overviewLoadInFlightRef\.current = false;\s*\}/,
    "Expected loadOverview to hold the in-flight latch until the latest generation finishes",
  );
  assert.doesNotMatch(
    dashboard,
    /setInterval\(\s*(?:\(\)\s*=>\s*)?(?:void\s+)?loadOverview\(\)/,
    "Expected no wall-clock overview interval that can cancel an in-flight collection",
  );
  assert.match(
    dashboard,
    /useSerializedPeriodicLoad\(\s*true,\s*loadOverview,\s*overviewLoadInFlightRef,/,
    "Expected periodic overview polls to use the serialized loader",
  );
  assert.match(
    dashboardSource.serializedPoll,
    /const scheduleNext = \(\) => \{[\s\S]*?window\.setTimeout\(\(\) => \{[\s\S]*?if \(inFlightRef\.current\) \{[\s\S]*?scheduleNext\(\);[\s\S]*?return;[\s\S]*?\}[\s\S]*?start\(\);[\s\S]*?\}, pollMs\);[\s\S]*?\};[\s\S]*?const start = \(\) => \{[\s\S]*?void Promise\.resolve\(load\(\)\)\.finally\(\(\) => \{[\s\S]*?scheduleNext\(\);[\s\S]*?\}\);[\s\S]*?\};[\s\S]*?start\(\);/,
    "Expected periodic polls to chain after settle and skip while a collection is in flight",
  );
  assert.match(
    dashboardSource.serializedPoll,
    /load: \(\) => void \| Promise<unknown>/,
    "Expected serialized periodic load to accept a valued promise so loadCapabilities stays chained",
  );
});

test("periodic workspace detail polls skip while a request is still in flight", () => {
  // Same slow-poll starvation as overview: a wall-clock interval that calls
  // loadWorkspace every pollMs advances workspaceDetailRequestGenerationRef,
  // so overlapping successful completions never apply setDetail.
  const dashboard = dashboardSource.dashboard;
  const detail = dashboardSource.detailLoader;
  assert.match(
    detail,
    /const workspaceDetailLoadInFlightRef = useRef\(false\);/,
    "Expected a workspace-detail in-flight latch so periodic polls can serialize",
  );
  assert.match(
    detail,
    /const loadWorkspace = useCallback\([\s\S]*?const generation = \+\+workspaceDetailRequestGenerationRef\.current;[\s\S]*?workspaceDetailLoadInFlightRef\.current = true;[\s\S]*?finally \{[\s\S]*?if \(generation === workspaceDetailRequestGenerationRef\.current\) \{\s*workspaceDetailLoadInFlightRef\.current = false;\s*\}/,
    "Expected loadWorkspace to hold the in-flight latch until the latest generation finishes",
  );
  assert.doesNotMatch(
    dashboard,
    /setInterval\(\s*(?:\(\)\s*=>\s*)?(?:void\s+)?loadWorkspace\(/,
    "Expected no wall-clock workspace-detail interval that can cancel an in-flight request",
  );
  assert.match(
    detail,
    /useSerializedPeriodicLoad\(\s*selectedId !== null,\s*loadSelectedWorkspace,\s*workspaceDetailLoadInFlightRef,/,
    "Expected periodic detail polls to chain through the serialized loader",
  );
  assert.match(
    dashboard,
    /selectedIdRef\.current[\s\S]*?loadWorkspace\(selectedWorkspaceId\)/,
    "Expected explicit refresh to call loadWorkspace so it can supersede an in-flight periodic detail load",
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
    /collected\.truncated[\s\S]*?truncationReason === "missing_cursor"[\s\S]*?omitted a continuation cursor[\s\S]*?Workspace list truncated/,
    "Expected loadOverview to surface a missing-cursor envelope separately from the page-ceiling warning",
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
    dashboardSource.detailLoader,
    /const loadWorkspace = useCallback\([\s\S]*?\} else if \(!workspaceDetailAuthDeniedRef\.current && !eventFeedAuthDeniedRef\.current\) \{\s*setError\(null\);\s*\}/,
    "Expected loadWorkspace success to clear only the workspace-detail error setter, and not while a base-detail or event-feed denial still owns the banner",
  );
  assert.match(
    dashboardSource.detailLoader,
    /const eventDenialOwnsBanner =\s*eventFeedAuthDeniedRef\.current &&\s*!\(events != null && feedAuthDenied\(events\) && firstFailure === events\);/,
    "Expected a latched event-feed denial to keep the banner when an earlier-listed sibling outage is the first failure",
  );
  assert.doesNotMatch(
    dashboardSource.detailLoader,
    /setOverviewError|setOverviewTruncationWarning/,
    "Expected loadWorkspace not to touch overview error or truncation warning",
  );
  assert.match(
    dashboard,
    /overviewTruncationWarning \? <ErrorBanner message=\{overviewTruncationWarning\} \/> : null/,
    "Expected truncation warning to render independently of the feed error banners",
  );
});

test("overview and workspace-detail errors clear only when their own feed succeeds", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6gAjDt: a successful
  // overview poll must not erase a retained runtime/events/operations/logs
  // warning, and a recovered detail load must not dismiss an overview outage.
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /const \[overviewError, setOverviewError\] = useState<string \| null>\(null\);\s*const \[workspaceDetailError, setWorkspaceDetailError\] = useState<string \| null>\(null\);/,
    "Expected independent overview and workspace-detail error state",
  );
  assert.match(
    dashboard,
    /setOverviewError\(null\);\s*setOverview\(/,
    "Expected overview success to clear only the overview error",
  );
  assert.doesNotMatch(
    dashboard,
    /setOverviewError\(null\);[\s\S]{0,160}?setWorkspaceDetailError\(null\);\s*setOverview\(/,
    "Expected overview success not to clear the workspace-detail error",
  );
  assert.match(
    dashboard,
    /if \(pageError !== null\) \{\s*setOverviewError\(pageError\);\s*\}/,
    "Expected overview page failure to record only the overview error",
  );
  assert.match(
    dashboard,
    /setError: setWorkspaceDetailError,/,
    "Expected detail loader and live stream to write the workspace-detail error slot",
  );
  assert.doesNotMatch(
    dashboard,
    /setError: setOverviewError,/,
    "Expected detail loader and live stream not to write the overview error slot",
  );
  assert.match(
    dashboard,
    /overviewError \? <ErrorBanner message=\{overviewError\} \/> : null/,
    "Expected overview error to render on its own banner",
  );
  assert.match(
    dashboard,
    /workspaceDetailError \? <ErrorBanner message=\{workspaceDetailError\} \/> : null/,
    "Expected workspace-detail error to render on its own banner",
  );
  assert.match(
    dashboard,
    /workspaceDetailError=\{workspaceDetailError\}/,
    "Expected the inspector to receive the workspace-detail error so the drawer does not hide the warning",
  );
  assert.match(
    dashboardSource.inspector,
    /workspaceDetailError \? \([\s\S]*?<ErrorBanner message=\{workspaceDetailError\} \/>/,
    "Expected the inspector drawer to show the workspace-detail error beside retained snapshots",
  );
  assert.match(
    dashboard,
    /setLogEntries\(\[\]\);\s*setStreamOffsets\(\{\}\);\s*setWorkspaceDetailError\(null\);/,
    "Expected selection changes to drop the previous workspace-detail error",
  );
});

test("loadWorkspace retains last-good diagnostics on transient feed failure; clears on gated-off or 401/403", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6f9g8g: inspector
  // runtime/events/operations/logs must not wipe last-successful snapshots on
  // polling blips; gated-off feeds still clear, and feed-level 401/403 drops cache.
  const dashboard = dashboardSource.detailLoader;
  const loadIdx = dashboard.indexOf("const loadWorkspace = useCallback");
  assert.ok(loadIdx > 0, "Expected loadWorkspace callback");
  const loadEnd = dashboard.indexOf("const loadSelectedWorkspace", loadIdx);
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
    /const feedAuthDenied = \(result: ApiEnvelope<unknown> \| null \| undefined\) =>\s*result != null && result\.ok === false && \(result\.status === 401 \|\| result\.status === 403\)/,
    "Expected feedAuthDenied to accept success or failure envelopes so listing 401/403 typechecks",
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

test("loadLogTail retains last-successful tails on transient refresh failure", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6gAHZU: automatic
  // selected-stream tail refresh must not wipe prior tails/live entries on
  // network or 5xx; surface the refresh error separately. 401/403 still drops
  // authorized stream contents.
  const tails = dashboardSource.logTails;
  const loadIdx = tails.indexOf("const loadLogTail = useCallback");
  assert.ok(loadIdx > 0, "Expected loadLogTail callback");
  const loadEnd = tails.indexOf("const reloadSelectedLogs = useCallback", loadIdx);
  assert.ok(loadEnd > loadIdx, "Expected loadLogTail callback end");
  const body = tails.slice(loadIdx, loadEnd);

  assert.match(
    body,
    /isLogTailAuthFailure\(result\.status\)/,
    "Expected loadLogTail to distinguish feed-level 401/403 from transient outages",
  );
  const authIdx = body.indexOf("if (isLogTailAuthFailure(result.status))");
  assert.ok(authIdx > 0, "Expected an auth-failure branch inside loadLogTail");
  const transientMarker = "Transient network/5xx";
  const transientIdx = body.indexOf(transientMarker, authIdx);
  assert.ok(transientIdx > authIdx, "Expected a non-auth refresh-error path");
  const authBody = body.slice(authIdx, transientIdx);
  assert.match(
    authBody,
    /logTailDeniedStreamKeysRef\.current\.add\(logTailRefreshErrorKey\(workspaceId, stream\.stream_id\)\);[\s\S]*?syncWorkspaceLogTailAuthDenied\(\s*logTailDeniedStreamKeysRef\.current,\s*workspaceId,\s*logTailAuthDeniedRef,\s*setLogTailAuthDenied,\s*\)/,
    "Expected 401/403 to record the denied stream, then sync the tail-denial latch so the inspector EventSource closes",
  );
  assert.match(
    tails,
    /function syncWorkspaceLogTailAuthDenied\([\s\S]*?const stillDenied = workspaceHasDeniedLogTail\(deniedStreamKeys, workspaceId\);[\s\S]*?if \(logTailAuthDeniedRef\.current !== stillDenied\) \{\s*logTailAuthDeniedRef\.current = stillDenied;\s*setLogTailAuthDenied\(stillDenied\);\s*\}/,
    "Expected tail-denial latch updates to follow remaining denied streams rather than a direct true assignment",
  );
  assert.match(
    authBody,
    /current\.filter\(\(entry\) => entry\.workspaceId !== workspaceId\)/,
    "Expected 401/403 to drop prior tail and live entries for the revoked workspace",
  );
  assert.match(authBody, /setStreamOffsets\(\{\}\)/, "Expected 401/403 to clear retained stream offsets");
  assert.match(authBody, /tail-error:/, "Expected 401/403 to record an error line after clearing authorized tails");

  const transientBody = body.slice(transientIdx, body.indexOf("const tailEntry", transientIdx));
  assert.match(
    transientBody,
    /logTailRefreshErrorKey\(workspaceId, stream\.stream_id\)/,
    "Expected transient failures to record a per-stream refresh error",
  );
  assert.doesNotMatch(
    transientBody,
    /current\.filter\(/,
    "Expected transient refresh failure not to delete the last-successful tail or live entries",
  );
  assert.doesNotMatch(
    transientBody,
    /tail-error:/,
    "Expected transient refresh failure not to replace retained diagnostics with an error line",
  );
  assert.match(
    body,
    /setLogTailRefreshErrors\(\(current\) =>\s*omitLogTailRefreshError\(current, workspaceId, stream\.stream_id\)\)/,
    "Expected a successful tail to clear that stream's refresh error",
  );
  assert.match(
    tails,
    /logTailRefreshError/,
    "Expected the hook to expose the refresh error separately from log entries",
  );
  assert.match(
    dashboardSource.inspector,
    /refreshError=\{logTailRefreshError\}/,
    "Expected the inspector logs panel to render the refresh error separately",
  );
  assert.match(
    dashboardSource.logs,
    /stale=\{Boolean\(refreshError\) && entries\.length > 0\}/,
    "Expected a retained tail snapshot to be marked stale while the refresh error is shown",
  );
});

test("tail authorization denial closes the inspector live stream", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6gARNY: a /logs/{stream}
  // 401/403 while listing stays reachable must latch denial and tear down the
  // selected workspace EventSource. Listing success must not clear that latch.
  const tails = dashboardSource.logTails;
  const authIdx = tails.indexOf("if (isLogTailAuthFailure(result.status))");
  assert.ok(authIdx > 0, "Expected an auth-failure branch inside loadLogTail");
  const authEnd = tails.indexOf("Transient network/5xx", authIdx);
  const authBody = tails.slice(authIdx, authEnd);
  assert.match(
    authBody,
    /if \(!logTailAuthDeniedRef\.current\) \{\s*noteGatedDetailDrop\(\s*gatedDetailDroppedFeedsRef,\s*gatedDetailFeedGenerationRef,\s*DROP_ALL_GATED_DETAIL_FEEDS,?\s*\);\s*\}[\s\S]*?syncWorkspaceLogTailAuthDenied\(\s*logTailDeniedStreamKeysRef\.current,\s*workspaceId,\s*logTailAuthDeniedRef,\s*setLogTailAuthDenied,\s*\);[\s\S]*?setStreamOffsets\(\{\}\)/,
    "Expected tail 401/403 to invalidate in-flight tails, sync the denial latch, and clear offsets",
  );
  assert.doesNotMatch(
    authBody,
    /logListingAuthDeniedRef\.current = true/,
    "Expected tail denial not to reuse the listing latch that a later listing 200 would clear",
  );

  assert.match(
    dashboardSource.liveStream,
    /const streamAuthDenied = \(\) =>\s*workspaceDetailAuthDeniedRef\.current \|\|\s*logListingAuthDeniedRef\.current \|\|\s*logTailAuthDeniedRef\.current;[\s\S]*?if \(frame\.type === "log"\) \{[\s\S]*?if \(streamAuthDenied\(\)\) \{\s*return;\s*\}/,
    "Expected live log frames to be dropped while listing, tail, or base-detail authorization is denied",
  );
  assert.match(
    tails,
    /logTailDeniedStreamKeysRef\.current\.add\(logTailRefreshErrorKey\(workspaceId, stream\.stream_id\)\);/,
    "Expected each tail 401/403 to record its stream so a sibling 200 cannot clear the workspace latch",
  );
  assert.match(
    tails,
    /const recoveredTailStillAuthorized = \(\) =>\s*!logListingAuthDeniedRef\.current &&\s*!workspaceDetailAuthDeniedRef\.current &&\s*!logTailDeniedStreamKeysRef\.current\.has\(recoveredKey\);[\s\S]*?setLogEntries\(\(current\) => \{\s*if \(!recoveredTailStillAuthorized\(\)\) \{\s*return current;\s*\}[\s\S]*?setStreamOffsets\(\(current\) => \{\s*if \(!recoveredTailStillAuthorized\(\)\) \{\s*return current;\s*\}/,
    "Expected a recovered inspector tail to apply unless listing, base-detail, or this stream is denied again",
  );
});

test("recovered inspector tails apply while a sibling denial holds the latch", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6gBuRs: a 200 deletes
  // that stream from the denied set, then must still write its snapshot while
  // logTailAuthDeniedRef stays true for another selected stream. Skipping the
  // write on the workspace latch drops every recovered tail except the last.
  const tails = dashboardSource.logTails;
  const successIdx = tails.indexOf("const recoveredKey = logTailRefreshErrorKey");
  assert.ok(successIdx > 0, "Expected the successful-tail path to name the recovered stream");
  const successEnd = tails.indexOf("const reloadSelectedLogs = useCallback", successIdx);
  const successBody = tails.slice(successIdx, successEnd);

  assert.match(
    successBody,
    /logTailDeniedStreamKeysRef\.current\.delete\(recoveredKey\);[\s\S]*?syncWorkspaceLogTailAuthDenied\(\s*logTailDeniedStreamKeysRef\.current,\s*workspaceId,\s*logTailAuthDeniedRef,\s*setLogTailAuthDenied,\s*\)/,
    "Expected a 200 to recover only its own stream and resync the latch from remaining denied siblings",
  );
  assert.match(
    tails,
    /function syncWorkspaceLogTailAuthDenied\([\s\S]*?const stillDenied = workspaceHasDeniedLogTail\(deniedStreamKeys, workspaceId\);/,
    "Expected the latch sync to keep EventSource closed while a sibling denial remains",
  );
  assert.match(
    successBody,
    /setLogEntries\(\(current\) => \{\s*if \(!recoveredTailStillAuthorized\(\)\) \{\s*return current;\s*\}/,
    "Expected earlier recovered inspector tails to be written while a sibling denial holds the latch",
  );
  assert.doesNotMatch(
    successBody,
    /if \(logListingAuthDeniedRef\.current \|\| logTailAuthDeniedRef\.current\) \{\s*return current;\s*\}/,
    "Expected a successful inspector tail not to skip setLogEntries while the workspace latch is held",
  );
});

test("automatic inspector tails skip unchanged stream metadata and do not supersede an in-flight read", () => {
  // Regression for PR #933 review 5135360306: each selected-workspace detail
  // poll installs a new detail.streams array. Restarting tails on that
  // identity change bumps per-stream generation and discards the slower
  // success, so an unsupported workspace_stream leaves the inspector empty.
  const tails = dashboardSource.logTails;
  const dashboard = dashboardSource.dashboard;
  assert.match(
    tails,
    /useLayoutEffect\(\(\) => \{\s*automaticSelectedStreamsRef\.current = selectedStreams;\s*automaticListedStreamIdsRef\.current = detailStreams\.map\(\(stream\) => stream\.stream_id\);\s*\}, \[detailStreams, selectedStreams\]\);/,
    "Expected automatic tail refs to sync in a layout effect so an in-flight 401/200 sees the latest listing before the selection effect",
  );
  assert.doesNotMatch(
    tails,
    /const automaticListedStreamIdsRef = useRef\([\s\S]{0,240}?automaticSelectedStreamsRef\.current =/,
    "Expected automatic tail refs not to be assigned during render (react-hooks/refs)",
  );
  assert.doesNotMatch(
    dashboard,
    /for \(const stream of detail\.streams\) \{\s*if \(selectedStreams\.includes\(stream\.stream_id\)\) \{\s*void loadLogTail\(/,
    "Expected the dashboard not to restart inspector tails on every detail.streams identity change",
  );
  assert.match(
    tails,
    /function automaticLogTailPart\(stream: WorkspaceLogStream\): string \{\s*return \[stream\.byte_count, stream\.line_count, stream\.opened_at, stream\.closed_at \?\? ""\]\.join\(":"\);\s*\}/,
    "Expected automatic tail refreshes to key off stream metadata, not array identity",
  );
  assert.match(
    tails,
    /planAutomaticLogTailRefresh\(\{[\s\S]*?deniedStreamKeys: logTailDeniedStreamKeysRef\.current,/,
    "Expected automatic tail refreshes to ask the listing scheduler, including denied streams",
  );
  const derived = readFileSync(new URL("./console-dashboard-derived.ts", import.meta.url), "utf8");
  assert.match(
    derived,
    /if \(unchanged && !denied\) \{\s*continue;\s*\}/,
    "Expected unchanged stream metadata to skip a new automatic tail read unless that stream is still authorization-denied",
  );
  assert.match(
    derived,
    /if \(input\.inFlightStreamKeys\.has\(generationKey\)\) \{\s*pending\.push\(stream\);\s*continue;\s*\}/,
    "Expected an in-flight automatic tail to be queued instead of bumping generation",
  );
  assert.match(
    tails,
    /for \(const item of plan\.pending\) \{[\s\S]*?pendingAutomaticTailsRef\.current\.set\(`\$\{selectedId\}:\$\{item\.streamId\}`, \{/,
    "Expected a queued automatic tail to stay pending until the in-flight read settles",
  );
  assert.match(
    tails,
    /\} finally \{\s*drainPendingAutomaticTail\(generationKey\);\s*\}/,
    "Expected a settled tail to start at most one queued automatic follow-up",
  );
});

test("failed automatic inspector tails retry when stream metadata is unchanged", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6gCRNM: the automatic
  // tail effect records previousAutomaticTailPartsRef as soon as it sees
  // metadata, then skips later polls with the same byte/line/open/close.
  // A transient 5xx or network failure must drop that recorded part so the
  // next poll retries, including when a sibling denial already advanced
  // gated-detail generation and discarded this non-auth result. The part is
  // captured before the read so a later stream mutation cannot miss the
  // fingerprint the effect recorded. A 401/403 also forgets that part so
  // the next listing refresh can retry, but must not start a read here.
  const tails = dashboardSource.logTails;
  assert.match(
    tails,
    /function forgetRecordedAutomaticTailPart\(\s*parts: Map<string, string>,\s*streamId: string,\s*part: string\s*\): void \{\s*if \(parts\.get\(streamId\) === part\) \{\s*parts\.delete\(streamId\);\s*\}\s*\}/,
    "Expected a failed automatic tail to forget only the part this attempt recorded",
  );

  const loadIdx = tails.indexOf("const loadLogTail = useCallback");
  assert.ok(loadIdx > 0, "Expected loadLogTail callback");
  const loadEnd = tails.indexOf("const reloadSelectedLogs = useCallback", loadIdx);
  const body = tails.slice(loadIdx, loadEnd);
  const authIdx = body.indexOf("if (isLogTailAuthFailure(result.status))");
  const transientIdx = body.indexOf("Transient network/5xx", authIdx);
  const successIdx = body.indexOf("const tailEntry", transientIdx);
  const gatedIdx = body.indexOf("const gatedGenerationAdvanced =");
  assert.ok(authIdx > 0 && transientIdx > authIdx && successIdx > transientIdx, "Expected auth and transient tail paths");
  assert.ok(gatedIdx > 0 && gatedIdx < authIdx, "Expected gated-generation discard before auth handling");

  const forgetCall =
    /forgetRecordedAutomaticTailPart\(\s*previousAutomaticTailPartsRef\.current,\s*stream\.stream_id,\s*scheduledAutomaticPart,\s*\);/;
  assert.match(
    body,
    /const scheduledAutomaticPart = automaticLogTailPart\(stream\);[\s\S]*?await apiGet/,
    "Expected the automatic tail fingerprint to be captured before the read returns",
  );

  const gatedBody = body.slice(gatedIdx, authIdx);
  const authBody = body.slice(authIdx, transientIdx);
  const transientBody = body.slice(transientIdx, successIdx);
  assert.match(
    gatedBody,
    /if \(\s*gatedGenerationAdvanced &&\s*!result\.ok &&\s*!isLogTailAuthFailure\(result\.status\)\s*\) \{\s*forgetRecordedAutomaticTailPart\(\s*previousAutomaticTailPartsRef\.current,\s*stream\.stream_id,\s*scheduledAutomaticPart,\s*\);\s*\}/,
    "Expected a 5xx discarded after a gated-generation bump to forget the recorded automatic tail part",
  );
  assert.match(
    transientBody,
    forgetCall,
    "Expected a transient 5xx or network failure to forget the recorded automatic tail part",
  );
  assert.match(
    authBody,
    forgetCall,
    "Expected a 401/403 tail denial to forget the recorded part so the next listing refresh retries",
  );
  assert.doesNotMatch(
    authBody,
    /loadLogTail\(/,
    "Expected a 401/403 tail denial not to start another read in the same turn",
  );
  assert.match(
    tails,
    /!shouldStartPendingAutomaticLogTail\(\{[\s\S]*?deniedStreamKeys: logTailDeniedStreamKeysRef\.current,/,
    "Expected a pending automatic tail not to start while that stream is still authorization-denied",
  );
});

test("sibling tail 401s are recorded after a gated-detail generation bump", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6gBuRq: the first
  // tail 401/403 calls noteGatedDetailDrop and advances
  // gatedDetailFeedGenerationRef. A sibling denial that captured the prior
  // generation must still join logTailDeniedStreamKeysRef; otherwise a later
  // 200 for only the recorded stream reopens EventSource. In-flight siblings
  // that have not returned yet must be snapshotted too, or that same 200
  // recovers while the other selected stream is still unauthorized or hanging.
  const tails = dashboardSource.logTails;
  const loadIdx = tails.indexOf("const loadLogTail = useCallback");
  assert.ok(loadIdx > 0, "Expected loadLogTail callback");
  const loadEnd = tails.indexOf("const reloadSelectedLogs = useCallback", loadIdx);
  assert.ok(loadEnd > loadIdx, "Expected loadLogTail callback end");
  const body = tails.slice(loadIdx, loadEnd);

  const hardDiscard = body.indexOf("epoch !== authorizedFeedEpochRef.current");
  const gatedDiscard = body.indexOf("gatedGenerationAdvanced &&");
  const authIdx = body.indexOf("if (isLogTailAuthFailure(result.status))");
  const recordIdx = body.indexOf("recordDeniedLogTailAndInFlightSiblings(");
  const settleAfterRecord = body.indexOf("settleInFlight();", recordIdx);
  assert.ok(hardDiscard > 0, "Expected a hard discard for epoch/generation/selection/listing");
  assert.ok(gatedDiscard > hardDiscard, "Expected gated-generation discard after the hard discard");
  assert.ok(authIdx > gatedDiscard, "Expected auth handling after the gated-generation exception");
  assert.ok(recordIdx > authIdx, "Expected denied-stream recording inside the auth-failure branch");
  assert.ok(settleAfterRecord > recordIdx, "Expected in-flight siblings to be snapshotted before this request settles");
  assert.match(
    body,
    /logTailInFlightStreamKeysRef\.current\.add\(generationKey\);/,
    "Expected each tail request to be marked in-flight before the read returns",
  );
  assert.match(
    body,
    /if \(gatedGenerationAdvanced && \(result\.ok \|\| !isLogTailAuthFailure\(result\.status\)\)\) \{\s*settleInFlight\(\);\s*return;\s*\}/,
    "Expected sibling 401/403 to survive a gated-detail generation bump and still be recorded",
  );
  assert.doesNotMatch(
    body.slice(0, authIdx),
    /gatedGeneration !== gatedDetailFeedGenerationRef\.current \|\|/,
    "Expected gated-detail generation mismatch not to discard sibling 401/403 before they are recorded",
  );
  assert.match(
    body,
    /recordDeniedLogTailAndInFlightSiblings\(\s*logTailDeniedStreamKeysRef\.current,\s*logTailInFlightStreamKeysRef\.current,\s*workspaceId,\s*stream\.stream_id,\s*activeNow,\s*\);/,
    "Expected a tail 401/403 to latch still-active in-flight siblings so a later 200 cannot recover the workspace early",
  );
  const successDelete = body.indexOf("logTailDeniedStreamKeysRef.current.delete(");
  assert.ok(successDelete > recordIdx, "Expected a 200 to clear only its own denied key after siblings are latched");
  assert.doesNotMatch(
    body.slice(gatedDiscard, authIdx),
    /logTailDeniedStreamKeysRef\.current\.delete\(/,
    "Expected a gated-generation discard of a non-auth result not to clear a snapshotted sibling denial",
  );
});

test("authorized feed clear and overview auth denial wipe truncation with the overview", () => {
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /const clearAuthorizedConsoleFeeds = useCallback\([\s\S]*?setOverview\(\[\]\);\s*setOverviewError\(null\);\s*setWorkspaceDetailError\(null\);[\s\S]*?workspaceDetailAuthDeniedRef\.current = false;[\s\S]*?setWorkspaceDetailAuthDenied\(false\);[\s\S]*?setOverviewTruncationWarning\(null\);/,
    "Expected clearAuthorizedConsoleFeeds to wipe truncation with the overview after clearing the base-detail denial latch",
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
    /noteGatedDetailDrop\(\s*gatedDetailDroppedFeedsRef,\s*gatedDetailFeedGenerationRef,\s*DROP_ALL_GATED_DETAIL_FEEDS,?\s*\);[\s\S]*?setOverview\(\[\]\);[\s\S]*?setOverviewTruncationWarning\(null\);[\s\S]*?setSelectedId\(null\);[\s\S]*?setDetail\(emptyDetail\);[\s\S]*?setLogsFullscreen\(false\);[\s\S]*?setFullscreenWorkspaceIds\(\[\]\);/,
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

test("loadOverview auth denial clears retained agent/model filters", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6f_LzJ: overview
  // feed-level 401/403 must drop tenant-learned filter metadata, not only rows.
  // Leaving retainedAgents/retainedModels and active agent/model/repo/search
  // filters intact exposes prior identifiers and can keep a later recovered
  // list empty. Mirror clearAuthorizedConsoleFeeds without calling it.
  const dashboard = dashboardSource.dashboard;
  const authDeniedIdx = dashboard.indexOf("if (pageAuthDenied) {");
  assert.ok(authDeniedIdx > 0, "Expected pageAuthDenied clear path in loadOverview");
  const loadOverviewEnd = dashboard.indexOf("}, [setSelectedId]);", authDeniedIdx);
  assert.ok(loadOverviewEnd > authDeniedIdx, "Expected loadOverview callback end after pageAuthDenied");
  const authDeniedBody = dashboard.slice(authDeniedIdx, loadOverviewEnd);
  assert.match(
    authDeniedBody,
    /setRetainedAgents\(\[\]\);\s*setRetainedModels\(\[\]\);[\s\S]*?setAgentFilters\(\[\]\);\s*setModelFilters\(\[\]\);\s*setRepoFilter\(""\);\s*setSearchText\(""\);/,
    "Expected overview auth denial to reset retained agent/model metadata and active filters",
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
    /const loadOverview = useCallback\([\s\S]*?if \(collected === null\) \{[\s\S]*?if \(pageError !== null\) \{\s*setOverviewError\(pageError\);\s*\}\s*if \(pageAuthDenied\) \{[\s\S]*?noteGatedDetailDrop\(\s*gatedDetailDroppedFeedsRef,\s*gatedDetailFeedGenerationRef,\s*DROP_ALL_GATED_DETAIL_FEEDS,?\s*\);[\s\S]*?setOverview\(\[\]\);\s*setOverviewTruncationWarning\(null\);[\s\S]*?setWorkspaceDetailError\(null\);/,
    "Expected loadOverview to clear overview and dependent surfaces on page auth denial and retain last-good on other page failures",
  );
  assert.doesNotMatch(
    dashboard,
    /const loadOverview = useCallback\([\s\S]*?if \(collected === null\) \{[\s\S]*?setOverviewError\(pageError\);[\s\S]*?setOverview\(\[\]\);\s*return;/,
    "Expected loadOverview not to blank the authorized overview on every collectOverviewPages null",
  );
});

test("loadDashboardSummary discards stale success and error via request generation", () => {
  const dashboard = dashboardSource.dashboard;
  const fleetFeeds = dashboardSource.fleetFeeds;
  assert.match(
    dashboard,
    /const dashboardSummaryRequestGenerationRef = useRef\(0\);/,
    "Expected a dashboard-summary request-generation ref so overlapping polls stay monotonic",
  );
  assert.match(
    fleetFeeds,
    /const loadDashboardSummary = useCallback\([\s\S]*?const generation = \+\+dashboardSummaryRequestGenerationRef\.current;[\s\S]*?claimFleetFeedSuccess\(generation, dashboardSummaryRequestGenerationRef\.current, marks\)[\s\S]*?setDashboardSummaryError\(null\);/,
    "Expected loadDashboardSummary to bump generation before fetch and discard a superseded success before applying it",
  );
});

test("loadDashboardSummary clears last-good snapshot on feed-level 401 or 403", () => {
  assert.match(
    dashboardSource.fleetFeeds,
    /const loadDashboardSummary = useCallback\([\s\S]*?if \(!result\.ok && \(result\.status === 401 \|\| result\.status === 403\)\) \{[\s\S]*?claimFleetFeedDenial\(generation, dashboardSummaryRequestGenerationRef\.current, marks\)[\s\S]*?setDashboardSummary\(null\);\s*setDashboardSummaryError\(result\.message\);\s*return;\s*\}[\s\S]*?claimFleetFeedOutage\(generation, marks\)[\s\S]*?setDashboardSummaryError\(result\.message\);/,
    "Expected loadDashboardSummary to drop authorized counters on 401/403 rather than retain last-good as a transient outage",
  );
});

test("loadMergeQueue clears last-good snapshot on feed-level 401 or 403", () => {
  assert.match(
    dashboardSource.fleetFeeds,
    /const loadMergeQueue = useCallback\([\s\S]*?if \(!result\.ok && \(result\.status === 401 \|\| result\.status === 403\)\) \{[\s\S]*?claimFleetFeedDenial\(generation, mergeQueueRequestGenerationRef\.current, marks\)[\s\S]*?setMergeQueue\(\[\]\);\s*setMergeQueueHasMore\(false\);\s*setMergeQueueError\(result\.message\);\s*setMergeQueueStatus\("error"\);\s*return;\s*\}[\s\S]*?claimFleetFeedOutage\(generation, marks\)[\s\S]*?setMergeQueueError\(result\.message\);\s*setMergeQueueStatus\("error"\);/,
    "Expected loadMergeQueue to drop authorized queue rows on 401/403 rather than retain last-good as a transient outage",
  );
});

test("loadResourceSaturation clears last-good snapshot on feed-level 401 or 403", () => {
  const fleetFeeds = dashboardSource.fleetFeeds;
  assert.match(
    fleetFeeds,
    /const loadResourceSaturation = useCallback\([\s\S]*?if \(!result\.ok && \(result\.status === 401 \|\| result\.status === 403\)\) \{[\s\S]*?claimFleetFeedDenial\(\s*generation,\s*resourceSaturationRequestGenerationRef\.current,\s*marks,\s*\)[\s\S]*?setResourceSaturation\(null\);\s*setResourceError\(result\.message\);\s*return;\s*\}[\s\S]*?claimFleetFeedOutage\(generation, marks\)[\s\S]*?setResourceError\(result\.message\);/,
    "Expected loadResourceSaturation to drop authorized saturation on 401/403 rather than retain last-good as a transient outage",
  );
  assert.match(
    dashboardSource.dashboard,
    /const resourceSaturationRequestGenerationRef = useRef\(0\);/,
    "Expected a resource-saturation request-generation ref so overlapping polls stay monotonic",
  );
  assert.match(
    fleetFeeds,
    /const loadResourceSaturation = useCallback\([\s\S]*?const generation = \+\+resourceSaturationRequestGenerationRef\.current;[\s\S]*?claimFleetFeedSuccess\(\s*generation,\s*resourceSaturationRequestGenerationRef\.current,\s*marks,\s*\)[\s\S]*?setResourceError\(null\);/,
    "Expected loadResourceSaturation to bump generation before fetch and discard a superseded success before applying it",
  );
});

test("loadWorkspaceSummary clears last-good snapshot on feed-level 401 or 403", () => {
  const fleetFeeds = dashboardSource.fleetFeeds;
  assert.match(
    fleetFeeds,
    /const loadWorkspaceSummary = useCallback\([\s\S]*?if \(!result\.ok && \(result\.status === 401 \|\| result\.status === 403\)\) \{[\s\S]*?claimFleetFeedDenial\(generation, workspaceSummaryRequestGenerationRef\.current, marks\)[\s\S]*?setWorkspaceSummary\(null\);\s*setWorkspaceSummaryError\(result\.message\);\s*return;\s*\}[\s\S]*?claimFleetFeedOutage\(generation, marks\)[\s\S]*?setWorkspaceSummaryError\(result\.message\);/,
    "Expected loadWorkspaceSummary to drop authorized reliability on 401/403 rather than retain last-good as a transient outage",
  );
  assert.match(
    dashboardSource.dashboard,
    /const workspaceSummaryRequestGenerationRef = useRef\(0\);/,
    "Expected a workspace-summary request-generation ref so overlapping polls stay monotonic",
  );
  assert.match(
    fleetFeeds,
    /const loadWorkspaceSummary = useCallback\([\s\S]*?const generation = \+\+workspaceSummaryRequestGenerationRef\.current;[\s\S]*?claimFleetFeedSuccess\(generation, workspaceSummaryRequestGenerationRef\.current, marks\)[\s\S]*?setWorkspaceSummaryError\(null\);/,
    "Expected loadWorkspaceSummary to bump generation before fetch and discard a superseded success before applying it",
  );
});

test("loadFailureSummary clears last-good snapshot on feed-level 401 or 403", () => {
  const fleetFeeds = dashboardSource.fleetFeeds;
  assert.match(
    fleetFeeds,
    /const loadFailureSummary = useCallback\([\s\S]*?if \(!result\.ok && \(result\.status === 401 \|\| result\.status === 403\)\) \{[\s\S]*?claimFleetFeedDenial\(generation, failureSummaryRequestGenerationRef\.current, marks\)[\s\S]*?setFailureSummary\(null\);\s*setFailureSummaryStatus\("error"\);\s*setFailureSummaryError\(result\.message\);\s*return;\s*\}/,
    "Expected loadFailureSummary to drop authorized failure examples on 401/403 rather than retain last-good as a transient outage",
  );
  assert.match(
    dashboardSource.dashboard,
    /const failureSummaryRequestGenerationRef = useRef\(0\);/,
    "Expected a failure-summary request-generation ref so overlapping polls stay monotonic",
  );
  assert.match(
    fleetFeeds,
    /const loadFailureSummary = useCallback\([\s\S]*?const generation = \+\+failureSummaryRequestGenerationRef\.current;[\s\S]*?claimFleetFeedSuccess\(generation, failureSummaryRequestGenerationRef\.current, marks\)[\s\S]*?setFailureSummaryError\(null\);/,
    "Expected loadFailureSummary to bump generation before fetch and discard a superseded success before applying it",
  );
});

test("loadFailureSummary treats advertised-feed 404 and 503 as refresh errors", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6f_3Jn: when failures
  // stays advertised, a 404/503 after a successful snapshot is an outage, not
  // capability withdrawal. Keep the last snapshot, record the error, and do
  // not swap in the unavailable placeholder. Withdrawal still clears via
  // clearNewlyUnsupportedCapabilityFeeds.
  const fleetFeeds = dashboardSource.fleetFeeds;
  const loadStart = fleetFeeds.indexOf("const loadFailureSummary = useCallback");
  assert.ok(loadStart > 0, "Expected loadFailureSummary");
  const loadEnd = fleetFeeds.indexOf("const reloadAvailableFeeds = useCallback", loadStart);
  assert.ok(loadEnd > loadStart, "Expected loadFailureSummary before reloadAvailableFeeds");
  const loadBody = fleetFeeds.slice(loadStart, loadEnd);
  const authBranch = loadBody.indexOf("result.status === 401 || result.status === 403");
  assert.ok(authBranch > 0, "Expected loadFailureSummary auth-denial branch");
  const afterAuth = loadBody.slice(authBranch);
  const denialApplied = afterAuth.indexOf("setFailureSummary(null);");
  assert.ok(denialApplied > 0, "Expected loadFailureSummary auth-denial to clear the snapshot");
  const authReturn = afterAuth.indexOf("return;", denialApplied);
  assert.ok(authReturn > 0, "Expected loadFailureSummary auth-denial branch to return");
  const outageBranch = afterAuth.slice(authReturn);
  assert.doesNotMatch(
    outageBranch,
    /setFailureSummaryStatus\("unavailable"\)/,
    "Expected advertised-feed 404/503 not to mark failure analysis unavailable",
  );
  assert.doesNotMatch(
    outageBranch,
    /setFailureSummary\(null\)/,
    "Expected advertised-feed outages to retain the last-successful failure snapshot",
  );
  assert.match(
    outageBranch,
    /setFailureSummaryStatus\("error"\);\s*setFailureSummaryError\(result\.message\);/,
    "Expected advertised-feed 404/503 to record a refresh error while failures stays advertised",
  );
});

test("loadMergeQueue discards stale success and error via request generation", () => {
  assert.match(
    dashboardSource.dashboard,
    /const mergeQueueRequestGenerationRef = useRef\(0\);/,
    "Expected a merge-queue request-generation ref so overlapping polls stay monotonic",
  );
  assert.match(
    dashboardSource.fleetFeeds,
    /const loadMergeQueue = useCallback\([\s\S]*?const generation = \+\+mergeQueueRequestGenerationRef\.current;[\s\S]*?claimFleetFeedSuccess\(generation, mergeQueueRequestGenerationRef\.current, marks\)[\s\S]*?setMergeQueueError\(null\);/,
    "Expected loadMergeQueue to bump generation before fetch and discard a superseded success before applying it",
  );
});

test("loadCloudRuntime discards stale success and error via request generation", () => {
  assert.match(
    dashboardSource.dashboard,
    /const cloudRuntimeRequestGenerationRef = useRef\(0\);/,
    "Expected a cloud-runtime request-generation ref so overlapping polls stay monotonic",
  );
  assert.match(
    dashboardSource.fleetFeeds,
    /const loadCloudRuntime = useCallback\([\s\S]*?const generation = \+\+cloudRuntimeRequestGenerationRef\.current;[\s\S]*?claimFleetFeedSuccess\(generation, cloudRuntimeRequestGenerationRef\.current, marks\)[\s\S]*?setCloudRuntimeError\(null\);/,
    "Expected loadCloudRuntime to bump generation before fetch and discard a superseded success before applying it",
  );
});

test("fleet feed loaders apply superseded 401/403 and outages until a newer success lands", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6gEfkK: a periodic
  // dashboard-summary (and the same capacity, cloud-runtime, reliability,
  // merge-queue, and failures) request can settle 401/403 or network/5xx after
  // Refresh has only started a newer request. Discarding that failure solely
  // because the newer request exists leaves revoked metrics visible or an
  // outage with no stale-data warning if the newer request hangs.
  const fleetFeeds = dashboardSource.fleetFeeds;
  for (const loader of [
    "loadResourceSaturation",
    "loadDashboardSummary",
    "loadCloudRuntime",
    "loadWorkspaceSummary",
    "loadMergeQueue",
    "loadFailureSummary",
  ]) {
    const loadStart = fleetFeeds.indexOf(`const ${loader} = useCallback`);
    assert.ok(loadStart > 0, `Expected ${loader}`);
    const loadEnd = fleetFeeds.indexOf("\n  const ", loadStart + 1);
    assert.ok(loadEnd > loadStart, `Expected ${loader} body`);
    const loadBody = fleetFeeds.slice(loadStart, loadEnd);
    const denialAt = loadBody.indexOf("claimFleetFeedDenial");
    const outageAt = loadBody.indexOf("claimFleetFeedOutage");
    const successAt = loadBody.indexOf("claimFleetFeedSuccess");
    assert.ok(denialAt > 0, `Expected ${loader} to claim a 401/403 before discarding it`);
    assert.ok(outageAt > denialAt, `Expected ${loader} to claim an outage after denial handling`);
    assert.ok(successAt > outageAt, `Expected ${loader} to claim success only after failure handling`);
    assert.doesNotMatch(
      loadBody,
      /generation !== \w+RequestGenerationRef\.current[\s\S]*?result\.status === 401/,
      `Expected ${loader} not to discard 401/403 solely because a newer request started`,
    );
  }
});

test("capability-gated feed polls chain after the previous invocation settles", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6f-1t6: a wall-clock
  // interval that calls a gated loader every pollMs advances that feed's
  // request generation, so every slower-than-interval success is discarded
  // and dashboard-summary, capacity, cloud-runtime, reliability, merge-queue,
  // or failures stays empty or permanently stale. The next invocation is
  // scheduled only after the preceding promise settles. Explicit
  // reloadAvailableFeeds still calls the loaders directly so a newer request
  // can supersede.
  const dashboard = dashboardSource.dashboard;
  const gated = dashboardSource.gatedPoll;
  assert.doesNotMatch(
    gated,
    /setInterval\(/,
    "Expected no wall-clock gated-feed interval that can supersede an in-flight request",
  );
  assert.match(
    gated,
    /const scheduleNext = \(\) => \{[\s\S]*?window\.setTimeout\(\(\) => \{[\s\S]*?start\(\);[\s\S]*?\}, pollMs\);[\s\S]*?\};[\s\S]*?const start = \(\) => \{[\s\S]*?void Promise\.resolve\(load\(\)\)\.finally\(\(\) => \{[\s\S]*?scheduleNext\(\);[\s\S]*?\}\);[\s\S]*?\};[\s\S]*?start\(\);/,
    "Expected gated feed polls to invoke immediately and chain the next tick only after settle",
  );
  for (const loader of [
    "pollDashboardSummary",
    "loadResourceSaturation",
    "pollCloudRuntime",
    "loadWorkspaceSummary",
    "loadMergeQueue",
    "loadFailureSummary",
  ]) {
    assert.match(
      dashboard,
      new RegExp(`useCapabilityGatedPoll\\([\\s\\S]*?${loader},?\\s*\\)`),
      `Expected ${loader} to stay on the serialized gated poll`,
    );
  }
});

test("periodic capability polls skip while a request is still in flight", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6f-1t4: a wall-clock
  // interval that calls loadCapabilities every pollMs advances
  // capabilityRequestGenerationRef, so every slower-than-interval success is
  // discarded and the console stays permanently unnegotiated. Periodic loads
  // serialize: the next poll is scheduled only after the previous invocation
  // settles, and a still-in-flight request is not replaced. Explicit refresh
  // and context-sync still call loadCapabilities directly so a newer request
  // can supersede.
  const dashboard = dashboardSource.dashboard;
  assert.match(
    dashboard,
    /const capabilityLoadInFlightRef = useRef\(false\);/,
    "Expected a capability in-flight latch so periodic polls can serialize",
  );
  assert.match(
    dashboard,
    /const loadCapabilities = useCallback\([\s\S]*?const generation = \+\+capabilityRequestGenerationRef\.current;[\s\S]*?capabilityLoadInFlightRef\.current = true;[\s\S]*?finally \{[\s\S]*?if \(generation === capabilityRequestGenerationRef\.current\) \{\s*capabilityLoadInFlightRef\.current = false;\s*\}/,
    "Expected loadCapabilities to hold the in-flight latch until the latest generation finishes",
  );
  assert.doesNotMatch(
    dashboard,
    /setInterval\(\s*(?:\(\)\s*=>\s*)?(?:void\s+)?loadCapabilities\(\)/,
    "Expected no wall-clock capability interval that can cancel an in-flight request",
  );
  assert.match(
    dashboard,
    /useSerializedPeriodicLoad\(\s*true,\s*loadCapabilities,\s*capabilityLoadInFlightRef,/,
    "Expected periodic capability polls to use the serialized loader",
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
    /const loadCapabilities = useCallback\([\s\S]*?const generation = \+\+capabilityRequestGenerationRef\.current;[\s\S]*?if \(\s*generation !== capabilityRequestGenerationRef\.current \|\|\s*generation <= revokedCapabilityGenerationRef\.current \|\|\s*generation < appliedCapabilityGenerationRef\.current\s*\) \{\s*return null;\s*\}/,
    "Expected loadCapabilities to bump generation before fetch and discard mismatched non-denial responses",
  );
  assert.match(
    dashboard,
    /const loadCapabilities = useCallback\([\s\S]*?if \(\s*generation !== capabilityRequestGenerationRef\.current \|\|\s*generation <= revokedCapabilityGenerationRef\.current \|\|\s*generation < appliedCapabilityGenerationRef\.current\s*\) \{\s*return null;\s*\}[\s\S]*?consoleAuthDeniedRef\.current = false;/,
    "Expected success-path denial-latch clear to run only after the generation freshness check",
  );
});

test("loadCapabilities applies superseded 401/403 unless a newer success recovered", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6gDKfM: a periodic
  // capability request can still be in flight when Refresh starts a newer
  // one. Discarding the older 401/403 only because the newer request started
  // leaves authorized feeds up if that newer request hangs or fails
  // transiently. A newer successful negotiation is the recovery that wins.
  const dashboard = dashboardSource.dashboard;
  const loadStart = dashboard.indexOf("const loadCapabilities = useCallback");
  assert.ok(loadStart > 0, "Expected loadCapabilities");
  const loadEnd = dashboard.indexOf("useConsoleFleetFeeds({", loadStart);
  assert.ok(loadEnd > loadStart, "Expected loadCapabilities body before loadResourceSaturation");
  const loadBody = dashboard.slice(loadStart, loadEnd);
  assert.match(
    loadBody,
    /if \(!result\.ok && \(result\.status === 401 \|\| result\.status === 403\)\) \{\s*applyAuthoritativeCapabilityDenial\(generation, result\.message\);\s*return null;\s*\}/,
    "Expected capability 401/403 to apply before a superseded-generation discard",
  );
  assert.match(
    loadBody,
    /if \(deniedGeneration < appliedCapabilityGenerationRef\.current\) \{\s*return;\s*\}/,
    "Expected an older capability denial to leave a newer applied success in place",
  );
  assert.match(
    loadBody,
    /revokedCapabilityGenerationRef\.current = Math\.max\(\s*revokedCapabilityGenerationRef\.current,\s*capabilityRequestGenerationRef\.current,\s*\)/,
    "Expected capability denial to revoke every request that has already started",
  );
  assert.match(
    loadBody,
    /appliedCapabilityGenerationRef\.current = Math\.max\(\s*appliedCapabilityGenerationRef\.current,\s*generation,\s*\)/,
    "Expected a landed capability 200 to record its generation as applied",
  );
  assert.doesNotMatch(
    loadBody,
    /if \(generation !== capabilityRequestGenerationRef\.current\) \{\s*return null;\s*\}[\s\S]*?result\.status === 401/,
    "Expected capability 401/403 not to be discarded solely because a newer request started",
  );
});

test("loadCapabilities applies superseded network/5xx until a newer success lands", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6gDcw4: a periodic
  // capability request can return network/5xx after Refresh has only started
  // a newer request. Discarding that outage because generation !== current
  // leaves capabilityError null if the newer request hangs, so retained
  // capabilities keep enabling mutating controls.
  const dashboard = dashboardSource.dashboard;
  const loadStart = dashboard.indexOf("const loadCapabilities = useCallback");
  assert.ok(loadStart > 0, "Expected loadCapabilities");
  const loadEnd = dashboard.indexOf("useConsoleFleetFeeds({", loadStart);
  assert.ok(loadEnd > loadStart, "Expected loadCapabilities body before loadResourceSaturation");
  const loadBody = dashboard.slice(loadStart, loadEnd);
  assert.match(
    dashboard,
    /const appliedCapabilityFailureGenerationRef = useRef\(0\);/,
    "Expected a capability failure-generation watermark so a newer start is not recovery",
  );
  assert.match(
    loadBody,
    /if \(!result\.ok && result\.status !== 404\) \{\s*const applied = applyTransientCapabilityOutage\(generation, result\.message\);\s*return applied \? appliedCapabilitiesRef\.current : null;\s*\}[\s\S]*?if \(\s*generation !== capabilityRequestGenerationRef\.current/,
    "Expected network/5xx outages to apply before discarding a non-latest generation",
  );
  assert.match(
    loadBody,
    /if \(failedGeneration < appliedCapabilityGenerationRef\.current\) \{\s*return false;\s*\}/,
    "Expected an older capability outage to leave a newer applied success in place",
  );
  assert.match(
    loadBody,
    /if \(generation < appliedCapabilityFailureGenerationRef\.current\) \{\s*return null;\s*\}/,
    "Expected an older capability success to leave a newer applied outage in place",
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
    /const applyTransientCapabilityOutage = \(failedGeneration: number, message: string\) => \{[\s\S]*?const retained = appliedCapabilitiesRef\.current;[\s\S]*?if \(retained === null\) \{\s*setCapabilities\(null\);\s*\}[\s\S]*?return true;/,
    "Expected 5xx/network capability outages to keep appliedCapabilitiesRef rather than nulling negotiated feeds",
  );
  assert.match(
    dashboard,
    /if \(!result\.ok && result\.status !== 404\) \{\s*const applied = applyTransientCapabilityOutage\(generation, result\.message\);\s*return applied \? appliedCapabilitiesRef\.current : null;\s*\}/,
    "Expected transient capability outages to apply before a superseded-generation discard",
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
  assert.match(
    dashboardSource.liveStream,
    /if \(!selectedId \|\| logListingAuthDenied \|\| logTailAuthDenied \|\| workspaceDetailAuthDenied\) \{\s*setStreamState\("idle"\);\s*return;\s*\}/,
    "Expected inspector /stream to close when listing, tail, or base-detail authorization is denied, not only drop frames after they arrive",
  );
  assert.match(
    dashboardSource.detailLoader,
    /const applyLogListingAuthDenial = \(result: ApiEnvelope<ListEnvelope<WorkspaceLogStream>>\) => \{[\s\S]*?logListingAuthDeniedRef\.current = true;[\s\S]*?setLogListingAuthDenied\(true\);[\s\S]*?setSelectedStreams\(\[\]\);[\s\S]*?setLogEntries\(\[\]\);[\s\S]*?setStreamOffsets\(\{\}\);[\s\S]*?void streamsPromise\.then\(\(result\) => \{[\s\S]*?applyLogListingAuthDenial\(result\);[\s\S]*?if \(allowLogs && streams != null && feedAuthDenied\(streams\)\) \{[\s\S]*?applyLogListingAuthDenial\(streams\);/,
    "Expected listing 401/403 to clear selection caches as soon as the listing settles and again after merge so a sibling 200 cannot restore selection",
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
    /dashboardSummaryRequestGenerationRef\.current \+= 1;[\s\S]*?cloudRuntimeRequestGenerationRef\.current \+= 1;[\s\S]*?mergeQueueRequestGenerationRef\.current \+= 1;[\s\S]*?resourceSaturationRequestGenerationRef\.current \+= 1;[\s\S]*?workspaceSummaryRequestGenerationRef\.current \+= 1;[\s\S]*?failureSummaryRequestGenerationRef\.current \+= 1;[\s\S]*?if \(appliedCapabilitiesRef\.current !== null\) \{\s*noteGatedDetailDrop\(\s*gatedDetailDroppedFeedsRef,\s*gatedDetailFeedGenerationRef,\s*DROP_ALL_GATED_DETAIL_FEEDS,?\s*\);\s*\}/,
    "Expected 404 gated clear to bump inventory request generations always, and gatedDetailFeedGenerationRef only when leaving a negotiated snapshot",
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
  // 404 is a distinct gated-clear path; transient retain must not call it.
  const idx404 = dashboard.indexOf("if (result.status === 404)");
  const idxRetain = dashboard.indexOf("const retained = appliedCapabilitiesRef.current");
  assert.ok(idx404 > 0 && idxRetain > 0, "Expected both 404 clear and 5xx/network retain");
  const retainBody = dashboard.slice(
    dashboard.indexOf("const applyTransientCapabilityOutage"),
    dashboard.indexOf("if (!result.ok && (result.status === 401 || result.status === 403))"),
  );
  assert.equal(
    retainBody.includes("clearCapabilityGatedInventories"),
    false,
    "Expected transient capability retain not to clear gated inventories like 404",
  );
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
    /if \(!allowStreamLogs \|\| listingDenied \|\| tailAuthDenied\) \{\s*setStreamState\("idle"\);\s*return;\s*\}/,
    "Expected WorkspaceLogColumn to open /stream only when listing+stream (allowStreamLogs) is allowed and listing or tail has not been auth-denied",
  );
});

test("fullscreen listing 401/403 applies while a newer poll is in flight", () => {
  // Regression for slow-denial starvation: a wall-clock interval starts poll
  // N+1 before poll N returns 401/403. Discarding every non-latest denial
  // leaves cached private tails and EventSource open. A newer applied 200
  // still wins; an older overlapping 200 stays rejected; a later generation
  // may recover.
  const logs = dashboardSource.logs;
  const loadStart = logs.indexOf("const loadStreams = useCallback");
  assert.ok(loadStart > 0, "Expected WorkspaceLogColumn.loadStreams");
  const loadEnd = logs.indexOf("useEffect(() => {", loadStart);
  const loadBody = logs.slice(loadStart, loadEnd);
  assert.match(
    loadBody,
    /if \(result\.status === 401 \|\| result\.status === 403\) \{\s*applyAuthoritativeListingDenial\(generation, result\.message\);/,
    "Expected listing 401/403 to apply before a superseded-generation discard",
  );
  assert.match(
    loadBody,
    /if \(deniedGeneration < appliedListingGenerationRef\.current\) \{\s*return;\s*\}/,
    "Expected an older listing denial to leave a newer applied success in place",
  );
  assert.match(
    loadBody,
    /revokedListingGenerationRef\.current = Math\.max\(\s*revokedListingGenerationRef\.current,\s*listingGenerationRef\.current,\s*\)/,
    "Expected listing denial to revoke every poll that has already started",
  );
  assert.match(
    loadBody,
    /if \(generation <= revokedListingGenerationRef\.current\) \{\s*return;\s*\}/,
    "Expected an older overlapping listing 200 to stay rejected after denial",
  );
  assert.doesNotMatch(
    loadBody,
    /if \(!result\.ok\) \{[\s\S]*?if \(generation !== listingGenerationRef\.current\) \{\s*return;\s*\}[\s\S]*?result\.status === 401/,
    "Expected listing 401/403 not to be discarded solely because a newer poll started",
  );
});

test("fullscreen listing 200 discards a success older than the last applied generation", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6gCA3J: when two
  // listing polls overlap and the newer 200 applies first, the older 200 must
  // not pass the revocation-only guard and overwrite streams. The first
  // completed success still lands because appliedListingGenerationRef starts
  // at 0 (strictly older, not equal).
  const logs = dashboardSource.logs;
  const loadStart = logs.indexOf("const loadStreams = useCallback");
  assert.ok(loadStart > 0, "Expected WorkspaceLogColumn.loadStreams");
  const loadEnd = logs.indexOf("useEffect(() => {", loadStart);
  const loadBody = logs.slice(loadStart, loadEnd);
  assert.match(
    loadBody,
    /if \(generation < appliedListingGenerationRef\.current\) \{\s*return;\s*\}/,
    "Expected an older listing 200 to be rejected after a newer success applied",
  );
  assert.match(
    loadBody,
    /appliedListingGenerationRef\.current = Math\.max\(\s*appliedListingGenerationRef\.current,\s*generation,\s*\)/,
    "Expected a landed listing 200 to record its generation as applied",
  );
  assert.match(
    loadBody,
    /const listingSuccessStillApplied = \(\) => \{\s*const stillApplied =\s*generation === appliedListingGenerationRef\.current &&\s*generation > revokedListingGenerationRef\.current &&\s*generation >= appliedListingFailureGenerationRef\.current;\s*if \(stillApplied && !committedListingActivity\) \{\s*streamActivityRef\.current = pendingStreamActivity;\s*committedListingActivity = true;\s*\}\s*return stillApplied;\s*\};/,
    "Expected queued listing writes to re-check the applied generation and a newer listing failure",
  );
  assert.match(
    loadBody,
    /setStreams\(\(current\) => \(listingSuccessStillApplied\(\) \? listingItems : current\)\);/,
    "Expected a stale listing 200 not to overwrite streams after a newer success",
  );
  assert.match(
    loadBody,
    /setSelectedStreams\(\(current\) =>\s*listingSuccessStillApplied\(\) \? pickWorkspaceLogStreams\(listingItems, current\) : current,?\s*\);/,
    "Expected a stale listing 200 not to overwrite selectedStreams after a newer success",
  );
});

test("fullscreen listing 200 flush does not rewind streams after a newer failure", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6gCwZw: the early
  // return rejects an older /logs 200 only if the newer 5xx has already
  // applied. A 200 that already passed that check can still flush setStreams
  // after the failure watermark advances, keeping the error text while
  // last-good streams rewind to the stale snapshot.
  const logs = dashboardSource.logs;
  const loadStart = logs.indexOf("const loadStreams = useCallback");
  assert.ok(loadStart > 0, "Expected WorkspaceLogColumn.loadStreams");
  const loadEnd = logs.indexOf("useEffect(() => {", loadStart);
  const loadBody = logs.slice(loadStart, loadEnd);
  assert.match(
    loadBody,
    /if \(generation < appliedListingFailureGenerationRef\.current\) \{\s*return;\s*\}/,
    "Expected an older listing 200 to be rejected after a newer failure applied",
  );
  assert.match(
    loadBody,
    /const listingSuccessStillApplied = \(\) => \{\s*const stillApplied =\s*generation === appliedListingGenerationRef\.current &&\s*generation > revokedListingGenerationRef\.current &&\s*generation >= appliedListingFailureGenerationRef\.current;\s*if \(stillApplied && !committedListingActivity\) \{\s*streamActivityRef\.current = pendingStreamActivity;\s*committedListingActivity = true;\s*\}\s*return stillApplied;\s*\};/,
    "Expected a queued listing 200 to drop its stream write after a newer failure",
  );
  assert.doesNotMatch(
    loadBody,
    /streamActivityRef\.current = updateLogStreamActivity\(/,
    "Expected a queued listing 200 not to commit stream activity before the failure watermark is re-checked",
  );
});

test("fullscreen tail 401/403 applies while a newer reload is in flight", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6gCSz-: a metadata
  // or manual reload increments tailRequestGenerationRef before an older
  // tail returns 401/403. Discarding that denial merely because the newer
  // request started leaves cached tails and EventSource open if the newer
  // request hangs or fails transiently. A newer applied 200 still wins; a
  // request that started before the denial stays rejected.
  const logs = dashboardSource.logs;
  const loadIdx = logs.indexOf("const loadSelectedTails = useCallback");
  assert.ok(loadIdx > 0, "Expected loadSelectedTails callback");
  const loadEnd = logs.indexOf("}, [allowLogs, selectedStreams, streams, workspace.workspace_id]);", loadIdx);
  assert.ok(loadEnd > loadIdx, "Expected loadSelectedTails callback end");
  const body = logs.slice(loadIdx, loadEnd);

  const denialStart = body.indexOf("const applyTailAuthDenial");
  assert.ok(denialStart > 0, "Expected applyTailAuthDenial");
  const denialEnd = body.indexOf("const readSelectedTail", denialStart);
  const denialBody = body.slice(denialStart, denialEnd);
  assert.match(
    denialBody,
    /generation < appliedTailGenerationRef\.current/,
    "Expected an older tail denial to leave a newer applied success in place",
  );
  assert.doesNotMatch(
    denialBody,
    /generation !== tailRequestGenerationRef\.current/,
    "Expected tail 401/403 not to be discarded solely because a newer reload started",
  );
  assert.match(
    denialBody,
    /revokedTailGenerationRef\.current = Math\.max\(\s*revokedTailGenerationRef\.current,\s*tailRequestGenerationRef\.current,\s*\)/,
    "Expected tail denial to revoke every reload that has already started",
  );
  assert.match(
    denialBody,
    /appliedTailGenerationRef\.current <= generation/,
    "Expected a queued denial clear to apply until a newer success owns the column",
  );
  assert.match(
    body,
    /generation <= revokedTailGenerationRef\.current/,
    "Expected an older overlapping tail 200 to stay rejected after denial",
  );
  assert.match(
    body,
    /appliedTailGenerationRef\.current = Math\.max\(appliedTailGenerationRef\.current, generation\)/,
    "Expected a landed tail 200 to record its generation as applied",
  );
});

test("fullscreen loadSelectedTails retains last-successful tails on transient refresh failure", () => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6gAqk-: a network or
  // 5xx tail read must not replace the last fullscreen snapshot with the
  // synthetic error entry from readLogTailEntry. Surface the refresh warning
  // separately and only install entries from successful reads. 401/403 still
  // drops authorized column contents.
  const logs = dashboardSource.logs;
  const loadIdx = logs.indexOf("const loadSelectedTails = useCallback");
  assert.ok(loadIdx > 0, "Expected loadSelectedTails callback");
  const loadEnd = logs.indexOf("}, [allowLogs, selectedStreams, streams, workspace.workspace_id]);", loadIdx);
  assert.ok(loadEnd > loadIdx, "Expected loadSelectedTails callback end");
  const body = logs.slice(loadIdx, loadEnd);

  const authIdx = body.indexOf("const applyTailAuthDenial");
  assert.ok(authIdx > 0, "Expected fullscreen tail 401/403 handling");
  const transientMarker = "Transient network/5xx";
  const transientIdx = body.indexOf(transientMarker, authIdx);
  assert.ok(transientIdx > authIdx, "Expected a non-auth fullscreen tail refresh-error path");
  const authBody = body.slice(authIdx, transientIdx);
  assert.match(
    authBody,
    /isFullscreenTailAuthFailure\(result\.status\)/,
    "Expected fullscreen tails to distinguish feed-level 401/403 from transient outages",
  );
  assert.match(
    authBody,
    /setEntries\(\(current\) => \(denialStillOwnsColumn\(\) \? \[\] : current\)\)/,
    "Expected 401/403 to drop authorized fullscreen tails",
  );

  // Successful apply clears the tail-auth latch. Do not bound this slice on
  // comment prose — that marker was rewritten when denied streams gained a latch.
  const retainEnd = body.indexOf("tailAuthDeniedRef.current = false", transientIdx);
  assert.ok(retainEnd > transientIdx, "Expected the all-failure retain path to return before successful tail apply");
  const transientBody = body.slice(transientIdx, retainEnd);
  assert.match(
    transientBody,
    /if \(successes\.length === 0\) \{[\s\S]*?setTailRefreshErrors/,
    "Expected transient failures to record a separate refresh warning",
  );
  assert.doesNotMatch(
    transientBody,
    /setEntries/,
    "Expected transient refresh failure not to replace the last-successful fullscreen tail",
  );
  assert.doesNotMatch(
    transientBody,
    /\.\.\.results\.map\(\(result\) => result\.entry\)/,
    "Expected fullscreen tails not to append readLogTailEntry's synthetic error entry",
  );

  const applyBody = body.slice(retainEnd);
  assert.match(
    applyBody,
    /\.\.\.successes\.map\(\(result\) => result\.entry\)/,
    "Expected only successful tail reads to replace fullscreen entries",
  );
  assert.doesNotMatch(
    applyBody,
    /\.\.\.results\.map\(\(result\) => result\.entry\)/,
    "Expected fullscreen tail replacement not to include failed reads",
  );

  assert.match(
    logs,
    /role="alert"[\s\S]*?tailRefreshError/,
    "Expected the fullscreen column to render the refresh warning separately from log payload",
  );
  assert.match(
    logs,
    /data-awf-stale=\{tailRefreshStale \? "true" : undefined\}/,
    "Expected a retained fullscreen tail snapshot to be marked stale while the refresh warning is shown",
  );
});

test("fullscreen listing refresh retries denied tails when metadata is unchanged", () => {
  // Regression for PR #933 review 5135880005: a static /logs listing keeps
  // selectedTailRefreshKey equal, so the fingerprint return never re-reads a
  // 401/403 tail. Only that stream's own 200 clears the latch, so the column
  // EventSource stays closed until Tail all or metadata changes. Retry denied
  // streams on the next listing refresh, but do not start a second reload
  // while one is already in flight — a newer generation would discard the
  // slower success.
  const logs = dashboardSource.logs;
  const effectStart = logs.indexOf("if (!selectedTailRefreshKey) {");
  assert.ok(effectStart > 0, "Expected the fullscreen tail refresh-key effect");
  const effectEnd = logs.indexOf("}, [loadSelectedTails, selectedTailRefreshKey]);", effectStart);
  assert.ok(effectEnd > effectStart, "Expected the fullscreen tail refresh-key effect to end");
  const effectBody = logs.slice(effectStart, effectEnd);

  assert.match(
    effectBody,
    /previousTailRefreshKey\.current === selectedTailRefreshKey &&\s*\(\s*tailDeniedStreamIdsRef\.current\.size === 0 \|\|\s*tailReloadInFlightCountRef\.current > 0\s*\)/,
    "Expected unchanged stream metadata to skip a new fullscreen tail read unless a denied stream can be retried",
  );
  assert.doesNotMatch(
    effectBody,
    /if \(previousTailRefreshKey\.current === selectedTailRefreshKey\) \{\s*return;\s*\}/,
    "Expected a static listing fingerprint not to block a retry of a currently denied fullscreen tail",
  );

  const loadIdx = logs.indexOf("const loadSelectedTails = useCallback");
  assert.ok(loadIdx > 0, "Expected loadSelectedTails callback");
  const loadEnd = logs.indexOf("}, [allowLogs, selectedStreams, streams, workspace.workspace_id]);", loadIdx);
  assert.ok(loadEnd > loadIdx, "Expected loadSelectedTails callback end");
  const loadBody = logs.slice(loadIdx, loadEnd);
  assert.match(
    loadBody,
    /tailReloadInFlightCountRef\.current \+= 1;[\s\S]*?finally \{\s*tailReloadInFlightCountRef\.current -= 1;\s*\}/,
    "Expected a fullscreen tail reload to stay marked in flight until it settles",
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
    /if \(plan\.clearRuntime \|\| plan\.clearEvents \|\| plan\.clearOperations \|\| plan\.clearLogs\) \{[\s\S]*?noteGatedDetailDrop\(\s*gatedDetailDroppedFeedsRef,\s*gatedDetailFeedGenerationRef,\s*gatedDetailDropFromWithdrawal\(plan\)\s*\);\s*\}/,
    "Expected same-identity inspector-detail withdrawal to record the drop mask and bump gatedDetailFeedGenerationRef",
  );
  assert.match(
    withdrawBody,
    /if \(plan\.clearEvents\) \{[\s\S]*?eventFeedAuthDeniedRef\.current = false;\s*setEventFeedAuthDenied\(false\);/,
    "Expected workspace_events withdrawal to clear the event-denial latch so a basic-detail 200 can drop the stale authorization banner",
  );
  assert.doesNotMatch(
    withdrawBody,
    /if \(capabilityFeedWithdrawalCleared\(plan\)\) \{\s*gatedDetailFeedGenerationRef\.current \+= 1;\s*\}/,
    "Expected unrelated fleet/capacity withdrawal not to bump the shared gated-detail generation",
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
