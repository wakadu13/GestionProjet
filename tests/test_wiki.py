"""Tests du wiki (port de Revision) : rendu Obsidian, index et liens, recherche (notes et PDF),
historique, édition et conflits, import, droits, pages de la section, commande wiki-import.

Lancer : uv run python -m unittest discover -s tests -v
"""
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from flask import g
from flask.testing import FlaskClient

from hangar import create_app
from hangar.extensions import db
from hangar.models import User
from hangar.wiki import historique, installation, texte_pdf
from hangar.wiki.models import WikiFavorite, WikiVisit
from hangar.wiki.vault import get_vault


def pdf_texte(*pages):
    """PDF minimal (Helvetica) : une page par texte, une ligne par « \\n » (repris des tests de Revision)."""
    objets = [b"<< /Type /Catalog /Pages 2 0 R >>",
              f"<< /Type /Pages /Kids [{' '.join(f'{4 + 2 * i} 0 R' for i in range(len(pages)))}] /Count {len(pages)} >>".encode(),
              b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    for i, texte in enumerate(pages):
        lignes = " ".join(f"({ligne}) Tj 0 -16 Td" for ligne in texte.split("\n"))
        flux = f"BT /F1 12 Tf 72 720 Td {lignes} ET".encode("cp1252")
        objets.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                      f"/Contents {5 + 2 * i} 0 R >>".encode())
        objets.append(b"<< /Length %d >>\nstream\n" % len(flux) + flux + b"\nendstream")
    sortie, positions = bytearray(b"%PDF-1.4\n"), []
    for i, objet in enumerate(objets, 1):
        positions.append(len(sortie))
        sortie += f"{i} 0 obj\n".encode() + objet + b"\nendobj\n"
    xref = len(sortie)
    sortie += f"xref\n0 {len(objets) + 1}\n0000000000 65535 f \n".encode()
    sortie += b"".join(f"{p:010d} 00000 n \n".encode() for p in positions)
    sortie += f"trailer\n<< /Size {len(objets) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(sortie)


class ClientIsole(FlaskClient):
    """Le contexte de test poussé dans setUp est réutilisé par les requêtes du client (même
    contexte d'application, donc même ``g``) : chaque requête repart d'un ``g`` vide, comme en vrai
    (utilisateur connecté relu depuis le cookie, menu de gauche recalculé)."""

    def open(self, *args, **kwargs):
        for cle in list(g):
            g.pop(cle, None)
        return super().open(*args, **kwargs)


class WikiTestCase(unittest.TestCase):
    role = "admin"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-wiki-test-")
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HANGAR_VAULT", None)  # jamais le vrai coffre de l'utilisateur
        self.app = create_app(self.tmp, testing=True)
        self.app.test_client_class = ClientIsole
        self.app.config["WIKI_VERIF_INTERVALLE"] = 0  # fichiers écrits par les tests vus tout de suite
        self.root = Path(self.app.config["WIKI_DIR"])
        self.ctx = self.app.test_request_context("/")
        self.ctx.push()
        self.user = User(username="moi", email="moi@hangar.local", role=self.role, first_name="Moi")
        self.user.set_password("secret1")
        db.session.add(self.user)
        db.session.commit()
        self.client = self.login("moi")

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def login(self, username, password="secret1"):
        c = self.app.test_client()
        page = c.get("/login").get_data(as_text=True)
        token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        r = c.post("/login", data={"login": username, "password": password, "_csrf": token})
        self.assertEqual(r.status_code, 302)
        with c.session_transaction() as s:
            s["_csrf"] = "jeton-test"
        return c

    def post(self, url, data=None, json_resp=False, client=None, **kw):
        data = dict(data or {})
        data["_csrf"] = "jeton-test"
        headers = {"Accept": "application/json"} if json_resp else {}
        return (client or self.client).post(url, data=data, headers=headers, **kw)

    def write(self, path, text):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
        return file

    def html(self, key):
        vault = get_vault()
        return vault.render(vault.notes[key.lower()])[0]


