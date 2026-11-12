"""Section Travail : import / export des tickets (aller-retour sans perte), éditions concurrentes des pages
de projet et des descriptions de ticket (refus clair au lieu d'écraser, ancien texte gardé dans l'historique).

Lancer : uv run python -m unittest tests.test_travail_edition -v
"""
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from datetime import date
from types import SimpleNamespace

from flask.testing import FlaskClient

from hangar import create_app
from hangar import issue_query as IQ
from hangar.extensions import db
from hangar.models import Activity, APIToken, Issue, IssueRelation, Module, Page, PageVersion, Project, User
from hangar.services import add_relation, create_issue, create_project, update_issue

PASSWORD = "hangar2026"


class Client(FlaskClient):
    """Le contexte d'application est partagé avec le test : sans ce nettoyage, le compte connecté par une requête
    (g._login_user) resterait celui de la requête précédente, même venant d'un autre client."""

    def open(self, *args, **kwargs):
        from flask import g
        for k in ("_login_user", "sidebar", "picker"):
            g.pop(k, None)
        return super().open(*args, **kwargs)


class TravailCase(unittest.TestCase):
    """Base de démonstration créée une fois par classe ; chaque test travaille dans ses propres projets."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="hangar-travail-")
        cls.app = create_app(cls.tmp, testing=True)
        cls.app.test_client_class = Client
        cls.ctx = cls.app.test_request_context("/")
        cls.ctx.push()
        from hangar.seed import seed_demo
        seed_demo(None, verbose=False)
        for u in User.query.all():  # comptes de démo utilisables tout de suite
            u.must_change_password = False
        db.session.commit()
        cls.me = User.query.filter_by(username="wacogne").one()
        cls.other = User.query.filter_by(username="lazaro").one()
        cls.eae = Project.query.filter_by(identifier="EAE").one()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.engine.dispose()
        cls.ctx.pop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def client(self, username="wacogne"):
        c = self.app.test_client()
        page = c.get("/login").get_data(as_text=True)
        token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        r = c.post("/login", data={"login": username, "password": PASSWORD, "_csrf": token})
        self.assertEqual(r.status_code, 302)
        return c, token

    @staticmethod
    def flashes(c):
        with c.session_transaction() as s:
            return [m for _, m in s.pop("_flashes", [])]

    def project(self, ident, name=None):
        p = create_project(name or f"Projet {ident}", ident, self.me, member_ids=[self.other.id])
        db.session.commit()
        return p


# ---------------------------------------------------------------------------
# Piste 4 : import / export des tickets
# ---------------------------------------------------------------------------

def exported(project):
    """Les tickets que l'export d'un projet contient, dans l'ordre du fichier."""
    return IQ.sort_issues(IQ.base_query().filter(Issue.project_id == project.id).all(), "key")


def snapshot(issues, refs=None):
    """État comparable de tickets. `refs` : clé -> position, pour comparer parents et dépendances entre projets."""
    refs = refs if refs is not None else {i.key: n for n, i in enumerate(issues)}
    out = []
    for i in issues:
        blockers = [r.related.key for r in IssueRelation.query.filter_by(issue_id=i.id, relation_type="blocked_by")]
        out.append({
            "title": i.title, "description": (i.description or "").replace("\r\n", "\n").strip(),
            "state": i.state.name if i.state else None, "priority": i.priority,
            "assignees": sorted(u.username for u in i.assignees), "labels": sorted(l.name for l in i.labels),
            "modules": sorted(m.name for m in i.modules), "cycle": i.cycle.name if i.cycle else None,
            "start": i.start_date, "target": i.target_date, "estimate": i.estimate,
            "parent": refs.get(i.parent.key, "autre:" + i.parent.key) if i.parent else None,
            "blocked_by": sorted(str(refs.get(k, "autre:" + k)) for k in blockers),
        })
    return out


def expected_copy(issues):
    """Ce qu'un import de l'export doit recréer : les liens vers des tickets hors export disparaissent, et les
    titres « Jalon … » / « Marge … » reçoivent l'étiquette du même nom (règle de l'import)."""
    refs = {i.key: n for n, i in enumerate(issues)}
    snap = snapshot(issues, refs)
    for s in snap:
        if isinstance(s["parent"], str):
            s["parent"] = None
        s["blocked_by"] = [b for b in s["blocked_by"] if not b.startswith("autre:")]
        for word in ("Jalon", "Marge"):
            if re.match(rf"^\s*{word.lower()}\b", s["title"], re.I) and word not in s["labels"]:
                s["labels"] = sorted(s["labels"] + [word])
    return snap


