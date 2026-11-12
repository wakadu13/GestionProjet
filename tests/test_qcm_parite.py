"""Parité des QCM avec Revision : application hors ligne (catalogue, téléchargement, synchronisation),
rappels par notification web (pywebpush simulé, aucun réseau), API de l'application Android de bout
en bout, révocation des jetons, blocage des connexions, alias des adresses de Revision.

Lancer : uv run python -m unittest tests.test_qcm_parite -v
Données synthétiques uniquement.
"""
import json
import re
import shutil
import tempfile
import threading
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from hangar import heure as horloge  # heure de l'agenda (Paris), pas celle de la machine
from hangar import create_app
from hangar.extensions import db
from hangar.models import User
from hangar.qcm.models import AbonnementPush, Activite, QuizProgress

SET_QCM = {
    "titre": "Électricité",
    "questions": [
        {"id": "ohm", "type": "QCM", "question": "Loi d'Ohm : $U = ?$", "choix": ["$RI$", "$R/I$"], "reponses": [0]},
        {"id": "pairs", "type": "QCR", "question": "Nombres pairs ?", "choix": ["2", "3", "4"], "reponses": [0, 2]},
        {"id": "carte", "type": "FLASHCARD", "question": "Recto ![[schema.png]]", "verso": "Verso"},
    ],
}
LANGUE = {
    "langue": "Anglais", "titre": "Cuisine",
    "vocabulaire": [
        {"id": "v1", "mot": "kitchen", "traductions": ["cuisine"]},
        {"id": "v2", "mot": "oven", "traductions": ["four"]},
    ],
}
APPLI = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36 ProjetPluriApp/2.40"


def _png():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(buf, "PNG")
    return buf.getvalue()


class PariteTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-qcm2-")
        self.app = create_app(self.tmp, testing=True)
        self.app.config["QCM_VERIF_INTERVALLE"] = 0

        def _g_neuf():
            # Les requêtes du test partagent le contexte d'application du test (et donc `g`) :
            # chacune repart sans compte, comme en vrai (un `g` neuf par requête).
            from flask import g
            g.pop("_login_user", None)
        self.app.before_request_funcs.setdefault(None, []).insert(0, _g_neuf)
        vault = Path(self.app.config["WIKI_DIR"])
        for rel, data in (("QCM/Physique/elec.json", SET_QCM), ("QCM/Anglais/Maison/cuisine.json", LANGUE)):
            p = vault / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        (vault / "Images").mkdir(parents=True, exist_ok=True)
        (vault / "Images" / "schema.png").write_bytes(_png())
        self.ctx = self.app.test_request_context("/")
        self.ctx.push()
        self.user = User(username="eleve", email="eleve@hangar.local", role="admin", first_name="Élève")
        self.user.set_password("secret1")
        self.autre = User(username="autre", email="autre@hangar.local", role="member")
        self.autre.set_password("secret1")
        db.session.add_all([self.user, self.autre])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self, login="eleve", password="secret1", **kw):
        from flask import g
        g.pop("_login_user", None)
        c = self.app.test_client()
        page = c.get("/login").get_data(as_text=True)
        self.token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        r = c.post("/login", data={"login": login, "password": password, "_csrf": self.token}, **kw)
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:300])
        return c

    def anonyme(self):
        from flask import g
        g.pop("_login_user", None)
        return self.app.test_client()

    def post_json(self, c, url, data, **kw):
        headers = kw.pop("headers", {})
        if "Authorization" not in headers:
            headers.setdefault("X-CSRF-Token", self.token)
        return c.post(url, data=json.dumps(data), content_type="application/json", headers=headers, **kw)


