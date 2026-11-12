@echo off
REM Met Hangar a jour : sauvegarde complete, nouvelle version, dependances, puis relance.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0mettre-a-jour.ps1"
