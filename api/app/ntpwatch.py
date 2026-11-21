"""Vigilante de la hora de las cámaras.

Las iCamra (y otros clones) se quedan con el horario de verano pegado: el OSD
del vídeo acaba una hora adelantado aunque su API ONVIF diga lo contrario, y
la grabación queda con la fecha corrida. También pierden el NTP cuando se
reinician o cuando se cae el enlace.

Este bucle revisa cada `CAMWAY_NTP_WATCH` segundos la hora local de cada cámara
publicada y, si se desvía más de `MAX_OFF` segundos, le repone el NTP de CamWay
y le limpia el DST.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time

from . import camera_osd

log = logging.getLogger("camway.ntp")

INTERVAL = max(120, int(os.environ.get("CAMWAY_NTP_WATCH", "600") or 600))
MAX_OFF = int(os.environ.get("CAMWAY_NTP_MAX_OFF", "60") or 60)
# Además del chequeo por desfase, se reaplica la corrección cada N horas: el
# fallo del DST pegado NO se ve por ONVIF (la cámara reporta hora local
# correcta mientras el OSD va una hora adelantado), así que hay que forzarlo
# cada cierto tiempo para que no se quede ahí para siempre.
REAPPLY_S = max(3600, int(float(os.environ.get("CAMWAY_NTP_REAPPLY_H", "12") or 12) * 3600))


def _camaras(store):
    """[(host, user, pwd)] de cada cámara publicada, sacando las credenciales
    de la URL RTSP (igual que hace el resto de la app)."""
    vistos = set()
    for dev in store.devices():
        for s in dev["streams"]:
            if not s.get("published"):
                continue
            m = re.match(r"rtsp://(?:([^:/@]+):([^@]+)@)?([^:/]+)", s.get("url") or "")
            if not m:
                continue
            host = m.group(3)
            if host in vistos:
                continue
            vistos.add(host)
            yield host, m.group(1) or "", m.group(2) or ""
            break            # una por equipo


async def agent(store) -> None:
    if INTERVAL <= 0:
        return
    ntp = os.environ.get("CAMWAY_NTP_HOST") or None
    log.info("vigilante de hora activo: revisa cada %d s (tope %d s, NTP %s)",
             INTERVAL, MAX_OFF, ntp or "sin servidor")
    await asyncio.sleep(90)          # deja arrancar MediaMTX y las cámaras
    ultimo = {}                      # host -> monotonic de la última corrección
    while True:
        try:
            for host, user, pwd in _camaras(store):
                try:
                    off, _tz = await camera_osd.onvif_read_time(host, user, pwd)
                except Exception:      # noqa: BLE001
                    continue
                vencido = (time.monotonic() - ultimo.get(host, 0.0)) >= REAPPLY_S
                desviado = off is not None and abs(off) > MAX_OFF
                if not vencido and not desviado:
                    continue
                log.info("cámara %s: reaplicando hora (desfase %s s%s)",
                         host, f"{off:+.0f}" if off is not None else "?",
                         ", toca revisión periódica" if vencido and not desviado else "")
                try:
                    await camera_osd.onvif_fix_clock(host, user, pwd, ntp)
                    off2, _ = await camera_osd.onvif_read_time(host, user, pwd)
                    ultimo[host] = time.monotonic()
                    log.info("cámara %s corregida (ahora %+.0f s)", host, off2 or 0)
                except Exception as e:  # noqa: BLE001
                    log.warning("no se pudo corregir la hora de %s: %s", host, e)
        except Exception:              # noqa: BLE001
            log.exception("fallo en el vigilante de hora")
        await asyncio.sleep(INTERVAL)
