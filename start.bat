@echo off
rem Pump Health Console - Windows launcher. Double-click to run.
rem First run creates .venv and installs requirements. The dashboard opens in your browser;
rem plug the ESP32 into any USB port and it switches to LIVE data automatically.
cd /d "%~dp0"
if not exist .venv\installed.ok (
  echo Setting up Python environment, one time only. This needs internet...
  if not exist .venv\Scripts\python.exe (
    python -m venv .venv || (echo ERROR: Python 3 not found. Install it from python.org and tick "Add to PATH", then run this again. & pause & exit /b 1)
  )
  .venv\Scripts\python -m pip install -q -r requirements.txt || (echo ERROR: could not install the packages. Check the internet connection and run this again. & pause & exit /b 1)
  echo ok> .venv\installed.ok
)
start "" /b cmd /c "timeout /t 3 /nobreak >nul & start http://localhost:8000"
echo Pump Health Console running at http://localhost:8000  (close this window to stop)
.venv\Scripts\python -m backend %*
pause
