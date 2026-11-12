"""Interface (vague v2b, groupe « ux-front ») : menus de l'éditeur du wiki (piste 17), fenêtres et petits écrans
(21), contraste des couleurs (33), clavier (34), menu Réglages et palette (52), messages d'erreur et tutoiement
(58), étiquettes et noms des champs, boutons et fenêtres (67).

Le comportement dans le navigateur a été vérifié avec Playwright ; ces tests figent ce qui se lit sans navigateur :
le HTML rendu, les feuilles de style (jetons de couleur, règles) et les points clés des scripts."""
import os
import re
import shutil
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from hangar import create_app
from hangar.extensions import db
from hangar.models import User

ROOT = Path(__file__).resolve().parent.parent / "hangar"
STATIC = ROOT / "static"
TEMPLATES = ROOT / "templates"
APP_JS = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
APP_CSS = (STATIC / "css" / "app.css").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------------------------------
# Outils : arbre HTML minimal, règles CSS, contraste WCAG
# ---------------------------------------------------------------------------------------------------------------
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.children, self.text = tag, dict(attrs), parent, [], ""

    def ancestors(self):
        n = self.parent
        while n is not None:
            yield n
            n = n.parent

    def classes(self):
        return (self.attrs.get("class") or "").split()

    def iter(self):
        for c in self.children:
            yield c
            yield from c.iter()

    def all_text(self):
        return (self.text + "".join(c.all_text() for c in self.children)).strip()


class Tree(HTMLParser):
    """Arbre tolérant : les balises vides sont fermées d'office, une fermeture orpheline est ignorée."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.cur = self.root
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.text += data

    def find_all(self, pred):
        return [n for n in self.root.iter() if pred(n)]

    def by_id(self, ident):
        found = self.find_all(lambda n: n.attrs.get("id") == ident)
        return found[0] if found else None


def css_rules(css):
    """[(sélecteurs, {propriété: valeur})] de toutes les règles, y compris dans les @media."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        sel = m.group(1).strip()
        if sel.startswith("@"):
            sel = sel.split("{")[-1].strip()
        decls = {}
        for d in m.group(2).split(";"):
            if ":" in d:
                k, v = d.split(":", 1)
                decls[k.strip()] = v.strip()
        out.append(([s.strip() for s in sel.split(",")], decls))
    return out


def rules_for(css, selector):
    return [d for sels, d in css_rules(css) if selector in sels]


def tokens(css, block_re):
    body = re.search(block_re, css, re.S).group(1)
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", body))


def contrast(a, b):
    def lum(h):
        rgb = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def sans_accents(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn").lower()


# ---------------------------------------------------------------------------------------------------------------
class AppCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-uxfront-")
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HANGAR_VAULT", None)  # jamais le vrai coffre de l'utilisateur
        self.app = create_app(self.tmp, testing=True)
        with self.app.app_context():
            for username, role in (("chef", "admin"), ("eleve", "member"), ("invite", "guest")):
                u = User(username=username, email=f"{username}@hangar.local", role=role, first_name=username.title())
                u.set_password("secret1")
                db.session.add(u)
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def token(self, c):
        return re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)

    def essai(self, c, login, password, ip="10.1.1.1"):
        page = c.get("/login", environ_base={"REMOTE_ADDR": ip}).get_data(as_text=True)
        tok = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        return c.post("/login", data={"login": login, "password": password, "_csrf": tok}, environ_base={"REMOTE_ADDR": ip})

    def client(self, username="chef"):
        c = self.app.test_client()
        self.assertEqual(self.essai(c, username, "secret1", ip="10.9.9.9").status_code, 302)
        return c

    def page(self, c, url, status=200):
        r = c.get(url)
        self.assertEqual(r.status_code, status, url)
        return r.get_data(as_text=True)


