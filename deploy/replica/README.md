# Réplica de un servidor CamWay (instalación nueva)

Todo lo necesario para levantar otro equipo igual al del taladro (`.30`) desde
cero, sin tocar el que ya está en producción.

El código del repo es **idéntico** a lo que corre en producción: la instalación
no depende de archivos sueltos del equipo viejo. Lo único que no está en git es
el `.env` (credenciales) y los datos (base de datos y grabaciones).

---

## 1. Requisitos

| | Mínimo | Recomendado |
|---|---|---|
| CPU | 4 núcleos | 8+ (el mosaico y la IA usan CPU real) |
| RAM | 8 GB | 16 GB |
| Disco | 120 GB | SSD/NVMe de 1 TB **dedicado a grabaciones** |
| Red | 1 NIC en la LAN de cámaras | 2 NIC: una LAN de cámaras + una con internet |
| SO | Ubuntu 24.04 LTS | Ubuntu 24.04 LTS (LTS) |

> **No uses Ubuntu 24.10** (el `.30` lo tiene y ya está sin soporte). Para un
> equipo nuevo, 24.04 LTS.

Puertos que hay que dejar libres: `8120/tcp` (panel), `8889/tcp` (WebRTC),
`8888/tcp` (HLS), `8554/tcp` (RTSP interno), `8189/udp` (ICE).

---

## 2. Instalación en 5 pasos

> Usa **`/opt/camway`** como carpeta: el nombre de los volúmenes de Docker sale
> del nombre de esa carpeta (`camway_camway-data`). Si la cambias, tendrás que
> ajustar también los comandos de restauración del punto 6.

```bash
# 1) clonar
sudo mkdir -p /opt/camway
sudo chown "$USER":"$USER" /opt/camway
git clone https://github.com/PLX-Technology/camway.git /opt/camway
cd /opt/camway/deploy/replica

# 2) instalar lo del sistema (docker, usuario, carpetas, hardening)
sudo ./bootstrap.sh --dir /opt/camway

# 3) configurar credenciales
sudo nano /opt/camway/.env          # como mínimo HOST_IP y SCAN_CIDR

# 4) levantar
cd /opt/camway && sudo docker compose up -d --build

# 5) comprobar
./verify.sh
```

Después, abre `http://IP-DEL-EQUIPO:8120`.

Si el equipo **no tiene internet**, copia el repo en un tar y usa
`sudo ./bootstrap.sh --dir /opt/camway --from-tar /ruta/camway.tar.gz`.

---

## 3. Qué hace `bootstrap.sh`

- Instala `docker.io` + `docker-compose-v2` (paquetes de Ubuntu, sin repos
  externos) y `net-tools`, `curl`.
- Crea el usuario `camway` (si no existe), lo mete en el grupo `docker` y le da
  la carpeta del proyecto.
- Crea `/opt/camway/rec` para las grabaciones.
- Genera `/opt/camway/.env` desde `env.example` con `CAMWAY_SECRET_KEY` y
  `EVO_DB_PASS` aleatorios (no sobreescribe uno existente).
- Aplica el endurecimiento del sistema:
  - `99-camway-resiliencia.conf` → si el kernel se cuelga, reinicia solo
    (`panic=10`, `hung_task_panic=1`), y `swappiness=10` para no hacer *thrash*.
  - `99-camway-journald.conf` → journal persistente, a disco cada 10 s. Sin
    esto, un cuelgue se lleva por delante los últimos minutos de log.
- Abre los puertos en `ufw` si está activo.
- Es **idempotente**: se puede volver a ejecutar sin romper nada.

Opciones:

```
--dir DIR        carpeta del proyecto (por defecto /opt/camway)
--user USER      usuario de servicio (por defecto camway)
--repo URL       repo a clonar (por defecto el de GitHub)
--branch BR      rama (por defecto main)
--from-tar FILE  instalar desde un tar en vez de clonar (equipos sin internet)
--tunnel         instalar el túnel inverso al VPS (requiere la clave SSH)
--no-build       no ejecutar docker compose build/up al final
```

---

## 4. Red

El `.30` tenía dos problemas que **no** hay que replicar:

1. **Ruta por defecto hacia la LAN de cámaras.** El perfil de la NIC de cámaras
   tenía `gateway 192.168.0.1`. Si esa antena se cae, todo el tráfico del
   servidor se va a un gateway que no existe y el equipo parece colgado.
   → La NIC de cámaras **no debe tener gateway** (`ipv4.never-default yes`).
