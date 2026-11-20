"""CamWay — API de descubrimiento y publicación de cámaras."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import ipaddress
import logging
import os
import re
import secrets
import shutil
import time
import sys
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Optional

import httpx
from fastapi import Body, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import capture, onvif
from .bluiris import BlueIris
from . import onvif_ptz
from . import onvif_encoder
from . import camera_osd
from . import notifier as notifmod
from . import sysmon
from . import talkback
from .mediamtx import MediaMTX, safe_name
from . import mediamtx as mtxmod
from .wallcache import wall as wall_cache
from .mosaic import mosaic as server_mosaic
from . import relay as relaymod
from .scanner import (
    Device, ScanProgress, Stream, expand_cidr, probe_device, scan, scan_onvif,
    strip_credentials,
)
from .store import Store

logging.basicConfig(
    level=os.environ.get("CAMWAY_LOG", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("camway")


class _QuietAccess(logging.Filter):
    """Saca del log de acceso de uvicorn las peticiones que se repiten sin parar:
    fotogramas del muro (varios por segundo por cámara y por pantalla), llamadas del
    detector, WHEP y sondeos de salud. Eran casi todo el log que llenó el disco el
    15/11/2026. Los errores (4xx/5xx) de esas rutas SÍ se registran."""
    _NOISY = ("/api/wall/", "/api/detector/", "/api/health", "/api/system/health",
              "/api/detections", "/whep/", "/static/")

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        if len(args) >= 5:
            path, status = str(args[2]), args[4]
            try:
                ok = int(status) < 400
            except (TypeError, ValueError):
                ok = True
            if ok and path.startswith(self._NOISY):
                return False
        return True


logging.getLogger("uvicorn.access").addFilter(_QuietAccess())

WEB_DIR = os.environ.get("CAMWAY_WEB_DIR") \
    or os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")
DEFAULT_CIDR = os.environ.get("CAMWAY_DEFAULT_CIDR", "192.168.1.0/24")

store = Store()
mtx = MediaMTX()

_bi_pool: dict[str, BlueIris] = {}   # base -> instancia (sesión reutilizada)


def _bi_configs() -> list[dict]:
    """Todos los Blue Iris configurados: el primario (ajuste 'blueiris') más los
    extra (ajuste 'blueiris_servers', lista de {base,user,pwd}). El primero es el
    primario: lo usan el proxy /bi/ y el alta por defecto."""
    cfgs = []
    primary = store.get("blueiris")
    if primary and primary.get("base"):
        cfgs.append(primary)
    for extra in (store.get("blueiris_servers") or []):
        if extra.get("base"):
            cfgs.append(extra)
    return cfgs


def get_bi(base: Optional[str] = None) -> Optional[BlueIris]:
    """Instancia de Blue Iris. Sin `base` devuelve el primario; con `base`
    devuelve ese servidor concreto (para cámaras de un segundo NVR)."""
    cfgs = _bi_configs()
    if not cfgs:
        return None
    if base:
        want = base.rstrip("/")
        cfg = next((c for c in cfgs if c["base"].rstrip("/") == want), None)
    else:
        cfg = cfgs[0]
    if not cfg:
        return None
    key = cfg["base"].rstrip("/")
    inst = _bi_pool.get(key)
    if inst is None or inst.user != cfg.get("user", ""):
        inst = BlueIris(cfg["base"], cfg.get("user", ""), cfg.get("pwd", ""))
        _bi_pool[key] = inst
    return inst


def _bi_for(dev: dict) -> Optional[BlueIris]:
    """El Blue Iris que tiene las grabaciones de ESTA cámara (su bi_server, o el
    primario si está vacío)."""
    return get_bi(dev.get("bi_server") or None)


# --------------------------------------------------------------------------
# Sesión de escaneo: una a la vez, con difusión de progreso por SSE
# --------------------------------------------------------------------------

class ScanSession:
    def __init__(self):
        self.task: Optional[asyncio.Task] = None
        self.subscribers: set[asyncio.Queue] = set()
        self.last: list[dict] = []
        self.running = False

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def emit(self, event: dict) -> None:
        self.last.append(event)
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Un cliente lento no debe frenar el escaneo.
                pass

    async def run(self, cidr: str, ports: tuple[int, ...], channels: int,
                  concurrency: int, connect_timeout: float) -> None:
        self.running = True
        self.last = []
        found = 0
        auth = 0
        try:
            # El escáner NO usa credenciales guardadas: solo descubre y marca
            # «pide clave». Probar claves en barrido vetaba la IP en las HiLook/
            # Hikvision (bloqueo anti-fuerza-bruta). Las cámaras con clave se dan
            # de alta UNA A UNA (sondeo suelto con su clave / credencial por cámara).
            creds = []
            async for prog in scan(
                cidr,
                credentials=creds,
                ports=ports,
                channels=channels,
                concurrency=concurrency,
                connect_timeout=connect_timeout,
            ):
                event = _progress_event(prog)
                dev = prog.device
                # Se guardan las que entregan video, pero TAMBIÉN las que hablan
                # RTSP y piden clave o no soltaron stream: antes desaparecían y
                # eran justo las cámaras protegidas que no veíamos.
                if dev is not None and (
                    dev.streams or dev.needs_auth or dev.error.startswith("responde RTSP")
                ):
                    store.upsert_device(dev)
                    event["saved"] = True
                    if dev.streams:
                        found += 1
                    else:
                        auth += 1
                self.emit(event)
            extra = f", {auth} responden RTSP sin darnos video (¿clave?)" if auth else ""
            self.emit({"stage": "fin", "message": f"{found} equipos con video{extra}"})
        except ValueError as exc:
            self.emit({"stage": "error", "message": str(exc)})
        except asyncio.CancelledError:
            self.emit({"stage": "cancelado", "message": "escaneo detenido"})
            raise
        except Exception as exc:
            log.exception("fallo el escaneo")
            self.emit({"stage": "error", "message": f"{exc.__class__.__name__}: {exc}"})
        finally:
            self.running = False

    async def run_onvif(self, cidr: str) -> None:
        self.running = True
        self.last = []
        nuevas = 0
        try:
            creds = []   # ONVIF tampoco usa credenciales guardadas (anti-bloqueo)
            # Solo se añaden cámaras que aún no tenemos con video; a las que
            # están «pide clave» ONVIF les puede dar por fin su ruta.
            con_video = {d["host"] for d in store.devices() if d["streams"]}
            async for prog in scan_onvif(cidr, credentials=creds):
                event = _progress_event(prog)
                dev = prog.device
                if dev is not None and dev.streams and dev.host not in con_video:
                    store.upsert_device(dev)
                    con_video.add(dev.host)
                    nuevas += 1
                    event["saved"] = True
                self.emit(event)
            self.emit({"stage": "fin", "message": f"ONVIF: {nuevas} cámaras nuevas con ruta RTSP"})
        except ValueError as exc:
            self.emit({"stage": "error", "message": str(exc)})
        except asyncio.CancelledError:
            self.emit({"stage": "cancelado", "message": "ONVIF detenido"})
            raise
        except Exception as exc:
            log.exception("fallo el descubrimiento ONVIF")
            self.emit({"stage": "error", "message": f"{exc.__class__.__name__}: {exc}"})
        finally:
            self.running = False


session = ScanSession()


class DetectionHub:
    """Reparte a los navegadores (por SSE) las detecciones que envía el
    detector server-side."""

    def __init__(self):
        self.subscribers: set[asyncio.Queue] = set()
        self.latest: dict[str, dict] = {}
        self.detector_seen: float = 0.0       # reloj del event loop (para detector_alive)
        self.detector_seen_ts: float = 0.0    # hora de pared de la última consulta (para /status)
        self.models: Optional[set] = None     # modelos que declaró el detector; None = no lo dijo (detector viejo)

    def detector_alive(self) -> bool:
        try:
            return (asyncio.get_event_loop().time() - self.detector_seen) < 15
        except RuntimeError:
            return False

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=128)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def publish(self, event: dict) -> None:
        self.latest[event["stream_id"]] = event
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


detections = DetectionHub()


def _alert_cam_name(stream_id: str) -> str:
    """Nombre legible de la cámara para los avisos (dispositivo + etiqueta)."""
    s = store.stream(stream_id)
    if not s:
        return stream_id
    dev = store.device(s["device_id"]) or {}
    return dev.get("name") or s.get("label") or dev.get("host") or stream_id


notifier = notifmod.Notifier(store, _alert_cam_name)


def _progress_event(p: ScanProgress) -> dict:
    event = {
        "stage": p.stage,
        "done": p.done,
        "total": p.total,
        "found": p.found,
        "message": p.message,
    }
    if p.device is not None:
        event["device"] = _device_payload(p.device)
    return event


def _device_payload(dev) -> dict:
    return {
        "id": dev.id,
        "host": dev.host,
        "port": dev.port,
        "vendor": dev.vendor,
        "server": dev.server,
        "kind": dev.kind,
        "needs_auth": dev.needs_auth,
        "permissive": dev.permissive,
        "error": dev.error,
        "streams": [
            {
                "path": s.path,
                "url": strip_credentials(s.url),
                "codec": s.codec,
                "width": s.width,
                "height": s.height,
                "profile": s.profile,
                "audio_codec": s.audio_codec,
                "label": s.label,
                "webrtc_ready": s.webrtc_ready,
            }
            for s in dev.streams
        ],
    }


# --------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    if sys.platform == "win32":
        # El proactor de Windows escupe ConnectionResetError en callbacks
        # cuando una cámara corta de golpe; es ruido, no un fallo.
        loop = asyncio.get_running_loop()
        default = loop.get_exception_handler()

        def quiet(loop_, context):
            exc = context.get("exception")
            if isinstance(exc, ConnectionResetError):
                return
            if default:
                default(loop_, context)
            else:
                loop_.default_exception_handler(context)

        loop.set_exception_handler(quiet)

    if store.get("cidr") is None:
        store.set("cidr", DEFAULT_CIDR)

    # Las rutas de MediaMTX se crean por API y viven solo en memoria: un
    # reinicio de MediaMTX (o del host) las borra, pero la base sigue marcando
    # los streams como publicados. Al arrancar, se vuelven a empujar para que
    # el mosaico se auto-repare sin intervención.
    reconcile_task = asyncio.create_task(_reconcile_on_start())
    relay_task = asyncio.create_task(relaymod.agent(_relay_cameras, _relay_resolve))
    cleanup_task = asyncio.create_task(_event_clip_cleanup())
    recycler_task = asyncio.create_task(_disk_recycler())
    backup_task = asyncio.create_task(_db_backup_loop())
    notifier_task = asyncio.create_task(notifier.run())

    yield

    notifier_task.cancel()
    reconcile_task.cancel()
    cleanup_task.cancel()
    recycler_task.cancel()
    backup_task.cancel()
    if session.task and not session.task.done():
        session.task.cancel()
    await wall_cache.stop_all()
    await mtx.close()


# ---------------- grabación local (opt-in por cámara, apagada por defecto) ----
REC_MODES = {"continuous", "hybrid", "event"}
RECORD_DIR = os.environ.get("CAMWAY_RECORD_DIR", "/rec")
EVENT_DIRNAME = "_eventos"          # subcarpeta de clips de evento (fuera de %path)

# Grabación continua FORZADA en TODAS las cámaras (p.ej. servidor de taladro):
# toda cámara publicada graba en CONTINUO aunque no tenga config propia, así no
# se queda ninguna sin grabar. Retención: CAMWAY_RECORD_ALL_HOURS (si >0, manda)
# o CAMWAY_RECORD_ALL_DAYS. El purgador por espacio libre protege el disco.
RECORD_ALL = os.environ.get("CAMWAY_RECORD_ALL", "").strip().lower() in ("1", "true", "yes", "si")
RECORD_ALL_DAYS = int(os.environ.get("CAMWAY_RECORD_ALL_DAYS", "7") or 7)
RECORD_ALL_HOURS = int(os.environ.get("CAMWAY_RECORD_ALL_HOURS", "0") or 0)


def _rec_cfg(device_id: str) -> Optional[dict]:
    """Config de grabación de una cámara, o None si NO graba (por defecto).
    Con CAMWAY_RECORD_ALL, una cámara sin config propia graba en CONTINUO."""
    cfg = (store.get("recording") or {}).get(device_id)
    if cfg is not None:
        return cfg
    if RECORD_ALL:
        d = {"mode": "continuous", "cont_days": RECORD_ALL_DAYS, "event_days": 7}
        if RECORD_ALL_HOURS > 0:
            d["cont_hours"] = RECORD_ALL_HOURS
        return d
    return None


EVENT_BUFFER = os.environ.get("CAMWAY_EVENT_BUFFER", "10m")


def _rec_delete_after(device_id: str) -> Optional[str]:
    """Duración de retención del continuo para MediaMTX (recordDeleteAfter), o
    None si la cámara no graba. Modo 'event' → solo un BÚFER corto (lo justo
    para recortar el clip de movimiento); continuous/hybrid → cont_days días."""
    c = _rec_cfg(device_id)
    if not c:
        return None
    if c.get("mode") == "event":
        return EVENT_BUFFER
    h = c.get("cont_hours")
    if h:                                    # retención en HORAS (p.ej. taladro: 24h)
        return f"{int(h)}h"
    return f"{int(c.get('cont_days', 3) or 3)}d"


def _rec_split(device_id: str) -> tuple[Optional[str], Optional[str]]:
    """Reparte la grabación entre el flujo principal y el substream `__lo`.

    En cámaras de DOBLE FLUJO (lo_url, p.ej. la IMOU 3K HEVC) se graba el
    SUBSTREAM ligero (H264, ~poco CPU) y el principal queda SIN grabar → así es
    `runOnDemand` y el HEVC solo se transcodifica cuando alguien maximiza. En
    flujo único se graba el principal como siempre.
    Devuelve (retención_principal, retención_lo); None = no graba esa ruta."""
    da = _rec_delete_after(device_id)
    if da is None:
        return None, None
    q = _quality_cfg(device_id)
    if q and q.get("lo_url"):
        return None, da          # graba el substream, principal on-demand
    return da, None              # flujo único: graba el principal


# ---------------- calidad dinámica (substream ligero + 3K bajo demanda) -------
def _quality_cfg(device_id: str) -> Optional[dict]:
    """Config de doble flujo: {lo_url, lo_codec} para cámaras de alta resolución
    (p.ej. la IMOU 3K). El substream se usa por defecto; el principal 3K solo al
    maximizar. Ausencia = la cámara usa un único flujo."""
    return (store.get("quality") or {}).get(device_id)


def _lo_name(primary_name: str) -> str:
    return f"{safe_name(primary_name)}__lo"


async def _publish_lo(device_id: str, primary_name: str,
                      record_delete_after: Optional[str] = None) -> Optional[str]:
    """Publica el substream (flujo ligero) como ruta auxiliar `<primary>__lo`.

    Siempre con lo_transcode: normaliza el substream a H264 baseline SOLO vídeo
    (sin audio AAC, que rompía el WebRTC) y sin reescalar → mosaico en vivo fluido.
    Si `record_delete_after` viene dado, además GRABA este substream (cámaras de
    doble flujo: se graba el ligero en vez del principal 3K, ver `_rec_split`)."""
    q = _quality_cfg(device_id)
    if not q or not q.get("lo_url"):
        return None
    lo_name = _lo_name(primary_name)
    await mtx.publish(lo_name, q["lo_url"], lo_transcode=True,
                      record_delete_after=record_delete_after)
    return lo_name


def _tv_source(s: dict, name: str) -> tuple[str, bool]:
    """Fuente del flujo ligero `__tv` de la app TV: (url, ¿transcodificar?).

    Por defecto sale del PRINCIPAL vía MediaMTX, transcodificado a 640 px (cuesta CPU
    de decodificar 1080p en el servidor por cada celda). Si el stream tiene SUBFLUJO
    configurado (setting `tvsub` = {stream_id: {"path": "/Streaming/channels/102",
    "transcode": false}}), el `__tv` lo toma directamente de la cámara: sin transcodificar
    si ya es H264 (CPU ~0), o transcodificando solo ese subflujo chico si es HEVC.
    Solo afecta a `__tv`: el principal y su grabación no se tocan."""
    cfg = (store.get("tvsub") or {}).get(s["id"])
    if cfg and cfg.get("path"):
        m = re.match(r"(rtsp://(?:[^@/]*@)?[^/]+)", s["url"] or "")
        if m:
            return m.group(1) + cfg["path"], bool(cfg.get("transcode"))
    return f"rtsp://127.0.0.1:8554/{safe_name(name)}", True


async def reconcile_published() -> dict:
    """Reempuja a MediaMTX todos los streams marcados como publicados."""
    ok = failed = 0
    for s in store.streams(published_only=True):
        name = s["mtx_name"] or s["id"]
        vt, at = _pub_flags(s)
        pda, lda = _rec_split(s["device_id"])
        if await mtx.publish(name, s["url"], transcode=vt, audio_transcode=at,
                             record_delete_after=pda):
            ok += 1
            await _publish_lo(s["device_id"], name, record_delete_after=lda)   # substream ligero (si hay)
            # Flujo ligero de la app TV (`__tv`, bajo demanda: no cuesta nada hasta que alguien lo lee).
            # Se restaura aquí junto al principal: si MediaMTX se reinicia solo, el TV no depende de
            # volver a pedir /api/wall para que su ruta exista otra vez (misma lógica que /api/wall).
            q = _quality_cfg(s["device_id"])
            if TV_LITE and not (q and q.get("lo_url")):
                tv_src, tv_tc = _tv_source(s, name)
                await mtx.publish(f"{safe_name(name)}__tv", tv_src, tv_transcode=tv_tc)
        else:
            failed += 1
    if ok or failed:
        log.info("reconciliación: %d rutas restauradas, %d fallos", ok, failed)
    await refresh_wall_producers()
    return {"restauradas": ok, "fallos": failed}


async def refresh_wall_producers() -> None:
    """Sincroniza los productores de fotogramas con los streams publicados.
    Pasa también la URL directa de cada cámara (fallback si MediaMTX la pierde)."""
    # Para cámaras con substream, el productor de fotogramas tira del flujo
    # LIGERO (p.ej. 640x352) por su ruta MediaMTX `<name>__lo`, en vez del 3K/720p
    # principal: el muro queda igual y se ahorra decodificar el grande 24/7. El
    # fallback directo también es el substream.
    cams = {}
    src = {}
    for s in store.streams(published_only=True):
        name = s["mtx_name"] or s["id"]
        q = _quality_cfg(s["device_id"])
        if q and q.get("lo_url"):
            cams[name] = q["lo_url"]          # fallback directo = substream
            src[name] = _lo_name(name)        # leer la ruta __lo (substream vía MediaMTX)
        else:
            cams[name] = s["url"]
    await wall_cache.ensure(cams, src)


def _apply_encoding() -> None:
    """Carga los parámetros de transcode guardados en Ajustes (si hay) sobre los
    valores por defecto del entorno."""
    enc = store.get("encoding") or {}
    if enc:
        mtxmod.set_encoding(
            max_width=enc.get("max_width"), max_height=enc.get("max_height"),
            max_fps=enc.get("max_fps"), max_bitrate=enc.get("max_bitrate"),
            preset=enc.get("preset"))


async def _reconcile_on_start() -> None:
    """Reconcilia las rutas de MediaMTX al arrancar, Y cada vez que MediaMTX
    vuelva a estar sano después de haberse caído (reinicio por su propio
    healthcheck, OOM, lo que sea). Antes esto solo se intentaba 30 veces (60s)
    al arrancar y, si fallaba, se rendía para siempre hasta un reinicio
    manual del contenedor `api` o un POST /api/reconcile a mano — en un
    taladro sin personal técnico eso significa que nadie lo dispara nunca."""
    _apply_encoding()
    was_healthy = False
    checked_once = False
    last_fix = 0.0
    while True:
        healthy = await mtx.healthy()
        if healthy and not was_healthy:
            last_fix = time.monotonic()
            await reconcile_published()
            try:
                await refresh_ptz()      # auto-detectar cámaras PTZ vía Blue Iris
            except Exception:  # noqa: BLE001
                pass
            log.info("MediaMTX sano: rutas reconciliadas")
        elif healthy and was_healthy and time.monotonic() - last_fix > 30:
            # MediaMTX puede reiniciarse (su healthcheck, OOM, corte) en MENOS que el periodo de sondeo:
            # nunca se ve «caído», pero vuelve con la configuración VACÍA (sin rutas ni grabación) y nada
            # lo reconciliaba. Se detecta por lo que FALTA: una ruta publicada que no está en MediaMTX.
            try:
                present = await mtx.list_paths()
                expected = {safe_name(s["mtx_name"] or s["id"]) for s in store.streams(published_only=True)}
                missing = sorted(n for n in expected if n not in present)
                if missing:          # confirmar: una consulta fallida puntual no debe disparar la restauración
                    await asyncio.sleep(2)
                    present = await mtx.list_paths()
                    missing = sorted(n for n in expected if n not in present)
                if missing and await mtx.healthy():
                    log.warning("MediaMTX perdió %d ruta(s) publicadas (¿reinicio rápido?); se restauran: %s",
                                len(missing), ", ".join(missing[:5]))
                    last_fix = time.monotonic()      # tope: una restauración cada 30 s como mucho
                    await reconcile_published()
            except Exception:  # noqa: BLE001
                log.exception("falló la comprobación de rutas de MediaMTX")
        elif not healthy and was_healthy:
            log.warning("MediaMTX dejó de responder; se reconciliará solo en cuanto vuelva")
        elif not healthy and not checked_once:
            log.warning("MediaMTX no respondió al arrancar; se reintentará solo")
        was_healthy = healthy
        checked_once = True
        # Al arrancar se sondea más seguido (la espera típica es de segundos);
        # ya en régimen, cada 10s es de sobra para notar una caída real.
        await asyncio.sleep(2 if not was_healthy else 10)


app = FastAPI(title="CamWay", version="1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def revalidate_static(request, call_next):
    """Los estáticos se sirven con 'no-cache' para que el navegador revalide
    y no quede pegada una versión vieja de app.js/style.css tras un rebuild.
    Sigue habiendo 304 cuando no cambian, así que no pesa."""
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache"
    return response


# --------------------------------------------------------------------------
# Autenticación y permisos por perfil
# --------------------------------------------------------------------------

COOKIE_NAME = "camway_session"

# Modo KIOSKO (cliente embebido de taladro): si CAMWAY_KIOSK=1, las peticiones
# desde la PROPIA máquina (127.0.0.1) pueden VER el video sin login (rejilla
# /live, muro, fotogramas, WHEP, detecciones). Las acciones de ADMIN siguen
# exigiendo login real. Seguro porque solo el localhost de esa PC lo alcanza.
KIOSK = os.environ.get("CAMWAY_KIOSK", "").strip().lower() in ("1", "true", "yes", "si")
# «Ve todas las cámaras» NO se expresa con cameras=None (rompía set(...) y listas):
# `cameras` es siempre una lista y `_kiosk` es la marca; todo el código pregunta
# por `_user_sees_all(user)` y nunca por `user["cameras"]` directamente.
_KIOSK_USER = {"id": -1, "username": "kiosko", "role": "viewer", "cameras": [], "_kiosk": True}

# Rutas sin sesión: estáticos, login, y las que consumen los contenedores
# internos (detector y el proxy Blue Iris que jala MediaMTX).
_PUBLIC_EXACT = {"/", "/tv", "/api/health", "/api/me", "/api/login",
                 "/api/logout", "/api/setup"}
_INTERNAL_PREFIXES = ("/bi/", "/api/detector")
# Excepción: el panel (cualquier usuario con sesión) consulta si el detector está vivo.
_DETECTOR_STATUS_PATH = "/api/detector/status"

# Rutas que exigen rol admin.
_ADMIN_PREFIXES = ("/api/scan", "/api/onvif", "/api/probe", "/api/reprobe",
                   "/api/reconcile", "/api/devices", "/api/credentials",
                   "/api/blueiris", "/api/users")


# Subred interna de compose (detector, proxy Blue Iris…). La fija docker-compose.yml
# y la autoriza mediamtx.yml; se puede cambiar con CAMWAY_INTERNAL_CIDR si en el
# equipo esa subred ya la usa otra red Docker.
try:
    _INTERNAL_NET = ipaddress.ip_network(os.environ.get("CAMWAY_INTERNAL_CIDR", "172.28.0.0/24"), strict=False)
except ValueError:
    _INTERNAL_NET = ipaddress.ip_network("172.28.0.0/24")


def _client_is_internal(request: Request) -> bool:
    host = request.client.host if request.client else ""
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    try:
        return ipaddress.ip_address(host) in _INTERNAL_NET
    except ValueError:
        return False


# Caché de sesión en memoria: el muro pide fotogramas a 6fps×N cámaras, no se
# puede consultar SQLite en cada request. TTL corto → los cambios de permiso o
# el cierre de sesión surten efecto en pocos segundos.
_SESSION_TTL = 15.0
_session_cache: dict[str, tuple[float, Optional[dict]]] = {}


def _lookup_session(token: str) -> Optional[dict]:
    if not token:
        return None
    now = asyncio.get_event_loop().time()
    hit = _session_cache.get(token)
    if hit and now - hit[0] < _SESSION_TTL:
        return hit[1]
    user = store.session_user(token)
    _session_cache[token] = (now, user)
    return user


def _is_admin_path(path: str, method: str) -> bool:
    if path.startswith(_ADMIN_PREFIXES):
        return True
    if path.endswith("/publish") or path.endswith("/optimize"):   # publicar / optimizar encoder
        return True
    if path.startswith("/api/settings") and method in ("POST", "PUT", "DELETE"):
        return True
    # Activar/apagar o reconfigurar la grabación: solo admin. (Ver/descargar lo
    # grabado es GET y queda sujeto al permiso por cámara, no a admin.)
    if path.startswith("/api/recordings") and method in ("POST", "PUT", "DELETE"):
        return True
    # Detener el mosaico del servidor lo apaga para todos: solo admin. (Armarlo,
    # POST, lo hace cada usuario con SUS cámaras: ver mosaic_set.)
    if path == "/api/mosaic" and method == "DELETE":
        return True
    # Config completa de grabación y redetección de PTZ: la segunda devuelve las
    # credenciales ONVIF de las cámaras PTZ, así que solo admin (cualquier método).
    if path in ("/api/recordings/config", "/api/ptz/refresh"):
        return True
    return False


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    method = request.method

    if method == "OPTIONS" or path.startswith("/static/"):
        return await call_next(request)

    if path.startswith(_INTERNAL_PREFIXES) and path != _DETECTOR_STATUS_PATH:
        if not _client_is_internal(request):
            return JSONResponse({"detail": "solo acceso interno"}, status_code=403)
        return await call_next(request)

    if path in _PUBLIC_EXACT:
        return await call_next(request)

    # Sesión por cookie, o por ?token= (para reproductores nativos como libVLC
    # en la app TV, que no manejan cookies). El token es el mismo de la sesión.
    user = _lookup_session(request.cookies.get(COOKIE_NAME, "")) \
        or _lookup_session(request.query_params.get("token", ""))
    # Kiosko: localhost puede VER sin login (nunca acciones de admin).
    if not user and KIOSK and _client_is_internal(request) and not _is_admin_path(path, method):
        user = _KIOSK_USER
    if not user:
        return JSONResponse({"detail": "no autenticado"}, status_code=401)
    if _is_admin_path(path, method) and user["role"] != "admin":
        # El jefe de seguridad gestiona SOLO los avisos (destinatarios y reglas
        # de sus cámaras); el permiso por cámara se valida en cada endpoint.
        if not (user["role"] == "seguridad" and path.startswith("/api/settings/alerts")):
            return JSONResponse({"detail": "requiere administrador"}, status_code=403)
    request.state.user = user
    return await call_next(request)


def _user_sees_all(user: Optional[dict]) -> bool:
    """Admin y kiosko local ven todas las cámaras; el resto, solo las asignadas."""
    return bool(user) and (user["role"] == "admin" or bool(user.get("_kiosk")))


def _user_allows(request: Request, device_id: str) -> bool:
    u = getattr(request.state, "user", None)
    if not u:
        return False
    if _user_sees_all(u):
        return True
    return device_id in set(u.get("cameras") or [])


def _stream_by_route(name: str) -> Optional[dict]:
    """Stream cuya ruta de MediaMTX es `name`, con la MISMA regla con la que se
    arma el mosaico (`_published_cams`): `mtx_name` o, si está vacío, safe_name(id)."""
    s = store.stream_by_mtx(name)
    if s:
        return s
    for cand in store.streams():
        if (cand["mtx_name"] or safe_name(cand["id"])) == name:
            return cand
    return None


def _sees_all(request: Request) -> bool:
    """Admin y kiosko local ven todas las cámaras."""
    return _user_sees_all(getattr(request.state, "user", None))


def _require_camera(request: Request, device_id: str) -> None:
    if not _user_allows(request, device_id):
        raise HTTPException(403, "sin permiso sobre esa cámara")


def _set_session_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax",
                    max_age=60 * 60 * 24 * 30, path="/")


# ---------------- endpoints de sesión y usuarios ----------------

@app.get("/api/me")
async def me(request: Request):
    logo = store.get("logo", "")   # público, para mostrarlo en el login
    site = store.get("site_name", "") or ""
    if store.user_count() == 0:
        return {"authenticated": False, "setup_needed": True, "logo": logo, "site": site}
    user = store.session_user(request.cookies.get(COOKIE_NAME, ""))
    if not user:
        return {"authenticated": False, "setup_needed": False, "logo": logo, "site": site}
    return {"authenticated": True, "setup_needed": False, "logo": logo, "site": site,
            "user": {"id": user["id"], "username": user["username"],
                     "role": user["role"], "cameras": user["cameras"]}}


@app.post("/api/setup")
async def setup(payload: dict = Body(...), request: Request = None):
    if store.user_count() > 0:
        raise HTTPException(409, "el sistema ya tiene usuarios")
    # Mientras no haya admin, /api/setup es "quien llegue primero gana".
    # En una red de taladro menos controlada que una oficina, eso deja una
    # ventana para que un tercero en la LAN cree el admin antes que el
    # técnico. Limitarlo a localhost cierra esa ventana: el navegador que
    # abre start.bat/el supervisor ya es localhost, así que no cambia el
    # flujo normal de instalación.
    if request is not None and not _client_is_internal(request):
        raise HTTPException(403, "crea el admin desde esta misma máquina (http://localhost:8120)")
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username or len(password) < 4:
        raise HTTPException(400, "usuario y contraseña (≥4) obligatorios")
    uid = store.create_user(username, password, role="admin")
    token = store.create_session(uid)
    resp = JSONResponse({"ok": True, "role": "admin", "username": username})
    _set_session_cookie(resp, token)
    return resp


# Fuerza bruta de /api/login: sin esto, cualquiera en la LAN del taladro
# puede probar contraseñas sin límite. Bloqueo simple por IP en memoria
# (no persiste entre reinicios; suficiente para frenar un script, no hace
# falta más para este caso de uso).
_LOGIN_MAX_FAILS = 5
_LOGIN_WINDOW_S = 60.0
_LOGIN_LOCKOUT_S = 60.0
_login_fails: dict[str, list[float]] = {}


def _login_wait_s(ip: str) -> float:
    now = time.monotonic()
    fails = [t for t in _login_fails.get(ip, ()) if now - t < _LOGIN_WINDOW_S]
    _login_fails[ip] = fails
    if len(fails) < _LOGIN_MAX_FAILS:
        return 0.0
    remaining = _LOGIN_LOCKOUT_S - (now - fails[-1])
    return max(0.0, remaining)


@app.post("/api/login")
async def login(payload: dict = Body(...), request: Request = None):
    ip = request.client.host if request is not None and request.client else "?"
    wait = _login_wait_s(ip)
    if wait > 0:
        raise HTTPException(429, f"demasiados intentos; espera {int(wait) + 1} s")
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    user = store.get_user_by_name(username)
    from .store import verify_password
    if not user or not verify_password(password, user["pwd_hash"]):
        _login_fails.setdefault(ip, []).append(time.monotonic())
        raise HTTPException(401, "usuario o contraseña incorrectos")
    _login_fails.pop(ip, None)
    token = store.create_session(user["id"])
    resp = JSONResponse({"ok": True, "role": user["role"], "username": username})
    _set_session_cookie(resp, token)
    return resp


@app.post("/api/logout")
async def logout(request: Request):
    token = request.cookies.get(COOKIE_NAME, "")
    store.delete_session(token)
    _session_cache.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@app.get("/api/users")
async def list_users():
    return {"users": store.list_users()}


@app.post("/api/users")
async def create_user(payload: dict = Body(...)):
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username or len(password) < 4:
        raise HTTPException(400, "usuario y contraseña (≥4) obligatorios")
    if store.get_user_by_name(username):
        raise HTTPException(409, "ese usuario ya existe")
    role = payload.get("role", "viewer")
    cameras = payload.get("cameras") or []
    uid = store.create_user(username, password, role=role, cameras=cameras)
    return {"ok": True, "id": uid}


@app.put("/api/users/{user_id}")
async def edit_user(user_id: int, payload: dict = Body(...)):
    if not store.get_user(user_id):
        raise HTTPException(404, "usuario desconocido")
    store.update_user(user_id,
                      password=payload.get("password") or None,
                      role=payload.get("role"),
                      cameras=payload.get("cameras"))
    if payload.get("password"):
        store.delete_user_sessions(user_id)   # forzar re-login al cambiar clave
    _session_cache.clear()                     # permisos nuevos, ya
    return {"ok": True}


@app.delete("/api/users/{user_id}")
async def remove_user(user_id: int, request: Request):
    if request.state.user["id"] == user_id:
        raise HTTPException(400, "no puedes borrar tu propio usuario")
    admins = [u for u in store.list_users() if u["role"] == "admin"]
    target = store.get_user(user_id)
    if target and target["role"] == "admin" and len(admins) <= 1:
        raise HTTPException(400, "debe quedar al menos un administrador")
    store.delete_user(user_id)
    _session_cache.clear()
    return {"ok": True}


@app.post("/api/reconcile")
async def reconcile():
    """Fuerza restaurar en MediaMTX las rutas de los streams publicados."""
    if not await mtx.healthy():
        raise HTTPException(502, "MediaMTX no responde")
    return await reconcile_published()


def _disk_free_pct(path: str) -> Optional[float]:
    try:
        usage = shutil.disk_usage(path)
        return round(100.0 * usage.free / usage.total, 1)
    except OSError:
        return None


def _last_backup_age_s() -> Optional[float]:
    backup_dir = os.environ.get("CAMWAY_BACKUP_DIR", "/data/backups")
    try:
        files = [os.path.join(backup_dir, f) for f in os.listdir(backup_dir)
                 if f.startswith("camway-") and f.endswith(".db")]
        if not files:
            return None
        newest = max(os.path.getmtime(f) for f in files)
        return round(time.time() - newest, 0)
    except OSError:
        return None


@app.get("/api/health")
async def health():
    # Pensado para diagnóstico 100% remoto (taladro sin personal técnico):
    # un solo endpoint que diga si algo anda mal, sin necesitar sesión admin
    # ni acceso físico. disco/respaldo en la misma carpeta que la base, así
    # que el % de disco libre es el que realmente importa para la grabación.
    return {
        "ok": True,
        "mediamtx": await mtx.healthy(),
        "scanning": session.running,
        "devices": len(store.devices()),
        "detector": detections.detector_alive(),
        "disk_free_pct": _disk_free_pct(os.path.dirname(store.path) or "/"),
        "last_backup_age_s": _last_backup_age_s(),
        # Cuántas cámaras publicadas llevan sin fotograma fresco (STALE_HIDE,
        # 30s) — para que una alerta externa detecte cámaras caídas sin
        # necesitar que alguien mire la pantalla.
        "cameras_stale": sum(
            1 for d in store.devices() for s in d.get("streams", ())
            if s.get("published") and wall_cache.age(s.get("mtx_name") or "") is None
        ),
    }


# ---------------- escaneo ----------------

@app.post("/api/scan")
async def start_scan(payload: dict = Body(default={})):
    if session.running:
        raise HTTPException(409, "ya hay un escaneo en curso")

    cidr = payload.get("cidr") or store.get("cidr", DEFAULT_CIDR)
    try:
        hosts = expand_cidr(cidr)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    ports = tuple(payload.get("ports") or store.get("ports", [554, 8554, 10554, 5554]))
    channels = int(payload.get("channels") or store.get("channels", 8))
    concurrency = int(payload.get("concurrency") or store.get("concurrency", 200))
    connect_timeout = float(payload.get("connect_timeout")
                            or store.get("connect_timeout", 1.2))

    store.set("cidr", cidr)
    store.set("ports", list(ports))
    store.set("channels", channels)

    session.task = asyncio.create_task(
        session.run(cidr, ports, channels, concurrency, connect_timeout)
    )
    return {"started": True, "hosts": len(hosts), "ports": list(ports), "cidr": cidr}


@app.post("/api/onvif-scan")
async def onvif_scan(payload: dict = Body(default={})):
    if session.running:
        raise HTTPException(409, "ya hay un escaneo en curso")
    cidr = payload.get("cidr") or store.get("cidr", DEFAULT_CIDR)
    try:
        hosts = expand_cidr(cidr)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    session.task = asyncio.create_task(session.run_onvif(cidr))
    return {"started": True, "hosts": len(hosts), "cidr": cidr}


@app.post("/api/scan/stop")
async def stop_scan():
    if session.task and not session.task.done():
        session.task.cancel()
        return {"stopped": True}
    return {"stopped": False}


@app.get("/api/scan/events")
async def scan_events():
    """Progreso en vivo por Server-Sent Events."""
    async def stream():
        q = session.subscribe()
        try:
            for event in list(session.last):
                yield f"data: {json.dumps(event)}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            session.unsubscribe(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/probe")
async def probe_one(payload: dict = Body(...)):
    """Interroga un equipo suelto, sin barrer nada."""
    host = payload.get("host")
    if not host:
        raise HTTPException(400, "falta host")
    port = int(payload.get("port", 554))
    channels = int(payload.get("channels", 8))
    # Credencial escrita en la fila (opcional): se prueba PRIMERO, luego las guardadas.
    u = (payload.get("username") or "").strip()
    p = payload.get("password") or ""
    # Si escribes una credencial, se prueba SOLO esa (ni el anónimo ni la lista):
    # minimiza los intentos fallidos que bloquean las Hikvision/HiLook.
    creds = [(u, p)] if u else store.credential_pairs()
    dev = await probe_device(host, port, creds, channels)
    if dev.streams:
        store.upsert_device(dev)
        # Si entró con la credencial escrita, recordarla para futuros escaneos.
        if u and payload.get("save"):
            existing = {(c["username"], c["password"]) for c in store.credentials()}
            if (u, p) not in existing:
                store.add_credential(u, p, payload.get("label", ""))
    return _device_payload(dev)


@app.post("/api/devices/{device_id}/credential")
async def probe_with_credential(device_id: str, payload: dict = Body(...)):
    """Prueba una cámara concreta con el usuario/clave que se escriba en su
    tarjeta. Si entra, la guarda con video; opcionalmente recuerda la
    credencial para futuros escaneos."""
    dev_row = store.device(device_id)
    if not dev_row:
        raise HTTPException(404, "equipo desconocido")

    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username:
        raise HTTPException(400, "falta usuario")

    # Solo la credencial escrita (no el anónimo ni la lista): minimiza intentos
    # fallidos que disparan el bloqueo anti-fuerza-bruta en Hikvision/HiLook.
    creds = [(username, password)]
    channels = int(store.get("channels", 8))
    dev = await probe_device(dev_row["host"], dev_row["port"], creds, channels)

    recovered = bool(dev.streams)
    if recovered:
        store.upsert_device(dev)
        if payload.get("save"):
            existing = {(c["username"], c["password"]) for c in store.credentials()}
            if (username, password) not in existing:
                store.add_credential(username, password, payload.get("label", ""))

    result = _device_payload(dev)
    result["recovered"] = recovered
    return result


@app.post("/api/reprobe")
async def reprobe():
    """Re-interroga solo los equipos que respondían RTSP pero sin video (los
    que piden clave), usando las credenciales cargadas. Mucho más rápido que
    repetir todo el barrido."""
    creds = store.credential_pairs()
    channels = int(store.get("channels", 8))
    targets = [d for d in store.devices() if not d["streams"]]
    recovered = []
    for d in targets:
        dev = await probe_device(d["host"], d["port"], creds, channels)
        if dev.streams:
            store.upsert_device(dev)
            recovered.append(dev.host)
    return {"probados": len(targets), "recuperados": len(recovered),
            "hosts": recovered}


# ---------------- IA server-side: config, detector y eventos ----------------

@app.get("/api/streams/{stream_id}/ai")
async def get_ai(stream_id: str, request: Request):
    s = store.stream(stream_id)
    if not s:
        raise HTTPException(404, "stream desconocido")
    _require_camera(request, s["device_id"])
    cfg = store.get_ai(request.state.user["id"], stream_id)
    return cfg or {"enabled": False, "target": "vehiculo", "targets": ["vehiculo"],
                   "min_score": 0.4, "action": "full", "zone": None, "schedule": None}


@app.put("/api/streams/{stream_id}/ai")
async def set_ai(stream_id: str, request: Request, payload: dict = Body(...)):
    s = store.stream(stream_id)
    if not s:
        raise HTTPException(404, "stream desconocido")
    _require_camera(request, s["device_id"])   # viewer puede configurar SU cámara
    # `targets` (lista) es lo nuevo; `target` (texto) sigue aceptándose para
    # clientes viejos (app de TV, zone.html). Ver store.normalize_targets.
    if "targets" in payload and not isinstance(payload["targets"], list):
        raise HTTPException(400, "'targets' debe ser una lista")
    store.set_ai(request.state.user["id"], stream_id, payload)
    return {"ok": True}


@app.get("/api/ai-configs")
async def ai_configs(request: Request):
    """Configuraciones IA del usuario actual (cada quien la suya)."""
    return {"configs": store.ai_configs_for(request.state.user["id"])}


_DEFAULT_LAYOUT = {"zones": [{"id": "default", "name": "Sin zona", "cams": []}]}


@app.get("/api/layout")
async def get_layout(request: Request):
    """Disposición del mosaico (zonas y orden) de ESTE usuario. Se guarda en el
    servidor para que persista entre navegadores/equipos (p.ej. el puesto del
    guardia con su propia cuenta)."""
    uid = request.state.user["id"]
    return store.get(f"layout_u{uid}") or _DEFAULT_LAYOUT


@app.put("/api/layout")
async def put_layout(request: Request, payload: dict = Body(...)):
    uid = request.state.user["id"]
    zones = payload.get("zones")
    if not isinstance(zones, list):
        raise HTTPException(400, "layout inválido: falta 'zones'")
    store.set(f"layout_u{uid}", {"zones": zones})
    return {"ok": True}


DETECTOR_ONLINE_S = 30.0     # el detector consulta /targets cada ~2 s; sin noticias en 30 s = caído


@app.get("/api/detector/status")
async def detector_status():
    """¿Está vivo el detector del servidor y qué modelos tiene? Lo usa el modal de IA
    del panel para decidir «detecta el servidor» o «detecta el navegador».
    `models`: general = personas/vehículos; epp = sin casco; fuego = fuego/humo.
    `true` = cargado o con su archivo ya descargado en el detector (puede cargarse
    al activarlo, sin internet). Cualquier usuario con sesión."""
    seen = detections.detector_seen_ts
    online = bool(seen) and (time.time() - seen) < DETECTOR_ONLINE_S
    declared = detections.models
    have = set() if not online else (declared if declared is not None else {"general"})
    return {"online": online, "last_seen": seen or None,
            "models": {"general": "general" in have, "epp": "epp" in have,
                       "fuego": "fuego" in have}}


@app.get("/api/detector/targets")
async def detector_targets(models: str = ""):
    """Cámaras que el detector debe vigilar: las de IA (corre el modelo → alarma)
    y las que GRABAN por evento/híbrido (solo el gate de movimiento → clip). Cada
    target lleva `ai` (correr YOLO) y `record_motion` (emitir movimiento)."""
    detections.detector_seen = asyncio.get_event_loop().time()
    detections.detector_seen_ts = time.time()
    # El detector declara qué modelos tiene (?models=general,epp,fuego); uno viejo no manda nada.
    detections.models = {m.strip() for m in models.split(",") if m.strip()} if models else None
    ai = {t["stream_id"]: t for t in store.enabled_ai()}
    # Cámaras con AVISO por WhatsApp/correo: el detector las vigila aunque
    # ningún usuario haya activado la IA en su navegador.
    for sid_, tg in notifier.targets_for_detector().items():
        row = store.stream(sid_)
        if not row:
            continue
        cur = ai.setdefault(sid_, {"stream_id": sid_, "url": row["url"], "targets": []})
        cur["targets"] = sorted(set(cur.get("targets") or []) | tg)
    rec = store.get("recording") or {}
    rec_devs = {d for d, c in rec.items() if c.get("mode") in ("event", "hybrid")}
    mtx_rtsp = os.environ.get("CAMWAY_MTX_RTSP", "rtsp://mediamtx:8554")
    out = []
    for dev in store.devices():
        rm = dev["id"] in rec_devs
        for s in dev.get("streams", []):
            if not s.get("published"):
                continue
            sid = s["id"]
            is_ai = sid in ai
            if not is_ai and not rm:
                continue
            # El detector lee del stream YA publicado en MediaMTX, NO de la
            # cámara directa: así no abre una 2ª/3ª conexión RTSP a la cámara
            # (las baratas —Grandstream, HiLook— topan en conexiones concurrentes
            # → el vivo se queda sin stream y aparecen errores de decode). Una
            # sola conexión por cámara, la de MediaMTX, compartida por todos.
            route = s["mtx_name"] or sid
            out.append({
                "stream_id": sid,
                "url": f"{mtx_rtsp}/{route}",
                "targets": ai[sid]["targets"] if is_ai else [],
                "ai": is_ai,
                "record_motion": rm,
            })
    return {"targets": out}


@app.post("/api/detector/events")
async def detector_events(payload: dict = Body(...)):
    """El detector envía aquí las detecciones de una cámara."""
    sid = payload.get("stream_id")
    if not sid:
        raise HTTPException(400, "falta stream_id")
    # MOVIMIENTO → GRABACIÓN (clip de evento). Independiente de la IA.
    if payload.get("motion"):
        _maybe_event_clip(sid)
    # CAJAS DE IA → ALARMA. La decide cada usuario en su navegador con su
    # propia configuración (zona/tipo/umbral/horario). No graba por sí sola.
    boxes = payload.get("boxes")
    if boxes is not None:
        detections.publish({
            "stream_id": sid,
            "ts": payload.get("ts"),
            "boxes": boxes,
        })
        # AVISO fuera de pantalla (WhatsApp/correo): regla del servidor por cámara.
        try:
            notifier.feed(sid, payload.get("ts") or time.time(), boxes,
                          payload.get("frame"), payload.get("sharp"))
        except Exception as e:  # noqa: BLE001
            log.info("aviso: %s", e)
    return {"ok": True}


@app.post("/api/detector/frame/{stream_id}")
async def detector_frame(stream_id: str, request: Request):
    """El detector (OpenCV, más tolerante que MediaMTX) deja aquí un fotograma.
    Solo se MUESTRA si el productor normal del muro está caído; en cuanto el
    RTSP se recupera, `wallcache._best` vuelve solo al modo normal."""
    s = store.stream(stream_id)
    if not s:
        raise HTTPException(404, "stream desconocido")
    jpg = await request.body()
    if jpg[:2] == b"\xff\xd8":                 # es un JPEG
        wall_cache.set_detector_frame(s["mtx_name"] or s["id"], jpg)
    return {"ok": True}


# ---------------- proxy WHEP (mismo origen → el navegador puede cerrar la sesión) ----------------

WHEP_UPSTREAM = os.environ.get("CAMWAY_MTX_WEBRTC_INTERNAL", "http://mediamtx:8889")
_whep_sessions: dict[str, str] = {}   # token -> URL de sesión en MediaMTX


@app.post("/whep/{name}")
async def whep_offer(name: str, request: Request):
    """Reenvía la oferta SDP a MediaMTX y devuelve la respuesta con un Location
    del mismo origen, para que el navegador pueda hacerle DELETE al cerrar."""
    sname = safe_name(name)
    if sname == "mosaico":
        # Mosaico del servidor: NO es un stream del inventario y es UNO solo para
        # todos (un ffmpeg). Lo arma quien llamó último a POST /api/mosaic, así
        # que puede contener cámaras que ESTE usuario no tiene: si es así, 403
        # (el cliente debe volver a pedir su mosaico con POST /api/mosaic).
        if not _sees_all(request):
            names = server_mosaic.current_names()
            if not names:
                raise HTTPException(404, "mosaico no armado")
            for n in names:
                s = _stream_by_route(n)
                # Falla CERRADO: un nombre que no resuelve a ninguna cámara
                # conocida no se deja pasar (podría ser una cámara ajena).
                if not s or not _user_allows(request, s["device_id"]):
                    raise HTTPException(403, "el mosaico actual incluye cámaras que no tienes asignadas")
    else:
        # La ruta auxiliar del substream es "<primario>__lo": para el permiso se
        # busca por el nombre PRIMARIO (el __lo no es el mtx_name de ningún stream).
        lookup = sname[:-4] if sname.endswith("__lo") else sname
        s = store.stream_by_mtx(lookup)
        if not s:
            raise HTTPException(404, "cámara desconocida")
        _require_camera(request, s["device_id"])   # el viewer solo ve sus cámaras
    offer = await request.body()
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{WHEP_UPSTREAM}/{safe_name(name)}/whep", content=offer,
                             headers={"Content-Type": "application/sdp"})
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"WHEP upstream: {exc.__class__.__name__}")

    resp = Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("Content-Type", "application/sdp"))
    loc = r.headers.get("Location")
    if loc:
        token = secrets.token_hex(8)
        _whep_sessions[token] = str(httpx.URL(f"{WHEP_UPSTREAM}/{safe_name(name)}/whep").join(loc))
        resp.headers["Location"] = f"/whep/x/{token}"
    return resp


@app.delete("/whep/x/{token}")
async def whep_delete(token: str):
    """Cierra la sesión WHEP en MediaMTX (evita sesiones zombis)."""
    url = _whep_sessions.pop(token, None)
    if url:
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                await c.delete(url)
        except httpx.HTTPError:
            pass
    return Response(status_code=204)


@app.get("/api/detections")
async def detections_stream(request: Request):
    """SSE que consume el mosaico para disparar alarmas sin correr el modelo.
    Un viewer solo recibe eventos de sus cámaras permitidas."""
    user = request.state.user
    sees_all = _user_sees_all(user)          # admin o kiosko (cameras no se mira)
    allowed = set(user.get("cameras") or [])
    dev_of: dict[str, str] = {}   # stream_id -> device_id (cache)

    def visible(stream_id: str) -> bool:
        if sees_all:
            return True
        did = dev_of.get(stream_id)
        if did is None:
            s = store.stream(stream_id)
            did = s["device_id"] if s else ""
            dev_of[stream_id] = did
        return did in allowed

    async def stream():
        q = detections.subscribe()
        try:
            # NO se reenvían detecciones viejas al conectar: dispararían la
            # alarma con un evento pasado cada vez que se abre el mosaico.
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if not visible(ev.get("stream_id", "")):
                    continue
                yield f"data: {json.dumps(ev)}\n\n"
        finally:
            detections.unsubscribe(q)

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/onvif")
async def onvif_discover(timeout: float = 4.0):
    """Extra opcional: sin multicast devuelve lista vacía, no es un fallo."""
    found = await onvif.discover(timeout=timeout)
    return {
        "devices": [
            {"address": d.address, "name": d.name,
             "hardware": d.hardware, "xaddrs": d.xaddrs}
            for d in found
        ],
        "nota": "el multicast no cruza subredes ni suele llegar dentro de Docker",
    }


# ---------------- inventario ----------------

@app.get("/api/devices")
async def list_devices(site: Optional[str] = None):
    devices = store.devices(site)
    for dev in devices:
        for s in dev["streams"]:
            s["url"] = strip_credentials(s["url"])
            s["webrtc_ready"] = s["codec"] == "H264"
            if s["published"]:
                s["whep"] = mtx.whep_url(s["mtx_name"])
    return {"devices": devices}


@app.post("/api/devices/manual")
async def add_manual_device(payload: dict = Body(...)):
    """Alta manual de una cámara que el escáner no encuentra (p.ej. MJPEG por
    HTTP como la GoAhead 172.16.0.109, cuyo flujo es videostream.cgi)."""
    host = (payload.get("host") or "").strip()
    url = (payload.get("url") or "").strip()
    if not host or not url:
        raise HTTPException(400, "faltan 'host' y 'url'")

    codec = (payload.get("codec") or "MJPEG").upper()
    port = int(payload.get("port") or 80)
    label = payload.get("label") or "manual"

    dev = Device(host=host, port=port,
                 server=payload.get("vendor", "Manual"),
                 vendor=payload.get("vendor", "Manual"),
                 needs_auth=bool(payload.get("needs_auth", True)),
                 auth_ok=True)
    dev.streams.append(Stream(
        path=payload.get("path", "/"),
        url=url,
        codec=codec,
        width=payload.get("width"),
        height=payload.get("height"),
        label=label,
    ))
    store.upsert_device(dev)

    result = {"ok": True, "device_id": dev.id}
    if payload.get("publish", True):
        s = next((x for x in store.device(dev.id)["streams"]), None)
        if s:
            name = s["mtx_name"] or safe_name(s["id"])
            vt, at = _pub_flags(s)
            pda, lda = _rec_split(s["device_id"])
            if await mtx.publish(name, s["url"], transcode=vt, audio_transcode=at,
                                 record_delete_after=pda):
                store.set_published(s["id"], True)
                await _publish_lo(s["device_id"], name, record_delete_after=lda)
                await refresh_wall_producers()
                result["published"] = True
                result["stream_id"] = s["id"]
            else:
                result["published"] = False
                result["error"] = "MediaMTX rechazó la ruta"
    return result


async def _probe_rtsp(url: str) -> tuple[Optional[str], Optional[int], Optional[int]]:
    """Sondea un RTSP con ffmpeg y devuelve (codec, ancho, alto), o (None,..) si
    no hay señal. Para un NVR detecta el códec REAL (H264/H265/MJPEG) de cada
    canal → así se transcodifica el H.265 (que WebRTC no reproduce) y se saltan
    los canales sin cámara."""
    ff = shutil.which("ffmpeg") or "ffmpeg"
    try:
        # Sin salida: ffmpeg lee la cabecera, imprime el códec y termina enseguida.
        proc = await asyncio.create_subprocess_exec(
            ff, "-nostdin", "-rtsp_transport", "tcp", "-i", url,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            return None, None, None
    except Exception:  # noqa: BLE001
        return None, None, None
    txt = err.decode("utf-8", "ignore")
    m = re.search(r"Video:\s*([A-Za-z0-9]+)", txt)
    if not m:
        return None, None, None
    raw = m.group(1).lower()
    codec = {"hevc": "HEVC", "h265": "HEVC", "h264": "H264", "mjpeg": "MJPEG"}.get(raw, raw.upper())
    rr = re.search(r"\b(\d{2,5})x(\d{2,5})\b", txt)
    return codec, (int(rr.group(1)) if rr else None), (int(rr.group(2)) if rr else None)


@app.post("/api/devices/add-nvr")
async def add_nvr(payload: dict = Body(...)):
    """Alta de un NVR por su IP: da de alta de una vez TODAS las cámaras
    asociadas (un canal = un stream del mismo dispositivo). Pensado para el
    cliente de taladro: solo se escribe IP + usuario + clave + nº de canales.

    Por defecto usa el SUBSTREAM (subtype=1, H264 liviano) → rejilla fluida y
    de bajo consumo. `proto`: 'dahua' (por defecto, /cam/realmonitor) o
    'hikvision' (/Streaming/channels)."""
    from urllib.parse import quote
    host = (payload.get("host") or "").strip()
    if not host:
        raise HTTPException(400, "falta la IP del NVR")
    port = int(payload.get("port") or 554)
    user = payload.get("user") or ""
    pwd = payload.get("pwd") or ""
    nch = max(1, min(64, int(payload.get("channels") or 4)))
    subtype = 0 if str(payload.get("subtype", 1)) == "0" else 1
    proto = (payload.get("proto") or "dahua").lower()
    vendor = payload.get("vendor") or ("Hikvision" if proto == "hikvision" else "Dahua")
    cred = f"{quote(user, safe='')}:{quote(pwd, safe='')}@" if user else ""

    def url_for(ch: int) -> str:
        if proto == "hikvision":
            return f"rtsp://{cred}{host}:{port}/Streaming/channels/{ch}{'01' if subtype == 0 else '02'}"
        return f"rtsp://{cred}{host}:{port}/cam/realmonitor?channel={ch}&subtype={subtype}"

    dev = Device(host=host, port=port, server="NVR", vendor=vendor,
                 needs_auth=bool(user), auth_ok=True)
    # Sondea cada canal EN PARALELO: detecta el códec real (transcodifica H.265,
    # que WebRTC no reproduce) y descarta los canales sin cámara.
    probes = await asyncio.gather(*[_probe_rtsp(url_for(ch)) for ch in range(1, nch + 1)])
    detalle = []
    for ch, (codec, w, h) in zip(range(1, nch + 1), probes):
        if codec:
            dev.streams.append(Stream(path=f"/ch{ch}", url=url_for(ch), codec=codec,
                                      width=w, height=h, label=f"Canal {ch}"))
            detalle.append({"canal": ch, "estado": "ok", "codec": codec,
                            "res": (f"{w}x{h}" if w else "")})
        else:
            # El sondeo no respondió (NVR lento, límite de sesiones RTSP, etc.).
            # Se agrega IGUAL forzando transcode (HEVC): así se ve aunque el
            # sondeo falle. Transcodificar sirve para H.264 y H.265.
            dev.streams.append(Stream(path=f"/ch{ch}", url=url_for(ch), codec="HEVC",
                                      width=None, height=None, label=f"Canal {ch}"))
            detalle.append({"canal": ch, "estado": "sin sondeo (se transcodifica)"})
    store.upsert_device(dev)

    published = []
    for s in store.device(dev.id)["streams"]:
        name = s["mtx_name"] or safe_name(s["id"])
        vt, at = _pub_flags(s)          # códec real → H.265 se transcodifica a H.264 para WebRTC
        pda, lda = _rec_split(s["device_id"])
        if await mtx.publish(name, s["url"], transcode=vt, audio_transcode=at,
                             record_delete_after=pda):
            store.set_published(s["id"], True)
            await _publish_lo(s["device_id"], name, record_delete_after=lda)
            published.append(s["id"])
    await refresh_wall_producers()
    return {"ok": True, "device_id": dev.id, "channels": nch,
            "published": len(published), "detalle": detalle}


@app.patch("/api/devices/{device_id}")
async def rename_device(device_id: str, payload: dict = Body(...)):
    if not store.device(device_id):
        raise HTTPException(404, "equipo desconocido")
    store.rename_device(device_id, payload.get("name", ""), payload.get("site", ""))
    return {"ok": True}


@app.put("/api/devices/{device_id}/quality")
async def set_quality(device_id: str, payload: dict = Body(...)):
    """Doble flujo: define el substream (flujo ligero) de una cámara 3K. Con
    `lo_url` vacío se quita (vuelve a flujo único). Admin (bajo /api/devices)."""
    if not store.device(device_id):
        raise HTTPException(404, "equipo desconocido")
    q = dict(store.get("quality") or {})
    lo_url = (payload.get("lo_url") or "").strip()
    if not lo_url:
        q.pop(device_id, None)
    else:
        q[device_id] = {
            "lo_url": lo_url,
            "lo_codec": (payload.get("lo_codec") or "H264").upper(),
            "lo_audio_codec": (payload.get("lo_audio_codec") or "AAC").upper(),
        }
    store.set("quality", q)
    await _republish_device(device_id)
    return {"ok": True, "quality": q.get(device_id)}


@app.put("/api/devices/{device_id}/tvsub")
async def set_tv_sub(device_id: str, payload: dict = Body(...)):
    """Subflujo para la app TV (solo el `__tv`; la grabación y el principal no cambian).
    payload: {"stream_id", "path": "/Streaming/channels/102", "transcode": bool}.
    `path` vacío lo quita (vuelve a transcodificar el principal)."""
    dev = store.device(device_id)
    if not dev:
        raise HTTPException(404, "equipo desconocido")
    sid = payload.get("stream_id")
    st = next((x for x in dev["streams"] if x["id"] == sid), None)
    if not st:
        raise HTTPException(404, "stream desconocido")
    path = (payload.get("path") or "").strip()
    if path and not path.startswith("/"):
        raise HTTPException(400, "'path' debe empezar por /")
    cfg = dict(store.get("tvsub") or {})
    if path:
        cfg[sid] = {"path": path, "transcode": bool(payload.get("transcode"))}
    else:
        cfg.pop(sid, None)
    store.set("tvsub", cfg)
    if TV_LITE and st["published"]:
        name = st["mtx_name"] or safe_name(st["id"])
        tv_src, tv_tc = _tv_source(st, name)
        await mtx.publish(f"{safe_name(name)}__tv", tv_src, tv_transcode=tv_tc)
    return {"ok": True, "tvsub": cfg.get(sid)}


@app.delete("/api/devices/{device_id}")
async def forget_device(device_id: str):
    dev = store.device(device_id)
    if not dev:
        raise HTTPException(404, "equipo desconocido")
    for s in dev["streams"]:
        if s["published"]:
            await mtx.unpublish(s["mtx_name"])
    store.forget_device(device_id)
    return {"ok": True}


@app.delete("/api/devices")
async def forget_all_devices():
    """Borra TODO el inventario (botón «Borrar todos» del escaneo) para
    empezar limpio, p.ej. al cambiar de red."""
    n = 0
    for dev in store.devices():
        for s in dev["streams"]:
            if s["published"]:
                await mtx.unpublish(s["mtx_name"])
        store.forget_device(dev["id"])
        n += 1
    await refresh_wall_producers()
    return {"ok": True, "removed": n}


# ---------------- publicación en MediaMTX ----------------

@app.post("/api/streams/{stream_id}/publish")
async def publish_stream(stream_id: str, payload: dict = Body(default={})):
    s = store.stream(stream_id)
    if not s:
        raise HTTPException(404, "stream desconocido")

    # H.264 se reenvía tal cual; el resto solo llega al navegador transcodificado.
    # Si el audio es AAC, se copia el vídeo y se pasa el audio a Opus (WebRTC).
    vt, at = _pub_flags(s)
    transcode = bool(payload.get("transcode", vt))
    name = s["mtx_name"] or safe_name(stream_id)
    pda, lda = _rec_split(s["device_id"])

    if not await mtx.publish(name, s["url"], transcode=transcode, audio_transcode=at,
                             record_delete_after=pda):
        raise HTTPException(502, "MediaMTX rechazo la ruta")

    store.set_published(stream_id, True)
    await _publish_lo(s["device_id"], name, record_delete_after=lda)
    await refresh_wall_producers()
    return {
        "ok": True,
        "name": name,
        "transcode": transcode,
        "whep": mtx.whep_url(name),
        "hls": mtx.hls_url(name),
    }


@app.delete("/api/streams/{stream_id}/publish")
async def unpublish_stream(stream_id: str):
    s = store.stream(stream_id)
    if not s:
        raise HTTPException(404, "stream desconocido")
    await mtx.unpublish(s["mtx_name"] or stream_id)
    store.set_published(stream_id, False)
    await refresh_wall_producers()
    return {"ok": True}


# Códecs de audio que WebRTC puede entregar al navegador TAL CUAL (sin
# transcodificar). PCMA/PCMU (G.711) es lo que usan las Grandstream; AAC
# (MPEG4-GENERIC) NO es compatible con WebRTC → esas no sonarían.
_WEBRTC_AUDIO = {"PCMA", "PCMU", "G711", "OPUS", "G722", "L16"}
# Audio AAC (así lo anuncia el RTSP): WebRTC NO lo reproduce → se transcodifica
# a Opus (copiando el vídeo). Tras eso la cámara sí tiene audio en el vivo.
_AAC_AUDIO = {"MPEG4-GENERIC", "AAC", "MP4A-LATM", "MPEG-4 AUDIO"}


def _audio_webrtc(codec: Optional[str]) -> bool:
    return bool(codec) and codec.upper() in _WEBRTC_AUDIO


def _audio_aac(codec: Optional[str]) -> bool:
    return bool(codec) and codec.upper() in _AAC_AUDIO


def _pub_flags(s: dict) -> tuple[bool, bool]:
    """(transcode_video, transcode_audio) para una fila de stream.
    Vídeo no-H264 (MJPEG/H265) → re-codificar vídeo. H264 con audio AAC →
    copiar vídeo y pasar el audio a Opus (para que suene por WebRTC)."""
    vt = s["codec"] != "H264"
    at = (not vt) and _audio_aac(s.get("audio_codec"))
    return vt, at


def _has_audio(s: dict) -> bool:
    """La cámara tendrá audio reproducible en el vivo (G.711 nativo, o AAC que
    transcodificamos a Opus)."""
    return _audio_webrtc(s.get("audio_codec")) or _audio_aac(s.get("audio_codec"))


# --------------------------------------------------------------------------
# Relevo a demanda hacia un Command Center remoto (ver relay.py)
# --------------------------------------------------------------------------

def _relay_cameras() -> list[dict]:
    out = []
    for dev in store.devices():
        for st in dev["streams"]:
            if st["published"]:
                out.append({"stream": st["id"], "title": dev["name"] or f'{dev["host"]} {st["label"]}',
                            "width": st["width"], "height": st["height"], "codec": st["codec"]})
    return out


def _relay_resolve(stream_id: str) -> Optional[str]:
    for dev in store.devices():
        for st in dev["streams"]:
            if st["id"] == stream_id and st["published"]:
                return mtx.rtsp_url(st["mtx_name"] or safe_name(st["id"]))
    return None


@app.get("/api/relay")
async def relay_status():
    return {"jobs": relaymod.relay.status()}


@app.get("/api/wall")
async def wall(request: Request):
    """Lo que necesita el mosaico: solo streams publicados y permitidos."""
    runtime = await mtx.runtime_paths()
    tiles = []
    for dev in store.devices():
        if not _user_allows(request, dev["id"]):
            continue                       # viewer: solo sus cámaras
        for s in dev["streams"]:
            if not s["published"]:
                continue
            state = runtime.get(s["mtx_name"], {})
            primary = s["mtx_name"] or safe_name(s["id"])
            q = _quality_cfg(dev["id"])
            has_lo = bool(q and q.get("lo_url"))
            # App TV: sin substream propio, se le sirve un flujo ligero (640 px) derivado del
            # principal, bajo demanda. La grabación y el principal quedan intactos.
            tv_name = None
            if TV_LITE and not has_lo:
                tv_name = f"{safe_name(primary)}__tv"
                if tv_name not in runtime:
                    tv_src, tv_tc = _tv_source(s, primary)
                    await mtx.publish(tv_name, tv_src, tv_transcode=tv_tc)
                    runtime[tv_name] = {}
            tiles.append({
                "stream_id": s["id"],
                "device_id": dev["id"],
                "title": dev["name"] or f'{dev["host"]} {s["label"]}',
                "host": dev["host"],
                "vendor": dev["vendor"],
                "codec": s["codec"],
                "width": s["width"],
                "height": s["height"],
                # whep = flujo por defecto (substream ligero si lo hay);
                # whep_hi = principal 3K, para pantalla completa.
                "whep": mtx.whep_url(_lo_name(primary) if has_lo else primary),
                "whep_hi": mtx.whep_url(primary) if has_lo else None,
                # HLS (para el modo TV, nativo en televisores de poca RAM).
                "hls": mtx.hls_url(_lo_name(primary) if has_lo else primary),
                "hls_hi": mtx.hls_url(primary),
                # RTSP por MediaMTX: para la app TV nativa (libVLC) en TIEMPO REAL.
                "rtsp": mtx.rtsp_url(tv_name or (_lo_name(primary) if has_lo else primary)),
                "rtsp_hi": mtx.rtsp_url(primary),
                "frame": f"/api/wall/{s['id']}/frame",
                "audio": _has_audio(s),
                "ptz": bool(_ptz_cfg(dev["id"])),
                "zoom": _ptz_has_zoom(dev["id"]),
                "talk": bool(_talk_cfg(dev["id"])),
                "rec": bool(_rec_cfg(dev["id"])),
                "ready": bool(state.get("ready")),
                "readers": len(state.get("readers", [])),
            })
    fps = float(os.environ.get("CAMWAY_WALL_FPS", "1.5"))
    # Modo por defecto del mosaico: "frames" (fotogramas, ligero para muchas
    # cámaras), "live" (vídeo WebRTC fluido, para pocas) o "auto" (según nº de
    # cámaras). El bundle del taladro lo pone en "live"; la oficina en "auto".
    # El usuario puede cambiarlo en la interfaz (se recuerda en el navegador).
    mosaic_default = os.environ.get("CAMWAY_MOSAIC_MODE", "auto").lower()
    return {"tiles": tiles, "mediamtx": await mtx.healthy(), "fps": fps,
            "mosaic_default": mosaic_default}


def _published_cams(request: Request) -> list:
    """Cámaras publicadas (ruta principal, no __lo) que el usuario puede ver,
    en orden estable: entrada para el mosaico del servidor."""
    out = []
    for dev in store.devices():
        if not _user_allows(request, dev["id"]):
            continue
        for s in dev["streams"]:
            if not s["published"]:
                continue
            name = s["mtx_name"] or safe_name(s["id"])
            if name.endswith("__lo"):
                continue
            out.append({"name": name, "whep": mtx.whep_url(name),
                        "title": dev["name"] or f'{dev["host"]} {s["label"]}',
                        "host": dev["host"]})
    return out


TV_LITE = os.environ.get("CAMWAY_TV_LITE", "1") not in ("0", "false", "no", "")


@app.post("/api/mosaic")
async def mosaic_set(request: Request, payload: dict = Body(default={})):
    """Arma/reconfigura el MOSAICO DEL SERVIDOR (un solo H.264 compuesto) para el
    layout (4/9/16) y la página pedidos. El cliente reproduce 1 stream en vez de
    N. Devuelve el whep del mosaico + las celdas (para etiquetas y para maximizar
    una cámara con su stream directo)."""
    cams = _published_cams(request)
    if not cams and not _sees_all(request):
        # Sin cámaras propias no hay nada que componer, y el mosaico es compartido:
        # no dejar que alguien sin cámaras desmonte el de los demás.
        raise HTTPException(403, "no tienes cámaras asignadas")
    try:
        layout = int(payload.get("layout") or 4)
        page = int(payload.get("page") or 0)
    except (TypeError, ValueError):
        layout, page = 4, 0
    info = await server_mosaic.set(mtx, [c["name"] for c in cams], layout, page,
                                   titles={c["name"]: c["title"] for c in cams})
    per = info["per_page"]
    info["cells"] = cams[info["page"] * per: info["page"] * per + per]
    info["total"] = len(cams)
    return info


@app.delete("/api/mosaic")
async def mosaic_stop(request: Request):
    """Detiene el ffmpeg del mosaico y retira la ruta. Solo admin (afecta a todos)."""
    _require_admin(request)
    await server_mosaic.stop(mtx)
    return {"ok": True}


_wall_small: dict[tuple[str, int], tuple[float, bytes]] = {}


def _downscale_jpeg(jpg: bytes, w: int) -> bytes:
    """Reduce el ancho del JPEG (para máquinas lentas). Usa Pillow en proceso."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(jpg))
        if im.width <= w:
            return jpg
        h = max(1, round(im.height * w / im.width))
        im = im.convert("RGB").resize((w, h), Image.BILINEAR)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=70)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return jpg


