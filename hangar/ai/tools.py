"""Outils de l'assistant (appel de fonctions) et actions proposées.

- Outils de lecture : chercher/lire les tickets, les tâches et l'agenda, les cycles, les pages, le wiki.
- Outils « proposer_… » : ils n'exécutent rien. Ils enregistrent une `AIAction` que le panneau affiche comme un
  bouton (« Créer la tâche », « Ajouter à la page »…) ; le clic de l'utilisateur l'exécute (`execute_action`),
  droits vérifiés à ce moment-là.
Un module peut ajouter un exécuteur d'action avec `register_action(kind, fn)` (fn(action, user) -> dict).
"""
from __future__ import annotations

import re
from datetime import date, time, timedelta
from pathlib import Path

from flask import url_for

from ..extensions import db
from ..models import (
    PRIORITY_LABELS, AgendaTask, Cycle, Issue, Page, PageVersion, Project, State, now,
)
from ..utils import can, parse_date, visible_projects
from . import context as C
from .core import validate_json
from .models import AIAction
from .providers import ToolCall

PRIORITIES = ["urgent", "high", "medium", "low", "none"]
_DATE = {"type": "string", "description": "Date au format AAAA-MM-JJ"}


def _schema(props: dict, required=()) -> dict:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


READ_TOOLS = [
    {"name": "chercher_tickets",
     "description": "Cherche des tickets (work items) dans les projets que l’utilisateur peut voir. Sans critère : ses "
                    "tickets ouverts. Renvoie clé, titre, état, priorité, échéance, assignés.",
     "input_schema": _schema({
         "texte": {"type": "string", "description": "Mots à chercher dans le titre ou la description"},
         "projet": {"type": "string", "description": "Identifiant (ex. EAE) ou nom du projet"},
         "etat": {"type": "string", "enum": ["ouverts", "termines", "tous"], "description": "Par défaut : ouverts"},
         "assignes_a_moi": {"type": "boolean", "description": "Seulement les tickets assignés à l’utilisateur"},
         "en_retard": {"type": "boolean", "description": "Seulement les tickets dont l’échéance est passée"},
         "limite": {"type": "integer", "description": "Nombre maximal de résultats (20 par défaut, 50 au plus)"}})},
    {"name": "lire_ticket",
     "description": "Détail complet d’un ticket : description, état, assignés, dates, cycle, sous-tickets, commentaires.",
     "input_schema": _schema({"cle": {"type": "string", "description": "Clé du ticket, ex. EAE-12"}}, ["cle"])},
    {"name": "lister_taches",
     "description": "Tâches personnelles de l’agenda de l’utilisateur (avec leur numéro #id).",
     "input_schema": _schema({
         "filtre": {"type": "string", "enum": ["toutes_ouvertes", "en_retard", "aujourdhui", "semaine", "sans_date",
                                               "terminees"], "description": "Par défaut : toutes_ouvertes"},
         "texte": {"type": "string", "description": "Mots à chercher dans le titre ou les notes"}})},
    {"name": "consulter_agenda",
     "description": "Emploi du temps de l’utilisateur (cours, contrôles, séances annulées) et ses tâches, jour par jour, "
                    "entre deux dates (62 jours au plus).",
     "input_schema": _schema({"debut": _DATE, "fin": {"type": "string", "description": "AAAA-MM-JJ (par défaut : "
                                                                                       "7 jours après le début)"}},
                             ["debut"])},
    {"name": "lister_cycles",
     "description": "Cycles (sprints) des projets visibles : dates, statut et avancement.",
     "input_schema": _schema({"projet": {"type": "string", "description": "Identifiant ou nom du projet (facultatif)"},
                              "statut": {"type": "string", "enum": ["en_cours", "a_venir", "tous"]}})},
    {"name": "chercher_pages",
     "description": "Cherche dans les pages de documentation des projets (titre et contenu) et renvoie des extraits.",
     "input_schema": _schema({"texte": {"type": "string", "description": "Mots à chercher"}}, ["texte"])},
]
WIKI_TOOL = {
    "name": "chercher_wiki",
    "description": "Cherche dans le wiki de cours (pages et PDF) les passages qui répondent à une question ; renvoie "
                   "des extraits avec leur titre et leur adresse.",
    "input_schema": _schema({"question": {"type": "string", "description": "Question ou mots-clés"}}, ["question"])}