2. **Wi-Fi con la misma subred.** Dos perfiles "Pacific-Logging" metían una
   segunda interfaz en `192.168.0.0/24` con su propio gateway.
   → Si el equipo no usa Wi-Fi, déjalo sin autoconexión.

Para dejarlo bien desde el principio:

```bash
sudo ./harden-network.sh --iface enp2s0 --ip 192.168.0.30/24
```

Hace exactamente eso: IP fija en la NIC de cámaras, **sin gateway**, sin DNS de
esa interfaz y con IPv6 desactivado en ella. La salida a internet va por la otra
NIC (DHCP del router).

---

### Hora de las cámaras (NTP)

El equipo sirve NTP a la LAN de cámaras con **chrony** (`allow 192.168.0.0/24`
en `/etc/chrony/conf.d/camway-lan.conf`). Hay que apuntar cada cámara a este
servidor: si se quedan con `time.windows.com` o `ipvs.icamra.com` (los valores
de fábrica), desde la LAN de cámaras no se alcanzan y el reloj se queda clavado
—con fecha de 2018— en el OSD y en las grabaciones.

Desde el panel: **poner en hora** en la ficha de la cámara. El endpoint
`POST /api/devices/{id}/osd {"set_time": true}` la apunta a `CAMWAY_NTP_HOST`
(= `HOST_IP`) y la deja en modo NTP, sin tocar su OSD.

Comprobación:

```bash
chronyc clients            # deben aparecer las IPs de las cámaras
```

> **Trampa del horario de verano (iCamra).** Estas cámaras pueden quedarse con
> el flag de horario de verano activo: el OSD muestra **una hora más** (UTC-3)
> aunque `GetSystemDateAndTime` responda `DaylightSavings=false` y una hora
> local correcta. Medido en el `.32` y el `.33`: OSD 17:05 con hora real 16:05.
>
> No basta con reenviar `SetSystemDateAndTime`: hay que forzar un cambio de
> zona y volver a la original. Secuencia que lo corrige (probada, verificada
> leyendo el OSD por OCR):
>
> 1. `SetSystemDateAndTime` con `TimeZone` = `America/Caracas` (la cámara la
>    interpreta como UTC +0).
> 2. `SetSystemDateAndTime` con `TimeZone` = `AST4ADT,M4.1.0/00:01:00,M10.5.0/00:01:00`
>    y `DaylightSavings=false` → vuelve a UTC-4 pero ya sin el flag.
>
> Conviene comprobarlo leyendo el OSD (no la API): la API puede decir una cosa
> y el vídeo otra.

### Grabaciones de segundos en vez de 10 minutos

Síntoma: cientos de MP4 al día por cámara, de entre 0,7 y 15 segundos, cuando
`recordSegmentDuration` está en **10m**.

Causa: MediaMTX **cierra el archivo cada vez que se cae la conexión con la
cámara** y abre otro al reconectar; no es que el segmento esté mal configurado.
En el `.30` el log mostraba, cada ~25 s:

```
[RTSP source] dial tcp 192.168.0.32:554: connect: no route to host
```

`no route to host` hacia un equipo de la misma LAN = **fallo de resolución
ARP**, no de la cámara. Con dos tarjetas metidas en el mismo segmento el kernel
mezcla vecinos (ARP flux). El 21/11/2026 la `.32` acumuló 1.074 archivos en un
día.

Arreglo (incluido aquí):

```bash
sudo install -m 0755 red/camway-arp-fija.sh /usr/local/sbin/
sudo install -m 0644 red/camway-arp-fija.service red/camway-arp-fija.timer /etc/systemd/system/
sudo install -m 0644 red/99-camway-red.conf /etc/sysctl.d/
sudo sysctl --system
sudo systemctl daemon-reload && sudo systemctl enable --now camway-arp-fija.timer
```

Deja la MAC de cada cámara como entrada **permanent** (sin resolución que
perder), endurece el ARP del kernel y **reaprende la MAC cada 10 minutos**, así
que si se cambia una cámara se corrige sola. Ajustar la interfaz en el
`.service` si en el equipo nuevo se llama distinto.

Verificación: los segmentos nuevos deben durar 600 s.

```bash
ls -t /opt/camway/rec/192-168-0-32_554__0 | head -3
docker exec camway-mediamtx ffprobe -v error -show_entries format=duration \
  -of csv=p=0 /rec/192-168-0-32_554__0/<archivo>.mp4
```

