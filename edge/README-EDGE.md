# CamWay Edge — instalación en un taladro (Windows)

Pila completa de CamWay (api + MediaMTX + detector IA) para correr **en el PC del
taladro**, con 4 cámaras y el **mosaico en vídeo en vivo** (fluido), que al
maximizar sube a 720p.

## Requisitos
- Windows 10/11 con **Docker Desktop** instalado y **abierto** (que diga *Running*).
- Las **4 cámaras en el mismo switch/segmento** que el PC (misma subred). Es lo
  más importante: si una cámara queda en otra subred (otro router con NAT), no se
  descubre.
- Internet (Starlink) la primera vez, para bajar imágenes y modelos.

## Instalación (vía AnyDesk)
1. Copia este bundle (`camway-edge.zip`) al PC del taladro por **AnyDesk → Transferir archivos**.
2. Descomprímelo (clic derecho → *Extraer todo*), p.ej. en `C:\CamWay`.
3. Abre **PowerShell** en esa carpeta y ejecuta:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\edge\install-edge.ps1
   ```
4. El script detecta la IP, escribe la configuración, construye y levanta todo, y
   crea en el Escritorio el acceso **“CamWay (Kiosco)”**.

## Primer uso
1. Abre `http://localhost:8120` (o el acceso de kiosco). Crea el usuario **admin**.
2. **Escanea** el segmento (el que muestra el instalador) o agrega las 4 cámaras a
   mano, y **publícalas**.
3. Para que el mosaico pese poco, a cada cámara 3K/5MP ponle su **substream** como
   flujo ligero (botón de calidad / substream). El flujo grande solo se usa al
   maximizar.

## Modo del mosaico
- Arranca en **🎬 Vídeo en vivo** (todas las cámaras fluidas).
- Con el botón **Mosaico** cambias a **🎞️ Fotogramas** (más ligero) y viceversa.
  La elección se recuerda en ese navegador.

## Operación
- Parar:  `docker compose down`
- Arrancar: `docker compose up -d`
- Logs:  `docker compose logs -f api`
- Que arranque solo con Windows: Docker Desktop → *Settings → Start Docker Desktop
  when you log in*, y la pila está con `restart: unless-stopped`.

## Ver el taladro desde la oficina (remoto)
Starlink usa CGNAT (sin IP pública entrante), así que para ver este taladro desde
la oficina hace falta una **malla** (Tailscale) o un **hub** — es un paso aparte
del de esta instalación. Localmente (en el PC del taladro) funciona sin nada de eso.
