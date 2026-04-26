import sys

with open("apps/console/components/console-dashboard.tsx", "r") as f:
    content = f.read()

# 1. Imports
content = content.replace(
    '  WorkspaceRetryResponse,\n  WorkspaceStatus,\n} from "@/lib/types";',
    '  WorkspaceRetryResponse,\n  WorkspaceStatus,\n  WorkspaceReliabilitySummary,\n} from "@/lib/types";'
)

# 2. State
content = content.replace(
    '  const [resourceError, setResourceError] = useState<string | null>(null);\n  const [mergeQueue, setMergeQueue] = useState<MergeQueueItem[]>([]);',
    '  const [resourceError, setResourceError] = useState<string | null>(null);\n  const [workspaceSummary, setWorkspaceSummary] = useState<WorkspaceReliabilitySummary | null>(null);\n  const [workspaceSummaryError, setWorkspaceSummaryError] = useState<string | null>(null);\n  const [mergeQueue, setMergeQueue] = useState<MergeQueueItem[]>([]);'
)

# 3. Callbacks
content = content.replace(
    '    setResourceError(null);\n    setResourceSaturation(result.data);\n  }, []);\n\n  const loadMergeQueue = useCallback(async () => {',
    '    setResourceError(null);\n    setResourceSaturation(result.data);\n  }, []);\n\n  const loadWorkspaceSummary = useCallback(async () => {\n    const result = await apiGet<WorkspaceReliabilitySummary>("/api/awf/metrics/workspaces/summary");\n    if (!result.ok) {\n      setWorkspaceSummaryError(result.message);\n      return;\n    }\n    setWorkspaceSummaryError(null);\n    setWorkspaceSummary(result.data);\n  }, []);\n\n  const loadMergeQueue = useCallback(async () => {'
)

# 4. Promise.all
content = content.replace(
    '    await Promise.all([loadOverview(), loadResourceSaturation(), loadMergeQueue(), loadFailureSummary()]);\n  }, [loadMergeQueue, loadOverview, loadResourceSaturation, loadFailureSummary, selectedId]);',
    '    await Promise.all([loadOverview(), loadResourceSaturation(), loadMergeQueue(), loadFailureSummary(), loadWorkspaceSummary()]);\n  }, [loadMergeQueue, loadOverview, loadResourceSaturation, loadFailureSummary, loadWorkspaceSummary, selectedId]);'
)

# 5. useEffect
content = content.replace(
    '    const interval = window.setInterval(() => void loadResourceSaturation(), pollMs);\n    return () => window.clearInterval(interval);\n  }, [loadResourceSaturation]);\n\n  useEffect(() => {\n    void loadMergeQueue();',
    '    const interval = window.setInterval(() => void loadResourceSaturation(), pollMs);\n    return () => window.clearInterval(interval);\n  }, [loadResourceSaturation]);\n\n  useEffect(() => {\n    void loadWorkspaceSummary();\n    const interval = window.setInterval(() => void loadWorkspaceSummary(), pollMs);\n    return () => window.clearInterval(interval);\n  }, [loadWorkspaceSummary]);\n\n  useEffect(() => {\n    void loadMergeQueue();'
)

# 6. onRefresh
content = content.replace(
    '            void loadOverview();\n            void loadResourceSaturation();\n            void loadMergeQueue();\n          })\n        }\n        isPending={isPending}',
    '            void loadOverview();\n            void loadResourceSaturation();\n            void loadMergeQueue();\n            void loadWorkspaceSummary();\n          })\n        }\n        isPending={isPending}'
)

# 7. ResourceCapacityPanel instantiation
content = content.replace(
    '          <div className="grid gap-4 p-4 pb-0 2xl:grid-cols-[minmax(0,1fr)_minmax(460px,0.85fr)]">\n            <ResourceCapacityPanel saturation={resourceSaturation} error={resourceError} />\n            <MergeQueuePanel',
    '          <div className="grid gap-4 p-4 pb-0 2xl:grid-cols-[minmax(0,1fr)_minmax(460px,0.85fr)]">\n            <ResourceCapacityPanel \n              saturation={resourceSaturation} \n              error={resourceError} \n              workspaceSummary={workspaceSummary}\n              workspaceSummaryError={workspaceSummaryError}\n            />\n            <MergeQueuePanel'
)

