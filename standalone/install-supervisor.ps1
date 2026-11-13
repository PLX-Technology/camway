# Instala el supervisor de CamWay como Tarea Programada que arranca con
# Windows, ANTES de iniciar sesion, bajo la cuenta SYSTEM. Si el propio
# supervisor muere, el Programador de tareas lo reinicia (doble capa junto
# con el bucle interno de supervisor.ps1).
#
# Uso: clic derecho > "Ejecutar con PowerShell" (como administrador), o:
#   powershell -ExecutionPolicy Bypass -File install-supervisor.ps1

$ErrorActionPreference = "Stop"
$taskName = "CamWay Supervisor"
$scriptPath = Join-Path $PSScriptRoot "supervisor.ps1"

if (-not (Test-Path $scriptPath)) {
    Write-Host "No se encontro supervisor.ps1 junto a este instalador." -ForegroundColor Red
    exit 1
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Este instalador necesita permisos de administrador. Ejecuta PowerShell como administrador y vuelve a correrlo." -ForegroundColor Red
    exit 1
}

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Write-Host "Ya existe la tarea '$taskName'; se reemplaza con esta configuracion."
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`" -Root `"$PSScriptRoot`""

$trigger = New-ScheduledTaskTrigger -AtStartup

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Tarea '$taskName' creada. Iniciandola ahora..."
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 3

$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Host "Estado: $($info.LastTaskResult) (0 = sin errores reportados todavia)"
Write-Host ""
Write-Host "El supervisor corre en segundo plano permanentemente. Revisa su actividad en:"
Write-Host "  $(Join-Path $PSScriptRoot 'supervisor.log')"
Write-Host ""
Write-Host "IMPORTANTE: si antes usabas start.bat manualmente o un acceso directo en Inicio"
Write-Host "para levantar CamWay, ya no hace falta — el supervisor lo hace solo desde el arranque."
Write-Host "Si camway-server.exe o mediamtx.exe estan corriendo ahora mismo (lanzados a mano),"
Write-Host "ciérralos para que el supervisor tome control limpio: taskkill /IM camway-server.exe /F"
Write-Host "y taskkill /IM mediamtx.exe /F — el supervisor los relanzara en segundos."