@app.get("/api/wall/{stream_id}/frame")
async def wall_frame(stream_id: str, request: Request, w: int = 0):
    """Último fotograma JPEG del productor persistente (mosaico sin vídeo).
    Con `w` devuelve una versión reducida (modo ligero para equipos lentos)."""
    s = store.stream(stream_id)
    if not s:
        raise HTTPException(404, "stream desconocido")
    _require_camera(request, s["device_id"])
    name = s["mtx_name"] or stream_id
    jpg = wall_cache.frame(name)
    if jpg is None:
        # Aún sin fotograma (recién publicado, o productor arrancando).
        await refresh_wall_producers()
        raise HTTPException(503, "sin fotograma todavía")

    if 80 <= w < 480:
        ts = wall_cache.frame_ts(name)
        key = (name, w)
        cached = _wall_small.get(key)
        if cached and cached[0] == ts:
            jpg = cached[1]                      # ya reducido para este fotograma
        else:
            jpg = await asyncio.get_event_loop().run_in_executor(
                None, _downscale_jpeg, jpg, w)
            _wall_small[key] = (ts, jpg)
            if len(_wall_small) > 200:
                _wall_small.clear()

    return Response(content=jpg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/bi/{short}/video.mjpg")
async def bi_mjpg(short: str):
    """Proxy MJPEG hacia Blue Iris con sesión mantenida (re-login si expira).
    Es la fuente estable que consume MediaMTX para las cámaras del NVR."""
    bi = get_bi()
    if not bi:
        raise HTTPException(404, "Blue Iris no configurado")

    client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=None))
    resp = None
    for attempt in range(2):
        sess = await bi.get_session(client, force=(attempt == 1))
        req = client.build_request(
            "GET", f"{bi.base}/mjpg/{short}/video.mjpg?session={sess}"
        )
        r = await client.send(req, stream=True)
        if r.status_code in (301, 302, 401, 403):
            await r.aclose()          # sesión caduca: re-login y reintento
            continue
        resp = r
        break
    if resp is None:
        await client.aclose()
        raise HTTPException(502, "Blue Iris no entregó el flujo")

    ct = resp.headers.get("content-type", "multipart/x-mixed-replace")

    async def gen():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(gen(), media_type=ct)


