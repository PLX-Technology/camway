"""Talkback (hablarle a la cámara) por el backchannel ONVIF / RTSP.

Las cámaras Dahua/IMOU exponen una pista de audio *sendonly* (Perfil T) cuando se
pide el stream por la URL ONVIF con la cabecera `Require: www.onvif.org/ver20/backchannel`.
El navegador captura el micrófono, lo codifica en G.711 A-law a 8 kHz y manda tramas
de 20 ms por WebSocket; aquí abrimos la sesión RTSP (DESCRIBE/SETUP/RECORD), y un
"pacer" empuja un paquete RTP cada 20 ms (silencio A-law si el usuario no habla,
para mantener la cadencia). Transporte TCP interleaved (atraviesa cualquier NAT/firewall).

Probado contra la IMOU Cruiser PS7F (2026-10-06): PCMA/8000 = PT dinámico 101.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import random
import re
import struct
from typing import Optional

_READ_TIMEOUT_S = 8.0

BACKCHANNEL = "www.onvif.org/ver20/backchannel"
PTIME_S = 0.02                 # 20 ms por paquete
SAMPLES = 160                  # 8000 Hz * 0.02 s
ALAW_SILENCE = b"\xd5" * SAMPLES   # silencio en A-law
# Ruta de stream ONVIF por defecto (Dahua/IMOU). El backchannel NO aparece en la
# ruta Dahua "pura"; hace falta &proto=Onvif.
DEFAULT_PATH = "/cam/realmonitor?channel=1&subtype=0&unicast=true&proto=Onvif"


def _digest(user: str, pwd: str, realm: str, nonce: str, method: str, uri: str) -> str:
    ha1 = hashlib.md5(f"{user}:{realm}:{pwd}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    return (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{resp}"')


class Backchannel:
    """Sesión RTSP RECORD hacia el altavoz de la cámara. Half-duplex, TCP interleaved."""

    def __init__(self, host: str, user: str, pwd: str, path: str = DEFAULT_PATH, port: int = 554):
        self.host, self.port, self.user, self.pwd = host, port, user, pwd
        self.url = f"rtsp://{host}:{port}{path}"
        self.base = self.url
        self._r: Optional[asyncio.StreamReader] = None
        self._w: Optional[asyncio.StreamWriter] = None
        self._cseq = 0
        self._realm = self._nonce = self._session = None
        self.channel = 0
        self.payload_type = 101          # PCMA/8000 (dinámico en Dahua/IMOU)
        self._seq = random.randint(0, 0xFFFF)
        self._ts = random.randint(0, 0x7FFFFFFF)
        self._ssrc = random.randint(0, 0x7FFFFFFF)
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
        self._closed = False

    # --- RTSP ---------------------------------------------------------------
    async def _request(self, method: str, uri: str, extra: dict) -> tuple[str, str]:
        self._cseq += 1
        h = {"CSeq": self._cseq, "User-Agent": "CamWay", "Require": BACKCHANNEL}
        h.update(extra)
        if self._session:
            h["Session"] = self._session
        if self._nonce:
            h["Authorization"] = _digest(self.user, self.pwd, self._realm, self._nonce, method, uri)
        raw = f"{method} {uri} RTSP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
        self._w.write(raw.encode())
        await self._w.drain()
        return await self._read_response()

    async def _read_response(self) -> tuple[str, str]:
        # Sin timeout aqui, una camara que acepta la conexion y luego nunca
        # responde (corte de energia a medias, firmware colgado) dejaba esta
        # tarea esperando para siempre — nunca tumbaba el servidor, pero si
        # un socket y una sesion de talkback huerfanos por cada intento.
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = await asyncio.wait_for(self._r.read(4096), timeout=_READ_TIMEOUT_S)
            if not chunk:
                break
            head += chunk
        head, _, rest = head.partition(b"\r\n\r\n")
        m = re.search(rb"Content-Length: (\d+)", head)
        body = rest
        if m:
            need = int(m.group(1))
            while len(body) < need:
                chunk = await asyncio.wait_for(self._r.read(4096), timeout=_READ_TIMEOUT_S)
                if not chunk:
                    break
                body += chunk
        return head.decode(errors="replace"), body.decode(errors="replace")

    async def open(self):
        self._r, self._w = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=8)
        # DESCRIBE #1 -> 401 con challenge digest
        head, _ = await self._request("DESCRIBE", self.url, {"Accept": "application/sdp"})
        m = re.search(r'realm="([^"]+)",\s*nonce="([^"]+)"', head)
        if not m:
            raise RuntimeError("la cámara no pidió autenticación digest")
        self._realm, self._nonce = m.group(1), m.group(2)
        # DESCRIBE #2 -> 200 + SDP
        head, body = await self._request("DESCRIBE", self.url, {"Accept": "application/sdp"})
        if "200" not in head.split("\r\n", 1)[0]:
            raise RuntimeError(f"DESCRIBE falló: {head.splitlines()[0] if head else '?'}")
        cb = re.search(r'Content-Base: (\S+)', head)
        content_base = cb.group(1) if cb else self.url
        # localizar la pista de audio sendonly (el backchannel) y su PT de PCMA
        track = None
        for blk in re.split(r'(?=^m=)', body, flags=re.M):
            if blk.startswith("m=audio") and "sendonly" in blk:
                c = re.search(r'a=control:(\S+)', blk)
                track = c.group(1) if c else None
                p = re.search(r'a=rtpmap:(\d+) PCMA/8000', blk)
                if p:
                    self.payload_type = int(p.group(1))
                break
        if not track:
            raise RuntimeError("la cámara no ofrece pista de audio de subida (sin talkback)")
        setup_uri = track if track.startswith("rtsp://") else content_base.rstrip("/") + "/" + track.lstrip("/")
        # SETUP (TCP interleaved)
        head, _ = await self._request("SETUP", setup_uri,
                                      {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"})
        if "200" not in head.split("\r\n", 1)[0]:
            raise RuntimeError(f"SETUP falló: {head.splitlines()[0] if head else '?'}")
        s = re.search(r'Session:\s*([^;\r\n]+)', head)
        if s:
            self._session = s.group(1).strip()
        ch = re.search(r'interleaved=(\d+)-\d+', head)
        self.channel = int(ch.group(1)) if ch else 0
        self.base = content_base
        # RECORD
        head, _ = await self._request("RECORD", content_base, {"Range": "npt=0.000-"})
        if "200" not in head.split("\r\n", 1)[0]:
            raise RuntimeError(f"RECORD falló: {head.splitlines()[0] if head else '?'}")

    def _rtp(self, alaw: bytes) -> bytes:
        hdr = struct.pack("!BBHII", 0x80, self.payload_type & 0x7F,
                          self._seq & 0xFFFF, self._ts & 0xFFFFFFFF, self._ssrc & 0xFFFFFFFF)
        self._seq += 1
        self._ts += SAMPLES
        return hdr + alaw

    async def _send_packet(self, alaw: bytes):
        rtp = self._rtp(alaw)
        self._w.write(b"$" + bytes([self.channel]) + struct.pack("!H", len(rtp)) + rtp)
        await self._w.drain()

    async def pace(self):
        """Empuja un paquete cada 20 ms: lo que haya en la cola, o silencio."""
        loop = asyncio.get_event_loop()
        start = loop.time()
        n = 0
        while not self._closed:
            try:
                frame = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                frame = ALAW_SILENCE
            # fragmentar/rellenar a múltiplos de 160 bytes (20 ms)
            if len(frame) != SAMPLES:
                frame = (frame + ALAW_SILENCE)[:SAMPLES] if len(frame) < SAMPLES else frame[:SAMPLES]
            try:
                await self._send_packet(frame)
            except (ConnectionError, OSError):
                break
            n += 1
            nxt = start + n * PTIME_S
            delay = nxt - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.5:               # nos quedamos atrás: resincroniza
                start = loop.time()
                n = 0

    async def feed(self, data: bytes):
        """Encola audio A-law del navegador, troceado en 160 bytes (20 ms)."""
        for i in range(0, len(data), SAMPLES):
            chunk = data[i:i + SAMPLES]
            try:
                self.queue.put_nowait(chunk)
            except asyncio.QueueFull:
                try:
                    self.queue.get_nowait()        # descarta lo más viejo
                    self.queue.put_nowait(chunk)
                except asyncio.QueueEmpty:
                    pass

    async def close(self):
        self._closed = True
        if self._w:
            try:
                await self._request("TEARDOWN", self.base, {})
            except Exception:  # noqa: BLE001
                pass
            try:
                self._w.close()
            except Exception:  # noqa: BLE001
                pass
