"""Avisos de alarma FUERA de la pantalla: foto del momento por WhatsApp
(Evolution API) y/o correo.

La alarma "de pantalla" la decide cada navegador (zona/horario/sonido por
usuario). Esto es distinto: una regla DEL SERVIDOR por cámara, que funciona
aunque nadie tenga el DVR abierto.

Flujo:
  detector ──cajas + JPEG anotado──▶ feed() ──(N detecciones seguidas,
  fuera del enfriamiento)──▶ cola en disco ──▶ worker ──▶ WhatsApp / correo

* La cola vive en disco (CAMWAY_ALERT_DIR): si Starlink se cae, el aviso espera
  y sale al volver el enlace (con la hora ORIGINAL del evento en el texto).
* Cada canal lleva su propia marca de "enviado": si el correo sale pero
  WhatsApp falla, se reintenta solo WhatsApp (no se duplica el correo).
* Los secretos (clave de Evolution, SMTP) van por variables de entorno, no por
  la base ni por la interfaz.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging
import os
import smtplib
import ssl
import time
from collections import deque
from email.message import EmailMessage
from typing import Optional

import httpx

log = logging.getLogger("camway.notifier")

# --- canales (entorno) -------------------------------------------------------
EVO_URL = os.environ.get("CAMWAY_EVO_URL", "").rstrip("/")
EVO_KEY = os.environ.get("CAMWAY_EVO_KEY", "")
EVO_INSTANCE = os.environ.get("CAMWAY_EVO_INSTANCE", "")
EVO_GROUP = os.environ.get("CAMWAY_ALERT_GROUP", "")         # JID: 1203...@g.us
EVO_API = os.environ.get("CAMWAY_EVO_API", "auto")           # v1 | v2 | auto (prueba v1 y cae a v2)
SMTP_HOST = os.environ.get("CAMWAY_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("CAMWAY_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("CAMWAY_SMTP_USER", "")
SMTP_PASS = os.environ.get("CAMWAY_SMTP_PASS", "")
SMTP_FROM = os.environ.get("CAMWAY_SMTP_FROM", "") or SMTP_USER
SMTP_TLS = os.environ.get("CAMWAY_SMTP_TLS", "starttls")      # starttls | ssl | none
ALERT_EMAILS = [e.strip() for e in os.environ.get("CAMWAY_ALERT_EMAILS", "").split(",") if e.strip()]
SITE_NAME = os.environ.get("CAMWAY_SITE_NAME", "Taladro")

ALERT_DIR = os.environ.get("CAMWAY_ALERT_DIR", "/data/alerts")
MAX_AGE_S = float(os.environ.get("CAMWAY_ALERT_MAX_AGE_S", str(6 * 3600)))   # más viejo: se descarta
RETRY_S = float(os.environ.get("CAMWAY_ALERT_RETRY_S", "30"))
# Tras confirmar la alarma se espera un poco más para reunir cuadros candidatos y
# mandar el MÁS NÍTIDO (no el primero ni el último, que suelen salir movidos).
COLLECT_S = float(os.environ.get("CAMWAY_ALERT_COLLECT_S", "1.0"))
CAND_KEEP_S = float(os.environ.get("CAMWAY_ALERT_CAND_KEEP_S", "8"))

# clase del detector -> texto del aviso
LABELS = {"head": "Persona sin casco", "fuego": "Fuego", "humo": "Humo"}
ICONS = {"head": "⛑️", "fuego": "🔥", "humo": "💨"}
ALERT_CLASSES = tuple(LABELS)

DEFAULT_RULE = {
    "enabled": False,
    "classes": ["head"],        # qué avisar
    "min_score": 0.5,           # confianza mínima
    "min_hits": 3,              # detecciones seguidas (~1,5 s a 2 fps)
    "cooldown": 300,            # s entre avisos de la misma cámara+clase
}


DEFAULT_CC = os.environ.get("CAMWAY_DEFAULT_CC", "58")       # país por defecto (Venezuela)


def evo_ready() -> bool:
    return bool(EVO_URL and EVO_KEY and EVO_INSTANCE)


def normalize_phone(raw: str) -> str:
    """Deja solo dígitos con código de país. Acepta +58 414-1234567, 0414 1234567,
    4141234567… Devuelve "" si no parece un celular válido (10–15 dígitos)."""
    d = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if d.startswith("00"):
        d = d[2:]
    elif d.startswith("0") and len(d) == 11:          # formato local: 0414xxxxxxx
        d = DEFAULT_CC + d[1:]
    elif len(d) == 10:                                # sin 0 ni país: 414xxxxxxx
        d = DEFAULT_CC + d
    return d if 10 <= len(d) <= 15 else ""


def email_ready() -> bool:
    return bool(SMTP_HOST and SMTP_FROM and ALERT_EMAILS)


def channels(n_recipients: int = 0) -> dict:
    return {"whatsapp": evo_ready() and bool(n_recipients or EVO_GROUP),
            "email": email_ready(), "group": bool(EVO_GROUP)}


class Notifier:
    def __init__(self, store, cam_name):
        self.store = store
        self.cam_name = cam_name              # callable(stream_id) -> str
        self._hits: dict[tuple, tuple] = {}   # (sid, cls) -> (n, last_ts)
        self._last_sent: dict[tuple, float] = {}
        self._cands: dict[str, deque] = {}    # sid -> candidatos (t, nitidez, score, cls, jpeg)
        self.log: deque = deque(maxlen=60)    # historial en memoria para la UI
        self._wake = asyncio.Event()
        os.makedirs(ALERT_DIR, exist_ok=True)

    def site(self) -> str:
        """Nombre del taladro/sitio: el que se pone en Ajustes (base) o, si no hay,
        la variable SITE_NAME del .env."""
        return (self.store.get("site_name") or "").strip() or SITE_NAME

    # ---------------- reglas por cámara ----------------

    def rules(self) -> dict:
        return (self.store.get("alerts") or {}).get("cameras", {})

    def rule(self, sid: str) -> dict:
        r = dict(DEFAULT_RULE)
        r.update(self.rules().get(sid, {}))
        return r

    def set_rule(self, sid: str, patch: dict) -> dict:
        cur = self.rule(sid)
        if "enabled" in patch:
            cur["enabled"] = bool(patch["enabled"])
        if "classes" in patch:
            cur["classes"] = [c for c in patch["classes"] if c in ALERT_CLASSES] or ["head"]
        if "min_score" in patch:
            cur["min_score"] = min(0.95, max(0.2, float(patch["min_score"])))
        if "min_hits" in patch:
            cur["min_hits"] = min(10, max(1, int(patch["min_hits"])))
        if "cooldown" in patch:
            cur["cooldown"] = min(3600, max(30, int(patch["cooldown"])))
        data = self.store.get("alerts") or {}
        data.setdefault("cameras", {})[sid] = cur
        self.store.set("alerts", data)
        return cur

    # ---------------- dispositivo que ENVÍA (vincular / desvincular) ----------------

    _wa_cache: tuple = (0.0, None)

    async def _evo(self, method: str, path: str, timeout: float = 25, **kw):
        async with httpx.AsyncClient(timeout=timeout) as c:
            return await c.request(method, EVO_URL + path, headers={"apikey": EVO_KEY}, **kw)

    async def wa_status(self, fresh: bool = False) -> dict:
        """Estado del dispositivo vinculado: unconfigured | offline (Evolution
        caído) | missing (sin instancia) | close/connecting | open. Con caché
        de 8 s para no martillar a Evolution desde la interfaz."""
        ts, val = self._wa_cache
        if not fresh and val is not None and time.time() - ts < 8:
            return val
        if not evo_ready():
            out = {"configured": False, "state": "unconfigured"}
        else:
            out = {"configured": True, "state": "offline"}
            try:
                r = await self._evo("GET", f"/instance/connectionState/{EVO_INSTANCE}", timeout=10)
                if r.status_code == 404:
                    out["state"] = "missing"
                elif r.status_code < 300:
                    j = r.json()
                    out["state"] = (j.get("instance") or {}).get("state") or j.get("state") or "unknown"
                if out["state"] == "open":
                    r = await self._evo("GET", f"/instance/fetchInstances?instanceName={EVO_INSTANCE}", timeout=10)
                    rows = r.json() if r.status_code < 300 else []
                    row = (rows[0] if isinstance(rows, list) and rows else rows) or {}
                    row = row.get("instance", row) if isinstance(row, dict) else {}
                    owner = row.get("ownerJid") or row.get("owner") or row.get("number") or ""
                    out["number"] = "".join(ch for ch in str(owner).split("@")[0] if ch.isdigit())
                    out["name"] = row.get("profileName") or ""
            except Exception:  # noqa: BLE001
                out["state"] = "offline"
        self._wa_cache = (time.time(), out)
        return out

    @staticmethod
    def _qr_of(d: dict) -> str:
        q = d.get("base64") or (d.get("qrcode") or {}).get("base64") or ""
        if q and not q.startswith("data:"):
            q = "data:image/png;base64," + q
        return q

    async def wa_link(self, phone: str = "") -> dict:
        """Prepara la vinculación de un dispositivo NUEVO: crea la instancia si no
        existe y devuelve el QR (o el código de 8 caracteres si se da un número)."""
        if not evo_ready():
            raise RuntimeError("WhatsApp no está configurado en el servidor")
        st = await self.wa_status(fresh=True)
        if st["state"] == "offline":
            raise RuntimeError("Evolution no responde (¿contenedor caído?)")
        if st["state"] == "open":
            return {"state": "open", "qr": "", "pairing_code": ""}
        if st["state"] == "missing":
            r = await self._evo("POST", "/instance/create", json={
                "instanceName": EVO_INSTANCE, "qrcode": True, "integration": "WHATSAPP-BAILEYS"})
            if r.status_code >= 300:
                raise RuntimeError(f"no se pudo crear la instancia: {r.text[:160]}")
            await asyncio.sleep(2)
        elif st["state"] == "close":
            # tras desvincular, Evolution necesita reiniciar la instancia para
            # volver a emitir un QR
            await self._evo("PUT", f"/instance/restart/{EVO_INSTANCE}")
            await asyncio.sleep(3)
        path = f"/instance/connect/{EVO_INSTANCE}"
        if phone:
            path += f"?number={normalize_phone(phone)}"
        r = await self._evo("GET", path, timeout=40)
        if r.status_code >= 300:
            raise RuntimeError(f"Evolution {r.status_code}: {r.text[:160]}")
        d = r.json()
        self._wa_cache = (0.0, None)
        return {"state": "connecting", "qr": self._qr_of(d),
                "pairing_code": d.get("pairingCode") or ""}

    async def wa_unlink(self) -> dict:
        """Cierra la sesión del dispositivo actual (queda sin enviar hasta vincular
        otro). Los destinatarios y reglas NO se tocan; los avisos pendientes
        esperan en cola hasta que haya un dispositivo vinculado."""
        if not evo_ready():
            raise RuntimeError("WhatsApp no está configurado en el servidor")
        r = await self._evo("DELETE", f"/instance/logout/{EVO_INSTANCE}")
        self._wa_cache = (0.0, None)
        if r.status_code >= 300 and r.status_code != 404:
            raise RuntimeError(f"Evolution {r.status_code}: {r.text[:160]}")
        return await self.wa_status(fresh=True)

    # ---------------- destinatarios (números de WhatsApp) ----------------

    def recipients(self) -> list:
        return list(self.store.get("alert_recipients") or [])

    def _save_recipients(self, rows: list) -> None:
        self.store.set("alert_recipients", rows)

    def add_recipient(self, name: str, phone: str) -> dict:
        ph = normalize_phone(phone)
        if not ph:
            raise ValueError("número inválido: usa código de país (ej. 58 414 1234567)")
        rows = self.recipients()
        if any(r["phone"] == ph for r in rows):
            raise ValueError("ese número ya está en la lista")
        rec = {"id": f"r{int(time.time() * 1000) % 10**9}", "name": (name or "").strip()[:40] or ph,
               "phone": ph, "enabled": True, "created": time.time()}
        rows.append(rec)
        self._save_recipients(rows)
        return rec

    def update_recipient(self, rid: str, patch: dict) -> dict:
        rows = self.recipients()
        for r in rows:
            if r["id"] == rid:
                if "enabled" in patch:
                    r["enabled"] = bool(patch["enabled"])
                if "name" in patch:
                    r["name"] = str(patch["name"]).strip()[:40] or r["phone"]
                self._save_recipients(rows)
                return r
        raise KeyError(rid)

    def delete_recipient(self, rid: str) -> None:
        self._save_recipients([r for r in self.recipients() if r["id"] != rid])

    async def check_whatsapp(self, phone: str):
        """True/False si el número tiene WhatsApp; None si no se pudo comprobar
        (Evolution caído o sin vincular): el alta no se bloquea por eso."""
        if not evo_ready():
            return None
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(f"{EVO_URL}/chat/whatsappNumbers/{EVO_INSTANCE}",
                                 headers={"apikey": EVO_KEY}, json={"numbers": [phone]})
            if r.status_code >= 300:
                return None
            data = r.json()
            if isinstance(data, list) and data:
                return bool(data[0].get("exists"))
        except Exception:  # noqa: BLE001
            return None
        return None

    async def send_text(self, phone: str, text: str) -> None:
        url = f"{EVO_URL}/message/sendText/{EVO_INSTANCE}"
        hdr = {"apikey": EVO_KEY, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=30) as c:
            if EVO_API == "v1":
                r = await c.post(url, headers=hdr, json={"number": phone, "textMessage": {"text": text}})
            else:
                r = await c.post(url, headers=hdr, json={"number": phone, "text": text})
                if r.status_code == 400 and EVO_API == "auto":
                    r = await c.post(url, headers=hdr, json={"number": phone, "textMessage": {"text": text}})
        if r.status_code >= 300:
            raise RuntimeError(f"WhatsApp {r.status_code}: {r.text[:200]}")

    def targets_for_detector(self) -> dict[str, set]:
        """stream_id -> targets del detector que hacen falta para esas reglas
        (así el aviso funciona sin que ningún usuario active la IA a mano)."""
        out: dict[str, set] = {}
        for sid, r in self.rules().items():
            if not r.get("enabled"):
                continue
            t = out.setdefault(sid, set())
            if "head" in r.get("classes", []):
                t.add("epp")
            if any(c in r.get("classes", []) for c in ("fuego", "humo")):
                t.add("fuego")
        return out

    # ---------------- entrada desde el detector ----------------

    def feed(self, sid: str, ts: float, boxes: list, frame_b64: Optional[str],
             sharp: Optional[float] = None) -> None:
        rule = self.rule(sid)
        if not rule["enabled"]:
            return
        now = time.time()
        best: dict[str, float] = {}
        for b in boxes or []:
            c = b.get("cls")
            if c in rule["classes"] and float(b.get("score", 0)) >= rule["min_score"]:
                best[c] = max(best.get(c, 0.0), float(b["score"]))
        if frame_b64 and best:
            try:
                dq = self._cands.setdefault(sid, deque(maxlen=12))
                top = max(best, key=best.get)
                dq.append((now, float(sharp or 0.0), best[top], top, base64.b64decode(frame_b64)))
            except Exception:  # noqa: BLE001
                pass
        for c in rule["classes"]:
            key = (sid, c)
            n, last = self._hits.get(key, (0, 0.0))
            if c in best:
                n = n + 1 if now - last <= 4.0 else 1
                self._hits[key] = (n, now)
            else:
                # un fallo suelto del modelo no reinicia la cuenta (baja 1)
                self._hits[key] = (max(0, n - 1), last)
                continue
            if n < rule["min_hits"]:
                continue
            if now - self._last_sent.get(key, 0.0) < rule["cooldown"]:
                continue
            self._last_sent[key] = now
            self._hits[key] = (0, now)
            # espera COLLECT_S para juntar más candidatos y elige el más nítido
            asyncio.get_event_loop().call_later(COLLECT_S, self._finalize, sid, c, ts or now)

    def _finalize(self, sid: str, cls: str, ts: float) -> None:
        now = time.time()
        cands = [c for c in self._cands.get(sid, ()) if c[3] == cls and now - c[0] <= CAND_KEEP_S]
        if not cands:
            self._last_sent.pop((sid, cls), None)       # sin foto: reintenta con el siguiente cuadro
            return
        # el más nítido; a igual nitidez, el de mayor confianza
        t, sharp, score, _c, jpeg = max(cands, key=lambda c: (c[1], c[2]))
        log.info("aviso %s/%s: %d candidatos, elegido nitidez %.1f (rango %.1f–%.1f)",
                 sid, cls, len(cands), sharp, min(c[1] for c in cands), max(c[1] for c in cands))
        self._cands[sid].clear()
        self.enqueue(sid, cls, score, t, jpeg)

    # ---------------- cola en disco ----------------

    def enqueue(self, sid: str, cls: str, score: float, ts: float, jpeg: bytes,
                test: bool = False) -> str:
        aid = f"{int(ts * 1000)}_{abs(hash((sid, cls))) % 10**6:06d}"
        meta = {
            "id": aid, "stream_id": sid, "camera": self.cam_name(sid), "cls": cls,
            "score": round(score, 3), "ts": ts, "test": test,
            "created": time.time(), "done": {}, "tries": 0, "error": "",
        }
        with open(os.path.join(ALERT_DIR, aid + ".jpg"), "wb") as fh:
            fh.write(jpeg)
        self._save_meta(meta)
        # `stream_id` permite filtrar el historial por las cámaras de cada usuario.
        self.log.appendleft({"id": aid, "ts": ts, "stream_id": sid,
                             "camera": meta["camera"], "cls": cls,
                             "score": meta["score"], "test": test, "status": "en cola"})
        log.info("aviso en cola %s (%s %s %.0f%%)", aid, meta["camera"], cls, score * 100)
        self._wake.set()
        return aid

    def _meta_path(self, aid: str) -> str:
        return os.path.join(ALERT_DIR, aid + ".json")

    def _save_meta(self, meta: dict) -> None:
        tmp = self._meta_path(meta["id"]) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
        os.replace(tmp, self._meta_path(meta["id"]))

    def pending(self) -> int:
        try:
            return sum(1 for f in os.listdir(ALERT_DIR) if f.endswith(".json"))
        except FileNotFoundError:
            return 0

    def _set_status(self, aid: str, status: str) -> None:
        for e in self.log:
            if e["id"] == aid:
                e["status"] = status

    # ---------------- texto ----------------

    def caption(self, meta: dict) -> str:
        when = dt.datetime.fromtimestamp(meta["ts"]).strftime("%d/%m/%Y %H:%M:%S")
        label = LABELS.get(meta["cls"], meta["cls"])
        icon = ICONS.get(meta["cls"], "⚠️")
        head = "🧪 PRUEBA de aviso" if meta.get("test") else f"{icon} ALERTA — {label}"
        return (f"{head}\n📍 {self.site()} · {meta['camera']}\n🕒 {when}\n"
                f"Confianza {int(meta['score'] * 100)}%\n_CamWay · Pacific Logging_")

    # ---------------- canales ----------------

    async def _send_whatsapp(self, meta: dict, jpeg: bytes, number: str) -> None:
        media = base64.b64encode(jpeg).decode()
        url = f"{EVO_URL}/message/sendMedia/{EVO_INSTANCE}"
        hdr = {"apikey": EVO_KEY, "Content-Type": "application/json"}
        cap = self.caption(meta)
        v1 = {"number": number, "options": {"delay": 800, "presence": "composing"},
              "mediaMessage": {"mediatype": "image", "fileName": "alerta.jpg",
                               "caption": cap, "media": media}}
        v2 = {"number": number, "mediatype": "image", "mimetype": "image/jpeg",
              "fileName": "alerta.jpg", "caption": cap, "media": media}
        async with httpx.AsyncClient(timeout=60) as c:
            if EVO_API == "v2":
                r = await c.post(url, headers=hdr, json=v2)
            else:
                r = await c.post(url, headers=hdr, json=v1)
                if r.status_code == 400 and EVO_API == "auto":   # v2 usa el cuerpo plano
                    r = await c.post(url, headers=hdr, json=v2)
        if r.status_code >= 300:
            raise RuntimeError(f"WhatsApp {r.status_code}: {r.text[:200]}")

    def _smtp_send(self, meta: dict, jpeg: bytes) -> None:
        msg = EmailMessage()
        label = LABELS.get(meta["cls"], meta["cls"])
        msg["Subject"] = ("[PRUEBA] " if meta.get("test") else "ALERTA: ") + \
            f"{label} — {self.site()} · {meta['camera']}"
        msg["From"] = SMTP_FROM
        msg["To"] = ", ".join(ALERT_EMAILS)
        msg.set_content(self.caption(meta).replace("_", ""))
        msg.add_attachment(jpeg, maintype="image", subtype="jpeg", filename="alerta.jpg")
        if SMTP_TLS == "ssl":
            srv = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30,
                                   context=ssl.create_default_context())
        else:
            srv = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        try:
            if SMTP_TLS == "starttls":
                srv.starttls(context=ssl.create_default_context())
            if SMTP_USER:
                srv.login(SMTP_USER, SMTP_PASS)
            srv.send_message(msg)
        finally:
            try:
                srv.quit()
            except Exception:  # noqa: BLE001
                pass

    # ---------------- worker ----------------

    async def run(self) -> None:
        """Vacía la cola; si algo falla (sin internet), reintenta cada RETRY_S."""
        while True:
            try:
                await self._drain()
            except Exception as e:  # noqa: BLE001
                log.info("cola de avisos: %s", e)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=RETRY_S)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _drain(self) -> None:
        try:
            files = sorted(f for f in os.listdir(ALERT_DIR) if f.endswith(".json"))
        except FileNotFoundError:
            return
        for fn in files:
            path = os.path.join(ALERT_DIR, fn)
            try:
                with open(path, encoding="utf-8") as fh:
                    meta = json.load(fh)
            except Exception:  # noqa: BLE001
                os.remove(path)
                continue
            aid = meta["id"]
            jpg_path = os.path.join(ALERT_DIR, aid + ".jpg")
            if time.time() - meta["created"] > MAX_AGE_S:
                self._finish(aid, path, jpg_path, "descartado (muy viejo)")
                continue
            try:
                with open(jpg_path, "rb") as fh:
                    jpeg = fh.read()
            except FileNotFoundError:
                os.remove(path)
                continue
            dests: list = []                      # (clave de entrega, tipo, destino)
            if evo_ready():
                for r in self.recipients():
                    if r.get("enabled", True):
                        dests.append((f"wa:{r['id']}", "whatsapp", r["phone"]))
                if EVO_GROUP:
                    dests.append(("wa:grupo", "whatsapp", EVO_GROUP))
            if email_ready():
                dests.append(("email", "email", ""))
            if not dests:
                self._finish(aid, path, jpg_path, "sin destinatarios/canal configurado")
                continue
            errs = []
            for key, kind, dest in dests:
                if meta["done"].get(key):
                    continue
                try:
                    if kind == "whatsapp":
                        await self._send_whatsapp(meta, jpeg, dest)
                        await asyncio.sleep(1.5)          # sin ráfagas (anti-bloqueo)
                    else:
                        await asyncio.get_event_loop().run_in_executor(None, self._smtp_send, meta, jpeg)
                    meta["done"][key] = time.time()
                    log.info("aviso %s enviado a %s", aid, key)
                except Exception as e:  # noqa: BLE001
                    errs.append(f"{key}: {e}")
            meta["tries"] += 1
            meta["error"] = " | ".join(errs)
            if errs:
                self._save_meta(meta)
                self._set_status(aid, f"reintentando ({meta['tries']}) {meta['error'][:80]}")
            else:
                self._finish(aid, path, jpg_path, f"enviado a {len(meta['done'])} destino(s)")

    def _finish(self, aid: str, path: str, jpg_path: str, status: str) -> None:
        self._set_status(aid, status)
        for p in (path, jpg_path):
            try:
                os.remove(p)
            except OSError:
                pass
