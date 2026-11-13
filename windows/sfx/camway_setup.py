"""Instalador .exe autoextraible de CamWay.

Lleva el paquete (zip) embebido. Al ejecutarse: lo extrae a %TEMP% y lanza
Instalar-CamWay.bat (que se auto-eleva a administrador). Un solo .exe, sin que el
usuario instale nada aparte.
"""
import ctypes
import os
import subprocess
import sys
import tempfile
import zipfile


def _base() -> str:
    # PyInstaller onefile: los datos van en sys._MEIPASS
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _msg(text: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, text, "CamWay", 0x10)
    except Exception:
        print(text)


def main() -> int:
    base = _base()
    pkg = None
    for f in os.listdir(base):
        lf = f.lower()
        if lf.startswith("camway-windows-setup") and lf.endswith(".zip"):
            pkg = os.path.join(base, f)
            break
    if not pkg:
        _msg("No se encontro el paquete de CamWay dentro del instalador.")
        return 1

    dest = os.path.join(tempfile.gettempdir(), "CamWaySetup")
    print("Preparando la instalacion de CamWay, un momento...")
    try:
        import shutil
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest, exist_ok=True)
        with zipfile.ZipFile(pkg) as z:
            z.extractall(dest)
    except Exception as e:  # noqa: BLE001
        _msg(f"No se pudo descomprimir el paquete:\n{e}")
        return 1

    bat = None
    for root, _dirs, files in os.walk(dest):
        if "Instalar-CamWay.bat" in files:
            bat = os.path.join(root, "Instalar-CamWay.bat")
            break
    if not bat:
        _msg("El paquete esta incompleto (falta Instalar-CamWay.bat).")
        return 1

    # El .bat se auto-eleva a administrador (UAC) y corre install.ps1.
    try:
        subprocess.run(["cmd", "/c", bat], cwd=os.path.dirname(bat))
    except Exception as e:  # noqa: BLE001
        _msg(f"No se pudo iniciar la instalacion:\n{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
