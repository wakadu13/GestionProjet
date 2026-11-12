"""Réseau et abonnement .ics (revue v3, groupe « réseau-abonnement ») :

- avec --lan-https, l'adresse d'abonnement donnée sur le PC est l'adresse https du téléphone (la même que
  Réglages › Profil), sans conseil « --host 0.0.0.0 » (qui servirait l'agenda en clair) ;
- /setup refusé dès qu'un proxy ou un tunnel est déclaré (--behind-proxy, --cloudflare), même s'il n'envoie
  aucun en-tête X-Forwarded-* ; avec --lan-https seul, ce PC (sans en-tête) peut toujours installer ;
- jeton d'abonnement : masqué dans le journal des erreurs, régénéré par « Déconnecter mes autres appareils »,
  gardé (et la page le dit) au simple changement de mot de passe.

Lancer : uv run python -m unittest tests.test_reseau_abonnement -v
"""
import os
import re
import shutil
import tempfile
import unittest
from argparse import Namespace
from unittest import mock

from hangar import agenda as A
from hangar import create_app, reseau_local
from hangar.extensions import db
from hangar.models import AgendaSettings, User

IP = "192.168.1.20"
SANS_RESEAU = {"HANGAR_BEHIND_PROXY": "", "HANGAR_CLOUDFLARE": "", "HANGAR_LAN_HTTPS": "", "HANGAR_HTTPS": ""}


class Base(unittest.TestCase):
    options = None  # Namespace des options réseau de « hangar run », ou None

    def setUp(self):
        from hangar import cli
        self.tmp = tempfile.mkdtemp(prefix="hangar-reseau-abo-")
        with mock.patch.dict(os.environ, SANS_RESEAU):
            if self.options is not None:
                cli.appliquer_options_reseau(self.options)
            self.app = create_app(self.tmp, testing=True)

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def creer_compte(self):
        with self.app.app_context():
            u = User(username="eleve", email="eleve@hangar.local", role="admin", first_name="Marie")
            u.set_password("secret-eleve")
            db.session.add(u)
            db.session.commit()
            return u.id

    def connecte(self, base_url="http://127.0.0.1:5000"):
        c = self.app.test_client()
        page = c.get("/login", base_url=base_url).get_data(as_text=True)
        self.csrf = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        r = c.post("/login", data={"_csrf": self.csrf, "login": "eleve", "password": "secret-eleve"},
                   base_url=base_url)
        self.assertEqual(r.status_code, 302)
        return c

    def jeton(self, uid):
        with self.app.app_context():
            s = db.session.get(AgendaSettings, uid)
            return s.feed_token if s else None

    def creer_jeton(self, uid):
        with self.app.app_context():
            return A.regenerate_feed_token(db.session.get(User, uid))


# ---------------------------------------------------------------------------
# Adresse d'abonnement avec --lan-https
# ---------------------------------------------------------------------------

