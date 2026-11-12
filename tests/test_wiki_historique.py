"""Tests de l'historique des pages du wiki (piste 7) : regroupement des enregistrements d'une même
séance d'écriture, points de repère toujours créés (corbeille, restauration, import, IA),
éclaircissage par paliers, plafonds de nombre et de taille, historique déjà présent sur le disque.

Lancer : uv run python -m unittest discover -s tests -v
"""
import json
import re
from datetime import datetime, timedelta
from unittest import mock

from test_wiki import WikiTestCase, pdf_texte

from hangar.extensions import db
from hangar.models import User
from hangar.wiki import historique


class Horloge(datetime):
    """Remplace ``datetime`` dans le module d'historique : ``now()`` renvoie l'instant choisi par le test."""
    instant = datetime(2026, 11, 9, 9, 0)

    @classmethod
    def now(cls, tz=None):
        return cls.instant


class HistoriqueTestCase(WikiTestCase):
    def setUp(self):
        super().setUp()
        Horloge.instant = datetime(2026, 11, 9, 9, 0)  # un lundi
        horloge = mock.patch.object(historique, "datetime", Horloge)
        horloge.start()
        self.addCleanup(horloge.stop)

    def regler(self, *args):
        Horloge.instant = datetime(*args)

    def avancer(self, **duree):
        Horloge.instant += timedelta(**duree)

    def ouvrir(self, key, client=None):
        """Jeton de version de l'éditeur, comme à son ouverture."""
        page = (client or self.client).get(f"/modifier/{key}").get_data(as_text=True)
        return re.search(r'name="version" value="(\d*)"', page).group(1)

    def enregistrer(self, key, texte, version, seance="", client=None):
        """Enregistrement de l'éditeur (Ctrl+S ou enregistrement auto) : réponse JSON."""
        r = self.post(f"/modifier/{key}", {"content": texte, "version": version, "seance": seance},
                      json_resp=True, client=client)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def textes(self, rel):
        """Textes des versions gardées, la plus ancienne d'abord."""
        return [historique.lire(rel, v["id"]) for v in reversed(historique.lister(rel))]


