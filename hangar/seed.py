"""Données de démonstration : l'équipe et le plan de tâches du projet pluridisciplinaire EAE.

Si le fichier Excel de planification est présent dans le dossier parent, ses tâches sont importées
(phases -> modules, parties -> étiquettes, dépendances -> relations « bloqué par »).
"""
from __future__ import annotations

import random
import secrets
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .extensions import db
from .models import (
    PALETTE, Activity, Comment, Cycle, Favorite, Issue, Page, Project, SavedView, Sticky, User, Workspace,
)
from .services import create_issue, create_project, get_or_create_label

ROOT = Path(__file__).resolve().parent.parent.parent  # dossier « Projet Pluri »
PLANNING = ROOT / "Planning" / "Plan_taches_gouvernance_donnees_v5.xlsx"
MEETING_NOTE = ROOT / "NoteRéunion.docx"

# Ancien mot de passe fixe de la démonstration, publié dans le README des premières versions : les bases
# créées alors l'ont encore. « hangar demo » tire désormais un mot de passe au hasard (nouveau_mot_de_passe).
DEMO_PASSWORD = "hangar2026"


def nouveau_mot_de_passe() -> str:
    """Mot de passe provisoire de la démonstration, tiré au hasard (12 caractères)."""
    return secrets.token_urlsafe(9)


def est_mot_de_passe_public(password: str) -> bool:
    return password == DEMO_PASSWORD


def comptes_mot_de_passe_public() -> list[str]:
    """Identifiants des comptes actifs dont le mot de passe est encore l'ancien mot de passe public."""
    return [u.username for u in User.query.filter_by(active=True).order_by(User.id)
            if u.check_password(DEMO_PASSWORD)]

TEAM = [
    # identifiant, prénom, nom, rôle
    ("wacogne", "Antoine", "Wacogne", "admin"),
    ("lazaro", "Raphaël", "Lazaro", "member"),
    ("brancourt", "Mathieu", "Brancourt", "member"),
    ("gali", "", "Gali", "member"),
    ("claeys", "", "Claeys", "member"),
    ("niord", "", "Niord", "member"),
    ("flohic", "Vincent", "Flohic", "member"),
    ("loubatier", "Benoit", "Loubatier", "member"),
    ("alquarni", "", "Alquarni", "member"),
]

PROJECT_DESCRIPTION = """Dans le contexte de la période d’intégration de l’École de l’Air et de l’Espace, les élèves de deuxième année chargés de l’administratif et les entités qui reçoivent les documents d’intégration font face à une forte redondance des informations : documents transmis sur papier, par voie dématérialisée et par clé USB, à destination de plusieurs entités.

**Objectif :** alléger l’envoi de ces documents et simplifier leur transmission, avec une plateforme de stockage et d’extraction des documents et des données conforme au RGPD.

**Livrable à six mois :** une recommandation appuyée par un benchmark, évaluée avec différents spécialistes.

**Principale incertitude :** le traitement des données sensibles."""

GUIDE = """Bienvenue dans **Hangar**, l’outil de pilotage de l’équipe. Tout fonctionne en local : les données restent sur cette machine (dossier `data/`).

## Les bases
- **Tickets** : chaque tâche du plan est un ticket (`EAE-12`). Change son état, sa priorité, ses assignés ou ses dates directement depuis les listes en cliquant sur les propriétés.
- **Vues** : liste, tableau kanban (glisser-déposer), tableur (modifications en masse), calendrier et Gantt.
- **Cycles** : les sprints de deux semaines, avec burndown et transfert des tickets non terminés.
- **Modules** : les phases du projet (Cadrage, Entretiens & cartographie, Cadre légal…).
- **Pages** : comptes rendus, notes de décision, fiches d’entretien (modèles fournis, historique des versions, import Word).
- **Intake** : boîte de réception des demandes à trier.

## Raccourcis clavier
| Touche | Action |
|---|---|
| `Ctrl` + `K` | Palette de commandes / recherche |
| `C` | Créer un ticket |
| `/` | Rechercher |
| `?` | Afficher les raccourcis |
| `Maj` + `D` | Basculer thème clair/sombre |

## Bonnes pratiques
- [ ] Mettre à jour l’état de ses tickets à chaque point hebdomadaire
- [ ] Mentionner un collègue avec `@identifiant` dans un commentaire pour le notifier
- [ ] Citer un ticket avec sa clé (ex. EAE-5) pour créer un lien automatique
- [ ] Enregistrer le temps passé sur les tâches longues
"""


