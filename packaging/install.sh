#!/usr/bin/env bash
#
# Agent Workspace Fabric installer.
#
# Resolves the AWF install manifest (awf-install-manifest.json), downloads the
# manifest-pinned wheel, verifies its sha256 BEFORE any install mutation, then
# installs from the locally verified wheel via `uv tool install` (default) or
# `pipx install` (--method pipx). After install it verifies that `awf` is
# runnable and prints correct PATH advice for zsh/bash/fish.
#
# This is a standalone, inspected script: bash 3.2 compatible (macOS default),
# jq-free, and dependency-light. It emits stable shell-level reason tokens on
# stderr (it does not depend on the Python first-run error catalog):
#
#   UNSUPPORTED_PLATFORM MISSING_DEPENDENCY MANIFEST_UNAVAILABLE MANIFEST_INVALID
#   CHANNEL_MISMATCH DOWNLOAD_FAILED CHECKSUM_MISMATCH INSTALL_METHOD_FAILED
#   AWF_NOT_REACHABLE UNINSTALL_REFUSED_UNMANAGED BAD_USAGE
#
# Testability seams (keep fixture tests hermetic, no real release/network):
#   AWF_INSTALL_MANIFEST   path or file:// URL to a local manifest JSON.
#   AWF_INSTALL_BASE_URL   base URL used to resolve a manifest by version/channel.
#   --shell <name>         override shell detection for PATH advice.

set -euo pipefail

PACKAGE="agent-workspace-fabric"
DEFAULT_REPO_URL="https://github.com/dimileeh/aira-agent-workspace-fabric"
MANIFEST_BASENAME="awf-install-manifest.json"

VERSION=""
CHANNEL="stable"
METHOD="uv"
INSTALL_DIR=""
SHELL_OVERRIDE=""
DRY_RUN=0
DO_UNINSTALL=0

WORK_DIR=""

# Resolved during a run.
PLATFORM_OS=""
PLATFORM_ARCH=""
MANIFEST_SOURCE=""
MANIFEST_FILE=""
MANIFEST_CHANNEL=""
ARTIFACT_NAME=""
ARTIFACT_SHA256=""
ARTIFACT_URL=""
ARTIFACT_FILE=""

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
    printf 'awf-install: %s: %s\n' "$token" "$*" >&2
    exit 1
}

bad_usage() {
    printf 'awf-install: %s: %s\n' "BAD_USAGE" "$*" >&2
    exit 2
}

usage() {
    cat <<'EOF'
Agent Workspace Fabric installer

Usage:
  install.sh [options]

Options:
  --version <X.Y.Z>     Install a specific package version (pins manifest/tag).
  --channel <name>      stable | prerelease (default: stable). When --version is
                        omitted, the resolved latest-release manifest must be on
                        this channel; pin --version to install a prerelease.
  --method <name>       uv | pipx (default: uv). pipx is the configured fallback.
  --install-dir <path>  Bin directory used for awf reachability and PATH advice.
  --dry-run             Resolve, download, and verify only; explain the planned
                        actions without installing or writing shell rc files.
  --uninstall           Remove an AWF-managed install only; refuse unknown
                        executables.
  --shell <name>        Override shell detection (zsh|bash|fish) for PATH advice.
  --help                Print this help and exit.

Trust:
  The installer verifies a manifest-pinned sha256 of the downloaded wheel before
  installing. A checksum mismatch, unsupported platform, or unmanaged uninstall
  target aborts before any mutation.
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

require_version() {
    # An empty --version (e.g. `--version=` or an empty wrapper variable) would
    # leave VERSION unset and make resolve_manifest fetch releases/latest — the
    # exact mutable install the caller asked to pin away from. --version is the
    # trust boundary (it selects a specific manifest/tag), so reject an explicit
    # empty value before resolving the manifest instead of silently degrading the
    # pin to latest. Omitting --version entirely is still valid (installs latest).
    if [ -z "$1" ]; then
        bad_usage "empty value for --version; pass --version <X.Y.Z> or omit it to install the latest release"
    fi
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --version)
                require_value "$1" "$#"
                VERSION="$2"
                require_version "$VERSION"
                shift 2
                ;;
            --version=*)
                VERSION="${1#*=}"
                require_version "$VERSION"
                shift
                ;;
            --channel)
                require_value "$1" "$#"
                CHANNEL="$2"
                shift 2
                ;;
            --channel=*)
                CHANNEL="${1#*=}"
                shift
                ;;
            --method)
                require_value "$1" "$#"
                METHOD="$2"
                shift 2
                ;;
            --method=*)
                METHOD="${1#*=}"
                shift
                ;;
            --install-dir)
                require_value "$1" "$#"
                INSTALL_DIR="$2"
                shift 2
                ;;
            --install-dir=*)
                INSTALL_DIR="${1#*=}"
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
            --uninstall)
                DO_UNINSTALL=1
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

    case "$CHANNEL" in
        stable | prerelease) ;;
        *) bad_usage "unsupported channel: $CHANNEL (expected stable|prerelease)" ;;
    esac

    case "$METHOD" in
        uv | pipx) ;;
        *) bad_usage "unsupported method: $METHOD (expected uv|pipx)" ;;
    esac
}

