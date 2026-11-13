# CamWay para Windows - instalador TODO INCLUIDO (sin Docker, sin Python, sin descargas).
#
# Instala o ACTUALIZA CamWay en un taladro. Trae dentro todo lo que necesita:
#   camway-server.exe (Python ya empaquetado), mediamtx.exe, ffmpeg.exe (con SRT y x264) y la configuracion.
# Conserva los datos (camaras, usuarios, grabaciones) al actualizar y migra los de una instalacion anterior.
#
# Uso normal: doble clic en Instalar-CamWay.bat (pide permisos de administrador).
# Opciones:   -InstallDir C:\CamWay  -RelayFile camway-relay-pdv-107.env  -HostIp 192.168.0.14  -ScanCidr 192.168.1.0/24
#             -Silent (sin preguntas)  -Kiosk (acceso de pantalla completa)  -NoService / -NoFirewall (pruebas)
# Solo ASCII a proposito: PowerShell 5.1 lee mal los acentos de scripts sin BOM.

[CmdletBinding()]
param(
    [string]$InstallDir = "C:\CamWay",
    [string]$RelayFile = "",
    [string]$HostIp = "",
    [string]$ScanCidr = "",
    [string]$AdminUser = "admin",
    [string]$AdminPass = "",   # vacio = se genera una al azar (no hay clave por defecto en el codigo)
    [switch]$Silent,
    [switch]$Kiosk,
    [switch]$NoService,
    [switch]$NoFirewall,
    [switch]$NoShortcuts
)

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$payload = Join-Path $here "payload"
$script:problems = New-Object System.Collections.ArrayList
$script:notes = New-Object System.Collections.ArrayList
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logFile = $null

function Log($msg, $color = "Gray") {
    Write-Host $msg -ForegroundColor $color
    if ($script:logFile) { try { Add-Content -Path $script:logFile -Value "$(Get-Date -Format 'HH:mm:ss') $msg" -Encoding utf8 } catch {} }
}
function Step($n, $msg) { Log ""; Log "[$n] $msg" "Cyan" }
function Ok($msg) { Log "    OK  $msg" "Green" }
function Warn($msg) { Log "    !!  $msg" "Yellow"; [void]$script:problems.Add($msg) }
function Note($msg) { [void]$script:notes.Add($msg) }
function Fail($msg) { Log ""; Log "ERROR: $msg" "Red"; if (-not $Silent) { Read-Host "Pulsa Enter para cerrar" | Out-Null }; exit 1 }
function Ask($prompt, $default) {
    if ($Silent) { return $default }
    $a = Read-Host "$prompt [Enter = $default]"
    if ([string]::IsNullOrWhiteSpace($a)) { return $default }
    return $a.Trim()
}

Log "==========================================================" "Cyan"
Log " CamWay para Windows - instalacion" "Cyan"
Log "==========================================================" "Cyan"
$ver = if (Test-Path (Join-Path $payload "VERSION")) { (Get-Content (Join-Path $payload "VERSION") -Raw).Trim() } else { "?" }
Log "Version del paquete: $ver"

# ---------------------------------------------------------------- 1) requisitos
Step 1 "Comprobando el equipo"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $isAdmin -and -not ($NoService -and $NoFirewall)) { Fail "Hace falta ejecutarlo como administrador (usa Instalar-CamWay.bat: clic derecho > Ejecutar como administrador)." }
if (-not [Environment]::Is64BitOperatingSystem) { Fail "Este paquete es de 64 bits y este Windows es de 32 bits." }
if ([Environment]::OSVersion.Version.Major -lt 10) { Fail "Se necesita Windows 10 o mas nuevo (este es $([Environment]::OSVersion.VersionString))." }
Ok "Windows $([Environment]::OSVersion.Version) de 64 bits"
if (-not (Test-Path $payload)) { Fail "Falta la carpeta 'payload' junto a este instalador. Descomprime TODO el paquete antes de instalar." }
$drive = (Split-Path -Qualifier $InstallDir)
try {
    $freeGb = [math]::Round((Get-PSDrive ($drive.TrimEnd(":"))).Free / 1GB, 1)
    if ($freeGb -lt 2) { Fail "Queda poco disco en $drive ($freeGb GB). Se necesitan al menos 2 GB libres, mas lo que ocupen las grabaciones." }
    Ok "Disco $drive : $freeGb GB libres"
} catch { Warn "No pude medir el disco de $drive" }

