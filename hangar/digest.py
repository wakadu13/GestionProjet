"""Résumé quotidien : un seul e-mail avec tout ce qu'il y a à faire dans la journée.

Chaque module de l'application ajoute sa « section » avec le décorateur `section` :

    @digest.section("agenda.cours", ordre=10)
    def _cours(ctx: digest.Contexte) -> digest.Section | None: ...

La fonction reçoit le contexte (utilisateur, jour résumé, instant de l'envoi) et retourne une section,
ou None quand elle n'a rien à dire. Le résumé n'est pas envoyé si aucune section n'a de contenu « à faire ».
Sections enregistrées : agenda (cours, tâches, retards, contrôles, changements du planning),
tickets de projet (ici même), révisions QCM (module qcm).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

from markupsafe import escape

_SECTIONS: list[tuple[int, str, Callable]] = []


@dataclass
class Contexte:
    user: object
    jour: date          # jour résumé (aujourd'hui le matin, demain le soir)
    at: datetime        # instant de l'envoi
    cfg: object = None  # MailConfig (pour les liens absolus)

    @property
    def aujourdhui(self) -> date:
        return self.at.date()

    def lien(self, path: str) -> str:
        from .mailer import absolute
        return absolute(path, self.cfg)


@dataclass
class Section:
    cle: str
    titre: str
    html: str
    ordre: int = 50
    compte: int = 0               # nombre d'éléments (affiché dans les tuiles)
    libelle: str = ""             # « 5 séances », « 3 à réviser »… (objet de l'e-mail et tuiles)
    couleur: str = "#1d2127"
    a_faire: bool = True          # False : section d'information seule (ne déclenche pas l'envoi)
    tuile: bool = True            # afficher le compte dans les tuiles du haut
    extra: dict = field(default_factory=dict)


def section(cle: str, ordre: int = 50):
    """Enregistre une section du résumé (idempotent : une clé n'est enregistrée qu'une fois)."""
    def deco(fn):
        global _SECTIONS
        _SECTIONS = [s for s in _SECTIONS if s[1] != cle]
        _SECTIONS.append((ordre, cle, fn))
        _SECTIONS.sort(key=lambda s: (s[0], s[1]))
        return fn
    return deco


def sections_enregistrees() -> list[str]:
    return [cle for _, cle, _ in _SECTIONS]


def calculer(user, jour: date, at: datetime | None = None) -> list[Section]:
    from .models import MailConfig
    at = at or datetime.now().replace(microsecond=0)
    ctx = Contexte(user=user, jour=jour, at=at, cfg=MailConfig.get())
    out = []
    for ordre, cle, fn in list(_SECTIONS):
        try:
            s = fn(ctx)
        except Exception:  # une section en échec ne bloque pas le résumé
            from flask import current_app
            current_app.logger.exception("Section %s du résumé quotidien", cle)
            continue
        if s is not None:
            s.ordre = ordre
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Mise en forme de l'e-mail
# ---------------------------------------------------------------------------

def liste_html(items: list[str]) -> str:
    return '<ul style="padding-left:18px;margin:0">' + "".join(f'<li style="margin:4px 0">{i}</li>' for i in items) + "</ul>"


def _tuiles(sections: list[Section]) -> str:
    tuiles = [s for s in sections if s.tuile and s.libelle]
    if not tuiles:
        return ""
    cells = "".join(
        f'<td style="padding:8px 12px;border:1px solid #e4e7ec;border-radius:8px;text-align:center;vertical-align:top">'
        f'<div style="font-size:19px;font-weight:700;color:{s.couleur}">{s.compte}</div>'
        f'<div style="font-size:11.5px;color:#6b7280;white-space:nowrap">{escape(s.libelle.split(" ", 1)[1] if " " in s.libelle else s.libelle)}</div>'
        f'</td><td width="6"></td>' for s in tuiles[:5])
    return f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0 6px"><tr>{cells}</tr></table>'


def construire(user, jour: date, at: datetime | None = None) -> tuple[str, str, list[Section]] | None:
    """(objet, html, sections) du résumé, ou None s'il n'y a rien à faire ce jour-là."""
    from . import mailer
    from .utils import DAYS, DAYS_FULL, MONTHS, MONTHS_FULL
    at = at or datetime.now().replace(microsecond=0)
    sections = calculer(user, jour, at)
    if not any(s.a_faire for s in sections):
        return None
    cfg = mailer._cfg()
    if jour == at.date():
        quand = "aujourd’hui"
    elif jour == at.date() + timedelta(days=1):
        quand = "demain"
    else:
        quand = f"le {DAYS_FULL[jour.weekday()]} {jour.day} {MONTHS_FULL[jour.month - 1]}"
    corps = (f"<p>Bonjour {escape(user.short_name)},</p>"
             f"<p>Voici ce qui t’attend {quand}, <b>{DAYS_FULL[jour.weekday()]} {jour.day} "
             f"{MONTHS_FULL[jour.month - 1]}</b>.</p>" + _tuiles(sections))
    for s in sections:
        corps += (f'<h2 style="font-size:14.5px;margin:20px 0 8px;padding-top:12px;border-top:1px solid #eef0f3;'
                  f'color:{s.couleur}">{escape(s.titre)}</h2>{s.html}')
    html = mailer.layout(f"Ta journée du {DAYS_FULL[jour.weekday()]} {jour.day} {MONTHS_FULL[jour.month - 1]}", corps,
                         button=("Ouvrir mon tableau de bord", mailer.absolute("/", cfg)), cfg=cfg,
                         footer=("Résumé quotidien envoyé par Hangar. Heure d’envoi et contenu : "
                                 f'<a href="{escape(mailer.absolute("/agenda/settings", cfg))}" style="color:#6b7280">'
                                 "Rappels et e-mails</a>."))
    bits = [s.libelle for s in sections if s.libelle and s.a_faire][:4]
    objet = f"📅 Ta journée du {DAYS[jour.weekday()]} {jour.day} {MONTHS[jour.month - 1]}"
    if bits:
        objet += " : " + ", ".join(bits)
    return objet, html, sections


def pluriel(n: int, mot: str, mots: str | None = None) -> str:
    return f"{n} {mot if n <= 1 else (mots or mot + 's')}"


# ---------------------------------------------------------------------------
# Section : tickets des projets (échéances)
# ---------------------------------------------------------------------------

def tickets_assignes(user) -> list | None:
    """Tickets (ouverts ou non) assignés à `user` dans les projets qu'il voit ; None s'il ne voit aucun projet."""
    from . import issue_query as IQ
    from .models import Issue, User
    from .utils import visible_projects
    pids = [p.id for p in visible_projects(user)]
    if not pids:
        return None
    return IQ.base_query().filter(Issue.project_id.in_(pids), Issue.assignees.any(User.id == user.id)).all()


def tickets_a_echeance(user, jour: date, aujourdhui: date | None = None, horizon: int = 3,
                       issues: list | None = None) -> dict | None:
    """Tickets ouverts assignés à `user`, avec une échéance, classés pour le jour `jour` :

    - « retard » : échéance passée (avant `aujourdhui`) ;
    - « aujourdhui » : à rendre aujourd'hui, quand le jour regardé est un autre jour (résumé du soir) ;
    - « du_jour » : à rendre le jour `jour` ;
    - « bientot » : à rendre dans les `horizon` jours qui suivent `jour`.

    Partagé par le résumé quotidien, le tableau de bord de l'agenda et l'accueil « Travail ».
    None si l'utilisateur ne voit aucun projet (`issues` : liste déjà chargée par `tickets_assignes`)."""
    if issues is None:
        issues = tickets_assignes(user)
        if issues is None:
            return None
    aujourdhui = aujourdhui or jour
    ouverts = [i for i in issues if not i.is_closed and i.target_date]
    par_date = (lambda i: (i.target_date, i.sequence_id))
    return {
        "retard": sorted([i for i in ouverts if i.target_date < aujourdhui], key=par_date),
        "aujourdhui": sorted([i for i in ouverts if i.target_date == aujourdhui != jour], key=par_date),
        "du_jour": sorted([i for i in ouverts if i.target_date == jour and jour >= aujourdhui], key=par_date),
        "bientot": sorted([i for i in ouverts if jour < i.target_date <= jour + timedelta(days=horizon)],
                          key=par_date),
    }


