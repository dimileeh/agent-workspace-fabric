"""Tracked ``docker compose exec`` helpers.

``docker compose exec`` has two process trees: the local docker-compose client
and the command running inside the container. Killing the local client on a
timeout does not guarantee the in-container process tree is gone, so AWF wraps
container commands with an invocation id and uses that id for targeted cleanup.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from awf.common.commands import AsyncCommandRunner, CommandResult
from awf.common.logging import get_logger

EXEC_PROCESS_CLEANUP_FAILED = "EXEC_PROCESS_CLEANUP_FAILED"
_INVOCATION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_log = get_logger(__name__)


class ComposeExecCleanupError(RuntimeError):
    """Raised when AWF cannot prove a compose-exec process tree is gone."""

    reason_code = EXEC_PROCESS_CLEANUP_FAILED

    def __init__(
        self,
        *,
        invocation_id: str,
        source: str,
        label: str,
        message: str,
        cleanup_result: CommandResult | None = None,
    ) -> None:
        self.invocation_id = invocation_id
        self.source = source
        self.label = label
        self.cleanup_result = cleanup_result
        super().__init__(
            f"{EXEC_PROCESS_CLEANUP_FAILED}: {source} {label} invocation {invocation_id}: {message}"
        )


@dataclass(frozen=True)
class TrackedComposeExec:
    """A compose-exec command plus the metadata needed for cleanup."""

    args: list[str]
    invocation_id: str
    compose_project: str
    compose_file: Path
    service: str
    workdir: str
    source: str
    label: str
    wrapper_script: str
    cleanup_script: str


def build_tracked_compose_exec(
    *,
    compose_project: str,
    compose_file: Path,
    cli_args: list[str],
    source: str,
    label: str,
    service: str = "agent",
    workdir: str = "/workspace",
    invocation_id: str | None = None,
    preserve_stdin: bool = False,
) -> TrackedComposeExec:
    """Build a compose-exec argv that tags the in-container command."""

    if not cli_args:
        raise ValueError("cli_args must not be empty")
    invocation_id = invocation_id or _new_invocation_id()
    _validate_invocation_id(invocation_id)
    wrapper_script = _tracked_exec_wrapper_script(preserve_stdin=preserve_stdin)
    cleanup_script = _cleanup_script(invocation_id)
    args = [
        *_compose_exec_prefix(
            compose_project=compose_project,
            compose_file=compose_file,
            service=service,
            workdir=workdir,
        ),
        "sh",
        "-lc",
        wrapper_script,
        "awf-exec",
        invocation_id,
        *cli_args,
    ]
    return TrackedComposeExec(
        args=args,
        invocation_id=invocation_id,
        compose_project=compose_project,
        compose_file=compose_file,
        service=service,
        workdir=workdir,
        source=source,
        label=label,
        wrapper_script=wrapper_script,
        cleanup_script=cleanup_script,
    )


def build_cleanup_compose_exec(invocation: TrackedComposeExec) -> list[str]:
    """Build the targeted cleanup command for a tracked invocation."""

    return [
        *_compose_exec_prefix(
            compose_project=invocation.compose_project,
            compose_file=invocation.compose_file,
            service=invocation.service,
            workdir=invocation.workdir,
        ),
        "sh",
        "-lc",
        invocation.cleanup_script,
        "awf-cleanup",
        invocation.invocation_id,
    ]


async def cleanup_compose_exec_invocation(
    runner: AsyncCommandRunner,
    invocation: TrackedComposeExec,
    *,
    workspace_id: str | None = None,
) -> CommandResult:
    """Run targeted cleanup and raise if tagged processes remain."""

    _log.warning(
        "compose_exec.cleanup.start",
        workspace_id=workspace_id,
        compose_project=invocation.compose_project,
        service=invocation.service,
        source=invocation.source,
        label=invocation.label,
        invocation_id=invocation.invocation_id,
        reason_code=EXEC_PROCESS_CLEANUP_FAILED,
    )
    result = await runner.run(build_cleanup_compose_exec(invocation))
    if result.ok:
        event = "compose_exec.cleanup.absent"
        if "awf cleanup: killed" in result.stdout or "awf cleanup: killed" in result.stderr:
            event = "compose_exec.cleanup.succeeded"
        _log.info(
            event,
            workspace_id=workspace_id,
            compose_project=invocation.compose_project,
            service=invocation.service,
            source=invocation.source,
            label=invocation.label,
            invocation_id=invocation.invocation_id,
            reason_code=EXEC_PROCESS_CLEANUP_FAILED,
        )
        return result

    stderr = _bounded(result.stderr.strip() or result.stdout.strip() or "<no cleanup output>")
    _log.error(
        "compose_exec.cleanup.failed",
        workspace_id=workspace_id,
        compose_project=invocation.compose_project,
        service=invocation.service,
        source=invocation.source,
        label=invocation.label,
        invocation_id=invocation.invocation_id,
        returncode=result.returncode,
        stderr=stderr,
        reason_code=EXEC_PROCESS_CLEANUP_FAILED,
    )
    raise ComposeExecCleanupError(
        invocation_id=invocation.invocation_id,
        source=invocation.source,
        label=invocation.label,
        message=stderr,
        cleanup_result=result,
    )


async def cleanup_compose_exec_invocation_after_cancellation(
    runner: AsyncCommandRunner,
    invocation: TrackedComposeExec,
    *,
    workspace_id: str | None = None,
) -> CommandResult:
    """Run cleanup to completion even if the caller is cancelled again."""

    cleanup_task = asyncio.create_task(
        cleanup_compose_exec_invocation(
            runner,
            invocation,
            workspace_id=workspace_id,
        )
    )
    while True:
        try:
            return await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            if cleanup_task.done():
                return cleanup_task.result()


def cleanup_failure_message(exc: ComposeExecCleanupError) -> str:
    """Bounded operator-facing message for cleanup failures."""

    return _bounded(str(exc), limit=2000)


def _compose_exec_prefix(
    *,
    compose_project: str,
    compose_file: Path,
    service: str,
    workdir: str,
) -> list[str]:
    return [
        "docker",
        "compose",
        "-p",
        compose_project,
        "-f",
        str(compose_file),
        "exec",
        "-T",
        "-w",
        workdir,
        service,
    ]


def _new_invocation_id() -> str:
    return f"awf_{uuid.uuid4().hex}"


def _validate_invocation_id(invocation_id: str) -> None:
    if not _INVOCATION_ID_RE.fullmatch(invocation_id):
        raise ValueError("invocation_id contains unsupported shell characters")


def _tracked_exec_wrapper_script(*, preserve_stdin: bool = False) -> str:
    # Positional argv after ``sh -lc <script>`` is:
    #   $0=awf-exec, $1=<invocation_id>, $2...=<original command argv>
    # The real command is never shell-joined; ``shift`` leaves it as "$@".
    stdin_setup = ""
    setsid_stdin_redirect = "</dev/null"
    setsid_stdin_cleanup = ""
    setsid_stdin_fd_close = ""
    exec_stdin_setup = "exec </dev/null"
    permissions_setup = ""
    if preserve_stdin:
        permissions_setup = """
