# CamWay para Windows - construye el PAQUETE DE INSTALACION (todo incluido).
#
#   powershell -ExecutionPolicy Bypass -File windows\build.ps1 [-Version 1.0.0]
#
# Hace: 1) compila camway-server.exe con PyInstaller a partir de api\ (con el codigo y la web ACTUALES, relevo incluido)
#       2) lo prueba (arranca y responde /api/health)  3) junta mediamtx.exe y ffmpeg.exe de windows\vendor\
#       4) calcula SHA-256 de todo  5) deja windows\dist\CamWay-Windows-Setup-<version>.zip
# Requisitos de ESTA maquina (la del desarrollador, no la del taladro): Python 3.12 con las dependencias de api\requirements.txt y PyInstaller.
# Solo ASCII a proposito (PowerShell 5.1).

param(
    [string]$Version = "1.0.0",
    [switch]$SkipExeBuild      # reutiliza api\dist\camway-server.exe sin recompilar (solo para probar el empaquetado)
)

$ErrorActionPreference = "Stop"
$win = $PSScriptRoot
$repo = Split-Path $win -Parent
$api = Join-Path $repo "api"
$vendor = Join-Path $win "vendor"
$tmp = Join-Path $win "build-tmp"
$stamp = Get-Date -Format "yyyyMMdd"
$full = "$Version-$stamp"
$name = "CamWay-Windows-Setup-$full"
$stage = Join-Path $win "dist\$name"

function Step($m) { Write-Host ""; Write-Host "== $m" -ForegroundColor Cyan }

foreach ($f in "mediamtx.exe", "ffmpeg.exe") {
    if (-not (Test-Path (Join-Path $vendor $f))) { throw "Falta windows\vendor\$f (copialo del paquete anterior o de una compilacion oficial)." }
}

if (-not $SkipExeBuild) {
    Step "1) Compilando camway-server.exe (PyInstaller)"
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    Push-Location $api
    try {
        & python -m PyInstaller camway-server.spec --noconfirm --clean --distpath (Join-Path $tmp "dist") --workpath (Join-Path $tmp "work")
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller fallo (codigo $LASTEXITCODE)" }
    } finally { Pop-Location }
    $exe = Join-Path $tmp "dist\camway-server.exe"
} else { $exe = Join-Path $tmp "dist\camway-server.exe"; if (-not (Test-Path $exe)) { $exe = Join-Path $api "dist\camway-server.exe" } }
if (-not (Test-Path $exe)) { throw "No existe $exe" }
Write-Host ("    camway-server.exe  {0:N1} MB" -f ((Get-Item $exe).Length / 1MB))

