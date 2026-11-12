"""Tests du wiki, vague v2a : tableaux et listes coupés par du `code` (piste 20), note illisible ou
non UTF-8 (piste 23), images externes (piste 49), liens symboliques et git à l'import (piste 65).

Lancer : uv run python -m unittest discover -s tests -v
"""
import io
import os
import re
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from test_wiki import WikiTestCase

from hangar.wiki import historique, images_externes, installation
from hangar.wiki.vault import decoder, get_vault

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
GIF = b"GIF89a" + b"\x00" * 20


def refuser_lecture(*noms):
    """Le système refuse d'ouvrir ces fichiers (antivirus, OneDrive hors connexion…) : simulé, car
    un chmod 000 n'empêche rien quand les tests tournent en administrateur."""
    vraie = Path.read_bytes

    def lire(self):
        if self.name in noms:
            raise PermissionError(13, "Permission denied", str(self))
        return vraie(self)

    return mock.patch.object(Path, "read_bytes", lire)


# ---------------------------------------------------------------------------
# Piste 20 : tableaux et listes coupés par du code en ligne
# ---------------------------------------------------------------------------

class TableauxEtListesTests(WikiTestCase):
    def test_cellule_avec_code_suivi_de_texte(self):
        self.write("A.md", "| A | B |\n|---|---|\n| 1 | `x`, fin |\n| 2 | y |")
        html = self.html("A")
        self.assertEqual(html.count("<table>"), 1)
        self.assertEqual(html.count("<tr>"), 3)  # en-tête + 2 lignes
        self.assertIn("<td><code>x</code>, fin</td>", html)
        self.assertNotIn("<p>|", html)

    def test_en_tete_en_code_gras(self):
        # Cas réel : « Hack/IntroductionToNetworking/09 - Adresses IP » (classes d'adresses).
        self.write("A.md", "|**`Classe`**|**Plage**|\n|---|---|\n| A | 1.0.0.0 |")
        html = self.html("A")
        self.assertIn("<th><strong><code>Classe</code></strong></th>", html)
        self.assertIn("<td>1.0.0.0</td>", html)
        self.assertNotIn("<p>|", html)

    def test_liste_avec_code_reste_serree(self):
        self.write("A.md", "- un `a`, suite\n- deux\n- trois")
        html = self.html("A")
        self.assertNotIn("<p>", html)  # aucun item transformé en paragraphe (espacement irrégulier)
        self.assertEqual(html.count("<li>"), 3)

    def test_paragraphe_suivi_d_un_tableau_ou_d_une_liste(self):
        # Comportement voulu au départ (comme Obsidian), gardé ; et désormais aussi après un `code`.
        self.write("A.md", "Intro\n| a | b |\n|---|---|\n| 1 | 2 |\n\nIntro `x`\n| c | d |\n|---|---|\n| 3 | 4 |\n\n"
                           "Liste :\n- un\n- deux")
        html = self.html("A")
        self.assertEqual(html.count("<table>"), 2)
        self.assertIn("<p>Intro <code>x</code></p>", html)
        self.assertIn("<p>Liste :</p>\n<ul>", html)

    def test_blocs_de_code_maths_et_citations_intacts(self):
        self.write("A.md", "Texte\n```\nligne\n| pas un tableau\n- pas une liste\n```\n\n$$\n|x| \\le 1\n$$\n\n"
                           "> citation\n> | a | b |")
        html = self.html("A")
        self.assertIn("ligne\n| pas un tableau\n- pas une liste\n</code></pre>", html)
        self.assertIn("\\[\n|x| \\le 1\n\\]", html)
        self.assertIn("<blockquote>", html)


# ---------------------------------------------------------------------------
# Piste 23 : note illisible, note non UTF-8
# ---------------------------------------------------------------------------

