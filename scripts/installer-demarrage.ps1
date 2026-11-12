# Hangar démarre tout seul, sans fenêtre, à chaque ouverture de ta session Windows.
# Ainsi les rappels, le résumé du matin et les notifications partent même si tu n'as pas lancé lancer.bat.
# À lancer par un double-clic sur scripts\demarrer-avec-windows.bat. Pour annuler : ne-plus-demarrer-avec-windows.bat.

. (Join-Path $PSScriptRoot 'hangar-commun.ps1')

try {
    $uv = Get-Uv
    if (-not $uv) {
        Write-Host 'uv est introuvable : installe-le d''abord (https://docs.astral.sh/uv/), puis recommence.' -ForegroundColor Red
        Wait-Touche
        exit 1
    }
    $journal = Join-Path $Depot 'data\journal'
    New-Item -ItemType Directory -Force -Path $journal | Out-Null
    $console = Join-Path $journal 'console.log'

    # Commande lancée en arrière-plan. Encodée en base64 : aucun souci d'espaces ou d'apostrophes dans les chemins.
    $commande = @"
Set-Location -LiteralPath '$($Depot -replace "'", "''")'
`$env:PYTHONUTF8 = '1'
& '$($uv -replace "'", "''")' run hangar run --no-browser *>> '$($console -replace "'", "''")'
"@
    $encodee = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($commande))

    $raccourci = Get-RaccourciDemarrage
    $shell = New-Object -ComObject WScript.Shell
    $lien = $shell.CreateShortcut($raccourci)
    $lien.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $lien.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -EncodedCommand $encodee"
    $lien.WorkingDirectory = $Depot
    $lien.WindowStyle = 7  # réduite : rien ne s'affiche
    $lien.Description = 'Hangar (démarrage sans fenêtre)'
    $lien.Save()

    Write-Host 'C''est fait : Hangar démarrera tout seul, sans fenêtre, à chaque ouverture de ta session.' -ForegroundColor Green
    Write-Host "Raccourci créé : $raccourci"
    Write-Host "Messages du serveur : $console"
    Write-Host 'Pour ouvrir Hangar : double-clic sur lancer.bat (il ouvre simplement le navigateur), ou http://127.0.0.1:5000'

    if (@(Get-ProcessusHangar).Count -eq 0) {
        Start-HangarCache | Out-Null
        Write-Host 'Hangar est lancé en arrière-plan dès maintenant.'
    } else {
        Write-Host 'Hangar tourne déjà : ferme sa fenêtre si tu veux qu''il passe en arrière-plan, puis rouvre ta session.'
    }
} catch {
    Write-Host "Échec : $($_.Exception.Message)" -ForegroundColor Red
    Wait-Touche
    exit 1
}
Wait-Touche
