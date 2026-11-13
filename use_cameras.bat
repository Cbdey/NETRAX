@echo off
rem 13 Nov: switch the camera plan. Only copies a file; restart run_all.bat afterwards.
rem   use_cameras.bat axis  -> Axis CCTV (cam_01) + laptop webcam (cam_02) + phone (cam_03) + USB entrance (cam_04)
rem   use_cameras.bat old   -> the camera setup from before 13 Nov (config\cameras.before_axis.yaml)
cd /d "%~dp0"
if /i "%~1"=="axis" (
  if not exist config\cameras.axis.yaml ( echo config\cameras.axis.yaml is missing & exit /b 1 )
  copy /y config\cameras.axis.yaml config\cameras.yaml >nul && echo Camera plan = AXIS. Now close NETRAX and start run_all.bat again.
  exit /b 0
)
if /i "%~1"=="old" (
  if not exist config\cameras.before_axis.yaml ( echo config\cameras.before_axis.yaml is missing & exit /b 1 )
  copy /y config\cameras.before_axis.yaml config\cameras.yaml >nul && echo Camera plan = OLD - before 13 Nov. Now close NETRAX and start run_all.bat again.
  exit /b 0
)
echo usage: use_cameras.bat axis   or   use_cameras.bat old
exit /b 1
