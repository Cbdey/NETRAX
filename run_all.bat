@echo off
REM Starts the backend + dashboard and the edge pipeline in two windows.
cd /d %~dp0
REM Fully local: no internet needed (models are in models/, dashboards load nothing from the web).
set YOLO_OFFLINE=1
start "NETRAX backend" cmd /k "venv\Scripts\activate && python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000"
timeout /t 4 /nobreak >nul
start "NETRAX edge" cmd /k "venv\Scripts\activate && python -m edge.run_edge"
timeout /t 3 /nobreak >nul
start http://127.0.0.1:8000
echo.
REM 11 Nov: the link for teammates on the same Wi-Fi (and the address the ESP32 boards need)
venv\Scripts\python.exe -m tools.lan_setup --brief
echo Full check / ESP32 addresses: venv\Scripts\python.exe -m tools.lan_setup
echo.
pause
