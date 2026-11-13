"""Cliente embebido de CamWay para Windows.

Abre una ventana nativa (motor WebView2 de Windows, mismo Chromium del Edge) que
muestra el muro de cámaras del servidor CamWay LOCAL — sin navegador, sin barra
de direcciones, sin pestañas. El usuario solo ve "la app de cámaras".

El servidor (camway-server.exe + mediamtx.exe) corre aparte como servicio; este
cliente espera a que responda y entonces carga su interfaz. Si no está listo,
muestra un aviso y reintenta.

Puerto configurable con la variable de entorno CAMWAY_CLIENT_PORT (por defecto 8120).
"""
from __future__ import annotations

import os
import sys
import time
import threading
import urllib.request

import webview

PORT = os.environ.get("CAMWAY_CLIENT_PORT", "8120").strip() or "8120"
BASE = f"http://127.0.0.1:{PORT}"
HEALTH = BASE + "/api/health"
LIVE = BASE + "/live"          # rejilla WebRTC en modo kiosko (sin login)
WAIT_SECONDS = int(os.environ.get("CAMWAY_CLIENT_WAIT", "240"))

_BG = "#0f1115"

_SPLASH = """<!doctype html><html><head><meta charset="utf-8">
<style>
  html,body{margin:0;height:100%%;background:%(bg)s;color:#e7ecf3;
    font:16px/1.4 system-ui,Segoe UI,Roboto,sans-serif}
  .wrap{height:100%%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px}
  .logo{font-size:34px;font-weight:800;letter-spacing:.3px}
  .logo .w{color:#2ad4e0}
  .msg{color:#8b96a7;font-size:14px}
  .dot{width:54px;height:54px;border-radius:50%%;border:4px solid #263041;border-top-color:#2ad4e0;
    animation:spin 1s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .err{color:#ff8f8f;max-width:520px;text-align:center}
  a{color:#2ad4e0}
</style></head>
<body><div class="wrap">
  <div class="logo">Cam<span class="w">Way</span></div>
  <div class="dot" id="dot"></div>
  <div class="msg" id="msg">Conectando con el servidor de cámaras…</div>
</div>
<script>
  window.setMsg = function(t, err){
    document.getElementById('msg').innerHTML = t;
    if(err){ document.getElementById('dot').style.display='none';
             document.getElementById('msg').className='err'; }
  };
</script></body></html>""" % {"bg": _BG}


def _server_up() -> bool:
    try:
        with urllib.request.urlopen(HEALTH, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _boot(window) -> None:
    """Espera a que el servidor local responda y carga su interfaz."""
    for i in range(WAIT_SECONDS):
        if _server_up():
            window.load_url(LIVE)
            return
        if i == 8:
            _set_msg(window, "Iniciando el servicio de cámaras…<br>"
                             "<span style='font-size:12px'>puede tardar unos segundos</span>")
        time.sleep(1)
    _set_msg(window,
             "No se pudo conectar con el servidor de cámaras en "
             f"<b>{BASE}</b>.<br><br>Revisa que el servicio <b>CamWay</b> esté corriendo "
             "(se reintenta solo) y vuelve a abrir esta ventana.",
             err=True)


def _set_msg(window, text: str, err: bool = False) -> None:
    try:
        window.evaluate_js(f"window.setMsg({text!r}, {str(err).lower()})")
    except Exception:
        pass


def _watchdog(window) -> None:
    """Si el servidor se cae mientras la app está abierta, vuelve al splash y
    espera a que regrese (en un taladro con cortes de luz el servicio reinicia)."""
    time.sleep(10)
    misses = 0
    while True:
        time.sleep(5)
        if _server_up():
            misses = 0
            continue
        misses += 1
        if misses == 3:                      # ~15 s sin servidor
            try:
                window.load_html(_SPLASH)
            except Exception:
                return
            _boot(window)
            misses = 0


def main() -> int:
    window = webview.create_window(
        "CamWay", html=_SPLASH,
        width=1280, height=800, min_size=(900, 600),
        background_color=_BG, text_select=False)

    def on_start():
        try:
            window.maximize()
        except Exception:
            pass
        _boot(window)
        threading.Thread(target=_watchdog, args=(window,), daemon=True).start()

    # gui='edgechromium' fuerza el motor WebView2 (Chromium) en Windows → WebRTC OK.
    webview.start(on_start, gui="edgechromium")
    return 0


if __name__ == "__main__":
    sys.exit(main())
