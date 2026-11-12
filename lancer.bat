@echo off
REM Lance Hangar (installe automatiquement les dependances au premier lancement).
REM Si Hangar tourne deja (fenetre ouverte ou demarrage avec Windows), ouvre simplement le navigateur.
REM Coffre du wiki : choisi une fois pour toutes avec  uv run hangar wiki-coffre "C:\chemin\du\coffre"
REM (memorise dans data\coffre.txt, donc respecte ici aussi). Pour forcer un autre coffre pour ce lancement :
REM set HANGAR_VAULT=C:\chemin\du\coffre
REM Autres outils (double-clic) : scripts\demarrer-avec-windows.bat, scripts\mettre-a-jour.bat
cd /d "%~dp0"
REM Sorties en UTF-8 (symboles affiches par les commandes, meme quand la sortie est redirigee)
set PYTHONUTF8=1
where uv >nul 2>nul || (echo uv est introuvable : https://docs.astral.sh/uv/ & pause & exit /b 1)
uv run hangar run %*
pause