ACTION_TOOLS = [
    {"name": "proposer_tache",
     "description": "Propose de créer une tâche dans l’agenda de l’utilisateur. N’exécute rien : un bouton « Créer la "
                    "tâche » s’affiche sous ta réponse et l’utilisateur décide.",
     "input_schema": _schema({
         "titre": {"type": "string"}, "date": _DATE,
         "heure_debut": {"type": "string", "description": "HH:MM (facultatif)"},
         "heure_fin": {"type": "string", "description": "HH:MM (facultatif)"},
         "notes": {"type": "string", "description": "Détails en Markdown"},
         "priorite": {"type": "string", "enum": PRIORITIES},
         "categorie": {"type": "string", "description": "Ex. Révisions, Projet, Sport"}}, ["titre"])},
    {"name": "proposer_ticket",
     "description": "Propose de créer un ticket dans un projet (bouton « Créer le ticket »). N’exécute rien.",
     "input_schema": _schema({
         "projet": {"type": "string", "description": "Identifiant du projet (ex. EAE) ; par défaut celui de la page"},
         "titre": {"type": "string"}, "description": {"type": "string", "description": "Markdown"},
         "priorite": {"type": "string", "enum": PRIORITIES}, "echeance": _DATE,
         "cycle_en_cours": {"type": "boolean", "description": "L’ajouter au cycle en cours du projet"},
         "assigner_a_moi": {"type": "boolean"}}, ["titre"])},
    {"name": "proposer_ajout_page",
     "description": "Propose d’ajouter un bloc Markdown au début ou à la fin d’une page (page du wiki ou page de "
                    "projet ; par défaut la page ouverte). Bouton « Ajouter à la page ». N’exécute rien.",
     "input_schema": _schema({
         "contenu": {"type": "string", "description": "Bloc Markdown à ajouter"},
         "position": {"type": "string", "enum": ["debut", "fin"], "description": "Par défaut : fin"},
         "page": {"type": "string", "description": "Adresse de la page (/projects/1/pages/3 ou /wiki/…) ; vide = page "
                                                   "ouverte"}}, ["contenu"])},
    {"name": "proposer_ajout_cycle",
     "description": "Propose d’ajouter des tickets au cycle en cours de leur projet (ou au cycle nommé). N’exécute rien.",
     "input_schema": _schema({
         "cles": {"type": "array", "items": {"type": "string"}, "description": "Clés des tickets, ex. [\"EAE-3\"]"},
         "cycle": {"type": "string", "description": "Nom du cycle (par défaut : le cycle en cours)"}}, ["cles"])},
    {"name": "proposer_echeance",
     "description": "Propose de reporter ou de changer l’échéance d’une tâche (#id) ou d’un ticket (clé). N’exécute rien.",
     "input_schema": _schema({
         "cible": {"type": "string", "description": "« #12 » pour la tâche 12, ou une clé de ticket « EAE-4 »"},
         "date": _DATE, "heure": {"type": "string", "description": "HH:MM pour une tâche (facultatif)"}},
         ["cible", "date"])},
    {"name": "proposer_terminer",
     "description": "Propose de marquer une tâche (#id) ou un ticket (clé) comme terminé. N’exécute rien.",
     "input_schema": _schema({"cible": {"type": "string"}}, ["cible"])},
]

TOOL_LABELS = {
    "chercher_tickets": "Recherche dans les tickets", "lire_ticket": "Lecture d’un ticket",
    "lister_taches": "Lecture des tâches", "consulter_agenda": "Lecture de l’agenda", "lister_cycles": "Lecture des cycles",
    "chercher_pages": "Recherche dans les pages", "chercher_wiki": "Recherche dans le wiki",
    "proposer_tache": "Proposition de tâche", "proposer_ticket": "Proposition de ticket",
    "proposer_ajout_page": "Proposition d’ajout à la page", "proposer_ajout_cycle": "Proposition d’ajout au cycle",
    "proposer_echeance": "Proposition de nouvelle échéance", "proposer_terminer": "Proposition de clôture",
}

_ACTION_HANDLERS: dict = {}


def register_action(kind: str, fn):
    """Remplace ou ajoute l'exécuteur d'une action : fn(action: AIAction, user) -> {"message", "url"}."""
    _ACTION_HANDLERS[kind] = fn
    return fn


