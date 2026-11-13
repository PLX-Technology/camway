"""Mosaico por fotogramas.

Un ffmpeg persistente por cámara publicada extrae ~1-2 fps desde MediaMTX
(reusa la ÚNICA conexión RTSP que MediaMTX mantiene contra la cámara) y deja
el último JPEG en memoria. Los navegadores lo leen de ahí, compartido entre
todos los espectadores: el coste no crece con cuánta gente mire, y el
navegador ya no decodifica 25 vídeos H.264 a la vez (lo que provocaba
"reader is too slow" y las desconexiones). El vídeo WebRTC en vivo solo se
abre al maximizar una cámara.

Frente a capture.snapshot (un proceso ffmpeg y una conexión RTSP nueva a la
cámara por cada pedido), aquí hay un proceso estable por cámara y cero
conexiones extra a la cámara.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

log = logging.getLogger("camway.wall")

MTX_RTSP = os.environ.get("CAMWAY_MTX_RTSP", "rtsp://mediamtx:8554")
FPS = os.environ.get("CAMWAY_WALL_FPS", "1.5")
WIDTH = int(os.environ.get("CAMWAY_WALL_WIDTH", "480"))
QUALITY = os.environ.get("CAMWAY_WALL_Q", "7")
# No usar NUNCA el fallback de leer la cámara DIRECTO (saltándose MediaMTX).
# En DVRs con pocas sesiones simultáneas (p.ej. el taladro) ese fallback abre
# una conexión EXTRA por cámara y le roba los slots a los transcodes live,
# creando un deadlock: los live no arrancan → la ruta MediaMTX queda vacía →
# wallcache sigue directo. Con esto wallcache solo lee de MediaMTX (compartido,
# cero conexiones extra al DVR); si la ruta está vacía, su lectura dispara el
# runOnDemand y espera, sin competir por slots.
NO_DIRECT = os.environ.get("CAMWAY_WALL_NO_DIRECT", "").strip().lower() in ("1", "true", "yes", "si")

_SOI = b"\xff\xd8"   # inicio de imagen JPEG
_EOI = b"\xff\xd9"   # fin de imagen JPEG
_MAX_BUF = 8_000_000


# Si el productor (ffmpeg/MediaMTX) no actualiza en este tiempo, se considera
# que el vídeo normal está caído y se cae al fotograma del detector.
FRESH_S = 4.0
# Cuánto vale un fotograma del detector antes de descartarlo.
DET_MAX_AGE = 12.0
# Si NO llega ningún fotograma nuevo (ni productor ni detector) en este tiempo,
# se deja de mostrar el último congelado y el muro pasa a "sin conexión" — así
# una cámara colgada no engaña pareciendo en vivo.
STALE_HIDE = 30.0
# Si ffmpeg no entrega NADA en este tiempo, se considera congelado: se mata y
# se reconecta (evita el frame pegado de fuentes MJPEG por HTTP como la 109).
STALL_S = 8.0


class WallCache:
    def __init__(self) -> None:
        self._frames: dict[str, tuple[float, bytes]] = {}       # productor (ffmpeg)
        self._det_frames: dict[str, tuple[float, bytes]] = {}   # detector (OpenCV), fallback
        self._tasks: dict[str, asyncio.Task] = {}
        self._want: set[str] = set()
        self._direct: dict[str, str] = {}   # name -> URL directo de la cámara (fallback)
        self._src: dict[str, str] = {}      # name -> ruta MediaMTX a leer (def: el propio name)

    def set_detector_frame(self, name: str, jpg: bytes) -> None:
        """El detector (que lee la cámara con OpenCV, más tolerante) deja aquí
        sus fotogramas; se usan solo si el productor normal está caído."""
        self._det_frames[name] = (time.time(), jpg)

    def _best(self, name: str) -> Optional[tuple[float, bytes]]:
        now = time.time()
        prod = self._frames.get(name)
        if prod and now - prod[0] < FRESH_S:
            return prod                       # vídeo normal al día: usarlo (auto-switch)
        det = self._det_frames.get(name)
        if det and now - det[0] < DET_MAX_AGE:
            return det                        # degradado: lo poquito que pesca el detector
        # Último recurso: el fotograma más reciente que haya — pero solo si no
        # lleva demasiado tiempo congelado; pasado STALE_HIDE → "sin conexión".
        best = max((h for h in (prod, det) if h), key=lambda h: h[0], default=None)
        if best and now - best[0] < STALE_HIDE:
            return best
        return None

    def frame(self, name: str) -> Optional[bytes]:
        hit = self._best(name)
        return hit[1] if hit else None

    def frame_ts(self, name: str) -> float:
        hit = self._best(name)
        return hit[0] if hit else 0.0

    def age(self, name: str) -> Optional[float]:
        hit = self._best(name)
        return (time.time() - hit[0]) if hit else None

    async def ensure(self, cameras, src_names: Optional[dict] = None) -> None:
        """Arranca un productor por cada cámara y para las que sobran.
        `cameras` puede ser un dict {name: url_directo} (habilita el fallback)
        o un iterable de nombres. `src_names` opcional mapea name -> la RUTA de
        MediaMTX a leer (por defecto el propio name); sirve para que una cámara
        con substream alimente el muro desde su flujo ligero `<name>__lo`."""
        self._src = dict(src_names or {})
        if isinstance(cameras, dict):
            self._direct = dict(cameras)
            names = set(cameras)
        else:
            names = set(cameras)
        self._want = names
        for n in names:
            t = self._tasks.get(n)
            if t is None or t.done():
                self._tasks[n] = asyncio.create_task(self._run(n))
        for n in list(self._tasks):
            if n not in names:
                self._tasks.pop(n).cancel()
                self._frames.pop(n, None)
                self._direct.pop(n, None)

    def _input_flags(self, url: str) -> list[str]:
        # probesize/analyzeduration con margen: con 32 bytes / 0 µs ffmpeg NO
        # logra sacar los parámetros del códec de algunas fuentes (p.ej. una ruta
        # transcodificada cuyo SDP no trae SPS/PPS) y no produce NADA. ffmpeg
        # para de sondear en cuanto tiene los parámetros, así que subir estos
        # topes no añade latencia a las cámaras que ya iban bien.
        base = ["-fflags", "nobuffer", "-flags", "low_delay",
                "-probesize", "1000000", "-analyzeduration", "2000000"]
        if url.startswith("rtsp://"):
            return ["-rtsp_transport", "tcp"] + base
        if url.startswith(("http://", "https://")):
            return base + ["-reconnect", "1", "-reconnect_streamed", "1",
                           "-reconnect_delay_max", "2"]
        return base

    async def _run_source(self, name: str, url: str) -> bool:
        """Corre ffmpeg sobre `url`, guarda los JPEG y devuelve True si produjo
        al menos un fotograma."""
        proc = None
        produced = False
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-loglevel", "error",
                *self._input_flags(url), "-i", url,
                "-an", "-vf", f"fps={FPS},scale={WIDTH}:-2",
                "-q:v", QUALITY, "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            buf = bytearray()
            while name in self._want:
                try:
                    # Watchdog: si ffmpeg se queda pegado (fuente MJPEG por HTTP
                    # que cuelga la conexión, como la 109) deja de llegar data
                    # pero el proceso no muere y el frame se congelaría para
                    # siempre. Cortar por timeout fuerza reconectar en _run.
                    chunk = await asyncio.wait_for(proc.stdout.read(65536),
                                                   timeout=STALL_S)
                except asyncio.TimeoutError:
                    log.info("productor %s congelado >%ss, reiniciando", name, STALL_S)
                    break
                if not chunk:
                    break                       # ffmpeg terminó
                buf += chunk
                while True:
                    start = buf.find(_SOI)
                    if start < 0:
                        if len(buf) > _MAX_BUF:
                            del buf[:-2]
                        break
                    end = buf.find(_EOI, start + 2)
                    if end < 0:
                        if start > 0:
                            del buf[:start]
                        break
                    self._frames[name] = (time.time(), bytes(buf[start:end + 2]))
                    del buf[:end + 2]
                    produced = True
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.kill()
            raise
        except Exception as e:  # noqa: BLE001
            log.debug("productor %s (%s): %s", name, url, e)
        finally:
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
        return produced

    async def _run(self, name: str) -> None:
        mtx_url = f"{MTX_RTSP}/{self._src.get(name, name)}"
        backoff = 2.0
        while name in self._want:
            # Preferir MediaMTX: una sola conexión a la cámara, repartida.
            produced = await self._run_source(name, mtx_url)
            # Band-aid: si MediaMTX no entregó NADA (ruta caída / cámara
            # flapeando), leer la cámara DIRECTO como hace el detector — su
            # cliente es más tolerante y pesca los fotogramas que sí llegan.
            # Se desactiva con CAMWAY_WALL_NO_DIRECT (DVRs con pocas sesiones).
            if not produced and name in self._want and not NO_DIRECT:
                direct = self._direct.get(name)
                if direct and direct != mtx_url:
                    await self._run_source(name, direct)
            if name in self._want:
                await asyncio.sleep(backoff)
                backoff = 3.0 if produced else min(backoff * 1.5, 15.0)

    async def stop_all(self) -> None:
        self._want.clear()
        for t in list(self._tasks.values()):
            t.cancel()
        self._tasks.clear()
        self._frames.clear()


wall = WallCache()
