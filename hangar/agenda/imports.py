"""Agenda : import de l'emploi du temps (.ics), annulation d'un import, badges « modifié / ajouté ».

Règle des imports : **le dernier fichier importé fait foi.**
- une séance du fichier (repérée par son UID) est créée ou mise à jour (notes et rappels personnels conservés) ;
- une séance déjà connue mais absente du nouveau fichier est marquée « retirée » si elle tombe dans une
  semaine couverte par le fichier, à partir d'aujourd'hui (le passé n'est jamais retiré). Une semaine n'est
  couverte que si le fichier y apporte au moins une vraie séance : d'une seule journée et pas « prévisionnelle ».
  Un bloc de plusieurs jours (formation militaire…) ou une prévision isolée ajoute et met à jour, sans jamais
  décider d'un retrait ;
- garde-fou : si une semaine voisine perdait la majorité de ses séances alors que le fichier n'y apporte qu'une
  ou deux séances, rien n'y est retiré et l'import le signale dans ses avertissements ;
- chaque import garde l'état d'avant : le dernier import peut être annulé (les rappels reviennent aussi).
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta

from flask import current_app

from .. import agenda as A  # le paquet : voir hangar/agenda/__init__.py
from .. import ics
from ..extensions import db
from ..models import AgendaEvent, AgendaGrade, AgendaImport, AgendaTask, User


# ---------------------------------------------------------------------------
# Import de l'emploi du temps
# ---------------------------------------------------------------------------

def _norm(f: str, v):
    if f in A.BOOL_FIELDS:
        return bool(v)
    if isinstance(v, datetime):
        return v.isoformat(timespec="minutes")
    if isinstance(v, date):
        return v.isoformat()
    return v if v is not None else ""


def _snapshot(ev: AgendaEvent) -> dict:
    snap = {f: _norm(f, getattr(ev, f)) for f in A.COMPARED}
    for f in A.META_FIELDS:
        snap[f] = getattr(ev, f)
    return snap


def _restore(ev: AgendaEvent, snap: dict) -> None:
    for k, v in snap.items():
        if k in ("start", "end") and isinstance(v, str):
            v = datetime.fromisoformat(v)
        setattr(ev, k, v)


def _store_file(user: User, raw: bytes, filename: str, at: datetime) -> str:
    folder = os.path.join(current_app.config["DATA_DIR"], "agenda", str(user.id))
    os.makedirs(folder, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(filename or "planning.ics")).strip("._")[-80:]
    name = f"{at:%Y%m%d_%H%M%S}_{safe or 'planning.ics'}"
    with open(os.path.join(folder, name), "wb") as fh:
        fh.write(raw)
    return name


def stored_path(imp: AgendaImport) -> str | None:
    if not imp.stored_name:
        return None
    p = os.path.join(current_app.config["DATA_DIR"], "agenda", str(imp.user_id), os.path.basename(imp.stored_name))
    return p if os.path.exists(p) else None


def _is_real_session(e) -> bool:
    """Vraie séance pour décider de la couverture d'une semaine : d'une seule journée et pas « prévisionnelle »."""
    if e.tentative or e.all_day:
        return False
    end = e.end or e.start
    last = (end - timedelta(microseconds=1)).date() if end > e.start else e.start.date()
    return last == e.start.date()


def covered_weeks(events) -> dict[date, int]:
    """{lundi : nombre de vraies séances du fichier dans la semaine} (voir `_is_real_session`)."""
    weeks: dict[date, int] = {}
    for e in events:
        if _is_real_session(e):
            w = ics.week_start(e.start.date())
            weeks[w] = weeks.get(w, 0) + 1
    return weeks


def _revive_reminders(ev: AgendaEvent, since: datetime | None, at: datetime) -> None:
    """Séance de retour dans le planning : les rappels ignorés pendant qu'elle était retirée (depuis `since`)
    repartent, tant que la séance n'est pas finie."""
    if since is None or not ev.is_active or ev.end <= at:
        return
    for r in ev.reminders:
        if r.status == "skipped" and r.remind_at is not None and r.remind_at >= since - timedelta(minutes=5):
            r.status, r.sent_at = "pending", None


def is_invitation(cal) -> bool:
    """Message d'agenda (invitation, réponse, annulation : METHOD:REQUEST, REPLY, CANCEL…) plutôt qu'un
    calendrier publié (METHOD:PUBLISH, ou rien).

    METHOD seul ne suffit pas : certains logiciels de planning exportent tout l'emploi du temps en
    METHOD:REQUEST. Un tel fichier n'est traité comme une invitation que s'il en a l'allure : au plus
    GUARD_MAX_SESSIONS séances, ou aucune séance d'un type de cours de l'école (CM, TD_TP…)."""
    if not cal.method or cal.method == "PUBLISH":
        return False
    if len(cal.events) <= A.GUARD_MAX_SESSIONS:
        return True
    return not any(e.kind in ics.KINDS for e in cal.events)