class ActionError(Exception):
    """Action impossible (le message est montré à l'utilisateur)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def wiki_available() -> bool:
    return C.wiki_vault() is not None


# ---------------------------------------------------------------------------
# Boîte à outils d'un tour de conversation
# ---------------------------------------------------------------------------

def _clip(v, n=300):
    s = str(v or "").strip()
    return s[:n]


def _find_project(ref: str, user) -> Project | None:
    ref = (ref or "").strip()
    if not ref:
        return None
    for p in visible_projects(user):
        if p.identifier.lower() == ref.lower() or p.name.lower() == ref.lower():
            return p
    for p in visible_projects(user):
        if ref.lower() in p.name.lower():
            return p
    return None


def find_issue(key: str, user) -> Issue | None:
    m = re.match(r"^\s*([A-Za-z][A-Za-z0-9]{0,9})-(\d+)\s*$", key or "")
    if not m:
        return None
    p = Project.query.filter(db.func.upper(Project.identifier) == m.group(1).upper()).first()
    if p is None or not can(user, "view", p):
        return None
    return Issue.query.filter_by(project_id=p.id, sequence_id=int(m.group(2))).first()


def _find_task(ref: str, user) -> AgendaTask | None:
    m = re.match(r"^\s*#?(\d+)\s*$", str(ref or ""))
    if not m:
        return None
    t = db.session.get(AgendaTask, int(m.group(1)))
    return t if t is not None and t.user_id == user.id else None


def _parse_hm(v) -> time | None:
    m = re.match(r"^\s*(\d{1,2})[:h](\d{2})?\s*$", str(v or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    return time(h, mi) if h < 24 and mi < 60 else None


def current_cycle(project: Project) -> Cycle | None:
    return next((c for c in project.cycles if c.archived_at is None and c.status == "current"), None)


class ToolBox:
    """Outils d'un tour de conversation : exécute les lectures et enregistre les actions proposées."""

    def __init__(self, user, message=None, page: dict | None = None):
        self.user = user
        self.message = message
        self.page = page or {}
        self.actions: list[AIAction] = []
        self.calls: list[dict] = []

    def definitions(self) -> list[dict]:
        tools = list(READ_TOOLS)
        if wiki_available():
            tools.append(WIKI_TOOL)
        return tools + ACTION_TOOLS

    def execute(self, call: ToolCall) -> tuple[str, bool]:
        defs = {t["name"]: t for t in self.definitions()}
        tool = defs.get(call.name)
        if tool is None:
            return f"Outil inconnu : {call.name}.", True
        args = call.input if isinstance(call.input, dict) else {}
        if "__invalid__" in args:
            return "Arguments illisibles (JSON invalide) : réessaie l’appel.", True
        args = {k: v for k, v in args.items() if k in tool["input_schema"]["properties"]}
        errs = validate_json(args, tool["input_schema"])
        if errs:
            return "Arguments invalides : " + " ; ".join(errs[:3]), True
        fn = getattr(self, "t_" + call.name)
        try:
            content = fn(**args)
            ok = True
        except ActionError as e:
            content, ok = e.message, False
        self.calls.append({"name": call.name, "label": TOOL_LABELS.get(call.name, call.name), "input": args,
                           "error": not ok})
        return content, not ok

    # Lectures -----------------------------------------------------------------------------------
    def t_chercher_tickets(self, texte="", projet="", etat="ouverts", assignes_a_moi=False, en_retard=False, limite=20):
        user = self.user
        if projet:
            p = _find_project(projet, user)
            if p is None:
                raise ActionError(f"Projet « {projet} » introuvable ou non visible.")
            pids = [p.id]
        else:
            pids = [p.id for p in visible_projects(user)]
        if not pids:
            return "Aucun projet visible."
        q = Issue.query.filter(Issue.project_id.in_(pids), Issue.archived_at.is_(None), Issue.is_visible_clause()) \
            .outerjoin(State, Issue.state_id == State.id)
        if etat == "ouverts":
            q = q.filter(db.or_(State.group.is_(None), State.group.notin_(("completed", "cancelled"))))
        elif etat == "termines":
            q = q.filter(State.group.in_(("completed", "cancelled")))
        for word in (texte or "").split()[:6]:
            like = f"%{word}%"
            q = q.filter(db.or_(Issue.title.ilike(like), Issue.description.ilike(like)))
        if assignes_a_moi or not (texte or projet or en_retard or etat != "ouverts"):
            q = q.filter(Issue.assignees.any(id=user.id))
        if en_retard:
            q = q.filter(Issue.target_date < date.today())
        limite = max(1, min(int(limite or 20), 50))
        issues = q.order_by(Issue.target_date.is_(None), Issue.target_date, Issue.updated_at.desc()).limit(limite).all()
        if not issues:
            return "Aucun ticket ne correspond."
        return f"{len(issues)} ticket(s) :\n" + "\n".join(f"- {C.issue_line(i)}" for i in issues)

    def t_lire_ticket(self, cle):
        i = find_issue(cle, self.user)
        if i is None:
            raise ActionError(f"Ticket « {cle} » introuvable ou non visible.")
        return C.issue_text(i) + f"\nAdresse : {url_for('issues.detail', pid=i.project_id, seq=i.sequence_id)}"

    def t_lister_taches(self, filtre="toutes_ouvertes", texte=""):
        from .. import agenda as A

        today = date.today()
        user = self.user
        if filtre == "terminees":
            tasks = AgendaTask.query.filter(AgendaTask.user_id == user.id, AgendaTask.done_at.isnot(None)) \
                .order_by(AgendaTask.done_at.desc()).limit(40).all()
        else:
            g = A.open_task_groups(user, today)
            keys = {"en_retard": ["overdue"], "aujourdhui": ["today"], "sans_date": ["nodate"],
                    "semaine": ["overdue", "today", "tomorrow", "week"]}.get(filtre, [k for k, _ in A.TASK_GROUP_LABELS])
            tasks = [t for k in keys for t in g.get(k, [])]
        if texte:
            words = texte.lower().split()
            tasks = [t for t in tasks if all(w in f"{t.title} {t.notes or ''} {t.category or ''}".lower() for w in words)]
        if not tasks:
            return "Aucune tâche ne correspond."
        return f"{len(tasks)} tâche(s) :\n" + "\n".join(f"- {C._task_line(t, today)}" for t in tasks[:60])

    def t_consulter_agenda(self, debut, fin=""):
        d0 = parse_date(debut)
        if d0 is None:
            raise ActionError("Date de début invalide (AAAA-MM-JJ).")
        d1 = parse_date(fin) if fin else d0 + timedelta(days=6)
        if d1 is None or d1 < d0:
            d1 = d0 + timedelta(days=6)
        d1 = min(d1, d0 + timedelta(days=61))
        return C.agenda_text(self.user, d0, d1, f"Agenda du {C._d(d0)} au {C._d(d1)} :")

    def t_lister_cycles(self, projet="", statut="tous"):
        projects = [_find_project(projet, self.user)] if projet else visible_projects(self.user)
        projects = [p for p in projects if p is not None]
        if not projects:
            raise ActionError(f"Projet « {projet} » introuvable.")
        wanted = {"en_cours": ("current",), "a_venir": ("upcoming",)}.get(statut, ("current", "upcoming", "completed", "draft"))
        lines = []
        for p in projects:
            for c in p.cycles:
                if c.archived_at is None and c.status in wanted:
                    pr = c.progress()
                    lines.append(f"- {p.identifier} · {c.name} — {c.status_label}, {C._d(c.start_date) or '—'} → "
                                 f"{C._d(c.end_date) or '—'}, {pr['done']}/{pr['total']} terminés ({pr['pct']} %)")
        return "\n".join(lines) or "Aucun cycle."

    def t_chercher_pages(self, texte):
        pids = [p.id for p in visible_projects(self.user)]
        words = [w for w in (texte or "").split() if len(w) > 2][:6] or [texte]
        q = Page.query.filter(Page.project_id.in_(pids), Page.archived_at.is_(None))
        q = q.filter(db.or_(*[db.or_(Page.title.ilike(f"%{w}%"), Page.content.ilike(f"%{w}%")) for w in words]))
        pages = [pg for pg in q.order_by(Page.updated_at.desc()).limit(20) if C.page_visible(pg, self.user)][:8]
        if not pages:
            return "Aucune page ne correspond."
        out = []
        for pg in pages:
            content = pg.content or ""
            low = content.lower()
            pos = min((low.find(w.lower()) for w in words if low.find(w.lower()) >= 0), default=0)
            excerpt = content[max(0, pos - 300):pos + 900].strip()
            out.append(f"## {pg.title} ({pg.project.name}) — {url_for('pages.view', pid=pg.project_id, page_id=pg.id)}\n"
                       f"{excerpt}")
        return "\n\n".join(out)

    def t_chercher_wiki(self, question):
        vault = C.wiki_vault()
        if vault is None:
            raise ActionError("Le wiki n’est pas disponible.")
        try:
            from ..wiki import demander  # type: ignore
            sources = demander.rassembler(vault, question)
        except Exception:  # noqa: BLE001 - repli : recherche simple dans les titres et le texte
            words = [w.lower() for w in question.split() if len(w) > 2][:8]
            scored = []
            for n in vault.notes.values():
                body = str(getattr(n, "body", "") or "")
                hay = (str(getattr(n, "title", "")) + " " + body).lower()
                score = sum(hay.count(w) for w in words)
                if score:
                    scored.append((score, n, body))
            scored.sort(key=lambda s: -s[0])
            sources = [{"titre": n.title, "url": n.url, "texte": body[:4000]} for _, n, body in scored[:5]]
        if not sources:
            return "Le wiki ne contient rien sur ce sujet."
        return "\n\n".join(f"[{i}] {s['titre']} — {s.get('url', '')}\n{s.get('texte', '')}" for i, s in enumerate(sources, 1))

    # Propositions ------------------------------------------------------------------------------
    def _propose(self, kind, label, summary, payload) -> str:
        a = AIAction(message_id=self.message.id if self.message is not None else None, user_id=self.user.id, kind=kind,
                     label=label, summary=summary[:500], created_at=now())
        a.payload = payload
        db.session.add(a)
        db.session.commit()
        self.actions.append(a)
        return (f"Proposition enregistrée (action {a.id}) : l’utilisateur voit un bouton « {label} » sous ta réponse. "
                "Ce n’est pas encore fait : présente-la comme une proposition.")

    def t_proposer_tache(self, titre, date="", heure_debut="", heure_fin="", notes="", priorite="none", categorie=""):
        d = parse_date(date) if date else None
        if date and d is None:
            raise ActionError("Date invalide (AAAA-MM-JJ).")
        t0, t1 = _parse_hm(heure_debut), _parse_hm(heure_fin)
        payload = {"titre": _clip(titre), "date": d.isoformat() if d else "", "heure_debut": t0.strftime("%H:%M") if t0 else "",
                   "heure_fin": t1.strftime("%H:%M") if t1 and t0 else "", "notes": (notes or "")[:5000],
                   "priorite": priorite if priorite in PRIORITIES else "none", "categorie": _clip(categorie, 60)}
        from .. import agenda as A
        when = A.task_when(AgendaTask(due_date=d, start_time=t0, end_time=t1)) if d else "sans date"
        return self._propose("tache", "Créer la tâche", f"{payload['titre']} — {when}", payload)

    def t_proposer_ticket(self, titre, projet="", description="", priorite="none", echeance="", cycle_en_cours=False,
                          assigner_a_moi=False):
        p = _find_project(projet, self.user) if projet else self._page_project()
        if p is None:
            raise ActionError("Précise le projet (identifiant, ex. EAE) : " + ", ".join(
                f"{x.identifier} ({x.name})" for x in visible_projects(self.user)[:12]))
        if not can(self.user, "edit", p):
            raise ActionError(f"L’utilisateur ne peut pas créer de ticket dans {p.name} (droits insuffisants).")
        d = parse_date(echeance) if echeance else None
        cyc = current_cycle(p) if cycle_en_cours else None
        payload = {"project_id": p.id, "titre": _clip(titre, 255), "description": (description or "")[:20000],
                   "priorite": priorite if priorite in PRIORITIES else "none", "echeance": d.isoformat() if d else "",
                   "cycle_id": cyc.id if cyc else None, "assigner": bool(assigner_a_moi)}
        extra = [PRIORITY_LABELS.get(payload["priorite"], "")] if payload["priorite"] != "none" else []
        if d:
            extra.append(f"échéance {C._d(d)}")
        if cyc:
            extra.append(f"cycle {cyc.name}")
        return self._propose("ticket", "Créer le ticket", f"{p.identifier} · {payload['titre']}"
                             + (f" ({', '.join(extra)})" if extra else ""), payload)

    def t_proposer_ajout_page(self, contenu, position="fin", page=""):
        target = (page or self.page.get("url") or "").split("?")[0]
        if not target:
            raise ActionError("Aucune page ouverte : précise l’adresse de la page.")
        endpoint, args = C.match(target)
        position = "debut" if position == "debut" else "fin"
        where = "en tête de" if position == "debut" else "à la fin de"
        if endpoint in ("pages.view", "pages.edit"):
            pg = db.session.get(Page, int(args.get("page_id") or 0))
            if pg is None or not C.page_visible(pg, self.user):
                raise ActionError("Page introuvable.")
            if not C.page_editable(pg, self.user):
                raise ActionError(f"L’utilisateur ne peut pas modifier la page « {pg.title} ».")
            payload = {"page_type": "projet", "page_id": pg.id, "contenu": contenu[:50000], "position": position}
            return self._propose("ajout_page", "Ajouter à la page", f"Bloc ajouté {where} « {pg.title} »", payload)
        if target.startswith("/wiki/"):
            vault, note = C.wiki_note_for(target)
            if note is None:
                raise ActionError("Page du wiki introuvable (ou wiki indisponible).")
            if self.user.is_guest:
                raise ActionError("Les invités ne peuvent pas modifier le wiki.")
            payload = {"page_type": "wiki", "url": target, "contenu": contenu[:50000], "position": position}
            return self._propose("ajout_page", "Ajouter à la page", f"Bloc ajouté {where} « {note.title} » (wiki)", payload)
        raise ActionError("La page ouverte n’est ni une page de projet ni une page du wiki : indique l’adresse d’une page.")

    def t_proposer_ajout_cycle(self, cles, cycle=""):
        issues = []
        for k in cles[:30]:
            i = find_issue(k, self.user)
            if i is None:
                raise ActionError(f"Ticket « {k} » introuvable.")
            if not can(self.user, "edit", i.project):
                raise ActionError(f"Droits insuffisants sur le projet {i.project.name}.")
            issues.append(i)
        if not issues:
            raise ActionError("Aucun ticket indiqué.")
        projects = {i.project_id for i in issues}
        if len(projects) > 1:
            raise ActionError("Les tickets doivent appartenir au même projet (fais une proposition par projet).")
        p = issues[0].project
        cyc = None
        if cycle:
            cyc = next((c for c in p.cycles if c.archived_at is None and c.name.lower() == cycle.lower()), None)
        cyc = cyc or current_cycle(p)
        if cyc is None:
            raise ActionError(f"Le projet {p.name} n’a pas de cycle en cours.")
        payload = {"issue_ids": [i.id for i in issues], "cycle_id": cyc.id}
        label = "Ajouter au cycle en cours" if cyc.status == "current" else f"Ajouter au cycle « {cyc.name} »"
        return self._propose("ajout_cycle", label, f"{', '.join(i.key for i in issues)} → {cyc.name}", payload)

    def _target(self, cible):
        t = _find_task(cible, self.user)
        if t is not None:
            return "tache", t
        i = find_issue(cible, self.user)
        if i is not None:
            return "ticket", i
        raise ActionError(f"« {cible} » ne désigne ni une tâche (#id) ni un ticket visible (clé EAE-12).")

    def t_proposer_echeance(self, cible, date, heure=""):
        d = parse_date(date)
        if d is None:
            raise ActionError("Date invalide (AAAA-MM-JJ).")
        kind, obj = self._target(cible)
        if kind == "tache":
            t0 = _parse_hm(heure)
            payload = {"type": "tache", "id": obj.id, "date": d.isoformat(), "heure": t0.strftime("%H:%M") if t0 else ""}
            return self._propose("echeance", "Reporter la tâche", f"#{obj.id} {obj.title} → {C._d(d)}"
                                 + (f" à {payload['heure']}" if payload["heure"] else ""), payload)
        if not can(self.user, "edit", obj.project):
            raise ActionError(f"Droits insuffisants sur le projet {obj.project.name}.")
        payload = {"type": "ticket", "id": obj.id, "date": d.isoformat()}
        return self._propose("echeance", "Changer l’échéance", f"{obj.key} {obj.title} → {C._d(d)}", payload)

    def t_proposer_terminer(self, cible):
        kind, obj = self._target(cible)
        if kind == "ticket" and not can(self.user, "edit", obj.project):
            raise ActionError(f"Droits insuffisants sur le projet {obj.project.name}.")
        label = "Marquer comme faite" if kind == "tache" else "Marquer comme terminé"
        name = f"#{obj.id} {obj.title}" if kind == "tache" else f"{obj.key} {obj.title}"
        return self._propose("terminer", label, name, {"type": kind, "id": obj.id})

    def _page_project(self) -> Project | None:
        url = self.page.get("url") or ""
        _, args = C.match(url) if url else (None, {})
        if args.get("pid"):
            p = db.session.get(Project, int(args["pid"]))
            if p is not None and can(self.user, "view", p):
                return p
        return None


