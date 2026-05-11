#!/usr/bin/env bash
# Auto-dispatch T42 wave-2 specs when wave-1 dependencies merge.
#
# Idempotent: safe to re-run. Uses awf.db to detect active workspaces by their
# task_title (which embeds the AIRA-T<N> identifier). Uses `gh pr view` to
# detect merged wave-1 PRs.
#
# Wave-1 → wave-2 → wave-3 dependency map:
#   T54 (PR #384) merged  →  unblocks T56, T60, T63 (wave 2)
#   T68 (PR #382) merged  →  unblocks T69 (wave 2)
#   T56 (PR #388) merged  →  unblocks T57, T70 (wave 3a, also T61)
#   T60 (PR #392) merged  →  unblocks T61 (wave 3a)
#   T57 (wave-3 PR) merged →  unblocks T58 (wave 3b)
#   T58 (wave-3 PR) merged →  unblocks T59 (wave 3c)
#
# Spec files (must already exist):
#   T56 → airaagent_t42_p1_3_ledger_event_bus.json
#   T60 → airaagent_t42_p1_7_pg_notify_hook.json
#   T63 → airaagent_t42_p1_10_replay_endpoint.json
#   T69 → airaagent_t42_p1_16_benchmark_runner.json
#   T57 → airaagent_t42_p1_4_taskevent_adapter.json
#   T70 → airaagent_t42_p1_17_feature_flag_config.json
#   T61 → airaagent_t42_p1_8_sse_publisher.json
#   T58 → airaagent_t42_p1_5_other_adapters.json
#   T59 → airaagent_t42_p1_6_wire_bus_into_sources.json
#
# Workspace cap: 12 (bumped from 5 on 2026-05-03 after capacity audit:
# 119GB RAM box, ~700 MiB per workspace at steady state, ~50 GB free).
# Only dispatches if active count + would-dispatch ≤ 12.

set -euo pipefail

AWF_ROOT="/home/dimileeh/Projects/aira-agent-workspace-fabric"
WORK_DIR="/tmp/awf-realrun"
DB="$WORK_DIR/awf.db"
LOG="$WORK_DIR/auto-dispatch-t42-wave2.log"
SPECS_DIR="$AWF_ROOT/scripts"

ts() { date -Iseconds; }
log() { echo "[$(ts)] $*" | tee -a "$LOG"; }

cd "$AWF_ROOT"

# How many IMPLEMENTATION workspaces are alive? Filter by task_kind so we
# don't count release-PR monitors or feature-PR monitors from past sessions
# (those are cheap pollers, not actively-executing agents). The 12-cap targets
# implementation workspaces (task_kind='feature_branch_pr') in non-terminal
# state.
active=$(sqlite3 "$DB" "SELECT COUNT(*) FROM workspaces WHERE task_kind='feature_branch_pr' AND status IN ('provisioning','ready','running','validating','pushing','monitoring_pr');")
log "active implementation workspaces: $active / 12"

# Has a given AIRA-T<N> task already been dispatched? Counts workspaces in
# NON-FAILED state. A workspace in `failed` status (e.g. infra failure during
# provisioning) is eligible for re-dispatch on the next cron fire.
already_dispatched() {
  local key="$1"
  local n
  n=$(sqlite3 "$DB" "SELECT COUNT(*) FROM workspaces WHERE task_title LIKE '%${key}%' AND status != 'failed';")
  [ "$n" -gt 0 ]
}

# Is a given PR merged?
pr_merged() {
  local pr_num="$1"
  local merged
  merged=$(gh pr view "$pr_num" --repo dimileeh/aira-agent --json mergedAt -q .mergedAt 2>/dev/null || echo "")
  [ -n "$merged" ] && [ "$merged" != "null" ]
}

# Has a given AIRA-T<N> task already been MERGED to development? Searches PR
# titles for the task key. Used by wave-3 deps that need a prior wave-3 task
# merged before they can dispatch.
task_pr_merged() {
  local task_key="$1"
  local repo="${2:-dimileeh/aira-agent}"
  local pr_num
  pr_num=$(gh pr list --repo "$repo" --state merged --search "$task_key in:title" --limit 1 --json number -q '.[0].number' 2>/dev/null)
  [ -n "$pr_num" ] && [ "$pr_num" != "null" ]
}

