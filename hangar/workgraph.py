"""Initiatives et Workgraph : avancement, santé, jalons, dépendances et graphe de l'espace de travail.

Calculs sans dépendance à la requête HTTP (sauf `url_for`), utilisés par les vues (hangar/views/workgraph.py),
les tests, le résumé quotidien (section « travail.initiatives ») et l'assistant IA (résolveur de contexte).

Règles :
- avancement = tickets terminés / tickets non annulés, sur l'ensemble des tickets des projets liés et des
  modules (épopées) liés à l'initiative (sans doublon) ;
- santé calculée (voir `compute_health`) d'après l'échéance, l'avancement attendu au vu du temps écoulé
  et la part de tickets en retard ; elle peut être forcée (`Initiative.health_override`) ;
- dépendances : relations « bloqué par » entre tickets de modules, de projets ou d'initiatives différents.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import url_for
from markupsafe import escape

from . import digest
from .extensions import db
from .icons import ICONS
from .models import (
    CLOSED_GROUPS, HEALTH_COLORS, HEALTH_LABELS, HEALTH_RANK, INITIATIVE_STATUSES, MODULE_STATUS_COLORS,
    MODULE_STATUS_LABELS, STATE_GROUPS, Initiative, Issue, IssueRelation, Label, Module, Project, State, User,
    Workspace, issue_labels, issue_modules,
)

# Icônes propres au module (sans toucher au fichier partagé hangar/icons.py)
ICONS.setdefault("lightbulb", '<path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 '
                              '1.5 3.5.7.7 1.3 1.5 1.5 2.5"/><path d="M9 18h6"/><path d="M10 22h4"/>')
ICONS.setdefault("zoom-in", '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/><path d="M11 8v6"/>'
                            '<path d="M8 11h6"/>')
ICONS.setdefault("zoom-out", '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/><path d="M8 11h6"/>')
ICONS.setdefault("fit", '<path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/>'
                        '<path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/>'
                        '<circle cx="12" cy="12" r="3"/>')
ICONS.setdefault("unlink", '<path d="m18.84 12.25 1.72-1.71a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>'
                           '<path d="m5.17 11.75-1.71 1.71a5 5 0 0 0 7.07 7.07l1.71-1.71"/>'
                           '<path d="M8 2v3"/><path d="M2 8h3"/><path d="M16 19v3"/><path d="M19 16h3"/>')
ICONS.setdefault("ban", '<circle cx="12" cy="12" r="10"/><path d="m4.9 4.9 14.2 14.2"/>')

STATUS_ORDER = {k: i for i, (k, _, _) in enumerate(INITIATIVE_STATUSES)}
GRAPH_STATUS_ORDER = {"active": 0, "planned": 1, "draft": 2, "completed": 3, "cancelled": 4}
MILESTONE_LABELS = ("jalon", "jalons", "milestone", "milestones")


# ---------------------------------------------------------------------------
# Avancement et santé (fonctions pures)
# ---------------------------------------------------------------------------

def stats_of(issues, today: date | None = None) -> dict:
    """Compte les tickets par groupe d'état ; `issues` : objets ayant `state_group` et `target_date`."""
    today = today or date.today()
    counts = {g: 0 for g, _, _ in STATE_GROUPS}
    overdue = 0
    for i in issues:
        g = i.state_group
        counts[g] = counts.get(g, 0) + 1
        if g not in CLOSED_GROUPS and i.target_date and i.target_date < today:
            overdue += 1
    total = sum(counts.values())
    done = counts["completed"]
    active = total - counts["cancelled"]
    return {"counts": counts, "total": total, "done": done, "open": active - done, "overdue": overdue,
            "pct": round(100 * done / active) if active else 0}


def _days(n: int) -> str:
    return f"{n} jour" if abs(n) <= 1 else f"{n} jours"


def compute_health(stats: dict, start: date | None, target: date | None, today: date | None = None,
                   closed: bool = False) -> tuple[str, list[str]]:
    """Santé calculée (« on_track », « at_risk », « off_track ») et ses raisons.

    - échéance dépassée sans que tout soit terminé : en retard ;
    - retard sur l'avancement attendu (part du temps écoulé entre début et échéance) :
      20 points ou plus = à risque, 35 points ou plus = en retard ;
    - échéance dans 7 jours ou moins avec moins de 80 % terminé : à risque ;
    - tickets en retard : 20 % des tickets ouverts (au moins 2) = à risque, 50 % (au moins 3) = en retard.
    """
    today = today or date.today()
    if closed:
        return "on_track", []
    level = "on_track"
    reasons: list[str] = []

    def worse(h: str, why: str):
        nonlocal level
        if HEALTH_RANK[h] > HEALTH_RANK[level]:
            level = h
        reasons.append(why)

    pct = stats.get("pct", 0)
    active = stats.get("total", 0) - stats.get("counts", {}).get("cancelled", 0)
    unfinished = pct < 100 or not active
    if target and unfinished:
        if target < today:
            worse("off_track", f"Échéance dépassée depuis {_days((today - target).days)}")
        else:
            if start and start < target and today > start and active:
                expected = round(100 * min(1.0, (today - start).days / (target - start).days))
                gap = expected - pct
                if gap >= 35:
                    worse("off_track", f"{pct} % terminé pour {expected} % du temps écoulé")
                elif gap >= 20:
                    worse("at_risk", f"{pct} % terminé pour {expected} % du temps écoulé")
            left = (target - today).days
            if left <= 7 and pct < 80:
                worse("at_risk", f"Échéance dans {_days(left)} avec {pct} % terminé")
    open_, late = stats.get("open", 0), stats.get("overdue", 0)
    if open_ and late:
        ratio = late / open_
        if late >= 3 and ratio >= 0.5:
            worse("off_track", f"{late} tickets en retard sur {open_} ouverts")
        elif late >= 2 and ratio >= 0.2:
            worse("at_risk", f"{late} tickets en retard sur {open_} ouverts")
    return level, reasons