def import_calendar(user: User, raw: bytes | str, filename: str = "planning.ics", mode: str = "weeks",
                    at: datetime | None = None) -> AgendaImport:
    """Importe un fichier .ics : le contenu du fichier fait foi pour les semaines qu'il couvre vraiment."""
    at = at or A.now()
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    cal = ics.parse(raw)
    if not cal.events:
        raise ics.ICSError("Aucune séance trouvée dans ce fichier.")
    mode = mode if mode in A.IMPORT_MODES else "weeks"
    if is_invitation(cal) and mode != "merge":
        # Invitation à une réunion (METHOD:REQUEST…) : elle ne décrit pas toute la semaine, elle ne doit rien
        # retirer (sinon les autres cours de la semaine disparaîtraient).
        mode = "merge"
        cal.warnings.append("Ce fichier est une invitation (réunion, rendez-vous…), pas un emploi du temps complet : "
                            "ses séances ont été ajoutées sans rien retirer du planning.")
    s = A.settings_of(user)  # créé (et validé) avant de toucher aux séances
    parsed = {e.uid: e for e in cal.events}
    weeks = covered_weeks(cal.events)
    if weeks:
        lo, hi = min(weeks), max(weeks) + timedelta(days=6)
    else:  # seulement des blocs de plusieurs jours ou des prévisions : période affichée, rien n'est retiré
        starts = sorted(e.start.date() for e in cal.events)
        lo, hi = ics.week_start(starts[0]), ics.week_start(starts[-1]) + timedelta(days=6)

    # Périodes déjà couvertes par les imports précédents : une séance qui y apparaît est « ajoutée »
    # (à signaler), sinon c'est une « nouvelle » semaine (rien de particulier à signaler).
    covered = [(i.range_start, i.range_end) for i in
               AgendaImport.query.filter_by(user_id=user.id, undone_at=None) if i.range_start and i.range_end]

    imp = AgendaImport(user_id=user.id, filename=(filename or "planning.ics")[:255], mode=mode, range_start=lo,
                       range_end=hi, total=len(parsed), warnings="\n".join(cal.warnings), created_at=at,
                       added=0, modified=0, removed=0, restored=0, unchanged=0)
    db.session.add(imp)
    db.session.flush()

    existing = {e.uid: e for e in AgendaEvent.query.filter_by(user_id=user.id)}
    floor = at.date()  # le passé n'est jamais retiré
    # Garde-fou : séances actives par semaine avant l'import, comptées comme celles qu'on pourrait
    # retirer (à partir d'aujourd'hui). Compter aussi les jours passés rendrait le garde-fou inopérant
    # en fin de semaine (4 séances retirées sur 10 « connues », alors que ce sont toutes celles qui restent).
    known_by_week: dict[date, int] = {}
    for e in existing.values():
        if e.status == "active" and e.start.date() >= floor:
            w = ics.week_start(e.start.date())
            known_by_week[w] = known_by_week.get(w, 0) + 1
    details, touched, revived = [], [], []
    for uid, pe in parsed.items():
        vals = pe.values()
        ev = existing.get(uid)
        if ev is None:
            d0 = pe.start.date()
            notable = any(a <= d0 <= b for a, b in covered)
            ev = AgendaEvent(user_id=user.id, uid=uid, created_import_id=imp.id, changed_import_id=imp.id,
                             change_kind="added" if notable else "new", created_at=at, updated_at=at,
                             status="active", **vals)
            db.session.add(ev)
            db.session.flush()
            _rattacher_note(user, ev)
            imp.added += 1
            details.append({"type": "added", "notable": notable, "event_id": ev.id, "after": _snapshot(ev)})
            continue
        before = _snapshot(ev)
        diffs = {f: [before[f], _norm(f, vals[f])] for f in A.COMPARED if before[f] != _norm(f, vals[f])}
        was_removed = ev.status != "active"
        if not diffs and not was_removed:
            ev.summary = vals["summary"]
            imp.unchanged += 1
            continue
        for k, v in vals.items():
            setattr(ev, k, v)
        kind = "restored" if was_removed else "modified"
        ev.status, ev.change_kind, ev.changed_import_id, ev.updated_at = "active", kind, imp.id, at
        ev.changes_json = json.dumps(diffs, ensure_ascii=False) if diffs else ""
        if kind == "restored":
            imp.restored += 1
            removal = db.session.get(AgendaImport, before.get("changed_import_id") or 0)
            revived.append((ev, removal.created_at if removal is not None else None))
        else:
            imp.modified += 1
        details.append({"type": kind, "event_id": ev.id, "fields": list(diffs), "before": before,
                        "after": _snapshot(ev)})
        touched.append(ev)

    imp.guarded_weeks = []  # semaines épargnées par le garde-fou (pour le message après l'import)
    if mode != "merge":
        to_remove: dict[date, list[AgendaEvent]] = {}
        for uid, ev in existing.items():
            if uid in parsed or ev.status != "active":
                continue
            d0 = ev.start.date()
            w = ics.week_start(d0)
            if d0 < floor or (mode == "weeks" and w not in weeks):
                continue
            to_remove.setdefault(w, []).append(ev)
        if mode == "weeks" and weeks:
            main = max(weeks.values())
            guard_lines = []
            for w, evs in sorted(to_remove.items()):
                n, known = weeks[w], known_by_week.get(w, 0)
                # Semaine voisine (pas la semaine principale du fichier) qui perdrait la majorité des
                # séances qui lui restent
                if n <= A.GUARD_MAX_SESSIONS and n < main and 2 * len(evs) > known:
                    imp.guarded_weeks.append(w)
                    guard_lines.append(
                        f"Semaine du {A.day_short(w)} : le fichier n’y apporte que {n} séance(s), alors que "
                        f"{len(evs)} des {known} séances encore prévues en auraient été retirées. Par prudence, "
                        "rien n’a été retiré cette semaine-là : importe le fichier de cette semaine pour la mettre à jour.")
                    del to_remove[w]
            if guard_lines:
                imp.warnings = "\n".join(([imp.warnings] if imp.warnings else []) + guard_lines)
        removing = {ev.id for evs in to_remove.values() for ev in evs}
        for uid, ev in existing.items():
            if ev.id not in removing:
                continue
            before = _snapshot(ev)
            ev.status, ev.change_kind, ev.changed_import_id, ev.updated_at = "removed", "removed", imp.id, at
            ev.changes_json = ""
            imp.removed += 1
            details.append({"type": "removed", "event_id": ev.id, "before": before})

    imp.details_json = json.dumps(details, ensure_ascii=False)
    try:
        imp.stored_name = _store_file(user, raw, filename, at)
    except OSError:
        imp.stored_name = ""
    for ev in touched:
        A.sync_event_reminders(ev, at)
    for ev, since in revived:
        _revive_reminders(ev, since, at)
    A.ensure_exam_reminders(user, at)
    db.session.flush()
    quota_off: list[str] = []
    blocs_retires: dict[int, list[dict]] = {}
    imp.exam_notes = "\n".join(A.sync_exam_objectives(user, at, A.exam_changes(details), quota_off)
                               + _replanifier_revisions(user, at, blocs_retires))
    if blocs_retires:  # blocs de révision supprimés avec leur contrôle : gardés pour l'annulation de l'import
        for d in details:
            if d["type"] != "added" and d["event_id"] in blocs_retires:
                d["blocs"] = blocs_retires[d["event_id"]]
        imp.details_json = json.dumps(details, ensure_ascii=False)
    imp.exam_quota_sets = "\n".join(quota_off)
    db.session.commit()
    if s.import_email and imp.changed:
        A.queue_import_email(user, imp, at)
        db.session.commit()
    return imp