class RenduTests(WikiTestCase):
    def test_wikilinks_et_retroliens(self):
        self.write("A.md", "[[B]] [[B|texte]] [[B#Mon Titre]] [[Alias B]] [[Absente]] [[#Local]]")
        self.write("sous/B.md", "---\naliases: [Alias B]\n---\n## Mon Titre")
        html = self.html("A")
        self.assertIn('href="/wiki/sous/B">B</a>', html)
        self.assertIn('href="/wiki/sous/B">texte</a>', html)
        self.assertIn('href="/wiki/sous/B#mon-titre">B &gt; Mon Titre</a>', html)
        self.assertIn('href="/wiki/sous/B">Alias B</a>', html)
        self.assertIn('is-unresolved" href="/wiki/Absente"', html)
        self.assertIn('href="#local">Local</a>', html)
        vault = get_vault()
        self.assertEqual([n.name for n in vault.backlinks["sous/b"]], ["A"])
        self.assertEqual([n.name for n in vault.unresolved["absente"]], ["A"])

    def test_resolution_comme_obsidian(self):
        self.write("Cours/Maths/Note.md", "[[Voisine]] [[../Autre]]")
        self.write("Cours/Maths/Voisine.md", "")
        self.write("Ailleurs/Voisine.md", "")
        self.write("Cours/Autre.md", "")
        vault = get_vault()
        note = vault.notes["cours/maths/note"]
        self.assertEqual(vault.resolve("Voisine", note).key, "Cours/Maths/Voisine")  # dossier de la note d'abord
        self.assertEqual(vault.resolve("../Autre", note).key, "Cours/Autre")  # chemin relatif
        self.assertEqual(vault.resolve("Ailleurs/Voisine").key, "Ailleurs/Voisine")
        self.assertIsNone(vault.resolve("Introuvable"))
        # Lien court /wiki/<nom> : redirection vers la page rangée dans un dossier
        r = self.client.get("/wiki/Autre")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/wiki/Cours/Autre"))

    def test_integrations(self):
        self.write("A.md", "![[img.png|200]]\n\n![[B#Section]]\n\n![[A]]\n\n![[cours.pdf#page=3]]")
        self.write("B.md", "# Intro\navant\n## Section\ndedans\n## Autre\napres")
        (self.root / "img.png").write_bytes(b"\x89PNG")
        (self.root / "cours.pdf").write_bytes(pdf_texte("Bonjour"))
        html = self.html("A")
        self.assertIn('<img class="embed-image" src="/fichiers/img.png" alt="img" width="200"', html)
        self.assertIn("dedans", html)
        self.assertNotIn("avant", html)
        self.assertNotIn("apres", html)
        self.assertIn('class="embed-pdf" src="/pdf/cours.pdf?integre=1#page=3"', html)
        r = self.client.get("/fichiers/img.png")
        self.assertEqual(r.status_code, 200)
        r.close()

    def test_callouts_tags_commentaires_code(self):
        self.write("A.md", (
            "> [!warning]- Titre *replié*\n> contenu #tag/sous\n\n"
            "> [!definition] Primitive\n> > [!success] Imbriqué\n> > dedans\n\n"
            "==surligné== ~~barré~~ %%secret%%\n\n"
            "```\n[[pas un lien]] #pas-un-tag\n```\n`#non`\n\n- [x] fait\n- [ ] à faire\n\nNote[^1].\n\n[^1]: Source."
        ))
        html = self.html("A")
        self.assertIn('<details class="callout" data-callout="warning">', html)
        self.assertIn("<em>replié</em>", html)
        self.assertIn('<div class="callout" data-callout="definition">', html)
        self.assertIn('data-callout="success"', html)
        self.assertIn('<a class="tag" href="/tags/tag/sous">#tag/sous</a>', html)
        self.assertIn("<mark>surligné</mark>", html)
        self.assertIn("<del>barré</del>", html)
        self.assertNotIn("secret", html)
        self.assertIn("[[pas un lien]] #pas-un-tag", html)
        self.assertIn("<code>#non</code>", html)
        self.assertIn('type="checkbox"', html)
        self.assertIn('class="footnote"', html)
        self.assertEqual([n.name for n in get_vault().notes_with_tag("tag")], ["A"])
        self.assertIn("A", self.client.get("/tags/tag/sous").get_data(as_text=True))

    def test_formules_mermaid_et_securite(self):
        self.write("A.md", "En ligne $x^2 < y$ et 5 $ ou 10 $.\n\n$$\n\\int_0^1 f\n$$\n\n```mermaid\ngraph LR\nA-->B\n```\n\n"
                           "<script>alert(1)</script><img src=x onerror=alert(1)> [x](javascript:alert(1))")
        html = self.html("A")
        self.assertIn('<span class="math math-inline">\\(x^2 &lt; y\\)</span>', html)
        self.assertIn('<div class="math math-block">\\[\n\\int_0^1 f\n\\]</div>', html)
        self.assertNotIn("10 $</span>", html)
        self.assertIn('class="language-mermaid"', html)
        self.assertNotIn("<script", html)
        self.assertNotIn("onerror", html)
        self.assertNotIn("javascript:", html)

    def test_proprietes_en_tableau(self):
        self.write("A.md", "---\ntitle: Titre affiché\ntags: [cours, maths]\naliases: [Alias]\nicon: 📡\nstatut: En cours\n"
                           "fini: false\nlien: https://exemple.fr\nliste: [a, b]\n---\nCorps")
        note = get_vault().notes["a"]
        self.assertEqual(note.title, "Titre affiché")
        self.assertEqual(note.tags, ["cours", "maths"])
        page = self.client.get("/wiki/A").get_data(as_text=True)
        self.assertIn("Titre affiché", page)
        self.assertIn('class="wiki-prop-k"', page)
        for attendu in ("statut", "En cours", "Non", 'href="https://exemple.fr"', "a, b", "📡", "#cours", "Alias"):
            self.assertIn(attendu, page)
        self.assertNotIn("icon", re.sub(r"<[^>]+>", "", page.split('class="wiki-props"')[1].split("markdown")[0]))


