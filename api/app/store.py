"""Persistencia en SQLite: equipos, streams, credenciales y ajustes."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from typing import Any, Optional

DB_PATH = os.environ.get("CAMWAY_DB", "/data/camway.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id          TEXT PRIMARY KEY,
    host        TEXT NOT NULL,
    port        INTEGER NOT NULL,
    vendor      TEXT DEFAULT '',
    server      TEXT DEFAULT '',
    kind        TEXT DEFAULT 'camara',
    name        TEXT DEFAULT '',
    site        TEXT DEFAULT '',
    bi_camera   TEXT DEFAULT '',
    bi_server   TEXT DEFAULT '',
    needs_auth  INTEGER DEFAULT 0,
    permissive  INTEGER DEFAULT 0,
    error       TEXT DEFAULT '',
    first_seen  REAL,
    last_seen   REAL
);

CREATE TABLE IF NOT EXISTS streams (
    id          TEXT PRIMARY KEY,
    device_id   TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    url         TEXT NOT NULL,
    codec       TEXT DEFAULT '',
    width       INTEGER,
    height      INTEGER,
    profile     TEXT DEFAULT '',
    audio_codec TEXT DEFAULT '',
    label       TEXT DEFAULT '',
    published   INTEGER DEFAULT 0,
    mtx_name    TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS credentials (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    password TEXT NOT NULL,
    label    TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS ai_config (
    user_id    INTEGER NOT NULL DEFAULT 0,
    stream_id  TEXT NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    enabled    INTEGER DEFAULT 0,
    target     TEXT DEFAULT 'vehiculo',
    min_score  REAL DEFAULT 0.4,
    action     TEXT DEFAULT 'full',
    zone       TEXT,
    schedule   TEXT,
    PRIMARY KEY (user_id, stream_id)
);

CREATE INDEX IF NOT EXISTS idx_streams_device ON streams(device_id);

CREATE TABLE IF NOT EXISTS users (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    username  TEXT UNIQUE NOT NULL,
    pwd_hash  TEXT NOT NULL,
    role      TEXT NOT NULL DEFAULT 'viewer',
    created   REAL
);

CREATE TABLE IF NOT EXISTS user_cameras (
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id TEXT NOT NULL,
    PRIMARY KEY (user_id, device_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    token    TEXT PRIMARY KEY,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created  REAL
);
"""


# --------------------------------------------------------------------------
# Contraseñas: PBKDF2-HMAC-SHA256 (stdlib, sin dependencias)
# --------------------------------------------------------------------------

_PBKDF2_ROUNDS = 200_000