# ---------------------------------------------------------------------------
# Exécution des actions (au clic de l'utilisateur)
# ---------------------------------------------------------------------------

# Contenu écrit par l'IA qu'une action ajoutera tel quel : montré dans la carte avant validation.
_APERCU = {"ajout_page": "contenu", "ticket": "description", "tache": "notes"}


def action_dict(a: AIAction) -> dict:
    champ = _APERCU.get(a.kind)
    apercu = str((a.payload or {}).get(champ) or "") if champ else ""
    return {"id": a.id, "kind": a.kind, "label": a.label, "summary": a.summary, "status": a.status, "apercu": apercu,
            "result": a.result, "icon": {"tache": "list-check", "ticket": "target", "ajout_page": "page",
                                         "ajout_cycle": "cycle", "echeance": "calendar", "terminer": "check-circle"}
            .get(a.kind, "zap")}


def execute_action(a: AIAction, user) -> dict:
    if a.user_id != user.id:
        raise ActionError("Action introuvable.", 404)
    if a.status == "done":
        raise ActionError("Cette action a déjà été faite.", 409)
    if a.status == "dismissed":
        raise ActionError("Cette action a été écartée.", 409)
    fn = _ACTION_HANDLERS.get(a.kind) or _BUILTIN_ACTIONS.get(a.kind)
    if fn is None:
        raise ActionError("Action inconnue.")
    try:
        result = fn(a, user) or {}
    except ActionError:
        db.session.rollback()
        raise
    a.status, a.done_at = "done", now()
    a.result = result
    db.session.commit()
    return result