def health_info(key: str | None) -> dict | None:
    if not key:
        return None
    return {"key": key, "label": HEALTH_LABELS.get(key, key), "color": HEALTH_COLORS.get(key, "#a3a3a3")}


def readable_project_ids(user) -> set[int]:
    """Projets dont un compte peut lire le contenu (titres de tickets, modules, jalons), archivés compris :
    mêmes règles que partout ailleurs (`visible_projects`)."""
    from .utils import visible_projects
    return {p.id for p in visible_projects(user, include_archived=True)}


# ---------------------------------------------------------------------------
# Instantané de l'espace : tickets, modules, relations (chargés une fois par requête)
# ---------------------------------------------------------------------------

class _Rec:
    """Ticket allégé (lecture seule) pour les calculs en masse."""
    __slots__ = ("id", "project_id", "seq", "key", "title", "target_date", "state_group", "modules")

    def __init__(self, id, project_id, seq, key, title, target_date, state_group):
        self.id, self.project_id, self.seq, self.key, self.title = id, project_id, seq, key, title
        self.target_date, self.state_group = target_date, state_group or "backlog"
        self.modules: list[int] = []

    @property
    def is_closed(self) -> bool:
        return self.state_group in CLOSED_GROUPS


class Snapshot:
    """Données de tout l'espace nécessaires au Workgraph, aux listes et au résumé quotidien.

    Les avancements des initiatives portent sur tous les projets liés (même ceux qu'un compte ne voit pas) ;
    la visibilité n'intervient qu'à l'affichage : `initiative_for` ne garde, pour un lecteur, que les tickets
    et dépendances des projets qu'il peut lire."""

    def __init__(self, today: date | None = None):
        self.today = today or date.today()
        self.projects = {p.id: p for p in Project.query.order_by(Project.name).all()}
        self.modules = {m.id: m for m in Module.query.order_by(Module.name).all()}
        rows = (db.session.query(Issue.id, Issue.project_id, Issue.sequence_id, Issue.title, Issue.target_date,
                                 State.group)
                .outerjoin(State, Issue.state_id == State.id)
                .filter(Issue.archived_at.is_(None), Issue.is_visible_clause()).all())
        self.issues: dict[int, _Rec] = {}
        self.by_project: dict[int, list[_Rec]] = defaultdict(list)
        self.by_module: dict[int, list[_Rec]] = defaultdict(list)
        for iid, pid, seq, title, target, group in rows:
            p = self.projects.get(pid)
            r = _Rec(iid, pid, seq, f"{p.identifier if p else '?'}-{seq}", title, target, group)
            self.issues[iid] = r
            self.by_project[pid].append(r)
        for iid, mid in db.session.query(issue_modules.c.issue_id, issue_modules.c.module_id).all():
            r = self.issues.get(iid)
            if r is not None and mid in self.modules:
                r.modules.append(mid)
                self.by_module[mid].append(r)
        # (bloqué, bloquant) : relations « bloqué par » entre tickets actifs
        self.blocked_by = [(a, b) for a, b in db.session.query(IssueRelation.issue_id, IssueRelation.related_id)
                           .filter(IssueRelation.relation_type == "blocked_by").all()
                           if a in self.issues and b in self.issues]
        self._ini_cache: dict[int, dict] = {}

    # -- ensembles de tickets -------------------------------------------------
    def initiative_issue_ids(self, ini: Initiative) -> set[int]:
        ids = {r.id for p in ini.projects for r in self.by_project.get(p.id, ())}
        ids |= {r.id for m in ini.modules for r in self.by_module.get(m.id, ())}
        return ids

    def stats(self, ids) -> dict:
        return stats_of((self.issues[i] for i in ids if i in self.issues), self.today)

    def project_stats(self, pid: int) -> dict:
        return stats_of(self.by_project.get(pid, ()), self.today)

    def module_stats(self, mid: int) -> dict:
        return stats_of(self.by_module.get(mid, ()), self.today)

    # -- santé ------------------------------------------------------------------
    def project_health(self, p: Project, st: dict | None = None) -> tuple[str, list[str]]:
        st = st or self.project_stats(p.id)
        return compute_health(st, p.start_date or (p.created_at.date() if p.created_at else None), p.target_date,
                              self.today, closed=p.archived_at is not None)

    def module_health(self, m: Module, st: dict | None = None) -> tuple[str, list[str]]:
        st = st or self.module_stats(m.id)
        if m.status == "cancelled":
            return None, []
        return compute_health(st, m.start_date or (m.created_at.date() if m.created_at else None), m.target_date,
                              self.today, closed=m.status == "completed")

    def initiative(self, ini: Initiative) -> dict:
        """Résumé calculé d'une initiative (mis en cache pour la requête)."""
        if ini.id in self._ini_cache:
            return self._ini_cache[ini.id]
        ids = self.initiative_issue_ids(ini)
        st = self.stats(ids)
        start, target = effective_dates(ini)
        auto, reasons = compute_health(st, start, target, self.today, closed=ini.status == "completed")
        if ini.status == "cancelled":
            health, auto, reasons = None, None, []
        elif ini.health_override in HEALTH_LABELS:
            health = ini.health_override
        else:
            health = auto
        blockers = [] if ini.is_closed else self.blockers(ids)
        out = {
            "id": ini.id, "pct": st["pct"], "stats": st, "issue_ids": ids,
            "health": health, "health_info": health_info(health), "health_auto": auto,
            "health_auto_info": health_info(auto), "forced": bool(ini.health_override in HEALTH_LABELS
                                                                  and ini.status != "cancelled"),
            "reasons": reasons, "start": start, "target": target,
            "late": bool(target and target < self.today and not ini.is_closed),
            "days_left": (target - self.today).days if target else None,
            "blockers": blockers, "blocked_open": len({b["blocked"].id for b in blockers}),
            "blockers_external": sum(1 for b in blockers if b["external"]),
        }
        self._ini_cache[ini.id] = out
        return out

    def initiative_for(self, ini: Initiative, readable: set[int] | None) -> dict:
        """Résumé de l'initiative vu par un lecteur : l'avancement et la santé restent ceux de toute
        l'initiative (des chiffres), mais les tickets (`issue_ids`, d'où les jalons) et les dépendances
        bloquantes se limitent aux projets qu'il peut lire (`readable`, voir `readable_project_ids` ; None = tout)."""
        s = self.initiative(ini)
        if readable is None:
            return s
        blockers = [b for b in s["blockers"]
                    if b["blocked"].project_id in readable and b["blocker"].project_id in readable]
        return {**s, "issue_ids": {i for i in s["issue_ids"] if self.issues[i].project_id in readable},
                "blockers": blockers, "blockers_hidden": len(s["blockers"]) - len(blockers)}

    # -- dépendances --------------------------------------------------------------
    def blockers(self, ids: set[int]) -> list[dict]:
        """Dépendances ouvertes qui freinent un ensemble de tickets (bloqué ouvert, bloquant ouvert)."""
        out = []
        for a, b in self.blocked_by:
            if a in ids:
                blocked, blocker = self.issues[a], self.issues[b]
                if not blocked.is_closed and not blocker.is_closed:
                    out.append({"blocked": blocked, "blocker": blocker, "external": b not in ids})
        out.sort(key=lambda d: (not d["external"], d["blocked"].key))
        return out


