@echo off
title Odoo 18 - Gestor & Sincronizador de Listas de Precios
cd /d "%~dp0"

echo ============================================================
echo   Iniciando Odoo 18 - Gestor de Listas de Precios (Mergal SA)
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] No se encontro el entorno virtual en .venv.
    echo Ejecuta: python -m venv .venv e instala las dependencias.
    pause
    exit /b 1
)

.\.venv\Scripts\python -m src
pause
