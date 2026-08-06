#!/bin/sh
set -eu

FORTRESS_TZ="${FORTRESS_TZ:-${TZ:-UTC}}"
FORTRESS_LANG="${FORTRESS_LANG:-${LANG:-en_US.UTF-8}}"

export TZ="$FORTRESS_TZ"
export LANG="$FORTRESS_LANG"
export TILION_TZ="$FORTRESS_TZ"
export TILION_LANG="$FORTRESS_LANG"

if [ "${UBLOCK_ORIGIN_LITE_ENABLED:-true}" = "1" ] || \
   [ "${UBLOCK_ORIGIN_LITE_ENABLED:-true}" = "true" ] || \
   [ "${UBLOCK_ORIGIN_LITE_ENABLED:-true}" = "yes" ]; then
    set -- "$@" --load-extension=/tmp/tillion-profile/.ublock-origin-lite
fi

exec /usr/bin/dumb-init -- /usr/local/bin/docker-entrypoint.sh "$@"
