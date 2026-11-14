@echo off
rem platmon, experimental Windows zip: serve this folder's platmon.ini and web page as your user and open
rem the page in the browser once the server answers. Ctrl+C or closing this window stops it.
setlocal
cd /d "%~dp0"
set "PORT=9797"
for /f "tokens=2 delims== " %%p in ('findstr /b /r /c:"port *=" platmon.ini') do set "PORT=%%p"
echo platmon: http://127.0.0.1:%PORT%/ (Ctrl+C stops it)
rem curl.exe ships with Windows 10 1803 and later; ping waits a second where timeout needs a console
start "" /b cmd /d /c "for /l %%i in (1,1,30) do (curl.exe -fs -o nul http://127.0.0.1:%PORT%/api/status && (start "" http://127.0.0.1:%PORT%/ & exit) || ping -n 2 127.0.0.1 >nul)"
platmon-server.exe serve platmon.ini --web web
echo platmon stopped.
pause