class AdresseAbonnement(Base):
    def setUp(self):
        super().setUp()
        self.uid = self.creer_compte()
        self.tok = self.creer_jeton(self.uid)

    def page(self, base_url="http://127.0.0.1:5000"):
        c = self.connecte(base_url)
        with mock.patch.object(reseau_local, "ip_reseau", return_value=IP):
            return c.get("/agenda/imports", base_url=base_url).get_data(as_text=True)

    def test_lan_https_adresse_https_du_telephone(self):
        # Ce que pose cmd_run avec --lan-https quand Caddy a démarré
        self.app.config.update(HANGAR_HOST="127.0.0.1",
                               PARTAGE={"host": "127.0.0.1", "port": 5000, "https_port": 5443})
        page = self.page()
        self.assertIn(f"https://{IP}:5443/agenda/flux/{self.tok}.ics", page)
        self.assertNotIn(f"http://{IP}:5000/agenda/flux/", page)
        self.assertNotIn("0.0.0.0", page)
        self.assertNotIn("n’accepte que les connexions de cet ordinateur", page)
        self.assertIn("certificat", page)  # le téléphone doit faire confiance à l'autorité de Caddy

    def test_meme_adresse_que_le_profil(self):
        self.app.config.update(HANGAR_HOST="127.0.0.1",
                               PARTAGE={"host": "127.0.0.1", "port": 5000, "https_port": 5443})
        with self.app.test_request_context("/", base_url="http://127.0.0.1:5000"):
            from flask import request
            with mock.patch.object(reseau_local, "ip_reseau", return_value=IP):
                tel = reseau_local.adresse_telephone(self.app, request)
        self.assertIn(tel["url"].rstrip("/") + f"/agenda/flux/{self.tok}.ics", self.page())

    def test_partage_http_sur_le_reseau(self):
        self.app.config.update(HANGAR_HOST="0.0.0.0", PARTAGE={"host": "0.0.0.0", "port": 5003, "https_port": None})
        page = self.page("http://127.0.0.1:5003")
        self.assertIn(f"http://{IP}:5003/agenda/flux/{self.tok}.ics", page)
        self.assertNotIn("n’accepte que les connexions de cet ordinateur", page)

    def test_local_seulement_conseille_https_et_non_0000(self):
        self.app.config.update(HANGAR_HOST="127.0.0.1", PARTAGE={"host": "127.0.0.1", "port": 5000, "https_port": None})
        page = self.page()
        self.assertIn("n’accepte que les connexions de cet ordinateur", page)
        self.assertIn("partager-en-https.bat", page)
        self.assertNotIn("--host 0.0.0.0", page)

    def test_lan_https_sans_reseau(self):
        self.app.config.update(HANGAR_HOST="127.0.0.1",
                               PARTAGE={"host": "127.0.0.1", "port": 5000, "https_port": 5443})
        c = self.connecte()
        with mock.patch.object(reseau_local, "ip_reseau", return_value=None):
            page = c.get("/agenda/imports", base_url="http://127.0.0.1:5000").get_data(as_text=True)
        self.assertNotIn("0.0.0.0", page)
        self.assertNotIn("partager-en-https.bat", page)  # déjà en HTTPS : le conseil serait faux
        self.assertIn("aucun réseau", page)

    def test_lan_https_caddy_en_echec(self):
        # --lan-https demandé, mais Caddy n'a pas démarré (port 5443 pris, Caddy introuvable) : la cause est
        # Caddy, pas l'option de lancement, que l'utilisateur vient justement d'utiliser.
        self.app.config.update(HANGAR_HOST="127.0.0.1",
                               PARTAGE={"host": "127.0.0.1", "port": 5000, "https_port": None, "lan_https": True})
        page = self.page()
        self.assertIn("Le partage HTTPS n’a pas démarré", page)
        self.assertIn("caddy.log", page)
        self.assertNotIn("partager-en-https.bat", page)
        self.assertNotIn("n’accepte que les connexions de cet ordinateur", page)


# ---------------------------------------------------------------------------
# /setup derrière un proxy déclaré
# ---------------------------------------------------------------------------

class SetupDerriereUnProxy(Base):
    options = Namespace(lan_https=False, cloudflare=False, behind_proxy=True, https=True)

    def test_refuse_sans_en_tete_de_transfert(self):
        # nginx réglé au minimum (proxy_pass seul) : chaque visiteur arrive en 127.0.0.1, sans X-Forwarded-For
        self.assertTrue(self.app.config["HANGAR_PROXY"])
        r = self.app.test_client().get("/setup")
        self.assertEqual(r.status_code, 403)
        self.assertIn("create-admin", r.get_data(as_text=True))
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["_csrf"] = "j"
        r = c.post("/setup", data={"_csrf": "j", "username": "intrus", "password": "mot-de-passe-long"})
        self.assertEqual(r.status_code, 403)
        with self.app.app_context():
            self.assertEqual(User.query.count(), 0)


class SetupDerriereCloudflare(Base):
    options = Namespace(lan_https=False, cloudflare=True, behind_proxy=False, https=False)

    def test_refuse_sans_en_tete(self):
        self.assertEqual(self.app.test_client().get("/setup").status_code, 403)


class SetupAvecLanHttps(Base):
    options = Namespace(lan_https=True, cloudflare=False, behind_proxy=False, https=False)

    def test_ce_pc_peut_installer(self):
        self.assertTrue(self.app.config["HANGAR_PROXY_LAN"])
        self.assertEqual(self.app.test_client().get("/setup").status_code, 200)  # http://127.0.0.1, sans Caddy

    def test_appareil_du_wifi_par_caddy_refuse(self):
        r = self.app.test_client().get("/setup", headers={"X-Forwarded-For": "192.168.1.30"})
        self.assertEqual(r.status_code, 403)


