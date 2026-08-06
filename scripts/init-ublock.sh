#!/bin/sh
set -eu

PROFILE_DIR=/profile
EXTENSION_DIR="$PROFILE_DIR/.ublock-origin-lite"
VERSION=2026.804.1652
SHA256=062d95b68aea70e6173a4c30d5f48a4d79f23ce889f3335a50b3edf7992c6c38

enabled="${UBLOCK_ORIGIN_LITE_ENABLED:-true}"
if [ "$enabled" != "1" ] && [ "$enabled" != "true" ] && [ "$enabled" != "yes" ]; then
    touch "$PROFILE_DIR/.ublock-init-ready"
    tail -f /dev/null
fi

if [ ! -f "$EXTENSION_DIR/manifest.json" ]; then
    apk add --no-cache curl unzip >/dev/null
    tmp_dir=$(mktemp -d)
    trap 'rm -rf "$tmp_dir"' EXIT
    archive="$tmp_dir/ubol.zip"
    url="https://github.com/uBlockOrigin/uBOL-home/releases/download/$VERSION/uBOLite_${VERSION}.chromium.zip"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL -o "$archive" "$url"
    else
        wget -q -O "$archive" "$url"
    fi
    echo "$SHA256  $archive" | sha256sum -c -
    mkdir -p "$EXTENSION_DIR"
    unzip -q "$archive" -d "$EXTENSION_DIR"
    chown -R 1000:1000 "$EXTENSION_DIR" 2>/dev/null || true
    chmod -R a+rX "$EXTENSION_DIR"
fi

touch "$PROFILE_DIR/.ublock-init-ready"
tail -f /dev/null
