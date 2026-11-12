"""QCM, corrections de la vague 2 (groupe « qcm ») :

- piste 19 : reprise de la progression de Revision (`hangar qcm-import-revision`), sûre et rejouable ;
- piste 22 : un fichier de QCM mal formé n'emporte plus toute la section, et son nom est donné ;
  listes de mots en UTF-16 ou à fins de ligne « CR », paquets Anki récents (collection.anki21b) ;
- piste 40 : hors ligne, la rétention visée avant un examen est la même que sur le serveur
  (fsrs.js comparé à fsrs.py avec Node, s'il est installé) ;
- piste 53 : le rappel quotidien compte comme le tableau de bord et se tait quand tout est fait ;
  envois en échec espacés, abonnement refusé plusieurs fois supprimé ;
- piste 56 : page hors ligne ouverte à une adresse non sécurisée : avertissement permanent ;
- piste 62 : notifications web : seuls les services de notification connus, sans redirection,
  sans renvoyer leur réponse au navigateur ;
- piste 63 : import .apkg, .docx et .xlsx : tailles décompressées, nombre d'entrées et durée bornés.

Lancer : uv run python -m unittest tests.test_qcm_v2c -v
Données synthétiques uniquement.
"""
import io
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from unittest import mock

from werkzeug.datastructures import FileStorage, MultiDict

from hangar import heure as horloge  # heure de l'agenda (Paris), pas celle de la machine
from hangar import create_app
from hangar.extensions import db
from hangar.models import User
from hangar.qcm import anki, paquets, quiz, verifier
from hangar.qcm.models import AbonnementPush, QuizProgress

RACINE = Path(__file__).resolve().parent.parent

SET_OK = {
    "titre": "Électricité",
    "questions": [
        {"id": "ohm", "type": "QCM", "question": "Loi d'Ohm : U = ?", "choix": ["RI", "R/I"], "reponses": [0]},
        {"id": "carte", "type": "FLASHCARD", "question": "Puissance", "verso": "P = UI"},
    ],
}
SET_CHOIX_NULL = {"titre": "Écrit par une IA",
                  "questions": [{"type": "QCM", "question": "Q ?", "choix": None, "reponses": [0]}]}
SET_REPONSES_NULL = {"titre": "IA 2",
                     "questions": [{"type": "QCR", "question": "Q ?", "choix": ["a", "b"], "reponses": [0, None]}]}
SET_TEXTE_NULL = {"titre": "IA 3", "questions": [{"type": "QRC", "question": "Q ?", "reponses_texte": None}]}


class BaseTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-qcm-v2c-")
        self.app = create_app(self.tmp, testing=True)
        self.app.config["QCM_VERIF_INTERVALLE"] = 0

        def _g_neuf():
            from flask import g
            g.pop("_login_user", None)
        self.app.before_request_funcs.setdefault(None, []).insert(0, _g_neuf)
        self.vault = Path(self.app.config["WIKI_DIR"])
        self.ecrire("QCM/Physique/elec.json", SET_OK)
        self.ctx = self.app.test_request_context("/")
        self.ctx.push()
        self.user = User(username="eleve", email="eleve@hangar.local", role="admin", first_name="Élève")
        self.user.set_password("secret1")
        self.invite = User(username="invite", email="invite@hangar.local", role="guest")
        self.invite.set_password("secret1")
        db.session.add_all([self.user, self.invite])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ecrire(self, rel, data):
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return p

    def client(self, login="eleve", password="secret1"):
        from flask import g
        g.pop("_login_user", None)
        c = self.app.test_client()
        page = c.get("/login").get_data(as_text=True)
        self.token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        r = c.post("/login", data={"login": login, "password": password, "_csrf": self.token})
        self.assertEqual(r.status_code, 302)
        return c

    def post_json(self, c, url, data):
        return c.post(url, data=json.dumps(data), content_type="application/json",
                      headers={"X-CSRF-Token": self.token})