class SetupLanHttpsEtProxyInternet(Base):
    options = Namespace(lan_https=True, cloudflare=False, behind_proxy=True, https=False)

    def test_refuse(self):
        self.assertFalse(self.app.config["HANGAR_PROXY_LAN"])
        self.assertEqual(self.app.test_client().get("/setup").status_code, 403)


# ---------------------------------------------------------------------------
# Jeton d'abonnement : journal des erreurs, déconnexion des appareils, mot de passe
# ---------------------------------------------------------------------------

class JetonAbonnement(Base):
    def setUp(self):
        super().setUp()
        self.uid = self.creer_compte()

    def test_masque_dans_le_journal_des_erreurs(self):
        from hangar.wiki.erreurs import masquer
        tok = "AbCdEf0123456789-_AbCdEf0123456789-_AbCdEf0"
        for texte in (f"/agenda/flux/{tok}.ics", f"/agenda/flux/{tok}.ics?x=1",
                      f"GET http://192.168.1.20:5000/agenda/flux/{tok}.ics"):
            t = masquer(texte)
            self.assertNotIn(tok, t)
            self.assertIn("/agenda/flux/", t)
        self.assertEqual(masquer("/agenda/flux/regenerer"), "/agenda/flux/regenerer")
        self.assertEqual(masquer("/agenda/flux/couper"), "/agenda/flux/couper")

    def test_erreur_500_sur_le_flux(self):
        from hangar.wiki.models import ErreurServeur
        tok = self.creer_jeton(self.uid)
        self.app.config["PROPAGATE_EXCEPTIONS"] = False
        with mock.patch.object(A, "export_ics", side_effect=RuntimeError("panne")):
            r = self.app.test_client().get(f"/agenda/flux/{tok}.ics")
        self.assertEqual(r.status_code, 500)
        with self.app.app_context():
            e = ErreurServeur.query.one()
            self.assertIn("/agenda/flux/", e.chemin)
            self.assertNotIn(tok, e.chemin + e.message + e.trace)

    def test_deconnecter_mes_autres_appareils_change_l_adresse(self):
        ancien = self.creer_jeton(self.uid)
        c = self.connecte()
        r = c.post("/settings/sessions/fermer", data={"_csrf": self.csrf}, follow_redirects=True,
                   base_url="http://127.0.0.1:5000")
        nouveau = self.jeton(self.uid)
        self.assertTrue(nouveau)
        self.assertNotEqual(ancien, nouveau)
        anon = self.app.test_client()
        self.assertEqual(anon.get(f"/agenda/flux/{ancien}.ics").status_code, 404)
        self.assertEqual(anon.get(f"/agenda/flux/{nouveau}.ics").status_code, 200)
        self.assertIn("abonnement", r.get_data(as_text=True).lower())

    def test_deconnecter_sans_abonnement_n_en_cree_pas(self):
        c = self.connecte()
        c.post("/settings/sessions/fermer", data={"_csrf": self.csrf}, base_url="http://127.0.0.1:5000")
        self.assertIsNone(self.jeton(self.uid))

    def test_mot_de_passe_garde_l_abonnement_et_le_dit(self):
        ancien = self.creer_jeton(self.uid)
        c = self.connecte()
        page = c.get("/settings/profile", base_url="http://127.0.0.1:5000").get_data(as_text=True)
        # La page dit ce que coupe chaque bouton
        self.assertIn("ne change pas l’adresse d’abonnement", page)
        self.assertIn("ce bouton la change aussi", page)
        # La boîte de confirmation le redit : on clique souvent pour fermer une session oubliée sur un autre PC
        confirm = re.search(r'action="/settings/sessions/fermer"[^>]*data-confirm="([^"]*)"', page)
        self.assertIsNotNone(confirm)
        self.assertIn("adresse d’abonnement", confirm.group(1))
        r = c.post("/settings/password", data={"_csrf": self.csrf, "current": "secret-eleve",
                                                "new": "nouveau-secret", "confirm": "nouveau-secret"},
                   follow_redirects=True, base_url="http://127.0.0.1:5000")
        self.assertEqual(self.jeton(self.uid), ancien)
        self.assertIn("Déconnecter mes autres appareils", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
