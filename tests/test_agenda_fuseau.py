"""Vague 3b, groupe « agenda-fuseau » : pistes 70 (socle de create_app, paquet agenda) et 71 (fuseau horaire)."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent


def _python(code: str, env: dict | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    """Lance `code` dans un interpréteur neuf (aucun module de Hangar déjà importé par un autre test)."""
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=RACINE, env=e, capture_output=True,
                          text=True, timeout=timeout)


# ---------------------------------------------------------------------------
# Piste 70 : create_app ne dépend plus d'un import caché du module QCM
# ---------------------------------------------------------------------------

class SocleCreateAppTests(unittest.TestCase):
    def test_base_neuve_dans_un_processus_neuf_a_les_tables_de_connexion(self):
        """Sur une base neuve, les tables du blocage des connexions et des jetons d'appareil existent dès le
        premier create_app, sans compter sur un autre test (ou un autre module) pour les importer."""
        with tempfile.TemporaryDirectory() as d:
            r = _python("""
                from sqlalchemy import inspect
                from hangar import create_app
                from hangar.extensions import db
                app = create_app(%r, testing=True)
                with app.app_context():
                    tables = set(inspect(db.engine).get_table_names())
                print("connexion_echec" in tables, "jeton_appareil" in tables, "appareils" in app.blueprints)
                # le crochet du jeton d'appareil passe avant la vérification CSRF du socle
                noms = [f.__name__ for f in app.before_request_funcs[None]]
                print(noms.index("_avant") < noms.index("_setup_and_csrf"))
            """ % d)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.split(), ["True", "True", "True", "True"])

    def test_create_app_importe_lui_meme_appareils_et_blocage(self):
        """Ni le blueprint « appareils » ni ses tables ne passent plus par le module QCM : retirer un jour
        l'import du module QCM ne casse plus la connexion sur une base neuve."""
        import re
        qcm_models = (RACINE / "hangar" / "qcm" / "models.py").read_text(encoding="utf-8")
        qcm_views = (RACINE / "hangar" / "qcm" / "views.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"^from \.\. import .*(appareils|blocage_connexion)", qcm_models, re.M))
        self.assertFalse("register_blueprint" in qcm_views)
        with tempfile.TemporaryDirectory() as d:
            # On empêche le module QCM d'importer quoi que ce soit d'autre : les tables viennent de create_app.
            r = _python("""
                import builtins
                vrai = builtins.__import__
                def garde(name, globals=None, locals=None, fromlist=(), level=0):
                    appelant = (globals or {}).get("__name__", "")
                    if appelant.startswith("hangar.qcm") and fromlist and (
                            "appareils" in fromlist or "blocage_connexion" in fromlist):
                        raise ImportError("import interdit depuis le module QCM")
                    return vrai(name, globals, locals, fromlist, level)
                builtins.__import__ = garde
                from sqlalchemy import inspect
                from hangar import create_app
                from hangar.extensions import db
                app = create_app(%r, testing=True)
                with app.app_context():
                    tables = set(inspect(db.engine).get_table_names())
                print("connexion_echec" in tables, "jeton_appareil" in tables)
            """ % d)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.split(), ["True", "True"])

    def test_gabarit_inutilise_supprime(self):
        self.assertFalse((RACINE / "hangar" / "templates" / "ai" / "placeholder.html").exists())

    def test_variables_jinja_declarees_une_seule_fois(self):
        """Les variables communes des gabarits sont des globales Jinja (visibles aussi des macros importées
        sans contexte) ; le context_processor n'ajoute que ce qui dépend de la requête."""
        import shutil
        from hangar import create_app
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        app = create_app(d, testing=True)
        g = app.jinja_env.globals
        for nom in ("icon", "csrf_token", "M", "IQ", "version", "can", "greeting", "markdown_toc"):
            self.assertIn(nom, g)
        with app.test_request_context("/"):
            ctx = {}
            for fn in app.template_context_processors[None]:
                ctx.update(fn())
        for nom in ("icon", "csrf_token", "M", "IQ", "version", "can", "greeting", "markdown_toc", "charts"):
            self.assertNotIn(nom, ctx)
        for nom in ("today", "qs", "can_delete_issues"):
            self.assertIn(nom, ctx)


