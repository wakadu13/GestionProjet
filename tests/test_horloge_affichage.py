"""Revue de la vague 3, groupe « horloge » : toutes les dates affichées suivent l'heure de l'agenda (hangar.heure),
y compris sur une machine en UTC (serveur, Raspberry Pi) ou dans un autre fuseau.

Chaque essai tourne dans un interpréteur neuf, machine réglée en UTC (TZ=UTC), comme tests/test_agenda_fuseau.py.
L'horloge de l'agenda est figée avec `heure._maintenant_utc` : les résultats attendus ne dépendent ni de l'heure
réelle ni du fuseau de la machine (sous Windows, TZ peut être ignoré : les attentes restent justes)."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent

ENTETE = """
import time
from datetime import date, datetime, timedelta, timezone
from unittest import mock
if hasattr(time, "tzset"):
    time.tzset()
from hangar import heure
def a(*args):
    return mock.patch.object(heure, "_maintenant_utc", return_value=datetime(*args, tzinfo=timezone.utc))
"""


def _lecteur_vtimezone(bloc: list[str]):
    """Décalage (« -0400 ») qu'un agenda lit dans un VTIMEZONE pour une heure locale : dernière apparition
    (DTSTART ou RRULE annuelle BYMONTH/BYDAY, jusqu'à UNTIL) commencée avant elle."""
    from datetime import timedelta
    jours = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
    debuts, cur = [], {}
    for ligne in bloc:
        if ligne in ("BEGIN:STANDARD", "BEGIN:DAYLIGHT"):
            cur = {}
        elif ligne in ("END:STANDARD", "END:DAYLIGHT"):
            d0 = datetime.strptime(cur["DTSTART"], "%Y%m%dT%H%M%S")
            debuts.append((d0, cur["TZOFFSETTO"]))
            if "RRULE" in cur:
                r = dict(x.split("=") for x in cur["RRULE"].split(";"))
                mois, rang, jour = int(r["BYMONTH"]), int(r["BYDAY"][:-2]), jours.index(r["BYDAY"][-2:])
                de = cur["TZOFFSETFROM"]
                ecart = timedelta(hours=int(de[1:3]), minutes=int(de[3:5])) * (-1 if de[0] == "-" else 1)
                jusqu = datetime.strptime(r["UNTIL"], "%Y%m%dT%H%M%SZ") if "UNTIL" in r else None
                for an in range(d0.year + 1, 2041):
                    premier = datetime(an, mois, 1, d0.hour, d0.minute, d0.second)
                    if rang > 0:
                        d = premier + timedelta(days=(jour - premier.weekday()) % 7 + 7 * (rang - 1))
                    else:
                        dernier = datetime(an + mois // 12, mois % 12 + 1, 1, d0.hour, d0.minute) - timedelta(days=1)
                        d = dernier - timedelta(days=(dernier.weekday() - jour) % 7)
                    if jusqu is not None and d - ecart > jusqu:
                        break
                    debuts.append((d, cur["TZOFFSETTO"]))
        elif ":" in ligne:
            k, v = ligne.split(":", 1)
            cur[k] = v

    def decalage(local):
        return max((o for o in debuts if o[0] <= local), key=lambda o: o[0])[1]
    return decalage


def _python(code: str, env: dict | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e.update({"TZ": "UTC", "HANGAR_TZ": "", "PYTHONIOENCODING": "utf-8"})
    e.update(env or {})
    return subprocess.run([sys.executable, "-c", ENTETE + textwrap.dedent(code)], cwd=RACINE, env=e,
                          capture_output=True, text=True, encoding="utf-8", timeout=timeout)


class UtilsHorlogeAgendaTests(unittest.TestCase):
    """Constats 3, 4, 5, 7, 14 et 22 : filtres |ago, |due, |date, week_bounds et greeting."""

    def _sortie(self, code, env=None):
        r = _python(code, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip().splitlines()

    def test_time_ago_compare_a_l_heure_de_l_agenda(self):
        """Machine en UTC : un commentaire de 50 min n'est plus « à l'instant », un de 130 min a 2 h."""
        sortie = self._sortie("""
            from hangar import models, utils
            with a(2026, 11, 12, 17, 19):
                print(utils.time_ago(models.now() - timedelta(minutes=50)))
                print(utils.time_ago(heure.now() - timedelta(minutes=130)))
                print(utils.time_ago(heure.now() - timedelta(seconds=20)))
        """)
        self.assertEqual(sortie, ["il y a 50 min", "il y a 2 h", "à l'instant"])

    def test_relative_due_et_is_overdue_lisent_la_meme_date(self):
        """12/11 à 23 h 30 UTC = 13/11 à 0 h 30 à Paris : l'échéance du 12 est « hier », celle du 13 « aujourd'hui »."""
        sortie = self._sortie("""
            from hangar import utils
            with a(2026, 11, 12, 23, 30):
                print(utils.relative_due(date(2026, 11, 12)))
                print(utils.relative_due(date(2026, 11, 13)))
                print(utils.relative_due(date(2026, 11, 14)))
        """)
        self.assertEqual(sortie, ["hier", "aujourd'hui", "demain"])

    def test_fmt_date_et_week_bounds_suivent_le_jour_de_l_agenda(self):
        sortie = self._sortie("""
            from hangar import utils
            with a(2026, 12, 31, 23, 30):  # déjà le 1er janvier 2027 à Paris
                print(utils.fmt_date(date(2027, 1, 5)))
            with a(2026, 11, 15, 23, 30):  # dimanche soir UTC = lundi 16/11 à Paris
                print(utils.week_bounds()[0].isoformat())
        """)
        self.assertEqual(sortie, ["5 janv.", "2026-11-16"])

    def test_greeting_suit_l_heure_de_l_agenda(self):
        sortie = self._sortie("""
            from hangar import utils
            for h in (23, 5, 12, 18):  # UTC : 0 h 30, 6 h 30, 13 h 30, 19 h 30 à Paris
                with a(2026, 11, 12 if h == 23 else 13, h, 30):
                    print(utils.greeting())
        """)
        self.assertEqual(sortie, ["Bonne nuit", "Bonjour", "Bon après-midi", "Bonsoir"])

    def test_worklog_date_par_defaut_est_le_jour_de_l_agenda(self):
        sortie = self._sortie("""
            from hangar.models import WorkLog
            with a(2026, 11, 12, 23, 30):
                print(WorkLog.__table__.c.date.default.arg(None).isoformat())
        """)
        self.assertEqual(sortie, ["2026-11-13"])

    def test_plus_d_horloge_machine_dans_les_fonctions_de_dates_de_utils(self):
        source = (RACINE / "hangar" / "utils.py").read_text(encoding="utf-8")
        self.assertNotIn("date.today()", source)
        self.assertNotIn("datetime.now()", source)


class MacroTicketHorlogeTests(unittest.TestCase):
    def test_bloquer_un_creneau_sur_un_ticket_en_retard_propose_le_jour_de_l_agenda(self):
        """Constat 6 : 12/11 à 23 h 30 UTC = 13/11 à Paris ; le ticket dû le 12 est en retard, et le bouton
        propose le 13 (pas la veille)."""
        with tempfile.TemporaryDirectory() as d:
            r = _python("""
                from hangar import agenda as A, create_app
                from hangar.agenda.vues import TeamItem
                from hangar.extensions import db
                app = create_app(%r, testing=True)
                with app.test_request_context("/"):
                    gabarit = app.jinja_env.from_string(
                        '{%% import "agenda/_macros.html" as ag with context %%}{{ ag.ticket_item(it) }}')
                    it = TeamItem(kind="ticket", date=date(2026, 11, 12), title="Rendu", label="PP-1", url="/x",
                                  uid="t1", overdue=True)
                    with a(2026, 11, 12, 23, 30):
                        html = gabarit.render(it=it, A=A)  # A : fourni par les vues de l'agenda
                    import re
                    print(re.findall(r'data-date="([^"]+)"', html))
                    db.session.remove()
                    db.engine.dispose()
            """ % d)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.split(), ["['2026-11-13']"])


class WikiDatesAgendaTests(unittest.TestCase):
    """Constat 8 : dates des fichiers du wiki (Note.modified, historique, corbeille) en heure de l'agenda ; les
    noms des versions sur disque gardent l'heure de la machine."""

    def test_dates_des_fichiers_converties_en_heure_de_l_agenda(self):
        with tempfile.TemporaryDirectory() as d:
            r = _python("""
                import os
                from pathlib import Path, PurePosixPath
                from hangar import create_app
                from hangar.extensions import db
                from hangar.wiki import historique, rangement
                from hangar.wiki.vault import Note
                utc = datetime(2026, 11, 12, 17, 53, tzinfo=timezone.utc)  # 18:53 à Paris
                print(Note(PurePosixPath("a.md"), utc.timestamp(), {}, "").modified)
                app = create_app(%r, testing=True)
                with app.test_request_context("/"):
                    # Nom de version écrit à l'heure de la machine (quelle qu'elle soit), comme sauvegarder()
                    machine = utc.astimezone().replace(tzinfo=None)
                    dossier = historique.dossier() / "Perdue.md"
                    dossier.mkdir(parents=True)
                    nom = machine.strftime(historique._FORMAT) + "-moi.md"
                    (dossier / nom).write_text("x", encoding="utf-8")
                    print(historique.lister("Perdue.md")[0]["date"])
                    print(historique.recentes()[0]["date"])
                    print(historique.pages_sans_fichier(set())[0]["derniere"])
                    historique.sauvegarder("Perdue.md", "y")
                    # sur disque : toujours l'heure de la machine (noms des versions inchangés)
                    neuf = max(historique._lire_nom(f.name)[0] for f in dossier.glob("*.md"))
                    print(abs((neuf - datetime.now()).total_seconds()) < 60, len(list(dossier.glob("*.md"))))

                    class Coffre:
                        root = Path(%r) / "coffre"
                        notes = {}
                    jetee = Coffre.root / rangement.CORBEILLE / "b.md"
                    jetee.parent.mkdir(parents=True)
                    jetee.write_text("z", encoding="utf-8")
                    os.utime(jetee, (utc.timestamp(), utc.timestamp()))
                    print(rangement.contenu(Coffre)["jetees"][0]["date"])
                    db.session.remove()
                    db.engine.dispose()
            """ % (d, d))
            self.assertEqual(r.returncode, 0, r.stderr)
            lignes = r.stdout.strip().splitlines()
            self.assertEqual(lignes[:4], ["2026-11-12 18:53:00"] * 4)
            self.assertEqual(lignes[4], "True 2")
            self.assertEqual(lignes[5], "2026-11-12 18:53:00")


class ExportIcsAutreFuseauTests(unittest.TestCase):
    """Constat 9 : HANGAR_TZ=America/Montreal ; chaque TZID du flux est décrit par un VTIMEZONE."""

    SCRIPT = """
        from hangar import agenda as A, create_app, ics
        from hangar.extensions import db
        from hangar.models import AgendaEvent, User
        app = create_app(%r, testing=True)
        with app.test_request_context("/"):
            u = User(username="moi", email="moi@hangar.local", role="admin")
            u.set_password("secret1")
            db.session.add(u)
            db.session.flush()
            for uid, debut in (("ete", datetime(2026, 7, 1, 9)), ("hiver", datetime(2027, 1, 15, 9))):
                db.session.add(AgendaEvent(user_id=u.id, uid=uid, title="Cours", start=debut,
                                           end=debut + timedelta(hours=2)))
            db.session.commit()
            %s
            with a(2026, 11, 12, 12, 0):
                texte = A.export_ics(u, include_team=False)
            print(texte)
            db.session.remove()
            db.engine.dispose()
    """

    def _export(self, d, extra=""):
        r = _python(self.SCRIPT % (d, extra), env={"HANGAR_TZ": "America/Montreal"})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_vtimezone_genere_pour_un_autre_fuseau(self):
        import re
        with tempfile.TemporaryDirectory() as d:
            texte = self._export(d)
        lignes = texte.replace("\n ", "").splitlines()  # lignes dépliées
        self.assertEqual(set(re.findall(r";TZID=([^:;]+)", texte)), {"America/Montreal"})
        self.assertIn("BEGIN:VTIMEZONE", lignes)
        self.assertIn("TZID:America/Montreal", lignes)
        self.assertIn("DTSTART;TZID=America/Montreal:20260701T090000", lignes)
        self.assertTrue(all(len(l.encode("utf-8")) <= 75 for l in texte.splitlines()))
        bloc = lignes[lignes.index("BEGIN:VTIMEZONE"):lignes.index("END:VTIMEZONE")]
        decalage = _lecteur_vtimezone(bloc)
        self.assertEqual(decalage(datetime(2026, 7, 1, 9)), "-0400")
        self.assertEqual(decalage(datetime(2027, 1, 15, 9)), "-0500")
        self.assertEqual(decalage(datetime(2031, 7, 1, 9)), "-0400")  # tâches répétées : années suivantes
        self.assertEqual(decalage(datetime(2031, 12, 1, 9)), "-0500")

    def test_vtimezone_conforme_a_zoneinfo_pour_plusieurs_fuseaux(self):
        """Décalage lu dans le VTIMEZONE = décalage de zoneinfo, à chaque heure de 2024 à 2030 (hémisphère sud,
        sans heure d'été, règles qui changent)."""
        from datetime import timedelta, timezone
        from zoneinfo import ZoneInfo
        from hangar import ics
        for nom in ("America/Montreal", "Australia/Sydney", "Asia/Tokyo", "America/Sao_Paulo", "Europe/London"):
            zone = ZoneInfo(nom)
            decalage = _lecteur_vtimezone(ics.vtimezone(nom, 2015, 2027))
            u = datetime(2015, 1, 1, 12)
            while u < datetime(2031, 1, 1):
                local = u.replace(tzinfo=timezone.utc).astimezone(zone)
                self.assertEqual(decalage(local.replace(tzinfo=None)),
                                 ics._decalage_ics(local.utcoffset()), (nom, local))
                u += timedelta(hours=7)

    def test_sans_vtimezone_les_heures_sont_ecrites_en_utc(self):
        with tempfile.TemporaryDirectory() as d:
            texte = self._export(d, "ics.vtimezone = lambda *a, **k: []")
        self.assertNotIn(";TZID=", texte)
        self.assertIn("DTSTART:20260701T130000Z", texte.splitlines())
        self.assertIn("DTEND:20260701T150000Z", texte.splitlines())
        self.assertIn("DTSTART:20270115T140000Z", texte.splitlines())

    def test_paris_garde_sa_definition(self):
        from hangar import ics
        self.assertIn("TZID:Europe/Paris", ics.vtimezone("Europe/Paris"))
        self.assertIn("RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU", ics.vtimezone())


if __name__ == "__main__":
    unittest.main()