chmod 700 "$awf_exec_dir" 2>/dev/null || true
""".strip()
        stdin_setup = """
stdin_path="$awf_exec_dir/stdin"
previous_umask="$(umask)"
umask 077
cat > "$stdin_path"
cat_status=$?
umask "$previous_umask"
if [ "$cat_status" -ne 0 ]; then
  rm -f "$stdin_path" 2>/dev/null || true
  exit "$cat_status"
fi
chmod 600 "$stdin_path" 2>/dev/null || true
""".strip()
        setsid_stdin_redirect = "<&9"
        setsid_stdin_cleanup = """
exec 9< "$stdin_path"
exec_status=$?
if [ "$exec_status" -ne 0 ]; then
  rm -f "$stdin_path" 2>/dev/null || true
  exit "$exec_status"
fi
rm -f "$stdin_path" 2>/dev/null || true
""".strip()
        setsid_stdin_fd_close = "exec 9<&-"
        exec_stdin_setup = """
exec < "$stdin_path"
exec_status=$?
if [ "$exec_status" -ne 0 ]; then
  rm -f "$stdin_path" 2>/dev/null || true
  exit "$exec_status"
fi
rm -f "$stdin_path" 2>/dev/null || true
""".strip()
    return f"""
set +e
invocation_id=$1
shift
unset GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES 2>/dev/null || true
export AWF_EXEC_INVOCATION_ID="$invocation_id"
awf_exec_dir="/tmp/awf-exec/$invocation_id"
mkdir -p "$awf_exec_dir" 2>/dev/null || true
{permissions_setup}
printf '%s\\n' "$$" > "$awf_exec_dir/wrapper_pid" 2>/dev/null || true
{stdin_setup}
if command -v setsid >/dev/null 2>&1; then
  {setsid_stdin_cleanup}
  setsid "$@" {setsid_stdin_redirect} &
  child_pid=$!
  {setsid_stdin_fd_close}
  printf '%s\\n' "$child_pid" > "$awf_exec_dir/pid" 2>/dev/null || true
  printf '%s\\n' "$child_pid" > "$awf_exec_dir/pgid" 2>/dev/null || true
  wait "$child_pid"
  exit $?
