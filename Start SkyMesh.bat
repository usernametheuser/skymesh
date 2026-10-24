@echo off
rem SkyMesh OS - double-click to start. First run installs everything it needs (internet needed once).
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title SkyMesh OS

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
  for %%P in ("%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python311\python.exe") do (
    if not defined BASEPY if exist %%P set "BASEPY=%%~P"
  )
  if not defined BASEPY (
    py -3 --version >nul 2>nul && set "BASEPY=py -3"
  )
  if not defined BASEPY (
    echo Python 3.11 or newer is not installed.
    echo Install it with:  winget install --id Python.Python.3.12 -e --source winget
    echo or from https://www.python.org/downloads/  then run this file again.
    pause
    exit /b 1
  )
  echo Creating the SkyMesh environment ^(first run only^)...
  !BASEPY! -m venv .venv || (echo Could not create .venv & pause & exit /b 1)
  set "PY=.venv\Scripts\python.exe"
)

if not exist ".venv\.installed" (
  echo Installing SkyMesh components ^(first run only, needs internet^)...
  "%PY%" -m pip install --disable-pip-version-check -q -r backend\requirements.txt || (echo Installation failed & pause & exit /b 1)
  echo ok> ".venv\.installed"
)

"%PY%" backend\skymesh.py serve --open %*
pause
