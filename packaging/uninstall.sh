#!/usr/bin/env bash
#
# Agent Workspace Fabric uninstaller.
#
# A standalone, hosted (curl | bash) uninstaller with a conservative,
# AWF-managed cleanup contract. It is the symmetric counterpart to
# packaging/install.sh and, like it, is fully self-contained: a piped one-liner
# cannot `source` a sibling library, so the managed uv/pipx removal helpers are
# mirrored verbatim from install.sh and guarded by a parity test rather than a
# runtime cross-script dependency.
#
# Contract:
#   * Preserve user and agent credentials by default. There is NO flag that
#     removes provider secrets, keyring entries, env refs, or gh/agent auth.
#   * Remove only AWF-managed installs (the uv/pipx package, exactly as
#     `install.sh --uninstall`) and an explicit allowlist of non-secret AWF
#     state under ${AWF_HOME} (config.yml, service/) when opted into.
#   * Refuse or no-op on an unmanaged `awf` (UNINSTALL_REFUSED_UNMANAGED);
#     nothing-installed is a clean no-op.
#   * --dry-run plans every action and mutates nothing.
#   * Destructive filesystem cleanup (state/config purge, uv removal) requires
#     --yes or an interactive confirmation; non-interactive without --yes fails
#     closed with CONFIRMATION_REQUIRED.
#   * --remove-uv removes uv only when an AWF ownership marker proves AWF
#     bootstrapped it (else UV_REMOVAL_REFUSED_UNOWNED).
#
# It emits stable shell-level reason tokens on stderr (it does not depend on the
# Python first-run error catalog):
#
#   UNSUPPORTED_PLATFORM MISSING_DEPENDENCY BAD_USAGE UNINSTALL_REFUSED_UNMANAGED
#   INSTALL_METHOD_FAILED CONFIRMATION_REQUIRED UV_REMOVAL_REFUSED_UNOWNED
#   UV_REMOVAL_FAILED STATE_CLEANUP_FAILED
#
# Testability seams (keep fixture tests hermetic, no real release/network):
#   AWF_HOME                       base dir for the state/config allowlist
#                                  (defaults to ${HOME}/.awf).
#   AWF_UV_MARKER                  path of the uv-ownership marker install.sh
#                                  writes (defaults to
#                                  ${HOME}/.awf/uv-bootstrap.marker).
#   AWF_UNINSTALL_FORCE_INTERACTIVE treat stdin as a TTY so the destructive
#                                  confirm prompt can be exercised under a piped
#                                  stdin.
#   --shell <name>                 override shell detection for any PATH advice.

set -euo pipefail

PACKAGE="agent-workspace-fabric"

# Base dir for the state/config cleanup allowlist. Matches the host setup default
# (~/.awf; see src/awf/host_setup/config.py and src/awf/cli/init_ops.py).
AWF_HOME="${AWF_HOME:-${HOME}/.awf}"
# uv-ownership marker the installer writes on a real bootstrap (see install.sh).
AWF_UV_MARKER="${AWF_UV_MARKER:-${HOME}/.awf/uv-bootstrap.marker}"

INSTALL_DIR=""
SHELL_OVERRIDE=""
DRY_RUN=0
NON_INTERACTIVE=0
ASSUME_YES=0
PURGE_CONFIG=0
PURGE_STATE=0
REMOVE_UV=0

# Set by uninstall_awf when an unmanaged awf is found on PATH. The refusal is
# deferred (not raised inline) so the marker-authorized uv-removal lane can still
# run; main() surfaces it as UNINSTALL_REFUSED_UNMANAGED after remove_uv.
UNMANAGED_AWF_PATH=""

# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

say() {
    printf '%s\n' "$*"
}

plan() {
    printf '[plan] %s\n' "$*"
}

warn() {
    printf '%s\n' "$*" >&2
}

# fail TOKEN message...  -> print reason token to stderr and exit non-zero.
fail() {
    local token="$1"
    shift
    printf 'awf-uninstall: %s: %s\n' "$token" "$*" >&2
    exit 1
}

