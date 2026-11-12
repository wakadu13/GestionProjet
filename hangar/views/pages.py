"""Pages : documentation du projet (wiki markdown) avec modèles, versions et import Word."""

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import heure
from ..extensions import db
from ..models import Page, PageVersion, now
from ..services import text_version, to_int
from ..utils import can, contient, get_project, slugify, tri_fr

bp = Blueprint("pages", __name__, url_prefix="/projects/<int:pid>/pages")

TEMPLATES = {
    "blank": ("Page vierge", "📄", ""),
    "meeting": ("Compte rendu de réunion", "🗓️", """## Informations
- **Date :** {today}
- **Participants :** @
- **Animateur :**
- **Secrétaire :**

## Ordre du jour
1.
2.

## Points abordés


## Décisions
-

## Actions
- [ ] Action — responsable — échéance
- [ ]

## Prochaine réunion
"""),
    "interview": ("Compte rendu d’entretien", "🎙️", """## Contexte
- **Interlocuteur :**
- **Service / fonction :**
- **Date :** {today}
- **Enquêteurs :** @
- **Type d’entretien :** ouvert / semi-directif

## Objectif de l’entretien


## Questions et réponses
### 1.
**Réponse :**

### 2.
**Réponse :**

## Irritants relevés
-

## Données / documents évoqués
| Document | Format | Destinataire | Remarque |
|---|---|---|---|
|  |  |  |  |

## Points à vérifier
- [ ]

## Suites à donner
- [ ]
"""),
    "decision": ("Note de décision", "⚖️", """## Contexte
Pourquoi une décision est nécessaire.

## Options envisagées
1. **Option A** — avantages / inconvénients
2. **Option B** — avantages / inconvénients

## Décision
Option retenue et justification.

## Conséquences
-

## Statut
Proposée · Validée · Remplacée — {today}
"""),
    "retro": ("Rétrospective de cycle", "🔁", """## Ce qui a bien fonctionné
-

## Ce qui peut être amélioré
-

## Ce que nous avons appris
-

## Actions pour le prochain cycle
- [ ] action — responsable
"""),
    "spec": ("Spécification fonctionnelle", "🧩", """## Objectif


## Utilisateurs concernés
-

## Parcours / cas d’usage
1.

## Exigences
| ID | Exigence | Priorité |
|---|---|---|
| EX-01 |  | Haute |

## Critères d’acceptation
- [ ]

## Questions ouvertes
-
"""),
}


def _visible(page: Page) -> bool:
    return page.is_public or page.owner_id == current_user.id


def _can_edit(page: Page) -> bool:
    if not can(current_user, "edit", page.project):
        return False
    if page.is_locked and page.owner_id != current_user.id and not can(current_user, "admin", page.project):
        return False
    return page.is_public or page.owner_id == current_user.id


def _page(pid, page_id):
    project = get_project(pid)
    if not project.pages_enabled:
        abort(404)
    page = db.session.get(Page, page_id)
    if page is None or page.project_id != project.id or not _visible(page):
        abort(404)
    return project, page


@bp.route("/")
@login_required
def index(pid):
    project = get_project(pid)
    if not project.pages_enabled:
        flash("Les pages sont désactivées pour ce projet.", "info")
        return redirect(url_for("issues.index", pid=pid))
    tab = request.args.get("tab", "public")
    q = Page.query.filter_by(project_id=pid)
    if tab == "archived":
        q = q.filter(Page.archived_at.isnot(None))
    else:
        q = q.filter(Page.archived_at.is_(None))
        if tab == "private":
            q = q.filter(Page.is_public.is_(False), Page.owner_id == current_user.id)
        elif tab == "mine":
            q = q.filter(Page.owner_id == current_user.id)
        else:
            q = q.filter(Page.is_public.is_(True))
    text = (request.args.get("q") or "").strip()
    if text:
        q = q.filter(db.or_(contient(Page.title, text), contient(Page.content, text)))
    sort = request.args.get("sort", "updated")
    q = q.order_by(tri_fr(Page.title)) if sort == "title" else q.order_by(Page.updated_at.desc())
    pages = [p for p in q.all() if _visible(p)]
    return render_template("pages/index.html", project=project, pages=pages, tab=tab, q=text, sort=sort,
                           templates=TEMPLATES, can_edit=can(current_user, "edit", project))


