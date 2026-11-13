"""Cliente ONVIF unicast: interroga un equipo en :8080/:80 y obtiene sus
URLs RTSP reales (GetStreamUri), en vez de adivinar rutas.

Muchas cámaras exponen ONVIF aunque su RTSP esté en un puerto no estándar; así
se descubren las que el barrido de puertos RTSP no encuentra (p. ej. una
Geovision en :80) y se obtiene la ruta exacta de cada perfil.

La autenticación es WS-Security UsernameToken con PasswordDigest, que es lo que
piden estas cámaras; la maneja la aplicación, como el resto.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote, urlparse

import httpx

from .scanner import Device, Stream

DEVICE_PATHS = ("/onvif/device_service",)
MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"


# --------------------------------------------------------------------------
# Construcción de mensajes SOAP
# --------------------------------------------------------------------------

_PWD_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0"


def _security_header(user: str, password: str, pwd_type: str = "digest") -> str:
    if not user:
        return ""
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    if pwd_type == "text":
        # Clave en texto plano: algunas cámaras rechazan el digest
        # ("Incorrect password type") y solo aceptan esta variante.
        pwd_el = f'<wsse:Password Type="{_PWD_NS}#PasswordText">{_xml(password)}</wsse:Password>'
        nonce_el = ""
    else:
        nonce = secrets.token_bytes(16)
        digest = base64.b64encode(
            hashlib.sha1(nonce + created.encode() + password.encode()).digest()
        ).decode()
        nonce_b64 = base64.b64encode(nonce).decode()
        pwd_el = (
            f'<wsse:Password Type="{_PWD_NS}#PasswordDigest">{digest}</wsse:Password>'
            f'<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce_b64}</wsse:Nonce>'
        )
        nonce_el = ""

    return (
        '<s:Header>'
        '<wsse:Security s:mustUnderstand="1" '
        'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
        'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        '<wsse:UsernameToken>'
        f'<wsse:Username>{_xml(user)}</wsse:Username>'
        f'{pwd_el}{nonce_el}'
        f'<wsu:Created>{created}</wsu:Created>'
        '</wsse:UsernameToken></wsse:Security></s:Header>'
    )


def _xml(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _envelope(body: str, user: str, password: str, pwd_type: str = "digest") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        f'{_security_header(user, password, pwd_type)}'
        f'<s:Body>{body}</s:Body></s:Envelope>'
    )


BODY_DEVICE_INFO = f'<tds:GetDeviceInformation xmlns:tds="{DEVICE_NS}"/>'
BODY_CAPABILITIES = (
    f'<tds:GetCapabilities xmlns:tds="{DEVICE_NS}">'
    '<tds:Category>Media</tds:Category></tds:GetCapabilities>'
)
BODY_PROFILES = f'<trt:GetProfiles xmlns:trt="{MEDIA_NS}"/>'


def _body_stream_uri(token: str) -> str:
    return (
        f'<trt:GetStreamUri xmlns:trt="{MEDIA_NS}" '
        'xmlns:tt="http://www.onvif.org/ver10/schema">'
        '<trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>'
        '<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>'
        '</trt:StreamSetup>'
        f'<trt:ProfileToken>{_xml(token)}</trt:ProfileToken></trt:GetStreamUri>'
    )


# --------------------------------------------------------------------------
# XML: búsqueda por nombre local (ignorando namespaces)
# --------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(elem, name: str):
    for e in elem.iter():
        if _local(e.tag) == name:
            return e
    return None


def _find_all(elem, name: str):
    return [e for e in elem.iter() if _local(e.tag) == name]


# --------------------------------------------------------------------------
# Llamadas
# --------------------------------------------------------------------------

async def _soap(client: httpx.AsyncClient, url: str, body: str,
                user: str, password: str, timeout: float, pwd_type: str = "digest"):
    try:
        r = await client.post(
            url,
            content=_envelope(body, user, password, pwd_type).encode(),
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            timeout=timeout,
        )
    except httpx.HTTPError:
        return None, 0
    try:
        return ET.fromstring(r.content), r.status_code
    except ET.ParseError:
        return None, r.status_code


def _is_fault(elem) -> bool:
    return elem is None or _find(elem, "Fault") is not None


async def probe(
    host: str,
    credentials: list[tuple[str, str]],
    ports: tuple[int, ...] = (8080, 80),
    timeout: float = 5.0,
) -> Optional[Device]:
    """Interroga ONVIF y devuelve un Device con las URLs RTSP reales, o None."""
    async with httpx.AsyncClient() as client:
        for port in ports:
            base = f"http://{host}:{port}"
            for path in DEVICE_PATHS:
                url = base + path
                resp, _ = await _soap(client, url, BODY_DEVICE_INFO, "", "", timeout)
                # ONVIF está aquí si responde SOAP (aunque sea un Fault de auth).
                if resp is None:
                    continue
                onvif_here = (_find(resp, "Manufacturer") is not None
                              or _find(resp, "Fault") is not None)
                if not onvif_here:
                    continue

                # Anónimo funcionó.
                if _find(resp, "Manufacturer") is not None:
                    return await _collect(client, host, url, resp, "", "", "digest", timeout)

                # Necesita credenciales: probar cada juego, con digest y texto.
                for u, p in credentials:
                    if not u:
                        continue
                    for pt in ("digest", "text"):
                        info2, _ = await _soap(client, url, BODY_DEVICE_INFO, u, p, timeout, pt)
                        if info2 is not None and _find(info2, "Manufacturer") is not None:
                            return await _collect(client, host, url, info2, u, p, pt, timeout)

                dev = Device(host=host, port=554, server="ONVIF", vendor="ONVIF",
                             needs_auth=True)
                dev.error = "ONVIF requiere credenciales válidas"
                return dev
    return None


async def _collect(client, host, url, info, user, pwd, pwd_type, timeout) -> Device:
    dev = Device(host=host, port=554, server="ONVIF", vendor="ONVIF",
                 needs_auth=bool(user))

    maker = _find(info, "Manufacturer")
    model = _find(info, "Model")
    parts = [x.text for x in (maker, model) if x is not None and x.text]
    if parts:
        dev.vendor = " ".join(parts).strip()
        dev.server = dev.vendor

    # Endpoint del servicio Media (si lo anuncia); si no, el mismo device_service.
    media_url = url
    caps, _ = await _soap(client, url, BODY_CAPABILITIES, user, pwd, timeout, pwd_type)
    if not _is_fault(caps):
        media = _find(caps, "Media")
        xaddr = _find(media, "XAddr") if media is not None else None
        if xaddr is not None and xaddr.text:
            media_url = _rehost(xaddr.text, host)

    profiles, _ = await _soap(client, media_url, BODY_PROFILES, user, pwd, timeout, pwd_type)
    if _is_fault(profiles):
        dev.auth_ok = not dev.needs_auth
        dev.error = "ONVIF respondió pero no entregó perfiles"
        return dev

    seen: set[str] = set()
    for prof in _find_all(profiles, "Profiles"):
        token = prof.get("token") or ""
        if not token:
            continue
        enc = _find(prof, "Encoding")
        res = _find(prof, "Resolution")
        width = height = None
        if res is not None:
            w = _find(res, "Width")
            h = _find(res, "Height")
            width = int(w.text) if w is not None and w.text else None
            height = int(h.text) if h is not None and h.text else None
        codec = _norm_codec(enc.text if enc is not None and enc.text else "")

        uri_resp, _ = await _soap(client, media_url, _body_stream_uri(token),
                                  user, pwd, timeout, pwd_type)
        if _is_fault(uri_resp):
            continue
        uri_el = _find(uri_resp, "Uri")
        if uri_el is None or not uri_el.text:
            continue
        rtsp = uri_el.text.strip()
        if rtsp in seen:
            continue
        seen.add(rtsp)

        parsed = urlparse(rtsp)
        dev.port = parsed.port or 554
        dev.streams.append(Stream(
            path=(parsed.path + (f"?{parsed.query}" if parsed.query else "")) or "/",
            url=_with_credentials(rtsp, user, pwd),
            codec=codec,
            width=width,
            height=height,
            profile="",
            audio_codec="",
            label=_find_text(prof, "Name") or token,
        ))

    if dev.streams:
        dev.auth_ok = True
    else:
        dev.error = dev.error or "ONVIF sin flujos RTSP"
    return dev


def _find_text(elem, name: str) -> str:
    e = _find(elem, name)
    return e.text.strip() if e is not None and e.text else ""


def _norm_codec(enc: str) -> str:
    e = enc.upper()
    if e in ("H264",):
        return "H264"
    if e in ("H265", "HEVC"):
        return "H265"
    if e in ("JPEG", "MJPEG"):
        return "JPEG"
    return e or "?"


def _rehost(xaddr: str, host: str) -> str:
    """La XAddr suele traer una IP interna equivocada: se usa el host real."""
    p = urlparse(xaddr)
    port = f":{p.port}" if p.port else ""
    return f"{p.scheme}://{host}{port}{p.path}"


def _with_credentials(rtsp: str, user: str, pwd: str) -> str:
    if not user:
        return rtsp
    p = urlparse(rtsp)
    auth = f"{quote(user, safe='')}:{quote(pwd, safe='')}@"
    netloc = p.netloc.split("@")[-1]
    rebuilt = f"{p.scheme}://{auth}{netloc}{p.path}"
    if p.query:
        rebuilt += f"?{p.query}"
    return rebuilt