# --------------------------------------------------------------------------
# Platform detection
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

    PLATFORM_OS="$os"
    PLATFORM_ARCH="$arch"
}

# --------------------------------------------------------------------------
# Workspace + fetch helpers
# --------------------------------------------------------------------------

setup_workdir() {
    WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/awf-install.XXXXXX")"
    trap cleanup EXIT
}

cleanup() {
    if [ -n "${WORK_DIR:-}" ] && [ -d "$WORK_DIR" ]; then
        rm -rf "$WORK_DIR"
    fi
}

need_download_tool() {
    if command -v curl >/dev/null 2>&1; then
        return 0
    fi
    if command -v wget >/dev/null 2>&1; then
        return 0
    fi
    fail MISSING_DEPENDENCY "need curl or wget to fetch over the network"
}

# fetch <src> <dest> -> copy/download src to dest. Returns non-zero on failure.
# Supports file:// URLs, local paths, and http(s) URLs.
fetch() {
    local src="$1"
    local dest="$2"
    case "$src" in
        file://*)
            local path="${src#file://}"
            [ -f "$path" ] || return 1
            cp "$path" "$dest" || return 1
            ;;
        http://* | https://*)
            need_download_tool
            if command -v curl >/dev/null 2>&1; then
                curl -fsSL "$src" -o "$dest" || return 1
            else
                wget -q -O "$dest" "$src" || return 1
            fi
            ;;
        *)
            [ -f "$src" ] || return 1
            cp "$src" "$dest" || return 1
            ;;
    esac
    return 0
}

# --------------------------------------------------------------------------
# Manifest resolution + parsing
# --------------------------------------------------------------------------

resolve_manifest() {
    local src
    if [ -n "${AWF_INSTALL_MANIFEST:-}" ]; then
        src="${AWF_INSTALL_MANIFEST}"
    else
        local base="${AWF_INSTALL_BASE_URL:-$DEFAULT_REPO_URL}"
        base="${base%/}"
        if [ -n "$VERSION" ]; then
            src="${base}/releases/download/v${VERSION}/${MANIFEST_BASENAME}"
        else
            src="${base}/releases/latest/download/${MANIFEST_BASENAME}"
        fi
    fi

    MANIFEST_SOURCE="$src"
    MANIFEST_FILE="${WORK_DIR}/${MANIFEST_BASENAME}"
    fetch "$src" "$MANIFEST_FILE" || fail MANIFEST_UNAVAILABLE "could not resolve manifest from $src"
    [ -s "$MANIFEST_FILE" ] || fail MANIFEST_UNAVAILABLE "empty manifest from $src"
}