# archivos descargados/copiados por AnyDesk pueden venir "bloqueados" por Windows: se desbloquean para que no pregunte nada al arrancar
Get-ChildItem -Path $here -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue

# integridad: una transferencia cortada por Starlink deja un .exe a medias; se detecta aqui, no en el taladro
$sums = Join-Path $payload "SHA256SUMS.txt"
if (Test-Path $sums) {
    $bad = @()
    foreach ($ln in Get-Content $sums) {
        if ($ln -match "^([0-9a-fA-F]{64})\s+\*?(.+)$") {
            $f = Join-Path $payload $Matches[2].Trim()
            if (-not (Test-Path $f)) { $bad += "$($Matches[2]) (falta)"; continue }
            if ((Get-FileHash -Algorithm SHA256 -Path $f).Hash -ne $Matches[1].ToUpper()) { $bad += "$($Matches[2]) (danado)" }
        }
    }
    if ($bad.Count) { Fail "El paquete esta incompleto o danado: $($bad -join ', '). Vuelve a copiarlo al equipo." }
    Ok "Archivos del paquete verificados (SHA-256)"
} else { Warn "El paquete no trae SHA256SUMS.txt: no se pudo verificar su integridad" }

# ---------------------------------------------------------------- 2) instalacion anterior
Step 2 "Buscando una instalacion anterior de CamWay"
$oldDirs = New-Object System.Collections.ArrayList
$running = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -in "camway-server.exe", "mediamtx.exe" })
# solo cuentan como "de CamWay" los procesos cuya carpeta tiene camway-server.exe: un mediamtx.exe suelto de otro programa NO se toca
foreach ($p in $running) { if ($p.ExecutablePath) { $d = Split-Path $p.ExecutablePath -Parent; if (Test-Path (Join-Path $d "camway-server.exe")) { [void]$oldDirs.Add($d) } } }
$task = Get-ScheduledTask -TaskName "CamWay Supervisor" -ErrorAction SilentlyContinue
if ($task) { foreach ($a in $task.Actions) { if ($a.Arguments -match '-Root\s+"([^"]+)"') { [void]$oldDirs.Add($Matches[1]) } } }
foreach ($d in "C:\CamWay", "C:\DWWEB") { if (Test-Path (Join-Path $d "camway-server.exe")) { [void]$oldDirs.Add($d) } }
$oldDirs = @($oldDirs | Where-Object { $_ } | Select-Object -Unique)
if ($running.Count) { Log "    Procesos de CamWay en marcha: $(($running | ForEach-Object { "$($_.Name) (PID $($_.ProcessId))" }) -join ', ')" }
if ($oldDirs.Count) { Log "    Carpetas con CamWay: $($oldDirs -join ', ')" } else { Log "    No hay instalacion anterior." }

# bases de datos candidatas (la mas reciente gana)
$dbCandidates = New-Object System.Collections.ArrayList
foreach ($d in ($oldDirs + @("C:\data", "C:\DWWEB", "C:\CamWay"))) {
    foreach ($rel in "data\camway.db", "camway.db") {
        $f = Join-Path $d $rel
        if (Test-Path $f) { [void]$dbCandidates.Add((Get-Item $f)) }
    }
}
$newDb = Join-Path $InstallDir "data\camway.db"
$dbCandidates = @($dbCandidates | Where-Object { $_.Length -gt 0 -and $_.FullName -ne $newDb } | Sort-Object LastWriteTime -Descending)

