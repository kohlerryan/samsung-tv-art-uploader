#!/usr/bin/env sh
set -e

# Load persistent overrides from /data if present (survive container restarts)
# Parse as plain KEY=VALUE lines (no shell eval) so special chars like '$' stay intact.
if [ -f "/data/overrides.env" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|'#'*)
        continue
        ;;
    esac
    key="${line%%=*}"
    value="${line#*=}"
    key="$(printf '%s' "$key" | tr -d '[:space:]')"
    [ -n "$key" ] || continue
    case "$key" in
      SAMSUNG_TV_ART_*)
        export "$key=$value"
        ;;
    esac
  done < "/data/overrides.env"
fi

TV_IP="${SAMSUNG_TV_ART_TV_IP:?Set SAMSUNG_TV_ART_TV_IP to your Frame TV IP address}"
ART_DIR="${SAMSUNG_TV_ART_ART_DIR:-/app/frame_tv_art_collections}"
UPDATE_MINUTES="${SAMSUNG_TV_ART_UPDATE_MINUTES:-3}"
CHECK_SECONDS="${SAMSUNG_TV_ART_CHECK_SECONDS:-60}"
SEQUENTIAL="${SAMSUNG_TV_ART_SEQUENTIAL:-true}"
STANDBY_FILE="${SAMSUNG_TV_ART_STANDBY_FILE:-/frame_tv_art_collections/standby.png}"
EXCLUDE="${SAMSUNG_TV_ART_EXCLUDE:-$STANDBY_FILE}"
TOKEN_FILE="${SAMSUNG_TV_ART_TOKEN_FILE:-/data/token_file.txt}"

MEDIA_ROOT="${SAMSUNG_TV_ART_MEDIA_ROOT:-/frame_tv_art_collections}"
FETCH_ON_START="${SAMSUNG_TV_ART_FETCH_ON_START:-false}"

ARGS=""
if [ "$SEQUENTIAL" = "true" ]; then
  ARGS="$ARGS -S"
fi
if [ -n "$EXCLUDE" ]; then
  ARGS="$ARGS -e $EXCLUDE"
fi
if [ -n "$STANDBY_FILE" ]; then
  ARGS="$ARGS --standby $STANDBY_FILE"
fi
# Optional: enable verbose debug logging for troubleshooting
if [ "${SAMSUNG_TV_ART_DEBUG:-false}" = "true" ]; then
  ARGS="$ARGS -D"
fi
# HA REST integration removed in MQTT-only build

# No sync step; read directly from /frame_tv_art_collections

export SAMSUNG_TV_ART_MEDIA_ROOT="$MEDIA_ROOT"

# If the operator provided external collection repositories, fetch only when explicitly enabled.
if [ "$FETCH_ON_START" = "true" ] || [ "$FETCH_ON_START" = "1" ] || [ "$FETCH_ON_START" = "yes" ]; then
  if [ -n "${SAMSUNG_TV_ART_COLLECTIONS:-}" ] || [ -f "/data/collections.list" ]; then
    echo "Fetching external collections (startup enabled)..."
    chmod +x /app/scripts/fetch_collections.sh 2>/dev/null || true
    /app/scripts/fetch_collections.sh || echo "Warning: fetch_collections.sh failed"
    # After fetching, (re)aggregate CSV to include newly pulled collections
    if [ -x "/app/scripts/aggregate_csv.py" ] || [ -f "/app/scripts/aggregate_csv.py" ]; then
      echo "Aggregating artwork CSV (runtime)..."
      python /app/scripts/aggregate_csv.py /app/frame_tv_art_collections /app/artwork_data.csv || echo "Warning: CSV aggregation failed at runtime"
    fi
  fi
else
  echo "Skipping startup collection fetch (SAMSUNG_TV_ART_FETCH_ON_START=$FETCH_ON_START)."
fi

# Ensure aggregated CSV exists even when startup fetch is disabled.
CSV_PATH="${SAMSUNG_TV_ART_CSV_PATH:-/app/artwork_data.csv}"
if [ ! -f "$CSV_PATH" ]; then
  if [ -x "/app/scripts/aggregate_csv.py" ] || [ -f "/app/scripts/aggregate_csv.py" ]; then
    echo "CSV missing at $CSV_PATH; generating from $MEDIA_ROOT..."
    python /app/scripts/aggregate_csv.py "$MEDIA_ROOT" "$CSV_PATH" || echo "Warning: CSV aggregation failed"
  fi
fi
# Export so the Python process inherits the resolved path
export SAMSUNG_TV_ART_CSV_PATH="$CSV_PATH"

# Ensure standby file exists on mounted media path for UI fallback backgrounds.
# If missing (or clearly a tiny generated placeholder), restore from bundled default.
DEFAULT_STANDBY="/app/standby.default.png"
if [ -n "$STANDBY_FILE" ]; then
  needs_restore="false"
  if [ ! -f "$STANDBY_FILE" ]; then
    needs_restore="true"
  else
    # Previous fallback produced a very small black PNG (~8-9KB). Replace it.
    size_bytes="$(wc -c < "$STANDBY_FILE" 2>/dev/null || echo 0)"
    case "$size_bytes" in
      ''|*[!0-9]*) size_bytes=0 ;;
    esac
    if [ "$size_bytes" -gt 0 ] && [ "$size_bytes" -le 12000 ]; then
      needs_restore="true"
      echo "Standby file at $STANDBY_FILE appears to be a tiny placeholder ($size_bytes bytes); restoring default"
    fi
  fi

  if [ "$needs_restore" = "true" ] && [ -f "$DEFAULT_STANDBY" ]; then
    echo "Restoring standby file at $STANDBY_FILE from bundled default"
    mkdir -p "$(dirname "$STANDBY_FILE")" 2>/dev/null || true
    cp -f "$DEFAULT_STANDBY" "$STANDBY_FILE" || echo "Warning: failed to restore standby file"
  fi