class NotesIllisiblesTests(WikiTestCase):
    def test_une_note_illisible_ne_met_plus_tout_le_wiki_en_erreur(self):
        self.write("Autre.md", "Une autre page")
        self.write("Verrou.md", "Contenu verrouillé")
        with refuser_lecture("Verrou.md"), self.assertLogs("hangar.wiki.vault", "WARNING") as journal:
            for url in ["/wiki/", "/wiki/Autre", "/sommaire/", "/recherche/?q=page", "/historique/"]:
                self.assertEqual(self.client.get(url).status_code, 200, url)
            vault = get_vault()
            self.assertEqual(vault.illisibles, ["Verrou.md"])
            self.assertNotIn("verrou", vault.notes)
            sommaire = self.client.get("/sommaire/").get_data(as_text=True)
            self.assertIn("data-illisibles", sommaire)
            self.assertIn("Verrou.md", sommaire)
            # Pas d'éditeur vide (l'enregistrer effacerait la page) : retour à la page, avec un message.
            r = self.client.get("/modifier/Verrou")
            self.assertEqual(r.status_code, 302)
        self.assertIn("Verrou.md", journal.output[0])  # noté une fois dans le journal du serveur
        self.assertEqual(len(journal.output), 1)
        # Redevenu lisible sans avoir changé (antivirus qui relâche le fichier) : relu tout seul.
        vault = get_vault()
        self.assertEqual(vault.illisibles, [])
        self.assertIn("verrou", vault.notes)
        self.assertNotIn("data-illisibles", self.client.get("/sommaire/").get_data(as_text=True))

    def test_note_devenue_illisible_garde_sa_derniere_version(self):
        self.write("Note.md", "Première version")
        self.assertIn("Première version", self.html("Note"))
        self.write("Note.md", "Seconde version, plus longue")
        with refuser_lecture("Note.md"), self.assertLogs("hangar.wiki.vault", "WARNING"):
            self.assertEqual(get_vault().notes["note"].body, "Première version")
            page = self.client.get("/wiki/Note").get_data(as_text=True)
            self.assertIn("Première version", page)
            self.assertIn("data-perimee", page)
            self.assertIn("Note.md", self.client.get("/sommaire/").get_data(as_text=True))
        self.assertEqual(get_vault().notes["note"].body, "Seconde version, plus longue")
        self.assertNotIn("data-perimee", self.client.get("/wiki/Note").get_data(as_text=True))

    def test_note_ansi_lisible_modifiable_et_convertie(self):
        fichier = self.root / "Ansi.md"
        fichier.write_bytes("# Café\nÉté, à bientôt".encode("cp1252"))
        self.assertIn("Été, à bientôt", self.html("Ansi"))  # plus de « � »
        r = self.client.get("/modifier/Ansi")
        self.assertEqual(r.status_code, 200)
        page = r.get_data(as_text=True)
        self.assertIn("data-autre-encodage", page)
        self.assertIn("Été, à bientôt", page)
        self.assertEqual(self.client.get("/historique/Ansi").status_code, 200)
        version = re.search(r'name="version" value="(\d+)"', page).group(1)
        # Enregistrée telle quelle : convertie en UTF-8, l'original gardé dans l'historique.
        r = self.post("/modifier/Ansi", {"content": "# Café\nÉté, à bientôt", "version": version})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(fichier.read_bytes().decode("utf-8"), "# Café\nÉté, à bientôt")
        versions = historique.lister("Ansi.md")
        self.assertEqual(len(versions), 1)
        self.assertEqual(historique.lire("Ansi.md", versions[0]["id"]), "# Café\nÉté, à bientôt")
        self.assertNotIn("data-autre-encodage", self.client.get("/modifier/Ansi").get_data(as_text=True))

    def test_restaurer_une_version_d_une_note_ansi(self):
        fichier = self.root / "Ansi.md"
        fichier.write_bytes("Évolution".encode("cp1252"))
        historique.sauvegarder("Ansi.md", "Ancienne", "moi")
        v = historique.lister("Ansi.md")[0]["id"]
        self.assertEqual(self.post("/historique/Ansi", {"action": "restaurer", "v": v}).status_code, 302)
        self.assertEqual(fichier.read_text(encoding="utf-8"), "Ancienne")
        self.assertEqual(historique.lire("Ansi.md", historique.lister("Ansi.md")[0]["id"]), "Évolution")

    def test_decoder(self):
        self.assertEqual(decoder("Été".encode("utf-8")), ("Été", "utf-8"))
        self.assertEqual(decoder("Été".encode("cp1252")), ("Été", "cp1252"))
        self.assertEqual(decoder("Été".encode("utf-16")), ("Été", "utf-16"))  # « Unicode » du Bloc-notes
        # Fichier mélangé : chaque passage lu dans son encodage.
        self.assertEqual(decoder("début é ".encode("utf-8") + "fin è".encode("cp1252"))[0], "début é fin è")


# ---------------------------------------------------------------------------
# Piste 49 : images externes
# ---------------------------------------------------------------------------

