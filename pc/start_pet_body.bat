@echo off
rem Feiyu PC body autostart. The watchdog (wd_pc_body.py, pythonw, no window)
rem owns ALL spawning: pet :7340 + pc_body :17860, port-probe based, detached.
rem 2026-11-05. Logs: wd_pc_body.log / pc_body.log / pet_standalone.log (append).
setlocal

set PETDIR=D:\StardewAI\dsh-pet
set PYEXE=C:\Users\Administrator\AppData\Local\Programs\Python\Python310\python.exe
set PYWEXE=C:\Users\Administrator\AppData\Local\Programs\Python\Python310\pythonw.exe

rem watchdog dedup (pythonw, port-mutex inside script)
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter \"name='pythonw.exe'\" | Where-Object { $_.CommandLine -match 'wd_pc_body\.py' }) { exit 7 }"
if %errorlevel%==7 goto :wddone
start "" "%PYWEXE%" "%PETDIR%\wd_pc_body.py"
:wddone

rem immediate once-check so the body comes up without waiting 60s
"%PYWEXE%" "%PETDIR%\wd_pc_body.py" once

endlocal
