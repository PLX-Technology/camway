#!/usr/bin/env python
"""Servidor de PRUEBA del panel web de CamWay (solo biblioteca estándar).

Imita lo justo de `api/app/main.py` para cargar el panel (`api/web`) en un
navegador con datos falsos, SIN instalar nada (ni FastAPI, ni Docker, ni
MediaMTX). No es un sustituto del servidor real: no hay vídeo, ni cámaras, ni
WhatsApp, ni detector. Todo vive en memoria; al reiniciar vuelve a empezar.

Uso (escucha SOLO en 127.0.0.1):

    python -I tools/mock_panel.py --port 8131 --role admin
    python -I tools/mock_panel.py --port 8131 --role viewer
    python -I tools/mock_panel.py --port 8131 --role seguridad
    python -I tools/mock_panel.py --port 8131 --role kiosk

Roles (igual que el servidor real):
  admin      todo.
  viewer     solo Mosaico y Grabaciones; ve las cámaras de 2 de los 3 equipos.
  seguridad  como viewer, pero gestiona los avisos (Ajustes → alertas).
  kiosk      sin sesión: /api/me dice «no autenticado» (el panel muestra el
             login), pero /live, /api/wall, fotogramas y /api/detections
             funcionan sin login (CAMWAY_KIOSK=1 desde localhost). Un login
             con cualquier clave entra como admin.

Con --role admin/viewer/seguridad ya hay «sesión»; «Salir» la cierra y
cualquier usuario/clave la reabre con ese mismo rol. La sesión es global del
servidor (una sola persona probando), no por navegador.

Los fixtures llevan TEXTOS HOSTILES a propósito (<img src=x onerror=alert(1)>
y "><svg onload=alert(2)>) en nombre de equipo, banner, vendor, error, título
de cámara, nombre de grabación, usuario y destinatario: si el panel los
interpreta como HTML, saltará alert(). Deben verse como texto.

Ruta solo del mock: POST/GET /__mock/mosaic-foreign simula que otro usuario armó el
mosaico con todas las cámaras (para ver el 403 de /whep/mosaico en un viewer).

WHEP/HLS responden 404 (no hay vídeo): el mosaico por defecto usa fotogramas
(--mosaic-mode frames); un favicon-512.png fijo hace de fotograma.
"""
from __future__ import annotations

import argparse
import copy
import json
import mimetypes
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_WEB = os.path.normpath(os.path.join(HERE, "..", "api", "web"))

H1 = "<img src=x onerror=alert(1)>"
H2 = '"><svg onload=alert(2)>'

COOKIE_NAME = "camway_session"
LOCK = threading.RLock()

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _mk_stream(dev_id: str, idx: int, path: str, url: str, codec: str, w, h,
               label: str, published: bool = True, audio: str | None = None,
               profile: str = "") -> dict:
    sid = f"{dev_id}__{idx}"
    return {
        "id": sid, "device_id": dev_id, "path": path, "url": url, "codec": codec,
        "width": w, "height": h, "profile": profile, "audio_codec": audio,
        "label": label, "published": 1 if published else 0, "mtx_name": sid,
    }


def _mk_device(host: str, port: int, **kw) -> dict:
    dev_id = f"{host}_{port}".replace(".", "-")
    d = {
        "id": dev_id, "host": host, "port": port, "vendor": "", "server": "",
        "kind": "camara", "name": "", "site": "", "bi_camera": "", "bi_server": "",
        "needs_auth": 0, "permissive": 0, "error": "", "auth_ok": 1,
        "last_seen": time.time(), "streams": [],
    }
    d.update(kw)
    return d