def _dt(d: date, h=9) -> datetime:
    return datetime.combine(d, time(h, 0))


def seed_demo(planning: str | None = None, verbose=True, password: str | None = None):
    """Crée l'équipe et le projet de démonstration dans une base vide.

    ``password`` : mot de passe provisoire de tous les comptes (par défaut tiré au hasard, renvoyé dans
    ``["password"]`` : l'appelant l'affiche une seule fois). Chaque compte, administrateur compris, doit le
    changer à sa première connexion."""
    say = print if verbose else (lambda *a, **k: None)
    if db.session.query(User.id).first() is not None:
        raise RuntimeError("La base contient déjà des données. Utilise « hangar demo --force » pour la remplacer "
                           "(une sauvegarde est faite avant) ou « hangar reset ».")
    password = password or nouveau_mot_de_passe()
    random.seed(7)
    ws = Workspace.get()
    ws.name = "Projet Pluri — EAE"
    ws.emoji = "✈️"
    ws.description = "Optimisation des procédures administratives de la période d’intégration de l’EAE."

    users = {}
    for i, (uname, first, last, role) in enumerate(TEAM):
        u = User(username=uname, email=User.internal_email(uname), first_name=first, last_name=last, role=role,
                 avatar_color=PALETTE[i % len(PALETTE)], capacity_hours=10, must_change_password=True)
        u.set_password(password)
        db.session.add(u)
        users[uname] = u
    db.session.flush()
    me = users["wacogne"]
    say(f"✓ {len(users)} membres créés (mot de passe provisoire à changer à la première connexion)")

    start, end = date(2026, 9, 22), date(2026, 12, 18)
    project = create_project(
        "Optimisation des procédures d’intégration EAE", "EAE", me, description=PROJECT_DESCRIPTION,
        emoji="🛫", color="#3f76ff", network="public", lead=me, member_ids=[u.id for u in users.values()],
        start_date=start, target_date=end, estimate_type="days",
    )
    db.session.flush()

    # Cycles de deux semaines à partir du lundi 21/09/2026
    cycles = []
    d = date(2026, 9, 21)
    k = 1
    while d <= end:
        c = Cycle(project=project, name=f"Sprint {k}", start_date=d, end_date=d + timedelta(days=13), owner=me,
                  description=f"Itération {k} du projet ({d:%d/%m} → {d + timedelta(days=13):%d/%m}).")
        db.session.add(c)
        cycles.append(c)
        d += timedelta(days=14)
        k += 1
    db.session.flush()
    say(f"✓ {len(cycles)} cycles de deux semaines")

    from .seed_documents import DATA_FILE, load_documents
    path = Path(planning) if planning else PLANNING
    imported = False
    source = "generic"
    if not planning and DATA_FILE.exists():
        # Tickets rédigés d'après les documents produits par l'équipe (pas le planning prévisionnel)
        load_documents(project, me, say=say)
        source = "documents"
    elif path.exists():
        source = "excel"
        from .importer import import_rows, read_table
        with open(path, "rb") as fh:
            rows = read_table(fh, path.name)
        report = import_rows(project, rows, me)
        db.session.flush()
        imported = True
        say(f"✓ {report['created']} tâches importées depuis {path.name}")
        for w in report["warnings"]:
            say("  ! " + w)
    else:
        _generic_tasks(project, me, users)
        say("✓ Tâches d’exemple créées (planning Excel introuvable)")

    # Cycle selon l'échéance + dates de création réalistes (import Excel / tâches d'exemple)
    for issue in (Issue.query.filter_by(project_id=project.id) if source != "documents" else []):
        ref = issue.target_date or issue.start_date
        if ref:
            for c in cycles:
                if c.start_date <= ref <= c.end_date:
                    issue.cycle = c
                    break
        created = _dt(min(issue.start_date or start, start) if issue.start_date else start)
        issue.created_at = created
        issue.updated_at = max(created, issue.completed_at or created)
    db.session.flush()
    for a in (Activity.query.filter(Activity.project_id == project.id, Activity.issue_id.isnot(None))
              if source != "documents" else []):
        if a.issue and a.verb in ("created", "imported"):
            a.created_at = a.issue.created_at
        elif a.issue:
            a.created_at = a.issue.created_at + timedelta(minutes=1)
    for c in (Comment.query.join(Issue).filter(Issue.project_id == project.id) if source != "documents" else []):
        c.created_at = (_dt(c.issue.target_date, 17) if c.issue.target_date and c.issue.target_date <= date.today()
                        else _dt(date.today() - timedelta(days=2), 14))
    if source != "documents":
        get_or_create_label(project, "RGPD")

    # Une demande d'exemple dans l'intake
    sample = create_issue(project, {
        "title": "(Exemple) Demande d’accès au dossier partagé pour un nouvel interlocuteur",
        "description": "Exemple de demande arrivée dans l’intake : accepte-la, refuse-la, reporte-la ou "
                       "marque-la comme doublon pour tester le triage.",
    }, users["gali"], intake=True)
    sample.intake_source = "Exemple"

    # Pages
    pages = [Page(project=project, title="Guide de démarrage Hangar", emoji="🧭", content=GUIDE, owner=me,
                  updated_by=me, is_public=True),
             Page(project=project, title="Présentation du projet", emoji="🎯", content=PROJECT_DESCRIPTION,
                  owner=me, updated_by=me, is_public=True)]
    if MEETING_NOTE.exists() and source != "documents":  # sinon : comptes rendus issus des documents
        try:
            from .importer import docx_to_markdown
            pages.append(Page(project=project, title="Note de réunion", emoji="🗒️",
                              content=docx_to_markdown(MEETING_NOTE.read_bytes()), owner=me, updated_by=me,
                              is_public=True))
        except Exception as exc:  # document illisible : on continue
            say(f"  ! Note de réunion non importée : {exc}")
    db.session.add_all(pages)

    # Vues enregistrées
    jalon = next((l for l in project.labels if l.name == "Jalon"), None)
    views = [
        ("Mes tâches ouvertes", "assignee=me&state_group=unstarted&state_group=started&layout=list&group_by=state"),
        ("En retard", "target=overdue&layout=list&group_by=assignee"),
        ("Non assignées", "assignee=none&state_group=backlog&state_group=unstarted&layout=list&group_by=module"),
        ("Priorité haute à traiter", "priority=urgent&priority=high&state_group=unstarted&state_group=started"
                                     "&layout=board&group_by=state"),
        ("Planning (Gantt)", "layout=gantt&order_by=start"),
    ]
    if jalon:
        views.append(("Jalons", f"label={jalon.id}&layout=list&group_by=state&order_by=target"))
    for name, q in (views if source != "documents" else []):
        db.session.add(SavedView(project=project, name=name, query=q, owner=me, is_public=True))

    db.session.add(Favorite(user_id=me.id, entity_type="project", entity_id=project.id))
    db.session.add(Sticky(user_id=me.id, color="#dbeafe",
                          content="Bienvenue ! Les autres membres se connectent avec leur identifiant "
                                  "(ex. « claeys ») et le mot de passe provisoire affiché par « hangar demo », "
                                  "puis choisissent le leur."))
    db.session.add(Sticky(user_id=me.id, color="#fef3c7",
                          content="Penser au point de suivi hebdomadaire : revue des tickets en retard."))
    db.session.commit()
    say("✓ Pages, vues enregistrées et pense-bêtes ajoutés")
    return {"project": project, "imported": imported, "password": password}


