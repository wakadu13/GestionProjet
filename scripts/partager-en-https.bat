@echo off
REM Ouvre Hangar au telephone, en HTTPS, sur le meme Wi-Fi (installe Caddy la premiere fois).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0partager-en-https.ps1" %*