def effective_dates(ini: Initiative) -> tuple[date | None, date | None]:
    """Dates de l'initiative, ou à défaut celles déduites des projets et modules liés."""
    start, target = ini.start_date, ini.target_date
    linked = list(ini.projects) + list(ini.modules)
    if start is None:
        starts = [x.start_date for x in linked if x.start_date]
        start = min(starts) if starts else (ini.created_at.date() if ini.created_at else None)
    if target is None:
        targets = [x.target_date for x in linked if x.target_date]
        target = max(targets) if targets else None
    return start, target


def initiative_issues(ini: Initiative) -> list[Issue]:
    """Tickets (objets complets) des projets et modules liés, sans doublon."""
    pids = [p.id for p in ini.projects]
    mids = [m.id for m in ini.modules]
    if not pids and not mids:
        return []
    cond = []
    if pids:
        cond.append(Issue.project_id.in_(pids))
    if mids:
        cond.append(Issue.modules.any(Module.id.in_(mids)))
    return (Issue.query.filter(Issue.archived_at.is_(None), Issue.is_visible_clause(), db.or_(*cond))
            .order_by(Issue.project_id, Issue.sequence_id).all())


def refresh_health(ini: Initiative, health: str | None, at: datetime | None = None) -> bool:
    """Retient la dernière santé constatée (et depuis quand) ; True si elle a changé."""
    if ini.health_seen != health:
        ini.health_seen = health
        ini.health_since = at or datetime.now().replace(microsecond=0)
        return True
    return False


