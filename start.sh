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

  # ── 1. Ensure a machine-id exists (dbus refuses to start without one) ──────
  if [ ! -s /etc/machine-id ]; then
    if command -v dbus-uuidgen >/dev/null 2>&1; then
      dbus-uuidgen > /etc/machine-id 2>/dev/null || true
    else
      # Fallback: generate a pseudo-random 32-char hex string
      cat /proc/sys/kernel/random/uuid 2>/dev/null | tr -d '-' > /etc/machine-id || true
    fi
  fi
  # Some distros symlink /var/lib/dbus/machine-id → /etc/machine-id; ensure both exist
  mkdir -p /var/lib/dbus
  [ -s /var/lib/dbus/machine-id ] || cp /etc/machine-id /var/lib/dbus/machine-id 2>/dev/null || true

  # ── 2. Clean up any stale dbus / avahi artifacts from a previous run ───────
  rm -f /run/dbus/system_bus_socket /run/dbus/pid
  rm -f /run/avahi-daemon/pid /run/avahi-daemon/socket
  mkdir -p /run/dbus /etc/avahi/services /run/avahi-daemon /etc/avahi

  # ── 3. Write Avahi daemon config ───────────────────────────────────────────
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

  # ── 4. Start dbus and wait for its socket ──────────────────────────────────
  dbus-daemon --system --address=unix:path=/run/dbus/system_bus_socket \
    --nofork --nopidfile >/dev/null 2>&1 &
  DBUS_PID=$!
  for i in $(seq 1 20); do
    [ -S /run/dbus/system_bus_socket ] && break
    sleep 0.2
  done
  if [ ! -S /run/dbus/system_bus_socket ]; then
    echo "mDNS: warning: dbus socket did not appear; avahi may not start correctly." >&2
  fi
  export DBUS_SYSTEM_BUS_ADDRESS="unix:path=/run/dbus/system_bus_socket"

  # ── 5. Write the HTTP service advertisement ────────────────────────────────
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

  # ── 6. Launch avahi-daemon ─────────────────────────────────────────────────
  # Run in foreground (no -D) backgrounded with &. --no-rlimits / --no-chroot /
  # --no-drop-root avoid capability and chroot failures inside containers.
  avahi-daemon --no-rlimits --no-chroot --no-drop-root >/var/log/avahi.log 2>&1 &
  AVAHI_PID=$!

  # ── 7. Wait for avahi-daemon to be ready (up to ~5 s) ──────────────────────
  for i in $(seq 1 25); do
    avahi-daemon --check >/dev/null 2>&1 && break
    # Also bail early if the process already died
    kill -0 "$AVAHI_PID" 2>/dev/null || break
    sleep 0.2
  done

  if avahi-daemon --check >/dev/null 2>&1; then
    echo "mDNS: avahi-daemon running (pid $AVAHI_PID)"
    # ── 8. Verify .local resolution (up to ~10 s) ────────────────────────────
    resolved=false
    for i in $(seq 1 20); do
      if avahi-resolve -n "${HOST_NAME}.local" >/dev/null 2>&1; then
        resolved=true
        break
      fi
      sleep 0.5
    done
    if [ "$resolved" = "true" ]; then
      echo "mDNS: ${HOST_NAME}.local is resolvable — advertising _http._tcp on 8080"
    else
      echo "mDNS: avahi is running but ${HOST_NAME}.local did not resolve within 10 s." >&2
      echo "mDNS: This is normal on Docker bridge networks — use 'network_mode: host'" >&2
      echo "mDNS: in your compose file for mDNS to work across the LAN." >&2
    fi
  else
    echo "mDNS: avahi-daemon failed to start. Last log output:" >&2
    tail -20 /var/log/avahi.log >&2 || true
    echo "mDNS: Continuing without mDNS advertisement." >&2
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
