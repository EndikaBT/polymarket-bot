@echo off
title Reiniciar Polymarket Bot
cd /d "%~dp0"

echo Deteniendo proceso actual...

REM Matar el proceso que esté usando el puerto 5000
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000 " ^| findstr "LISTENING" 2^>nul') do (
    taskkill /F /PID %%a >nul 2>&1
)

REM Esperar un momento para que libere el puerto
timeout /t 2 /nobreak >nul

echo Reiniciando...

REM Si el servicio está instalado, arrancarlo por Task Scheduler (sin ventana)
schtasks /query /tn "PolymarketBot" >nul 2>&1
if not errorlevel 1 (
    schtasks /run /tn "PolymarketBot" >nul 2>&1
    echo Bot reiniciado como servicio. La web estara disponible en http://localhost:5000
) else (
    REM Si no hay servicio instalado, abrir ventana normal
    start "Polymarket Bot" cmd /k "python app.py"
    timeout /t 2 /nobreak >nul
    start http://localhost:5000
    echo Bot reiniciado en ventana aparte.
)

echo.
pause
