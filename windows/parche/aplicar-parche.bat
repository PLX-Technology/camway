@echo off
rem Parche de interfaz de CamWay (rejilla /live mas estable). No reinstala nada.
cd /d "%~dp0"
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Pidiendo permisos de administrador...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
set "DST=C:\CamWay"
if not exist "%DST%\camway-server.exe" (
  echo No se encontro CamWay en %DST%. Instala el cliente primero.
  pause
  exit /b 1
)
echo.
echo  Aplicando parche de interfaz...
robocopy "%~dp0web" "%DST%\web" /E /IS /IT /NFL /NDL /NJH /NJS /NP >nul
if %errorlevel% geq 8 ( echo No se pudo copiar la interfaz. & pause & exit /b 1 )
rem Hacer que el servidor use esa carpeta web
findstr /I /C:"CAMWAY_WEB_DIR" "%DST%\config\camway.env" >nul 2>&1
if %errorlevel% neq 0 (
  >>"%DST%\config\camway.env" echo.
  >>"%DST%\config\camway.env" echo CAMWAY_WEB_DIR=C:\CamWay\web
)
echo  Reiniciando el servidor (el supervisor lo levanta solo en unos segundos)...
taskkill /F /IM camway-server.exe >nul 2>&1
echo.
echo  LISTO. Espera ~15 segundos y vuelve a abrir "Ver Camaras".
echo  La rejilla deberia quedar estable (sin reconexiones).
echo.
pause