class RegroupementTests(HistoriqueTestCase):
    def test_soixante_enregistrements_auto_gardent_le_texte_d_avant_la_seance(self):
        # Cas de la piste : enregistrement auto pendant 20 minutes, la version d'avant la séance survit.
        self.write("Fiche.md", "VERSION D'HIER\n")
        version, seance = self.ouvrir("Fiche"), ""
        for i in range(1, 61):
            reponse = self.enregistrer("Fiche", f"frappe {i}\n", version, seance)
            version, seance = reponse["version"], reponse["seance"]
            self.avancer(seconds=20)
        self.assertEqual(seance, "1")
        self.assertEqual((self.root / "Fiche.md").read_text(encoding="utf-8"), "frappe 60\n")
        # Une version au début de la séance puis une toutes les 10 minutes (le 31e enregistrement, à
        # 10 minutes pile, garde le texte du 30e), pas une par enregistrement.
        self.assertEqual(self.textes("Fiche.md"), ["VERSION D'HIER\n", "frappe 30\n"])

    def test_point_de_controle_toutes_les_dix_minutes(self):
        self.write("Note.md", "départ")
        version = self.ouvrir("Note")
        r = self.enregistrer("Note", "a", version)
        self.assertEqual(r["seance"], "1")
        self.avancer(minutes=5)
        r = self.enregistrer("Note", "b", r["version"], r["seance"])
        self.avancer(minutes=4, seconds=59)
        r = self.enregistrer("Note", "c", r["version"], r["seance"])
        self.assertEqual(self.textes("Note.md"), ["départ"])
        self.avancer(seconds=1)  # 10 minutes après la dernière version gardée
        r = self.enregistrer("Note", "d", r["version"], r["seance"])
        self.assertEqual(self.textes("Note.md"), ["départ", "c"])

    def test_nouvelle_seance_ou_page_modifiee_ailleurs_cree_une_version(self):
        self.write("Note.md", "départ")
        r = self.enregistrer("Note", "un", self.ouvrir("Note"))
        # Modifiée dans Obsidian juste après : le prochain éditeur ouvert garde ce texte, même 1 minute après.
        self.avancer(minutes=1)
        self.write("Note.md", "ajouté dans Obsidian")
        r = self.enregistrer("Note", "deux", self.ouvrir("Note"))
        self.assertEqual(self.textes("Note.md"), ["départ", "ajouté dans Obsidian"])
        # Un « séance » envoyé avec un jeton périmé est refusé comme avant (conflit), sans rien écrire.
        r = self.post("/modifier/Note", {"content": "x", "version": "0", "seance": "1"}, json_resp=True)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(self.textes("Note.md")), 2)

    def test_enregistrement_sans_changement_n_ouvre_pas_de_seance(self):
        self.write("Note.md", "pareil")
        r = self.enregistrer("Note", "pareil", self.ouvrir("Note"))
        self.assertEqual(r["seance"], "")
        self.assertEqual(historique.lister("Note.md"), [])
        # Une page créée depuis l'éditeur ouvre la séance (son texte vient de cet éditeur).
        r = self.enregistrer("Neuve", "premier jet", "")
        self.assertEqual(r["seance"], "1")
        r = self.enregistrer("Neuve", "second jet", r["version"], r["seance"])
        self.assertEqual(r["seance"], "1")

    def test_points_de_repere_toujours_crees(self):
        self.write("Note.md", "départ")
        r = self.enregistrer("Note", "a", self.ouvrir("Note"))
        r = self.enregistrer("Note", "b", r["version"], r["seance"])
        self.assertEqual(self.textes("Note.md"), ["départ"])
        # Restauration, dans la même minute : le texte remplacé est gardé.
        premiere = historique.lister("Note.md")[-1]["id"]
        self.assertEqual(self.post("/historique/Note", {"action": "restaurer", "v": premiere}).status_code, 302)
        self.assertEqual(self.textes("Note.md"), ["départ", "b"])
        # Écritures de l'IA, import… (appel par défaut) : jamais regroupées, même auteur, même instant.
        historique.sauvegarder("Note.md", "avant IA 1", "moi")
        historique.sauvegarder("Note.md", "avant IA 2", "moi")
        # Corbeille : le texte de la page aussi.
        self.assertEqual(self.post("/supprimer-page/Note").status_code, 302)
        self.assertEqual(self.textes("Note.md"), ["départ", "b", "avant IA 1", "avant IA 2", "départ"])

    def test_regroupement_seulement_pour_le_meme_auteur(self):
        historique.sauvegarder("Note.md", "un", "alice", edition=True)
        self.assertIsNone(historique.sauvegarder("Note.md", "deux", "alice", regrouper=True))
        self.assertIsNotNone(historique.sauvegarder("Note.md", "trois", "bob", regrouper=True))
        self.assertEqual(self.textes("Note.md"), ["un", "trois"])

    def test_citations_prises_d_affilee_partagent_une_version(self):
        (self.root / "Cours").mkdir(parents=True, exist_ok=True)
        (self.root / "Cours" / "Signal.pdf").write_bytes(pdf_texte("Shannon"))
        self.write("Cours/Notes.md", "# Notes\n")
        headers = {"X-CSRF-Token": "jeton-test", "Content-Type": "application/json"}

        def citer(texte):
            r = self.client.post("/pdf-annotation/", headers=headers, data=json.dumps(
                {"pdf": "Cours/Signal.pdf", "page": 1, "texte": texte, "cible": "note", "page_wiki": "Cours/Notes"}))
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

        for i in range(5):
            citer(f"citation {i}")
            self.avancer(minutes=1)
        self.assertEqual(self.textes("Cours/Notes.md"), ["# Notes\n"])
        self.assertIn("citation 4", (self.root / "Cours" / "Notes.md").read_text(encoding="utf-8"))
        self.avancer(minutes=10)
        citer("plus tard")
        self.assertEqual(len(self.textes("Cours/Notes.md")), 2)