def milestones(ini: Initiative, snap: Snapshot, issue_ids: set[int] | None = None,
               readable: set[int] | None = None) -> list[dict]:
    """Jalons datés : début et échéance de l'initiative, fins des projets et modules, tickets « Jalon ».

    `readable` : projets que le lecteur peut lire (None = tous) ; ceux des autres projets n'apparaissent pas."""
    today = snap.today
    ids = issue_ids if issue_ids is not None else snap.initiative_issue_ids(ini)

    def ok(pid) -> bool:
        return readable is None or pid in readable

    out = []

    def add(d, title, kind, done, url=None, sub=""):
        if d:
            out.append({"date": d, "title": title, "kind": kind, "done": bool(done), "url": url, "sub": sub,
                        "late": d < today and not done})

    url = url_for("workgraph.initiative_detail", iid=ini.id)
    add(ini.start_date, "Début de l’initiative", "initiative", ini.start_date and ini.start_date <= today, url)
    for p in (p for p in ini.projects if ok(p.id)):
        st = snap.project_stats(p.id)
        add(p.target_date, f"Fin du projet {p.name}", "project",
            p.archived_at is not None or (st["total"] and st["pct"] == 100),
            url_for("issues.index", pid=p.id), p.identifier)
    mods = {m.id: m for p in ini.projects for m in p.modules if m.archived_at is None}
    mods.update({m.id: m for m in ini.modules})
    mods = {mid: m for mid, m in mods.items() if ok(m.project_id)}
    for m in sorted(mods.values(), key=lambda m: m.name.lower()):
        add(m.target_date, f"Module « {m.name} »", "module", m.status in ("completed", "cancelled"),
            url_for("modules.detail", pid=m.project_id, mid=m.id), snap.projects[m.project_id].identifier
            if m.project_id in snap.projects else "")
    if ids:
        rows = (db.session.query(issue_labels.c.issue_id).join(Label, Label.id == issue_labels.c.label_id)
                .filter(db.func.lower(Label.name).in_(MILESTONE_LABELS)).all())
        for (iid,) in rows:
            r = snap.issues.get(iid)
            if r is not None and iid in ids and r.target_date and ok(r.project_id):
                add(r.target_date, r.title, "issue", r.is_closed,
                    url_for("issues.detail", pid=r.project_id, seq=r.seq), r.key)
    add(ini.target_date, "Échéance de l’initiative", "initiative", ini.status == "completed", url)
    out.sort(key=lambda m: (m["date"], m["kind"] != "initiative"))
    return out


# ---------------------------------------------------------------------------
# Graphe (Workgraph)
# ---------------------------------------------------------------------------

TABS = {"initiative": "Initiative", "project": "Projet", "team": "Équipe"}


def _person(u: User | None) -> dict | None:
    if u is None:
        return None
    return {"id": u.id, "name": u.display_name, "initials": u.initials, "color": u.avatar_color}


def _fmt(d: date | None) -> str:
    from .utils import fmt_date
    return fmt_date(d) if d else ""


