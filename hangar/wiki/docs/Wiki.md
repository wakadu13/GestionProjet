---
title: Le wiki
---
# Le wiki de Hangar

Le wiki affiche un **coffre Obsidian** : chaque fichier `.md` du coffre devient une page, sous-dossiers compris.
Il reprend le wiki de l'application « Revision » (même auteur), avec ses QCM et ses fonctions d'IA, dans
l'interface de Hangar. Le coffre est le dossier `vault/` des données de Hangar (ou celui donné par la variable
`HANGAR_VAULT`) ; on peut l'ouvrir tel quel dans Obsidian. Pour y copier un coffre existant :
`hangar wiki-import <dossier ou adresse git>`.

Droits : tous les comptes lisent le wiki et font les QCM ; les **membres** et les **administrateurs** créent,
modifient et importent des pages (pas les invités).

## Écrire des pages

`Cours/Physique/Ohm.md` est servi sur `/wiki/Cours/Physique/Ohm`. Les modifications apparaissent au rechargement
de la page, sans redémarrer le serveur (le coffre est relu au plus toutes les 2 secondes ; ce que le wiki écrit
lui-même est visible tout de suite). La note `Accueil.md` sert de page d'accueil (`HANGAR_WIKI_HOME` pour en
choisir une autre).

Syntaxe prise en charge (voir l'onglet [Syntaxe Obsidian](/documentation/syntaxe/)) :

- liens `[[Page]]`, `[[Page|texte]]`, `[[Page#Titre]]`, `[[Page#^bloc]]`, alias définis dans les propriétés,
  liens Markdown `[texte](Page.md)` ;
- intégrations `![[image.png|300]]`, `![[Note]]`, `![[Note#Titre]]`, audio, vidéo et PDF ;
- callouts `> [!note]`, repliables avec `+` ou `-`, imbriquables ;
- propriétés YAML (`title`, `tags`, `aliases`, `icon`…), tags `#tag` et `#tag/sous-tag` ;
- `==surlignage==`, `~~barré~~`, `%%commentaires%%`, listes de tâches, tableaux, notes de bas de page,
  formules `$...$` / `$$...$$`, diagrammes ` ```mermaid `.

Un lien vers une page inexistante s'affiche en pointillés ; la page « n'existe pas encore » propose de la créer.

### Éditeur dans le navigateur

Le bouton **Modifier** d'une page (ou <kbd>Ctrl</kbd>+<kbd>E</kbd>) ouvre un éditeur façon Obsidian, construit
sur CodeMirror 6 comme Obsidian lui-même :

- **Édition** (aperçu en direct) : la mise en forme s'affiche pendant la saisie (titres, gras, liens, cases à
  cocher cliquables, images, encadrés colorés) et la syntaxe Markdown ne réapparaît que sur la ligne en cours ;
  **Source** : le Markdown brut ; **Lecture** : la page telle qu'elle sera publiée ; **Côte à côte** : le texte à
  gauche, la page rendue à droite, mise à jour pendant la saisie ;
- **formules LaTeX** rendues pendant la saisie (KaTeX) ; un clic sur une formule réaffiche son LaTeX. Bouton ∑
  ou <kbd>Ctrl</kbd>+<kbd>Maj</kbd>+<kbd>M</kbd> ;
- `[[` propose les pages (alias compris), `![[` les images et fichiers du coffre, `#` les tags ;
  <kbd>Ctrl</kbd>+clic ouvre un lien dans un nouvel onglet ; <kbd>Ctrl</kbd>+<kbd>K</kbd> : lien vers une page
  (la palette s'ouvre alors avec <kbd>Ctrl</kbd>+<kbd>P</kbd>) ;
- <kbd>Ctrl</kbd>+<kbd>S</kbd> enregistre sans quitter la page (enregistrement automatique en option), avec
  détection des modifications faites ailleurs ;
- **liens suggérés** : à l'enregistrement, les titres (ou alias) de pages existantes cités sans lien sont
  proposés sous l'éditeur ; un clic change la première mention en `[[lien]]` ;
- une image **collée** ou **glissée** dans le texte est ajoutée au coffre et intégrée, rangée comme dans
  Obsidian (réglage « Dossier des pièces jointes » de `.obsidian/app.json`) ;
- menu **Fichier** : ouvrir un `.md` ou un `.tex` de l'ordinateur, télécharger la page en `.md`, convertir du
  LaTeX en Markdown, historique des versions ;
- **brouillon de secours** : le texte non enregistré est gardé sur l'appareil et proposé à la prochaine
  ouverture (« Reprendre le brouillon » ou « Ignorer ») ;
- menus **Encadré** (définition, théorème, méthode, exemple, démonstration repliée…) et **Insérer** (symboles,
  formules types, date du jour, séparateur, aligner le tableau) ; boutons **Annuler / Rétablir** ; bouton **?**
  (aide et raccourcis) ; plan de la page, compteur de mots, recherche et remplacement (<kbd>Ctrl</kbd>+<kbd>F</kbd>) ;
- **✦ Améliorer** (avec une IA configurée) : l'IA corrige l'orthographe et rend plus clair le passage sélectionné
  (ou toute la page) sans toucher au sens ni au balisage ; <kbd>Ctrl</kbd>+<kbd>Z</kbd> annule ;
- **historique des versions** (`/historique/<page>`) : chaque enregistrement qui modifie une page garde le texte
  précédent, hors du coffre (50 versions par page au plus) ; différences ligne à ligne, texte complet,
  **restauration**.

## Lire hors ligne, installer le wiki sur un téléphone

Le wiki est une application installable (manifeste `/manifest.webmanifest`) : depuis Chrome ou Safari,
« Ajouter à l'écran d'accueil » l'installe sur Android comme sur iPhone. Un service worker (`/sw.js`) garde en
cache les pages lues, les images et les fichiers statiques : sans connexion, une page déjà ouverte s'affiche
encore ; sinon la page [hors connexion](/hors-connexion/) liste ce qui est disponible. Sur le **sommaire**,
« ⤓ Hors ligne » sur un dossier met en cache d'un coup ses pages, ses images et ses PDF (clic de nouveau pour
les retirer). Le service worker ne s'occupe que des pages de lecture du wiki : les autres sections de Hangar
et les QCM (qui ont leur propre application hors ligne) passent sans lui.

Il faut une adresse en HTTPS, ou `localhost` : sur le réseau local en HTTP simple, les navigateurs refusent les
service workers (le wiki marche, mais sans lecture hors ligne).

## Lecture : le menu se cache

Sur une page ou un PDF, le menu de gauche (collections, favoris, révision…) est caché pour laisser toute la
largeur au texte. Il revient d'un appui sur la languette › du bord gauche, d'un appui ou d'un glissement du doigt
depuis ce bord, ou quand la souris touche ce bord ; un appui sur le texte (ou <kbd>Échap</kbd>) le referme. Le
bouton ▯ en haut du menu le garde affiché pendant la lecture (choix retenu sur l'appareil) ; un nouvel appui le
cache de nouveau. Sur téléphone, le menu reste derrière le bouton ☰.

**Imprimer** : le bouton « Imprimer » d'une page (ou <kbd>Ctrl</kbd>+<kbd>P</kbd>) n'imprime que le contenu, en
clair même en thème sombre, sans menus ni boutons, encadrés repliés ouverts ; « Enregistrer au format PDF » du
navigateur donne une fiche à garder.

## Plein écran

Bouton ⛶ en haut du menu du wiki (ou dans la barre de l'éditeur, ou dans le menu ⋯ d'une page), ou
<kbd>Ctrl</kbd>+<kbd>Maj</kbd>+<kbd>F</kbd> : il ne reste que le texte. Le menu revient quand la souris touche le
**bord gauche** de l'écran, le panneau de droite (plan de la page, sommaire du PDF, plan de l'éditeur) le **bord
droit**, la barre d'outils de l'éditeur le **bord haut** ; ils se cachent quand la souris s'en éloigne. Sur écran
tactile, un appui sur le bord. <kbd>Échap</kbd> pour quitter. On passe d'une page à l'autre (menu, liens,
recherche, retour) sans quitter le plein écran ; l'éditeur, les QCM, le graphe et le lecteur PDF s'ouvrent en
rechargeant la page, ce qui fait quitter le plein écran au navigateur.

## Recherche, « Demander au wiki » et graphe

- **Recherche** (`/recherche/`) : dans les pages et dans le **texte des PDF**, page par page, avec un lien qui
  ouvre le lecteur à la bonne page. Le texte des PDF est lu une fois, en arrière-plan, à la première recherche
  (puis seulement pour les PDF nouveaux ou modifiés) ; la commande `hangar wiki-indexer-pdf` fait tout d'un coup.
  Un PDF scanné (images sans texte) n'est pas trouvable.
- **✦ Demander au wiki** (`/demander/`, menu du wiki et palette, avec une IA configurée) : le tuteur étendu à
  tout le wiki. Le serveur choisit les pages et les pages de PDF qui parlent le plus de la question, l'IA répond
  d'après elles en citant ses sources [1], [2]… : chaque numéro mène à la page ou à la page du PDF.
- **Tuteur de la page** : sous chaque page de cours, « ✦ Demander au tuteur » : une question, une réponse de l'IA
  ancrée sur la page, qui dit quand la réponse n'y est pas ; les échanges restent sous la page et nourrissent les
  questions suivantes. « Interroge-moi » lance le tuteur dans le panneau de l'assistant.
- **📷 Corriger ma copie** (`/copie/`, palette, lien sous le tuteur de chaque page) : les photos d'une copie
  manuscrite (1 à 4 pages), l'énoncé et la page du cours en option ; l'IA transcrit la démarche, la corrige étape
  par étape et fait un bilan avec une note indicative.
- **Graphe** (`/graphe/`, menu, et « Voir dans le graphe » sous les rétroliens d'une page) : les pages et leurs
  liens, colorés par dossier ; glisser pour se déplacer, molette ou deux doigts pour zoomer, survol pour voir les
  liens d'une page, clic pour l'ouvrir ; recherche d'une page, vue limitée aux voisines d'une page (1 ou 2 niveaux).

## Thème et palette de commandes

- **Thème** : bouton ☾ de la barre du haut (ou <kbd>Maj</kbd>+<kbd>D</kbd>), retenu dans le profil.
- **Palette de commandes** : <kbd>Ctrl</kbd>+<kbd>K</kbd> (ou le champ de recherche de la barre du haut) ouvre une
  page ou un PDF du wiki, un set de QCM, lance une session de langue ou une commande (nouvelle page, importer,
  Aujourd'hui, révision intelligente, Demander au wiki, plein écran, thème…), au clavier, avec une recherche
  tolérante aux accents. Dans l'éditeur, <kbd>Ctrl</kbd>+<kbd>K</kbd> reste le lien vers une page : utiliser
  <kbd>Ctrl</kbd>+<kbd>P</kbd>.

## Réviser une page

Sous chaque page, **Réviser ce cours** regroupe :

- les **sets de QCM écrits d'après la page** (par l'IA, ou avec `"source_page"` dans le JSON) : maîtrise et
  boutons Réviser, Tout, Examen ;
- **🃏 Créer les cartes de la page**, sans IA : chaque encadré à titre (`> [!definition] Titre`, théorème,
  méthode, formule…) devient une carte recto/verso et chaque surlignage `==texte==` un texte à trous, dans
  « Page (cartes) » (`QCM/<dossier de la page>/Page (cartes).json`) ; régénérer garde la progression des cartes
  inchangées ;
- avec une IA : **✦ Générer un QCM** (20 questions, en brouillon à relire sur la page Travaux de l'IA) et
  **✦ Fiche de révision** (l'essentiel de la page dans « Page (Fiche).md », liée à la page).

## Cours écrits en LaTeX

Un cours LaTeX (document `.tex` complet ou extrait) est **converti en page** du wiki : chapitres et sections
numérotés, formules (macros `\newcommand` développées, équations numérotées), théorèmes, définitions,
remarques… (`\newtheorem`) et encadrés `tcolorbox` en callouts colorés, démonstrations repliées, listes,
tableaux, renvois `\ref` et notes de bas de page. Trois façons :

- importer le fichier `.tex` sur `/importer/` (il devient `nom.md`), ou la page **Cours LaTeX** (`/latex/`) ;
- dans l'éditeur, coller le LaTeX (la conversion est proposée), ou menu **Fichier ▸ Ouvrir un fichier .md ou .tex** ;
- une page qui contient déjà du LaTeX brut affiche un bandeau « Le convertir en page ».

Les figures dessinées en LaTeX (TikZ, circuitikz…) ne peuvent pas être affichées sans LaTeX : leur légende est
gardée et leur code source rangé dans un encadré replié. Pour les voir, importer aussi le PDF compilé et
l'intégrer avec `![[cours.pdf]]`.

## Importer des fichiers .md et des PDF

**Importer** dans le menu du wiki (`/importer/`) : glisser-déposer des notes `.md`, des PDF, ou un **dossier
entier** (un coffre Obsidian par ex.) dont les sous-dossiers, images, PDF, sons et vidéos sont importés avec.
On choisit le dossier de destination ; un fichier existant n'est remplacé que si « Remplacer » est coché. Les
dossiers cachés (`.obsidian`, `.trash`) et les autres types de fichiers (HTML, scripts…) sont ignorés.
Limites : 5 Mo par note, 100 Mo par pièce jointe. Les images de plus de 2000 pixels de côté sont réduites à
l'import, sous le même nom.

**Images hébergées sur d'autres sites** (`![légende](https://…)`) : le wiki ne charge jamais rien d'un autre
site ; il affiche à leur place un lien « Image externe : légende ↗ ». Le bouton **« Enregistrer ces images dans
le wiki »** de la page d'import (ou `hangar wiki-import --images`) les télécharge une fois dans le dossier
`image/` du coffre (ou celui réglé dans Obsidian), puis remplace leurs liens par `![[image.png|légende]]` :
elles s'affichent alors aussi hors ligne. Seules de vraies images (PNG, JPEG, GIF, WebP, BMP, AVIF) de 20 Mo au
plus sont gardées ; l'ancien texte des pages modifiées reste dans l'historique.

### Transformer un cours en page avec l'IA

Avec une IA configurée (réglages de l'IA, `/ia/reglages/`), chaque cours importé (PDF ou `.tex`) peut devenir
une page du wiki rédigée par l'IA : plan du cours en sections, formules en LaTeX, encadrés pour les définitions,
théorèmes, démonstrations (repliées), méthodes et pièges. Case **« Transformer les cours avec l'IA »** sur la page
d'import (cochée par défaut, choix retenu sur l'appareil), ou bouton **« Créer une page de cours avec l'IA »** sur
un PDF. La transformation se fait en arrière-plan ; la page **Travaux de l'IA** (`/transformations/`) suit
l'avancement et permet de relancer un échec. Une page modifiée à la main n'est jamais écrasée. Case **« sans
urgence »** : la demande part dans un lot à moitié prix (Claude), traité sous 24 heures.

## Lire les PDF dans le wiki

Les PDF du coffre s'ouvrent dans un lecteur intégré (`/pdf/<chemin>`, construit sur PDF.js de Mozilla) qui
marche aussi sur téléphone : défilement continu, zoom (aussi <kbd>Ctrl</kbd>+molette), recherche dans le texte
sans tenir compte des accents, sommaire du document, plein écran, reprise à la dernière page lue, téléchargement.

- Les PDF apparaissent dans l'arborescence du menu et dans le sommaire.
- Dans une note : `[[cours.pdf]]` est un lien vers le lecteur, `![[cours.pdf]]` affiche le lecteur dans la page
  et `![[cours.pdf#page=3]]` l'ouvre à la page 3.
- La page d'un PDF liste les notes qui le citent (rétroliens).
- **Garder un passage** : sélectionner du texte dans le lecteur fait apparaître « 🃏 Garder ce passage » : soit
  une **carte de révision** recto/verso dans le set « <PDF> — annotations » (`QCM/Annotations/<PDF>.json`), soit
  une **citation** ajoutée au bas d'une page du wiki (par défaut la page du cours, créée au besoin).

## Erreurs du serveur

Page [Erreurs du serveur](/erreurs/) (administrateurs, section Réglages, avec un badge quand il y en a de
nouvelles) : chaque page en erreur 500 et chaque incident du fil des travaux de l'IA, regroupés quand ils se
répètent, avec l'adresse, le compte, l'endroit du code et la trace complète ; « Marquer comme réglée »,
« Tout marquer comme réglé », « Effacer les erreurs réglées ». Une erreur réglée qui revient est rouverte.

## Adresses

- `/wiki/` : accueil du wiki ; `/wiki/<chemin>` : une page ; `/fichiers/<chemin>` : pièces jointes du coffre
- `/modifier/<chemin>` : éditeur ; `/nouvelle/` : nouvelle page ; `/importer/` : import ; `/latex/` : cours LaTeX
- `/pdf/<chemin>` : lecteur de PDF (`?integre=1` : version intégrée dans les notes) ; `/pdf-annotation/` (POST)
- `/sommaire/`, `/tags/`, `/tags/<tag>`, `/recherche/?q=…`, `/graphe/` et `/graphe.json`
- `/historique/` et `/historique/<page>` : versions des pages
- `/palette.json` : données de la palette de commandes
- `/documentation/` et `/documentation/<ia|qcm|syntaxe|hangar>/` : cette documentation
- `/sw.js`, `/manifest.webmanifest`, `/hors-connexion/`, `/hors-ligne/dossier.json` : lecture hors ligne
- `/erreurs/` : erreurs du serveur (administrateurs)
- `/demander/`, `/copie/`, `/claude/tuteur/`, `/claude/ameliorer/`, `/transformations/` : fonctions d'IA
- `/qcm/…` : QCM, langues, statistiques, planning ; `/qcm/aujourdhui/` : révisions du jour
- anciennes adresses de Revision, redirigées : `/aujourdhui/` → `/qcm/aujourdhui/`, `/claude/reglages/` →
  `/ia/reglages/`, `/compte/` → profil, `/utilisateurs/` → équipe, `/connexion/` → connexion
