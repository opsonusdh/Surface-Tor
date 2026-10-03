#!/bin/sh

# Terminate execution immediately if any command returns a non-zero exit code
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"


if [ ! -d "Replica" ]; then
    echo "[*] Cloning the upstream web proxy isolation engine..."
    git clone https://github.com/sarperavci/Replica.git
else
    echo "[*] Replica workspace directory already exists. Skipping clone stage."
fi

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
        echo "[ERROR] Unsupported Linux architecture framework detected: $ARCH_RAW"
        exit 1
        ;;
esac

echo "[*] Target Environment Identified: Linux | Architecture=$ARCH"


DOWNLOAD_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH"
BINARY_NAME="cloudflared"

echo "[*] Extracting standalone binary from Cloudflare distribution..."
curl -L "$DOWNLOAD_URL" -o "$BINARY_NAME"
chmod +x "$BINARY_NAME"
export PATH="$SCRIPT_DIR:$PATH"

echo "[SUCCESS] Standalone proxy daemon '$BINARY_NAME' deployed inside application workspace."


echo "[*] Transitioning to local runtime environment configuration loop..."

if command -v pip3 >/dev/null 2>&1; then
    PIP_BIN="pip3"
elif command -v pip >/dev/null 2>&1; then
    PIP_BIN="pip"
else
    echo "[WARNING] No systemic 'pip' package binary located. Please execute dependencies manually."
    exit 0
fi

echo "[*] Triggering dependency installation layer using $PIP_BIN..."
$PIP_BIN install -r requirements.txt

echo "========================================================================="
echo "[*] LINUX SYSTEM METRICS DEPLOYMENT COMPLETED."
echo " -> Proxy Node Stack: Active & Configured."
echo " -> Tunnel Gateway Execution Binary: Ready (./cloudflared)."
echo "========================================================================="
