@echo off
REM Hangar demarre tout seul, sans fenetre, a chaque ouverture de ta session Windows.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer-demarrage.ps1"
