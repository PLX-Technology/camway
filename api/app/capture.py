"""Miniaturas por ffmpeg, para las tarjetas del inventario.

Un fotograma suelto cuesta un proceso, así que se cachea unos segundos: en
una rejilla de 30 cámaras la diferencia entre cachear y no cachear es entre
un proceso y treinta cada vez que alguien recarga.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

FFMPEG = "ffmpeg"
CACHE_TTL = 10.0

_cache: dict[str, tuple[float, bytes]] = {}
_locks: dict[str, asyncio.Lock] = {}


async def snapshot(
    url: str,
    width: int = 480,
    timeout: float = 12.0,
    cache_key: Optional[str] = None,
) -> Optional[bytes]:
    """Devuelve un JPEG del stream, o None si no se pudo."""
    key = cache_key or url
    now = time.time()

    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        # Otro pedido pudo haberla traído mientras esperábamos el candado.
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]

        args = [
            FFMPEG, "-nostdin", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", url,
            "-frames:v", "1",
            "-vf", f"scale={width}:-2",
            "-q:v", "6",
            "-f", "image2", "-vcodec", "mjpeg", "-update", "1",
            "pipe:1",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return None

        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return None

        if proc.returncode != 0 or not out:
            return None

        _cache[key] = (time.time(), out)
        return out


def invalidate(cache_key: str) -> None:
    _cache.pop(cache_key, None)
