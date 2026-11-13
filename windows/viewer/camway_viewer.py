"""Visor remoto de CamWay (ligero, solo-ver).

Se ejecuta en cualquier PC Windows. Pide IP + puerto + usuario + clave de un
servidor CamWay (p. ej. la máquina del taladro), se conecta, inicia sesión y
muestra la rejilla de cámaras EN VIVO (WebRTC). Las cámaras ya están en el
servidor: el visor las detecta/muestra solas al entrar.

- No instala servidor ni dependencias: usa el motor WebView2 que ya trae Windows.
- Recuerda el último servidor (%APPDATA%\\CamWayVisor\\config.json).
- Cambiar de servidor: botón flotante (↩) o Ctrl+Alt+S.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

import webview

APPDIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "CamWayVisor")
CFG = os.path.join(APPDIR, "config.json")
_BG = "#0f1115"


def load_cfg() -> dict:
    try:
        with open(CFG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(c: dict) -> None:
    try:
        os.makedirs(APPDIR, exist_ok=True)
        with open(CFG, "w", encoding="utf-8") as f:
            json.dump(c, f)
    except Exception:
        pass


FORM_HTML = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<style>
  :root{--bg:%(bg)s;--panel:#161b22;--line:#273040;--fg:#e7ecf3;--mut:#8b96a7;--acc:#2ad4e0;--bad:#ef4444}
  *{box-sizing:border-box} html,body{margin:0;height:100%%;background:var(--bg);color:var(--fg);
    font:15px system-ui,Segoe UI,Roboto,sans-serif}
  .wrap{min-height:100%%;display:flex;align-items:center;justify-content:center;padding:24px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:26px;width:420px;max-width:100%%}
  .logo{font-size:30px;font-weight:800;text-align:center;margin-bottom:2px} .logo .w{color:var(--acc)}
  .sub{color:var(--mut);text-align:center;margin:0 0 18px;font-size:13px}
  label{display:block;margin:12px 0 5px;color:var(--mut);font-size:13px}
  input{width:100%%;background:#0f141b;color:var(--fg);border:1px solid var(--line);border-radius:9px;padding:11px 12px;font:inherit}
  .row{display:flex;gap:10px}.row>div{flex:1}
  button{width:100%%;margin-top:20px;border:0;border-radius:10px;background:var(--acc);color:#04222a;font:inherit;font-weight:700;padding:13px;cursor:pointer}
  button:disabled{opacity:.6;cursor:default}
  .msg{margin-top:12px;min-height:18px;font-size:13px;text-align:center}.msg.err{color:var(--bad)}
</style></head>
<body><div class="wrap"><div class="card">
  <div class="logo">Cam<span class="w">Way</span></div>
  <p class="sub">Visor remoto — conéctate al servidor de cámaras</p>
  <label>Dirección del servidor</label>
  <div class="row">
    <div style="flex:2"><input id="host" placeholder="IP del servidor (ej. 192.168.1.50)" inputmode="decimal"></div>
    <div><input id="port" placeholder="Puerto" value="8120" inputmode="numeric"></div>
  </div>
  <div class="row">
    <div><label>Usuario</label><input id="user" placeholder="usuario" autocomplete="username"></div>
    <div><label>Clave</label><input id="pass" type="password" placeholder="clave" autocomplete="current-password"></div>
  </div>
  <button id="go">Conectar</button>
  <button id="cancel" style="display:none;margin-top:9px;background:transparent;color:var(--mut);border:1px solid var(--line)">Cancelar — volver a las cámaras</button>
  <div class="msg" id="msg"></div>
  <div class="sub" style="margin:14px 0 0">Los datos quedan guardados: la próxima vez entra solo.</div>
</div></div>
<script>
  const $=s=>document.querySelector(s);
  window.prefill=function(c){ if(!c)return; $('#host').value=c.host||''; $('#port').value=c.port||'8120';
    $('#user').value=c.user||''; $('#pass').value=c.pwd||'';
    if(c.host) $('#cancel').style.display='block'; };   // ya hay servidor guardado → se puede cancelar
  window.setMsg=function(t,err){ const m=$('#msg'); m.textContent=t||''; m.className='msg'+(err?' err':''); };
  async function go(){
    const host=$('#host').value.trim(); if(!host){ setMsg('Escribe la IP del servidor.',true); return; }
    $('#go').disabled=true; setMsg('Conectando…');
    try{
      const r=await window.pywebview.api.connect(host,$('#port').value.trim(),$('#user').value.trim(),$('#pass').value);
      if(!r||!r.ok){ setMsg((r&&r.error)||'No se pudo conectar.',true); $('#go').disabled=false; }
    }catch(e){ setMsg('No se pudo conectar.',true); $('#go').disabled=false; }
  }
  $('#go').onclick=go;
  $('#cancel').onclick=function(){ try{ window.pywebview.api.reconnect(); }catch(e){} };
  $('#pass').addEventListener('keydown',e=>{ if(e.key==='Enter') go(); });
  $('#host').focus();
</script></body></html>""" % {"bg": _BG}


