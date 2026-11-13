# Supervisor de proceso — paquete standalone (sin Docker)

Resuelve el hallazgo crítico de la auditoría de producción: si `camway-server.exe`
o `mediamtx.exe` truenan (crash, OOM, lo que sea) en un taladro sin personal
técnico, nadie los reiniciaba — el video quedaba muerto indefinidamente.

## Qué hace

- `supervisor.ps1`: vigila ambos procesos cada 5 segundos; si alguno no está
  corriendo, lo relanza con las mismas variables de entorno que usaba
  `start.bat` (detecta la IP LAN solo). Lleva un log con cada reinicio en
  `supervisor.log` (se recorta solo si pasa de 2 MB, para no llenar el disco).
- `install-supervisor.ps1`: registra `supervisor.ps1` como Tarea Programada de
  Windows que arranca **con el sistema** (antes de iniciar sesión), bajo la
  cuenta `SYSTEM`, con reinicio automático de la propia tarea si llegara a
  morir (hasta 999 veces, cada 1 minuto). Dos capas: el supervisor reinicia
  los `.exe`, y el Programador de tareas reinicia al supervisor.

Probado en vivo: matar el proceso (o todo su árbol, típico de un `.exe`
empaquetado con PyInstaller que corre como padre+hijo) → el supervisor lo
detecta y relanza en ~5-8 segundos.

## Instalación en un taladro nuevo

1. Copia esta carpeta (`supervisor.ps1`, `install-supervisor.ps1`) **dentro**
   de la misma carpeta donde ya está `camway-server.exe`, `mediamtx.exe`,
   `mediamtx.yml` y `ffmpeg.exe` (el paquete standalone, p. ej. `C:\CamWay` o
   `C:\DWWEB`).
2. Si ya tenías CamWay corriendo a mano (vía `start.bat`), ciérralo:
   `taskkill /IM camway-server.exe /F` y `taskkill /IM mediamtx.exe /F`.
3. Clic derecho en `install-supervisor.ps1` → **Ejecutar con PowerShell**
   (pide permisos de administrador la primera vez). El supervisor arranca
   solo y queda registrado para sobrevivir a reinicios/cortes de luz.
4. Ya NO hace falta `start.bat` ni un acceso directo en Inicio — el
   supervisor levanta todo solo desde el arranque de Windows, incluso antes
   de que alguien inicie sesión.

## Diagnóstico remoto

`supervisor.log` (en la misma carpeta) muestra cada arranque y cada
reinicio con fecha/hora — si un taladro reporta "se fue el video", ese log
dice si fue un crash-loop (reinicios repetidos, hay que investigar la causa
real) o si simplemente nunca arrancó (revisar que los `.exe` existan en la
ruta esperada).

## Pendiente de este mismo hallazgo (no cubierto aquí)

La auditoría también marcó la app Android TV sin supervisor/watchdog de
crash — es un problema análogo pero en Android, se resuelve aparte en
`tvapp/` (no en este paquete Windows).