fi

# Optional: advertise mDNS (.local) name and HTTP service via Avahi
if [ "${SAMSUNG_TV_ART_MDNS_ENABLE:-true}" = "true" ]; then
  HOST_NAME="${SAMSUNG_TV_ART_PUBLIC_HOSTNAME:-${HOSTNAME}}"
  DOMAIN_NAME="local"
  mkdir -p /run/dbus /etc/avahi/services /run/avahi-daemon /etc/avahi
  # Configure Avahi to use our desired hostname (independent of kernel hostname)
  cat > /etc/avahi/avahi-daemon.conf <<EOF
[server]
host-name=${HOST_NAME}
domain-name=${DOMAIN_NAME}
use-ipv4=yes
use-ipv6=yes

[wide-area]
enable-wide-area=no

[publish]
publish-hinfo=no
publish-aaaa-on-ipv4=no
publish-a-on-ipv6=no
EOF

  # Start minimal system dbus for avahi-daemon
  dbus-daemon --system --address=unix:path=/run/dbus/system_bus_socket --nofork --nopidfile >/dev/null 2>&1 &
  # Give dbus a brief moment to create the socket
  for i in $(seq 1 10); do
    [ -S /run/dbus/system_bus_socket ] && break
    sleep 0.2
  done
  # Create an mDNS service for the Web UI (_http._tcp on port 8080)
  cat > /etc/avahi/services/samsung-tv-art-http.service <<EOF
<?xml version="1.0" standalone='no'?><!--*-nxml-*-->
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">${HOST_NAME} Web UI</name>
  <service>
    <type>_http._tcp</type>
    <port>8080</port>
  </service>
</service-group>
EOF
  # Launch avahi-daemon in background (fail visibly if it can't start)
  if ! avahi-daemon -D; then
    echo "mDNS: avahi-daemon failed to start; printing debug output..." >&2
    avahi-daemon -f --debug 2>&1 | sed -n '1,120p' >&2 &
  fi
  # Check that avahi-daemon is running
  if ! avahi-daemon --check >/dev/null 2>&1; then
    echo "mDNS: warning: avahi-daemon does not appear to be running (will continue)." >&2
  fi
  # Health check: wait until our .local resolves (up to ~10s)
  for i in $(seq 1 20); do
    if avahi-resolve -n "${HOST_NAME}.local" >/dev/null 2>&1; then
      echo "mDNS: ${HOST_NAME}.local is resolvable; advertising _http._tcp on 8080"
      break
    fi
    sleep 0.5
  done
  if ! avahi-resolve -n "${HOST_NAME}.local" >/dev/null 2>&1; then
    echo "mDNS: warning: could not verify ${HOST_NAME}.local resolution (avahi not running yet, interface mismatch, or multicast filtered). Continuing."
  fi
fi

# Optional: serve local web UI + media via simple HTTP server
if [ "${SAMSUNG_TV_ART_LOCAL_WEB:-false}" = "true" ]; then
  # Generate env defaults for the web UI
  WS_HOST="${SAMSUNG_TV_ART_MQTT_HOST:-$HOSTNAME}"
  WS_PORT="${SAMSUNG_TV_ART_MQTT_WS_PORT:-9001}"
  WS_URL="ws://$WS_HOST:${WS_PORT}"
  UI_USER="${SAMSUNG_TV_ART_MQTT_USERNAME:-}"
  UI_PASS=""
  if [ "${SAMSUNG_TV_ART_LOCAL_WEB_EXPOSE_PASSWORD:-false}" = "true" ]; then
    UI_PASS="${SAMSUNG_TV_ART_MQTT_PASSWORD:-}"
  fi
  # Prefer a friendly hostname for the log/URL if provided; fall back to container name/id
  PUBLIC_HOST="${SAMSUNG_TV_ART_PUBLIC_HOSTNAME:-${SAMSUNG_TV_ART_CONTAINER_NAME:-${CONTAINER_NAME:-${HOSTNAME}}}}"
  mkdir -p /app/www
  cat > /app/www/env.json <<EOF
{
  "broker": "${WS_URL}",
  "username": "${UI_USER}",
  "password": "${UI_PASS}"
}
EOF
  # Serve from / with SPA fallback: any unknown path routes to /app/www/index.html
  python /app/serve.py >/dev/null 2>&1 &
  echo "Local web UI available at http://$PUBLIC_HOST:8080/ (default broker ${WS_URL}); media at /app/frame_tv_art_collections and /media"
fi

# Seeding from host is intentionally removed for baked-only builds.

# Startup info: show baked CSV path and count of baked images
if [ -f "$CSV_PATH" ]; then
  echo "Using baked CSV: $CSV_PATH"
else
  echo "Using baked CSV: $CSV_PATH (file not found)"
fi
img_count=$(find "$MEDIA_ROOT" -type f 2>/dev/null | wc -l | tr -d ' ')
echo "Baked media files under $MEDIA_ROOT: ${img_count}"

exec python /app/uploader.py "$TV_IP" \
  -f "$ART_DIR" \
  -u "$UPDATE_MINUTES" \
  -c "$CHECK_SECONDS" \
  -t "$TOKEN_FILE" \
  $ARGS
