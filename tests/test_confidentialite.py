"""Cloisonnement des projets privés : bilans enregistrés, initiatives (page, Workgraph, texte envoyé à l'IA)
et onglet « Temps » d'un profil ne montrent que ce que le lecteur a le droit de voir.

Mise en place commune : un projet public « EAE » (tout le monde) et un projet privé « Conseil de discipline »
(l'administrateur seul), avec un ticket « Jalon », un module, une dépendance vers le projet public et du temps saisi.

Lancer : uv run python -m unittest tests.test_confidentialite -v
"""
import json
import re
import shutil
import tempfile
import unittest
from datetime import date, timedelta

from hangar import create_app
from hangar import workgraph as W
from hangar.extensions import db
from hangar.models import BilanReport, Initiative, Label, Module, ProjectMember, User, WorkLog
from hangar.services import add_relation, create_issue, create_project

SECRETS = ("Licenciement de Paul", "Avis du chef de corps", "Dossier secret", "Conseil de discipline",
           "entretien préalable avec Paul")


class ConfidentialiteCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-test-")
        self.app = create_app(self.tmp, testing=True)
        self.ctx = self.app.test_request_context("/")
        self.ctx.push()
        self.today = date.today()
        self.admin = self._user("chef", "admin", "Anne", "Chef")
        self.member = self._user("membre", "member", "Marc", "Membre")
        self.guest = self._user("invite", "guest", "Ines", "Invitée")
        everyone = [self.admin.id, self.member.id, self.guest.id]
        self.pub = create_project("EAE", "EAE", self.admin, member_ids=everyone)
        self.priv = create_project("Conseil de discipline", "CDD", self.admin, network="private")
        self.mod = Module(project=self.priv, name="Dossier secret", status="in-progress",
                          target_date=self.today + timedelta(days=10))
        db.session.add(self.mod)
        db.session.flush()
        # Projet privé : un jalon daté et un ticket qui bloque le projet public
        self.secret = self._issue(self.priv, "Licenciement de Paul", [self.mod], self.today + timedelta(days=5))
        jalon = Label(project=self.priv, name="Jalon", color="#dc2626")
        db.session.add(jalon)
        self.secret.labels.append(jalon)
        self.secret_blocker = self._issue(self.priv, "Avis du chef de corps", [])
        # Projet public
        self.public = self._issue(self.pub, "Planning des entretiens", [], self.today + timedelta(days=3))
        self.public_blocker = self._issue(self.pub, "Grille d’évaluation", [])
        add_relation(self.public, self.secret_blocker, "blocked_by", self.admin)   # privé → public
        add_relation(self.public, self.public_blocker, "blocked_by", self.admin)   # public → public
        # Temps saisi par l'administrateur
        db.session.add(WorkLog(issue=self.secret, user=self.admin, minutes=90, date=self.today,
                               description="entretien préalable avec Paul"))
        db.session.add(WorkLog(issue=self.public, user=self.admin, minutes=30, date=self.today,
                               description="relecture du planning"))
        # Initiative liée aux deux projets
        self.ini = Initiative(name="Fin d’année", emoji="🎯", status="active", created_by=self.admin,
                              lead=self.admin, start_date=self.today - timedelta(days=30),
                              target_date=self.today + timedelta(days=60))
        self.ini.projects = [self.pub, self.priv]
        db.session.add(self.ini)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- outils ---------------------------------------------------------------------
    def _user(self, username, role, first, last):
        u = User(username=username, email=User.internal_email(username), role=role, first_name=first, last_name=last)
        u.set_password("secret1")
        db.session.add(u)
        db.session.flush()
        return u

    def _issue(self, project, title, modules, target=None):
        i = create_issue(project, {"title": title, "module_ids": [m.id for m in modules], "target_date": target},
                         self.admin)
        db.session.flush()
        return i

    def client(self, username):
        from flask import g
        for k in ("_login_user", "sidebar", "picker"):  # contexte d'application partagé avec le test
            g.pop(k, None)
        c = self.app.test_client()
        page = c.get("/login").get_data(as_text=True)
        token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        r = c.post("/login", data={"login": username, "password": "secret1", "_csrf": token})
        self.assertEqual(r.status_code, 302)
        return c, token

    def assertNoSecret(self, text, where=""):
        for s in SECRETS:
            self.assertNotIn(s, text, f"« {s} » visible {where}")

    def save_bilan(self, username, project="", title="Bilan de la semaine"):
        c, tok = self.client(username)
        r = c.post("/bilan/save", data={"_csrf": tok, "pid": "", "project": project, "period": "since_start",
                                        "title": title})
        self.assertEqual(r.status_code, 302)
        return db.session.query(BilanReport).order_by(BilanReport.id.desc()).first()