def build_fixtures() -> dict:
    # 1) NVR Dahua de 4 canales (limpio; Blue Iris emparejado para grabaciones).
    nvr = _mk_device("172.16.10.50", 554, vendor="Dahua", server="Dahua RTSP Server",
                     kind="nvr", name="NVR patio de tuberías", bi_camera="nvr_ch1",
                     needs_auth=1)
    nvr["streams"] = [
        _mk_stream(nvr["id"], i, f"/ch{i + 1}",
                   f"rtsp://172.16.10.50:554/cam/realmonitor?channel={i + 1}&subtype=1",
                   "H264", 704, 576, f"Canal {i + 1}", published=(i != 3),
                   audio="PCMA" if i == 0 else None, profile="Main")
        for i in range(4)
    ]
    # 2) Equipo con textos hostiles en nombre, banner, vendor y etiqueta.
    host = _mk_device("172.16.10.61", 554, vendor=H2, server=H2 + " RTSP/1.0 banner",
                      name=H1, needs_auth=1)
    host["streams"] = [
        _mk_stream(host["id"], 0, "/Streaming/Channels/101",
                   "rtsp://172.16.10.61:554/Streaming/Channels/101", "H265", 2560, 1440,
                   "principal " + H1, audio="MPEG4-GENERIC", profile="Main"),
        _mk_stream(host["id"], 1, "/Streaming/Channels/102",
                   "rtsp://172.16.10.61:554/Streaming/Channels/102", "H264", 640, 360,
                   "sub", profile="Baseline"),
    ]
    # 3) Hikvision sin nombre: el título de la teja sale de «host + etiqueta».
    hik = _mk_device("172.16.10.72", 554, vendor="Hikvision", server="Hikvision-Webs",
                     needs_auth=1)
    hik["streams"] = [
        _mk_stream(hik["id"], 0, "/h264/ch1/main/av_stream",
                   "rtsp://172.16.10.72:554/h264/ch1/main/av_stream", "H264", 1920, 1080,
                   "Entrada " + H1, audio="PCMU"),
        _mk_stream(hik["id"], 1, "/h264/ch1/sub/av_stream",
                   "rtsp://172.16.10.72:554/h264/ch1/sub/av_stream", "H264", 640, 360,
                   "Salida", published=True),
    ]
    # 4) Equipo que pide clave y no entrega vídeo; el error es hostil.
    auth = _mk_device("172.16.10.88", 554, vendor="HiLook", server="HiLook 1.0",
                      needs_auth=1, error=f"responde RTSP pero pide clave 401 {H1}")
    devices = [nvr, host, hik, auth]

    users = [
        {"id": 1, "username": "admin", "role": "admin", "created": time.time() - 9e6,
         "cameras": [], "pwd": "x"},
        {"id": 2, "username": "guardia", "role": "viewer", "created": time.time() - 5e6,
         "cameras": [nvr["id"], hik["id"]], "pwd": "x"},
        {"id": 3, "username": "jefe_seguridad", "role": "seguridad",
         "created": time.time() - 4e6, "cameras": [nvr["id"], host["id"], hik["id"]],
         "pwd": "x"},
        {"id": 4, "username": H2, "role": "viewer", "created": time.time() - 1e6,
         "cameras": [host["id"]], "pwd": "x"},
    ]
    recipients = [
        {"id": "r1", "name": "Jefe de turno", "phone": "584141234567", "enabled": True},
        {"id": "r2", "name": H1, "phone": "584249876543", "enabled": True},
        {"id": "r3", "name": "Guardia nocturno", "phone": "584161112233", "enabled": False},
    ]
    now = time.time()
    alert_log = [
        {"id": "a1", "stream_id": nvr["streams"][0]["id"], "ts": now - 600,
         "camera": nvr["name"], "cls": "head", "score": 0.91, "test": False,
         "status": "enviado"},
        {"id": "a2", "stream_id": host["streams"][0]["id"], "ts": now - 1500,
         "camera": host["name"], "cls": "fuego", "score": 0.77, "test": False,
         "status": "enviado"},
        {"id": "a3", "stream_id": hik["streams"][0]["id"], "ts": now - 3000,
         "camera": f"{hik['host']} Entrada", "cls": "humo", "score": 0.66,
         "test": True, "status": "error: sin WhatsApp"},
        {"id": "a4", "stream_id": host["streams"][1]["id"], "ts": now - 5000,
         "camera": H2, "cls": "head", "score": 0.58, "test": False,
         "status": "en cola"},
    ]
    alert_rules = {
        nvr["streams"][0]["id"]: {"enabled": True, "classes": ["head"], "min_score": 0.5,
                                   "min_hits": 3, "cooldown": 300},
        host["streams"][0]["id"]: {"enabled": True, "classes": ["fuego", "humo"],
                                    "min_score": 0.5, "min_hits": 3, "cooldown": 300},
    }
    rec_cfg = {
        host["id"]: {"mode": "hybrid", "cont_days": 3, "event_days": 7},
        hik["id"]: {"mode": "continuous", "cont_days": 2, "event_days": 7},
    }
    t0 = now - 7200
    local_files = {
        host["id"]: [
            {"kind": "event", "name": H1 + ".mp4", "size": 18_400_000, "mtime": now - 900,
             "stream": host["streams"][0]["id"]},
            {"kind": "cont", "name": "2026-11-13_10-00-00-000000.mp4",
             "size": 412_000_000, "mtime": t0, "stream": host["streams"][0]["id"]},
        ],
        hik["id"]: [
            {"kind": "cont", "name": "2026-11-13_09-00-00-000000.mp4",
             "size": 380_000_000, "mtime": t0 - 3600, "stream": hik["streams"][0]["id"]},
        ],
    }
    return {
        "devices": devices, "users": users, "recipients": recipients,
        "alert_log": alert_log, "alert_rules": alert_rules, "rec_cfg": rec_cfg,
        "local_files": local_files,
        "settings": {"cidr": "172.16.10.0/24", "ports": [554, 8554, 10554, 5554],
                     "channels": 8, "concurrency": 200, "connect_timeout": 1.2,
                     "logo": "", "site_name": "Taladro 7 (mock)"},
        "encoding": {"max_width": 1280, "max_height": 720, "max_fps": 15,
                     "max_bitrate": "", "preset": "ultrafast"},
        "credentials": [{"id": 1, "username": "admin", "label": "Hikvision", "password": "x"},
                        {"id": 2, "username": H1, "label": H2, "password": ""}],
        "layout": {"zones": [{"id": "default", "name": "Sin zona", "cams": []}]},
        "ai": {},
        "ptz": {},
        "next_user_id": 5,
        "next_cred_id": 3,
        "next_rec_id": 4,
        "mosaic": None,
    }


DEFAULT_RULE = {"enabled": False, "classes": ["head"], "min_score": 0.5,
                "min_hits": 3, "cooldown": 300}
LABELS = {"head": "Persona sin casco", "fuego": "Fuego", "humo": "Humo"}

# --------------------------------------------------------------------------
# Estado global
# --------------------------------------------------------------------------

ARGS = None            # argparse.Namespace
DATA = build_fixtures()
SESSION = {"user": None}      # usuario con sesión (dict) o None
ADMIN_USER = DATA["users"][0]


def role_user(role: str):
    by_role = {"admin": 1, "viewer": 2, "seguridad": 3}
    uid = by_role.get(role)
    if uid is None:
        return None
    return next(u for u in DATA["users"] if u["id"] == uid)


KIOSK_USER = {"id": -1, "username": "kiosko", "role": "viewer", "cameras": [],
              "_kiosk": True}


# --------------------------------------------------------------------------
# Utilidades de forma (imitan main.py)
# --------------------------------------------------------------------------


def strip_credentials(url: str) -> str:
    return re.sub(r"(rtsp://)[^/@]*@", r"\1", url or "")


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]", "_", name)


def device_view(dev: dict) -> dict:
    """Como `GET /api/devices`: streams sin credenciales, webrtc_ready, whep."""
    out = copy.deepcopy(dev)
    for s in out["streams"]:
        s["url"] = strip_credentials(s["url"])
        s["webrtc_ready"] = s["codec"] == "H264"
        if s["published"]:
            s["whep"] = f"/whep/{safe_name(s['mtx_name'])}"
    return out


def device_payload(dev: dict) -> dict:
    """Como `_device_payload` (eventos del escaneo y /api/probe): sin ids de stream."""
    return {
        "id": dev["id"], "host": dev["host"], "port": dev["port"],
        "vendor": dev["vendor"], "server": dev["server"], "kind": dev["kind"],
        "needs_auth": bool(dev["needs_auth"]), "permissive": bool(dev["permissive"]),
        "error": dev["error"],
        "streams": [{
            "path": s["path"], "url": strip_credentials(s["url"]), "codec": s["codec"],
            "width": s["width"], "height": s["height"], "profile": s["profile"],
            "audio_codec": s["audio_codec"], "label": s["label"],
            "webrtc_ready": s["codec"] == "H264",
        } for s in dev["streams"]],
    }


def find_device(device_id: str):
    return next((d for d in DATA["devices"] if d["id"] == device_id), None)


def find_stream(stream_id: str):
    for d in DATA["devices"]:
        for s in d["streams"]:
            if s["id"] == stream_id:
                return d, s
    return None, None


def user_allows(user, device_id: str) -> bool:
    if not user:
        return False
    if user["role"] == "admin" or user.get("_kiosk"):
        return True
    return device_id in set(user.get("cameras") or [])


def tile_title(dev: dict, s: dict) -> str:
    return dev["name"] or f'{dev["host"]} {s["label"]}'


# --------------------------------------------------------------------------
# Autenticación (copia de la lógica de auth_gate)
# --------------------------------------------------------------------------

PUBLIC_EXACT = {"/", "/tv", "/api/health", "/api/me", "/api/login", "/api/logout",
                "/api/setup"}