def build_graph(user, tab: str = "initiative", group: str = "lead", filters: dict | None = None,
                today: date | None = None, snap: Snapshot | None = None) -> dict:
    """Nœuds, liens hiérarchiques et dépendances du Workgraph pour un compte.

    Colonnes : Espace → Initiatives (ou Responsables / Membres pour l'onglet Équipe) → Projets → Modules.
    Retourne {"tab", "columns", "nodes", "edges", "deps", "counts"} (sérialisable en JSON)."""
    from .utils import visible_projects
    filters = filters or {}
    tab = tab if tab in TABS else "initiative"
    snap = snap or Snapshot(today)
    today = snap.today
    ws = Workspace.get()
    projects = visible_projects(user)
    if filters.get("project_ids"):
        projects = [p for p in projects if p.id in filters["project_ids"]]
    hide_closed = bool(filters.get("hide_closed"))
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    edge_keys: set[tuple[str, str]] = set()

    def link(s, t, kind="tree"):
        if (s, t) not in edge_keys and s in nodes and t in nodes:
            edge_keys.add((s, t))
            edges.append({"s": s, "t": t, "kind": kind})
            nodes[t]["parents"].append(s)

    nodes["ws"] = {"id": "ws", "kind": "workspace", "col": 0, "title": ws.name, "emoji": ws.emoji,
                   "url": url_for("projects.index"), "parents": [], "badge": "Espace de travail"}

    # -- colonne 1 ------------------------------------------------------------
    inis: list[Initiative] = []
    if tab == "initiative":
        all_inis = Initiative.query.all()
        linked_pids = {p.id for i in all_inis for p in i.projects}
        inis = [i for i in all_inis if _keep_initiative(i, snap, filters, user)]
        inis.sort(key=lambda i: (GRAPH_STATUS_ORDER.get(i.status, 9), i.name.lower()))
        for ini in inis:
            s = snap.initiative(ini)
            nodes[f"i:{ini.id}"] = {
                "id": f"i:{ini.id}", "kind": "initiative", "col": 1, "title": ini.name, "emoji": ini.emoji or "💡",
                "url": url_for("workgraph.initiative_detail", iid=ini.id), "parents": [], "badge": "Initiative",
                "pct": s["pct"], "stats": _small(s["stats"]), "health": s["health_info"], "forced": s["forced"],
                "reasons": s["reasons"], "status": {"key": ini.status, "label": ini.status_label,
                                                    "color": ini.status_color},
                "lead": _person(ini.lead), "target": s["target"].isoformat() if s["target"] else None,
                "target_label": _fmt(s["target"]), "late": s["late"], "closed": ini.is_closed,
                "link_url": url_for("workgraph.initiative_link", iid=ini.id),
            }
            link("ws", f"i:{ini.id}")
        orphans = [p for p in projects if p.id not in linked_pids]
        if orphans:
            nodes["i:none"] = {"id": "i:none", "kind": "group", "col": 1, "title": "Sans initiative",
                               "parents": [], "badge": "Groupe", "url": None,
                               "sub": f"{len(orphans)} projet{'s' if len(orphans) > 1 else ''}"}
            link("ws", "i:none")
        shown_pids = {p.id for i in inis for p in i.projects} | {p.id for p in orphans}
        projects = [p for p in projects if p.id in shown_pids]
    elif tab == "team":
        people: dict[int | None, User | None] = {}
        for p in projects:
            if group == "members":
                for u in p.member_users:
                    people[u.id] = u
            else:
                people[p.lead_id if p.lead else None] = p.lead
        for uid, u in sorted(people.items(), key=lambda kv: (kv[1] is None, kv[1].display_name.lower()
                                                             if kv[1] else "")):
            nid = f"u:{uid or 'none'}"
            owned = [p for p in projects if (p.lead_id == uid if group != "members"
                                               else any(m.id == uid for m in p.member_users))]
            nodes[nid] = {"id": nid, "kind": "person", "col": 1,
                          "title": u.display_name if u else "Sans responsable", "person": _person(u),
                          "parents": [], "badge": "Responsable" if group != "members" else "Membre", "url":
                          url_for("team.profile", username=u.username) if u else None,
                          "sub": f"{len(owned)} projet{'s' if len(owned) > 1 else ''}"}
            link("ws", nid)

    # -- projets ----------------------------------------------------------------
    pcol = 1 if tab == "project" else 2
    for p in projects:
        st = snap.project_stats(p.id)
        h, reasons = snap.project_health(p, st)
        nid = f"p:{p.id}"
        nodes[nid] = {
            "id": nid, "kind": "project", "col": pcol, "title": p.name, "emoji": p.emoji, "color": p.color,
            "identifier": p.identifier, "url": url_for("issues.index", pid=p.id), "parents": [],
            "badge": "Projet", "pct": st["pct"], "stats": _small(st), "health": health_info(h), "reasons": reasons,
            "lead": _person(p.lead), "target": p.target_date.isoformat() if p.target_date else None,
            "target_label": _fmt(p.target_date), "late": bool(p.target_date and p.target_date < today
                                                             and st["pct"] < 100),
            "closed": p.archived_at is not None,
        }
    for p in projects:
        nid = f"p:{p.id}"
        if tab == "initiative":
            parents = [f"i:{i.id}" for i in inis if any(x.id == p.id for x in i.projects)] or ["i:none"]
            for par in parents:
                link(par, nid)
        elif tab == "team":
            if group == "members":
                for u in p.member_users:
                    link(f"u:{u.id}", nid)
            else:
                link(f"u:{p.lead_id if p.lead else 'none'}", nid)
        else:
            link("ws", nid)

    # -- modules (épopées) ----------------------------------------------------------
    mcol = pcol + 1
    shown = {p.id for p in projects}
    for m in snap.modules.values():
        if m.project_id not in shown or m.archived_at is not None:
            continue
        if hide_closed and m.status in ("completed", "cancelled"):
            continue
        st = snap.module_stats(m.id)
        h, reasons = snap.module_health(m, st)
        p = snap.projects[m.project_id]
        nid = f"m:{m.id}"
        nodes[nid] = {
            "id": nid, "kind": "module", "col": mcol, "title": m.name, "identifier": p.identifier,
            "url": url_for("modules.detail", pid=m.project_id, mid=m.id), "parents": [], "badge": "Module",
            "pct": st["pct"], "stats": _small(st), "health": health_info(h), "reasons": reasons,
            "status": {"key": m.status, "label": MODULE_STATUS_LABELS.get(m.status, m.status),
                       "color": MODULE_STATUS_COLORS.get(m.status, "#a3a3a3")},
            "lead": _person(m.lead), "target": m.target_date.isoformat() if m.target_date else None,
            "target_label": _fmt(m.target_date),
            "late": bool(m.target_date and m.target_date < today and m.status not in ("completed", "cancelled")),
            "closed": m.status in ("completed", "cancelled"),
        }
        link(f"p:{m.project_id}", nid)
    if tab == "initiative":  # modules liés directement à une initiative
        for ini in inis:
            for m in ini.modules:
                link(f"i:{ini.id}", f"m:{m.id}", "direct")

    # -- dépendances --------------------------------------------------------------
    deps = _dependencies(snap, nodes, inis if tab == "initiative" else [], readable_project_ids(user))
    for d in deps:
        nodes[d["s"]].setdefault("blocks", 0)
        nodes[d["t"]].setdefault("blocked_by", 0)
        nodes[d["s"]]["blocks"] += d["open"]
        nodes[d["t"]]["blocked_by"] += d["open"]

    columns = [{"col": 0, "kind": "workspace", "title": "Espace"}]
    if tab == "initiative":
        columns.append({"col": 1, "kind": "initiative", "title": "Initiatives"})
    elif tab == "team":
        columns.append({"col": 1, "kind": "person", "title": "Membres" if group == "members" else "Responsables"})
    columns += [{"col": pcol, "kind": "project", "title": "Projets"},
                {"col": mcol, "kind": "module", "title": "Modules"}]
    for c in columns:
        c["count"] = sum(1 for n in nodes.values() if n["col"] == c["col"] and n["kind"] == c["kind"])
    return {"tab": tab, "group": group, "columns": columns, "nodes": list(nodes.values()), "edges": edges,
            "deps": deps, "today": today.isoformat()}