class BilanTests(ConfidentialiteCase):
    def test_bilan_global_de_l_admin_reserve(self):
        """Le bilan « Tous les projets » de l'administrateur embarque le projet privé : ni liste, ni lecture,
        ni export, ni envoi pour un membre ou un invité."""
        r = self.save_bilan("chef", title="Bilan global du chef")
        self.assertIn("Licenciement de Paul", r.data_json)  # l'auteur voit bien tout
        for username in ("membre", "invite"):
            c, tok = self.client(username)
            page = c.get("/bilan").get_data(as_text=True)
            self.assertNotIn("Bilan global du chef", page)
            self.assertNoSecret(page, f"dans /bilan pour {username}")
            self.assertEqual(c.get(f"/bilan/reports/{r.id}").status_code, 403)
            self.assertEqual(c.get(f"/bilan/reports/{r.id}/export.md").status_code, 403)
            self.assertEqual(c.get(f"/bilan/reports/{r.id}/export.docx").status_code, 403)
            self.assertEqual(c.post(f"/bilan/reports/{r.id}/send",
                                    data={"_csrf": tok, "others": "dehors@example.com"}).status_code, 403)
        # L'auteur et les administrateurs gardent l'accès
        c, _ = self.client("chef")
        self.assertIn("Bilan global du chef", c.get("/bilan").get_data(as_text=True))
        self.assertEqual(c.get(f"/bilan/reports/{r.id}").status_code, 200)

    def test_bilan_d_un_seul_projet_prive_choisi_dans_le_selecteur(self):
        r = self.save_bilan("chef", project=str(self.priv.id), title="Bilan du conseil")
        self.assertEqual([p["id"] for p in r.data["scope"]["projects"]], [self.priv.id])
        c, _ = self.client("membre")
        self.assertNotIn("Bilan du conseil", c.get("/bilan").get_data(as_text=True))
        self.assertEqual(c.get(f"/bilan/reports/{r.id}").status_code, 403)

    def test_bilan_sur_projets_visibles_reste_partage(self):
        r = self.save_bilan("chef", project=str(self.pub.id), title="Bilan EAE")
        for username in ("membre", "invite"):
            c, _ = self.client(username)
            self.assertIn("Bilan EAE", c.get("/bilan").get_data(as_text=True))
            page = c.get(f"/bilan/reports/{r.id}")
            self.assertEqual(page.status_code, 200)
            self.assertNoSecret(page.get_data(as_text=True), f"dans le bilan EAE pour {username}")
            self.assertEqual(c.get(f"/bilan/reports/{r.id}/export.md").status_code, 200)

    def test_actualiser_reprend_les_projets_du_bilan(self):
        """Un administrateur qui actualise le bilan global d'un membre n'y fait pas entrer ses projets privés."""
        r = self.save_bilan("membre", title="Bilan du membre")
        self.assertEqual([p["id"] for p in r.data["scope"]["projects"]], [self.pub.id])
        c, tok = self.client("chef")
        res = c.post(f"/bilan/reports/{r.id}", data={"_csrf": tok, "refresh": "1", "title": r.title})
        self.assertEqual(res.status_code, 302)
        db.session.expire_all()
        r = db.session.get(BilanReport, r.id)
        self.assertEqual([p["id"] for p in r.data["scope"]["projects"]], [self.pub.id])
        self.assertNoSecret(r.data_json, "dans le bilan actualisé")
        c, _ = self.client("membre")
        self.assertNoSecret(c.get(f"/bilan/reports/{r.id}/export.md").get_data(as_text=True), "dans l'export")

    def test_ancien_bilan_sans_liste_de_projets(self):
        """Un bilan global dont on ne connaît pas les projets couverts est réservé à son auteur et aux admins."""
        from hangar.bilan import compute_bilan
        data = compute_bilan([self.pub], self.today, self.today)
        del data["scope"]["projects"]  # contenu sans la liste des projets couverts
        old = BilanReport(title="Ancien bilan", data_json=json.dumps(data, ensure_ascii=False),
                          created_by=self.member, period_start=self.today, period_end=self.today)
        db.session.add(old)
        db.session.commit()
        c, _ = self.client("invite")
        self.assertNotIn("Ancien bilan", c.get("/bilan").get_data(as_text=True))
        self.assertEqual(c.get(f"/bilan/reports/{old.id}").status_code, 403)
        for username in ("membre", "chef"):
            c, _ = self.client(username)
            self.assertIn("Ancien bilan", c.get("/bilan").get_data(as_text=True))
            self.assertEqual(c.get(f"/bilan/reports/{old.id}").status_code, 200)

    def _bilan_du_membre_avec_le_projet_prive(self):
        """Le membre fait partie du projet privé quand il enregistre son bilan « Tous les projets »."""
        db.session.add(ProjectMember(project=self.priv, user=self.member, role="member"))
        db.session.commit()
        r = self.save_bilan("membre", title="Bilan du membre")
        self.assertEqual(sorted(p["id"] for p in r.data["scope"]["projects"]), sorted([self.pub.id, self.priv.id]))
        return r

    def _actualiser_en_admin(self, r):
        c, tok = self.client("chef")
        res = c.post(f"/bilan/reports/{r.id}", data={"_csrf": tok, "refresh": "1", "title": r.title})
        self.assertEqual(res.status_code, 302)
        db.session.expire_all()
        return db.session.get(BilanReport, r.id)

    def test_actualiser_sans_les_projets_que_l_auteur_ne_voit_plus(self):
        """Membre retiré d'un projet privé : quand un administrateur actualise son bilan, les nouveautés de ce
        projet n'y entrent pas (l'auteur lit toujours son propre bilan)."""
        r = self._bilan_du_membre_avec_le_projet_prive()
        ProjectMember.query.filter_by(project_id=self.priv.id, user_id=self.member.id).delete()
        self._issue(self.priv, "Nouveau secret après départ", [], self.today)
        db.session.commit()
        c, _ = self.client("membre")
        self.assertEqual(c.get(f"/projects/{self.priv.id}/issues").status_code, 403)
        r = self._actualiser_en_admin(r)
        self.assertEqual([p["id"] for p in r.data["scope"]["projects"]], [self.pub.id])
        self.assertNotIn("Nouveau secret après départ", r.data_json)
        self.assertNoSecret(r.data_json, "dans le bilan actualisé")
        c, _ = self.client("membre")
        self.assertEqual(c.get(f"/bilan/reports/{r.id}").status_code, 200)
        for url in (f"/bilan/reports/{r.id}", f"/bilan/reports/{r.id}/export.md"):
            body = c.get(url).get_data(as_text=True)
            self.assertNotIn("Nouveau secret après départ", body)
            self.assertNoSecret(body, f"dans {url}")

    def test_actualiser_le_bilan_d_un_compte_desactive(self):
        """Un compte désactivé garde ses adhésions : l'actualisation par un administrateur ne vide pas son bilan."""
        r = self._bilan_du_membre_avec_le_projet_prive()
        self.member.active = False
        db.session.commit()
        r = self._actualiser_en_admin(r)
        self.assertEqual(sorted(p["id"] for p in r.data["scope"]["projects"]), sorted([self.pub.id, self.priv.id]))


