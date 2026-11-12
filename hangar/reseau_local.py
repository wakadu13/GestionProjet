"""Ouvrir Hangar sur le téléphone, en HTTPS, sur le réseau local (Wi-Fi de la maison, de l'école).

Pourquoi HTTPS : hors de cet ordinateur, le navigateur du téléphone n'accepte le wiki et les QCM hors ligne
(service worker) et les notifications qu'en HTTPS. L'appli Android, elle, marche déjà en http.

Comment : waitress ne sait pas faire de HTTPS. ``hangar run --lan-https`` lance donc Caddy (un petit serveur
gratuit, un seul fichier) devant Hangar :

- Hangar n'écoute que cet ordinateur (127.0.0.1) : personne sur le réseau ne lui parle directement, il peut donc
  croire les en-têtes X-Forwarded-* écrits par Caddy (adresse du visiteur, https) ;
- Caddy écoute le réseau en HTTPS sur le port 5443, avec sa propre autorité de certification (``tls internal``),
  rangée dans ``data/https/caddy`` ; le Caddyfile est réécrit à chaque lancement avec l'adresse IP du moment,
  et Caddy refait seul le certificat si elle a changé : l'autorité, installée une fois sur le téléphone, reste ;
- le certificat de l'autorité (public, sans secret) est proposé au téléphone par Hangar :
  ``/settings/certificat-hangar.crt``, avec son empreinte pour vérifier.

Caddy est cherché dans HANGAR_CADDY, ``data/outils/caddy.exe``, le PATH, puis là où winget l'installe.
``scripts\\partager-en-https.bat`` l'installe s'il manque et lance Hangar avec cette option.
"""
from __future__ import annotations

import base64
import hashlib
import os
import shutil
import socket
import subprocess
from pathlib import Path

PORT_HTTPS = 5443
NOM_CERTIFICAT = "certificat-hangar.crt"


def ip_reseau() -> str | None:
    """Adresse de cet ordinateur sur le réseau local (aucun paquet n'est envoyé), ou None sans réseau."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


def dossier_https(data_dir) -> Path:
    return Path(data_dir) / "https"


def trouver_caddy(data_dir) -> Path | None:
    exe = "caddy.exe" if os.name == "nt" else "caddy"
    candidats = []
    if os.environ.get("HANGAR_CADDY"):
        candidats.append(Path(os.environ["HANGAR_CADDY"]))
    candidats.append(Path(data_dir) / "outils" / exe)
    trouve = shutil.which("caddy")
    if trouve:
        candidats.append(Path(trouve))
    if os.environ.get("LOCALAPPDATA"):  # winget : la console déjà ouverte ne voit pas encore le PATH mis à jour
        candidats.append(Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "WinGet" / "Links" / exe)
    return next((c for c in candidats if c.is_file()), None)


def caddyfile(ip: str, port_hangar: int, stockage, port_https: int = PORT_HTTPS) -> str:
    """Configuration de Caddy : HTTPS sur ``ip:port_https`` vers Hangar (127.0.0.1:port_hangar)."""
    racine = str(Path(stockage)).replace("\\", "/").replace('"', "")
    return f"""# Écrit par Hangar à chaque lancement avec --lan-https : inutile de le modifier.
{{
	admin off
	skip_install_trust
	auto_https disable_redirects
	storage file_system {{
		root "{racine}"
	}}
}}

https://{ip}:{port_https} {{
	tls internal
	reverse_proxy 127.0.0.1:{port_hangar}
}}
"""


def certificat_racine(data_dir) -> Path:
    """Certificat de l'autorité de Caddy (créé à son premier lancement), à installer sur le téléphone."""
    return dossier_https(data_dir) / "caddy" / "pki" / "authorities" / "local" / "root.crt"


def empreinte(pem: bytes) -> str:
    """Empreinte SHA-256 d'un certificat PEM (comme l'affichent Android et iOS : « AB:CD:… »)."""
    texte = pem.decode("ascii", errors="ignore")
    debut, fin = "-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----"
    if debut not in texte or fin not in texte:
        raise ValueError("certificat illisible")
    corps = texte.split(debut, 1)[1].split(fin, 1)[0]
    der = base64.b64decode("".join(corps.split()))
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def demarrer_caddy(caddy: Path, data_dir, ip: str, port_hangar: int, port_https: int = PORT_HTTPS):
    """Écrit le Caddyfile et lance Caddy (sortie dans data/journal/caddy.log). Renvoie le processus."""
    dossier = dossier_https(data_dir)
    (dossier / "caddy").mkdir(parents=True, exist_ok=True)
    fichier = dossier / "Caddyfile"
    fichier.write_text(caddyfile(ip, port_hangar, dossier / "caddy", port_https), encoding="utf-8")
    journal = Path(data_dir) / "journal"
    journal.mkdir(parents=True, exist_ok=True)
    sortie = open(journal / "caddy.log", "ab")  # noqa: SIM115 - gardé ouvert tant que Caddy tourne
    # Caddy partage la console de Hangar (pas de CREATE_NO_WINDOW) : si on ferme la fenêtre ou fait Ctrl+C, Windows
    # l'arrête aussi, même quand Hangar n'a pas le temps de le faire (sinon il garderait le port 5443).
    try:
        return subprocess.Popen([str(caddy), "run", "--config", str(fichier), "--adapter", "caddyfile"],
                                stdout=sortie, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    finally:
        sortie.close()  # le processus garde sa propre copie


def arreter(processus) -> None:
    if processus is None or processus.poll() is not None:
        return
    processus.terminate()
    try:
        processus.wait(timeout=5)
    except subprocess.TimeoutExpired:
        processus.kill()


def fin_du_journal(data_dir, lignes: int = 5) -> str:
    try:
        texte = (Path(data_dir) / "journal" / "caddy.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(texte.strip().splitlines()[-lignes:])


HOTES_LOCAUX = ("127.0.0.1", "localhost", "::1")


def adresse_telephone(app, request) -> dict | None:
    """Adresse à ouvrir sur le téléphone : {"url", "https"}, ou None si Hangar n'est ouvert qu'à cet ordinateur.

    ``app.config["PARTAGE"]`` est posé par ``hangar run`` : {"host", "port", "https_port"}."""
    partage = app.config.get("PARTAGE") or {}
    hote = request.host.rsplit(":", 1)[0].strip("[]") if not request.host.endswith("]") else request.host.strip("[]")
    if hote not in HOTES_LOCAUX:  # page ouverte depuis le réseau : l'adresse en cours marche déjà
        return {"url": request.host_url, "https": request.scheme == "https"}
    ip = ip_reseau()
    if not ip:
        return None
    if partage.get("https_port"):
        return {"url": f"https://{ip}:{partage['https_port']}/", "https": True}
    if partage.get("host") not in (None, *HOTES_LOCAUX):
        return {"url": f"http://{ip}:{partage['port']}/", "https": False}
    return None


def message_demarrage(url: str, https: bool, avec_qr: bool = True) -> str:
    """Bloc affiché dans la console : l'adresse pour le téléphone et son QR code."""
    from . import qr
    lignes = [f"  → Sur ton téléphone (même Wi-Fi) : {url}"]
    if avec_qr:
        lignes += ["    Scanne ce QR code avec l’appareil photo du téléphone :", ""]
        lignes += ["    " + ligne for ligne in qr.terminal(url).splitlines()]
        lignes.append("")
    if https:
        lignes.append("    Première fois : installe le certificat de Hangar sur le téléphone (Réglages › Profil ›\n"
                      "    « Sur ton téléphone » explique comment), sinon le navigateur affichera un avertissement.")
    return "\n".join(lignes)