dispatch() {
  local key="$1"
  local spec="$2"
  local label="$3"
  if already_dispatched "$key"; then
    log "  $key already dispatched — skip"
    return
  fi
  if [ "$active" -ge 12 ]; then
    log "  $key blocked — cap reached ($active/12)"
    return
  fi
  log "  dispatching $key ($label) spec=$spec"
  nohup .venv/bin/python scripts/run_awf.py --config "$SPECS_DIR/$spec" --work-dir "$WORK_DIR" --keep-state \
    > "$WORK_DIR/${key}-launch.log" 2>&1 &
  local pid=$!
  log "  $key spawned pid=$pid"
  active=$((active + 1))
}

# Check wave-1 deps + dispatch wave-2 tasks that are unblocked.
log "--- checking wave-1 PR merge status ---"

if pr_merged 384; then
  log "T54 (PR #384) merged — wave-2 schema deps unblocked"
  dispatch "AIRA-T56" "airaagent_t42_p1_3_ledger_event_bus.json" "bus core + append() helper"
  dispatch "AIRA-T60" "airaagent_t42_p1_7_pg_notify_hook.json" "pg_notify after_commit hook"
  dispatch "AIRA-T63" "airaagent_t42_p1_10_replay_endpoint.json" "replay endpoint"
else
  log "T54 (PR #384) NOT merged yet — T56/T60/T63 stay queued"
fi

if pr_merged 382; then
  log "T68 (PR #382) merged — benchmark runner unblocked"
  dispatch "AIRA-T69" "airaagent_t42_p1_16_benchmark_runner.json" "benchmark runner"
else
  log "T68 (PR #382) NOT merged yet — T69 stays queued"
fi

# Wave-3 chain: T58 needs T57 merged; T59 needs T58 merged.
log "--- checking wave-3 chained dependencies ---"

if task_pr_merged "AIRA-T57"; then
  log "T57 (TaskEvent adapter) merged — T58 unblocked"
  dispatch "AIRA-T58" "airaagent_t42_p1_5_other_adapters.json" "other 5 adapters"
else
  log "T57 NOT merged yet — T58 stays queued"
fi

if task_pr_merged "AIRA-T58"; then
  log "T58 (other 5 adapters) merged — T59 unblocked"
  dispatch "AIRA-T59" "airaagent_t42_p1_6_wire_bus_into_sources.json" "wire bus into 6 source paths"
else
  log "T58 NOT merged yet — T59 stays queued"
fi

# Governance UX chain: T80 (backend endpoints) → unblocks T81 (frontend warnings).
log "--- checking governance-UX chain (T80 → T81) ---"

if task_pr_merged "AIRA-T80"; then
  log "T80 (governance endpoints) merged — T81 unblocked"
  dispatch "AIRA-T81" "airaweb_t81_proactive_contradiction_warnings.json" "proactive contradiction warnings"
else
  log "T80 NOT merged yet — T81 stays queued"
fi

# Wave-4 chain: T62 (SSE wiring) → unblocks T65 (aira-web subscription).
# Wave-4 chain: T64 (DLQ retry) + T66 (observability) → unblock T67 (runbook).
log "--- checking wave-4 chained dependencies ---"

if task_pr_merged "AIRA-T62"; then
  log "T62 (SSE wiring) merged — T65 (aira-web subscription) unblocked"
  dispatch "AIRA-T65" "airaweb_t65_team_ledger_sse_subscription.json" "aira-web team-ledger SSE subscription"
else
  log "T62 NOT merged yet — T65 stays queued"
fi

# T67 needs BOTH T64 (DLQ retry endpoint reference) and T66 (alerts to reference)
if task_pr_merged "AIRA-T64" && task_pr_merged "AIRA-T66"; then
  log "T64 + T66 both merged — T67 (runbook) unblocked"
  dispatch "AIRA-T67" "airaagent_t42_p1_14_runbook.json" "ledger-bus-not-flowing runbook"
else
  log "T64 OR T66 not merged yet — T67 stays queued"
fi

# Phase 2 closeout chain: T86 (authority ranking) → unblocks T87 (benchmark re-run).
# T87 has a hard dep on T86 because the runner imports SOURCE_AUTHORITY_SCORE from
# T86's contradiction_engine module + the synthesis path it benchmarks must include
# the authority-ranked arbitration to produce a meaningful Phase 2 measurement.
log "--- checking Phase 2 closeout chain (T86 → T87) ---"

if task_pr_merged "AIRA-T86"; then
  log "T86 (authority ranking) merged — T87 (benchmark re-run) unblocked"
  dispatch "AIRA-T87" "airaagent_t42_p2_6_benchmark_phase2_rerun.json" "Phase 2 benchmark re-run + baseline update"