def latest_import(user: User) -> AgendaImport | None:
    return (AgendaImport.query.filter_by(user_id=user.id, undone_at=None)
            .order_by(AgendaImport.id.desc()).first())


def undo_import(imp: AgendaImport, at: datetime | None = None) -> list[str]:
    """Rétablit l'état d'avant l'import (uniquement pour le dernier import), rappels compris : les rappels
    automatiques des contrôles sont recréés et les rappels ignorés pendant le retrait repartent. Les dates
    d'examen des QCM suivent aussi (retourne les phrases qui le disent)."""
    at = at or A.now()
    last = AgendaImport.query.filter_by(user_id=imp.user_id, undone_at=None).order_by(AgendaImport.id.desc()).first()
    if last is None or last.id != imp.id:
        raise ValueError("Seul le dernier import peut être annulé.")
    blocs_notes: list[str] = []
    rendre: list[tuple[AgendaEvent, list[dict]]] = []  # blocs de révision supprimés par l'import, à rendre
    for d in reversed(imp.details):
        ev = db.session.get(AgendaEvent, d.get("event_id"))
        if ev is None:
            continue
        if d["type"] == "added":
            # Blocs « Réviser … » générés pour cette séance (hangar/controles.py) : ils partent avec elle,
            # sinon ils resteraient dans l'agenda sans contrôle ; ceux déjà faits restent (historique).
            n = AgendaTask.query.filter(AgendaTask.event_id == ev.id, AgendaTask.revision_auto.is_(True),
                                        AgendaTask.done_at.is_(None)).delete(synchronize_session="fetch")
            if n:
                blocs_notes.append(f"{ev.title} : contrôle retiré du planning, {n} bloc(s) de révision supprimé(s).")
            AgendaTask.query.filter_by(event_id=ev.id).update({"event_id": None})
            AgendaGrade.query.filter_by(event_id=ev.id).update({"event_id": None})  # la note reste
            db.session.delete(ev)
        else:
            _restore(ev, d["before"])
            ev.updated_at = at
            A.sync_event_reminders(ev, at)
            if d["type"] == "removed":
                _revive_reminders(ev, imp.created_at, at)
            if d.get("blocs"):
                rendre.append((ev, d["blocs"]))
    imp.undone_at = at
    db.session.flush()
    user = db.session.get(User, imp.user_id)
    notes = []
    if user is not None:
        blocs_notes += _rendre_blocs(user, rendre, at)
        A.ensure_exam_reminders(user, at)
        db.session.flush()
        notes = A.sync_exam_objectives(user, at, A.exam_changes(imp.details, undo=True))
        notes += A._restore_auto_quota(user, (imp.exam_quota_sets or "").splitlines())
        notes += _replanifier_revisions(user, at)
    db.session.commit()
    return blocs_notes + notes


