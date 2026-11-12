"""Comptes et sécurité (vague v2c) : sessions et cookies « Rester connecté » révoqués au changement de mot de
passe (piste 35), e-mails des membres vers l'extérieur (piste 37), connexion derrière un proxy ou un tunnel
et blocage des essais (piste 43), valeurs sensibles masquées dans /erreurs/ (piste 55), adresse de retour
après la connexion (piste 64).

Lancer : uv run python -m unittest tests.test_securite_comptes -v
Données synthétiques uniquement.
"""
import json
import shutil
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

from hangar import heure as horloge  # heure de l'agenda (Paris), pas celle de la machine
from hangar import create_app
from hangar.extensions import db
from hangar.models import EmailMessage, MailConfig, User, Workspace, now

CSRF = "jeton-de-test"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-secu-comptes-")
        self.app = create_app(self.tmp, testing=True)
        with self.app.app_context():
            admin = User(username="chef", email="chef@hangar.local", role="admin", first_name="Jean",
                         contact_email="chef@exemple.fr")
            admin.set_password("secret-chef")
            membre = User(username="eleve", email="eleve@hangar.local", role="member", first_name="Marie",
                          contact_email="eleve@exemple.fr")
            membre.set_password("secret-eleve")
            db.session.add_all([admin, membre])
            db.session.commit()
            self.admin_id, self.membre_id = admin.id, membre.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self, **kw):
        c = self.app.test_client(**kw)
        with c.session_transaction() as s:
            s["_csrf"] = CSRF
        return c

    def connecter(self, login="eleve", password="secret-eleve", remember=True, c=None, **kw):
        c = c or self.client()
        data = {"_csrf": CSRF, "login": login, "password": password}
        if remember:
            data["remember"] = "1"
        r = c.post("/login", data=data, **kw)
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:500])
        return c

    @staticmethod
    def cookie(c, nom):
        ck = c.get_cookie(nom)
        return ck.value if ck else None

    def connecte(self, c):
        """Le client est-il connecté ? (page du profil sans renvoi vers la connexion)"""
        return c.get("/settings/profile").status_code == 200