# ---------------------------------------------------------------------------
# Piste 70 : hangar/agenda devient un paquet, mêmes noms qu'avant
# ---------------------------------------------------------------------------

class PaquetAgendaTests(unittest.TestCase):
    def test_paquet_et_noms_reexportes(self):
        from hangar import agenda as A
        self.assertTrue(hasattr(A, "__path__"), "hangar.agenda doit être un paquet")
        for nom in ("import_calendar", "undo_import", "week_view", "month_view", "list_view", "dashboard",
                    "run_jobs", "export_ics", "process_reminders", "send_reminder", "send_daily_programs",
                    "check_source", "check_sources", "subject_links", "_subject_link", "_mail", "now",
                    "IMPORT_MODES", "RECURRENCES", "REMINDER_OFFSETS", "TASK_GROUP_LABELS", "SOURCE_KINDS",
                    "DAYS", "AgendaTask", "ics", "is_exam", "hm", "feed_user", "regenerate_feed_token"):
            self.assertTrue(hasattr(A, nom), nom)
        # le nom d'une fonction n'est jamais masqué par celui d'un module du paquet
        self.assertTrue(callable(A.export_ics))

    def test_remplacer_agenda_now_agit_dans_les_modules(self):
        """`mock.patch.object(agenda, "now")` agit encore sur tout l'agenda, comme avant le découpage."""
        import shutil
        from datetime import date, datetime, time
        from unittest import mock

        from hangar import agenda as A
        from hangar import create_app
        from hangar.extensions import db
        from hangar.models import AgendaImport, AgendaTask, User
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        app = create_app(d, testing=True)
        with app.test_request_context("/"):
            u = User(username="moi", email="moi@hangar.local", role="admin")
            u.set_password("secret1")
            db.session.add(u)
            db.session.flush()
            imp = AgendaImport(user_id=u.id, filename="a.ics", created_at=datetime(2020, 1, 1, 9))
            t = AgendaTask(user_id=u.id, title="Rendu", due_date=date(2020, 1, 3), start_time=time(10))
            db.session.add_all([imp, t])
            db.session.commit()
            self.assertEqual(A.recent_import_ids(u), set())  # vraie horloge : import trop ancien
            with mock.patch.object(A, "now", return_value=datetime(2020, 1, 2, 9)):
                self.assertEqual(A.recent_import_ids(u), {imp.id})  # imports.py
                A.set_task_reminders(t, [60], "app")  # rappels.py : rappel à 9 h, pas encore passé
                self.assertEqual([r.remind_at for r in t.reminders], [datetime(2020, 1, 3, 9)])
                self.assertEqual(A.dashboard(u)["today"], date(2020, 1, 2))  # vues.py
            db.session.remove()
            db.engine.dispose()


# ---------------------------------------------------------------------------
# Piste 71 : l'heure « maintenant » est celle de l'emploi du temps (Paris), pas celle de la machine
# ---------------------------------------------------------------------------

class FuseauMachineUTCTests(unittest.TestCase):
    def test_machine_en_utc_l_horloge_de_l_agenda_reste_a_l_heure_de_paris(self):
        """Serveur ou Raspberry Pi réglé en UTC : now() et today() donnent quand même l'heure de Paris."""
        r = _python("""
            import time
            from datetime import datetime, timedelta, timezone
            from zoneinfo import ZoneInfo
            if hasattr(time, "tzset"):
                time.tzset()
            from hangar import agenda, models
            paris = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Paris")).replace(tzinfo=None)
            for valeur in (models.now(), agenda.now()):
                print(abs((valeur - paris).total_seconds()) < 5)
        """, env={"TZ": "UTC", "HANGAR_TZ": ""})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ["True", "True"])

    def test_rappel_15_min_avant_un_cours_de_8_h_part_a_7_h_45_heure_de_paris(self):
        """Machine en UTC, 6 h 46 UTC = 7 h 46 à Paris : le rappel « 15 min avant » du cours de 8 h part."""
        with tempfile.TemporaryDirectory() as d:
            r = _python("""
                import time
                from datetime import date, datetime, time as heure_du_jour, timezone
                from unittest import mock
                if hasattr(time, "tzset"):
                    time.tzset()
                from hangar import agenda as A, create_app, heure
                from hangar.extensions import db
                from hangar.models import AgendaTask, User
                app = create_app(%r, testing=True)
                with app.test_request_context("/"):
                    u = User(username="moi", email="moi@hangar.local", role="admin")
                    u.set_password("secret1")
                    db.session.add(u)
                    db.session.flush()
                    t = AgendaTask(user_id=u.id, title="Cours", due_date=date(2026, 11, 12),
                                   start_time=heure_du_jour(8, 0))
                    db.session.add(t)
                    db.session.flush()
                    A.set_task_reminders(t, [15], "app", at=datetime(2026, 11, 11, 12))
                    db.session.commit()
                    utc = datetime(2026, 11, 12, 6, 46, tzinfo=timezone.utc)
                    with mock.patch.object(heure, "_maintenant_utc", return_value=utc):
                        print(A.now(), A.process_reminders())
                    print(t.reminders[0].status)
                    db.session.remove()
                    db.engine.dispose()
            """ % d, env={"TZ": "UTC", "HANGAR_TZ": ""})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.split(), ["2026-11-12", "07:46:00", "1", "sent"])