# ---------------------------------------------------------------------------------------------------------------
# Piste 17 : menus « Encadré » et « Insérer » de l'éditeur du wiki
# ---------------------------------------------------------------------------------------------------------------
class EditeurWikiMenus(AppCase):
    def test_menus_de_l_editeur_hors_du_gestionnaire_des_menus_dd(self):
        """Le gestionnaire général des menus (app.js) ne doit attraper que des déclencheurs placés dans un .dd :
        avant, les <details data-menu> de l'éditeur étaient captés, le clic annulé, puis le script plantait."""
        html = self.page(self.client(), "/modifier/Essai")
        tree = Tree(html)
        self.assertTrue(tree.find_all(lambda n: n.tag == "details" and "toolbar-menu" in n.classes()))
        for n in tree.find_all(lambda n: "data-menu" in n.attrs):
            self.assertTrue(any("dd" in a.classes() for a in n.ancestors()),
                            f"<{n.tag} data-menu> hors d'un .dd : le gestionnaire de app.js le capterait")
        # Les deux menus gardent ce dont editor.js a besoin (le conteneur intérieur)
        self.assertIn("data-menu-encadres", html)
        self.assertIn("data-menu-inserer", html)

    def test_app_js_ne_capte_que_les_declencheurs_dans_un_dd(self):
        self.assertIn("const MENU_TRIGGER = '.dd [data-menu]';", APP_JS)
        self.assertNotIn("e.target.closest('[data-menu]')", APP_JS)


# ---------------------------------------------------------------------------------------------------------------
# Piste 21 : fenêtres sur un portable, petits écrans
# ---------------------------------------------------------------------------------------------------------------
class FenetresEtPetitsEcrans(AppCase):
    def test_le_corps_de_la_fenetre_defile_seul(self):
        """Contenu dans un <form> : le form devient une colonne flexible, seul .modal-b défile ; l'en-tête
        et les boutons (« Créer la tâche ») restent visibles même sur un écran de 650 px de haut."""
        form = rules_for(APP_CSS, "dialog.modal > form")
        self.assertTrue(form, "aucune règle dialog.modal > form")
        merged = {k: v for d in form for k, v in d.items()}
        self.assertEqual(merged.get("display"), "flex")
        self.assertEqual(merged.get("flex-direction"), "column")
        self.assertEqual(merged.get("min-height"), "0")
        self.assertIn("hidden", [d.get("overflow") for d in rules_for(APP_CSS, "dialog.modal[open]")])
        body = {k: v for d in rules_for(APP_CSS, ".modal-b") for k, v in d.items()}
        self.assertEqual(body.get("overflow"), "auto")
        self.assertEqual(body.get("min-height"), "0")
        self.assertTrue(body.get("flex", "").startswith("1"))

    def test_toutes_les_fenetres_ont_leur_corps_au_bon_endroit(self):
        """La règle globale suppose .modal-b enfant du <dialog> ou de son <form> : sinon le contenu serait coupé."""
        c = self.client()
        for url in ("/agenda", "/team/", "/bilan", "/settings/profile"):
            tree = Tree(self.page(c, url))
            for d in tree.find_all(lambda n: n.tag == "dialog" and "modal" in n.classes()):
                bodies = [n for n in d.iter() if "modal-b" in n.classes()]
                self.assertTrue(bodies, f"{url} #{d.attrs.get('id')} sans .modal-b")
                for b in bodies:
                    ok = b.parent is d or (b.parent.tag == "form" and b.parent.parent is d)
                    self.assertTrue(ok, f"{url} #{d.attrs.get('id')} : .modal-b mal placé")

    def test_icone_du_bouton_dans_un_etat_vide(self):
        """« .empty .ic » grossissait aussi l'icône du bouton « Importer un fichier .ics » (40 px dans 26 px)."""
        self.assertFalse(rules_for(APP_CSS, ".empty .ic"))
        self.assertEqual(rules_for(APP_CSS, ".empty > .ic")[0].get("width"), "40px")

    def test_periode_libre_du_bilan_passe_a_la_ligne(self):
        tree = Tree(self.page(self.client(), "/bilan"))
        actions = tree.find_all(lambda n: "header-actions" in n.classes())[0]
        form = [n for n in actions.iter() if n.tag == "form"][0]
        self.assertIn("wrap", form.classes())

    def test_fil_d_ariane_coupe_avec_points_de_suspension(self):
        first = {k: v for d in rules_for(APP_CSS, ".crumbs > a:first-of-type") for k, v in d.items()}
        self.assertEqual(first.get("flex"), "0 1 auto")
        names = {k: v for d in rules_for(APP_CSS, ".crumbs > a") for k, v in d.items()}
        self.assertEqual(names.get("text-overflow"), "ellipsis")
        self.assertEqual(names.get("min-width"), "0")