def _small(st: dict) -> dict:
    return {k: st[k] for k in ("total", "done", "open", "overdue")}


def _keep_initiative(ini: Initiative, snap: Snapshot, filters: dict, user) -> bool:
    if filters.get("status") and ini.status not in filters["status"]:
        return False
    if filters.get("hide_closed") and ini.is_closed:
        return False
    if filters.get("mine") and user is not None and ini.lead_id != user.id:
        return False
    if filters.get("health"):
        if snap.initiative(ini)["health"] not in filters["health"]:
            return False
    return True


def _dependencies(snap: Snapshot, nodes: dict, inis: list[Initiative],
                  readable: set[int] | None = None) -> list[dict]:
    """Dépendances entre nœuds affichés : « s bloque t » (au moins un ticket de s bloque un ticket de t).

    Seules comptent les relations entre tickets de projets que le lecteur peut lire (`readable`, None = tous)."""
    member: dict[int, list[str]] = defaultdict(list)  # ticket -> initiatives affichées qui le contiennent
    for ini in inis:
        for iid in snap.initiative(ini)["issue_ids"]:
            member[iid].append(f"i:{ini.id}")
    pairs: dict[tuple[str, str], dict] = {}

    def add(s, t, blocker, blocked):
        if s == t or s not in nodes or t not in nodes:
            return
        d = pairs.setdefault((s, t), {"s": s, "t": t, "n": 0, "open": 0, "items": []})
        is_open = not blocker.is_closed and not blocked.is_closed
        d["n"] += 1
        d["open"] += 1 if is_open else 0
        d["items"].append({"blocker": blocker.key, "blocker_title": blocker.title, "blocked": blocked.key,
                           "blocked_title": blocked.title, "open": is_open})

    for a, b in snap.blocked_by:
        blocked, blocker = snap.issues[a], snap.issues[b]
        if readable is not None and (blocked.project_id not in readable or blocker.project_id not in readable):
            continue
        if blocked.project_id != blocker.project_id:
            add(f"p:{blocker.project_id}", f"p:{blocked.project_id}", blocker, blocked)
        for mb in blocker.modules:
            for ma in blocked.modules:
                add(f"m:{mb}", f"m:{ma}", blocker, blocked)
        for ib in member.get(b, ()):
            for ia in member.get(a, ()):
                if ib not in member.get(a, ()) and ia not in member.get(b, ()):
                    add(ib, ia, blocker, blocked)
    out = []
    for d in pairs.values():
        d["items"].sort(key=lambda x: (not x["open"], x["blocked"]))
        d["more"] = max(0, len(d["items"]) - 8)
        d["items"] = d["items"][:8]
        d["kind"] = nodes[d["s"]]["kind"]
        out.append(d)
    out.sort(key=lambda d: (d["s"], d["t"]))
    return out


def table_rows(graph: dict) -> list[dict]:
    """Lignes hiérarchiques (vue Tableau) : parcours en profondeur depuis l'espace, un nœud peut apparaître
    sous plusieurs parents (projet lié à deux initiatives)."""
    nodes = {n["id"]: n for n in graph["nodes"]}
    children: dict[str, list[str]] = defaultdict(list)
    for e in graph["edges"]:
        children[e["s"]].append(e["t"])
    order = {n["id"]: i for i, n in enumerate(graph["nodes"])}
    rows: list[dict] = []

    def walk(nid, depth, path):
        kids = sorted(children.get(nid, ()), key=lambda k: (nodes[k]["col"], order[k]))
        if nid != "ws":
            rows.append({"node": nodes[nid], "depth": depth, "path": path, "parent": path.rsplit("/", 1)[0],
                         "has_children": bool(kids)})
        for k in kids:
            if f"/{k}/" not in f"{path}/":
                walk(k, depth + 1 if nid != "ws" else 0, f"{path}/{k}")

    walk("ws", 0, "ws")
    return rows


