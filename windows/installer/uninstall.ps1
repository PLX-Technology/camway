# CamWay - desinstalador. Quita el arranque automatico, el firewall y los accesos. Por omision CONSERVA los datos (data\, rec\, config\).
#   -BorrarDatos  borra tambien la base de datos, las grabaciones y la configuracion (NO se puede deshacer).
param([string]$InstallDir = $PSScriptRoot, [switch]$BorrarDatos, [switch]$Silent)
$ErrorActionPreference = "Continue"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $isAdmin) { Write-Host "Ejecutalo como administrador." -ForegroundColor Red; exit 1 }
if (-not $Silent) {
    $a = Read-Host "Se desinstalara CamWay de $InstallDir. Los datos $(if ($BorrarDatos) { 'TAMBIEN se BORRARAN' } else { 'se conservan' }). Escribe SI para continuar"
    if ($a -ne "SI") { Write-Host "Cancelado."; exit 0 }
}
Write-Host "Deteniendo el servicio..."
Stop-ScheduledTask -TaskName "CamWay Supervisor" -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "CamWay Supervisor" -Confirm:$false -ErrorAction SilentlyContinue
foreach ($pr in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase) -and $_.Name -in "camway-server.exe", "mediamtx.exe", "ffmpeg.exe" })) {
    & taskkill.exe /T /F /PID $pr.ProcessId 2>&1 | Out-Null
}
Get-NetFirewallRule -DisplayName "CamWay*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
Remove-Item (Join-Path ([Environment]::GetFolderPath("CommonDesktopDirectory")) "CamWay.url") -ErrorAction SilentlyContinue
Remove-Item (Join-Path ([Environment]::GetFolderPath("CommonStartup")) "CamWay Kiosco.lnk") -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
foreach ($f in "camway-server.exe", "mediamtx.exe", "ffmpeg.exe", "mediamtx.yml", "supervisor.ps1", "VERSION", "SHA256SUMS.txt", "diagnostico.ps1", "LEEME.txt") { Remove-Item (Join-Path $InstallDir $f) -Force -ErrorAction SilentlyContinue }
if ($BorrarDatos) { foreach ($d in "data", "rec", "config", "logs") { Remove-Item (Join-Path $InstallDir $d) -Recurse -Force -ErrorAction SilentlyContinue } }
Write-Host "Listo. $(if ($BorrarDatos) { 'Datos borrados.' } else { "Los datos siguen en $InstallDir (data\, rec\, config\); borra la carpeta a mano si ya no los quieres." })" -ForegroundColor Green
if (-not $Silent) { Read-Host "Pulsa Enter para cerrar" | Out-Null }