def _generic_tasks(project: Project, me: User, users: dict):
    from .services import get_or_create_module
    tasks = [
        ("Cadrage", "Rédiger la fiche de cadrage", "high", "completed", 0, 3),
        ("Cadrage", "Valider le planning avec l’encadrant", "high", "completed", 2, 5),
        ("Entretiens & cartographie", "Préparer le guide d’entretien", "high", "started", 5, 12),
        ("Entretiens & cartographie", "Cartographier les flux de transmission", "medium", "unstarted", 10, 24),
        ("Cadre légal & conformité", "Identifier le DPO et le circuit de validation", "high", "unstarted", 12, 20),
        ("Spécification plateforme", "Rédiger les exigences de dépôt des documents", "medium", "backlog", 30, 45),
        ("Pilote & finalisation", "Préparer la présentation finale", "high", "backlog", 70, 85),
    ]
    team = list(users.values())
    for idx, (mod, title, prio, group, s, e) in enumerate(tasks):
        m = get_or_create_module(project, mod)
        db.session.flush()
        st = project.first_state_of(group) if group != "unstarted" else project.default_state()
        create_issue(project, {
            "title": title, "priority": prio, "state_id": st.id, "module_ids": [m.id],
            "assignee_ids": [team[idx % len(team)].id],
            "start_date": project.start_date + timedelta(days=s), "target_date": project.start_date + timedelta(days=e),
        }, me)