# ---------------------------------------------------------------------------
# Piste 22 : fichiers mal formés
# ---------------------------------------------------------------------------
class FichierMalFormeTests(BaseTestCase):
    def test_un_set_mal_forme_ne_bloque_plus_la_section_et_il_est_nomme(self):
        self.ecrire("QCM/IA/choix_null.json", SET_CHOIX_NULL)
        self.ecrire("QCM/IA/reponses_null.json", SET_REPONSES_NULL)
        self.ecrire("QCM/IA/texte_null.json", SET_TEXTE_NULL)
        c = self.client()
        for url in ("/qcm/", "/qcm/aujourdhui/", "/qcm/importer/", "/qcm/mon-espace/",
                    "/qcm/hors-ligne/catalogue/", "/api/rappel/"):
            self.assertEqual(c.get(url).status_code, 200, url)
        lib = quiz.get_library()
        self.assertIn("QCM/Physique/elec", lib.sets)  # les autres sets restent utilisables
        erreurs = " | ".join(lib.errors)
        for nom in ("choix_null.json", "reponses_null.json", "texte_null.json"):
            self.assertIn(f"QCM/IA/{nom}", erreurs)
        self.assertIn("QCM/IA/choix_null", lib.ids_invalides)
        self.assertIn("QCM/IA/choix_null.json", c.get("/qcm/").get_data(as_text=True))

    def test_validation_explicite_des_listes(self):
        for data, attendu in ((SET_CHOIX_NULL, "'choix' doit être une liste"),
                              (SET_REPONSES_NULL, "indice de réponse"),
                              (SET_TEXTE_NULL, "'reponses_texte' doit être une liste")):
            with self.assertRaises(ValueError) as cm:
                quiz.set_depuis_json(data, PurePosixPath("QCM/IA/x.json"))
            self.assertIn(attendu, str(cm.exception))

    def test_json_avec_bom_accepte_et_ansi_nomme(self):
        p = self.vault / "QCM/Win/bom.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\xef\xbb\xbf" + json.dumps(SET_OK).encode("utf-8"))
        (self.vault / "QCM/Win/ansi.json").write_bytes(
            json.dumps({"titre": "Été", "questions": SET_OK["questions"]}, ensure_ascii=False).encode("cp1252"))
        lib = quiz.get_library()
        self.assertIn("QCM/Win/bom", lib.sets)
        self.assertTrue(any("QCM/Win/ansi.json" in e and "UTF-8" in e for e in lib.errors), lib.errors)

    def test_import_et_verification_d_un_fichier_mal_forme(self):
        c = self.client()
        corps = json.dumps(SET_CHOIX_NULL).encode()
        r = c.post("/qcm/importer/", data={"_csrf": self.token, "categorie": "IA",
                                           "fichiers": (io.BytesIO(corps), "ia.json")},
                   content_type="multipart/form-data", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("choix&#39; doit être une liste", r.get_data(as_text=True))
        r = c.post("/qcm/mon-espace/", data={"_csrf": self.token, "action": "json",
                                             "fichiers": (io.BytesIO(corps), "ia.json")},
                   content_type="multipart/form-data", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        # qcm-verifier : le fichier fautif est signalé, sans interrompre la vérification.
        p = self.ecrire("QCM/IA/choix_null.json", SET_CHOIX_NULL)
        ok, message, _ = verifier.verifier_fichier(p, self.vault)
        self.assertFalse(ok)
        self.assertIn("QCM/IA/choix_null.json", message)
        with mock.patch.object(quiz, "set_depuis_json", side_effect=KeyError("x")):
            ok, message, _ = verifier.verifier_fichier(self.vault / "QCM/Physique/elec.json", self.vault)
        self.assertFalse(ok)
        self.assertIn("QCM/Physique/elec.json", message)

    def _liste(self, contenu, nom="mots.txt"):
        form = MultiDict({"langue": "Anglais", "cartes": "decouverte"})
        return paquets.lire_liste(form, FileStorage(io.BytesIO(contenu), filename=nom))

    def test_liste_de_mots_utf16_et_fins_de_ligne_cr(self):
        texte = "kitchen\tcuisine\noven\tfour\nspoon\tcuillère\n"
        for contenu in (texte.replace("\n", "\r\n").encode("utf-16"),       # Excel « Texte Unicode »
                        texte.replace("\n", "\r").encode("utf-8"),          # « CSV (Macintosh) »
                        texte.replace("\t", ";").replace("\n", "\r").encode("cp1252")):
            paquet, _options, ignorees, erreur = self._liste(contenu)
            self.assertIsNone(erreur, contenu[:20])
            self.assertEqual([m["mot"] for m in paquet["vocabulaire"]], ["kitchen", "oven", "spoon"])
            self.assertEqual(paquet["vocabulaire"][2]["traductions"], ["cuillère"])
            self.assertEqual(ignorees, 0)

    def test_liste_de_mots_csv_illisible_donne_un_message(self):
        with mock.patch.object(paquets.csv, "reader", side_effect=paquets.csv.Error("boum")):
            paquet, _o, _i, erreur = self._liste("a,b\nc,d\n".encode())
        self.assertIsNone(paquet)
        self.assertIn("illisible", erreur)

    def test_paquet_anki_recent_donne_le_conseil(self):
        ancien = anki.apkg("x", "Bio", "", [("n1", "Cœur", "", False)])  # factice : verso vide
        tampon = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(ancien)) as src, zipfile.ZipFile(tampon, "w") as dst:
            dst.writestr("collection.anki2", src.read("collection.anki2"))
            dst.writestr("collection.anki21b", b"(\xb5/\xfd compresse zstd")
            dst.writestr("media", "{}")
        with self.assertRaises(anki.ImportAnkiImpossible) as cm:
            anki.lire_apkg(tampon.getvalue(), "recent.apkg")
        self.assertIn("Compatibilité avec les anciennes versions", str(cm.exception))


# ---------------------------------------------------------------------------
# Piste 63 : archives piégées (.apkg, .docx, .xlsx)
# ---------------------------------------------------------------------------
def _zip(entrees):
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as z:
        for nom, contenu in entrees.items():
            z.writestr(nom, contenu)
    return tampon.getvalue()


def _base_anki(dossier, sql):
    chemin = Path(dossier) / "c.anki2"
    base = sqlite3.connect(chemin)
    base.executescript(sql)
    base.commit()
    base.close()
    return chemin.read_bytes()


class ArchivesPiegeesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-qcm-v2c-zip-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_apkg_trop_gros_une_fois_decompresse_refuse_sans_le_lire(self):
        contenu = _zip({"collection.anki2": b"\0" * (3 * 1024 * 1024)})  # quelques Ko envoyés
        self.assertLess(len(contenu), 50_000)
        with mock.patch.object(anki, "TAILLE_MAX_BASE", 1024 * 1024), \
                mock.patch("zipfile.ZipExtFile.read", side_effect=AssertionError("lu")):
            with self.assertRaises(anki.ImportAnkiImpossible) as cm:
                anki.lire_apkg(contenu, "bombe.apkg")
        self.assertIn("décompressé", str(cm.exception))

    def test_apkg_trop_de_fichiers(self):
        from hangar import archives
        contenu = _zip({f"{i}": b"" for i in range(30)} | {"collection.anki2": b""})
        self.assertEqual(archives._nombre_entrees(contenu), 31)
        with mock.patch.object(anki, "MAX_ENTREES_APKG", 10), \
                mock.patch("zipfile.ZipFile.__init__", side_effect=AssertionError("ouvert")):
            with self.assertRaises(anki.ImportAnkiImpossible) as cm:
                anki.lire_apkg(contenu, "x.apkg")
        self.assertIn("trop de fichiers", str(cm.exception))

    def test_apkg_dont_les_notes_sont_une_vue_est_refuse(self):
        base = _base_anki(self.tmp, """
            CREATE TABLE col (decks TEXT, models TEXT);
            CREATE VIEW notes AS WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 300000000)
                SELECT x AS id, 1 AS mid, 'a' || char(31) || 'b' AS flds, '' AS tags FROM n;
        """)
        debut = horloge.now()
        with self.assertRaises(anki.ImportAnkiImpossible) as cm:
            anki.lire_apkg(_zip({"collection.anki2": base}), "vue.apkg")
        self.assertIn("ce n'est pas un paquet Anki", str(cm.exception))
        self.assertLess((horloge.now() - debut).total_seconds(), 2)

    def test_apkg_lecture_trop_longue_interrompue(self):
        base = _base_anki(self.tmp, """
            CREATE TABLE col (decks TEXT, models TEXT);
            CREATE TABLE notes (id INTEGER, mid INTEGER, flds TEXT, tags TEXT);
            WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 20000)
                INSERT INTO notes SELECT -x, 1, 'r' || char(31) || 'v', '' FROM n;
        """)
        with mock.patch.object(anki, "DUREE_MAX_LECTURE", -1):
            with self.assertRaises(anki.ImportAnkiImpossible) as cm:
                anki.lire_apkg(_zip({"collection.anki2": base}), "lent.apkg")
        self.assertIn("trop longue", str(cm.exception))
        # Sans la limite de temps : 5 000 notes au plus, lues directement par la requête.
        s = anki.lire_apkg(_zip({"collection.anki2": base}), "lent.apkg")
        self.assertEqual(len(s["questions"]), anki.MAX_NOTES_IMPORT)

    def test_apkg_colonne_generee_refusee(self):
        # Quelques Ko envoyés, mais chaque note « contiendrait » 4 Mo calculés à la lecture.
        base = _base_anki(self.tmp, """
            CREATE TABLE col (decks TEXT, models TEXT);
            INSERT INTO col VALUES ('{}', '{}');
            CREATE TABLE notes (id INTEGER, mid INTEGER, tags TEXT,
                                flds TEXT GENERATED ALWAYS AS (printf('%.*c', 4000000, 'x')) VIRTUAL);
            WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 200)
                INSERT INTO notes (id, mid, tags) SELECT x, 1, '' FROM n;
        """)
        contenu = _zip({"collection.anki2": base})
        self.assertLess(len(contenu), 20_000)
        debut = horloge.now()
        with self.assertRaises(anki.ImportAnkiImpossible) as cm:
            anki.lire_apkg(contenu, "calcul.apkg")
        self.assertIn("ce n'est pas un paquet Anki", str(cm.exception))
        self.assertLess((horloge.now() - debut).total_seconds(), 2)

    def test_apkg_volume_total_des_notes_borne(self):
        # Une colonne ajoutée après coup avec une longue valeur par défaut : chaque note la renvoie
        # sans qu'elle soit stockée. Seul un budget global sur ce qui est lu protège la mémoire.
        base = _base_anki(self.tmp, f"""
            CREATE TABLE col (decks TEXT, models TEXT);
            INSERT INTO col VALUES ('{{}}', '{{}}');
            CREATE TABLE notes (id INTEGER, mid INTEGER, tags TEXT);
            WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 1000)
                INSERT INTO notes SELECT x, 1, '' FROM n;
            ALTER TABLE notes ADD COLUMN flds TEXT DEFAULT '{"x" * 100_000}';
        """)
        contenu = _zip({"collection.anki2": base})
        self.assertLess(len(contenu), 20_000)  # 100 Mo de texte une fois lu
        with mock.patch.object(anki, "VOLUME_MAX_NOTES", 10 * 1024 * 1024):
            with self.assertRaises(anki.ImportAnkiImpossible) as cm:
                anki.lire_apkg(contenu, "lourd.apkg")
        self.assertIn("trop volumineux", str(cm.exception))

    def test_paquet_anki_sain_toujours_importe(self):
        contenu = anki.apkg("x", "Bio", "", [("n1", "Cœur", "Pompe", False), ("n2", "Rein", "Filtre", True)])
        s = anki.lire_apkg(contenu, "bio.apkg")
        self.assertEqual([q["question"] for q in s["questions"]], ["Cœur", "Rein"])

    def test_docx_piege(self):
        from hangar import importer
        xml = (b'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
               b"<w:body><w:p><w:r><w:t>Bonjour</w:t></w:r></w:p></w:body></w:document>")
        self.assertIn("Bonjour", importer.docx_to_markdown(_zip({"word/document.xml": xml})))
        gros = _zip({"word/document.xml": xml + b" " * (3 * 1024 * 1024)})
        with mock.patch.object(importer, "DOCX_TAILLE_MAX_XML", 1024 * 1024):
            with self.assertRaises(ValueError) as cm:
                importer.docx_to_markdown(gros, "cr.docx")
        self.assertIn("cr.docx", str(cm.exception))
        self.assertIn("décompressé", str(cm.exception))
        rires = (b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
                 b"<l>&b;</l>")
        with self.assertRaises(ValueError):
            importer.docx_to_markdown(_zip({"word/document.xml": rires}))
        with self.assertRaises(ValueError):
            importer.docx_to_markdown(b"pas un zip")

    def test_xlsx_piege(self):
        from hangar import importer
        contenu = _zip({"xl/workbook.xml": b"\0" * (3 * 1024 * 1024)})
        with mock.patch.object(importer, "XLSX_TAILLE_MAX", 1024 * 1024), \
                mock.patch("openpyxl.load_workbook", side_effect=AssertionError("ouvert")):
            with self.assertRaises(ValueError) as cm:
                importer.read_table(io.BytesIO(contenu), "taches.xlsx")
        self.assertIn("décompressé", str(cm.exception))

    def test_xlsx_cellules_vides_mises_en_forme_au_dela_de_la_borne(self):
        # Deux tickets, et une cellule seulement colorée tout en bas : le classeur s'importe.
        import openpyxl
        from openpyxl.styles import PatternFill

        from hangar import importer
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Titre", "État"])
        ws.append(["A", "À faire"])
        ws.append(["B", "Terminé"])
        ws["A30000"].fill = PatternFill("solid", fgColor="FFFF00")
        tampon = io.BytesIO()
        wb.save(tampon)
        lignes = importer.read_table(io.BytesIO(tampon.getvalue()), "taches.xlsx")
        self.assertEqual([l["title"] for l in lignes], ["A", "B"])
        # Au-delà de la borne en lignes réellement remplies : refus clair.
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Titre"])
        for i in range(30):
            ws.append([f"T{i}"])
        tampon = io.BytesIO()
        wb.save(tampon)
        with mock.patch.object(importer, "XLSX_MAX_LIGNES", 20):
            with self.assertRaises(ValueError) as cm:
                importer.read_table(io.BytesIO(tampon.getvalue()), "taches.xlsx")
        self.assertIn("20 lignes", str(cm.exception))


class ImportApkgPiegeHttpTests(BaseTestCase):
    def test_mon_espace_d_un_invite_renvoie_un_message(self):
        base = _base_anki(self.tmp, "CREATE TABLE col (decks TEXT, models TEXT); CREATE VIEW notes AS SELECT 1 AS id;")
        c = self.client("invite")
        r = c.post("/qcm/mon-espace/", data={"_csrf": self.token, "action": "json",
                                             "fichiers": (io.BytesIO(_zip({"collection.anki2": base})), "p.apkg")},
                   content_type="multipart/form-data", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("ce n&#39;est pas un paquet Anki", r.get_data(as_text=True))


# ---------------------------------------------------------------------------
# Piste 53 : rappel quotidien
# ---------------------------------------------------------------------------
FCM = "https://fcm.googleapis.com/fcm/send/appareil-1"


class RappelTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        cartes = [{"id": f"c{i}", "type": "FLASHCARD", "question": f"Recto {i}", "verso": "V"} for i in range(30)]
        self.ecrire("QCM/Droit/gros.json", {"titre": "Gros set", "questions": cartes})
        self.now = horloge.now().replace(hour=19, minute=30, second=0, microsecond=0)

    def _vue(self, set_id, qid, echeance, quand):
        db.session.add(QuizProgress(user_id=self.user.id, set_id=set_id, question_id=qid, box=1, seen=1, correct=1,
                                    last_review=quand, premiere_revue=quand, last_correct=True, stabilite=3.0,
                                    difficulte=5.0, echeance=echeance, decouverte=quand))

    def test_rien_a_rappeler_quand_les_revisions_sont_faites(self):
        from hangar.qcm import rappels
        from hangar.qcm.models import Activite
        hier = self.now - timedelta(days=1)
        for i in range(10):
            self._vue("QCM/Droit/gros", f"c{i}", self.now + timedelta(days=3), hier)
        db.session.add(Activite(user_id=self.user.id, jour=self.now.date(), reponses=10, justes=10))
        db.session.commit()
        etat = rappels.etat_rappel(self.user.id, self.now)
        # 20 + 2 questions jamais vues, aucun quota : rien n'est dû (le tableau de bord dit « 0 »).
        self.assertEqual((etat["a_revoir"], etat["qcm"], etat["nouvelles"]), (0, 0, 0))
        self.assertIsNone(rappels.texte_rappel(etat))

    def test_rappel_distingue_revisions_et_nouvelles_prevues(self):
        from hangar.qcm import rappels
        from hangar.qcm.models import QuizDeck
        from hangar.qcm.services import resume_du_jour
        hier = self.now - timedelta(days=1)
        self._vue("QCM/Droit/gros", "c0", hier, hier - timedelta(days=3))  # due
        db.session.add(QuizDeck(user_id=self.user.id, set_id="QCM/Droit/gros", nouvelles_jour=5))
        db.session.commit()
        etat = rappels.etat_rappel(self.user.id, self.now)
        self.assertEqual((etat["a_revoir"], etat["qcm"], etat["langues"], etat["nouvelles"]), (6, 6, 0, 5))
        self.assertEqual(rappels.texte_rappel(etat), "1 à revoir · 5 nouvelles à découvrir")
        # Même compte que le tableau de bord
        self.assertEqual(resume_du_jour(self.user)["cartes_dues"], 6)

    def test_envoi_en_echec_reessaye_15_minutes_plus_tard(self):
        import requests

        from hangar.qcm import rappels
        db.session.add(AbonnementPush(user_id=self.user.id, endpoint=FCM, p256dh="k", auth="a", heure=19, minute=0))
        db.session.commit()
        with mock.patch("pywebpush.webpush", side_effect=requests.ConnectionError("pas de réseau")) as wp:
            for s in range(0, 600, 30):  # le fil d'arrière-plan passe toutes les 30 s pendant 10 min
                c = rappels.envoyer_rappels_dus(self.now + timedelta(seconds=s))
            self.assertEqual(wp.call_count, 1)
            self.assertEqual(c["erreurs"], 0)
            rappels.envoyer_rappels_dus(self.now + timedelta(minutes=16))
            self.assertEqual(wp.call_count, 2)
        a = AbonnementPush.query.one()  # une panne réseau ne supprime pas l'abonnement
        self.assertIsNone(a.dernier_envoi)
        with mock.patch("pywebpush.webpush") as wp:
            self.assertEqual(rappels.envoyer_rappels_dus(self.now + timedelta(minutes=32))["envoyes"], 1)
        a = AbonnementPush.query.one()
        self.assertEqual((a.dernier_envoi, a.dernier_echec, a.refus), (self.now.date(), None, 0))

    def test_abonnement_refuse_plusieurs_fois_supprime(self):
        from pywebpush import WebPushException

        from hangar.qcm import rappels
        db.session.add(AbonnementPush(user_id=self.user.id, endpoint=FCM, p256dh="k", auth="a", heure=19, minute=0))
        db.session.commit()
        refus = WebPushException("refusé", response=mock.Mock(status_code=403, text=""))
        with mock.patch("pywebpush.webpush", side_effect=refus) as wp:
            for k in range(rappels.MAX_REFUS):
                rappels.envoyer_rappels_dus(self.now + k * timedelta(minutes=20))
        self.assertEqual(wp.call_count, rappels.MAX_REFUS)
        self.assertEqual(AbonnementPush.query.count(), 0)


# ---------------------------------------------------------------------------
# Piste 62 : adresses des notifications web
# ---------------------------------------------------------------------------
class AdressesPushTests(BaseTestCase):
    def _abonner(self, c, endpoint):
        return self.post_json(c, "/api/push/abonner/", {"subscription": {"endpoint": endpoint,
                                                                         "keys": {"p256dh": "k", "auth": "a"}},
                                                        "heure": 19, "minute": 0})

    def test_seuls_les_services_de_notification_connus_sont_acceptes(self):
        c = self.client("invite")
        for refusee in ("https://localhost:8443/admin/etat", "https://127.0.0.1/", "https://192.168.1.1/",
                        "https://intranet.exemple.invalid/admin", "https://fcm.googleapis.com.exemple.org/x",
                        "https://fcm.googleapis.com:8443/x", "https://u:p@fcm.googleapis.com/x",
                        "http://fcm.googleapis.com/fcm/send/x", "https://[::1]/x", "https://notify.windows.com.evil/x"):
            self.assertEqual(self._abonner(c, refusee).status_code, 400, refusee)
        self.assertEqual(AbonnementPush.query.count(), 0)
        for acceptee in (FCM, "https://updates.push.services.mozilla.com/wpush/v2/abc",
                         "https://wns2-par02p.notify.windows.com/w/?token=abc", "https://web.push.apple.com/QAbc",
                         "https://FCM.googleapis.com./fcm/send/x"):
            self.assertEqual(self._abonner(c, acceptee).status_code, 200, acceptee)

    def test_le_test_ne_renvoie_pas_la_reponse_du_service(self):
        from pywebpush import WebPushException
        c = self.client("invite")
        self._abonner(c, FCM)
        erreur = WebPushException("Push failed: 403 Forbidden\nResponse body:<html>version 1.2.3 secret</html>",
                                  response=mock.Mock(status_code=403, text="<html>secret</html>"))
        for effet in (erreur, ConnectionRefusedError("Connection refused")):
            with mock.patch("pywebpush.webpush", side_effect=effet):
                r = self.post_json(c, "/api/push/tester/", {"endpoint": FCM})
            self.assertEqual(r.status_code, 502)
            corps = r.get_data(as_text=True)
            self.assertNotIn("secret", corps)
            self.assertNotIn("refused", corps)

    def test_abonnement_ancien_vers_une_adresse_locale_supprime_sans_requete(self):
        from hangar.qcm import rappels
        db.session.add(AbonnementPush(user_id=self.invite.id, endpoint="https://192.168.1.1/admin", p256dh="k",
                                      auth="a", heure=0, minute=0))
        db.session.commit()
        with mock.patch("pywebpush.webpush") as wp:
            c = rappels.envoyer_rappels_dus()
        self.assertEqual((wp.call_count, c["expires"]), (0, 1))
        self.assertEqual(AbonnementPush.query.count(), 0)

    def test_l_envoi_ne_suit_aucune_redirection(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        import requests

        from hangar.qcm import push
        visites = []

        class Gestionnaire(BaseHTTPRequestHandler):
            def do_POST(self):
                visites.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/admin/wifi")
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST

            def log_message(self, *a):
                pass

        serveur = HTTPServer(("127.0.0.1", 0), Gestionnaire)
        fil = threading.Thread(target=serveur.serve_forever, daemon=True)
        fil.start()
        try:
            session = push._session_sans_redirection()
            with self.assertRaises(requests.TooManyRedirects):
                session.post(f"http://127.0.0.1:{serveur.server_port}/push", data=b"x", timeout=5,
                             proxies={"http": None, "https": None})
            session.close()
        finally:
            serveur.shutdown()
            serveur.server_close()
        self.assertEqual(visites, ["/push"])  # /admin/wifi jamais demandé
        # Et c'est bien cette session que reçoit pywebpush
        abonnement = AbonnementPush(user_id=self.user.id, endpoint=FCM, p256dh="k", auth="a")
        with mock.patch("pywebpush.webpush") as wp:
            push.envoyer(abonnement, "t", "x")
        self.assertEqual(wp.call_args.kwargs["requests_session"].max_redirects, 0)


# ---------------------------------------------------------------------------
# Piste 40 : planification hors ligne avant un examen (fsrs.js = fsrs.py)
# ---------------------------------------------------------------------------
NODE = shutil.which("node")
FSRS_JS = RACINE / "hangar" / "static" / "qcm" / "fsrs.js"
# Charge fsrs.js (écrit pour le navigateur) dans Node et applique les cas reçus sur l'entrée standard.
SCRIPT_NODE = r"""
const fs = require("fs");
globalThis.window = globalThis;
eval(fs.readFileSync(process.argv[1], "utf8"));
const F = window.FSRS;
const cas = JSON.parse(fs.readFileSync(0, "utf8"));
const out = { suites: [], retentions: [] };
for (const c of cas.suites) {
  let etat = null;
  const pas = [];
  for (const [jours, note, retention] of c) {
    etat = etat ? F.suivante(etat, jours, note) : F.premiere(note);
    pas.push([etat.stabilite, etat.difficulte, F.delaiMs(etat, note, retention) / F.JOUR_MS]);
  }
  out.suites.push(pas);
}
for (const [date, now] of cas.retentions) out.retentions.push(F.retentionCible(date, now));
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "Node.js absent : comparaison de fsrs.js ignorée")
class PariteFsrsJsTests(unittest.TestCase):
    def _node(self, cas):
        r = subprocess.run([NODE, "-e", SCRIPT_NODE, str(FSRS_JS)], input=json.dumps(cas), capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_fsrs_js_identique_a_fsrs_py_avec_ou_sans_examen(self):
        import random

        from hangar.qcm import fsrs, langues
        hasard = random.Random(40)
        suites = []
        for _ in range(300):
            suites.append([[round(hasard.uniform(0, 60), 3), hasard.choice(fsrs.NOTES), hasard.choice([None, 0.9, 0.95])]
                           for _ in range(hasard.randint(1, 8))])
        suites.append([[0, fsrs.BIEN, None], [22, fsrs.BIEN, 0.95]])
        js = self._node({"suites": suites, "retentions": []})["suites"]
        for suite, pas_js in zip(suites, js):
            etat = None
            for (jours, note, retention), (s_js, d_js, delai_js) in zip(suite, pas_js):
                etat = fsrs.suivante(etat, jours, note) if etat else fsrs.premiere(note)
                self.assertAlmostEqual(etat.stabilite, s_js, places=9)
                self.assertAlmostEqual(etat.difficulte, d_js, places=9)
                delai = langues.delai(etat, note, retention)
                self.assertAlmostEqual(delai.total_seconds() / 86400, delai_js, places=6, msg=(suite, retention))

    def test_retention_cible_identique_au_serveur(self):
        from hangar.qcm import planning
        cas, attendu = [], []
        for heure in (0, 9, 23):
            now = datetime(2026, 11, 12, heure, 30)
            for jours in (-3, 0, 1, 2, 14, 29, 30, 31, 60):
                d = now.date() + timedelta(days=jours)
                cas.append([d.isoformat(), now.timestamp() * 1000])
                attendu.append(planning.retention_cible(d, now))
            cas.append([None, now.timestamp() * 1000])
            attendu.append(planning.retention_cible(None, now))
        self.assertEqual(self._node({"suites": [], "retentions": cas})["retentions"], attendu)

    def test_carte_reussie_deux_semaines_avant_l_examen(self):
        # Le cas du constat : stabilité 22,7 jours, examen dans 14 jours → 10 jours (serveur), plus 23.
        from hangar.qcm import fsrs, langues, planning
        now = datetime(2026, 11, 12, 10)
        examen = (now.date() + timedelta(days=14)).isoformat()
        script = (f"globalThis.window=globalThis;eval(require('fs').readFileSync({json.dumps(str(FSRS_JS))},'utf8'));"
                  f"const F=window.FSRS;process.stdout.write(String(F.delaiMs({{stabilite:22.7}},3,"
                  f"F.retentionCible({json.dumps(examen)},{now.timestamp() * 1000}))/F.JOUR_MS))")
        r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
        serveur = langues.delai(fsrs.Etat(22.7, 5), fsrs.BIEN, planning.retention_cible(date.fromisoformat(examen), now))
        self.assertEqual(float(r.stdout), serveur.days)
        self.assertEqual(serveur.days, 10)


class HorsLigneExamenTests(BaseTestCase):
    def test_le_paquet_hors_ligne_donne_la_date_d_examen(self):
        from hangar.qcm.models import QuizDeck
        db.session.add(QuizDeck(user_id=self.user.id, set_id="QCM/Physique/elec", date_objectif=date(2026, 12, 1)))
        db.session.commit()
        c = self.client()
        sets = c.get("/qcm/hors-ligne/sets/?id=QCM/Physique/elec").get_json()["sets"]
        self.assertEqual(sets[0]["date_objectif"], "2026-12-01")
        js = (RACINE / "hangar/static/qcm/qcm_hors_ligne.js").read_text(encoding="utf-8")
        self.assertIn("F.retentionCible(set.date_objectif, now)", js)


# ---------------------------------------------------------------------------
# Piste 56 : page hors ligne ouverte à une adresse non sécurisée
# ---------------------------------------------------------------------------
# Lance qcm_hors_ligne.js dans Node avec un faux document minimal (aucun réseau, base vide) et
# renvoie l'état de l'avertissement et les textes affichés.
SCRIPT_PAGE = r"""
const fs = require("fs");
const [fichier, securise, appli] = process.argv.slice(1);
const elements = {}, crees = [];
function faux() {
  const cible = { hidden: true, textContent: "", dataset: {}, style: {}, classList: { toggle() {}, add() {}, remove() {} } };
  return new Proxy(cible, { get: (t, k) => (k in t ? t[k] : () => []), set: (t, k, v) => { t[k] = v; return true; } });
}
globalThis.window = globalThis;
globalThis.document = {
  getElementById: (id) => (elements[id] = elements[id] || faux()),
  createElement: () => { const e = faux(); crees.push(e); return e; },
  querySelectorAll: () => [], addEventListener() {}, title: "",
};
Object.defineProperty(globalThis, "navigator", { value: { onLine: false }, configurable: true });
globalThis.location = { hash: "", pathname: "/qcm/hors-ligne/", search: "" };
globalThis.history = { replaceState() {} };
globalThis.addEventListener = () => {};
globalThis.isSecureContext = securise === "1";
if (appli === "1") globalThis.QcmReseau = { fetch() {} };
globalThis.QcmAttente = { tous: async () => [], lire: async () => null, ecrire: async () => {}, Deconnecte: class {} };
globalThis.FSRS = {};
eval(fs.readFileSync(fichier, "utf8"));
setTimeout(() => process.stdout.write(JSON.stringify({
  avertissement: !document.getElementById("hl-non-securise").hidden,
  sousTitre: document.getElementById("hl-sous-titre").textContent,
  textes: crees.map((e) => e.textContent).filter(Boolean),
})), 50);
"""


class AdresseNonSecuriseeTests(BaseTestCase):
    @unittest.skipUnless(NODE, "Node.js absent")
    def test_avertissement_permanent_hors_contexte_securise(self):
        page = RACINE / "hangar/static/qcm/qcm_hors_ligne.js"

        def lancer(securise, appli=False):
            r = subprocess.run([NODE, "-e", SCRIPT_PAGE, str(page), "1" if securise else "0", "1" if appli else "0"],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads(r.stdout)

        http = lancer(securise=False)  # http://192.168.x.x
        self.assertTrue(http["avertissement"])
        self.assertIn("ne s'ouvre pas sans réseau", http["sousTitre"])
        self.assertFalse(any("disponibles sans réseau" in t for t in http["textes"]), http["textes"])
        self.assertTrue(any("gardés sur l'appareil" in t for t in http["textes"]), http["textes"])
        for res in (lancer(securise=True), lancer(securise=False, appli=True)):  # HTTPS ou 127.0.0.1 ; appli Android
            self.assertFalse(res["avertissement"])
            self.assertTrue(any("disponibles sans réseau" in t for t in res["textes"]), res["textes"])

    def test_la_page_contient_l_avertissement(self):
        c = self.client()
        page = c.get("/qcm/hors-ligne/").get_data(as_text=True)
        self.assertRegex(page, r'id="hl-non-securise" hidden')
        self.assertIn("utilise l’appli Android, ou ouvre Hangar par une adresse en HTTPS", page)


# ---------------------------------------------------------------------------
# Piste 19 : reprise de la progression de Revision
# ---------------------------------------------------------------------------
# Schéma des tables de Revision tel que Django le crée sous SQLite (wiki/models.py de Revision).
SCHEMA_REVISION = """
CREATE TABLE "wiki_quizprogress" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "player" varchar(150) NOT NULL,
  "set_id" varchar(500) NOT NULL, "question_id" varchar(255) NOT NULL, "box" smallint unsigned NOT NULL,
  "seen" integer unsigned NOT NULL, "correct" integer unsigned NOT NULL, "last_review" datetime NULL,
  "premiere_revue" datetime NULL, "last_correct" bool NULL, "stabilite" real NULL, "difficulte" real NULL,
  "echeance" datetime NULL, "decouverte" datetime NULL, "oublis" integer unsigned NOT NULL,
  UNIQUE ("player", "set_id", "question_id"));
CREATE TABLE "wiki_quizdeck" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "player" varchar(150) NOT NULL,
  "set_id" varchar(500) NOT NULL, "nouvelles_jour" integer unsigned NULL, "revisions_jour" integer unsigned NULL,
  "date_objectif" date NULL, "auto_quota" bool NOT NULL, "temps_ms" bigint unsigned NOT NULL,
  "reponses_chronometrees" integer unsigned NOT NULL, UNIQUE ("player", "set_id"));
CREATE TABLE "wiki_activite" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "player" varchar(150) NOT NULL,
  "jour" date NOT NULL, "reponses" integer unsigned NOT NULL, "justes" integer unsigned NOT NULL,
  "nouveaux" integer unsigned NOT NULL, UNIQUE ("player", "jour"));