# ---------------------------------------------------------------------------------------------------------------
# Piste 33 : contraste (WCAG AA 4,5:1) en thème clair et sombre
# ---------------------------------------------------------------------------------------------------------------
LIGHT = r":root\s*\{(.*?)\}"
DARK = r':root\[data-theme="dark"\]\s*\{(.*?)\}'
DARK_SYSTEM = r':root\[data-theme="system"\]\s*\{(.*?)\}'
TEXTES = ("text", "text-2", "text-3", "accent-text", "danger", "success", "warning")
FONDS = ("bg", "bg-2", "bg-3", "sidebar", "surface")


class Contraste(unittest.TestCase):
    def themes(self):
        return {"clair": tokens(APP_CSS, LIGHT), "sombre": tokens(APP_CSS, DARK)}

    def test_les_deux_blocs_du_theme_sombre_sont_identiques(self):
        self.assertEqual(tokens(APP_CSS, DARK), tokens(APP_CSS, DARK_SYSTEM))

    def test_textes_lisibles_sur_tous_les_fonds(self):
        for theme, t in self.themes().items():
            for fg in TEXTES:
                for bg in FONDS:
                    self.assertGreaterEqual(contrast(t[fg], t[bg]), 4.5, f"{theme} : --{fg} sur --{bg}")
            for fg in ("text-2", "text-3"):  # lignes survolées, élément actif
                for bg in ("hover", "active"):
                    self.assertGreaterEqual(contrast(t[fg], t[bg]), 4.5, f"{theme} : --{fg} sur --{bg}")
            for name in ("danger", "success", "warning"):  # pastilles colorées (badge-success…)
                self.assertGreaterEqual(contrast(t[name], t[f"{name}-bg"]), 4.5, f"{theme} : --{name} sur --{name}-bg")

    def test_bouton_principal(self):
        """Texte du bouton principal (et des pastilles d'accent) au repos et au survol, dans les deux thèmes."""
        for theme, t in self.themes().items():
            self.assertGreaterEqual(contrast(t["on-accent"], t["accent"]), 4.5, theme)
            self.assertGreaterEqual(contrast(t["on-accent"], t["accent-2"]), 4.5, f"{theme} (survol)")
            self.assertGreaterEqual(contrast(t["on-danger"], t["danger"]), 4.5, f"{theme} (bouton Supprimer)")
        for sel in (".btn-primary", ".btn-primary:hover", ".sb-badge", ".rail-badge"):
            self.assertEqual(rules_for(APP_CSS, sel)[0].get("color"), "var(--on-accent)", sel)

    def test_plus_de_blanc_en_dur_sur_l_accent(self):
        for path in [STATIC / "css" / "app.css", STATIC / "qcm" / "qcm.css", STATIC / "ai" / "ai.css", STATIC / "wiki" / "wiki.css"]:
            for sels, d in css_rules(path.read_text(encoding="utf-8")):
                if d.get("background") in ("var(--accent)", "var(--accent-2)"):
                    self.assertNotIn(d.get("color"), ("#fff", "#ffffff", "white"), f"{path.name} {sels}")


