import re

with open("apps/console/components/console-dashboard.tsx", "r") as f:
    content = f.read()

stale_block = """          <div className="flex flex-col items-end gap-1">
            <div className="flex items-center gap-1">
              <Badge value={overview.status} />
              {overview.status === "running" && overview.subphase ? (
                <span className="inline-flex h-6 items-center rounded-md border border-slate-200 bg-slate-100 px-2 text-[11px] font-medium text-slate-800">
                  ({overview.subphase})
                </span>
              ) : null}
            </div>
            {overview.is_stale_running ? (
              <span className="inline-flex h-6 items-center gap-1 rounded-md border border-amber-200 bg-amber-50 px-2 text-[11px] font-medium text-amber-900">
                <AlertCircle size={12} aria-hidden />
                <span>Stale execution (check logs)</span>
              </span>
            ) : null}
          </div>"""

content = content.replace(
    '          <Badge value={overview.status} />\n        </div>\n        <div className="grid min-w-0 gap-2 sm:grid-cols-2 xl:grid-cols-4">',
    stale_block + '\n        </div>\n        <div className="grid min-w-0 gap-2 sm:grid-cols-2 xl:grid-cols-4">'
)

# And in WorkspaceList:
list_stale_block = """              <div className="flex shrink-0 flex-col items-end gap-1">
                <div className="flex items-center gap-1">
                  <Badge value={item.status} />
                  {item.status === "running" && item.subphase ? (
                    <span className="inline-flex h-6 items-center rounded-md border border-slate-200 bg-slate-100 px-2 text-[11px] font-medium text-slate-800">
                      ({item.subphase})
                    </span>
                  ) : null}
                </div>
                {item.is_stale_running ? (
                  <span className="inline-flex h-6 max-w-44 items-center gap-1 rounded-md border border-amber-200 bg-amber-50 px-2 text-[11px] font-medium text-amber-900">
                    <AlertCircle size={12} aria-hidden />
                    <span className="truncate">Stale running</span>
                  </span>
                ) : null}"""

content = content.replace(
    '              <div className="flex shrink-0 flex-col items-end gap-1">\n                <Badge value={item.status} />',
    list_stale_block
)

with open("apps/console/components/console-dashboard.tsx", "w") as f:
    f.write(content)
print("Patched console-dashboard.tsx")