def hash_password(pwd: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(pwd: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pwd.encode(),
                                 bytes.fromhex(salt_hex), int(rounds))
        return secrets.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


class Store:
    def __init__(self, path: str = DB_PATH):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # Resiliencia ante cortes de luz (taladros): WAL protege contra
        # CORRUPCIÓN incluso con synchronous=NORMAL (lo documenta el propio
        # SQLite) — lo único que se arriesga con NORMAL es perder las últimas
        # transacciones si el corte de luz cae justo entre el commit y el
        # siguiente checkpoint, nunca una base dañada; eso ya lo cubre el
        # respaldo diario (_db_backup_loop). A cambio, cada commit deja de
        # forzar un fsync completo — con FULL, cada `store.upsert_device()`
        # durante un escaneo (uno por equipo encontrado, pueden ser decenas en
        # ráfaga) bloqueaba el event loop entero mientras el disco confirmaba
        # la escritura; en un disco lento/industrial eso congelaba el video en
        # vivo de TODAS las cámaras, no solo esa escritura.
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self._migrate_encrypt_credentials()
        self.conn.commit()

    def backup(self, dest_path: str) -> None:
        """Copia en caliente y consistente de la base (API backup de SQLite).
        Sirve para tener una instantánea reciente si un apagón corrompe la viva."""
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        tmp = dest_path + ".tmp"
        with self._lock:
            dst = sqlite3.connect(tmp)
            try:
                self.conn.backup(dst)
                dst.commit()
            finally:
                dst.close()
        os.replace(tmp, dest_path)   # atómico: el destino nunca queda a medias

    def _migrate(self) -> None:
        # ai_config pasó de global (PK stream_id) a por-usuario (PK user_id,
        # stream_id). Si la tabla vieja no tiene user_id, se recrea.
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(ai_config)")]
        if cols and "user_id" not in cols:
            self.conn.execute("DROP TABLE ai_config")
            self.conn.executescript(_SCHEMA)
        dcols = [r[1] for r in self.conn.execute("PRAGMA table_info(devices)")]
        if dcols and "bi_camera" not in dcols:
            self.conn.execute("ALTER TABLE devices ADD COLUMN bi_camera TEXT DEFAULT ''")
        if dcols and "bi_server" not in dcols:
            # Qué Blue Iris sirve las grabaciones de esta cámara (vacío = el
            # primario). Permite un segundo NVR (ver [[camway-blueiris]]).
            self.conn.execute("ALTER TABLE devices ADD COLUMN bi_server TEXT DEFAULT ''")

    def _migrate_encrypt_credentials(self) -> None:
        """Cifra en el sitio las credenciales guardadas en texto plano antes
        de activar este cifrado (filas sin el prefijo "enc:"). Una sola vez:
        las filas ya cifradas se saltan de inmediato."""
        from . import secrets_crypto
        rows = self.conn.execute("SELECT id, password FROM credentials").fetchall()
        for r in rows:
            if r["password"] and not r["password"].startswith("enc:"):
                self.conn.execute(
                    "UPDATE credentials SET password = ? WHERE id = ?",
                    (secrets_crypto.encrypt(r["password"]), r["id"]),
                )

    # ---------------- equipos y streams ----------------

    def upsert_device(self, dev) -> None:
        """Guarda un Device del escáner junto con sus streams.

        Conserva el nombre y el sitio que haya puesto el usuario, y el estado
        'published' de los streams que ya existían.
        """
        now = time.time()
        with self._lock:
            cur = self.conn.cursor()
            prev = cur.execute(
                "SELECT name, site, first_seen FROM devices WHERE id = ?", (dev.id,)
            ).fetchone()

            cur.execute(
                """INSERT INTO devices
                   (id, host, port, vendor, server, kind, name, site,
                    needs_auth, permissive, error, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     host=excluded.host, port=excluded.port,
                     vendor=excluded.vendor, server=excluded.server,
                     kind=excluded.kind, needs_auth=excluded.needs_auth,
                     permissive=excluded.permissive, error=excluded.error,
                     last_seen=excluded.last_seen""",
                (dev.id, dev.host, dev.port, dev.vendor, dev.server, dev.kind,
                 prev["name"] if prev else "", prev["site"] if prev else "",
                 int(dev.needs_auth), int(dev.permissive), dev.error,
                 prev["first_seen"] if prev else now, now),
            )

            # Si el escaneo NO trae streams (p.ej. la cámara solo respondió
            # "pide clave"), se CONSERVAN los streams existentes. Antes se
            # borraban y una cámara de alta manual (IMOU, GoAhead 109) o una
            # ya conocida que momentáneamente pidió auth desaparecía del muro.
            if not dev.streams:
                self.conn.commit()
                return

            published = {
                r["path"]: (r["published"], r["mtx_name"])
                for r in cur.execute(
                    "SELECT path, published, mtx_name FROM streams WHERE device_id = ?",
                    (dev.id,),
                )
            }
            cur.execute("DELETE FROM streams WHERE device_id = ?", (dev.id,))

            for idx, s in enumerate(dev.streams):
                sid = f"{dev.id}__{idx}"
                was_pub, mtx_name = published.get(s.path, (0, ""))
                cur.execute(
                    """INSERT INTO streams
                       (id, device_id, path, url, codec, width, height, profile,
                        audio_codec, label, published, mtx_name)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (sid, dev.id, s.path, s.url, s.codec, s.width, s.height,
                     s.profile, s.audio_codec, s.label, was_pub,
                     mtx_name or sid),
                )
            self.conn.commit()

    def devices(self, site: Optional[str] = None) -> list[dict]:
        q = "SELECT * FROM devices"
        args: tuple = ()
        if site:
            q += " WHERE site = ?"
            args = (site,)
        q += " ORDER BY host, port"
        with self._lock:
            rows = self.conn.execute(q, args).fetchall()
            out = []
            for r in rows:
                dev = dict(r)
                dev["streams"] = [
                    dict(s) for s in self.conn.execute(
                        "SELECT * FROM streams WHERE device_id = ? ORDER BY id",
                        (r["id"],),
                    )
                ]
                out.append(dev)
        return out

    def device(self, device_id: str) -> Optional[dict]:
        return next((d for d in self.devices() if d["id"] == device_id), None)

    def stream(self, stream_id: str) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM streams WHERE id = ?", (stream_id,)
            ).fetchone()
        return dict(row) if row else None

    def stream_by_mtx(self, mtx_name: str) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM streams WHERE mtx_name = ?", (mtx_name,)).fetchone()
        return dict(row) if row else None

    def streams(self, published_only: bool = False) -> list[dict]:
        q = "SELECT * FROM streams"
        if published_only:
            q += " WHERE published = 1"
        with self._lock:
            return [dict(r) for r in self.conn.execute(q).fetchall()]

    def set_published(self, stream_id: str, published: bool) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE streams SET published = ? WHERE id = ?",
                (int(published), stream_id),
            )
            self.conn.commit()

    def rename_device(self, device_id: str, name: str, site: str = "") -> None:
        with self._lock:
            if site:
                self.conn.execute(
                    "UPDATE devices SET name = ?, site = ? WHERE id = ?",
                    (name, site, device_id),
                )
            else:
                self.conn.execute(
                    "UPDATE devices SET name = ? WHERE id = ?", (name, device_id)
                )
            self.conn.commit()

    def set_bi_camera(self, device_id: str, bi_camera: str,
                      bi_server: Optional[str] = None) -> None:
        with self._lock:
            if bi_server is None:
                self.conn.execute("UPDATE devices SET bi_camera = ? WHERE id = ?",
                                  (bi_camera, device_id))
            else:
                self.conn.execute(
                    "UPDATE devices SET bi_camera = ?, bi_server = ? WHERE id = ?",
                    (bi_camera, bi_server, device_id))
            self.conn.commit()

    def forget_device(self, device_id: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
            self.conn.commit()

    # ---------------- credenciales ----------------

    def credentials(self) -> list[dict]:
        from . import secrets_crypto
        with self._lock:
            rows = [dict(r) for r in self.conn.execute(
                "SELECT * FROM credentials ORDER BY id"
            ).fetchall()]
        for r in rows:
            r["password"] = secrets_crypto.decrypt(r["password"])
        return rows

    def credential_pairs(self) -> list[tuple[str, str]]:
        """Juegos a probar; siempre incluye el anónimo al principio."""
        pairs = [("", "")]
        pairs += [(c["username"], c["password"]) for c in self.credentials()]
        return pairs

    def add_credential(self, username: str, password: str, label: str = "") -> int:
        from . import secrets_crypto
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO credentials (username, password, label) VALUES (?,?,?)",
                (username, secrets_crypto.encrypt(password), label),
            )
            self.conn.commit()
            return cur.lastrowid

    def delete_credential(self, cred_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM credentials WHERE id = ?", (cred_id,))
            self.conn.commit()

    # ---------------- usuarios, sesiones y permisos ----------------

    def user_count(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def create_user(self, username: str, password: str, role: str = "viewer",
                    cameras: Optional[list[str]] = None) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO users (username, pwd_hash, role, created) VALUES (?,?,?,?)",
                (username, hash_password(password),
                 role if role in ("admin", "seguridad") else "viewer", time.time()),
            )
            uid = cur.lastrowid
            for dev in (cameras or []):
                self.conn.execute(
                    "INSERT OR IGNORE INTO user_cameras (user_id, device_id) VALUES (?,?)",
                    (uid, dev),
                )
            self.conn.commit()
            return uid

    def update_user(self, user_id: int, password: Optional[str] = None,
                    role: Optional[str] = None,
                    cameras: Optional[list[str]] = None) -> None:
        with self._lock:
            if password:
                self.conn.execute("UPDATE users SET pwd_hash = ? WHERE id = ?",
                                  (hash_password(password), user_id))
            if role in ("admin", "viewer", "seguridad"):
                self.conn.execute("UPDATE users SET role = ? WHERE id = ?",
                                  (role, user_id))
            if cameras is not None:
                self.conn.execute("DELETE FROM user_cameras WHERE user_id = ?", (user_id,))
                for dev in cameras:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO user_cameras (user_id, device_id) VALUES (?,?)",
                        (user_id, dev),
                    )
            self.conn.commit()

    def delete_user(self, user_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            self.conn.commit()

    def _user_cameras(self, cur, user_id: int) -> list[str]:
        return [r["device_id"] for r in cur.execute(
            "SELECT device_id FROM user_cameras WHERE user_id = ?", (user_id,))]

    def get_user(self, user_id: int) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                return None
            u = dict(row)
            u["cameras"] = self._user_cameras(self.conn, user_id)
        return u

    def get_user_by_name(self, username: str) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if not row:
                return None
            u = dict(row)
            u["cameras"] = self._user_cameras(self.conn, u["id"])
        return u

    def list_users(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, username, role, created FROM users ORDER BY username").fetchall()
            out = []
            for r in rows:
                u = dict(r)
                u["cameras"] = self._user_cameras(self.conn, r["id"])
                out.append(u)
        return out

    def allowed_cameras(self, user_id: int) -> set[str]:
        with self._lock:
            return {r["device_id"] for r in self.conn.execute(
                "SELECT device_id FROM user_cameras WHERE user_id = ?", (user_id,))}

    # --- sesiones ---

    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self.conn.execute(
                "INSERT INTO sessions (token, user_id, created) VALUES (?,?,?)",
                (token, user_id, time.time()))
            self.conn.commit()
        return token

    def session_user(self, token: str) -> Optional[dict]:
        if not token:
            return None
        with self._lock:
            row = self.conn.execute(
                "SELECT user_id FROM sessions WHERE token = ?", (token,)).fetchone()
        return self.get_user(row["user_id"]) if row else None

    def delete_session(self, token: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            self.conn.commit()

    def delete_user_sessions(self, user_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            self.conn.commit()

    # ---------------- config IA (para el detector server-side) ----------------

    def get_ai(self, user_id: int, stream_id: str) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM ai_config WHERE user_id = ? AND stream_id = ?",
                (user_id, stream_id)).fetchone()
        if not row:
            return None
        cfg = dict(row)
        cfg["enabled"] = bool(cfg["enabled"])
        cfg["zone"] = json.loads(cfg["zone"]) if cfg["zone"] else None
        cfg["schedule"] = json.loads(cfg["schedule"]) if cfg["schedule"] else None
        return cfg

    def set_ai(self, user_id: int, stream_id: str, cfg: dict) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO ai_config
                   (user_id, stream_id, enabled, target, min_score, action, zone, schedule)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id, stream_id) DO UPDATE SET
                     enabled=excluded.enabled, target=excluded.target,
                     min_score=excluded.min_score, action=excluded.action,
                     zone=excluded.zone, schedule=excluded.schedule""",
                (user_id, stream_id, int(bool(cfg.get("enabled"))),
                 cfg.get("target", "vehiculo"), float(cfg.get("min_score", 0.4)),
                 cfg.get("action", "full"),
                 json.dumps(cfg.get("zone")) if cfg.get("zone") else None,
                 json.dumps(cfg.get("schedule")) if cfg.get("schedule") else None),
            )
            self.conn.commit()

    def ai_configs_for(self, user_id: int) -> dict:
        """Todas las configuraciones IA de un usuario: stream_id -> cfg."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM ai_config WHERE user_id = ?", (user_id,)).fetchall()
        out = {}
        for r in rows:
            cfg = dict(r)
            cfg["enabled"] = bool(cfg["enabled"])
            cfg["zone"] = json.loads(cfg["zone"]) if cfg["zone"] else None
            cfg["schedule"] = json.loads(cfg["schedule"]) if cfg["schedule"] else None
            out[cfg["stream_id"]] = cfg
        return out

    def enabled_ai(self) -> list[dict]:
        """Cámaras con IA activada por CUALQUIER usuario (unión), con su URL y
        el conjunto de targets pedidos (para que el detector sepa si debe correr
        el modelo de fuego). Una sola corrida por cámara; emite cajas crudas."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT a.stream_id, s.url,
                          GROUP_CONCAT(DISTINCT a.target) AS targets
                   FROM ai_config a JOIN streams s ON s.id = a.stream_id
                   WHERE a.enabled = 1
                   GROUP BY a.stream_id, s.url"""
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["targets"] = (d.get("targets") or "").split(",") if d.get("targets") else []
            out.append(d)
        return out

    # ---------------- ajustes ----------------

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return default

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO settings (key, value) VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, json.dumps(value)),
            )
            self.conn.commit()
