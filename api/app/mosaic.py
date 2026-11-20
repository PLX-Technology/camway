"""Mosaico del SERVIDOR: compone N cámaras en UN solo H.264.

El cliente decodifica 1 stream en vez de N → casi un thin-client. CamWay corre
un ffmpeg que lee las rutas YA publicadas en MediaMTX (sin recargar las cámaras:
para H.265 lee la ruta H.264 que CamWay ya transcodifica para grabar/ver), las
compone por capas con `overlay` sobre un fondo a 15 fps y publica el resultado
como la ruta `mosaico`.

Dinámico: layout 4/9/16 y paginado; al cambiar se reinicia el ffmpeg con las
cámaras de esa página. Todo es 16:9 (celdas 16:9) para que el cliente lo muestre
sin deformar y las etiquetas por celda cuadren por porcentaje.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from typing import Optional

log = logging.getLogger("camway.mosaic")

MTX_RTSP = os.environ.get("CAMWAY_MTX_RTSP", "rtsp://mediamtx:8554")
MOSAIC_NAME = "mosaico"
BITRATE = os.environ.get("CAMWAY_MOSAIC_BITRATE", "3500k")

# Tamaño de celda por layout (todo 16:9; total <= 1920x1080).
_CELL = {1: (1280, 720), 4: (640, 360), 9: (480, 270), 16: (480, 270)}

# Rotulación quemada en el mosaico: barra inferior con la fecha/hora, dibujada
# por CamWay con drawtext. El nombre de la cámara arriba es opcional y viene
# apagado (CAMWAY_MOSAIC_TITLE=1 para volver a ponerlo).
#
# Por qué aquí y no en la cámara: las celdas recortan la imagen para llenar el
# 16:9, y una cámara 4:3 (p.ej. la .32, 704x576) pierde el 15,6 % de arriba y
# el 15,6 % de abajo, que es justo donde el fabricante pone su OSD. Además el
# OSD de la cámara no siempre se puede activar (las iCamra aceptan el SetOSD y
# lo ignoran). Dibujarlo después del recorte garantiza que se vea siempre.
# Desactivar del todo con CAMWAY_MOSAIC_LABELS=0.
_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
LABELS = os.environ.get("CAMWAY_MOSAIC_LABELS", "1") not in ("0", "false", "no", "")
TITLE = os.environ.get("CAMWAY_MOSAIC_TITLE", "0") not in ("0", "false", "no", "")
# Qué celdas llevan el reloj de CamWay:
#   auto (defecto) → solo las que se RECORTAN al pasar a 16:9, porque ahí se
#                    pierde el reloj que quema la propia cámara. Las cámaras
#                    16:9 ya traen el suyo y pintar otro lo solaparía.
#   all            → todas.  off → ninguna.
CLOCK_MODE = (os.environ.get("CAMWAY_MOSAIC_CLOCK") or "auto").strip().lower()


def _needs_clock(name: str, sizes: dict, cw: int, ch: int) -> bool:
    """¿Hay que pintar el reloj en esta celda? Con `auto`, solo si la imagen
    se recorta (relación de aspecto distinta a la celda), que es el caso en el
    que el OSD de la cámara queda fuera del encuadre."""
    if CLOCK_MODE == "off":
        return False
    if CLOCK_MODE == "all":
        return True
    wh = (sizes or {}).get(name)
    if not wh or not wh[0] or not wh[1]:
        return True                 # sin datos de resolución: pintar (no perderlo)
    sw, sh = float(wh[0]), float(wh[1])
    return abs((sw / sh) - (cw / ch)) > 0.02


def _label_file(k: int, text: str) -> str:
    """Escribe el nombre de la celda en un archivo: drawtext:textfile evita
    todo el infierno de escapes (comillas, dos puntos, comas, %) del texto
    en línea. Se reescribe en cada montaje del mosaico."""
    path = f"/tmp/mosaic_label_{k}.txt"
    clean = " ".join((text or "").split())[:30]
    with open(path, "w", encoding="utf-8") as f:
        f.write(clean or " ")
    return path


def _drawtext(k: int, label: str, ch: int) -> str:
    """Barra inferior de la celda: banda negra de TODO el ancho con la
    fecha/hora a la derecha (y el nombre a la izquierda si CAMWAY_MOSAIC_TITLE).

    La banda es lo que hace que el rótulo del visor (que se dibuja sobre el
    video, abajo a la izquierda) quede sobre el mismo negro y no se vean dos
    recuadros distintos pegados.
    """
    fs = max(12, ch // 20)
    bh = fs + 14                      # alto de la banda
    banda = f",drawbox=x=0:y=ih-{bh}:w=iw:h={bh}:color=black@0.45:t=fill"
    # Reloj: expansion=strftime evalúa el formato en cada fotograma; los ':' del
    # formato van escapados para que el parser del filtro no los corte.
    reloj = (f",drawtext=fontfile={_FONT}:text='%Y-%m-%d %H\\:%M\\:%S'"
             f":expansion=strftime:x=w-tw-10:y=h-{bh}+({bh}-th)/2"
             f":fontsize={fs}:fontcolor=white")
    if not TITLE:
        return banda + reloj
    return (banda
            + f",drawtext=fontfile={_FONT}:textfile={_label_file(k, label)}"
              f":x=10:y=h-{bh}+({bh}-th)/2:fontsize={fs}:fontcolor=white"
            + reloj)

# Watchdog del mosaico: el ffmpeg puede quedar VIVO pero ATASCADO (sin publicar)
# cuando una cámara se congela y se recupera. Como proc.wait() no lo detecta
# (el proceso no muere), se comprueba cada _STALL_INTERVAL que la ruta 'mosaico'
# siga creciendo en MediaMTX; si no crece durante _STALL_CHECKS chequeos
# seguidos, se mata y relanza (recalculando antes qué cámaras están vivas).
_STALL_INTERVAL = 5    # segundos entre chequeos
_STALL_CHECKS = 2      # 2 chequeos sin crecer (~10 s) → reiniciar
_GRACE = 20            # segundos tras arrancar antes de evaluar (deja publicar)
_REARM_MIN = 30        # mínimo entre rearranques por cambio de cámaras (anti-flapping)
_RSS_MAX_MB = 800      # si el ffmpeg pasa de esto, se relanza (red de seguridad)


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


def _rss_mb(pid: int):
    """Memoria residente del proceso en MB (None si ya no existe)."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        return None
    return None