@app.get("/api/blueiris/cameras")
async def blueiris_cameras():
    """Lista las cámaras del NVR (para el alta y la deduplicación)."""
    bi = get_bi()
    if not bi:
        raise HTTPException(404, "Blue Iris no configurado")
    try:
        return {"cameras": await bi.cameras()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Blue Iris: {e}")


# ---------------- grabaciones (histórico de Blue Iris) ----------------

REC_DIR = "/tmp/camrec"
os.makedirs(REC_DIR, exist_ok=True)


def _prune_rec(keep: int = 40) -> None:
    """Mantiene acotado el caché de MP4 remuxeados."""
    try:
        mp4s = [os.path.join(REC_DIR, f) for f in os.listdir(REC_DIR) if f.endswith(".mp4")]
        mp4s.sort(key=os.path.getmtime)
        for f in mp4s[:-keep]:
            os.remove(f)
    except OSError:
        pass


def _rec_device(device_id: str, request: Request) -> dict:
    dev = store.device(device_id)
    if not dev or not dev.get("bi_camera"):
        raise HTTPException(404, "cámara sin grabaciones")
    _require_camera(request, device_id)
    return dev


def _check_clip_path(dev: dict, path: str) -> None:
    """El clip debe pertenecer a la cámara del usuario (evita traversal)."""
    if "/" in path or "\\" in path or ".." in path:
        raise HTTPException(400, "ruta inválida")
    if not path.startswith(dev["bi_camera"] + "."):
        raise HTTPException(403, "ese clip no es de esta cámara")


@app.get("/api/recordings/devices")
async def rec_devices(request: Request):
    """Cámaras (permitidas) con grabaciones: de Blue Iris (NVR) o LOCALES (las
    que CamWay graba por su cuenta). `source` distingue cuál es cada una."""
    out = []
    for d in store.devices():
        if not _user_allows(request, d["id"]):
            continue
        pub = next((s for s in d["streams"] if s["published"]), None)
        thumb = f"/api/wall/{pub['id']}/frame" if pub else ""
        # Prioridad a la grabación LOCAL: si CamWay la graba por su cuenta, se
        # listan esas grabaciones (aunque la cámara tenga un bi_camera heredado
        # de cuando se emparejó con Blue Iris). Solo Blue Iris si NO graba local.
        if _rec_cfg(d["id"]):
            out.append({
                "device_id": d["id"], "name": d["name"] or d["host"],
                "source": "local", "thumb": thumb,
            })
        elif d.get("bi_camera"):
            out.append({
                "device_id": d["id"], "name": d["name"] or d["host"],
                "source": "blueiris", "bi_camera": d["bi_camera"], "thumb": thumb,
            })
    return {"devices": out, "available": get_bi() is not None}


@app.get("/api/recordings/{device_id}/clips")
async def rec_clips(device_id: str, request: Request, date: str = ""):
    dev = _rec_device(device_id, request)
    bi = _bi_for(dev)
    if not bi:
        raise HTTPException(404, "Blue Iris no configurado")
    start = end = None
    if date:
        try:
            d0 = dt.datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "fecha inválida (YYYY-MM-DD)")
        start = int(d0.timestamp())
        end = int((d0 + dt.timedelta(days=1)).timestamp())
    try:
        clips = await bi.cliplist(dev["bi_camera"], start, end)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Blue Iris: {e}")
    out = []
    for c in clips:
        ts = c.get("date", 0)
        if start is not None and not (start <= ts < end):
            continue
        out.append({"path": c.get("path"), "date": ts,
                    "size": c.get("filesize"), "flags": c.get("flags")})
    out.sort(key=lambda x: x["date"] or 0, reverse=True)
    return {"clips": out, "camera": dev["name"] or dev["host"]}