else
  log "T86 NOT merged yet — T87 stays queued"
fi

# Phase 3 chain:
#   Wave 1 (parallel, dispatched manually at start): T88, T89, T90
#   Wave 2:
#     T91 (e2e test) needs T88 (consumer) + T90 (policy_gate) merged
#     T92 (bridge audit) needs T89 (publisher coverage) merged
log "--- checking Phase 3 chain (Wave 2: T88+T90 → T91; T89 → T92) ---"

if task_pr_merged "AIRA-T88" && task_pr_merged "AIRA-T90"; then
  log "T88 + T90 both merged — T91 (Phase 3 e2e test) unblocked"
  dispatch "AIRA-T91" "airaagent_t42_p3_4_e2e_test.json" "Phase 3 e2e: contradiction → orchestrator → log → SSE"
else
  log "T88 OR T90 not merged yet — T91 stays queued"
fi

if task_pr_merged "AIRA-T89"; then
  log "T89 (publisher coverage) merged — T92 (bridge audit) unblocked"
  dispatch "AIRA-T92" "airaagent_t42_p3_5_bridge_audit.json" "Phase 3 bridge audit (Phase 4 prereq)"
else
  log "T89 NOT merged yet — T92 stays queued"
fi

# Stuck-claude auto-kill: any active workspace whose claude proc has been
# alive >15 min with <5% CPU is stuck in ep_poll (awaiting an Anthropic API
# response that won't arrive). The fix every time has been to kill -9 the
# proc so AWF's monitor respawns it. Bake that into the cron so I don't
# have to surface stuck workspaces manually.
log "--- stuck-claude check (any claude alive >15min with <5% CPU = kill) ---"

stuck_kill_count=0
for ws_id in $(sqlite3 "$DB" "SELECT id FROM workspaces WHERE status IN ('running','validating','pushing','monitoring_pr');" 2>/dev/null); do
  container="awf-${ws_id}-agent"
  # Skip workspaces whose container isn't up.
  docker ps --filter "name=^${container}$" --format '{{.Names}}' 2>/dev/null | grep -q "$container" || continue

  # Read claude proc state. Format: "PID ETIME PCPU".
  # `|| true`: when claude isn't running yet (provisioning/early startup),
  # `ps -C claude` returns rc=1 — pipefail + set -e would kill the script.
  proc_info=$(docker exec "$container" ps -o pid=,etime=,pcpu= -C claude 2>/dev/null | head -1 | awk '{$1=$1};1' || true)
  [ -z "$proc_info" ] && continue

  pid=$(echo "$proc_info" | awk '{print $1}')
  etime=$(echo "$proc_info" | awk '{print $2}')
  pcpu=$(echo "$proc_info" | awk '{print $3}')

  # Convert etime to seconds. Format variants: SS, MM:SS, HH:MM:SS, D-HH:MM:SS.
  etime_s=$(awk -v t="$etime" 'BEGIN{
    n=split(t,a,/[-:]/);
    if(n==1) print a[1];
    else if(n==2) print a[1]*60+a[2];
    else if(n==3) print a[1]*3600+a[2]*60+a[3];
    else if(n==4) print a[1]*86400+a[2]*3600+a[3]*60+a[4];
  }')

  pcpu_int=${pcpu%.*}
  [ -z "$pcpu_int" ] && pcpu_int=0

  if [ "${etime_s:-0}" -gt 900 ] && [ "$pcpu_int" -lt 5 ]; then
    log "  STUCK: ws=${ws_id:0:16} pid=$pid etime=${etime} (${etime_s}s) cpu=${pcpu}% — killing"
    docker exec "$container" kill -9 "$pid" 2>/dev/null && stuck_kill_count=$((stuck_kill_count + 1))
  fi
done
log "  stuck claudes killed this tick: $stuck_kill_count"

# Phase 4 chain (Guardian Agent — see ~/.claude/plans/aira-t42-project-ledger-orchestrator.md):
#   Wave 1 (manually dispatched): T93 schema, T94 design docs
#   Wave 2: T95 (Guardian core) — needs T93
#   Wave 3 (4 validators in parallel) — needs T95 + T94:
#     T96 GitHub, T97 Slack, T98 Telegram, T99 Sprint-status
#   Wave 4 + 5 will be added later (specs not yet written)
log "--- checking Phase 4 chain (Wave 2: T93 → T95; Wave 3: T95+T94 → T96/T97/T98/T99) ---"

