# Envuelve el paquete CamWay (zip) en UN .exe autoejecutable con IExpress (nativo de Windows).
# Uso: powershell -ExecutionPolicy Bypass -File windows\sfx\make-exe.ps1
$ErrorActionPreference = "Stop"
$sfx = $PSScriptRoot
$win = Split-Path $sfx -Parent
$dist = Join-Path $win "dist"

# 1) el zip mas reciente del build
$zip = Get-ChildItem (Join-Path $dist "CamWay-Windows-Setup-*.zip") | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $zip) { throw "No hay CamWay-Windows-Setup-*.zip en $dist. Corre build.ps1 primero." }
$ver = ($zip.BaseName -replace '^CamWay-Windows-Setup-', '')
Write-Host "Paquete: $($zip.Name)  ($([math]::Round($zip.Length/1MB)) MB)"

# 2) carpeta de ensamblaje con el zip + el arranque
$stage = Join-Path $sfx "stage"
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Copy-Item $zip.FullName $stage
Copy-Item (Join-Path $sfx "run-setup.cmd") $stage

# 3) archivo .SED para IExpress
$exeOut = Join-Path $dist "CamWay-Setup-$ver.exe"
Remove-Item $exeOut -Force -ErrorAction SilentlyContinue
$sed = Join-Path $sfx "camway.sed"
$sedText = @"
[Version]
Class=IEXPRESS
SEDVersion=3
[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=1
HideExtractAnimation=0
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=%InstallPrompt%
DisplayLicense=%DisplayLicense%
FinishMessage=%FinishMessage%
TargetName=%TargetName%
FriendlyName=%FriendlyName%
AppLaunched=%AppLaunched%
PostInstallCmd=%PostInstallCmd%
AdminQualifiedName=%AdminQualifiedName%
SourceFiles=SourceFiles
[Strings]
InstallPrompt=
DisplayLicense=
FinishMessage=
TargetName=$exeOut
FriendlyName=Instalador de CamWay
AppLaunched=cmd /c run-setup.cmd
PostInstallCmd=<None>
AdminQualifiedName=
FILE0=run-setup.cmd
FILE1=$($zip.Name)
[SourceFiles]
SourceFiles0=$stage
[SourceFiles0]
%FILE0%=
%FILE1%=
"@
Set-Content -Path $sed -Value $sedText -Encoding ascii

# 4) construir con IExpress (silencioso)
Write-Host "Construyendo $([System.IO.Path]::GetFileName($exeOut)) con IExpress..."
$p = Start-Process -FilePath "$env:WINDIR\System32\iexpress.exe" -ArgumentList "/N","/Q","`"$sed`"" -Wait -PassThru
if (-not (Test-Path $exeOut)) { throw "IExpress no genero el .exe (codigo $($p.ExitCode))" }
Write-Host ("LISTO: {0}  ({1:N0} MB)" -f $exeOut, ((Get-Item $exeOut).Length/1MB)) -ForegroundColor Green
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