_thumb_cache: dict[str, bytes] = {}


@app.get("/api/recordings/{device_id}/thumb")
async def rec_thumb(device_id: str, request: Request, path: str):
    dev = _rec_device(device_id, request)
    _check_clip_path(dev, path)
    jpg = _thumb_cache.get(path)      # caché compartido: 2ª vista instantánea
    if jpg is None:
        bi = _bi_for(dev)
        jpg = await bi.fetch_thumb(path) if bi else None
        if not jpg:
            raise HTTPException(503, "sin miniatura")
        if len(_thumb_cache) > 3000:
            _thumb_cache.clear()
        _thumb_cache[path] = jpg
    return Response(content=jpg, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/recordings/{device_id}/video.mp4")
async def rec_video(device_id: str, request: Request, path: str):
    """Descarga el BVR de Blue Iris y lo remuxa a MP4 (sin transcodificar) para
    reproducción nativa con seeking."""
    dev = _rec_device(device_id, request)
    _check_clip_path(dev, path)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", path)
    out = os.path.join(REC_DIR, safe + ".mp4")

    if not (os.path.exists(out) and os.path.getsize(out) > 0):
        bi = _bi_for(dev)
        bvr = await bi.fetch_clip(path) if bi else None
        if not bvr:
            raise HTTPException(502, "no se pudo obtener el clip")
        src = os.path.join(REC_DIR, safe + ".bvr")
        with open(src, "wb") as f:
            f.write(bvr)
        # MP4 normal con moov al frente (+faststart): es lo que el <video> del
        # navegador carga bien por Range (progresivo + seek). -c copy no
        # transcodifica. Los clips a medio grabar fallan el remux y se descartan
        # por el returncode (abajo).
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-v", "error", "-fflags", "+genpts",
            "-i", src, "-c", "copy", "-movflags", "+faststart",
            "-f", "mp4", out,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        rc = await proc.wait()
        try:
            os.remove(src)
        except OSError:
            pass
        _prune_rec()
        if rc != 0 or not (os.path.exists(out) and os.path.getsize(out) > 1000):
            try:
                os.remove(out)          # no cachear un MP4 corrupto
            except OSError:
                pass
            raise HTTPException(502, "no se pudo preparar el vídeo (clip incompleto)")

    return FileResponse(out, media_type="video/mp4")


# ---------------- PTZ ----------------

PTZ_GOAHEAD = {"up": 2, "down": 0, "left": 4, "right": 6, "stop": 1}
# Blue Iris json cmd=ptz, códigos de botón (pan/tilt/zoom)
PTZ_BI = {"left": 0, "right": 1, "up": 2, "down": 3,
          "zoomin": 5, "zoomout": 6, "stop": 7}


def _ptz_cfg(device_id: str) -> Optional[dict]:
    return (store.get("ptz") or {}).get(device_id)


def _ptz_has_zoom(device_id: str) -> bool:
    """¿La cámara ofrece zoom en el pad PTZ? Blue Iris siempre lo trae; en ONVIF
    depende de lo detectado al activarlo."""
    c = _ptz_cfg(device_id)
    if not c:
        return False
    return c.get("method") == "blueiris" or bool(c.get("zoom"))


def _talk_cfg(device_id: str) -> Optional[dict]:
    """Config de talkback (hablarle a la cámara). Explícita en settings "talk",
    o derivada de un PTZ ONVIF (misma credencial, backchannel por &proto=Onvif)."""
    t = (store.get("talk") or {}).get(device_id)
    if t:
        return t
    p = _ptz_cfg(device_id)
    if p and p.get("method") == "onvif":
        return {"host": p["host"], "user": p["user"], "pwd": p["pwd"],
                "path": p.get("talk_path") or talkback.DEFAULT_PATH}
    return None


async def refresh_ptz() -> dict:
    """Auto-detecta qué cámaras son PTZ preguntándole a Blue Iris (que las
    gestiona y sabe cuáles tienen PTZ) y las controla por su propio comando."""
    bi = get_bi()
    # Conserva las entradas MANUALES (p.ej. onvif de la IMOU); solo re-detecta
    # las de Blue Iris.
    cfg: dict = {k: v for k, v in (store.get("ptz") or {}).items()
                 if v.get("method") != "blueiris"}
    if bi:
        try:
            cams = await bi.cameras()
        except Exception:  # noqa: BLE001
            cams = []
        ptz_shorts = {c["short"] for c in cams if c.get("ptz")}
        # Cámaras que BI marca PTZ pero que en realidad NO se mueven (fijas).
        exclude = set(store.get("ptz_exclude") or [])
        for dev in store.devices():
            bc = dev.get("bi_camera")
            if bc and bc in ptz_shorts and bc not in exclude:
                cfg[dev["id"]] = {"method": "blueiris", "camera": bc}
    store.set("ptz", cfg)
    log.info("PTZ auto-detectadas: %d cámaras", len(cfg))
    return cfg


@app.post("/api/ptz/refresh")
async def ptz_refresh(request: Request):
    # Devuelve la config PTZ COMPLETA, con usuario y clave ONVIF de cada cámara:
    # solo admin (además del middleware, por si cambia el prefijo).
    _require_admin(request)
    return {"ptz": await refresh_ptz()}


@app.get("/api/streams/{stream_id}/ptz")
async def ptz(stream_id: str, request: Request, dir: str):
    s = store.stream(stream_id)
    if not s:
        raise HTTPException(404, "stream desconocido")
    _require_camera(request, s["device_id"])
    cfg = _ptz_cfg(s["device_id"])
    if not cfg:
        raise HTTPException(400, "esta cámara no tiene PTZ configurado")

    method = cfg.get("method")
    act = request.query_params.get("act", "")   # 'start' | 'stop' | '' (paso suelto)
    if act == "stop" and method != "onvif":
        return {"ok": True}   # métodos de paso (goahead/blueiris) no tienen 'soltar'
    if method == "goahead":
        cmd = PTZ_GOAHEAD.get(dir)
        if cmd is None:
            raise HTTPException(400, "dirección no soportada")
        try:
            async with httpx.AsyncClient(timeout=6) as c:
                r = await c.get(f"http://{cfg['host']}/decoder_control.cgi",
                                params={"loginuse": cfg["user"], "loginpas": cfg["pwd"],
                                        "command": cmd, "onestep": 1})
            return {"ok": r.status_code == 200}
        except httpx.HTTPError as e:
            raise HTTPException(502, f"cámara PTZ: {e.__class__.__name__}")

    if method == "blueiris":
        btn = PTZ_BI.get(dir)
        if btn is None:
            raise HTTPException(400, "dirección no soportada")
        bi = get_bi()
        if not bi:
            raise HTTPException(404, "Blue Iris no configurado")
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                sess = await bi.get_session(client, force=True)
                r = await client.post(f"{bi.base}/json",
                                       json={"cmd": "ptz", "camera": cfg["camera"],
                                             "button": btn, "session": sess})
            return {"ok": r.json().get("result") == "success"}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Blue Iris PTZ: {e}")

    if method == "onvif":
        try:
            if act == "stop":
                await onvif_ptz.stop(cfg["host"], cfg["user"], cfg["pwd"])
            else:
                # 'start' (clientes viejos) y clic normal → SIEMPRE paso acotado
                # (mover+detener). Nunca movimiento continuo sin stop: imposible
                # que la cámara quede girando sin control.
                await onvif_ptz.step(cfg["host"], cfg["user"], cfg["pwd"], dir)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"ONVIF PTZ: {e}")

    raise HTTPException(400, "método PTZ desconocido")