class QcmDatesMachineUTCTests(unittest.TestCase):
    def test_dates_echangees_avec_l_appareil_et_reprise_en_heure_de_paris(self):
        """Machine en UTC : les échéances envoyées à l'appli, les réponses faites hors ligne et les dates
        reprises de Revision sont dans l'heure de l'agenda (Paris), comme quiz.maintenant()."""
        r = _python("""
            import time
            from datetime import datetime, timezone
            from unittest import mock
            if hasattr(time, "tzset"):
                time.tzset()
            from hangar import heure
            from hangar.qcm import hors_ligne, quiz, reprise
            from hangar.qcm import views as qv
            utc = datetime(2026, 11, 12, 17, 12, 19, tzinfo=timezone.utc)
            with mock.patch.object(heure, "_maintenant_utc", return_value=utc):
                m = quiz.maintenant()
                iso = hors_ligne._date_iso(m)
                print(m.isoformat(), iso, datetime.fromisoformat(iso) == utc)
                print(hors_ligne._date_reponse(utc.isoformat(), m) == m,
                      hors_ligne._date_reponse("2026-11-12T17:12:19Z", m) == m,
                      qv._date_reponse("2026-11-12T17:12:19.000Z", m) == m,
                      reprise._date_heure("2026-11-12 17:12:19.123456") == m)
                ete = datetime(2026, 7, 1, 23, 30)
                print(hors_ligne._date_iso(ete), heure.vers_agenda(datetime.fromisoformat(hors_ligne._date_iso(ete))) == ete)
        """, env={"TZ": "UTC", "HANGAR_TZ": ""})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ["2026-11-12T18:12:19", "2026-11-12T18:12:19+01:00", "True",
                                            "True", "True", "True", "True",
                                            "2026-07-01T23:30:00+02:00", "True"])


