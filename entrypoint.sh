#!/usr/bin/env bash
# Start tailscaled (userspace networking, HTTP proxy on localhost:1055) when
# TS_AUTHKEY is set, wait for it to be Running, then run the worker ("$@").
# If either process exits, the container exits so the orchestrator restarts it.
set -euo pipefail

APP_UID=1000
APP_GID=1000
TS_STATE_DIR=/var/lib/tailscale
TS_SOCKET=/tmp/tailscaled.sock

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) entrypoint: $*"; }

# Started as root: make the mounted volumes writable, then drop to the app user.
# Bind mounts are created root-owned by Docker, so this runs on every start.
if [[ "$(id -u)" == 0 ]]; then
  mkdir -p /data "$TS_STATE_DIR"
  chown -R "$APP_UID:$APP_GID" /data "$TS_STATE_DIR"
  exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --init-groups --no-new-privs "$0" "$@"
fi

if [[ -z "${TS_AUTHKEY:-}" ]]; then
  log "TS_AUTHKEY not set, Tailscale disabled; printers are reached directly"
  exec "$@"
fi

# Never send a Headscale key to Tailscale's default control server.
if [[ "$TS_AUTHKEY" == hskey-* && -z "${TS_LOGIN_SERVER:-}" ]]; then
  log "TS_AUTHKEY is a Headscale key but TS_LOGIN_SERVER is not set; refusing to log in"
  exit 1
fi

backend_state() {
  tailscale status --json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("BackendState",""))' 2>/dev/null \
    || true
}

log "starting tailscaled (userspace networking)"
tailscaled \
  --tun=userspace-networking \
  --state="$TS_STATE_DIR/tailscaled.state" \
  --statedir="$TS_STATE_DIR" \
  --socket="$TS_SOCKET" \
  --outbound-http-proxy-listen=localhost:1055 \
  --no-logs-no-support \
  > >(grep --line-buffered -v '\[RATELIMIT\]') 2>&1 &
# $! is tailscaled itself, not the filter. The filter only drops tailscaled's
# notices that it is holding back repeats of a debug message.
tsd_pid=$!

stopping=""
trap 'stopping=1; kill -TERM "$tsd_pid" ${worker_pid:+"$worker_pid"} 2>/dev/null || true' TERM INT

state=""
for _ in $(seq 1 30); do
  kill -0 "$tsd_pid" 2>/dev/null || { log "tailscaled exited during startup"; exit 1; }
  state="$(backend_state)"
  [[ -n "$state" && "$state" != "NoState" ]] && break
  sleep 1
done
log "tailscaled backend state: ${state:-unknown}"

up_args=(--reset --accept-routes --hostname="${TS_HOSTNAME:-ricoh-ldap-sync}" --timeout=60s)
[[ -n "${TS_LOGIN_SERVER:-}" ]] && up_args+=(--login-server="$TS_LOGIN_SERVER")
# The auth key is only needed for the first login; after that the node key in
# $TS_STATE_DIR is reused, so an expired key does not break restarts.
if [[ "$state" == "NeedsLogin" ]]; then
  log "logging in with TS_AUTHKEY"
  up_args+=(--auth-key="$TS_AUTHKEY")
fi
if ! tailscale up "${up_args[@]}"; then
  log "tailscale up failed"
  kill -TERM "$tsd_pid" 2>/dev/null || true
  exit 1
fi

for _ in $(seq 1 60); do
  state="$(backend_state)"
  [[ "$state" == "Running" ]] && break
  kill -0 "$tsd_pid" 2>/dev/null || { log "tailscaled exited"; exit 1; }
  sleep 1
done
if [[ "$state" != "Running" ]]; then
  log "tailscale did not reach Running (state: ${state:-unknown})"
  kill -TERM "$tsd_pid" 2>/dev/null || true
  exit 1
fi
log "tailscale is Running; starting worker"

"$@" &
worker_pid=$!

set +e
wait -n "$tsd_pid" "$worker_pid"
code=$?
set -e
if [[ -n "$stopping" ]]; then
  log "received stop signal"
  code=0
elif ! kill -0 "$tsd_pid" 2>/dev/null; then
  log "tailscaled exited (code $code); stopping"
  code=1
else
  log "worker exited (code $code); stopping"
fi
kill -TERM "$tsd_pid" "$worker_pid" 2>/dev/null || true
wait 2>/dev/null || true
exit "$code"
