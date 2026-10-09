@echo off
setlocal
cd /d "%~dp0"
echo ==================================================
echo STEP Portable - Compilacion sin permisos de admin
 echo ==================================================
where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py -3.11"
) else (
  set "PY=python"
)
%PY% -m venv .venv
if errorlevel 1 goto error
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
if errorlevel 1 goto error
python -m pip install -r requirements.txt
if errorlevel 1 goto error
python -m PyInstaller --noconfirm --clean --windowed --onedir --name STEP-Portable --collect-all vtkmodules --collect-all OCP visor_step.py
if errorlevel 1 goto error
echo.
echo LISTO: dist\STEP-Portable\STEP-Portable.exe
echo Copia toda la carpeta STEP-Portable, no solo el EXE.
pause
exit /b 0
:error
echo.
echo ERROR: revisa mensajes. Necesitas Python 3.11 x64 y conexion a internet para construir.
pause
exit /b 1