# ---------------------------------------------------------------------------
# Résumé texte (assistant IA)
# ---------------------------------------------------------------------------

def _line_initiative(ini: Initiative, s: dict) -> str:
    bits = [ini.status_label]
    if s["health_info"]:
        bits.append(s["health_info"]["label"] + (" (forcée)" if s["forced"] else ""))
    bits.append(f"{s['pct']} % ({s['stats']['done']}/{s['stats']['total'] - s['stats']['counts']['cancelled']} "
                f"tickets terminés)")
    txt = f"### {ini.emoji or '💡'} {ini.name} — " + " · ".join(bits)
    meta = []
    if ini.lead:
        meta.append(f"Responsable : {ini.lead.display_name}")
    if s["target"]:
        from .utils import fmt_date
        n = s["days_left"]
        rel = ("aujourd’hui" if n == 0 else "demain" if n == 1 else f"dans {n} jours" if n > 1
               else f"dépassée depuis {_days(-n)}")
        meta.append(f"Échéance : {fmt_date(s['target'], True)} ({rel})")
    if s["stats"]["overdue"]:
        meta.append(f"{s['stats']['overdue']} ticket(s) en retard")
    if meta:
        txt += "\n" + " · ".join(meta)
    if s["reasons"]:
        txt += "\nSanté : " + " ; ".join(s["reasons"])
    return txt


def overview_text(user, snap: Snapshot | None = None) -> str:
    from .utils import visible_projects
    snap = snap or Snapshot()
    inis = sorted(Initiative.query.all(), key=lambda i: (GRAPH_STATUS_ORDER.get(i.status, 9), i.name.lower()))
    visible = {p.id for p in visible_projects(user)}
    readable = readable_project_ids(user)
    counts = defaultdict(int)
    parts = []
    for ini in inis:
        s = snap.initiative_for(ini, readable)
        if s["health"]:
            counts[s["health"]] += 1
        txt = _line_initiative(ini, s)
        projs = [p for p in ini.projects if p.id in visible]
        if projs:
            txt += "\nProjets : " + ", ".join(f"{p.identifier} {p.name} ({snap.project_stats(p.id)['pct']} %)"
                                               for p in projs)
        mods = [m for m in ini.modules if m.project_id in readable]
        if mods:
            txt += "\nModules : " + ", ".join(f"{m.name} ({snap.module_stats(m.id)['pct']} %)" for m in mods)
        if s["blockers"]:
            ext = [b for b in s["blockers"] if b["external"]]
            txt += f"\nDépendances bloquantes ouvertes : {len(s['blockers'])}"
            if ext:
                txt += " dont externes : " + "; ".join(
                    f"{b['blocker'].key} « {b['blocker'].title} » bloque {b['blocked'].key}" for b in ext[:5])
        parts.append(txt)
    linked = {p.id for i in inis for p in i.projects}
    orphans = [snap.projects[pid] for pid in sorted(visible) if pid not in linked and pid in snap.projects]
    head = (f"Workgraph de l’espace « {Workspace.get().name} » au {date.today():%d/%m/%Y} : {len(inis)} initiative(s)"
            + (" — " + ", ".join(f"{n} {HEALTH_LABELS[k].lower()}" for k, n in counts.items()) if counts else "")
            + ".")
    if orphans:
        parts.append("Projets sans initiative : " + ", ".join(
            f"{p.identifier} {p.name} ({snap.project_stats(p.id)['pct']} %)" for p in orphans))
    graph = build_graph(user, "initiative", snap=snap)
    open_deps = [d for d in graph["deps"] if d["open"] and d["kind"] in ("project", "module")]
    if open_deps:
        titles = {n["id"]: n["title"] for n in graph["nodes"]}
        parts.append("Dépendances ouvertes entre projets / modules : " + "; ".join(
            f"{titles[d['s']]} bloque {titles[d['t']]} ({d['open']} ticket(s))" for d in open_deps[:12]))
    return head + "\n\n" + "\n\n".join(parts) if parts else head + "\nAucune initiative ni projet."