class ImportExportTests(TravailCase):
    FILES = {"csv": "export.csv", "xlsx": "export.xlsx", "json": "export.json"}

    def export(self, c, project, fmt):
        r = c.get(f"/projects/{project.id}/issues/export.{fmt}")
        self.assertEqual(r.status_code, 200)
        return r.data

    def do_import(self, c, tok, project, data, filename, update=False):
        form = {"_csrf": tok, "file": (io.BytesIO(data), filename)}
        if update:
            form["update_existing"] = "1"
        r = c.post(f"/projects/{project.id}/issues/import", data=form, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        return self.flashes(c)

    def copies(self, project, offset=0):
        return (Issue.query.filter(Issue.project_id == project.id, Issue.sequence_id > offset)
                .order_by(Issue.sequence_id).all())

    def test_export_contains_many_links(self):
        """Garde-fou : le projet de démo a bien des parents, des dépendances et des modules avec virgule."""
        src = exported(self.eae)
        self.assertGreater(sum(1 for i in src if i.parent), 10)
        self.assertGreater(sum(1 for s in snapshot(src) if s["blocked_by"]), 10)
        self.assertTrue(any("," in m.name for m in self.eae.modules))

    def test_round_trip_to_empty_project(self):
        c, tok = self.client()
        src = exported(self.eae)
        want = expected_copy(src)
        for n, fmt in enumerate(("csv", "xlsx", "json")):
            with self.subTest(fmt=fmt):
                target = self.project(f"VID{n}")
                msgs = self.do_import(c, tok, target, self.export(c, self.eae, fmt), self.FILES[fmt])
                self.assertIn(f"Import terminé : {len(src)} créé(s), 0 mis à jour", msgs[0])
                got = self.copies(target)
                self.assertEqual(len(got), len(src))
                got_snap = snapshot(got, {i.key: k for k, i in enumerate(got)})
                for k, (a, b) in enumerate(zip(want, got_snap)):
                    self.assertEqual(a, b, f"{fmt} : ticket {src[k].key}")
                self.assertEqual({m.name for m in target.modules} - {m.name for m in self.eae.modules}, set(),
                                 "aucun module fantôme")

    def test_round_trip_to_project_with_tickets(self):
        """Vers un projet qui a déjà des tickets, case « mettre à jour » cochée ou non : les tickets existants
        restent intacts et les parents / dépendances pointent vers les copies, pas vers les tickets d'origine."""
        c, tok = self.client()
        src = exported(self.eae)
        want = expected_copy(src)
        cases = [("csv", True), ("csv", False), ("xlsx", True), ("json", True), ("json", False)]
        for n, (fmt, update) in enumerate(cases):
            with self.subTest(fmt=fmt, update=update):
                target = self.project(f"MEM{n}")
                a = create_issue(target, {"title": "Choisir le sujet", "description": "Texte écrit à la main",
                                          "priority": "urgent"}, self.me)
                b = create_issue(target, {"title": "Lire la bibliographie", "priority": "high"}, self.me)
                d = create_issue(target, {"title": "Écrire le plan", "parent_id": a.id}, self.me)
                add_relation(d, b, "blocked_by", self.me)
                db.session.commit()
                before = snapshot([a, b, d])
                msgs = self.do_import(c, tok, target, self.export(c, self.eae, fmt), self.FILES[fmt], update)
                self.assertIn(f"Import terminé : {len(src)} créé(s), 0 mis à jour", msgs[0])
                if update:
                    self.assertTrue(any("ne sont pas celles de ce projet" in m for m in msgs), msgs)
                self.assertEqual(snapshot([a, b, d]), before, "les tickets d'origine ne bougent pas")
                got = self.copies(target, offset=3)
                got_snap = snapshot(got, {i.key: k for k, i in enumerate(got)})
                self.assertEqual(len(got), len(src))
                for k, (x, y) in enumerate(zip(want, got_snap)):
                    self.assertEqual(x, y, f"{fmt} : ticket {src[k].key}")

    def test_reimport_own_export_with_update_changes_nothing(self):
        """Exporter son projet puis réimporter le fichier tel quel, case cochée : aucun ticket ne bouge."""
        c, tok = self.client()
        target = self.eae
        issues = exported(target)
        before = snapshot(issues)
        modules = sorted(m.name for m in target.modules)
        n_act = Activity.query.filter(Activity.project_id == target.id).count()
        for fmt in ("xlsx", "csv", "json"):
            with self.subTest(fmt=fmt):
                msgs = self.do_import(c, tok, target, self.export(c, target, fmt), self.FILES[fmt], update=True)
                self.assertIn(f"Import terminé : 0 créé(s), {len(issues)} mis à jour", msgs[0])
                db.session.expire_all()
                self.assertEqual(snapshot(exported(target)), before)
                self.assertEqual(sorted(m.name for m in target.modules), modules)
                self.assertEqual(Activity.query.filter(Activity.project_id == target.id).count(), n_act,
                                 "rien n'a changé, donc rien dans l'historique")

    def test_reimport_planning_keeps_fields_absent_from_file(self):
        """Réimporter un planning (sans colonnes Priorité ni Description) ne touche qu'aux colonnes présentes."""
        c, tok = self.client()
        target = self.project("PLN")
        v1 = "ID;Tâche;Début;Échéance\n1;Cadrer le sujet;01/10/2026;10/10/2026\n2;Rédiger;11/10/2026;20/10/2026\n"
        self.do_import(c, tok, target, v1.encode(), "planning.csv")
        t1, t2 = self.copies(target)
        update_issue(t1, {"priority": "urgent", "description": "Contexte rédigé dans Hangar"}, self.me)
        db.session.commit()
        v2 = v1.replace("10/10/2026", "15/10/2026").replace("20/10/2026", "25/10/2026")
        msgs = self.do_import(c, tok, target, v2.encode(), "planning.csv", update=True)
        self.assertIn("0 créé(s), 2 mis à jour", msgs[0])
        db.session.expire_all()
        self.assertEqual(t1.priority, "urgent")
        self.assertEqual(t1.description, "Contexte rédigé dans Hangar")
        self.assertEqual((t1.target_date, t2.target_date), (date(2026, 10, 15), date(2026, 10, 25)))

    def test_external_file_names_and_labels(self):
        """Étiquettes créées par l'import gardées dès le 1er ticket ; noms entiers avant découpage."""
        c, tok = self.client()
        target = self.project("GNT")
        db.session.add(Module(project=target, name="Phase 1, préparation", status="planned"))
        from hangar.services import get_or_create_label
        get_or_create_label(target, "Santé")
        get_or_create_label(target, "Analyse")
        db.session.commit()
        csv_text = ("Tâche;Étiquettes;Module\n"
                    "Tâche A;Terrain;Phase 1, préparation\n"
                    "Tâche B;Terrain, R&D;Recueil terrain (entretiens, sondages)\n"
                    "Tâche C;Santé et sécurité;Cadrage, Restitution\n"
                    'Tâche D;Terrain / Analyse;"""Études, enquêtes"", Cadrage"\n')  # cellule : "Études, enquêtes", Cadrage
        self.do_import(c, tok, target, csv_text.encode(), "gantt.csv")
        a, b, d, e = self.copies(target)
        self.assertEqual([l.name for l in a.labels], ["Terrain"])
        self.assertEqual(sorted(l.name for l in b.labels), ["R&D", "Terrain"])
        self.assertEqual([l.name for l in d.labels], ["Santé et sécurité"])  # « sécurité » n'existe pas
        self.assertEqual(sorted(l.name for l in e.labels), ["Analyse", "Terrain"])  # deux étiquettes connues
        self.assertEqual([m.name for m in a.modules], ["Phase 1, préparation"])
        self.assertEqual([m.name for m in b.modules], ["Recueil terrain (entretiens, sondages)"])
        self.assertEqual(sorted(m.name for m in d.modules), ["Cadrage", "Restitution"])
        self.assertEqual(sorted(m.name for m in e.modules), ["Cadrage", "Études, enquêtes"])

    def test_names_survive_export_format(self):
        from hangar.importer import _join_names, _split_names
        names = ["RGPD, droits & conformité", "Recueil terrain (entretiens, sondages)", 'Lot « A »; "B"', "R&D"]
        self.assertEqual(_split_names(_join_names(names)), names)
        self.assertEqual(_split_names(["A, B", "C"]), ["A, B", "C"])  # liste JSON prise telle quelle
        self.assertEqual(_split_names("Phase (1, 2"), ["Phase (1", "2"])  # parenthèse non fermée

    def test_reimport_without_update_warns_about_duplicates(self):
        c, tok = self.client()
        target = self.project("DBL")
        planning = "ID;Tâche\n1;Cadrer\n2;Rédiger\n".encode()
        self.do_import(c, tok, target, planning, "p.csv")
        msgs = self.do_import(c, tok, target, planning, "p.csv")
        self.assertIn("2 créé(s), 0 mis à jour", msgs[0])
        self.assertTrue(any("2 ligne(s) correspondent à des tickets déjà présents" in m for m in msgs), msgs)

    def test_json_export_lists_blockers(self):
        c, _ = self.client()
        payload = json.loads(self.export(c, self.eae, "json"))
        rel = IssueRelation.query.filter_by(relation_type="blocked_by").join(
            Issue, IssueRelation.issue_id == Issue.id).filter(Issue.project_id == self.eae.id).first()
        item = next(it for it in payload["issues"] if it["key"] == rel.issue.key)
        self.assertIn(rel.related.key, item["blocked_by"])

    def test_update_option_off_by_default_and_backup_before_update(self):
        c, tok = self.client()
        target = self.project("SAV")
        page = c.get(f"/projects/{target.id}/settings/import").get_data(as_text=True)
        box = re.search(r'<input[^>]*name="update_existing"[^>]*>', page).group(0)
        self.assertNotIn("checked", box)
        folder = self.app.config["BACKUP_DIR"]

        def newest():  # date de la dernière sauvegarde (un nom à la seconde près peut être réécrit)
            files = [os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".db")]
            return max((os.stat(f).st_mtime_ns for f in files), default=0)
        t0 = newest()
        msgs = self.do_import(c, tok, target, "ID;Tâche;Statut\n1;A;À faire\n".encode(), "a.csv")
        self.assertEqual(newest(), t0, "pas de sauvegarde pour un simple ajout")
        self.assertFalse(any("sauvegarde" in m.lower() for m in msgs), msgs)
        msgs = self.do_import(c, tok, target, "ID;Tâche;Statut\n1;A;À faire\n".encode(), "a.csv", update=True)
        self.assertGreater(newest(), t0)
        self.assertTrue(any("sauvegarde" in m.lower() for m in msgs), msgs)
        self.assertIn("0 créé(s), 1 mis à jour", msgs[0])


