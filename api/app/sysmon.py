"""Resumen de salud del equipo para el admin: lee Netdata (que ya recoge lm-sensors).

CamWay corre en Docker y no ve /sys del anfitrión, así que en vez de duplicar
la lectura de sensores se pregunta a la API local de Netdata. Si Netdata no
está, el endpoint responde `ok: false` y la UI lo dice sin romper nada.
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx

NETDATA_URL = os.environ.get("CAMWAY_NETDATA_URL", "http://host.docker.internal:19999").rstrip("/")
_TIMEOUT = 4.0
_CHARTS_TTL = 600.0          # la lista de gráficas casi no cambia: se pide cada 10 min

# umbral (aviso, crítico) en °C por tipo de sensor
_TEMP_LIMITS = {"cpu": (80, 90), "ssd": (70, 80), "gpu": (80, 90)}
_charts_cache: tuple[float, dict] = (0.0, {})


async def _get(c: httpx.AsyncClient, path: str):
    r = await c.get(NETDATA_URL + path)
    r.raise_for_status()
    return r.json()


async def _point(c: httpx.AsyncClient, chart: str) -> dict[str, float]:
    """Último valor de cada dimensión de una gráfica ({} si no existe)."""
    try:
        d = await _get(c, f"/api/v1/data?chart={chart}&after=-10&points=1&group=average&format=json")
        labels, row = d["labels"][1:], d["data"][0][1:]
        return {k: (v or 0.0) for k, v in zip(labels, row)}
    except Exception:  # noqa: BLE001
        return {}


async def _chart_ids(c: httpx.AsyncClient) -> dict:
    """Sensores y contenedores disponibles (cacheado)."""
    global _charts_cache
    now = time.monotonic()
    if _charts_cache[1] and now - _charts_cache[0] < _CHARTS_TTL:
        return _charts_cache[1]
    ids = (await _get(c, "/api/v1/charts"))["charts"]
    temps, power, conts = [], [], []
    for k in ids:
        if k.startswith("sensors.temperature_") and k.endswith("_input"):
            temps.append(k)
        elif k.startswith("sensors.power_") and k.endswith("_input"):
            power.append(k)
        elif k.startswith("cgroup_camway-") and k.endswith(".cpu"):
            conts.append(k[len("cgroup_"):-len(".cpu")])
    out = {"temps": temps, "power": power, "containers": sorted(conts)}
    _charts_cache = (now, out)
    return out


def _sensor_label(chart: str) -> tuple[str, str] | None:
    """(tipo, nombre legible) o None si el sensor no aporta al resumen."""
    if "k10temp" in chart or "coretemp" in chart:
        return "cpu", "CPU"
    if "nvme" in chart:
        return ("ssd", "SSD NVMe") if "Composite" in chart else None   # los «Sensor n» sobran
    if "amdgpu" in chart or "radeon" in chart:
        return "gpu", "GPU integrada"
    return None


def _level(value: float, warn: float, bad: float) -> str:
    return "bad" if value >= bad else "warn" if value >= warn else "ok"


async def health() -> dict:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            ids = await _chart_ids(c)
            temp_q = [_point(c, ch) for ch in ids["temps"]]
            cont_q = [(_point(c, f"cgroup_{n}.cpu"), _point(c, f"cgroup_{n}.mem_usage")) for n in ids["containers"]]
            (info, cpu, ram, load, up, disk, net, alarms, *rest) = await asyncio.gather(
                _get(c, "/api/v1/info"), _point(c, "system.cpu"), _point(c, "system.ram"),
                _point(c, "system.load"), _point(c, "system.uptime"), _point(c, "disk_space./"),
                _point(c, "system.net"), _get(c, "/api/v1/alarms?active"),
                *temp_q, *[q for pair in cont_q for q in pair],
            )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"No se pudo leer Netdata en {NETDATA_URL} ({type(e).__name__})"}

    temps_raw, cont_raw = rest[:len(temp_q)], rest[len(temp_q):]
    issues: list[str] = []

    cpu_pct = round(sum(cpu.values()), 1)
    ram_total = sum(ram.values()) or 1.0
    ram_used = ram.get("used", 0.0)
    ram_pct = round(100 * ram_used / ram_total, 1)
    d_used, d_avail = disk.get("used", 0.0), disk.get("avail", 0.0)
    disk_pct = round(100 * d_used / ((d_used + d_avail) or 1.0), 1)
    cores = int(info.get("cores_total") or 0)

    sensors = []
    for chart, vals in zip(ids["temps"], temps_raw):
        lab = _sensor_label(chart)
        if not lab or "input" not in vals:
            continue
        kind, name = lab
        warn, bad = _TEMP_LIMITS[kind]
        t = round(vals["input"], 1)
        lvl = _level(t, warn, bad)
        sensors.append({"name": name, "kind": kind, "c": t, "level": lvl})
        if lvl != "ok":
            issues.append(f"{name} a {t} °C")

    containers = []
    for i, name in enumerate(ids["containers"]):
        cp, mem = cont_raw[2 * i], cont_raw[2 * i + 1]
        containers.append({"name": name.removeprefix("camway-"),
                           "cpu": round(sum(cp.values()), 1), "mem_mb": round(mem.get("ram", 0.0))})

    checks = [("CPU", cpu_pct, 85, 95), ("Memoria", ram_pct, 85, 93), ("Disco", disk_pct, 85, 93)]
    levels = {"cpu": _level(cpu_pct, 85, 95), "ram": _level(ram_pct, 85, 93), "disk": _level(disk_pct, 85, 93)}
    for label, val, warn, bad in checks:
        if _level(val, warn, bad) != "ok":
            issues.append(f"{label} al {val:.0f} %")

    # Alarmas propias de Netdata, sin el ruido de sistemas de archivos temporales
    noisy = ("/run", "/tmp", "/dev", "/sys", "/snap", "/boot")
    for a in (alarms.get("alarms") or {}).values():
        ch = a.get("chart", "")
        if ch.startswith(("disk_space.", "disk_inodes.")) and ch.split(".", 1)[1].startswith(noisy):
            continue
        issues.append(f"Netdata: {a.get('name', '?')} ({ch})")

    status = "ok"
    if issues:
        status = "bad" if (any(v == "bad" for v in levels.values()) or any(s["level"] == "bad" for s in sensors)) else "warn"

    return {
        "ok": True,
        "status": status,
        "issues": issues,
        "cpu": {"pct": cpu_pct, "cores": cores, "level": levels["cpu"],
                "load": [round(load.get(k, 0.0), 2) for k in ("load1", "load5", "load15")]},
        "ram": {"pct": ram_pct, "used_gb": round(ram_used / 1024, 1), "total_gb": round(ram_total / 1024, 1),
                "level": levels["ram"]},
        "disk": {"pct": disk_pct, "used_gb": round(d_used), "total_gb": round(d_used + d_avail),
                 "free_gb": round(d_avail), "level": levels["disk"]},
        "net": {"down_mbps": round(abs(net.get("received", 0.0)) / 1000, 2),
                "up_mbps": round(abs(net.get("sent", 0.0)) / 1000, 2)},
        "uptime_s": int(up.get("uptime", 0)),
        "sensors": sensors,
        "containers": containers,
        "url": NETDATA_URL,
    }