async def _empty_for(page_ready, cw, ch):
    """Imagen de 'sin señal' solo si hace falta (alguna celda caída o vacía)."""
    if not any(n is None for n in page_ready):
        return None
    return await _ensure_empty(cw, ch)


def _ff_args(route_names, cols, rows, cw, ch, empty_img=None, titles=None, sizes=None) -> list:
    """Compone con `overlay` sobre un fondo que SIEMPRE late a 15 fps.

    Antes se usaba `xstack`: esa cadena exige un fotograma de TODAS las
    entradas para poder emitir uno. Si una cámara tiene un microcorte (RTSP
    deja de entregar 1-2 s), xstack deja de producir y el mosaico ENTERO se
    congela aunque las demás cámaras sigan vivas — y como la ruta seguía
    'ready' en MediaMTX, el watchdog no lo detectaba.

    Con `overlay`, el fondo (color) marca el ritmo: si una cámara se para, su
    celda repite el último fotograma y el resto sigue en vivo. Nunca se
    congela el mosaico completo.
    """
    n = cols * rows
    total_w, total_h = cols * cw, rows * ch
    args = ["ffmpeg", "-nostdin", "-loglevel", os.environ.get("CAMWAY_MOSAIC_LOGLEVEL", "warning"),
            "-f", "lavfi", "-i", f"color=c=0x0b0f14:s={total_w}x{total_h}:r=15"]
    fc = ["[0:v]format=yuv420p[base]"]
    prev = "base"
    idx = 1                          # índice de la próxima entrada de ffmpeg
    for k in range(n):
        name = route_names[k] if k < len(route_names) else None
        x, y = (k % cols) * cw, (k // cols) * ch
        if name:
            # analyzeduration/probesize bajos: ffmpeg por defecto analiza hasta
            # 5s cada entrada antes de arrancar → el mosaico tardaba ~10s en salir.
            args += ["-rtsp_transport", "tcp", "-fflags", "nobuffer",
                     "-analyzeduration", "1000000", "-probesize", "1000000",
                     # 16 hilos por decodificador (uno por CPU) hinchan el
                     # proceso a ~500 MB: con 2 basta de sobra para 640x360.
                     "-threads", "2",
                     "-thread_queue_size", "64",
                     # Si la cámara deja de entregar datos, el socket da error a
                     # los 10 s (microsegundos) en vez de quedarse esperando para
                     # siempre: el proceso sale y el watchdog lo relanza sin esa
                     # cámara. En ffmpeg 7.x el nombre es `-timeout`; `-rw_timeout`
                     # ya no existe como opción de entrada y aborta el arranque.
                     "-timeout", "10000000",
                     "-i", f"{MTX_RTSP}/{name}"]
            fc.append(f"[{idx}:v]fps=15,scale={cw}:{ch}:force_original_aspect_ratio=increase,"
                      f"crop={cw}:{ch},setsar=1,format=yuv420p[c{k}]")
        elif empty_img:
            # Celda sin señal: logo CamWay semitransparente (imagen fija en bucle).
            # `-re` es imprescindible: sin él, image2 decodifica el PNG a la
            # velocidad de la CPU (no a 15 fps) y el proceso se come la memoria
            # hasta que el contenedor muere por OOM (~40 MB/min). Con `-re`
            # entrega exactamente 15 fps y el consumo queda plano.
            args += ["-re", "-loop", "1", "-framerate", "15", "-i", empty_img]
            fc.append(f"[{idx}:v]fps=15,scale={cw}:{ch},setsar=1,format=yuv420p[c{k}]")
        else:
            continue                    # celda vacía: el fondo ya es oscuro
        idx += 1
        # Rótulo DESPUÉS del recorte: así el nombre y el reloj caen siempre
        # dentro de lo visible, incluso en cámaras 4:3 que pierden los bordes.
        # Solo se pinta el reloj donde se recorta: si la cámara ya trae el suyo
        # (16:9 sin recorte) se dejaría duplicado.
        if name and LABELS and os.path.exists(_FONT) and _needs_clock(name, sizes or {}, cw, ch):
            label = (titles or {}).get(name) or name
            fc[-1] = fc[-1].replace(f"[c{k}]", _drawtext(k, label, ch) + f"[c{k}]")
        # eof_action=repeat: si la entrada se acaba, deja el último fotograma
        # en su celda en vez de detener la composición.
        fc.append(f"[{prev}][c{k}]overlay={x}:{y}:eof_action=repeat:shortest=0[o{k}]")
        prev = f"o{k}"
    fc.append(f"[{prev}]format=yuv420p[v]")
    chain = ";".join(fc)
    args += ["-filter_complex_threads", "2", "-filter_complex", chain, "-map", "[v]",
             "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
             "-threads", "4",
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
        self._last_bytes = None   # bytes publicados en el chequeo anterior
        self._all_names: list = []    # todas las rutas publicadas (para repaginar/rearmar)
        self._titles: dict = {}       # {nombre_ruta: nombre visible} para el rótulo
        self._sizes: dict = {}        # {nombre_ruta: (ancho, alto)} de la fuente
        self._layout = 4
        self._page = 0
        self._launched_at = 0.0       # monotonic del último arranque de ffmpeg
        self._last_rearm = 0.0        # monotonic del último rearme por cámaras

    async def _resolve(self):
        """(page_ready, cfg) de la página actual, consultando qué rutas están vivas."""
        layout, page = self._layout, self._page
        page_names = self._all_names[page * layout: page * layout + layout]
        try:
            ready = {name for name, p in (await self._mtx.runtime_paths()).items() if p.get("ready")}
        except Exception:  # noqa: BLE001
            ready = set(page_names)
        page_ready = [n if n in ready else None for n in page_names]
        return page_ready, (layout, page, tuple(page_ready))

    async def _refresh(self) -> bool:
        """Recalcula qué cámaras están vivas y rearma los argumentos si cambió.

        Así una cámara que se cayó pasa a 'sin señal' y una que volvió reaparece
        sola, sin que el cliente tenga que pedir el mosaico otra vez.
        Devuelve True si la configuración cambió (hay que relanzar ffmpeg).
        """
        if not self._all_names or self._mtx is None:
            return False
        page_ready, cfg = await self._resolve()
        if cfg == self._cfg:
            return False
        layout = self._layout
        cols = int(round(math.sqrt(layout))) if layout > 1 else 1
        rows = math.ceil(layout / cols)
        cw, ch = _CELL[layout]
        empty = await _empty_for(page_ready, cw, ch)
        self._cfg = cfg
        self._args = _ff_args(page_ready, cols, rows, cw, ch, empty, self._titles, self._sizes)
        log.info("mosaico: cámaras vivas ahora %s", cfg[2])
        return True

    async def set(self, mtx, route_names_all, layout, page, titles=None, sizes=None) -> dict:
        """(Re)arma el mosaico. `route_names_all` = nombres de ruta publicados,
        en orden. `titles` = {nombre_ruta: nombre visible} para el rótulo;
        `sizes` = {nombre_ruta: (ancho, alto)} para decidir si la celda recorta.
        Devuelve whep + info de layout/paginado."""
        self._mtx = mtx
        if layout not in _CELL:
            layout = 4
        cols = int(round(math.sqrt(layout))) if layout > 1 else 1
        rows = math.ceil(layout / cols)
        cw, ch = _CELL[layout]
        per = layout
        self._layout = layout
        self._all_names = list(route_names_all or [])
        self._titles = dict(titles or {})
        self._sizes = dict(sizes or {})
        pages = max(1, math.ceil(len(self._all_names) / per)) if self._all_names else 1
        page = max(0, min(int(page or 0), pages - 1))
        self._page = page
        # Solo se componen las cámaras que están publicando (ready): una cámara
        # caída devuelve 404 en su RTSP y tumba el ffmpeg del mosaico. Las caídas
        # se dejan como None (celda "sin señal") y el mosaico se rearma solo
        # cuando cambie la lista (el watchdog llama a _refresh()).
        page_ready, cfg = await self._resolve()
        # Imagen de "sin señal" (logo CamWay) para las celdas vacías o caídas.
        empty_img = await _empty_for(page_ready, cw, ch)
        async with self._lock:
            await mtx.ensure_publisher_path(MOSAIC_NAME)
            self._want = True
            alive = self._proc is not None and self._proc.returncode is None
            if cfg == self._cfg and alive:
                pass                        # ya corre con esta config: no reiniciar
            else:
                self._cfg = cfg
                self._args = _ff_args(page_ready, cols, rows, cw, ch, empty_img, self._titles, self._sizes)
                await self._restart()
        return {"whep": f"/whep/{MOSAIC_NAME}", "layout": layout, "page": page,
                "pages": pages, "cols": cols, "rows": rows, "per_page": per}

    def current_names(self) -> list:
        """Rutas (mtx_name) de las cámaras que componen el mosaico ahora mismo, o
        [] si no hay mosaico armado. Sirve para decidir quién puede verlo: el
        ffmpeg es UNO para todos, así que quien lo lea ve TODAS estas cámaras."""
        return [n for n in (self._cfg[2] if self._cfg else []) if n]

    async def _restart(self) -> None:
        # Mata el ffmpeg anterior si sigue vivo, SIN cancelar el _watch actual:
        # _restart() puede venir del propio _watch (que si se cancelara a sí
        # mismo abortaría este relanzamiento) o de set() (donde el _watch viejo
        # sale solo al ver que self._proc cambió).
        old = self._proc
        self._proc = None
        self._last_bytes = None      # el contador se reinicia con la ruta
        if old is not None and old.returncode is None:
            try:
                old.kill()
                await old.wait()
            except (ProcessLookupError, asyncio.CancelledError):
                pass
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._args, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            self._launched_at = time.monotonic()
            log.info("mosaico ffmpeg lanzado pid=%s", self._proc.pid)
        except Exception as e:  # noqa: BLE001
            log.warning("no se pudo lanzar el mosaico: %s", e)
            return
        self._task = asyncio.create_task(self._watch(self._proc))

    async def _watch(self, proc) -> None:
        """Vigila el ffmpeg del mosaico: lo relanza si MUERE, si se ATASCA
        (deja de publicar en MediaMTX aunque el proceso siga vivo) o si cambió
        el conjunto de cámaras vivas."""
        stale = 0
        while True:
            await asyncio.sleep(_STALL_INTERVAL)
            if not self._want or self._proc is not proc:
                return
            if proc.returncode is not None:
                break                          # murió solo
            if time.monotonic() - self._launched_at < _GRACE:
                continue                       # aún arrancando: no evaluar
            rss = _rss_mb(proc.pid)
            if rss is not None and rss > _RSS_MAX_MB:
                log.warning("mosaico ffmpeg usando %d MB (tope %d) → reiniciando",
                            rss, _RSS_MAX_MB)
                break
            if not await self._is_publishing():
                stale += 1
                if stale >= _STALL_CHECKS:
                    log.warning("mosaico ffmpeg sin publicar durante %ds → reiniciando",
                                stale * _STALL_INTERVAL)
                    break
                continue
            stale = 0
            # ¿Se cayó o volvió alguna cámara? Rearmar para que la celda pase a
            # "sin señal" o vuelva la imagen. Con histéresis para no reiniciar
            # en bucle si la red parpadea.
            if time.monotonic() - self._last_rearm >= _REARM_MIN:
                async with self._lock:
                    if not self._want or self._proc is not proc:
                        return
                    if await self._refresh():
                        self._last_rearm = time.monotonic()
                        log.info("mosaico: cambió el conjunto de cámaras → rearmando")
                        break
        # (re)lanzar: primero se recalcula qué cámaras están vivas, para que una
        # cámara caída quede como "sin señal" y no tumbe el mosaico otra vez.
        if self._want and self._proc is proc:
            await asyncio.sleep(2)
            async with self._lock:
                if self._want and self._proc is proc:
                    await self._refresh()
                    await self._restart()

    async def _is_publishing(self) -> bool:
        """True si la ruta 'mosaico' está lista Y sigue creciendo.

        Comprobar solo `ready` no bastaba: con el mosaico congelado la sesión
        RTSP sigue abierta y MediaMTX la marca lista aunque no entren
        fotogramas nuevos. Si `bytesReceived` no avanza entre chequeos, está
        atascado y hay que relanzar.
        """
        if self._mtx is None:
            return True                          # sin cliente: no reiniciar a ciegas
        try:
            paths = await self._mtx.runtime_paths()
            p = paths.get(MOSAIC_NAME)
            if not (p and p.get("ready")):
                return False
            total = int(p.get("bytesReceived") or 0)
            prev = self._last_bytes
            self._last_bytes = total
            if prev is None:
                return True                      # primer chequeo tras arrancar
            return total > prev                  # sin crecimiento = congelado
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
