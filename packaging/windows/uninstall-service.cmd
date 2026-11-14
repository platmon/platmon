@echo off
rem platmon, experimental Windows zip: stop and remove the Windows service "platmon" and its files in
rem %ProgramFiles%\platmon, keeping platmon.ini there (delete the folder to remove it too).
rem Run as administrator (right-click, Run as administrator).
setlocal
set "DEST=%ProgramFiles%\platmon"
fltmc >nul 2>&1 || (echo Run this as administrator: right-click uninstall-service.cmd, Run as administrator.& pause & exit /b 1)

sc.exe query platmon >nul 2>&1
if errorlevel 1 (echo No platmon service is installed.) else (
  call :stop || (pause & exit /b 1)
  sc.exe delete platmon >nul || (pause & exit /b 1)
  echo The platmon service is removed.
)
if exist "%DEST%\web" rmdir /s /q "%DEST%\web"
for %%f in (platmon-server.exe README.txt LICENSE LICENSE-MIT) do if exist "%DEST%\%%f" del /q "%DEST%\%%f"
rem this script may run from that folder: it stays then, with platmon.ini
if /i not "%~dp0"=="%DEST%\" if exist "%DEST%\uninstall-service.cmd" del /q "%DEST%\uninstall-service.cmd"
if exist "%DEST%\platmon.ini" (echo Kept %DEST%\platmon.ini.) else (rmdir "%DEST%" 2>nul)
pause
exit /b 0

:stop
sc.exe stop platmon >nul 2>&1
for /l %%i in (1,1,30) do (
  sc.exe query platmon | find "STOPPED" >nul && exit /b 0
  ping -n 2 127.0.0.1 >nul
)
echo The platmon service did not stop.
exit /b 1
