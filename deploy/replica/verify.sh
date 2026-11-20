#!/usr/bin/env bash
# Comprobación rápida del servidor CamWay.
#
#   ./verify.sh                     # usa /opt/camway/.env
#   ./verify.sh --dir /opt/camway
#
# Sale con código 1 si algo está claramente mal.

set -uo pipefail

DIR=/opt/camway
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "opción desconocida: $1" >&2; exit 2 ;;
  esac
done

OK=0; BAD=0
ok()   { printf '  \033[1;32mOK\033[0m    %s\n' "$*"; OK=$((OK+1)); }
bad()  { printf '  \033[1;31mFALLA\033[0m %s\n' "$*"; BAD=$((BAD+1)); }
warn() { printf '  \033[1;33mAVISO\033[0m %s\n' "$*"; }
head1(){ printf '\n\033[1;36m%s\033[0m\n' "$*"; }

HOST_IP="$( { grep -m1 '^HOST_IP=' "$DIR/.env" 2>/dev/null || sudo -n grep -m1 '^HOST_IP=' "$DIR/.env" 2>/dev/null; } | cut -d= -f2-)"
[ -n "$HOST_IP" ] || HOST_IP=$(hostname -I | awk '{print $1}')

head1 "Contenedores"
RUNNING=$(docker ps --format '{{.Names}}' 2>/dev/null)
for c in camway-api camway-mediamtx; do
  if echo "$RUNNING" | grep -qx "$c"; then ok "$c arriba"; else bad "$c no está corriendo"; fi
done

head1 "API y panel"
# Sin curl a propósito: en muchos servidores mínimos no está instalado.
http_ok() {
  python3 - "$1" <<'PY' >/dev/null 2>&1
import sys, urllib.request
urllib.request.urlopen(sys.argv[1], timeout=5).read(1)
PY
}
if http_ok "http://127.0.0.1:8120/api/health"; then
  ok "API responde en :8120"
else
  bad "API no responde en :8120"
fi
if [ -n "$HOST_IP" ] && http_ok "http://$HOST_IP:8120/api/health"; then
  ok "panel alcanzable en http://$HOST_IP:8120"
else
  warn "no se pudo probar el panel por $HOST_IP (normal si aún no hay IP fija)"
fi

head1 "Rutas de cámara en MediaMTX"
PATHS=$(docker exec camway-mediamtx wget -qO- http://127.0.0.1:9997/v3/paths/list 2>/dev/null)
if [ -z "$PATHS" ]; then
  bad "MediaMTX no responde a su API"
else
  echo "$PATHS" | python3 -c '
import json,sys
d=json.load(sys.stdin)
items=[i for i in d.get("items",[]) if i.get("name","").startswith("192") or i.get("name","").startswith("172")]
if not items: print("  (ninguna cámara publicada todavía)")
for i in items:
    print("  %-30s ready=%s readers=%d" % (i.get("name"), i.get("ready"), len(i.get("readers") or [])))
' 2>/dev/null || bad "no se pudo leer la lista de rutas"
fi

head1 "Mosaico"
MOS=$(docker exec camway-mediamtx wget -qO- http://127.0.0.1:9997/v3/paths/get/mosaico 2>/dev/null)
if [ -z "$MOS" ]; then
  warn "el mosaico no está armado (nadie lo ha pedido todavía desde la TV o el panel)"
else
  echo "$MOS" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("  ready=%s  bytes=%s  tracks=%s" % (d.get("ready"), d.get("bytesReceived"),
      [(t.get("codec"), (t.get("codecProps") or {}).get("width"), (t.get("codecProps") or {}).get("height")) for t in (d.get("tracks2") or [])]))
' 2>/dev/null
  H1=$(docker exec camway-api sh -c 'ffmpeg -nostdin -loglevel error -rtsp_transport tcp -i rtsp://mediamtx:8554/mosaico -frames:v 1 -f image2pipe -vcodec mjpeg pipe:1 2>/dev/null' 2>/dev/null | md5sum | cut -c1-8)
  sleep 2
  H2=$(docker exec camway-api sh -c 'ffmpeg -nostdin -loglevel error -rtsp_transport tcp -i rtsp://mediamtx:8554/mosaico -frames:v 1 -f image2pipe -vcodec mjpeg pipe:1 2>/dev/null' 2>/dev/null | md5sum | cut -c1-8)
  if [ -n "$H1" ] && [ "$H1" != "$H2" ]; then
    ok "el mosaico se mueve (fotogramas distintos)"
  elif [ -n "$H1" ]; then
    bad "el mosaico NO se mueve (fotograma repetido: $H1)"
  else
    warn "no se pudo capturar un fotograma del mosaico"
  fi
fi

head1 "Memoria del mosaico"
MEM=$(docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' 2>/dev/null | awk '$1=="camway-api"{print $2}')
[ -n "$MEM" ] && ok "camway-api: $MEM (el mosaico debe quedar plano; tope del contenedor 1.5 GB)"

head1 "Red"
DEF=$(ip route show default | wc -l)
if [ "$DEF" -eq 1 ]; then
  ok "una sola ruta por defecto: $(ip route show default | awk '{print $3" por "$5}')"
elif [ "$DEF" -eq 0 ]; then
  warn "sin ruta por defecto (el equipo no tiene internet)"
else
  bad "$DEF rutas por defecto: si una apunta a la LAN de cámaras, el equipo se cuelga cuando esa antena cae"
  ip route show default | sed 's/^/        /'
fi

head1 "Endurecimiento"
[ "$(sysctl -n kernel.hung_task_panic 2>/dev/null)" = "1" ] && ok "hung_task_panic activo (se reinicia solo si se cuelga)" || warn "hung_task_panic no activo"
[ -f /etc/sysctl.d/99-camway-resiliencia.conf ] && ok "sysctl de resiliencia instalado" || warn "falta 99-camway-resiliencia.conf"

head1 "Túnel al VPS"
if [ "$(systemctl show camway-tunnel -p LoadState --value 2>/dev/null)" = "loaded" ]; then
  if systemctl is-active --quiet camway-tunnel; then
    N=$(systemctl show camway-tunnel -p NRestarts --value 2>/dev/null)
    ok "camway-tunnel activo (reinicios: ${N:-0})"
    [ "${N:-0}" -gt 20 ] && warn "muchos reinicios: revisar si el puerto remoto está ocupado en el VPS"
  else
    bad "camway-tunnel no está activo"
  fi
else
  warn "túnel no instalado"
fi

printf '\n\033[1m%d comprobaciones OK, %d fallos\033[0m\n\n' "$OK" "$BAD"
[ "$BAD" -eq 0 ]