# ---------------------------------------------------------------- 3) preguntas (red y relevo)
Step 3 "Red del equipo"
$cfgDir = Join-Path $InstallDir "config"
$cfgFile = Join-Path $cfgDir "camway.env"
$existing = [ordered]@{}
if (Test-Path $cfgFile) {
    foreach ($ln in Get-Content $cfgFile -Encoding utf8) {
        $t = $ln.Trim(); if (-not $t -or $t.StartsWith("#")) { continue }
        $i = $t.IndexOf("="); if ($i -gt 0) { $existing[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim() }
    }
    Log "    Se encontro una configuracion previa: se conserva (clave de cifrado, relevo, IP)."
}
$nics = @()
try {
    foreach ($c in (Get-NetIPConfiguration -ErrorAction Stop | Where-Object { $_.IPv4Address -and $_.IPv4Address[0].IPAddress -notlike "169.254.*" -and $_.IPv4Address[0].IPAddress -notlike "127.*" })) {
        $ip = $c.IPv4Address[0].IPAddress
        $nics += [pscustomobject]@{ Ip = $ip; Name = $c.InterfaceAlias; Gw = [bool]$c.IPv4DefaultGateway; Net = (($ip -replace '\.\d+$', '.0') + "/24") }
    }
} catch {}
if ($HostIp) { $chosenIp = $HostIp }
elseif ($existing["HOST_IP"]) { $chosenIp = $existing["HOST_IP"] }
elseif ($nics.Count -eq 0) { $chosenIp = Ask "No detecte la IP de red. Escribela (ej. 192.168.1.50)" "127.0.0.1" }
else {
    $def = ($nics | Where-Object { $_.Gw } | Select-Object -First 1); if (-not $def) { $def = $nics[0] }
    if ($nics.Count -gt 1) {
        Log "    Este equipo tiene varias redes:"
        for ($i = 0; $i -lt $nics.Count; $i++) { Log ("      {0}) {1,-16} {2,-26} {3}" -f ($i + 1), $nics[$i].Ip, $nics[$i].Name, $(if ($nics[$i].Gw) { "(sale a Internet)" } else { "" })) }
        $n = Ask "Cual usar para ver CamWay desde la red del taladro (numero)" ([array]::IndexOf($nics, $def) + 1)
        $sel = $nics[[int]$n - 1]; if (-not $sel) { $sel = $def }
        $chosenIp = $sel.Ip
    } else { $chosenIp = $def.Ip }
}
Ok "IP de este equipo: $chosenIp"
$defCidr = if ($ScanCidr) { $ScanCidr } elseif ($existing["SCAN_CIDR"]) { $existing["SCAN_CIDR"] } else { (($chosenIp -replace '\.\d+$', '.0') + "/24") }
if (-not $ScanCidr -and $nics.Count -gt 1) { Log "    Segmentos disponibles: $(($nics | ForEach-Object { $_.Net }) -join '  ')" }
$chosenCidr = if ($ScanCidr) { $ScanCidr } else { Ask "Segmento donde estan las CAMARAS" $defCidr }
Ok "Segmento de camaras: $chosenCidr"

# relevo hacia el Command Center
if (-not $RelayFile) { $f = Get-ChildItem -Path $here -Filter "camway-relay*.env" -File -ErrorAction SilentlyContinue | Select-Object -First 1; if ($f) { $RelayFile = $f.FullName } }
$relay = [ordered]@{}
$relayKeys = "CAMWAY_RELAY_CC", "CAMWAY_RELAY_SOURCE", "CAMWAY_RELAY_KEY", "CAMWAY_RELAY_SRT", "CAMWAY_RELAY_USER", "CAMWAY_RELAY_PASS", "CAMWAY_RELAY_PASSPHRASE"
if ($RelayFile) {
    if (-not (Test-Path $RelayFile)) { Fail "No existe el archivo de conexion: $RelayFile" }
    foreach ($ln in Get-Content $RelayFile -Encoding utf8) {
        $t = $ln.Trim(); if (-not $t -or $t.StartsWith("#")) { continue }
        $i = $t.IndexOf("="); if ($i -gt 0) { $relay[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim() }
    }
    $miss = @($relayKeys | Where-Object { -not $relay[$_] })
    if ($miss.Count) { Fail "El archivo de conexion esta incompleto (faltan: $($miss -join ', ')). Genera uno nuevo en el Command Center: Administracion > Video." }
    Ok "Relevo de video hacia el Command Center: fuente '$($relay['CAMWAY_RELAY_SOURCE'])' ($($relay['CAMWAY_RELAY_CC']))"
} elseif ($relayKeys | Where-Object { $existing[$_] }) {
    foreach ($k in $relayKeys) { if ($existing[$k]) { $relay[$k] = $existing[$k] } }
    Ok "Relevo de video: se conserva el que ya estaba configurado ('$($relay['CAMWAY_RELAY_SOURCE'])')"
} else {
    Log "    Sin archivo de conexion (camway-relay-*.env): se instala SIN relevo hacia el Command Center." "Yellow"
    Note "Para ver las camaras desde el Command Center, genera el archivo de conexion en Administracion > Video y vuelve a ejecutar este instalador junto a ese archivo."
}

# ---------------------------------------------------------------- 4) detener lo anterior
Step 4 "Deteniendo la version anterior"
if ($task -and -not $NoService) { try { Stop-ScheduledTask -TaskName "CamWay Supervisor" -ErrorAction SilentlyContinue } catch {} }
$stopDirs = @($oldDirs) + @($InstallDir)
foreach ($pr in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
    $kill = $false
    if ($pr.Name -in "camway-server.exe", "mediamtx.exe", "ffmpeg.exe" -and $pr.ExecutablePath) {      # solo los programas de CamWay (nunca otros que se llamen igual)
        foreach ($d in $stopDirs) { if ($d -and $pr.ExecutablePath.StartsWith($d, [StringComparison]::OrdinalIgnoreCase)) { $kill = $true } }
    }
    if ($kill) { try { & taskkill.exe /T /F /PID $pr.ProcessId 2>&1 | Out-Null } catch {} }
}
Start-Sleep -Seconds 2
Ok "Procesos anteriores detenidos"
# inicios automaticos viejos (start.bat en Inicio): se desactivan, no se borran
$startupDirs = @([Environment]::GetFolderPath("Startup"), [Environment]::GetFolderPath("CommonStartup")) | Where-Object { $_ -and (Test-Path $_) }
foreach ($sd in $startupDirs) {
    Get-ChildItem $sd -Filter "*.lnk" -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $t = (New-Object -ComObject WScript.Shell).CreateShortcut($_.FullName).TargetPath
            if ($t -match "start\.bat$" -or $t -match "camway") { Rename-Item $_.FullName ($_.Name + ".desactivado") -Force; Ok "Inicio automatico antiguo desactivado: $($_.Name)" }
        } catch {}
    }
}

