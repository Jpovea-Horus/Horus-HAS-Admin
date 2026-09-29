@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0\.."
title Horus HAS Admin

echo.
echo  Horus HAS Admin
echo  ===============
echo.

if exist .venv\Scripts\python.exe (
    echo  [INFO] Usando entorno virtual (.venv)
    .venv\Scripts\python.exe app\main.py
    goto :fin
)

where py >nul 2>&1
if %errorlevel%==0 (
    py app\main.py
    goto :fin
)

where python >nul 2>&1
if %errorlevel%==0 (
    python app\main.py
    goto :fin
)

echo  [ERROR] Python 3.10+ no encontrado.
echo  Instale Python o use: dist\Horus HAS Admin.exe
echo.
pause
exit /b 1

:fin
echo.
pause
