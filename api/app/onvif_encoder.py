"""Ajuste del encoder por ONVIF: pone el GOP (GovLength) en ~1 s para bajar la
latencia (keyframe = fps). Opcional la hora por NTP.

El GOP grande de las cámaras (keyframe cada varios segundos) es la causa típica
de 2-5 s de retraso en el vídeo en vivo. Bajarlo a 1 s da tiempo casi real, sin
transcodificar. Funciona en cámaras ONVIF de verdad (Dahua/Hik); no en las que
tienen ONVIF apagado (algunas Grandstream) ni en las solo-nube.

Método SetVideoEncoderConfiguration: se REUSA el bloque que devuelve
GetVideoEncoderConfigurations (cambiando solo GovLength) para no omitir campos.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
from datetime import datetime, timezone

import httpx

TRT = "http://www.onvif.org/ver10/media/wsdl"
MEDIA_PATHS = ("/onvif/media_service", "/onvif/Media", "/onvif/device_service")


def _security(user: str, pwd: str) -> str:
    nonce = secrets.token_bytes(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + pwd.encode()).digest()).decode()
    nb = base64.b64encode(nonce).decode()
    w = "http://docs.oasis-open.org/wss/2004/01"
    return (
        f'<s:Header><Security s:mustUnderstand="1" xmlns="{w}/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        f'<UsernameToken><Username>{user}</Username>'
        f'<Password Type="{w}/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>'
        f'<Nonce EncodingType="{w}/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nb}</Nonce>'
        f'<Created xmlns="{w}/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
        '</UsernameToken></Security></s:Header>'
    )


def _envelope(user: str, pwd: str, body: str) -> str:
    return ('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            f'xmlns:trt="{TRT}" xmlns:tt="http://www.onvif.org/ver10/schema">'
            + _security(user, pwd) + '<s:Body>' + body + '</s:Body></s:Envelope>')


async def _post(client, host, path, user, pwd, body):
    return await client.post(f"http://{host}{path}", content=_envelope(user, pwd, body),
                             headers={"Content-Type": "application/soap+xml"}, timeout=10)


async def set_gop(host: str, user: str, pwd: str, seconds: int = 1) -> list[dict]:
    """Pone GovLength = fps*seconds en cada config de encoder. Devuelve la lista
    de {token, encoding, fps, gov} ya aplicada. Lanza si ONVIF no responde."""
    async with httpx.AsyncClient() as client:
        # localizar el servicio media que responde GetVideoEncoderConfigurations
        txt = None
        for path in MEDIA_PATHS:
            try:
                r = await _post(client, host, path, user, pwd, '<trt:GetVideoEncoderConfigurations/>')
            except httpx.HTTPError:
                continue
            if r.status_code == 200 and "VideoEncoderConfiguration" in r.text:
                media, txt = path, r.text
                break
        if txt is None:
            raise RuntimeError("la cámara no respondió ONVIF (encoder)")

        blocks = re.findall(r'(<(\w+):Configurations\b.*?</\2:Configurations>)', txt, re.S)
        if not blocks:
            raise RuntimeError("sin configuraciones de encoder")

        applied: list[dict] = []
        for blk, pref in blocks:
            tok = re.search(r'token="([^"]+)"', blk)
            fr = re.search(r'<\w*:?FrameRateLimit>([^<]+)<', blk)
            enc = re.search(r'<\w*:?Encoding>([^<]+)<', blk)
            fps = int(float(fr.group(1))) if fr else 15
            target = max(1, fps * max(1, seconds))
            conf = (blk.replace(f"<{pref}:Configurations", "<trt:Configuration")
                       .replace(f"</{pref}:Configurations>", "</trt:Configuration>"))
            conf = re.sub(r'(<\w*:?GovLength>)[^<]+(</\w*:?GovLength>)',
                          rf'\g<1>{target}\g<2>', conf)
            body = (f'<trt:SetVideoEncoderConfiguration>{conf}'
                    '<trt:ForcePersistence>true</trt:ForcePersistence></trt:SetVideoEncoderConfiguration>')
            r = await _post(client, host, media, user, pwd, body)
            ok = r.status_code == 200 and "Fault" not in r.text
            applied.append({
                "token": tok.group(1) if tok else "?",
                "encoding": enc.group(1) if enc else "?",
                "fps": fps, "gov": target, "ok": ok,
            })
        if not any(a["ok"] for a in applied):
            raise RuntimeError("el SET de encoder fue rechazado por la cámara")
        return applied
