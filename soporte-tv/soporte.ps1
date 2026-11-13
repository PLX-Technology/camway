# Soporte de TV Android / Google TV por red local (adb).  Uso:  soporte.bat [IP]
$ErrorActionPreference = 'Continue'
$Raiz = $PSScriptRoot
$Adb  = Join-Path $Raiz 'adb\adb.exe'
if (-not (Test-Path $Adb)) { $Adb = (Get-Command adb -ErrorAction SilentlyContinue).Source }
if (-not $Adb) { Write-Host 'No encuentro adb.exe (carpeta adb\ junto a este script).' -ForegroundColor Red; exit 1 }
$Salida = Join-Path $Raiz 'salida'; New-Item -ItemType Directory -Force $Salida | Out-Null
$Puerto = 5555
$Script:Tv = $null          # "ip:puerto" del televisor activo

function Sh { param([string]$c) & $Adb -s $Script:Tv shell $c 2>&1 }
function Marca { Get-Date -Format 'yyyyMMdd_HHmmss' }
function Nom { $Script:Tv -replace '[:.]', '-' }

# ---------------- descubrimiento ----------------
function Get-RedLocal {
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notmatch '^(127|169\.254)' -and $_.PrefixLength -ge 16 -and $_.PrefixLength -le 30 }
}
function Expand-Cidr([string]$cidr) {
    $ip, $pf = $cidr -split '/'; $pf = [int]$pf
    $b = [System.Net.IPAddress]::Parse($ip).GetAddressBytes(); [array]::Reverse($b)
    $n = [BitConverter]::ToUInt32($b, 0)
    $total = [math]::Pow(2, 32 - $pf)
    if ($total -gt 8192) { throw 'Rango demasiado grande (maximo /19).' }
    $mask = [uint32]([math]::Pow(2, 32) - $total)
    $red = $n -band $mask
    for ($i = 1; $i -lt $total - 1; $i++) {
        $x = [BitConverter]::GetBytes([uint32]($red + $i)); [array]::Reverse($x)
        ([System.Net.IPAddress]::new($x)).ToString()
    }
}
function Find-Tvs([string]$cidr) {
    Write-Host "Buscando TVs con depuracion activa en $cidr (puerto $Puerto)..." -ForegroundColor Cyan
    $hosts = @(Expand-Cidr $cidr); $found = New-Object System.Collections.ArrayList
    for ($i = 0; $i -lt $hosts.Count; $i += 512) {
        $lote = $hosts[$i..([math]::Min($i + 511, $hosts.Count - 1))]
        $tareas = foreach ($h in $lote) { $c = New-Object Net.Sockets.TcpClient; [pscustomobject]@{ H = $h; C = $c; T = $c.ConnectAsync($h, $Puerto) } }
        Start-Sleep -Milliseconds 1000
        foreach ($t in $tareas) { if ($t.T.IsCompleted -and -not $t.T.IsFaulted -and $t.C.Connected) { [void]$found.Add($t.H) }; $t.C.Close() }
    }
    return , @($found)
}
function Info-Tv([string]$ip) {
    $null = & $Adb connect "${ip}:$Puerto" 2>&1
    $est = (& $Adb devices | Select-String ([regex]::Escape("${ip}:$Puerto"))) -as [string]
    if ($est -match 'unauthorized') { return [pscustomobject]@{ Ip = $ip; Estado = 'SIN AUTORIZAR: acepta el aviso en el TV (Permitir siempre)'; Modelo = '' } }
    if ($est -notmatch 'device\s*$') { return [pscustomobject]@{ Ip = $ip; Estado = 'no responde'; Modelo = '' } }
    $m = (& $Adb -s "${ip}:$Puerto" shell 'getprop ro.product.model' 2>&1) -join ''
    [pscustomobject]@{ Ip = $ip; Estado = 'listo'; Modelo = $m.Trim() }
}
function Conectar([string]$ip) {
    $i = Info-Tv $ip
    if ($i.Estado -ne 'listo') { Write-Host "$ip : $($i.Estado)" -ForegroundColor Yellow; return $false }
    $Script:Tv = "${ip}:$Puerto"; Write-Host "Conectado a $ip ($($i.Modelo))" -ForegroundColor Green; return $true
}
function Elegir-Tv {
    $redes = @(Get-RedLocal)
    $def = '192.168.1.0/24'
    if ($redes.Count) { $p = $redes[0].IPAddress -split '\.'; $def = "$($p[0]).$($p[1]).$($p[2]).0/24" }
    $r = Read-Host "Rango a escanear [$def]  (ej. 172.16.0.0/22, o una IP directa)"
    if (-not $r) { $r = $def }
    if ($r -notmatch '/') { return (Conectar $r) }
    $ips = Find-Tvs $r
    if (-not $ips.Count) { Write-Host 'No hay TVs con depuracion por red en ese rango.' -ForegroundColor Yellow; return $false }
    $lista = @(); $n = 1
    foreach ($ip in $ips) { $i = Info-Tv $ip; $lista += $i; Write-Host ("[{0}] {1,-15} {2,-22} {3}" -f $n, $i.Ip, $i.Modelo, $i.Estado); $n++ }
    $s = Read-Host 'Numero de TV'
    if ($s -notmatch '^\d+$' -or [int]$s -lt 1 -or [int]$s -gt $lista.Count) { return $false }
    return (Conectar $lista[[int]$s - 1].Ip)
}