class HeureCommuneTests(unittest.TestCase):
    def setUp(self):
        from hangar import heure
        self.heure = heure
        self.ancien = os.environ.get("HANGAR_TZ")
        self.addCleanup(self._remettre)

    def _remettre(self):
        if self.ancien is None:
            os.environ.pop("HANGAR_TZ", None)
        else:
            os.environ["HANGAR_TZ"] = self.ancien

    def _a(self, utc):
        from unittest import mock
        return mock.patch.object(self.heure, "_maintenant_utc", return_value=utc)

    def test_heure_d_ete_et_d_hiver_de_paris(self):
        from datetime import date, datetime, timezone
        os.environ.pop("HANGAR_TZ", None)
        with self._a(datetime(2026, 7, 1, 21, 30, 15, 999, tzinfo=timezone.utc)):
            self.assertEqual(self.heure.now(), datetime(2026, 7, 1, 23, 30, 15))
        with self._a(datetime(2026, 12, 31, 23, 30, tzinfo=timezone.utc)):  # déjà le 1er janvier à Paris
            self.assertEqual(self.heure.today(), date(2027, 1, 1))

    def test_paris_sans_base_des_fuseaux(self):
        """Windows sans tzdata : l'heure de Paris est calculée avec la règle européenne."""
        from datetime import datetime, timezone
        from unittest import mock
        os.environ.pop("HANGAR_TZ", None)
        with mock.patch.object(self.heure, "_zone", return_value=None):
            for utc, attendu in ((datetime(2026, 3, 29, 0, 59), datetime(2026, 3, 29, 1, 59)),
                                 (datetime(2026, 3, 29, 1, 0), datetime(2026, 3, 29, 3, 0)),
                                 (datetime(2026, 10, 25, 0, 59), datetime(2026, 10, 25, 2, 59)),
                                 (datetime(2026, 10, 25, 1, 0), datetime(2026, 10, 25, 2, 0))):
                with self._a(utc.replace(tzinfo=timezone.utc)):
                    self.assertEqual(self.heure.now(), attendu)

    def test_fuseau_reglable_et_heure_de_la_machine(self):
        from datetime import datetime, timezone
        utc = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)
        os.environ["HANGAR_TZ"] = "America/Montreal"
        with self._a(utc):
            self.assertEqual(self.heure.now(), datetime(2026, 11, 12, 1, 0))
            self.assertEqual(self.heure.description()["libelle"], "fuseau America/Montreal")
        os.environ["HANGAR_TZ"] = "local"
        with self._a(utc):
            self.assertEqual(self.heure.now(), utc.astimezone().replace(tzinfo=None))
            self.assertEqual(self.heure.description()["libelle"], "heure de cet ordinateur")
            self.assertFalse(self.heure.description()["introuvable"])
        os.environ["HANGAR_TZ"] = "Pas/UnFuseau"
        with self._a(utc):
            self.assertEqual(self.heure.now(), utc.astimezone().replace(tzinfo=None))
            self.assertTrue(self.heure.description()["introuvable"])

    def test_aller_retour_vers_et_depuis_l_agenda(self):
        """vers_agenda et depuis_agenda sont réciproques, avec ou sans base des fuseaux."""
        from datetime import datetime, timedelta, timezone
        from unittest import mock
        os.environ.pop("HANGAR_TZ", None)
        cas = ((datetime(2026, 1, 15, 12, 0), "+01:00"), (datetime(2026, 7, 15, 12, 0), "+02:00"),
               (datetime(2026, 10, 25, 3, 30), "+01:00"), (datetime(2026, 3, 29, 1, 59), "+01:00"))
        for sans_base in (False, True):
            with mock.patch.object(self.heure, "_zone", return_value=None) if sans_base else mock.patch.object(
                    self.heure, "nom_configure", return_value="Europe/Paris"):
                for naive, decalage in cas:
                    aware = self.heure.depuis_agenda(naive)
                    self.assertTrue(aware.isoformat().endswith(decalage), (sans_base, aware))
                    self.assertEqual(self.heure.vers_agenda(aware), naive)
                    self.assertEqual(self.heure.vers_agenda(aware.astimezone(timezone.utc)), naive)
                self.assertEqual(self.heure.vers_agenda(datetime(2026, 1, 1, 9)), datetime(2026, 1, 1, 9))
                autre = datetime(2026, 1, 1, 9, tzinfo=timezone(timedelta(hours=-5)))
                self.assertEqual(self.heure.vers_agenda(autre), datetime(2026, 1, 1, 15))

    def test_tzdata_dans_les_dependances(self):
        """Sous Windows, zoneinfo n'a pas de base des fuseaux sans le paquet tzdata."""
        self.assertIn('"tzdata', (RACINE / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn('name = "tzdata"', (RACINE / "uv.lock").read_text(encoding="utf-8"))

    def test_page_rappels_et_e_mails_affiche_le_fuseau(self):
        import re
        import shutil
        from hangar import create_app
        from hangar.extensions import db
        from hangar.models import User
        os.environ.pop("HANGAR_TZ", None)
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        app = create_app(d, testing=True)
        with app.app_context():
            u = User(username="moi", email="moi@hangar.local", role="admin")
            u.set_password("secret1")
            db.session.add(u)
            db.session.commit()
        c = app.test_client()
        page = c.get("/login").get_data(as_text=True)
        token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        c.post("/login", data={"login": "moi", "password": "secret1", "_csrf": token})
        html = c.get("/agenda/settings").get_data(as_text=True)
        self.assertIn("heure de Paris", html)
        self.assertIn("Horloge de l’agenda", html)
        with app.app_context():
            db.session.remove()
            db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
