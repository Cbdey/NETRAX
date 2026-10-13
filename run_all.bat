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
