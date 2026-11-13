# CamWay - supervisor (Windows, sin Docker). Lo lanza la Tarea Programada "CamWay Supervisor" al arrancar Windows, como SYSTEM.
#
# - Lee config\camway.env en CADA arranque de proceso (cambiar la configuracion = reiniciar el supervisor o esperar a un reinicio).
# - Mantiene vivos mediamtx.exe y camway-server.exe: si uno cae, lo relanza en segundos.
# - Ademas de "esta vivo", comprueba que RESPONDA (http://127.0.0.1:8120/api/health) y lo reinicia si deja de responder.
# - Si cambia la IP del equipo (DHCP), reinicia ambos para anunciar la nueva (WebRTC).
# - Guarda la salida de cada programa en logs\ (con rotacion) y sus propios eventos en logs\supervisor.log.
# Solo ASCII a proposito: PowerShell 5.1 lee mal los acentos de scripts sin BOM.

param([string]$Root = $PSScriptRoot)

$ErrorActionPreference = "Continue"
$logDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$supLog = Join-Path $logDir "supervisor.log"

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    try { Add-Content -Path $supLog -Value $line -Encoding utf8 } catch {}
    try {
        if ((Get-Item $supLog -ErrorAction SilentlyContinue).Length -gt 2MB) {
            $tail = Get-Content $supLog -Tail 2000
            Set-Content -Path $supLog -Value $tail -Encoding utf8
        }
    } catch {}
}

function Read-Config {
    $cfg = [ordered]@{}
    $f = Join-Path $Root "config\camway.env"
    if (Test-Path $f) {
        foreach ($ln in Get-Content $f -Encoding utf8) {
            $t = $ln.Trim()
            if (-not $t -or $t.StartsWith("#")) { continue }
            $i = $t.IndexOf("=")
            if ($i -lt 1) { continue }
            $cfg[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim()
        }
    }
    return $cfg
}

function Get-AutoIp {
    try {
        $cfgs = Get-NetIPConfiguration -ErrorAction Stop | Where-Object { $_.IPv4Address }
        $gw = $cfgs | Where-Object { $_.IPv4DefaultGateway } | Select-Object -First 1
        if ($gw) { return $gw.IPv4Address[0].IPAddress }
        $any = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.PrefixOrigin -ne "WellKnown" } |
            Sort-Object SkipAsSource, InterfaceMetric | Select-Object -First 1
        if ($any) { return $any.IPAddress }
    } catch {}
    return "127.0.0.1"
}

function Rotate-Log($path) {
    try {
        if ((Test-Path $path) -and (Get-Item $path).Length -gt 5MB) {
            Move-Item -Force $path "$path.1"
        }
    } catch {}
}

function Stop-Tree($proc) {
    if (-not $proc) { return }
    try { & taskkill.exe /T /F /PID $proc.Id 2>&1 | Out-Null } catch {}
}

# Si el supervisor se reinicia, puede haber procesos huerfanos de una corrida anterior: se limpian (solo los de esta carpeta).
function Stop-Orphans {
    foreach ($n in "camway-server.exe", "mediamtx.exe", "ffmpeg.exe") {
        Get-CimInstance Win32_Process -Filter "Name='$n'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase) } |
            ForEach-Object { try { & taskkill.exe /T /F /PID $_.ProcessId 2>&1 | Out-Null } catch {} }
    }
}

function Set-Env($cfg, $hostIp) {
    $dataDir = Join-Path $Root "data"
    $recDir = Join-Path $Root "rec"
    New-Item -ItemType Directory -Force -Path $dataDir, $recDir, (Join-Path $dataDir "backups") | Out-Null
    $parts = $hostIp.Split(".")
    $cidr = if ($cfg["SCAN_CIDR"]) { $cfg["SCAN_CIDR"] } elseif ($parts.Count -eq 4) { "$($parts[0]).$($parts[1]).$($parts[2]).0/24" } else { "192.168.1.0/24" }
    $fwd = { param($p) $p -replace "\\", "/" }

    # valores por omision (lo que decia start.bat) ...
    $e = [ordered]@{
        CAMWAY_PORT                 = "8120"
        CAMWAY_DB                   = (& $fwd (Join-Path $dataDir "camway.db"))
        CAMWAY_BACKUP_DIR           = (& $fwd (Join-Path $dataDir "backups"))
        CAMWAY_RECORD_DIR           = (& $fwd $recDir)
        CAMWAY_MTX_API              = "http://127.0.0.1:9997"
        CAMWAY_MTX_RTSP             = "rtsp://127.0.0.1:8554"
        CAMWAY_MTX_WEBRTC           = "http://${hostIp}:8889"
        CAMWAY_MTX_WEBRTC_INTERNAL  = "http://127.0.0.1:8889"
        CAMWAY_MTX_PLAYBACK         = "http://127.0.0.1:9996"
        CAMWAY_DEFAULT_CIDR         = $cidr
        CAMWAY_MOSAIC_MODE          = "live"
        CAMWAY_MAX_BITRATE          = "800k"
        CAMWAY_WALL_FPS             = "4"
        MTX_WEBRTCADDITIONALHOSTS   = $hostIp
    }
    # ... y encima lo que diga config\camway.env (CAMWAY_*, MTX_*): permite ajustar sin tocar este script
    foreach ($k in $cfg.Keys) {
        if ($k -like "CAMWAY_*" -or $k -like "MTX_*") { $e[$k] = $cfg[$k] }
    }
    # el relevo se quita si ya no esta en la configuracion (no queda pegado de una corrida anterior)
    Get-ChildItem Env: | Where-Object { $_.Name -like "CAMWAY_RELAY_*" -and -not $e.Contains($_.Name) } | ForEach-Object { Remove-Item -Path "Env:$($_.Name)" -ErrorAction SilentlyContinue }
    foreach ($k in $e.Keys) { Set-Item -Path "Env:$k" -Value $e[$k] }
    $env:Path = "$Root;$env:Path"          # ffmpeg.exe (incluido) a la vista de MediaMTX y del relevo
}

