"""Section Travail, vague 2 : exports Excel / Word / CSV robustes (caractères collés, textes commençant par « = »),
cloisonnement (favoris, vues, relations, notifications), fermeture automatique, droits de suppression et d'import,
numérotation des tickets sans collision.

Lancer : uv run python -m unittest tests.test_travail -v
"""
import io
import os
import re
import shutil
import tempfile
import threading
import unittest
from datetime import timedelta

from hangar import create_app
from hangar.extensions import db
from hangar.models import (
    Attachment, Cycle, EmailMessage, Favorite, Issue, IssueRelation, MailConfig, Module, Notification, Page,
    ProjectMember, SavedView, User, now,
)
from hangar.services import create_issue, create_project, update_issue

PASSWORD = "secret1"


class TravailCase(unittest.TestCase):
    """Un projet public « EAE » (tout le monde) et un projet privé « SEC » (l'administrateur et Paul)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-travail-v2-")
        self.app = create_app(self.tmp, testing=True)
        self.ctx = self.app.test_request_context("/")
        self.ctx.push()
        self.admin = self._user("chef", "admin", "Anne", "Chef")
        self.member = self._user("membre", "member", "Marc", "Membre")
        self.paul = self._user("paul", "member", "Paul", "Durand")
        self.carol = self._user("carol", "member", "Carol", "Petit")
        self.guest = self._user("invite", "guest", "Ines", "Invitée")
        self.pub = create_project("Commun", "EAE", self.admin,
                                  member_ids=[self.member.id, self.paul.id, self.guest.id])
        self.priv = create_project("Projet secret", "SEC", self.admin, network="private",
                                   member_ids=[self.paul.id, self.member.id])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- outils ---------------------------------------------------------------------
    def _user(self, username, role, first, last):
        u = User(username=username, email=f"{username}@exemple.fr", contact_email=f"{username}@exemple.fr",
                 role=role, first_name=first, last_name=last)
        u.set_password(PASSWORD)
        db.session.add(u)
        db.session.flush()
        return u

    def client(self, username):
        from flask import g
        for k in ("_login_user", "sidebar", "picker"):  # contexte d'application partagé avec le test
            g.pop(k, None)
        c = self.app.test_client()
        page = c.get("/login").get_data(as_text=True)
        token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        r = c.post("/login", data={"login": username, "password": PASSWORD, "_csrf": token})
        self.assertEqual(r.status_code, 302)
        return c, token

    def get(self, c, url, **kw):
        from flask import g
        for k in ("_login_user", "sidebar", "picker"):
            g.pop(k, None)
        return c.get(url, **kw)

    def post(self, c, url, **kw):
        from flask import g
        for k in ("_login_user", "sidebar", "picker"):
            g.pop(k, None)
        return c.post(url, **kw)

    def issue(self, project, title, actor=None, **data):
        i = create_issue(project, dict(title=title, **data), actor or self.admin)
        db.session.commit()
        return i

    def state(self, project, group):
        return project.first_state_of(group)


# ---------------------------------------------------------------------------
# Piste 30 : exports
# ---------------------------------------------------------------------------

class ExportTests(TravailCase):
    def test_xlsx_texte_commencant_par_egal_reste_du_texte_et_revient_au_reimport(self):
        from hangar import importer
        import openpyxl
        i = self.issue(self.pub, "=1+1")
        j = self.issue(self.pub, "=> relancer le fournisseur")
        data = importer.export_xlsx([i, j], "Commun").getvalue()
        ws = openpyxl.load_workbook(io.BytesIO(data)).worksheets[0]
        self.assertEqual(ws["B2"].value, "=1+1")
        self.assertEqual(ws["B2"].data_type, "s")  # pas une formule
        self.assertEqual(ws["B3"].data_type, "s")
        # Réimport en mise à jour : les titres sont retrouvés, rien n'est ignoré
        rows = importer.read_table(io.BytesIO(data), "export.xlsx")
        report = importer.import_rows(self.pub, rows, self.admin, update_existing=True)
        db.session.commit()
        self.assertEqual((report["updated"], report["skipped"]), (2, 0))
        db.session.refresh(i)
        self.assertEqual(i.title, "=1+1")

    def test_csv_formules_protegees_et_reimport_fidele(self):
        from hangar import importer
        i = self.issue(self.pub, "=1+1")
        j = self.issue(self.pub, "'déjà entre apostrophes")
        k = self.issue(self.pub, "- vérifier le devis")
        text = importer.export_csv([i, j, k])
        self.assertIn(";'=1+1;", text)
        self.assertIn(";'- vérifier le devis;", text)
        self.assertIn(";'déjà entre apostrophes;", text)  # apostrophe d'origine : pas de deuxième
        rows = importer.read_table(io.BytesIO(text.encode("utf-8")), "export.csv")
        self.assertEqual([r["title"] for r in rows], ["=1+1", "'déjà entre apostrophes", "- vérifier le devis"])

    def test_csv_analytique_protege(self):
        lab = __import__("hangar.services", fromlist=["get_or_create_label"]).get_or_create_label(self.pub, "=1+1")
        db.session.flush()
        self.issue(self.pub, "Ticket étiqueté", label_ids=[lab.id])
        c, _ = self.client("chef")
        r = self.get(c, f"/projects/{self.pub.id}/analytics?tab=custom&x=label&format=csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("'=1+1;", r.get_data(as_text=True))

    def _ticket_en_cours(self, title):
        i = self.issue(self.pub, title)
        update_issue(i, {"state_id": self.state(self.pub, "started").id}, self.admin)
        db.session.commit()
        return i

    def test_caractere_de_controle_ne_bloque_ni_excel_ni_word(self):
        import openpyxl
        i = self._ticket_en_cours("x")
        i.title = "Préparer la soutenance\x0bdiapos 3 à 5"   # ticket déjà en base (avant la correction)
        i.description = "Note\x01 collée"
        db.session.commit()
        c, tok = self.client("chef")
        r = self.get(c, f"/projects/{self.pub.id}/issues/export.xlsx")
        self.assertEqual(r.status_code, 200)
        ws = openpyxl.load_workbook(io.BytesIO(r.data)).worksheets[0]
        self.assertEqual(ws["B2"].value, "Préparer la soutenance\ndiapos 3 à 5")
        self.assertEqual(self.get(c, f"/bilan/export.docx?pid={self.pub.id}").status_code, 200)
        self.assertEqual(self.get(c, "/bilan/export.docx").status_code, 200)
        # Bilan enregistré : ses données figées contiennent le caractère
        r = self.post(c, "/bilan/save", data={"_csrf": tok, "pid": self.pub.id, "period": "since_start",
                                              "title": "Bilan\x0b de la semaine"})
        self.assertEqual(r.status_code, 302)
        from hangar.models import BilanReport
        rid = db.session.query(BilanReport).order_by(BilanReport.id.desc()).first().id
        self.assertEqual(self.get(c, f"/bilan/reports/{rid}/export.docx").status_code, 200)

    def test_titre_saisi_sans_caractere_de_controle(self):
        c, tok = self.client("chef")
        r = self.post(c, "/issues/new", data={"_csrf": tok, "project_id": self.pub.id,
                                              "title": "Préparer\x0bles diapos\tvite"})
        self.assertEqual(r.status_code, 302)
        i = Issue.query.order_by(Issue.id.desc()).first()
        self.assertEqual(i.title, "Préparer les diapos vite")


# ---------------------------------------------------------------------------
# Piste 36 : cloisonnement
# ---------------------------------------------------------------------------

class CloisonnementTests(TravailCase):
    def setUp(self):
        super().setUp()
        self.secret = self.issue(self.priv, "Licenciement de Paul")
        self.page_privee = Page(project=self.pub, title="PagePriveeAnne", owner=self.admin, is_public=False)
        self.page_secret = Page(project=self.priv, title="PageDuSecret", owner=self.admin)
        self.vue_secret = SavedView(project=self.priv, name="VueSecreteSanctions", owner=self.admin)
        self.vue_privee = SavedView(project=self.pub, name="VuePriveeAnne", description="sanctions",
                                    owner=self.admin, is_public=False)
        self.cycle_secret = Cycle(project=self.priv, name="CycleSecret", owner=self.admin)
        self.module_secret = Module(project=self.priv, name="ModuleSecret", status="planned")
        db.session.add_all([self.page_privee, self.page_secret, self.vue_secret, self.vue_privee,
                            self.cycle_secret, self.module_secret])
        db.session.commit()

    def test_favori_refuse_pour_un_objet_invisible(self):
        c, tok = self.client("carol")  # membre de l'espace, hors du projet secret
        cases = [("page", self.page_privee.id), ("page", self.page_secret.id), ("project", self.priv.id),
                 ("view", self.vue_secret.id), ("view", self.vue_privee.id), ("cycle", self.cycle_secret.id),
                 ("module", self.module_secret.id)]
        for etype, eid in cases:
            r = self.post(c, f"/favorite/{etype}/{eid}", data={"_csrf": tok})
            self.assertEqual(r.status_code, 404, (etype, eid))
        self.assertEqual(Favorite.query.filter_by(user_id=self.carol.id).count(), 0)
        # Un objet visible reste ajoutable
        r = self.post(c, f"/favorite/project/{self.pub.id}", data={"_csrf": tok})
        self.assertEqual(r.status_code, 302)

    def test_barre_laterale_ne_montre_pas_un_favori_devenu_invisible(self):
        for etype, eid in [("page", self.page_privee.id), ("project", self.priv.id), ("view", self.vue_secret.id),
                           ("page", self.page_secret.id)]:
            db.session.add(Favorite(user_id=self.carol.id, entity_type=etype, entity_id=eid))
        db.session.commit()
        c, _ = self.client("carol")
        html = self.get(c, f"/projects/{self.pub.id}/issues").get_data(as_text=True)
        for secret in ("PagePriveeAnne", "Projet secret", "VueSecreteSanctions", "PageDuSecret"):
            self.assertNotIn(secret, html)

    def test_vue_d_un_autre_projet_ou_privee_ignoree(self):
        c, _ = self.client("carol")
        for vid in (self.vue_secret.id, self.vue_privee.id):
            html = self.get(c, f"/projects/{self.pub.id}/issues?view={vid}").get_data(as_text=True)
            self.assertNotIn("VueSecreteSanctions", html)
            self.assertNotIn("VuePriveeAnne", html)

    def test_relation_vers_un_ticket_invisible_introuvable(self):
        from hangar.services import ensure_member
        ensure_member(self.pub, self.carol)  # Carol peut modifier ses tickets du projet commun
        mine = self.issue(self.pub, "Mon ticket", actor=self.carol)
        acts = len(self.secret.activities)
        c, tok = self.client("carol")
        self.post(c, f"/projects/{self.pub.id}/issues/{mine.sequence_id}/relations",
                  data={"_csrf": tok, "related_id": self.secret.id, "relation_type": "relates_to"})
        self.assertEqual(IssueRelation.query.count(), 0)
        db.session.expire_all()
        self.assertEqual(len(self.secret.activities), acts)
        html = self.get(c, f"/projects/{self.pub.id}/issues/{mine.sequence_id}").get_data(as_text=True)
        self.assertNotIn("Licenciement de Paul", html)

    def _enable_mail(self):
        cfg = MailConfig.get()
        cfg.enabled = True
        cfg.send_notifications = True
        db.session.commit()

    def test_membre_retire_ne_recoit_plus_rien(self):
        self._enable_mail()
        update_issue(self.secret, {"assignee_ids": [self.paul.id]}, self.admin)
        db.session.commit()
        self.assertTrue(self.secret.is_subscribed(self.paul))
        c, tok = self.client("chef")
        m = ProjectMember.query.filter_by(project_id=self.priv.id, user_id=self.paul.id).one()
        self.post(c, f"/projects/{self.priv.id}/settings/members",
                  data={"_csrf": tok, "action": "remove", "member_id": m.id})
        db.session.expire_all()
        self.assertFalse(self.secret.is_subscribed(self.paul))
        before = (Notification.query.filter_by(user_id=self.paul.id).count(),
                  EmailMessage.query.filter_by(to_user_id=self.paul.id).count())
        self.post(c, f"/projects/{self.priv.id}/issues/{self.secret.sequence_id}/comments",
                  data={"_csrf": tok, "body": "Avis rendu"})
        self.post(c, f"/projects/{self.priv.id}/issues/{self.secret.sequence_id}/edit",
                  data={"_csrf": tok, "title": "Licenciement de Paul — validé"})
        after = (Notification.query.filter_by(user_id=self.paul.id).count(),
                 EmailMessage.query.filter_by(to_user_id=self.paul.id).count())
        self.assertEqual(before, after)

    def test_abonne_reste_hors_projet_ne_recoit_rien(self):
        # Ancien abonnement (avant la correction, ou projet passé de public à privé) : notify() filtre quand même
        self.secret.subscribers.append(self.carol)
        db.session.commit()
        c, tok = self.client("chef")
        self.post(c, f"/projects/{self.priv.id}/issues/{self.secret.sequence_id}/comments",
                  data={"_csrf": tok, "body": "Nouvel élément"})
        self.assertEqual(Notification.query.filter_by(user_id=self.carol.id).count(), 0)

    def test_quitter_le_projet_retire_les_abonnements(self):
        self.secret.subscribers.append(self.member)
        db.session.commit()
        c, tok = self.client("membre")
        self.post(c, f"/projects/{self.priv.id}/leave", data={"_csrf": tok})
        db.session.expire_all()
        self.assertFalse(self.secret.is_subscribed(self.member))

    def test_mention_d_un_compte_hors_projet_ignoree(self):
        self._enable_mail()
        c, tok = self.client("chef")
        self.post(c, f"/projects/{self.priv.id}/issues/{self.secret.sequence_id}/comments",
                  data={"_csrf": tok, "body": "Je préviens @invite et @carol"})
        db.session.expire_all()
        for u in (self.guest, self.carol):
            self.assertEqual(Notification.query.filter_by(user_id=u.id).count(), 0)
            self.assertEqual(EmailMessage.query.filter_by(to_user_id=u.id).count(), 0)
            self.assertFalse(self.secret.is_subscribed(u))
        # Un membre cité est bien prévenu
        self.post(c, f"/projects/{self.priv.id}/issues/{self.secret.sequence_id}/comments",
                  data={"_csrf": tok, "body": "Et @paul ?"})
        self.assertEqual(Notification.query.filter_by(user_id=self.paul.id, kind="mention").count(), 1)


# ---------------------------------------------------------------------------
# Piste 38 : fermeture automatique
# ---------------------------------------------------------------------------

class FermetureAutoTests(TravailCase):
    def setUp(self):
        super().setUp()
        self.pub.auto_close_months = 1
        db.session.commit()

    def _old(self, *issues):
        old = now() - timedelta(days=40)
        for i in issues:
            db.session.execute(db.update(Issue).where(Issue.id == i.id).values(updated_at=old))
        db.session.commit()
        db.session.expire_all()

    def _open_list(self):
        c, _ = self.client("invite")
        self.assertEqual(self.get(c, f"/projects/{self.pub.id}/issues").status_code, 200)
        db.session.expire_all()

    def test_temps_saisi_compte_comme_activite(self):
        i = self.issue(self.pub, "Rédiger le mémoire", state_id=self.state(self.pub, "started").id)
        self._old(i)
        c, tok = self.client("membre")
        self.post(c, f"/projects/{self.pub.id}/issues/{i.sequence_id}/worklogs",
                  data={"_csrf": tok, "duration": "2h"})
        self._open_list()
        self.assertEqual(db.session.get(Issue, i.id).state_group, "started")

    def test_parent_avec_sous_ticket_ouvert_garde(self):
        parent = self.issue(self.pub, "Rédiger le mémoire", state_id=self.state(self.pub, "started").id)
        self.issue(self.pub, "Chapitre 2", parent_id=parent.id)
        self._old(parent)
        self._open_list()
        self.assertEqual(db.session.get(Issue, parent.id).state_group, "started")

    def test_demande_d_invite_en_attente_et_projet_archive_epargnes(self):
        demande = create_issue(self.pub, {"title": "Demande d'invité"}, self.guest, intake=True)
        db.session.commit()
        self._old(demande)
        self._open_list()
        self.assertNotEqual(db.session.get(Issue, demande.id).state_group, "cancelled")
        from hangar.services import run_automations
        i = self.issue(self.pub, "Vieux ticket")
        self._old(i)
        self.pub.archived_at = now()
        db.session.commit()
        run_automations(self.pub)
        self.assertNotEqual(db.session.get(Issue, i.id).state_group, "cancelled")

    def test_ticket_abandonne_ferme_et_assignes_prevenus(self):
        i = self.issue(self.pub, "Oublié", assignee_ids=[self.member.id])
        self._old(i)
        self._open_list()
        self.assertEqual(db.session.get(Issue, i.id).state_group, "cancelled")
        n = Notification.query.filter_by(user_id=self.member.id, issue_id=i.id).order_by(Notification.id.desc()).first()
        self.assertIn("Fermé automatiquement", n.message)


# ---------------------------------------------------------------------------
# Piste 42 : suppression définitive et import
# ---------------------------------------------------------------------------

class DroitsTests(TravailCase):
    def test_suppression_depuis_la_fiche(self):
        self.pub.lead = self.paul
        db.session.commit()
        a, b = self.issue(self.pub, "Ticket A"), self.issue(self.pub, "Ticket B")
        c, tok = self.client("membre")  # simple membre : refusé, le ticket reste
        self.post(c, f"/projects/{self.pub.id}/issues/{a.sequence_id}/delete", data={"_csrf": tok})
        self.assertIsNotNone(db.session.get(Issue, a.id))
        html = self.get(c, f"/projects/{self.pub.id}/issues/{a.sequence_id}").get_data(as_text=True)
        self.assertNotIn(f"/issues/{a.sequence_id}/delete", html)
        c, tok = self.client("paul")  # responsable du projet
        self.post(c, f"/projects/{self.pub.id}/issues/{a.sequence_id}/delete", data={"_csrf": tok})
        db.session.expire_all()
        self.assertIsNone(db.session.get(Issue, a.id))
        c, tok = self.client("chef")  # administrateur
        self.post(c, f"/projects/{self.pub.id}/issues/{b.sequence_id}/delete", data={"_csrf": tok})
        db.session.expire_all()
        self.assertIsNone(db.session.get(Issue, b.id))

    def test_suppression_en_masse(self):
        self.pub.lead = self.paul
        db.session.commit()
        a = self.issue(self.pub, "Ticket A")
        c, tok = self.client("membre")
        self.post(c, "/issues/bulk", data={"_csrf": tok, "ids": [a.id], "bulk_action": "delete"})
        self.assertIsNotNone(db.session.get(Issue, a.id))
        c, tok = self.client("paul")
        self.post(c, "/issues/bulk", data={"_csrf": tok, "ids": [a.id], "bulk_action": "delete"})
        db.session.expire_all()
        self.assertIsNone(db.session.get(Issue, a.id))

    def test_suppression_par_l_api_retire_aussi_le_fichier(self):
        from hangar.models import APIToken
        a = self.issue(self.pub, "Ticket avec pièce jointe")
        folder = os.path.join(self.app.config["UPLOAD_DIR"], str(self.pub.id))
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "abc_piece.txt")
        with open(path, "w") as fh:
            fh.write("contenu")
        db.session.add(Attachment(issue=a, filename="piece.txt", stored_name=f"{self.pub.id}/abc_piece.txt",
                                  size=7, mimetype="text/plain", uploaded_by=self.admin))
        tm, raw_m = APIToken.generate(self.member, "membre")
        ta, raw_a = APIToken.generate(self.admin, "admin")
        db.session.add_all([tm, ta])
        db.session.commit()
        c = self.app.test_client()
        r = c.delete(f"/api/v1/issues/{a.id}", headers={"X-API-Key": raw_m})
        self.assertEqual(r.status_code, 403)
        self.assertIsNotNone(db.session.get(Issue, a.id))
        r = c.delete(f"/api/v1/issues/{a.id}", headers={"X-API-Key": raw_a})
        self.assertEqual(r.status_code, 200)
        db.session.expire_all()
        self.assertIsNone(db.session.get(Issue, a.id))
        self.assertFalse(os.path.exists(path))

    def _import(self, username, csv_text, **form):
        c, tok = self.client(username)
        data = {"_csrf": tok, "file": (io.BytesIO(csv_text.encode("utf-8")), "import.csv")}
        data.update(form)
        r = self.post(c, f"/projects/{self.priv.id}/issues/import", data=data, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        with c.session_transaction() as s:
            return [m for _, m in s.pop("_flashes", [])]

    CSV = "Titre;Responsable;Appui\nAction 1;Carol Petit;Ines Invitée\nAction 2;Kevin Inconnu;Paul Durand\n"

    def members(self):
        db.session.expire_all()
        return {m.user.username for m in ProjectMember.query.filter_by(project_id=self.priv.id)}

    def test_import_par_un_membre_ne_fait_entrer_personne(self):
        before = self.members()
        flashes = self._import("membre", self.CSV, create_members="1")
        self.assertEqual(self.members(), before)  # ni Carol, ni l'invitée, ni compte créé
        self.assertIsNone(User.query.filter_by(username="kevin.inconnu").first())
        self.assertTrue(any("Carol Petit" in f and "pas membres" in f for f in flashes), flashes)
        # Paul, membre, est bien assigné
        i = Issue.query.filter_by(project_id=self.priv.id, title="Action 2").one()
        self.assertEqual([u.username for u in i.assignees], ["paul"])

    def test_import_par_l_administrateur(self):
        flashes = self._import("chef", self.CSV, create_members="1")
        self.assertIn("carol", self.members())
        self.assertIn("invite", self.members())
        kevin = User.query.filter_by(username="kevin.inconnu").one()
        self.assertFalse(kevin.active)  # compte à activer ensuite
        self.assertTrue(any("Invités ajoutés" in f and "Ines" in f for f in flashes), flashes)


# ---------------------------------------------------------------------------
# Piste 66 : numérotation sans collision
# ---------------------------------------------------------------------------

class SuppressionEnMasseAffichageTests(TravailCase):
    """Relecture piste 42 : la barre d'actions groupées ne propose la corbeille qu'à qui peut supprimer,
    et un refus est expliqué (avec l'archivage proposé) au lieu d'un « 0 ticket(s) mis à jour. »."""

    def test_bouton_corbeille_masque_pour_un_simple_membre(self):
        self.pub.lead = self.paul
        db.session.commit()
        self.issue(self.pub, "Ticket A")
        url = f"/projects/{self.pub.id}/issues?layout=spreadsheet"
        c, _ = self.client("membre")
        html = self.get(c, url).get_data(as_text=True)
        self.assertIn("data-bulkbar", html)
        self.assertNotIn('data-bulk-btn="delete"', html)
        c, _ = self.client("paul")
        self.assertIn('data-bulk-btn="delete"', self.get(c, url).get_data(as_text=True))

    def test_refus_explique_et_propose_d_archiver(self):
        a = self.issue(self.pub, "Ticket A")
        c, tok = self.client("membre")
        r = self.post(c, "/issues/bulk", data={"_csrf": tok, "ids": [a.id], "bulk_action": "delete"},
                      follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertIsNotNone(db.session.get(Issue, a.id))
        self.assertIn("Tu peux l’archiver à la place", html)
        self.assertNotIn("0 ticket(s) mis à jour", html)


class DemoDatesTests(unittest.TestCase):
    """Relecture piste 38 : charger la démo ne fait pas passer tous ses tickets pour modifiés aujourd'hui."""

    def test_tickets_de_la_demo_gardent_leur_date_de_modification(self):
        tmp = tempfile.mkdtemp(prefix="hangar-travail-demo-")
        app = create_app(tmp, testing=True)
        try:
            with app.test_request_context("/"):
                from hangar.seed import seed_demo
                seed_demo(None, verbose=False, password=PASSWORD)
                issues = Issue.query.all()
                self.assertTrue(issues)
                today = now().date()
                anciens = [i for i in issues if i.updated_at.date() < today]
                self.assertGreater(len(anciens), len(issues) // 2)
                for i in issues:
                    if i.completed_at:
                        self.assertEqual(i.updated_at, i.completed_at, i.key)
                db.session.remove()
                db.engine.dispose()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class NumerotationTests(TravailCase):
    def test_creations_simultanees_numeros_distincts(self):
        app, pid, uid = self.app, self.pub.id, self.admin.id
        n = 4
        barrier = threading.Barrier(n)
        errors, seqs = [], []

        def worker(k):
            with app.test_request_context("/"):
                try:
                    from hangar.models import Project
                    project = db.session.get(Project, pid)
                    actor = db.session.get(User, uid)
                    project.states  # noqa: B018 - tout est chargé avant de partir ensemble
                    project.members  # noqa: B018
                    barrier.wait()
                    i = create_issue(project, {"title": f"Action {k}"}, actor)
                    db.session.commit()
                    seqs.append(i.sequence_id)
                except Exception as exc:  # noqa: BLE001
                    db.session.rollback()
                    errors.append(repr(exc))
                finally:
                    db.session.remove()

        threads = [threading.Thread(target=worker, args=(k,)) for k in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(seqs), [1, 2, 3, 4])

    def test_deplacement_vers_un_projet_prend_un_nouveau_numero(self):
        from hangar.services import move_issue_to_project
        self.issue(self.priv, "Déjà là")
        i = self.issue(self.pub, "À déplacer")
        move_issue_to_project(i, self.priv, self.admin)
        db.session.commit()
        self.assertEqual((i.project_id, i.sequence_id), (self.priv.id, 2))


if __name__ == "__main__":
    unittest.main()
