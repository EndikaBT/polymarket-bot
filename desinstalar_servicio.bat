@echo off
title Desinstalar servicio Polymarket Bot

net session >nul 2>&1
if errorlevel 1 (
    echo Este script necesita permisos de administrador.
    echo Haz clic derecho sobre el archivo y selecciona "Ejecutar como administrador".
    pause
    exit /b 1
)

set TASK_NAME=PolymarketBot

echo Eliminando tarea programada "%TASK_NAME%"...
schtasks /delete /tn "%TASK_NAME%" /f

if errorlevel 1 (
    echo La tarea no existia o ya fue eliminada.
) else (
    echo Servicio eliminado correctamente. El bot ya no arrancara al iniciar sesion.
)

echo.
pause
