# CamWay Edge - instalador para taladro (Windows + Docker Desktop).
# Uso en PowerShell, con Docker Desktop ABIERTO:
#   powershell -ExecutionPolicy Bypass -File .\edge\install-edge.ps1

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
if (-not (Test-Path (Join-Path $root "docker-compose.yml"))) { $root = $here }
Set-Location $root
Write-Host "== CamWay Edge - instalacion en $root ==" -ForegroundColor Cyan

# 1) Docker disponible y corriendo
try { docker info *> $null } catch {
  Write-Host "ERROR: Docker Desktop no responde. Abrelo, espera a Running, y reintenta." -ForegroundColor Red
  exit 1
}

# 2) IP LAN del equipo
$ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.PrefixOrigin -ne "WellKnown" } |
  Sort-Object -Property SkipAsSource, InterfaceMetric |
  Select-Object -First 1).IPAddress
if (-not $ip) { $ip = Read-Host "No detecte la IP LAN. Escribela, ejemplo 192.168.1.50" }
$cidr = ($ip -replace '\.\d+$', '.0') + "/24"
Write-Host "IP LAN detectada: $ip   segmento de escaneo: $cidr" -ForegroundColor Green
$ans = Read-Host "Enter para aceptar, o escribe otra IP LAN"
if ($ans) { $ip = $ans; $cidr = ($ip -replace '\.\d+$', '.0') + "/24" }

# 3) Escribir .env
$envPath = Join-Path $root ".env"
$envLines = @(
  "# Generado por install-edge.ps1",
  "HOST_IP=$ip",
  "SCAN_CIDR=$cidr",
  "# Mosaico en VIDEO EN VIVO por defecto. Cambiable desde la interfaz.",
  "CAMWAY_MOSAIC_MODE=live",
  "WALL_FPS=4",
  "# Calidad del flujo grande (vivo al maximizar y grabacion). 720p.",
  "CAMWAY_MAX_WIDTH=1280",
  "CAMWAY_MAX_HEIGHT=720",
  "CAMWAY_MAX_FPS=15",
  "# Tope de bitrate: hace la grabacion predecible. ~0.8 Mbps/camara => 4",
  "# camaras caben ~21 dias continuos en 1 TB. En la UI, retencion = 21 dias.",
  "CAMWAY_MAX_BITRATE=800k"
)
$envLines | Set-Content -Encoding utf8 $envPath
Write-Host "Escrito $envPath" -ForegroundColor Green

# 4) Construir y levantar
Write-Host "Construyendo imagenes. La primera vez tarda y baja dependencias." -ForegroundColor Cyan
docker compose build
Write-Host "Levantando la pila..." -ForegroundColor Cyan
docker compose up -d

# 5) Acceso directo de kiosco
$url = "http://localhost:8120"
$cands = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
           "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe")
$chrome = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($chrome) {
  $args = "--kiosk --start-fullscreen --autoplay-policy=no-user-gesture-required $url"
  $ws = New-Object -ComObject WScript.Shell
  # En el Escritorio y TAMBIEN en Inicio, para que el kiosco vuelva solo tras un reinicio.
  $targets = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "CamWay Kiosco.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Startup")) "CamWay Kiosco.lnk")
  )
  foreach ($lnk in $targets) {
    $sc = $ws.CreateShortcut($lnk)
    $sc.TargetPath = $chrome
    $sc.Arguments = $args
    $sc.Save()
  }
  Write-Host "Acceso de kiosco creado en el Escritorio y en Inicio (arranca solo tras reiniciar)." -ForegroundColor Green
} else {
  Write-Host "Chrome no encontrado. Abre $url en tu navegador." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "== LISTO ==" -ForegroundColor Cyan
Write-Host "CamWay esta en: $url"
Write-Host "Paso 1: abre esa direccion y crea el usuario admin la primera vez."
Write-Host "Paso 2: escanea el segmento $cidr o agrega las 4 camaras y publicalas."
Write-Host "Paso 3: el mosaico arranca en VIDEO EN VIVO; al maximizar sube a 720p."
Write-Host "Parar: docker compose down   Logs: docker compose logs -f api"
Write-Host ""
Write-Host "== PERSISTENCIA / APAGONES ==" -ForegroundColor Cyan
Write-Host "La config vive en el volumen Docker 'camway-data' (SQLite WAL) y hay respaldo"
Write-Host "diario en /data/backups. NO se pierde al reiniciar. Para que vuelva solo:"
Write-Host " - Docker Desktop: Settings > General > 'Start Docker Desktop when you log in'."
Write-Host " - Windows: configura inicio de sesion automatico (netplwiz) si la PC queda sola."
Write-Host " - RECOMENDADO: un UPS pequeno. Un corte en seco puede corromper el disco de WSL2."
