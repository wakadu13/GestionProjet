@echo off
REM Lance Hangar (installe automatiquement les dependances au premier lancement).
cd /d "%~dp0"
where uv >nul 2>nul || (echo uv est introuvable : https://docs.astral.sh/uv/ & pause & exit /b 1)
uv run hangar run %*
pause