function Start-Watched($name, $exe, $arguments, $logName) {
    $exePath = Join-Path $Root $exe
    if (-not (Test-Path $exePath)) { Write-Log "ERROR: no existe $exePath"; return $null }
    $out = Join-Path $logDir "$logName.log"
    $err = Join-Path $logDir "$logName.err.log"
    Rotate-Log $out; Rotate-Log $err
    try {
        $p = if ($arguments) {
            Start-Process -FilePath $exePath -ArgumentList $arguments -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
        } else {
            Start-Process -FilePath $exePath -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
        }
        Write-Log "$name iniciado (PID $($p.Id))"
        return $p
    } catch {
        Write-Log "ERROR al iniciar $name : $_"
        return $null
    }
}

function Test-Health {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8120/api/health" -UseBasicParsing -TimeoutSec 5
        $j = $r.Content | ConvertFrom-Json
        return @{ api = $true; mtx = [bool]$j.mediamtx }
    } catch { return @{ api = $false; mtx = $false } }
}

Stop-Orphans
$cfg = Read-Config
$hostIp = if ($cfg["HOST_IP"]) { $cfg["HOST_IP"] } else { Get-AutoIp }
Write-Log "=== Supervisor iniciado === IP=$hostIp raiz=$Root"
Set-Env $cfg $hostIp

$procMtx = $null; $procApi = $null
$restartsMtx = 0; $restartsApi = 0
$apiStarted = Get-Date
$badApi = 0; $badMtx = 0
$lastIpCheck = Get-Date; $lastHealth = Get-Date

while ($true) {
    if (-not $procMtx -or $procMtx.HasExited) {
        if ($procMtx) { $restartsMtx++; Write-Log "MediaMTX caido (codigo $($procMtx.ExitCode)). Reinicio numero $restartsMtx" }
        $procMtx = Start-Watched "MediaMTX" "mediamtx.exe" "mediamtx.yml" "mediamtx"
        $badMtx = 0
        Start-Sleep -Seconds 2
    }
    if (-not $procApi -or $procApi.HasExited) {
        if ($procApi) { $restartsApi++; Write-Log "CamWay Server caido (codigo $($procApi.ExitCode)). Reinicio numero $restartsApi" }
        $cfg = Read-Config                                  # tomar de nuevo la configuracion (por ejemplo, el relevo recien agregado)
        Set-Env $cfg $hostIp
        $procApi = Start-Watched "CamWay Server" "camway-server.exe" "" "camway-server"
        $apiStarted = Get-Date; $badApi = 0
    }

    # La IP del equipo cambio (DHCP): se reinician ambos para anunciar la nueva en WebRTC.
    if (((Get-Date) - $lastIpCheck).TotalSeconds -ge 60) {
        $lastIpCheck = Get-Date
        $cfg2 = Read-Config
        if (-not $cfg2["HOST_IP"]) {
            $now = Get-AutoIp
            if ($now -and $now -ne "127.0.0.1" -and $now -ne $hostIp) {
                Write-Log "La IP cambio ($hostIp -> $now): se reinician los servicios"
                $hostIp = $now
                Stop-Tree $procApi; Stop-Tree $procMtx
                $cfg = $cfg2; Set-Env $cfg $hostIp
                $procApi = $null; $procMtx = $null
                continue
            }
        }
    }

    # "Vivo" no basta: tambien tiene que RESPONDER. Con 4 fallos seguidos (~2 min) se reinicia lo que no responde.
    if (((Get-Date) - $lastHealth).TotalSeconds -ge 30 -and ((Get-Date) - $apiStarted).TotalSeconds -ge 90) {
        $lastHealth = Get-Date
        $h = Test-Health
        if ($h.api) { $badApi = 0 } else { $badApi++ }
        if ($h.api -and -not $h.mtx) { $badMtx++ } else { $badMtx = 0 }
        if ($badApi -ge 4) { Write-Log "CamWay Server no responde: se reinicia"; Stop-Tree $procApi; $procApi = $null; $badApi = 0 }
        elseif ($badMtx -ge 4) { Write-Log "MediaMTX no responde: se reinicia"; Stop-Tree $procMtx; $procMtx = $null; $badMtx = 0 }
    }

    Start-Sleep -Seconds 5
}
