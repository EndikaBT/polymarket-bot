@echo off
title Deploy Polymarket Bot
cd /d "%~dp0"

echo.
echo  Ejecutando tests...
echo  ─────────────────────────────────────
echo.

pytest
if errorlevel 1 (
    echo.
    echo  TESTS FALLIDOS — deploy cancelado.
    echo  Revisa los errores y vuelve a intentarlo.
    echo.
    pause
    exit /b 1
)

echo.
echo  Tests OK — reiniciando servicio...
echo  ─────────────────────────────────────
echo.

call reiniciar.bat