class ImagesExternesRenduTests(WikiTestCase):
    def test_image_externe_devient_un_lien_vers_son_adresse(self):
        self.write("A.md", "![Capture du menu](https://cdn.example.com/a.png)\n\n"
                           '<img src="https://cdn.example.com/b.png" alt="Schéma">\n\n'
                           "[![Logo](https://cdn.example.com/logo.png)](https://academy.example.com/module/9)\n\n"
                           "![[local.png]]\n\n`![code](https://cdn.example.com/c.png)`")
        (self.root / "local.png").write_bytes(PNG)
        html = self.html("A")
        self.assertIn('<a class="image-externe" href="https://cdn.example.com/a.png"', html)
        self.assertIn("Image externe : Capture du menu ↗</a>", html)
        self.assertIn('href="https://cdn.example.com/b.png"', html)
        self.assertIn("Image externe : Schéma", html)
        # Image dans un lien : simple texte (pas de lien dans un lien), le lien d'origine reste.
        self.assertRegex(html, r'<a href="https://academy\.example\.com/module/9"><span class="image-externe"[^>]*>'
                               r"Image externe : Logo</span></a>")
        self.assertIn('<img class="embed-image" src="/fichiers/local.png"', html)
        self.assertIn("<code>![code](https://cdn.example.com/c.png)</code>", html)
        self.assertNotRegex(html, r"<img(?![^>]*\ssrc=)[^>]*>")  # plus aucune image vide

    def test_trouver_et_remplacer(self):
        texte = ("---\ncouverture: ![x](https://h/couv.png)\n---\n![Légende [1]](https://h/a.png \"titre\")\n"
                 "| ![Dans | tableau](https://h/a.png) | b |\n```\n![code](https://h/a.png)\n```\n"
                 '<img src="//h/b.png" width="300">')
        trouvees = images_externes.trouver(texte)
        self.assertEqual([o.url for o in trouvees],
                         ["https://h/couv.png", "https://h/a.png", "https://h/a.png", "https://h/b.png"])
        nouveau = images_externes.remplacer(texte, {"https://h/a.png": "a.png", "https://h/b.png": "image/b.png",
                                                    "https://h/couv.png": "couv.png"})
        self.assertIn("couverture: ![x](https://h/couv.png)", nouveau)  # propriétés YAML intactes
        self.assertIn("![[a.png|Légende 1]]\n", nouveau)
        self.assertIn("| ![[a.png\\|Dans tableau]] | b |", nouveau)  # « \| » dans un tableau, comme Obsidian
        self.assertIn("![code](https://h/a.png)", nouveau)  # bloc de code intact
        self.assertIn("![[image/b.png|300]]", nouveau)


def faux_site(adresses):
    """Remplace le téléchargement : {adresse: contenu, ou exception à lever}."""
    def lire(url):
        reponse = adresses[url]
        if isinstance(reponse, Exception):
            raise reponse
        return reponse
    return lire


