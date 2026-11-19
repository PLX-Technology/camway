"""Mosaico del SERVIDOR: compone N cámaras en UN solo H.264.

El cliente decodifica 1 stream en vez de N → casi un thin-client. CamWay corre
un ffmpeg que lee las rutas YA publicadas en MediaMTX (sin recargar las cámaras:
para H.265 lee la ruta H.264 que CamWay ya transcodifica para grabar/ver), las
compone con `xstack` y publica el resultado como la ruta `mosaico`.

Dinámico: layout 4/9/16 y paginado; al cambiar se reinicia el ffmpeg con las
cámaras de esa página. Todo es 16:9 (celdas 16:9) para que el cliente lo muestre
sin deformar y las etiquetas por celda cuadren por porcentaje.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import Optional

log = logging.getLogger("camway.mosaic")

MTX_RTSP = os.environ.get("CAMWAY_MTX_RTSP", "rtsp://mediamtx:8554")
MOSAIC_NAME = "mosaico"
BITRATE = os.environ.get("CAMWAY_MOSAIC_BITRATE", "3500k")

# Tamaño de celda por layout (todo 16:9; total <= 1920x1080).
_CELL = {1: (1280, 720), 4: (640, 360), 9: (480, 270), 16: (480, 270)}

# Watchdog del mosaico: el ffmpeg puede quedar VIVO pero ATASCADO (sin publicar)
# cuando una cámara se congela y se recupera. Como proc.wait() no lo detecta
# (el proceso no muere), se comprueba cada _STALL_INTERVAL que la ruta 'mosaico'
# siga 'ready' en MediaMTX; si no publica durante _STALL_CHECKS chequeos seguidos,
# se mata y relanza.
_STALL_INTERVAL = 10   # segundos entre chequeos
_STALL_CHECKS = 3      # 3 chequeos sin publicar (~30 s) → reiniciar


# Imagen de "sin señal" (fondo oscuro + logo CamWay semitransparente), cacheada
# por tamaño de celda. Se usa para las celdas vacías del mosaico.
_EMPTY: dict = {}


async def _ensure_empty(cw, ch):
    key = (cw, ch)
    if key in _EMPTY:
        return _EMPTY[key]
    path = f"/tmp/mosaic_empty_{cw}x{ch}.png"
    if os.path.exists(path) and os.path.getsize(path) > 0:   # un 0 bytes (generación cortada) no vale
        _EMPTY[key] = path
        return path
    web = os.environ.get("CAMWAY_WEB_DIR") or "/app/web"
    logo = os.path.join(web, "login-logo.png")
    if not os.path.exists(logo):
        _EMPTY[key] = None
        return None
    lw = max(64, int(cw * 0.45))
    tmp = path + ".tmp.png"
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", f"color=c=0x0b0f14:s={cw}x{ch}",
           "-i", logo,
           "-filter_complex",
           f"[1:v]scale={lw}:-1,format=rgba,colorchannelmixer=aa=0.20[lg];"
           f"[0:v][lg]overlay=(W-w)/2:(H-h)/2[v]",
           "-map", "[v]", "-frames:v", "1", tmp]
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(p.wait(), timeout=15)
        if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, path)           # se publica entero o no se publica
    except Exception:  # noqa: BLE001
        pass
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    ok = os.path.exists(path) and os.path.getsize(path) > 0
    if ok:
        _EMPTY[key] = path                  # si falló no se cachea: se reintenta en la próxima
    return path if ok else None


def _ff_args(route_names, cols, rows, cw, ch, empty_img=None) -> list:
    n = cols * rows
    args = ["ffmpeg", "-nostdin", "-loglevel", os.environ.get("CAMWAY_MOSAIC_LOGLEVEL", "warning")]
    fc = []
    for k in range(n):
        name = route_names[k] if k < len(route_names) else None
        if name:
            # analyzeduration/probesize bajos: ffmpeg por defecto analiza hasta
            # 5s cada entrada antes de arrancar → el mosaico tardaba ~10s en salir.
            args += ["-rtsp_transport", "tcp", "-fflags", "nobuffer",
                     "-analyzeduration", "1000000", "-probesize", "1000000",
                     "-i", f"{MTX_RTSP}/{name}"]
            fc.append(f"[{k}:v]fps=15,scale={cw}:{ch}:force_original_aspect_ratio=increase,"
                      f"crop={cw}:{ch},setsar=1,format=yuv420p[v{k}]")
        elif empty_img:
            # Celda sin señal: logo CamWay semitransparente (imagen fija en bucle).
            args += ["-loop", "1", "-framerate", "15", "-i", empty_img]
            fc.append(f"[{k}:v]scale={cw}:{ch},setsar=1,format=yuv420p[v{k}]")
        else:
            args += ["-f", "lavfi", "-i", f"color=c=0x0b0f14:s={cw}x{ch}:r=15"]
            fc.append(f"[{k}:v]setsar=1,format=yuv420p[v{k}]")
    if n == 1:
        # xstack exige 2 entradas o más: con una sola celda (rejilla 1×1) la
        # imagen pasa tal cual.
        chain = fc[0] + ";[v0]null[v]"
    else:
        layout = "|".join(f"{(k % cols) * cw}_{(k // cols) * ch}" for k in range(n))
        chain = (";".join(fc) + ";" + "".join(f"[v{k}]" for k in range(n))
                 + f"xstack=inputs={n}:layout={layout}[v]")
    args += ["-filter_complex", chain, "-map", "[v]",
             "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
             "-profile:v", "baseline", "-pix_fmt", "yuv420p",
             "-b:v", BITRATE, "-maxrate", BITRATE, "-bufsize", BITRATE,
             "-g", "15", "-an",   # keyframe cada ~1s: el cliente arranca más rápido
             "-f", "rtsp", "-rtsp_transport", "tcp", f"{MTX_RTSP}/{MOSAIC_NAME}"]
    return args


class MosaicManager:
    def __init__(self) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None
        self._want = False
        self._lock = asyncio.Lock()
        self._args: list = []
        self._cfg = None     # (layout, page, tuple(page_names)) en curso
        self._mtx = None     # cliente MediaMTX para el watchdog (se fija en set())

    async def set(self, mtx, route_names_all, layout, page) -> dict:
        """(Re)arma el mosaico. `route_names_all` = nombres de ruta publicados,
        en orden. Devuelve whep + info de layout/paginado."""
        self._mtx = mtx
        if layout not in _CELL:
            layout = 4
        cols = int(round(math.sqrt(layout))) if layout > 1 else 1
        rows = math.ceil(layout / cols)
        cw, ch = _CELL[layout]
        per = layout
        pages = max(1, math.ceil(len(route_names_all) / per)) if route_names_all else 1
        page = max(0, min(int(page or 0), pages - 1))
        page_names = route_names_all[page * per: page * per + per]
        cfg = (layout, page, tuple(page_names))
        # Imagen de "sin señal" (logo CamWay) para las celdas vacías de este layout.
        empty_img = await _ensure_empty(cw, ch) if len(page_names) < per else None
        async with self._lock:
            await mtx.ensure_publisher_path(MOSAIC_NAME)
            self._want = True
            alive = self._proc is not None and self._proc.returncode is None
            if cfg == self._cfg and alive:
                pass                        # ya corre con esta config: no reiniciar
            else:
                self._cfg = cfg
                self._args = _ff_args(page_names, cols, rows, cw, ch, empty_img)
                await self._restart()
        return {"whep": f"/whep/{MOSAIC_NAME}", "layout": layout, "page": page,
                "pages": pages, "cols": cols, "rows": rows, "per_page": per}

    def current_names(self) -> list:
        """Rutas (mtx_name) de las cámaras que componen el mosaico ahora mismo, o
        [] si no hay mosaico armado. Sirve para decidir quién puede verlo: el
        ffmpeg es UNO para todos, así que quien lo lea ve TODAS estas cámaras."""
        return list(self._cfg[2]) if self._cfg else []

    async def _restart(self) -> None:
        await self._kill()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._args, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            log.info("mosaico ffmpeg lanzado pid=%s", self._proc.pid)
        except Exception as e:  # noqa: BLE001
            log.warning("no se pudo lanzar el mosaico: %s", e)
            return
        self._task = asyncio.create_task(self._watch(self._proc))

    async def _watch(self, proc) -> None:
        """Vigila el ffmpeg del mosaico: lo relanza si MUERE o si se ATASCA
        (deja de publicar en MediaMTX aunque el proceso siga vivo)."""
        stale = 0
        while True:
            await asyncio.sleep(_STALL_INTERVAL)
            if not self._want or self._proc is not proc:
                return
            if proc.returncode is not None:
                break                          # murió solo
            if await self._is_publishing():
                stale = 0
            else:
                stale += 1
                if stale >= _STALL_CHECKS:
                    log.warning("mosaico ffmpeg sin publicar durante %ds → reiniciando",
                                stale * _STALL_INTERVAL)
                    break
        # (re)lanzar con los mismos args
        if self._want and self._proc is proc:
            await asyncio.sleep(2)
            async with self._lock:
                if self._want and self._proc is proc:
                    await self._restart()

    async def _is_publishing(self) -> bool:
        """True si la ruta 'mosaico' está lista en MediaMTX (el ffmpeg publica)."""
        if self._mtx is None:
            return True                          # sin cliente: no reiniciar a ciegas
        try:
            paths = await self._mtx.runtime_paths()
            p = paths.get(MOSAIC_NAME)
            return bool(p and p.get("ready"))
        except Exception:                        # noqa: BLE001
            return True                          # ante la duda, no reiniciar

    async def _kill(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.kill()
                await self._proc.wait()
            except ProcessLookupError:
                pass
        self._proc = None

    async def stop(self, mtx) -> None:
        async with self._lock:
            self._want = False
            self._cfg = None             # ya no hay mosaico: nadie debe poder leerlo
            await self._kill()
        try:
            await mtx.unpublish(MOSAIC_NAME)
        except Exception:  # noqa: BLE001
            pass


mosaic = MosaicManager()