# ---- cliente embebido (camway-client.exe): ventana WebView2 que muestra /live ----
if (-not $SkipExeBuild) {
    Step "1b) Compilando el cliente embebido (camway-client.exe)"
    $clientSrc = Join-Path $win "client"
    if (-not (Test-Path (Join-Path $clientSrc "camway_client.py"))) { throw "Falta windows\client\camway_client.py" }
    Push-Location $clientSrc
    try {
        & python -m PyInstaller --noconfirm --windowed --name camway-client --icon camway.ico `
            --collect-all webview --collect-all pythonnet --collect-all clr_loader `
            --distpath (Join-Path $tmp "client-dist") --workpath (Join-Path $tmp "client-work") camway_client.py
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller del cliente fallo (codigo $LASTEXITCODE)" }
    } finally { Pop-Location }
}
$clientDir = Join-Path $tmp "client-dist\camway-client"
if (-not (Test-Path (Join-Path $clientDir "camway-client.exe"))) { throw "No existe el cliente en $clientDir (ejecuta sin -SkipExeBuild)" }
Write-Host ("    cliente embebido   {0:N1} MB (carpeta)" -f ((Get-ChildItem $clientDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB))

Step "2) Probando el ejecutable (arranca y responde)"
$testDir = Join-Path $env:TEMP "camway-build-test"
Remove-Item $testDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $testDir | Out-Null
$env:CAMWAY_PORT = "18120"; $env:CAMWAY_DB = (Join-Path $testDir "camway.db") -replace "\\", "/"
$env:CAMWAY_BACKUP_DIR = (Join-Path $testDir "bk") -replace "\\", "/"; $env:CAMWAY_RECORD_DIR = (Join-Path $testDir "rec") -replace "\\", "/"
$env:CAMWAY_MTX_API = "http://127.0.0.1:19997"
$p = Start-Process -FilePath $exe -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $testDir "out.log") -RedirectStandardError (Join-Path $testDir "err.log")
$ok = $false
try {
    for ($i = 0; $i -lt 40; $i++) {
        try { $h = (Invoke-WebRequest -Uri "http://127.0.0.1:18120/api/health" -UseBasicParsing -TimeoutSec 10).Content | ConvertFrom-Json; if ($h.ok) { $ok = $true; break } } catch {}
        Start-Sleep -Milliseconds 500
    }
    # la pagina web empaquetada tambien tiene que servirse
    if ($ok) { $page = (Invoke-WebRequest -Uri "http://127.0.0.1:18120/" -UseBasicParsing -TimeoutSec 5); if ($page.StatusCode -ne 200 -or $page.Content.Length -lt 500) { $ok = $false } }
} finally { try { & taskkill.exe /T /F /PID $p.Id 2>&1 | Out-Null } catch {} }
if (-not $ok) { Write-Host (Get-Content (Join-Path $testDir "err.log") -Tail 20 -ErrorAction SilentlyContinue | Out-String); throw "El ejecutable compilado no arranco o no sirvio la web." }
Write-Host "    El ejecutable arranca, responde /api/health y sirve la web" -ForegroundColor Green
Remove-Item Env:CAMWAY_PORT, Env:CAMWAY_DB, Env:CAMWAY_BACKUP_DIR, Env:CAMWAY_RECORD_DIR, Env:CAMWAY_MTX_API -ErrorAction SilentlyContinue

Step "3) Armando el paquete $name"
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
$payload = Join-Path $stage "payload"
New-Item -ItemType Directory -Force -Path $payload | Out-Null
Copy-Item $exe (Join-Path $payload "camway-server.exe")
Copy-Item (Join-Path $vendor "mediamtx.exe") $payload
Copy-Item (Join-Path $vendor "ffmpeg.exe") $payload
Copy-Item (Join-Path $repo "mediamtx\mediamtx.yml") $payload
Copy-Item (Join-Path $win "installer\supervisor.ps1") $payload
Copy-Item $clientDir (Join-Path $payload "client") -Recurse      # cliente embebido -> payload\client\
Set-Content -Path (Join-Path $payload "VERSION") -Value $full -Encoding ascii
foreach ($f in "install.ps1", "uninstall.ps1", "diagnostico.ps1", "Instalar-CamWay.bat", "Desinstalar-CamWay.bat") { Copy-Item (Join-Path $win "installer\$f") $stage }
Copy-Item (Join-Path $win "installer\LEEME.txt") $stage

Step "4) Firmas SHA-256"
$lines = foreach ($f in Get-ChildItem $payload -File) { "$((Get-FileHash -Algorithm SHA256 -Path $f.FullName).Hash)  $($f.Name)" }
Set-Content -Path (Join-Path $payload "SHA256SUMS.txt") -Value $lines -Encoding ascii
$lines | ForEach-Object { Write-Host "    $_" }

Step "5) Comprimiendo"
$zip = Join-Path $win "dist\$name.zip"
Remove-Item $zip -Force -ErrorAction SilentlyContinue
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory($stage, $zip, [IO.Compression.CompressionLevel]::Optimal, $true)
Write-Host ("    {0}  ({1:N0} MB)" -f $zip, ((Get-Item $zip).Length / 1MB)) -ForegroundColor Green
Write-Host ""
Write-Host "Listo. Copia ese .zip al equipo del taladro, descomprimelo y ejecuta Instalar-CamWay.bat" -ForegroundColor Cyan
