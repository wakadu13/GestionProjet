"""Parcours de toutes les pages sur les données de démonstration (projets, tickets, cycles, modules,
bilans, réglages…) : chaque page doit vraiment s'afficher (200, sauf exceptions écrites), et aucune page
sautée sans raison écrite. Un visiteur sans session est renvoyé vers la connexion partout, sauf sur une
liste blanche. Et les commandes en ligne écrivent en UTF-8 même quand leur sortie est redirigée dans un
encodage limité (cp1252 sous Windows)."""
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

from hangar import cli, create_app
from hangar.extensions import db
from hangar.models import (
    AgendaImport, AgendaTask, Attachment, BilanReport, Cycle, EmailMessage, Initiative, Issue, Module, Notification,
    Page, Project, SavedView, User,
)

MOT_DE_PASSE = "mot-de-passe-des-tests"

# Routes GET que le parcours ne visite pas, et le test qui les affiche à sa place (avec les fichiers du coffre,
# un set de QCM ou un fournisseur d'IA qu'il prépare). Le parcours échoue si une route est sautée sans être ici,
# ou si une route d'ici n'existe plus : la liste reste exacte.
SAUTEES = {
    "static": "fichiers statiques : tests/test_web_cache.py",
    "auth.login": "PremierLancementTests (ci-dessous) et toutes les connexions des tests",
    "auth.logout": "PremierLancementTests (ci-dessous) : déconnexion",
    "auth.setup": "PremierLancementTests (ci-dessous)",
    "auth.signup": "PremierLancementTests (ci-dessous)",
    "wiki.page": "tests/test_wiki.py DroitsEtPagesTests.test_pages_de_la_section",
    "wiki.edit": "tests/test_wiki.py DroitsEtPagesTests.test_pages_de_la_section",
    "wiki.historique": "tests/test_wiki.py DroitsEtPagesTests.test_pages_de_la_section",
    "wiki.tag": "tests/test_wiki.py DroitsEtPagesTests.test_pages_de_la_section",
    "wiki.pdf": "tests/test_wiki.py DroitsEtPagesTests.test_pages_de_la_section",
    "wiki.file": "tests/test_wiki.py DroitsEtPagesTests.test_pages_de_la_section",
    "wiki.renommer": "tests/test_exploitation.py test_renommer_met_a_jour_liens_favoris_historique_et_qcm",
    "qcm.fichier": "tests/test_qcm.py test_cartes_et_editeur",
    "qcm.langue_menu": "tests/test_qcm.py test_routes_langues",
    "qcm.langue_play": "tests/test_qcm.py test_routes_langues",
    "ai.provider_models": "tests/test_ai.py test_test_connection_and_model_lists (fournisseur d'IA simulé)",
    "ai.copie_photo": "tests/test_ai_revision.py test_copie",
    "ai.relire": "tests/test_ai_revision.py test_generer_qcm_brouillon_relecture_publication",
}

# Pages qui ne répondent pas 200 dans le parcours, volontairement.
NON_200 = {
    # API à jeton : la session du navigateur ne suffit pas (tests/test_outillage.py ApiV1Tests avec un jeton)
    "/api/v1/me": 401, "/api/v1/projects": 401, "/api/v1/projects/{pid}": 401,
    "/api/v1/projects/{pid}/issues": 401, "/api/v1/issues/{iid}": 401, "/api/v1/issues/{iid}/comments": 401,
    # Raccourcis et alias : ils renvoient vers la vraie page
    "/i/{key}": 302, "/notifications/{nid}/open": 302, "/projects/{pid}": 302, "/projects/{pid}/issues/{seq}/edit": 302,
    "/projects/{pid}/views/{vid}": 302, "/views/{gvid}": 302,  # une vue enregistrée ouvre la liste filtrée
    "/aujourdhui/": 302, "/claude/reglages/": 302, "/compte/": 302, "/compte/mot-de-passe/": 302,
    "/connexion/": 302, "/inscription/": 302, "/utilisateurs/": 302,
    "/settings/backup/download": 302,  # sans ?name= : retour à la page des sauvegardes (la copie demande un POST)
    "/qcm/generer/": 302, "/qcm/importer-vocabulaire/": 302,  # aucune IA réglée : renvoi vers les réglages
    # Sans ?set= ni coffre de QCM : page introuvable (avec un set : tests/test_qcm.py)
    "/qcm/jouer/": 404, "/qcm/exporter/": 404,
    "/hors-ligne/dossier.json": 404,  # sans coffre prêt pour le hors-ligne (tests/test_wiki_parite.py)
}

