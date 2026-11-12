# Hangar ne démarre plus avec ta session Windows, et le Hangar lancé en arrière-plan est arrêté.
# À lancer par un double-clic sur scripts\ne-plus-demarrer-avec-windows.bat.

. (Join-Path $PSScriptRoot 'hangar-commun.ps1')

try {
    $raccourci = Get-RaccourciDemarrage
    if (Test-Path -LiteralPath $raccourci) {
        Remove-Item -LiteralPath $raccourci -Force
        Write-Host 'Hangar ne démarrera plus avec ta session.' -ForegroundColor Green
    } else {
        Write-Host 'Hangar ne démarrait pas avec ta session : rien à retirer.'
    }
    if (Stop-Hangar) {
        Write-Host 'Le Hangar qui tournait a été arrêté. Pour le relancer : double-clic sur lancer.bat.'
    }
} catch {
    Write-Host "Échec : $($_.Exception.Message)" -ForegroundColor Red
    Wait-Touche
    exit 1
}
Wait-Touche
