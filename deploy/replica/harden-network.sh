#!/usr/bin/env bash
# Deja la NIC de cámaras con IP fija y SIN ruta por defecto.
#
# Por qué: en el servidor del taladro esa NIC tenía gateway 192.168.0.1. Cuando
# la antena se caía, todo el tráfico del equipo se iba a un gateway inexistente
# y el servidor parecía colgado (además de que el túnel al VPS se caía).
#
#   sudo ./harden-network.sh --iface enp2s0 --ip 192.168.0.30/24
#   sudo ./harden-network.sh --iface enp2s0 --ip 192.168.0.30/24 --no-wifi
#
# No reinicia la interfaz: los cambios se aplican en el siguiente arranque o con
#   sudo nmcli connection up "<perfil>"
# así no se pierde la sesión SSH.

set -euo pipefail

IFACE=
IP_CIDR=
KILL_WIFI=0

while [ $# -gt 0 ]; do
  case "$1" in
    --iface)   IFACE="$2"; shift 2 ;;
    --ip)      IP_CIDR="$2"; shift 2 ;;
    --no-wifi) KILL_WIFI=1; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "opción desconocida: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "ERROR: con sudo" >&2; exit 1; }
[ -n "$IFACE" ] && [ -n "$IP_CIDR" ] || { echo "ERROR: faltan --iface y --ip" >&2; exit 2; }

CON="$(nmcli -t -f NAME,DEVICE con show --active | awk -F: -v d="$IFACE" '$2==d{print $1; exit}')"
if [ -z "$CON" ]; then
  CON="$(nmcli -t -f NAME,DEVICE con show | awk -F: -v d="$IFACE" '$2==d{print $1; exit}')"
fi
if [ -z "$CON" ]; then
  echo "No hay perfil de NetworkManager para $IFACE. Creando uno nuevo."
  nmcli con add type ethernet con-name "camway-lan" ifname "$IFACE" \
        ipv4.method manual ipv4.addresses "$IP_CIDR" >/dev/null
  CON="camway-lan"
fi

echo "Perfil: $CON  ($IFACE)"

nmcli con mod "$CON" \
  ipv4.method manual \
  ipv4.addresses "$IP_CIDR" \
  ipv4.gateway "" \
  ipv4.never-default yes \
  ipv4.dns "" \
  ipv4.ignore-auto-dns yes \
  ipv6.method disabled

echo "  IP fija $IP_CIDR, sin gateway, sin DNS, sin IPv6"

if [ "$KILL_WIFI" -eq 1 ]; then
  while IFS=: read -r name type; do
    [ "$type" = "802-11-wireless" ] || continue
    nmcli con mod "$name" connection.autoconnect no
    echo "  Wi-Fi sin autoconexión: $name"
  done < <(nmcli -t -f NAME,TYPE con show)
fi

echo
echo "Aplicar ahora (corta la red de esa NIC unos segundos):"
echo "  sudo nmcli connection up \"$CON\""
echo
echo "Comprobar después:"
echo "  ip route show default          # debe quedar SOLO la de internet"
echo "  ip -br addr show $IFACE"
