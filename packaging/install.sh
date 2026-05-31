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
#   CHANNEL_MISMATCH VERSION_MISMATCH PACKAGE_MISMATCH INSECURE_URL DOWNLOAD_FAILED
#   CHECKSUM_MISMATCH INSTALL_METHOD_FAILED AWF_NOT_REACHABLE UNINSTALL_REFUSED_UNMANAGED
#   BAD_USAGE
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
    local version="$1"
    # Normalize a tag-style pin. Users and CI commonly pin the git tag form
    # vX.Y.Z (e.g. --version v0.1.0), but VERSION is interpolated as the *bare*
    # release version everywhere downstream: resolve_manifest builds the asset URL
    # as .../releases/download/v${VERSION}/..., and verify_version compares the
    # manifest's bare "version" field to VERSION and its source.tag to v${VERSION}.
    # A leading v left in place double-prepends (vv0.1.0), breaking the manifest
    # fetch or tripping VERSION_MISMATCH for a correct release. PEP 440 versions
    # always start with a digit, so a single leading v/V is unambiguously the tag
    # prefix; strip it so the stored token is the canonical bare version.
    case "$version" in
        [vV]*) version="${version#[vV]}" ;;
    esac
    # VERSION is interpolated directly into the manifest URL path segment
    # (resolve_manifest builds .../releases/download/v${VERSION}/...). A value
    # carrying a slash or a path-traversal sequence (e.g. ../../../evil) would
    # escape that segment; against a file:// base (the AWF_INSTALL_BASE_URL test
    # seam) it could redirect the manifest fetch to an arbitrary local path before
    # the sha256 gate runs. Constrain the normalized --version to a plain version
    # token (must start alphanumeric; only letters, digits, '.', '-', '_', '+'
    # thereafter) so the fetch stays confined to the expected v<VERSION> segment.
    # The empty alternative also rejects a bare "v"/"V" that normalizes to nothing.
    # This still admits PEP 440 release tags such as 1.2.3 or 1.2.3rc1.
    case "$version" in
        "" | [!A-Za-z0-9]* | *[!A-Za-z0-9._+-]*)
            bad_usage "invalid --version '$1'; expected a version like X.Y.Z (letters, digits, '.', '-', '_', '+' only)"
            ;;
    esac
    VERSION="$version"
}

