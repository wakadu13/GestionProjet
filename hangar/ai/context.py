"""Contexte de la page courante pour l'assistant, et recherche des éléments à ajouter avec « @ ».

Un résolveur est une fonction `fn(path, args, user) -> dict | None` :
- `path` : chemin de la page (« /projects/1/issues/12 »), sans la chaîne de requête ;
- `args` : paramètres de la route reconnue (ex. {"pid": 1, "seq": 12}), plus les paramètres de la requête
  (?date=…) et "_endpoint" (nom Flask de la route, ex. "issues.detail") ;
- retour : {"type", "title", "text", "url", "icon"} décrivant la page (le texte est transmis au modèle).
Les modules ajoutent les leurs avec `hangar.ai.register_context(fn)` ; ils passent avant ceux d'ici.
Les éléments « @ » (pages, tickets, tâches…) sont des adresses résolues de la même façon.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import Counter
from datetime import date, datetime, time, timedelta

from flask import current_app, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.routing import RequestRedirect

from ..extensions import db
from ..models import (
    PRIORITY_LABELS, AgendaTask, Comment, Cycle, Issue, Module, Page, Project, State,
)
from ..utils import can, fmt_date, visible_projects

log = logging.getLogger(__name__)

# Texte gardé d'une page (garde-fou mémoire). La part transmise au modèle dépend du fournisseur (chat.page_limit) :
# au-delà, ce sont des extraits (passages utiles à la question, ou aperçu de toute la page), signalés comme tels.
MAX_CONTEXT_CHARS = 400_000
MAX_ITEMS = 60
_MENTION_SOURCES: list = []


def register_mention_source(fn):
    """fn(q: str, user) -> [{"type", "title", "subtitle", "icon", "url"}] : éléments proposés après « @ »."""
    if fn not in _MENTION_SOURCES:
        _MENTION_SOURCES.append(fn)
    return fn


# ---------------------------------------------------------------------------
# Reconnaissance de l'adresse et appel des résolveurs
# ---------------------------------------------------------------------------

def match(path: str) -> tuple[str | None, dict]:
    adapter = current_app.url_map.bind("localhost")
    for _ in range(2):
        try:
            endpoint, args = adapter.match(path, method="GET")
            return endpoint, dict(args)
        except RequestRedirect as r:
            from urllib.parse import urlsplit
            path = urlsplit(r.new_url).path
        except HTTPException:
            return None, {}
    return None, {}


def resolve(path: str, user, query: dict | None = None) -> dict | None:
    """Description de la page `path` pour l'utilisateur (droits vérifiés par chaque résolveur), ou None."""
    from . import _CONTEXT_RESOLVERS

    path = (path or "/").split("?")[0].split("#")[0] or "/"
    endpoint, view_args = match(path)
    args = {k: v for k, v in (query or {}).items() if isinstance(k, str)}
    args.update(view_args)
    args["_endpoint"] = endpoint or ""
    for fn in list(_CONTEXT_RESOLVERS) + [_builtin]:
        try:
            r = fn(path, dict(args), user)
        except Exception:  # noqa: BLE001 - un résolveur défaillant ne bloque pas l'assistant
            log.exception("Résolveur de contexte %s", getattr(fn, "__name__", fn))
            db.session.rollback()
            continue
        if r:
            out = {"type": str(r.get("type") or "page"), "title": str(r.get("title") or "Page"),
                   "text": str(r.get("text") or ""), "url": str(r.get("url") or path),
                   "icon": str(r.get("icon") or _ICONS.get(str(r.get("type")), "page"))}
            out["size"] = len(out["text"])  # taille d'origine (la page peut être coupée ensuite)
            out["text"] = out["text"][:MAX_CONTEXT_CHARS]
            out["key"] = page_key(out["url"], out["text"])
            return out
    return None


def page_key(url: str, text: str) -> str:
    return hashlib.sha1((url + "\n" + text).encode("utf-8")).hexdigest()[:16]