# Extract the wheel artifact's name, sha256, and url from the T11 manifest.
# Portable, jq-free: relies on the manifest's stable pretty-printed, sorted-key
# shape (kind sorts first within each artifact object, so an artifact's own
# name/sha256/url follow its "kind" line and precede the next object's "kind").
parse_manifest() {
    local fields
    fields="$(
        awk '
            function value(line,   v) {
                v = line
                sub(/^[^:]*:[[:space:]]*"?/, "", v)
                sub(/"?,?[[:space:]]*$/, "", v)
                return v
            }
            /"kind"[[:space:]]*:[[:space:]]*"wheel"/ { in_wheel = 1; next }
            in_wheel && /"kind"[[:space:]]*:/ { in_wheel = 0 }
            in_wheel && /"name"[[:space:]]*:/ && name == "" { name = value($0) }
            in_wheel && /"sha256"[[:space:]]*:/ && sha == "" { sha = value($0) }
            in_wheel && /"url"[[:space:]]*:/ && url == "" { url = value($0) }
            END {
                if (name == "" || sha == "" || url == "") {
                    exit 3
                }
                print name
                print sha
                print url
            }
        ' "$MANIFEST_FILE"
    )" || fail MANIFEST_INVALID "manifest has no resolvable wheel artifact: $MANIFEST_SOURCE"

    ARTIFACT_NAME="$(printf '%s\n' "$fields" | sed -n '1p')"
    ARTIFACT_SHA256="$(printf '%s\n' "$fields" | sed -n '2p')"
    ARTIFACT_URL="$(printf '%s\n' "$fields" | sed -n '3p')"

    if [ -z "$ARTIFACT_NAME" ] || [ -z "$ARTIFACT_SHA256" ] || [ -z "$ARTIFACT_URL" ]; then
        fail MANIFEST_INVALID "manifest wheel artifact is missing required fields: $MANIFEST_SOURCE"
    fi
    if ! printf '%s' "$ARTIFACT_SHA256" | grep -Eq '^[0-9a-fA-F]{64}$'; then
        fail MANIFEST_INVALID "manifest wheel sha256 is not 64 hex characters: $MANIFEST_SOURCE"
    fi
    # The wheel name becomes the download-destination basename
    # (download_artifact builds ${WORK_DIR}/${ARTIFACT_NAME}). A malformed or
    # compromised manifest whose name carries a path traversal (../../...) or an
    # absolute path would make fetch write outside WORK_DIR — before checksum
    # verification and even under --dry-run. Require a plain basename so an
    # untrusted manifest cannot clobber arbitrary user-writable files.
    case "$ARTIFACT_NAME" in
        */* | '.' | '..')
            fail MANIFEST_INVALID "manifest wheel name is not a plain filename ('${ARTIFACT_NAME}'): $MANIFEST_SOURCE"
            ;;
    esac
}

# Extract the manifest's top-level "channel" string (jq-free). The T11 manifest
# is pretty-printed with sorted keys and no artifact carries a "channel" key, so
# the first match is the authoritative release channel.
extract_manifest_channel() {
    sed -n 's/.*"channel"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$MANIFEST_FILE" \
        | head -n 1
}

# When no explicit --version pins the release, the resolved manifest's channel
# must match the requested --channel. Version/tag pinning is the trust boundary
# (RELEASING.md), so a pinned --version selects the manifest directly and the
# channel field is then informational only. A manifest without a channel field
# (legacy/hand-authored) is not enforced here; parse_manifest still guards shape.
verify_channel() {
    [ -z "$VERSION" ] || return 0
    # extract_manifest_channel pipes sed into `head -n 1`; under pipefail, head
    # can close the pipe before sed finishes and leave sed taking SIGPIPE, so the
    # pipeline may exit non-zero. `|| true` degrades that to an empty channel,
    # which the guard below already treats as "no channel field" rather than
    # letting set -e abort the install with no reason token.
    MANIFEST_CHANNEL="$(extract_manifest_channel || true)"
    [ -n "$MANIFEST_CHANNEL" ] || return 0
    if [ "$MANIFEST_CHANNEL" != "$CHANNEL" ]; then
        fail CHANNEL_MISMATCH "requested channel ${CHANNEL} but the resolved manifest is on the ${MANIFEST_CHANNEL} channel (${MANIFEST_SOURCE}); pass --version <X.Y.Z> to install a specific ${CHANNEL} release"
    fi
}

download_artifact() {
    ARTIFACT_FILE="${WORK_DIR}/${ARTIFACT_NAME}"
    fetch "$ARTIFACT_URL" "$ARTIFACT_FILE" || fail DOWNLOAD_FAILED "could not download artifact: $ARTIFACT_URL"
    [ -s "$ARTIFACT_FILE" ] || fail DOWNLOAD_FAILED "downloaded artifact is empty: $ARTIFACT_URL"
}

# --------------------------------------------------------------------------
# Checksum verification
# --------------------------------------------------------------------------

compute_sha256() {
    local file="$1"
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$file" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$file" | awk '{print $1}'
    else
        return 1
    fi
}

verify_checksum() {
    local file="$1"
    local expected="$2"
    local actual
    actual="$(compute_sha256 "$file")" || fail MISSING_DEPENDENCY "need sha256sum or shasum to verify the artifact"

    actual="$(printf '%s' "$actual" | tr '[:upper:]' '[:lower:]')"
    expected="$(printf '%s' "$expected" | tr '[:upper:]' '[:lower:]')"
    if [ "$actual" != "$expected" ]; then
        fail CHECKSUM_MISMATCH "artifact $ARTIFACT_URL sha256 $actual does not match manifest $expected"
    fi
}

# --------------------------------------------------------------------------
# Install
# --------------------------------------------------------------------------

install_artifact() {
    case "$METHOD" in
        uv) install_uv ;;
        pipx) install_pipx ;;
    esac
}

install_uv() {
    command -v uv >/dev/null 2>&1 || fail MISSING_DEPENDENCY "uv is not installed (needed for --method uv)"
    if [ -n "$INSTALL_DIR" ]; then
        UV_TOOL_BIN_DIR="$INSTALL_DIR" uv tool install --force "$ARTIFACT_FILE" \
            || fail INSTALL_METHOD_FAILED "uv tool install failed for $ARTIFACT_NAME"
    else
        uv tool install --force "$ARTIFACT_FILE" \
            || fail INSTALL_METHOD_FAILED "uv tool install failed for $ARTIFACT_NAME"
    fi
}

install_pipx() {
    command -v pipx >/dev/null 2>&1 || fail MISSING_DEPENDENCY "pipx is not installed (needed for --method pipx)"
    if [ -n "$INSTALL_DIR" ]; then
        PIPX_BIN_DIR="$INSTALL_DIR" pipx install --force "$ARTIFACT_FILE" \
            || fail INSTALL_METHOD_FAILED "pipx install failed for $ARTIFACT_NAME"
    else
        pipx install --force "$ARTIFACT_FILE" \
            || fail INSTALL_METHOD_FAILED "pipx install failed for $ARTIFACT_NAME"
    fi
}

# --------------------------------------------------------------------------
# Reachability + PATH advice
# --------------------------------------------------------------------------

default_bin_dir() {
    if [ -n "$INSTALL_DIR" ]; then
        printf '%s' "$INSTALL_DIR"
        return 0
    fi
    # With no --install-dir override, ask the active tool where it actually links
    # executables instead of hard-coding ~/.local/bin, which would mis-report a
    # tool configured to install elsewhere as AWF_NOT_REACHABLE and print PATH
    # advice for the wrong directory.
    #
    # uv derives its bin dir from UV_TOOL_BIN_DIR / XDG_BIN_HOME / XDG_DATA_HOME
    # (defaulting to ~/.local/bin); `uv tool dir --bin` resolves the real path.
    if [ "$METHOD" = "uv" ] && command -v uv >/dev/null 2>&1; then
        local uv_bin
        uv_bin="$(uv tool dir --bin 2>/dev/null || true)"
        if [ -n "$uv_bin" ]; then
            printf '%s' "$uv_bin"
            return 0
        fi
    fi
    # pipx links console scripts into PIPX_BIN_DIR (also defaulting to
    # ~/.local/bin); `pipx environment --value PIPX_BIN_DIR` resolves the real
    # path so a configured PIPX_BIN_DIR is honored rather than silently ignored.
    if [ "$METHOD" = "pipx" ] && command -v pipx >/dev/null 2>&1; then
        local pipx_bin
        pipx_bin="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || true)"
        if [ -n "$pipx_bin" ]; then
            printf '%s' "$pipx_bin"
            return 0
        fi
    fi
    printf '%s' "${HOME}/.local/bin"
}

detect_shell() {
    local candidate="${SHELL_OVERRIDE:-${SHELL:-}}"
    case "$candidate" in
        *fish*) printf '%s' "fish" ;;
        *zsh*) printf '%s' "zsh" ;;
        *bash*) printf '%s' "bash" ;;
        "") printf '%s' "bash" ;;
        *) printf '%s' "${candidate##*/}" ;;
    esac
}