CREATE TABLE "wiki_tempsrevision" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "player" varchar(150) NOT NULL,
  "jour" date NOT NULL, "matiere" varchar(200) NOT NULL, "temps_ms" bigint unsigned NOT NULL,
  "reponses" integer unsigned NOT NULL, UNIQUE ("player", "jour", "matiere"));
CREATE TABLE "wiki_objectifrevision" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT,
  "player" varchar(150) NOT NULL UNIQUE, "reponses_jour" integer unsigned NULL);
CREATE TABLE "wiki_explication" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "set_id" varchar(500) NOT NULL,
  "question_id" varchar(255) NOT NULL, "texte" text NOT NULL, "modele" varchar(64) NOT NULL,
  "jetons_entree" integer unsigned NOT NULL, "jetons_sortie" integer unsigned NOT NULL, "cree_le" datetime NOT NULL,
  UNIQUE ("set_id", "question_id"));
"""
SET_PERSO = {"titre": "Mes fiches", "questions": [{"id": "f1", "type": "FLASHCARD", "question": "Recto", "verso": "Verso"}]}


def _local(texte_utc):
    """Heure naïve de l'agenda (Paris) d'une date-heure UTC de Django (comme la reprise)."""
    from hangar import heure
    return heure.vers_agenda(datetime.fromisoformat(texte_utc).replace(tzinfo=timezone.utc)).replace(microsecond=0)


class RepriseRevisionTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.pi = Path(self.tmp) / "revision-data"  # DATA_DIR de Revision, copié depuis le Pi
        (self.pi / "perso" / "eleve" / "Mes sets").mkdir(parents=True)
        (self.pi / "perso" / "eleve" / "Mes sets" / "fiches.json").write_text(json.dumps(SET_PERSO), encoding="utf-8")
        self.base = self.pi / "db.sqlite3"
        b = sqlite3.connect(self.base)
        b.executescript(SCHEMA_REVISION)
        prog = "INSERT INTO wiki_quizprogress (player, set_id, question_id, box, seen, correct, last_review, " \
               "premiere_revue, last_correct, stabilite, difficulte, echeance, decouverte, oublis) " \
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        b.executemany(prog, [
            ("eleve", "QCM/Physique/elec", "ohm", 3, 5, 4, "2026-11-10 08:30:00.123456", "2026-10-01 07:00:00", 1,
             22.7, 4.2, "2026-12-02 08:30:00", "2026-10-01 07:00:00", 1),
            ("eleve", "QCM/Physique/elec", "carte", 1, 2, 1, "2026-11-09 23:30:00", "2026-11-01 10:00:00", 0,
             1.2, 6.0, "2026-11-09 23:40:00", "2026-11-01 10:00:00", 1),
            ("eleve", "~eleve/Mes sets/fiches", "f1", 1, 1, 1, "2026-11-08 12:00:00", "2026-11-08 12:00:00", 1,
             3.1, 5.0, "2026-11-11 12:00:00", "2026-11-08 12:00:00", 0),
            ("autre", "QCM/Physique/elec", "ohm", 1, 1, 0, "2026-11-10 09:00:00", "2026-11-10 09:00:00", 0,
             0.4, 7.0, "2026-11-10 09:10:00", "2026-11-10 09:00:00", 0),
        ])
        b.execute("INSERT INTO wiki_quizdeck (player, set_id, nouvelles_jour, revisions_jour, date_objectif, auto_quota, "
                  "temps_ms, reponses_chronometrees) VALUES ('eleve', 'QCM/Physique/elec', 10, NULL, '2026-12-15', 1, 84000, 7)")
        b.executemany("INSERT INTO wiki_activite (player, jour, reponses, justes, nouveaux) VALUES (?, ?, ?, ?, ?)",
                      [("eleve", "2026-11-09", 30, 25, 5), ("eleve", "2026-11-10", 12, 10, 0)])
        b.execute("INSERT INTO wiki_tempsrevision (player, jour, matiere, temps_ms, reponses) "
                  "VALUES ('eleve', '2026-11-10', 'Physique', 600000, 12)")
        b.execute("INSERT INTO wiki_objectifrevision (player, reponses_jour) VALUES ('eleve', 40)")
        b.execute("INSERT INTO wiki_explication (set_id, question_id, texte, modele, jetons_entree, jetons_sortie, cree_le) "
                  "VALUES ('QCM/Physique/elec', 'ohm', 'U = R × I.', 'claude', 100, 50, '2026-11-01 10:00:00')")
        b.commit()
        b.close()

    def _empreinte_base(self):
        import hashlib
        return hashlib.sha256(self.base.read_bytes()).hexdigest()

    def test_reprise_complete_sans_toucher_a_la_base_de_revision(self):
        from hangar.qcm import reprise
        from hangar.qcm.models import Activite, Explication, ObjectifRevision, QuizDeck, TempsRevision
        avant = self._empreinte_base()
        bilan = reprise.reprendre(self.base, self.user)
        self.assertEqual(self._empreinte_base(), avant)  # base de Revision intacte
        self.assertEqual(bilan.progression["ajoutees"], 3)
        p = QuizProgress.query.filter_by(user_id=self.user.id, set_id="QCM/Physique/elec", question_id="ohm").one()
        self.assertEqual((p.box, p.seen, p.correct, p.last_correct, p.stabilite, p.oublis), (3, 5, 4, True, 22.7, 1))
        self.assertEqual(p.last_review, _local("2026-11-10 08:30:00"))  # UTC → heure locale
        self.assertEqual(p.echeance, _local("2026-12-02 08:30:00"))
        # Le joueur « autre » n'est pas repris dans ce compte
        self.assertEqual(QuizProgress.query.filter_by(user_id=self.user.id).count(), 3)
        d = QuizDeck.query.filter_by(user_id=self.user.id, set_id="QCM/Physique/elec").one()
        self.assertEqual((d.nouvelles_jour, d.revisions_jour, d.date_objectif, d.auto_quota, d.temps_ms),
                         (10, None, date(2026, 12, 15), True, 84000))
        self.assertEqual({a.jour: a.reponses for a in Activite.query.filter_by(user_id=self.user.id)},
                         {date(2026, 11, 9): 30, date(2026, 11, 10): 12})  # dates simples telles quelles
        self.assertEqual(TempsRevision.query.filter_by(user_id=self.user.id).one().temps_ms, 600000)
        self.assertEqual(ObjectifRevision.de(self.user.id), 40)
        self.assertEqual(Explication.query.one().texte, "U = R × I.")
        # Espace personnel : fichier copié, identifiant « ~eleve/… » devenu « ~<id du compte>/… »
        perso_id = f"~{self.user.id}/Mes sets/fiches"
        self.assertTrue((Path(self.tmp) / "qcm_perso" / str(self.user.id) / "Mes sets" / "fiches.json").is_file())
        self.assertIn(perso_id, quiz.get_library().sets)
        self.assertEqual(QuizProgress.query.filter_by(user_id=self.user.id, set_id=perso_id).count(), 1)
        self.assertEqual(bilan.introuvables, [])
        # Visible dans Hangar : la question révisée n'est plus « nouvelle »
        c = self.client()
        self.assertEqual(c.get("/qcm/").status_code, 200)

    def test_relancer_ne_double_rien_et_garde_ce_qui_est_plus_recent(self):
        from hangar.qcm import reprise
        from hangar.qcm.models import Activite, QuizDeck
        reprise.reprendre(self.base, self.user)
        # Entre-temps, Hangar a servi : nouvelle révision de « ohm », 50 réponses le 10/11, quota changé.
        p = QuizProgress.query.filter_by(user_id=self.user.id, question_id="ohm").one()
        recente = datetime(2026, 11, 12, 18, 0)
        p.last_review, p.seen, p.box = recente, 6, 4
        Activite.query.filter_by(user_id=self.user.id, jour=date(2026, 11, 10)).one().reponses = 50
        QuizDeck.query.filter_by(user_id=self.user.id).one().nouvelles_jour = 20
        db.session.commit()
        etat_avant = sorted((q.set_id, q.question_id, q.seen, q.last_review) for q in QuizProgress.query.all())
        bilan = reprise.reprendre(self.base, self.user)
        self.assertEqual(bilan.progression["ajoutees"], 0)
        self.assertEqual(sorted((q.set_id, q.question_id, q.seen, q.last_review) for q in QuizProgress.query.all()),
                         etat_avant)
        p = QuizProgress.query.filter_by(user_id=self.user.id, question_id="ohm").one()
        self.assertEqual((p.last_review, p.seen, p.box), (recente, 6, 4))
        self.assertEqual({a.jour: a.reponses for a in Activite.query.filter_by(user_id=self.user.id)},
                         {date(2026, 11, 9): 30, date(2026, 11, 10): 50})
        self.assertEqual(QuizDeck.query.filter_by(user_id=self.user.id).one().nouvelles_jour, 20)
        self.assertEqual(QuizDeck.query.count(), 1)

    def test_revision_plus_recente_l_emporte(self):
        from hangar.qcm import reprise
        # Progression plus ancienne déjà dans Hangar (par exemple une réponse donnée en essayant Hangar)
        db.session.add(QuizProgress(user_id=self.user.id, set_id="QCM/Physique/elec", question_id="ohm", box=0, seen=1,
                                    correct=0, last_review=datetime(2026, 10, 1, 9), premiere_revue=datetime(2026, 9, 1, 9),
                                    stabilite=0.4, difficulte=7.0, echeance=datetime(2026, 10, 1, 9, 10)))
        db.session.commit()
        reprise.reprendre(self.base, self.user)
        p = QuizProgress.query.filter_by(user_id=self.user.id, question_id="ohm").one()
        self.assertEqual((p.stabilite, p.box, p.seen, p.last_review), (22.7, 3, 5, _local("2026-11-10 08:30:00")))
        self.assertEqual(p.premiere_revue, datetime(2026, 9, 1, 9))  # la plus ancienne des deux

    def test_set_renomme_retrouve_par_son_contenu(self):
        from hangar.qcm import reprise
        # Dans Revision, le set s'appelait QCM/Ancien/electricite ; dans Hangar, il a été rangé ailleurs.
        coffre = Path(self.tmp) / "revision-vault"
        (coffre / "QCM" / "Ancien").mkdir(parents=True)
        (coffre / "QCM" / "Ancien" / "electricite.json").write_text(json.dumps(SET_OK), encoding="utf-8")
        b = sqlite3.connect(self.base)
        b.execute("UPDATE wiki_quizprogress SET set_id = 'QCM/Ancien/electricite' WHERE set_id = 'QCM/Physique/elec'")
        b.execute("UPDATE wiki_quizdeck SET set_id = 'QCM/Ancien/electricite'")
        b.commit()
        b.close()
        sans = reprise.reprendre(self.base, self.user, essai=True)  # sans le coffre : introuvable, rien d'enregistré
        self.assertEqual(sans.introuvables, ["QCM/Ancien/electricite"])
        self.assertEqual(QuizProgress.query.count(), 0)
        bilan = reprise.reprendre(self.base, self.user, coffre=coffre)
        self.assertEqual(bilan.par_contenu, {"QCM/Ancien/electricite": "QCM/Physique/elec"})
        self.assertEqual(QuizProgress.query.filter_by(user_id=self.user.id, set_id="QCM/Physique/elec").count(), 2)
        from hangar.qcm.models import QuizDeck
        self.assertEqual(QuizDeck.query.one().set_id, "QCM/Physique/elec")

    def test_commande(self):
        from argparse import Namespace

        from hangar.qcm import cli
        args = dict(data=self.tmp, base=str(self.base), compte="ELEVE@hangar.local", joueur=None, coffre=None,
                    perso=None, essai=False)
        with mock.patch("builtins.print") as imprime:
            cli.cmd_import_revision(Namespace(**args))
        sortie = "\n".join(" ".join(map(str, c.args)) for c in imprime.call_args_list)
        self.assertIn("3 ajoutée(s)", sortie)
        self.assertIn("relancer la commande sans risque", sortie)
        with mock.patch("builtins.print") as imprime, self.assertRaises(SystemExit):
            cli.cmd_import_revision(Namespace(**(args | {"joueur": "inconnu"})))
        self.assertIn("joueurs : autre, eleve", " ".join(map(str, imprime.call_args.args)))
        with mock.patch("builtins.print") as imprime, self.assertRaises(SystemExit):
            cli.cmd_import_revision(Namespace(**(args | {"base": str(Path(self.tmp) / "vide.sqlite3")})))
        self.assertIn("introuvable", " ".join(map(str, imprime.call_args.args)))


if __name__ == "__main__":
    unittest.main()
