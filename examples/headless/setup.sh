#!/bin/bash
# samsung-tv-art one-time setup script
# Run this once after SSH-ing into your Radxa (or any Debian-based host).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/kohlerryan/samsung-tv-art-uploader/main/examples/headless/setup.sh | bash
#   — or copy this file to the host and run: bash setup.sh
#
# ── Before running ────────────────────────────────────────────────────────────
#   Set TV_IP below to the IP address of your Samsung Frame TV.
#   Optionally uncomment COLLECTIONS to fetch artwork from git repos.
# ─────────────────────────────────────────────────────────────────────────────

set -e

# ── User configuration — edit before running ──────────────────────────────────

TV_IP="CHANGE_ME"           # IP address of your Samsung Frame TV
MQTT_PASSWORD=""            # leave blank for anonymous MQTT access
# COLLECTIONS="https://github.com/kohlerryan/Abbott_Handerson_Thayer.git https://github.com/kohlerryan/Adalbert_Stifter.git https://github.com/kohlerryan/Akseli_Gallen-Kallela.git https://github.com/kohlerryan/Albert_Bierstadt.git https://github.com/kohlerryan/Alfred_Sisley.git https://github.com/kohlerryan/Alphonse_Mucha.git https://github.com/kohlerryan/Andy_Warhol.git https://github.com/kohlerryan/Antoine_Chintreuil.git https://github.com/kohlerryan/Arthur_Streeton.git https://github.com/kohlerryan/Banksy.git https://github.com/kohlerryan/Berthe_Morisot.git https://github.com/kohlerryan/Camille_Pissarro.git https://github.com/kohlerryan/Charles_Marion_Russell.git https://github.com/kohlerryan/Childe_Hassam.git https://github.com/kohlerryan/Claude_Monet.git https://github.com/kohlerryan/Diego_Velazquez.git https://github.com/kohlerryan/Edgar_Degas.git https://github.com/kohlerryan/Edouard_Manet.git https://github.com/kohlerryan/Edvard_Munch.git https://github.com/kohlerryan/Edward_Hopper.git https://github.com/kohlerryan/El_Greco.git https://github.com/kohlerryan/Eugene_Boudin.git https://github.com/kohlerryan/Eugene_Delacroix.git https://github.com/kohlerryan/Francois_Boucher.git https://github.com/kohlerryan/Franz_Marc.git https://github.com/kohlerryan/Frederic_Remington.git https://github.com/kohlerryan/Frederick_McCubbin.git https://github.com/kohlerryan/George_Stubbs.git https://github.com/kohlerryan/George_Wesley_Bellows.git https://github.com/kohlerryan/Georges_Seurat.git https://github.com/kohlerryan/Gustav_Courbet.git https://github.com/kohlerryan/Gustav_Klimt.git https://github.com/kohlerryan/Gustave_Caillebotte.git https://github.com/kohlerryan/Henri_de_Toulouse-Lautrec.git https://github.com/kohlerryan/Henri_Matisse.git https://github.com/kohlerryan/Henri_Rousseau.git https://github.com/kohlerryan/Jackson_Pollock.git https://github.com/kohlerryan/Jacob_Maris.git https://github.com/kohlerryan/Keith_Haring.git https://github.com/kohlerryan/Leonardo_da_Vinci.git https://github.com/kohlerryan/Marc_Chagall.git https://github.com/kohlerryan/Mark_Rothko.git https://github.com/kohlerryan/Mary_Cassatt.git https://github.com/kohlerryan/Max_Ernst.git https://github.com/kohlerryan/Norman_Rockwell.git https://github.com/kohlerryan/Pablo_Picasso.git https://github.com/kohlerryan/Paul_Cezanne.git https://github.com/kohlerryan/Paul_Gauguin.git https://github.com/kohlerryan/Paul_Klee.git https://github.com/kohlerryan/Pierre-Auguste_Renoir.git https://github.com/kohlerryan/Rembrandt_Harmenszoon_van_Rijn.git https://github.com/kohlerryan/Sandro_Botticelli.git https://github.com/kohlerryan/Vincent_van_Gogh.git https://github.com/kohlerryan/Winslow_Homer.git"

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

  watchtower:
    image: containrrr/watchtower
    container_name: watchtower
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      - WATCHTOWER_CLEANUP=true
      - WATCHTOWER_POLL_INTERVAL=3600
    command: samsung-tv-art
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
${COLLECTIONS:+SAMSUNG_TV_ART_COLLECTIONS=${COLLECTIONS}}
${MQTT_PASSWORD:+SAMSUNG_TV_ART_MQTT_PASSWORD=${MQTT_PASSWORD}}
EOF
fi

# ── Start ─────────────────────────────────────────────────────────────────────
# Use sg to activate docker group (works immediately after usermod, no re-login needed)
sg docker -c "cd '$PROJECT_DIR' && docker compose up -d"

echo ""
echo "Done. Web UI available at http://$(hostname -I | awk '{print $1}'):8080"
echo "Or: http://$(hostname).local:8080"