ADMIN_PREFIXES = ("/api/scan", "/api/onvif", "/api/probe", "/api/reprobe",
                  "/api/reconcile", "/api/devices", "/api/credentials",
                  "/api/blueiris", "/api/users")


def is_admin_path(path: str, method: str) -> bool:
    if path.startswith(ADMIN_PREFIXES):
        return True
    if path.endswith("/publish") or path.endswith("/optimize"):
        return True
    if path.startswith("/api/settings") and method in ("POST", "PUT", "DELETE"):
        return True
    if path.startswith("/api/recordings") and method in ("POST", "PUT", "DELETE"):
        return True
    if path == "/api/mosaic" and method == "DELETE":          # A2: apaga el mosaico de todos
        return True
    if path in ("/api/recordings/config", "/api/ptz/refresh"):   # A4: config completa / claves ONVIF
        return True
    return False


# --------------------------------------------------------------------------
# Escaneo y detecciones (SSE)
# --------------------------------------------------------------------------


class ScanSession:
    def __init__(self):
        self.cond = threading.Condition()
        self.events: list[dict] = []
        self.running = False

    def emit(self, ev: dict):
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()

    def run(self, kind: str):
        self.running = True
        with self.cond:
            self.events = []
        try:
            new_dev = _mk_device("172.16.10.90", 554, vendor="Uniview",
                                 server="Uniview RTSP Server " + H1)
            new_dev["streams"] = [_mk_stream(new_dev["id"], 0, "/media/video1",
                                             "rtsp://172.16.10.90:554/media/video1",
                                             "H264", 1280, 720, "main", published=False)]
            total = 254
            stage1, stage2 = ("barrido", "huella") if kind == "scan" else \
                ("onvif-barrido", "onvif")
            for done in range(0, total + 1, 51):
                time.sleep(0.35)
                self.emit({"stage": stage1, "done": min(done, total), "total": total,
                           "found": 0, "message": f"172.16.10.{min(done, 254)}"})
            time.sleep(0.3)
            self.emit({"stage": stage2, "done": 1, "total": 1, "found": 1,
                       "message": new_dev["host"], "device": device_payload(new_dev),
                       "saved": True})
            with LOCK:
                if not find_device(new_dev["id"]):
                    DATA["devices"].append(new_dev)
            time.sleep(0.3)
            msg = "1 equipos con video" if kind == "scan" else \
                "ONVIF: 1 cámaras nuevas con ruta RTSP"
            self.emit({"stage": "fin", "message": msg})
        finally:
            self.running = False


SCAN = ScanSession()
STOP_FLAG = threading.Event()


class DetectionHub:
    def __init__(self):
        self.cond = threading.Condition()
        self.seq = 0
        self.last = None

    def publish(self, ev):
        with self.cond:
            self.seq += 1
            self.last = ev
            self.cond.notify_all()


DET = DetectionHub()


def detection_ticker():
    n = 0
    while not STOP_FLAG.wait(8.0):
        streams = [s for d in DATA["devices"] for s in d["streams"] if s["published"]]
        if not streams:
            continue
        s = streams[n % len(streams)]
        n += 1
        DET.publish({"stream_id": s["id"], "ts": time.time(), "boxes": [
            {"cls": "head", "score": 0.82, "x": 0.35, "y": 0.2, "w": 0.18, "h": 0.3}]})