fi
printf '%s\\n' "$$" > "$awf_exec_dir/pid" 2>/dev/null || true
{exec_stdin_setup}
exec "$@"
""".strip()


def _cleanup_script(invocation_id: str) -> str:
    target_marker = f"AWF_EXEC_INVOCATION_ID={invocation_id}"
    return f"""
set +e
invocation_id={invocation_id}
target_marker='{target_marker}'
awf_exec_dir="/tmp/awf-exec/$invocation_id"
self_pid=$$

is_number() {{
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}}

add_pid() {{
  pid=$1
  is_number "$pid" || return 0
  [ "$pid" = "$self_pid" ] && return 0
  kill -0 "$pid" 2>/dev/null || return 0
  case " $pids " in
    *" $pid "*) ;;
    *) pids="$pids $pid" ;;
  esac
}}

collect_pids() {{
  pids=""
  for file in "$awf_exec_dir/pid" "$awf_exec_dir/wrapper_pid"; do
    [ -r "$file" ] || continue
    while IFS= read -r pid; do
      add_pid "$pid"
    done < "$file"
  done
  for env in /proc/[0-9]*/environ; do
    [ -r "$env" ] || continue
    pid=${{env%/environ}}
    pid=${{pid##*/}}
    [ "$pid" = "$self_pid" ] && continue
    if tr '\\000' '\\n' < "$env" 2>/dev/null | grep -qx "$target_marker"; then
      add_pid "$pid"
    fi
  done
}}

kill_groups() {{
  [ -r "$awf_exec_dir/pgid" ] || return 0
  while IFS= read -r pgid; do
    is_number "$pgid" || continue
    [ "$pgid" -gt 1 ] 2>/dev/null || continue
    kill -"$1" -- "-$pgid" 2>/dev/null || kill -"$1" "-$pgid" 2>/dev/null || true
  done < "$awf_exec_dir/pgid"
}}

kill_pids() {{
  for pid in $pids; do
    [ "$pid" = "$self_pid" ] && continue
    kill -"$1" "$pid" 2>/dev/null || true
  done
}}

awf_cleanup_sleep_seconds=0.1

sleep_between_checks() {{
  if [ "$awf_cleanup_sleep_seconds" = "0.1" ]; then
    if sleep 0.1 2>/dev/null; then
      return 0
    fi
    awf_cleanup_sleep_seconds=1
    awf_cleanup_wait_limit=2
  fi
  sleep "$awf_cleanup_sleep_seconds"
}}

wait_until_absent() {{
  i=0
  awf_cleanup_wait_limit=20
  [ "$awf_cleanup_sleep_seconds" = "1" ] && awf_cleanup_wait_limit=2
  while [ "$i" -lt "$awf_cleanup_wait_limit" ]; do
    collect_pids
    [ -z "$pids" ] && return 0
    sleep_between_checks
    i=$((i + 1))
  done
  return 1
}}

collect_pids
if [ -z "$pids" ]; then
  echo "awf cleanup: absent $invocation_id"
  exit 0
fi
kill_groups TERM
kill_pids TERM
if ! wait_until_absent; then
  kill_groups KILL
  kill_pids KILL
  wait_until_absent
fi
collect_pids
if [ -n "$pids" ]; then
  echo "awf cleanup: tagged processes still alive for $invocation_id:$pids" >&2
  exit 1
fi
rm -rf "$awf_exec_dir" 2>/dev/null || true
echo "awf cleanup: killed $invocation_id"
exit 0
""".strip()


def _bounded(value: str, *, limit: int = 1000) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."
