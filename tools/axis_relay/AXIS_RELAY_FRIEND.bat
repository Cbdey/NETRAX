@echo off
rem ======================================================================================================
rem  NETRAX - Axis camera relay. Run this on the laptop the AXIS camera is plugged into (Ethernet cable).
rem  It lets the NETRAX laptop see the camera over the shared Wi-Fi:
rem      <this laptop's Wi-Fi IP>:8554  ->  192.168.0.90:554   (video, RTSP)
rem      <this laptop's Wi-Fi IP>:8090  ->  192.168.0.90:80    (camera web page)
rem  Uses only Windows' own port forwarding (netsh portproxy). Nothing is installed. Needs administrator rights.
rem      AXIS_RELAY_FRIEND.bat          start (stays on after restarts until removed)
rem      AXIS_RELAY_FRIEND.bat remove   stop and remove everything it added
rem  Requirements: this laptop on the SAME Wi-Fi as the NETRAX laptop, its Ethernet set to 192.168.0.x / 24.
rem ======================================================================================================
setlocal
set CAM=192.168.0.90
net session >nul 2>&1
if errorlevel 1 (
  echo Asking for administrator rights...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%~1' -Verb RunAs"
  exit /b
)
if /i "%~1"=="remove" goto remove

echo.
echo [1/4] Checking the Axis camera at %CAM% ...
powershell -NoProfile -Command "if (-not (Test-NetConnection %CAM% -Port 554 -InformationLevel Quiet -WarningAction SilentlyContinue)) { exit 1 }"
if errorlevel 1 (
  echo    The camera does not answer on %CAM%:554.
  echo    Check the Ethernet cable and that this laptop's Ethernet IP is 192.168.0.x with prefix 24.
  pause
  exit /b 1
)
echo    camera answers.

echo [2/4] Starting the Windows IP Helper service - needed for port forwarding ...
sc config iphlpsvc start= auto >nul
net start iphlpsvc >nul 2>&1

echo [3/4] Forwarding ports 8554 and 8090 to the camera ...
netsh interface portproxy delete v4tov4 listenport=8554 listenaddress=0.0.0.0 >nul 2>&1
netsh interface portproxy delete v4tov4 listenport=8090 listenaddress=0.0.0.0 >nul 2>&1
netsh interface portproxy add v4tov4 listenport=8554 listenaddress=0.0.0.0 connectport=554 connectaddress=%CAM%
netsh interface portproxy add v4tov4 listenport=8090 listenaddress=0.0.0.0 connectport=80 connectaddress=%CAM%
netsh advfirewall firewall delete rule name="NETRAX Axis relay" >nul 2>&1
netsh advfirewall firewall add rule name="NETRAX Axis relay" dir=in action=allow protocol=TCP localport=8554,8090 profile=any >nul
netsh interface portproxy show v4tov4

echo [4/4] Checking the relay ...
powershell -NoProfile -Command "if (-not (Test-NetConnection 127.0.0.1 -Port 8554 -InformationLevel Quiet -WarningAction SilentlyContinue)) { exit 1 }"
if errorlevel 1 (
  echo    The relay does not answer on port 8554 - restart this laptop once and run this file again.
  pause
  exit /b 1
)
echo.
echo RELAY IS ON. Wi-Fi address of this laptop - tell the NETRAX laptop if it asks:
powershell -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -match 'Wi-Fi|WLAN' } | ForEach-Object { '    ' + $_.IPAddress + '   camera web page: http://' + $_.IPAddress + ':8090' }"
echo.
echo Keep this laptop ON, plugged in, awake and on the same Wi-Fi. NETRAX finds the relay by itself.
echo To stop: AXIS_RELAY_FRIEND.bat remove
pause
exit /b 0

:remove
netsh interface portproxy delete v4tov4 listenport=8554 listenaddress=0.0.0.0 >nul 2>&1
netsh interface portproxy delete v4tov4 listenport=8090 listenaddress=0.0.0.0 >nul 2>&1
netsh advfirewall firewall delete rule name="NETRAX Axis relay" >nul 2>&1
echo Relay removed.
pause
exit /b 0
