# Self-hosted server guide

This guide walks through running `samsung-tv-art` as a persistent, self-updating server on a **Raspberry Pi Zero 2 W**.

The result is a headless server that:
- runs the uploader and a local Mosquitto MQTT broker 24/7
- auto-restarts after a reboot or crash
- automatically pulls and applies new image releases without manual intervention

---

## What you need

- A **Raspberry Pi Zero 2 W** (arm64, runs cool, ~$15)
- A microSD card (≥16 GB recommended)
- WiFi connection to your LAN (same network as the Frame TV) — or a USB-OTG ethernet adapter
- A computer to flash the SD card and SSH from

---

## 1. Flash the OS

Download and open [Raspberry Pi Imager](https://www.raspberrypi.com/software/).

1. **Choose Device** → Raspberry Pi Zero 2 W
2. **Choose OS** → Raspberry Pi OS (other) → **Raspberry Pi OS Lite (64-bit)**
3. **Choose Storage** → your SD card
4. Click **Next** → **Edit Settings** (OS Customisation)

In the customisation screen, configure everything now so no keyboard or monitor is ever needed:

| Setting | Value |
|---|---|
| Hostname | `samsung-tv-art` |
| Username | `uploader` (or any name you prefer) |
| Password | your choice |
| Configure WiFi | your SSID and password |
| Enable SSH | checked (use password auth or add your public key) |

Click **Save** → **Yes** → flash the card.

Insert the card and power on. The Pi will boot and be reachable on your LAN in about 30–60 seconds.

> The [`setup.sh`](../examples/headless/setup.sh) script handles everything after this point — Docker install, directory structure, config files, and starting all containers.

---

## 2. SSH in

If you set the hostname to `samsung-tv-art` in Pi Imager, connect directly:
```bash
ssh uploader@samsung-tv-art.local
```

Otherwise find the board's IP from your router and SSH in by IP, then set the hostname:
```bash
ssh uploader@<board-ip>

# Set the hostname (so the board is reachable at samsung-tv-art.local)
OLD_HOSTNAME=$(hostname)
sudo hostnamectl set-hostname samsung-tv-art
sudo sed -i "s/$OLD_HOSTNAME/samsung-tv-art/g" /etc/hosts
sudo systemctl restart avahi-daemon
```

> `sudo: unable to resolve host` warnings during the hostname commands are harmless — they complete successfully.

---

## 3. Run the setup script

The setup script handles everything: system update, Docker install, directory structure, config files, and starting all containers.

```bash
# Download the script
curl -fsSL https://raw.githubusercontent.com/kohlerryan/samsung-tv-art-uploader/main/examples/headless/setup.sh -o setup.sh

# Set your Frame TV's IP address before running
nano setup.sh        # change TV_IP="CHANGE_ME" to your TV's IP

bash setup.sh
```

That's it — once the script finishes, skip to [step 9 (web UI)](#9-access-the-web-ui).

> If you prefer to set things up manually, follow steps 4–8 below instead.

---

## 4. (Manual) Create the project directory

```bash
mkdir -p ~/samsung-tv-art/{data,media,mosquitto/{config,data,log}}
cd ~/samsung-tv-art
```

| Directory | Purpose |
|---|---|
| `data/` | TV auth token, settings overrides, upload cache — persists across restarts |
| `media/` | Optional local artwork — place collection subfolders here |
| `mosquitto/` | Mosquitto config and persistent data |

---

## 5. (Manual) Configure Mosquitto

Create the Mosquitto config file:
```bash
cat > mosquitto/config/mosquitto.conf << 'EOF'
listener 1883
allow_anonymous true
listener 9001
protocol websockets
allow_anonymous true
persistence true
persistence_location /mosquitto/data/
log_dest file /mosquitto/log/mosquitto.log
EOF
```

> If you want authenticated MQTT, see [Mosquitto password file docs](https://mosquitto.org/documentation/authentication-methods/) and update the `SAMSUNG_TV_ART_MQTT_USERNAME` / `SAMSUNG_TV_ART_MQTT_PASSWORD` vars accordingly.

---

## 6. (Manual) Create the env file

```bash
nano samsung-tv-art.env
```

Paste and fill in at minimum:
```env
# ── TV ────────────────────────────────────────────────────────────────────────
SAMSUNG_TV_ART_TV_IP=192.168.1.xxx        # IP address of your Frame TV

# ── Rotation ──────────────────────────────────────────────────────────────────
SAMSUNG_TV_ART_UPDATE_MINUTES=30          # rotate artwork every 30 minutes
SAMSUNG_TV_ART_MAX_UPLOADS=30             # keep up to 30 images on the TV
SAMSUNG_TV_ART_SEQUENTIAL=false           # false = random shuffle

# ── MQTT ──────────────────────────────────────────────────────────────────────
SAMSUNG_TV_ART_MQTT_HOST=localhost        # Mosquitto runs on the same host
SAMSUNG_TV_ART_MQTT_PORT=1883

# Uncomment to enable Home Assistant MQTT Discovery (auto-creates HA entities)
# SAMSUNG_TV_ART_MQTT_DISCOVERY=true
# SAMSUNG_TV_ART_MQTT_DISCOVERY_PREFIX=homeassistant

# ── Web UI ────────────────────────────────────────────────────────────────────
SAMSUNG_TV_ART_LOCAL_WEB=true
```

See `examples/samsung-tv-art.env.example` in the repo for the full list of options.

> **Collections are managed via the web UI.** Once the container is running, open `http://samsung-tv-art.local:8080`, go to the **Settings** tab, and use the **Collections** section to select artwork repos and trigger a fetch. No env var needed.

---

## 7. (Manual) Create the compose file

```bash
nano docker-compose.yml
```

```yaml
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

```

`network_mode: host` is used throughout so the uploader can reach the TV via mDNS/multicast and so Mosquitto is reachable at `localhost` from inside the uploader container.

---

## 8. (Manual) Start everything

```bash
docker compose up -d
```

Check that all three containers are running:
```bash
docker compose ps
```

Follow the uploader logs to confirm it connects to the TV:
```bash
docker compose logs -f samsung-tv-art
```

On first run the TV will display a pairing prompt — accept it on the TV. The auth token is written to `data/token_file.txt` and reused on subsequent starts.

---

## 9. Access the web UI

Open a browser on any device on your LAN:

```
http://samsung-tv-art.local:8080
```

Or by IP:
```
http://<board-ip>:8080
```

---

## Automatic updates

`setup.sh` installs two cron jobs for the `uploader` user:

| Trigger | Action |
|---|---|
| Boot (after 15s delay) | Pull latest image and recreate container if changed |
| Daily at 3am | Same |

Logs are written to `~/samsung-tv-art/update.log`. To inspect them:
```bash
tail -f ~/samsung-tv-art/update.log
```

To trigger an immediate update manually:
```bash
cd ~/samsung-tv-art && docker compose pull samsung-tv-art && docker compose up -d samsung-tv-art
```

---

## Useful commands

| Task | Command |
|---|---|
| View live uploader logs | `docker compose logs -f samsung-tv-art` |
| Restart the uploader | `docker compose restart samsung-tv-art` |
| Stop everything | `docker compose down` |
| Force an immediate update | `cd ~/samsung-tv-art && docker compose pull samsung-tv-art && docker compose up -d samsung-tv-art` |
| Check Mosquitto logs | `tail -f mosquitto/log/mosquitto.log` |