bad_usage() {
    printf 'awf-uninstall: %s: %s\n' "BAD_USAGE" "$*" >&2
    exit 2
}

usage() {
    cat <<'EOF'
Agent Workspace Fabric uninstaller

Usage:
  uninstall.sh [options]

Options:
  --dry-run             Plan every action and mutate nothing.
  --non-interactive     Never prompt; destructive lanes then require --yes.
  --yes                 Explicit consent for destructive cleanup (state/config/uv).
  --install-dir <path>  Bin dir the matching install used (forwarded to uv/pipx
                        removal so neither manager orphans the awf entrypoint).
  --purge-config        Remove the non-secret host setup config (~/.awf/config.yml).
  --purge-state         Remove AWF host runtime/state (~/.awf/service).
  --remove-uv           Also remove uv, only when an AWF ownership marker proves
                        AWF bootstrapped it (else UV_REMOVAL_REFUSED_UNOWNED).
  --all                 Convenience: managed package + --purge-config + --purge-state
                        (NOT credentials, NOT uv unless --remove-uv is also given).
  --shell <name>        Override shell detection (zsh|bash|fish) for PATH advice.
  --help                Print this help and exit.

Trust:
  Your credentials are preserved by default: this uninstaller never removes
  provider secrets, keyring entries, env refs, or gh/agent auth, and there is no
  flag that does. The default action removes only an AWF-managed uv/pipx package
  (exactly like the installer's --uninstall); an unmanaged awf is never removed
  (UNINSTALL_REFUSED_UNMANAGED). Destructive filesystem cleanup
  (--purge-config/--purge-state/--remove-uv) needs --yes or an interactive
  confirmation, and uv is removed only when an AWF ownership marker proves AWF
  bootstrapped it. Docker volumes and Compose stacks are left intact; stop them
  with `awf service gc` or `docker compose down` before uninstalling if desired.
EOF
}

# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------

require_value() {
    # require_value <flag> <count-remaining>
    if [ "$2" -lt 2 ]; then
        bad_usage "missing value for $1"
    fi
}

require_install_dir() {
    # Mirrors install.sh: every downstream use gates on [ -n "$INSTALL_DIR" ], so
    # an explicit empty value is indistinguishable from omitting the flag and would
    # silently mutate uv/pipx's default bin dir. Reject emptiness at the boundary;
    # omitting --install-dir entirely is still valid (uses the manager default).
    if [ -z "$1" ]; then
        bad_usage "empty value for --install-dir; pass --install-dir <path> or omit it to use the default bin directory"
    fi
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --install-dir)
                require_value "$1" "$#"
                INSTALL_DIR="$2"
                require_install_dir "$INSTALL_DIR"
                shift 2
                ;;
            --install-dir=*)
                INSTALL_DIR="${1#*=}"
                require_install_dir "$INSTALL_DIR"
                shift
                ;;
            --shell)
                require_value "$1" "$#"
                SHELL_OVERRIDE="$2"
                shift 2
                ;;
            --shell=*)
                SHELL_OVERRIDE="${1#*=}"
                shift
                ;;
            --dry-run)
                DRY_RUN=1
                shift
                ;;
            --non-interactive)
                NON_INTERACTIVE=1
                shift
                ;;
            --yes)
                ASSUME_YES=1
                shift
                ;;
            --purge-config)
                PURGE_CONFIG=1
                shift
                ;;
            --purge-state)
                PURGE_STATE=1
                shift
                ;;
            --remove-uv)
                REMOVE_UV=1
                shift
                ;;
            --all)
                PURGE_CONFIG=1
                PURGE_STATE=1
                shift
                ;;
            --help | -h)
                usage
                exit 0
                ;;
            *)
                bad_usage "unknown argument: $1"
                ;;
        esac
    done
}

# --------------------------------------------------------------------------
# Platform detection (mirrored from install.sh)
# --------------------------------------------------------------------------

detect_platform() {
    local os arch
    os="$(uname -s)"
    arch="$(uname -m)"

    case "$os" in
        Darwin | Linux) ;;
        *) fail UNSUPPORTED_PLATFORM "unsupported operating system: $os (supported: macOS, Linux)" ;;
    esac

    case "$arch" in
        x86_64 | amd64 | arm64 | aarch64) ;;
        *) fail UNSUPPORTED_PLATFORM "unsupported architecture: $arch" ;;
    esac
}

