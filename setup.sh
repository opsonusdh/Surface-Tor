#!/bin/sh

# Terminate execution immediately if any command returns a non-zero exit code
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# Pinned third-party versions: what this project was tested with.
#
# Bump these deliberately (and re-verify) rather than tracking "latest". A new
# release can regress or be withdrawn, and a download of "whatever is newest"
# can't be checked against anything.
# ---------------------------------------------------------------------------
REPLICA_COMMIT="f21df1031b686c467533e48806f60c8ce1dce762"

PINNED_CLOUDFLARED_VERSION="2026.9.3"
# SHA-256 of the official cloudflared-linux-* release assets for the version
# above, as published in Cloudflare's own release notes.
SHA256_LINUX_AMD64="77e26d8d900e0b8469f416239d14b5f296525fdf79fee6f511ef55609e3fbac2"
SHA256_LINUX_ARM64="aaeb2d7d0da3614634c7e03ab13487a1522c2e79165ed2929cfe23d5e95b326d"

# Override with e.g.  CLOUDFLARED_VERSION=2026.10.0 ./setup.sh  or  =latest.
# Anything other than the pinned version is downloaded WITHOUT verification.
CLOUDFLARED_VERSION="${CLOUDFLARED_VERSION:-$PINNED_CLOUDFLARED_VERSION}"


# ---------------------------------------------------------------------------
# Replica (the reverse-proxy engine each chain node runs)
# ---------------------------------------------------------------------------
if [ ! -d "Replica" ]; then
    echo "[*] Cloning Replica, pinned to commit $(printf '%s' "$REPLICA_COMMIT" | cut -c1-7)..."
    git clone https://github.com/sarperavci/Replica.git
    git -C Replica checkout --quiet "$REPLICA_COMMIT"
else
    echo "[*] Replica directory already exists. Leaving your checkout as it is."
    CURRENT_COMMIT="$(git -C Replica rev-parse HEAD 2>/dev/null || echo unknown)"
    if [ "$CURRENT_COMMIT" != "$REPLICA_COMMIT" ]; then
        echo "[NOTE] Replica is at $CURRENT_COMMIT; this project was tested with $REPLICA_COMMIT."
    fi
fi

echo "[*] Applying local patch (adds -u/--user-agent support to Replica)..."
PY_BIN="$(command -v python3 || command -v python || true)"
if [ -n "$PY_BIN" ]; then
    "$PY_BIN" "$SCRIPT_DIR/scripts/patch_replica.py" || \
        echo "[WARNING] Could not apply the Replica patch; -u/--user-agent will refuse to run until scripts/patch_replica.py succeeds."
else
    echo "[WARNING] No python found to apply scripts/patch_replica.py; -u/--user-agent will refuse to run until it is applied."
fi


# ---------------------------------------------------------------------------
# cloudflared
# ---------------------------------------------------------------------------
ARCH_RAW="$(uname -m)"
ARCH=""

case "$ARCH_RAW" in
    x86_64|amd64)
        ARCH="amd64"
        ;;
    i386|i686|x86)
        ARCH="386"
        ;;
    arm64|aarch64)
        ARCH="arm64"
        ;;
    arm*)
        ARCH="arm"
        ;;
    *)
        echo "[ERROR] Unsupported Linux architecture detected: $ARCH_RAW"
        exit 1
        ;;
esac

echo "[*] Target Environment Identified: Linux | Architecture=$ARCH"

if ! command -v curl >/dev/null 2>&1; then
    echo "[ERROR] curl is required to download cloudflared. Install it first (Termux: pkg install curl)."
    exit 1
fi

file_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d' ' -f1
    else
        echo ""
    fi
}

EXPECTED_SHA=""
if [ "$CLOUDFLARED_VERSION" = "$PINNED_CLOUDFLARED_VERSION" ]; then
    case "$ARCH" in
        amd64) EXPECTED_SHA="$SHA256_LINUX_AMD64" ;;
        arm64) EXPECTED_SHA="$SHA256_LINUX_ARM64" ;;
    esac
fi

if [ "$CLOUDFLARED_VERSION" = "latest" ]; then
    DOWNLOAD_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH"
else
    DOWNLOAD_URL="https://github.com/cloudflare/cloudflared/releases/download/$CLOUDFLARED_VERSION/cloudflared-linux-$ARCH"
fi
BINARY_NAME="cloudflared"

if [ -n "$EXPECTED_SHA" ] && [ -x "$BINARY_NAME" ] && [ "$(file_sha256 "$BINARY_NAME")" = "$EXPECTED_SHA" ]; then
    echo "[*] cloudflared $CLOUDFLARED_VERSION is already present and its checksum matches. Skipping download."
else
    echo "[*] Downloading cloudflared $CLOUDFLARED_VERSION from the official Cloudflare releases..."
    rm -f "$BINARY_NAME.download"
    # -f: fail on HTTP errors instead of saving an error page as the "binary".
    curl -fL --retry 3 -# -o "$BINARY_NAME.download" "$DOWNLOAD_URL"

    if [ -n "$EXPECTED_SHA" ]; then
        ACTUAL_SHA="$(file_sha256 "$BINARY_NAME.download")"
        if [ -z "$ACTUAL_SHA" ]; then
            echo "[WARNING] No sha256sum/shasum tool found; skipping checksum verification."
        elif [ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]; then
            rm -f "$BINARY_NAME.download"
            echo "[ERROR] Checksum mismatch for the downloaded cloudflared."
            echo "        expected: $EXPECTED_SHA"
            echo "        actual:   $ACTUAL_SHA"
            echo "        Refusing to install it."
            exit 1
        else
            echo "[*] Checksum verified against Cloudflare's published SHA-256."
        fi
    else
        echo "[WARNING] No pinned checksum for cloudflared $CLOUDFLARED_VERSION on $ARCH; the download was NOT verified."
    fi

    mv "$BINARY_NAME.download" "$BINARY_NAME"
    chmod +x "$BINARY_NAME"
fi

echo "[SUCCESS] cloudflared deployed inside the application directory."


# ---------------------------------------------------------------------------
# Python dependencies
# ---------------------------------------------------------------------------
echo "[*] Installing Python dependencies..."

if command -v pip3 >/dev/null 2>&1; then
    PIP_BIN="pip3"
elif command -v pip >/dev/null 2>&1; then
    PIP_BIN="pip"
else
    echo "[WARNING] No 'pip' found. Please install the dependencies in requirements.txt manually."
    exit 0
fi

echo "[*] Using $PIP_BIN..."
$PIP_BIN install -r requirements.txt

echo "========================================================================="
echo "[*] SETUP COMPLETED."
echo " -> Proxy engine (Replica): ready."
echo " -> Tunnel client: ready (./cloudflared)."
echo "========================================================================="