# --------------------------------------------------------------------------
# Manejador HTTP
# --------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CamWayMock/1.0"

    # --- salida -----------------------------------------------------------
    def log_message(self, fmt, *args):
        if ARGS and ARGS.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, obj, status: int = 200, extra: dict | None = None):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json", extra)

    def error(self, status: int, detail: str):
        self.json({"detail": detail}, status)

    def body_json(self) -> dict:
        raw = self.body_raw()
        if not raw:
            return {}
        try:
            v = json.loads(raw.decode("utf-8"))
            return v if isinstance(v, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def body_raw(self) -> bytes:
        """Lee (una sola vez) el cuerpo según Content-Length. Si un manejador no lo
        lee, `_drain_body` lo descarta al final: con HTTP/1.1 keep-alive un cuerpo
        sin leer queda en el socket y la petición siguiente llega corrupta."""
        n, self._unread = self._unread, 0
        return self.rfile.read(n) if n else b""

    def _drain_body(self):
        if self._unread:
            n, self._unread = self._unread, 0
            try:
                self.rfile.read(n)
            except OSError:
                pass

    # --- despacho ---------------------------------------------------------
    def do_GET(self): self._dispatch("GET")
    def do_POST(self): self._dispatch("POST")
    def do_PUT(self): self._dispatch("PUT")
    def do_PATCH(self): self._dispatch("PATCH")
    def do_DELETE(self): self._dispatch("DELETE")
    def do_HEAD(self): self._dispatch("GET")

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain", {
            "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "*",
            "Access-Control-Allow-Headers": "*"})

    def _dispatch(self, method: str):
        try:
            self._unread = max(0, int(self.headers.get("Content-Length") or 0))
        except ValueError:
            self._unread = 0
        try:
            self._dispatch_inner(method)
        finally:
            self._drain_body()          # POST/PUT/PATCH/DELETE/401/403/404: siempre

    def _dispatch_inner(self, method: str):
        u = urlparse(self.path)
        path = u.path
        self.query = {k: v[-1] for k, v in parse_qs(u.query).items()}
        try:
            if path.startswith("/static/"):
                return self.serve_static(path[len("/static/"):])
            if path in ("/", "/tv", "/live", "/nvr") and method == "GET":
                page = {"/": "index.html", "/tv": "tv.html", "/live": "live.html",
                        "/nvr": "nvr.html"}[path]
                return self.serve_file(os.path.join(ARGS.web, page), nocache=True)

            if path == "/__mock/mosaic-foreign" and method in ("GET", "POST"):
                # Solo del mock: simula que OTRO usuario (admin) armó el mosaico con
                # TODAS las cámaras, para probar cómo reacciona el panel al 403 de
                # /whep/mosaico (A2) sin necesitar dos sesiones.
                names = [safe_name(x["mtx_name"]) for dv in DATA["devices"]
                         for x in dv["streams"] if x["published"]]
                DATA["mosaic"] = {"layout": 16, "page": 0, "names": names[:16]}
                return self.json({"ok": True, "names": DATA["mosaic"]["names"]})

            user = self.gate(path, method)
            self.user = user
            self.route(method, path)
        except ApiError as e:
            self.error(e.status, e.detail)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[mock] error en {method} {path}: {e!r}\n")
            try:
                self.error(500, f"error del mock: {e.__class__.__name__}: {e}")
            except OSError:
                pass

    def gate(self, path: str, method: str):
        if path in PUBLIC_EXACT:
            return SESSION["user"]
        if path.startswith("/api/detector") or path.startswith("/bi/"):
            return None                       # interno (el mock solo recibe localhost)
        user = SESSION["user"]
        if not user and ARGS.role == "kiosk" and not is_admin_path(path, method):
            user = KIOSK_USER
        if not user:
            raise ApiError(401, "no autenticado")
        if is_admin_path(path, method) and user["role"] != "admin":
            if not (user["role"] == "seguridad" and path.startswith("/api/settings/alerts")):
                raise ApiError(403, "requiere administrador")
        return user

    # --- estáticos --------------------------------------------------------
    def serve_static(self, rel: str):
        rel = unquote(rel)
        full = os.path.normpath(os.path.join(ARGS.web, rel))
        if not (full == ARGS.web or full.startswith(ARGS.web + os.sep)) or not os.path.isfile(full):
            return self.error(404, "Not Found")
        self.serve_file(full, nocache=True)

    def serve_file(self, full: str, nocache: bool = False):
        if not os.path.isfile(full):
            return self.error(404, "Not Found")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith(".js"):
            ctype = "application/javascript"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            data = fh.read()
        self._send(200, data, ctype, {"Cache-Control": "no-cache"} if nocache else None)

    def frame_png(self):
        path = os.path.join(ARGS.web, "favicon-512.png")
        with open(path, "rb") as fh:
            data = fh.read()
        self._send(200, data, "image/png", {"Cache-Control": "no-store"})

    # --- rutas ------------------------------------------------------------
    def route(self, method: str, path: str):
        user = self.user
        d = DATA
        q = self.query

        def m(pattern: str):
            return re.fullmatch(pattern, path)

        # ---- sesión y usuarios
        if path == "/api/me" and method == "GET":
            s = d["settings"]
            base = {"logo": s["logo"], "site": s["site_name"], "setup_needed": False}
            u = SESSION["user"]
            if not u:
                return self.json({"authenticated": False, **base})
            return self.json({"authenticated": True, **base,
                              "user": {"id": u["id"], "username": u["username"],
                                       "role": u["role"], "cameras": u["cameras"]}})
        if path == "/api/setup" and method == "POST":
            raise ApiError(409, "el sistema ya tiene usuarios")
        if path == "/api/login" and method == "POST":
            body = self.body_json()
            who = ARGS.role if ARGS.role != "kiosk" else "admin"
            SESSION["user"] = role_user(who)
            return self.json({"ok": True, "role": who,
                              "username": (body.get("username") or "").strip()},
                             extra={"Set-Cookie": f"{COOKIE_NAME}=mock; Path=/; HttpOnly; SameSite=Lax"})
        if path == "/api/logout" and method == "POST":
            SESSION["user"] = None
            return self.json({"ok": True},
                             extra={"Set-Cookie": f"{COOKIE_NAME}=; Path=/; Max-Age=0"})

        if path == "/api/users":
            if method == "GET":
                return self.json({"users": [self._user_view(x) for x in d["users"]]})
            if method == "POST":
                b = self.body_json()
                name = (b.get("username") or "").strip()
                if not name or len(b.get("password") or "") < 4:
                    raise ApiError(400, "usuario y contraseña (≥4) obligatorios")
                if any(x["username"] == name for x in d["users"]):
                    raise ApiError(409, "ese usuario ya existe")
                with LOCK:
                    uid = d["next_user_id"]
                    d["next_user_id"] += 1
                    d["users"].append({"id": uid, "username": name,
                                       "role": b.get("role", "viewer"),
                                       "created": time.time(),
                                       "cameras": list(b.get("cameras") or []), "pwd": "x"})
                return self.json({"ok": True, "id": uid})
        mu = m(r"/api/users/(\d+)")
        if mu:
            uid = int(mu.group(1))
            target = next((x for x in d["users"] if x["id"] == uid), None)
            if method == "PUT":
                if not target:
                    raise ApiError(404, "usuario desconocido")
                b = self.body_json()
                if b.get("role") in ("admin", "viewer", "seguridad"):
                    target["role"] = b["role"]
                if b.get("cameras") is not None:
                    target["cameras"] = list(b["cameras"])
                return self.json({"ok": True})
            if method == "DELETE":
                if user and user["id"] == uid:
                    raise ApiError(400, "no puedes borrar tu propio usuario")
                admins = [x for x in d["users"] if x["role"] == "admin"]
                if target and target["role"] == "admin" and len(admins) <= 1:
                    raise ApiError(400, "debe quedar al menos un administrador")
                d["users"] = [x for x in d["users"] if x["id"] != uid]
                return self.json({"ok": True})

        # ---- salud
        if path == "/api/health" and method == "GET":
            devs = d["devices"]
            return self.json({
                "ok": True, "mediamtx": True, "scanning": SCAN.running,
                "devices": len(devs), "detector": ARGS.detector == "on",
                "disk_free_pct": 63.4, "last_backup_age_s": 5400.0, "cameras_stale": 0})
        if path == "/api/system/health" and method == "GET":
            self._require_admin()
            return self.json(self._sysmon())
        if path == "/api/reconcile" and method == "POST":
            return self.json({"restauradas": 7, "fallos": 0})

        # ---- escaneo
        if path == "/api/scan" and method == "POST":
            return self._start_scan("scan")
        if path == "/api/onvif-scan" and method == "POST":
            return self._start_scan("onvif")
        if path == "/api/scan/stop" and method == "POST":
            return self.json({"stopped": False})
        if path == "/api/scan/events" and method == "GET":
            return self._sse_scan()
        if path == "/api/probe" and method == "POST":
            b = self.body_json()
            host = b.get("host")
            if not host:
                raise ApiError(400, "falta host")
            dev = _mk_device(host, int(b.get("port", 554)), vendor="Genérico (mock)",
                             server="mock RTSP")
            dev["streams"] = [_mk_stream(dev["id"], 0, "/stream1",
                                         f"rtsp://{host}:554/stream1", "H264", 1280, 720,
                                         "main", published=False)]
            return self.json(device_payload(dev))
        if path == "/api/onvif" and method == "GET":
            return self.json({"devices": [
                {"address": "172.16.10.101", "name": "ONVIF " + H1, "hardware": "DS-2CD",
                 "xaddrs": ["http://172.16.10.101/onvif/device_service"]}],
                "nota": "el multicast no cruza subredes ni suele llegar dentro de Docker"})
        if path == "/api/reprobe" and method == "POST":
            return self.json({"probados": 1, "recuperados": 0, "hosts": []})

        # ---- inventario
        if path == "/api/devices":
            if method == "GET":
                return self.json({"devices": [device_view(x) for x in d["devices"]]})
            if method == "DELETE":
                n = len(d["devices"])
                d["devices"] = []
                return self.json({"ok": True, "removed": n})
        if path == "/api/devices/manual" and method == "POST":
            b = self.body_json()
            if not b.get("host") or not b.get("url"):
                raise ApiError(400, "faltan 'host' y 'url'")
            return self.json({"ok": True, "device_id": safe_name(b["host"]), "published": False,
                              "error": "MediaMTX no existe en el mock"})
        if path == "/api/devices/add-nvr" and method == "POST":
            b = self.body_json()
            if not (b.get("host") or "").strip():
                raise ApiError(400, "falta la IP del NVR")
            n = int(b.get("channels") or 4)
            return self.json({"ok": True, "device_id": "mock", "channels": n,
                              "published": n,
                              "detalle": [{"canal": i + 1, "estado": "ok", "codec": "H264",
                                           "res": "704x576"} for i in range(n)]})
        md = m(r"/api/devices/([^/]+)")
        if md and md.group(1) not in ("manual", "add-nvr"):
            did = md.group(1)
            dev = find_device(did)
            if method == "PATCH":
                if not dev:
                    raise ApiError(404, "equipo desconocido")
                b = self.body_json()
                dev["name"] = b.get("name", "")
                if "site" in b:
                    dev["site"] = b.get("site", "")
                return self.json({"ok": True})
            if method == "DELETE":
                if not dev:
                    raise ApiError(404, "equipo desconocido")
                d["devices"] = [x for x in d["devices"] if x["id"] != did]
                return self.json({"ok": True})
        md = m(r"/api/devices/([^/]+)/(credential|quality|ptz-config|osd)")
        if md:
            did, what = md.group(1), md.group(2)
            dev = find_device(did)
            if what == "credential" and method == "POST":
                if not dev:
                    raise ApiError(404, "equipo desconocido")
                b = self.body_json()
                if not (b.get("username") or "").strip():
                    raise ApiError(400, "falta usuario")
                ok = (b.get("username") or "").strip() == "admin"
                if ok and not dev["streams"]:
                    dev["streams"] = [_mk_stream(did, 0, "/stream1", f"rtsp://{dev['host']}:554/stream1",
                                                 "H264", 1280, 720, "main", published=False)]
                    dev["error"] = ""
                res = device_payload(dev)
                if ok:
                    res["streams"] = [dict(s, id=dev["streams"][i]["id"])
                                      for i, s in enumerate(res["streams"])]
                res["recovered"] = ok
                return self.json(res)
            if what == "quality" and method == "PUT":
                return self.json({"ok": True, "quality": None})
            if what == "ptz-config":
                if method == "GET":
                    cfg = d["ptz"].get(did)
                    return self.json({"enabled": False, "method": "onvif"} if not cfg else
                                     {"enabled": True, "method": "onvif", "host": cfg.get("host", ""),
                                      "user": cfg.get("user", ""), "zoom": True, "fixed": False})
                if method == "PUT":
                    b = self.body_json()
                    if not b.get("enabled"):
                        d["ptz"].pop(did, None)
                        return self.json({"enabled": False})
                    d["ptz"][did] = {"host": b.get("host") or (dev or {}).get("host", ""),
                                     "user": b.get("user") or ""}
                    return self.json({"enabled": True, "method": "onvif",
                                      "host": d["ptz"][did]["host"], "zoom": True})
            if what == "osd" and method == "POST":
                return self.json({"ok": True, "osd": {"method": "isapi"},
                                  "time": {"method": "isapi"}, "errors": []})

        # ---- publicar / streams
        ms = m(r"/api/streams/([^/]+)/publish")
        if ms:
            dev, s = find_stream(ms.group(1))
            if not s:
                raise ApiError(404, "stream desconocido")
            if method == "POST":
                s["published"] = 1
                name = s["mtx_name"]
                return self.json({"ok": True, "name": name, "transcode": s["codec"] != "H264",
                                  "whep": f"/whep/{name}",
                                  "hls": f"http://127.0.0.1:8888/{name}/index.m3u8"})
            if method == "DELETE":
                s["published"] = 0
                return self.json({"ok": True})
        ms = m(r"/api/streams/([^/]+)/(ai|ptz|optimize|snapshot)")
        if ms:
            sid, what = ms.group(1), ms.group(2)
            dev, s = find_stream(sid)
            if not s:
                raise ApiError(404, "stream desconocido")
            self._require_camera(dev["id"])
            if what == "ai":
                if method == "GET":
                    return self.json(d["ai"].get(sid) or {
                        "enabled": False, "target": "vehiculo", "min_score": 0.4,
                        "action": "full", "zone": None, "schedule": None})
                if method == "PUT":
                    b = self.body_json()
                    d["ai"][sid] = {"user_id": user["id"], "stream_id": sid,
                                    "enabled": bool(b.get("enabled")),
                                    "target": b.get("target", "vehiculo"),
                                    "min_score": float(b.get("min_score", 0.4)),
                                    "action": b.get("action", "full"),
                                    "zone": b.get("zone") or None,
                                    "schedule": b.get("schedule") or None}
                    return self.json({"ok": True})
            if what == "ptz" and method == "GET":
                return self.json({"ok": True})
            if what == "optimize" and method == "POST":
                return self.json({"ok": True, "configs": []})
            if what == "snapshot" and method == "GET":
                return self.frame_png()
        if path == "/api/ai-configs" and method == "GET":
            return self.json({"configs": d["ai"]})
        if path == "/api/layout":
            if method == "GET":
                return self.json(d["layout"])
            if method == "PUT":
                b = self.body_json()
                if not isinstance(b.get("zones"), list):
                    raise ApiError(400, "layout inválido: falta 'zones'")
                d["layout"] = {"zones": b["zones"]}
                return self.json({"ok": True})
        if path == "/api/ptz/refresh" and method == "POST":
            return self.json({"ptz": {}})
        if path == "/api/relay" and method == "GET":
            return self.json({"jobs": []})
        if path == "/api/blueiris/cameras" and method == "GET":
            return self.json({"cameras": [], "configured": False})

        # ---- muro y mosaico
        if path == "/api/wall" and method == "GET":
            return self.json(self._wall())
        mw = m(r"/api/wall/([^/]+)/frame")
        if mw and method == "GET":
            dev, s = find_stream(mw.group(1))
            if not s:
                raise ApiError(404, "stream desconocido")
            self._require_camera(dev["id"])
            return self.frame_png()
        if path == "/api/mosaic":
            if method == "POST":
                return self._mosaic_set()
            if method == "DELETE":
                self._require_admin()
                d["mosaic"] = None
                return self.json({"ok": True})
        if path.startswith("/whep/"):
            return self._whep(method, path)

        # ---- detecciones
        if path == "/api/detections" and method == "GET":
            return self._sse_detections()
        if path == "/api/detector/targets" and method == "GET":
            return self.json({"targets": []})
        if path == "/api/detector/events" and method == "POST":
            self.body_raw()
            return self.json({"ok": True})
        if path.startswith("/api/detector/frame/") and method == "POST":
            self.body_raw()
            return self.json({"ok": True})

        # ---- grabaciones
        if path == "/api/recordings/devices" and method == "GET":
            return self.json(self._rec_devices())
        if path == "/api/recordings/config" and method == "GET":
            self._require_admin()
            return self.json({"recording": d["rec_cfg"]})
        mr = m(r"/api/recordings/([^/]+)/(clips|thumb|video\.mp4|file|config)")
        if mr:
            did, what = mr.group(1), mr.group(2)
            if what == "config" and method == "PUT":
                dev = find_device(did)
                if not dev:
                    raise ApiError(404, "equipo desconocido")
                b = self.body_json()
                if not b.get("enabled"):
                    d["rec_cfg"].pop(did, None)
                else:
                    d["rec_cfg"][did] = {"mode": b.get("mode", "hybrid"),
                                         "cont_days": max(1, int(b.get("cont_days", 3))),
                                         "event_days": max(1, int(b.get("event_days", 7)))}
                return self.json({"ok": True, "config": d["rec_cfg"].get(did)})
            if method == "GET":
                dev = find_device(did)
                if what in ("clips", "thumb", "video.mp4"):
                    if not dev or not dev.get("bi_camera"):
                        raise ApiError(404, "cámara sin grabaciones")
                self._require_camera(did)
                if what == "clips":
                    now = time.time()
                    return self.json({"clips": [
                        {"path": f"{dev['bi_camera']}.20261113_{h:02d}0000.bvr",
                         "date": now - 3600 * (k + 1), "size": f"00:0{k + 1}:12 ({3 + k}.2MB)",
                         "flags": 2 if k == 0 else 0}
                        for k, h in enumerate((9, 8, 7))], "camera": tile_title(dev, dev["streams"][0])})
                if what == "thumb":
                    return self.frame_png()
                raise ApiError(404, "el mock no sirve vídeo")
        mr = m(r"/api/recordings/([^/]+)")
        if mr and method == "GET":
            did = mr.group(1)
            self._require_camera(did)
            files = sorted(d["local_files"].get(did, []), key=lambda x: -x["mtime"])
            cfg = d["rec_cfg"].get(did)
            return self.json({"recordings": files, "config": cfg})

        # ---- credenciales y ajustes
        if path == "/api/credentials":
            if method == "GET":
                return self.json({"credentials": [
                    {"id": c["id"], "username": c["username"], "label": c["label"],
                     "has_password": bool(c["password"])} for c in d["credentials"]]})
            if method == "POST":
                b = self.body_json()
                if not (b.get("username") or "").strip():
                    raise ApiError(400, "falta usuario")
                cid = d["next_cred_id"]
                d["next_cred_id"] += 1
                d["credentials"].append({"id": cid, "username": b["username"].strip(),
                                         "label": b.get("label", ""),
                                         "password": b.get("password", "")})
                return self.json({"id": cid})
        mc = m(r"/api/credentials/(\d+)")
        if mc and method == "DELETE":
            d["credentials"] = [c for c in d["credentials"] if c["id"] != int(mc.group(1))]
            return self.json({"ok": True})

        if path == "/api/settings":
            if method == "GET":
                return self.json(self._settings_view())
            if method == "POST":
                b = self.body_json()
                s = d["settings"]
                for k in ("cidr", "ports", "channels", "concurrency", "connect_timeout", "logo"):
                    if k in b:
                        s[k] = b[k]
                if "site_name" in b:
                    s["site_name"] = " ".join(str(b["site_name"] or "").split())[:60]
                return self.json(dict(s))
        if path == "/api/settings/encoding":
            if method == "GET":
                self._require_admin()                 # A4: solo admin
                return self.json({"encoding": dict(d["encoding"]),
                                  "disk": {"total": 500_000_000_000, "free": 310_000_000_000,
                                           "used": 190_000_000_000},
                                  "recording_cameras": len(d["rec_cfg"])})
            if method == "PUT":
                b = self.body_json()
                for k in ("max_width", "max_height", "max_fps", "max_bitrate", "preset"):
                    if k in b:
                        d["encoding"][k] = b[k]
                return self.json({"encoding": dict(d["encoding"])})

        # ---- alertas y WhatsApp
        if path == "/api/settings/alerts" and method == "GET":
            return self.json(self._alerts_view())
        if path == "/api/settings/whatsapp" and method == "GET":
            self._require_admin()
            return self.json({"configured": True, "state": "open",
                              "number": "584141112222", "name": "CamWay " + H1})
        if path == "/api/settings/whatsapp/link" and method == "POST":
            self._require_admin()
            b = self.body_json()
            if b.get("phone"):
                return self.json({"state": "connecting", "qr": "", "pairing_code": "ABCD1234"})
            qr = ("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='160' "
                  "height='160'><rect width='160' height='160' fill='white'/><rect x='10' y='10' "
                  "width='50' height='50'/><rect x='100' y='10' width='50' height='50'/>"
                  "<rect x='10' y='100' width='50' height='50'/></svg>")
            return self.json({"state": "connecting", "qr": qr, "pairing_code": ""})
        if path == "/api/settings/whatsapp/unlink" and method == "POST":
            self._require_admin()
            return self.json({"ok": True, "state": "close"})
        if path.startswith("/api/settings/alerts/recipients"):
            self._require_alert_manager()
        if path == "/api/settings/alerts/recipients" and method == "POST":
            b = self.body_json()
            phone = "".join(ch for ch in str(b.get("phone") or "") if ch.isdigit())
            if not (10 <= len(phone) <= 15):
                raise ApiError(400, "número inválido: usa código de país (ej. 58 414 1234567)")
            if any(r["phone"] == phone for r in d["recipients"]):
                raise ApiError(400, "ese número ya está en la lista")
            with LOCK:
                d["next_rec_id"] += 1
                rec = {"id": f"r{d['next_rec_id']}", "name": (b.get("name") or "").strip()[:40] or phone,
                       "phone": phone, "enabled": True}
                d["recipients"].append(rec)
            return self.json({"recipient": rec, "warning": None})
        mrc = m(r"/api/settings/alerts/recipients/([^/]+)(/test)?")
        if mrc:
            rid, test = mrc.group(1), mrc.group(2)
            rec = next((r for r in d["recipients"] if r["id"] == rid), None)
            if test and method == "POST":
                if not rec:
                    raise ApiError(404, "destinatario desconocido")
                return self.json({"ok": True})
            if method == "PUT":
                if not rec:
                    raise ApiError(404, "destinatario desconocido")
                b = self.body_json()
                if "enabled" in b:
                    rec["enabled"] = bool(b["enabled"])
                if "name" in b:
                    rec["name"] = str(b["name"]).strip()[:40] or rec["phone"]
                return self.json({"recipient": rec})
            if method == "DELETE":
                d["recipients"] = [r for r in d["recipients"] if r["id"] != rid]
                return self.json({"ok": True})
        if path == "/api/settings/alerts/test" and method == "POST":
            b = self.body_json()
            dev, s = find_stream(b.get("stream_id") or "")
            if not s:
                raise ApiError(404, "stream desconocido")
            self._require_camera(dev["id"])
            ch = self._channels()
            return self.json({"ok": True, "id": "test1", "channels": ch})
        ma = m(r"/api/settings/alerts/([^/]+)")
        if ma and method == "PUT":
            sid = unquote(ma.group(1))
            dev, s = find_stream(sid)
            if not s:
                raise ApiError(404, "stream desconocido")
            self._require_camera(dev["id"])
            b = self.body_json()
            cur = dict(DEFAULT_RULE)
            cur.update(d["alert_rules"].get(sid, {}))
            if "enabled" in b:
                cur["enabled"] = bool(b["enabled"])
            if "classes" in b:
                cur["classes"] = [c for c in b["classes"] if c in LABELS] or ["head"]
            if "min_score" in b:
                cur["min_score"] = min(0.95, max(0.2, float(b["min_score"])))
            if "min_hits" in b:
                cur["min_hits"] = min(10, max(1, int(b["min_hits"])))
            if "cooldown" in b:
                cur["cooldown"] = min(3600, max(30, int(b["cooldown"])))
            d["alert_rules"][sid] = cur
            return self.json({"rule": cur})

        raise ApiError(404, f"ruta no implementada en el mock: {method} {path}")

    # --- piezas -----------------------------------------------------------
    def _require_admin(self):
        if self.user["role"] != "admin":
            raise ApiError(403, "requiere administrador")

    def _settings_view(self) -> dict:
        """A4: igual que main.py. Mismas claves siempre (app.js hace s.ports.join);
        a un no-admin se le vacían los valores de red."""
        s = DATA["settings"]
        if self.user["role"] == "admin":
            return dict(s)
        return {"logo": s["logo"], "site_name": s["site_name"], "cidr": "", "ports": [],
                "channels": None, "concurrency": None, "connect_timeout": None}

    def _sees_all(self) -> bool:
        return self.user["role"] == "admin" or bool(self.user.get("_kiosk"))

    def _require_alert_manager(self):
        if self.user["role"] not in ("admin", "seguridad"):
            raise ApiError(403, "requiere administrador o jefe de seguridad")

    def _require_camera(self, device_id: str):
        if not user_allows(self.user, device_id):
            raise ApiError(403, "sin permiso sobre esa cámara")

    @staticmethod
    def _user_view(u: dict) -> dict:
        return {"id": u["id"], "username": u["username"], "role": u["role"],
                "created": u["created"], "cameras": list(u["cameras"] or [])}

    def _channels(self) -> dict:
        n = sum(1 for r in DATA["recipients"] if r.get("enabled", True))
        return {"whatsapp": bool(n), "email": False, "group": False}

    def _alerts_view(self) -> dict:
        """Como `get_alert_settings` de main.py (A1): admin y seguridad ven los
        destinatarios; el resto no. Reglas e historial, solo de sus cámaras
        (admin: todo)."""
        d = DATA
        manager = self.user["role"] in ("admin", "seguridad")
        cams = {}
        for sid, rule in d["alert_rules"].items():
            dev, s = find_stream(sid)
            if s and user_allows(self.user, dev["id"]):
                cams[sid] = dict(DEFAULT_RULE, **rule)

        def visible(e: dict) -> bool:
            if self.user["role"] == "admin":
                return True
            dev, s = find_stream(e.get("stream_id") or "")
            return bool(s) and user_allows(self.user, dev["id"])

        return {
            "channels": self._channels(), "site": d["settings"]["site_name"],
            "recipients": [dict(r) for r in d["recipients"]] if manager else [],
            "cameras": cams, "defaults": DEFAULT_RULE, "labels": LABELS,
            "pending": 1 if manager else 0,
            "log": [dict(e) for e in d["alert_log"] if visible(e)][:30],
            "wa_state": "open" if manager else "",
        }

    def _wall(self) -> dict:
        tiles = []
        for dev in DATA["devices"]:
            if not user_allows(self.user, dev["id"]):
                continue
            for s in dev["streams"]:
                if not s["published"]:
                    continue
                primary = safe_name(s["mtx_name"])
                tiles.append({
                    "stream_id": s["id"], "device_id": dev["id"],
                    "title": tile_title(dev, s), "host": dev["host"],
                    "vendor": dev["vendor"], "codec": s["codec"],
                    "width": s["width"], "height": s["height"],
                    "whep": f"/whep/{primary}", "whep_hi": None,
                    "hls": f"http://127.0.0.1:8888/{primary}/index.m3u8",
                    "hls_hi": f"http://127.0.0.1:8888/{primary}/index.m3u8",
                    "rtsp": f"rtsp://127.0.0.1:8554/{primary}",
                    "rtsp_hi": f"rtsp://127.0.0.1:8554/{primary}",
                    "frame": f"/api/wall/{s['id']}/frame",
                    "audio": bool(s["audio_codec"]),
                    "ptz": dev["id"] in DATA["ptz"] or dev["kind"] == "nvr",
                    "zoom": dev["kind"] == "nvr",
                    "talk": bool(s["audio_codec"]),
                    "rec": dev["id"] in DATA["rec_cfg"],
                    "ready": True, "readers": 0})
        return {"tiles": tiles, "mediamtx": True, "fps": 1.5,
                "mosaic_default": ARGS.mosaic_mode}

    def _published_cams(self) -> list:
        out = []
        for dev in DATA["devices"]:
            if not user_allows(self.user, dev["id"]):
                continue
            for s in dev["streams"]:
                if s["published"]:
                    name = safe_name(s["mtx_name"])
                    out.append({"name": name, "whep": f"/whep/{name}",
                                "title": tile_title(dev, s), "host": dev["host"]})
        return out

    def _mosaic_set(self):
        b = self.body_json()
        cams = self._published_cams()
        if not cams and not self._sees_all():
            raise ApiError(403, "no tienes cámaras asignadas")
        try:
            layout = int(b.get("layout") or 4)
            page = int(b.get("page") or 0)
        except (TypeError, ValueError):
            layout, page = 4, 0
        if layout not in (1, 4, 9, 16):
            layout = 4
        import math
        cols = int(round(math.sqrt(layout))) if layout > 1 else 1
        rows = math.ceil(layout / cols)
        pages = max(1, math.ceil(len(cams) / layout)) if cams else 1
        page = max(0, min(page, pages - 1))
        # El mosaico es UNO para todos (un ffmpeg): lo que arma este usuario es lo
        # que verá cualquiera que lo lea; /whep/mosaico decide quién puede (A2).
        DATA["mosaic"] = {"layout": layout, "page": page,
                          "names": [c["name"] for c in cams[page * layout: page * layout + layout]]}
        info = {"whep": "/whep/mosaico", "layout": layout, "page": page, "pages": pages,
                "cols": cols, "rows": rows, "per_page": layout,
                "cells": cams[page * layout: page * layout + layout], "total": len(cams)}
        return self.json(info)

    def _whep(self, method: str, path: str):
        if method == "DELETE" and path.startswith("/whep/x/"):
            self.body_raw()
            return self._send(204, b"", "text/plain")
        self.body_raw()
        if path == "/whep/mosaico" and not self._sees_all():
            m = DATA["mosaic"]
            if not m or not m["names"]:
                return self.error(404, "mosaico no armado")
            for n in m["names"]:
                _dev, s = find_stream(n)
                if not s or not user_allows(self.user, _dev["id"]):      # falla cerrado
                    return self.error(403, "el mosaico actual incluye cámaras que no tienes asignadas")
        return self.error(404, "sin vídeo en el servidor de prueba (WHEP no disponible)")

    def _rec_devices(self) -> dict:
        out = []
        for dev in DATA["devices"]:
            if not user_allows(self.user, dev["id"]):
                continue
            pub = next((s for s in dev["streams"] if s["published"]), None)
            thumb = f"/api/wall/{pub['id']}/frame" if pub else ""
            name = dev["name"] or dev["host"]
            if dev["id"] in DATA["rec_cfg"]:
                out.append({"device_id": dev["id"], "name": name, "source": "local",
                            "thumb": thumb})
            elif dev.get("bi_camera"):
                out.append({"device_id": dev["id"], "name": name, "source": "blueiris",
                            "bi_camera": dev["bi_camera"], "thumb": thumb})
        return {"devices": out, "available": True}

    @staticmethod
    def _sysmon() -> dict:
        return {
            "ok": True, "status": "warn", "issues": ["Disco al 87 %"],
            "cpu": {"pct": 23.5, "cores": 8, "level": "ok", "load": [0.8, 0.7, 0.6]},
            "ram": {"pct": 51.2, "used_gb": 8.2, "total_gb": 16.0, "level": "ok"},
            "disk": {"pct": 87.0, "used_gb": 435, "total_gb": 500, "free_gb": 65, "level": "warn"},
            "net": {"down_mbps": 12.4, "up_mbps": 3.1}, "uptime_s": 987654,
            "sensors": [{"name": "CPU " + H1, "kind": "cpu", "c": 61.0, "level": "ok"},
                        {"name": "NVMe", "kind": "disk", "c": 47.0, "level": "ok"}],
            "containers": [{"name": "api", "cpu": 3.2, "mem_mb": 210},
                           {"name": "mediamtx", "cpu": 11.8, "mem_mb": 340}],
            "url": "http://127.0.0.1:19999"}

    def _start_scan(self, kind: str):
        b = self.body_json()
        if SCAN.running:
            raise ApiError(409, "ya hay un escaneo en curso")
        cidr = b.get("cidr") or DATA["settings"]["cidr"]
        if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?", cidr):
            raise ApiError(400, f"segmento inválido: {cidr}")
        DATA["settings"]["cidr"] = cidr
        threading.Thread(target=SCAN.run, args=(kind,), daemon=True).start()
        out = {"started": True, "hosts": 254, "cidr": cidr}
        if kind == "scan":
            out["ports"] = list(DATA["settings"]["ports"])
        return self.json(out)

    # --- SSE ---------------------------------------------------------------
    def _sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _sse_scan(self):
        self._sse_headers()
        sent = 0
        while not STOP_FLAG.is_set():
            with SCAN.cond:
                if sent >= len(SCAN.events):
                    SCAN.cond.wait(timeout=15)
                batch = SCAN.events[sent:]
                sent += len(batch)
            if not batch:
                self.wfile.write(b": keepalive\n\n")
            for ev in batch:
                self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

    def _sse_detections(self):
        self._sse_headers()
        user = self.user
        with DET.cond:
            seen = DET.seq
        while not STOP_FLAG.is_set():
            with DET.cond:
                if DET.seq == seen:
                    DET.cond.wait(timeout=15)
                ev, now = DET.last, DET.seq
                fresh = now != seen
                seen = now
            if not fresh or ev is None:
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                continue
            dev, _s = find_stream(ev["stream_id"])
            if dev and user_allows(user, dev["id"]):
                self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode("utf-8"))
                self.wfile.flush()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv=None) -> int:
    global ARGS
    ap = argparse.ArgumentParser(description="Servidor de prueba del panel web de CamWay")
    ap.add_argument("--port", type=int, default=8131)
    ap.add_argument("--role", choices=("admin", "viewer", "seguridad", "kiosk"),
                    default="admin")
    ap.add_argument("--web", default=DEFAULT_WEB,
                    help="carpeta del panel (por defecto api/web de este repo)")
    ap.add_argument("--mosaic-mode", choices=("frames", "live", "auto"), default="frames",
                    help="modo por defecto del mosaico (frames = fotogramas fijos, sin WHEP)")
    ap.add_argument("--detector", choices=("on", "off"), default="on",
                    help="on: /api/health dice detector=true (el panel usa SSE y no carga el modelo)")
    ap.add_argument("--no-detections", action="store_true",
                    help="no emitir detecciones falsas cada 8 s por /api/detections")
    ap.add_argument("--verbose", action="store_true", help="registrar cada petición")
    ARGS = ap.parse_args(argv)
    ARGS.web = os.path.normpath(os.path.abspath(ARGS.web))
    if not os.path.isdir(ARGS.web):
        print(f"No existe la carpeta del panel: {ARGS.web}", file=sys.stderr)
        return 2
    mimetypes.add_type("image/svg+xml", ".svg")
    SESSION["user"] = role_user(ARGS.role)
    if not ARGS.no_detections:
        threading.Thread(target=detection_ticker, daemon=True).start()
    httpd = Server(("127.0.0.1", ARGS.port), Handler)
    print(f"Mock del panel CamWay en http://127.0.0.1:{ARGS.port}/  "
          f"(rol: {ARGS.role}; web: {ARGS.web})", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        STOP_FLAG.set()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