class RetentionTests(HistoriqueTestCase):
    def test_eclaircissage_par_paliers(self):
        def edition(texte):
            historique.sauvegarder("Cours.md", texte, "moi", edition=True)

        # Une séance de trois heures, une version toutes les 10 minutes, et une restauration à 10 h 25.
        for minutes in range(0, 180, 10):
            self.regler(2026, 11, 9, 9 + minutes // 60, minutes % 60)
            edition(f"9/11 {9 + minutes // 60}:{minutes % 60:02d}")
            if minutes == 80:
                self.regler(2026, 11, 9, 10, 25)
                historique.sauvegarder("Cours.md", "avant restauration", "moi")
        self.regler(2026, 11, 9, 12, 30)
        edition("9/11 12:30")
        # Moins d'une heure : tout ; au-delà : une par heure (la plus ancienne) ; le point de repère reste.
        self.assertEqual(self.textes("Cours.md"), ["9/11 9:00", "9/11 10:00", "avant restauration", "9/11 11:00",
                                                    "9/11 11:40", "9/11 11:50", "9/11 12:30"])
        # Plus d'un jour : une par jour.
        self.regler(2026, 11, 11, 12, 0)
        edition("11/11")
        self.assertEqual(self.textes("Cours.md"), ["9/11 9:00", "avant restauration", "11/11"])
        self.regler(2026, 11, 12, 10, 0)
        edition("12/11")
        self.regler(2026, 11, 17, 10, 0)
        edition("17/11")
        self.assertEqual(self.textes("Cours.md"), ["9/11 9:00", "avant restauration", "11/11", "12/11", "17/11"])
        # Plus d'un mois : une par semaine (du lundi au dimanche).
        self.regler(2026, 12, 20, 10, 0)
        edition("20/12")
        self.assertEqual(self.textes("Cours.md"), ["9/11 9:00", "avant restauration", "17/11", "20/12"])
        # Plus de six mois : une toutes les quatre semaines (9/11 et 17/11 dans la même tranche).
        self.regler(2027, 6, 15, 10, 0)
        edition("15/06")
        self.assertEqual(self.textes("Cours.md"), ["9/11 9:00", "avant restauration", "20/12", "15/06"])

    def test_plafond_du_nombre_de_versions(self):
        for i in range(120):
            historique.sauvegarder("Note.md", f"v{i}", "moi")
            self.avancer(seconds=1)
        textes = self.textes("Note.md")
        self.assertEqual(len(textes), historique.MAX_VERSIONS)
        self.assertEqual(textes[0], "v0")  # le texte d'origine de la page reste
        self.assertEqual(textes[1], "v21")
        self.assertEqual(textes[-1], "v119")

    def test_plafond_de_taille(self):
        with mock.patch.object(historique, "MAX_OCTETS", 1000):
            for i in range(6):
                historique.sauvegarder("Note.md", f"{i}" * 300, "moi")
                self.avancer(seconds=1)
        self.assertEqual([t[0] for t in self.textes("Note.md")], ["0", "4", "5"])
        # Une version plus grosse que le plafond n'empêche pas d'enregistrer : la plus récente reste toujours.
        with mock.patch.object(historique, "MAX_OCTETS", 100):
            historique.sauvegarder("Note.md", "x" * 500, "moi")
        self.assertEqual([t[0] for t in self.textes("Note.md")], ["0", "x"])

    def test_historique_deja_present_garde_tel_quel(self):
        # 50 versions écrites par la version précédente de Hangar (format <horodatage>-<auteur>.md).
        d = historique._dossier_page("Note.md")
        d.mkdir(parents=True)
        debut = datetime(2026, 11, 1, 8, 0)
        for i in range(50):
            nom = (debut + timedelta(minutes=i)).strftime("%Y%m%dT%H%M%S%f") + "-jean_dupont.md"
            (d / nom).write_text(f"ancienne {i}", encoding="utf-8")
        self.write("Note.md", "actuelle")
        self.enregistrer("Note", "modifiée", self.ouvrir("Note"))
        textes = self.textes("Note.md")
        self.assertEqual(len(textes), 51)
        self.assertEqual(textes[0], "ancienne 0")
        self.assertEqual(textes[-1], "actuelle")
        self.assertEqual(historique.lister("Note.md")[-1]["auteur"], "jean dupont")


class AffichageTests(HistoriqueTestCase):
    def test_pages_d_historique_avec_versions_d_edition(self):
        self.write("Note.md", "version 1")
        self.enregistrer("Note", "version 2", self.ouvrir("Note"))
        v = historique.lister("Note.md")[0]
        self.assertIn("_moi.md", v["id"])
        self.assertEqual(v["auteur"], "moi")
        self.assertIn(f'/historique/Note?v={v["id"]}', self.client.get("/historique/").get_data(as_text=True))
        page = self.client.get(f'/historique/Note?v={v["id"]}').get_data(as_text=True)
        self.assertIn("une par heure", page)
        self.assertIn('class="diff-ajout">version 2', page)
        self.assertEqual(self.post("/historique/Note", {"action": "restaurer", "v": v["id"]}).status_code, 302)
        self.assertEqual((self.root / "Note.md").read_text(encoding="utf-8"), "version 1")
        self.assertIsNone(historique.lire("Note.md", "../Note.md"))
        self.assertIsNone(historique.lire("Note.md", "20261309T000000000000_moi.md"))  # date impossible

    def test_deux_versions_au_meme_instant_ne_s_ecrasent_pas(self):
        historique.sauvegarder("Note.md", "un", "moi")
        historique.sauvegarder("Note.md", "deux", "moi", edition=True)
        historique.sauvegarder("Note.md", "trois", "moi")
        self.assertEqual(self.textes("Note.md"), ["un", "deux", "trois"])

    def test_auteur_different_editeur_multi_comptes(self):
        autre = User(username="autre", email="autre@hangar.local", role="admin", first_name="Autre")
        autre.set_password("secret1")
        db.session.add(autre)
        db.session.commit()
        client_autre = self.login("autre")
        self.write("Note.md", "départ")
        r = self.enregistrer("Note", "par moi", self.ouvrir("Note"))
        self.avancer(minutes=1)
        r = self.enregistrer("Note", "par autre", self.ouvrir("Note", client_autre), client=client_autre)
        self.assertEqual(self.textes("Note.md"), ["départ", "par moi"])
        self.assertEqual([v["auteur"] for v in historique.lister("Note.md")], ["autre", "moi"])
