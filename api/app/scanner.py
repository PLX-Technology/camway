"""Descubrimiento de cámaras: barrido TCP del segmento + huella RTSP.

El barrido TCP es el camino principal porque funciona igual dentro de un
contenedor; el descubrimiento ONVIF por multicast (onvif.py) es un extra que
se degrada sin romper nada cuando la red no deja pasar el multicast.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Optional

from .rtsp import Media, RtspProbe, get_options, probe_path

import logging
_log = logging.getLogger("camway")

RTSP_PORTS = (554, 8554, 10554, 5554)

# Techo de seguridad: un /19 son 8190 hosts y un /16 son 65534. Sin esto, un
# CIDR mal escrito deja el escaneo corriendo media hora.
MAX_HOSTS = 8192


@dataclass
class Stream:
    """Un flujo concreto y utilizable de una cámara."""
    path: str
    url: str
    codec: str
    width: Optional[int] = None
    height: Optional[int] = None
    profile: str = ""
    audio_codec: str = ""
    label: str = ""

    @property
    def webrtc_ready(self) -> bool:
        """MediaMTX reenvía H.264 tal cual; lo demás habría que transcodificar."""
        return self.codec == "H264"

    @property
    def pixels(self) -> int:
        return (self.width or 0) * (self.height or 0)


@dataclass
class Device:
    """Un equipo encontrado: cámara IP, DVR o NVR."""
    host: str
    port: int
    server: str = ""
    vendor: str = "desconocido"
    needs_auth: bool = False
    auth_ok: bool = False
    permissive: bool = False        # acepta cualquier ruta y sirve el stream 0
    streams: list[Stream] = field(default_factory=list)
    error: str = ""

    @property
    def id(self) -> str:
        return f"{self.host}_{self.port}".replace(".", "-")

    @property
    def kind(self) -> str:
        return "nvr" if len(self.streams) > 2 else "camara"

    @property
    def best(self) -> Optional[Stream]:
        """El stream que conviene mostrar: H.264 y el de mayor resolución."""
        if not self.streams:
            return None
        usable = [s for s in self.streams if s.webrtc_ready] or self.streams
        return max(usable, key=lambda s: s.pixels)


# --------------------------------------------------------------------------
# Rutas candidatas por fabricante
# --------------------------------------------------------------------------

_VENDOR_SIGNATURES = (
    ("grandstream", "Grandstream"),
    ("hikvision", "Hikvision"),
    ("dahua", "Dahua"),
    ("axis", "Axis"),
    ("uniview", "Uniview"),
    ("unv", "Uniview"),
    ("bosch", "Bosch"),
    ("vivotek", "Vivotek"),
    ("tp-link", "TP-Link"),
    ("tapo", "TP-Link Tapo"),
    ("reolink", "Reolink"),
    ("amcrest", "Amcrest"),
    ("hipcam", "HiPCam"),
    ("live555", "Live555"),
    ("h264dvr", "Xiongmai"),
    ("xmeye", "Xiongmai"),
)


def identify_vendor(banner: str) -> str:
    low = banner.lower()
    for needle, name in _VENDOR_SIGNATURES:
        if needle in low:
            return name
    return "desconocido"


def vendor_paths(vendor: str, channels: int = 8, user: str = "", pwd: str = "") -> list[tuple[str, str]]:
    """Rutas documentadas del fabricante: (ruta, etiqueta)."""
    out: list[tuple[str, str]] = []

    if vendor == "Xiongmai":
        # Estos DVR genéricos (muy comunes en clones baratos) no piden
        # credencial por cabecera RTSP: la llevan EN la propia ruta.
        from urllib.parse import quote
        # Sin usuario exige el parametro "user" no vacio para aceptar la
        # peticion (lo rechaza con 451), aunque la clave este en blanco; el
        # usuario de fabrica de estos equipos siempre es "admin".
        u, p = quote(user or "admin", safe=""), quote(pwd, safe="")
        for ch in range(1, channels + 1):
            out += [(f"/user={u}&password={p}&channel={ch}&stream=0.sdp", f"canal {ch} principal"),
                    (f"/user={u}&password={p}&channel={ch}&stream=1.sdp", f"canal {ch} secundario")]
        return out

    if vendor == "Grandstream":
        out += [("/0", "principal"), ("/1", "secundario"),
                ("/2", "terciario"), ("/4", "cuarto")]

    elif vendor == "Hikvision":
        for ch in range(1, channels + 1):
            out += [(f"/Streaming/Channels/{ch}01", f"canal {ch} principal"),
                    (f"/Streaming/Channels/{ch}02", f"canal {ch} secundario")]

    elif vendor in ("Dahua", "Amcrest"):
        for ch in range(1, channels + 1):
            out += [(f"/cam/realmonitor?channel={ch}&subtype=0", f"canal {ch} principal"),
                    (f"/cam/realmonitor?channel={ch}&subtype=1", f"canal {ch} secundario")]

    elif vendor == "Axis":
        out += [("/axis-media/media.amp", "principal"),
                ("/axis-media/media.amp?resolution=640x480", "secundario")]

    elif vendor == "Uniview":
        for ch in range(1, channels + 1):
            out += [(f"/unicast/c{ch}/s0/live", f"canal {ch} principal"),
                    (f"/unicast/c{ch}/s1/live", f"canal {ch} secundario")]

    elif vendor == "Reolink":
        out += [("/h264Preview_01_main", "principal"),
                ("/h264Preview_01_sub", "secundario")]

    elif vendor == "TP-Link Tapo":
        out += [("/stream1", "principal"), ("/stream2", "secundario")]

    return out


# Fondo genérico: muchos equipos no anuncian marca en el banner.
GENERIC_PATHS: list[tuple[str, str]] = [
        ("/", "raiz"),
        ("/live", "live"),
        ("/live/ch0", "live ch0"),
        ("/stream1", "stream1"),
        ("/stream2", "stream2"),
        ("/h264", "h264"),
        ("/video1", "video1"),
        ("/media/video1", "media/video1"),
        ("/ch01/0", "ch01/0"),
        ("/11", "11"),
        ("/cam/realmonitor?channel=1&subtype=0", "dahua ch1 principal"),
        ("/cam/realmonitor?channel=1&subtype=1", "dahua ch1 secundario"),
        ("/Streaming/Channels/101", "hik ch1 principal"),
        ("/Streaming/Channels/102", "hik ch1 secundario"),
        ("/onvif1", "onvif1"),
        ("/profile1", "profile1"),
]


def candidate_paths(vendor: str, channels: int = 8, user: str = "", pwd: str = "") -> list[tuple[str, str]]:
    """Rutas del fabricante primero, luego el fondo genérico, sin repetir."""
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for path, label in vendor_paths(vendor, channels, user, pwd) + GENERIC_PATHS:
        if path not in seen:
            seen.add(path)
            unique.append((path, label))
    return unique


# --------------------------------------------------------------------------
# Huella de un stream, para no listar cuatro veces el mismo video
# --------------------------------------------------------------------------

def stream_fingerprint(medias: list[Media]) -> str:
    """Identifica el contenido de un SDP, ignorando la ruta que lo sirvió."""
    parts = []
    for m in medias:
        parts.append(f"{m.kind}:{m.codec}:{m.width}x{m.height}:{m.payload_type}")
    return hashlib.md5("|".join(parts).encode()).hexdigest()


_BOGUS_PATH = "/camway-ruta-que-no-existe-9f3a"

# Algunos NVR (sobre todo Dahua) no sirven la raiz "/" como recurso RTSP: ahi
# siempre responden 401, sin importar si la credencial es correcta. Antes de
# descartar una credencial por un 401 en la raiz, se confirma contra una ruta
# de canal real. Se prueban varios canales (no solo el 1): si las camaras
# estan en un segmento propio del NVR, el canal 1 puede estar libre y dar 401
# por "sin stream" en vez de por credencial mala.
def _auth_fallback_paths(channels: int) -> list[str]:
    n = max(1, min(channels, 4))
    out: list[str] = []
    for ch in range(1, n + 1):
        out.append(f"/cam/realmonitor?channel={ch}&subtype=0")
    for ch in range(1, n + 1):
        out.append(f"/Streaming/Channels/{ch}01")
    return out


async def _is_permissive(host: str, port: int, user: str, pwd: str, timeout: float) -> bool:
    """¿El equipo devuelve 200 para cualquier ruta inventada?

    Los Grandstream lo hacen: /live, /stream1 y /loquesea sirven el stream 0.
    Si es así, solo hay que fiarse de la lista del fabricante y deduplicar.
    """
    p = await probe_path(host, port, _BOGUS_PATH, user, pwd, timeout)
    return p.status == 200


# --------------------------------------------------------------------------
# Barrido
# --------------------------------------------------------------------------

async def _port_open(host: str, port: int, timeout: float) -> bool:
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        return True
    except Exception:
        return False
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


def expand_cidr(cidr: str) -> list[str]:
    """Expande un CIDR o un rango 'a-b' a direcciones, con techo de seguridad."""
    cidr = cidr.strip()
    if "-" in cidr and "/" not in cidr:
        start, end = (x.strip() for x in cidr.split("-", 1))
        a = int(ipaddress.IPv4Address(start))
        # Permite tanto 172.16.0.1-172.16.0.50 como 172.16.0.1-50
        b = int(ipaddress.IPv4Address(end)) if "." in end else (a - a % 256) + int(end)
        if b < a:
            a, b = b, a
        if b - a + 1 > MAX_HOSTS:
            raise ValueError(f"el rango supera {MAX_HOSTS} direcciones")
        return [str(ipaddress.IPv4Address(i)) for i in range(a, b + 1)]

    net = ipaddress.ip_network(cidr, strict=False)
    if net.num_addresses > MAX_HOSTS:
        raise ValueError(
            f"{cidr} son {net.num_addresses} direcciones; el tope es {MAX_HOSTS}. "
            "Usa una mascara mas estrecha o un rango."
        )
    hosts = list(net.hosts()) if net.num_addresses > 2 else list(net)
    return [str(h) for h in hosts]


@dataclass
class ScanProgress:
    stage: str
    done: int = 0
    total: int = 0
    found: int = 0
    device: Optional[Device] = None
    message: str = ""


async def scan(
    cidr: str,
    credentials: Optional[list[tuple[str, str]]] = None,
    ports: tuple[int, ...] = RTSP_PORTS,
    channels: int = 8,
    connect_timeout: float = 1.2,
    rtsp_timeout: float = 4.0,
    concurrency: int = 200,
    on_progress: Optional[Callable[[ScanProgress], None]] = None,
) -> AsyncIterator[ScanProgress]:
    """Recorre el segmento y va emitiendo progreso y equipos conforme aparecen."""
    credentials = credentials or [("", "")]
    hosts = expand_cidr(cidr)
    targets = [(h, p) for h in hosts for p in ports]

    sem = asyncio.Semaphore(concurrency)
    open_targets: list[tuple[str, int]] = []
    done = 0

    yield ScanProgress(stage="barrido", done=0, total=len(targets),
                       message=f"{len(hosts)} direcciones x {len(ports)} puertos")

    async def check(host: str, port: int):
        async with sem:
            return (host, port) if await _port_open(host, port, connect_timeout) else None

    # Fase 1: qué puertos RTSP están abiertos.
    pending = [asyncio.create_task(check(h, p)) for h, p in targets]
    for coro in asyncio.as_completed(pending):
        res = await coro
        done += 1
        if res:
            open_targets.append(res)
        if done % 64 == 0 or done == len(targets):
            yield ScanProgress(stage="barrido", done=done, total=len(targets),
                               found=len(open_targets))

    yield ScanProgress(stage="huella", done=0, total=len(open_targets),
                       found=len(open_targets),
                       message=f"{len(open_targets)} puertos RTSP abiertos")

    # Fase 2: interrogar cada equipo encontrado (concurrencia baja: son muchos
    # DESCRIBE por equipo y los DVR aguantan pocas sesiones a la vez).
    fp_sem = asyncio.Semaphore(min(16, max(4, concurrency // 16)))

    async def fingerprint(host: str, port: int) -> Device:
        async with fp_sem:
            return await probe_device(host, port, credentials, channels,
                                      rtsp_timeout)

    done = 0
    tasks = [asyncio.create_task(fingerprint(h, p)) for h, p in open_targets]
    for coro in asyncio.as_completed(tasks):
        dev = await coro
        done += 1
        yield ScanProgress(stage="huella", done=done, total=len(open_targets),
                           found=done, device=dev)

    yield ScanProgress(stage="listo", done=len(open_targets),
                       total=len(open_targets), found=len(open_targets))


ONVIF_PORTS = (8080, 80)


async def scan_onvif(
    cidr: str,
    credentials: Optional[list[tuple[str, str]]] = None,
    ports: tuple[int, ...] = ONVIF_PORTS,
    timeout: float = 4.0,
    concurrency: int = 128,
    connect_timeout: float = 1.2,
) -> AsyncIterator[ScanProgress]:
    """Descubre cámaras por ONVIF en :8080/:80 y obtiene sus URLs RTSP reales."""
    from . import onvif_probe

    credentials = credentials or [("", "")]
    hosts = expand_cidr(cidr)
    sem = asyncio.Semaphore(concurrency)

    yield ScanProgress(stage="onvif-barrido", done=0, total=len(hosts),
                       message=f"buscando ONVIF en {len(hosts)} direcciones")

    async def find_open(host: str):
        async with sem:
            for p in ports:
                if await _port_open(host, p, connect_timeout):
                    return (host, p)
            return None

    open_hosts: list[tuple[str, int]] = []
    done = 0
    for coro in asyncio.as_completed([asyncio.create_task(find_open(h)) for h in hosts]):
        res = await coro
        done += 1
        if res:
            open_hosts.append(res)
        if done % 64 == 0 or done == len(hosts):
            yield ScanProgress(stage="onvif-barrido", done=done, total=len(hosts),
                               found=len(open_hosts))

    yield ScanProgress(stage="onvif", done=0, total=len(open_hosts),
                       found=len(open_hosts),
                       message=f"{len(open_hosts)} equipos con ONVIF")

    psem = asyncio.Semaphore(min(12, max(4, concurrency // 8)))

    async def do_probe(host: str, port: int) -> Optional[Device]:
        order = (port,) + tuple(p for p in ports if p != port)
        async with psem:
            return await onvif_probe.probe(host, credentials, ports=order, timeout=timeout)

    done = 0
    for coro in asyncio.as_completed(
        [asyncio.create_task(do_probe(h, p)) for h, p in open_hosts]
    ):
        dev = await coro
        done += 1
        yield ScanProgress(stage="onvif", done=done, total=len(open_hosts),
                           found=done, device=dev)

    yield ScanProgress(stage="onvif-listo", done=len(open_hosts),
                       total=len(open_hosts), found=len(open_hosts))


# Techo de tiempo total por equipo. Sin esto, un NVR que rechaza TODAS las
# credenciales guardadas puede multiplicar el costo: cada credencial prueba
# hasta 8 rutas de respaldo, cada una hasta `timeout` segundos — con varias
# credenciales guardadas eso son minutos por un solo equipo terco, y con dos
# o tres equipos así el escaneo completo se estira a decenas de minutos.
PROBE_DEVICE_BUDGET_S = 45.0


async def probe_device(
    host: str,
    port: int,
    credentials: list[tuple[str, str]],
    channels: int = 8,
    timeout: float = 4.0,
) -> Device:
    """Interroga un equipo, con un techo de tiempo total (ver
    PROBE_DEVICE_BUDGET_S) para que un equipo terco no estire el escaneo
    entero a costa de los demás."""
    try:
        return await asyncio.wait_for(
            _probe_device_inner(host, port, credentials, channels, timeout),
            timeout=PROBE_DEVICE_BUDGET_S,
        )
    except asyncio.TimeoutError:
        dev = Device(host=host, port=port, vendor="desconocido")
        dev.error = f"se agoto el tiempo de interrogacion ({PROBE_DEVICE_BUDGET_S:.0f}s)"
        _log.info("diag-nvr host=%s PRESUPUESTO DE TIEMPO AGOTADO", host)
        return dev


async def _probe_device_inner(
    host: str,
    port: int,
    credentials: list[tuple[str, str]],
    channels: int,
    timeout: float,
) -> Device:
    alive, banner = await get_options(host, port, timeout)
    dev = Device(host=host, port=port, server=banner,
                 vendor=identify_vendor(banner))
    if not alive:
        dev.error = "no responde a OPTIONS"
        return dev

    # Busca el primer juego de credenciales que abra el equipo.
    user, pwd = "", ""
    probe_root: Optional[RtspProbe] = None
    for u, p in credentials:
        probe_root = await probe_path(host, port, "/", u, p, timeout)
        _log.info("diag-nvr raiz user=%r status=%s error=%r",
                   u, probe_root.status, probe_root.error)
        if probe_root.status != 401:
            user, pwd = u, p
            break
        dev.needs_auth = True
        if not u:
            continue
        # La raiz dio 401: puede ser una credencial mala o un NVR que no sirve
        # "/" como recurso. Confirmar contra una ruta de canal real antes de
        # descartarla.
        for fb_path in _auth_fallback_paths(channels):
            fb_probe = await probe_path(host, port, fb_path, u, p, timeout)
            _log.info("diag-nvr fallback %-35s user=%r status=%s error=%r",
                       fb_path, u, fb_probe.status, fb_probe.error)
            if fb_probe.ok:
                user, pwd = u, p
                probe_root = fb_probe
                break
            if fb_probe.error:
                # No fue un rechazo de credencial (401): fue un fallo de red
                # (timeout, conexion rechazada...). Probar las demas rutas de
                # respaldo contra un host que no responde solo gasta tiempo.
                break
            if fb_probe.status == 401:
                # Credencial RECHAZADA en una ruta real: NO seguir probando mas
                # rutas con la misma clave mala. En camaras Hikvision/HiLook ese
                # martilleo (16 rutas x 401) dispara el anti-fuerza-bruta y
                # BLOQUEA la IP; una ruta basta para saber que la clave no sirve.
                break
        if user:
            break

    if not user and probe_root is not None and probe_root.status == 401:
        dev.error = "credenciales no validas"
        return dev
    dev.auth_ok = True

    if dev.vendor == "desconocido" and probe_root is not None and probe_root.server:
        dev.server = probe_root.server
        dev.vendor = identify_vendor(probe_root.server)

    dev.permissive = await _is_permissive(host, port, user, pwd, timeout)

    if dev.permissive:
        # El equipo sirve el stream 0 para cualquier ruta inventada, así que
        # probar el fondo genérico solo produciría alias del mismo video.
        # Solo son fiables las rutas documentadas del fabricante.
        paths = vendor_paths(dev.vendor, channels, user, pwd) or [("/", "raiz")]
    else:
        paths = candidate_paths(dev.vendor, channels, user, pwd)

    _log.info("diag-nvr host=%s vendor=%s permisivo=%s user=%r rutas=%d",
               host, dev.vendor, dev.permissive, user, len(paths))
    seen_fp: set = set()
    for path, label in paths:
        probe = await probe_path(host, port, path, user, pwd, timeout)
        _log.info("diag-nvr canal %-45s status=%s medias=%d error=%r",
                   path, probe.status, len(probe.medias), probe.error)
        if not probe.ok or not probe.medias:
            continue
        video = probe.video
        if video is None or not video.codec:
            continue

        # El fingerprint solo compara codec/resolucion/payload: en un DVR
        # multicanal, un canal SIN camara repite ese mismo "molde" en los
        # demas canales vacios y los hacia pasar por alias del mismo video.
        # Si la etiqueta ya trae el numero de canal, se agrega al fingerprint
        # para no confundir canales distintos con contenido casualmente igual.
        chan_m = re.match(r"canal (\d+)", label)
        fp = (chan_m.group(1) if chan_m else None, stream_fingerprint(probe.medias))
        if fp in seen_fp:
            continue
        seen_fp.add(fp)

        audio = next((m.codec for m in probe.medias if m.kind == "audio"), "")
        dev.streams.append(Stream(
            path=path,
            url=_url_with_credentials(host, port, path, user, pwd),
            codec=video.codec,
            width=video.width,
            height=video.height,
            profile=video.profile,
            audio_codec=audio,
            label=label,
        ))

    if not dev.streams:
        dev.error = dev.error or "responde RTSP pero no entrega ningun stream"
    return dev


def _url_with_credentials(host: str, port: int, path: str, user: str, pwd: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    if user:
        from urllib.parse import quote
        return f"rtsp://{quote(user, safe='')}:{quote(pwd, safe='')}@{host}:{port}{path}"
    return f"rtsp://{host}:{port}{path}"


def strip_credentials(url: str) -> str:
    """Para mostrar en la interfaz sin filtrar la clave."""
    return re.sub(r"rtsp://[^@/]+@", "rtsp://", url)
