@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0\.."
title Gestor Nexxo 800

echo.
echo  Gestor Nexxo 800
echo  ================
echo.

rem Sin bloques ( ): una linea entre parentesis que empieza por comillas
rem se ejecuta aunque el IF sea falso (bug de cmd.exe).
if exist ".venv\Scripts\python.exe" goto :use_venv
echo  [INFO] Primera ejecucion: verificando requisitos previos...
powershell -NoProfile -ExecutionPolicy Bypass -File "requisitos_previos\verificar_requisitos.ps1"
if exist ".venv\Scripts\python.exe" goto :use_venv
where py >nul 2>&1
if %errorlevel%==0 goto :use_py
where python >nul 2>&1
if %errorlevel%==0 goto :use_python

echo  [ERROR] Python 3.10+ no encontrado.
echo  Ejecute requisitos_previos\instalar_requisitos.bat o use: dist\Gestor Nexxo 800.exe
echo.
pause
exit /b 1

:use_venv
echo  [INFO] Usando entorno virtual (.venv)
call ".venv\Scripts\python.exe" "app\main.py"
goto :fin

:use_py
py -3 "app\main.py"
goto :fin

:use_python
python "app\main.py"
goto :fin

:fin
echo.
pause
