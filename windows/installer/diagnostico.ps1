# CamWay - diagnostico para soporte remoto (AnyDesk). No cambia nada. Muestra el estado y guarda un resumen en diagnostico.txt
param([string]$InstallDir = $PSScriptRoot)
$ErrorActionPreference = "Continue"
$out = New-Object System.Collections.ArrayList
function P($t = "") { Write-Host $t; [void]$out.Add($t) }
P "=== Diagnostico de CamWay $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
P "Equipo: $env:COMPUTERNAME   Carpeta: $InstallDir"
if (Test-Path (Join-Path $InstallDir "VERSION")) { P "Version: $((Get-Content (Join-Path $InstallDir 'VERSION') -Raw).Trim())" }
P ""; P "--- Servicio (Tarea Programada) ---"
$t = Get-ScheduledTask -TaskName "CamWay Supervisor" -ErrorAction SilentlyContinue
if ($t) { $i = Get-ScheduledTaskInfo -TaskName "CamWay Supervisor"; P "Estado: $($t.State)  Ultimo resultado: $($i.LastTaskResult)  Ultima ejecucion: $($i.LastRunTime)" } else { P "NO existe la tarea 'CamWay Supervisor' (no arranca sola con Windows)" }
P ""; P "--- Procesos ---"
foreach ($n in "camway-server", "mediamtx", "ffmpeg") {
    $ps = @(Get-Process -Name $n -ErrorAction SilentlyContinue)
    P ("{0,-15} {1}" -f $n, $(if ($ps.Count) { "en marcha ($($ps.Count))  PID $(($ps | ForEach-Object Id) -join ',')  memoria $([math]::Round(($ps | Measure-Object WorkingSet64 -Sum).Sum / 1MB)) MB" } else { "NO esta corriendo" }))
}
P ""; P "--- Respuesta ---"
try { $h = (Invoke-WebRequest -Uri "http://127.0.0.1:8120/api/health" -UseBasicParsing -TimeoutSec 5).Content | ConvertFrom-Json; P "CamWay API: OK   MediaMTX: $($h.mediamtx)   Camaras: $($h.devices)   Disco libre: $($h.disk_free_pct)%   Ultimo respaldo: $($h.last_backup_age_s) s" } catch { P "CamWay API: NO RESPONDE ($($_.Exception.Message))" }
P ""; P "--- Red ---"
Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } | ForEach-Object { P "  $($_.IPAddress)/$($_.PrefixLength)  ($($_.InterfaceAlias))" }
P ""; P "--- Configuracion (sin secretos) ---"
$cfg = Join-Path $InstallDir "config\camway.env"
if (Test-Path $cfg) { Get-Content $cfg | Where-Object { $_ -and -not $_.StartsWith("#") } | ForEach-Object { $k = ($_ -split "=", 2)[0]; $v = ($_ -split "=", 2)[1]; if ($k -match "KEY|PASS|SECRET") { $v = "(oculto)" }; P "  $k=$v" } } else { P "  no existe $cfg" }
$relayOn = (Get-Content $cfg -ErrorAction SilentlyContinue | Where-Object { $_ -like "CAMWAY_RELAY_CC=*" })
P "Relevo hacia el Command Center: $(if ($relayOn) { 'CONFIGURADO' } else { 'no configurado' })"
P ""; P "--- Ultimas lineas de los registros ---"
foreach ($f in "supervisor.log", "camway-server.err.log", "camway-server.log", "mediamtx.err.log") {
    $lf = Join-Path $InstallDir "logs\$f"
    if ((Test-Path $lf) -and (Get-Item $lf).Length -gt 0) { P ""; P "[$f]"; Get-Content $lf -Tail 12 | ForEach-Object { P "  $_" } }
}
$o = Join-Path $InstallDir "diagnostico.txt"
try { Set-Content -Path $o -Value $out -Encoding utf8; Write-Host ""; Write-Host "Resumen guardado en $o" -ForegroundColor Green } catch {}
Read-Host "Pulsa Enter para cerrar" | Out-Null