def _do_tache(a, user):
    from .. import agenda as A

    p = a.payload
    d = parse_date(p.get("date")) if p.get("date") else None
    t = AgendaTask(user_id=user.id, title=(p.get("titre") or "Tâche")[:300], notes=p.get("notes") or "", due_date=d,
                   start_time=_parse_hm(p.get("heure_debut")) if d else None,
                   priority=p.get("priorite") if p.get("priorite") in PRIORITY_LABELS else "none",
                   category=(p.get("categorie") or "")[:60], created_at=now(), updated_at=now())
    t.end_time = _parse_hm(p.get("heure_fin")) if t.start_time else None
    if t.end_time and t.end_time <= t.start_time:
        t.end_time = None
    db.session.add(t)
    db.session.flush()
    s = A.settings_of(user)
    if d and s.default_reminder is not None and s.default_reminder >= 0:
        A.set_task_reminders(t, [s.default_reminder], s.reminder_channel or "email")
    db.session.commit()
    return {"message": "Tâche créée" + (f" pour {A.due_label(d)}" if d else "") + ".",
            "url": url_for("agenda.tasks", focus=t.id), "task_id": t.id}


def _do_ticket(a, user):
    from ..services import create_issue

    p = a.payload
    project = db.session.get(Project, int(p.get("project_id") or 0))
    if project is None or not can(user, "edit", project):
        raise ActionError("Tu ne peux pas créer de ticket dans ce projet.", 403)
    data = {"title": p.get("titre") or "Sans titre", "description": p.get("description") or "",
            "priority": p.get("priorite") or "none"}
    if p.get("echeance"):
        data["target_date"] = p["echeance"]
    cyc = db.session.get(Cycle, int(p["cycle_id"])) if p.get("cycle_id") else None
    if cyc is not None and cyc.project_id == project.id:
        data["cycle_id"] = cyc.id
    if p.get("assigner"):
        data["assignee_ids"] = [user.id]
    issue = create_issue(project, data, user)
    db.session.commit()
    return {"message": f"Ticket {issue.key} créé.", "url": url_for("issues.detail", pid=project.id, seq=issue.sequence_id),
            "key": issue.key}