# ---------------------------------------------------------------------------
# Application hors ligne
# ---------------------------------------------------------------------------
class HorsLigneTests(PariteTestCase):
    def test_page_service_worker_et_manifeste(self):
        c = self.client()
        page = c.get("/qcm/hors-ligne/").get_data(as_text=True)
        self.assertIn('data-sw-url="/qcm/sw.js"', page)
        self.assertIn("qcm_hors_ligne.js", page)
        self.assertIn('rel="manifest"', page)
        sw = self.anonyme().get("/qcm/sw.js")
        self.assertEqual(sw.status_code, 200)
        self.assertIn("javascript", sw.headers["Content-Type"])
        texte = sw.get_data(as_text=True)
        for attendu in ("importScripts", "/static/qcm/qcm_hors_ligne.js", "KaTeX_Main-Regular.woff2",
                        'addEventListener("push"', "notificationclick", "qcm-envoi"):
            self.assertIn(attendu, texte)
        m = json.loads(self.anonyme().get("/qcm/manifest.webmanifest").get_data(as_text=True))
        self.assertEqual((m["start_url"], m["scope"], m["display"]), ("/qcm/hors-ligne/", "/qcm/", "standalone"))
        with self.anonyme().get(m["icons"][0]["src"]) as icone:
            self.assertEqual(icone.status_code, 200)
        # Sans compte : la page renvoie vers la connexion
        self.assertEqual(self.anonyme().get("/qcm/hors-ligne/").status_code, 302)

    def test_catalogue_et_telechargement(self):
        c = self.client()
        cat = c.get("/qcm/hors-ligne/catalogue/").get_json()
        self.assertEqual(cat["joueur"], "eleve")
        self.assertTrue(cat["csrf"])
        self.assertEqual([s["id"] for s in cat["sets"]], ["QCM/Physique/elec"])
        self.assertEqual(cat["sets"][0]["total"], 3)
        self.assertEqual(cat["langues"][0]["id"], "QCM/Anglais/Maison/cuisine")
        self.assertEqual(cat["langues"][0]["type"], "langue")

        rep = c.get("/qcm/hors-ligne/sets/?id=QCM/Physique/elec&id=QCM/Anglais/Maison/cuisine&id=QCM/inconnu")
        data = rep.get_json()
        self.assertEqual(rep.headers["Cache-Control"], "no-store")
        self.assertEqual(data["manquants"], ["QCM/inconnu"])
        qcm, langue = data["sets"]
        self.assertEqual([x["id"] for x in qcm["questions"]], ["ohm", "pairs", "carte"])
        self.assertIn('class="math', qcm["questions"][0]["enonce"])          # LaTeX pré-rendu
        self.assertIn("<img", qcm["questions"][2]["enonce"])                 # image du coffre
        self.assertEqual(qcm["progression"], {})
        self.assertEqual(qcm["quotas"], {"nouvelles_jour": None, "revisions_jour": None})
        self.assertEqual(langue["type"], "langue")
        saisies = [e for e in langue["exercices"] if e["forme"] == "saisie"]
        self.assertTrue(saisies and "reponses" in saisies[0] and "tolerant" in saisies[0])
        self.assertTrue(all("entree" in e and "max_par_session" in e for e in langue["exercices"]))

    def test_synchronisation_rejoint_la_progression(self):
        c = self.client()
        csrf = c.get("/qcm/hors-ligne/catalogue/").get_json()["csrf"]
        maintenant = datetime.now(timezone.utc)
        hier = (maintenant - timedelta(days=1)).isoformat()
        avant_hier = (maintenant - timedelta(days=2)).isoformat().replace("+00:00", "Z")
        exo = c.get("/qcm/hors-ligne/sets/?id=QCM/Anglais/Maison/cuisine").get_json()["sets"][0]["exercices"][0]["exo_id"]
        reponses = [
            {"uid": "a2", "set_id": "QCM/Physique/elec", "question_id": "ohm", "correct": True, "note": None,
             "duree_ms": 4000, "date": hier},
            {"uid": "a1", "set_id": "QCM/Physique/elec", "question_id": "ohm", "correct": False, "note": None,
             "duree_ms": 9000, "date": avant_hier},
            {"uid": "c1", "set_id": "QCM/Physique/elec", "question_id": "carte", "note": "facile", "date": hier},
            {"uid": "l1", "set_id": "QCM/Anglais/Maison/cuisine", "exo_id": exo, "note": 3, "duree_ms": 3000, "date": hier},
            {"uid": "x1", "set_id": "QCM/Physique/elec", "question_id": "inconnue", "correct": True, "date": hier},
            {"uid": "x2", "set_id": "QCM/Physique/elec", "question_id": "ohm", "correct": True, "date": "pas une date"},
        ]
        r = c.post("/qcm/hors-ligne/synchro/", data=json.dumps({"reponses": reponses}), content_type="application/json",
                   headers={"X-CSRF-Token": csrf})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        res = r.get_json()
        self.assertEqual(sorted(res["traitees"]), ["a1", "a2", "c1", "l1", "x1", "x2"])
        self.assertEqual(res["enregistrees"], 4)
        # Rejouées dans l'ordre chronologique : ratée avant-hier, réussie hier.
        p = QuizProgress.query.filter_by(user_id=self.user.id, set_id="QCM/Physique/elec", question_id="ohm").one()
        self.assertEqual((p.seen, p.correct, p.last_correct), (2, 1, True))
        self.assertEqual(p.last_review.date(), (horloge.now() - timedelta(days=1)).date())
        self.assertIsNotNone(p.stabilite)
        self.assertGreater(p.echeance, p.last_review)
        jours = {a.jour: a.reponses for a in Activite.query.filter_by(user_id=self.user.id)}
        self.assertEqual(sum(jours.values()), 4)
        self.assertEqual(len(jours), 2)  # avant-hier et hier
        lp = QuizProgress.query.filter_by(user_id=self.user.id, set_id="QCM/Anglais/Maison/cuisine", question_id=exo).one()
        self.assertIsNotNone(lp.decouverte)
        # Renvoi après une coupure : rien n'est compté deux fois.
        r = c.post("/qcm/hors-ligne/synchro/", data=json.dumps({"reponses": reponses[:2]}),
                   content_type="application/json", headers={"X-CSRF-Token": csrf})
        self.assertEqual(r.get_json()["enregistrees"], 0)
        # La progression revient dans le prochain téléchargement (dates avec fuseau).
        prog = c.get("/qcm/hors-ligne/sets/?id=QCM/Physique/elec").get_json()["sets"][0]["progression"]
        self.assertEqual(prog["ohm"]["seen"], 2)
        self.assertRegex(prog["ohm"]["last_review"], r"[+-]\d\d:\d\d$")
        self.assertEqual(prog["carte"]["box"], 2)  # « facile » : deux boîtes d'un coup
        # La page des stats et « Aujourd'hui » voient ces réponses.
        self.assertEqual(c.get("/qcm/aujourdhui/").status_code, 200)

    def test_synchronisation_exige_le_jeton_csrf_et_un_corps_valide(self):
        c = self.client()
        r = c.post("/qcm/hors-ligne/synchro/", data=json.dumps({"reponses": []}), content_type="application/json")
        self.assertEqual(r.status_code, 400)
        r = c.post("/qcm/hors-ligne/synchro/", data=json.dumps({"rien": 1}), content_type="application/json",
                   headers={"X-CSRF-Token": self.token})
        self.assertEqual(r.status_code, 400)

    def test_boutons_hors_ligne_de_la_page_des_qcm(self):
        c = self.client()
        page = c.get("/qcm/").get_data(as_text=True)
        self.assertIn('href="/qcm/hors-ligne/"', page)
        self.assertIn("/qcm/hors-ligne/#telecharger=QCM%2FPhysique%2Felec", page)
        self.assertIn("Garder sur cet appareil", page)
        # Dans l'application Android : sa page hors ligne embarquée
        page = c.get("/qcm/", headers={"User-Agent": APPLI}).get_data(as_text=True)
        self.assertIn("https://appassets.androidplatform.net/index.html", page)
        self.assertIn("https://appassets.androidplatform.net/connexion.html", page)


