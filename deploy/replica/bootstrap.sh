#!/usr/bin/env bash
# Instalación base de un servidor CamWay en Ubuntu (24.04 LTS recomendado).
# Idempotente: se puede volver a ejecutar sin romper nada.
#
#   sudo ./bootstrap.sh --dir /opt/camway
#   sudo ./bootstrap.sh --dir /opt/camway --tunnel --no-build
#   sudo ./bootstrap.sh --dir /opt/camway --from-tar /media/camway.tar.gz
#
# No levanta los contenedores si se pasa --no-build.

set -euo pipefail

DIR=/opt/camway
SVC_USER=camway
REPO=https://github.com/PLX-Technology/camway.git
BRANCH=main
FROM_TAR=
WITH_TUNNEL=0
DO_BUILD=1
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)       DIR="$2"; shift 2 ;;
    --user)      SVC_USER="$2"; shift 2 ;;
    --repo)      REPO="$2"; shift 2 ;;
    --branch)    BRANCH="$2"; shift 2 ;;
    --from-tar)  FROM_TAR="$2"; shift 2 ;;
    --tunnel)    WITH_TUNNEL=1; shift ;;
    --no-build)  DO_BUILD=0; shift ;;
    -h|--help)   sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "opción desconocida: $1" >&2; exit 2 ;;
  esac
done

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "hay que ejecutarlo con sudo"

# ---------------------------------------------------------------- sistema ---
log "Paquetes base"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 curl ca-certificates git openssl \
  || apt-get install -y -qq docker.io docker-compose curl ca-certificates git openssl
systemctl enable --now docker >/dev/null 2>&1 || true

if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  die "no hay docker compose disponible"
fi

# ----------------------------------------------------------------- usuario --
log "Usuario de servicio: $SVC_USER"
if ! id "$SVC_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "$SVC_USER"
  echo "  creado (sin contraseña: poner una con 'passwd $SVC_USER' si hace falta)"
fi
usermod -aG docker "$SVC_USER"

# --------------------------------------------------------------- proyecto --
log "Proyecto en $DIR"
mkdir -p "$DIR"
if [ -n "$FROM_TAR" ]; then
  [ -f "$FROM_TAR" ] || die "no existe $FROM_TAR"
  tar xzf "$FROM_TAR" -C "$DIR" --strip-components=1
  echo "  descomprimido desde $FROM_TAR"
elif [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch --all --prune
  git -C "$DIR" checkout "$BRANCH"
  git -C "$DIR" pull --ff-only
  echo "  repo actualizado"
elif [ -z "$(ls -A "$DIR" 2>/dev/null)" ]; then
  git clone --branch "$BRANCH" "$REPO" "$DIR"
  echo "  clonado desde $REPO"
else
  echo "  $DIR ya tiene contenido y no es un repo git: se deja como está"
fi

mkdir -p "$DIR/rec"
chown -R "$SVC_USER":"$SVC_USER" "$DIR"

# ------------------------------------------------------------------- .env --
log "Configuración (.env)"
if [ ! -f "$DIR/.env" ]; then
  cp "$HERE/env.example" "$DIR/.env"
  SECRET="$(openssl rand -base64 32)"
  EVOPASS="$(openssl rand -hex 16)"
  sed -i "s|^CAMWAY_SECRET_KEY=.*|CAMWAY_SECRET_KEY=${SECRET}|" "$DIR/.env"
  sed -i "s|^EVO_DB_PASS=.*|EVO_DB_PASS=${EVOPASS}|" "$DIR/.env"
  sed -i "s|^CAMWAY_REC_DIR=.*|CAMWAY_REC_DIR=${DIR}/rec|" "$DIR/.env"
  chown "$SVC_USER":"$SVC_USER" "$DIR/.env"
  chmod 600 "$DIR/.env"
  echo "  creado con claves aleatorias -> REVISAR HOST_IP y SCAN_CIDR"
else
  echo "  ya existe: no se toca"
fi

# ------------------------------------------------------------ endurecimiento -
log "Endurecimiento del sistema"
install -m 0644 "$HERE/99-camway-resiliencia.conf" /etc/sysctl.d/99-camway-resiliencia.conf
sysctl --system >/dev/null
install -d /etc/systemd/journald.conf.d
install -m 0644 "$HERE/99-camway-journald.conf" /etc/systemd/journald.conf.d/99-camway.conf
systemctl restart systemd-journald
echo "  sysctl y journald aplicados"

# ------------------------------------------------------------------ firewall -
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  log "Abriendo puertos en ufw"
  for p in 8120/tcp 8889/tcp 8888/tcp 8554/tcp 8189/udp; do ufw allow "$p" >/dev/null; done
  echo "  8120/tcp 8889/tcp 8888/tcp 8554/tcp 8189/udp"
fi

# -------------------------------------------------------------------- tunel --
if [ "$WITH_TUNNEL" -eq 1 ]; then
  log "Túnel inverso al VPS"
  KEY="/home/$SVC_USER/.ssh/vps_tunnel"
  if [ ! -f "$KEY" ]; then
    install -d -m 0700 -o "$SVC_USER" -g "$SVC_USER" "/home/$SVC_USER/.ssh"
    sudo -u "$SVC_USER" ssh-keygen -t ed25519 -N "" -f "$KEY" -q
    echo "  clave creada: $KEY.pub"
    echo "  >>> hay que autorizarla en el VPS antes de que el túnel funcione:"
    echo "      ssh-copy-id -i $KEY.pub root@rtdriller.com"
  fi
  install -m 0644 "$HERE/camway-tunnel.service" /etc/systemd/system/camway-tunnel.service
  systemctl daemon-reload
  systemctl enable camway-tunnel >/dev/null 2>&1 || true
  echo "  servicio instalado (revisar el puerto remoto antes de arrancarlo)"
fi

# ------------------------------------------------------------------ arranque -
if [ "$DO_BUILD" -eq 1 ]; then
  log "Construyendo y levantando contenedores"
  ( cd "$DIR" && $DC up -d --build )
  log "Estado"
  docker ps --format 'table {{.Names}}\t{{.Status}}'
else
  log "Omitido el arranque (--no-build). Cuando toque:"
  echo "  cd $DIR && sudo $DC up -d --build"
fi

HOST_ONLY=$(grep -m1 '^HOST_IP=' "$DIR/.env" 2>/dev/null | cut -d= -f2-)

cat <<EOF

Listo. Siguientes pasos:
  1. Revisar $DIR/.env  (HOST_IP y SCAN_CIDR son lo mínimo)
  2. (Opcional) red fija de la NIC de cámaras, SIN gateway:
       sudo $HERE/harden-network.sh --iface enp2s0 --ip 192.168.0.30/24
  3. Verificar:  $HERE/verify.sh
  4. Panel:  http://${HOST_ONLY:-IP-DEL-EQUIPO}:8120

EOF
