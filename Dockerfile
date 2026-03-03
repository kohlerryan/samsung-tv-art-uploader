FROM python:3.11-slim

LABEL org.opencontainers.image.source=https://github.com/kohlerryan/samsung-tv-art-uploader

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git avahi-daemon avahi-utils dbus \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir git+https://github.com/NickWaterton/samsung-tv-ws-api.git pillow paho-mqtt

WORKDIR /app
COPY start.sh /app/start.sh
COPY serve.py /app/serve.py
COPY uploader.py /app/uploader.py
COPY scripts/ /app/scripts/
RUN chmod +x /app/scripts/*.sh || true

# Web UI and standby artwork baked into the image
COPY www/ /app/www/
COPY assets/standby.png /app/frame_tv_art_collections/standby.png
COPY assets/standby.png /app/standby.default.png

RUN chmod +x /app/start.sh

ENTRYPOINT ["/app/start.sh"]