# ---------------------------------------------------------------------------
# Piste 35 : changer de mot de passe ferme les autres sessions et les cookies « Rester connecté »
# ---------------------------------------------------------------------------
class SessionsTests(Base):
    def test_ancien_cookie_rester_connecte_refuse_apres_changement(self):
        a = self.connecter()
        vieux = self.cookie(a, "remember_token")
        self.assertTrue(vieux)
        b = self.connecter(remember=False)  # un autre navigateur, resté connecté
        r = a.post("/settings/password", data={"_csrf": CSRF, "current": "secret-eleve", "new": "nouveau-secret",
                                               "confirm": "nouveau-secret"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(self.connecte(a), "le navigateur qui change le mot de passe reste connecté")
        self.assertNotEqual(self.cookie(a, "remember_token"), vieux, "son cookie « Rester connecté » est renouvelé")
        self.assertFalse(self.connecte(b), "l'autre navigateur est déconnecté")
        c = self.app.test_client()
        c.set_cookie("remember_token", vieux)
        self.assertFalse(self.connecte(c), "l'ancien cookie « Rester connecté » ne marche plus")

    def test_sans_rester_connecte_pas_de_cookie_apres_changement(self):
        a = self.connecter(remember=False)
        a.post("/settings/password", data={"_csrf": CSRF, "current": "secret-eleve", "new": "nouveau-secret",
                                           "confirm": "nouveau-secret"})
        self.assertTrue(self.connecte(a))
        self.assertIsNone(self.cookie(a, "remember_token"))

    def test_mot_de_passe_reinitialise_hors_session(self):
        a = self.connecter()
        vieux = self.cookie(a, "remember_token")
        with self.app.app_context():  # réinitialisation par un admin ou en ligne de commande
            u = db.session.get(User, self.membre_id)
            u.set_password("autre-secret")
            db.session.commit()
        self.assertFalse(self.connecte(a))
        c = self.app.test_client()
        c.set_cookie("remember_token", vieux)
        self.assertFalse(self.connecte(c))
        self.assertIsNone(self.cookie(c, "remember_token"), "le cookie périmé est effacé")

    def test_deconnecter_mes_autres_appareils(self):
        from hangar.appareils import JetonAppareil
        a = self.connecter()
        b = self.connecter()
        vieux_b = self.cookie(b, "remember_token")
        with self.app.app_context():
            _, cle = JetonAppareil.creer(db.session.get(User, self.membre_id), "Téléphone")
            db.session.commit()
        r = a.post("/settings/sessions/fermer", data={"_csrf": CSRF})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(self.connecte(a))
        self.assertFalse(self.connecte(b))
        c = self.app.test_client()
        c.set_cookie("remember_token", vieux_b)
        self.assertFalse(self.connecte(c))
        # L'application Android garde son jeton (sauf demande)
        r = self.app.test_client().post("/api/appareil/session/", headers={"Authorization": f"Bearer {cle}"})
        self.assertEqual(r.status_code, 200)
        # … sauf si on le demande
        a.post("/settings/sessions/fermer", data={"_csrf": CSRF, "appareils": "1"})
        self.assertTrue(self.connecte(a))
        r = self.app.test_client().post("/api/appareil/session/", headers={"Authorization": f"Bearer {cle}"})
        self.assertEqual(r.status_code, 401)

    def test_ancien_format_de_session_refuse_sans_erreur(self):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(self.membre_id)  # session écrite par l'ancienne version
        r = c.get("/settings/profile")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = "pas-un-nombre:abc"
        self.assertEqual(c.get("/settings/profile").status_code, 302)

    def test_cookie_d_un_compte_supprime_n_ouvre_pas_le_suivant(self):
        a = self.connecter()
        vieux = self.cookie(a, "remember_token")
        with self.app.app_context():
            u = db.session.get(User, self.membre_id)
            db.session.delete(u)
            db.session.commit()
            nouveau = User(id=self.membre_id, username="nouveau", email="nouveau@hangar.local", role="admin")
            nouveau.set_password("secret-eleve")  # même mot de passe, même numéro
            db.session.add(nouveau)
            db.session.commit()
        c = self.app.test_client()
        c.set_cookie("remember_token", vieux)
        self.assertFalse(self.connecte(c))

    def test_cookie_rester_connecte_samesite(self):
        c = self.client()
        r = c.post("/login", data={"_csrf": CSRF, "login": "eleve", "password": "secret-eleve", "remember": "1"})
        entetes = [h for h in r.headers.getlist("Set-Cookie") if h.startswith("remember_token=")]
        self.assertTrue(entetes)
        self.assertIn("SameSite=Lax", entetes[0])
        self.assertIn("HttpOnly", entetes[0])
        self.assertNotIn("Secure", entetes[0])  # servi en http : pas de Secure (sinon la connexion casserait)

    def test_base_de_la_version_precedente(self):
        """Une base sans les nouvelles colonnes reprend sans rien faire : comptes, réglages et connexion."""
        from sqlalchemy import text
        with self.app.app_context():
            for table, col in (("user", "session_version"), ("workspace", "login_accounts"),
                               ("mail_config", "members_external")):
                MailConfig.get()
                db.session.execute(text(f'ALTER TABLE "{table}" DROP COLUMN "{col}"'))
            db.session.commit()
            db.session.remove()
            db.engine.dispose()
        self.app = create_app(self.tmp, testing=True)
        with self.app.app_context():
            self.assertEqual(db.session.get(User, self.membre_id).session_version, 0)
            self.assertTrue(Workspace.get().login_accounts)
            self.assertFalse(MailConfig.get().members_external)
        a = self.connecter()
        self.assertTrue(self.connecte(a))
        a.post("/settings/sessions/fermer", data={"_csrf": CSRF})
        self.assertTrue(self.connecte(a))


# ---------------------------------------------------------------------------
# Piste 37 : e-mails des membres vers l'extérieur
# ---------------------------------------------------------------------------
class MailMembresTests(Base):
    def composer(self, c, others="", user_ids=(), sujet="Sujet"):
        data = {"_csrf": CSRF, "subject": sujet, "body": "Bonjour", "others": others}
        if user_ids:
            data["user_ids"] = [str(i) for i in user_ids]
        return c.post("/mail/compose", data=data, follow_redirects=True)

    def nb(self):
        with self.app.app_context():
            return EmailMessage.query.count()

    def test_membre_ne_peut_pas_ecrire_dehors_par_defaut(self):
        c = self.connecter()
        r = self.composer(c, others="a@ext.example, b@ext.example")
        self.assertEqual(self.nb(), 0)
        self.assertIn("hors de l’équipe", r.get_data(as_text=True))
        # vers l'équipe : toujours possible (adresse d'un membre, même saisie à la main)
        self.composer(c, user_ids=[self.admin_id])
        self.assertEqual(self.nb(), 1)
        self.composer(c, others="chef@exemple.fr")
        self.assertEqual(self.nb(), 2)

    def test_reglage_et_plafonds(self):
        with self.app.app_context():
            MailConfig.get().members_external = True
            db.session.commit()
        c = self.connecter()
        self.composer(c, others="a@ext.example, b@ext.example")
        self.assertEqual(self.nb(), 2)
        trop = ", ".join(f"x{i}@ext.example" for i in range(21))
        r = self.composer(c, others=trop)
        self.assertEqual(self.nb(), 2)
        self.assertIn("20 destinataires", r.get_data(as_text=True))
        # plafond sur 24 h (e-mails manuels et bilans de ce compte)
        with self.app.app_context():
            for i in range(98):
                db.session.add(EmailMessage(to_address=f"y{i}@ext.example", subject="s", kind="manual",
                                            status="outbox", created_by_id=self.membre_id,
                                            created_at=now() - timedelta(hours=2)))
            db.session.add(EmailMessage(to_address="vieux@ext.example", subject="s", kind="bilan", status="outbox",
                                        created_by_id=self.membre_id, created_at=now() - timedelta(hours=30)))
            db.session.commit()
        r = self.composer(c, others="c@ext.example, d@ext.example, e@ext.example")
        self.assertEqual(self.nb(), 101, "100 par jour au plus : rien n'est envoyé")
        self.assertIn("24 heures", r.get_data(as_text=True))

    def test_envoi_refuse_garde_destinataires_et_aide_adaptee(self):
        c = self.connecter()
        page = c.get("/mail/compose").get_data(as_text=True)
        self.assertIn("un administrateur doit l’autoriser dans Réglages › E-mails", page)
        self.assertNotIn("interlocuteurs extérieurs", page)
        r = self.composer(c, others="a@ext.example, b@ext.example", user_ids=[self.admin_id])
        page = r.get_data(as_text=True)
        self.assertEqual(self.nb(), 0)
        self.assertIn('value="a@ext.example, b@ext.example"', page)
        self.assertRegex(page, r'name="user_ids" value="%d"\s+checked' % self.admin_id)
        with self.app.app_context():
            MailConfig.get().members_external = True
            db.session.commit()
        self.assertIn("interlocuteurs extérieurs", c.get("/mail/compose").get_data(as_text=True))

    def test_administrateur_sans_plafond(self):
        c = self.connecter("chef", "secret-chef")
        self.composer(c, others=", ".join(f"x{i}@ext.example" for i in range(25)))
        self.assertEqual(self.nb(), 25)

    def creer_bilan(self):

        from hangar.bilan import compute_bilan
        from hangar.models import BilanReport
        with self.app.app_context():
            fin = horloge.today()
            data = compute_bilan([], fin - timedelta(days=7), fin, "Semaine")
            r = BilanReport(title="Bilan", data_json=json.dumps(data, ensure_ascii=False, default=str),
                            period_start=fin - timedelta(days=7), period_end=fin, created_by_id=self.membre_id)
            db.session.add(r)
            db.session.commit()
            return r.id

    def test_bilan_memes_regles_et_reponse(self):
        rid = self.creer_bilan()
        c = self.connecter()
        c.post(f"/bilan/reports/{rid}/send", data={"_csrf": CSRF, "others": "a@ext.example"})
        self.assertEqual(self.nb(), 0)
        c.post(f"/bilan/reports/{rid}/send", data={"_csrf": CSRF, "user_ids": [str(self.admin_id)]})
        with self.app.app_context():
            m = EmailMessage.query.one()
            self.assertEqual(m.reply_to, "eleve@exemple.fr")  # les réponses vont à l'auteur, pas au Gmail de l'élève

    def test_bilan_message_selon_le_vrai_mode(self):
        rid = self.creer_bilan()
        with self.app.app_context():
            cfg = MailConfig.get()
            cfg.enabled, cfg.mode, cfg.smtp_host = False, "smtp", "smtp.exemple.org"
            db.session.commit()
        c = self.connecter()
        r = c.post(f"/bilan/reports/{rid}/send", data={"_csrf": CSRF, "user_ids": [str(self.admin_id)]},
                   follow_redirects=True)
        page = r.get_data(as_text=True)
        self.assertNotIn("boîte d’envoi locale", page)
        self.assertIn("envoyé(s) par e-mail", page)


# ---------------------------------------------------------------------------
# Piste 43 : proxy, tunnel, blocage des essais, liste des comptes
# ---------------------------------------------------------------------------
class ProxyBlocageTests(Base):
    def test_boucle_locale_jamais_bloquee_en_entier(self):
        from hangar import blocage_connexion as b
        with self.app.app_context():
            for i in range(b.LIMITE_IP + 5):
                b.noter_echec(f"compte{i}", "127.0.0.1")
            self.assertEqual(b.bloque("eleve", "127.0.0.1"), 0, "derrière un tunnel, tout le monde a 127.0.0.1")
            self.assertEqual(b.bloque("eleve", "::1"), 0)
            self.assertFalse(any(x["adresse"] for x in b.bloques()))
            for _ in range(b.LIMITE):
                b.noter_echec("eleve", "127.0.0.1")
            self.assertGreater(b.bloque("eleve", "127.0.0.1"), 0, "le blocage par compte reste")
            for i in range(b.LIMITE_IP):
                b.noter_echec(f"compte{i}", "10.0.0.8")
            self.assertGreater(b.bloque("autre", "10.0.0.8"), 0, "une adresse ordinaire reste bloquée")

    def test_connexion_android_exige_du_json(self):
        from hangar import blocage_connexion as b
        c = self.app.test_client()
        corps = json.dumps({"username": "eleve", "password": "faux"})
        for _ in range(3):
            r = c.post("/api/appareil/connexion/", data=corps, content_type="text/plain")
            self.assertEqual(r.status_code, 400)
        with self.app.app_context():
            self.assertEqual(b.EchecConnexion.query.count(), 0)
        r = c.post("/api/appareil/connexion/", json={"username": "eleve", "password": "secret-eleve"})
        self.assertEqual(r.status_code, 200)

    def comptes_affiches(self, **kw):
        page = self.app.test_client().get("/login", **kw).get_data(as_text=True)
        return "data-account=\"eleve\"" in page

    def test_liste_des_comptes(self):
        self.assertTrue(self.comptes_affiches())  # sur cet ordinateur
        self.assertTrue(self.comptes_affiches(environ_base={"REMOTE_ADDR": "192.168.1.20"}))  # réseau local
        # derrière un proxy ou un tunnel (même mal réglé) : jamais
        self.assertFalse(self.comptes_affiches(headers={"X-Forwarded-For": "203.0.113.9"}))
        self.assertFalse(self.comptes_affiches(headers={"CF-Connecting-IP": "203.0.113.9"}))
        with self.app.app_context():
            Workspace.get().login_accounts = False
            db.session.commit()
        self.assertTrue(self.comptes_affiches())
        self.assertFalse(self.comptes_affiches(environ_base={"REMOTE_ADDR": "192.168.1.20"}))

    def test_reglage_liste_des_comptes(self):
        c = self.connecter("chef", "secret-chef")
        c.post("/settings/workspace", data={"_csrf": CSRF, "name": "Esp", "emoji": "✈️"})
        with self.app.app_context():
            self.assertFalse(Workspace.get().login_accounts)
        c.post("/settings/workspace", data={"_csrf": CSRF, "name": "Esp", "emoji": "✈️", "login_accounts": "1"})
        with self.app.app_context():
            self.assertTrue(Workspace.get().login_accounts)

    def test_mode_https_derriere_un_proxy(self):
        d = tempfile.mkdtemp(prefix="hangar-https-")
        try:
            with mock.patch.dict("os.environ", {"HANGAR_BEHIND_PROXY": "1", "HANGAR_HTTPS": "1"}):
                app = create_app(d, testing=True)
            self.assertTrue(app.config["SESSION_COOKIE_SECURE"])
            self.assertTrue(app.config["REMEMBER_COOKIE_SECURE"])
            with app.app_context():
                u = User(username="moi", email="moi@hangar.local", role="admin")
                u.set_password("secret-moi")
                db.session.add(u)
                db.session.commit()
            vu = {}

            @app.get("/_essai_adresse")
            def _essai():
                from flask import request

                from hangar import blocage_connexion as b
                vu.update(url=request.host_url, ip=request.remote_addr, client=b.ip_client())
                return "ok"
            c = app.test_client()
            entetes = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9",
                       "X-Forwarded-Host": "hangar.exemple.fr"}
            r = c.get("/login", headers=entetes)
            self.assertIn("max-age=", r.headers.get("Strict-Transport-Security", ""))
            self.assertNotIn("data-account=\"moi\"", r.get_data(as_text=True))  # en ligne : comptes jamais listés
            # ProxyFix : l'adresse publique et le visiteur sont bien vus par l'application
            with mock.patch.dict("os.environ", {"HANGAR_BEHIND_PROXY": "1"}):
                c.get("/_essai_adresse", headers=entetes)
            self.assertEqual(vu.get("url"), "https://hangar.exemple.fr/")
            self.assertEqual(vu.get("ip"), "203.0.113.9")
            self.assertEqual(vu.get("client"), "203.0.113.9")
            with app.app_context():
                db.session.remove()
                db.engine.dispose()
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_par_defaut_ni_secure_ni_hsts_ni_en_tetes_crus(self):
        self.assertFalse(self.app.config.get("SESSION_COOKIE_SECURE"))
        r = self.app.test_client().get("/login", headers={"X-Forwarded-Proto": "https"})
        self.assertNotIn("Strict-Transport-Security", r.headers)

    def test_avertissement_au_demarrage(self):
        from hangar import cli
        self.assertIsNone(cli.avertissement_exposition("127.0.0.1"))
        texte = cli.avertissement_exposition("0.0.0.0")
        self.assertIn("réseau", texte)
        self.assertIn("Mettre Hangar en ligne", texte)

    def test_options_de_lancement(self):
        from hangar import cli
        env = {}
        with mock.patch.dict("os.environ", env, clear=False):
            cli.appliquer_options_reseau(mock.Mock(cloudflare=True, behind_proxy=False, https=False))
            import os
            self.assertEqual(os.environ.get("HANGAR_CLOUDFLARE"), "1")
            self.assertEqual(os.environ.get("HANGAR_HTTPS"), "1")

    def test_mot_de_passe_8_caracteres(self):
        c = self.connecter()
        c.post("/settings/password", data={"_csrf": CSRF, "current": "secret-eleve", "new": "court12",
                                           "confirm": "court12"})
        with self.app.app_context():
            self.assertTrue(db.session.get(User, self.membre_id).check_password("secret-eleve"))


# ---------------------------------------------------------------------------
# Piste 55 : valeurs sensibles masquées dans /erreurs/
# ---------------------------------------------------------------------------
class ErreursMasqueesTests(Base):
    def test_parametres_sql_absents_de_l_erreur_enregistree(self):
        from sqlalchemy.exc import IntegrityError

        from hangar.wiki import erreurs
        from hangar.wiki.models import ErreurServeur
        with self.app.app_context():
            doublon = User(username="eleve", email="x@hangar.local", password_hash="MotDePasseAppli-abcd-efgh")
            db.session.add(doublon)
            try:
                db.session.commit()
            except IntegrityError as exc:
                db.session.rollback()
                self.assertNotIn("MotDePasseAppli", str(exc))
                erreurs.noter(exc, contexte="essai")
            e = ErreurServeur.query.one()
            self.assertNotIn("MotDePasseAppli", e.message)
            self.assertNotIn("MotDePasseAppli", e.trace)

    def test_erreurs_deja_enregistrees_masquees_a_l_affichage(self):
        from hangar.wiki.models import ErreurServeur
        sql = ("(sqlite3.OperationalError) database is locked\n[SQL: UPDATE mail_config SET smtp_password=? "
               "WHERE mail_config.id = ?]\n[parameters: ('MotDePasseAppli-abcd-efgh', 1)]")
        with self.app.app_context():
            db.session.add(ErreurServeur(empreinte="x" * 40, type="OperationalError", message=sql, lieu="a.py:1",
                                         chemin="/settings/mail?token=JETON-SECRET&page=2", methode="POST",
                                         utilisateur="chef", trace="Traceback\n" + sql + "\nAuthorization: Bearer ABC123",
                                         nombre=1, premiere=now(), derniere=now(), reglee=False))
            db.session.commit()
        c = self.connecter("chef", "secret-chef")
        page = c.get("/erreurs/").get_data(as_text=True)
        self.assertIn("database is locked", page)
        self.assertIn("UPDATE mail_config", page)
        self.assertNotIn("MotDePasseAppli", page)
        self.assertNotIn("JETON-SECRET", page)
        self.assertNotIn("ABC123", page)
        self.assertIn("page=2", page)

    def test_masquer(self):
        from hangar.wiki.erreurs import masquer
        self.assertEqual(masquer("rien à cacher"), "rien à cacher")
        t = masquer("x [parameters: ('a', 'b')] y")
        self.assertNotIn("'a'", t)
        self.assertIn("[parameters: masqués]", t)
        self.assertNotIn("s3cr3t", masquer("/x?password=s3cr3t&q=1"))
        self.assertIn("q=1", masquer("/x?password=s3cr3t&q=1"))
        self.assertNotIn("sk-ant", masquer("api_key='sk-ant-123'"))

    def test_masquer_garde_code_des_traces_et_sql_lisibles(self):
        from hangar.wiki.erreurs import masquer
        trace = ('Traceback (most recent call last):\n'
                 '  File "hangar/views/auth.py", line 12, in changer\n'
                 '    u.password_hash = generate_password_hash(password)\n'
                 'ValueError: password=hunter2 refusé\n')
        t = masquer(trace)
        self.assertIn("    u.password_hash = generate_password_hash(password)\n", t)
        self.assertNotIn("hunter2", t)
        t = masquer("[SQL: UPDATE x SET smtp_password=? WHERE token = :token_1] [parameters: ('MotDePasse', 1)]")
        self.assertIn("SET smtp_password=? WHERE token = :token_1]", t)
        self.assertNotIn("MotDePasse", t)


# ---------------------------------------------------------------------------
# Piste 64 : adresse de retour après la connexion
# ---------------------------------------------------------------------------
class RetourApresConnexionTests(Base):
    def test_is_safe_next(self):
        from hangar.utils import is_safe_next
        for mauvais in ("/\t/evil.example", "/\n/evil.example", "/\r/evil.example", "//evil.example",
                        "/\\evil.example", "\\/evil.example", "https://evil.example/", "/\x00/x", None, "",
                        "javascript:alert(1)", "evil.example"):
            self.assertFalse(is_safe_next(mauvais), repr(mauvais))
        for bon in ("/", "/agenda/taches", "/projects/1/issues?state=open#x", "/wiki/Notes%20de%20cours"):
            self.assertTrue(is_safe_next(bon), bon)

    def test_connexion_avec_tabulation(self):
        c = self.client()
        r = c.post("/login?next=/%09/evil.example", data={"_csrf": CSRF, "login": "eleve",
                                                          "password": "secret-eleve"})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("evil.example", r.headers["Location"])
        c = self.client()
        r = c.post("/login?next=/%0A/evil.example", data={"_csrf": CSRF, "login": "eleve",
                                                          "password": "secret-eleve"})
        self.assertEqual(r.status_code, 302)
        c = self.client()
        r = c.post("/login?next=/agenda/", data={"_csrf": CSRF, "login": "eleve", "password": "secret-eleve"})
        self.assertEqual(r.headers["Location"], "/agenda/")


if __name__ == "__main__":
    unittest.main()
