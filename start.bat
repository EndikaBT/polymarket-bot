@echo off
title Polymarket Bot
cd /d "%~dp0"

REM Activate virtual environment if present
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
) else if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

echo.
echo  ╔══════════════════════════════════╗
echo  ║       POLYMARKET BOT             ║
echo  ║  http://localhost:5000           ║
echo  ╚══════════════════════════════════╝
echo.

REM Open browser after 3 seconds (in background)
start /b cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:5000"

python app.py

echo.
echo  Bot detenido.
pause