# ---------------------------------------------------------------------------------------------------------------
# Piste 34 : clavier
# ---------------------------------------------------------------------------------------------------------------
class Clavier(AppCase):
    def test_lien_aller_au_contenu(self):
        tree = Tree(self.page(self.client(), "/projects/"))
        body = tree.find_all(lambda n: n.tag == "body")[0]
        focusables = [n for n in body.iter() if n.tag in ("a", "button", "input", "select", "textarea") and n.attrs.get("type") != "hidden"]
        first = focusables[0]
        self.assertEqual((first.tag, first.attrs.get("href")), ("a", "#contenu"))
        self.assertEqual(first.all_text(), "Aller au contenu")
        main = tree.by_id("contenu")
        self.assertEqual(main.tag, "main")
        self.assertEqual(main.attrs.get("tabindex"), "-1")

    def test_echap_aria_expanded_et_focus_rendu(self):
        # Échap ferme le menu ouvert et rend le focus au bouton qui l'a ouvert
        self.assertRegex(APP_JS, r"if \(e\.key !== 'Escape' \|\| e\.defaultPrevented\) return;")
        self.assertIn("if (trigger && (dd.contains(active) || active === document.body)) trigger.focus();", APP_JS)
        # état annoncé des menus et des groupes repliables
        self.assertIn("t.setAttribute('aria-expanded'", APP_JS)
        # sélecteur : focus rendu après Échap ou un choix ; rechargement : focus retrouvé
        self.assertIn("if (back && p) giveBack(p);", APP_JS)
        self.assertIn("focus = viaClavier ? focusSelector(document.activeElement) : null", APP_JS)
        self.assertIn("const f = saved.focus && $(saved.focus);", APP_JS)

    def test_selecteur_dans_une_fenetre(self):
        """Ouvert depuis la fenêtre « Nouveau ticket », le sélecteur (état, priorité…) était ajouté hors de la
        fenêtre : caché derrière elle et inerte (ni souris ni clavier), et Échap fermait toute la fenêtre."""
        self.assertIn("(anchor.closest('dialog[open]') || document.body).appendChild(el);", APP_JS)
        self.assertIn("input.focus({ preventScroll: true })", APP_JS)
        self.assertIn("focusBack: () => $('.prop', box)", APP_JS)

    def test_seance_focalisee_visible(self):
        for d in rules_for(APP_CSS, ".wk-ev:focus-visible"):
            self.assertNotEqual(d.get("outline"), "none")
        self.assertTrue(any(d.get("outline", "").startswith("2px solid") for d in rules_for(APP_CSS, ".wk-ev:focus-visible")))


# ---------------------------------------------------------------------------------------------------------------
# Piste 52 : un seul menu Réglages, mêmes libellés, palette
# ---------------------------------------------------------------------------------------------------------------
class MenuReglages(AppCase):
    def test_plus_de_second_menu_dans_les_pages(self):
        self.assertFalse((TEMPLATES / "settings" / "_nav.html").exists())
        c = self.client()
        for url in ("/settings/profile", "/settings/workspace", "/settings/backup", "/settings/tokens", "/settings/about", "/settings/mail"):
            self.assertNotIn("settings-nav", self.page(c, url), url)

    def test_memes_libelles_partout(self):
        tree = Tree(self.page(self.client(), "/settings/profile"))
        sidebar = tree.by_id("sidebar")
        side = {a.attrs["href"]: a.text.strip() for a in sidebar.iter() if a.tag == "a" and "sb-link" in a.classes()}
        menu = tree.find_all(lambda n: "tb-ws" in n.classes())[0]
        top = {a.attrs["href"]: a.text.strip() for a in menu.iter() if a.tag == "a" and "menu-item" in a.classes()}
        commun = set(side) & set(top)
        self.assertGreaterEqual(len(commun), 6)
        for href in commun:
            self.assertEqual(top[href], side[href], href)
        self.assertEqual(side.get("/team/"), "Membres")  # l'ancien menu de la page le proposait
        # Les liens qui mènent à une autre section le disent
        labels = {a.attrs["href"]: a.all_text() for a in sidebar.iter() if a.tag == "a"}
        self.assertIn("→ Agenda", labels["/agenda/settings"])
        self.assertIn("→ IA", labels["/ia/reglages/"])

    def test_la_palette_trouve_les_reglages(self):
        texts = []
        for chunk in re.findall(r"\{ title: '[^']+', icon: [^}]*\}", APP_JS):
            title = re.search(r"title: '([^']+)'", chunk).group(1)
            mots = re.search(r"mots: '([^']*)'", chunk)
            texts.append(sans_accents(f"{title} {mots.group(1) if mots else ''}"))
        for q in ("réglages", "paramètres", "sauvegarde", "planning", "bilan", "envoi des e-mails", "fournisseurs"):
            self.assertTrue(any(sans_accents(q) in t for t in texts), q)
        self.assertIn("`${a.title} ${a.mots || ''}`", APP_JS)
        # Adresses fournies à la palette : celles des administrateurs seulement pour eux
        admin = self.page(self.client("chef"), "/projects/")
        member = self.page(self.client("eleve"), "/projects/")
        self.assertIn('"backup": "/settings/backup"', admin)
        self.assertIn('"backup": null', member)
        self.assertIn('"planning": "/planning"', member)


