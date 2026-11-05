@echo off
rem Tablet companion mode - Miracast route (2026-11-05)
rem 1) open the Windows wireless display receiver (""Connect"" app)
rem 2) show the pet page URL for the tablet browser
setlocal

echo [1/3] Opening the wireless display receiver (Miracast)...
start "" explorer.exe "shell:appsFolder\Microsoft.PPIProjection_cw5n1h2txyewy!Microsoft.PPIProjection"
timeout /t 4 /nobreak >nul

echo.
echo [2/3] On the tablet: swipe down the control center -^> Wireless Display
echo       -^> pick this PC, then accept the prompt on the PC screen.
echo.

echo [3/3] Pet page for the tablet browser (fullscreen landscape is best):
python -c "print('   http://<PC-Tailscale-IP>:17860/pet?token=' + open(r'<path-to>\.eye-token').read().strip())"
echo.
echo pc_body picks the mirror window up automatically - no clicking needed.
echo Check status:  curl -H "Authorization: Bearer ^<token^>" http://<PC-Tailscale-IP>:17860/health
pause