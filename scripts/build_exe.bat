@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0\.."
title Build Gestor Nexxo 800

echo.
echo  Compilando ejecutable portable (Gestor Nexxo 800)...
echo.

where py >nul 2>&1
if %errorlevel% neq 0 (
    where python >nul 2>&1
    if %errorlevel% neq 0 (
        echo  [ERROR] Python no encontrado.
        pause
        exit /b 1
    )
    set PY=python
) else (
    set PY=py
)

%PY% -m pip install -r requirements.txt -q
%PY% -m pip install pyinstaller -q
%PY% -m PyInstaller Horus_HAS_Admin.spec --noconfirm --clean

if %errorlevel% neq 0 (
    echo  [ERROR] Fallo la compilacion.
    pause
    exit /b 1
)

echo.
echo  Listo: dist\Gestor Nexxo 800.exe
echo  Copie ese archivo a cualquier PC Windows.
echo.
pause