print_path_advice() {
    # Reuse a bindir already resolved by the caller (verify_awf computes it via
    # default_bin_dir) when one is passed; only resolve it here otherwise.
    # Threading the cached value in avoids a second `uv tool dir --bin` /
    # `pipx environment` subprocess per successful install without changing the
    # advice itself.
    local bindir="${1:-}" shell rc line login_rc="" candidate
    [ -n "$bindir" ] || bindir="$(default_bin_dir)"
    shell="$(detect_shell)"
    case "$shell" in
        fish)
            rc="${HOME}/.config/fish/config.fish"
            line="fish_add_path ${bindir}"
            ;;
        zsh)
            rc="${HOME}/.zshrc"
            line="export PATH=\"${bindir}:\$PATH\""
            ;;
        bash)
            rc="${HOME}/.bashrc"
            line="export PATH=\"${bindir}:\$PATH\""
            # Interactive non-login bash reads ~/.bashrc, but login shells (macOS
            # Terminal, SSH) read the first existing login profile and may not
            # source ~/.bashrc. Surface that file too so both shell types pick up
            # the export instead of only non-login shells.
            for candidate in "${HOME}/.bash_profile" "${HOME}/.bash_login" "${HOME}/.profile"; do
                if [ -f "$candidate" ]; then
                    login_rc="$candidate"
                    break
                fi
            done
            ;;
        *)
            rc="${HOME}/.profile"
            line="export PATH=\"${bindir}:\$PATH\""
            ;;
    esac
    warn "awf is installed in ${bindir}, which is not on your PATH."
    warn "Add it by appending this line to ${rc}:"
    warn "    ${line}"
    if [ -n "$login_rc" ]; then
        warn "Your login shell may read ${login_rc} instead of ${rc}; add the same line there too."
    fi
    warn "Then restart your shell or 'source ${rc}'."
}