# JS que se inyecta en la página /live del servidor remoto: inicia sesión
# (mismo origen → la cookie queda en el WebView), recarga para mostrar la rejilla,
# y agrega un botón flotante + atajo para volver a elegir servidor.
def _login_js(user: str, pwd: str) -> str:
    return (
        "(async()=>{"
        "  try{ await fetch('/api/login',{method:'POST',"
        "    headers:{'Content-Type':'application/json'},"
        "    body:JSON.stringify({username:%s,password:%s})}); }catch(e){}"
        "  location.reload();"
        "})()" % (json.dumps(user), json.dumps(pwd))
    )


_EXTRAS_JS = (
    "(function(){ if(window.__cwExtras)return; window.__cwExtras=true;"
    " var b=document.createElement('button'); b.textContent='\\u2699 Servidor';"
    " b.title='Editar datos de conexión (IP, usuario, clave)';"
    " b.style.cssText='position:fixed;right:12px;top:10px;z-index:99999;opacity:.55;"
    "border:0;border-radius:8px;background:rgba(10,14,20,.7);color:#fff;"
    "font:13px system-ui;padding:7px 12px;cursor:pointer;box-shadow:0 1px 6px rgba(0,0,0,.4)';"
    " b.onmouseenter=function(){b.style.opacity=1}; b.onmouseleave=function(){b.style.opacity=.55};"
    " b.onclick=function(){ if(window.pywebview&&pywebview.api) pywebview.api.forget(); };"
    " document.body.appendChild(b);"
    " document.addEventListener('keydown',function(e){"
    "   if(e.ctrlKey&&e.altKey&&(e.key==='s'||e.key==='S')){ if(window.pywebview&&pywebview.api) pywebview.api.forget(); } });"
    "})()"
)


class Api:
    def __init__(self):
        self.window = None
        self._target = None     # (host, port, user, pwd)
        self._logged = False    # login inyectado ya (se resetea al conectar/olvidar)

    def _base(self, host: str, port: str) -> str:
        return f"http://{host}:{port or '8120'}"

    def connect(self, host: str, port: str, user: str, pwd: str) -> dict:
        host = (host or "").strip()
        port = (str(port) or "8120").strip() or "8120"
        base = self._base(host, port)
        # alcanzabilidad: cualquier respuesta HTTP (incluso 401/404) = servidor vivo
        ok = False
        for path in ("/api/me", "/healthz", "/api/app", "/"):
            try:
                urllib.request.urlopen(base + path, timeout=6)
                ok = True
                break
            except urllib.error.HTTPError:
                ok = True
                break
            except Exception:
                continue
        if not ok:
            return {"ok": False, "error": f"No se pudo conectar a {host}:{port}. Revisa la IP/puerto y la red."}
        save_cfg({"host": host, "port": port, "user": user, "pwd": pwd})
        self._target = (host, port, user, pwd)
        self._logged = False
        try:
            self.window.load_url(base + "/live")
        except Exception:
            return {"ok": False, "error": "No se pudo abrir el servidor."}
        return {"ok": True}

    def forget(self) -> None:
        # Mostrar el formulario con los datos guardados para EDITARLOS (no borra nada).
        self._target = None
        self._logged = False
        try:
            self.window.load_html(FORM_HTML)
            cfg = load_cfg()
            if cfg:
                self.window.evaluate_js("window.prefill && window.prefill(%s)" % json.dumps(cfg))
        except Exception:
            pass

    def reconnect(self) -> dict:
        """Cancelar la edición: volver a conectar con los datos guardados."""
        cfg = load_cfg()
        if not cfg.get("host"):
            return {"ok": False}
        return self.connect(cfg["host"], cfg.get("port", "8120"), cfg.get("user", ""), cfg.get("pwd", ""))


API = Api()


def _on_loaded() -> None:
    """Al cargar una página: si es la /live del servidor, inyecta login + extras."""
    if not API._target:
        return
    try:
        url = API.window.get_current_url() or ""
    except Exception:
        url = ""
    host, port, user, pwd = API._target
    if url.startswith(API._base(host, port)) and "/live" in url:
        try:
            API.window.evaluate_js(_EXTRAS_JS)
            if not API._logged:                     # login SOLO la primera vez (evita bucle de reload)
                API._logged = True
                API.window.evaluate_js(_login_js(user, pwd))
        except Exception:
            pass


def main() -> int:
    window = webview.create_window(
        "CamWay Visor", html=FORM_HTML, js_api=API,
        width=1100, height=720, min_size=(820, 560), background_color=_BG)
    API.window = window
    window.events.loaded += _on_loaded

    def boot():
        try:
            window.maximize()
        except Exception:
            pass
        cfg = load_cfg()
        if cfg.get("host"):
            # auto-conectar al último servidor; si falla, queda el formulario con los datos
            window.evaluate_js("window.prefill && window.prefill(%s)" % json.dumps(cfg))
            r = API.connect(cfg["host"], cfg.get("port", "8120"), cfg.get("user", ""), cfg.get("pwd", ""))
            if not r.get("ok"):
                window.evaluate_js("window.setMsg && window.setMsg(%s,true)" % json.dumps(r.get("error", "")))

    webview.start(boot, gui="edgechromium")
    return 0


if __name__ == "__main__":
    sys.exit(main())