def initiative_text(ini: Initiative, user, snap: Snapshot | None = None) -> str:
    """Texte transmis à l'assistant IA : seulement ce que `user` peut lire (rien des projets privés des autres)."""
    from .utils import excerpt, fmt_date
    snap = snap or Snapshot()
    readable = readable_project_ids(user)
    s = snap.initiative_for(ini, readable)
    txt = _line_initiative(ini, s)
    if ini.description:
        txt += "\n\nDescription :\n" + excerpt(ini.description, 1500)
    projs = [p for p in ini.projects if p.id in readable]
    hidden = len(ini.projects) - len(projs)
    if projs or hidden:
        txt += "\n\nProjets liés :"
        for p in projs:
            st = snap.project_stats(p.id)
            txt += (f"\n- {p.identifier} {p.name} : {st['pct']} %, {st['open']} ouvert(s), {st['overdue']} en retard"
                    + (f", fin {fmt_date(p.target_date, True)}" if p.target_date else ""))
        if hidden:
            txt += f"\n- et {hidden} projet(s) privé(s), non détaillé(s)"
    mods = [m for m in ini.modules if m.project_id in readable]
    if mods:
        txt += "\n\nModules liés :"
        for m in mods:
            st = snap.module_stats(m.id)
            txt += f"\n- {m.name} ({m.status_label}) : {st['pct']} %, {st['open']} ouvert(s)"
    jal = [j for j in milestones(ini, snap, s["issue_ids"], readable) if not j["done"]][:10]
    if jal:
        txt += "\n\nJalons à venir ou en retard :" + "".join(
            f"\n- {fmt_date(j['date'], True)} : {j['title']}{' (en retard)' if j['late'] else ''}" for j in jal)
    if s["blockers"]:
        txt += "\n\nDépendances bloquantes ouvertes :" + "".join(
            f"\n- {b['blocked'].key} « {b['blocked'].title} » bloqué par {b['blocker'].key} "
            f"« {b['blocker'].title} »{' (hors initiative)' if b['external'] else ''}" for b in s["blockers"][:15])
    if ini.updates:
        txt += "\n\nDernières mises à jour :" + "".join(
            f"\n- {u.created_at:%d/%m/%Y} ({u.health_label}, {u.progress if u.progress is not None else '?'} %) "
            f"{u.author.display_name if u.author else ''} : {excerpt(u.body, 300)}" for u in ini.updates[:3])
    return txt


def ai_context(path: str, args, user) -> dict | None:
    """Résolveur de contexte du panneau IA pour /workgraph et /initiatives/…"""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    path = path or ""
    if path.startswith("/initiatives/"):
        seg = path.split("/")[2] if len(path.split("/")) > 2 else ""
        if not seg.isdigit():
            return None
        ini = db.session.get(Initiative, int(seg))
        if ini is None:
            return None
        return {"type": "initiative", "title": f"Initiative « {ini.name} »", "text": initiative_text(ini, user),
                "url": url_for("workgraph.initiative_detail", iid=ini.id)}
    if path.rstrip("/") in ("/workgraph", "/initiatives"):
        return {"type": "workgraph", "title": "Initiatives et Workgraph", "text": overview_text(user),
                "url": url_for("workgraph.index") if path.startswith("/workgraph")
                else url_for("workgraph.initiatives")}
    return None


from . import ai as _ai  # noqa: E402 - contexte du panneau IA (même paquet : toujours présent)

_ai.register_context(ai_context)


# ---------------------------------------------------------------------------
# Résumé quotidien : initiatives de l'utilisateur passées « à risque » ou « en retard »
# ---------------------------------------------------------------------------

@digest.section("travail.initiatives", ordre=70)
def _digest_initiatives(ctx):
    mine = Initiative.query.filter(db.or_(Initiative.lead_id == ctx.user.id,
                                          db.and_(Initiative.lead_id.is_(None),
                                                  Initiative.created_by_id == ctx.user.id))).all()
    mine = [i for i in mine if not i.is_closed]
    if not mine:
        return None
    snap = Snapshot(ctx.aujourdhui)
    changed = False
    items = []
    for ini in mine:
        s = snap.initiative(ini)
        changed |= refresh_health(ini, s["health"], ctx.at)
        recent = ini.health_since and ini.health_since >= ctx.at - timedelta(hours=26)
        if s["health"] in ("at_risk", "off_track") and recent:
            items.append((ini, s))
    if changed:
        db.session.commit()
    if not items:
        return None
    items.sort(key=lambda x: (-HEALTH_RANK[x[1]["health"]], x[0].name.lower()))
    lignes = []
    for ini, s in items:
        h = s["health_info"]
        lien = ctx.lien(f"/initiatives/{ini.id}")  # pas d'url_for : l'envoi tourne hors requête
        lignes.append(
            f'<a href="{escape(lien)}" style="color:#1d2127;font-weight:600;text-decoration:none">'
            f'{escape(ini.emoji or "💡")} {escape(ini.name)}</a> '
            f'<span style="color:{h["color"]};font-weight:600">· {escape(h["label"])}</span> '
            f'<span style="color:#6b7280">· {s["pct"]} %'
            + (f' · {escape("; ".join(s["reasons"][:2]))}' if s["reasons"] else "") + "</span>")
    n = len(items)
    return digest.Section("travail.initiatives", "Initiatives à surveiller", digest.liste_html(lignes), compte=n,
                          libelle=digest.pluriel(n, "initiative"), couleur="#d97706", a_faire=False, tuile=False)