# --------------------------------------------------------------------------
# Interactivity + confirmation gate
# --------------------------------------------------------------------------

# Whether the destructive confirm prompt may be shown. --non-interactive is the
# explicit override and wins over everything. The AWF_UNINSTALL_FORCE_INTERACTIVE
# seam then stands in for a real TTY (the black-box tests run the script under a
# piped, non-TTY stdin). Only if neither applies do we consult `[ -t 0 ]`.
stdin_is_interactive() {
    [ "$NON_INTERACTIVE" -eq 1 ] && return 1
    [ "${AWF_UNINSTALL_FORCE_INTERACTIVE:-0}" = "1" ] && return 0
    [ -t 0 ]
}

# confirm_destructive "<what>"  -> 0 only when the destructive lane is authorized.
# --yes is explicit consent; --dry-run authorizes the plan (callers still branch
# on DRY_RUN before mutating); otherwise prompt on a TTY. A non-interactive run
# with no --yes returns 1 so the caller fails CONFIRMATION_REQUIRED. The
# `|| reply=""` keeps a closed stdin from tripping `set -e` via read's status.
confirm_destructive() {
    [ "$ASSUME_YES" -eq 1 ] && return 0
    if [ "$DRY_RUN" -eq 1 ]; then
        return 0
    fi
    if stdin_is_interactive; then
        printf '%s' "Remove $1? This cannot be undone. [y/N] " >&2
        local reply=""
        IFS= read -r reply || reply=""
        case "$reply" in
            y | Y | yes | YES | Yes) return 0 ;;
            *) return 1 ;;
        esac
    fi
    return 1
}

# --------------------------------------------------------------------------
# uv-ownership-gated uv removal
# --------------------------------------------------------------------------

# Remove uv only when --remove-uv is given AND the AWF ownership marker proves AWF
# bootstrapped it — proof means the marker exists AND carries the installed_by=awf
# line bootstrap_uv writes, not merely that some file sits at the path. Without
# that proof we refuse (UV_REMOVAL_REFUSED_UNOWNED) so a uv the user installed
# themselves is never removed. The removal is destructive,
# so it is confirm-gated; --dry-run only plans it. Removal deliberately deletes the
# uv/uvx binaries the installer placed rather than delegating to a `uv self`
# subcommand: deleting the recorded binaries has predictable scope (exactly what
# bootstrap_uv placed, in the marker's uv_bin_dir) and no shell-RC or PATH side
# effects, and stays correct regardless of which `uv self` subcommands a given uv
# version happens to expose. bootstrap_uv recorded that directory in the marker
# (uv_bin_dir=...). If the recorded binaries are not actually present (a wrong/legacy
# uv_bin_dir, binaries the user moved, or a stale marker) or deleting them fails, we
# raise UV_REMOVAL_FAILED and keep the marker so a later --remove-uv can retry, rather
# than reporting a false success while uv is still installed.
remove_uv() {
    [ "$REMOVE_UV" -eq 1 ] || return 0
    if [ ! -f "$AWF_UV_MARKER" ]; then
        fail UV_REMOVAL_REFUSED_UNOWNED "refusing to remove uv: no AWF ownership marker at ${AWF_UV_MARKER}; AWF only removes a uv it bootstrapped"
    fi
    # A regular file at the marker path is not by itself proof: an empty, corrupt,
    # or unrelated file there must not authorize deleting uv. bootstrap_uv writes a
    # deterministic installed_by=awf line, so require it before trusting the marker.
    if ! grep -q '^installed_by=awf$' "$AWF_UV_MARKER" 2>/dev/null; then
        fail UV_REMOVAL_REFUSED_UNOWNED "refusing to remove uv: ownership marker at ${AWF_UV_MARKER} lacks installed_by=awf; AWF only removes a uv it bootstrapped"
    fi
    confirm_destructive "uv (AWF-bootstrapped)" \
        || fail CONFIRMATION_REQUIRED "uv removal requires --yes (non-interactive) or interactive confirmation"
    # The bin dir AWF linked uv/uvx into, recorded by bootstrap_uv. Fall back to the
    # installer default if a legacy marker predates the uv_bin_dir field.
    local uv_bin_dir=""
    uv_bin_dir="$(awk -F= '$1 == "uv_bin_dir" { print substr($0, index($0, "=") + 1); exit }' "$AWF_UV_MARKER" 2>/dev/null || true)"
    [ -n "$uv_bin_dir" ] || uv_bin_dir="${HOME}/.local/bin"
    if [ "$DRY_RUN" -eq 1 ]; then
        plan "remove uv binaries (uv, uvx) from ${uv_bin_dir} and delete ${AWF_UV_MARKER}"
        return 0
    fi
    # rm -f exits zero on already-missing paths, so a wrong/legacy uv_bin_dir,
    # binaries the user moved, or a stale marker would let us delete the ownership
    # marker and report success while uv is still on the system. Treat removal as
    # done only when at least one recorded binary is actually present; otherwise
    # fail closed (keeping the marker) so the proof survives and a later
    # --remove-uv can retry once uv sits at the recorded location.
    if [ ! -e "${uv_bin_dir}/uv" ] && [ ! -e "${uv_bin_dir}/uvx" ]; then
        fail UV_REMOVAL_FAILED "no AWF-bootstrapped uv binaries found in ${uv_bin_dir}; keeping ${AWF_UV_MARKER} so --remove-uv can retry once uv is at the recorded location"
    fi
    rm -f "${uv_bin_dir}/uv" "${uv_bin_dir}/uvx" \
        || fail UV_REMOVAL_FAILED "could not remove uv binaries from ${uv_bin_dir}; keeping ${AWF_UV_MARKER} so --remove-uv can retry"
    rm -f "$AWF_UV_MARKER"
    say "Removed AWF-bootstrapped uv."
}

