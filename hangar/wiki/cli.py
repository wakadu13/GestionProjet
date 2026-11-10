"""Commandes du wiki (hangar …) :

- ``hangar wiki-import <source> [--remplacer]`` : copie un coffre Obsidian dans le wiki, depuis
  un dossier local ou une adresse git (ex. https://github.com/wakadu13/Revision : clone
  superficiel, puis copie de son dossier ``vault/``), sans ``.obsidian``, ``.trash`` ni ``.git`` ;
- ``hangar wiki-indexer-pdf`` : lit d'un coup le texte de tous les PDF du coffre (recherche).
"""
from __future__ import annotations

import sys


def cmd_wiki_import(args):
    from .. import create_app
    from .installation import importer_coffre
    app = create_app(args.data)
    with app.app_context():
        destination = app.config["WIKI_DIR"]
        print(f"Import de {args.source} vers {destination}…")
        try:
            bilan = importer_coffre(args.source, destination, remplacer=args.remplacer)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"✗ {exc}")
            sys.exit(1)
    print(f"✓ {len(bilan.copies)} fichier(s) copié(s), {len(bilan.remplaces)} remplacé(s) "
          f"({bilan.notes} page(s) .md au total).")
    if bilan.ignores:
        print(f"  {len(bilan.ignores)} fichier(s) existaient déjà et ont été gardés "
              "(ajoute --remplacer pour les écraser ; l'ancien texte des pages reste dans l'historique).")


def cmd_wiki_indexer_pdf(args):
    from .. import create_app
    from . import texte_pdf
    from .vault import get_vault
    app = create_app(args.data)
    with app.app_context():
        n = texte_pdf.indexer(get_vault())
    print(f"✓ Texte de {n} PDF lu (les autres étaient déjà à jour).")


def register(sub) -> dict:
    """Ajoute les sous-commandes du wiki à l'analyseur ; retourne {nom: fonction(args)}."""
    wi = sub.add_parser("wiki-import", help="importer un coffre Obsidian (dossier local ou dépôt git) dans le wiki")
    wi.add_argument("source", help="dossier du coffre, ou adresse git (le dossier vault/ du dépôt est pris s'il existe)")
    wi.add_argument("--remplacer", action="store_true",
                    help="écraser les fichiers déjà présents (l'ancien texte des pages reste dans l'historique)")
    sub.add_parser("wiki-indexer-pdf", help="lire le texte de tous les PDF du wiki (recherche)")
    return {"wiki-import": cmd_wiki_import, "wiki-indexer-pdf": cmd_wiki_indexer_pdf}
