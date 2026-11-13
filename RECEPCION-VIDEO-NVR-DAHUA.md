# Recepción de video desde un NVR Dahua (para integrar en DrillWay)

Guía práctica de cómo CamWay recibe el video de un NVR Dahua y cómo replicarlo
en **DrillWay** para ver algunas cámaras. Pensado para un taladro, donde las
cámaras están en la **red aislada de los puertos PoE del NVR** y solo el NVR es
accesible desde la red del PC.

---

## 1. Modelo de acceso (lo más importante)

En estos NVR las cámaras **no son accesibles directamente**: cuelgan de los
puertos PoE del NVR, en una subred interna (p. ej. `10.x`). El único equipo que
se ve desde la LAN del taladro es **el NVR**.

El NVR **hace de puente**: expone cada cámara como un **canal** por su propio
servicio RTSP. Es decir:

```
PC / DrillWay  ──RTSP──►  NVR (192.168.1.22:554)  ──►  Cámara del canal N (red aislada)
```

No hace falta llegar a cada cámara ni escanear la red aislada: **se piden los
canales al NVR**.

---

## 2. URL RTSP (Dahua)

```
rtsp://USUARIO:CLAVE@IP_DEL_NVR:554/cam/realmonitor?channel=N&subtype=M
```

- **`USUARIO:CLAVE`** = credencial **RTSP** del NVR (suele ser la de admin; algunos
  NVR tienen un usuario RTSP aparte — revisar *Red → RTSP* o *Usuarios* en el NVR).
  Si la clave tiene caracteres especiales (`@`, `:`, `/`, `*`…), **URL-encodearla**.
- **`channel=N`** = número de canal/cámara en el NVR (1, 2, 3, …).
- **`subtype=0`** = **flujo principal** (alta resolución).
- **`subtype=1`** = **substream** (baja resolución; ideal para ver varias a la vez
  con poco consumo).
- Puerto **554** (RTSP estándar). Usar siempre **transporte TCP** (más estable que UDP).

Ejemplo (canal 1, substream):
```
rtsp://admin:Cl4veNVR@192.168.1.22:554/cam/realmonitor?channel=1&subtype=1
```

> **Otras marcas (referencia):**
> - **Hikvision:** `rtsp://user:pass@ip:554/Streaming/channels/<N>01` (principal) / `<N>02` (sub).
> - **Uniview:** `rtsp://user:pass@ip:554/unicast/c<N>/s0/live`.
> Si la ruta Dahua no responde, probar la de la marca real del NVR.

**Prueba rápida de una URL** (VLC → *Medio → Abrir ubicación de red*, o ffmpeg):
```bash
ffplay -rtsp_transport tcp "rtsp://admin:Cl4veNVR@192.168.1.22:554/cam/realmonitor?channel=1&subtype=0"
```

---

## 3. ⚠️ El detalle que rompe todo: el códec (H.265)

Muchos de estos NVR/cámaras entregan **H.265 (HEVC)**, sobre todo en el substream.

- **ffmpeg, VLC y reproductores nativos SÍ** reproducen H.265.
- **Los navegadores por WebRTC NO** reproducen H.265 (Chrome/Edge/WebView2).
  También HLS en H.265 tiene soporte muy limitado en navegador.

Por eso, para ver el video **en una web** (como DrillWay) hay que:

1. **Detectar el códec real** de cada canal, y
2. Si es **H.265 → transcodificar a H.264** (y el audio, si lo hay, a AAC/Opus).

A 704×576 (resolución típica del substream) transcodificar es **liviano**
(~0,1–0,2 de un núcleo por cámara). En principal (alta) pesa más.

**Detectar el códec con ffmpeg** (lo que hace CamWay):
```bash
ffmpeg -rtsp_transport tcp -i "rtsp://.../channel=1&subtype=1" -t 0.2 -f null -
# En la salida: "Stream #0:0: Video: hevc ..."  -> H.265 (hay que transcodificar)
#               "Stream #0:0: Video: h264 ..."  -> H.264 (se puede pasar tal cual)
```

---

## 4. Arquitectura recomendada para DrillWay