# ---------------------------------------------------------------------------------------------------------------
# Piste 58 : messages d'erreur, connexion, tutoiement
# ---------------------------------------------------------------------------------------------------------------
class MessagesEtConnexion(AppCase):
    def test_erreur_de_connexion_dans_la_page(self):
        c = self.app.test_client()
        r = self.essai(c, "eleve", "faux")
        html = r.get_data(as_text=True)
        tree = Tree(html)
        err = tree.find_all(lambda n: "form-error" in n.classes())
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0].attrs.get("role"), "alert")
        self.assertIn("Identifiant ou mot de passe incorrect.", err[0].all_text())
        self.assertEqual(tree.by_id("login-ident").attrs.get("value"), "eleve")  # identifiant gardé
        self.assertNotIn("data-flash", html)  # plus de bulle qui disparaît en 5 s

    def test_essais_restants_puis_blocage_avec_la_commande(self):
        c = self.app.test_client()
        for _ in range(2):
            self.essai(c, "eleve", "faux")
        self.assertIn("Encore 2 essais avant un blocage de 30 minutes.", self.essai(c, "eleve", "faux").get_data(as_text=True))
        self.assertIn("Encore 1 essai avant", self.essai(c, "eleve", "faux").get_data(as_text=True))
        html = self.essai(c, "eleve", "faux").get_data(as_text=True)
        self.assertIn("connexion bloquée pendant encore 30 minutes", html)
        self.assertIn("uv run hangar connexions-debloquer", html)
        with self.app.app_context():
            from hangar import blocage_connexion as blocage
            self.assertEqual(blocage.essais_restants("eleve", "10.1.1.1"), 0)
            self.assertEqual(blocage.essais_restants("eleve", "10.1.1.2"), 5)

    def test_pages_d_erreur_en_francais(self):
        c = self.client("eleve")
        r = c.get("/nexistepas")
        self.assertEqual(r.status_code, 404)
        html = r.get_data(as_text=True)
        self.assertNotIn("The requested URL", html)
        self.assertIn("Cette page n’existe pas ou plus.", html)
        r = c.get("/settings/workspace")  # abort(403) sans explication
        self.assertEqual(r.status_code, 403)
        self.assertIn("Tu n’as pas accès", r.get_data(as_text=True))
        r = c.get("/agenda/tasks/clear-done")  # réservée au POST
        self.assertEqual(r.status_code, 405)
        html = r.get_data(as_text=True)
        self.assertIn('lang="fr"', html)
        self.assertNotIn("Method Not Allowed", html)
        self.assertIn("Action non disponible", html)
        self.assertIn("POST", r.headers.get("Allow", ""))
        r = c.get("/api/reactions")
        self.assertEqual(r.status_code, 405)
        self.assertNotIn("not allowed", r.get_json()["error"])

    def test_bulles_d_erreur_annoncees_et_fermables(self):
        c = self.client()
        with c.session_transaction() as s:
            s["_flashes"] = [("error", "Boum"), ("success", "Bravo")]
        tree = Tree(self.page(c, "/projects/"))
        box = tree.by_id("toasts")
        self.assertEqual(box.attrs.get("aria-live"), "polite")
        roles = {n.all_text(): n.attrs.get("role") for n in box.children if "toast" in n.classes()}
        self.assertEqual(roles, {"Boum": "alert", "Bravo": "status"})
        # app.js : une erreur reste jusqu'à ce qu'on la ferme (bouton « Fermer »)
        self.assertIn("if (el.classList.contains('error')) return;", APP_JS)
        self.assertIn("x.setAttribute('aria-label', 'Fermer');", APP_JS)

    def test_nouveau_ticket_sans_projet_ouvre_la_creation_de_projet(self):
        self.assertIn('"newProject": "/projects/new"', self.page(self.client("eleve"), "/projects/"))
        self.assertIn('"newProject": null', self.page(self.client("invite"), "/agenda"))
        self.assertIn("location.href = H.nav.newProject;", APP_JS)

    def test_tutoiement(self):
        vous = re.compile(r"(?<![-\w])(vous|votre|vos)(?![-\w])|\b(continuez|pensez|écrivez|tapez|choisissez|choisissez-la|"
                          r"tracez|nommez|placez|collez|vérifiez|enregistrez|sélectionnez|téléchargez-le|refaites)\b",
                          re.I)
        fichiers = [STATIC / "qcm" / "quiz.js", STATIC / "qcm" / "masque.js", STATIC / "wiki" / "editor.js",
                    STATIC / "wiki" / "lecteur_pdf.js", TEMPLATES / "partials" / "sidebar_travail.html",
                    TEMPLATES / "bilan" / "index.html"]
        for path in fichiers:
            text = path.read_text(encoding="utf-8")
            if path.suffix == ".js":  # seulement les chaînes, pas les commentaires
                text = "\n".join(re.findall(r"\"[^\"\n]*\"|'[^'\n]*'|`[^`]*`", text))
            found = vous.findall(text)
            self.assertFalse(found, f"{path.name} : {found}")


