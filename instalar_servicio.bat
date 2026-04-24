@echo off
title Instalar Polymarket Bot como servicio de Windows
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
    echo Este script necesita permisos de administrador.
    echo Haz clic derecho sobre el archivo y selecciona "Ejecutar como administrador".
    pause
    exit /b 1
)

set TASK_NAME=PolymarketBot
set SCRIPT_DIR=%~dp0
set PYTHON_EXE=pythonw.exe

REM Buscar pythonw.exe en el PATH
where pythonw >nul 2>&1
if errorlevel 1 (
    echo No se encontro pythonw.exe. Asegurate de que Python este en el PATH.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('where pythonw') do set PYTHON_FULL=%%i

echo.
echo  Instalando tarea programada "%TASK_NAME%"...
echo  Script: %SCRIPT_DIR%app.py
echo  Python: %PYTHON_FULL%
echo.

REM Eliminar si ya existe
schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1

REM Crear tarea: se ejecuta al inicio de sesion del usuario actual, con maxima prioridad
schtasks /create ^
  /tn "%TASK_NAME%" ^
  /tr "\"%PYTHON_FULL%\" \"%SCRIPT_DIR%app.py\"" ^
  /sc ONLOGON ^
  /rl HIGHEST ^
  /delay 0000:10 ^
  /f

if errorlevel 1 (
    echo Error al crear la tarea. Revisa los permisos.
    pause
    exit /b 1
)

echo.
echo  Servicio instalado correctamente.
echo  El bot arrancara automaticamente cada vez que inicies sesion en Windows.
echo  Para iniciarlo ahora sin reiniciar, ejecuta start.bat
echo.
pause
