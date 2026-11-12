# Fonctions partagées par les scripts Windows de Hangar (chargé par les autres scripts, pas à lancer seul).

$ErrorActionPreference = 'Stop'
$Depot = Split-Path -Parent $PSScriptRoot
$NomRaccourci = 'Hangar.lnk'
$PortHangar = 5000

function Get-Uv {
    $commande = Get-Command uv -ErrorAction SilentlyContinue
    if ($commande) { return $commande.Source }
    $parDefaut = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (Test-Path -LiteralPath $parDefaut) { return $parDefaut }
    return $null
}

function Get-RaccourciDemarrage {
    # Dossier « Démarrage » de la session (shell:startup) : pas besoin d'être administrateur.
    return Join-Path ([Environment]::GetFolderPath('Startup')) $NomRaccourci
}

function Get-ProcessusHangar {
    # Le programme qui écoute sur le port de Hangar, s'il s'agit bien de Python (lancé par uv).
    try {
        $connexions = Get-NetTCPConnection -LocalPort $PortHangar -State Listen -ErrorAction Stop
    } catch {
        return @()
    }
    $trouves = @()
    foreach ($c in $connexions) {
        $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
        if ($p -and $p.ProcessName -match '^(python|pythonw|uv)') { $trouves += $p }
    }
    return $trouves
}

function Stop-Hangar {
    # Arrête le Hangar qui tourne (fenêtre ou démarrage caché). Renvoie $true s'il tournait.
    $processus = @(Get-ProcessusHangar)
    if ($processus.Count -eq 0) { return $false }
    foreach ($p in $processus) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    return $true
}

function Start-HangarCache {
    # Lance Hangar sans fenêtre, comme au démarrage de la session.
    $raccourci = Get-RaccourciDemarrage
    if (Test-Path -LiteralPath $raccourci) {
        Start-Process -FilePath $raccourci
        return $true
    }
    return $false
}

function Wait-Touche {
    Write-Host ''
    Read-Host 'Appuie sur Entrée pour fermer cette fenêtre' | Out-Null
}
