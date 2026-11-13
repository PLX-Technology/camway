"""Conector IMOU Open API → MediaMTX.

Las cámaras IMOU "solo nube" no exponen RTSP local; su Open API
(open.imoulife.com) sí entrega un stream HLS por la nube. Este conector:

  1. Pide un accessToken (firma MD5 con el appSecret; se cachea ~2 días).
  2. Crea el "live" del dispositivo (bindDeviceLive) y obtiene la URL HLS
     (getLiveStreamInfo) del stream elegido (0=HD, 1=SD).
  3. Lanza ffmpeg: HLS (nube) → RTSP a una ruta FIJA de MediaMTX (-c copy).
     Así CamWay ve un RTSP local estable (rtsp://mediamtx:8554/<MTX_PATH>)
     aunque la URL de IMOU caduque: cada tanto se refresca y se reinicia ffmpeg.

Es un stream por la NUBE (latencia + depende de internet + cuotas de IMOU):
bien para la oficina, NO para el borde del taladro (ahí usar RTSP local).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import time
import uuid

import httpx

APP_ID      = os.environ["IMOU_APP_ID"]
APP_SECRET  = os.environ["IMOU_APP_SECRET"]
DEVICE_ID   = os.environ["IMOU_DEVICE_ID"]          # SN del dispositivo
DEVICE_CODE = os.environ.get("IMOU_DEVICE_CODE", "")  # código de seguridad (1ª vez, para vincular)
CHANNEL_ID  = os.environ.get("IMOU_CHANNEL_ID", "0")
STREAM_ID   = os.environ.get("IMOU_STREAM_ID", "0")  # 0=HD, 1=SD
# Centro de datos: openapi.easy4ip.com (global). Si tu cuenta es de otra región
# cámbialo (p.ej. openapi-fk.easy4ip.com, openapi-sg.easy4ip.com).
BASE        = os.environ.get("IMOU_BASE", "https://openapi.easy4ip.com/openapi")
MTX_API     = os.environ.get("CAMWAY_MTX_API", "http://mediamtx:9997")
MTX_RTSP    = os.environ.get("CAMWAY_MTX_RTSP", "rtsp://mediamtx:8554")
MTX_PATH    = os.environ.get("IMOU_MTX_PATH", "imou-office")
REFRESH_S   = int(os.environ.get("IMOU_REFRESH_S", "1800"))   # re-pedir HLS cada 30 min


def _sign(t: int, nonce: str) -> str:
    return hashlib.md5(f"time:{t},nonce:{nonce},appSecret:{APP_SECRET}".encode()).hexdigest()


# La respuesta de accessToken trae el data center real de la cuenta
# (currentDomain, p.ej. openapi-or.easy4ip.com); las demás llamadas van ahí.
_dc = {"base": BASE}


def call(method: str, params: dict) -> dict:
    t = int(time.time())
    nonce = str(uuid.uuid4())
    body = {
        "system": {"ver": "1.0", "appId": APP_ID, "time": t, "nonce": nonce, "sign": _sign(t, nonce)},
        "id": str(uuid.uuid4()),
        "params": params,
    }
    r = httpx.post(f"{_dc['base']}/{method}", json=body, timeout=20)
    r.raise_for_status()
    js = r.json()
    res = js.get("result", {})
    if str(res.get("code")) != "0":
        raise RuntimeError(f"{method}: {res.get('code')} {res.get('msg')}")
    return res.get("data", {}) or {}


_token = {"val": None, "exp": 0}


def token() -> str:
    if _token["val"] and time.time() < _token["exp"]:
        return _token["val"]
    data = call("accessToken", {})
    dom = data.get("currentDomain")
    if dom:
        _dc["base"] = dom.rstrip("/") + "/openapi"   # usar el DC real de la cuenta
    _token["val"] = data["accessToken"]
    # expireTime viene en segundos (~3 días); refrescamos con margen
    _token["exp"] = time.time() + min(int(data.get("expireTime", 172800)), 172800) - 3600
    print(f"[imou] accessToken OK (exp ~{int((_token['exp']-time.time())/3600)}h)", flush=True)
    return _token["val"]


def hls_url() -> str:
    tok = token()
    p = {"token": tok, "deviceId": DEVICE_ID, "channelId": CHANNEL_ID}
    # crear el live (idempotente: si ya existe, IMOU devuelve error benigno)
    try:
        call("bindDeviceLive", p)
    except RuntimeError as e:
        print(f"[imou] bindDeviceLive: {e} (puede ser que ya existía)", flush=True)
    data = call("getLiveStreamInfo", p)
    streams = data.get("streams", [])
    pick = next((s for s in streams if str(s.get("streamId")) == STREAM_ID), None) or (streams[0] if streams else None)
    if not pick or not pick.get("hls"):
        raise RuntimeError(f"sin URL HLS en respuesta: {data}")
    return pick["hls"]


def ensure_mtx_path():
    """Crea la ruta en MediaMTX como 'publisher' (para que ffmpeg pueda empujar)."""
    try:
        httpx.post(f"{MTX_API}/v3/config/paths/add/{MTX_PATH}",
                   json={"source": "publisher"}, timeout=8)
    except httpx.HTTPError:
        pass


def bind_device():
    """Vincula el dispositivo a la app del Open Platform (1ª vez). Necesita el
    código de seguridad de la cámara (etiqueta / app). Idempotente: si ya está
    vinculado, IMOU devuelve un error benigno que ignoramos."""
    if not DEVICE_CODE:
        return
    try:
        call("bindDevice", {"token": token(), "deviceId": DEVICE_ID, "code": DEVICE_CODE})
        print("[imou] dispositivo vinculado a la app", flush=True)
    except RuntimeError as e:
        print(f"[imou] bindDevice: {e} (probablemente ya estaba vinculado)", flush=True)


def main():
    print(f"[imou] conector arrancando: device={DEVICE_ID} ch={CHANNEL_ID} stream={STREAM_ID} → {MTX_RTSP}/{MTX_PATH}", flush=True)
    bind_device()
    ensure_mtx_path()
    while True:
        try:
            url = hls_url()
            print(f"[imou] HLS obtenida, empujando a MediaMTX (refresh {REFRESH_S}s)", flush=True)
            proc = subprocess.Popen(
                ["ffmpeg", "-nostdin", "-loglevel", "warning",
                 "-i", url, "-c", "copy",
                 "-f", "rtsp", "-rtsp_transport", "tcp", f"{MTX_RTSP}/{MTX_PATH}"],
            )
            # correr hasta que ffmpeg muera o toque refrescar la URL
            t0 = time.time()
            while proc.poll() is None and time.time() - t0 < REFRESH_S:
                time.sleep(2)
            if proc.poll() is None:
                proc.terminate()
                try: proc.wait(timeout=5)
                except subprocess.TimeoutExpired: proc.kill()
        except Exception as e:  # noqa: BLE001
            print(f"[imou] error: {e}; reintento en 15s", flush=True)
            time.sleep(15)


if __name__ == "__main__":
    main()