def _insert_block(text: str, block: str, position: str) -> str:
    block = block.strip()
    if position != "debut":
        return (text.rstrip() + "\n\n" + block + "\n") if text.strip() else block + "\n"
    lines = text.splitlines()
    i = 0
    if lines and lines[0].strip() == "---":  # propriétés YAML en tête
        end = next((n for n in range(1, len(lines)) if lines[n].strip() == "---"), None)
        if end is not None:
            i = end + 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and re.match(r"^#\s", lines[i]):  # titre principal
        i += 1
    head, tail = "\n".join(lines[:i]).rstrip(), "\n".join(lines[i:]).strip()
    return "\n\n".join(x for x in (head, block, tail) if x) + "\n"


def _do_ajout_page(a, user):
    p = a.payload
    if p.get("page_type") == "wiki":
        return _do_ajout_wiki(a, user)
    pg = db.session.get(Page, int(p.get("page_id") or 0))
    if pg is None or not C.page_visible(pg, user):
        raise ActionError("Page introuvable.", 404)
    if not C.page_editable(pg, user):
        raise ActionError("Tu ne peux pas modifier cette page.", 403)
    db.session.add(PageVersion(page=pg, title=pg.title, content=pg.content, author=pg.updated_by or user))
    pg.content = _insert_block(pg.content or "", p.get("contenu") or "", p.get("position"))
    pg.updated_at, pg.updated_by = now(), user
    db.session.commit()
    return {"message": f"Bloc ajouté à la page « {pg.title} ».", "url": url_for("pages.view", pid=pg.project_id, page_id=pg.id)}