if task_pr_merged "AIRA-T93"; then
  log "T93 (drift report schema) merged — T95 (Guardian core) unblocked"
  dispatch "AIRA-T95" "airaagent_t42_p4_2_guardian_core.json" "Guardian core agent + log consumer"
else
  log "T93 NOT merged yet — T95 stays queued"
fi

if task_pr_merged "AIRA-T94" && task_pr_merged "AIRA-T95"; then
  log "T94 + T95 both merged — Phase 4 Wave 3 (4 validators) unblocked"
  # Stagger by 6s. Parallel run_awf.py instances race on `git remote update`
  # for the shared mirror; losers die with `cannot lock ref refs/remotes/origin/development`
  # (see ~/.claude/projects/-home-dimileeh-Projects-aira/memory/feedback_awf_parallel_dispatch_git_lock.md).
  # `dispatch` is a no-op for already-dispatched tasks so re-runs stay safe.
  dispatch "AIRA-T99" "airaagent_t42_p4_6_sprint_validator.json" "Sprint-status validator (framework smoke test)"
  sleep 6
  dispatch "AIRA-T96" "airaagent_t42_p4_3_github_validator.json" "GitHub PR validator"
  sleep 6
  dispatch "AIRA-T97" "airaagent_t42_p4_4_slack_validator.json" "Slack reaction validator"
  sleep 6
  dispatch "AIRA-T98" "airaagent_t42_p4_5_telegram_validator.json" "Telegram validator"
else
  log "T94 OR T95 not merged yet — Phase 4 Wave 3 stays queued"
fi

# Phase 4 Wave 4 chain (operationalization):
#   Wave 4A: T100 metrics + exit-criteria (P4.7) — needs Wave 3 done (T96/T97/T98/T99 all merged)
#   Wave 4B (parallel after T100): T101 propose-fix backend (P4.8) + T103 benchmark (P4.10)
#   Wave 4C: T102 propose-fix UI (P4.9) — needs T101 merged (aira-WEB repo)
#   Wave 4D: T104 runbook (P4.12) — needs T100/T101/T102/T103 all merged
log "--- checking Phase 4 Wave 4 chain (T100 → T101+T103 → T102; T100+T101+T102+T103 → T104) ---"

if task_pr_merged "AIRA-T96" && task_pr_merged "AIRA-T97" && task_pr_merged "AIRA-T98" && task_pr_merged "AIRA-T99"; then
  log "Wave 3 fully merged — T100 (metrics + exit-criteria) unblocked"
  dispatch "AIRA-T100" "airaagent_t42_p4_7_guardian_metrics.json" "Guardian metrics + exit-criteria evaluator"
else
  log "Wave 3 not fully merged — T100 stays queued"
fi

if task_pr_merged "AIRA-T100"; then
  log "T100 merged — Wave 4B (T101 + T103) unblocked"
  dispatch "AIRA-T101" "airaagent_t42_p4_8_propose_fix_backend.json" "Propose-fix backend API + applier registry"
  sleep 6
  dispatch "AIRA-T103" "airaagent_t42_p4_10_guardian_benchmark.json" "Guardian benchmark (10 deliberate drifts)"
else
  log "T100 not merged yet — Wave 4B (T101 + T103) stays queued"
fi

if task_pr_merged "AIRA-T101"; then
  log "T101 merged — T102 (aira-web propose-fix UI) unblocked"
  dispatch "AIRA-T102" "airaweb_t42_p4_9_propose_fix_ui.json" "Propose-fix UI (aira-web)"
else
  log "T101 not merged yet — T102 stays queued"
fi

# T102 lives in aira-WEB (everything else in this chain is aira-AGENT). Pass the
# repo arg explicitly so the merge-check hits the right repo; without it the
# check always returns false and T104 stays gated forever.
if task_pr_merged "AIRA-T100" && task_pr_merged "AIRA-T101" && task_pr_merged "AIRA-T102" dimileeh/aira-web && task_pr_merged "AIRA-T103"; then
  log "T100+T101+T102+T103 all merged — T104 (runbook) unblocked"
  dispatch "AIRA-T104" "airaagent_t42_p4_12_guardian_runbook.json" "Guardian operational runbook"
else
  log "Wave 4B/C not fully merged — T104 stays queued"
fi

# Status summary at end
log "--- final state: active=$active/12 ---"
log ""
