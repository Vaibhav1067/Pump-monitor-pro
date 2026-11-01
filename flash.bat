@echo off
rem Flash the ESP32 pump-node firmware. Plug the ESP32 into USB, then double-click this file.
cd /d "%~dp0"
if not exist .venv\installed.ok (
  echo Setting up Python environment, one time only. This needs internet...
  if not exist .venv\Scripts\python.exe (
    python -m venv .venv || (echo ERROR: Python 3 not found. Install it from python.org and tick "Add to PATH", then run this again. & pause & exit /b 1)
  )
  .venv\Scripts\python -m pip install -q -r requirements.txt || (echo ERROR: could not install the packages. Check the internet connection and run this again. & pause & exit /b 1)
  echo ok> .venv\installed.ok
)
.venv\Scripts\python tools\flash_esp32.py %*
pause
