@echo off
title NETRAX - phone call setup
cd /d %~dp0
call venv\Scripts\activate
echo.
echo ===== NETRAX fire / smoke PHONE CALL setup =====
echo.
if exist tools\platform-tools\adb.exe goto have_adb
echo [1/4] Downloading Google's Android tool (adb, about 7 MB) from dl.google.com ...
curl.exe -L --fail -o "%TEMP%\netrax_platform_tools.zip" https://dl.google.com/android/repository/platform-tools-latest-windows.zip
if errorlevel 1 goto no_download
tar -xf "%TEMP%\netrax_platform_tools.zip" -C tools
del "%TEMP%\netrax_platform_tools.zip"
if not exist tools\platform-tools\adb.exe goto no_download
:have_adb
echo [1/4] Android tool is installed: tools\platform-tools\adb.exe
echo.
echo [2/4] Who should NETRAX call when there is fire or smoke?
echo       Type the supervisor's mobile number (example 9812345678) and press Enter.
echo       Just press Enter to keep the number that is already saved.
set NUM=
set /p NUM=      Number: 
if not "%NUM%"=="" python -m tools.phone_call_setup --to "%NUM%"
echo.
echo [3/4] The calling phone: an Android phone with a SIM, connected to this laptop by USB,
echo       with Developer options - USB debugging ON. If the phone shows "Allow USB debugging?", tap ALLOW now.
pause
tools\platform-tools\adb.exe start-server
python -m tools.phone_call_setup > data\phone_call_setup.log 2>&1
type data\phone_call_setup.log
echo.
echo [4/4] Test: ring the supervisor once now?
python -m tools.phone_call_setup --test
echo.
echo Finished. Restart NETRAX (close its windows, then run_all.bat) so it uses the new settings.
pause
exit /b
:no_download
echo.
echo Could not download the Android tool. Check the internet, or download
echo https://dl.google.com/android/repository/platform-tools-latest-windows.zip yourself and unzip it into the tools folder.
pause
