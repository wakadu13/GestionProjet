"""Agenda : « Préparer ce contrôle », révisions planifiées, notes et moyennes (logique : hangar/controles.py).

Les routes s'ajoutent au blueprint de l'agenda (adresses /agenda/…, noms agenda.…)."""
from __future__ import annotations

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import agenda as A
from .. import controles as C
from ..extensions import db
from ..models import AgendaEvent, AgendaGrade, now
from ..utils import back_url, parse_date
from .agenda import _fetch, _own_event, _payloads, bp


def _own_grade(gid: int) -> AgendaGrade:
    g = db.session.get(AgendaGrade, gid)
    if g is None or g.user_id != current_user.id:
        abort(404)
    return g


def tableau_de_bord(d: dict) -> dict:
    """Statut des contrôles à venir et contrôles passés sans note, pour le tableau de bord de l'agenda."""
    out = {"ctl_statuts": {}, "a_noter": [], "C_classe": C.classe, "C_libelle": C.libelle}
    try:
        out["ctl_statuts"] = C.indicateurs(current_user, d["exams"][:6], d["at"])
        out["a_noter"] = C.a_noter(current_user, d["at"])
    except Exception:  # noqa: BLE001 - le tableau de bord ne doit jamais tomber à cause d'un module
        from flask import current_app
        current_app.logger.exception("Contrôles du tableau de bord")
    return out


@bp.route("/agenda/events/<int:eid>/preparer")
@login_required
def event_prepare(eid):
    """Tout ce qu'il faut pour préparer un contrôle, au même endroit."""
    ev = _own_event(eid)
    at = now()
    p = C.preparer(current_user, ev, at)
    plan = C.proposer(current_user, ev, at) if not p["passe"] else None
    links = {}
    try:
        links = A.subject_links(current_user, ev)  # suggestions quand la matière n'est pas encore reliée
    except Exception:  # noqa: BLE001
        pass
    data, _ = _payloads([ev], p["taches"])
    return render_template("agenda/controle.html", p=p, ev=ev, plan=plan, links=links, data=data,
                           blocs=C._blocs_auto(current_user, ev, at), C=C)


@bp.route("/agenda/events/<int:eid>/planifier", methods=["POST"])
@login_required
def event_plan(eid):
    """Crée (ou retire) les blocs de révision proposés sur la page du contrôle."""
    ev = _own_event(eid)
    page = url_for("agenda.event_prepare", eid=ev.id)
    if request.form.get("action") == "retirer":
        n = C.retirer(current_user, ev)
        db.session.commit()
        flash(f"{n} bloc(s) de révision retiré(s) de ton agenda." if n else "Aucun bloc de révision à retirer.",
              "success" if n else "info")
        return redirect(back_url(page))
    r = C.appliquer(current_user, ev)
    db.session.commit()
    if r["crees"]:
        msg = (f"{r['crees']} bloc(s) de révision ajouté(s) à ton agenda ({A.duration_label(r['total'])} en tout)."
               + (f" {r['retires']} ancien(s) bloc(s) remplacé(s)." if r["retires"] else ""))
        if r["sans_creneau"]:
            msg += " Pas de créneau libre le " + ", ".join(A.day_short(d) for d in r["sans_creneau"]) + "."
        flash(msg, "success")
    else:
        flash("Rien à planifier : tu es prêt, ou il n’y a pas de créneau libre d’ici le contrôle.", "info")
    return redirect(back_url(page))


def _form_note():
    f = request.form
    value, scale, coef = C.lire_nombre(f.get("value")), C.lire_nombre(f.get("scale")) or 20, C.lire_nombre(f.get("coef"))
    return value, scale, coef if coef is not None else 1


@bp.route("/agenda/events/<int:eid>/resultat", methods=["POST"])
@login_required
def event_grade(eid):
    """Note d'un contrôle passé (fenêtre de la séance ou page du contrôle), ou « pas de note »."""
    ev = _own_event(eid)
    default = url_for("agenda.event_prepare", eid=ev.id)
    if request.form.get("skip"):
        g = C.note_de(current_user, ev) or C._nouvelle_note(current_user.id, ev, now())
        g.skipped = True
        db.session.commit()
        msg = "C’est noté : ce contrôle ne sera plus rappelé."
        if _fetch():
            return jsonify(ok=True, message=msg)
        flash(msg, "success")
        return redirect(back_url(default))
    value, scale, coef = _form_note()
    err = C.verifier_note(value, scale, coef)
    if err:
        if _fetch():
            return jsonify(ok=False, error=err), 400
        flash(err, "error")
        return redirect(back_url(default))
    C.noter(current_user, ev, value, scale, coef, label=request.form.get("label"))
    db.session.commit()
    msg = f"Note enregistrée : {value:g}/{scale:g}."
    if _fetch():
        return jsonify(ok=True, message=msg)
    flash(msg, "success")
    return redirect(back_url(default))


@bp.route("/agenda/notes")
@login_required
def grades():
    """Notes des contrôles, moyennes par matière et générale, maîtrise des QCM le jour du contrôle."""
    m = C.moyennes(current_user)
    a_noter = C.a_noter(current_user)
    data, _ = _payloads(a_noter, [])
    sujets = sorted({g.subject for g in AgendaGrade.query.filter_by(user_id=current_user.id)}
                    | {e.title for e in AgendaEvent.query.filter(AgendaEvent.user_id == current_user.id,
                                                                AgendaEvent.status == "active")
                       if A.is_exam(e)}, key=str.lower)
    return render_template("agenda/notes.html", m=m, graphes=C.graphiques(m), a_noter=a_noter, sujets=sujets,
                           data=data, recent=set(), today=now().date(), C=C)


@bp.route("/agenda/notes", methods=["POST"])
@login_required
def grade_create():
    """Note saisie à la main, sans séance (épreuve absente de l'emploi du temps : sport, militaire…)."""
    subject = (request.form.get("subject") or "").strip()
    if not subject:
        flash("Indique la matière.", "error")
        return redirect(url_for("agenda.grades"))
    value, scale, coef = _form_note()
    err = C.verifier_note(value, scale, coef)
    if err:
        flash(err, "error")
        return redirect(url_for("agenda.grades"))
    C.noter(current_user, None, value, scale, coef, subject=subject,
            day=parse_date(request.form.get("day")) or now().date(), label=request.form.get("label") or "")
    db.session.commit()
    flash(f"Note ajoutée en {subject} : {value:g}/{scale:g}.", "success")
    return redirect(url_for("agenda.grades"))


@bp.route("/agenda/notes/<int:gid>", methods=["POST"])
@login_required
def grade_update(gid):
    g = _own_grade(gid)
    value, scale, coef = _form_note()
    err = C.verifier_note(value, scale, coef)
    if err:
        flash(err, "error")
        return redirect(back_url(url_for("agenda.grades")))
    C.noter(current_user, None, value, scale, coef, subject=request.form.get("subject") or "",
            day=parse_date(request.form.get("day")), label=request.form.get("label"), grade=g)
    db.session.commit()
    flash("Note modifiée.", "success")
    return redirect(back_url(url_for("agenda.grades")))


@bp.route("/agenda/notes/<int:gid>/delete", methods=["POST"])
@login_required
def grade_delete(gid):
    g = _own_grade(gid)
    if g.event_id is not None and g.mastery_at is not None:
        # Garder la maîtrise figée le jour du contrôle : seule la note est effacée.
        g.value, g.skipped, g.updated_at = None, False, now()
    else:
        db.session.delete(g)
    db.session.commit()
    flash("Note supprimée.", "success")
    return redirect(back_url(url_for("agenda.grades")))
