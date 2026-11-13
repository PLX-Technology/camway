"""Actualizador CamWay (taladro) — un solo clic, sin escribir comandos.

Lleva el camway-server.exe nuevo embebido y aplica el arreglo completo:
  - reemplaza el servidor (deja respaldo .bak),
  - ajusta C:\\CamWay\\config\\camway.env: quita CAMWAY_WEB_DIR (usa la web
    embebida con el live.js anti-churn) y activa CAMWAY_NO_AUDIO=1 y
    CAMWAY_WALL_NO_DIRECT=1,
  - reinicia el servicio "CamWay Supervisor".

Se compila con --uac-admin: al abrirlo pide permiso de administrador.
"""
import ctypes
import os
import shutil
import subprocess
import sys
import time

ROOT = r"C:\CamWay"
TASK = "CamWay Supervisor"
ENVKEYS = ("CAMWAY_WEB_DIR", "CAMWAY_NO_AUDIO", "CAMWAY_WALL_NO_DIRECT")
ADD = ["CAMWAY_NO_AUDIO=1", "CAMWAY_WALL_NO_DIRECT=1"]


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run(args):
    try:
        return subprocess.run(args, capture_output=True, text=True)
    except Exception as e:  # noqa: BLE001
        print("   (aviso)", e)
        return None


def main() -> int:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.argv[0])))
    newexe = os.path.join(base, "camway-server.exe")
    tgt = os.path.join(ROOT, "camway-server.exe")
    envf = os.path.join(ROOT, "config", "camway.env")

    print("=" * 60)
    print(" CamWay · Arreglo del taladro")
    print(" (audio fuera + wallcache sin directo + live.js anti-churn)")
    print("=" * 60)

    if not os.path.isdir(ROOT):
        print(f"\nERROR: no existe {ROOT}. Ejecuta esto en el PC del servidor CamWay.")
        return 1
    if not os.path.exists(newexe):
        print("\nERROR: no encuentro el camway-server.exe embebido en el paquete.")
        return 1

    print("\n[1/4] Deteniendo el servicio...")
    run(["schtasks", "/End", "/TN", TASK])
    run(["taskkill", "/F", "/IM", "camway-server.exe"])
    run(["taskkill", "/F", "/IM", "ffmpeg.exe"])
    time.sleep(3)

    print("[2/4] Reemplazando camway-server.exe (respaldo en camway-server.exe.bak)...")
    try:
        if os.path.exists(tgt):
            shutil.copy2(tgt, tgt + ".bak")
        shutil.copy2(newexe, tgt)
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR al reemplazar el exe: {e}")
        print("¿El servidor sigue corriendo? Cierra el CamWay Visor y reintenta.")
        return 1

    print("[3/4] Ajustando camway.env...")
    try:
        lines = []
        if os.path.exists(envf):
            with open(envf, encoding="utf-8", errors="replace") as f:
                for ln in f:
                    ln = ln.rstrip("\r\n")
                    key = ln.split("=", 1)[0].strip()
                    if key in ENVKEYS:
                        continue            # se vuelven a escribir / se quitan
                    lines.append(ln)
        lines += ADD
        os.makedirs(os.path.dirname(envf), exist_ok=True)
        with open(envf, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR al ajustar camway.env: {e}")
        return 1

    print("[4/4] Reiniciando el servicio...")
    run(["schtasks", "/Run", "/TN", TASK])
    time.sleep(10)

    print("\n" + "=" * 60)
    print(" LISTO.")
    print(" Abre el CamWay Visor: los 3 canales deben salir solos y estables.")
    print(" Para revertir: en C:\\CamWay está camway-server.exe.bak")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    if not is_admin():
        print("Este actualizador necesita permisos de administrador.")
        print("Cierra esta ventana y ábrelo con clic derecho -> Ejecutar como administrador.")
        try:
            input("\nPresiona Enter para cerrar...")
        except Exception:
            pass
        sys.exit(1)
    code = main()
    try:
        input("\nPresiona Enter para cerrar...")
    except Exception:
        pass
    sys.exit(code)