def _device_rtsp_creds(device_id: str) -> tuple[str, str, str]:
    """Deriva host/usuario/clave ONVIF de la URL RTSP de la cámara."""
    dev = store.device(device_id)
    host = (dev or {}).get("host") or ""
    user = pwd = ""
    for s in (dev or {}).get("streams", []):
        m = re.match(r'rtsp://(?:([^:/@]+):([^@]+)@)?([^:/]+)', s.get("url") or "")
        if m:
            user, pwd, host = m.group(1) or "", m.group(2) or "", m.group(3) or host
            break
    return host, user, pwd


@app.get("/api/devices/{device_id}/ptz-config")
async def get_ptz_config(device_id: str):
    """Estado del PTZ de una cámara (para el ajuste por cámara en la UI)."""
    cfg = _ptz_cfg(device_id)
    if not cfg:
        return {"enabled": False, "method": "onvif"}
    return {"enabled": True, "method": cfg.get("method", "onvif"),
            "host": cfg.get("host", ""), "user": cfg.get("user", ""),
            "zoom": _ptz_has_zoom(device_id),
            "fixed": cfg.get("method") == "blueiris"}


@app.put("/api/devices/{device_id}/ptz-config")
async def set_ptz_config(device_id: str, payload: dict = Body(default={})):
    """Activa o desactiva el PTZ (ONVIF) de una cámara. Al activar, valida que la
    cámara responda ONVIF con esas credenciales antes de guardar."""
    if not store.device(device_id):
        raise HTTPException(404, "cámara desconocida")
    ptz = dict(store.get("ptz") or {})
    current = ptz.get(device_id)
    if current and current.get("method") == "blueiris":
        raise HTTPException(409, "esta cámara la gestiona Blue Iris; su PTZ es automático")

    if not payload.get("enabled"):
        ptz.pop(device_id, None)
        store.set("ptz", ptz)
        return {"enabled": False}

    host = (payload.get("host") or "").strip()
    user = payload.get("user")
    pwd = payload.get("pwd")
    dhost, duser, dpwd = _device_rtsp_creds(device_id)
    host = host or dhost
    user = duser if user is None else user
    pwd = dpwd if pwd is None else pwd
    if not host:
        raise HTTPException(400, "no se pudo determinar la dirección de la cámara")
    if not await onvif_ptz.probe(host, user or "", pwd or ""):
        raise HTTPException(502, "la cámara no respondió ONVIF con esas credenciales")
    zoom = await onvif_ptz.has_zoom(host, user or "", pwd or "")
    ptz[device_id] = {"method": "onvif", "host": host, "user": user or "", "pwd": pwd or "", "zoom": zoom}
    store.set("ptz", ptz)
    return {"enabled": True, "method": "onvif", "host": host, "zoom": zoom}


