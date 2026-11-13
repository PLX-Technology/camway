"""Control PTZ por ONVIF (para cámaras IMOU/Dahua que bloquean la CGI HTTP).

Autentica con WS-Security UsernameToken (digest = Base64(SHA1(nonce+created+pwd))),
obtiene el token de perfil (GetProfiles) y manda ContinuousMove + Stop para
emular un "paso" por clic. Cachea el token de perfil por host.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import secrets
from datetime import datetime, timezone, timedelta

import httpx

SPEED = float(os.environ.get("CAMWAY_ONVIF_PTZ_SPEED", "0.6"))
STEP_S = float(os.environ.get("CAMWAY_ONVIF_PTZ_STEP", "0.3"))
ZOOM_SPEED = float(os.environ.get("CAMWAY_ONVIF_ZOOM_SPEED", "0.6"))
MOVE_TIMEOUT = int(os.environ.get("CAMWAY_ONVIF_MOVE_TIMEOUT", "2"))  # auto-stop de seguridad (s)

_tokens: dict[str, str] = {}    # host -> profile token (cache)
_offsets: dict[str, float] = {}  # host -> segundos a sumar a utcnow para igualar el reloj de la cámara
_VEL = {"up": (0.0, 1.0), "down": (0.0, -1.0), "left": (-1.0, 0.0), "right": (1.0, 0.0)}
_ZOOM = {"zoomin": 1.0, "zoomout": -1.0}


def _security(user: str, pwd: str, created: str) -> str:
    nonce = secrets.token_bytes(16)
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + pwd.encode()).digest()).decode()
    nb = base64.b64encode(nonce).decode()
    wss = "http://docs.oasis-open.org/wss/2004/01"
    return (
        f'<s:Header><Security s:mustUnderstand="1" xmlns="{wss}/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        f'<UsernameToken><Username>{user}</Username>'
        f'<Password Type="{wss}/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>'
        f'<Nonce EncodingType="{wss}/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nb}</Nonce>'
        f'<Created xmlns="{wss}/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
        '</UsernameToken></Security></s:Header>'
    )


def _envelope(user: str, pwd: str, body: str, created: str) -> str:
    return ('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
            + _security(user, pwd, created) + '<s:Body>' + body + '</s:Body></s:Envelope>')


async def _ensure_offset(client, host: str) -> float:
    """Desfase (s) entre el reloj de la cámara y el nuestro. Muchas cámaras traen
    el reloj sin ajustar (1970) y rechazan la firma WS-Security por 'replay' si el
    timestamp no cae en SU ventana. Pedimos GetSystemDateAndTime (sin auth) y
    firmamos con la hora de la cámara."""
    if host in _offsets:
        return _offsets[host]
    off = 0.0
    body = '<GetSystemDateAndTime xmlns="http://www.onvif.org/ver10/device/wsdl"/>'
    env = ('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
           + body + '</s:Body></s:Envelope>')
    for path in ("/onvif/device_service", "/onvif/Device", "/onvif/services"):
        try:
            r = await client.post(f"http://{host}{path}", content=env,
                                  headers={"Content-Type": "application/soap+xml"}, timeout=6)
        except httpx.HTTPError:
            continue
        if r.status_code == 200 and "UTCDateTime" in r.text:
            seg = re.search(r"UTCDateTime>(.*?)</[^>]*UTCDateTime>", r.text, re.S)
            seg = seg.group(1) if seg else r.text
            def g(tag):
                m = re.search(tag + r">(\d+)<", seg)
                return int(m.group(1)) if m else None
            try:
                cam = datetime(g("Year"), g("Month"), g("Day"),
                               g("Hour"), g("Minute"), g("Second"), tzinfo=timezone.utc)
                off = (cam - datetime.now(timezone.utc)).total_seconds()
            except Exception:  # noqa: BLE001
                off = 0.0
            break
    _offsets[host] = off
    return off


async def _post(client, host, path, user, pwd, body):
    off = await _ensure_offset(client, host)
    created = (datetime.now(timezone.utc) + timedelta(seconds=off)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return await client.post(f"http://{host}{path}", content=_envelope(user, pwd, body, created),
                             headers={"Content-Type": "application/soap+xml"}, timeout=8)


async def _profile_token(client, host, user, pwd) -> str:
    if host in _tokens:
        return _tokens[host]
    body = '<GetProfiles xmlns="http://www.onvif.org/ver10/media/wsdl"/>'
    for path in ("/onvif/media_service", "/onvif/Media", "/onvif/device_service"):
        try:
            r = await _post(client, host, path, user, pwd, body)
        except httpx.HTTPError:
            continue
        if r.status_code == 200 and "token=" in r.text:
            m = (re.search(r'[Pp]rofiles[^>]*token="([^"]+)"', r.text)
                 or re.search(r'token="([^"]+)"', r.text))
            if m:
                _tokens[host] = m.group(1)
                return _tokens[host]
    raise RuntimeError("no se pudo obtener el perfil ONVIF (¿credenciales?)")


async def probe(host: str, user: str, pwd: str) -> bool:
    """Comprueba que la cámara responde ONVIF con esas credenciales (obtiene un
    perfil) SIN mover nada. Para validar al activar el PTZ desde la UI."""
    _tokens.pop(host, None)      # no dar por buena una credencial vieja cacheada
    async with httpx.AsyncClient() as client:
        try:
            await _profile_token(client, host, user, pwd)
            return True
        except Exception:
            return False


async def has_zoom(host: str, user: str, pwd: str) -> bool:
    """¿La cámara soporta zoom óptico por ONVIF? (muchas PTZ domésticas, p.ej.
    IMOU Cruiser, solo hacen pan/tilt). Mira si GetConfigurations expone un
    espacio de Zoom."""
    try:
        async with httpx.AsyncClient() as client:
            await _ensure_offset(client, host)
            body = '<GetConfigurations xmlns="http://www.onvif.org/ver20/ptz/wsdl"/>'
            for path in ("/onvif/ptz_service", "/onvif/PTZ", "/onvif/device_service"):
                try:
                    r = await _post(client, host, path, user, pwd, body)
                except httpx.HTTPError:
                    continue
                if r.status_code == 200 and "Fault" not in r.text:
                    return "Zoom" in r.text
        return False
    except Exception:  # noqa: BLE001
        return False


def _vel_for(direction: str):
    z = _ZOOM.get(direction)
    x, y = _VEL.get(direction, (0.0, 0.0))
    if z is not None:
        return f'<Zoom x="{z*ZOOM_SPEED}" xmlns="http://www.onvif.org/ver10/schema"/>'
    if x == 0.0 and y == 0.0:
        return None
    return f'<PanTilt x="{x*SPEED}" y="{y*SPEED}" xmlns="http://www.onvif.org/ver10/schema"/>'


async def move(host: str, user: str, pwd: str, direction: str):
    """Inicia un movimiento continuo. SIEMPRE con Timeout: si se pierde el stop
    (fallo de red, el 'soltar' que no llega), la cámara se detiene sola en
    MOVE_TIMEOUT s y nunca queda girando sin control. El control 'mantener
    pulsado' reenvía move() periódicamente para sostener el giro."""
    vel = _vel_for(direction)
    if vel is None:
        raise RuntimeError("dirección no soportada")
    async with httpx.AsyncClient() as client:
        token = await _profile_token(client, host, user, pwd)
        body = ('<ContinuousMove xmlns="http://www.onvif.org/ver20/ptz/wsdl">'
                f'<ProfileToken>{token}</ProfileToken><Velocity>{vel}</Velocity>'
                f'<Timeout>PT{MOVE_TIMEOUT}S</Timeout></ContinuousMove>')
        for path in ("/onvif/ptz_service", "/onvif/PTZ", "/onvif/device_service"):
            try:
                r = await _post(client, host, path, user, pwd, body)
            except httpx.HTTPError:
                continue
            if r.status_code == 200 and "Fault" not in r.text:
                return
        raise RuntimeError("ContinuousMove ONVIF falló")


async def stop(host: str, user: str, pwd: str):
    async with httpx.AsyncClient() as client:
        token = await _profile_token(client, host, user, pwd)
        body = ('<Stop xmlns="http://www.onvif.org/ver20/ptz/wsdl">'
                f'<ProfileToken>{token}</ProfileToken><PanTilt>true</PanTilt><Zoom>true</Zoom></Stop>')
        for path in ("/onvif/ptz_service", "/onvif/PTZ", "/onvif/device_service"):
            try:
                r = await _post(client, host, path, user, pwd, body)
            except httpx.HTTPError:
                continue
            if r.status_code == 200 and "Fault" not in r.text:
                return
        raise RuntimeError("Stop ONVIF falló")


async def step(host: str, user: str, pwd: str, direction: str):
    """Un 'paso' breve: mover y DETENER en la misma operación. El stop se
    reintenta para que SIEMPRE quede detenida (es imposible que gire sin fin)."""
    await move(host, user, pwd, direction)
    await asyncio.sleep(STEP_S)
    last = None
    for _ in range(3):                 # el stop es crítico: reintentar
        try:
            await stop(host, user, pwd)
            return
        except Exception as e:  # noqa: BLE001
            last = e
            await asyncio.sleep(0.2)
    if last:
        raise last