# --------------------------------------------------------------------------
# AWF state/config cleanup (opt-in, credential-preserving)
# --------------------------------------------------------------------------

# remove_state_path <flag-set?> <path> <kind> <human>  -> remove an allowlisted
# entry under ${AWF_HOME}. A missing target is a benign no-op (no confirmation
# needed); a present target is confirm-gated, dry-run previews the exact path, and
# a removal failure on a present path raises STATE_CLEANUP_FAILED rather than being
# masked. It NEVER removes ${AWF_HOME} itself or anything off the allowlist, so a
# credential/secrets path beside config.yml and service/ survives every lane.
remove_state_path() {
    local enabled="$1" target="$2" kind="$3" human="$4"
    [ "$enabled" -eq 1 ] || return 0
    if [ ! -e "$target" ]; then
        say "No ${human} at ${target}; nothing to remove."
        return 0
    fi
    confirm_destructive "${human} at ${target}" \
        || fail CONFIRMATION_REQUIRED "${human} removal requires --yes (non-interactive) or interactive confirmation"
    if [ "$DRY_RUN" -eq 1 ]; then
        plan "remove ${human} ${kind} ${target}"
        return 0
    fi
    if [ "$kind" = "dir" ]; then
        rm -rf "$target" || fail STATE_CLEANUP_FAILED "could not remove ${human} at ${target}"
    else
        rm -f "$target" || fail STATE_CLEANUP_FAILED "could not remove ${human} at ${target}"
    fi
    say "Removed ${human} ${target}."
}

purge_state() {
    # service/ is a directory; config.yml is a file. Both are the only entries the
    # allowlist authorizes — ${AWF_HOME} and any sibling (e.g. a secrets store) are
    # never touched.
    remove_state_path "$PURGE_STATE" "${AWF_HOME}/service" "dir" "AWF host state"
    remove_state_path "$PURGE_CONFIG" "${AWF_HOME}/config.yml" "file" "AWF config"
}

# --------------------------------------------------------------------------
# Managed package removal (mirrored verbatim from install.sh; parity-guarded)
# --------------------------------------------------------------------------