# ---------------------------------------------------------------------------------------------------------------
# Piste 67 : étiquettes reliées, boutons et fenêtres nommés
# ---------------------------------------------------------------------------------------------------------------
class EtiquettesEtNoms(AppCase):
    def test_connexion_etiquettes_reliees(self):
        tree = Tree(self.page(self.app.test_client(), "/login"))
        for name in ("login", "password"):
            inp = tree.find_all(lambda n: n.tag == "input" and n.attrs.get("name") == name)[0]
            labels = tree.find_all(lambda n: n.tag == "label" and n.attrs.get("for") == inp.attrs.get("id"))
            self.assertTrue(inp.attrs.get("id") and labels, name)

    def test_bouton_menu_fenetres_et_fermer_nommes(self):
        tree = Tree(self.page(self.client(), "/projects/"))
        for b in tree.find_all(lambda n: "data-sidebar-toggle" in n.attrs):
            self.assertTrue(b.attrs.get("aria-label") or b.attrs.get("title"))
        dlg = tree.by_id("create-issue-modal")
        heading = tree.by_id(dlg.attrs.get("aria-labelledby"))
        self.assertEqual(heading.all_text(), "Nouveau ticket")
        close = [n for n in dlg.iter() if "data-close" in n.attrs][0]
        self.assertEqual(close.attrs.get("aria-label"), "Fermer")
        self.assertTrue(tree.by_id("palette").attrs.get("aria-label"))
        palette_input = [n for n in tree.by_id("palette").iter() if n.tag == "input"][0]
        self.assertTrue(palette_input.attrs.get("aria-label"))

    def test_garde_fou_pour_les_autres_gabarits(self):
        """Les ~120 autres « <label>Texte</label><input> » sont reliés au chargement par app.js."""
        self.assertIn("$$('label:not([for])', root)", APP_JS)
        self.assertIn("lab.htmlFor = f.id;", APP_JS)
        self.assertIn("b.setAttribute('aria-label', 'Fermer');", APP_JS)
        self.assertIn("d.setAttribute('aria-labelledby', h.id);", APP_JS)
        # le motif existe bien dans les gabarits (le garde-fou a du travail)
        tree = Tree(self.page(self.client(), "/settings/profile"))
        orphans = tree.find_all(lambda n: n.tag == "label" and "for" not in n.attrs and n.parent is not None
                                and "field" in n.parent.classes())
        self.assertTrue(orphans)


if __name__ == "__main__":
    unittest.main()