@app.post("/api/streams/{stream_id}/optimize")
async def optimize_realtime(stream_id: str, request: Request):
    """Optimiza la cámara para TIEMPO REAL por ONVIF: pone el keyframe (GOP) en
    ~1 s, lo que baja el retraso del vídeo en vivo de varios segundos a ~0,5 s.
    Funciona en cámaras ONVIF de verdad (Dahua/Hik). Admin."""
    s = store.stream(stream_id)
    if not s:
        raise HTTPException(404, "stream desconocido")
    _require_camera(request, s["device_id"])
    # Credenciales ONVIF: del PTZ onvif si existe, o de la URL RTSP de la cámara.
    host = user = pwd = None
    p = _ptz_cfg(s["device_id"])
    if p and p.get("method") == "onvif":
        host, user, pwd = p.get("host"), p.get("user"), p.get("pwd")
    if not host:
        m = re.match(r'rtsp://(?:([^:/@]+):([^@]+)@)?([^:/]+)', s.get("url") or "")
        if m:
            user, pwd, host = m.group(1) or "", m.group(2) or "", m.group(3)
    if not host:
        raise HTTPException(400, "no se pudo determinar la cámara para ONVIF")
    try:
        res = await onvif_encoder.set_gop(host, user or "", pwd or "", seconds=1)
        return {"ok": True, "configs": res}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"ONVIF encoder: {e}")


