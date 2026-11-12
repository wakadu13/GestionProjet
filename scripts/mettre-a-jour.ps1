# Met Hangar à jour : sauvegarde complète, nouvelle version du code (git), dépendances, puis relance.
# À lancer par un double-clic sur scripts\mettre-a-jour.bat. Tes données (dossier data\) ne sont pas touchées.

. (Join-Path $PSScriptRoot 'hangar-commun.ps1')

function Stop-Ici([string]$message) {
    Write-Host $message -ForegroundColor Red
    Wait-Touche
    exit 1
}

Set-Location -LiteralPath $Depot
$env:PYTHONUTF8 = '1'

$uv = Get-Uv
if (-not $uv) { Stop-Ici 'uv est introuvable : installe-le (https://docs.astral.sh/uv/), puis recommence.' }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Stop-Ici 'git est introuvable : installe Git pour Windows (https://git-scm.com/download/win), puis recommence.'
}
if (-not (Test-Path -LiteralPath (Join-Path $Depot '.git'))) {
    Stop-Ici 'Ce dossier n''a pas été récupéré avec git (téléchargé en .zip ?) : télécharge la nouvelle version à la main.'
}

$modifies = git status --porcelain --untracked-files=no
if ($modifies) {
    Write-Host 'Des fichiers de Hangar ont été modifiés à la main :' -ForegroundColor Yellow
    $modifies | ForEach-Object { Write-Host "  $_" }
    Stop-Ici 'Mise à jour arrêtée pour ne pas écraser ces changements. Demande de l''aide, ou annule-les avec « git checkout -- . ».'
}

Write-Host '1/4 Sauvegarde complète de tes données…'
& $uv run hangar backup --complet --garder 5
if ($LASTEXITCODE -eq 1) { Stop-Ici 'La sauvegarde a échoué : rien n''a été changé.' }
# Code 2 : sauvegarde faite, mais des fichiers ouverts ailleurs n'ont pas pu être copiés (déjà affichés).

Write-Host '2/4 Récupération de la nouvelle version…'
$avant = git rev-parse HEAD
git pull --ff-only
if ($LASTEXITCODE -ne 0) { Stop-Ici 'La récupération a échoué (pas d''Internet ? version modifiée ?). Hangar n''a pas changé.' }
$apres = git rev-parse HEAD
# Une mise à jour précédente inachevée (dépendances pas installées) ou un Hangar lancé avec un autre code que
# celui du disque : on termine le travail, même si git n'a rien apporté de nouveau cette fois.
$marqueur = Join-Path (Get-DossierDonnees) 'mise-a-jour-inachevee'
$infos = Get-InfosLancement
$lance = if ($infos -and $infos.commit) { [string]$infos.commit } else { '' }
if ($avant -eq $apres -and -not (Test-Path -LiteralPath $marqueur) -and (-not $lance -or $lance -eq $apres)) {
    Write-Host 'Hangar était déjà à jour.' -ForegroundColor Green
    Wait-Touche
    exit 0
}
New-Item -ItemType File -Force -Path $marqueur | Out-Null

Write-Host '3/4 Arrêt de Hangar et installation des dépendances…'
# Hangar arrêté AVANT l'installation : Windows interdit de remplacer un programme en cours d'utilisation.
$tournait = Stop-Hangar
& $uv sync
if ($LASTEXITCODE -ne 0) {
    Stop-Ici ('L''installation des dépendances a échoué (pas d''Internet ?). Hangar est arrêté : relance ce ' +
              'script quand Internet revient, il reprendra là où il s''est arrêté.')
}
Remove-Item -LiteralPath $marqueur -Force -ErrorAction SilentlyContinue

Write-Host '4/4 Relance de Hangar…'
if (Start-HangarCache) {
    Write-Host 'Hangar a redémarré en arrière-plan avec la nouvelle version.' -ForegroundColor Green
} elseif ($tournait) {
    Write-Host 'Hangar a été arrêté : relance-le avec lancer.bat pour utiliser la nouvelle version.' -ForegroundColor Green
} else {
    Write-Host 'Mise à jour terminée : lance Hangar avec lancer.bat.' -ForegroundColor Green
}
Wait-Touche
