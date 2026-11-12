"""Agenda personnel : emploi du temps importé (.ics), tâches, rappels par e-mail, vues calendrier.

Un module par sujet :
- `commun` : réglages de l'import, couleurs, libellés et formats de dates ;
- `imports` : import de l'emploi du temps (.ics) et annulation d'un import ;
- `taches` : tâches, répétitions, occurrences ;
- `rappels` : rappels des tâches et des séances, et leur envoi au bon moment ;
- `courriels` : e-mails et notifications des rappels, résumé quotidien ;
- `source` : emploi du temps récupéré tout seul (dossier surveillé ou adresse .ics) ;
- `travaux` : travaux de fond lancés par le fil des e-mails (`run_jobs`) ;
- `vues` : semaine, mois, liste, tableau de bord, données des fenêtres de détail ;
- `exportation` : export .ics et abonnement en lecture seule ;
- `liens` : séance ↔ collection du wiki ↔ catégorie de QCM, dates d'examen des QCM.

Tous leurs noms restent accessibles ici (`from hangar import agenda; agenda.week_view(…)`), comme avant le
découpage. Entre eux, les modules passent par le paquet (`A.nom`) : remplacer `agenda.nom` dans un test
(`mock.patch.object(agenda, "send_reminder")`, `agenda.now`…) agit donc aussi sur les autres modules.

Règle des imports : **le dernier fichier importé fait foi** (détail dans `imports.py`).
"""
from __future__ import annotations

from ..models import RECURRENCES, REMINDER_OFFSETS, now

# Ordre : les sections du résumé quotidien sont déclarées par `courriels` à son import.
from . import commun, imports, taches, rappels, courriels, source, travaux, vues, exportation, liens  # noqa: I001

_MODULES = (commun, imports, taches, rappels, courriels, source, travaux, vues, exportation, liens)

for _module in _MODULES:
    globals().update({nom: valeur for nom, valeur in vars(_module).items()
                      if not nom.startswith("__") and nom not in ("A", "annotations")})
del _module

__all__ = ["IMPORT_MODES", "RECURRENCES", "REMINDER_OFFSETS", "import_calendar", "undo_import", "week_view",
           "month_view", "list_view", "dashboard", "run_jobs", "export_ics", "now"]
