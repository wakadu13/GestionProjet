"""Données de l'utilisateur : sauvegardes et commandes qui effacent.

- Piste 1 : sauvegarde complète (.zip) avec le wiki, les sets de QCM, les copies, les pièces jointes, l'agenda
  et la clé des notifications, même quand le coffre est ailleurs (HANGAR_VAULT) ; téléchargement sans secrets ;
  « hangar backup --complet --vers --garder » ; export JSON des projets qui garde assignés, étiquettes, modules.
- Piste 2 : « hangar demo » tire un mot de passe au hasard, affiché une fois, à changer par tous les comptes ;
  l'ancien mot de passe public force le changement et déclenche un avertissement au démarrage en réseau.
- Piste 6 : « demo --force » demande confirmation et sauvegarde ; « reset » s'arrête si la sauvegarde échoue
  et met les pièces jointes de côté au lieu de les supprimer.
"""
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from hangar import cli, create_app
from hangar.ai.models import AIProvider
from hangar.extensions import db
from hangar.models import AgendaTask, Issue, MailConfig, User

SMTP_SECRET = "SECRET-SMTP-PW-42"
CLE_SECRETE = "sk-ant-SECRET-KEY-4242"


def _fermer(app):
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


def _csrf(client, url="/login"):
    return re.search(r'name="_csrf" value="([^"]+)"', client.get(url).get_data(as_text=True)).group(1)


def _connecter(app, login, password):
    c = app.test_client()
    r = c.post("/login", data={"login": login, "password": password, "_csrf": _csrf(c)})
    return c, r


def _cli(*args, entree=None):
    """Lance une commande ; renvoie (sortie, code de sortie ou None)."""
    out = io.StringIO()
    code = None
    with redirect_stdout(out), mock.patch("builtins.input", side_effect=lambda *_: entree or ""):
        try:
            cli.main(list(args))
        except SystemExit as exc:
            code = exc.code
    return out.getvalue(), code


def _db_du_zip(zf: zipfile.ZipFile, dossier: Path) -> Path:
    chemin = dossier / f"extrait_{len(list(dossier.iterdir()))}.db"
    chemin.write_bytes(zf.read("hangar.db"))
    return chemin


def _secrets_dans(chemin_db: Path) -> tuple[bool, bool]:
    brut = chemin_db.read_bytes()
    return SMTP_SECRET.encode() in brut, CLE_SECRETE.encode() in brut