verify_awf() {
    local resolved="" on_path=0 bindir
    bindir="$(default_bin_dir)"
    if [ -x "${bindir}/awf" ]; then
        # Prefer the binary at the directory we just installed into (INSTALL_DIR
        # when given, else uv's resolved tool bin dir, falling back to
        # ~/.local/bin). Verifying that file directly — instead
        # of whatever `command -v awf` resolves to — avoids falsely passing on an
        # older, unrelated awf earlier on PATH that would shadow the new install
        # and suppress PATH advice for the location we actually wrote to.
        resolved="${bindir}/awf"
        if [ "$(command -v awf 2>/dev/null)" = "$resolved" ]; then
            on_path=1
        fi
    elif [ -n "$INSTALL_DIR" ]; then
        # An explicit --install-dir pins exactly where the binary must land. If
        # it is absent there, the install did not place it; do not fall back to a
        # shadowing awf elsewhere on PATH and report a false success.
        resolved=""
    elif command -v awf >/dev/null 2>&1; then
        # Default install with nothing in ~/.local/bin: uv/pipx may have placed
        # the binary elsewhere on PATH, so trust a reachable awf as a last resort.
        resolved="$(command -v awf)"
        on_path=1
    fi

    if [ -z "$resolved" ]; then
        print_path_advice "$bindir"
        fail AWF_NOT_REACHABLE "awf is not runnable after install; install reported success but no awf binary was found"
    fi

    if ! "$resolved" --help >/dev/null 2>&1; then
        print_path_advice "$bindir"
        fail AWF_NOT_REACHABLE "awf installed at $resolved but is not runnable"
    fi

    if [ "$on_path" -eq 0 ]; then
        print_path_advice "$bindir"
    fi
}

