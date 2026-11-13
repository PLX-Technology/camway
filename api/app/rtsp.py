"""Cliente RTSP mínimo: OPTIONS / DESCRIBE con autenticación Digest y Basic.

Suficiente para huellear un equipo (marca, códecs, resolución, streams) sin
depender de ffprobe, que cuesta un proceso por cámara.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

USER_AGENT = "CamWay/1.0"
DEFAULT_TIMEOUT = 4.0


@dataclass
class Media:
    """Una pista dentro del SDP (video, audio o metadatos)."""
    kind: str                       # video | audio | application
    codec: str                      # H264, H265, JPEG, AAC, ...
    payload_type: int
    control: str = ""
    width: Optional[int] = None
    height: Optional[int] = None
    profile: str = ""


@dataclass
class RtspProbe:
    """Resultado de interrogar una ruta RTSP concreta."""
    url: str
    status: int = 0
    server: str = ""
    needs_auth: bool = False
    auth_ok: bool = False
    medias: list[Media] = field(default_factory=list)
    sdp: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def video(self) -> Optional[Media]:
        return next((m for m in self.medias if m.kind == "video"), None)


# --------------------------------------------------------------------------
# Autenticación
# --------------------------------------------------------------------------

def _parse_www_authenticate(header: str) -> dict:
    scheme = "digest" if header.lower().lstrip().startswith("digest") else "basic"
    params = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', header))
    params["_scheme"] = scheme
    return params


def _digest_header(user: str, pwd: str, method: str, uri: str, chal: dict) -> str:
    realm = chal.get("realm", "")
    nonce = chal.get("nonce", "")
    ha1 = hashlib.md5(f"{user}:{realm}:{pwd}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    parts = [
        f'username="{user}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
        f'response="{resp}"',
    ]
    if "opaque" in chal:
        parts.append('opaque="' + chal["opaque"] + '"')
    return "Digest " + ", ".join(parts)


def _basic_header(user: str, pwd: str) -> str:
    token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return f"Basic {token}"


# --------------------------------------------------------------------------
# Lectura del protocolo
# --------------------------------------------------------------------------

async def _read_response(reader: asyncio.StreamReader, timeout: float) -> tuple[int, dict, str]:
    """Lee cabeceras hasta la línea en blanco y luego Content-Length bytes."""
    raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
    head = raw.decode("utf-8", "replace")
    lines = head.split("\r\n")
    status = 0
    m = re.match(r"RTSP/1\.\d\s+(\d+)", lines[0])
    if m:
        status = int(m.group(1))

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    body = ""
    length = int(headers.get("content-length", 0) or 0)
    if length > 0:
        data = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
        body = data.decode("utf-8", "replace")
    return status, headers, body


async def probe_path(
    host: str,
    port: int,
    path: str,
    user: str = "",
    password: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> RtspProbe:
    """Hace DESCRIBE sobre una ruta y devuelve lo que el equipo declara."""
    if path and not path.startswith("/"):
        path = "/" + path
    url = f"rtsp://{host}:{port}{path}"
    probe = RtspProbe(url=url)

    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )

        async def describe(w, r, auth: str = "") -> tuple[int, dict, str]:
            req = (
                f"DESCRIBE {url} RTSP/1.0\r\n"
                "CSeq: 1\r\n"
                "Accept: application/sdp\r\n"
                f"User-Agent: {USER_AGENT}\r\n"
            )
            if auth:
                req += f"Authorization: {auth}\r\n"
            req += "\r\n"
            w.write(req.encode())
            await w.drain()
            return await _read_response(r, timeout)

        status, headers, body = await describe(writer, reader)
        probe.server = headers.get("server", "")

        if status == 401:
            probe.needs_auth = True
            chal = _parse_www_authenticate(headers.get("www-authenticate", ""))
            if user:
                # Algunos equipos cierran la conexión tras el 401: reconectar.
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=timeout
                )
                if chal.get("_scheme") == "digest":
                    auth = _digest_header(user, password, "DESCRIBE", url, chal)
                else:
                    auth = _basic_header(user, password)
                status, headers, body = await describe(writer, reader, auth)
                probe.auth_ok = status == 200
        elif status == 200:
            probe.auth_ok = True

        probe.status = status
        probe.sdp = body
        if status == 200 and body:
            probe.medias = parse_sdp(body)

    except asyncio.TimeoutError:
        probe.error = "timeout"
    except asyncio.IncompleteReadError:
        probe.error = "respuesta truncada"
    except (ConnectionError, OSError) as exc:
        probe.error = "conexion: " + exc.__class__.__name__
    except Exception as exc:  # respuesta fuera de norma
        probe.error = f"{exc.__class__.__name__}: {exc}"
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    return probe


async def get_options(host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """OPTIONS es la forma más barata de saber si hay un RTSP vivo detrás."""
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        req = (
            f"OPTIONS rtsp://{host}:{port} RTSP/1.0\r\n"
            "CSeq: 1\r\n"
            f"User-Agent: {USER_AGENT}\r\n\r\n"
        )
        writer.write(req.encode())
        await writer.drain()
        status, headers, _ = await _read_response(reader, timeout)
        # 401 también confirma que es un RTSP: solo pide credenciales.
        return status in (200, 401), headers.get("server", "")
    except Exception:
        return False, ""
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


# --------------------------------------------------------------------------
# SDP
# --------------------------------------------------------------------------

_CODEC_BY_PT = {26: "JPEG", 32: "MPEG1/2", 33: "MP2T", 0: "PCMU", 8: "PCMA"}

_CODEC_ALIASES = {"H264": "H264", "H265": "H265", "HEVC": "H265", "JPEG": "JPEG"}


def parse_sdp(sdp: str) -> list[Media]:
    medias: list[Media] = []
    current: Optional[Media] = None

    for line in sdp.splitlines():
        line = line.strip()

        if line.startswith("m="):
            # m=<kind> <port> <proto> <fmt>
            parts = line[2:].split()
            if len(parts) < 4:
                continue
            try:
                pt = int(parts[3])
            except ValueError:
                pt = -1
            current = Media(kind=parts[0], codec=_CODEC_BY_PT.get(pt, ""), payload_type=pt)
            medias.append(current)

        elif current is None:
            continue

        elif line.startswith("a=rtpmap:"):
            m = re.match(r"a=rtpmap:(\d+)\s+([^/]+)/", line)
            if m and int(m.group(1)) == current.payload_type:
                name = m.group(2).upper()
                current.codec = _CODEC_ALIASES.get(name, name)

        elif line.startswith("a=control:"):
            current.control = line[len("a=control:"):].strip()

        elif line.startswith("a=fmtp:") and current.kind == "video":
            if current.codec == "H264":
                sprop = re.search(r"sprop-parameter-sets=([^;\s]+)", line)
                if sprop:
                    _apply_h264_sps(current, sprop.group(1))

    return medias


def _apply_h264_sps(media: Media, sprop: str) -> None:
    sets = sprop.split(",")
    if not sets or not sets[0]:
        return
    try:
        sps = base64.b64decode(sets[0] + "=" * (-len(sets[0]) % 4))
    except Exception:
        return
    info = parse_h264_sps(sps)
    if info:
        media.width, media.height, media.profile = info


# --------------------------------------------------------------------------
# SPS de H.264 — la resolución que declara la cabecera del equipo suele mentir
# --------------------------------------------------------------------------

class _BitReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def bit(self) -> int:
        byte = self.pos >> 3
        if byte >= len(self.data):
            raise EOFError
        val = (self.data[byte] >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return val

    def bits(self, n: int) -> int:
        val = 0
        for _ in range(n):
            val = (val << 1) | self.bit()
        return val

    def ue(self) -> int:
        """Exp-Golomb sin signo."""
        zeros = 0
        while self.bit() == 0:
            zeros += 1
            if zeros > 32:
                raise ValueError("exp-golomb fuera de rango")
        if zeros == 0:
            return 0
        return (1 << zeros) - 1 + self.bits(zeros)

    def se(self) -> int:
        """Exp-Golomb con signo."""
        k = self.ue()
        return (k + 1) // 2 if k % 2 else -(k // 2)


_PROFILE_NAMES = {66: "Baseline", 77: "Main", 88: "Extended", 100: "High",
                  110: "High 10", 122: "High 4:2:2", 244: "High 4:4:4"}

_HIGH_PROFILES = {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}


def _strip_emulation(data: bytes) -> bytes:
    """Quita los bytes 0x03 de anti-emulación del flujo NAL."""
    out = bytearray()
    i = 0
    while i < len(data):
        if i + 2 < len(data) and data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 3:
            out += data[i:i + 2]
            i += 3
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def _skip_scaling_list(r: _BitReader, size: int) -> None:
    last = next_scale = 8
    for _ in range(size):
        if next_scale != 0:
            next_scale = (last + r.se() + 256) % 256
        last = next_scale if next_scale != 0 else last


def parse_h264_sps(nal: bytes) -> Optional[tuple[int, int, str]]:
    """Devuelve (ancho, alto, perfil) leyendo el SPS. None si no se puede."""
    if len(nal) < 4:
        return None
    if nal[0] & 0x1F != 7:      # no es un SPS
        return None

    try:
        r = _BitReader(_strip_emulation(nal[1:]))
        profile_idc = r.bits(8)
        r.bits(8)                               # constraint flags + reserved
        level_idc = r.bits(8)
        r.ue()                                  # seq_parameter_set_id

        chroma_format_idc = 1
        if profile_idc in _HIGH_PROFILES:
            chroma_format_idc = r.ue()
            if chroma_format_idc == 3:
                r.bit()                         # separate_colour_plane_flag
            r.ue()                              # bit_depth_luma_minus8
            r.ue()                              # bit_depth_chroma_minus8
            r.bit()                             # qpprime_y_zero_transform_bypass
            if r.bit():                         # seq_scaling_matrix_present
                count = 8 if chroma_format_idc != 3 else 12
                for i in range(count):
                    if r.bit():
                        _skip_scaling_list(r, 16 if i < 6 else 64)

        r.ue()                                  # log2_max_frame_num_minus4
        poc_type = r.ue()
        if poc_type == 0:
            r.ue()                              # log2_max_pic_order_cnt_lsb_minus4
        elif poc_type == 1:
            r.bit()                             # delta_pic_order_always_zero
            r.se()                              # offset_for_non_ref_pic
            r.se()                              # offset_for_top_to_bottom_field
            for _ in range(r.ue()):
                r.se()

        r.ue()                                  # max_num_ref_frames
        r.bit()                                 # gaps_in_frame_num_allowed
        width_mbs = r.ue() + 1
        height_map_units = r.ue() + 1
        frame_mbs_only = r.bit()
        if not frame_mbs_only:
            r.bit()                             # mb_adaptive_frame_field_flag
        r.bit()                                 # direct_8x8_inference_flag

        crop_l = crop_r = crop_t = crop_b = 0
        if r.bit():                             # frame_cropping_flag
            crop_l, crop_r, crop_t, crop_b = r.ue(), r.ue(), r.ue(), r.ue()

        width = width_mbs * 16
        height = (2 - frame_mbs_only) * height_map_units * 16

        # Las unidades de recorte dependen del submuestreo de croma.
        if chroma_format_idc == 0:
            sub_w, sub_h = 1, 1
        elif chroma_format_idc == 2:
            sub_w, sub_h = 2, 1
        elif chroma_format_idc == 3:
            sub_w, sub_h = 1, 1
        else:
            sub_w, sub_h = 2, 2

        width -= (crop_l + crop_r) * sub_w
        height -= (crop_t + crop_b) * sub_h * (2 - frame_mbs_only)

        if not (0 < width <= 16384 and 0 < height <= 16384):
            return None

        profile = _PROFILE_NAMES.get(profile_idc, f"perfil {profile_idc}")
        return width, height, f"{profile} L{level_idc / 10:.1f}"
    except (EOFError, ValueError, ZeroDivisionError):
        return None