El patrón que usa CamWay (y el más robusto): un **gateway de medios** que jala el
RTSP del NVR y lo entrega al navegador en un formato web. Lo más simple es usar
**[MediaMTX](https://github.com/bluenviron/mediamtx)** (un solo binario, gratis):

```
NVR (RTSP/H.265) ──► ffmpeg (transcode a H.264) ──► MediaMTX ──► DrillWay (navegador)
                                                      │
                                        WebRTC (baja latencia, <1 s)  ó  HLS (simple, ~3-5 s)
```

### Opción A — MediaMTX (recomendada: baja latencia)

1. Instala MediaMTX (binario único, Windows/Linux).
2. Define un *path* por cámara. Para H.264 directo (sin transcode):
   ```yaml
   # mediamtx.yml
   paths:
     cam1:
       source: rtsp://admin:Cl4veNVR@192.168.1.22:554/cam/realmonitor?channel=1&subtype=0
       sourceProtocol: tcp
   ```
   Para **H.265 → transcode a H.264** (usa ffmpeg, se enciende bajo demanda):
   ```yaml
   paths:
     cam1:
       runOnDemand: >
         ffmpeg -rtsp_transport tcp
         -i rtsp://admin:Cl4veNVR@192.168.1.22:554/cam/realmonitor?channel=1&subtype=1
         -c:v libx264 -preset ultrafast -tune zerolatency -profile:v baseline -pix_fmt yuv420p -g 30
         -an -f rtsp rtsp://127.0.0.1:8554/cam1
       runOnDemandRestart: yes
       runOnDemandCloseAfter: 15s
   ```
3. En DrillWay, el navegador reproduce:
   - **WebRTC (WHEP):** `http://SERVIDOR:8889/cam1/whep` — latencia <1 s, lo mejor para “vivo”.
   - **HLS:** `http://SERVIDOR:8888/cam1/index.m3u8` — más simple, úsalo con `hls.js` en un `<video>`.

   Ejemplo HLS en el frontend:
   ```html
   <video id="v" controls autoplay muted></video>
   <script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.17"></script>
   <script>
     const hls = new Hls();
     hls.loadSource('http://SERVIDOR:8888/cam1/index.m3u8');
     hls.attachMedia(document.getElementById('v'));
   </script>
   ```

MediaMTX ya trae WebRTC, HLS y RTSP; solo defines los *paths*. Para varias
cámaras, repites el bloque (`cam1`, `cam2`, …).

### Opción B — Solo ffmpeg → HLS (mínimo, sin MediaMTX)

Si solo quieren ver 1–2 cámaras y evitar dependencias, ffmpeg puede escribir HLS
H.264 a una carpeta que DrillWay sirva como estático:

```bash
ffmpeg -rtsp_transport tcp -i "rtsp://admin:Cl4veNVR@192.168.1.22:554/cam/realmonitor?channel=1&subtype=1" \
  -c:v libx264 -preset veryfast -tune zerolatency -profile:v baseline -pix_fmt yuv420p -g 30 -an \
  -f hls -hls_time 1 -hls_list_size 4 -hls_flags delete_segments+append_list \
  /ruta/publica/cam1/index.m3u8
```
Y en DrillWay se reproduce `…/cam1/index.m3u8` con `hls.js` (igual que arriba).
Latencia ~3–6 s. Si el stream ya es **H.264**, cambia `-c:v libx264 …` por `-c:v copy`
(sin transcodificar, casi 0 CPU).

### Opción C — Fotograma JPEG (lo más barato, “casi en vivo”)

Para una miniatura o un refresco de ~1 fps (sin latencia de buffer), DrillWay
puede pedir un JPEG periódico:
```bash
ffmpeg -rtsp_transport tcp -i "rtsp://.../channel=1&subtype=1" -vf fps=1 -update 1 -q:v 5 /ruta/cam1.jpg
```
y el frontend recarga `cam1.jpg?t=<timestamp>` cada segundo. Útil para muchas
cámaras a la vez con consumo mínimo (es lo que CamWay usa para su muro).

---

## 5. Receta mínima para “ver alguna cámara” en DrillWay

1. **Averiguar la credencial RTSP** del NVR y confirmar una URL en VLC
   (`channel=1&subtype=0` y `subtype=1`).
2. **Detectar el códec** con el comando ffmpeg de la sección 3.
3. Elegir entrega:
   - Pocas cámaras, baja latencia → **MediaMTX + WebRTC** (Opción A).
   - Simple, tolera 3–5 s → **HLS** (Opción A-HLS o B).
   - Muro de muchas con poco consumo → **JPEG 1 fps** (Opción C).
4. Si el códec es **H.265**, meter el **transcode a H.264** (ffmpeg) en el paso de
   entrega. Si ya es H.264, pasar `-c:v copy` (sin CPU).

---

## 6. Gotchas (lecciones de CamWay)

- **H.265 ≠ web.** El síntoma clásico: el stream “conecta” pero el `<video>` queda
  negro/“reconectando”. Casi siempre es H.265 sin transcodificar. Verifícalo con
  ffmpeg o con `http://IP:9997/v3/paths/list` de MediaMTX (`tracks: ["H265",…]`).
- **Substream vs principal.** El `subtype=1` (sub) es chico → ideal para varias
  cámaras y para transcodificar barato. El `subtype=0` (principal) es alta
  resolución → úsalo solo al “maximizar” una cámara.
- **Canales apagados.** Si un canal no tiene cámara, su RTSP no responde: sondéalo
  y descártalo (no lo muestres como “conectando” para siempre).
- **Transporte TCP.** Forzar `-rtsp_transport tcp`; el UDP se corta en muchas redes.
- **Máximo de conexiones RTSP del NVR.** Los NVR limitan cuántas sesiones RTSP
  simultáneas sirven. Un gateway (MediaMTX/ffmpeg) abre **una** conexión por canal
  y la reparte a todos los visores → no abras una RTSP por cada usuario.
- **Credenciales en la URL.** Van dentro de la URL RTSP; guárdalas **cifradas** en
  el backend, nunca en el frontend ni en logs.
- **Audio.** Suele venir en G.711; para web transcodificar a AAC (HLS) u Opus
  (WebRTC), o simplemente descartarlo (`-an`) si no hace falta.

---

## 7. Resumen de una línea

> El NVR Dahua entrega cada cámara por **RTSP** en
> `/cam/realmonitor?channel=N&subtype=M`; para verlo en una web hay que
> **transcodificar H.265→H.264** y entregarlo por **WebRTC o HLS** (lo más fácil:
> **MediaMTX** como gateway). Una sola conexión al NVR por canal, repartida a todos.
