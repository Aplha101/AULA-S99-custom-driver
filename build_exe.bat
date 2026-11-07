@echo off
setlocal
cd /d "%~dp0"
title Building AulaS99.exe

echo ================================================
echo   Aula S99 - build AulaS99.exe
echo   Run this next to s99_control.py and s99_visualizer.py
echo   Add the word "debug" (build_exe.bat debug) to keep a console window
echo ================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.10 or newer from python.org
  echo and tick "Add python.exe to PATH" in the installer, then run this again.
  pause
  exit /b 1
)

if not exist s99_control.py (
  echo s99_control.py is not in this folder.
  pause
  exit /b 1
)
if not exist s99_visualizer.py (
  echo s99_visualizer.py is not in this folder.
  pause
  exit /b 1
)

set WINMODE=--noconsole
if /i "%~1"=="debug" set WINMODE=--console

echo [1/2] Installing the libraries...
python -m pip install --upgrade pip
python -m pip install hidapi pyaudiowpatch numpy mss pynput pyinstaller
if errorlevel 1 (
  echo.
  echo Installing the libraries failed. Check your internet connection and read the messages above.
  pause
  exit /b 1
)

echo.
echo [2/2] Building the exe (this takes a minute or two)...
python -m PyInstaller --noconfirm --clean --onefile %WINMODE% --name AulaS99 ^
  --hidden-import hid ^
  --hidden-import mss.windows ^
  --hidden-import pynput.keyboard._win32 ^
  --hidden-import pynput.mouse._win32 ^
  --collect-all pyaudiowpatch ^
  s99_control.py
if errorlevel 1 (
  echo.
  echo The build failed. Copy the last lines above and send them to me.
  pause
  exit /b 1
)

echo.
echo ================================================
echo   Done!  Your program is:  %~dp0dist\AulaS99.exe
echo   You can copy that single file anywhere.
echo ================================================
pause