class RechercheTests(WikiTestCase):
    def test_recherche_notes(self):
        self.write("A.md", "Le mot girafe est ici, au Réseau.")
        self.write("B.md", "Rien.")
        self.write("Dossier/Réseaux locaux.md", "texte")
        vault = get_vault()
        self.assertEqual([n.name for n, _ in vault.search("girafe")], ["A"])
        resultats = vault.search("reseau")  # sans accent
        self.assertEqual({n.name for n, _ in resultats}, {"A", "Réseaux locaux"})
        self.assertIn("<mark>Réseau</mark>", dict((n.name, s) for n, s in resultats)["A"])
        page = self.client.get("/recherche/", query_string={"q": "girafe"}).get_data(as_text=True)
        self.assertIn("/wiki/A", page)
        # Titre exact et seul résultat : ouverture directe de la page
        r = self.client.get("/recherche/", query_string={"q": "B"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/wiki/B"))

    def test_html_echappe(self):
        self.write("A.md", "<b>gras</b> & girafe")
        _, extrait = get_vault().search("girafe")[0]
        self.assertIn("&lt;b&gt;", extrait)

    def test_recherche_dans_les_pdf(self):
        (self.root / "Cours").mkdir()
        (self.root / "Cours" / "Signal.pdf").write_bytes(pdf_texte("Introduction", "Theoreme de Shannon\nRepliement"))
        vault = get_vault()
        self.assertEqual(len(texte_pdf.a_indexer(vault)), 1)
        self.assertEqual(texte_pdf.indexer(vault), 1)
        self.assertEqual(texte_pdf.a_indexer(vault), [])
        resultats = texte_pdf.chercher(vault, "shannon")
        self.assertEqual(resultats[0]["pdf"].name, "Signal.pdf")
        self.assertEqual(resultats[0]["pages"][0][0], 2)
        page = self.client.get("/recherche/", query_string={"q": "shannon"}).get_data(as_text=True)
        self.assertIn("/pdf/Cours/Signal.pdf#page=2", page)

    def test_palette_ctrl_k(self):
        self.write("Cours/Intégrales.md", "---\nicon: 📐\n---\nPrimitives")
        pages = self.client.get("/api/search", query_string={"q": "integrales"}).get_json()["pages"]
        self.assertIn({"title": "Intégrales", "sub": "Wiki · Cours", "emoji": "📐", "url": "/wiki/Cours/Int%C3%A9grales"}, pages)


class EditionTests(WikiTestCase):
    def test_creer_une_page_depuis_le_navigateur(self):
        self.write("Accueil.md", "[[Planning]]")
        r = self.client.get("/wiki/Planning")
        self.assertEqual(r.status_code, 404)
        self.assertIn("Créer cette page", r.get_data(as_text=True))
        self.assertIn('id="editor"', self.client.get("/modifier/Planning").get_data(as_text=True))
        r = self.post("/modifier/Planning", {"content": "Voir [[Accueil]]\r\n", "version": ""})
        self.assertEqual(r.status_code, 302)
        self.assertEqual((self.root / "Planning.md").read_text(encoding="utf-8"), "Voir [[Accueil]]\n")
        self.assertIn('href="/wiki/Accueil"', self.client.get("/wiki/Planning").get_data(as_text=True))
        self.assertEqual([n.name for n in get_vault().backlinks["accueil"]], ["Planning"])
        visite = WikiVisit.query.filter_by(user_id=self.user.id, key="Planning").one()
        self.assertIsNotNone(visite.edited_at)

    def test_modifier_et_conflit(self):
        self.write("Dossier/Note.md", "avant")
        r = self.client.get("/modifier/Note")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/modifier/Dossier/Note"))
        page = self.client.get("/modifier/Dossier/Note").get_data(as_text=True)
        version = re.search(r'name="version" value="(\d+)"', page).group(1)
        self.assertEqual(self.post("/modifier/Dossier/Note", {"content": "après", "version": version}).status_code, 302)
        # Une version périmée n'écrase pas le fichier
        r = self.post("/modifier/Dossier/Note", {"content": "écrasé", "version": "0"})
        self.assertIn("modifiée ailleurs", r.get_data(as_text=True))
        r = self.post("/modifier/Dossier/Note", {"content": "écrasé", "version": "0"}, json_resp=True)
        self.assertEqual(r.status_code, 409)
        self.assertEqual((self.root / "Dossier/Note.md").read_text(encoding="utf-8"), "après")

    def test_enregistrer_sans_quitter_ctrl_s(self):
        r = self.post("/modifier/Dossier/Note", {"content": "un\r\n", "version": ""}, json_resp=True)
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["url"], "/wiki/Dossier/Note")
        r = self.post("/modifier/Dossier/Note", {"content": "deux", "version": data["version"]}, json_resp=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual((self.root / "Dossier/Note.md").read_text(encoding="utf-8"), "deux")
        self.assertEqual(len(historique.lister("Dossier/Note.md")), 1)

    def test_choix_de_liens_apercu_et_noms_invalides(self):
        self.write("a/Même.md", "")
        self.write("b/Même.md", "")
        self.write("Unique.md", "")
        page = self.client.get("/modifier/X").get_data(as_text=True)
        choix = json.loads(re.search(r'id="link-choices">(.*?)</script>', page, re.S).group(1))
        self.assertEqual([c["link"] for c in choix], ["a/Même", "b/Même", "Unique"])
        r = self.post("/apercu/X", {"content": "[[Unique]] **gras**"})
        self.assertIn('href="/wiki/Unique"', r.get_data(as_text=True))
        self.assertIn("<strong>gras</strong>", r.get_data(as_text=True))
        self.assertEqual(self.client.get("/modifier/a:b").status_code, 404)
        self.assertEqual(self.client.get("/modifier/.cache/x").status_code, 404)

    def test_nouvelle_page(self):
        self.write("Cours/Existe.md", "")
        r = self.client.get("/nouvelle/", query_string={"name": "Neuve", "folder": "Cours"})
        self.assertTrue(r.headers["Location"].endswith("/modifier/Cours/Neuve"))
        self.assertIn("déjà", self.client.get("/nouvelle/", query_string={"name": "Existe", "folder": "Cours"}).get_data(as_text=True))
        self.assertIn("ne doit pas", self.client.get("/nouvelle/", query_string={"name": "a#b"}).get_data(as_text=True))

    def test_historique_et_restauration(self):
        self.write("Note.md", "version 1")
        page = self.client.get("/modifier/Note").get_data(as_text=True)
        version = re.search(r'name="version" value="(\d+)"', page).group(1)
        self.post("/modifier/Note", {"content": "version 2", "version": version})
        versions = historique.lister("Note.md")
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["auteur"], "moi")
        page = self.client.get("/historique/Note").get_data(as_text=True)
        self.assertIn("version 1", page)
        self.assertIn('class="diff-ajout">version 2', page)
        self.assertIn(f'/historique/Note?v={versions[0]["id"]}', self.client.get("/historique/").get_data(as_text=True))
        r = self.post("/historique/Note", {"action": "restaurer", "v": versions[0]["id"]})
        self.assertEqual(r.status_code, 302)
        self.assertEqual((self.root / "Note.md").read_text(encoding="utf-8"), "version 1")
        self.assertEqual(len(historique.lister("Note.md")), 2)  # la version remplacée est gardée
        self.assertEqual(self.post("/historique/Note", {"action": "restaurer", "v": "nimporte"}).status_code, 404)
        self.assertEqual(historique.differences("a\nb", "a\nc")[-1], {"type": "ajout", "texte": "c"})

    def test_latex(self):
        r = self.post("/latex/", {"source": "\\section{Intro}\nSoit $f$.\n\\begin{theorem}Vrai.\\end{theorem}"}, json_resp=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Intro", r.get_json()["markdown"])
        self.assertEqual(self.post("/latex/", {"source": " "}, json_resp=True).status_code, 400)
        r = self.post("/latex/", {"source": "\\section{Intro}\nTexte.", "nom": "Chapitre 1", "dossier": "Cours"})
        self.assertTrue(r.headers["Location"].endswith("/wiki/Cours/Chapitre%201"))
        self.assertIn("Intro", (self.root / "Cours" / "Chapitre 1.md").read_text(encoding="utf-8"))

    def test_favoris_recents_et_corbeille(self):
        self.write("Cours/Fiche.md", "# Fiche")
        self.client.get("/wiki/Cours/Fiche")
        r = self.client.post("/favoris-wiki/Cours/Fiche", headers={"X-CSRF-Token": "jeton-test", "X-Requested-With": "fetch"})
        self.assertEqual(r.get_json(), {"ok": True, "favorite": True})
        self.assertEqual(WikiFavorite.query.filter_by(user_id=self.user.id).count(), 1)
        page = self.client.get("/wiki/").get_data(as_text=True)
        menu = page.split('class="sb-section-h"><span>Favoris')[1]
        self.assertIn('href="/wiki/Cours/Fiche"', menu.split("Récents")[0])
        self.assertIn('href="/wiki/Cours/Fiche"', menu.split("Récents")[1])
        r = self.client.post("/favoris-wiki/Cours/Fiche", headers={"X-CSRF-Token": "jeton-test", "X-Requested-With": "fetch"})
        self.assertFalse(r.get_json()["favorite"])
        r = self.post("/supprimer-page/Cours/Fiche")
        self.assertEqual(r.status_code, 302)
        self.assertFalse((self.root / "Cours" / "Fiche.md").exists())
        self.assertTrue((self.root / ".trash" / "Fiche.md").exists())
        self.assertNotIn("cours/fiche", get_vault().notes)


class ImportTests(WikiTestCase):
    def test_import_de_fichiers(self):
        fichiers = [(io.BytesIO("# Note\n![[photo.png]]".encode()), "note.md"),
                    (io.BytesIO(b"\x89PNG"), "photo.png"),
                    (io.BytesIO(b"<html>"), "page.html"),
                    (io.BytesIO(b"x"), ".obsidian/app.json"),
                    (io.BytesIO("\\section{Cours}\nTexte".encode()), "cours.tex")]
        r = self.client.post("/importer/", data={"_csrf": "jeton-test", "dossier": "Import",
                                                 "fichiers": fichiers,
                                                 "chemins": ["Coffre/note.md", "Coffre/photo.png", "page.html",
                                                             ".obsidian/app.json", "cours.tex"]},
                             headers={"Accept": "application/json"}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        res = {x["name"]: x for x in r.get_json()["results"]}
        self.assertTrue(res["Coffre/note.md"]["ok"])
        self.assertEqual(res["Coffre/note.md"]["url"], "/wiki/Import/Coffre/note")
        self.assertTrue(res["Coffre/photo.png"]["ok"])
        self.assertFalse(res["page.html"]["ok"])
        self.assertTrue(res[".obsidian/app.json"]["ignored"])
        self.assertEqual(res["cours.tex"]["message"], "converti depuis LaTeX")
        self.assertTrue((self.root / "Import/Coffre/photo.png").is_file())
        self.assertIn('src="/fichiers/Import/Coffre/photo.png"', self.html("Import/Coffre/note"))
        # Sans « Remplacer », un fichier existant est gardé ; avec, il est remplacé (historique gardé)
        def envoyer(texte, **extra):
            data = {"_csrf": "jeton-test", "dossier": "Import", "chemins": ["Coffre/note.md"],
                    "fichiers": [(io.BytesIO(texte.encode()), "note.md")], **extra}
            return self.client.post("/importer/", data=data, headers={"Accept": "application/json"},
                                    content_type="multipart/form-data").get_json()["results"][0]
        self.assertFalse(envoyer("autre")["ok"])
        self.assertEqual(envoyer("autre", remplacer="1")["message"], "remplacé")
        self.assertEqual(len(historique.lister("Import/Coffre/note.md")), 1)

    def test_image_collee_dans_l_editeur(self):
        self.write("Cours/Note.md", "")
        (self.root / "Pasted image.png").write_bytes(b"\x89PNG")
        data = {"_csrf": "jeton-test", "pour_note": "Cours/Note", "chemins": ["Pasted image.png"],
                "fichiers": [(io.BytesIO(b"\x89PNG2"), "Pasted image.png")]}
        r = self.client.post("/importer/", data=data, headers={"Accept": "application/json"}, content_type="multipart/form-data")
        res = r.get_json()["results"][0]
        self.assertTrue(res["ok"])
        self.assertEqual(res["path"], "Pasted image 1.png")
        self.assertEqual(res["link"], "Pasted image 1.png")

    def test_commande_wiki_import(self):
        source = Path(self.tmp) / "source"
        (source / ".obsidian").mkdir(parents=True)
        (source / ".obsidian" / "app.json").write_text("{}")
        (source / ".trash").mkdir()
        (source / ".trash" / "Jetée.md").write_text("x")
        (source / "Cours").mkdir()
        (source / "Accueil.md").write_text("Mon accueil [[Cours/Leçon]]", encoding="utf-8")
        (source / "Cours" / "Leçon.md").write_text("Contenu", encoding="utf-8")
        (source / "Cours" / "schema.png").write_bytes(b"\x89PNG")
        (source / "Guide").mkdir()
        (source / "Guide" / "Syntaxe Obsidian.md").write_text("Mon guide", encoding="utf-8")
        (source / "QCM").mkdir()
        (source / "QCM" / "set.json").write_text('{"titre": "Set", "questions": []}')
        # Coffre neuf : pages de bienvenue, remplacées par l'import (elles n'ont pas été modifiées)
        self.assertEqual(sorted(installation.amorcer(self.root)), ["Accueil.md", "Fichiers/hangar-logo.svg",
                                                                  "Guide/Syntaxe Obsidian.md"])
        self.assertEqual(installation.amorcer(self.root), [])  # déjà amorcé : rien
        self.write("Cours/Leçon.md", "Ma version")
        from hangar.cli import main
        sortie = io.StringIO()
        with mock.patch.dict(os.environ, {"HANGAR_VAULT": str(self.root)}), redirect_stdout(sortie):
            main(["--data", self.tmp, "wiki-import", str(source)])
        self.assertIn("copié", sortie.getvalue())
        self.assertEqual((self.root / "Accueil.md").read_text(encoding="utf-8"), "Mon accueil [[Cours/Leçon]]")
        self.assertEqual((self.root / "Guide" / "Syntaxe Obsidian.md").read_text(encoding="utf-8"), "Mon guide")
        self.assertFalse((self.root / "Fichiers").exists())  # logo du guide de bienvenue retiré (plus cité)
        self.assertEqual((self.root / "Cours" / "Leçon.md").read_text(encoding="utf-8"), "Ma version")  # gardée
        self.assertTrue((self.root / "Cours" / "schema.png").is_file())
        self.assertTrue((self.root / "QCM" / "set.json").is_file())
        self.assertFalse((self.root / ".obsidian").exists())
        self.assertFalse((self.root / ".trash").exists())
        with mock.patch.dict(os.environ, {"HANGAR_VAULT": str(self.root)}), redirect_stdout(io.StringIO()):
            main(["--data", self.tmp, "wiki-import", str(source), "--remplacer"])
        self.assertEqual((self.root / "Cours" / "Leçon.md").read_text(encoding="utf-8"), "Contenu")
        self.assertEqual(historique.lire("Cours/Leçon.md", historique.lister("Cours/Leçon.md")[0]["id"]), "Ma version")
        # Un dépôt (comme Revision) : son dossier vault/ est pris
        depot = Path(self.tmp) / "depot"
        (depot / "vault").mkdir(parents=True)
        (depot / "README.md").write_text("pas une page")
        (depot / "vault" / "Depuis le depot.md").write_text("ok")
        bilan = installation.importer_coffre(str(depot), self.root)
        self.assertEqual(bilan.copies, ["Depuis le depot.md"])
        self.assertFalse((self.root / "README.md").exists())
        with self.assertRaises(FileNotFoundError):
            installation.importer_coffre(str(Path(self.tmp) / "absent"), self.root)
        self.assertTrue(installation.est_url_git("https://github.com/wakadu13/Revision"))


class PdfEtFichiersTests(WikiTestCase):
    def setUp(self):
        super().setUp()
        (self.root / "Cours").mkdir()
        (self.root / "Cours" / "Signal.pdf").write_bytes(pdf_texte("Shannon"))

    def test_lecteur_et_fichiers(self):
        page = self.client.get("/pdf/Cours/Signal.pdf").get_data(as_text=True)
        self.assertIn('data-src="/fichiers/Cours/Signal.pdf"', page)
        self.assertIn("vendor/pdfjs/pdf.worker.min.js", page)
        integre = self.client.get("/pdf/Cours/Signal.pdf?integre=1").get_data(as_text=True)
        self.assertIn("is-embedded", integre)
        self.assertNotIn('class="sidebar"', integre)
        r = self.client.get("/fichiers/Cours/Signal.pdf")
        self.assertEqual(r.mimetype, "application/pdf")
        self.assertNotIn("attachment", r.headers.get("Content-Disposition", ""))
        r.close()
        self.write("page.html", "<script>alert(1)</script>")
        r = self.client.get("/fichiers/page.html")
        self.assertIn("attachment", r.headers["Content-Disposition"])
        self.assertIn("sandbox", r.headers["Content-Security-Policy"])
        r.close()
        self.assertEqual(self.client.get("/fichiers/../hangar.db").status_code, 404)
        self.assertEqual(self.client.get("/pdf/absent.pdf").status_code, 404)

    def test_annotations(self):
        headers = {"X-CSRF-Token": "jeton-test", "Content-Type": "application/json"}
        r = self.client.post("/pdf-annotation/", headers=headers, data=json.dumps(
            {"pdf": "Cours/Signal.pdf", "page": 1, "texte": "Shannon  dit", "cible": "note", "page_wiki": "Cours/Notes"}))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("> [!quote] Signal, p. 1\n> Shannon dit", (self.root / "Cours" / "Notes.md").read_text(encoding="utf-8"))
        r = self.client.post("/pdf-annotation/", headers=headers, data=json.dumps(
            {"pdf": "Cours/Signal.pdf", "page": 1, "texte": "Shannon", "cible": "carte"}))
        self.assertEqual(r.status_code, 200)
        carte = json.loads((self.root / "QCM" / "Annotations" / "Signal.json").read_text(encoding="utf-8"))
        self.assertEqual(carte["questions"][0]["type"], "FLASHCARD")
        self.assertEqual(self.client.post("/pdf-annotation/", headers=headers, data="{}").status_code, 400)


class DroitsEtPagesTests(WikiTestCase):
    def test_pages_de_la_section(self):
        installation.amorcer(self.root)
        self.write("Cours/Maths/Intégrales.md", "---\ntags: [maths]\n---\n# Intégrales\n$x$")
        (self.root / "Cours" / "cours.pdf").write_bytes(pdf_texte("Bonjour"))
        for url in ["/wiki/", "/wiki/Accueil", "/wiki/Guide/Syntaxe Obsidian", "/wiki/Cours/Maths/Intégrales",
                    "/modifier/Cours/Maths/Intégrales", "/sommaire/", "/sommaire/?dossier=Cours", "/tags/", "/tags/maths",
                    "/recherche/", "/recherche/?q=int", "/graphe/", "/graphe.json", "/historique/",
                    "/historique/Cours/Maths/Intégrales", "/nouvelle/", "/importer/", "/latex/", "/documentation/",
                    "/pdf/Cours/cours.pdf", "/fichiers/Fichiers/hangar-logo.svg"]:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            r.close()
        page = self.client.get("/wiki/Cours/Maths/Intégrales").get_data(as_text=True)
        self.assertIn('href="/sommaire/?dossier=Cours"', page)  # fil d'Ariane
        self.assertIn("Collections", page)
        self.assertIn('class="wiki-tree-folder" open', page)  # dossier de la page ouvert
        r = self.client.get("/static/wiki/wiki.js")
        script = r.get_data(as_text=True)
        r.close()
        self.assertIn("katex/katex.min.js", script)  # bibliothèques locales (static/vendor/), pas de CDN
        self.assertNotIn("cdn.", script)
        graphe = self.client.get("/graphe.json").get_json()
        self.assertIn("Intégrales", [n["t"] for n in graphe["noeuds"]])

    def test_dossier_qcm_hors_des_collections(self):
        self.write("QCM/Linux/Note.md", "")
        self.write("Cours/A.md", "")
        page = self.client.get("/wiki/Cours/A").get_data(as_text=True)
        menu = page.split('<aside class="sidebar"')[1].split("</aside>")[0]
        self.assertIn(">Cours<", menu)
        self.assertNotIn(">QCM<", menu)

    def test_anonyme_redirige(self):
        c = self.app.test_client()
        r = c.get("/wiki/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_contexte_pour_le_panneau_ia(self):
        from hangar import ai
        from hangar.wiki.commun import contexte_ia
        self.assertIn(contexte_ia, ai._CONTEXT_RESOLVERS)
        self.write("Cours/Note.md", "---\ntags: [x]\n---\nTexte **gras**")
        ctx = contexte_ia("/wiki/Cours/Note", {}, self.user)
        self.assertEqual(ctx, {"type": "wiki_page", "key": "Cours/Note", "title": "Note", "url": "/wiki/Cours/Note",
                               "text": "Texte **gras**"})
        self.assertEqual(contexte_ia("/wiki/Cours/Note%20absente", {}, self.user), None)
        self.assertEqual(contexte_ia("/agenda/", {}, self.user), None)
        self.assertEqual(contexte_ia("/modifier/Cours/Note", {}, self.user)["key"], "Cours/Note")

    def test_boutons_ia_seulement_si_disponible(self):
        self.write("Note.md", "x")
        self.assertNotIn("Générer un QCM", self.client.get("/wiki/Note").get_data(as_text=True))
        self.assertNotIn('data-ameliorer-url', self.client.get("/modifier/Note").get_data(as_text=True))
        with mock.patch("hangar.ai.disponible", return_value=True):
            page = self.client.get("/wiki/Note").get_data(as_text=True)
            self.assertIn("Générer un QCM", page)
            self.assertIn('action="/qcm/generer/"', page)
            self.assertIn('data-ameliorer-url="/claude/ameliorer/"', self.client.get("/modifier/Note").get_data(as_text=True))


class InviteTests(WikiTestCase):
    role = "guest"

    def test_un_invite_lit_mais_ne_modifie_pas(self):
        self.write("Note.md", "Bonjour")
        page = self.client.get("/wiki/Note").get_data(as_text=True)
        self.assertIn("Bonjour", page)
        self.assertNotIn("/modifier/Note", page)
        self.assertNotIn("Nouvelle page", page)
        for url in ["/modifier/Note", "/nouvelle/", "/importer/", "/latex/"]:
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.assertEqual(self.post("/modifier/Note", {"content": "piraté", "version": ""}).status_code, 403)
        self.assertEqual(self.post("/apercu/Note", {"content": "x"}).status_code, 403)
        self.assertEqual(self.post("/supprimer-page/Note").status_code, 403)
        self.assertEqual(self.post("/historique/Note", {"action": "restaurer", "v": "x"}).status_code, 403)
        self.assertEqual((self.root / "Note.md").read_text(encoding="utf-8"), "Bonjour")
        # Les favoris restent permis (propres au compte)
        r = self.client.post("/favoris-wiki/Note", headers={"X-CSRF-Token": "jeton-test", "X-Requested-With": "fetch"})
        self.assertTrue(r.get_json()["favorite"])


if __name__ == "__main__":
    unittest.main()