# ---------------------------------------------------------------- 5) copiar archivos
Step 5 "Instalando en $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir, (Join-Path $InstallDir "data"), (Join-Path $InstallDir "rec"), (Join-Path $InstallDir "logs"), $cfgDir | Out-Null
$logFile = Join-Path $InstallDir "logs\install-$stamp.log"
$script:logFile = $logFile
Log "Instalacion $stamp, paquete $ver" "DarkGray"
$null = & robocopy.exe $payload $InstallDir /E /IS /IT /R:3 /W:2 /NFL /NDL /NJH /NJS /NP /XF SHA256SUMS.txt
if ($LASTEXITCODE -ge 8) { Fail "No se pudieron copiar los archivos (robocopy codigo $LASTEXITCODE). Cierra lo que use la carpeta $InstallDir y reintenta." }
Copy-Item (Join-Path $payload "SHA256SUMS.txt") (Join-Path $InstallDir "SHA256SUMS.txt") -Force -ErrorAction SilentlyContinue
Copy-Item (Join-Path $here "uninstall.ps1") (Join-Path $InstallDir "uninstall.ps1") -Force -ErrorAction SilentlyContinue
Copy-Item (Join-Path $here "diagnostico.ps1") (Join-Path $InstallDir "diagnostico.ps1") -Force -ErrorAction SilentlyContinue
Ok "Programas copiados (camway-server, mediamtx, ffmpeg, supervisor)"

# datos: si es la primera vez en esta carpeta y hay una base de una instalacion anterior, se COPIA (la original queda intacta como respaldo)
if (-not (Test-Path $newDb)) {
    if ($dbCandidates.Count) {
        $src = $dbCandidates[0]
        Log "    Base de datos anterior: $($src.FullName)  ($([math]::Round($src.Length / 1KB)) KB, $($src.LastWriteTime))"
        foreach ($sfx in "", "-wal", "-shm") { $sf = $src.FullName + $sfx; if (Test-Path $sf) { Copy-Item $sf ($newDb + $sfx) -Force } }
        Ok "Camaras, usuarios y configuracion migrados (la base original no se toco)"
        Note "Si alguna camara pide de nuevo su usuario/clave, vuelve a guardarlas: la version anterior no fijaba la clave de cifrado y esas contrasenas pueden no poder leerse."
    } else { Log "    Base de datos nueva (la primera vez crea el usuario admin en el navegador)." }
} else { Ok "Se conservan los datos de $InstallDir\data" }