def _do_ajout_wiki(a, user):
    if user.is_guest:
        raise ActionError("Les invités ne peuvent pas modifier le wiki.", 403)
    p = a.payload
    vault, note = C.wiki_note_for(p.get("url") or "")
    if note is None:
        raise ActionError("Page du wiki introuvable (ou wiki indisponible).", 404)
    path = Path(vault.root) / note.path
    old = path.read_text(encoding="utf-8")
    try:  # historique des versions du wiki, s'il existe
        from ..wiki import historique  # type: ignore
        historique.sauvegarder(Path(note.path).as_posix(), old, user.username)
    except Exception:  # noqa: BLE001
        pass
    path.write_text(_insert_block(old, p.get("contenu") or "", p.get("position")), encoding="utf-8", newline="\n")
    try:
        from ..wiki import vault as wiki_vault_module  # type: ignore
        getattr(wiki_vault_module, "invalider", lambda: None)()
    except Exception:  # noqa: BLE001
        pass
    return {"message": f"Bloc ajouté à « {note.title} ».", "url": p.get("url")}


def _do_ajout_cycle(a, user):
    from ..services import update_issue

    p = a.payload
    cyc = db.session.get(Cycle, int(p.get("cycle_id") or 0))
    if cyc is None or not can(user, "edit", cyc.project):
        raise ActionError("Cycle introuvable ou droits insuffisants.", 403)
    keys = []
    for iid in p.get("issue_ids") or []:
        i = db.session.get(Issue, int(iid))
        if i is not None and i.project_id == cyc.project_id:
            update_issue(i, {"cycle_id": cyc.id}, user)
            keys.append(i.key)
    db.session.commit()
    return {"message": f"{len(keys)} ticket(s) ajouté(s) au cycle « {cyc.name} ».",
            "url": url_for("cycles.detail", pid=cyc.project_id, cid=cyc.id)}


