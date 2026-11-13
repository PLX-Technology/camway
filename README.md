# CamWay

Descubre las cámaras de una red y las muestra en un mosaico de video en vivo.

Barre el segmento buscando puertos RTSP, interroga cada equipo que encuentra
para saber qué es y qué entrega, y publica los streams elegidos como WebRTC
de baja latencia (~0,5 s) para verlos todos en una rejilla.

## Cómo levantarlo

```bash
cp .env.example .env     # ajusta HOST_IP y SCAN_CIDR
docker compose up -d --build
```

Luego abre <http://localhost:8120>.

`HOST_IP` tiene que ser la IP real del equipo en la LAN, no `127.0.0.1`: la usa
el navegador para hablar con MediaMTX y la usa MediaMTX para anunciar su
candidato ICE. Con `127.0.0.1` el mosaico no conecta desde otra máquina.

## Cómo funciona

```
                      ┌──────────────┐
  cámaras / DVR ─────▶│   MediaMTX   │──── WebRTC ────▶ navegador (mosaico)
   (RTSP)             │              │
                      └──────▲───────┘
                             │ API de control (crea y borra rutas)
                      ┌──────┴───────┐
                      │  CamWay API  │──── barrido + huella RTSP ──▶ la red
                      │  (FastAPI)   │
                      └──────────────┘
```

MediaMTX abre **una sola** sesión RTSP contra cada cámara y la reparte a todos
los espectadores. Eso es lo que evita agotar el límite de sesiones simultáneas
de un DVR cuando varias personas abren el mosaico a la vez. Además las rutas
son *on demand*: si nadie mira, no se tira del equipo.

### El descubrimiento

1. **Barrido TCP** del segmento en los puertos RTSP (554, 8554, 10554, 5554).
   Es el camino principal porque funciona igual dentro de un contenedor.
2. **`OPTIONS`** contra cada puerto abierto: confirma que hay un RTSP vivo y
   devuelve el banner del fabricante.
3. **`DESCRIBE`** sobre las rutas conocidas de ese fabricante. El SDP dice el
   códec real; el SPS de H.264 da la resolución real.
4. **Deduplicado por huella del SDP**, porque muchos equipos sirven el mismo
   video en varias rutas distintas.

El descubrimiento ONVIF por multicast (`/api/onvif`) existe como atajo, pero
es un extra: el multicast no cruza subredes y casi nunca llega dentro de
Docker. Si no contesta nadie, no es un fallo.

## Detalles que costaron sangre

**Equipos con rutas permisivas.** Las Grandstream devuelven `200 OK` para
*cualquier* ruta inventada: `/live`, `/stream1` y `/loquesea` sirven todas el
stream 0. Probar una lista genérica contra ellas produce seis copias del mismo
video. CamWay detecta el caso pidiendo primero una ruta que no existe; si
contesta `200`, solo se fía de las rutas documentadas del fabricante.

**La cabecera del equipo miente.** Una Grandstream anuncia su substream como
`480*272*15*256` (256 kbps) y en realidad entrega **1.708 kbps** medidos,
porque es MJPEG y no tiene compresión entre cuadros. Por eso la resolución se
lee del SPS y el códec del `rtpmap`, nunca del banner.

**WebRTC solo admite H.264.** MJPEG y H.265 hay que transcodificarlos, y eso
cuesta del orden de un tercio de núcleo por cámara. CamWay lo hace solo cuando
el códec obliga, y lo avisa al publicar. Si tus cámaras tienen el substream en
MJPEG, sale mucho más barato reconfigurarlo a H.264 desde la web del equipo
que transcodificar.

**La API de MediaMTX pide autenticación desde la 1.21** y de fábrica solo la
acepta desde localhost, así que un contenedor vecino recibe `401`. Aquí se
resuelve por origen en vez de con una credencial: la red de compose tiene
subred fija (`172.28.0.0/24`), `mediamtx.yml` autoriza la acción `api` solo a
ese rango, y **el puerto 9997 no se publica al host**. Crear y borrar rutas
queda fuera del alcance de la LAN.

**WebRTC detrás de Docker bridge.** MediaMTX solo ve su IP interna y la anuncia
como candidata ICE; el navegador no la alcanza. Hay dos piezas: publicar el
puerto UDP `8189` (multiplexor ICE) y declararle la IP del host con
`MTX_WEBRTCADDITIONALHOSTS`. Faltando cualquiera de las dos, la señalización
funciona pero el video nunca llega.

**Las rutas de MediaMTX son efímeras.** Se crean por su API de control y viven
solo en memoria; un reinicio de MediaMTX o del host las borra, pero la base de
CamWay sigue marcando los streams como publicados. El síntoma es `error: WHEP
400` en todo el mosaico tras un reinicio. CamWay se auto-repara: al arrancar el
`api` espera a que MediaMTX responda y reempuja todas las rutas de los streams
marcados como publicados (`_reconcile_on_start`). También hay
`POST /api/reconcile` para forzarlo a mano. Ojo: solo se dispara al arrancar el
`api`; si se reinicia únicamente MediaMTX, hay que llamar a `/api/reconcile`.

## API

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/api/health` | Estado de la API y de MediaMTX |
| `POST` | `/api/scan` | Lanza el barrido (`{"cidr": "...", "channels": 8}`) |
| `GET` | `/api/scan/events` | Progreso en vivo por SSE |
| `POST` | `/api/scan/stop` | Detiene el barrido en curso |
| `POST` | `/api/probe` | Interroga un equipo suelto |
| `GET` | `/api/devices` | Inventario guardado |
| `PATCH` | `/api/devices/{id}` | Renombrar / asignar sitio |
| `DELETE` | `/api/devices/{id}` | Olvidar un equipo |
| `POST` | `/api/streams/{id}/publish` | Publica el stream en MediaMTX |
| `DELETE` | `/api/streams/{id}/publish` | Lo retira |
| `GET` | `/api/streams/{id}/snapshot` | Fotograma JPEG (cacheado 10 s) |
| `GET` | `/api/wall` | Lo que consume el mosaico |
| `GET` | `/api/onvif` | Descubrimiento por multicast (opcional) |

## Credenciales

Los juegos usuario/clave se cargan en **Ajustes** y se guardan en la base
SQLite local. Al escanear, cada equipo que responda `401` se prueba con ellos
en orden hasta que uno funcione. No hay lista de claves por defecto
incorporada: solo se prueba lo que tú cargues.

## Límites actuales

- El barrido tiene un tope de **8192 direcciones** por escaneo. Un `/19`
  entero entra justo; un `/16` hay que partirlo.
- Un solo escaneo a la vez.
- Resolución por SPS solo para H.264. En H.265 se reporta el códec sin
  resolución.
- El inventario es de un único sitio. Para varios urbanismos hace falta el
  campo `site` (ya está en la base) y una instancia por LAN reportando a un
  central por VPN.

## Siguiente paso natural

Para el escenario multi-sitio: una instancia de CamWay por urbanismo, cada una
escaneando su propia LAN, con WireGuard hacia un servidor central que agregue
los inventarios. El campo `site` de la tabla `devices` ya está previsto para
eso.
