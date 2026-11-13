#!/usr/bin/env python3
"""Evolution API LOCAL (contenedor camway-evolution, perfil "alertas") para los
avisos con foto por WhatsApp. Se corre en el servidor del taladro:

    python3 evo_local.py crear     # crea la instancia "alertas" (idempotente)
    python3 evo_local.py qr        # guarda el QR en /tmp/qr_alertas.png
    python3 evo_local.py estado    # state=open cuando el número ya está vinculado
    python3 evo_local.py grupos    # grupos del número vinculado: JID | nombre
    python3 evo_local.py grupo JID # fija ALERT_GROUP en /opt/camway/.env

La clave se lee de /opt/camway/.env y NUNCA se imprime.
"""
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request

ENV = os.environ.get("CAMWAY_ENV", "/opt/camway/.env")
BASE = os.environ.get("EVO_LOCAL", "http://127.0.0.1:8085")
INST = "alertas"
QR = "/tmp/qr_alertas.png"


def env(name):
    for line in open(ENV, encoding="utf-8"):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    return ""


KEY = env("EVO_KEY")


def call(method, path, body=None, timeout=40):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"apikey": KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:200]}


def save_qr(d):
    q = d.get("base64") or (d.get("qrcode") or {}).get("base64") or ""
    if q:
        with open(QR, "wb") as fh:
            fh.write(base64.b64decode(q.split(",")[-1]))
        print("QR guardado en", QR)
        return True
    return False


def main():
    if not KEY:
        sys.exit("EVO_KEY no está en " + ENV)
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "crear":
        code, d = call("POST", "/instance/create", {"instanceName": INST, "qrcode": True, "integration": "WHATSAPP-BAILEYS"})
        print("crear:", code, str(d)[:140].replace(KEY, "***"))
        if not save_qr(d):
            print("(el QR llega unos segundos después: usa 'qr')")
    elif cmd == "qr":
        code, d = call("GET", f"/instance/connect/{INST}")
        print("connect:", code, list(d)[:5])
        if not save_qr(d):
            print("sin QR todavía (¿ya está vinculada?)")
    elif cmd == "estado":
        print(call("GET", f"/instance/connectionState/{INST}"))
    elif cmd == "grupos":
        code, d = call("GET", f"/group/fetchAllGroups/{INST}?getParticipants=false", timeout=90)
        if code != 200:
            sys.exit(f"{code} {d}")
        for g in (d if isinstance(d, list) else d.get("groups", [])):
            print(g.get("id"), "|", g.get("subject"))
    elif cmd == "grupo":
        jid = sys.argv[2]
        txt = open(ENV, encoding="utf-8").read()
        if re.search(r"^ALERT_GROUP=.*$", txt, re.M):
            txt = re.sub(r"^ALERT_GROUP=.*$", "ALERT_GROUP=" + jid, txt, flags=re.M)
        else:
            txt += ("" if txt.endswith("\n") else "\n") + "ALERT_GROUP=" + jid + "\n"
        open(ENV, "w", encoding="utf-8").write(txt)
        print("ALERT_GROUP fijado a", jid)
    else:
        print(__doc__)


main()