def nb_tickets_dus(user, aujourdhui: date) -> tuple[int, int]:
    """(à rendre au plus tard aujourd'hui, dont en retard) : tickets ouverts assignés à `user`, pour la pastille
    de l'icône « Travail ». Une seule requête légère (elle tourne sur chaque page)."""
    from .extensions import db
    from .models import CLOSED_GROUPS, Issue, State, User
    from .utils import visible_projects
    pids = [p.id for p in visible_projects(user)]
    if not pids:
        return 0, 0
    dates = (Issue.query.outerjoin(State, Issue.state_id == State.id)
             .filter(Issue.archived_at.is_(None), Issue.is_visible_clause(), Issue.project_id.in_(pids),
                     Issue.assignees.any(User.id == user.id), Issue.target_date.isnot(None),
                     Issue.target_date <= aujourdhui,
                     db.or_(State.group.is_(None), State.group.notin_(CLOSED_GROUPS)))
             .with_entities(Issue.target_date).all())
    return len(dates), sum(1 for (d,) in dates if d < aujourdhui)


@section("projets.tickets", ordre=60)
def _tickets(ctx: Contexte) -> Section | None:
    from .mailer import issue_card
    t = tickets_a_echeance(ctx.user, ctx.jour, ctx.aujourdhui)
    if t is None:
        return None
    retard, reste, du_jour, bientot = t["retard"], t["aujourdhui"], t["du_jour"], t["bientot"]
    if not (retard or reste or du_jour or bientot):
        return None
    html = ""
    for titre, lst, couleur in (("Reste à rendre aujourd’hui", reste, "#d97706"), ("En retard", retard, "#dc2626"),
                                ("À rendre ce jour", du_jour, "#d97706"),
                                ("Dans les 3 jours suivants", bientot, "#4a5260")):
        if lst:
            html += f'<div style="font-size:12.5px;font-weight:600;color:{couleur};margin:8px 0 2px">{titre} ({len(lst)})</div>'
            html += "".join(issue_card(i, ctx.cfg) for i in lst[:8])
    n = len(retard) + len(reste) + len(du_jour)
    return Section("projets.tickets", "Tickets de projet", html, compte=n or len(bientot),
                   libelle=pluriel(n or len(bientot), "ticket"), couleur="#3f76ff")
