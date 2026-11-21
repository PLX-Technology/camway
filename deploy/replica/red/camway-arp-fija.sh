#!/bin/sh
# Fija la MAC de cada cámara en la tabla ARP del servidor.
#
# Por qué: con dos tarjetas en el mismo segmento, el kernel a veces pierde la
# resolución ARP de una cámara y MediaMTX falla con "connect: no route to
# host". Cada fallo corta la grabación en curso: el 21/11/2026 el .30 tenía
# 1.074 archivos de la .32 en un día (uno cada ~25 s) en vez de segmentos de
# 10 minutos.
#
# Con la entrada en `permanent` no hay resolución que perder. El script vuelve
# a aprender la MAC en cada pasada, así que si se cambia una cámara se corrige
# solo.
#
# Uso:  camway-arp-fija.sh [interfaz]     (por defecto enp2s0)

set -u
IF="${1:-enp2s0}"
REC="${CAMWAY_REC_DIR:-/opt/camway/rec}"

[ -d "$REC" ] || { echo "no existe $REC"; exit 1; }

for d in "$REC"/192-168-*_554__0; do
  [ -d "$d" ] || continue
  ip="$(basename "$d" | cut -d_ -f1 | tr '-' '.')"
  # un ping para que el kernel resuelva la MAC si no la tiene
  ping -c 1 -W 1 "$ip" >/dev/null 2>&1 || continue
  mac="$(ip neigh show "$ip" dev "$IF" | awk '{for(i=1;i<=NF;i++) if($i=="lladdr") print $(i+1)}')"
  if [ -n "$mac" ]; then
    ip neigh replace "$ip" lladdr "$mac" dev "$IF" nud permanent && \
      echo "$ip -> $mac (permanent, $IF)"
  else
    echo "$ip: sin MAC (¿cámara caída?)"
  fi
done