@bp.route("/new", methods=["POST"])
@login_required
def create(pid):
    project = get_project(pid, "edit")
    tpl = request.form.get("template", "blank")
    tname, temoji, tcontent = TEMPLATES.get(tpl, TEMPLATES["blank"])
    title = (request.form.get("title") or "").strip() or (tname if tpl != "blank" else "Sans titre")
    page = Page(project=project, title=title[:255], emoji=request.form.get("emoji") or temoji,
                content=tcontent.replace("{today}", heure.today().strftime("%d/%m/%Y")),
                owner=current_user, updated_by=current_user,
                is_public=request.form.get("is_public", "1") == "1",
                parent_id=to_int(request.form.get("parent_id")))
    db.session.add(page)
    db.session.commit()
    return redirect(url_for("pages.edit", pid=pid, page_id=page.id))


@bp.route("/<int:page_id>")
@login_required
def view(pid, page_id):
    project, page = _page(pid, page_id)
    children = [c for c in page.children if c.archived_at is None and _visible(c)]
    return render_template("pages/view.html", project=project, page=page, children=children,
                           can_edit=_can_edit(page))


@bp.route("/<int:page_id>/edit", methods=["GET", "POST"])
@login_required
def edit(pid, page_id):
    project, page = _page(pid, page_id)
    if not _can_edit(page):
        abort(403)
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()[:255] or "Sans titre"
        content = request.form.get("content", "")
        changed = content != (page.content or "") or title != page.title
        base = request.form.get("version")
        if changed and base is not None and base != page_version(page):
            # Modifiée entre-temps (autre personne, autre onglet, assistant) : on n'écrit rien.
            message = _conflict_message(page)
            if request.headers.get("X-Requested-With") == "fetch":
                return {"ok": False, "conflict": True, "error": message}, 409
            return render_template("pages/edit.html", project=project, page=page, page_version=page_version(page),
                                   draft={"title": title, "content": content}, error=message), 409
        if changed:
            last = page.versions[0] if page.versions else None
            # Le texte remplacé est archivé s'il vient de quelqu'un d'autre ; sinon une version au plus toutes
            # les 5 minutes, sauf sauvegarde explicite
            explicit = request.form.get("explicit") == "1"
            if explicit or last is None or page.updated_by_id != current_user.id or \
                    (now() - last.created_at).total_seconds() > 300:
                db.session.add(PageVersion(page=page, title=page.title, content=page.content,
                                           author=page.updated_by or current_user))
            page.title, page.content = title, content
            page.updated_at, page.updated_by = now(), current_user
        page.emoji = request.form.get("emoji") or page.emoji
        db.session.commit()
        if request.headers.get("X-Requested-With") == "fetch":
            return {"ok": True, "saved_at": page.updated_at.strftime("%H:%M:%S"), "version": page_version(page)}
        flash("Page enregistrée.", "success")
        return redirect(url_for("pages.view", pid=pid, page_id=page.id))
    return render_template("pages/edit.html", project=project, page=page, page_version=page_version(page))


def page_version(page: Page) -> str:
    """Empreinte du titre et du contenu enregistrés : l'éditeur la renvoie à chaque sauvegarde."""
    return text_version(page.title, page.content)


def _conflict_message(page: Page) -> str:
    who = page.updated_by
    at = page.updated_at.strftime("%H:%M") if page.updated_at else ""
    if who is not None and who.id == current_user.id:
        what = (f"Cette page a été modifiée ailleurs à {at} (autre onglet, autre appareil ou assistant) "
                "pendant que tu écrivais.")
    else:
        name = who.display_name if who is not None else "quelqu’un d’autre"
        what = f"Cette page a été modifiée par {name} à {at} pendant que tu écrivais."
    return what + " Rien n’a été écrasé et ton texte est toujours là."


@bp.route("/<int:page_id>/versions")
@login_required
def versions(pid, page_id):
    project, page = _page(pid, page_id)
    vid = to_int(request.args.get("v"))
    current = db.session.get(PageVersion, vid) if vid else None
    if current and current.page_id != page.id:
        current = None
    diff = None
    if current:
        import difflib
        diff = list(difflib.unified_diff((current.content or "").splitlines(), (page.content or "").splitlines(),
                                         "version", "actuelle", lineterm="", n=2))
    return render_template("pages/versions.html", project=project, page=page, current=current, diff=diff,
                           can_edit=_can_edit(page))