class InitiativeTests(ConfidentialiteCase):
    def test_page_initiative_sans_contenu_prive(self):
        for username in ("membre", "invite"):
            c, _ = self.client(username)
            res = c.get(f"/initiatives/{self.ini.id}")
            self.assertEqual(res.status_code, 200)
            page = res.get_data(as_text=True)
            self.assertNoSecret(page, f"sur la page de l'initiative pour {username}")
            self.assertIn("Grille d’évaluation", page)   # dépendance entre tickets visibles
            self.assertIn("1 projet(s) privé(s) lié(s) non affiché(s)", page)
            self.assertIn("1 dépendance(s) liée(s) à des projets privés non affichée(s)", page)
        c, _ = self.client("chef")
        page = c.get(f"/initiatives/{self.ini.id}").get_data(as_text=True)
        for s in ("Licenciement de Paul", "Avis du chef de corps", "Dossier secret", "Conseil de discipline"):
            self.assertIn(s, page)

    def test_jalons_et_dependances_filtres(self):
        snap = W.Snapshot()
        readable = W.readable_project_ids(self.member)
        s = snap.initiative_for(self.ini, readable)
        self.assertNotIn(self.secret.id, s["issue_ids"])
        self.assertEqual({(b["blocked"].id, b["blocker"].id) for b in s["blockers"]},
                         {(self.public.id, self.public_blocker.id)})
        titles = " ".join(j["title"] for j in W.milestones(self.ini, snap, s["issue_ids"], readable))
        self.assertNoSecret(titles, "dans les jalons")
        # L'avancement reste celui de toute l'initiative
        self.assertEqual(s["stats"], snap.initiative(self.ini)["stats"])
        admin = snap.initiative_for(self.ini, W.readable_project_ids(self.admin))
        self.assertIn(self.secret.id, admin["issue_ids"])
        self.assertIn("Licenciement de Paul",
                      [j["title"] for j in W.milestones(self.ini, snap, admin["issue_ids"],
                                                         W.readable_project_ids(self.admin))])

    def test_texte_envoye_a_l_ia(self):
        ctx = W.ai_context(f"/initiatives/{self.ini.id}", {}, self.member)
        self.assertNoSecret(ctx["text"], "dans le contexte IA de l'initiative")
        self.assertIn("EAE", ctx["text"])
        self.assertIn("Grille d’évaluation", ctx["text"])
        ctx = W.ai_context("/workgraph", {}, self.member)
        self.assertNoSecret(ctx["text"], "dans le contexte IA du Workgraph")
        ctx = W.ai_context(f"/initiatives/{self.ini.id}", {}, self.admin)
        self.assertIn("Licenciement de Paul", ctx["text"])
        self.assertIn("Conseil de discipline", ctx["text"])

    def test_workgraph_sans_titre_prive(self):
        """Initiative « Fin d'année » réduite au projet privé, bloquant une autre initiative publique : le lien
        entre les deux initiatives repose sur un ticket privé, son titre ne doit apparaître nulle part."""
        other = Initiative(name="Autre initiative", created_by=self.admin, status="planned")
        db.session.add(other)
        other.projects = [self.pub]
        self.ini.projects = [self.priv]
        db.session.commit()
        c, _ = self.client("membre")
        for url in ("/workgraph/data.json", "/workgraph", "/workgraph?view=table", "/initiatives",
                    f"/initiatives/{other.id}"):
            res = c.get(url)
            self.assertEqual(res.status_code, 200, url)
            self.assertNoSecret(res.get_data(as_text=True), f"dans {url}")
        # côté administrateur, la dépendance entre initiatives est bien là, avec ses titres
        g = W.build_graph(self.admin, "initiative")
        self.assertIn("Avis du chef de corps", json.dumps(g, ensure_ascii=False))


class ProfilTests(ConfidentialiteCase):
    def test_onglet_temps_sans_saisie_privee(self):
        for username in ("membre", "invite"):
            c, _ = self.client(username)
            res = c.get("/team/chef?tab=time")
            self.assertEqual(res.status_code, 200)
            page = res.get_data(as_text=True)
            self.assertNoSecret(page, f"dans l'onglet Temps pour {username}")
            self.assertIn("relecture du planning", page)
            self.assertIn("Planning des entretiens", page)
            self.assertIn('<div class="v">30 min</div>', page)   # total des saisies visibles
            self.assertNotIn('<div class="v">2 h</div>', page)  # et non 90 + 30 min
        c, _ = self.client("chef")
        page = c.get("/team/chef?tab=time").get_data(as_text=True)
        self.assertIn("entretien préalable avec Paul", page)
        self.assertIn("Licenciement de Paul", page)


if __name__ == "__main__":
    unittest.main()