# ---------------------------------------------------------------------------
# Piste 11 : pages de projet modifiées à plusieurs
# ---------------------------------------------------------------------------

class PageConcurrencyTests(TravailCase):
    def setUp(self):
        self.page = Page(project=self.eae, title="Compte rendu de réunion", content="## Notes\n", owner=self.me,
                         updated_by=self.me, is_public=True)
        db.session.add(self.page)
        db.session.commit()
        self.url = f"/projects/{self.eae.id}/pages/{self.page.id}/edit"

    def open_editor(self, c):
        html = c.get(self.url).get_data(as_text=True)
        m = re.search(r'name="version" value="([^"]*)"', html)
        return m.group(1) if m else None

    def autosave(self, c, tok, content, version, title="Compte rendu de réunion"):
        data = {"_csrf": tok, "title": title, "content": content, "explicit": "0"}
        if version is not None:
            data["version"] = version
        return c.post(self.url, data=data, headers={"X-Requested-With": "fetch"})

    def texts_in_history(self):
        db.session.expire_all()
        return [v.content or "" for v in PageVersion.query.filter_by(page_id=self.page.id)]

    def test_second_writer_is_refused_without_losing_anything(self):
        a, ta = self.client("wacogne")
        b, tb = self.client("lazaro")
        va, vb = self.open_editor(a), self.open_editor(b)
        self.assertTrue(va and va == vb)
        r = self.autosave(a, ta, "## Notes\nTexte du secrétaire\n", va)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["version"])
        r = self.autosave(b, tb, "## Notes\nTexte de l’animateur\n", vb)
        self.assertEqual(r.status_code, 409)
        j = r.get_json()
        self.assertFalse(j["ok"])
        self.assertIn("Antoine Wacogne", j["error"])
        db.session.expire_all()
        self.assertIn("Texte du secrétaire", self.page.content)

    def test_same_tab_saves_several_times(self):
        a, ta = self.client()
        v = self.open_editor(a)
        for k in range(3):
            r = self.autosave(a, ta, f"## Notes\nversion {k}\n", v)
            self.assertEqual(r.status_code, 200, k)
            v = r.get_json()["version"]

    def test_explicit_save_conflict_shows_both_texts(self):
        a, ta = self.client("wacogne")
        b, tb = self.client("lazaro")
        vb = self.open_editor(b)
        self.autosave(a, ta, "## Notes\nTexte du secrétaire\n", self.open_editor(a))
        r = b.post(self.url, data={"_csrf": tb, "title": "Compte rendu de réunion", "explicit": "1",
                                   "content": "## Notes\nTexte de l’animateur\n", "version": vb})
        self.assertEqual(r.status_code, 409)
        html = r.get_data(as_text=True)
        self.assertIn("Texte de l’animateur", html)   # son texte, toujours dans l'éditeur
        self.assertIn("Texte du secrétaire", html)    # le texte actuel, pour comparer
        self.assertIn("modifiée par Antoine Wacogne", html)
        db.session.expire_all()
        self.assertNotIn("animateur", self.page.content)
        # Il fusionne puis enregistre : accepté, et le texte remplacé reste dans l'historique
        new_version = re.search(r'name="version" value="([^"]*)"', html).group(1)
        r = b.post(self.url, data={"_csrf": tb, "title": "Compte rendu de réunion", "explicit": "1",
                                   "content": "## Notes\nTexte de l’animateur\n", "version": new_version})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(any("Texte du secrétaire" in t for t in self.texts_in_history()))

    def test_alternating_authors_keep_every_text_in_history(self):
        a, ta = self.client("wacogne")
        b, tb = self.client("lazaro")
        written = []
        for k, (c, tok, who) in enumerate([(a, ta, "A"), (b, tb, "B"), (a, ta, "A"), (b, tb, "B"), (a, ta, "A")]):
            text = f"## Notes\nligne {who}{k}\n"
            r = self.autosave(c, tok, text, self.open_editor(c))
            self.assertEqual(r.status_code, 200)
            written.append(text)
        history = self.texts_in_history()
        for text in written[:-1]:
            self.assertIn(text, history, "chaque texte remplacé par un autre auteur est archivé")

    def test_assistant_block_is_not_erased_by_open_editor(self):
        from hangar.ai.tools import _do_ajout_page
        a, ta = self.client()
        v = self.open_editor(a)
        action = SimpleNamespace(payload={"page_type": "projet", "page_id": self.page.id,
                                          "contenu": "BLOC AJOUTÉ PAR L’ASSISTANT", "position": "fin"})
        _do_ajout_page(action, self.me)
        r = self.autosave(a, ta, "## Notes\nje continue de taper\n", v)
        self.assertEqual(r.status_code, 409)
        self.assertIn("autre onglet", r.get_json()["error"])
        db.session.expire_all()
        self.assertIn("BLOC AJOUTÉ PAR L’ASSISTANT", self.page.content)

    def test_restore_archives_text_of_its_author(self):
        a, ta = self.client("wacogne")
        b, tb = self.client("lazaro")
        self.autosave(b, tb, "## Notes\nTexte de B\n", self.open_editor(b))
        old = PageVersion.query.filter_by(page_id=self.page.id).first()
        r = a.post(f"/projects/{self.eae.id}/pages/{self.page.id}/versions/{old.id}/restore", data={"_csrf": ta})
        self.assertEqual(r.status_code, 302)
        db.session.expire_all()
        v = next(v for v in PageVersion.query.filter_by(page_id=self.page.id) if "Texte de B" in (v.content or ""))
        self.assertEqual(v.author_id, self.other.id)


