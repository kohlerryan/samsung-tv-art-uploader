# Samsung Frame TV Art Uploader

Automatically uploads and rotates artwork on a **Samsung Frame TV**, with Home Assistant integration via MQTT for live entity state, collection selection, and a custom Lovelace card.

## Features

- Fetches artwork collections from git repositories (or uses a local bind-mount)
- Rotates a randomized or sequential set of images on the TV on a configurable schedule
- Publishes artwork metadata (title, artist, description, collection) to MQTT for Home Assistant
- MQTT discovery — entities are auto-created in HA with no manual YAML
- Built-in web UI (port 8080) for collection selection, settings, and manual refresh
- Home Assistant Lovelace card with live progress display during refresh operations
- mDNS advertisement (`samsung-tv-art.local`) via Avahi

## Requirements

- Samsung Frame TV (The Frame, any year with Art Mode)
- MQTT broker (e.g. Mosquitto)
- Docker host on the same LAN as the TV
- _(Optional)_ Home Assistant with MQTT integration

## Quick start

```bash
# 1. Copy and edit the env file
cp examples/samsung-tv-art.env.example samsung-tv-art.env
# Edit samsung-tv-art.env — set TV IP, MQTT host/credentials, etc.

# 2. Copy and edit the compose file
cp examples/docker-compose.yml docker-compose.yml

# 3. Build and start
docker compose up -d
```

Open the web UI at `http://samsung-tv-art.local:8080` (or `http://<host-ip>:8080`).

## Artwork collections

### Option A — Git repositories

Set `SAMSUNG_TV_ART_COLLECTIONS` in your env file to a list of git repository URLs.  
Each repo should contain `.jpg`/`.png` image files and an optional `.csv` with metadata.

```env
SAMSUNG_TV_ART_COLLECTIONS=https://github.com/you/Monet.git
  https://github.com/you/Degas.git
```

Click **Update & Refresh** in the web UI or HA card to fetch the latest commits and re-seed the TV at any time.

### Option B — Local bind-mount

Place collection subdirectories inside `./media`:
```
media/
  Monet/
    Monet_1906_Water Lilies.jpg
    ...
  Degas/
    ...
```

Each subdirectory becomes a selectable collection. The container maps `./media` → `/app/frame_tv_art_collections`.

## Configuration

All settings are controlled via environment variables. Copy `examples/samsung-tv-art.env.example` to `samsung-tv-art.env` for a fully commented reference.

Key variables:

| Variable | Default | Description |
|---|---|---|
| `SAMSUNG_TV_ART_TV_IP` | _(required)_ | IP address of the Frame TV |
| `SAMSUNG_TV_ART_UPDATE_MINUTES` | `30` | Artwork rotation interval |
| `SAMSUNG_TV_ART_MAX_UPLOADS` | `30` | Max images kept on TV at once |
| `SAMSUNG_TV_ART_SEQUENTIAL` | `false` | `true` = fixed order, `false` = shuffle |
| `SAMSUNG_TV_ART_MQTT_HOST` | — | MQTT broker hostname or IP |
| `SAMSUNG_TV_ART_COLLECTIONS` | — | Newline-separated git repo URLs |
| `SAMSUNG_TV_ART_GITHUB_TOKEN` | — | GitHub PAT for private repos |
| `SAMSUNG_TV_ART_FETCH_ON_START` | `false` | Fetch collections on container start |
| `SAMSUNG_TV_ART_LOCAL_WEB` | `false` | Enable the web UI on port 8080 |
| `SAMSUNG_TV_ART_MDNS_ENABLE` | `true` | Advertise via mDNS as `<hostname>.local` |

See `examples/samsung-tv-art.env.example` for the full list with descriptions.

## Home Assistant card

Install the custom Lovelace card from the `ha-card/` directory.  
See [`ha-card/README.md`](ha-card/README.md) for installation steps and [`examples/ha-lovelace-card.yaml.example`](examples/ha-lovelace-card.yaml.example) for a complete card configuration.

### Mixed-content / image URLs

Browsers block HTTP image requests from HTTPS pages. If HA is served over HTTPS, configure both URL fields in the card so it can pick the right one:

```yaml
image_path_http: http://10.0.0.10:8080/app/frame_tv_art_collections
image_path_https: https://samsung-tv-art.yourdomain.com/app/frame_tv_art_collections
```

Or copy the media folder into HA's `www` directory to serve images from `/local`:

```bash
docker cp samsung-tv-art:/app/frame_tv_art_collections/. \
  /path/to/ha-config/www/frame_tv_art_collections/
```

Then set `image_path: /local/frame_tv_art_collections` in the card config.

## Persistent data

The `./data` bind-mount stores files that survive container restarts:

| File | Purpose |
|---|---|
| `data/token_file.txt` | TV authentication token (auto-created on first connect) |
| `data/uploaded_files_cache.json` | Maps local filenames to TV content IDs |
| `data/overrides.env` | Runtime settings overrides (written by web UI Settings panel) |
| `data/collections.list` | Alternative to env var — one git URL per line |

## MQTT topics

| Topic | Direction | Description |
|---|---|---|
| `frame_tv/selected_artwork/state` | publish | Currently displayed artwork filename |
| `frame_tv/selected_artwork/attributes` | publish | Full artwork metadata (title, artist, description, …) |
| `frame_tv/selected_collections/state` | publish / subscribe | Active collection names |
| `frame_tv/collections/attributes` | publish | All available collections list |
| `frame_tv/cmd/collections/refresh` | subscribe | Trigger a Refresh |
| `frame_tv/cmd/settings/sync_collections` | subscribe | Trigger Update & Refresh (git fetch + reseed) |
| `frame_tv/ack/collections/refresh` | publish | Progress acks during refresh |

## Repository structure

```
samsung-tv-art/
├── Dockerfile
├── start.sh               — container entrypoint: fetches collections, starts uploader
├── uploader.py            — main TV uploader and MQTT integration
├── serve.py               — minimal HTTP server for the web UI
├── assets/
│   └── standby.png        — default standby artwork baked into the image
├── ha-card/
│   ├── samsung-tv-art-card.js   — Home Assistant Lovelace card
│   └── README.md
├── scripts/
│   ├── fetch_collections.sh     — git clone/pull collection repos at runtime
│   ├── aggregate_csv.py         — merges per-collection CSVs into a single artwork_data.csv
│   └── bake_addons.sh           — build-time alternative to fetch_collections.sh
├── www/
│   └── index.html         — web UI source (baked into container image)
├── examples/
│   ├── docker-compose.yml
│   ├── samsung-tv-art.env.example
│   └── ha-lovelace-card.yaml.example
├── data/                  — bind-mount target (gitignored contents)
└── media/                 — bind-mount target for local artwork (gitignored contents)
```

## Troubleshooting

**TV not connecting** — Check `SAMSUNG_TV_ART_TV_IP` and ensure the container is on the same network as the TV. The TV may prompt for a pairing confirmation on first connect.

**No entities in HA** — Confirm `SAMSUNG_TV_ART_MQTT_DISCOVERY=true` and that your MQTT broker is reachable from the container.

**Images not showing in HA card** — Open the browser console and look for `FRAME-TV-ART-CARD: computed bgUrl`. Mixed-content errors mean you need to use `image_path_https` or serve images from `/local`.

**Check container logs:**
```bash
docker logs -f samsung-tv-art
```

## License

MIT — see [LICENSE](LICENSE).


