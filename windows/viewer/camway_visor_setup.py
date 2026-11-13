"""Instalador del Visor remoto de CamWay.

Copia CamWayVisor.exe a %LOCALAPPDATA%\\CamWayVisor, crea accesos directos
(escritorio + Inicio) y lo deja **arrancando automáticamente al iniciar sesión**.
No necesita permisos de administrador (instalación por usuario).

Un visor con ventana no puede ser un "servicio" de Windows real (los servicios
no tienen escritorio); el arranque al iniciar sesión es el equivalente correcto.
"""
import ctypes
import os
import subprocess
import sys


def _base() -> str:
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _msg(text: str, title: str = "CamWay Visor", flags: int = 0x40) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, flags)
    except Exception:
        print(text)


def main() -> int:
    src = os.path.join(_base(), "CamWayVisor.exe")
    if not os.path.isfile(src):
        _msg("No se encontró CamWayVisor.exe dentro del instalador.", flags=0x10)
        return 1

    appdir = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "CamWayVisor")
    dst = os.path.join(appdir, "CamWayVisor.exe")
    try:
        os.makedirs(appdir, exist_ok=True)
        import shutil
        # Si ya está corriendo, intentar cerrarlo para poder reemplazarlo
        subprocess.run(["taskkill", "/F", "/IM", "CamWayVisor.exe"],
                       creationflags=0x08000000, capture_output=True)
        shutil.copy2(src, dst)
    except Exception as e:  # noqa: BLE001
        _msg(f"No se pudo instalar:\n{e}", flags=0x10)
        return 1

    desktop = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
    startup = os.path.join(os.environ.get("APPDATA", ""),
                           r"Microsoft\Windows\Start Menu\Programs\Startup")

    def _lnk(path: str) -> str:
        p = path.replace("'", "''")
        d = dst.replace("'", "''")
        w = appdir.replace("'", "''")
        return (f"$s=$w.CreateShortcut('{p}');$s.TargetPath='{d}';"
                f"$s.WorkingDirectory='{w}';$s.IconLocation='{d},0';$s.Save();")

    ps = "$w=New-Object -ComObject WScript.Shell;"
    if os.path.isdir(desktop):
        ps += _lnk(os.path.join(desktop, "CamWay Visor.lnk"))
    if os.path.isdir(startup):
        ps += _lnk(os.path.join(startup, "CamWay Visor.lnk"))
    try:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       creationflags=0x08000000, capture_output=True)
    except Exception:  # noqa: BLE001
        pass

    try:
        subprocess.Popen([dst], cwd=appdir)
    except Exception:  # noqa: BLE001
        pass

    _msg("CamWay Visor quedó instalado.\n\n"
         "• Se abrió ahora y arrancará solo cada vez que inicies sesión en esta PC.\n"
         "• La primera vez, escribe la dirección del servidor (IP + puerto + usuario + clave).\n"
         "• Acceso directo creado en el Escritorio.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
