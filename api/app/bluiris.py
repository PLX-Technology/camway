"""Proxy de sesión con Blue Iris.

Blue Iris re-sirve cada cámara por MJPEG (`/mjpg/<short>/video.mjpg`) pero exige
una sesión que caduca; su re-stream RTSP no está expuesto. Este proxy mantiene
una sesión viva (re-login automático cuando expira) y le ofrece a CamWay una URL
local estable `http://api:8000/bi/<short>/video.mjpg` que MediaMTX transcodifica
como cualquier cámara MJPEG. Así las cámaras del NVR entran sin necesitar la
contraseña de cada cámara física (Blue Iris ya las tiene).
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Optional

import httpx


class BlueIris:
    def __init__(self, base: str, user: str, pwd: str):
        self.base = base.rstrip("/")
        self.user = user
        self.pwd = pwd
        self.session: Optional[str] = None
        self._lock = asyncio.Lock()

    async def _do_login(self, client: httpx.AsyncClient) -> str:
        r = (await client.post(f"{self.base}/json", json={"cmd": "login"})).json()
        s = r.get("session")
        resp = hashlib.md5(f"{self.user}:{s}:{self.pwd}".encode()).hexdigest()
        r2 = (await client.post(
            f"{self.base}/json",
            json={"cmd": "login", "session": s, "response": resp},
        )).json()
        if r2.get("result") != "success":
            raise RuntimeError("Blue Iris rechazó el login")
        self.session = s
        return s

    async def get_session(self, client: httpx.AsyncClient, force: bool = False) -> str:
        async with self._lock:
            if force or not self.session:
                await self._do_login(client)
            return self.session

    async def cliplist(self, short: str, start=None, end=None) -> list[dict]:
        """Clips (grabaciones) de una cámara, opcionalmente en un rango unix."""
        async with httpx.AsyncClient(timeout=25) as client:
            s = await self.get_session(client, force=True)
            body = {"cmd": "cliplist", "camera": short, "session": s}
            if start is not None:
                body["startdate"] = int(start)
            if end is not None:
                body["enddate"] = int(end)
            r = (await client.post(f"{self.base}/json", json=body)).json()
            return r.get("data") or []

    async def fetch_clip(self, path: str) -> Optional[bytes]:
        """Descarga el BVR de un clip (re-login si la sesión caducó)."""
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            for attempt in range(2):
                s = await self.get_session(client, force=(attempt == 1))
                r = await client.get(f"{self.base}/clips/{path}?session={s}")
                if r.status_code == 200 and r.content[:4] == b"BLUE":
                    return r.content
        return None

    async def fetch_thumb(self, path: str) -> Optional[bytes]:
        """JPEG póster de un clip (`/file/clips`)."""
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            for attempt in range(2):
                s = await self.get_session(client, force=(attempt == 1))
                r = await client.get(f"{self.base}/file/clips/{path}?session={s}")
                if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                    return r.content
        return None

    async def cameras(self) -> list[dict]:
        """Lista de cámaras (short, name, online, w, h), sin grupos."""
        async with httpx.AsyncClient(timeout=12) as client:
            s = await self.get_session(client, force=True)
            data = (await client.post(
                f"{self.base}/json", json={"cmd": "camlist", "session": s}
            )).json().get("data", [])
        out = []
        for c in data:
            sn = c.get("optionValue", "")
            if not sn or sn.startswith("@") or str(c.get("optionDisplay", "")).startswith("+"):
                continue
            out.append({
                "short": sn, "name": c.get("optionDisplay", sn),
                "online": bool(c.get("isOnline")),
                "ptz": bool(c.get("ptz")),
                "width": c.get("width"), "height": c.get("height"),
            })
        return out