uv_lists_package() {
    command -v uv >/dev/null 2>&1 || return 1
    # Anchor to the package as a whole token (uv prints the name at the start of
    # the line) so a differently-named fork such as my-agent-workspace-fabric-fork
    # is not matched as a substring and mistaken for an AWF-managed install.
    # grep -w would not be enough here: the hyphens in $PACKAGE are non-word
    # characters, so a word-boundary match would still accept a *-fabric-* name.
    # Do not use `grep -q` here: under `set -o pipefail` it exits on the first
    # match and closes the pipe, so when `uv tool list` prints more tools after
    # the AWF entry the producer takes SIGPIPE and the pipeline exits 141. That
    # non-zero status would make `|| return 1` report a genuinely uv-managed
    # install as unmanaged, so removal would refuse it or no-op rather than remove
    # the managed copy. Redirecting grep's output to /dev/null instead drains the
    # whole stream so the producer finishes writing and exits 0; only grep's own
    # match (0) / no-match (1) status then decides the result.
    uv tool list 2>/dev/null | grep -E "^${PACKAGE}( |$)" >/dev/null || return 1
    return 0
}

pipx_lists_package() {
    command -v pipx >/dev/null 2>&1 || return 1
    # Require the package name as its own token (start of line or preceded by a
    # space, and followed by a space or end of line) so a differently-named fork
    # is not matched as a substring. The leading-space alternative tolerates the
    # "package " prefix that `pipx list` emits; the trailing ( |$) mirrors
    # uv_lists_package so a line ending exactly at the name (no version) still
    # matches a genuinely managed install. As in uv_lists_package, avoid
    # `grep -q`: under pipefail an early-exiting grep SIGPIPEs `pipx list` when the
    # AWF entry is not the last line printed, and the resulting 141 pipeline status
    # would turn a real pipx-managed install into a false "unmanaged". Draining the
    # stream via >/dev/null keeps the producer alive to EOF so only grep's
    # match/no-match status decides.
    pipx list 2>/dev/null | grep -E "(^| )${PACKAGE}( |$)" >/dev/null || return 1
    return 0
}

resolve_awf_path() {
    if command -v awf >/dev/null 2>&1; then
        command -v awf
        return 0
    fi
    if [ -n "$INSTALL_DIR" ] && [ -x "${INSTALL_DIR}/awf" ]; then
        printf '%s' "${INSTALL_DIR}/awf"
        return 0
    fi
    return 1
}

# Uninstall via uv with the same bin dir the matching install used. install_uv
# links the executable into UV_TOOL_BIN_DIR="$INSTALL_DIR"; `uv tool uninstall`
# removes entry points from the bin dir it computes *at uninstall time*, so
# without re-exporting that path uv deletes the tool env but leaves
# ${INSTALL_DIR}/awf orphaned and still exits 0 — a still-runnable awf masked by
# a success exit. With no --install-dir, leave UV_TOOL_BIN_DIR unset so uv uses
# its normal default rather than an empty bin dir.
uninstall_via_uv() {
    if [ -n "$INSTALL_DIR" ]; then
        UV_TOOL_BIN_DIR="$INSTALL_DIR" uv tool uninstall "$PACKAGE"
    else
        uv tool uninstall "$PACKAGE"
    fi
}

# Uninstall via pipx, mirroring install_pipx's PIPX_BIN_DIR for the same reason:
# pipx links console scripts into PIPX_BIN_DIR and, on uninstall, only removes
# scripts from the bin dir it computes now. Without the install-time PIPX_BIN_DIR
# it removes the venv but leaves ${INSTALL_DIR}/awf behind and still exits 0.
# Leave PIPX_BIN_DIR unset when no --install-dir was given.
uninstall_via_pipx() {
    if [ -n "$INSTALL_DIR" ]; then
        PIPX_BIN_DIR="$INSTALL_DIR" pipx uninstall "$PACKAGE"
    else
        pipx uninstall "$PACKAGE"
    fi
}

