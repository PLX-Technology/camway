#!/usr/bin/env bash
# Copia de seguridad de un servidor CamWay.
#
#   ./backup.sh --out /mnt/backup                 # config + base de datos
#   ./backup.sh --out /mnt/backup --rec           # además las grabaciones
#   ./backup.sh --out /mnt/backup --dir /opt/camway
#
# La base de datos guarda cámaras, usuarios y ajustes. Sin CAMWAY_SECRET_KEY
# del .env, las credenciales de cámara que contiene no se pueden descifrar: por
# eso el .env va SIEMPRE dentro del respaldo.

set -euo pipefail

DIR=/opt/camway
OUT=
WITH_REC=0

while [ $# -gt 0 ]; do
  case "$1" in
    --out)  OUT="$2"; shift 2 ;;
    --dir)  DIR="$2"; shift 2 ;;
    --rec)  WITH_REC=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "opción desconocida: $1" >&2; exit 2 ;;
  esac
done

[ -n "$OUT" ] || { echo "ERROR: falta --out DIRECTORIO" >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo "ERROR: con sudo (lee el .env y los volúmenes)" >&2; exit 1; }

STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$OUT/camway-$STAMP"
mkdir -p "$DEST"

echo "==> Configuración"
cp -a "$DIR/.env" "$DEST/env"
cp -a "$DIR/docker-compose.yml" "$DEST/" 2>/dev/null || true
cp -a "$DIR/docker-compose.override.yml" "$DEST/" 2>/dev/null || true
[ -d "$DIR/mediamtx" ] && cp -a "$DIR/mediamtx" "$DEST/"
chmod -R go-rwx "$DEST"

echo "==> Base de datos (volumen camway_camway-data)"
if docker volume inspect camway_camway-data >/dev/null 2>&1; then
  docker run --rm -v camway_camway-data:/d:ro -v "$DEST":/b alpine \
    tar czf /b/camway-data.tgz -C /d .
  echo "    $(du -h "$DEST/camway-data.tgz" | cut -f1)"
else
  echo "    (el volumen camway_camway-data no existe todavía)"
fi

if [ "$WITH_REC" -eq 1 ]; then
  REC="$(grep -m1 '^CAMWAY_REC_DIR=' "$DIR/.env" 2>/dev/null | cut -d= -f2-)"
  REC="${REC:-$DIR/rec}"
  if [ -d "$REC" ]; then
    echo "==> Grabaciones ($REC)"
    tar czf "$DEST/grabaciones.tgz" -C "$(dirname "$REC")" "$(basename "$REC")"
    echo "    $(du -h "$DEST/grabaciones.tgz" | cut -f1)"
  fi
fi

cat > "$DEST/LEEME.txt" <<EOF
Respaldo CamWay $STAMP

env                  configuración completa (incluye CAMWAY_SECRET_KEY)
camway-data.tgz      base de datos: cámaras, usuarios, ajustes
docker-compose*.yml  despliegue
mediamtx/            configuración de MediaMTX
grabaciones.tgz      solo si se pidió con --rec

Restaurar en un equipo nuevo:
  1. instalar con deploy/replica/bootstrap.sh
  2. docker run --rm -v camway_camway-data:/d -v "\$PWD":/b alpine \\
       sh -c 'rm -rf /d/* && tar xzf /b/camway-data.tgz -C /d'
  3. cp env /opt/camway/.env   (ajustar HOST_IP)
  4. cd /opt/camway && docker compose up -d --force-recreate
EOF

echo
echo "Respaldo listo en $DEST"
du -sh "$DEST"