class SauvegardeCompleteTests(unittest.TestCase):
    """Piste 1 : tout ce qui n'est pas dans la base doit aussi être sauvegardé."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hangar-donnees-"))
        self.data = self.tmp / "data"
        self.coffre = self.tmp / "Obsidian" / "Cours EOA"  # coffre hors du dossier des données
        self.env = mock.patch.dict(os.environ, {"HANGAR_VAULT": str(self.coffre)})
        self.env.start()
        self.app = create_app(self.data, testing=True)
        with self.app.app_context():
            u = User(username="moi", email="moi@hangar.local", role="admin")
            u.set_password("secret12")
            db.session.add(u)
            MailConfig.get().smtp_password = SMTP_SECRET
            db.session.add(AIProvider(name="Claude", kind="anthropic", api_key=CLE_SECRETE))
            db.session.commit()
        fichiers = {
            self.coffre / "Cours" / "Thermo.md": "# Thermodynamique",
            self.coffre / "QCM" / "Physique" / "set1.json": "{}",
            self.coffre / ".git" / "HEAD": "ref: refs/heads/main",
            self.data / "wiki_versions" / "Cours" / "Thermo.md" / "20261101-moi.md": "ancienne version",
            self.data / "qcm_perso" / "1" / "Perso" / "set.json": "{}",
            self.data / "copies" / "1" / "copie.jpg": "jpg",
            self.data / "uploads" / "1" / "rapport.pdf": "pdf",
            self.data / "agenda" / "1" / "edt.ics": "BEGIN:VCALENDAR",
            self.data / "vapid_prive.pem": "cle",
            self.data / "outbox" / "message.eml": "e-mail",
            self.data / "backups" / "hangar_20200101_000000.db": "ancienne sauvegarde",
        }
        for chemin, texte in fichiers.items():
            chemin.parent.mkdir(parents=True, exist_ok=True)
            chemin.write_text(texte, encoding="utf-8")
        self.extraits = self.tmp / "extraits"
        self.extraits.mkdir()

    def tearDown(self):
        _fermer(self.app)
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _verifier_archive(self, zf: zipfile.ZipFile):
        noms = set(zf.namelist())
        for attendu in ("hangar.db", "LISEZMOI.txt", "vault/Cours/Thermo.md", "vault/QCM/Physique/set1.json",
                        "wiki_versions/Cours/Thermo.md/20261101-moi.md", "qcm_perso/1/Perso/set.json",
                        "copies/1/copie.jpg", "uploads/1/rapport.pdf", "agenda/1/edt.ics", "vapid_prive.pem"):
            self.assertIn(attendu, noms)
        self.assertFalse([n for n in noms if n.startswith(("backups/", "outbox/")) or "/.git/" in n
                          or n == "secret.key"], noms)
        con = sqlite3.connect(_db_du_zip(zf, self.extraits))
        try:
            self.assertEqual(con.execute("select username from user").fetchall(), [("moi",)])
        finally:
            con.close()
        self.assertIn(str(self.coffre), zf.read("LISEZMOI.txt").decode("utf-8"))

    def test_commande_complete_vers_un_autre_dossier_et_rotation(self):
        """« hangar backup --complet --vers <clé> --garder 2 » : tout le contenu, et seulement 2 archives."""
        cle = self.tmp / "CLE_USB"
        cle.mkdir()
        (cle / "mes_notes.txt").write_text("à ne jamais toucher")
        _fermer(self.app)
        for _ in range(3):
            sortie, code = _cli("--data", str(self.data), "backup", "--complet", "--vers", str(cle / "Hangar"),
                                "--garder", "2")
            self.assertIsNone(code, sortie)
        archives = sorted(p.name for p in (cle / "Hangar").iterdir())
        self.assertEqual(len(archives), 2, archives)
        self.assertTrue(all(re.fullmatch(r"hangar_complet_\d{8}_\d{6}(_\d+)?\.zip", a) for a in archives), archives)
        self.assertTrue((cle / "mes_notes.txt").exists())
        with zipfile.ZipFile(cle / "Hangar" / archives[-1]) as zf:
            self._verifier_archive(zf)
            self.assertEqual(_secrets_dans(_db_du_zip(zf, self.extraits)), (True, True))  # vraie migration
        # Rien n'a été ajouté dans data/backups
        self.assertEqual(sorted(os.listdir(self.data / "backups")), ["hangar_20200101_000000.db"])

    def test_commande_sans_secrets(self):
        _fermer(self.app)
        sortie, code = _cli("--data", str(self.data), "backup", "--complet", "--sans-secrets",
                            "--vers", str(self.tmp / "sortie"))
        self.assertIsNone(code, sortie)
        (archive,) = (self.tmp / "sortie").iterdir()
        with zipfile.ZipFile(archive) as zf:
            self.assertEqual(_secrets_dans(_db_du_zip(zf, self.extraits)), (False, False))
            self.assertIn("SANS SECRETS", zf.read("LISEZMOI.txt").decode("utf-8"))

    def test_tout_telecharger_depuis_la_page(self):
        """Bouton « Tout télécharger » : archive complète, sans secrets par défaut, rien gardé dans backups/."""
        c, r = _connecter(self.app, "moi", "secret12")
        self.assertEqual(r.status_code, 302)
        page = c.get("/settings/backup").get_data(as_text=True)
        self.assertIn("Tout télécharger", page)
        self.assertIn(str(self.coffre), page)  # la page dit où est le coffre qu'elle sauvegarde
        token = _csrf(c, "/settings/backup")
        avant = sorted(os.listdir(self.data / "backups"))

        r = c.post("/settings/backup/telecharger", data={"_csrf": token, "format": "zip"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/zip")
        with zipfile.ZipFile(io.BytesIO(r.get_data())) as zf:
            self._verifier_archive(zf)
            self.assertEqual(_secrets_dans(_db_du_zip(zf, self.extraits)), (False, False))
        r.close()

        r = c.post("/settings/backup/telecharger", data={"_csrf": token, "format": "zip", "secrets": "1"})
        with zipfile.ZipFile(io.BytesIO(r.get_data())) as zf:
            self.assertEqual(_secrets_dans(_db_du_zip(zf, self.extraits)), (True, True))
        r.close()

        # « La base seule » et l'ancienne adresse de téléchargement (en POST) : sans secrets non plus
        for r in (c.post("/settings/backup/telecharger", data={"_csrf": token, "format": "db"}),
                  c.post("/settings/backup/download", data={"_csrf": token})):
            self.assertEqual(r.status_code, 200)
            copie = self.extraits / f"base_{len(list(self.extraits.iterdir()))}.db"
            copie.write_bytes(r.get_data())
            r.close()
            self.assertEqual(_secrets_dans(copie), (False, False))
            con = sqlite3.connect(copie)
            try:
                self.assertEqual(con.execute("select username from user").fetchall(), [("moi",)])
            finally:
                con.close()
        self.assertEqual(sorted(os.listdir(self.data / "backups")), avant)

    def test_sauvegarde_complete_gardee_listee_puis_supprimee(self):
        c, _ = _connecter(self.app, "moi", "secret12")
        token = _csrf(c, "/settings/backup")
        c.post("/settings/backup", data={"_csrf": token, "action": "create_full"})
        zips = [n for n in os.listdir(self.data / "backups") if n.endswith(".zip")]
        self.assertEqual(len(zips), 1, zips)
        self.assertIn(zips[0], c.get("/settings/backup").get_data(as_text=True))
        r = c.get(f"/settings/backup/download?name={zips[0]}")
        self.assertEqual(r.status_code, 200)
        r.close()
        c.post("/settings/backup", data={"_csrf": token, "action": "delete", "name": zips[0]})
        self.assertFalse((self.data / "backups" / zips[0]).exists())

    def test_deux_sauvegardes_dans_la_meme_seconde(self):
        """Avant : le nom à la seconde près faisait écraser la sauvegarde précédente."""
        from hangar.views.settings import make_backup
        with self.app.app_context():
            a, b = make_backup(), make_backup()
        self.assertNotEqual(a, b)
        self.assertTrue(os.path.exists(a) and os.path.exists(b))

    def test_mot_de_passe_smtp_par_variable_d_environnement(self):
        from hangar import mailer
        with self.app.app_context():
            cfg = MailConfig.get()
            cfg.smtp_password = ""
            with mock.patch.dict(os.environ, {"HANGAR_SMTP_PASSWORD": "depuis-env"}):
                self.assertEqual(mailer.smtp_password(cfg), "depuis-env")
            cfg.smtp_password = "enregistre"
            self.assertEqual(mailer.smtp_password(cfg), "enregistre")


class ExportJsonTests(unittest.TestCase):
    """Piste 1 : l'export JSON des projets garde assignés, étiquettes et modules ; il est envoyé directement."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-export-")
        self.app = create_app(self.tmp, testing=True)
        with self.app.test_request_context("/"):
            from hangar.services import create_issue, create_project, get_or_create_label, get_or_create_module
            u = User(username="moi", email="moi@hangar.local", role="admin")
            u.set_password("secret12")
            db.session.add(u)
            db.session.flush()
            p = create_project("Projet test", "PT", u)
            db.session.flush()
            label, module = get_or_create_label(p, "Réunion"), get_or_create_module(p, "Cadrage")
            db.session.flush()
            issue = create_issue(p, {"title": "Préparer la réunion"}, u)
            issue.assignees.append(u)
            issue.labels.append(label)
            issue.modules.append(module)
            db.session.commit()

    def tearDown(self):
        _fermer(self.app)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_export_projets(self):
        c, _ = _connecter(self.app, "moi", "secret12")
        r = c.get("/settings/backup/export.json")
        self.assertEqual(r.status_code, 200)
        brut = r.get_data()
        payload = json.loads(brut)
        (ticket,) = payload["issues"]
        self.assertEqual([a["username"] for a in ticket["assignees"]], ["moi"])
        self.assertEqual([l["name"] for l in ticket["labels"]], ["Réunion"])
        self.assertEqual([m["name"] for m in ticket["modules"]], ["Cadrage"])
        self.assertNotIn("password_hash", payload["users"][0])
        # Rien n'est écrit dans data/backups
        self.assertEqual([n for n in os.listdir(os.path.join(self.tmp, "backups")) if n.endswith(".json")], [])
        # L'import JSON d'un projet sait le relire, assignés et étiquettes compris
        from hangar.importer import read_table
        (ligne,) = read_table(io.BytesIO(brut), "export.json")
        # Listes de noms (un nom qui contient une virgule n'est pas coupé), comme les relit l'import
        self.assertEqual((ligne["assignees"], ligne["labels"], ligne["module"]), (["moi"], ["Réunion"], ["Cadrage"]))


