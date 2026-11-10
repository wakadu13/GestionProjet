"""Commandes des QCM (hangar …).

`uv run hangar qcm-verifier [fichier.json | dossier …]` : vérifie des fichiers de questions ou
de langue (par exemple écrits par une IA) avant de les déposer dans le coffre ; sans argument,
tout le dossier QCM/ du coffre (port de la commande `verifier_qcm` de Revision).
"""
from __future__ import annotations

import sys
from pathlib import Path

VERT, ROUGE, JAUNE, FIN = "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def _couleur(texte, code):
    return f"{code}{texte}{FIN}" if sys.stdout.isatty() else texte


def cmd_verifier(args) -> None:
    from .verifier import verifier_fichier

    chemins = [Path(c) for c in args.chemins]
    if not chemins:
        from .. import create_app
        app = create_app(args.data)
        chemins = [Path(app.config["WIKI_DIR"]) / "QCM"]
    fichiers = []
    for p in chemins:
        if p.is_dir():
            fichiers += [(f, p) for f in sorted(p.rglob("*.json"))
                         if not any(part.startswith(".") for part in f.relative_to(p).parts)]
        elif p.is_file():
            fichiers.append((p, p.parent))
        else:
            print(_couleur(f"✗ Introuvable : {p}", ROUGE))
            sys.exit(1)

    invalides = 0
    for f, racine in fichiers:
        ok, resume, avert = verifier_fichier(f, racine)
        if ok:
            print(_couleur("✓ ", VERT) + str(f))
            print(f"    {resume}")
            for a in avert:
                print(_couleur(f"    ⚠ {a}", JAUNE))
        else:
            invalides += 1
            print(_couleur("✗ ", ROUGE) + str(f))
            print(_couleur(f"    {resume}", ROUGE))
    print()
    if invalides:
        print(_couleur(f"{invalides} fichier(s) invalide(s) : ils seraient ignorés par Hangar. "
                       "Recolle l'erreur à l'IA pour qu'elle corrige.", ROUGE))
        sys.exit(1)
    print(_couleur(f"{len(fichiers)} fichier(s) valide(s).", VERT))


def register(sub) -> dict:
    p = sub.add_parser("qcm-verifier", help="vérifier des fichiers de QCM ou de langue (.json)")
    p.add_argument("chemins", nargs="*", help="fichiers .json ou dossiers (défaut : QCM/ du coffre)")
    return {"qcm-verifier": cmd_verifier}