# ---------------------------------------------------------------- 6) configuracion
Step 6 "Escribiendo la configuracion"
$secret = $existing["CAMWAY_SECRET_KEY"]
if (-not $secret) {
    $bytes = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $secret = ([Convert]::ToBase64String($bytes)).Replace("+", "-").Replace("/", "_")
    Log "    Clave de cifrado de credenciales generada (se guarda en config\camway.env; NO se pierde al reiniciar)."
}
$lines = New-Object System.Collections.ArrayList
[void]$lines.Add("# CamWay - configuracion de este equipo. Editable; se lee en cada arranque del servicio.")
[void]$lines.Add("# Generado por el instalador $stamp (paquete $ver)")
[void]$lines.Add("HOST_IP=$chosenIp")
[void]$lines.Add("SCAN_CIDR=$chosenCidr")
[void]$lines.Add("# Cifra las contrasenas de las camaras guardadas. NO la cambies ni la pierdas: las credenciales guardadas quedarian ilegibles.")
[void]$lines.Add("CAMWAY_SECRET_KEY=$secret")
[void]$lines.Add("# Modo kiosko: el cliente embebido local (127.0.0.1) muestra el video sin login.")
[void]$lines.Add("CAMWAY_KIOSK=1")
foreach ($k in $existing.Keys) {            # ajustes que el tecnico haya puesto a mano (CAMWAY_*/MTX_*), salvo los que el instalador maneja
    if (($k -like "CAMWAY_*" -or $k -like "MTX_*") -and $k -ne "CAMWAY_SECRET_KEY" -and $k -ne "CAMWAY_KIOSK" -and $k -notlike "CAMWAY_RELAY_*") { [void]$lines.Add("$k=$($existing[$k])") }
}
if ($relay.Count) {
    [void]$lines.Add("# Relevo de video hacia el Command Center (SRT cifrado, solo mientras alguien mira)")
    foreach ($k in $relayKeys) { [void]$lines.Add("$k=$($relay[$k])") }
}
if (Test-Path $cfgFile) { Copy-Item $cfgFile "$cfgFile.bak-$stamp" -Force }
[IO.File]::WriteAllLines($cfgFile, [string[]]$lines, (New-Object Text.UTF8Encoding $false))      # UTF-8 sin BOM
if (-not $NoService) {                        # la clave y el relevo son secretos: solo SYSTEM y administradores
    try { & icacls.exe $cfgDir /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" 2>&1 | Out-Null } catch { Warn "No pude restringir los permisos de config\" }
}
Ok "config\camway.env escrito"

# ---------------------------------------------------------------- 7) firewall
if (-not $NoFirewall) {
    Step 7 "Firewall de Windows"
    try {
        Get-NetFirewallRule -DisplayName "CamWay*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
        New-NetFirewallRule -DisplayName "CamWay (web y video)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8120, 8889, 8888, 8554 -Profile Any -RemoteAddress LocalSubnet | Out-Null
        New-NetFirewallRule -DisplayName "CamWay (WebRTC UDP)" -Direction Inbound -Action Allow -Protocol UDP -LocalPort 8189 -Profile Any -RemoteAddress LocalSubnet | Out-Null
        Ok "Reglas creadas (solo la red local; la salida a Internet no necesita reglas)"
    } catch { Warn "No pude crear las reglas del firewall: $($_.Exception.Message). Si el mosaico no abre desde otro equipo, permitelas a mano (TCP 8120, 8889; UDP 8189)." }
}

# ---------------------------------------------------------------- 8) servicio
$supPath = Join-Path $InstallDir "supervisor.ps1"
if (-not $NoService) {
    Step 8 "Arranque automatico con Windows"
    try {
        if (Get-ScheduledTask -TaskName "CamWay Supervisor" -ErrorAction SilentlyContinue) { Unregister-ScheduledTask -TaskName "CamWay Supervisor" -Confirm:$false }
        $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$supPath`" -Root `"$InstallDir`""
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd
        Register-ScheduledTask -TaskName "CamWay Supervisor" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
        Start-ScheduledTask -TaskName "CamWay Supervisor"
        Ok "Tarea 'CamWay Supervisor' creada: arranca con Windows (antes de iniciar sesion) y se reinicia sola"
    } catch { Fail "No pude registrar el arranque automatico: $($_.Exception.Message)" }

    # ------------------------------------------------------------ 9) verificacion
    Step 9 "Comprobando que funciona"
    $up = $false
    for ($i = 0; $i -lt 45; $i++) {
        try { $h = (Invoke-WebRequest -Uri "http://127.0.0.1:8120/api/health" -UseBasicParsing -TimeoutSec 8).Content | ConvertFrom-Json; if ($h.ok) { $up = $true; break } } catch {}
        Start-Sleep -Seconds 1
    }
    if ($up) {
        Ok "CamWay responde en http://localhost:8120"
        if ($h.mediamtx) { Ok "MediaMTX (video) listo" } else { Warn "CamWay responde pero MediaMTX no. Revisa logs\mediamtx.err.log" }
        Log "    Camaras registradas: $($h.devices)"
        # Sembrar el usuario administrador (solo si la base es nueva; /api/setup es "el primero gana" y solo localhost)
        try {
            $generada = $false
            if (-not $AdminPass) {
                # sin clave dada: una al azar (sin caracteres ambiguos), solo ASCII
                $chars = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
                $bytes = New-Object byte[] 14
                [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
                $AdminPass = -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
                $generada = $true
            }
            $sb = @{ username = $AdminUser; password = $AdminPass } | ConvertTo-Json -Compress
            Invoke-WebRequest -Uri "http://127.0.0.1:8120/api/setup" -Method Post -UseBasicParsing -TimeoutSec 8 -ContentType "application/json" -Body $sb | Out-Null
            Ok "Usuario administrador creado: $AdminUser"
            if ($generada) {
                $script:adminNueva = $AdminPass
                try {
                    Set-Content -Path (Join-Path $InstallDir "config\admin-inicial.txt") -Encoding ASCII `
                        -Value @("Usuario: $AdminUser", "Clave:   $AdminPass", "", "Guarda la clave en un lugar seguro y BORRA este archivo.")
                } catch {}
            }
        } catch {
            $code = try { [int]$_.Exception.Response.StatusCode } catch { 0 }
            if ($code -eq 409) { Log "    El administrador ya existia (se conserva)." }
            else { Warn "No pude crear el admin automaticamente; se crea al abrir el cliente la primera vez." }
        }
    } else { Warn "CamWay no respondio en 45 s. Revisa $InstallDir\logs\camway-server.err.log y supervisor.log" }
}

# prueba del relevo: llegar al Command Center y mandar 3 s de video de prueba por SRT (UDP) hasta su servidor
if ($relay.Count) {
    Step 10 "Probando la conexion con el Command Center"
    $ua = "Mozilla/5.0 (CamWay-relay) Chrome/120"
    try {
        $r = Invoke-WebRequest -Uri ($relay["CAMWAY_RELAY_CC"].TrimEnd("/") + "/api/video/agent/sync") -Method Post -UseBasicParsing -TimeoutSec 15 -UserAgent $ua `
            -Headers @{ "X-Relay-Source" = $relay["CAMWAY_RELAY_SOURCE"]; "X-Relay-Key" = $relay["CAMWAY_RELAY_KEY"] } -ContentType "application/json" -Body '{"running":[]}'
        Ok "El Command Center acepto esta fuente ('$($relay['CAMWAY_RELAY_SOURCE'])')"
    } catch {
        $code = try { [int]$_.Exception.Response.StatusCode } catch { 0 }
        if ($code -eq 403) { Warn "El Command Center rechazo la clave de la fuente '$($relay['CAMWAY_RELAY_SOURCE'])'. Genera de nuevo el archivo de conexion y reinstala." }
        else { Warn "No se pudo hablar con $($relay['CAMWAY_RELAY_CC']) ($($_.Exception.Message)). Revisa la salida a Internet de este equipo." }
    }
    $ffmpeg = Join-Path $InstallDir "ffmpeg.exe"
    $srtHost = $relay["CAMWAY_RELAY_SRT"]
    $sid = "publish:prueba-instalacion-$($relay['CAMWAY_RELAY_SOURCE']):$($relay['CAMWAY_RELAY_USER']):$($relay['CAMWAY_RELAY_PASS'])"
    $url = "srt://${srtHost}?streamid=$sid&passphrase=$($relay['CAMWAY_RELAY_PASSPHRASE'])&pbkeylen=16&mode=caller&connect_timeout=6000"
    $errFile = Join-Path $env:TEMP "camway-srt-test.err"
    try {
        $p = Start-Process -FilePath $ffmpeg -ArgumentList @("-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10", "-t", "3", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-f", "mpegts", $url) -NoNewWindow -PassThru -Wait -RedirectStandardError $errFile
        if ($p.ExitCode -eq 0) { Ok "Video de prueba enviado por SRT a $srtHost (la salida UDP funciona)" }
        else { Warn "No se pudo enviar video por SRT a $srtHost (UDP). Puede estar bloqueado por el firewall o la red de este equipo. Detalle: $((Get-Content $errFile -ErrorAction SilentlyContinue | Select-Object -Last 1))" }
    } catch { Warn "No pude ejecutar la prueba de SRT: $($_.Exception.Message)" }
    Remove-Item $errFile -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------- accesos
if (-not $NoShortcuts) {
    try {
        $desk = [Environment]::GetFolderPath("CommonDesktopDirectory")
        $ws = New-Object -ComObject WScript.Shell
        $clientExe = Join-Path $InstallDir "client\camway-client.exe"
        if (Test-Path $clientExe) {
            $wd = Split-Path $clientExe
            # Escritorio: "Ver Camaras" -> cliente embebido (ventana propia, sin navegador)
            $d = $ws.CreateShortcut((Join-Path $desk "Ver Camaras.lnk"))
            $d.TargetPath = $clientExe; $d.WorkingDirectory = $wd; $d.IconLocation = "$clientExe,0"
            $d.Description = "Ver las camaras en vivo (CamWay)"; $d.Save()
            # Auto-arranque al iniciar sesion: el video aparece solo
            $s = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath("CommonStartup")) "CamWay.lnk"))
            $s.TargetPath = $clientExe; $s.WorkingDirectory = $wd; $s.IconLocation = "$clientExe,0"; $s.Save()
            Ok "Acceso 'Ver Camaras' en el escritorio + auto-arranque al iniciar sesion"
            if (-not $Silent) { try { Start-Process -FilePath $clientExe -WorkingDirectory $wd | Out-Null; Ok "Cliente abierto" } catch {} }
        } else {
            Warn "No encontre el cliente embebido en $clientExe (quedo instalado solo el servidor; abrir http://localhost:8120)"
        }
        # Acceso de administracion por navegador, por si se necesita
        Set-Content -Path (Join-Path $desk "CamWay (administrar).url") -Value "[InternetShortcut]`r`nURL=http://localhost:8120`r`n" -Encoding ascii
    } catch { Warn "No pude crear los accesos directos: $($_.Exception.Message)" }
}

# ---------------------------------------------------------------- resumen
Log ""
Log "==========================================================" "Cyan"
if ($script:problems.Count) { Log " INSTALADO CON $($script:problems.Count) AVISO(S):" "Yellow"; $script:problems | ForEach-Object { Log "   - $_" "Yellow" } }
else { Log " INSTALACION COMPLETA" "Green" }
Log "==========================================================" "Cyan"
Log " Ver camaras:            doble clic en 'Ver Camaras' (se abre solo al iniciar sesion)"
Log " Agregar el NVR:         en el cliente, boton (engranaje) > escribe IP + usuario + clave del NVR"
if ($script:adminNueva) {
    Log " Administrador:          usuario '$AdminUser'  clave: $($script:adminNueva)   <-- anotala; tambien en config\admin-inicial.txt (borralo luego)" "Yellow"
} else {
    Log " Administrador:          usuario '$AdminUser'  (clave definida en la instalacion)"
}
Log " Administrar (web):      http://localhost:8120   (desde la red: http://${chosenIp}:8120)"
if ($relay.Count) { Log " Command Center:         asigna cada camara al taladro en Administracion > Video" }
Log " Archivos:               $InstallDir   (config\ , data\ , rec\ , logs\)"
Log " Si algo falla:          clic derecho > Ejecutar con PowerShell en $InstallDir\diagnostico.ps1"
$script:notes | ForEach-Object { Log " Nota: $_" "Yellow" }
Log ""
if (-not $Silent) { Read-Host "Pulsa Enter para cerrar" | Out-Null }
