@echo off
rem Arranque del instalador .exe (IExpress): extrae el paquete y lo ejecuta.
setlocal enabledelayedexpansion
set "SRC=%~dp0"
set "ZIP="
for %%F in ("%SRC%CamWay-Windows-Setup-*.zip") do set "ZIP=%%F"
if not defined ZIP (
  echo No se encontro el paquete de CamWay dentro del instalador.
  pause
  exit /b 1
)
set "DEST=%TEMP%\CamWaySetup"
rmdir /s /q "%DEST%" 2>nul
mkdir "%DEST%" 2>nul
echo.
echo  Preparando la instalacion de CamWay, un momento...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%ZIP%' -DestinationPath '%DEST%' -Force" || (echo No se pudo descomprimir el paquete. & pause & exit /b 1)
set "PKG=%DEST%"
for /d %%D in ("%DEST%\CamWay-Windows-Setup-*") do set "PKG=%%D"
if not exist "%PKG%\Instalar-CamWay.bat" (
  echo El paquete esta incompleto (falta Instalar-CamWay.bat).
  pause
  exit /b 1
)
call "%PKG%\Instalar-CamWay.bat"
