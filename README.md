# ✈️ Hangar — agenda, cours, QCM, assistant IA et gestion de projet, 100 % locaux

Application web en **Python** (Flask + SQLite) organisée comme [Plane](https://plane.so) : une barre d'icônes à gauche donne accès à six sections.

| Section | Contenu |
|---|---|
| **Agenda** | tableau de bord, emploi du temps importé chaque semaine (`.ics`), calendrier, tâches, rappels et **résumé quotidien par e-mail** |
| **Travail** | gestion de projet d'équipe : projets, tickets, cycles, modules, **initiatives**, **Workgraph**, planning, analytique, bilans |
| **Wiki** | le coffre Obsidian des cours (port complet de [Revision](https://github.com/wakadu13/Revision)) : rendu Obsidian, formules, éditeur, historique, PDF, graphe |
| **QCM** | révision espacée (FSRS) des sets de QCM et des langues, planning d'examens, statistiques |
| **IA** | assistant façon « Plane AI » branché sur **Claude** ou sur des **IA open source** (Ollama, LM Studio, vLLM…), avec les fonctions d'IA de Revision |
| **Réglages** | profil, espace (dont le coffre du wiki), e-mails, fournisseurs d'IA, sauvegardes, version |

Aucun service en ligne n'est nécessaire : les données restent sur ta machine et l'interface fonctionne hors connexion (KaTeX, Mermaid, CodeMirror et pdf.js sont embarqués, aucun CDN). Seul l'assistant IA contacte le fournisseur que tu choisis (et rien du tout avec un modèle local).

## Agenda : emploi du temps, tâches et rappels

Le **tableau de bord** (page d'accueil) montre la séance en cours ou la prochaine (avec compte à rebours), le programme du jour, la semaine en grille horaire, les tâches à faire, les contrôles à venir et les changements du dernier planning importé.

**Importer l'emploi du temps chaque semaine** (menu *Emploi du temps*, ou le bandeau du tableau de bord)
- Dépose le fichier `.ics` exporté de ton planning. **Le dernier fichier importé fait foi** :
  - chaque séance est reconnue par son identifiant (`UID`) : si l'horaire, la salle ou l'intervenant changent, elle est **mise à jour** et marquée « modifiée » (avant → après) ;
  - une séance absente du nouveau fichier est **retirée** (barrée dans le calendrier), seulement pour les semaines couvertes par le fichier et à partir d'aujourd'hui : le passé n'est jamais effacé. Une semaine n'est couverte que si le fichier y apporte au moins une vraie séance d'une journée : un bloc de plusieurs jours (formation militaire…) ou une séance `(PREVISION)` qui déborde sur une autre semaine n'y retire rien ;
  - garde-fou : si une semaine voisine perdait la majorité de ses séances alors que le fichier n'y en apporte qu'une ou deux, rien n'y est retiré et la page de l'import l'explique. Une alerte s'affiche aussi quand des séances d'aujourd'hui ou de demain sont retirées ;
  - tes **notes**, **rappels** et **tâches liées** restent attachés à la séance d'un import à l'autre ;
  - chaque import a sa page de détail (ajouts, modifications, retraits) et le **dernier import peut être annulé** (les rappels des séances, y compris celui des contrôles, reviennent aussi). Le fichier d'origine est conservé.
- Format de l'école reconnu : `Réseaux avancés 3 CM` → matière, n° de séance, type (CM, TD/TP, contrôle écrit, sport, militaire, conférence…) ; `(PREVISION)` → séance prévisionnelle (hachurée) ; rubriques `Apprenants` et `Intervenants` de la description. Les autres fichiers `.ics` (Google Agenda, Outlook…) fonctionnent aussi.
- En ligne de commande : `uv run hangar agenda-import planning.ics` (option `--mode future` pour remplacer tout le planning à venir, `--mode merge` pour ne rien retirer).

**Calendrier** : vues semaine (grille horaire, ligne « maintenant »), mois et liste ; clic sur une séance pour ses détails, une note personnelle, un rappel ou une tâche liée ; clic sur un créneau libre pour créer une tâche à cette heure ; export `.ics` (cours et/ou tâches, avec les rappels) vers Google Agenda / Outlook / téléphone ; impression.

**Tâches** : date, heure de début et de fin (ou journée entière), notes, priorité, catégorie, couleur, séance liée, **répétition** (tous les jours, du lundi au vendredi, chaque semaine, toutes les deux semaines, chaque mois, le même jour : une tâche du 31 revient le 31 quand le mois le permet — cocher l'occurrence passe à la suivante, avec un bouton « Annuler » en cas d'erreur), report rapide (demain, lundi, +1 semaine). Raccourci clavier `T`.

**Rappels et e-mails** (menu *Rappels et e-mails*)
- Rappels par tâche ou par séance : à l'heure, 15 min, 1 h, la veille, 2 jours, 1 semaine avant… ou à une date précise ; par **e-mail + notification** dans l'application, ou notification seule.
- **Résumé quotidien** par e-mail (activé par défaut à 7 h) : un seul message avec tout ce qu'il y a à faire — cours du jour (horaires, salles), tâches du jour et en retard, tickets de projet à échéance, cartes de QCM à réviser et examens proches, contrôles de la semaine, changements récents du planning, initiatives à risque. Le matin pour la journée, ou la veille au soir pour le lendemain ; rien n'est envoyé les jours vides ; aperçu dans *Agenda › Résumé quotidien*.
- **Rappel automatique des contrôles** (la veille, 2 jours, 1 semaine avant), e-mail récapitulatif des **changements après chaque import**.
- Pour recevoir réellement les e-mails : renseigne ton adresse dans *Rappels et e-mails*, puis (administrateur) configure l'envoi dans *Réglages › Envoi des e-mails* (aussi dans le menu de l'espace) — mode **SMTP**, préréglage Gmail avec un [mot de passe d'application](https://myaccount.google.com/apppasswords). Sinon, les messages sont seulement enregistrés en `.eml` dans `data/outbox/`.
- Les rappels partent tant que Hangar est lancé (`uv run hangar`). Sous Windows, fais-le démarrer tout seul avec ta session : voir [Sous Windows](#sous-windows--démarrage-avec-la-session-et-mise-à-jour). Si l'application était fermée à l'heure d'un rappel, il est envoyé à la réouverture, sauf si la séance ou la tâche est déjà terminée (un rappel « à une date précise » part encore jusqu'à 24 h après son heure, même après l'échéance de la tâche). Une tâche répétée a un rappel à chaque occurrence, même si tu n'as pas coché la précédente.
- Si un e-mail ne part pas à cause d'une panne passagère (Wi-Fi pas encore revenu au réveil du portable, serveur injoignable), Hangar réessaie seul après 1, 5, 15 puis 60 minutes, jusqu'à la fin de la séance ou de la tâche pour un rappel, jusqu'à la fin de la journée pour le résumé (24 h pour les autres e-mails). Un mot de passe refusé ou une adresse rejetée ne sont pas réessayés. Les e-mails en échec s'affichent dans *Rappels et e-mails* et sur le tableau de bord, avec un bouton « Renvoyer les échecs ».

**Matières reliées au wiki et aux QCM** : dans la fenêtre d'une séance, relie la matière à sa collection du wiki et à sa catégorie de QCM (suggestions automatiques d'après le nom, le sigle ou l'intervenant). Pour un contrôle, « Préparer ce contrôle » fixe la date d'examen des QCM liés au jour du contrôle : les révisions s'organisent pour être prêt à temps.

## Wiki : les cours (coffre Obsidian)

Port complet du wiki de [Revision](https://github.com/wakadu13/Revision) : chaque fichier `.md` du coffre est une page.
- **Récupérer tes cours** : `uv run hangar wiki-import https://github.com/wakadu13/Revision` (ou le chemin d'un dossier Obsidian ; `--remplacer` pour écraser, `--images` pour enregistrer aussi les images hébergées sur d'autres sites). Les cours sont alors copiés dans `data/vault/`.
- **Ou travailler directement dans ton coffre Obsidian** (Hangar et Obsidian lisent les mêmes fichiers) : choisis-le une fois pour toutes dans *Réglages › Espace › Coffre du wiki* (sur l'ordinateur de Hangar), ou avec `uv run hangar wiki-coffre "C:\Users\moi\Documents\Obsidian\Cours"`. Le choix est gardé dans `data/coffre.txt` et vaut pour tous les lancements, `lancer.bat` compris ; `uv run hangar wiki-coffre` seul affiche le coffre utilisé, `--defaut` revient à `data/vault/`. Le chemin du coffre s'affiche au démarrage.
  - Si ce dossier est introuvable (clé ou disque débranché, OneDrive pas encore synchronisé, faute de frappe), le wiki le dit au lieu d'ouvrir un coffre vide, et Hangar ne crée rien à sa place : rebranche le disque puis recharge la page. Hangar n'écrit jamais ses pages de bienvenue dans un coffre que tu as choisi.
  - Pour un seul lancement, la variable `HANGAR_VAULT` passe avant ce choix. Sous Windows : `set HANGAR_VAULT=C:\chemin\du\coffre` puis `uv run hangar` dans l'invite de commandes (cmd), ou `$env:HANGAR_VAULT = "C:\chemin\du\coffre"` dans PowerShell ; sous Linux/macOS : `HANGAR_VAULT=/chemin/du/coffre uv run hangar`.
- **Corbeille et renommage** : « Mettre à la corbeille » range la page dans le dossier `.trash` du coffre, à son chemin d'origine ; la page *Corbeille* du menu du wiki la remet en place d'un clic. Une page supprimée hors du wiki (ou une corbeille vidée) garde son historique : la Corbeille le liste, et « Recréer la page avec cette version » la fait revenir. « Renommer / déplacer » (menu ⋯ d'une page) met à jour les liens `[[…]]` des autres pages (leur ancien texte reste dans l'historique), ainsi que l'historique, les favoris, les pages récentes, le tuteur et les QCM générés d'après la page. Un lien écrit avec un alias de la page reste tel quel : l'alias suit la page.
- Syntaxe Obsidian : liens `[[…]]` et alias, intégrations `![[…]]` (images, PDF, notes, sections), encadrés `> [!note]`, propriétés YAML, tags, formules `$…$` / `$$…$$`, diagrammes Mermaid, surlignage, notes de bas de page…
- **Images hébergées ailleurs** : le wiki ne charge jamais rien d'un autre site. Il affiche un lien « Image externe : légende ↗ » à la place de l'image. Le bouton « Enregistrer ces images dans le wiki » de la page Importer (ou `hangar wiki-import --images`) les télécharge une fois dans `image/` et met les pages à jour (`![[image.png]]`) : elles s'affichent alors aussi hors ligne.
- **Fichiers récalcitrants** : une note que Windows refuse d'ouvrir (antivirus, OneDrive « en ligne uniquement » sans réseau) ne bloque plus le wiki. Elle est signalée dans le sommaire et relue dès que possible. Une note écrite avec un vieux Bloc-notes (ANSI) s'ouvre normalement et passe au format habituel quand tu l'enregistres (l'original reste dans l'historique).
- Menu façon Plane : collections (les dossiers), favoris, pages récentes, sommaire, tags, graphe, recherche (pages **et texte des PDF**).
- Éditeur CodeMirror (aperçu en direct, côte à côte, Ctrl+S, collage d'images, liens suggérés, brouillon de secours), historique des versions avec différences et restauration (une version au début de chaque séance d'écriture puis toutes les 10 minutes ; avec le temps, une par heure, par jour, par semaine puis par mois ; la corbeille, les restaurations, les imports et l'IA gardent toujours leur version ; au plus 100 versions et 20 Mo par page), import de fichiers (.md, PDF, images, LaTeX), lecteur PDF avec annotations, conversion LaTeX → page.
- **Lecture** comme dans Revision : le menu se cache pendant la lecture (languette ›, bord de l'écran, ou bouton ▯ pour le garder), plein écran, impression propre (Ctrl+P), palette de commandes intégrée à Ctrl+K (pages, PDF, sets de QCM, sessions de langue, commandes).
- **Hors ligne et installable** : le wiki s'installe sur téléphone (« Ajouter à l'écran d'accueil »). Les pages lues restent lisibles sans réseau : un bandeau « Copie gardée sur cet appareil du … » te le signale. « ⤓ Hors ligne » sur un dossier du sommaire garde d'un coup ses pages, images et PDF, sans toucher à tes « Récents ». Le menu de gauche ne contient que la branche de la page ouverte, et les autres dossiers se chargent quand tu les ouvres : une page pèse environ 3 fois moins, un dossier gardé hors ligne aussi.
- Sous chaque page : « Réviser ce cours » (QCM écrits d'après la page, cartes, QCM et fiche par l'IA) et le **tuteur** (questions sur la page, « 📷 Corriger ma copie »). Le menu du wiki donne aussi accès à tout Revision : Aujourd'hui, QCM, Langues, Demander au wiki, Corriger ma copie, Transformer un cours, Travaux de l'IA, Documentation.
- Documentation intégrée (`/documentation/`), page des erreurs du serveur pour les administrateurs, anciennes adresses de Revision redirigées.

## QCM et révision espacée

Section séparée du wiki. Tout fichier `.json` du coffre contenant une clé `questions` est un set (catégorie = dossier, `QCM/` par défaut, comme dans Revision).
- Types QCM, QCR, texte à trous, réponse rédigée, flashcards, images masquées ; langues (vocabulaire, conjugaison, grammaire).
- Réponses tapées (textes à trous, réponses rédigées) : majuscules, accents, apostrophe et ligatures ne comptent pas (« noeud » est juste pour « nœud »), en ligne comme hors ligne.
- Algorithme **FSRS** : chaque carte revient juste avant d'être oubliée ; « Réviser tout ce qui est dû » ; quotas par set, **date d'examen** avec quotas ajustés automatiquement (« Ajuster auto. ») : si tu es en retard, plus de nouvelles questions par jour et des révisions sans limite ; si l'examen est trop proche pour tout savoir, les questions jamais vues sont réparties pour que tu les aies toutes vues avant la veille de l'examen.
- Le planning estime quand tu seras prêt pour ton examen sans ralentir Hangar, même quand tu rates encore beaucoup de questions : l'accueil, le calendrier et les pages QCM restent rapides quand une date d'examen est posée.
- **Ranger tes fichiers sans perdre ta progression** : la progression suit un set déplacé ou renommé, même dans Obsidian ou l'explorateur de fichiers (Hangar le reconnaît à son contenu, sans toucher au fichier). Si Hangar ne peut pas trancher (deux copies identiques, set très modifié), la page *Statistiques* liste les « progressions sans fichier », avec « Rattacher à… » et « Oublier ».
- Statistiques (calendrier d'activité, série de jours, temps par matière), espace personnel, import/export JSON, CSV et Anki (`.apkg`), signalements.
- Les cartes dues apparaissent sur le tableau de bord et dans le résumé quotidien. `uv run hangar qcm-verifier` contrôle les fichiers.
- **QCM hors ligne** (`/qcm/hors-ligne/`, installable) : télécharge des sets et des paquets de langue, révise sans réseau ; les réponses rejoignent ta progression au retour de la connexion, même si le set a été renommé ou déplacé entre-temps. Si son fichier est introuvable pour l'instant (en cours de modification, par exemple), elles restent sur l'appareil jusqu'à ce qu'il revienne.
- **Rappel quotidien par notification** du navigateur, à l'heure choisie (page « Aujourd'hui ») : ce qu'il reste à réviser et ta série de jours.
- **Appli Android de Revision** : Hangar répond aux mêmes adresses (connexion par jeton d'appareil, QCM hors ligne, rappel, widget, partage vers le wiki) ; il suffit d'indiquer l'adresse de Hangar dans les réglages de l'appli. Les appareils connectés se révoquent depuis *Réglages › Profil*.
- Connexion protégée : à partir de 3 essais restants, la page de connexion annonce combien il en reste ; après 5 mauvais mots de passe, la connexion est bloquée 30 minutes. Le message reste affiché sur la page et donne la façon de débloquer tout de suite : sur le PC qui fait tourner Hangar, `uv run hangar connexions-debloquer`, ou un administrateur depuis son profil (Connexions bloquées).

## Assistant IA (Claude ou IA open source)

Bouton **Assistant IA** en haut à droite (ou Ctrl+J) : un panneau comme « Plane AI », qui connaît la page ouverte (cours du wiki, ticket, tableau de bord, set de QCM, workgraph…).
- **Fournisseurs** (*Réglages › Fournisseurs d'IA*, préréglages en un clic) : **Claude** via l'API Anthropic (SDK officiel, `claude-opus-5-5` par défaut, modèle rapide pour les petites tâches), ou toute API **compatible OpenAI** : **Ollama** (`ollama pull qwen2.5`, adresse `http://localhost:11434/v1`), LM Studio, vLLM, llama.cpp, OpenRouter, Mistral, Groq, Together, ou une adresse personnalisée. Test de connexion, liste des modèles, plafond de dépense mensuel, suivi de l'usage.
- Réponses en direct, « @ » pour ajouter du contexte, « / » pour les commandes ; l'assistant consulte tickets, tâches, agenda et wiki, et **propose des actions** que tu valides d'un clic : créer la tâche, créer le ticket, ajouter à la page, ajouter au cycle, changer une échéance.
- Avant de valider une action, « Voir le contenu » montre le texte exact qui sera ajouté (bloc de page, description du ticket, notes de la tâche). L'assistant traite le contenu des pages, des PDF et des commentaires comme des données, jamais comme des consignes, et ses réponses ne chargent aucune image externe : une consigne cachée dans un document ne peut pas faire sortir tes données sans clic.
- Fonctions d'IA de Revision : expliquer une réponse de QCM, corriger une réponse rédigée, générer un QCM ou des fiches de vocabulaire depuis un cours, « Demander au wiki » (réponses avec sources), tuteur d'une page, améliorer la rédaction dans l'éditeur, transformer un PDF ou du LaTeX en page, corriger une copie photographiée.
- En ligne de commande : `hangar ia-fournisseurs`, `ia-ajouter ollama`, `ia-tester`, `ia-demander "…"`, `ia-usage`, `ia-travaux`.
- **Plafond mensuel** (*Réglages › Plafond et consignes*) : il compte aussi les **travaux en lot** dès leur dépôt (chaque lot réserve son coût au plus, remplacé par le coût réel à son retour) et les **réponses arrêtées en cours de route** (bouton Arrêter, coupure). Un nouveau travail est refusé quand le plafond est atteint. Un modèle Claude absent de la table des prix est compté au tarif le plus cher de sa famille, et les réglages affichent « Tarif inconnu » (y compris pour un service payant compatible OpenAI dont tu n'as pas saisi les prix).
- **Travaux** (*IA › Travaux*) : « Annuler » arrête un travail en attente, en cours ou dans un lot (le lot est aussi annulé chez Anthropic ; ce qui est déjà traité reste facturé). Un lot impossible à récupérer (clé changée, lot expiré, plus de 29 jours) passe en échec et se relance d'un clic. `hangar run` reprend les travaux interrompus dès le démarrage.
- **Pages longues** : l'IA reçoit les passages de la page qui parlent de ta question (ou un aperçu de toute la page pour « Interroge-moi » et « Résumer »), et sait qu'elle n'a que des extraits. La pastille du panneau affiche « page coupée ». Claude lit jusqu'à 80 000 caractères de la page ouverte, un modèle local 12 000.
- **Réponses coupées** : une réponse arrêtée par la limite de longueur porte la mention « Réponse coupée ». Une explication de QCM coupée n'est pas gardée, et un administrateur peut refaire une explication gardée (« ↻ Refaire l'explication »).
- **Ollama et mémoire** : par défaut, Ollama ne garde que 4 096 jetons et oublie le début d'une demande plus longue, sans prévenir. Hangar t'avertit quand c'est le cas. Pour lui donner plus de mémoire sous Windows : dans l'application Ollama, *Settings › Context length* ; ou crée la variable d'environnement utilisateur `OLLAMA_CONTEXT_LENGTH` = `16384` (menu Démarrer › « Modifier les variables d'environnement pour votre compte »), puis quitte Ollama (icône près de l'horloge › *Quit Ollama*) et relance-le.
- **Clé d'API liée à l'adresse** : si tu changes l'adresse d'un fournisseur sans retaper sa clé, la clé enregistrée est effacée et n'est jamais envoyée au nouveau serveur. Une redirection vers un autre serveur est refusée.

## Travail : initiatives et Workgraph

En plus des projets, tickets, cycles et modules :
- **Initiatives** : objectifs qui regroupent des projets et des modules, avec avancement, **santé** calculée (dans les temps, à risque, en retard) et notes d'avancement datées.
- **Workgraph** : graphe Espace → Initiatives → Projets → Modules, avec les flèches de dépendances entre tickets (bloque / bloqué par) ; vue Tableau ; regroupements par initiative, projet ou équipe.
- **Projets privés respectés** : sur la page d'une initiative, dans le Workgraph et dans le texte envoyé à l'assistant IA, tu ne vois que les jalons, modules et dépendances des projets auxquels tu as accès. L'avancement et la santé d'une initiative comptent quand même tous ses projets ; la page indique combien de projets privés liés ne sont pas affichés.

## Démarrage rapide

Prérequis : [uv](https://docs.astral.sh/uv/) (il installe Python et les dépendances tout seul) et, pour récupérer Hangar et le mettre à jour, [Git](https://git-scm.com/download/win).

```bash
git clone https://github.com/wakadu13/GestionProjet
cd GestionProjet
uv run hangar
```

Sous Windows, il suffit ensuite de **double-cliquer sur `lancer.bat`** dans le dossier `GestionProjet` (Linux/macOS : `./lancer.sh`). Le navigateur s'ouvre sur <http://127.0.0.1:5000>. Garde la fenêtre ouverte tant que tu utilises Hangar (ou fais-le démarrer avec Windows, voir plus bas).

- **Première ouverture sur une base vide** : un écran d'installation crée l'espace de travail et le compte administrateur.
- **Données du projet EAE** : `uv run hangar demo` crée l'équipe (9 membres), le projet « Optimisation des procédures d'intégration EAE », 7 sprints de deux semaines et **123 tickets rédigés d'après les documents du projet** (fiche de cadrage, comptes rendus, matrices RGPD, benchmark, plan d'actions, mails prêts à envoyer, démonstrateur…) et 5 pages de comptes rendus. Les documents eux-mêmes ne sont pas dans le dépôt : pour les joindre aux tickets (et avoir les 12 comptes rendus), copie le contenu du dossier « Projet Pluri » (« Avancement projet », « fichier généré »…) dans le dossier qui contient `GestionProjet`, puis lance `uv run hangar rebuild-docs`. La commande `demo` affiche le chemin exact où elle les cherche. Le planning Excel prévisionnel, hors du dépôt lui aussi, reste importable : `uv run hangar demo --planning "chemin\vers\Plan_taches_gouvernance_donnees_v5.xlsx"`.

Connexion par **identifiant et mot de passe** (pas d'e-mail) ; l'écran de connexion affiche les membres : clique sur ton nom puis tape le mot de passe.

`uv run hangar demo` tire au hasard un **mot de passe provisoire**, commun aux 9 comptes, et l'affiche **une seule fois** dans le terminal : note-le. Chaque compte, administrateur compris, doit le changer à sa première connexion.

| Identifiant | Rôle |
|---|---|
| `wacogne` | Administrateur |
| `lazaro`, `brancourt`, `gali`, `claeys`, `niord`, `flohic`, `loubatier`, `alquarni` | Membres |

> Base créée par une ancienne version : l'ancien mot de passe de démonstration, qui était public, doit être changé à la prochaine connexion, et `uv run hangar run --host 0.0.0.0` avertit au démarrage tant qu'un compte l'utilise encore.

> Prénoms retrouvés dans les documents : Raphaël Lazaro, Mathieu Brancourt, Vincent Flohic, Benoit Loubatier. Les autres se complètent depuis **Équipe** ou **Réglages › Profil**.

## Sous Windows : démarrage avec la session et mise à jour

Les rappels, le résumé du matin et les notifications ne partent que si Hangar tourne. Pour ne plus y penser, dans le dossier `GestionProjet\scripts` :

- **`demarrer-avec-windows.bat`** (double-clic) : Hangar démarre tout seul, **sans fenêtre**, à chaque ouverture de ta session, et tout de suite. Pour l'ouvrir, double-clique sur `lancer.bat` (il voit que Hangar tourne et ouvre seulement le navigateur) ou va sur <http://127.0.0.1:5000>. Aucun droit d'administrateur n'est demandé : c'est un raccourci dans le dossier *Démarrage* de ta session.
- **`ne-plus-demarrer-avec-windows.bat`** : annule, et arrête le Hangar qui tourne en arrière-plan.
- **`mettre-a-jour.bat`** : fait une sauvegarde complète (dans `data\backups`, les 5 dernières sont gardées), récupère la nouvelle version (`git pull`), installe ce qu'il faut, puis relance Hangar. Il s'arrête sans rien changer si quelque chose cloche (pas d'Internet, fichiers de Hangar modifiés à la main, sauvegarde impossible).

Sans fenêtre, les messages d'erreur vont dans `data\journal\` : `hangar.log` (erreurs et avertissements) et `console.log` (ce qu'afficherait la fenêtre). Les erreurs des pages et des envois automatiques sont aussi listées pour les administrateurs sur la page des erreurs du serveur (`/erreurs/`).

**Version qui tourne** : *Réglages › À propos* affiche la version lancée. Si tu as mis Hangar à jour sans le relancer, cette page et `lancer.bat` te préviennent que l'ancienne version tourne encore : ferme sa fenêtre (ou redémarre l'ordinateur s'il démarre avec ta session), puis relance-le.

## Travailler à plusieurs (réseau local)

```bash
uv run hangar run --host 0.0.0.0
```

L'adresse à partager (ex. `http://192.168.1.20:5000`) s'affiche dans le terminal. Chaque membre se connecte avec son compte depuis son navigateur. Le serveur (waitress, 8 fils) gère sans souci une équipe de cette taille. Avant de partager l'adresse, vérifie que chaque compte a choisi son propre mot de passe : au démarrage en réseau, Hangar avertit si un compte a encore l'ancien mot de passe de démonstration.

**Modifier la même chose en même temps.** Si quelqu'un modifie une page ou la description d'un ticket pendant que tu l'écris (un coéquipier, ton autre onglet ou l'assistant IA), Hangar n'écrase rien. Ton enregistrement est refusé avec un message qui dit qui l'a modifiée. Ton texte reste dans l'éditeur et la version actuelle s'affiche à côté : reprends ce qui manque, puis enregistre à nouveau. Le texte remplacé reste toujours dans l'historique : onglet *Historique* d'une page, ou « Voir le texte d'avant » / « Restaurer ce texte » dans l'historique d'un ticket.

**Garde-fous pour l'usage à plusieurs.** Les couleurs (états, étiquettes, projets, avatars, pense-bêtes) ne sont acceptées qu'au format `#rrggbb`. Une pièce jointe de ticket ne s'ouvre dans le navigateur que si c'est une image PNG, JPEG, GIF, WebP ou un PDF (d'après son extension) : tout le reste, SVG compris, est téléchargé et ne peut rien exécuter. Hangar n'affiche aucune image venue d'Internet : dans un ticket, un commentaire, une page ou une réponse de l'IA, `![texte](https://…)` devient un simple lien. La déconnexion se fait avec le bouton du menu (l'ancienne adresse `/deconnexion/` demande une confirmation), et « Télécharger la base actuelle » est un bouton : une image piégée ne peut ni te déconnecter ni créer de copie de la base.

Pour un navigateur d'une autre machine (téléphone, autre PC), Hangar compresse ses pages. Les fichiers de l'application (styles, scripts, icônes) restent dans le navigateur jusqu'à leur prochaine modification, même après un `git pull`. `HANGAR_COMPRESSION=jamais` (ou `toujours`) change ce réglage.

**Appareil partagé** : « Se déconnecter » (menu de l'espace) propose « Effacer aussi les pages gardées sur cet appareil ». La case est cochée d'office, sauf sur la machine qui fait tourner Hangar. Elle efface les pages du wiki lues ou gardées hors ligne, les QCM téléchargés et les brouillons de l'éditeur. S'il reste des réponses de QCM pas encore envoyées, Hangar essaie de les envoyer, et te prévient sinon. Quand un autre compte se connecte sur le même navigateur, les pages du wiki gardées pour le compte précédent sont effacées.

**Sécurité des pages** : chaque réponse porte les en-têtes nosniff, X-Frame-Options et Referrer-Policy, et une politique de contenu (CSP) qui n'exécute que les scripts de Hangar, ne charge que ses images et ne se connecte qu'à Hangar. En cas de souci avec une page : `HANGAR_CSP=rapport` (les blocages sont seulement signalés dans la console du navigateur) ou `HANGAR_CSP=non`.

## Fonctionnalités

**Tickets (work items)**
- États personnalisables par groupe (Backlog, Non commencé, En cours, Terminé, Annulé), priorités, assignés multiples, étiquettes (avec parent), dates de début et d'échéance, estimations (points, Fibonacci, tailles, heures, jours-homme), cycle, modules.
- Sous-tickets avec progression, relations *bloque / bloqué par / lié à / doublon de*, liens, pièces jointes (glisser-déposer), duplication, déplacement vers un autre projet, archivage.
- Commentaires markdown avec réactions, **@mentions** et liens automatiques vers les tickets (`EAE-12`), historique complet de chaque modification (l'ancienne description est gardée et peut être restaurée), suivi du temps passé.

**Vues et organisation**
- 5 dispositions : **liste**, **tableau kanban** (glisser-déposer entre colonnes), **tableur** (modifications en masse), **calendrier** (glisser une échéance), **Gantt** (déplacer / redimensionner les barres).
- Filtres (état, priorité, assigné, étiquette, cycle, module, dates, créateur, abonnement, texte), regroupement, tri, propriétés affichées, **vues enregistrées** partagées, favoris.
- Modification en ligne de toutes les propriétés, ajout rapide dans chaque groupe.

**Pilotage**
- **Cycles** (sprints) : avancement, burndown, répartition par assigné / étiquette, transfert des tickets non terminés.
- **Modules** (phases, lots) avec statut, responsable, membres, dates, progression.
- **Intake** : boîte de réception des demandes (accepter, refuser, reporter, marquer comme doublon).
- **Pages** : wiki markdown avec modèles (compte rendu de réunion, d'entretien, note de décision, rétrospective, spécification), sommaire, historique des versions avec différences, verrouillage, pages privées, sauvegarde automatique, **import de documents Word (.docx)**.
- **Analytique** : indicateurs, répartition par état et priorité, tendance créés/terminés, analyse personnalisée (axe X × segment × métrique, export CSV), statistiques par membre.

**Planning (Gantt)**
- Page **Planning** par projet et pour tout l'espace : couloirs par module (ou par assigné / cycle), barre de synthèse avec avancement, bandes des sprints, ligne « aujourd'hui », jalons en losanges, **flèches de dépendances** (en rouge quand les dates se contredisent).
- Zoom jour / semaine / mois / trimestre, glisser pour déplacer, tirer les bords pour changer les dates, filtres, impression / PDF.
- Onglet **Tendances** de l'analytique : flux cumulé (CFD), burn-up, vélocité par cycle, délais de réalisation, âge des tickets ouverts, date de fin estimée.

**Bilan**
- Bilan d'avancement par projet ou pour tout l'espace, sur la semaine, le mois, depuis le début ou une période libre : météo du projet (temps écoulé vs travail terminé), indicateurs, points d'attention générés automatiquement, réalisations par module, en cours, retards, blocages, jalons, avancement des modules et des cycles, contributions de chaque membre, documents produits, prochaines échéances.
- **Bilans enregistrés** (chiffres figés) avec sections rédigées : faits marquants, difficultés, décisions, prochaines étapes. Un bilan enregistré n'est visible que par les personnes qui ont accès à tous les projets qu'il couvre ; sinon il reste réservé à son auteur et aux administrateurs. « Actualiser » recalcule le bilan sur ses projets d'origine.
- Export **Word (.docx)**, Markdown, impression / PDF, et **envoi par e-mail**.

**Envoi des e-mails** (*Réglages › Envoi des e-mails*, aussi dans le menu de l'espace)
- Notifications instantanées (assignation, mention, commentaire…), rappel quotidien des échéances, récapitulatif hebdomadaire, bilan hebdomadaire envoyé aux responsables, e-mails manuels avec modèles (relance, convocation, compte rendu, demande de documents).
- Mode **boîte d'envoi locale** par défaut : rien ne sort de la machine, les messages sont enregistrés en `.eml` dans `data/outbox/` (ouvrables avec Thunderbird/Outlook). Mode **SMTP** pour un envoi réel (préréglages Gmail avec mot de passe d'application, Outlook, Orange, Free).
- En cas de panne passagère, nouvel essai automatique (1, 5, 15 puis 60 min) jusqu'à la date limite du message ; bouton **« Renvoyer les échecs »** après avoir corrigé les réglages (par exemple un mot de passe d'application). Les incidents du fil d'arrière-plan sont notés sur la page des erreurs du serveur (`/erreurs/`).
- Le bilan hebdomadaire envoyé automatiquement (ou par `hangar mail-send`) est le bilan complet, le même que le bouton « Lancer ». Si son calcul échoue, un résumé simplifié part quand même et l'erreur est notée sur `/erreurs/`.
- Chaque membre renseigne son adresse de notification et ses préférences dans **Réglages › Profil** (l'adresse ne sert pas à la connexion).

**Équipe**
- Rôles d'espace (administrateur, membre, invité) et de projet, invitations avec mot de passe provisoire, désactivation, réinitialisation.
- Profils (statistiques, carte d'activité, tickets, temps passé), **charge de travail hebdomadaire**, capacité, **absences**.
- Sur le profil d'un membre, l'onglet Temps n'affiche que le temps saisi sur des projets que tu peux voir.
- Notifications (assignation, mention, commentaire, changement d'état), mise en sommeil, archivage.

**Confort**
- Palette de commandes `Ctrl+K`, qui trouve aussi les réglages (« réglages », « paramètres », « sauvegarde », « e-mails »…). Raccourcis clavier : `C` créer, `/` rechercher, `G` puis `H/I/P/N/A/T`, `?` aide, `Échap` ferme un menu, le menu latéral ou le panneau IA. Après un changement fait au clavier, le focus reste sur la case modifiée, même après le rechargement de la liste. Lien « Aller au contenu » au premier `Tab`. Contrastes conformes à WCAG AA (4,5:1) en thème clair et sombre. Thème clair / sombre, pense-bêtes sur l'accueil, interface responsive.
- Un seul menu **Réglages** (icône en bas de la barre de gauche), avec les mêmes noms dans le menu de l'espace et dans la palette. « → Agenda », « → IA » et « → Travail » signalent les réglages rangés dans une autre section (Rappels et e-mails, Fournisseurs d'IA, Membres).
- **Recherche à la française**, partout (palette `Ctrl+K`, filtres des listes, liaison de tickets, pages, wiki, QCM, assistant IA, « @ ») : majuscules, accents, forme de l'apostrophe (’ ou ') et ligatures ne comptent pas — « etablir » trouve « Établir le plan d’actions », « noeud » trouve « Nœud ». « % » et « _ » sont cherchés tels quels. Les listes sont triées à la française : « anglais, Droit, École, Tactique, Zèbre », « Linux 2 » avant « Linux 10 ».

**Données**
- Import Excel / CSV / JSON (détection automatique des colonnes : Titre/Tâche, Statut, Priorité, Responsable, Appui, Début, Échéance, Phase, Partie, Dépend de…), export Excel (avec feuille de synthèse), CSV, JSON, Markdown, calendrier iCalendar (.ics). Un fichier exporté par Hangar se réimporte sans perte, dans le même projet ou dans un autre : étiquettes, modules (un nom qui contient une virgule est écrit entre guillemets), parent et « bloqué par » visent les tickets du fichier. L'option « Mettre à jour les tickets existants » est décochée par défaut. Cochée, elle ne change que les cellules remplies (une colonne absente ou vide garde la valeur du ticket), ne touche jamais aux tickets d'un autre projet (une clé `EAE-…` importée dans `MEM` devient un nouveau ticket) et fait une sauvegarde de la base juste avant.
- **Sauvegarde complète (.zip)** en un clic ou en ligne de commande (base, wiki, sets de QCM, copies, pièces jointes, agenda), sauvegardes de la base restaurables en un clic, export JSON des projets (Travail).
- **API REST** `/api/v1` avec jetons personnels (voir *Réglages › Jetons d'API*, ou *Jetons d'API* dans le menu de l'espace). Pour modifier une description sans écraser celle d'un autre, renvoie `description_version` (lu dans le ticket) avec la nouvelle `description` : la réponse est 409 si elle a changé entre-temps.

## Commandes

```bash
uv run hangar                       # lance le serveur (http://127.0.0.1:5000)
uv run hangar run --port 8080       # autre port ; --host 0.0.0.0 pour le réseau local ; --no-browser
uv run hangar demo                  # données de démonstration du projet EAE (affiche une fois le mot de passe provisoire)
uv run hangar demo --force          # remplace la base par la démonstration (demande « oui », sauvegarde avant ; wiki gardé ; --yes)
uv run hangar rebuild-docs          # recrée les tickets du projet EAE d'après les documents (sauvegarde avant)
uv run hangar agenda-import planning.ics  # importe l'emploi du temps (.ics) ; --user identifiant, --mode weeks|future|merge
uv run hangar mail-send             # envoie la file d'e-mails, les rappels de l'agenda et les envois programmés maintenant
uv run hangar wiki-import [<dossier|url git>] [--remplacer] [--images]   # importe un coffre Obsidian (cours) dans le wiki ; --images enregistre aussi les images hébergées ailleurs
uv run hangar wiki-coffre "C:\chemin\du\coffre"   # utilise ce coffre Obsidian à chaque lancement (sans dossier : affiche le coffre ; --defaut : data/vault)
uv run hangar wiki-indexer-pdf      # lit le texte de tous les PDF du wiki (recherche)
uv run hangar qcm-verifier          # vérifie les fichiers de QCM et de langue
uv run hangar qcm-rappels           # envoie maintenant les rappels de révision dus (notifications)
uv run hangar connexions-debloquer  # lève les blocages de connexion
uv run hangar ia-fournisseurs       # liste les fournisseurs d'IA configurés (celui utilisé par défaut)
uv run hangar ia-ajouter ollama     # ajoute un fournisseur d'IA (anthropic, ollama, lmstudio, vllm, openrouter, mistral…)
uv run hangar ia-tester             # teste la connexion aux fournisseurs d'IA
uv run hangar ia-demander "…"       # pose une question au modèle configuré
uv run hangar ia-usage              # dépense et jetons de l'IA ce mois-ci
uv run hangar ia-travaux            # fait les travaux d'IA en attente et récupère les lots terminés
uv run hangar mail-test adresse@ex.fr  # e-mail de test
uv run hangar backup                # sauvegarde de la base seule dans data/backups/
uv run hangar backup --complet --vers E:\Sauvegardes --garder 10   # sauvegarde complète (.zip) ailleurs, garde les 10 dernières ; --sans-secrets
uv run hangar restore copie.db      # remplace la base par une sauvegarde .db (vérifiée, état actuel sauvegardé avant ; Hangar arrêté)
uv run hangar reset                 # efface la base (sauvegarde avant, pièces jointes mises de côté dans data/backups ; wiki gardé)
uv run hangar create-admin          # crée ou répare un compte administrateur
uv run hangar --data /chemin run    # utiliser un autre dossier de données
uv run python -m unittest discover -s tests   # tests (lancés aussi par GitHub Actions sous Linux et Windows)
```

## Où sont les données ?

Tout est dans le dossier **`data/` à la racine du dépôt** (`GestionProjet\data\`, à côté de `lancer.bat`) ; le chemin exact s'affiche au lancement (« → Données : … ») et dans *Réglages › Espace*. Le dossier `GestionProjet\hangar\` ne contient que le code.

- `hangar.db` : la base SQLite (projets, tickets, pages, comptes…), avec ses fichiers `hangar.db-wal` et `hangar.db-shm` pendant que Hangar tourne
- `uploads/` : les pièces jointes
- `backups/` : les sauvegardes
- `outbox/` : les e-mails en mode « boîte d'envoi locale »
- `agenda/` : les fichiers `.ics` importés (un dossier par utilisateur)
- `vault/` : le coffre du wiki (pages `.md`, images, PDF, sets de QCM `.json`, corbeille `.trash/`) — ou le dossier choisi avec `hangar wiki-coffre` (retenu dans `coffre.txt`) ou indiqué par `HANGAR_VAULT`
- `wiki_versions/` : l'historique des pages du wiki (un dossier par page ; `<date>-<auteur>.md` = version gardée avant une restauration, une mise à la corbeille, un import ou l'IA, ainsi que toutes les versions d'avant cette mise à jour ; `<date>_<auteur>.md` = version d'écriture, éclaircie avec le temps)
- `qcm_perso/` : les sets de l'espace personnel de chaque compte
- `copies/` : les photos des copies corrigées par l'IA
- `secret.key` : la clé de session (générée automatiquement)
- `vapid_prive.pem` : la clé des notifications (sans elle, chaque appareil doit se réabonner)
- `journal/` : le journal des erreurs (`hangar.log`) et, au démarrage sans fenêtre, ce qu'afficherait la console
- `serveur.json` : la version lancée (pour prévenir quand une mise à jour attend une relance)

Pour transférer Hangar sur une autre machine : fais une sauvegarde complète en cochant « Inclure le mot de passe des e-mails et les clés d'API », installe Hangar sur la nouvelle machine, puis restaure-la (voir ci-dessous). Tu peux aussi, **Hangar arrêté** (fenêtre fermée), copier tout le dossier `GestionProjet`, ou au moins son dossier `data/`, et le coffre s'il est ailleurs. Ne copie jamais `hangar.db` seul pendant que Hangar tourne : les derniers changements sont encore dans `hangar.db-wal`. Pour une copie de la base à chaud, passe par *Réglages › Sauvegardes* ou `uv run hangar backup`.

### Sauvegarder et restaurer

- **Sauvegarde complète (.zip)** (Réglages › Sauvegardes › « Tout télécharger (.zip) » ou « Créer une sauvegarde complète », ou `uv run hangar backup --complet`). Elle contient la base, le coffre du wiki (même s'il est ailleurs avec `HANGAR_VAULT`), `wiki_versions/`, `qcm_perso/`, `copies/`, `uploads/`, `agenda/` et `vapid_prive.pem`. Elle ne contient pas `backups/`, `outbox/` ni `secret.key` (la perdre oblige seulement à se reconnecter). Un fichier `LISEZMOI.txt` dans l'archive en décrit le contenu.
- **Sauvegarde de la base (.db)** : la base seule. Elle se restaure en un clic sur la page, mais sans le wiki, les sets de QCM ni les fichiers. Avant de remplacer ta base, Hangar vérifie que le fichier est une base Hangar intacte et sauvegarde l'état actuel. Une sauvegarde faite avec une version plus ancienne de Hangar est mise à jour aussitôt : pas besoin de redémarrer. Tu es ensuite déconnecté (les comptes sont ceux de la sauvegarde). Pour une copie venue d'un autre PC, dépose le fichier `.db` dans `data/backups/` (il apparaît dans la liste) ou utilise `uv run hangar restore copie.db`, Hangar arrêté.
- **Secrets** : la base contient en clair le mot de passe SMTP et les clés d'API de l'IA. Les téléchargements depuis la page les retirent par défaut ; coche la case pour les garder si tu changes de machine. Les copies gardées dans `data/backups/` et celles de `hangar backup` les contiennent (option `--sans-secrets`). Pour ne pas enregistrer le mot de passe SMTP dans la base, laisse le champ vide et définis la variable d'environnement `HANGAR_SMTP_PASSWORD`, comme `ANTHROPIC_API_KEY` pour l'IA.
- **Sauvegarde automatique sous Windows** : Planificateur de tâches › Créer une tâche de base › Quotidienne › Démarrer un programme. Programme : `uv` (ou son chemin complet, ex. `C:\Users\<toi>\.local\bin\uv.exe`). Arguments : `run hangar backup --complet --vers "D:\OneDrive\Hangar" --garder 14`. Commencer dans : le dossier `GestionProjet`. Le code de sortie vaut 1 si la sauvegarde a échoué (clé débranchée, disque plein) et 2 si des fichiers verrouillés manquent dans l'archive.
- **Restaurer une sauvegarde complète** : arrête Hangar. Décompresse le .zip dans `data/` en remplaçant les fichiers existants ; supprime d'abord `hangar.db-wal` et `hangar.db-shm` s'ils existent. Si `HANGAR_VAULT` pointe ailleurs, copie le contenu de `vault/` dans ton coffre. Relance Hangar. Si la copie était sans secrets, ressaisis le mot de passe SMTP et les clés d'API.
- **Export JSON des projets (Travail)** : fichier lisible avec les projets, les tickets (assignés, étiquettes et modules compris), les pages et les commentaires, que l'import JSON d'un projet sait relire. Ce n'est pas une sauvegarde : ni agenda, ni QCM, ni wiki.
- `demo --force` et `reset` sauvegardent la base avant d'effacer et mettent `uploads/`, `qcm_perso/`, `copies/` et `agenda/` de côté dans `data/backups/hangar_<date>_fichiers/`. Pour revenir en arrière, restaure la sauvegarde puis remets ces dossiers dans `data/`. Le coffre du wiki et son historique ne sont jamais effacés.

## Structure du code

```
GestionProjet/
├── pyproject.toml          dépendances (uv)
├── lancer.bat, lancer.sh   lancement en double-clic (Windows) ou en terminal (Linux/macOS)
├── scripts/                Windows : démarrage avec la session, arrêt de ce démarrage, mise à jour (.bat + .ps1)
├── hangar/                 le code (aucune donnée ici)
│   ├── __init__.py         application Flask, sécurité (CSRF), mise à niveau de la base, aides de gabarits
│   ├── web.py              en-têtes de sécurité (CSP), cache des fichiers statiques, feuille d'icônes, compression
│   ├── cli.py              ligne de commande
│   ├── lancement.py        version lancée, journal des erreurs (data/journal), mise à jour en attente
│   ├── models.py           modèle de données (SQLAlchemy)
│   ├── services.py         logique métier : tickets, historique, notifications
│   ├── issue_query.py      filtres, tris, regroupements, calendrier, Gantt
│   ├── texte.py            recherche et tri à la française (accents, apostrophes, ligatures ; « École » avant « Zèbre »)
│   ├── ics.py              lecture des fichiers .ics (format de l'école et iCalendar standard)
│   ├── agenda.py           agenda : imports (le dernier fait foi), tâches, rappels, vues semaine / mois
│   ├── digest.py           résumé quotidien par e-mail (chaque module y ajoute sa section)
│   ├── mailer.py           e-mails : file d'envoi, SMTP ou .eml, envois planifiés, fil d'arrière-plan
│   ├── bilan.py            bilans d'avancement (calcul, Markdown, Word)
│   ├── workgraph.py        initiatives : avancement, santé, graphe
│   ├── wiki/               wiki (port de Revision : vault, render, latex, historique, corbeille, texte_pdf…)
│   ├── qcm/                QCM et révision espacée (port de Revision : quiz, fsrs, langues, planning…)
│   ├── ai/                 assistant IA : fournisseurs (Claude, compatibles OpenAI), panneau, outils, fonctions
│   ├── appareils.py        jetons d'appareil (appli Android)
│   ├── blocage_connexion.py  blocage après des mots de passe faux
│   ├── importer.py         import/export Excel, CSV, JSON, Word
│   ├── charts.py           graphiques SVG générés en Python
│   ├── securite.py         garde-fous communs : couleurs, adresses locales, fichiers servis, images externes
│   ├── sauvegarde.py       sauvegardes complètes (.zip), copies sans secrets, vérification avant restauration
│   ├── seed.py, seed_documents.py, seed_data/   données de démonstration du projet EAE
│   ├── views/              routes (une par domaine : tickets, cycles, pages, équipe, API…)
│   ├── templates/          gabarits Jinja
│   └── static/             CSS et JavaScript ; bibliothèques embarquées (KaTeX, Mermaid, highlight.js,
│                           CodeMirror, pdf.js) dans static/vendor et static/wiki/vendor : aucun CDN
├── tests/                  tests unittest de toutes les sections (uv run python -m unittest discover -s tests)
└── data/                   tes données : base, coffre, sauvegardes… (créé au premier lancement)
```

Pour les développeurs : dans les gabarits, écris `url_for('static', filename=…)` sans `?v=` : l'empreinte du fichier est ajoutée toute seule. Un bloc `<script>` écrit dans un gabarit porte `nonce="{{ csp_nonce() }}"`. Pas d'attribut `onclick=` ni de lien `javascript:` : utilise `data-imprimer`, `data-retour`, `data-confirm` ou `data-confirm-msg`.
