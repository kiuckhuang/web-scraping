#!/bin/sh
# Render the SearXNG settings from the template using .env overrides, then
# hand off to the upstream entrypoint (which fixes ownership/secret and
# starts granian). Missing env values fall back to these defaults.
set -eu

# Reject an unset or placeholder key ("make init" generates a real one; any
# value containing "change-me" is a template placeholder and must never be
# used as the actual instance secret).
case "${SEARXNG_SECRET_KEY:-}" in
    "" | *change-me*)
        SEARXNG_SECRET_KEY="$('/usr/local/searxng/.venv/bin/python' -c 'import secrets; print(secrets.token_hex(32))')"
        export SEARXNG_SECRET_KEY
        echo "SEARXNG_SECRET_KEY was not provided (or still a placeholder); generated an ephemeral startup secret" >&2
        ;;
esac

export SEARXNG_REQUEST_TIMEOUT="${SEARXNG_REQUEST_TIMEOUT:-10}"
export SEARXNG_MAX_REQUEST_TIMEOUT="${SEARXNG_MAX_REQUEST_TIMEOUT:-15}"
export SEARXNG_BAN_TIME_ON_FAIL="${SEARXNG_BAN_TIME_ON_FAIL:-5}"
export SEARXNG_MAX_BAN_TIME_ON_FAIL="${SEARXNG_MAX_BAN_TIME_ON_FAIL:-120}"
export SEARXNG_SUSPEND_TOO_MANY="${SEARXNG_SUSPEND_TOO_MANY:-180}"
# Outbound proxy for SearXNG engine requests (empty = direct egress). Needed
# where the direct egress IP is bot-challenged by engines (google/duckduckgo
# served CAPTCHAs from this host's direct route; the campus proxy passes).
export SEARXNG_OUTGOING_PROXY="${SEARXNG_OUTGOING_PROXY:-}"

/usr/local/searxng/.venv/bin/python \
    /usr/local/bin/ws-render-settings.py \
    /etc/searxng-templates/settings.template.yml \
    /tmp/searxng-settings.yml

export SEARXNG_SETTINGS_PATH=/tmp/searxng-settings.yml

# Bot detection derives its config dir from SEARXNG_SETTINGS_PATH (/tmp), so
# make the limiter config available there too.
if [ -f /etc/searxng/limiter.toml ]; then
    cp /etc/searxng/limiter.toml /tmp/limiter.toml
fi

# granian loads the rendered file above; the upstream entrypoint only checks
# that a settings.yml exists in its config dir (/etc/searxng). That dir is
# effectively read-only here (ro file binds inside it), so a settings.yml is
# pre-mounted there as a placeholder (see podman-compose.yml).
exec /usr/local/searxng/entrypoint.sh "$@"
