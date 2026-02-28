# Samsung Frame TV Art — Home Assistant Lovelace Card

This directory contains the custom Lovelace card (`samsung-tv-art-card.js`) that provides in-HA control of the Samsung Frame TV art uploader.

## Installation

### Option A — Manual copy

1. Copy `samsung-tv-art-card.js` into your HA config directory:
   ```bash
   mkdir -p <ha-config>/www/samsung-tv-art-card/
   cp samsung-tv-art-card.js <ha-config>/www/samsung-tv-art-card/
   ```

2. Register the resource in `configuration.yaml`:
   ```yaml
   lovelace:
     resources:
       - url: /local/samsung-tv-art-card/samsung-tv-art-card.js?v=1.5.6
         type: module
   ```

3. Restart Home Assistant.

### Option B — HACS (coming soon)

HACS support is planned for a future release.

## Dashboard card

After installation, add the card to a dashboard view. See [`examples/ha-lovelace-card.yaml.example`](../examples/ha-lovelace-card.yaml.example) for a complete configuration example.

## Features

- Displays the currently shown artwork with title, artist, and description pulled from MQTT
- Collection selector — choose which artwork collections the TV should cycle through
- Refresh button — clears uploads and re-seeds the TV with a fresh randomized set
- Update & Refresh button — fetches latest collection updates from git, rebuilds the artwork database, then re-seeds
- Live progress log during any refresh operation
- Settings panel — TV IP, max uploads, rotation interval
- Supports HTTP and HTTPS image paths to avoid mixed-content issues

## Version

Current version: **v1.5.6** — update the `?v=` cache-buster in the resource URL when upgrading.