# ---------------------------------------------------------------------------
# Rappels par notification web
# ---------------------------------------------------------------------------
SUB = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "BPcle", "auth": "secret"}}


class PushTests(PariteTestCase):
    def test_cle_vapid_creee_au_premier_usage(self):
        c = self.client()
        pem = Path(self.tmp) / "vapid_prive.pem"
        self.assertFalse(pem.exists())
        d = c.get("/api/push/cle/").get_json()
        self.assertTrue(pem.is_file())
        self.assertRegex(d["cle"], r"^[A-Za-z0-9_-]{87}$")  # point P-256 non compressé, base64url
        self.assertEqual(d["abonnements"], {})
        self.assertEqual(c.get("/api/push/cle/").get_json()["cle"], d["cle"])  # gardée

    def test_abonnement_envoi_et_expiration(self):
        from pywebpush import WebPushException

        from hangar.qcm import rappels
        c = self.client()
        self.assertEqual(self.post_json(c, "/api/push/abonner/", {"subscription": {"endpoint": "http://x"}}).status_code, 400)
        r = self.post_json(c, "/api/push/abonner/", {"subscription": SUB, "heure": 23, "minute": 59})
        self.assertEqual(r.get_json(), {"ok": True, "heure": 23, "minute": 59})
        a = AbonnementPush.query.one()
        self.assertEqual((a.user_id, a.heure, a.minute, a.p256dh), (self.user.id, 23, 59, "BPcle"))
        self.assertIn(SUB["endpoint"], c.get("/api/push/cle/").get_json()["abonnements"])

        midi = horloge.now().replace(hour=12, minute=0, second=0, microsecond=0)
        with mock.patch("pywebpush.webpush") as wp:
            self.assertEqual(rappels.envoyer_rappels_dus(midi)["envoyes"], 0)  # pas encore l'heure
            a.heure, a.minute, a.dernier_envoi = 7, 30, None
            db.session.commit()
            c1 = rappels.envoyer_rappels_dus(midi)
            self.assertEqual(c1["envoyes"], 1)
            kwargs = wp.call_args.kwargs
            self.assertEqual(kwargs["subscription_info"]["endpoint"], SUB["endpoint"])
            charge = json.loads(kwargs["data"])
            # Les 3 questions jamais vues d'un set sans quota ne sont pas « à revoir » (piste 53 : compté
            # comme le tableau de bord) ; rien n'a encore été fait aujourd'hui, d'où ce rappel.
            self.assertEqual(charge["texte"], "Rien d'urgent : quelques nouveaux mots ?")
            self.assertEqual(charge["url"], "/qcm/aujourdhui/")
            self.assertTrue(kwargs["vapid_claims"]["sub"].startswith(("mailto:", "https://")))
            self.assertTrue(Path(kwargs["vapid_private_key"]).is_file())
            # Au plus un rappel par jour
            self.assertEqual(rappels.envoyer_rappels_dus(midi + timedelta(hours=3))["envoyes"], 0)
            self.assertEqual(wp.call_count, 1)
            # Le lendemain : abonnement expiré (410) → supprimé
            wp.side_effect = WebPushException("parti", response=mock.Mock(status_code=410))
            c2 = rappels.envoyer_rappels_dus(midi + timedelta(days=1))
            self.assertEqual(c2["expires"], 1)
        self.assertEqual(AbonnementPush.query.count(), 0)

    def test_rien_si_tout_est_fait(self):
        from hangar.qcm import rappels
        self.assertIsNone(rappels.texte_rappel({"a_revoir": 0, "serie": 3, "aujourdhui": 12}))
        self.assertEqual(rappels.texte_rappel({"a_revoir": 2, "serie": 3, "aujourdhui": 0}),
                         "2 à revoir · série de 3 jours à prolonger")
        self.assertEqual(rappels.texte_rappel({"a_revoir": 0, "serie": 0, "aujourdhui": 0}),
                         "Rien d'urgent : quelques nouveaux mots ?")
        db.session.add(AbonnementPush(user_id=self.user.id, endpoint=SUB["endpoint"], p256dh="k", auth="a", heure=0, minute=0))
        db.session.commit()
        with mock.patch.object(rappels, "etat_rappel", return_value={"a_revoir": 0, "qcm": 0, "langues": 0,
                                                                     "serie": 1, "aujourdhui": 5}), \
                mock.patch("pywebpush.webpush") as wp:
            c = rappels.envoyer_rappels_dus()
        self.assertEqual((c["rien"], c["envoyes"], wp.call_count), (1, 0, 0))
        self.assertIsNotNone(AbonnementPush.query.one().dernier_envoi)

    def test_tester_desabonner_et_commande(self):
        from hangar.qcm import cli
        c = self.client()
        self.post_json(c, "/api/push/abonner/", {"subscription": SUB, "heure": 0, "minute": 0})
        with mock.patch("pywebpush.webpush") as wp:
            self.assertEqual(self.post_json(c, "/api/push/tester/", {"endpoint": SUB["endpoint"]}).get_json(), {"ok": True})
            self.assertIn("(test)", json.loads(wp.call_args.kwargs["data"])["titre"])
        self.assertEqual(self.post_json(c, "/api/push/tester/", {"endpoint": "https://autre"}).status_code, 404)
        # « uv run hangar qcm-rappels » : l'abonnement de 00:00, déjà « envoyé » aujourd'hui à l'abonnement
        AbonnementPush.query.update({"dernier_envoi": None})
        db.session.commit()
        with mock.patch("pywebpush.webpush") as wp, mock.patch("builtins.print") as imprime:
            cli.cmd_rappels(Namespace(data=self.tmp))
        self.assertEqual(wp.call_count, 1)
        self.assertIn("1 envoyé(s)", imprime.call_args.args[0])
        self.assertEqual(self.post_json(c, "/api/push/desabonner/", {"endpoint": SUB["endpoint"]}).get_json(), {"ok": True})
        self.assertEqual(AbonnementPush.query.count(), 0)

    def test_fil_d_arriere_plan_envoie_les_rappels(self):
        from hangar import mailer
        appele, fin_du_tour = threading.Event(), threading.Event()

        def dormir(_secondes):
            fin_du_tour.set()
            threading.Event().wait()  # le fil (démon) reste ensuite endormi : plus rien après le test

        horloge = mock.Mock(sleep=dormir)
        with mock.patch("hangar.qcm.rappels.envoyer_rappels_dus", side_effect=lambda **kw: appele.set()), \
                mock.patch.object(mailer, "run_scheduled"), mock.patch.object(mailer, "send_pending"), \
                mock.patch("hangar.agenda.run_jobs"), mock.patch.object(mailer, "_worker", None), \
                mock.patch.object(mailer, "time", horloge):
            fil = mailer.start_worker(self.app, interval=3600)
            self.assertTrue(appele.wait(10))
            self.assertTrue(fin_du_tour.wait(10))
        self.assertTrue(fil.daemon)

    def test_panneau_de_la_page_aujourdhui(self):
        c = self.client()
        page = c.get("/qcm/aujourdhui/").get_data(as_text=True)
        self.assertIn('id="rappel-web"', page)
        self.assertIn("rappel_web.js", page)
        self.assertIn('data-cle-url="/api/push/cle/"', page)
        app = c.get("/qcm/aujourdhui/", headers={"User-Agent": APPLI}).get_data(as_text=True)
        self.assertRegex(app, r'id="rappel-web" hidden')
        self.assertIn("connexion.html", app)