# Visiteur sans session : seules ces pages répondent sans renvoyer vers la connexion.
PUBLIQUES = {
    "/login", "/setup", "/signup",
    "/api/status/",           # l'appli Android vérifie que le serveur répond
    "/api/v1/",               # présentation de l'API à jeton (aucune donnée)
    "/hors-connexion/",       # page affichée par le service worker quand le serveur ne répond pas
    "/sw.js", "/qcm/sw.js", "/manifest.webmanifest", "/qcm/manifest.webmanifest",
    "/icones.svg",
    "/connexion/", "/inscription/",  # alias de /login et /signup
}


class PagesSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="hangar-smoke-")
        cls.app = create_app(cls.tmp, testing=True)
        with cls.app.test_request_context("/"):
            from hangar.ai.models import AIConversation
            from hangar.qcm import masque, quiz
            from hangar.seed import seed_demo
            seed_demo(None, verbose=False, password=MOT_DE_PASSE)
            p = Project.query.filter_by(identifier="EAE").first() or Project.query.first()
            u = User.query.filter_by(username="wacogne").one()
            u.must_change_password = False  # la démo l'exige pour tous : ici on parcourt les pages directement
            issue = Issue.query.filter_by(project_id=p.id).order_by(Issue.sequence_id).first()
            db.session.add(AgendaTask(user_id=u.id, title="Tâche de test"))
            # Un objet de chaque sorte, pour afficher aussi les pages de détail
            uploads = cls.app.config["UPLOAD_DIR"]
            with open(os.path.join(uploads, "piece.txt"), "w", encoding="utf-8") as fh:
                fh.write("pièce jointe")
            att = Attachment(issue_id=issue.id, filename="note.txt", stored_name="piece.txt", size=12,
                             uploaded_by_id=u.id)
            dossier_agenda = os.path.join(cls.app.config["DATA_DIR"], "agenda", str(u.id))
            os.makedirs(dossier_agenda, exist_ok=True)
            with open(os.path.join(dossier_agenda, "edt.ics"), "w", encoding="utf-8") as fh:
                fh.write("BEGIN:VCALENDAR\nEND:VCALENDAR\n")
            imp = AgendaImport(user_id=u.id, filename="edt.ics", stored_name="edt.ics")
            images = quiz.dossier_perso(u.id) / masque.DOSSIER_IMAGES_PERSO
            images.mkdir(parents=True, exist_ok=True)
            (images / "schema.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            mail = EmailMessage(to_address="moi@exemple.fr", subject="Essai", body_text="Bonjour", status="outbox")
            conv = AIConversation(user_id=u.id, title="Essai")
            vue_globale = SavedView(project_id=None, name="Mes tickets", query="assignee=me", owner_id=u.id)
            ini = Initiative(name="Initiative de test", created_by_id=u.id)
            from hangar.bilan import compute_bilan, period_bounds
            debut, fin, libelle, _ = period_bounds("since_start", [p])
            rapport = BilanReport(project_id=p.id, title="Bilan de test", period_start=debut, period_end=fin,
                                  data_json=json.dumps(compute_bilan([p], debut, fin, libelle), default=str),
                                  highlights="Faits **marquants**", created_by_id=u.id)
            db.session.add_all([att, imp, mail, conv, vue_globale, ini, rapport])
            db.session.flush()
            notif = Notification.query.filter_by(user_id=u.id).first()
            commun = {"pid": p.id, "seq": issue.sequence_id, "key": issue.key, "username": "wacogne",
                      "page_id": Page.query.filter_by(project_id=p.id).first().id}
            cls.values = commun
            # Valeurs par route : un même nom de paramètre désigne des objets différents selon la route.
            cls.par_route = {
                "cycles.detail": {"cid": Cycle.query.filter_by(project_id=p.id).first().id},
                "modules.detail": {"mid": Module.query.filter_by(project_id=p.id).first().id},
                "saved_views.open": {"vid": db.session.query(SavedView).filter_by(project_id=p.id).first().id},
                "saved_views.open_global": {"vid": vue_globale.id},
                "api.issue_get": {"iid": issue.id},
                "apiv1.issue": {"iid": issue.id},
                "apiv1.comments": {"iid": issue.id},
                "issues.attachment_get": {"aid": att.id},
                "home.notification_open": {"nid": notif.id},
                "mail.message": {"mid": mail.id},
                "mail.message_eml": {"mid": mail.id},
                "ai.conversation": {"cid": conv.id},
                "ai.conversation_json": {"cid": conv.id},
                "workgraph.initiative_detail": {"iid": ini.id},
                "agenda.import_detail": {"iid": imp.id},
                "agenda.import_file": {"iid": imp.id},
                "bilan.report": {"rid": rapport.id},
                "qcm.image_perso": {"nom": "schema.png"},
                "wiki.documentation": [{}, {"doc": "hangar"}, {"doc": "qcm"}],
                "issues.export": [{"fmt": f} for f in ("csv", "xlsx", "json", "md")],
                "bilan.export_live": [{"fmt": f} for f in ("md", "docx")],
                "bilan.export_report": [{"rid": rapport.id, "fmt": f} for f in ("md", "docx")],
            }
            from hangar.views.projects import SECTIONS
            cls.par_route["projects.settings"] = [{"pid": p.id}] + [{"pid": p.id, "section": s} for s, _, _ in SECTIONS]
            cls.ids = {"pid": p.id, "seq": issue.sequence_id, "key": issue.key, "iid": issue.id, "nid": notif.id,
                       "vid": cls.par_route["saved_views.open"]["vid"], "gvid": vue_globale.id}
            db.session.commit()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            db.engine.dispose()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _client(self):
        c = self.app.test_client()
        token = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        r = c.post("/login", data={"login": "wacogne", "password": MOT_DE_PASSE, "_csrf": token})
        self.assertEqual(r.status_code, 302)
        return c

    def _urls(self):
        """Toutes les adresses GET à visiter, et les routes sautées (endpoint → raison)."""
        from flask import url_for
        urls, sautees, sans_valeur = set(), set(), []
        with self.app.test_request_context("/"):
            for rule in self.app.url_map.iter_rules():
                if "GET" not in rule.methods:
                    continue
                if rule.endpoint in SAUTEES:
                    sautees.add(rule.endpoint)
                    continue
                requis = set(rule.arguments) - set(rule.defaults or {})
                jeux = self.par_route.get(rule.endpoint)
                if jeux is None:
                    if not requis <= set(self.values):
                        sans_valeur.append(f"{rule.endpoint} ({rule.rule})")
                        continue
                    jeux = [{a: self.values[a] for a in requis}]
                elif isinstance(jeux, dict):
                    jeux = [{**{a: self.values[a] for a in requis if a in self.values}, **jeux}]
                for jeu in jeux:
                    if not requis <= set(jeu):
                        continue  # l'autre règle du même endpoint (paramètre facultatif)
                    urls.add(url_for(rule.endpoint, **jeu))
        return urls, sautees, sans_valeur

    def test_every_get_page(self):
        """Chaque route GET est vraiment affichée (200, ou le code écrit dans NON_200), ou sautée avec la
        référence du test qui la couvre."""
        urls, sautees, sans_valeur = self._urls()
        self.assertEqual(sans_valeur, [], "routes sans valeur de paramètre : ajoute-les à par_route ou à SAUTEES")
        self.assertEqual(sautees, set(SAUTEES), "SAUTEES cite une route qui n'existe plus")
        pid = self.values["pid"]
        for layout in ("list", "board", "spreadsheet", "calendar", "gantt"):
            urls.add(f"/projects/{pid}/issues?layout={layout}")
            urls.add(f"/issues?layout={layout}")
        for view in ("week", "month", "list"):
            urls.add(f"/agenda/calendar?view={view}")
        self.assertGreater(len(urls), 150)
        attendus = {k.format(**self.ids): v for k, v in NON_200.items()}
        self.assertLessEqual(set(attendus), urls, "NON_200 cite une adresse que le parcours ne visite pas")
        c = self._client()
        erreurs = []
        for url in sorted(urls):
            r = c.get(url)
            code = r.status_code
            r.close()  # réponses de fichiers (sauvegarde, exports) : fichier refermé tout de suite
            if code != attendus.get(url, 200):
                erreurs.append(f"{code} {url}")
        self.assertEqual(erreurs, [], "pages qui ne s'affichent pas comme prévu")

    def test_visiteur_sans_session(self):
        """Sans session, toute page GET renvoie vers /login (ou répond 401 pour l'API), sauf la liste blanche."""
        urls, _, _ = self._urls()
        with self.app.test_request_context("/"):
            from flask import url_for
            for endpoint, jeu in (("auth.login", {}), ("auth.setup", {}), ("auth.signup", {}), ("auth.logout", {}),
                                  ("wiki.page", {"path": "Accueil"}), ("wiki.file", {"path": "a.png"}),
                                  ("qcm.langue_menu", {"langue": "anglais"}), ("ai.copie_photo", {"pk": 1, "n": 0}),
                                  ("ai.provider_models", {"pid": 1}), ("ai.relire", {"jid": 1}),
                                  ("qcm.fichier", {"path": "a.png"}), ("wiki.renommer", {"path": "Accueil"}),
                                  ("wiki.edit", {"path": "Accueil"}), ("wiki.historique", {"path": "Accueil"}),
                                  ("wiki.tag", {"tag": "maths"}), ("wiki.pdf", {"path": "a.pdf"}),
                                  ("qcm.langue_play", {"langue": "anglais"})):
                urls.add(url_for(endpoint, **jeu))
        c = self.app.test_client()
        fautes = []
        for url in sorted(urls):
            if url.split("?")[0] in PUBLIQUES:
                continue
            etape = url
            for _ in range(3):  # un alias (/compte/ → /settings/profile) renvoie d'abord vers la vraie page
                r = c.get(etape)
                code, cible = r.status_code, r.headers.get("Location", "").split("#")[0]
                r.close()
                if code not in (301, 302, 303, 308) or cible.split("?")[0] in ("/login", "/connexion/"):
                    break
                etape = cible
            vers_connexion = code in (301, 302, 303) and cible.split("?")[0] in ("/login", "/connexion/")
            if not (vers_connexion or code == 401):
                fautes.append(f"{code} {url} {cible}")
        self.assertEqual(fautes, [], "pages accessibles sans être connecté")
        self.assertGreater(len(urls), 150)

    def test_ticket_workflow(self):
        """Créer un ticket, le commenter, changer son état : les vues principales restent en état de marche."""
        c = self._client()
        token = re.search(r'name="_csrf" value="([^"]+)"', c.get("/home").get_data(as_text=True)).group(1)
        pid = self.values["pid"]
        r = c.post("/api/issues/quick", json={"project_id": pid, "title": "Ticket du test de parcours"},
                   headers={"X-CSRF-Token": token})
        self.assertLess(r.status_code, 400, r.get_data(as_text=True)[:300])
        with self.app.app_context():
            issue = Issue.query.filter_by(title="Ticket du test de parcours").one()
            seq, iid = issue.sequence_id, issue.id
        r = c.post(f"/projects/{pid}/issues/{seq}/comments", data={"_csrf": token, "body": "Commentaire **markdown**"})
        self.assertLess(r.status_code, 400)
        r = c.post(f"/api/issues/{iid}", json={"priority": "high"}, headers={"X-CSRF-Token": token})
        self.assertLess(r.status_code, 400, r.get_data(as_text=True)[:300])
        for url in (f"/projects/{pid}/issues/{seq}", f"/projects/{pid}/issues", "/home", "/bilan", "/analytics"):
            self.assertEqual(c.get(url).status_code, 200, url)


class CliEncodingTests(unittest.TestCase):
    def test_redirected_output_in_cp1252(self):
        tmp = tempfile.mkdtemp(prefix="hangar-cli-")
        try:
            ics = f"{tmp}/planning.ics"
            with open(ics, "w", encoding="utf-8") as fh:
                fh.write("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:1\nDTSTART:20261112T080000\nDTEND:20261112T100000\n"
                         "SUMMARY:Cours  CM \\n\nEND:VEVENT\nEND:VCALENDAR\n")
            app = create_app(f"{tmp}/data", testing=True)
            with app.app_context():
                u = User(username="moi", email="moi@hangar.local", role="admin")
                u.set_password("secret1")
                db.session.add(u)
                db.session.commit()
                db.session.remove()
                db.engine.dispose()
            raw = io.BytesIO()
            stream = io.TextIOWrapper(raw, encoding="cp1252")  # sortie redirigée sous Windows
            with redirect_stdout(stream):
                cli.main(["--data", f"{tmp}/data", "agenda-import", ics])  # affiche « → »
                stream.flush()
            self.assertIn("→", raw.getvalue().decode("utf-8"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
