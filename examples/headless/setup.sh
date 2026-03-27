#!/bin/bash
# samsung-tv-art one-time setup script
# Run this once after SSH-ing into your Raspberry Pi Zero 2 W (or any Debian-based host).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/kohlerryan/samsung-tv-art-uploader/main/examples/headless/setup.sh | bash
#   — or copy this file to the host and run: bash setup.sh
#
# ── Before running ────────────────────────────────────────────────────────────
#   Set TV_IP below to the IP address of your Samsung Frame TV.
#   Collections are selected after setup via the web UI (Settings → Collections).
# ─────────────────────────────────────────────────────────────────────────────

set -e

# ── User configuration — edit before running ──────────────────────────────────

TV_IP="CHANGE_ME"           # IP address of your Samsung Frame TV
MQTT_PASSWORD=""            # leave blank for anonymous MQTT access

# ─────────────────────────────────────────────────────────────────────────────

PROJECT_DIR="$HOME/samsung-tv-art"

if [[ "$TV_IP" == "CHANGE_ME" ]]; then
    echo "ERROR: Set TV_IP at the top of this script before running."
    exit 1
fi

# ── Hostname resolution ───────────────────────────────────────────────────────
# Prevents "sudo: unable to resolve host" warnings
if ! grep -qF "$(hostname)" /etc/hosts 2>/dev/null; then
    echo "127.0.1.1 $(hostname)" | sudo tee -a /etc/hosts >/dev/null
fi

# ── System update ─────────────────────────────────────────────────────────────
sudo apt-get update -y && sudo apt-get upgrade -y

# ── Docker ────────────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    sudo systemctl enable docker
    echo "Docker installed."
fi

# ── Project directory ─────────────────────────────────────────────────────────
if [ ! -d "$PROJECT_DIR" ]; then
    mkdir -p "$PROJECT_DIR"/{data,media,mosquitto/{config,data,log}}
fi

# ── Mosquitto config ──────────────────────────────────────────────────────────
if [ ! -f "$PROJECT_DIR/mosquitto/config/mosquitto.conf" ]; then
    mkdir -p "$PROJECT_DIR/mosquitto/config"
    cat > "$PROJECT_DIR/mosquitto/config/mosquitto.conf" << 'EOF'
listener 1883
allow_anonymous true
listener 9001
protocol websockets
allow_anonymous true
persistence true
persistence_location /mosquitto/data/
log_dest file /mosquitto/log/mosquitto.log
EOF
fi

# ── docker-compose.yml ────────────────────────────────────────────────────────
if [ ! -f "$PROJECT_DIR/docker-compose.yml" ]; then
    cat > "$PROJECT_DIR/docker-compose.yml" << 'EOF'
services:
  mosquitto:
    image: eclipse-mosquitto:latest
    container_name: mosquitto
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./mosquitto/config:/mosquitto/config
      - ./mosquitto/data:/mosquitto/data
      - ./mosquitto/log:/mosquitto/log

  samsung-tv-art:
    image: ghcr.io/kohlerryan/samsung-tv-art:latest
    container_name: samsung-tv-art
    restart: unless-stopped
    network_mode: host
    env_file:
      - ./samsung-tv-art.env
    volumes:
      - ./data:/data
      - ./media:/app/frame_tv_art_collections
    depends_on:
      - mosquitto

EOF
fi

# ── samsung-tv-art.env ────────────────────────────────────────────────────────
if [ ! -f "$PROJECT_DIR/samsung-tv-art.env" ]; then
    cat > "$PROJECT_DIR/samsung-tv-art.env" << EOF
SAMSUNG_TV_ART_TV_IP=${TV_IP}
SAMSUNG_TV_ART_UPDATE_MINUTES=30
SAMSUNG_TV_ART_MAX_UPLOADS=30
SAMSUNG_TV_ART_SEQUENTIAL=false
SAMSUNG_TV_ART_MQTT_HOST=localhost
SAMSUNG_TV_ART_MQTT_PORT=1883
SAMSUNG_TV_ART_LOCAL_WEB=true
${MQTT_PASSWORD:+SAMSUNG_TV_ART_MQTT_PASSWORD=${MQTT_PASSWORD}}
EOF
fi

# ── Cron ─────────────────────────────────────────────────────────────────────
if ! command -v crontab &>/dev/null; then
    sudo apt-get install -y cron
    sudo systemctl enable cron
fi

# ── Auto-update cron jobs ────────────────────────────────────────────────────
# Pulls and recreates the samsung-tv-art container if a new image is available.
# Runs at boot (after a short delay for Docker to be ready) and nightly at 3am.
# PATH is set explicitly so docker is found in cron's minimal environment.
# Always replaces existing entries so re-running setup.sh keeps them current.
UPDATE_CMD="PATH=/usr/local/bin:/usr/bin:/bin && cd '$PROJECT_DIR' && docker compose pull samsung-tv-art && docker compose up -d samsung-tv-art >> '$PROJECT_DIR/update.log' 2>&1"
(
    crontab -l 2>/dev/null \
        | grep -vF 'docker compose pull samsung-tv-art'
    echo "@reboot sleep 15 && $UPDATE_CMD"
    echo "0 3 * * * $UPDATE_CMD"
) | crontab -
echo "Auto-update cron jobs installed (@reboot + 3am)."

# ── Start ─────────────────────────────────────────────────────────────────────
# Use sg to activate docker group (works immediately after usermod, no re-login needed)
sg docker -c "cd '$PROJECT_DIR' && docker compose up -d"

echo ""
echo "Done. Web UI available at http://$(hostname -I | awk '{print $1}'):8080"
echo "Or: http://$(hostname).local:8080"