# ---------------------------------------------------------------------------
# Application Android
# ---------------------------------------------------------------------------
class AppliAndroidTests(PariteTestCase):
    def connecter_appli(self, username="eleve", password="secret1", **kw):
        c = self.anonyme()
        r = c.post("/api/appareil/connexion/", data=json.dumps({"username": username, "password": password,
                                                                "appareil": "Google Pixel 8"}),
                   content_type="application/json", **kw)
        return r

    def test_de_bout_en_bout(self):
        # Connexion : identifiants → jeton (sans cookie ni jeton CSRF, comme Pont.java)
        self.assertEqual(self.connecter_appli(password="faux").status_code, 401)
        self.assertEqual(self.anonyme().post("/api/appareil/connexion/", data="pas du json",
                                             content_type="application/json").status_code, 400)
        r = self.connecter_appli()
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["joueur"], "eleve")
        self.assertNotIn("Set-Cookie", r.headers)
        bearer = {"Authorization": "Bearer " + data["jeton"], "Accept": "application/json"}
        from hangar.appareils import JetonAppareil
        j = JetonAppareil.query.one()
        self.assertEqual(j.nom, "Google Pixel 8")
        self.assertNotEqual(j.empreinte, data["jeton"])  # haché
        self.assertEqual(len(j.empreinte), 64)

        app = self.anonyme()
        # Rappel quotidien et widget
        r = app.get("/api/rappel/", headers=bearer)
        self.assertEqual(r.status_code, 200)
        etat = r.get_json()
        self.assertEqual(set(etat), {"a_revoir", "qcm", "langues", "nouvelles", "serie", "aujourdhui", "admin"})
        # 3 questions jamais vues, sans quota de nouvelles : rien de dû (piste 53, comme le tableau de bord).
        self.assertEqual((etat["a_revoir"], etat["qcm"], etat["langues"], etat["admin"]), (0, 0, 0, True))
        self.assertNotIn("Set-Cookie", r.headers)
        # QCM hors ligne embarqué : catalogue, sets, synchro (sans CSRF), images des questions
        cat = app.get("/qcm/hors-ligne/catalogue/", headers=bearer).get_json()
        self.assertEqual(cat["joueur"], "eleve")
        sets = app.get("/qcm/hors-ligne/sets/?id=QCM/Physique/elec", headers=bearer).get_json()["sets"]
        src = re.search(r'src="([^"]+)"', sets[0]["questions"][2]["enonce"]).group(1)
        with app.get(src, headers=bearer) as image:
            self.assertEqual(image.status_code, 200)
        r = app.post("/qcm/hors-ligne/synchro/", headers={**bearer, "Content-Type": "application/json"},
                     data=json.dumps({"reponses": [{"uid": "u1", "set_id": "QCM/Physique/elec", "question_id": "ohm",
                                                    "correct": True, "date": datetime.now(timezone.utc).isoformat()}]}))
        self.assertEqual(r.get_json(), {"traitees": ["u1"], "enregistrees": 1})
        # Le jeton n'ouvre que ces adresses
        self.assertEqual(app.get("/qcm/", headers=bearer).status_code, 403)
        self.assertEqual(app.get("/api/rappel/", headers={"Authorization": "Bearer faux"}).status_code, 401)

        # Session pour la WebView, liée au jeton
        web = self.anonyme()
        r = web.post("/api/appareil/session/", headers=bearer)
        self.assertEqual(r.get_json(), {"ok": True, "joueur": "eleve"})
        self.assertIn("session=", r.headers["Set-Cookie"])
        page = web.get("/qcm/", headers={"User-Agent": APPLI})
        self.assertEqual(page.status_code, 200)
        self.assertIn("QCM de révision", page.get_data(as_text=True))
        # Le profil liste l'appareil
        self.assertIn("Google Pixel 8", web.get("/settings/profile").get_data(as_text=True))
        # « Se déconnecter » de Hangar (bouton du menu, en POST), dans l'appli : ses réglages (qui révoquent le jeton)
        jeton_csrf = re.search(r'csrf: "([^"]+)"', page.get_data(as_text=True)).group(1)
        r = web.post("/logout", data={"_csrf": jeton_csrf}, headers={"User-Agent": APPLI})
        self.assertEqual(r.headers["Location"], "https://appassets.androidplatform.net/connexion.html")
        r = web.get("/logout", headers={"User-Agent": APPLI})  # jamais en GET : page de confirmation
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/deconnexion/"))
        self.assertEqual(web.get("/qcm/", headers={"User-Agent": APPLI}).status_code, 200)

        # Déconnexion de l'appli : jeton révoqué, session de la WebView fermée
        self.assertEqual(app.post("/api/appareil/deconnexion/", headers=bearer).get_json(), {"ok": True})
        self.assertEqual(app.get("/api/rappel/", headers=bearer).status_code, 401)
        self.assertEqual(app.post("/api/appareil/session/", headers=bearer).status_code, 401)
        r = web.get("/qcm/", headers={"User-Agent": APPLI})
        self.assertEqual(r.status_code, 302)
        # L'appli reconnaît la connexion à « /connexion… » pour rouvrir la session avec son jeton
        self.assertTrue(r.headers["Location"].startswith("/connexion/?next="), r.headers["Location"])
        self.assertEqual(JetonAppareil.query.count(), 0)

    def test_partage_vers_le_wiki_avec_le_jeton(self):
        """« Partager » vers l'appli : multipart sur /importer/ avec le jeton (Pont.envoyerMultipart)."""
        import io
        jeton = self.connecter_appli().get_json()["jeton"]
        r = self.anonyme().post("/importer/", headers={"Authorization": "Bearer " + jeton, "Accept": "application/json"},
                                data={"dossier": "Importés", "claude": "1", "chemins": "Cours partagé.md",
                                      "fichiers": (io.BytesIO("# Cours partagé\n\nTexte.".encode()), "Cours partagé.md")},
                                content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        res = r.get_json()["results"]
        self.assertTrue(res[0]["ok"])
        self.assertTrue((Path(self.app.config["WIKI_DIR"]) / "Importés" / "Cours partagé.md").is_file())
        # Un compte invité n'importe rien (403), même avec un jeton valide
        invite = User(username="invite", email="invite@hangar.local", role="guest")
        invite.set_password("secret1")
        db.session.add(invite)
        db.session.commit()
        jeton = self.connecter_appli(username="invite").get_json()["jeton"]
        r = self.anonyme().post("/importer/", headers={"Authorization": "Bearer " + jeton, "Accept": "application/json"},
                                data={"dossier": "Importés", "fichiers": (io.BytesIO(b"# x"), "x.md")},
                                content_type="multipart/form-data")
        self.assertEqual(r.status_code, 403)

    def test_sans_jeton(self):
        c = self.anonyme()
        self.assertEqual(c.post("/api/appareil/deconnexion/").status_code, 400)  # pas de jeton (et pas de CSRF)
        self.assertEqual(c.get("/api/rappel/").status_code, 401)
        st = c.get("/api/status/").get_json()
        self.assertEqual(st["status"], "ok")
        datetime.fromisoformat(st["time"])
        # Navigateur ordinaire : la connexion reste /login
        r = c.get("/qcm/")
        self.assertTrue(urlpath(r.headers["Location"]).startswith("/login"))

    def test_revocation_depuis_le_profil(self):
        from hangar.appareils import JetonAppareil
        self.connecter_appli()
        self.connecter_appli(username="autre")
        mien = JetonAppareil.query.filter_by(user_id=self.user.id).one()
        sien = JetonAppareil.query.filter_by(user_id=self.autre.id).one()
        c = self.client()
        self.assertEqual(c.post(f"/compte/appareils/{sien.id}/revoquer/", data={"_csrf": self.token}).status_code, 404)
        r = c.post(f"/compte/appareils/{mien.id}/revoquer/", data={"_csrf": self.token})
        self.assertEqual(r.headers["Location"], "/settings/profile#appareils")
        self.assertEqual(JetonAppareil.query.count(), 1)

    def test_revocation_au_changement_de_mot_de_passe_et_a_la_desactivation(self):
        from hangar.appareils import JetonAppareil
        jeton = self.connecter_appli().get_json()["jeton"]
        bearer = {"Authorization": "Bearer " + jeton}
        self.assertEqual(self.anonyme().get("/api/rappel/", headers=bearer).status_code, 200)
        c = self.client()
        r = c.post("/settings/password", data={"current": "secret1", "new": "nouveau1", "confirm": "nouveau1",
                                               "_csrf": self.token})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(JetonAppareil.query.filter_by(user_id=self.user.id).count(), 0)
        self.assertEqual(self.anonyme().get("/api/rappel/", headers=bearer).status_code, 401)
        # Mot de passe réinitialisé par un admin
        self.connecter_appli(username="autre")
        c.post(f"/team/{self.autre.id}/admin", data={"action": "reset", "_csrf": self.token})
        self.assertEqual(JetonAppareil.query.filter_by(user_id=self.autre.id).count(), 0)
        # Compte désactivé
        db.session.refresh(self.autre)
        self.autre.set_password("secret1")
        db.session.commit()
        jeton = self.connecter_appli(username="autre").get_json()["jeton"]
        c.post(f"/team/{self.autre.id}/admin", data={"action": "toggle", "_csrf": self.token})
        self.assertEqual(JetonAppareil.query.filter_by(user_id=self.autre.id).count(), 0)
        self.assertEqual(self.anonyme().get("/api/rappel/", headers={"Authorization": "Bearer " + jeton}).status_code, 401)
        self.assertEqual(self.connecter_appli(username="autre").status_code, 401)


def urlpath(location):
    from urllib.parse import urlsplit
    return urlsplit(location).path + ("?" + urlsplit(location).query if urlsplit(location).query else "")


# ---------------------------------------------------------------------------
# Blocage des connexions
# ---------------------------------------------------------------------------
class BlocageTests(PariteTestCase):
    def essai(self, c, password, ip="10.0.0.5", login="eleve"):
        page = c.get("/login", environ_base={"REMOTE_ADDR": ip}).get_data(as_text=True)
        token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        return c.post("/login", data={"login": login, "password": password, "_csrf": token},
                      environ_base={"REMOTE_ADDR": ip}, follow_redirects=False)

    def test_blocage_apres_cinq_echecs(self):
        c = self.anonyme()
        for _ in range(5):
            r = self.essai(c, "faux")
            self.assertEqual(r.status_code, 200)
            self.assertIn("Identifiant ou mot de passe incorrect", r.get_data(as_text=True))
        r = self.essai(c, "secret1")  # même le bon mot de passe est refusé
        self.assertEqual(r.status_code, 200)
        self.assertIn("connexion bloquée pendant encore 30 minutes", r.get_data(as_text=True))
        # Une autre adresse, ou un autre compte depuis cette adresse, peut se connecter.
        self.assertEqual(self.essai(self.anonyme(), "secret1", ip="10.0.0.6").status_code, 302)
        self.assertEqual(self.essai(self.anonyme(), "secret1", login="autre").status_code, 302)
        # L'application Android est bloquée de la même façon.
        r = self.anonyme().post("/api/appareil/connexion/", data=json.dumps({"username": "eleve", "password": "secret1"}),
                                content_type="application/json", environ_base={"REMOTE_ADDR": "10.0.0.5"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("trop d'essais", r.get_json()["error"])
        # Admin : la liste sur son profil, puis le déblocage
        admin = self.client(environ_base={"REMOTE_ADDR": "10.0.0.9"})
        profil = admin.get("/settings/profile").get_data(as_text=True)
        self.assertIn("Connexions bloquées", profil)
        self.assertIn("@eleve", profil)
        admin.post("/compte/connexions/debloquer/", data={"identifiant": "eleve", "ip": "10.0.0.5", "_csrf": self.token})
        self.assertEqual(self.essai(c, "secret1").status_code, 302)

    def test_reussite_remet_a_zero_et_expiration(self):
        from hangar import blocage_connexion as b
        c = self.anonyme()
        for _ in range(4):
            self.essai(c, "faux")
        self.assertEqual(self.essai(c, "secret1").status_code, 302)  # remise à zéro
        c = self.anonyme()
        for _ in range(4):
            self.essai(c, "faux")
        self.assertEqual(self.essai(c, "secret1").status_code, 302)
        for _ in range(5):
            b.noter_echec("eleve", "10.0.0.7")
        self.assertEqual(b.bloque("Eleve", "10.0.0.7"), 30)
        self.assertEqual(b.bloque("eleve", "10.0.0.7", horloge.now() + timedelta(minutes=31)), 0)
        # Après 30 minutes, une nouvelle série repart de zéro
        b.noter_echec("eleve", "10.0.0.7", horloge.now() + timedelta(minutes=31))
        self.assertEqual(b.bloque("eleve", "10.0.0.7", horloge.now() + timedelta(minutes=31)), 0)

    def test_blocage_par_adresse_et_commande(self):
        from hangar import blocage_connexion as b
        from hangar.qcm import cli
        for i in range(b.LIMITE_IP):
            b.noter_echec(f"compte{i}", "10.0.0.8")
        self.assertGreater(b.bloque("eleve", "10.0.0.8"), 0)  # l'adresse entière
        self.assertEqual(b.bloque("eleve", "10.0.0.9"), 0)
        self.assertTrue(any(x["adresse"] for x in b.bloques()))
        with mock.patch("builtins.print"):
            cli.cmd_debloquer(Namespace(data=self.tmp, identifiant=None, ip="10.0.0.8"))
        self.assertEqual(b.bloque("eleve", "10.0.0.8"), 0)

    def test_nouveau_mot_de_passe_leve_le_blocage(self):
        from hangar import blocage_connexion as b
        for _ in range(5):
            b.noter_echec("autre", "10.0.0.5")
        self.assertTrue(b.bloque("autre", "10.0.0.5"))
        self.autre.set_password("nouveau1")
        db.session.commit()
        self.assertEqual(b.bloque("autre", "10.0.0.5"), 0)

    def test_ip_derriere_un_proxy(self):
        from hangar import blocage_connexion as b
        with self.app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"},
                                           headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.4", "CF-Connecting-IP": "198.51.100.2"}):
            self.assertEqual(b.ip_client(), "127.0.0.1")  # par défaut, les en-têtes ne sont pas crus
            with mock.patch.dict("os.environ", {"HANGAR_BEHIND_PROXY": "1"}):
                self.assertEqual(b.ip_client(), "203.0.113.4")
            with mock.patch.dict("os.environ", {"HANGAR_CLOUDFLARE": "1"}):
                self.assertEqual(b.ip_client(), "198.51.100.2")


# ---------------------------------------------------------------------------
# Alias des adresses de Revision
# ---------------------------------------------------------------------------
class AliasTests(PariteTestCase):
    def test_alias(self):
        c = self.client()
        attendus = {
            "/aujourdhui/": "/qcm/aujourdhui/",
            "/connexion/?next=/qcm/": "/login?next=%2Fqcm%2F",
            "/compte/": "/settings/profile",
            "/compte/mot-de-passe/": "/settings/profile#password",
            "/inscription/": "/signup",
            "/utilisateurs/": "/team/",
        }
        for ancien, nouveau in attendus.items():
            r = c.get(ancien)
            self.assertIn(r.status_code, (301, 302), ancien)
            self.assertEqual(r.headers["Location"], nouveau, ancien)
        self.assertEqual(c.get("/aujourdhui/", follow_redirects=True).status_code, 200)
        # /deconnexion/ : comme dans Revision, on ne se déconnecte qu'en POST ; en GET, un bouton de confirmation
        r = c.get("/deconnexion/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('<form method="post" action="/logout"', r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
