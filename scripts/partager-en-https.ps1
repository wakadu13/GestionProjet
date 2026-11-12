# Ouvre Hangar à ton téléphone, en HTTPS, sur le même Wi-Fi : le wiki et les QCM hors ligne et les notifications
# y marchent alors aussi. À lancer par un double-clic sur scripts\partager-en-https.bat.
# La première fois, installe Caddy (petit serveur gratuit qui chiffre la connexion) et ouvre le port 5443 dans le
# pare-feu de Windows (réseaux privés seulement). Ensuite, lance Hangar avec « --lan-https » : l'adresse et son
# QR code s'affichent dans cette fenêtre et dans Réglages › Profil › Sur ton téléphone.

. (Join-Path $PSScriptRoot 'hangar-commun.ps1')
$PortHttps = 5443

function Get-Caddy {
    # Même ordre de recherche que Hangar (hangar/reseau_local.py)
    if ($env:HANGAR_CADDY -and (Test-Path -LiteralPath $env:HANGAR_CADDY)) { return $env:HANGAR_CADDY }
    $local = Join-Path (Get-DossierDonnees) 'outils\caddy.exe'
    if (Test-Path -LiteralPath $local) { return $local }
    $commande = Get-Command caddy -ErrorAction SilentlyContinue
    if ($commande) { return $commande.Source }
    if ($env:LOCALAPPDATA) {
        $winget = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\caddy.exe'
        if (Test-Path -LiteralPath $winget) { return $winget }
    }
    return $null
}

function Install-Caddy {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host 'Installation de Caddy avec winget (le gestionnaire de logiciels de Windows)...'
        & $winget.Source install --id CaddyServer.Caddy --exact --silent --accept-source-agreements --accept-package-agreements | Out-Host
        $trouve = Get-Caddy
        if ($trouve) { return $trouve }
    }
    # Sans winget (ou s'il a échoué) : le fichier officiel, rangé dans data\outils\caddy.exe
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'amd64' }
    $dossier = Join-Path (Get-DossierDonnees) 'outils'
    New-Item -ItemType Directory -Force -Path $dossier | Out-Null
    $cible = Join-Path $dossier 'caddy.exe'
    Write-Host 'Téléchargement de Caddy depuis caddyserver.com...'
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $partiel = "$cible.part"
    Invoke-WebRequest -UseBasicParsing -Uri "https://caddyserver.com/api/download?os=windows&arch=$arch" -OutFile $partiel
    Move-Item -Force -LiteralPath $partiel -Destination $cible
    return $cible
}

function Open-PareFeu {
    # Une règle « Hangar HTTPS » pour le port 5443, sur les réseaux privés seulement (pas les Wi-Fi publics).
    if (Get-NetFirewallRule -DisplayName 'Hangar HTTPS' -ErrorAction SilentlyContinue) { return }
    Write-Host ''
    Write-Host "Windows va te demander l'autorisation d'ouvrir le port $PortHttps aux appareils de ton réseau privé." -ForegroundColor Yellow
    $regle = "New-NetFirewallRule -DisplayName 'Hangar HTTPS' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $PortHttps -Profile Private"
    try {
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -ArgumentList @('-NoProfile', '-Command', $regle)
    } catch {
        Write-Host "Pas d'autorisation : si le téléphone n'arrive pas à se connecter, accepte la fenêtre du pare-feu de Windows au premier lancement de Caddy (coche « Réseaux privés »)." -ForegroundColor Yellow
    }
}

function Test-ReseauPrive {
    try {
        $publics = @(Get-NetConnectionProfile -ErrorAction Stop | Where-Object { $_.NetworkCategory -eq 'Public' })
    } catch {
        return
    }
    if ($publics.Count -gt 0) {
        Write-Host ''
        Write-Host "Ton Wi-Fi « $($publics[0].Name) » est réglé en réseau public : Windows y bloque le téléphone." -ForegroundColor Yellow
        Write-Host "Si c'est ton Wi-Fi (maison, chambre), passe-le en réseau privé : Paramètres > Réseau et Internet > Wi-Fi > $($publics[0].Name) > Type de profil réseau : Privé."
        Write-Host 'Sur un Wi-Fi public (gare, café), ne partage pas Hangar.'
    }
}

try {
    $uv = Get-Uv
    if (-not $uv) {
        Write-Host 'uv est introuvable : installe-le d''abord (https://docs.astral.sh/uv/), puis recommence.' -ForegroundColor Red
        Wait-Touche
        exit 1
    }
    $caddy = Get-Caddy
    if (-not $caddy) {
        $caddy = Install-Caddy
        Write-Host "Caddy est installé : $caddy" -ForegroundColor Green
    }
    $env:HANGAR_CADDY = $caddy
    Open-PareFeu
    Test-ReseauPrive

    if (@(Get-ProcessusHangar).Count -gt 0) {
        Write-Host ''
        Write-Host 'Hangar tourne déjà sans partage : je l''arrête pour le relancer en HTTPS.'
        Stop-Hangar | Out-Null
    }
    Write-Host ''
    Write-Host 'Garde cette fenêtre ouverte tant que tu utilises Hangar sur le téléphone (Ctrl+C pour arrêter).'
    Set-Location -LiteralPath $Depot
    $env:PYTHONUTF8 = '1'
    & $uv run hangar run --lan-https @args
} catch {
    Write-Host "Échec : $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Sans Internet, installe Caddy à la main : copie caddy.exe dans le dossier data\outils de Hangar, puis recommence.'
    Wait-Touche
    exit 1
}
Wait-Touche