@bp.route("/<int:page_id>/versions/<int:vid>/restore", methods=["POST"])
@login_required
def restore_version(pid, page_id, vid):
    project, page = _page(pid, page_id)
    if not _can_edit(page):
        abort(403)
    v = db.session.get(PageVersion, vid)
    if not v or v.page_id != page.id:
        abort(404)
    db.session.add(PageVersion(page=page, title=page.title, content=page.content,
                               author=page.updated_by or current_user))  # l'auteur du texte remplacé
    page.title, page.content = v.title or page.title, v.content
    page.updated_at, page.updated_by = now(), current_user
    db.session.commit()
    flash("Version restaurée.", "success")
    return redirect(url_for("pages.view", pid=pid, page_id=page.id))


@bp.route("/<int:page_id>/<action>", methods=["POST"])
@login_required
def action(pid, page_id, action):
    project, page = _page(pid, page_id)
    is_owner = page.owner_id == current_user.id or can(current_user, "admin", project)
    if action == "lock" and is_owner:
        page.is_locked = not page.is_locked
        flash("Page verrouillée." if page.is_locked else "Page déverrouillée.", "info")
    elif action == "visibility" and is_owner:
        page.is_public = not page.is_public
        flash("Page rendue publique." if page.is_public else "Page rendue privée.", "info")
    elif action == "archive" and is_owner:
        page.archived_at = None if page.archived_at else now()
        flash("Page archivée." if page.archived_at else "Page restaurée.", "info")
        db.session.commit()
        return redirect(url_for("pages.index", pid=pid))
    elif action == "duplicate" and can(current_user, "edit", project):
        copy = Page(project=project, title=f"{page.title} (copie)", emoji=page.emoji, content=page.content,
                    owner=current_user, updated_by=current_user, is_public=page.is_public, parent_id=page.parent_id)
        db.session.add(copy)
        db.session.commit()
        flash("Page dupliquée.", "success")
        return redirect(url_for("pages.view", pid=pid, page_id=copy.id))
    elif action == "delete" and is_owner:
        title = page.title
        for c in page.children:
            c.parent_id = page.parent_id
        db.session.delete(page)
        db.session.commit()
        flash(f"Page « {title} » supprimée.", "info")
        return redirect(url_for("pages.index", pid=pid))
    elif action == "move" and _can_edit(page):
        parent_id = to_int(request.form.get("parent_id"))
        if parent_id != page.id:
            page.parent_id = parent_id
            flash("Page déplacée.", "success")
    else:
        abort(403)
    db.session.commit()
    return redirect(url_for("pages.view", pid=pid, page_id=page.id))


@bp.route("/<int:page_id>/export.md")
@login_required
def export_md(pid, page_id):
    project, page = _page(pid, page_id)
    body = f"# {page.title}\n\n{page.content or ''}\n"
    return Response(body, mimetype="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={slugify(page.title)}.md"})


@bp.route("/import", methods=["POST"])
@login_required
def import_doc(pid):
    from ..importer import docx_to_markdown
    project = get_project(pid, "edit")
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choisis un fichier .docx, .md ou .txt.", "error")
        return redirect(url_for("pages.index", pid=pid))
    name = f.filename
    stem = name.rsplit(".", 1)[0]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    try:
        if ext == "docx":
            content = docx_to_markdown(f.stream.read(), name)
        elif ext in ("md", "txt", "markdown"):
            content = f.stream.read().decode("utf-8", errors="replace")
        else:
            flash("Format non pris en charge (docx, md, txt).", "error")
            return redirect(url_for("pages.index", pid=pid))
    except Exception as exc:
        flash(f"Lecture impossible : {exc}", "error")
        return redirect(url_for("pages.index", pid=pid))
    page = Page(project=project, title=stem[:255], emoji="📥", content=content, owner=current_user,
                updated_by=current_user, is_public=True)
    db.session.add(page)
    db.session.commit()
    flash(f"Document « {name} » importé.", "success")
    return redirect(url_for("pages.view", pid=pid, page_id=page.id))