@app.post("/api/devices/{device_id}/osd")
async def set_camera_osd(device_id: str, request: Request, payload: dict = Body(...)):
    """Escribe el OSD (nombre mostrado + sello de fecha/hora) DENTRO de la cámara
    y, opcional, la pone en hora. ISAPI (HiLook/Hik) con caída a ONVIF (HJT).
    Es un cambio PERSISTENTE en el equipo (queda quemado en el video). Admin."""
    dev = store.device(device_id)
    if not dev:
        raise HTTPException(404, "equipo desconocido")
    # Credenciales de la cámara: del PTZ onvif si existe, o de su URL RTSP.
    host = user = pwd = None
    p = _ptz_cfg(device_id)
    if p and p.get("method") == "onvif":
        host, user, pwd = p.get("host"), p.get("user"), p.get("pwd")
    if not host:
        url = next((s.get("url") for s in dev.get("streams", []) if s.get("url")), "")
        m = re.match(r'rtsp://(?:([^:/@]+):([^@]+)@)?([^:/]+)', url or "")
        if m:
            user, pwd, host = m.group(1) or "", m.group(2) or "", m.group(3)
    host = host or dev.get("host")
    if not host:
        raise HTTPException(400, "no se pudo determinar la dirección de la cámara")
    name = payload.get("name")
    name = name.strip() if isinstance(name, str) else None
    try:
        res = await camera_osd.apply_osd(
            host, user or "", pwd or "", name=name,
            show_name=bool(payload.get("show_name", True)),
            show_time=bool(payload.get("show_time", True)),
            set_time=bool(payload.get("set_time", False)),
            ntp=(payload.get("ntp") or None),
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"OSD: {e}")
    if not res.get("ok"):
        raise HTTPException(502, "; ".join(res.get("errors") or ["no se pudo aplicar el OSD"]))
    return res


@app.websocket("/api/talk/{stream_id}")
async def talk_ws(ws: WebSocket, stream_id: str):
    """Push-to-talk: el navegador manda audio G.711 A-law (8 kHz, tramas de 20 ms)
    y lo empujamos al altavoz de la cámara por el backchannel ONVIF/RTSP."""
    await ws.accept()
    s = store.stream(stream_id)
    if not s:
        await ws.close(code=4404)
        return
    # Auth por cookie (el middleware HTTP no corre en WebSockets).
    user = store.session_user(ws.cookies.get(COOKIE_NAME, ""))
    if not user or not (_user_sees_all(user) or s["device_id"] in set(user.get("cameras") or [])):
        await ws.close(code=4403)
        return
    cfg = _talk_cfg(s["device_id"])
    if not cfg:
        await ws.send_json({"error": "esta cámara no admite talkback"})
        await ws.close()
        return

    bc = talkback.Backchannel(cfg["host"], cfg["user"], cfg["pwd"],
                              cfg.get("path") or talkback.DEFAULT_PATH)
    try:
        await asyncio.wait_for(bc.open(), timeout=15.0)
    except Exception as e:  # noqa: BLE001
        log.info("talkback %s: %s", stream_id, e)
        try:
            await ws.send_json({"error": str(e)})
        except Exception:  # noqa: BLE001
            pass
        await ws.close()
        return

    await ws.send_json({"ok": True, "pt": bc.payload_type})
    pacer = asyncio.create_task(bc.pace())
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data:
                await bc.feed(data)
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.info("talkback %s cerró: %s", stream_id, e)
    finally:
        pacer.cancel()
        await bc.close()


# ---------------- grabación: configuración, listado, descarga, clips ----------
PLAYBACK_URL = os.environ.get("CAMWAY_MTX_PLAYBACK", "http://mediamtx:9996")
EVENT_CLIP_SECONDS = int(os.environ.get("CAMWAY_EVENT_CLIP_SECONDS", "20"))
EVENT_CLIP_PRE = int(os.environ.get("CAMWAY_EVENT_CLIP_PRE", "5"))
EVENT_DEBOUNCE_S = float(os.environ.get("CAMWAY_EVENT_DEBOUNCE", "60"))
_event_last: dict[str, float] = {}       # stream_id -> último clip (debounce)


def _device_mtx_names(device_id: str) -> list[str]:
    dev = store.device(device_id)
    has_lo = bool(_quality_cfg(device_id) and _quality_cfg(device_id).get("lo_url"))
    names = []
    for s in (dev["streams"] if dev else []):
        if not s["published"]:
            continue
        name = s["mtx_name"] or safe_name(s["id"])
        names.append(name)
        if has_lo:                       # doble flujo: la grabación está en el __lo
            names.append(_lo_name(name))
    return names


async def _republish_device(device_id: str, include_lo: bool = True) -> None:
    """Reempuja las rutas de una cámara para aplicar en caliente un cambio.
    `include_lo=False` NO toca el substream `__lo`: así un cambio de grabación
    (que solo afecta a la ruta principal) no corta el vivo del mosaico/APK, que
    mira el `__lo`."""
    dev = store.device(device_id)
    if not dev:
        return
    pda, lda = _rec_split(device_id)
    has_lo = bool(_quality_cfg(device_id) and _quality_cfg(device_id).get("lo_url"))
    for s in dev["streams"]:
        if not s["published"]:
            continue
        name = s["mtx_name"] or safe_name(s["id"])
        vt, at = _pub_flags(s)
        await mtx.publish(name, s["url"], transcode=vt, audio_transcode=at, record_delete_after=pda)
        # Reempuja el __lo si se pide, O si la grabación vive en él (doble flujo):
        # un cambio de grabación DEBE reaplicarse al substream aunque include_lo=False.
        if include_lo or has_lo:
            await _publish_lo(s["device_id"], name, record_delete_after=lda)
    await refresh_wall_producers()


@app.get("/api/recordings/config")
async def recordings_config(request: Request):
    """Toda la config de grabación (admin). Por defecto vacío = nada graba."""
    _require_admin(request)
    return {"recording": store.get("recording") or {}}


@app.put("/api/recordings/{device_id}/config")
async def set_recording_config(device_id: str, payload: dict = Body(...)):
    if not store.device(device_id):
        raise HTTPException(404, "equipo desconocido")
    cfg = dict(store.get("recording") or {})
    enabled = bool(payload.get("enabled"))
    if not enabled:
        cfg.pop(device_id, None)                 # apagar = quitar la entrada
    else:
        mode = payload.get("mode", "hybrid")
        if mode not in REC_MODES:
            raise HTTPException(400, "modo inválido")
        entry = {
            "mode": mode,
            "cont_days": max(1, int(payload.get("cont_days", 3))),
            "event_days": max(1, int(payload.get("event_days", 7))),
        }
        ch = payload.get("cont_hours")          # retención fina en horas (24 por defecto en taladros)
        if ch not in (None, "", 0, "0"):
            entry["cont_hours"] = max(1, int(ch))
        cfg[device_id] = entry
    store.set("recording", cfg)
    await _republish_device(device_id, include_lo=False)   # no cortar el vivo (__lo)
    log.info("grabación %s: %s", device_id, cfg.get(device_id, "APAGADA"))
    return {"ok": True, "config": cfg.get(device_id)}


def _list_dir(path: str, kind: str) -> list[dict]:
    out = []
    try:
        with os.scandir(path) as it:
            for e in it:
                if not e.is_file() or not e.name.lower().endswith(".mp4"):
                    continue
                st = e.stat()
                out.append({"kind": kind, "name": e.name, "size": st.st_size,
                            "mtime": st.st_mtime})
    except FileNotFoundError:
        pass
    return out


@app.get("/api/recordings/{device_id}")
async def list_recordings(device_id: str, request: Request):
    """Lista segmentos del continuo y clips de evento de una cámara."""
    _require_camera(request, device_id)
    files = []
    for name in _device_mtx_names(device_id):
        for f in _list_dir(os.path.join(RECORD_DIR, name), "cont"):
            f["stream"] = name
            files.append(f)
        for f in _list_dir(os.path.join(RECORD_DIR, EVENT_DIRNAME, name), "event"):
            f["stream"] = name
            files.append(f)
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return {"recordings": files, "config": _rec_cfg(device_id)}


REC_CACHE = os.path.join(RECORD_DIR, "_cache")


def _prune_rec_cache(keep: int = 20) -> None:
    try:
        mp4s = [os.path.join(REC_CACHE, f) for f in os.listdir(REC_CACHE) if f.endswith(".mp4")]
        mp4s.sort(key=os.path.getmtime)
        for f in mp4s[:-keep]:
            try:
                os.remove(f)
            except OSError:
                pass
    except FileNotFoundError:
        pass


async def _playable_mp4(full: str, stream: str, kind: str, name: str) -> str:
    """Devuelve un MP4 reproducible en el navegador. Las grabaciones de MediaMTX
    son fMP4 con audio OPUS → el navegador tarda/falla al iniciarlas. Se remuxea
    (vídeo H264 copiado + audio a AAC + faststart) una vez y se cachea; así
    arranca al instante y permite buscar (Range). Si el remux falla, sirve el
    original."""
    cache_name = f"{safe_name(stream)}__{kind}__{name}"
    cached = os.path.join(REC_CACHE, cache_name)
    try:
        fresh = (os.path.isfile(cached)
                 and os.path.getmtime(cached) >= os.path.getmtime(full)
                 and os.path.getsize(cached) > 1024)
        if not fresh:
            os.makedirs(REC_CACHE, exist_ok=True)
            tmp = cached + ".tmp"
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", full,
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", "-f", "mp4", tmp,   # .tmp → forzar muxer
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
            if proc.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 1024:
                os.replace(tmp, cached)
            else:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return full
        _prune_rec_cache()
        return cached
    except Exception:  # noqa: BLE001
        return full


@app.get("/api/recordings/{device_id}/file")
async def get_recording_file(device_id: str, request: Request,
                             stream: str, name: str, kind: str = "cont"):
    """Sirve un .mp4 grabado, reproducible en el navegador (remux a H264+AAC
    faststart, cacheado) y con soporte de Range para buscar."""
    _require_camera(request, device_id)
    if stream not in _device_mtx_names(device_id) or "/" in name or "\\" in name \
            or ".." in name or not name.lower().endswith(".mp4"):
        raise HTTPException(400, "ruta inválida")
    base = os.path.join(RECORD_DIR, EVENT_DIRNAME, stream) if kind == "event" \
        else os.path.join(RECORD_DIR, stream)
    full = os.path.join(base, name)
    if not os.path.isfile(full):
        raise HTTPException(404, "grabación no encontrada")
    serve = await _playable_mp4(full, stream, kind, name)
    return FileResponse(serve, media_type="video/mp4", filename=name)