## 5. Túnel inverso al VPS (OTA)
Permite entrar al equipo del cliente sin abrir puertos ni usar VPN.

```bash
# en el servidor nuevo
ssh-keygen -t ed25519 -N "" -f /home/camway/.ssh/vps_tunnel
ssh-copy-id -i /home/camway/.ssh/vps_tunnel.pub root@rtdriller.com   # o pegar la clave a mano

sudo ./bootstrap.sh --dir /opt/camway --tunnel --no-build
sudo systemctl status camway-tunnel
```

Desde el VPS: `ssh -p 2222 camway@127.0.0.1` (el puerto remoto es configurable en
`camway-tunnel.service`; **cada equipo necesita un puerto distinto**).

> `ExitOnForwardFailure=yes` es a propósito: si el puerto remoto está ocupado, el
> servicio falla y reintenta en vez de quedarse "arriba" sin túnel. En el `.30`
> llegó a 224 reinicios por una sesión vieja reteniendo el 2222.

---

## 6. Migrar los datos del `.30` actual

Solo si la réplica debe arrancar con las cámaras, usuarios y ajustes ya puestos.

```bash
# en el equipo VIEJO: base de datos (cámaras, usuarios, ajustes)
docker run --rm -v camway_camway-data:/d -v "$PWD":/b alpine \
  tar czf /b/camway-data.tgz -C /d .

# y el .env (tiene las credenciales de cámaras y de Evolution)
sudo cp /opt/camway/.env ./camway-env

# en el equipo NUEVO
docker run --rm -v camway_camway-data:/d -v "$PWD":/b alpine \
  sh -c 'rm -rf /d/* && tar xzf /b/camway-data.tgz -C /d'
sudo cp camway-env /opt/camway/.env      # ajustar HOST_IP
cd /opt/camway && sudo docker compose up -d --force-recreate
```

Las grabaciones (`/opt/camway/rec`, ~525 GB en el `.30`) **no** se copian salvo
que se quieran conservar: son 14 días de video.

---

## 7. Copias de seguridad

```bash
./backup.sh --out /mnt/backup        # .env + compose + mediamtx.yml + SQLite
./backup.sh --out /mnt/backup --rec  # además, las grabaciones
```

Guarda también, fuera del equipo, una copia del `.env`: sin
`CAMWAY_SECRET_KEY` las credenciales de cámara guardadas en la base de datos no
se pueden descifrar.

---

## 8. Verificación rápida (`verify.sh`)

- Contenedores arriba y sanos.
- Panel respondiendo en `:8120`.
- Rutas de cámara publicadas en MediaMTX (`ready`).
- Mosaico publicado y con fotogramas que cambian.
- Memoria del mosaico (debe quedarse plana; si crece, algo se atascó).
- Una sola ruta por defecto (la de internet).
- Estado del túnel, si está instalado.

---

## 9. Problemas conocidos (ya corregidos, pero conviene saberlos)

| Síntoma | Causa | Estado |
|---|---|---|
| El mosaico se congela aunque las cámaras sigan | `xstack` exige un fotograma de **todas** las entradas | Corregido: composición con `overlay` |
| El servidor se cuelga cuando cae la antena | La celda "sin señal" decodificaba a máxima velocidad (+40 MB/min) → OOM → swap | Corregido: `-re` en esa entrada; RSS plano |
| El mosaico usaba 525 MB | 16 hilos de decodificación por cámara | Corregido: `-threads 2` → ~180 MB |
| Tras un apagón el mosaico quedaba congelado 30 s | El watchdog solo miraba `ready` | Corregido: mira bytes publicados, RSS y rearma cámaras |
| Al volver una cámara no reaparecía | La lista se calculaba solo al pedir el mosaico | Corregido: rearme automático |
| Una cámara 4:3 (p. ej. `.32`, 704×576) no muestra el título ni la fecha/hora | El recorte a 16:9 se come el 15,6 % de arriba y de abajo, justo donde el fabricante pone su OSD | Corregido: CamWay pinta la fecha/hora en esa celda (`MOSAIC_CLOCK=auto`) |
| El reloj salía **duplicado** (dos fechas superpuestas) | La cámara 16:9 ya quema el suyo y el recorte no lo toca | Corregido: con `auto` solo se pinta donde el recorte borra el de la cámara |