def _apercu(texte: str, budget: int, morceaux: int = 8) -> str:
    """Aperçu réparti sur toute la page : le début de `morceaux` tranches régulières, coupées à une fin de
    paragraphe ou de ligne (un quiz ou un résumé porte ainsi sur tout le cours, pas sur son seul début)."""
    part = max(1, budget // morceaux - 10)
    pas = len(texte) / morceaux
    sortie = []
    for i in range(morceaux):
        debut = int(i * pas)
        if i:  # reprendre au début d'un paragraphe ou d'une ligne
            for sep in ("\n\n", "\n"):
                j = texte.find(sep, debut, debut + part // 2)
                if j >= 0:
                    debut = j + len(sep)
                    break
        bout = texte[debut:debut + part]
        coupe = max(bout.rfind("\n\n"), bout.rfind("\n"))
        if coupe > part // 2:
            bout = bout[:coupe]
        sortie.append(bout.strip())
    return "\n\n[…]\n\n".join(b for b in sortie if b)


_SEPARATEUR_RE = re.compile(r"\n(?:[ \t\r]*\n)+")
_TITRE_RE = re.compile(r"(#{1,6})[ \t]")
_NOMBRE_RE = re.compile(r"\d+(?:[.,]\d+)*")
TAILLE_BLOC = 2000  # un paragraphe plus long est découpé (le budget se remplit alors au plus près)
MARQUE_COUPURE = "\n\n[…]\n\n"


def _blocs(texte: str, taille: int = TAILLE_BLOC) -> list[tuple[int, int]]:
    """Paragraphes de la page, en positions (début, fin) ; un paragraphe très long est découpé à une fin de ligne
    ou de phrase."""
    blocs, debut = [], 0
    for m in [*_SEPARATEUR_RE.finditer(texte), None]:
        fin = m.start() if m else len(texte)
        while fin - debut > taille:
            coupe = texte.rfind("\n", debut + taille // 2, debut + taille)
            if coupe < 0:
                coupe = texte.rfind(". ", debut + taille // 2, debut + taille)
                coupe = coupe + 1 if coupe >= 0 else debut + taille
            if texte[debut:coupe].strip():
                blocs.append((debut, coupe))
            debut = coupe
        if texte[debut:fin].strip():
            blocs.append((debut, fin))
        debut = m.end() if m else len(texte)
    return blocs


def extraits(texte: str, question: str, budget: int) -> tuple[str, bool]:
    """(texte transmis, coupé ?) : la page entière si elle tient dans `budget` caractères ; sinon des extraits qui
    remplissent le budget, choisis dans cet ordre :
    1. les paragraphes qui parlent le plus de la question (mots-clés et numéros, comme « chapitre 12 » ; un mot rare
       dans la page compte plus, un titre compte triple) ;
    2. la suite des sections dont le titre correspond (le texte du chapitre 12, pas seulement son titre) ;
    3. le reste de la page, au plus près de ces passages.
    Sans mot-clé trouvé dans la page : un aperçu de toute la page. « […] » marque chaque coupure."""
    if len(texte) <= budget:
        return texte, False
    from ..wiki.demander import mots_cles
    from ..wiki.vault import fold

    mots = mots_cles(question or "")
    nombres = list(dict.fromkeys(_NOMBRE_RE.findall(question or "")))[:10]
    blocs = _blocs(texte, min(TAILLE_BLOC, max(200, budget // 4)))  # le passage le plus utile tient toujours
    replie = fold(texte)
    niveaux = [len(t.group(1)) if (t := _TITRE_RE.match(texte, a)) else 0 for a, _ in blocs]
    comptes = []  # par paragraphe : {terme de la question : occurrences}
    for (a, b), niveau in zip(blocs, niveaux):
        c = {m: k for m in mots if (k := replie.count(m, a, b))}
        if nombres and (niveau or c):  # un numéro seul compte dans un titre, ou à côté d'un mot-clé
            jetons = _NOMBRE_RE.findall(replie, a, b)
            c.update({x: k for x in nombres if (k := jetons.count(x))})
        comptes.append(c)
    n = len(blocs)
    frequence = Counter(t for c in comptes for t in c)
    if not frequence:
        return _apercu(texte, budget), True
    poids = {t: math.log(1 + n / (1 + f)) for t, f in frequence.items()}
    scores = [(3 if niveau else 1) * sum(poids[t] * (1 + math.log(k)) for t, k in c.items())
              for c, niveau in zip(comptes, niveaux)]
    # Passages retenus : ceux qui parlent vraiment de la question, pas tous les « Chapitre N » pour « chapitre 12 ».
    seuil = max(scores) / 4
    trouves = sorted((i for i in range(n) if scores[i] and scores[i] >= seuil), key=lambda i: (-scores[i], i))
    rang: dict[int, tuple] = {i: (0, -scores[i], i) for i in trouves}
    for i in (i for i in trouves if niveaux[i]):
        j = i + 1
        while j < n and not (niveaux[j] and niveaux[j] <= niveaux[i]):
            j += 1
        if blocs[j - 1][1] - blocs[i][0] <= len(texte) // 2:  # pas pour le titre de toute la page
            for k in range(i + 1, j):
                rang.setdefault(k, (1, -scores[i], k))
    ancres, distance = set(trouves), [n] * n
    for sens in (range(n), range(n - 1, -1, -1)):
        proche = None
        for i in sens:
            if i in ancres:
                proche = i
            if proche is not None:
                distance[i] = min(distance[i], abs(i - proche))
    choisis, total, coupe = [], 0, None
    for i in sorted(range(n), key=lambda i: rang.get(i, (2, distance[i], i))):
        a, b = blocs[i]
        place = budget - total - len(MARQUE_COUPURE)
        if b - a <= place:
            choisis.append(i)
            total += b - a + len(MARQUE_COUPURE)
        elif i not in rang:
            # Contexte autour des passages : le paragraphe qui ne tient plus est coupé, et le remplissage s'arrête
            # (sinon les petits titres lointains combleraient la place, un par un).
            if place >= 200:
                fin = max(texte.rfind("\n", a, a + place), texte.rfind(". ", a, a + place) + 1)
                blocs[i] = (a, fin if fin > a + place // 2 else a + place)
                choisis.append(i)
                coupe = i
            break
    if not choisis:  # même le passage le plus utile dépasse le budget : son début
        i = min(range(n), key=lambda i: rang.get(i, (2, distance[i], i)))
        blocs[i] = (blocs[i][0], blocs[i][0] + max(0, budget - 20))
        choisis, coupe = [i], i
    suites: list[list[int]] = []  # paragraphes qui se suivent : recopiés tels quels, séparateurs compris
    for i in sorted(choisis):
        if suites and suites[-1][1] == i - 1 and coupe != i - 1:
            suites[-1][1] = i
        else:
            suites.append([i, i])
    sortie = MARQUE_COUPURE.join(texte[blocs[a][0]:blocs[b][1]].strip("\n") for a, b in suites)
    if suites and suites[0][0] > 0:
        sortie = "[…]\n\n" + sortie
    if suites and (suites[-1][1] < n - 1 or coupe == n - 1):
        sortie += "\n\n[…]"
    return sortie[:budget], True


def note_extraits(taille: int, transmis: int) -> str:
    """Mention ajoutée pour le modèle quand il ne reçoit que des extraits d'une page."""
    n = lambda x: f"{x:,}".replace(",", " ")  # noqa: E731
    return (f"[Page longue : seuls des extraits sont transmis (environ {n(transmis)} caractères sur {n(taille)}). "
            "Si la réponse n’y est pas, dis que tu ne l’as pas trouvée dans ces extraits, sans affirmer que la page "
            "n’en parle pas.]")


_ICONS = {"agenda": "calendar", "calendrier": "calendar", "taches": "list-check", "tache": "list-check",
          "ticket": "target", "tickets": "list", "projet": "briefcase", "cycle": "cycle", "cycles": "cycle",
          "module": "module", "modules": "module", "page": "page", "pages": "page", "wiki": "book", "qcm": "graduation",
          "accueil": "home", "workgraph": "workgraph"}


def _builtin(path, args, user):
    fn = _BY_ENDPOINT.get(args.get("_endpoint") or "")
    if fn is None and path.startswith("/wiki/"):
        fn = _wiki_page
    return fn(path, args, user) if fn else None


# ---------------------------------------------------------------------------
# Formatage commun
# ---------------------------------------------------------------------------

def _d(d) -> str:
    return fmt_date(d, True) if d else ""


def issue_line(i: Issue) -> str:
    bits = [f"{i.key} · {i.title}", i.state.name if i.state else "sans état"]
    if i.priority and i.priority != "none":
        bits.append(f"priorité {PRIORITY_LABELS.get(i.priority, i.priority).lower()}")
    if i.target_date:
        bits.append(f"échéance {_d(i.target_date)}" + (" (en retard)" if i.is_overdue else ""))
    if i.assignees:
        bits.append("assigné à " + ", ".join(u.display_name for u in i.assignees))
    if i.cycle:
        bits.append(f"cycle {i.cycle.name}")
    return " — ".join(bits)


def issue_text(i: Issue, comments: int = 20) -> str:
    p = i.project
    lines = [f"Ticket {i.key} : {i.title}", f"Projet : {p.name} ({p.identifier})",
             f"État : {i.state.name if i.state else '—'} ({i.state.group_label if i.state else '—'})"
             f" · Priorité : {PRIORITY_LABELS.get(i.priority, i.priority)}"]
    if i.assignees:
        lines.append("Assignés : " + ", ".join(u.display_name for u in i.assignees))
    if i.labels:
        lines.append("Étiquettes : " + ", ".join(l.name for l in i.labels))
    if i.start_date or i.target_date:
        lines.append(f"Début : {_d(i.start_date) or '—'} · Échéance : {_d(i.target_date) or '—'}"
                     + (" (en retard)" if i.is_overdue else ""))
    if i.cycle:
        lines.append(f"Cycle : {i.cycle.name} ({i.cycle.status_label})")
    if i.modules:
        lines.append("Modules : " + ", ".join(m.name for m in i.modules))
    if i.estimate is not None:
        lines.append(f"Estimation : {i.estimate_display}")
    if i.parent:
        lines.append(f"Ticket parent : {i.parent.key} · {i.parent.title}")
    kids = i.visible_children
    if kids:
        lines.append("Sous-tickets : " + " ; ".join(f"{k.key} {k.title} ({k.state.name if k.state else '—'})" for k in kids))
    rel = [f"{label} {other.key}" for label, other, _ in i.relations() if other]
    if rel:
        lines.append("Relations : " + ", ".join(rel))
    lines.append(f"Créé le {_d(i.created_at)} par {i.created_by.display_name if i.created_by else '—'}"
                 + (f" · terminé le {_d(i.completed_at)}" if i.completed_at else ""))
    lines.append("\nDescription :\n" + ((i.description or "").strip() or "(aucune)"))
    cs = Comment.query.filter_by(issue_id=i.id).order_by(Comment.created_at).all()
    if cs:
        shown = cs[-comments:]
        lines.append(f"\nCommentaires ({len(cs)}{', les ' + str(len(shown)) + ' derniers' if len(cs) > len(shown) else ''}) :")
        for c in shown:
            who = c.author.display_name if c.author else "?"
            lines.append(f"- {who}, {c.created_at:%d/%m/%Y %H:%M} : {(c.body or '').strip()}")
    return "\n".join(lines)


def _open_issues(project_ids, extra=None, limit=MAX_ITEMS):
    from .. import issue_query as IQ

    if not project_ids:
        return []
    q = IQ.base_query().join(State, Issue.state_id == State.id, isouter=True) \
        .filter(Issue.project_id.in_(project_ids), db.or_(State.group.is_(None), State.group.notin_(("completed", "cancelled"))))
    if extra is not None:
        q = q.filter(extra)
    issues = q.limit(400).all()
    order = {"urgent": 0, "high": 1, "medium": 2, "low": 3, "none": 4}
    issues.sort(key=lambda i: (i.target_date or date.max, order.get(i.priority, 9), i.id))
    return issues[:limit]


def _task_line(t: AgendaTask, today: date) -> str:
    from .. import agenda as A

    bits = [f"#{t.id} {t.title}"]
    if t.due_date:
        when = A.task_when(t)
        bits.append(when + (f" (en retard, {A.due_label(t.due_date, today)})" if t.is_overdue else ""))
    else:
        bits.append("sans date")
    if t.priority and t.priority != "none":
        bits.append(f"priorité {PRIORITY_LABELS.get(t.priority, t.priority).lower()}")
    if t.category:
        bits.append(t.category)
    if t.recurrence:
        bits.append("répétée")
    if t.done_at:
        bits.append(f"terminée le {_d(t.done_at)}")
    return " — ".join(bits)


def _event_line(ev, with_day: bool = False) -> str:
    from .. import agenda as A

    when = A.event_when(ev) if (with_day or ev.multi_day) else f"{A.hm(ev.start)}–{A.hm(ev.end)}"
    bits = [f"{when} {ev.title}"]
    extra = [x for x in (A.kind_label(ev.kind, True) if ev.kind else "", f"salle {ev.location}" if ev.location else "",
                         ev.teachers) if x]
    if extra:
        bits.append("(" + ", ".join(extra) + ")")
    if A.is_exam(ev):
        bits.append("[CONTRÔLE]")
    if ev.tentative:
        bits.append("[prévisionnel]")
    if ev.cancelled:
        bits.append("[annulé]")
    if ev.status == "removed":
        bits.append("[retiré du planning]")
    if ev.note:
        bits.append(f"— note : {ev.note.strip()}")
    return " ".join(bits)


def agenda_text(user, start: date, end: date, title: str | None = None) -> str:
    """Cours et tâches du `start` au `end` inclus, jour par jour."""
    from .. import agenda as A

    today = date.today()
    lines = [title] if title else []
    evs = A.events_between(user, datetime.combine(start, time(0)), datetime.combine(end + timedelta(days=1), time(0)),
                           include_removed=False)
    occs = A.task_occurrences(user, start, end, include_done=True)
    d = start
    while d <= end:
        day_evs = [e for e in evs if e.start.date() <= d <= e.last_day]
        day_tasks = [o for o in occs if o.date == d]
        if day_evs or day_tasks:
            lines.append(f"{A.day_long(d).capitalize()}{' (aujourd’hui)' if d == today else ''} :")
            for e in day_evs:
                lines.append(f"- {_event_line(e)}")
            for o in day_tasks:
                t = o.task
                done = " [faite]" if t.done_at else ""
                hour = f"{A.hm(t.start_time)} " if t.start_time else ""
                lines.append(f"- tâche #{t.id} : {hour}{t.title}{done}{' (occurrence répétée)' if o.ghost else ''}")
        d += timedelta(days=1)
    if len(lines) <= (1 if title else 0):
        lines.append("Rien de prévu sur cette période.")
    return "\n".join(lines)


def tasks_text(user, today: date | None = None) -> str:
    from .. import agenda as A

    today = today or date.today()
    groups = A.open_task_groups(user, today)
    lines = []
    for key, label in A.TASK_GROUP_LABELS:
        items = groups.get(key) or []
        if items:
            lines.append(f"{label} ({len(items)}) :")
            lines += [f"- {_task_line(t, today)}" for t in items[:40]]
    return "\n".join(lines) or "Aucune tâche ouverte."


# ---------------------------------------------------------------------------
# Résolveurs de Hangar
# ---------------------------------------------------------------------------

def _agenda_dashboard(path, args, user):
    from .. import agenda as A
    from .. import ics

    today = date.today()
    d = A.dashboard(user)
    lines = [f"Tableau de bord de l’agenda — {A.day_long(today)}"]
    if d["current"]:
        lines.append(f"En ce moment : {_event_line(d['current'])}")
    if d["next"]:
        lines.append(f"Prochain cours : {_event_line(d['next'], with_day=True)}")
    exams = d["exams"]
    if exams:
        lines.append("\nContrôles à venir (45 jours) :")
        lines += [f"- {_event_line(e, with_day=True)}" for e in exams[:15]]
    ws = ics.week_start(today)
    lines.append("\n" + agenda_text(user, today, max(ws + timedelta(days=6), today + timedelta(days=2)),
                                    "Cours et tâches d’aujourd’hui à la fin de la semaine :"))
    lines.append("\nTâches ouvertes :\n" + tasks_text(user, today))
    if d["changes"]:
        lines.append("\nChangements récents du planning :")
        lines += [f"- {_event_line(e, with_day=True)} ({e.change_kind})" for e in d["changes"][:10]]
    return {"type": "agenda", "title": "Tableau de bord de l’agenda", "text": "\n".join(lines),
            "url": url_for("agenda.dashboard"), "icon": "calendar"}


def _agenda_calendar(path, args, user):
    from .. import ics
    from ..utils import parse_date

    ref = parse_date(args.get("date")) or date.today()
    view = args.get("view") or "week"
    if view == "month":
        start = ref.replace(day=1)
        end = (start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)) \
            - timedelta(days=1)
        title = f"Calendrier du mois ({fmt_date(start, True)} – {fmt_date(end, True)})"
    else:
        start = ics.week_start(ref)
        end = start + timedelta(days=6)
        title = f"Calendrier de la semaine du {fmt_date(start, True)}"
    return {"type": "calendrier", "title": title, "text": agenda_text(user, start, end, title + " :"),
            "url": path, "icon": "calendar"}


def _agenda_tasks(path, args, user):
    from .. import agenda as A

    focus = args.get("focus")
    if focus and str(focus).isdigit():
        t = db.session.get(AgendaTask, int(focus))
        if t is not None and t.user_id == user.id:
            lines = [f"Tâche {_task_line(t, date.today())}"]
            if t.notes:
                lines.append(f"Notes :\n{t.notes.strip()}")
            if t.event:
                lines.append(f"Liée au cours : {_event_line(t.event, with_day=True)}")
            if t.reminders:
                lines.append("Rappels : " + ", ".join(r.label for r in t.reminders))
            return {"type": "tache", "title": f"Tâche · {t.title}", "text": "\n".join(lines),
                    "url": url_for("agenda.tasks", focus=t.id), "icon": "list-check"}
    done = A.AgendaTask.query.filter(A.AgendaTask.user_id == user.id, A.AgendaTask.done_at.isnot(None)).count()
    return {"type": "taches", "title": "Tâches", "icon": "list-check", "url": url_for("agenda.tasks"),
            "text": "Tâches de l’agenda\n" + tasks_text(user) + f"\n\n({done} tâche(s) terminée(s) non listée(s).)"}


def _project_or_none(args, user):
    p = db.session.get(Project, int(args.get("pid") or 0))
    return p if p is not None and can(user, "view", p) else None


def _issue_detail(path, args, user):
    p = _project_or_none(args, user)
    if p is None:
        return None
    i = Issue.query.filter_by(project_id=p.id, sequence_id=int(args.get("seq") or 0)).first()
    if i is None:
        return None
    return {"type": "ticket", "title": f"{i.key} · {i.title}", "text": issue_text(i), "icon": "target",
            "url": url_for("issues.detail", pid=p.id, seq=i.sequence_id)}


def _issue_by_key(path, args, user):
    key = str(args.get("key") or "")
    if "-" not in key:
        return None
    ident, _, seq = key.upper().rpartition("-")
    p = Project.query.filter_by(identifier=ident).first()
    if p is None or not seq.isdigit():
        return None
    return _issue_detail(path, {"pid": p.id, "seq": int(seq)}, user)


def _project_issues(path, args, user):
    p = _project_or_none(args, user)
    if p is None:
        return None
    s = p.stats()
    issues = _open_issues([p.id])
    lines = [f"Projet {p.name} ({p.identifier}) — liste des tickets",
             f"{s['total']} tickets : {s['open']} ouverts, {s['done']} terminés ({s['progress']} %)."]
    lines.append(f"\nTickets ouverts ({len(issues)}{' premiers' if len(issues) >= MAX_ITEMS else ''}) :")
    lines += [f"- {issue_line(i)}" for i in issues]
    return {"type": "tickets", "title": f"{p.name} · Tickets", "text": "\n".join(lines), "icon": "list",
            "url": url_for("issues.index", pid=p.id)}


def _project_overview(path, args, user):
    p = _project_or_none(args, user)
    if p is None:
        return None
    s = p.stats()
    lines = [f"Projet {p.name} ({p.identifier})", (p.description or "").strip() or "(pas de description)",
             f"Tickets : {s['total']} au total, {s['open']} ouverts, {s['done']} terminés ({s['progress']} %).",
             "Membres : " + ", ".join(u.display_name for u in p.member_users)]
    if p.lead:
        lines.append(f"Responsable : {p.lead.display_name}")
    if p.start_date or p.target_date:
        lines.append(f"Dates : {_d(p.start_date) or '—'} → {_d(p.target_date) or '—'}")
    cycles = [c for c in p.cycles if c.archived_at is None and c.status in ("current", "upcoming")]
    if cycles:
        lines.append("Cycles : " + " ; ".join(f"{c.name} ({c.status_label}, {_d(c.start_date)} → {_d(c.end_date)}, "
                                               f"{c.progress()['pct']} %)" for c in cycles))
    mods = [m for m in p.modules if m.archived_at is None]
    if mods:
        lines.append("Modules : " + " ; ".join(f"{m.name} ({m.status_label}, {m.progress()['pct']} %)" for m in mods))
    overdue = [i for i in _open_issues([p.id]) if i.is_overdue]
    if overdue:
        lines.append("\nTickets en retard :")
        lines += [f"- {issue_line(i)}" for i in overdue[:20]]
    return {"type": "projet", "title": p.name, "text": "\n".join(lines), "icon": "briefcase",
            "url": url_for("projects.detail", pid=p.id)}


def _cycle_detail(path, args, user):
    p = _project_or_none(args, user)
    c = db.session.get(Cycle, int(args.get("cid") or 0)) if p else None
    if c is None or c.project_id != p.id:
        return None
    pr = c.progress()
    lines = [f"Cycle « {c.name} » du projet {p.name} — {c.status_label}",
             f"Du {_d(c.start_date) or '—'} au {_d(c.end_date) or '—'}"
             + (f" ({c.days_left} jour(s) restant(s))" if c.status == "current" and c.days_left is not None else ""),
             f"Avancement : {pr['done']} / {pr['total']} tickets terminés ({pr['pct']} %)."]
    if c.description:
        lines.append(c.description.strip())
    lines.append("\nTickets du cycle :")
    lines += [f"- {issue_line(i)}" for i in sorted(c.active_issues, key=lambda i: (i.is_closed, i.target_date or date.max))[:MAX_ITEMS]]
    return {"type": "cycle", "title": f"{p.name} · {c.name}", "text": "\n".join(lines), "icon": "cycle",
            "url": url_for("cycles.detail", pid=p.id, cid=c.id)}


def _cycles_index(path, args, user):
    p = _project_or_none(args, user)
    if p is None:
        return None
    lines = [f"Cycles du projet {p.name} :"]
    for c in p.cycles:
        if c.archived_at is None:
            pr = c.progress()
            lines.append(f"- {c.name} — {c.status_label}, {_d(c.start_date) or '—'} → {_d(c.end_date) or '—'}, "
                         f"{pr['done']}/{pr['total']} tickets terminés")
    return {"type": "cycles", "title": f"{p.name} · Cycles", "text": "\n".join(lines), "icon": "cycle", "url": path}


def _module_detail(path, args, user):
    p = _project_or_none(args, user)
    m = db.session.get(Module, int(args.get("mid") or 0)) if p else None
    if m is None or m.project_id != p.id:
        return None
    pr = m.progress()
    lines = [f"Module « {m.name} » du projet {p.name} — {m.status_label}",
             f"Responsable : {m.lead.display_name if m.lead else '—'} · Dates : {_d(m.start_date) or '—'} → "
             f"{_d(m.target_date) or '—'}", f"Avancement : {pr['done']} / {pr['total']} ({pr['pct']} %)."]
    if m.description:
        lines.append(m.description.strip())
    lines.append("\nTickets du module :")
    lines += [f"- {issue_line(i)}" for i in m.active_issues[:MAX_ITEMS]]
    return {"type": "module", "title": f"{p.name} · {m.name}", "text": "\n".join(lines), "icon": "module",
            "url": url_for("modules.detail", pid=p.id, mid=m.id)}


def _modules_index(path, args, user):
    p = _project_or_none(args, user)
    if p is None:
        return None
    lines = [f"Modules du projet {p.name} :"]
    for m in p.modules:
        if m.archived_at is None:
            pr = m.progress()
            lines.append(f"- {m.name} — {m.status_label}, {pr['done']}/{pr['total']} tickets terminés")
    return {"type": "modules", "title": f"{p.name} · Modules", "text": "\n".join(lines), "icon": "module", "url": path}


def page_visible(page: Page, user) -> bool:
    return page.archived_at is None and can(user, "view", page.project) and (page.is_public or page.owner_id == user.id)


def page_editable(page: Page, user) -> bool:
    if not can(user, "edit", page.project):
        return False
    if page.is_locked and page.owner_id != user.id and not can(user, "admin", page.project):
        return False
    return page.is_public or page.owner_id == user.id


def _page_view(path, args, user):
    pg = db.session.get(Page, int(args.get("page_id") or 0))
    if pg is None or pg.project_id != int(args.get("pid") or 0) or not page_visible(pg, user):
        return None
    text = f"Page « {pg.title} » du projet {pg.project.name} (modifiée le {_d(pg.updated_at)})\n\n{pg.content or ''}"
    return {"type": "page", "title": f"{pg.emoji or ''} {pg.title}".strip(), "text": text, "icon": "page",
            "url": url_for("pages.view", pid=pg.project_id, page_id=pg.id)}


def _pages_index(path, args, user):
    p = _project_or_none(args, user)
    if p is None:
        return None
    pages = [pg for pg in Page.query.filter_by(project_id=p.id).order_by(Page.updated_at.desc()).limit(80)
             if page_visible(pg, user)]
    lines = [f"Pages du projet {p.name} :"] + [f"- {pg.title} (modifiée le {_d(pg.updated_at)})" for pg in pages]
    return {"type": "pages", "title": f"{p.name} · Pages", "text": "\n".join(lines), "icon": "page", "url": path}


def _home(path, args, user):
    pids = [p.id for p in visible_projects(user)]
    mine = _open_issues(pids, Issue.assignees.any(id=user.id))
    lines = ["Accueil des projets — mes tickets ouverts :"] + [f"- {issue_line(i)}" for i in mine]
    if not mine:
        lines.append("(aucun ticket ouvert assigné)")
    cycles = [c for p in visible_projects(user) if p.cycles_enabled for c in p.cycles
              if c.archived_at is None and c.status == "current"]
    if cycles:
        lines.append("\nCycles en cours :")
        lines += [f"- {c.project.name} · {c.name} (jusqu’au {_d(c.end_date)}, {c.progress()['pct']} %)" for c in cycles]
    return {"type": "accueil", "title": "Accueil des projets", "text": "\n".join(lines), "icon": "home", "url": path}


def _workgraph(path, args, user):
    projects = visible_projects(user)
    lines = ["Vue d’ensemble du travail (projets visibles) :"]
    for p in projects:
        s = p.stats()
        cur = next((c for c in p.cycles if c.archived_at is None and c.status == "current"), None)
        overdue = sum(1 for i in _open_issues([p.id]) if i.is_overdue)
        lines.append(f"- {p.name} ({p.identifier}) : {s['open']} ouverts, {s['done']} terminés ({s['progress']} %)"
                     + (f", cycle en cours « {cur.name} » ({cur.progress()['pct']} %)" if cur else "")
                     + (f", {overdue} en retard" if overdue else ""))
    return {"type": "workgraph", "title": "Workgraph", "text": "\n".join(lines), "icon": "workgraph", "url": path}


def wiki_vault():
    """Coffre du wiki si le module wiki est disponible (import avec repli), sinon None."""
    try:
        from ..wiki.vault import get_vault  # type: ignore
        return get_vault()
    except Exception:  # noqa: BLE001
        return None


def wiki_note_for(path: str):
    vault = wiki_vault()
    if vault is None:
        return None, None
    try:
        for note in vault.notes.values():
            if getattr(note, "url", None) == path:
                return vault, note
    except Exception:  # noqa: BLE001
        pass
    return vault, None


def _wiki_page(path, args, user):
    vault, note = wiki_note_for(path)
    if note is None:
        return None
    try:
        from ..wiki.vault import lire_note
        body = lire_note(vault.root / note.path)[0]  # page « ANSI » comprise
    except Exception:  # noqa: BLE001
        body = getattr(note, "body", "") or ""
    return {"type": "wiki", "title": getattr(note, "title", path), "text": f"Page du wiki « {note.title} »\n\n{body}",
            "icon": "book", "url": path}


_BY_ENDPOINT = {
    "agenda.dashboard": _agenda_dashboard, "agenda.calendar": _agenda_calendar, "agenda.tasks": _agenda_tasks,
    "issues.detail": _issue_detail, "issues.by_key": _issue_by_key, "issues.index": _project_issues,
    "projects.detail": _project_overview, "cycles.detail": _cycle_detail, "cycles.index": _cycles_index,
    "modules.detail": _module_detail, "modules.index": _modules_index, "pages.view": _page_view,
    "pages.edit": _page_view, "pages.index": _pages_index, "home.index": _home, "home.workspace_issues": _home,
    "workgraph.index": _workgraph, "workgraph.initiatives": _workgraph,
}


# ---------------------------------------------------------------------------
# Éléments « @ »
# ---------------------------------------------------------------------------

def search_mentions(q: str, user, limit: int = 14) -> list[dict]:
    q = (q or "").strip()
    like = f"%{q}%"
    projects = visible_projects(user)
    pids = [p.id for p in projects]
    out: list[dict] = []
    ql = q.lower()
    for p in projects:
        if not q or ql in p.name.lower() or ql in p.identifier.lower():
            out.append({"type": "projet", "title": p.name, "subtitle": p.identifier, "icon": "briefcase",
                        "url": url_for("projects.detail", pid=p.id)})
    issues = []
    if pids:
        if "-" in q and q.rsplit("-", 1)[1].isdigit():
            ident, seq = q.upper().rsplit("-", 1)
            issues = Issue.query.join(Project).filter(Project.identifier == ident, Issue.sequence_id == int(seq),
                                                      Issue.project_id.in_(pids)).all()
        if not issues:
            iq = Issue.query.filter(Issue.project_id.in_(pids), Issue.archived_at.is_(None), Issue.is_visible_clause())
            iq = iq.filter(Issue.title.ilike(like)) if q else iq.filter(Issue.assignees.any(id=user.id))
            issues = iq.order_by(Issue.updated_at.desc()).limit(6).all()
    out += [{"type": "ticket", "title": f"{i.key} · {i.title}", "subtitle": i.project.name, "icon": "target",
             "url": url_for("issues.detail", pid=i.project_id, seq=i.sequence_id)} for i in issues]
    if pids:
        pq = Page.query.filter(Page.project_id.in_(pids), Page.archived_at.is_(None))
        if q:
            pq = pq.filter(Page.title.ilike(like))
        out += [{"type": "page", "title": pg.title, "subtitle": pg.project.name, "icon": "page",
                 "url": url_for("pages.view", pid=pg.project_id, page_id=pg.id)}
                for pg in pq.order_by(Page.updated_at.desc()).limit(5) if page_visible(pg, user)]
        if q:
            out += [{"type": "cycle", "title": c.name, "subtitle": c.project.name, "icon": "cycle",
                     "url": url_for("cycles.detail", pid=c.project_id, cid=c.id)}
                    for c in Cycle.query.filter(Cycle.project_id.in_(pids), Cycle.archived_at.is_(None),
                                                Cycle.name.ilike(like)).limit(4)]
            out += [{"type": "module", "title": m.name, "subtitle": m.project.name, "icon": "module",
                     "url": url_for("modules.detail", pid=m.project_id, mid=m.id)}
                    for m in Module.query.filter(Module.project_id.in_(pids), Module.archived_at.is_(None),
                                                 Module.name.ilike(like)).limit(4)]
    tq = AgendaTask.query.filter(AgendaTask.user_id == user.id, AgendaTask.done_at.is_(None))
    if q:
        tq = tq.filter(AgendaTask.title.ilike(like))
    out += [{"type": "tache", "title": t.title, "subtitle": "Tâche" + (f" · {_d(t.due_date)}" if t.due_date else ""),
             "icon": "list-check", "url": url_for("agenda.tasks", focus=t.id)}
            for t in tq.order_by(AgendaTask.due_date.is_(None), AgendaTask.due_date).limit(5)]
    if not q:
        out.insert(0, {"type": "agenda", "title": "Mon agenda (cette semaine)", "subtitle": "Cours, contrôles, tâches",
                       "icon": "calendar", "url": url_for("agenda.dashboard")})
    out += _wiki_mentions(q)
    for fn in list(_MENTION_SOURCES):
        try:
            out += [dict(x) for x in (fn(q, user) or [])][:6]
        except Exception:  # noqa: BLE001
            log.exception("Source d'éléments « @ »")
    seen, uniq = set(), []
    for x in out:
        if x.get("url") and x["url"] not in seen:
            seen.add(x["url"])
            uniq.append(x)
    return uniq[:limit]


def _wiki_mentions(q: str) -> list[dict]:
    vault = wiki_vault()
    if vault is None or not q:
        return []
    ql = q.lower()
    try:
        notes = [n for n in vault.notes.values() if ql in str(getattr(n, "title", "")).lower()][:6]
        return [{"type": "wiki", "title": n.title, "subtitle": "Wiki", "icon": "book", "url": n.url} for n in notes]
    except Exception:  # noqa: BLE001
        return []