class ImagesExternesTelechargementTests(WikiTestCase):
    def setUp(self):
        super().setUp()
        self.write("Cours/Réseau.md", "![Menu du site](https://cdn.example.com/modules/49/menu.png)\n\n"
                                      "| Logo | Texte |\n|---|---|\n"
                                      "| ![Logo](https://cdn.example.com/logo.png?v=2) | x |\n\n"
                                      "![Page](https://cdn.example.com/page.png)\n\n![Absente](https://cdn.example.com/404.png)")
        self.write("Autre.md", "Même image : ![Menu](https://cdn.example.com/modules/49/menu.png)")
        (self.root / "image").mkdir()
        (self.root / "image" / "menu.png").write_bytes(b"\x89PNG\r\n\x1a\nAUTRE")  # même nom, autre image
        self.site = faux_site({
            "https://cdn.example.com/modules/49/menu.png": PNG,
            "https://cdn.example.com/logo.png?v=2": GIF,
            "https://cdn.example.com/page.png": b"<!doctype html><title>Erreur</title>",
            "https://cdn.example.com/404.png": urllib.error.HTTPError("https://cdn.example.com/404.png", 404,
                                                                       "Not Found", {}, None),
        })

    def test_telecharger_et_reecrire_les_pages(self):
        self.assertEqual(images_externes.inventaire(get_vault()), (5, 2))
        bilan = images_externes.telecharger(get_vault(), "moi", lire=self.site)
        self.assertEqual((bilan.images, bilan.pages, bilan.adresses), (5, 2, 4))
        self.assertEqual(sorted(bilan.telechargees), ["image/logo.gif", "image/menu 1.png"])
        self.assertEqual((self.root / "image" / "menu 1.png").read_bytes(), PNG)
        self.assertEqual(dict(bilan.echecs), {"https://cdn.example.com/page.png": "ce n'est pas une image (PNG, "
                                                                                    "JPEG, GIF, WebP, BMP ou AVIF)",
                                              "https://cdn.example.com/404.png": "le site répond « erreur 404 »"})
        reseau = (self.root / "Cours" / "Réseau.md").read_text(encoding="utf-8")
        self.assertIn("![[menu 1.png|Menu du site]]", reseau)
        self.assertIn("| ![[logo.gif\\|Logo]] | x |", reseau)
        self.assertIn("![Page](https://cdn.example.com/page.png)", reseau)  # échec : lien d'origine gardé
        self.assertEqual((self.root / "Autre.md").read_text(encoding="utf-8"), "Même image : ![[menu 1.png|Menu]]")
        self.assertEqual(sorted(bilan.pages_modifiees), ["Autre.md", "Cours/Réseau.md"])
        self.assertEqual(len(historique.lister("Cours/Réseau.md")), 1)  # ancien texte gardé
        html = self.html("Cours/Réseau")
        self.assertIn('<img class="embed-image" src="/fichiers/image/menu%201.png" alt="Menu du site"', html)
        self.assertIn('src="/fichiers/image/logo.gif" alt="Logo"', html)
        self.assertEqual(images_externes.inventaire(get_vault()), (2, 1))
        # Relancé : rien n'est téléchargé deux fois.
        bilan = images_externes.telecharger(get_vault(), "moi", lire=self.site)
        self.assertEqual((bilan.images, bilan.telechargees, bilan.pages_modifiees), (2, [], []))

    def test_bouton_de_la_page_importer(self):
        page = self.client.get("/importer/").get_data(as_text=True)
        self.assertIn("5 images de 2 pages sont hébergées sur d'autres sites", page)
        self.assertIn('action="/importer/images-externes/"', page)
        with mock.patch.object(images_externes, "ouvrir", self.site):
            r = self.post("/importer/images-externes/")
        self.assertEqual(r.status_code, 302)
        page = self.client.get("/importer/").get_data(as_text=True)
        self.assertIn("data-images-bilan", page)
        self.assertIn("2 images enregistrées dans le wiki, 2 pages mises à jour", page)
        self.assertIn("erreur 404", page)

    def test_commande_wiki_import_images(self):
        from hangar.cli import main
        sortie = io.StringIO()
        with mock.patch.dict(os.environ, {"HANGAR_VAULT": str(self.root)}), redirect_stdout(sortie), \
                mock.patch.object(images_externes, "ouvrir", self.site):
            main(["--data", self.tmp, "wiki-import", "--images"])
        self.assertIn("5 image(s) externe(s) trouvée(s) dans 2 page(s) ; 2 enregistrée(s)", sortie.getvalue())
        self.assertIn("✗ https://cdn.example.com/404.png : le site répond « erreur 404 »", sortie.getvalue())
        self.assertIn("![[logo.gif", (self.root / "Cours" / "Réseau.md").read_text(encoding="utf-8"))

    def test_bilan_de_l_import_compte_les_images_externes(self):
        source = Path(self.tmp) / "source"
        source.mkdir()
        (source / "Capture.md").write_text("![a](https://h/a.png) ![b](https://h/b.png)", encoding="utf-8")
        from hangar.cli import main
        sortie = io.StringIO()
        with mock.patch.dict(os.environ, {"HANGAR_VAULT": str(self.root)}), redirect_stdout(sortie):
            main(["--data", self.tmp, "wiki-import", str(source)])
        self.assertIn("2 image(s) de 1 page(s) sont hébergées sur d'autres sites", sortie.getvalue())
        self.assertIn("--images", sortie.getvalue())

    def test_garde_fous_du_telechargement(self):
        with self.assertRaises(images_externes.ImageRefusee):
            images_externes.ouvrir("file:///etc/passwd")
        self.assertIsNone(images_externes.type_image(b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>"))
        self.assertEqual(images_externes.type_image(b"RIFF\x00\x00\x00\x00WEBPVP8 "), ".webp")
        self.assertEqual(images_externes._nom("https://h/x/Capture%20d%27%C3%A9cran.jpeg?x=1", ".jpg"),
                         "Capture d'écran.jpeg")
        self.assertEqual(images_externes._nom("https://h/x/[a]#b.png", ".gif"), "a.gif")
        self.assertEqual(images_externes._nom("https://h/", ".png"), "image.png")
        self.assertEqual(images_externes._nom("https://h/con.png", ".png"), "image-con.png")  # nom réservé de Windows


class ImagesExternesInviteTests(WikiTestCase):
    role = "guest"

    def test_un_invite_ne_lance_pas_le_telechargement(self):
        self.write("A.md", "![a](https://h/a.png)")
        self.assertEqual(self.post("/importer/images-externes/").status_code, 403)


# ---------------------------------------------------------------------------
# Piste 65 : liens symboliques et git à l'import d'un coffre
# ---------------------------------------------------------------------------

def lien(cible, chemin):
    try:
        os.symlink(cible, chemin)
    except (OSError, NotImplementedError) as exc:  # Windows sans le mode développeur
        raise unittest.SkipTest(f"liens symboliques indisponibles : {exc}") from exc


class ImportLiensSymboliquesTests(WikiTestCase):
    def test_lien_vers_un_fichier_hors_du_coffre_n_est_pas_copie(self):
        base = Path(self.tmp) / "depot"
        (base / "vault" / "Cours").mkdir(parents=True)
        secret = Path(self.tmp) / "secret.key"
        secret.write_text("clé de session", encoding="utf-8")
        (base / "vault" / "Cours" / "Vrai.md").write_text("Vraie page", encoding="utf-8")
        (base / "LISEZMOI.md").write_text("dans le dépôt", encoding="utf-8")
        lien(secret, base / "vault" / "Cours" / "Annexe.md")
        lien(secret, base / "vault" / "Cours" / "cle.txt")
        lien(Path(self.tmp) / "nexiste" / "pas.md", base / "vault" / "Aa casse.md")  # traité en premier
        lien(base / "vault" / "Cours" / "Vrai.md", base / "vault" / "Interne.md")  # lien interne : gardé
        lien(base / "LISEZMOI.md", base / "vault" / "Lisezmoi.md")  # dans le dépôt importé : gardé
        lien(base / "vault" / "Cours" / "Vrai.md", base / "vault" / "Zz dernier.md")  # après le lien cassé
        bilan = installation.importer_coffre(str(base), self.root)
        self.assertEqual(sorted(bilan.exclus), ["Aa casse.md", "Cours/Annexe.md", "Cours/cle.txt"])
        self.assertFalse((self.root / "Cours" / "Annexe.md").exists())
        self.assertFalse((self.root / "Cours" / "cle.txt").exists())
        self.assertEqual((self.root / "Interne.md").read_text(encoding="utf-8"), "Vraie page")
        self.assertEqual((self.root / "Lisezmoi.md").read_text(encoding="utf-8"), "dans le dépôt")
        self.assertTrue((self.root / "Zz dernier.md").is_file())  # l'import est allé au bout
        self.assertNotIn("clé de session", self.client.get("/wiki/Cours/Annexe").get_data(as_text=True))
        # Message de la commande
        from hangar.cli import main
        sortie = io.StringIO()
        with mock.patch.dict(os.environ, {"HANGAR_VAULT": str(self.root)}), redirect_stdout(sortie):
            main(["--data", self.tmp, "wiki-import", str(base)])
        self.assertIn("3 lien(s) symbolique(s) non copié(s)", sortie.getvalue())

    def test_dossier_vault_qui_est_un_lien_vers_l_exterieur(self):
        dehors = Path(self.tmp) / "donnees"
        dehors.mkdir()
        (dehors / "secret.md").write_text("secret", encoding="utf-8")
        base = Path(self.tmp) / "depot"
        base.mkdir()
        lien(dehors, base / "vault")
        bilan = installation.importer_coffre(str(base), self.root)
        self.assertEqual((bilan.copies, bilan.exclus), ([], ["secret.md"]))
        self.assertFalse((self.root / "secret.md").exists())


class ClonerTests(unittest.TestCase):
    def test_l_adresse_n_est_jamais_une_option_de_git(self):
        with mock.patch("shutil.which", return_value="/usr/bin/git"), \
                mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as run:
            installation.cloner("https://example.com/depot.git", Path("dest"))
        commande = run.call_args[0][0]
        self.assertEqual(commande[-3:], ["--", "https://example.com/depot.git", "dest"])
        with self.assertRaises(ValueError):
            installation.cloner("--upload-pack=touch X;.git", Path("dest"))


if __name__ == "__main__":
    unittest.main()