# --------------------------------------------------------------------------
# Uninstall (AWF-managed only)
# --------------------------------------------------------------------------

uv_lists_package() {
    command -v uv >/dev/null 2>&1 || return 1
    # Anchor to the package as a whole token (uv prints the name at the start of
    # the line) so a differently-named fork such as my-agent-workspace-fabric-fork
    # is not matched as a substring and mistaken for an AWF-managed install.
    # grep -w would not be enough here: the hyphens in $PACKAGE are non-word
    # characters, so a word-boundary match would still accept a *-fabric-* name.
    uv tool list 2>/dev/null | grep -qE "^${PACKAGE}( |$)" || return 1
    return 0
}

pipx_lists_package() {
    command -v pipx >/dev/null 2>&1 || return 1
    # Require the package name as its own token (start of line or preceded by a
    # space, and followed by a space or end of line) so a differently-named fork
    # is not matched as a substring. The leading-space alternative tolerates the
    # "package " prefix that `pipx list` emits; the trailing ( |$) mirrors
    # uv_lists_package so a line ending exactly at the name (no version) still
    # matches a genuinely managed install.
    pipx list 2>/dev/null | grep -qE "(^| )${PACKAGE}( |$)" || return 1
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
            if uv tool uninstall "$PACKAGE"; then
                say "Uninstalled ${PACKAGE} via uv."
                removed=1
            else
                failed_via="uv"
            fi
        fi
        if [ "$pipx_managed" -eq 1 ]; then
            if pipx uninstall "$PACKAGE"; then
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
        fail UNINSTALL_REFUSED_UNMANAGED "refusing to remove unmanaged awf at ${awf_path}; not installed by uv or pipx"
    fi

    say "No AWF-managed installation found; nothing to uninstall."
    return 0
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

run_install() {
    setup_workdir

    plan "resolve install manifest"
    resolve_manifest
    parse_manifest
    verify_channel
    say "Resolved manifest: ${MANIFEST_SOURCE}"

    plan "download artifact: ${ARTIFACT_URL}"
    download_artifact

    plan "verify sha256: ${ARTIFACT_SHA256}"
    verify_checksum "$ARTIFACT_FILE" "$ARTIFACT_SHA256"
    say "Checksum verified for ${ARTIFACT_NAME}."

    if [ "$DRY_RUN" -eq 1 ]; then
        # uv and pipx diverge: the real commands are `uv tool install` and
        # `pipx install` (pipx has no `tool` subcommand), so the plan must not
        # template a single `${METHOD} tool install` string for both.
        if [ "$METHOD" = "uv" ]; then
            plan "install via ${METHOD}: uv tool install ${ARTIFACT_NAME}"
        else
            plan "install via ${METHOD}: pipx install ${ARTIFACT_NAME}"
        fi
        plan "verify awf reachability"
        say "Dry run complete; no changes were made."
        return 0
    fi

    plan "install via ${METHOD}: ${ARTIFACT_NAME}"
    install_artifact

    plan "verify awf reachability"
    verify_awf

    say "Installed ${PACKAGE}. Run 'awf --help' to get started."
}

main() {
    parse_args "$@"

    # The platform guard precedes every mutation path: an unsupported OS/arch
    # aborts with UNSUPPORTED_PLATFORM before install or uninstall touches uv/pipx.
    detect_platform

    if [ "$DO_UNINSTALL" -eq 1 ]; then
        uninstall_awf
        return 0
    fi

    run_install
}

main "$@"
