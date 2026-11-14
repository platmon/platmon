@echo off
rem platmon, experimental Windows zip: install or upgrade the Windows service "platmon".
rem Run as administrator (right-click, Run as administrator). The service runs as LocalSystem and starts
rem with Windows, so its files go to %ProgramFiles%\platmon, which only administrators can change:
rem platmon-server.exe and the web page are replaced, an existing platmon.ini is kept.
setlocal
set "DEST=%ProgramFiles%\platmon"
fltmc >nul 2>&1 || (echo Run this as administrator: right-click install-service.cmd, Run as administrator.& pause & exit /b 1)
cd /d "%~dp0"
if not exist platmon-server.exe (echo platmon-server.exe is not in this folder: run the script from the unzipped folder.& pause & exit /b 1)
if /i "%CD%"=="%DEST%" (echo Run the script from the unzipped folder, not from %DEST%.& pause & exit /b 1)

sc.exe query platmon >nul 2>&1
if errorlevel 1 (set "EXISTS=") else (set "EXISTS=1")
if defined EXISTS call :stop || (pause & exit /b 1)

if not exist "%DEST%" mkdir "%DEST%" || (pause & exit /b 1)
copy /y platmon-server.exe "%DEST%\" >nul || (pause & exit /b 1)
for %%f in (README.txt LICENSE LICENSE-MIT uninstall-service.cmd) do copy /y %%f "%DEST%\" >nul
if exist "%DEST%\platmon.ini" (echo Keeping %DEST%\platmon.ini.) else (copy /y platmon.ini "%DEST%\" >nul || (pause & exit /b 1))
if exist "%DEST%\web" rmdir /s /q "%DEST%\web"
xcopy /e /i /q /y web "%DEST%\web" >nul || (pause & exit /b 1)

set "BIN=\"%DEST%\platmon-server.exe\" service \"%DEST%\platmon.ini\" --web \"%DEST%\web\""
if defined EXISTS (
  sc.exe config platmon start= auto binPath= "%BIN%" >nul || (pause & exit /b 1)
) else (
  sc.exe create platmon start= auto DisplayName= platmon binPath= "%BIN%" >nul || (pause & exit /b 1)
)
sc.exe description platmon "platmon, a lightweight platform monitor (experimental on Windows)" >nul
sc.exe start platmon >nul || (echo The service did not start: "%DEST%\platmon-server.exe" config-check "%DEST%\platmon.ini" shows why.& pause & exit /b 1)

set "PORT=9797"
for /f "tokens=2 delims== " %%p in ('findstr /b /r /c:"port *=" "%DEST%\platmon.ini"') do set "PORT=%%p"
echo platmon is installed and running: http://127.0.0.1:%PORT%/
echo Settings: %DEST%\platmon.ini (then: sc.exe stop platmon, sc.exe start platmon). To remove it: uninstall-service.cmd
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