# 8. ResourceCapacityPanel body
content = content.replace(
    'function ResourceCapacityPanel({\n  saturation,\n  error,\n}: {\n  saturation: ResourceSaturationSummary | null;\n  error: string | null;\n}) {\n  return (\n    <Panel title="Resource / Capacity" icon={<Server size={16} aria-hidden />}>\n      {!saturation ? (\n        <MutedLine>{error ? `Unable to load capacity: ${error}` : "Capacity snapshot loading."}</MutedLine>\n      ) : (\n        <div className="grid gap-3">\n          {error ? (\n            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">\n              Showing last capacity snapshot. Refresh failed: {error}\n            </div>\n          ) : null}\n          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">\n            <Fact label="Active" value={`${saturation.workspace_counts.active_total} workspaces`} />\n            <Fact\n              label="Reserved CPU"\n              value={`${formatScalar(saturation.reserved_resources.steady_cpu)} steady / ${formatScalar(\n                saturation.reserved_resources.peak_cpu,\n              )} peak`}\n            />\n            <Fact\n              label="Reserved memory"\n              value={`${formatGb(saturation.reserved_resources.steady_memory_gb)} steady / ${formatGb(\n                saturation.reserved_resources.peak_memory_gb,\n              )} peak`}\n            />\n            <Fact\n              label="Disk free"\n              value={`${bytes(saturation.disk.free_bytes)} / ${formatPercent(saturation.disk.percent_free)}`}\n            />\n          </div>',
    '''function ResourceCapacityPanel({
  saturation,
  error,
  workspaceSummary,
  workspaceSummaryError,
}: {
  saturation: ResourceSaturationSummary | null;
  error: string | null;
  workspaceSummary: WorkspaceReliabilitySummary | null;
  workspaceSummaryError: string | null;
}) {
  const totalReason = workspaceSummary ? workspaceSummary.actionable_reason_count + workspaceSummary.unactionable_reason_count : 0;
  const coverage = totalReason > 0 ? Math.round((workspaceSummary!.actionable_reason_count / totalReason) * 100) : 0;

  return (
    <Panel title="Resource / Capacity" icon={<Server size={16} aria-hidden />}>
      {!saturation ? (
        <MutedLine>{error ? `Unable to load capacity: ${error}` : "Capacity snapshot loading."}</MutedLine>
      ) : (
        <div className="grid gap-3">
          {error ? (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
              Showing last capacity snapshot. Refresh failed: {error}
            </div>
          ) : null}
          {workspaceSummaryError ? (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
              Unable to load workspace reliability metrics: {workspaceSummaryError}
            </div>
          ) : null}
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
            <Fact label="Active" value={`${saturation.workspace_counts.active_total} workspaces`} />
            <Fact 
              label="Stuck" 
              value={workspaceSummary ? `${workspaceSummary.stuck_count} workspaces` : "—"} 
            />
            <Fact 
              label="Reason Coverage" 
              value={workspaceSummary ? `${coverage}% (${totalReason} tracked)` : "—"} 
            />
            <Fact
              label="Reserved CPU"
              value={`${formatScalar(saturation.reserved_resources.steady_cpu)} steady / ${formatScalar(
                saturation.reserved_resources.peak_cpu,
              )} peak`}
            />
            <Fact
              label="Reserved memory"
              value={`${formatGb(saturation.reserved_resources.steady_memory_gb)} steady / ${formatGb(
                saturation.reserved_resources.peak_memory_gb,
              )} peak`}
            />
            <Fact
              label="Disk free"
              value={`${bytes(saturation.disk.free_bytes)} / ${formatPercent(saturation.disk.percent_free)}`}
            />
          </div>'''
)

with open("apps/console/components/console-dashboard.tsx", "w") as f:
    f.write(content)
