"""Wiki : coffre Obsidian de cours (port de github.com/wakadu13/Revision).

Les pages sont les fichiers .md du coffre (``WIKI_DIR`` : ``data/vault`` ou ``HANGAR_VAULT``).

- ``vault`` : index du coffre (notes, pièces jointes, liens ``[[…]]``, alias, tags, recherche, arbre),
  relu au plus toutes les ``WIKI_VERIF_INTERVALLE`` secondes ;
- ``render`` : rendu Obsidian (callouts, intégrations, propriétés, formules, mermaid…) ;
- ``latex`` : cours LaTeX → Markdown ; ``historique`` : versions des pages (``DATA_DIR/wiki_versions``) ;
- ``texte_pdf`` : texte des PDF pour la recherche ; ``uploads`` : import de fichiers ;
- ``cartes``, ``demander`` : logique pure reprise telle quelle, utilisée par le module d'IA ;
- ``commun`` : droits, menu de gauche, contexte du panneau IA ; ``installation`` : premier lancement et
  ``hangar wiki-import`` ; ``views`` : routes (blueprint ``wiki``).
"""
