@echo off
rem Hardware check: reports on every sensor in plain words. Run it with the pump running normally.
rem Close the dashboard first, because only one program can use the USB port.
cd /d "%~dp0"
if not exist .venv\installed.ok (
  echo Setting up Python environment, one time only. This needs internet...
  if not exist .venv\Scripts\python.exe (
    python -m venv .venv || (echo ERROR: Python 3 not found. Install it from python.org and tick "Add to PATH", then run this again. & pause & exit /b 1)
  )
  .venv\Scripts\python -m pip install -q -r requirements.txt || (echo ERROR: could not install the packages. Check the internet connection and run this again. & pause & exit /b 1)
  echo ok> .venv\installed.ok
)
.venv\Scripts\python tools\check_rig.py %*
pause
