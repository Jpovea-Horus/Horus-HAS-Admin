@echo off
chcp 65001 >nul 2>&1
title Gestor Nexxo 800 - Requisitos previos
cd /d "%~dp0"

echo.
echo  Gestor Nexxo 800 - Requisitos previos
echo  =====================================
echo.
echo   1. Verificar e instalar lo que falte (recomendado)
echo   2. Solo verificar (no instala nada)
echo   3. Solo uso el .exe portable (instalar solo cloudflared)
echo.
set /p OP=" Opcion [1]: "
if "%OP%"=="" set OP=1

set FLAGS=
if "%OP%"=="2" set FLAGS=-SoloVerificar
if "%OP%"=="3" set FLAGS=-SinPython

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0verificar_requisitos.ps1" %FLAGS%

echo.
pause