async def _save_event_clip(stream_id: str, mtx_name: str, event_ts: float, label: str):
    """Tras un margen (para que exista el 'después'), recorta del continuo un clip
    centrado en el evento vía el servidor de playback y lo guarda aparte."""
    await asyncio.sleep(EVENT_CLIP_SECONDS)       # espera a que se grabe el "después"
    start = dt.datetime.fromtimestamp(event_ts - EVENT_CLIP_PRE, dt.timezone.utc)
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    evdir = os.path.join(RECORD_DIR, EVENT_DIRNAME, mtx_name)
    try:
        os.makedirs(evdir, exist_ok=True)
    except OSError:
        return
    out = os.path.join(evdir, start.strftime("%Y-%m-%d_%H-%M-%S") + f"_{safe_name(label)}.mp4")
    params = {"path": mtx_name, "start": start_iso,
              "duration": EVENT_CLIP_PRE + EVENT_CLIP_SECONDS, "format": "mp4"}
    try:
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.get(f"{PLAYBACK_URL}/get", params=params)
        if r.status_code == 200 and r.content[:4] and len(r.content) > 1024:
            with open(out, "wb") as fh:
                fh.write(r.content)
            log.info("clip de evento %s (%d KB)", os.path.basename(out), len(r.content) // 1024)
        else:
            log.info("playback sin material para clip %s: %s", mtx_name, r.status_code)
    except Exception as e:  # noqa: BLE001
        log.info("clip de evento %s falló: %s", mtx_name, e)


def _maybe_event_clip(stream_id: str) -> None:
    """Hay MOVIMIENTO en una cámara que graba por evento/híbrido: programa un
    clip de evento (con antirrebote por cámara). La grabación la dispara el
    movimiento, NO la IA (la IA es solo para la alarma)."""
    s = store.stream(stream_id)
    if not s:
        return
    cfg = _rec_cfg(s["device_id"])
    if not cfg or cfg.get("mode") not in ("event", "hybrid"):
        return
    now = asyncio.get_event_loop().time()
    if now - _event_last.get(stream_id, 0) < EVENT_DEBOUNCE_S:
        return
    _event_last[stream_id] = now
    name = s["mtx_name"] or safe_name(stream_id)
    if _quality_cfg(s["device_id"]) and _quality_cfg(s["device_id"]).get("lo_url"):
        name = _lo_name(name)            # doble flujo: el buffer grabado está en el __lo
    asyncio.create_task(_save_event_clip(
        stream_id, name, dt.datetime.now().timestamp(), "movimiento"))


async def _db_backup_loop() -> None:
    """Respaldo diario de la base (config: usuarios, cámaras, PTZ, grabación…).
    En un taladro con apagones, si la base viva se corrompe, queda una
    instantánea reciente para restaurar. Conserva los últimos 7 días."""
    backup_dir = os.environ.get("CAMWAY_BACKUP_DIR", "/data/backups")
    keep = int(os.environ.get("CAMWAY_BACKUP_KEEP", "7"))
    await asyncio.sleep(30)                 # un respaldo poco después de arrancar
    while True:
        try:
            dest = os.path.join(backup_dir, "camway-" + dt.date.today().isoformat() + ".db")
            await asyncio.get_event_loop().run_in_executor(None, store.backup, dest)
            # podar los más viejos
            try:
                files = sorted(f for f in os.listdir(backup_dir)
                               if f.startswith("camway-") and f.endswith(".db"))
                for f in files[:-keep]:
                    try:
                        os.remove(os.path.join(backup_dir, f))
                    except OSError:
                        pass
            except FileNotFoundError:
                pass
            log.info("respaldo de base: %s", dest)
        except Exception as e:  # noqa: BLE001
            log.info("respaldo de base falló: %s", e)
        await asyncio.sleep(86400)


REC_MIN_FREE_GB = float(os.environ.get("CAMWAY_REC_MIN_FREE_GB", "25"))


async def _disk_recycler() -> None:
    """Recicla los segmentos CONTINUOS más viejos cuando el disco de grabación se
    llena (como hacía Blue Iris): así la grabación de muchas cámaras nunca llena
    el disco, solo recorta la historia más vieja. Los clips de evento no se tocan
    aquí (los purga _event_clip_cleanup por antigüedad)."""
    target_free = REC_MIN_FREE_GB * (1024 ** 3)
    while True:
        try:
            if shutil.disk_usage(RECORD_DIR).free < target_free:
                segs = []       # (mtime, path) de segmentos continuos
                try:
                    for entry in os.scandir(RECORD_DIR):
                        if not entry.is_dir() or entry.name.startswith("_"):
                            continue      # salta _eventos y _cache
                        try:
                            with os.scandir(entry.path) as it:
                                for e in it:
                                    if e.is_file() and e.name.lower().endswith(".mp4"):
                                        segs.append((e.stat().st_mtime, e.path))
                        except OSError:
                            pass
                except FileNotFoundError:
                    pass
                segs.sort()               # más viejos primero
                freed = 0
                for _mtime, path in segs:
                    if shutil.disk_usage(RECORD_DIR).free >= target_free:
                        break
                    try:
                        freed += os.path.getsize(path)
                        os.remove(path)
                    except OSError:
                        pass
                if freed:
                    log.info("reciclaje de grabación: liberados %d MB (disco lleno)",
                             freed // (1024 * 1024))
        except Exception as e:  # noqa: BLE001
            log.info("reciclaje de grabación: %s", e)
        await asyncio.sleep(120)


async def _event_clip_cleanup() -> None:
    """Borra clips de evento más viejos que la retención de cada cámara."""
    while True:
        try:
            cfgs = store.get("recording") or {}
            retention = {}       # mtx_name -> días
            for dev_id, c in cfgs.items():
                for name in _device_mtx_names(dev_id):
                    retention[name] = int(c.get("event_days", 7) or 7)
            evroot = os.path.join(RECORD_DIR, EVENT_DIRNAME)
            now = dt.datetime.now().timestamp()
            for name, days in retention.items():
                d = os.path.join(evroot, name)
                cutoff = now - days * 86400
                try:
                    with os.scandir(d) as it:
                        for e in it:
                            if e.is_file() and e.stat().st_mtime < cutoff:
                                try:
                                    os.remove(e.path)
                                except OSError:
                                    pass
                except FileNotFoundError:
                    pass
        except Exception as e:  # noqa: BLE001
            log.info("limpieza de clips: %s", e)
        await asyncio.sleep(3600)


@app.get("/api/streams/{stream_id}/snapshot")
async def stream_snapshot(stream_id: str, request: Request, width: int = 480):
    s = store.stream(stream_id)
    if not s:
        raise HTTPException(404, "stream desconocido")
    _require_camera(request, s["device_id"])
    jpeg = await capture.snapshot(s["url"], width=width, cache_key=stream_id)
    if jpeg is None:
        raise HTTPException(503, "no se pudo capturar un fotograma")
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


# ---------------- credenciales y ajustes ----------------

@app.get("/api/credentials")
async def list_credentials():
    # Nunca se devuelve la clave, solo si la hay.
    return {"credentials": [
        {"id": c["id"], "username": c["username"], "label": c["label"],
         "has_password": bool(c["password"])}
        for c in store.credentials()
    ]}


@app.post("/api/credentials")
async def add_credential(payload: dict = Body(...)):
    username = payload.get("username", "").strip()
    if not username:
        raise HTTPException(400, "falta usuario")
    cid = store.add_credential(username, payload.get("password", ""),
                               payload.get("label", ""))
    return {"id": cid}


@app.delete("/api/credentials/{cred_id}")
async def delete_credential(cred_id: int):
    store.delete_credential(cred_id)
    return {"ok": True}


def _settings_view(full: bool) -> dict:
    """Ajustes para el panel. `app.js` los pide al arrancar con CUALQUIER rol
    (`loadSettings`) y hace `s.ports.join(...)`, así que la forma no cambia: a un
    no-admin se le vacían los valores de red (segmento, puertos, tiempos) y solo
    se le dan el logo y el nombre del sitio (que ya son públicos en /api/me)."""
    out = {
        "logo": store.get("logo", ""),
        "site_name": store.get("site_name", "") or "",
    }
    if full:
        out.update({
            "cidr": store.get("cidr", DEFAULT_CIDR),
            "ports": store.get("ports", [554, 8554, 10554, 5554]),
            "channels": store.get("channels", 8),
            "concurrency": store.get("concurrency", 200),
            "connect_timeout": store.get("connect_timeout", 1.2),
        })
    else:
        out.update({"cidr": "", "ports": [], "channels": None,
                    "concurrency": None, "connect_timeout": None})
    return out


@app.get("/api/settings")
async def get_settings(request: Request):
    return _settings_view(request.state.user["role"] == "admin")


@app.post("/api/settings")
async def set_settings(payload: dict = Body(...)):
    for key in ("cidr", "ports", "channels", "concurrency", "connect_timeout", "logo"):
        if key in payload:
            store.set(key, payload[key])
    if "site_name" in payload:                  # nombre del taladro/sitio de esta instancia
        store.set("site_name", " ".join(str(payload["site_name"] or "").split())[:60])
    return _settings_view(True)               # POST solo lo alcanza admin (auth_gate)


def _recipient_view(r: dict) -> dict:
    return {"id": r["id"], "name": r["name"], "phone": r["phone"],
            "enabled": r.get("enabled", True)}


def _can_manage_alerts(request: Request) -> bool:
    """Admin y jefe de seguridad gestionan los avisos (destinatarios y reglas).
    El resto (viewer, kiosko) no ve teléfonos ni el estado del envío."""
    return request.state.user["role"] in ("admin", "seguridad")


def _require_alert_manager(request: Request) -> None:
    if not _can_manage_alerts(request):
        raise HTTPException(403, "requiere administrador o jefe de seguridad")


@app.get("/api/settings/alerts")
async def get_alert_settings(request: Request):
    """Avisos por WhatsApp/correo: canales listos (por entorno), destinatarios,
    reglas por cámara y historial. No devuelve secretos.

    - admin: todo.
    - seguridad: destinatarios (los gestiona), pero reglas e historial solo de
      SUS cámaras.
    - resto (viewer, kiosko): sin destinatarios (teléfonos), sin cola ni estado
      de WhatsApp; reglas e historial solo de sus cámaras."""
    rec = notifier.recipients()
    manager = _can_manage_alerts(request)
    cams = {}
    for sid in notifier.rules():
        s = store.stream(sid)
        if s and _user_allows(request, s["device_id"]):
            cams[sid] = notifier.rule(sid)

    def _visible(entry: dict) -> bool:
        if request.state.user["role"] == "admin":
            return True
        s = store.stream(entry.get("stream_id") or "")
        return bool(s) and _user_allows(request, s["device_id"])

    return {
        "channels": notifmod.channels(sum(1 for r in rec if r.get("enabled", True))),
        "site": notifier.site(),
        "recipients": [_recipient_view(r) for r in rec] if manager else [],
        "cameras": cams,
        "defaults": notifmod.DEFAULT_RULE,
        "labels": notifmod.LABELS,
        "pending": notifier.pending() if manager else 0,
        "log": [e for e in list(notifier.log) if _visible(e)][:30],
        "wa_state": (await notifier.wa_status())["state"] if manager else "",
    }


def _require_admin(request: Request) -> None:
    if request.state.user["role"] != "admin":
        raise HTTPException(403, "requiere administrador")


@app.get("/api/system/health")
async def system_health(request: Request):
    """Resumen de salud del equipo (CPU, RAM, disco, temperaturas, contenedores) vía Netdata."""
    _require_admin(request)
    return await sysmon.health()


@app.get("/api/settings/whatsapp")
async def whatsapp_status(request: Request):
    """Estado del dispositivo que ENVÍA los avisos (solo admin)."""
    _require_admin(request)
    return await notifier.wa_status(fresh=True)


@app.post("/api/settings/whatsapp/link")
async def whatsapp_link(request: Request, payload: dict = Body(default={})):
    """Vincular un dispositivo nuevo: devuelve QR (o código si se manda `phone`)."""
    _require_admin(request)
    try:
        return await notifier.wa_link(payload.get("phone", ""))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@app.post("/api/settings/whatsapp/unlink")
async def whatsapp_unlink(request: Request):
    """Desvincular el dispositivo actual (solo admin)."""
    _require_admin(request)
    try:
        return await notifier.wa_unlink()
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@app.post("/api/settings/alerts/recipients")
async def add_alert_recipient(request: Request, payload: dict = Body(...)):
    _require_alert_manager(request)
    try:
        rec = notifier.add_recipient(payload.get("name", ""), payload.get("phone", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    exists = await notifier.check_whatsapp(rec["phone"])
    warn = None
    if exists is False:
        notifier.delete_recipient(rec["id"])
        raise HTTPException(400, "ese número no tiene WhatsApp")
    if exists is None:
        warn = "No se pudo comprobar si el número tiene WhatsApp (se guardó igual)."
    return {"recipient": _recipient_view(rec), "warning": warn}


@app.put("/api/settings/alerts/recipients/{rid}")
async def put_alert_recipient(rid: str, request: Request, payload: dict = Body(...)):
    _require_alert_manager(request)
    try:
        return {"recipient": _recipient_view(notifier.update_recipient(rid, payload))}
    except KeyError:
        raise HTTPException(404, "destinatario desconocido")


@app.delete("/api/settings/alerts/recipients/{rid}")
async def delete_alert_recipient(rid: str, request: Request):
    _require_alert_manager(request)
    notifier.delete_recipient(rid)
    return {"ok": True}


@app.post("/api/settings/alerts/recipients/{rid}/test")
async def test_alert_recipient(rid: str, request: Request):
    """Mensaje de PRUEBA (solo texto) a UN número, para confirmar que le llega."""
    _require_alert_manager(request)
    rec = next((r for r in notifier.recipients() if r["id"] == rid), None)
    if not rec:
        raise HTTPException(404, "destinatario desconocido")
    if not notifmod.evo_ready():
        raise HTTPException(409, "WhatsApp no está configurado en el servidor")
    try:
        await notifier.send_text(
            rec["phone"],
            f"🧪 Prueba de CamWay — {notifier.site()}\n"
            f"Hola {rec['name']}, este número recibirá las alertas de seguridad con foto.")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))
    return {"ok": True}


@app.put("/api/settings/alerts/{stream_id}")
async def put_alert_rule(stream_id: str, request: Request, payload: dict = Body(...)):
    s = store.stream(stream_id)
    if not s:
        raise HTTPException(404, "stream desconocido")
    _require_camera(request, s["device_id"])
    return {"rule": notifier.set_rule(stream_id, payload)}


@app.post("/api/settings/alerts/test")
async def test_alert(request: Request, payload: dict = Body(...)):
    """Manda un aviso de PRUEBA con una foto actual de la cámara a TODOS los
    destinatarios activos (comprueba el flujo completo sin esperar un evento)."""
    sid = payload.get("stream_id")
    s = store.stream(sid or "")
    if not s:
        raise HTTPException(404, "stream desconocido")
    _require_camera(request, s["device_id"])
    ch = notifmod.channels(sum(1 for r in notifier.recipients() if r.get("enabled", True)))
    if not (ch["whatsapp"] or ch["email"]):
        raise HTTPException(409, "no hay destinatarios: agrega al menos un número")
    route = s["mtx_name"] or sid
    url = f"{os.environ.get('CAMWAY_MTX_RTSP', 'rtsp://mediamtx:8554')}/{route}"
    jpeg = await capture.snapshot(url, width=1280, cache_key="alert-test-" + sid)
    if jpeg is None:
        raise HTTPException(503, "no se pudo capturar un fotograma de la cámara")
    cls = (payload.get("cls") or "head")
    aid = notifier.enqueue(sid, cls if cls in notifmod.LABELS else "head", 0.99,
                           time.time(), jpeg, test=True)
    return {"ok": True, "id": aid, "channels": ch}


@app.get("/api/settings/encoding")
async def get_encoding_settings(request: Request):
    """Parámetros de calidad del transcode (resolución, fps, bitrate) + espacio
    de disco y nº de cámaras grabando, para estimar días de grabación en la UI.
    Solo admin: el panel solo lo pide en Ajustes de administrador."""
    _require_admin(request)
    import shutil
    try:
        du = shutil.disk_usage(RECORD_DIR)
        disk = {"total": du.total, "free": du.free, "used": du.used}
    except Exception:  # noqa: BLE001
        disk = None
    return {"encoding": mtxmod.current_encoding(), "disk": disk,
            "recording_cameras": len(store.get("recording") or {})}


@app.put("/api/settings/encoding")
async def set_encoding_settings(payload: dict = Body(...)):
    """Cambia la calidad del transcode (afecta vivo al maximizar y grabación) y
    la reaplica a todas las rutas. Admin."""
    enc = dict(store.get("encoding") or {})
    for k in ("max_width", "max_height", "max_fps", "max_bitrate", "preset"):
        if k in payload:
            enc[k] = payload[k]
    store.set("encoding", enc)
    mtxmod.set_encoding(max_width=enc.get("max_width"), max_height=enc.get("max_height"),
                        max_fps=enc.get("max_fps"), max_bitrate=enc.get("max_bitrate"),
                        preset=enc.get("preset"))
    await reconcile_published()      # reaplica el transcode a todas las rutas
    return {"encoding": mtxmod.current_encoding()}


# ---------------- interfaz ----------------

if os.path.isdir(WEB_DIR):
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(os.path.join(WEB_DIR, "index.html"))

    @app.get("/tv")
    async def tv_mode():
        """Modo TV: mosaico a pantalla completa por HLS, nativo y ligero para
        televisores (Android TV de poca RAM). Navegable con el control remoto."""
        return FileResponse(os.path.join(WEB_DIR, "tv.html"))

    @app.get("/live")
    async def live_grid():
        """Rejilla de video en VIVO por WebRTC (cliente embebido de taladro):
        pocas cámaras, baja latencia y bajo consumo (substream + decode por HW).
        Pensada para el modo kiosko (localhost sin login)."""
        return FileResponse(os.path.join(WEB_DIR, "live.html"))

    @app.get("/nvr")
    async def nvr_wizard():
        """Asistente para dar de alta un NVR por su IP (todos sus canales)."""
        return FileResponse(os.path.join(WEB_DIR, "nvr.html"))
