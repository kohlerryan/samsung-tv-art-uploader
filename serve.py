#!/usr/bin/env python3
import os
import sys
import json
import signal
import functools
from urllib.parse import urlparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

INDEX_PATH = "/app/www/index.html"
ROOT_DIR = "/"

class FallbackHandler(SimpleHTTPRequestHandler):
    # directory is passed via functools.partial
    def _json(self, code, payload):
        try:
            data = json.dumps(payload).encode('utf-8')
        except Exception:
            data = b'{}'
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_api_get_env(self):
        keys = [
            'SAMSUNG_TV_ART_MAX_UPLOADS',
            'SAMSUNG_TV_ART_UPDATE_MINUTES',
            'SAMSUNG_TV_ART_TV_IP',
        ]
        env = {k: os.environ.get(k, '') for k in keys}
        self._json(200, env)

    def _read_overrides(self):
        overrides_path = '/data/overrides.env'
        current = {}
        try:
            with open(overrides_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    k, v = line.split('=', 1)
                    current[k.strip()] = v.strip()
        except Exception:
            current = {}
        return current

    def _read_ui_mqtt(self):
        # Build UI MQTT defaults from overrides.env if present, else from process env
        ov = self._read_overrides()
        host = ov.get('SAMSUNG_TV_ART_MQTT_HOST') or os.environ.get('SAMSUNG_TV_ART_MQTT_HOST') or os.environ.get('HOSTNAME') or 'localhost'
        ws_port = ov.get('SAMSUNG_TV_ART_MQTT_WS_PORT') or os.environ.get('SAMSUNG_TV_ART_MQTT_WS_PORT') or '9001'
        user = ov.get('SAMSUNG_TV_ART_MQTT_USERNAME') or os.environ.get('SAMSUNG_TV_ART_MQTT_USERNAME') or ''
        pw = ov.get('SAMSUNG_TV_ART_MQTT_PASSWORD') or os.environ.get('SAMSUNG_TV_ART_MQTT_PASSWORD') or ''
        broker = f"ws://{host}:{ws_port}"
        return {'broker': broker, 'username': user, 'password': pw}

    def _write_ui_mqtt(self, data):
        # Parse broker URL and persist into overrides.env as discrete env vars
        broker = str(data.get('broker', '') or '').strip()
        username = str(data.get('username', '') or '')
        password = str(data.get('password', '') or '')
        host_val, ws_port_val = None, None
        if broker:
            try:
                u = urlparse(broker if '://' in broker else 'ws://' + broker)
                host_val = u.hostname or ''
                if u.port is not None:
                    ws_port_val = str(u.port)
                else:
                    ws_port_val = '443' if (u.scheme or '').lower() == 'wss' else '9001'
            except Exception:
                host_val, ws_port_val = None, None
        updates = {}
        if host_val:
            updates['SAMSUNG_TV_ART_MQTT_HOST'] = host_val
        if ws_port_val:
            updates['SAMSUNG_TV_ART_MQTT_WS_PORT'] = ws_port_val
        updates['SAMSUNG_TV_ART_MQTT_USERNAME'] = username
        updates['SAMSUNG_TV_ART_MQTT_PASSWORD'] = password
        return self._merge_overrides(updates)

    def _merge_overrides(self, updates):
        overrides_path = '/data/overrides.env'
        # Read existing overrides
        current = self._read_overrides()
        # Merge only allowed keys
        allowed = {
            'SAMSUNG_TV_ART_MAX_UPLOADS',
            'SAMSUNG_TV_ART_UPDATE_MINUTES',
            'SAMSUNG_TV_ART_TV_IP',
            # UI MQTT defaults persisted via overrides
            'SAMSUNG_TV_ART_MQTT_HOST',
            'SAMSUNG_TV_ART_MQTT_WS_PORT',
            'SAMSUNG_TV_ART_MQTT_USERNAME',
            'SAMSUNG_TV_ART_MQTT_PASSWORD',
        }
        for k, v in updates.items():
            if k in allowed and isinstance(v, str):
                current[k] = v
        # Write back
        try:
            os.makedirs('/data', exist_ok=True)
            with open(overrides_path, 'w', encoding='utf-8') as f:
                for k in sorted(current.keys()):
                    f.write(f"{k}={current[k]}\n")
            return True
        except Exception:
            return False

    def _handle_api_set_env(self):
        length = int(self.headers.get('Content-Length', '0') or '0')
        try:
            body = self.rfile.read(length).decode('utf-8') if length > 0 else '{}'
            data = json.loads(body or '{}')
        except Exception:
            self._json(400, {'ok': False, 'error': 'invalid json'})
            return
        # Basic validation/coercion
        updates = {}
        try:
            if 'SAMSUNG_TV_ART_MAX_UPLOADS' in data:
                updates['SAMSUNG_TV_ART_MAX_UPLOADS'] = str(int(data['SAMSUNG_TV_ART_MAX_UPLOADS']))
            if 'SAMSUNG_TV_ART_UPDATE_MINUTES' in data:
                updates['SAMSUNG_TV_ART_UPDATE_MINUTES'] = str(int(float(data['SAMSUNG_TV_ART_UPDATE_MINUTES'])))
            if 'SAMSUNG_TV_ART_TV_IP' in data:
                updates['SAMSUNG_TV_ART_TV_IP'] = str(data['SAMSUNG_TV_ART_TV_IP']).strip()
        except Exception:
            self._json(400, {'ok': False, 'error': 'validation failed'})
            return
        if not updates:
            self._json(400, {'ok': False, 'error': 'no updates'})
            return
        ok = self._merge_overrides(updates)
        if not ok:
            self._json(500, {'ok': False, 'error': 'failed to write overrides'})
            return
        self._json(200, {'ok': True})

    def _handle_api_restart(self):
        # Request graceful restart by signaling PID 1 (main process)
        try:
            os.kill(1, signal.SIGTERM)
            self._json(200, {'ok': True})
        except Exception as e:
            self._json(500, {'ok': False, 'error': str(e)})

    def do_GET(self):
        if self.path.startswith('/api/env'):
            return self._handle_api_get_env()
        if self.path.startswith('/api/ui-mqtt'):
            return self._json(200, self._read_ui_mqtt())
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith('/api/env'):
            return self._handle_api_set_env()
        if self.path.startswith('/api/restart'):
            return self._handle_api_restart()
        if self.path.startswith('/api/ui-mqtt'):
            length = int(self.headers.get('Content-Length', '0') or '0')
            try:
                body = self.rfile.read(length).decode('utf-8') if length > 0 else '{}'
                data = json.loads(body or '{}')
            except Exception:
                return self._json(400, {'ok': False, 'error': 'invalid json'})
            ok = self._write_ui_mqtt(data if isinstance(data, dict) else {})
            return self._json(200 if ok else 500, {'ok': ok})
        return super().do_POST()

    def do_OPTIONS(self):
        # Preflight CORS
        if self.path.startswith('/api/'):
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.end_headers()
            return
        return super().do_OPTIONS()
    def send_head(self):
        # Resolve the requested path to filesystem
        path = self.translate_path(self.path)
        # If directory, try index.html within; otherwise default to SPA index
        if os.path.isdir(path):
            for index in ("index.html", "index.htm"):
                index_path = os.path.join(path, index)
                if os.path.exists(index_path):
                    self.path = self.path.rstrip("/") + "/" + index
                    return SimpleHTTPRequestHandler.send_head(self)
            return self._send_index()
        # If file exists, serve normally
        if os.path.exists(path):
            return SimpleHTTPRequestHandler.send_head(self)
        # Fallback to SPA index for any non-existent path
        return self._send_index()

    def _send_index(self):
        try:
            with open(INDEX_PATH, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            # Write directly; we return None to indicate we've handled the response
            self.wfile.write(data)
            return None
        except Exception:
            # If index not found, fall back to 404
            self.send_error(404, "File not found")
            return None

    def log_message(self, format, *args):
        # Reduce noise; print to stderr but can be silenced by redirecting output
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format%args))


def main():
    port = int(os.environ.get("PORT", "8080"))
    Handler = functools.partial(FallbackHandler, directory=ROOT_DIR)
    httpd = ThreadingHTTPServer(("", port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