def _replanifier_revisions(user: User, at: datetime, journal: dict | None = None) -> list[str]:
    """Blocs de révision générés (hangar/controles.py) replacés après un import ou son annulation. `journal`
    reçoit les blocs supprimés avec leur contrôle (pour l'annulation)."""
    try:
        from .. import controles
        return controles.replanifier_apres_import(user, at, journal)
    except Exception:  # noqa: BLE001 - l'import passe quand même
        current_app.logger.exception("Blocs de révision après un import")
        return []


def _rendre_blocs(user: User, rendre: list, at: datetime) -> list[str]:
    """Annulation : les blocs de révision supprimés par l'import (contrôle retiré) reviennent avec le contrôle."""
    notes = []
    try:
        from .. import controles
        for ev, photos in rendre:
            n = controles.rendre_blocs(user, ev, photos, at)
            if n:
                notes.append(f"{ev.title} : contrôle rétabli, {n} bloc(s) de révision rétabli(s).")
    except Exception:  # noqa: BLE001 - l'annulation passe quand même
        current_app.logger.exception("Blocs de révision rendus à l'annulation d'un import")
    return notes


def _rattacher_note(user: User, ev: AgendaEvent) -> None:
    """Contrôle (re)créé par un import : une note restée sans séance (annulation de l'import qui l'avait ajouté)
    de la même matière et du même jour lui est rattachée, pour ne pas la redemander ni la compter deux fois."""
    if not A.is_exam(ev):
        return
    key = A.subject_key(ev.title)
    for g in AgendaGrade.query.filter(AgendaGrade.user_id == user.id, AgendaGrade.event_id.is_(None),
                                      AgendaGrade.day == ev.start.date()).order_by(AgendaGrade.id):
        if A.subject_key(g.subject) == key and not AgendaGrade.query.filter_by(event_id=ev.id).first():
            g.event_id = ev.id
            return


def recent_import_ids(user: User, at: datetime | None = None, days: int = 7) -> set[int]:
    at = at or A.now()
    rows = AgendaImport.query.filter(AgendaImport.user_id == user.id, AgendaImport.undone_at.is_(None),
                                     AgendaImport.created_at >= at - timedelta(days=days)).all()
    return {r.id for r in rows}


def change_badge(ev: AgendaEvent, recent: set[int]) -> tuple[str, str] | None:
    """(libellé, classe) du badge de changement récent, ou None."""
    if ev.changed_import_id not in recent:
        return None
    return {"added": ("Ajouté", "accent"), "modified": ("Modifié", "warning"), "removed": ("Retiré", "danger"),
            "restored": ("Rétabli", "accent")}.get(ev.change_kind)


def describe_change(field_name: str, before, after) -> str:
    def fmt(v):
        if field_name in A.BOOL_FIELDS:
            return "oui" if v else "non"
        if field_name in ("start", "end") and v:
            dt = datetime.fromisoformat(v)
            return f"{A.day_short(dt.date())} {A.hm(dt)}"
        return v or "—"
    return f"{A.FIELD_LABELS.get(field_name, field_name)} : {fmt(before)} → {fmt(after)}"


def change_lines(changes: dict) -> list[str]:
    out = []
    if "start" in changes and "end" in changes:
        b0, a0 = changes["start"]
        b1, a1 = changes["end"]
        bs, as_ = datetime.fromisoformat(b0), datetime.fromisoformat(a0)
        be, ae = datetime.fromisoformat(b1), datetime.fromisoformat(a1)
        out.append(f"Horaire : {A.day_short(bs.date())} {A.hm(bs)}–{A.hm(be)} → {A.day_short(as_.date())} {A.hm(as_)}–{A.hm(ae)}")
        changes = {k: v for k, v in changes.items() if k not in ("start", "end")}
    out += [describe_change(k, *v) for k, v in changes.items()]
    return out


