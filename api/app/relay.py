"""Relevo de video hacia un servidor remoto (Command Center), A DEMANDA.

Problema: por un túnel HTTP (Cloudflare) el WebRTC no pasa y el mosaico cae a fotogramas JPEG pixelados.
Solución: este CamWay EMPUJA (conexión de SALIDA, no abre puertos) el video por SRT cifrado a un MediaMTX del
servidor, que lo sirve como LL-HLS. Solo transmite mientras el servidor lo pide y lo mantiene vivo; si deja de
avisar, se apaga solo (un enlace Starlink no puede subir video 24 h).

TODO es de salida: este CamWay "sondea" al Command Center cada ~2 s ("estas son mis cámaras y esto transmito") y el
servidor contesta qué debe transmitir ahora. Funciona detrás de CGNAT/Starlink y de Cloudflare Access (nada entra).

Configuración (variables de entorno):
    CAMWAY_RELAY_CC         URL del Command Center, p.ej. https://rw.pacific-logging.com
    CAMWAY_RELAY_SOURCE     id de esta fuente en el Command Center (p.ej. oficina, pdv-107)
    CAMWAY_RELAY_KEY        clave de esa fuente (la misma que se registró en el Command Center)
    CAMWAY_RELAY_SRT        host:puerto SRT del servidor, p.ej. 187.77.221.60:8890
    CAMWAY_RELAY_USER/PASS  credenciales de publicación en MediaMTX
    CAMWAY_RELAY_PASSPHRASE frase de cifrado SRT (10–79 caracteres)
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Optional
from urllib.parse import quote

log = logging.getLogger("camway.relay")

# bitrate acotado por perfil: lo que cabe en un enlace de subida modesto
PROFILES = {
    "bajo":  {"w": 854,  "h": 480,  "fps": 12, "kbps": 400},
    "medio": {"w": 1280, "h": 720,  "fps": 15, "kbps": 1000},
    "alto":  {"w": 1920, "h": 1080, "fps": 25, "kbps": 2500},
}
IDLE_STOP_S = 60.0            # si el servidor no contesta en este tiempo, se apaga sola
PATH_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")


def enabled() -> bool:
    return bool(os.environ.get("CAMWAY_RELAY_CC") and os.environ.get("CAMWAY_RELAY_SOURCE") and os.environ.get("CAMWAY_RELAY_KEY")
                and os.environ.get("CAMWAY_RELAY_SRT") and os.environ.get("CAMWAY_RELAY_USER")
                and os.environ.get("CAMWAY_RELAY_PASS") and os.environ.get("CAMWAY_RELAY_PASSPHRASE"))


def srt_url(path: str, profile: str) -> str:
    host = os.environ["CAMWAY_RELAY_SRT"]
    sid = f"publish:{path}:{os.environ['CAMWAY_RELAY_USER']}:{os.environ['CAMWAY_RELAY_PASS']}"
    p = os.environ["CAMWAY_RELAY_PASSPHRASE"]   # generada alfanumérica: ffmpeg no decodifica el porcentaje
    # latency en µs para libsrt vía ffmpeg: más margen en enlaces con pérdida (satelital)
    return f"srt://{host}?streamid={sid}&passphrase={p}&pbkeylen=16&latency=1500000&mode=caller&connect_timeout=4000"


def command(source: str, path: str, profile: str) -> list[str]:
    pr = PROFILES[profile]
    vf = (f"scale='min({pr['w']},iw)':'min({pr['h']},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,"
          f"fps={pr['fps']}")
    # analyzeduration/probesize acotados: por omisión ffmpeg "analiza" hasta 5 s de la cámara antes de empezar a enviar, y eso se sumaba al tiempo hasta ver imagen
    return ["ffmpeg", "-nostdin", "-loglevel", "warning", "-rtsp_transport", "tcp", "-fflags", "nobuffer", "-flags", "low_delay",
            "-analyzeduration", "1000000", "-probesize", "1000000", "-i", source,
            "-an", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-profile:v", "main",
            "-b:v", f"{pr['kbps']}k", "-maxrate", f"{pr['kbps']}k", "-bufsize", f"{pr['kbps'] * 2}k",
            "-g", str(pr["fps"]), "-keyint_min", str(pr["fps"]), "-sc_threshold", "0", "-pix_fmt", "yuv420p",
            "-f", "mpegts", srt_url(path, profile)]


class Job:
    def __init__(self, path: str, source: str, profile: str) -> None:
        self.path, self.source, self.profile = path, source, profile
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.last = time.time()
        self.started = 0.0
        self.restarts = 0
        self.err = ""
        self.task: Optional[asyncio.Task] = None
        self.stop_flag = False


class RelayManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}

    async def _run(self, j: Job) -> None:
        while not j.stop_flag:
            try:
                j.proc = await asyncio.create_subprocess_exec(*command(j.source, j.path, j.profile),
                                                              stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                j.started = time.time()
                _, err = await j.proc.communicate()
                j.err = (err or b"").decode("utf-8", "replace").strip().splitlines()[-1][:200] if err else ""
            except Exception as e:  # noqa: BLE001
                j.err = f"{type(e).__name__}: {e}"
            if j.stop_flag:
                break
            j.restarts = 0 if time.time() - j.started > 30 else j.restarts + 1
            log.warning("relevo %s cayó (%s); reintento", j.path, j.err)
            await asyncio.sleep(min(15, 2 + j.restarts * 2))

    async def start(self, path: str, source: str, profile: str) -> dict:
        if profile not in PROFILES or not PATH_RE.match(path):
            raise ValueError("perfil o ruta inválidos")
        j = self.jobs.get(path)
        if j and j.profile == profile and j.source == source and not j.stop_flag:
            j.last = time.time()
            return self.status_of(j)
        if j:
            await self.stop(path)
        j = Job(path, source, profile)
        self.jobs[path] = j
        j.task = asyncio.create_task(self._run(j))
        log.info("relevo %s iniciado (%s)", path, profile)
        return self.status_of(j)

    def keepalive(self, path: str) -> Optional[dict]:
        j = self.jobs.get(path)
        if not j:
            return None
        j.last = time.time()
        return self.status_of(j)

    async def stop(self, path: str) -> None:
        j = self.jobs.pop(path, None)
        if not j:
            return
        j.stop_flag = True
        if j.proc and j.proc.returncode is None:
            try:
                j.proc.terminate()
                await asyncio.wait_for(j.proc.wait(), 5)
            except Exception:  # noqa: BLE001
                try:
                    j.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        if j.task:
            j.task.cancel()
        log.info("relevo %s detenido", path)

    async def reaper(self) -> None:
        while True:
            await asyncio.sleep(10)
            now = time.time()
            for p in [p for p, j in self.jobs.items() if now - j.last > IDLE_STOP_S]:
                log.info("relevo %s sin keepalive: se apaga", p)
                await self.stop(p)

    @staticmethod
    def status_of(j: Job) -> dict:
        alive = bool(j.proc and j.proc.returncode is None)
        return {"path": j.path, "profile": j.profile, "running": alive, "restarts": j.restarts, "error": j.err,
                "up_s": int(time.time() - j.started) if alive else 0}

    def status(self) -> list[dict]:
        return [self.status_of(j) for j in self.jobs.values()]


relay = RelayManager()


async def agent(list_cameras, resolve) -> None:
    """Sondea al Command Center. `list_cameras()` -> [{stream,title,width,height,codec}]; `resolve(stream)` -> url RTSP local o None."""
    import httpx
    if not enabled():
        log.info("relevo de video apagado (faltan variables CAMWAY_RELAY_*)")
        return
    url = os.environ["CAMWAY_RELAY_CC"].rstrip("/") + "/api/video/agent/sync"
    hdr = {"X-Relay-Source": os.environ["CAMWAY_RELAY_SOURCE"], "X-Relay-Key": os.environ["CAMWAY_RELAY_KEY"],
           "User-Agent": "Mozilla/5.0 (CamWay-relay) Chrome/120"}
    last_cams = 0.0
    last_ok = time.time()
    fails = 0
    # sondeo largo: el servidor retiene la respuesta (hasta 10 s) mientras no haya nada nuevo y contesta al instante cuando alguien empieza a mirar,
    # en vez de enterarse en el siguiente sondeo (hasta 2 s después). Si el servidor es antiguo y no lo soporta, responde YA y se sigue cada 2 s.
    hdr["X-Relay-Wait"] = "10"
    async with httpx.AsyncClient(timeout=25.0, headers=hdr) as c:
        while True:
            try:
                body: dict = {"running": [{"path": j["path"], "profile": j["profile"], "running": j["running"], "error": j["error"]}
                                          for j in relay.status()]}
                if time.time() - last_cams > 60:
                    body["cameras"] = list_cameras()
                r = await c.post(url, json=body)
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                if "cameras" in body:
                    last_cams = time.time()
                want = {w["path"]: w for w in r.json().get("relays", [])}
                for path, w in want.items():
                    src = resolve(str(w.get("stream")))
                    if src:
                        await relay.start(path, src, str(w.get("profile") or "medio"))
                for path in [p for p in relay.jobs if p not in want]:
                    await relay.stop(path)
                fails = 0
                last_ok = time.time()
                await asyncio.sleep(0.3 if r.json().get("wait") else 2.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                fails += 1
                if time.time() - last_ok > IDLE_STOP_S:          # el servidor no contesta: no se sigue subiendo video a ciegas
                    for p in list(relay.jobs):
                        await relay.stop(p)
                if fails in (1, 10) or fails % 60 == 0:
                    log.warning("relevo: sin respuesta del Command Center (%s)", e)
                await asyncio.sleep(min(30, 2 + fails * 2))