# ---------------- acciones ----------------
function Prop([string]$p) { ((Sh "getprop $p") -join '').Trim() }
function Ver-Info {
    Write-Host "`n=== $Script:Tv ===" -ForegroundColor Cyan
    "Modelo          : $(Prop 'ro.product.model')"
    "Android         : $(Prop 'ro.build.version.release') (SDK $(Prop 'ro.build.version.sdk'))"
    "Compilacion     : $(Prop 'ro.build.fingerprint')"
    $ip = ((Sh 'ip -f inet addr show wlan0 | grep inet') -join '').Trim()
    if (-not $ip) { $ip = ((Sh 'ip -f inet addr show eth0 | grep inet') -join '').Trim() }
    "Red (IP)        : $ip"
    $w = ((Sh 'dumpsys wifi | grep -m1 mWifiInfo') -join '').Trim(); if ($w.Length -gt 220) { $w = $w.Substring(0, 220) }
    "Wi-Fi           : $w"
    $up = [int](((Sh 'cat /proc/uptime') -join '').Split('.')[0])
    "Encendido hace  : $([math]::Round($up / 3600, 1)) h"
    "Memoria         : $((Sh 'grep -E MemAvailable /proc/meminfo') -join '')"
    "Almacenamiento  : $((Sh 'df /data | tail -1') -join '')"
    $cw = ((Sh "dumpsys package com.pacific.camwaytv | grep versionName") -join '').Trim()
    "CamWay TV       : $(if ($cw) { $cw } else { 'no instalada' })"
    "App en pantalla : $(((Sh 'dumpsys activity activities | grep topResumedActivity') -join '').Trim())"
}
function Captura {
    $f = Join-Path $Salida ("captura_{0}_{1}.png" -f (Nom), (Marca))
    $null = Sh 'screencap -p /sdcard/_sop.png'; $null = & $Adb -s $Script:Tv pull '/sdcard/_sop.png' $f 2>&1; $null = Sh 'rm /sdcard/_sop.png'
    if (Test-Path $f) { Write-Host "Guardada: $f" -ForegroundColor Green; Start-Process $f } else { Write-Host 'No se pudo capturar.' -ForegroundColor Red }
}
function Grabar {
    $seg = Read-Host 'Segundos a grabar (max 180) [15]'; if ($seg -notmatch '^\d+$') { $seg = 15 }; $seg = [math]::Min([int]$seg, 180)
    Write-Host "Grabando $seg s..."; $null = Sh "screenrecord --time-limit $seg /sdcard/_sop.mp4"
    $f = Join-Path $Salida ("video_{0}_{1}.mp4" -f (Nom), (Marca))
    $null = & $Adb -s $Script:Tv pull '/sdcard/_sop.mp4' $f 2>&1; $null = Sh 'rm /sdcard/_sop.mp4'
    if (Test-Path $f) { Write-Host "Guardado: $f" -ForegroundColor Green; Start-Process $f }
}
function Vivo {
    $s = @((Join-Path $Raiz 'scrcpy\scrcpy.exe'), (Get-Command scrcpy -ErrorAction SilentlyContinue).Source) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $s) { Write-Host 'Para la vista en vivo copia la carpeta de scrcpy en  soporte-tv\scrcpy\  (github.com/Genymobile/scrcpy/releases).' -ForegroundColor Yellow; return }
    $env:ADB = $Adb
    Start-Process $s -ArgumentList @('-s', $Script:Tv, '--max-size', '1280', '--window-title', "TV $Script:Tv")
}
function Enviar-Texto { $t = Read-Host 'Texto'; if ($t) { $null = Sh ("input text '" + (($t -replace "'", '') -replace ' ', '%s') + "'") } }
function Control-Remoto {
    Write-Host 'Flechas, Enter=OK, Backspace/Esc=Atras, H=Home, M=Menu, +/- volumen, T=texto, Q=salir' -ForegroundColor Cyan
    while ($true) {
        $k = [Console]::ReadKey($true); $cod = $null
        switch ($k.Key) {
            'UpArrow'    { $cod = 'KEYCODE_DPAD_UP' }
            'DownArrow'  { $cod = 'KEYCODE_DPAD_DOWN' }
            'LeftArrow'  { $cod = 'KEYCODE_DPAD_LEFT' }
            'RightArrow' { $cod = 'KEYCODE_DPAD_RIGHT' }
            'Enter'      { $cod = 'KEYCODE_DPAD_CENTER' }
            'Backspace'  { $cod = 'KEYCODE_BACK' }
            'Escape'     { $cod = 'KEYCODE_BACK' }
            'H'          { $cod = 'KEYCODE_HOME' }
            'M'          { $cod = 'KEYCODE_MENU' }
            'OemPlus'    { $cod = 'KEYCODE_VOLUME_UP' }
            'Add'        { $cod = 'KEYCODE_VOLUME_UP' }
            'OemMinus'   { $cod = 'KEYCODE_VOLUME_DOWN' }
            'Subtract'   { $cod = 'KEYCODE_VOLUME_DOWN' }
            'T'          { Enviar-Texto }
            'Q'          { return }
        }
        if ($cod) { $null = Sh "input keyevent $cod" }
    }
}
function Ajustes {
    $m = [ordered]@{
        '1' = @('Wi-Fi', 'android.settings.WIFI_SETTINGS')
        '2' = @('Red / Internet', 'android.settings.WIRELESS_SETTINGS')
        '3' = @('Ajustes generales', 'android.settings.SETTINGS')
        '4' = @('Aplicaciones', 'android.settings.APPLICATION_SETTINGS')
        '5' = @('Fecha y hora', 'android.settings.DATE_SETTINGS')
        '6' = @('Pantalla', 'android.settings.DISPLAY_SETTINGS')
        '7' = @('Opciones de desarrollador', 'android.settings.APPLICATION_DEVELOPMENT_SETTINGS')
        '8' = @('Informacion del TV', 'android.settings.DEVICE_INFO_SETTINGS')
        '9' = @('Mostrar sobre otras apps', 'android.settings.action.MANAGE_OVERLAY_PERMISSION')
    }
    foreach ($k in $m.Keys) { '[{0}] {1}' -f $k, $m[$k][0] }
    $s = Read-Host 'Abrir'
    if ($m.Contains($s)) { $null = Sh "am start -a $($m[$s][1])"; Write-Host "Abierto: $($m[$s][0])" -ForegroundColor Green }
}
function Abrir-App {
    $apps = @()
    foreach ($cat in 'LEANBACK_LAUNCHER', 'LAUNCHER') {
        $apps += @(Sh "cmd package query-activities --brief -a android.intent.action.MAIN -c android.intent.category.$cat") | Where-Object { $_ -match '^[\w.]+/' }
    }
    $apps = @($apps | ForEach-Object { $_.ToString().Trim() } | Sort-Object -Unique)
    if (-not $apps.Count) { Write-Host 'No pude listar apps.' -ForegroundColor Yellow; return }
    $n = 1; foreach ($a in $apps) { '[{0}] {1}' -f $n, $a; $n++ }
    $s = Read-Host 'Abrir (numero)'
    if ($s -match '^\d+$' -and [int]$s -ge 1 -and [int]$s -le $apps.Count) { $null = Sh "am start -n $($apps[[int]$s - 1])"; Write-Host 'Abierta.' -ForegroundColor Green }
}
function Ver-Log {
    $f = Read-Host 'Filtro del log (vacio = camwaytv, * = todo; Ctrl+C para salir)'; if (-not $f) { $f = 'camwaytv' }
    $null = Sh 'logcat -c'
    if ($f -eq '*') { & $Adb -s $Script:Tv logcat } else { & $Adb -s $Script:Tv logcat | Select-String -Pattern $f -SimpleMatch }
}
function Guardar-Log {
    $f = Join-Path $Salida ("log_{0}_{1}.txt" -f (Nom), (Marca))
    & $Adb -s $Script:Tv logcat -d > $f; Write-Host "Log guardado: $f" -ForegroundColor Green
}
function Instalar {
    $a = (Read-Host 'Ruta del .apk (arrastra el archivo aqui)').Trim('"'); if (-not (Test-Path $a)) { Write-Host 'No existe.' -ForegroundColor Red; return }
    & $Adb -s $Script:Tv install -r $a
}
function Reiniciar-CamWay {
    $null = Sh 'am force-stop com.pacific.camwaytv'; $null = Sh 'am start -n com.pacific.camwaytv/.MainActivity'; Write-Host 'CamWay reiniciada.' -ForegroundColor Green
}
function Reiniciar-Tv {
    if ((Read-Host 'Reiniciar el TV (s/N)') -eq 's') {
        $null = & $Adb -s $Script:Tv reboot
        Write-Host 'Reiniciando. Al volver, la depuracion por red puede no reactivarse sola: verifica con la opcion de buscar.' -ForegroundColor Yellow
        $Script:Tv = $null
    }
}
function Shell-Libre { Write-Host 'Shell del TV (exit para salir)' -ForegroundColor Cyan; & $Adb -s $Script:Tv shell }