require_install_dir() {
    # --install-dir scopes three things: where uv/pipx link the executable
    # (UV_TOOL_BIN_DIR/PIPX_BIN_DIR), how `awf` reachability is verified, and which
    # bin dir uninstall re-exports so neither manager orphans the link. Every
    # downstream use gates on [ -n "$INSTALL_DIR" ], so an empty value (e.g.
    # `--install-dir=` or an empty wrapper variable) is indistinguishable from
    # omitting the flag and silently mutates uv/pipx's default bin dir instead of
    # the caller's intended isolated location. Reject an explicit empty value at
    # the boundary; omitting --install-dir entirely is still valid (uses default).
    # Unlike --version this imposes no charset constraint: a bin path legitimately
    # contains slashes, so only emptiness is rejected here.
    if [ -z "$1" ]; then
        bad_usage "empty value for --install-dir; pass --install-dir <path> or omit it to use the default bin directory"
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

    # Install-only pins are meaningless during --uninstall: uninstall_awf removes
    # whatever uv/pipx currently manage regardless of version, and ignores the
    # channel entirely. Silently accepting them would let `install.sh --version
    # 0.2.0 --uninstall` look version-gated while it performs a standard managed
    # removal. Reject the combination at parse time so the no-op flag is obvious at
    # the boundary instead of after the fact. --method/--install-dir are NOT
    # rejected: uninstall probes both managers by presence (not by --method, so a
    # routing-follows-discovery removal stays valid) and re-exports --install-dir
    # as the install-time bin dir so neither manager orphans the executable.
    if [ "$DO_UNINSTALL" -eq 1 ]; then
        if [ -n "$VERSION" ]; then
            bad_usage "--version is not valid with --uninstall; uninstall removes whatever uv/pipx currently manage regardless of version"
        fi
        # CHANNEL defaults to stable, so an explicit --channel stable is
        # indistinguishable from the default and harmless; only a non-default
        # channel signals a meaningful (but ignored) pin worth rejecting.
        if [ "$CHANNEL" != "stable" ]; then
            bad_usage "--channel is not valid with --uninstall; uninstall is channel-independent"
        fi
    fi
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
# Supports file:// URLs, local paths, and https:// URLs. Plain http:// is
# refused (INSECURE_URL): the sha256 gate proves the wheel matches the manifest
# but cannot detect a *substituted* manifest, so a manifest fetched over plain
# HTTP — or one whose artifact url is http:// — lets a network-adjacent attacker
# swap both the manifest and a matching-sha256 malicious wheel before the gate
# runs. file:// stays allowed as the hermetic test seam; https:// and local
# paths are the production sources. https:// downloads additionally pin every
# redirect hop to https (curl --proto-redir, wget --https-only) so a redirect
# cannot quietly downgrade the transfer to plain http past the scheme guard.
fetch() {
    local src="$1"
    local dest="$2"
    case "$src" in
        file://*)
            local path="${src#file://}"
            [ -f "$path" ] || return 1
            cp "$path" "$dest" || return 1
            ;;
        http://*)
            fail INSECURE_URL "refusing to fetch over plain HTTP ($src): the sha256 gate cannot detect a substituted manifest, so an http:// source breaks the install trust chain; use https:// (or a file:// path for local testing)"
            ;;
        https://*)
            need_download_tool
            if command -v curl >/dev/null 2>&1; then
                # -L follows redirects; --proto/--proto-redir pin the initial
                # request and every redirect hop to https, so a 30x bounce to
                # plain http:// cannot smuggle manifest/wheel bytes past the
                # INSECURE_URL guard above (which only sees the initial scheme).
                # Legitimate https:// -> https:// CDN redirects still follow.
                curl -fsSL --proto '=https' --proto-redir '=https' "$src" -o "$dest" || return 1
            else
                # wget follows redirects by default; --https-only refuses any
                # redirect hop that downgrades to plain http:// for the same
                # reason (it fails closed if the redirect target is not https).
                wget -q --https-only -O "$dest" "$src" || return 1
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
# Field matches are anchored to the wheel object's own indentation so nested
# objects inside the artifact — its "platform" block and, once release signing
# populates it, "signatures" array entries (which sort before "url" and may
# carry their own "kind"/"name"/"url" keys) — neither end the wheel block early
# nor get mistaken for the wheel's own fields.
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
            function indent(line) {
                match(line, /^ */)
                return RLENGTH
            }
            /"kind"[[:space:]]*:[[:space:]]*"wheel"/ {
                if (!in_wheel) { in_wheel = 1; wheel_indent = indent($0) }
                next
            }
            in_wheel && indent($0) == wheel_indent && /"kind"[[:space:]]*:/ { in_wheel = 0 }
            in_wheel && indent($0) == wheel_indent && /"name"[[:space:]]*:/ && name == "" { name = value($0) }
            in_wheel && indent($0) == wheel_indent && /"sha256"[[:space:]]*:/ && sha == "" { sha = value($0) }
            in_wheel && indent($0) == wheel_indent && /"url"[[:space:]]*:/ && url == "" { url = value($0) }
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

# Extract the manifest's top-level "version" string (jq-free). Like
# extract_manifest_channel, this relies on the T11 manifest's stable
# pretty-printed, sorted-key shape: "version" appears once at the top level and
# on no artifact object. The leading-quote anchor ("version") never matches the
# unrelated "schema_version" key (which is preceded by an underscore and carries
# an unquoted integer value), so the first match is the authoritative version.
extract_manifest_version() {
    sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$MANIFEST_FILE" \
        | head -n 1
}

# Extract the manifest's source.tag string (jq-free). "tag" is a quoted key only
# inside the top-level "source" object (no artifact carries a "tag" key), so the
# first match is the release tag the manifest attributes itself to.
extract_manifest_tag() {
    sed -n 's/.*"tag"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$MANIFEST_FILE" \
        | head -n 1
}

# Extract the manifest's top-level "package" string (jq-free). Like the other
# extract_manifest_* helpers, this relies on the T11 manifest's stable
# pretty-printed, sorted-key shape: "package" appears once at the top level and on
# no artifact object (the leading-quote anchor never matches a longer key that
# merely ends in "package"), so the first match is the package the manifest
# attributes itself to.
extract_manifest_package() {
    sed -n 's/.*"package"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$MANIFEST_FILE" \
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

# When --version pins a release, the resolved manifest must actually describe
# that release. resolve_manifest fetches the manifest from a tag-pinned URL
# (releases/download/v${VERSION}/...), but that only pins the URL path, not the
# served bytes: a release asset or mirror (AWF_INSTALL_BASE_URL) that serves a
# manifest for a *different* tag would otherwise be installed silently, because
# that manifest's wheel name/url/sha256 are internally consistent — the checksum
# gate only proves the wheel matches the manifest, not that the manifest matches
# the pin. Version/tag pinning is the documented trust boundary (RELEASING.md),
# so cross-check the manifest's own version and source.tag against --version
# before downloading and refuse a mismatch, so a misattached or stale manifest
# cannot turn a pinned install into a different release. A manifest that omits
# these fields (legacy/hand-authored) is not enforced here; parse_manifest still
# guards the wheel artifact's shape.
verify_version() {
    [ -n "$VERSION" ] || return 0
    # extract_manifest_version/_tag pipe sed into `head -n 1`; as in
    # verify_channel, head can close the pipe before sed finishes and leave sed
    # taking SIGPIPE, so under pipefail the pipeline may exit non-zero. `|| true`
    # degrades that to an empty value, which the guards below treat as "field
    # absent" rather than letting set -e abort the install with no reason token.
    local manifest_version manifest_tag
    manifest_version="$(extract_manifest_version || true)"
    manifest_tag="$(extract_manifest_tag || true)"
    if [ -n "$manifest_version" ] && [ "$manifest_version" != "$VERSION" ]; then
        fail VERSION_MISMATCH "requested version ${VERSION} but the resolved manifest declares version ${manifest_version} (${MANIFEST_SOURCE}); the pinned release asset or mirror is serving a manifest for a different release"
    fi
    if [ -n "$manifest_tag" ] && [ "$manifest_tag" != "v${VERSION}" ]; then
        fail VERSION_MISMATCH "requested version ${VERSION} but the resolved manifest is tagged ${manifest_tag} (${MANIFEST_SOURCE}); the pinned release asset or mirror is serving a manifest for a different release"
    fi
}

# The resolved manifest must describe the package this installer installs. Unlike
# the version/channel checks this is enforced for every install (pinned or not):
# a release asset or mirror (AWF_INSTALL_BASE_URL) that serves a manifest for a
# *different* package which happens to share this version/tag would otherwise be
# installed silently, because that manifest's wheel name/url/sha256 are internally
# consistent — the checksum gate only proves the wheel matches the manifest, not
# that the manifest is for ${PACKAGE}. Such a misattached manifest would mutate
# the user's environment with the wrong tool via uv/pipx. The checked-in generator
# emits a top-level "package" field (scripts/generate_install_manifest.py), so
# cross-check it against PACKAGE before downloading and refuse a mismatch. A
# manifest that omits the field (legacy/hand-authored) is not enforced here,
# mirroring how verify_channel/verify_version tolerate a missing field.
verify_package() {
    # extract_manifest_package pipes sed into `head -n 1`; as in verify_channel,
    # head can close the pipe before sed finishes and leave sed taking SIGPIPE, so
    # under pipefail the pipeline may exit non-zero. `|| true` degrades that to an
    # empty value, which the guard below treats as "field absent" rather than
    # letting set -e abort the install with no reason token.
    local manifest_package
    manifest_package="$(extract_manifest_package || true)"
    [ -n "$manifest_package" ] || return 0
    if [ "$manifest_package" != "$PACKAGE" ]; then
        fail PACKAGE_MISMATCH "resolved manifest is for package ${manifest_package} but this installer installs ${PACKAGE} (${MANIFEST_SOURCE}); the release asset or mirror is serving a manifest for a different package"
    fi
}

# Normalize a distribution name with PEP 503 / wheel-filename (PEP 427) rules:
# lowercase, then collapse any run of '-', '_' and '.' to a single '-'. This
# mirrors scripts/generate_install_manifest.py's _normalize_distribution_package
# (re.sub(r"[-_.]+", "-", name).lower()) so the installer compares the wheel's
# escaped distribution component against PACKAGE on the same footing the generator
# used to validate it. A jq-free, BRE sed expression keeps this portable to the
# bash 3.2 / BSD userland the installer targets (no `sed -E`).
normalize_dist_name() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed 's/[-_.][-_.]*/-/g'
}

# The manifest's *selected wheel artifact* must itself be a wheel for ${PACKAGE} at
# the version the manifest declares. parse_manifest only proves the wheel name is a
# safe plain basename, and verify_package/verify_version only check the manifest's
# top-level fields, so a misattached or hand-crafted manifest could pair a top-level
# package=${PACKAGE} / version=X with a wheel artifact for a *different* distribution
# or version (e.g. other_tool-0.1.0-py3-none-any.whl, or
# agent_workspace_fabric-9.9.9-...). The checksum gate only proves the downloaded
# bytes match that artifact's own sha256 -- not that the artifact is a wheel for
# ${PACKAGE} -- so without this check the installer would download that wheel and
# hand it to uv/pipx, mutating the environment with the wrong package before
# reachability fails (or even reporting success if it exposes an `awf` command).
# Cross-check the wheel filename's own distribution/version before downloading.
# Wheel filenames are {distribution}-{version}-{python}-{abi}-{platform}.whl with
# the distribution and version escaped (PEP 427/503), so normalize both sides the
# way the generator's _validate_distribution_metadata does before comparing. When the
# manifest omits its top-level version (legacy/hand-authored), the wheel filename is
# still cross-checked against a pinned --version so an incomplete manifest cannot
# smuggle a different release past the pin; only an unpinned install with no declared
# version is left unenforced (nothing to compare against).
verify_artifact_name() {
    case "$ARTIFACT_NAME" in
        *.whl) ;;
        *)
            fail MANIFEST_INVALID "manifest wheel artifact name is not a .whl file ('${ARTIFACT_NAME}'): $MANIFEST_SOURCE"
            ;;
    esac

    local stem dist after_dist wheel_version
    stem="${ARTIFACT_NAME%.whl}"
    dist="${stem%%-*}"
    after_dist="${stem#*-}"
    if [ "$after_dist" = "$stem" ]; then
        # No '-' after the distribution component: not a {dist}-{version}-... wheel,
        # so its version cannot be identified. Refuse rather than guess.
        fail MANIFEST_INVALID "manifest wheel artifact name is not a {dist}-{version} wheel ('${ARTIFACT_NAME}'): $MANIFEST_SOURCE"
    fi
    wheel_version="${after_dist%%-*}"

    local norm_dist norm_package
    norm_dist="$(normalize_dist_name "$dist")"
    norm_package="$(normalize_dist_name "$PACKAGE")"
    if [ "$norm_dist" != "$norm_package" ]; then
        fail PACKAGE_MISMATCH "manifest wheel artifact ${ARTIFACT_NAME} is for distribution '${dist}', not ${PACKAGE} (${MANIFEST_SOURCE}); the release asset or mirror is serving a manifest whose wheel is for a different package"
    fi

    # extract_manifest_version pipes sed into `head -n 1`; as in verify_version, head
    # can close the pipe before sed finishes and leave sed taking SIGPIPE, so under
    # pipefail the pipeline may exit non-zero. `|| true` degrades that to an empty
    # value, which the guard below treats as "field absent" rather than letting
    # set -e abort the install with no reason token.
    local manifest_version
    manifest_version="$(extract_manifest_version || true)"

    # Choose the version the wheel must match. The manifest's declared top-level
    # version is authoritative when present (verify_version already proved it equals
    # any --version pin). When it is absent, verify_version could not enforce the pin
    # either, so the wheel filename is the only remaining evidence of which release
    # this is: if the caller pinned --version, compare the wheel version against that
    # pin (the documented trust boundary, RELEASING.md) so an incomplete manifest
    # omitting both top-level version and source.tag cannot install a different
    # release (e.g. agent_workspace_fabric-0.2.0-...whl under --version 0.1.0). With
    # neither a declared manifest version nor a pin there is nothing to compare
    # against, so accept the wheel version as the version of record.
    local expected_version expected_source
    if [ -n "$manifest_version" ]; then
        expected_version="$manifest_version"
        expected_source="the manifest declares version ${manifest_version}"
    elif [ -n "$VERSION" ]; then
        expected_version="$VERSION"
        expected_source="the pinned --version is ${VERSION}"
    else
        return 0
    fi
    local lc_wheel_version lc_expected_version
    lc_wheel_version="$(printf '%s' "$wheel_version" | tr '[:upper:]' '[:lower:]')"
    lc_expected_version="$(printf '%s' "$expected_version" | tr '[:upper:]' '[:lower:]')"
    if [ "$lc_wheel_version" != "$lc_expected_version" ]; then
        fail VERSION_MISMATCH "manifest wheel artifact ${ARTIFACT_NAME} is for version ${wheel_version} but ${expected_source} (${MANIFEST_SOURCE}); the release asset or mirror is serving a manifest whose wheel is for a different release"
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

# A human-readable summary of the install step — NOT a copy-pasteable command.
# It names the chosen method (`uv tool install` / `pipx install`) and the wheel
# by its manifest filename, rendered identically in the dry-run plan and the
# real-install plan so a dry-run preview and a live run log show the same
# "install via <method>: <command>" line. uv and pipx diverge: the real commands
# are `uv tool install` and `pipx install` (pipx has no `tool` subcommand), so
# the plan must not template a single `${METHOD} tool install` string for both.
#
# This summary intentionally differs from what install_uv()/install_pipx()
# actually exec: those pass `--force` and the absolute "$ARTIFACT_FILE" path
# (plus a UV_TOOL_BIN_DIR/PIPX_BIN_DIR prefix when --install-dir is set). Both
# are omitted on purpose. "$ARTIFACT_FILE" lives under the per-run mktemp work
# dir that the EXIT trap deletes, so echoing it would be neither reproducible by
# the reader nor identical across runs — using the stable wheel name keeps the
# dry-run and real-install plan lines byte-for-byte equal (see
# test_real_install_plan_matches_dry_run_command). `--force` is an operational
# reinstall flag, not part of identifying what is being installed.
install_command() {
    case "$METHOD" in
        uv) printf 'uv tool install %s' "$ARTIFACT_NAME" ;;
        pipx) printf 'pipx install %s' "$ARTIFACT_NAME" ;;
    esac
}

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
    # An optional second argument overrides the opening line. verify_awf passes a
    # context-aware message in the "binary not found" branch, where the default
    # "awf is installed in ${bindir}" wording would be factually wrong (nothing
    # was placed there) and contradict the AWF_NOT_REACHABLE failure that follows.
    local bindir="${1:-}" opening="${2:-}" shell rc line login_rc="" candidate
    [ -n "$bindir" ] || bindir="$(default_bin_dir)"
    # Strip a trailing slash so the suggested `export PATH=` line never embeds a
    # double slash (e.g. ".../bin/:$PATH") when --install-dir or a uv/pipx bin
    # dir carries one; keeps the advice copy-pasteable and matches verify_awf.
    bindir="${bindir%/}"
    shell="$(detect_shell)"
    case "$shell" in
        fish)
            rc="${HOME}/.config/fish/config.fish"
            # Quote the dir: fish_add_path word-splits its arguments, so an
            # unquoted bindir containing whitespace (e.g. ".../Application
            # Support/awf/bin") would be pasted as several paths and never make
            # awf reachable. Match the quoting the zsh/bash lines already use.
            line="fish_add_path \"${bindir}\""
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
    if [ -n "$opening" ]; then
        warn "$opening"
    else
        warn "awf is installed in ${bindir}, which is not on your PATH."
    fi
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
    # Normalise away any trailing slash before composing "${bindir}/awf": a value
    # like ".../bin/" would otherwise build ".../bin//awf", which `command -v awf`
    # never reports (it returns the canonical single-slash form), so the on_path
    # string compare below would spuriously fail and print PATH advice even when
    # awf is already reachable.
    bindir="${bindir%/}"
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
        # No awf was found at ${bindir}/awf, at --install-dir, or on PATH. Do not
        # claim it "is installed in ${bindir}" — it is not. Give an honest,
        # conditional opening but still emit the exact PATH-export line so a user
        # whose uv/pipx did drop the binary into ${bindir} can recover.
        print_path_advice "$bindir" "awf was not found after install; if uv/pipx placed it in ${bindir}, add that directory to your PATH."
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
    # Do not use `grep -q` here: under `set -o pipefail` it exits on the first
    # match and closes the pipe, so when `uv tool list` prints more tools after
    # the AWF entry the producer takes SIGPIPE and the pipeline exits 141. That
    # non-zero status would make `|| return 1` report a genuinely uv-managed
    # install as unmanaged, so --uninstall would refuse it or no-op rather than
    # remove the managed copy. Redirecting grep's output to /dev/null instead
    # drains the whole stream so the producer finishes writing and exits 0; only
    # grep's own match (0) / no-match (1) status then decides the result.
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
    # matches a genuinely managed install.
    # As in uv_lists_package, avoid `grep -q`: under pipefail an early-exiting
    # grep SIGPIPEs `pipx list` when the AWF entry is not the last line printed,
    # and the resulting 141 pipeline status would turn a real pipx-managed install
    # into a false "unmanaged". Draining the stream via >/dev/null keeps the
    # producer alive to EOF so only grep's match/no-match status decides.
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
# scripts from the bin dir it computes now. Without the install-time
# PIPX_BIN_DIR it removes the venv but leaves ${INSTALL_DIR}/awf behind and still
# exits 0. Leave PIPX_BIN_DIR unset when no --install-dir was given.
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
    verify_version
    verify_package
    verify_artifact_name
    say "Resolved manifest: ${MANIFEST_SOURCE}"

    plan "download artifact: ${ARTIFACT_URL}"
    download_artifact

    plan "verify sha256: ${ARTIFACT_SHA256}"
    verify_checksum "$ARTIFACT_FILE" "$ARTIFACT_SHA256"
    say "Checksum verified for ${ARTIFACT_NAME}."

    if [ "$DRY_RUN" -eq 1 ]; then
        plan "install via ${METHOD}: $(install_command)"
        plan "verify awf reachability"
        say "Dry run complete; no changes were made."
        return 0
    fi

    plan "install via ${METHOD}: $(install_command)"
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
