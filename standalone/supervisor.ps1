# Supervisor del paquete standalone de CamWay (sin Docker), para taladros.
#
# Vigila camway-server.exe y mediamtx.exe: si alguno cae (crash, OOM, lo que
# sea), lo vuelve a levantar solo, sin esperar a que alguien note la pantalla
# negra. Corre en un bucle infinito que NUNCA termina; la propia Tarea
# Programada que lo invoca (ver install-supervisor.ps1) lo reinicia si el
# supervisor mismo muere. Dos capas de resiliencia, cero dependencias
# externas (solo PowerShell, que ya trae Windows).
#
# Se asume que este script vive junto a camway-server.exe, mediamtx.exe,
# mediamtx.yml y ffmpeg.exe (la misma carpeta del paquete standalone).

param(
    [string]$Root = $PSScriptRoot
)

$ErrorActionPreference = "Continue"
$logFile = Join-Path $Root "supervisor.log"

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    try { Add-Content -Path $logFile -Value $line -Encoding utf8 } catch {}
    Write-Host $line
    # El log no debe crecer sin tope en un disco chico de taladro.
    try {
        if ((Get-Item $logFile -ErrorAction SilentlyContinue).Length -gt 2MB) {
            $tail = Get-Content $logFile -Tail 2000
            Set-Content -Path $logFile -Value $tail -Encoding utf8
        }
    } catch {}
}

function Get-HostIp {
    try {
        $ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object {
                $_.IPAddress -notlike "127.*" -and
                $_.IPAddress -notlike "169.254.*" -and
                $_.PrefixOrigin -ne "WellKnown"
            } |
            Sort-Object SkipAsSource, InterfaceMetric |
            Select-Object -First 1).IPAddress
    } catch { $ip = $null }
    if (-not $ip) { $ip = "127.0.0.1" }
    return $ip
}

$hostIp = Get-HostIp
$parts = $hostIp.Split(".")
$subnet = if ($parts.Count -eq 4) { "$($parts[0]).$($parts[1]).$($parts[2]).0/24" } else { "192.168.1.0/24" }

$dataDir = Join-Path $Root "data"
$recDir = Join-Path $Root "rec"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
New-Item -ItemType Directory -Force -Path $recDir | Out-Null

$env:CAMWAY_PORT = "8120"
$env:CAMWAY_DB = (Join-Path $dataDir "camway.db") -replace "\\", "/"
$env:CAMWAY_RECORD_DIR = $recDir -replace "\\", "/"
$env:CAMWAY_MTX_API = "http://127.0.0.1:9997"
$env:CAMWAY_MTX_RTSP = "rtsp://127.0.0.1:8554"
$env:CAMWAY_MTX_WEBRTC = "http://${hostIp}:8889"
$env:CAMWAY_MTX_WEBRTC_INTERNAL = "http://127.0.0.1:8889"
$env:CAMWAY_MTX_PLAYBACK = "http://127.0.0.1:9996"
$env:CAMWAY_DEFAULT_CIDR = $subnet
$env:CAMWAY_MOSAIC_MODE = "live"
$env:CAMWAY_MAX_BITRATE = "800k"
$env:CAMWAY_WALL_FPS = "4"
$env:MTX_WEBRTCADDITIONALHOSTS = $hostIp
$env:Path = "$Root;$env:Path"

Write-Log "=== Supervisor iniciado === IP=$hostIp CIDR=$subnet raiz=$Root"

function Start-Watched($name, $exeName, $args) {
    $exePath = Join-Path $Root $exeName
    if (-not (Test-Path $exePath)) {
        Write-Log "ERROR: no existe $exePath"
        return $null
    }
    try {
        if ($args) {
            $p = Start-Process -FilePath $exePath -ArgumentList $args -WorkingDirectory $Root -WindowStyle Hidden -PassThru
        } else {
            $p = Start-Process -FilePath $exePath -WorkingDirectory $Root -WindowStyle Hidden -PassThru
        }
        Write-Log "$name iniciado (PID $($p.Id))"
        return $p
    } catch {
        Write-Log "ERROR al iniciar $name : $_"
        return $null
    }
}

$procMtx = $null
$procApi = $null
$restartsMtx = 0
$restartsApi = 0

while ($true) {
    if (-not $procMtx -or $procMtx.HasExited) {
        if ($procMtx -and $procMtx.HasExited) {
            $restartsMtx++
            Write-Log "MediaMTX caido (codigo $($procMtx.ExitCode)). Reinicio numero $restartsMtx"
        }
        $procMtx = Start-Watched "MediaMTX" "mediamtx.exe" "mediamtx.yml"
        Start-Sleep -Seconds 2
    }

    if (-not $procApi -or $procApi.HasExited) {
        if ($procApi -and $procApi.HasExited) {
            $restartsApi++
            Write-Log "CamWay Server caido (codigo $($procApi.ExitCode)). Reinicio numero $restartsApi"
        }
        $procApi = Start-Watched "CamWay Server" "camway-server.exe" ""
    }

    Start-Sleep -Seconds 5
}
