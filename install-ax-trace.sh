#!/bin/bash
# Installer for ax-trace — the Arize Coding Harness Tracing CLI.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install-ax-trace.sh | bash
#
# Environment variables:
#   AX_TRACE_VERSION   Pin to a specific version (e.g. v0.1.0). Default: latest.
#   AX_TRACE_INSTALL_DIR   Install directory. Default: ~/.local/bin.

set -euo pipefail

REPO="Arize-ai/coding-harness-tracing"
INSTALL_DIR="${AX_TRACE_INSTALL_DIR:-$HOME/.local/bin}"
VERSION="${AX_TRACE_VERSION:-}"

# -- detect OS + arch --------------------------------------------------------
detect_platform() {
    local os arch
    os="$(uname -s | tr '[:upper:]' '[:lower:]')"
    arch="$(uname -m)"
    case "$arch" in
        x86_64|amd64) arch="amd64" ;;
        arm64|aarch64) arch="arm64" ;;
        *) echo "error: unsupported architecture: $arch" >&2; exit 1 ;;
    esac
    case "$os" in
        linux|darwin) ;;
        *) echo "error: unsupported OS: $os" >&2; exit 1 ;;
    esac
    echo "${os}_${arch}"
}

# -- resolve version (latest if unset) --------------------------------------
resolve_version() {
    if [[ -n "$VERSION" ]]; then echo "$VERSION"; return; fi
    # Find the latest tag matching ax-trace-v*
    local api="https://api.github.com/repos/$REPO/releases"
    local tag
    tag="$(curl -sSL "$api" | grep -oE '"tag_name": "ax-trace-v[^"]+"' | head -1 | sed 's/.*"ax-trace-v\(.*\)"/v\1/')"
    if [[ -z "$tag" ]]; then
        echo "error: could not resolve latest ax-trace version" >&2
        exit 1
    fi
    echo "$tag"
}

# -- download + verify ------------------------------------------------------
download_release() {
    local version="$1" platform="$2"
    local tmpdir; tmpdir="$(mktemp -d)"
    local base="https://github.com/$REPO/releases/download/ax-trace-${version}"
    local archive="ax-trace_${version#v}_${platform}.tar.gz"
    local checksums="checksums.txt"

    curl -sSLf "$base/$archive" -o "$tmpdir/$archive"
    curl -sSLf "$base/$checksums" -o "$tmpdir/$checksums"

    # Verify SHA256
    (cd "$tmpdir" && grep "$archive" "$checksums" | sha256sum -c -) || {
        echo "error: SHA256 verification failed for $archive" >&2
        exit 1
    }

    tar -xzf "$tmpdir/$archive" -C "$tmpdir"
    echo "$tmpdir/ax-trace"
}

# -- install + PATH check ---------------------------------------------------
install_binary() {
    local src="$1"
    mkdir -p "$INSTALL_DIR"
    cp "$src" "$INSTALL_DIR/ax-trace"
    chmod +x "$INSTALL_DIR/ax-trace"
    echo "[ax-trace] Installed to $INSTALL_DIR/ax-trace"

    case ":$PATH:" in
        *":$INSTALL_DIR:"*) ;;
        *)
            echo ""
            echo "[ax-trace] $INSTALL_DIR is not on your PATH. Add this line to your shell rc:"
            echo "  export PATH=\"$INSTALL_DIR:\$PATH\""
            echo ""
            ;;
    esac
}

main() {
    local platform version binary
    platform="$(detect_platform)"
    version="$(resolve_version)"
    echo "[ax-trace] Installing ax-trace $version for $platform"
    binary="$(download_release "$version" "$platform")"
    install_binary "$binary"
    echo ""
    echo "[ax-trace] Run 'ax-trace claude' to get started"
}

main "$@"