class MotDePasseDemoTests(unittest.TestCase):
    """Piste 2 : plus de mot de passe public pour les comptes de démonstration."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-demo-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_demo_tire_un_mot_de_passe_affiche_une_fois(self):
        sortie, code = _cli("--data", self.tmp, "demo")
        self.assertIsNone(code, sortie)
        m = re.search(r"Mot de passe provisoire de tous les comptes : (\S+)", sortie)
        self.assertIsNotNone(m, sortie)
        mdp = m.group(1)
        self.assertNotEqual(mdp, "hangar2026")
        self.assertNotIn("hangar2026", sortie)
        self.assertEqual(sortie.count(mdp), 1)
        app = create_app(self.tmp, testing=True)
        try:
            with app.app_context():
                users = User.query.all()
                self.assertEqual(len(users), 9)
                self.assertTrue(all(u.must_change_password for u in users))  # administrateur compris
                self.assertTrue(all(u.check_password(mdp) for u in users))
                from hangar.models import Sticky
                self.assertFalse([s for s in Sticky.query if mdp in s.content or "hangar2026" in s.content])
            c, r = _connecter(app, "wacogne", mdp)
            self.assertEqual(r.status_code, 302)
            r = c.get("/home")
            self.assertEqual(r.status_code, 302)
            self.assertIn("must_change=1", r.headers["Location"])
        finally:
            _fermer(app)

    def test_seed_demo_accepte_un_mot_de_passe(self):
        app = create_app(self.tmp, testing=True)
        try:
            with app.test_request_context("/"):
                from hangar.seed import seed_demo
                res = seed_demo(None, verbose=False, password="mot-de-passe-fixe")
                self.assertEqual(res["password"], "mot-de-passe-fixe")
                self.assertTrue(User.query.filter_by(username="wacogne").one().check_password("mot-de-passe-fixe"))
        finally:
            _fermer(app)

    def test_ancien_mot_de_passe_public(self):
        """Base créée par une ancienne version : l'admin a encore « hangar2026 » et rien ne l'oblige à changer."""
        app = create_app(self.tmp, testing=True)
        try:
            with app.app_context():
                u = User(username="wacogne", email="wacogne@hangar.local", role="admin", must_change_password=False)
                u.set_password("hangar2026")
                db.session.add(u)
                db.session.commit()
            self.assertIsNone(cli.avertissement_reseau(app, "127.0.0.1"))
            alerte = cli.avertissement_reseau(app, "0.0.0.0")
            self.assertIsNotNone(alerte)
            self.assertIn("wacogne", alerte)

            c, r = _connecter(app, "wacogne", "hangar2026")
            self.assertEqual(r.status_code, 302)
            r = c.get("/home")
            self.assertEqual(r.status_code, 302)  # changement de mot de passe exigé
            self.assertIn("must_change=1", r.headers["Location"])
            token = _csrf(c, "/settings/profile")
            # Reprendre le même mot de passe public est refusé
            c.post("/settings/password", data={"_csrf": token, "current": "hangar2026", "new": "hangar2026",
                                               "confirm": "hangar2026"})
            with app.app_context():
                self.assertTrue(User.query.one().must_change_password)
            c.post("/settings/password", data={"_csrf": token, "current": "hangar2026", "new": "un-vrai-secret",
                                               "confirm": "un-vrai-secret"})
            with app.app_context():
                self.assertFalse(User.query.one().must_change_password)
            self.assertIsNone(cli.avertissement_reseau(app, "0.0.0.0"))
        finally:
            _fermer(app)


class CommandesQuiEffacentTests(unittest.TestCase):
    """Piste 6 : « demo --force » et « reset » ne perdent rien sans prévenir."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hangar-effacer-"))
        self.data = self.tmp / "data"
        app = create_app(self.data, testing=True)
        with app.app_context():
            u = User(username="eleve", email="eleve@hangar.local", role="admin")
            u.set_password("secret12")
            db.session.add(u)
            db.session.flush()
            db.session.add(AgendaTask(user_id=u.id, title="Réviser la thermo"))
            db.session.commit()
        _fermer(app)
        for chemin in (self.data / "uploads" / "1" / "pj.txt", self.data / "qcm_perso" / "1" / "Perso" / "a.json",
                       self.data / "vault" / "Mon cours.md"):
            chemin.parent.mkdir(parents=True, exist_ok=True)
            chemin.write_text("contenu", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _comptes(self):
        con = sqlite3.connect(self.data / "hangar.db")
        try:
            return [r[0] for r in con.execute("select username from user order by id")]
        finally:
            con.close()

    def _sauvegardes(self):
        return sorted(p for p in (self.data / "backups").iterdir() if p.suffix == ".db")

    def test_demo_force_demande_confirmation(self):
        sortie, code = _cli("--data", str(self.data), "demo", "--force", entree="non")
        self.assertIn("Annulé", sortie)
        self.assertEqual(self._comptes(), ["eleve"])
        self.assertEqual(self._sauvegardes(), [])

    def test_demo_force_sauvegarde_et_met_de_cote(self):
        sortie, code = _cli("--data", str(self.data), "demo", "--force", entree="oui")
        self.assertIsNone(code, sortie)
        self.assertIn("wacogne", self._comptes())
        (copie,) = self._sauvegardes()
        con = sqlite3.connect(copie)
        try:
            self.assertEqual(con.execute("select title from agenda_task").fetchall(), [("Réviser la thermo",)])
        finally:
            con.close()
        # Les fichiers rangés par numéro de compte ne passent pas au nouveau compte n° 1 (wacogne)
        self.assertFalse((self.data / "qcm_perso" / "1").exists())
        self.assertFalse((self.data / "uploads" / "1" / "pj.txt").exists())
        cote = copie.with_name(copie.stem + "_fichiers")
        self.assertTrue((cote / "uploads" / "1" / "pj.txt").exists())
        self.assertTrue((cote / "qcm_perso" / "1" / "Perso" / "a.json").exists())
        self.assertTrue((self.data / "vault" / "Mon cours.md").exists())  # le coffre n'est jamais touché
        # --yes : sans question (pour un script)
        sortie, code = _cli("--data", str(self.data), "demo", "--force", "--yes")
        self.assertIsNone(code, sortie)
        self.assertEqual(len(self._sauvegardes()), 2)

    def test_reset_s_arrete_si_la_sauvegarde_echoue(self):
        with mock.patch("hangar.views.settings.make_backup", side_effect=OSError(28, "No space left on device")):
            sortie, code = _cli("--data", str(self.data), "reset", "--yes")
        self.assertEqual(code, 1, sortie)
        self.assertIn("rien n’a été effacé", sortie)
        self.assertEqual(self._comptes(), ["eleve"])
        self.assertTrue((self.data / "uploads" / "1" / "pj.txt").exists())

    def test_reset_met_les_pieces_jointes_de_cote(self):
        sortie, code = _cli("--data", str(self.data), "reset", "--yes")
        self.assertIsNone(code, sortie)
        self.assertEqual(self._comptes(), [])
        (copie,) = self._sauvegardes()
        cote = copie.with_name(copie.stem + "_fichiers")
        self.assertTrue((cote / "uploads" / "1" / "pj.txt").exists())
        self.assertTrue((self.data / "uploads").is_dir())
        self.assertTrue((self.data / "vault" / "Mon cours.md").exists())

    def test_sans_clavier_message_clair(self):
        """Planificateur de tâches, script, « < nul » : plus de trace Python, un message qui parle de --yes."""
        for commande in (("demo", "--force"), ("reset",)):
            out = io.StringIO()
            code = None
            with redirect_stdout(out), mock.patch("builtins.input", side_effect=EOFError):
                try:
                    cli.main(["--data", str(self.data), *commande])
                except SystemExit as exc:
                    code = exc.code
            self.assertEqual(code, 1, out.getvalue())  # un script voit que rien n'a été fait
            self.assertIn("--yes", out.getvalue())
            self.assertEqual(self._comptes(), ["eleve"])
            self.assertFalse((self.data / "backups").exists() and self._sauvegardes())
            self.assertTrue((self.data / "uploads" / "1" / "pj.txt").exists())

    def _admin(self, login="admin2"):
        app = create_app(self.data, testing=True)
        with app.app_context():
            u = User(username=login, email=f"{login}@hangar.local", role="admin")
            u.set_password("secret12")
            db.session.add(u)
            db.session.commit()
        return app

    def test_sauvegarde_d_avant_effacement_jamais_retiree_par_la_rotation(self):
        """« reset » puis « backup --garder 1 » : avant, la base d'avant l'effacement disparaissait et ses
        fichiers mis de côté restaient seuls, invisibles depuis la page Sauvegardes."""
        sortie, code = _cli("--data", str(self.data), "reset", "--yes")
        self.assertIsNone(code, sortie)
        (securite,) = self._sauvegardes()
        self.assertTrue(securite.name.startswith("hangar_avant_effacement_"), securite.name)
        cote = securite.with_name(securite.stem + "_fichiers")
        self.assertTrue((cote / "uploads" / "1" / "pj.txt").exists())
        # Paire au nom ordinaire (faite par une version précédente) : protégée elle aussi
        ancienne = self.data / "backups" / "hangar_20200101_000000.db"
        shutil.copy(securite, ancienne)
        (self.data / "backups" / "hangar_20200101_000000_fichiers" / "uploads").mkdir(parents=True)
        for _ in range(3):
            sortie, code = _cli("--data", str(self.data), "backup", "--garder", "1")
            self.assertIsNone(code, sortie)
        self.assertTrue(securite.exists(), sorted(os.listdir(self.data / "backups")))
        self.assertTrue(cote.is_dir())
        self.assertTrue(ancienne.exists())
        routine = [p for p in self._sauvegardes() if p not in (securite, ancienne)]
        self.assertEqual(len(routine), 1, routine)  # la rotation joue toujours sur les sauvegardes ordinaires

        # La page montre la sauvegarde d'avant l'effacement ET le dossier qui l'accompagne
        app = self._admin()
        try:
            c, _ = _connecter(app, "admin2", "secret12")
            page = c.get("/settings/backup").get_data(as_text=True)
            self.assertIn(securite.name, page)
            self.assertIn(cote.name, page)
            self.assertIn("Avant effacement", page)
            # Supprimer la sauvegarde depuis la page retire aussi ses fichiers mis de côté
            token = _csrf(c, "/settings/backup")
            c.post("/settings/backup", data={"_csrf": token, "action": "delete", "name": securite.name})
            self.assertFalse(securite.exists())
            self.assertFalse(cote.exists())
            self.assertTrue(routine[0].exists())
        finally:
            _fermer(app)

    def test_dossier_de_fichiers_seul_visible_et_supprimable(self):
        """Un dossier de fichiers mis de côté dont la base a disparu (supprimée à la main…) reste visible."""
        sortie, code = _cli("--data", str(self.data), "reset", "--yes")
        self.assertIsNone(code, sortie)
        (securite,) = self._sauvegardes()
        cote = securite.with_name(securite.stem + "_fichiers")
        securite.unlink()
        app = self._admin()
        try:
            c, _ = _connecter(app, "admin2", "secret12")
            page = c.get("/settings/backup").get_data(as_text=True)
            self.assertIn(cote.name, page)
            self.assertIn("Fichiers mis de côté", page)
            r = c.get(f"/settings/backup/download?name={cote.name}")
            self.assertEqual(r.status_code, 404)  # un dossier ne se télécharge pas
            r.close()
            token = _csrf(c, "/settings/backup")
            c.post("/settings/backup", data={"_csrf": token, "action": "delete", "name": cote.name})
            self.assertFalse(cote.exists())
            # Rien d'autre que ce qui suit le nom d'un dossier mis de côté ne peut être supprimé ainsi
            (self.data / "backups" / "autre").mkdir()
            c.post("/settings/backup", data={"_csrf": token, "action": "delete", "name": "autre"})
            c.post("/settings/backup", data={"_csrf": token, "action": "delete", "name": "../uploads"})
            self.assertTrue((self.data / "backups" / "autre").is_dir())
            self.assertTrue((self.data / "uploads").is_dir())
        finally:
            _fermer(app)


class RelectureSauvegardeTests(unittest.TestCase):
    """Piste 1, après relecture : fichier illisible signalé partout ; « --vers ~/… » sans --complet."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hangar-relecture-"))
        self.data = self.tmp / "data"
        self.app = create_app(self.data, testing=True)
        with self.app.app_context():
            u = User(username="moi", email="moi@hangar.local", role="admin")
            u.set_password("secret12")
            db.session.add(u)
            db.session.commit()
        self.coffre = Path(self.app.config["WIKI_DIR"])
        for nom in ("Thermo.md", "verrouille.pdf"):
            (self.coffre / "Cours").mkdir(parents=True, exist_ok=True)
            (self.coffre / "Cours" / nom).write_text("contenu", encoding="utf-8")

    def tearDown(self):
        _fermer(self.app)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _verrou(self):
        """Fichier ouvert dans un autre logiciel sous Windows : sa lecture échoue."""
        ecrire = zipfile.ZipFile.write

        def write(zf, filename, *args, **kwargs):
            if str(filename).endswith("verrouille.pdf"):
                raise PermissionError(13, "Le processus ne peut pas accéder au fichier", str(filename))
            return ecrire(zf, filename, *args, **kwargs)
        return mock.patch.object(zipfile.ZipFile, "write", write)

    def _verifier_manquant(self, zf: zipfile.ZipFile):
        noms = zf.namelist()
        self.assertIn("vault/Cours/Thermo.md", noms)
        self.assertNotIn("vault/Cours/verrouille.pdf", noms)
        self.assertIn("FICHIERS_MANQUANTS.txt", noms)
        self.assertIn("vault/Cours/verrouille.pdf", zf.read("FICHIERS_MANQUANTS.txt").decode("utf-8"))
        lisezmoi = zf.read("LISEZMOI.txt").decode("utf-8")
        self.assertIn("SAUVEGARDE INCOMPLÈTE", lisezmoi)
        self.assertIn("vault/Cours/verrouille.pdf", lisezmoi)
        self.assertIn("FICHIERS_MANQUANTS.txt", lisezmoi)

    def test_tout_telecharger_signale_un_fichier_illisible(self):
        c, _ = _connecter(self.app, "moi", "secret12")
        token = _csrf(c, "/settings/backup")
        r = c.post("/settings/backup/telecharger", data={"_csrf": token, "format": "zip"})
        with zipfile.ZipFile(io.BytesIO(r.get_data())) as zf:  # tout est lisible : pas de liste
            self.assertNotIn("FICHIERS_MANQUANTS.txt", zf.namelist())
        r.close()
        self.assertNotIn("verrouille.pdf", c.get("/settings/backup").get_data(as_text=True))

        with self._verrou():
            r = c.post("/settings/backup/telecharger", data={"_csrf": token, "format": "zip"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/zip")
        with zipfile.ZipFile(io.BytesIO(r.get_data())) as zf:
            self._verifier_manquant(zf)
        r.close()
        # Au prochain affichage de la page, l'élève est prévenu
        self.assertIn("vault/Cours/verrouille.pdf", c.get("/settings/backup").get_data(as_text=True))

    def test_sauvegarde_complete_gardee_et_commande_listent_le_fichier_illisible(self):
        c, _ = _connecter(self.app, "moi", "secret12")
        token = _csrf(c, "/settings/backup")
        with self._verrou():
            r = c.post("/settings/backup", data={"_csrf": token, "action": "create_full"}, follow_redirects=True)
        self.assertIn("vault/Cours/verrouille.pdf", r.get_data(as_text=True))
        (archive,) = (self.data / "backups").glob("hangar_complet_*.zip")
        with zipfile.ZipFile(archive) as zf:
            self._verifier_manquant(zf)
        _fermer(self.app)
        with self._verrou():
            sortie, code = _cli("--data", str(self.data), "backup", "--complet", "--vers", str(self.tmp / "cle"))
        self.assertEqual(code, 2, sortie)
        self.assertIn("vault/Cours/verrouille.pdf", sortie)
        (archive,) = (self.tmp / "cle").iterdir()
        with zipfile.ZipFile(archive) as zf:
            self._verifier_manquant(zf)

    def test_vers_avec_tilde_sans_complet(self):
        """« hangar backup --vers ~/Sauv » : la base va dans le dossier personnel, pas dans « ./~/Sauv »."""
        maison, courant = self.tmp / "maison", self.tmp / "courant"
        maison.mkdir()
        courant.mkdir()
        _fermer(self.app)
        avant = os.getcwd()
        os.chdir(courant)
        try:
            with mock.patch.dict(os.environ, {"HOME": str(maison), "USERPROFILE": str(maison)}):
                sortie, code = _cli("--data", str(self.data), "backup", "--vers", "~/Sauv", "--garder", "1")
                self.assertIsNone(code, sortie)
                sortie, code = _cli("--data", str(self.data), "backup", "--vers", "~/Sauv", "--garder", "1")
                self.assertIsNone(code, sortie)
        finally:
            os.chdir(avant)
        self.assertEqual(list(courant.iterdir()), [])
        copies = list((maison / "Sauv").iterdir())
        self.assertEqual(len(copies), 1, copies)
        self.assertRegex(copies[0].name, r"^hangar_\d{8}_\d{6}(_\d+)?\.db$")
        # Clé débranchée : le dossier parent manque, rien n'est créé à sa place
        sortie, code = _cli("--data", str(self.data), "backup", "--vers", str(self.tmp / "E" / "Sauv"))
        self.assertEqual(code, 1, sortie)
        self.assertFalse((self.tmp / "E").exists())


if __name__ == "__main__":
    unittest.main()