# ---------------------------------------------------------------------------
# Piste 16 : description d'un ticket
# ---------------------------------------------------------------------------

class IssueDescriptionTests(TravailCase):
    def setUp(self):
        self.issue = create_issue(self.eae, {"title": "Rédiger les critères", "description": "Texte d’origine"},
                                  self.me)
        db.session.commit()
        self.url = f"/projects/{self.eae.id}/issues/{self.issue.sequence_id}"

    def open_issue(self, c):
        html = c.get(self.url).get_data(as_text=True)
        m = re.search(r'name="description_version" value="([^"]*)"', html)
        return m.group(1) if m else None

    def save(self, c, tok, text, version):
        data = {"_csrf": tok, "description": text}
        if version is not None:
            data["description_version"] = version
        return c.post(self.url + "/edit", data=data)

    def description_activities(self):
        return (Activity.query.filter_by(issue_id=self.issue.id, field="description")
                .order_by(Activity.id).all())

    def test_old_text_is_kept_and_can_be_restored(self):
        c, tok = self.client()
        self.save(c, tok, "Deuxième version", self.open_issue(c))
        self.save(c, tok, "", self.open_issue(c))
        acts = self.description_activities()
        self.assertEqual([(a.old_value, a.new_value) for a in acts],
                         [("Texte d’origine", "Deuxième version"), ("Deuxième version", "")])
        html = c.get(self.url + "?tab=activity").get_data(as_text=True)
        self.assertIn("Deuxième version", html)
        r = c.post(f"{self.url}/description/{acts[1].id}/restore", data={"_csrf": tok})
        self.assertEqual(r.status_code, 302)
        db.session.expire_all()
        self.assertEqual(self.issue.description, "Deuxième version")
        self.assertEqual(self.description_activities()[-1].old_value, "")

    def test_concurrent_edit_is_refused_and_shows_both_texts(self):
        a, ta = self.client("wacogne")
        b, tb = self.client("lazaro")
        va, vb = self.open_issue(a), self.open_issue(b)
        self.assertTrue(va and va == vb)
        self.assertEqual(self.save(b, tb, "Texte d’origine\n\nCritères ajoutés par le coéquipier", vb).status_code,
                         302)
        r = self.save(a, ta, "Texte d’origine corrigé", va)
        self.assertEqual(r.status_code, 409)
        html = r.get_data(as_text=True)
        self.assertIn("Texte d’origine corrigé", html)
        self.assertIn("Critères ajoutés par le coéquipier", html)
        self.assertIn("Raphaël Lazaro", html)
        db.session.expire_all()
        self.assertIn("Critères ajoutés", self.issue.description)
        # Après avoir comparé, il enregistre avec la nouvelle base : accepté, l'ancien texte reste dans l'historique
        nv = re.search(r'name="description_version" value="([^"]*)"', html).group(1)
        self.assertEqual(self.save(a, ta, "Texte fusionné", nv).status_code, 302)
        db.session.expire_all()
        self.assertEqual(self.issue.description, "Texte fusionné")
        self.assertIn("Critères ajoutés", self.description_activities()[-1].old_value)

    def test_unchanged_text_with_old_base_does_not_overwrite(self):
        a, ta = self.client("wacogne")
        b, tb = self.client("lazaro")
        va = self.open_issue(a)
        self.save(b, tb, "Ajout du coéquipier", self.open_issue(b))
        self.assertEqual(self.save(a, ta, "Texte d’origine", va).status_code, 302)
        db.session.expire_all()
        self.assertEqual(self.issue.description, "Ajout du coéquipier")

    def test_title_inline_edit_is_not_blocked(self):
        a, ta = self.client()
        r = a.post(self.url + "/edit", data={"_csrf": ta, "title": "Nouveau titre"},
                   headers={"X-Requested-With": "fetch"})
        self.assertEqual(r.status_code, 200)
        db.session.expire_all()
        self.assertEqual(self.issue.title, "Nouveau titre")

    def test_internal_api_checks_base(self):
        a, ta = self.client()
        url = f"/api/issues/{self.issue.id}"
        h = {"X-CSRF-Token": ta, "X-Requested-With": "fetch"}
        base = a.get(url).get_json()["issue"]["description_version"]
        update_issue(self.issue, {"description": "Changé ailleurs"}, self.other)
        db.session.commit()
        r = a.patch(url, json={"description": "Mon texte", "description_version": base}, headers=h)
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()["conflict"])
        self.assertEqual(r.get_json()["issue"]["description"], "Changé ailleurs")
        r = a.patch(url, json={"description": "Mon texte"}, headers=h)  # sans base : comme avant
        self.assertEqual(r.status_code, 200)

    def test_token_api_checks_base(self):
        token, raw = APIToken.generate(self.me, "test")
        db.session.add(token)
        db.session.commit()
        c = self.app.test_client()
        h = {"X-API-Key": raw}  # « Authorization: Bearer » est pris par les jetons d’appareil
        url = f"/api/v1/issues/{self.issue.id}"
        base = c.get(url, headers=h).get_json()["issue"]["description_version"]
        update_issue(self.issue, {"description": "Changé ailleurs"}, self.other)
        db.session.commit()
        r = c.patch(url, json={"description": "Depuis le téléphone", "description_version": base}, headers=h)
        self.assertEqual(r.status_code, 409)
        db.session.expire_all()
        self.assertEqual(self.issue.description, "Changé ailleurs")


if __name__ == "__main__":
    unittest.main()
