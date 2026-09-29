@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0\.."
title Horus HAS Admin

echo.
echo  Horus HAS Admin
echo  ===============
echo.

rem Sin bloques ( ): una linea entre parentesis que empieza por comillas
rem se ejecuta aunque el IF sea falso (bug de cmd.exe).
if exist ".venv\Scripts\python.exe" goto :use_venv
where py >nul 2>&1
if %errorlevel%==0 goto :use_py
where python >nul 2>&1
if %errorlevel%==0 goto :use_python

echo  [ERROR] Python 3.10+ no encontrado.
echo  Instale Python o use: dist\Horus HAS Admin.exe
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
