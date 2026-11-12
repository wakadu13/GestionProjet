"""Commandes du wiki (hangar …) :

- ``hangar wiki-import <source> [--remplacer] [--images]`` : copie un coffre Obsidian dans le wiki, depuis
  un dossier local ou une adresse git (ex. https://github.com/wakadu13/Revision : clone
  superficiel, puis copie de son dossier ``vault/``), sans ``.obsidian``, ``.trash`` ni ``.git`` ;
  ``hangar wiki-import --images`` (avec ou sans source) télécharge en plus dans le coffre les images
  hébergées sur d'autres sites, et réécrit les liens des pages en ``![[image.png]]`` ;
- ``hangar wiki-indexer-pdf`` : lit d'un coup le texte de tous les PDF du coffre (recherche) ;
- ``hangar wiki-coffre [<dossier> | --defaut]`` : affiche, mémorise (dans data/coffre.txt) ou oublie le dossier du
  coffre ; vaut pour tous les lancements, lancer.bat compris (la variable HANGAR_VAULT reste prioritaire).
"""
from __future__ import annotations

import sys


def cmd_wiki_import(args):
    from .. import create_app
    from .installation import importer_coffre
    if not args.source and not args.images:
        print("✗ Donne le dossier ou l'adresse git du coffre à importer "
              "(ou --images pour télécharger les images externes du wiki).")
        sys.exit(2)
    app = create_app(args.data)
    with app.app_context():
        destination = app.config["WIKI_DIR"]
        if args.source:
            print(f"Import de {args.source} vers {destination}…")
            try:
                bilan = importer_coffre(args.source, destination, remplacer=args.remplacer)
            except (OSError, RuntimeError, ValueError) as exc:
                print(f"✗ {exc}")
                sys.exit(1)
            _afficher_bilan(bilan, images=args.images)
        if args.images:
            _telecharger_images()


def _afficher_bilan(bilan, images=False):
    print(f"✓ {len(bilan.copies)} fichier(s) copié(s), {len(bilan.remplaces)} remplacé(s) "
          f"({bilan.notes} page(s) .md au total).")
    if bilan.ignores:
        print(f"  {len(bilan.ignores)} fichier(s) existaient déjà et ont été gardés "
              "(ajoute --remplacer pour les écraser ; l'ancien texte des pages reste dans l'historique).")
    if bilan.exclus:
        print(f"  {len(bilan.exclus)} lien(s) symbolique(s) non copié(s) : ils mènent hors du dossier importé, "
              "vers un fichier caché (réglages de git…) ou nulle part "
              f"({', '.join(bilan.exclus[:5])}{'…' if len(bilan.exclus) > 5 else ''}).")
    if bilan.echecs:
        print(f"  {len(bilan.echecs)} fichier(s) non copié(s) : lecture ou écriture refusée par l'ordinateur "
              f"({', '.join(bilan.echecs[:5])}{'…' if len(bilan.echecs) > 5 else ''}).")
    if bilan.images_externes and not images:
        print(f"  {bilan.images_externes} image(s) de {bilan.pages_images_externes} page(s) sont hébergées sur "
              "d'autres sites : le wiki n'affiche qu'un lien vers elles. Ajoute --images pour les enregistrer "
              "dans le wiki.")


def _telecharger_images():
    from .images_externes import telecharger
    from .vault import get_vault

    print("Téléchargement des images externes du wiki…")

    def progression(i, total, url):
        print(f"  [{i}/{total}] {url}")

    bilan = telecharger(get_vault(), auteur="import", progression=progression)
    if not bilan.images:
        print("✓ Aucune image externe dans le wiki.")
        return
    print(f"✓ {bilan.images} image(s) externe(s) trouvée(s) dans {bilan.pages} page(s) ; "
          f"{len(bilan.telechargees)} enregistrée(s) dans le wiki, {len(bilan.pages_modifiees)} page(s) mise(s) à jour "
          "(l'ancien texte reste dans l'historique).")
    for url, raison in bilan.echecs:
        print(f"  ✗ {url} : {raison}")
    for rel in bilan.pages_illisibles:
        print(f"  ✗ {rel} : page illisible, non mise à jour")


def cmd_wiki_indexer_pdf(args):
    from .. import create_app
    from . import texte_pdf
    from .vault import get_vault
    app = create_app(args.data)
    with app.app_context():
        n = texte_pdf.indexer(get_vault())
    print(f"✓ Texte de {n} PDF lu (les autres étaient déjà à jour).")


def cmd_wiki_coffre(args):
    import os
    from pathlib import Path

    from .. import BASE_DIR
    from . import coffre
    data = Path(args.data or os.environ.get("HANGAR_DATA") or BASE_DIR / "data").resolve()
    if args.dossier or args.defaut:
        data.mkdir(parents=True, exist_ok=True)
        try:
            retenu = coffre.memoriser(data, None if args.defaut else args.dossier)
        except (OSError, ValueError) as exc:
            print(f"✗ {exc}")
            if isinstance(exc, FileNotFoundError):
                print("  Vérifie le chemin (copie-le depuis la barre d'adresse de l'Explorateur), et que le disque "
                      "ou la clé est branché. Mets-le entre guillemets s'il contient des espaces.")
            sys.exit(1)
        print(f"✓ Coffre du wiki : {retenu}" if retenu else "✓ Coffre du wiki : dossier par défaut (data/vault).")
        print("  Retenu pour tous les lancements (lancer.bat compris). Relance Hangar s'il tourne déjà.")
    dossier, origine = coffre.choisir(data)
    if not (args.dossier or args.defaut):
        print(f"Coffre du wiki : {dossier} ({coffre.ORIGINES[origine]})")
    if origine == coffre.VARIABLE:
        print("  ! La variable HANGAR_VAULT est définie : c'est elle qui compte tant qu'elle existe.")
    if origine != coffre.DEFAUT and not dossier.is_dir():
        print("  ! Ce dossier est introuvable : le wiki affichera un avertissement au lieu de tes pages.")
        sys.exit(1)


def register(sub) -> dict:
    """Ajoute les sous-commandes du wiki à l'analyseur ; retourne {nom: fonction(args)}."""
    wi = sub.add_parser("wiki-import", help="importer un coffre Obsidian (dossier local ou dépôt git) dans le wiki")
    wi.add_argument("source", nargs="?", default="",
                    help="dossier du coffre, ou adresse git (le dossier vault/ du dépôt est pris s'il existe)")
    wi.add_argument("--remplacer", action="store_true",
                    help="écraser les fichiers déjà présents (l'ancien texte des pages reste dans l'historique)")
    wi.add_argument("--images", action="store_true",
                    help="télécharger dans le wiki les images hébergées sur d'autres sites et mettre à jour les pages")
    sub.add_parser("wiki-indexer-pdf", help="lire le texte de tous les PDF du wiki (recherche)")
    wc = sub.add_parser("wiki-coffre", help="afficher ou choisir le dossier du coffre du wiki (ton coffre Obsidian)")
    wc.add_argument("dossier", nargs="?", default="", help="dossier du coffre à utiliser à chaque lancement")
    wc.add_argument("--defaut", action="store_true", help="revenir au dossier par défaut (data/vault)")
    return {"wiki-import": cmd_wiki_import, "wiki-indexer-pdf": cmd_wiki_indexer_pdf,
            "wiki-coffre": cmd_wiki_coffre}