# ---------------- menu ----------------
if ($args.Count -ge 1 -and $args[0] -match '^\d+\.\d+\.\d+\.\d+$') { [void](Conectar $args[0]) }
while ($true) {
    $estado = if ($Script:Tv) { $Script:Tv } else { 'sin conexion' }
    Write-Host "`n===== SOPORTE TV  [$estado] =====" -ForegroundColor Cyan
    if (-not $Script:Tv) {
        '[1] Buscar / conectar a un TV'
        '[0] Salir'
        switch (Read-Host 'Opcion') { '1' { [void](Elegir-Tv) } '0' { exit } }
        continue
    }
    '[1]  Info del TV y de CamWay          [9]  Ver log en vivo'
    '[2]  Captura de pantalla              [10] Guardar log completo'
    '[3]  Grabar video de la pantalla      [11] Instalar / actualizar APK'
    '[4]  Vista en vivo (scrcpy)           [12] Reiniciar CamWay'
    '[5]  Control remoto con el teclado    [13] Reiniciar el TV'
    '[6]  Abrir ajustes (Wi-Fi, IP...)     [14] Shell libre'
    '[7]  Escribir texto                   [15] Cambiar de TV'
    '[8]  Abrir una app                    [0]  Salir'
    switch (Read-Host 'Opcion') {
        '1' { Ver-Info }  '2' { Captura }  '3' { Grabar }  '4' { Vivo }  '5' { Control-Remoto }
        '6' { Ajustes }   '7' { Enviar-Texto }  '8' { Abrir-App }  '9' { Ver-Log }  '10' { Guardar-Log }
        '11' { Instalar } '12' { Reiniciar-CamWay }  '13' { Reiniciar-Tv }  '14' { Shell-Libre }
        '15' { $Script:Tv = $null }  '0' { exit }
    }
}
