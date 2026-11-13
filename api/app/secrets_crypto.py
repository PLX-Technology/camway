"""Cifrado en reposo de las credenciales de cámaras/NVR guardadas en SQLite.

Antes se guardaban en texto plano: quien tuviera acceso al volumen de datos
(o a una copia del respaldo diario) se llevaba todas las claves de cámaras
del sitio tal cual. La clave de cifrado vive en una variable de entorno, NO
en el volumen de datos — así un respaldo robado, sin esa variable, no sirve
para leer las contraseñas.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

_log = logging.getLogger("camway")

_ENV_VAR = "CAMWAY_SECRET_KEY"
_fernet: Fernet | None = None
_warned = False


def _derive_key(raw: str) -> bytes:
    """Admite tanto una clave Fernet real (urlsafe-base64, 32 bytes) como
    cualquier texto: si no es una clave Fernet válida, deriva una de 32 bytes
    por SHA-256 (más cómodo para el operador que generar una con `Fernet.
    generate_key()` a mano)."""
    raw_b = raw.encode()
    try:
        Fernet(raw_b)
        return raw_b
    except Exception:
        return base64.urlsafe_b64encode(hashlib.sha256(raw_b).digest())


def _get_fernet() -> Fernet:
    global _fernet, _warned
    if _fernet is not None:
        return _fernet
    key = os.environ.get(_ENV_VAR, "").strip()
    if not key:
        # Sin CAMWAY_SECRET_KEY: se genera una y se avisa fuerte. Sirve para
        # que el sistema funcione sin configuración extra, pero si el
        # contenedor se recrea desde cero (no solo se reinicia) sin fijar la
        # variable, las credenciales guardadas quedan ilegibles — por eso el
        # aviso insiste en fijarla en el .env.
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        if not _warned:
            _log.warning(
                "CAMWAY_SECRET_KEY no esta definida: se genero una clave al vuelo "
                "para cifrar las credenciales de camaras. Si el contenedor se "
                "recrea (no solo se reinicia) sin fijar esta variable en el .env, "
                "las credenciales guardadas quedaran ilegibles. Fijarla: "
                "%s=%s", _ENV_VAR, key,
            )
            _warned = True
    _fernet = Fernet(_derive_key(key))
    return _fernet


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return plaintext
    return "enc:" + _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(stored: str) -> str:
    """Si `stored` no es un valor cifrado por esta misma función (p.ej. una
    credencial guardada ANTES de activar el cifrado), se devuelve tal cual:
    compatibilidad con filas viejas sin necesitar una migración aparte."""
    if not stored or not stored.startswith("enc:"):
        return stored
    try:
        return _get_fernet().decrypt(stored[4:].encode()).decode()
    except (InvalidToken, ValueError):
        _log.warning("no se pudo descifrar una credencial guardada (clave distinta?)")
        return stored