def _do_echeance(a, user):
    from .. import agenda as A
    from ..services import update_issue

    p = a.payload
    d = parse_date(p.get("date"))
    if d is None:
        raise ActionError("Date invalide.")
    if p.get("type") == "tache":
        t = _find_task(str(p.get("id")), user)
        if t is None:
            raise ActionError("Tâche introuvable.", 404)
        t.due_date = d
        if p.get("heure"):
            t.start_time = _parse_hm(p["heure"])
            t.end_time = None
        t.updated_at = now()
        db.session.flush()
        A.resync_task(t)
        db.session.commit()
        return {"message": f"Tâche reportée au {C._d(d)}.", "url": url_for("agenda.tasks", focus=t.id)}
    i = db.session.get(Issue, int(p.get("id") or 0))
    if i is None or not can(user, "edit", i.project):
        raise ActionError("Ticket introuvable ou droits insuffisants.", 403)
    update_issue(i, {"target_date": d.isoformat()}, user)
    db.session.commit()
    return {"message": f"Échéance de {i.key} fixée au {C._d(d)}.",
            "url": url_for("issues.detail", pid=i.project_id, seq=i.sequence_id)}


def _do_terminer(a, user):
    from .. import agenda as A
    from ..services import update_issue

    p = a.payload
    if p.get("type") == "tache":
        t = _find_task(str(p.get("id")), user)
        if t is None:
            raise ActionError("Tâche introuvable.", 404)
        if t.done_at:
            return {"message": "Tâche déjà faite.", "url": url_for("agenda.tasks", focus=t.id)}
        nxt = A.complete_task(t)
        db.session.commit()
        return {"message": "Tâche faite." + (f" Prochaine occurrence : {C._d(nxt)}." if nxt else ""),
                "url": url_for("agenda.tasks")}
    i = db.session.get(Issue, int(p.get("id") or 0))
    if i is None or not can(user, "edit", i.project):
        raise ActionError("Ticket introuvable ou droits insuffisants.", 403)
    st = i.project.first_state_of("completed")
    if st is None:
        raise ActionError("Le projet n’a pas d’état « terminé ».")
    update_issue(i, {"state_id": st.id}, user)
    db.session.commit()
    return {"message": f"{i.key} marqué comme terminé.", "url": url_for("issues.detail", pid=i.project_id, seq=i.sequence_id)}


_BUILTIN_ACTIONS = {"tache": _do_tache, "ticket": _do_ticket, "ajout_page": _do_ajout_page, "ajout_cycle": _do_ajout_cycle,
                    "echeance": _do_echeance, "terminer": _do_terminer}

