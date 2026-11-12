@echo off
REM Hangar ne demarre plus avec ta session Windows (et le Hangar lance en arriere-plan est arrete).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0retirer-demarrage.ps1"
