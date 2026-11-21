"""OSD "quemado" en la cámara: nombre mostrado en el video + sello de fecha/hora,
y puesta en hora del reloj de la cámara.

A diferencia de un overlay que pinta CamWay, esto escribe la configuración DENTRO
de la cámara, así que el texto queda grabado en el propio video (se ve en cualquier
reproductor y en las grabaciones). Es un cambio PERSISTENTE en el equipo.

Dos vías según el fabricante:
  - ISAPI (Hikvision / HiLook): REST/XML con digest. Fiable y probado.
  - ONVIF (HJT explosion-proof y genéricas ONVIF): SOAP. La hora (SetSystemDateAndTime)
    es estándar; el OSD (SetOSD) varía por modelo → mejor esfuerzo, validar en campo.

Las credenciales salen de la URL RTSP de la cámara (quien llama las extrae).
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
from datetime import datetime, timezone

import httpx

XML_HDR = {"Content-Type": "application/xml"}


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------
# ISAPI (Hikvision / HiLook)
# --------------------------------------------------------------------------

def _set_enabled(xml: str, tag: str, val: bool) -> str:
    """Pone <enabled>true/false</enabled> DENTRO del primer bloque <tag>…</tag>."""
    def repl(m: re.Match) -> str:
        blk = m.group(0)
        if "<enabled>" in blk:
            return re.sub(r"<enabled>.*?</enabled>",
                          f"<enabled>{'true' if val else 'false'}</enabled>",
                          blk, count=1, flags=re.S)
        return blk
    return re.sub(rf"<{tag}\b.*?</{tag}>", repl, xml, count=1, flags=re.S)


async def isapi_osd(host: str, user: str, pwd: str, name: str | None,
                    show_name: bool, show_time: bool, ch: int = 1) -> dict:
    """Pone el nombre del canal y activa/desactiva los overlays de nombre y
    fecha/hora por ISAPI. Lanza si la cámara no habla ISAPI."""
    auth = httpx.DigestAuth(user, pwd)
    done = {"name": False, "name_overlay": False, "time_overlay": False}
    async with httpx.AsyncClient(auth=auth, timeout=12) as c:
        base = f"http://{host}/ISAPI/System/Video/inputs/channels/{ch}"
        # 1) Texto del nombre del canal
        if name is not None:
            r = await c.get(base)
            if r.status_code != 200 or "<" not in r.text:
                raise RuntimeError(f"ISAPI canal {r.status_code} (¿no es Hikvision/HiLook?)")
            xml = r.text
            if "<name>" in xml:
                xml = re.sub(r"<name>.*?</name>", f"<name>{_esc(name)}</name>",
                             xml, count=1, flags=re.S)
            else:
                xml = re.sub(r"(</id>)", rf"\g<1><name>{_esc(name)}</name>", xml, count=1)
            rp = await c.put(base, content=xml, headers=XML_HDR)
            if rp.status_code not in (200, 201):
                raise RuntimeError(f"ISAPI PUT nombre {rp.status_code}")
            done["name"] = True
        # 2) Overlays: mostrar nombre + mostrar fecha/hora
        r = await c.get(base + "/overlays")
        if r.status_code != 200:
            raise RuntimeError(f"ISAPI overlays {r.status_code}")
        ov = r.text
        ov = _set_enabled(ov, "channelNameOverlay", show_name)
        ov = _set_enabled(ov, "DateTimeOverlay", show_time)
        rp = await c.put(base + "/overlays", content=ov, headers=XML_HDR)
        if rp.status_code not in (200, 201):
            raise RuntimeError(f"ISAPI PUT overlays {rp.status_code}")
        done["name_overlay"] = show_name
        done["time_overlay"] = show_time
    return {"method": "isapi", **done}


async def isapi_set_time(host: str, user: str, pwd: str,
                         ntp: str | None, tz_hik: str = "CST-4:00:00") -> dict:
    """Pone el reloj de la cámara. Con `ntp` activa modo NTP contra ese servidor;
    sin `ntp` fija la hora MANUAL al instante actual (one-shot). tz_hik por
    defecto = Venezuela (UTC-4; Hik usa signo invertido)."""
    auth = httpx.DigestAuth(user, pwd)
    async with httpx.AsyncClient(auth=auth, timeout=12) as c:
        if ntp:
            body = ('<NTPServer><id>1</id><addressingFormatType>hostname'
                    f'</addressingFormatType><hostName>{_esc(ntp)}</hostName>'
                    '<portNo>123</portNo></NTPServer>')
            await c.put(f"http://{host}/ISAPI/System/time/ntpServers/1",
                        content=body, headers=XML_HDR)
            t = (f'<Time><timeMode>NTP</timeMode><timeZone>{tz_hik}</timeZone></Time>')
        else:
            now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            t = (f'<Time><timeMode>manual</timeMode>'
                 f'<localTime>{now}</localTime><timeZone>{tz_hik}</timeZone></Time>')
        rp = await c.put(f"http://{host}/ISAPI/System/time", content=t, headers=XML_HDR)
        if rp.status_code not in (200, 201):
            raise RuntimeError(f"ISAPI PUT time {rp.status_code}")
    return {"method": "isapi", "mode": "ntp" if ntp else "manual"}


# --------------------------------------------------------------------------
# ONVIF (HJT explosion-proof y genéricas) — hora estándar; OSD mejor esfuerzo
# --------------------------------------------------------------------------

_DEV = "http://www.onvif.org/ver10/device/wsdl"
_MEDIA = "http://www.onvif.org/ver10/media/wsdl"
_SCHEMA = "http://www.onvif.org/ver10/schema"
_DEV_PATHS = ("/onvif/device_service", "/onvif/Device", "/onvif/device")
_MEDIA_PATHS = ("/onvif/media", "/onvif/media_service", "/onvif/Media", "/onvif/device_service")


def _wss(user: str, pwd: str) -> str:
    nonce = secrets.token_bytes(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + pwd.encode()).digest()).decode()
    nb = base64.b64encode(nonce).decode()
    w = "http://docs.oasis-open.org/wss/2004/01"
    return (f'<s:Header><Security s:mustUnderstand="1" '
            f'xmlns="{w}/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
            f'<UsernameToken><Username>{_esc(user)}</Username>'
            f'<Password Type="{w}/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>'
            f'<Nonce EncodingType="{w}/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nb}</Nonce>'
            f'<Created xmlns="{w}/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
            '</UsernameToken></Security></s:Header>')


def _env(user: str, pwd: str, body: str, ns: str) -> str:
    return ('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            f'xmlns:x="{ns}" xmlns:tt="{_SCHEMA}">'
            + _wss(user, pwd) + '<s:Body>' + body + '</s:Body></s:Envelope>')


async def _soap(client, host, paths, user, pwd, body, ns):
    for path in paths:
        try:
            r = await client.post(f"http://{host}{path}", content=_env(user, pwd, body, ns),
                                   headers={"Content-Type": "application/soap+xml"}, timeout=10)
        except httpx.HTTPError:
            continue
        if r.status_code in (200, 400, 500) and "Envelope" in r.text:
            return path, r
    return None, None


async def onvif_set_ntp(host: str, user: str, pwd: str, server: str) -> dict:
    """Apunta la cámara a un servidor NTP concreto (el de CamWay).

    Muchas cámaras dejan el NTP en servidores de internet que no alcanzan desde
    la LAN de cámaras (p. ej. `time.windows.com`), así que el reloj se queda
    congelado en 2018 y el OSD y las grabaciones salen con fecha falsa.
    """
    body = ('<x:SetNTP><x:FromDHCP>false</x:FromDHCP>'
            f'<x:NTPManual><tt:Type>IPv4</tt:Type>'
            f'<tt:IPv4Address>{_esc(server)}</tt:IPv4Address></x:NTPManual></x:SetNTP>')
    async with httpx.AsyncClient() as c:
        path, r = await _soap(c, host, _DEV_PATHS, user, pwd, body, _DEV)
    if r is None:
        raise RuntimeError("la cámara no respondió ONVIF (SetNTP)")
    if "Fault" in r.text:
        raise RuntimeError("ONVIF SetNTP rechazado")
    return {"method": "onvif", "ntp": server}


async def onvif_set_time(host: str, user: str, pwd: str, ntp: str | None = None) -> dict:
    """Pone el reloj de la cámara.

    Con `ntp`: apunta la cámara a ese servidor y la deja en modo NTP (la zona
    horaria NO se toca: los clones iCamra la ignoran en SetSystemDateAndTime y
    aceptar una desconocida la deja en UTC).
    Sin `ntp`: fija la hora MANUAL al UTC actual, con DST off.
    """
    if ntp:
        res = await onvif_set_ntp(host, user, pwd, ntp)
        res["time"] = "ntp"
        return res
    now = datetime.now(timezone.utc)
    body = (f'<x:SetSystemDateAndTime><x:DateTimeType>Manual</x:DateTimeType>'
            f'<x:DaylightSavings>false</x:DaylightSavings>'
            f'<x:UTCDateTime><tt:Time><tt:Hour>{now.hour}</tt:Hour>'
            f'<tt:Minute>{now.minute}</tt:Minute><tt:Second>{now.second}</tt:Second></tt:Time>'
            f'<tt:Date><tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month>'
            f'<tt:Day>{now.day}</tt:Day></tt:Date></x:UTCDateTime></x:SetSystemDateAndTime>')
    async with httpx.AsyncClient() as c:
        path, r = await _soap(c, host, _DEV_PATHS, user, pwd, body, _DEV)
    if r is None:
        raise RuntimeError("la cámara no respondió ONVIF (device)")
    if "Fault" in r.text:
        raise RuntimeError("ONVIF SetSystemDateAndTime rechazado")
    return {"method": "onvif", "time": "utc-now"}


async def onvif_set_osd_text(host: str, user: str, pwd: str, name: str) -> dict:
    """Best-effort: localiza un OSD de texto y le pone `name`. El soporte de
    SetOSD varía por modelo → validar en el HJT real. Lanza si no se puede."""
    if not (name or "").strip():
        # Nunca mandar un texto vacío: algunas cámaras lo aceptan y BORRAN el
        # rótulo que tenían (pasó con la .34 al pulsar solo «poner en hora»).
        raise RuntimeError("texto de OSD vacío: no se toca")
    async with httpx.AsyncClient() as c:
        # Prueba TODAS las rutas del servicio Media: algunas cámaras responden
        # Fault en las rutas "estándar" y sirven GetOSDs en otra (p.ej. /onvif/media).
        path = r = None
        for p in _MEDIA_PATHS:
            _p, _r = await _soap(c, host, (p,), user, pwd, '<x:GetOSDs/>', _MEDIA)
            if _r is not None and "Fault" not in _r.text and "token=" in _r.text:
                path, r = _p, _r
                break
        if r is None or "OSD" not in r.text:
            raise RuntimeError("la cámara no expone OSD por ONVIF (GetOSDs)")
        # El elemento puede ser <OSD token="..."> o <OSDs token="..."> (plural).
        tok = (re.search(r'OSDs?\s+token="([^"]+)"', r.text)
               or re.search(r'<\w*:?OSDs?\b[^>]*token="([^"]+)"', r.text))
        cfgtok = re.search(r'<\w*:?VideoSourceConfigurationToken>([^<]+)<', r.text)
        if not tok:
            raise RuntimeError("sin OSD de texto editable")
        vsct = cfgtok.group(1) if cfgtok else ""
        body = (f'<x:SetOSD><x:OSD token="{tok.group(1)}">'
                f'<tt:VideoSourceConfigurationToken>{vsct}</tt:VideoSourceConfigurationToken>'
                '<tt:Type>Text</tt:Type>'
                '<tt:Position><tt:Type>UpperLeft</tt:Type></tt:Position>'
                f'<tt:TextString><tt:Type>Plain</tt:Type><tt:PlainText>{_esc(name)}</tt:PlainText></tt:TextString>'
                '</x:OSD></x:SetOSD>')
        _, rp = await _soap(c, host, (path,), user, pwd, body, _MEDIA)
    if rp is None or "Fault" in rp.text:
        raise RuntimeError("ONVIF SetOSD rechazado por la cámara (modelo no compatible)")
    return {"method": "onvif", "osd_text": name}


# --------------------------------------------------------------------------
# Despachador: intenta ISAPI y, si no, ONVIF
# --------------------------------------------------------------------------

async def apply_osd(host: str, user: str, pwd: str, name: str | None,
                    show_name: bool, show_time: bool, set_time: bool,
                    ntp: str | None = None, do_osd: bool = True) -> dict:
    """Aplica OSD (nombre + overlays) y, opcional, pone en hora. Prueba ISAPI
    (Hikvision/HiLook) y cae a ONVIF (HJT/genéricas). Devuelve qué funcionó.

    `do_osd=False` deja el OSD de la cámara intacto y solo toca la hora: es lo
    que hay que usar cuando el panel pide únicamente «poner en hora».
    """
    res: dict = {"host": host, "errors": []}
    # --- OSD (nombre + overlays) ---
    if do_osd:
        try:
            res["osd"] = await isapi_osd(host, user, pwd, name, show_name, show_time)
        except Exception as e_isapi:  # noqa: BLE001
            try:
                res["osd"] = await onvif_set_osd_text(host, user, pwd, name or "")
                res["osd"]["note"] = "ONVIF: overlays de fecha/hora no se tocan por esta vía"
            except Exception as e_onvif:  # noqa: BLE001
                res["errors"].append(f"OSD: ISAPI='{e_isapi}' ONVIF='{e_onvif}'")
    # --- hora ---
    if set_time:
        try:
            res["time"] = await isapi_set_time(host, user, pwd, ntp)
        except Exception as e_isapi:  # noqa: BLE001
            try:
                res["time"] = await onvif_set_time(host, user, pwd, ntp)
            except Exception as e_onvif:  # noqa: BLE001
                res["errors"].append(f"hora: ISAPI='{e_isapi}' ONVIF='{e_onvif}'")
    res["ok"] = "osd" in res or "time" in res
    return res


# --------------------------------------------------------------------------
# Vigilancia de la hora (para el watchdog periódico)
# --------------------------------------------------------------------------

_TZ_RE = re.compile(r"<tt:TZ>([^<]*)</tt:TZ>")
_LOCAL_RE = re.compile(
    r"<tt:LocalDateTime><tt:Time><tt:Hour>(\d+)</tt:Hour><tt:Minute>(\d+)</tt:Minute>"
    r"<tt:Second>(\d+)</tt:Second></tt:Time><tt:Date><tt:Year>(\d+)</tt:Year>"
    r"<tt:Month>(\d+)</tt:Month><tt:Day>(\d+)</tt:Day>")


async def onvif_read_time(host: str, user: str, pwd: str):
    """(desfase_en_segundos, tz_actual) o (None, None) si no se puede leer.

    El desfase es la hora LOCAL de la cámara menos la hora local del servidor.
    """
    async with httpx.AsyncClient() as c:
        path, r = await _soap(c, host, _DEV_PATHS, user, pwd,
                              "<x:GetSystemDateAndTime/>", _DEV)
    if r is None or "Fault" in r.text:
        return None, None
    m = _LOCAL_RE.search(r.text)
    if not m:
        return None, None
    cam = datetime(int(m.group(4)), int(m.group(5)), int(m.group(6)),
                   int(m.group(1)), int(m.group(2)), int(m.group(3)))
    off = (cam - datetime.now()).total_seconds()
    tz = _TZ_RE.search(r.text)
    return off, (tz.group(1) if tz else None)


async def onvif_fix_clock(host: str, user: str, pwd: str, ntp: str | None) -> dict:
    """Apunta la cámara al NTP de CamWay y limpia el horario de verano pegado.

    El truco que funciona en las iCamra: forzar un cambio de zona (America/
    Caracas, que toman como UTC+0) y volver a la zona original con
    DaylightSavings=false. Sin ese viaje de ida y vuelta el flag de DST sigue
    activo y el OSD queda una hora adelantado, aunque la API diga que no.
    """
    if ntp:
        await onvif_set_ntp(host, user, pwd, ntp)
    _, tz = await onvif_read_time(host, user, pwd)
    tz = tz or "AST4ADT,M4.1.0/00:01:00,M10.5.0/00:01:00"
    async with httpx.AsyncClient() as c:
        for zona in ("America/Caracas", tz):
            body = ('<x:SetSystemDateAndTime><x:DateTimeType>NTP</x:DateTimeType>'
                    '<x:DaylightSavings>false</x:DaylightSavings>'
                    f'<x:TimeZone><tt:TZ>{_esc(zona)}</tt:TZ></x:TimeZone>'
                    '</x:SetSystemDateAndTime>')
            await _soap(c, host, _DEV_PATHS, user, pwd, body, _DEV)
    return {"ntp": ntp, "tz": tz}