uninstall_awf() {
    # Probe both managers up front. A package installed by both uv and pipx is
    # reported by each, so acting on only the first-found manager would leave the
    # other copy installed while the run reports success (awf could stay
    # runnable). Discover both, then remove from every manager that owns it.
    local uv_managed=0
    local pipx_managed=0
    local removed=0
    uv_lists_package && uv_managed=1
    pipx_lists_package && pipx_managed=1

    if [ "$DRY_RUN" -eq 1 ]; then
        [ "$uv_managed" -eq 1 ] && plan "uninstall via uv: uv tool uninstall ${PACKAGE}"
        [ "$pipx_managed" -eq 1 ] && plan "uninstall via pipx: pipx uninstall ${PACKAGE}"
        if [ "$uv_managed" -eq 1 ] || [ "$pipx_managed" -eq 1 ]; then
            say "Dry run complete; no changes were made."
            return 0
        fi
        # Neither manager owns it: fall through to the unmanaged refusal / no-op
        # below. That is a policy check, not a mutation, so it still applies
        # under --dry-run.
    else
        # Attempt every manager that owns the package before deciding the exit
        # status. Failing fast on the first manager's uninstall would skip the
        # second, leaving its copy installed — the partial uninstall the
        # dual-manager probe exists to prevent. Remove from each owner that can
        # be removed, then fail if any removal failed so a still-runnable awf is
        # never masked by a success exit.
        local failed_via=""
        if [ "$uv_managed" -eq 1 ]; then
            if uninstall_via_uv; then
                say "Uninstalled ${PACKAGE} via uv."
                removed=1
            else
                failed_via="uv"
            fi
        fi
        if [ "$pipx_managed" -eq 1 ]; then
            if uninstall_via_pipx; then
                say "Uninstalled ${PACKAGE} via pipx."
                removed=1
            else
                failed_via="${failed_via:+$failed_via and }pipx"
            fi
        fi
        if [ -n "$failed_via" ]; then
            fail INSTALL_METHOD_FAILED "uninstall via ${failed_via} failed for $PACKAGE; awf may remain installed"
        fi
        [ "$removed" -eq 1 ] && return 0
    fi

    local awf_path=""
    awf_path="$(resolve_awf_path || true)"
    if [ -n "$awf_path" ]; then
        # Defer the refusal rather than exiting here. remove_uv is authorized
        # independently by the AWF ownership marker, so an unmanaged awf must not
        # strand a bootstrapped uv (the same reason purge_state runs ahead of this
        # lane). main() raises UNINSTALL_REFUSED_UNMANAGED after remove_uv has had
        # its chance to run, so the refusal still surfaces non-zero.
        UNMANAGED_AWF_PATH="$awf_path"
        return 0
    fi

    say "No AWF-managed installation found; nothing to uninstall."
    return 0
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

main() {
    parse_args "$@"

    # The platform guard precedes every mutation path: an unsupported OS/arch
    # aborts with UNSUPPORTED_PLATFORM before touching uv/pipx or the filesystem.
    detect_platform

    # State purge runs first: it is an explicit, authorized lane independent of the
    # package, so the package lane's unmanaged refusal must not block it. The
    # package lane then runs before remove_uv because uv is the very tool it uses:
    # deleting the uv binaries drops uv from PATH, after which `uv tool uninstall`
    # can no longer run and a still-installed, formerly uv-managed awf would be
    # misread as unmanaged (UNINSTALL_REFUSED_UNMANAGED) — uv and state already
    # gone. Removing the package before its manager keeps that lane correct; uv
    # removal comes last.
    purge_state
    uninstall_awf
    remove_uv

    # uninstall_awf defers its unmanaged-awf refusal so the marker-authorized uv
    # lane above still runs; surface it now that remove_uv has had its turn.
    if [ -n "$UNMANAGED_AWF_PATH" ]; then
        fail UNINSTALL_REFUSED_UNMANAGED "refusing to remove unmanaged awf at ${UNMANAGED_AWF_PATH}; not installed by uv or pipx"
    fi

    say "Credentials were preserved. AWF does not remove provider secrets, keyring entries, env refs, or gh/agent auth."
    say "Docker volumes and Compose stacks were left intact; stop them with 'awf service gc' or 'docker compose down' if you no longer need them."
}

main "$@"
