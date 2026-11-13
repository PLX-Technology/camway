"""Puente con MediaMTX: publica cada stream descubierto como ruta WebRTC.

MediaMTX abre UNA sola sesión RTSP contra la cámara y la reparte a todos los
que miren. Es justo lo que evita agotar el límite de sesiones simultáneas de
un DVR cuando varias personas abren el mosaico.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

import httpx

MTX_API = os.environ.get("CAMWAY_MTX_API", "http://mediamtx:9997")
MTX_WEBRTC = os.environ.get("CAMWAY_MTX_WEBRTC", "http://localhost:8889")

# Cuánto espera MediaMTX antes de soltar la cámara al quedarse sin espectadores.
CLOSE_AFTER = os.environ.get("CAMWAY_MTX_CLOSE_AFTER", "15s")

# Tope de resolución del transcode (ancho). Un flujo 3K (2880px) transcodificado
# a H.264 en vivo ahoga al navegador y a la CPU y la imagen se CONGELA al
# maximizar. Se escala a este ancho (720p por defecto): nítido en pantalla
# completa y sostenible. Solo reduce; nunca amplía (min con el ancho real).
MAX_WIDTH = int(os.environ.get("CAMWAY_MAX_WIDTH", "1280"))
MAX_HEIGHT = int(os.environ.get("CAMWAY_MAX_HEIGHT", "720"))
# Fotogramas/seg del transcode (menos fps = menos peso). 0 = respetar la fuente.
MAX_FPS = int(os.environ.get("CAMWAY_MAX_FPS", "15"))
# Tope de bitrate del vídeo transcodificado (p.ej. "800k"). Vacío = CRF libre
# (calidad constante, tamaño impredecible). Fijarlo hace la GRABACIÓN predecible
# y permite calcular cuántos días caben en el disco. Afecta vivo y grabación.
MAX_BITRATE = os.environ.get("CAMWAY_MAX_BITRATE", "").strip()
# Preset de x264. Con tope de bitrate conviene uno mejor que "ultrafast" para
# que la imagen no se vea pastosa al bitrate objetivo (veryfast es buen balance).
X264_PRESET = os.environ.get("CAMWAY_X264_PRESET", "veryfast" if MAX_BITRATE else "ultrafast")
# Aceleración por hardware del transcode. "" = software (libx264). "qsv" = Intel
# QuickSync: decodifica (RTSP) y codifica en la iGPU (h264_qsv) → libera la CPU,
# clave para ver varias cámaras H.265 a la vez en equipos de pocos núcleos
# (p.ej. el PC del taladro, Intel HD 530). Requiere un ffmpeg con soporte QSV.
HWACCEL = os.environ.get("CAMWAY_HWACCEL", "").strip().lower()

# Quitar el audio del transcode. En sitios como el taladro el DVR entrega
# audio pcm_mulaw con timestamps erráticos (inunda el log de "Non-monotonic
# DTS / Queue input backward in time", gasta CPU y puede desestabilizar la
# sesión) y además nadie lo usa. Con esto el transcode sale SOLO vídeo.
NO_AUDIO = os.environ.get("CAMWAY_NO_AUDIO", "").strip().lower() in ("1", "true", "yes", "si")

# Grabación local (opt-in por cámara). La carpeta se comparte por volumen con
# el contenedor del API (que lista y sirve lo grabado).
RECORD_DIR = os.environ.get("CAMWAY_RECORD_DIR", "/rec")
RECORD_SEGMENT = os.environ.get("CAMWAY_RECORD_SEGMENT", "10m")


def set_encoding(max_width=None, max_height=None, max_fps=None,
                 max_bitrate=None, preset=None) -> None:
    """Actualiza en caliente los parámetros del transcode (desde Ajustes).
    Lo que se pasa como None/"" no se toca."""
    global MAX_WIDTH, MAX_HEIGHT, MAX_FPS, MAX_BITRATE, X264_PRESET
    if max_width:
        MAX_WIDTH = int(max_width)
    if max_height:
        MAX_HEIGHT = int(max_height)
    if max_fps is not None and max_fps != "":
        MAX_FPS = int(max_fps)
    if max_bitrate is not None:
        MAX_BITRATE = str(max_bitrate).strip()
    if preset:
        X264_PRESET = preset
    elif max_bitrate is not None:            # auto: veryfast si hay tope, si no ultrafast
        X264_PRESET = "veryfast" if MAX_BITRATE else "ultrafast"


def current_encoding() -> dict:
    return {"max_width": MAX_WIDTH, "max_height": MAX_HEIGHT,
            "max_fps": MAX_FPS, "max_bitrate": MAX_BITRATE, "preset": X264_PRESET}


def record_keys(delete_after: Optional[str]) -> dict:
    """Claves de grabación continua para una ruta de MediaMTX.
    `delete_after` = duración de retención (ej. '3d', '10m'); borra lo más viejo.
    None/'' = no borrar."""
    return {
        "record": True,
        "recordPath": f"{RECORD_DIR}/%path/%Y-%m-%d_%H-%M-%S-%f",
        "recordFormat": "fmp4",
        "recordSegmentDuration": RECORD_SEGMENT,
        "recordPartDuration": "1s",
        "recordDeleteAfter": delete_after or "0s",
    }

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_\-]")


def safe_name(name: str) -> str:
    return _SAFE_NAME.sub("_", name)


def transcode_command(source_url: str) -> str:
    """ffmpeg para los streams que WebRTC no admite tal cual (MJPEG, H.265).

    Cuesta CPU de verdad: del orden de un tercio de núcleo por cámara a
    resolución baja. Por eso solo se usa cuando el códec obliga.
    """
    if source_url.startswith("rtsp://"):
        # nobuffer+low_delay: sin esto ffmpeg acumula 1-3 s de entrada (latencia).
        input_flags = "-rtsp_transport tcp -fflags nobuffer -flags low_delay "
    elif source_url.startswith(("http://", "https://")):
        # MJPEG por HTTP (multipart/x-mixed-replace): no trae timestamps (los
        # fabricamos con el reloj) y muchas cámaras cierran la conexión cada
        # tanto -> ffmpeg saldría en EOF y la ruta se reiniciaría con un hueco
        # de segundos (comportamiento "errático"). Con -reconnect se reengancha
        # solo, sin cortar la salida.
        input_flags = (
            "-use_wallclock_as_timestamps 1 -fflags nobuffer -flags low_delay "
            "-reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 "
            "-reconnect_delay_max 2 "
        )
    else:
        input_flags = ""
    # Encaja el vídeo dentro de MAX_WIDTH×MAX_HEIGHT conservando el aspecto, para
    # que un 3K transcodificado no congele el vivo al maximizar. Se usa
    # force_original_aspect_ratio (sin comas: ffmpeg trata la coma como separador
    # de filtros y MediaMTX no respeta el escape). force_divisible_by=2 → alturas
    # pares que libx264 exige.
    vf = (f"scale={MAX_WIDTH}:{MAX_HEIGHT}:"
          "force_original_aspect_ratio=decrease:force_divisible_by=2")
    fps = f"-r {MAX_FPS} " if MAX_FPS > 0 else ""
    # Control de tasa: con tope → CBR aproximado (tamaño predecible para calcular
    # días de grabación); sin tope → CRF libre (calidad constante).
    if MAX_BITRATE:
        try:
            buf = str(int(MAX_BITRATE.rstrip("kK")) * 2) + "k"
        except ValueError:
            buf = MAX_BITRATE
        rate = f"-b:v {MAX_BITRATE} -maxrate {MAX_BITRATE} -bufsize {buf} "
    else:
        rate = ""
    # Codificador: QuickSync (GPU) o libx264 (software). Para QSV decodificamos
    # también en la GPU, pero solo en RTSP (en MJPEG/HTTP el decode se deja en
    # software, más compatible; el encode sigue por GPU).
    if HWACCEL == "qsv":
        hwdec = "-hwaccel qsv " if source_url.startswith("rtsp://") else ""
        venc = (f"-c:v h264_qsv -preset {X264_PRESET} -profile:v baseline "
                "-pix_fmt nv12 ")
    else:
        hwdec = ""
        venc = (f"-c:v libx264 -preset {X264_PRESET} -tune zerolatency -profile:v baseline "
                "-pix_fmt yuv420p ")
    if NO_AUDIO:
        # Solo vídeo: evita el audio pcm_mulaw con DTS erráticos del DVR.
        audio = "-an -map 0:v:0 "
    else:
        # Audio a Opus SI la fuente lo trae (el `?` lo hace opcional: las MJPEG
        # sin audio no fallan). Así la IMOU (HEVC+AAC) conserva su audio.
        audio = "-c:a libopus -b:a 48k -ar 48000 -ac 1 -map 0:v:0 -map 0:a:0? "
    return (
        "ffmpeg -nostdin -loglevel warning "
        f"{hwdec}{input_flags}-i {source_url} "
        f"-vf {vf} {fps}"
        f"{venc}"
        f"{rate}"
        "-g 30 "
        f"{audio}"
        "-f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/$MTX_PATH"
    )


def lo_transcode_command(source_url: str) -> str:
    """Transcode del SUBSTREAM (flujo ligero) para el mosaico en vivo por WebRTC.
    Normaliza a H264 baseline, keyframes regulares (-g 30) y SOLO vídeo (sin
    audio): el substream suele traer audio AAC, que WebRTC no sirve y hacía que
    la sesión no se estableciera (reconexión). NO reescala (el substream ya es
    chico), así se mantiene liviano para el cliente."""
    if source_url.startswith("rtsp://"):
        input_flags = "-rtsp_transport tcp -fflags nobuffer -flags low_delay "
    elif source_url.startswith(("http://", "https://")):
        input_flags = ("-use_wallclock_as_timestamps 1 -fflags nobuffer -flags low_delay "
                       "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 2 ")
    else:
        input_flags = ""
    # -g 15 (1 s a 15 fps): keyframes frecuentes → menor latencia de arranque/recuperación.
    return (
        "ffmpeg -nostdin -loglevel warning "
        f"{input_flags}-i {source_url} -an "
        "-c:v libx264 -preset ultrafast -tune zerolatency -profile:v baseline "
        "-pix_fmt yuv420p -g 15 "
        "-f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/$MTX_PATH"
    )


def tv_transcode_command(source_url: str) -> str:
    """Flujo LIGERO para la app TV (muro): lee el principal desde el propio MediaMTX
    (no abre otra conexión a la cámara), lo baja a 640 de ancho, H264 baseline, sin
    audio. El TV (poca RAM, 2 decodificadores HW) deja de decodificar 1080p por celda."""
    return (
        # wallclock: hay cámaras (Grandstream) con marcas de tiempo irregulares y una pista de datos
        # extra que dejaban a ffmpeg atascado (el path __tv se caía por timeout); -map 0:v:0 = solo vídeo.
        # -thread_type slice: el decodificador de ffmpeg con hilos POR FOTOGRAMA retiene varios fotogramas
        # antes de sacar el primero (≈150 ms en 720p/25 fps); por segmento no añade retardo (medido).
        "ffmpeg -nostdin -loglevel warning -rtsp_transport tcp -fflags nobuffer+discardcorrupt -flags low_delay "
        f"-thread_type slice -threads 2 -use_wallclock_as_timestamps 1 -i {source_url} -map 0:v:0 -an -sn -dn -vf scale=640:-2 "
        "-c:v libx264 -preset ultrafast -tune zerolatency -profile:v baseline "
        "-pix_fmt yuv420p -g 25 -fps_mode vfr -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/$MTX_PATH"
    )


def audio_transcode_command(source_url: str) -> str:
    """Para cámaras H.264 cuyo audio es AAC (MPEG-4): WebRTC no reproduce AAC.
    Se COPIA el vídeo (sin re-codificar, CPU ~0) y solo se transcodifica el
    audio a Opus, que es el códec nativo de WebRTC. Así suena en el vivo.
    """
    return (
        "ffmpeg -nostdin -loglevel warning -rtsp_transport tcp "
        f"-i {source_url} -c:v copy -c:a libopus -b:a 48k -ar 48000 -ac 1 "
        "-f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/$MTX_PATH"
    )


class MediaMTX:
    def __init__(self, api_url: str = MTX_API, webrtc_url: str = MTX_WEBRTC):
        self.api_url = api_url.rstrip("/")
        self.webrtc_url = webrtc_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=10.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def healthy(self) -> bool:
        try:
            r = await self._client.get(f"{self.api_url}/v3/config/global/get")
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def list_paths(self) -> dict[str, Any]:
        try:
            r = await self._client.get(f"{self.api_url}/v3/config/paths/list")
            r.raise_for_status()
            data = r.json()
            return {p["name"]: p for p in data.get("items", [])}
        except httpx.HTTPError:
            return {}

    async def runtime_paths(self) -> dict[str, Any]:
        """Estado real: si la fuente está conectada y cuántos leen."""
        try:
            r = await self._client.get(f"{self.api_url}/v3/paths/list")
            r.raise_for_status()
            return {p["name"]: p for p in r.json().get("items", [])}
        except httpx.HTTPError:
            return {}

    async def publish(self, name: str, source_url: str, transcode: bool = False,
                      audio_transcode: bool = False, lo_transcode: bool = False,
                      tv_transcode: bool = False, record_delete_after: Optional[str] = None) -> bool:
        """Crea o actualiza la ruta. Idempotente.

        transcode: re-codifica vídeo (MJPEG/H.265 → H.264, escalado, con audio Opus).
        audio_transcode: copia el vídeo H.264 y transcodifica solo el audio a Opus.
        lo_transcode: substream para WebRTC → H264 baseline, solo vídeo, sin reescalar.
        record_days: si se da, GRABA en continuo (retención en días) y mantiene
        la fuente SIEMPRE conectada (sin on-demand), porque MediaMTX solo graba
        mientras la fuente está activa."""
        name = safe_name(name)
        recording = record_delete_after is not None

        if transcode or audio_transcode or lo_transcode or tv_transcode:
            if tv_transcode:
                cmd = tv_transcode_command(source_url)
            elif lo_transcode:
                cmd = lo_transcode_command(source_url)
            elif transcode:
                cmd = transcode_command(source_url)
            else:
                cmd = audio_transcode_command(source_url)
            if recording:
                # Siempre encendido: runOnInit corre aunque no haya espectadores.
                config = {
                    "source": "publisher",
                    "runOnInit": cmd,
                    "runOnInitRestart": True,
                }
            else:
                config = {
                    "source": "publisher",
                    "runOnDemand": cmd,
                    "runOnDemandRestart": True,
                    "runOnDemandCloseAfter": CLOSE_AFTER,
                }
        else:
            config = {
                "source": source_url,
                # Grabando: NO on-demand (la fuente queda conectada 24/7).
                "sourceOnDemand": not recording,
                "sourceOnDemandCloseAfter": CLOSE_AFTER,
                "rtspTransport": "tcp",
            }

        if recording:
            config.update(record_keys(record_delete_after))

        # Si la ruta ya existe, se BORRA y se recrea en vez de parchear: un
        # PATCH no cambia bien el TIPO de fuente (p.ej. de `source: rtsp` a
        # `publisher`+runOnDemand para el transcode de audio) — quedan claves
        # viejas (sourceOnDemand) en conflicto y MediaMTX ignora el cambio.
        existing = await self.list_paths()
        try:
            if name in existing:
                await self._client.delete(f"{self.api_url}/v3/config/paths/delete/{name}")
            r = await self._client.post(
                f"{self.api_url}/v3/config/paths/add/{name}", json=config)
            return r.status_code in (200, 201)
        except httpx.HTTPError:
            return False

    async def unpublish(self, name: str) -> bool:
        name = safe_name(name)
        try:
            r = await self._client.delete(
                f"{self.api_url}/v3/config/paths/delete/{name}"
            )
            return r.status_code in (200, 404)
        except httpx.HTTPError:
            return False

    async def ensure_publisher_path(self, name: str) -> bool:
        """Crea una ruta que ACEPTA un publicador externo (p.ej. el ffmpeg del
        mosaico publica aquí). Idempotente."""
        name = safe_name(name)
        existing = await self.list_paths()
        if name in existing:
            return True
        try:
            r = await self._client.post(
                f"{self.api_url}/v3/config/paths/add/{name}",
                json={"source": "publisher"})
            return r.status_code in (200, 201)
        except httpx.HTTPError:
            return False

    def whep_url(self, name: str) -> str:
        """Endpoint WHEP que consume el navegador — vía el proxy de la API
        (mismo origen) para poder cerrar la sesión y evitar el cross-puerto."""
        return f"/whep/{safe_name(name)}"

    def hls_url(self, name: str) -> str:
        return f"{self.webrtc_url.replace(':8889', ':8888')}/{safe_name(name)}/index.m3u8"

    def rtsp_url(self, name: str) -> str:
        """URL RTSP por MediaMTX (lectura anónima) para clientes nativos de baja
        latencia (p.ej. la app TV con libVLC). host := el de CAMWAY_MTX_WEBRTC."""
        host = self.webrtc_url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        return f"rtsp://{host}:8554/{safe_name(name)}"
