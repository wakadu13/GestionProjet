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

Il affiche aussi « Mes tickets » : tes tickets de projet en retard ou à rendre dans les 7 jours. L'icône « Travail » porte une pastille pour les tickets en retard ou à rendre aujourd'hui.

L'agenda montre aussi le travail d'équipe : les échéances de tes tickets ouverts et la fin des sprints de tes projets s'affichent dans le bandeau « journée » de la semaine, dans les vues mois et liste et dans « Aujourd'hui ». Le « + » d'un ticket (ou « Bloquer un créneau » sur sa fiche) crée une tâche avec le titre et le lien du ticket.

**Importer l'emploi du temps chaque semaine** (menu *Emploi du temps*, ou le bandeau du tableau de bord)
- Dépose le fichier `.ics` exporté de ton planning. **Le dernier fichier importé fait foi** :
  - chaque séance est reconnue par son identifiant (`UID`) : si l'horaire, la salle ou l'intervenant changent, elle est **mise à jour** et marquée « modifiée » (avant → après) ;
  - une séance absente du nouveau fichier est **retirée** (barrée dans le calendrier), seulement pour les semaines couvertes par le fichier et à partir d'aujourd'hui : le passé n'est jamais effacé. Une semaine n'est couverte que si le fichier y apporte au moins une vraie séance d'une journée : un bloc de plusieurs jours (formation militaire…) ou une séance `(PREVISION)` qui déborde sur une autre semaine n'y retire rien ;
  - garde-fou : si une semaine voisine perdait la majorité de ses séances alors que le fichier n'y en apporte qu'une ou deux, rien n'y est retiré et la page de l'import l'explique. Une alerte s'affiche aussi quand des séances d'aujourd'hui ou de demain sont retirées ;
  - tes **notes**, **rappels** et **tâches liées** restent attachés à la séance d'un import à l'autre ;
  - chaque import a sa page de détail (ajouts, modifications, retraits) et le **dernier import peut être annulé** (les rappels des séances, y compris celui des contrôles, reviennent aussi). Le fichier d'origine est conservé.
- Format de l'école reconnu : `Réseaux avancés 3 CM` → matière, n° de séance, type (CM, TD/TP, contrôle écrit, sport, militaire, conférence…) ; `(PREVISION)` → séance prévisionnelle (hachurée) ; rubriques `Apprenants` et `Intervenants` de la description. Les autres fichiers `.ics` (Google Agenda, Outlook…) fonctionnent aussi.
- En ligne de commande : `uv run hangar agenda-import planning.ics` (option `--mode future` pour remplacer tout le planning à venir, `--mode merge` pour ne rien retirer).
- **Récupération automatique** (administrateur, *Emploi du temps › Récupérer le planning tout seul*) : Hangar surveille un dossier (par exemple Téléchargements ; le fichier `.ics` le plus récent compte) ou lit l'adresse d'abonnement `.ics` de l'école toutes les 3 heures, tant qu'il est lancé. Chaque nouveau fichier est importé comme un dépôt à la main (le dernier fait foi), avec une notification « Emploi du temps mis à jour : … ». Un planning identique n'est pas réimporté. Une invitation à une réunion (`METHOD:REQUEST` avec une ou deux séances) ou un `.ics` qui ne ressemble pas à ton emploi du temps est laissé de côté, pour ne rien retirer par erreur. Le bandeau « Nouveau planning ? » ne revient que si la source est muette depuis une semaine.
- Une invitation importée à la main est seulement ajoutée : elle ne retire jamais les autres séances de sa semaine.

**Calendrier** : vues semaine (grille horaire, ligne « maintenant »), mois et liste ; clic sur une séance pour ses détails, une note personnelle, un rappel ou une tâche liée ; clic sur un créneau libre pour créer une tâche à cette heure ; export `.ics` (cours, tâches et tickets, avec les rappels) vers Google Agenda / Outlook / téléphone ; impression. **S'abonner depuis mon téléphone** (*Emploi du temps*) : une adresse secrète, en lecture seule, à coller une fois dans l'agenda du téléphone (iPhone : Réglages › Calendrier › Comptes › Ajouter un calendrier avec abonnement ; Android : appli ICSx⁵) ou dans Outlook. Cours, contrôles, tâches et tickets s'y mettent à jour tout seuls. « Nouvelle adresse » rend l'ancienne inutilisable. Le téléphone doit pouvoir joindre le PC : même Wi-Fi, Hangar lancé avec `uv run hangar run --host 0.0.0.0` (ou en ligne). La page te prévient si ce n'est pas le cas.

Les fichiers `.ics` exportés (agenda et « Calendrier (.ics) » des tickets) respectent la norme iCalendar (RFC 5545 : fuseau Europe/Paris décrit, textes échappés, lignes repliées). Les rappels d'une tâche sans heure sonnent à l'heure de référence (8 h par défaut), comme dans Hangar.

**Tâches** : date, heure de début et de fin (ou journée entière), notes, priorité, catégorie, couleur, séance liée, **répétition** (tous les jours, du lundi au vendredi, chaque semaine, toutes les deux semaines, chaque mois, le même jour : une tâche du 31 revient le 31 quand le mois le permet — cocher l'occurrence passe à la suivante, avec un bouton « Annuler » en cas d'erreur), report rapide (demain, lundi, +1 semaine). Raccourci clavier `T`.

**Rappels et e-mails** (menu *Rappels et e-mails*)
- Rappels par tâche ou par séance : à l'heure, 15 min, 1 h, la veille, 2 jours, 1 semaine avant… ou à une date précise ; par **e-mail + notification** dans l'application, notification dans l'application seule, ou **notification sur mes appareils** : notification du système, même si Hangar est en arrière-plan. Pour cela, active l'appareil dans *Rappels et e-mails › Notifications sur cet appareil*. Ça marche tout de suite sur le PC où tourne Hangar ; sur un téléphone, il faut HTTPS (voir « Ouvrir Hangar sur ton téléphone »). C'est indépendant du rappel quotidien des QCM : couper l'un ne coupe pas l'autre.
- **Résumé quotidien** par e-mail (activé par défaut à 7 h) : un seul message avec tout ce qu'il y a à faire — cours du jour (horaires, salles), tâches du jour et en retard, tickets de projet à échéance, cartes de QCM à réviser et examens proches, contrôles de la semaine, changements récents du planning, initiatives à risque. Le matin pour la journée, ou la veille au soir pour le lendemain ; rien n'est envoyé les jours vides ; aperçu dans *Agenda › Résumé quotidien*.
  - Si Hangar était fermé à l'heure prévue : le résumé du matin part à la première ouverture avant 18 h, puis plus du tout (pas de programme d'une journée finie). En mode soir, si le résumé n'a pas pu partir la veille, celui du jour part à la première ouverture du lendemain, avant 18 h. Le résumé du soir rappelle aussi ce qui reste à faire et à rendre aujourd'hui. Le récapitulatif hebdomadaire et le bilan du vendredi sont rattrapés au premier lancement suivant de la même semaine.
- **« Aucun e-mail »** (profil) arrête tous les e-mails automatiques : notifications, rappels de l'agenda (la notification dans l'appli reste), résumé quotidien, e-mail après un import, récapitulatifs. Les e-mails que tu demandes toi-même (test, « M'envoyer le résumé maintenant ») partent quand même. Le résumé quotidien n'est pas activé pour un compte qui ne s'est jamais connecté.
- **Rappel automatique des contrôles** (la veille, 2 jours, 1 semaine avant), e-mail récapitulatif des **changements après chaque import**.
- Pour recevoir réellement les e-mails : renseigne ton adresse dans *Rappels et e-mails*, puis (administrateur) configure l'envoi dans *Réglages › Envoi des e-mails* (aussi dans le menu de l'espace) — mode **SMTP**, préréglage Gmail avec un [mot de passe d'application](https://myaccount.google.com/apppasswords). Sinon, les messages sont seulement enregistrés en `.eml` dans `data/outbox/`.
- Les rappels partent tant que Hangar est lancé (`uv run hangar`). Sous Windows, fais-le démarrer tout seul avec ta session : voir [Sous Windows](#sous-windows--démarrage-avec-la-session-et-mise-à-jour). Si l'application était fermée à l'heure d'un rappel, il est envoyé à la réouverture, sauf si la séance ou la tâche est déjà terminée (un rappel « à une date précise » part encore jusqu'à 24 h après son heure, même après l'échéance de la tâche). Une tâche répétée a un rappel à chaque occurrence, même si tu n'as pas coché la précédente.
- Si un e-mail ne part pas à cause d'une panne passagère (Wi-Fi pas encore revenu au réveil du portable, serveur injoignable), Hangar réessaie seul après 1, 5, 15 puis 60 minutes, jusqu'à la fin de la séance ou de la tâche pour un rappel, jusqu'à la fin de la journée pour le résumé (24 h pour les autres e-mails). Un mot de passe refusé ou une adresse rejetée ne sont pas réessayés. Les e-mails en échec s'affichent dans *Rappels et e-mails* et sur le tableau de bord, avec un bouton « Renvoyer les échecs ».

**Matières reliées au wiki et aux QCM** : dans la fenêtre d'une séance, relie la matière à sa collection du wiki et à sa catégorie de QCM (suggestions automatiques d'après le nom, le sigle ou l'intervenant). Pour un contrôle, la fenêtre mène à sa page « Préparer ce contrôle » (ci-dessous).

- **Préparer un contrôle** : depuis « Contrôles à venir » (tableau de bord) ou depuis la fenêtre d'un contrôle, le bouton « Préparer ce contrôle » ouvre une page qui rassemble tout : le compte à rebours, les pages du cours relié (ouvertes ou non, avec le temps de lecture restant ; c'est une estimation, car une page compte comme lue dès qu'elle a été ouverte une fois et les PDF ne sont pas suivis), les sets de QCM de la matière avec leur maîtrise et leur statut pour le jour du contrôle, le temps déjà passé sur les QCM, les tâches liées, l'examen blanc et le bouton « Fixer la date d'examen des QCM ». Un statut (prêt, dans les temps, à commencer, en retard) apparaît aussi sur le tableau de bord et dans le résumé quotidien.
- **Planifier mes révisions** : sur cette même page, Hangar propose des blocs « Réviser <matière> · N min ». Ils sont placés dans les créneaux libres de ton emploi du temps (hors cours et repas, jusqu'à 22 h), du jour où il faut commencer jusqu'à la veille du contrôle, et leur durée suit la charge du jour (QCM et lecture). Un clic les ajoute à ton agenda comme tâches liées au contrôle, sans rappel par e-mail. Après un nouvel import, les blocs qui tombent sur un cours sont replacés, et ceux d'un contrôle retiré du planning sont supprimés.
- **Mes notes** (menu Agenda) : après un contrôle, saisis ta note (barème et coefficient) dans sa fenêtre ou sur sa page, ou ajoute à la main une note pour une épreuve absente de l'emploi du temps (sport, militaire…). La page montre la moyenne par matière et la moyenne générale (pondérées par les coefficients), leur évolution et, pour les matières reliées aux QCM, ta maîtrise des QCM le jour du contrôle (figée au début du contrôle) à côté de la note obtenue. Le tableau de bord rappelle les contrôles passés dont la note manque.

Une fois la matière reliée à une catégorie de QCM, son prochain contrôle devient tout seul la date d'examen des sets sans date. Si un nouvel import avance, repousse ou retire le contrôle, la date d'examen suit (ou est effacée), et la page de l'import le dit. Une date que tu as choisie toi-même dans *QCM › Planning* n'est jamais modifiée. Annuler l'import remet les dates.

## Wiki : les cours (coffre Obsidian)

Port complet du wiki de [Revision](https://github.com/wakadu13/Revision) : chaque fichier `.md` du coffre est une page.
- **Récupérer tes cours** : `uv run hangar wiki-import https://github.com/wakadu13/Revision` (ou le chemin d'un dossier Obsidian ; `--remplacer` pour écraser, `--images` pour enregistrer aussi les images hébergées sur d'autres sites). Les cours sont alors copiés dans `data/vault/`.
- **Ou travailler directement dans ton coffre Obsidian** (Hangar et Obsidian lisent les mêmes fichiers) : choisis-le une fois pour toutes dans *Réglages › Espace › Coffre du wiki* (sur l'ordinateur de Hangar), ou avec `uv run hangar wiki-coffre "C:\Users\moi\Documents\Obsidian\Cours"`. Le choix est gardé dans `data/coffre.txt` et vaut pour tous les lancements, `lancer.bat` compris ; `uv run hangar wiki-coffre` seul affiche le coffre utilisé, `--defaut` revient à `data/vault/`. Le chemin du coffre s'affiche au démarrage.
  - Si ce dossier est introuvable (clé ou disque débranché, OneDrive pas encore synchronisé, faute de frappe), le wiki le dit au lieu d'ouvrir un coffre vide, et Hangar ne crée rien à sa place : rebranche le disque puis recharge la page. Hangar n'écrit jamais ses pages de bienvenue dans un coffre que tu as choisi.
  - Pour un seul lancement, la variable `HANGAR_VAULT` passe avant ce choix. Sous Windows : `set HANGAR_VAULT=C:\chemin\du\coffre` puis `uv run hangar` dans l'invite de commandes (cmd), ou `$env:HANGAR_VAULT = "C:\chemin\du\coffre"` dans PowerShell ; sous Linux/macOS : `HANGAR_VAULT=/chemin/du/coffre uv run hangar`.
- **Corbeille et renommage** : « Mettre à la corbeille » range la page dans le dossier `.trash` du coffre, à son chemin d'origine ; la page *Corbeille* du menu du wiki la remet en place d'un clic. Une page supprimée hors du wiki (ou une corbeille vidée) garde son historique : la Corbeille le liste, et « Recréer la page avec cette version » la fait revenir. « Renommer / déplacer » (menu ⋯ d'une page) met à jour les liens `[[…]]` des autres pages (leur ancien texte reste dans l'historique), ainsi que l'historique, les favoris, les pages récentes, le tuteur et les QCM générés d'après la page. Un lien écrit avec un alias de la page reste tel quel : l'alias suit la page.
- Syntaxe Obsidian : liens `[[…]]` et alias, intégrations `![[…]]` (images, PDF, notes, sections), encadrés `> [!note]`, propriétés YAML, tags, formules `$…$` / `$$…$$`, diagrammes Mermaid, surlignage, notes de bas de page…
- **Images hébergées ailleurs** : le wiki ne charge jamais rien d'un autre site. Il affiche un lien « Image externe : légende ↗ » à la place de l'image. C'est la même règle partout dans Hangar (tickets, pages, réponses de l'IA, QCM). Le bouton « Enregistrer ces images dans le wiki » de la page Importer (ou `hangar wiki-import --images`) les télécharge une fois dans `image/` et met les pages à jour (`![[image.png]]`) : elles s'affichent alors aussi hors ligne.
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
- Imports contrôlés avant lecture : paquet Anki (50 Mo envoyés, 200 Mo une fois décompressé, 5 000 cartes et 15 secondes de lecture au plus), document Word (20 Mo de texte décompressé), classeur Excel (200 Mo décompressés et 20 000 lignes). Un paquet d'une version récente d'Anki doit être exporté en cochant « Compatibilité avec les anciennes versions d'Anki ». Un fichier de QCM mal formé ne bloque plus la section : il est nommé dans les erreurs de la page QCM et par `qcm-verifier`.
- Les cartes dues apparaissent sur le tableau de bord et dans le résumé quotidien. `uv run hangar qcm-verifier` contrôle les fichiers.
- **QCM hors ligne** (`/qcm/hors-ligne/`, installable) : télécharge des sets et des paquets de langue, révise sans réseau ; les réponses rejoignent ta progression au retour de la connexion, même si le set a été renommé ou déplacé entre-temps. Si son fichier est introuvable pour l'instant (en cours de modification, par exemple), elles restent sur l'appareil jusqu'à ce qu'il revienne.
  - À une adresse http://192.168.x.x, un téléphone sans l'appli Android peut télécharger des sets, mais la page QCM hors ligne ne s'ouvrira pas sans réseau : le navigateur ne la garde qu'en HTTPS ou sur l'ordinateur qui fait tourner Hangar. La page le signale. Utilise l'appli Android, ou sers Hangar en HTTPS. Hors ligne, dans les 30 jours avant un examen, une carte réussie revient au même moment qu'en ligne.
- **Rappel quotidien par notification** du navigateur, à l'heure choisie (page « Aujourd'hui ») : ce qu'il reste à réviser et ta série de jours.
  - Le rappel du soir et le widget Android comptent comme le tableau de bord : les révisions dues plus les nouvelles questions prévues par tes quotas, par exemple « 3 à revoir · 5 nouvelles à découvrir ». Le rappel se tait quand tout est fait. Les notifications web marchent avec Chrome, Edge, Firefox et Safari ; une autre adresse de notification est refusée. Si l'envoi échoue (pas d'Internet), Hangar réessaie 15 minutes plus tard ; un abonnement refusé trois fois de suite est supprimé, et il suffit de réactiver le rappel sur l'appareil.
- **Appli Android de Revision** : Hangar répond aux mêmes adresses (connexion par jeton d'appareil, QCM hors ligne, rappel, widget, partage vers le wiki) ; il suffit d'indiquer l'adresse de Hangar dans les réglages de l'appli. Les appareils connectés se révoquent depuis *Réglages › Profil*.
- Connexion protégée : à partir de 3 essais restants, la page de connexion annonce combien il en reste ; après 5 mauvais mots de passe, la connexion est bloquée 30 minutes. Le message reste affiché sur la page et donne la façon de débloquer tout de suite : sur le PC qui fait tourner Hangar, `uv run hangar connexions-debloquer`, ou un administrateur depuis son profil (Connexions bloquées).

### Passer de Revision (Raspberry Pi) à Hangar

`wiki-import` copie tes cours et tes sets, mais pas ta progression. Elle est restée dans la base de Revision sur le Pi : états des questions, échéances, série de jours, temps par matière, quotas et dates d'examen. Fais les étapes dans cet ordre et n'éteins pas le Pi avant la fin :

1. Sur le téléphone, synchronise une dernière fois l'appli avec le Pi (page QCM hors ligne, « Synchroniser »).
2. Importe le coffre : `uv run hangar wiki-import https://github.com/wakadu13/Revision` (ou le dossier `vault/`).
3. Copie sur le PC le dossier de données de Revision (le fichier `db.sqlite3` et le dossier `perso/`), puis reprends ta progression :
   `uv run hangar qcm-import-revision chemin\vers\db.sqlite3 --compte <ton identifiant Hangar>`
   - `--joueur <nom>` : ton nom d'utilisateur dans Revision, s'il est différent de ton identifiant Hangar ;
   - `--coffre chemin\vers\vault` : retrouve par leur contenu les sets que tu as renommés ou déplacés depuis ;
   - `--essai` : affiche le bilan sans rien enregistrer.
   La base de Revision n'est jamais modifiée et tu peux relancer la commande sans risque : rien n'est compté deux fois. Ton espace perso (`perso/<nom>/`) et les explications de l'IA sont repris aussi. Un set introuvable garde sa progression ; tu la retrouves sur la page des statistiques des QCM, où tu peux la rattacher.
4. Seulement ensuite, indique l'adresse de Hangar dans les réglages de l'appli Android. Si tu le fais avant, sa première synchronisation remplace la progression du téléphone par celle, encore vide, de Hangar.

## Assistant IA (Claude ou IA open source)

Bouton **Assistant IA** en haut à droite (ou Ctrl+J) : un panneau comme « Plane AI », qui connaît la page ouverte (cours du wiki, ticket, tableau de bord, set de QCM, workgraph…).
- **Fournisseurs** (*Réglages › Fournisseurs d'IA*, préréglages en un clic) : **Claude** via l'API Anthropic (SDK officiel, `claude-opus-5-5` par défaut, modèle rapide pour les petites tâches), ou toute API **compatible OpenAI** : **Ollama** (`ollama pull qwen2.5`, adresse `http://localhost:11434/v1`), LM Studio, vLLM, llama.cpp, OpenRouter, Mistral, Groq, Together, ou une adresse personnalisée. Test de connexion, liste des modèles, plafond de dépense mensuel, suivi de l'usage.
- Réponses en direct, « @ » pour ajouter du contexte, « / » pour les commandes ; l'assistant consulte tickets, tâches, agenda et wiki, et **propose des actions** que tu valides d'un clic : créer la tâche, créer le ticket, ajouter à la page, ajouter au cycle, changer une échéance.
- Avant de valider une action, « Voir le contenu » montre le texte exact qui sera ajouté (bloc de page, description du ticket, notes de la tâche). L'assistant traite le contenu des pages, des PDF et des commentaires comme des données, jamais comme des consignes, et ses réponses ne chargent aucune image externe : une consigne cachée dans un document ne peut pas faire sortir tes données sans clic.
- Fonctions d'IA de Revision : expliquer une réponse de QCM, corriger une réponse rédigée, générer un QCM ou des fiches de vocabulaire depuis un cours, « Demander au wiki » (réponses avec sources), tuteur d'une page, améliorer la rédaction dans l'éditeur, transformer un PDF ou du LaTeX en page, corriger une copie photographiée.
- En ligne de commande : `hangar ia-fournisseurs`, `ia-ajouter ollama`, `ia-tester`, `ia-demander "…"`, `ia-usage`, `ia-travaux`, `ia-evaluer`.
- **Plafond mensuel** (*Réglages › Plafond et consignes*) : il compte aussi les **travaux en lot** dès leur dépôt (chaque lot réserve son coût au plus, remplacé par le coût réel à son retour) et les **réponses arrêtées en cours de route** (bouton Arrêter, coupure). Un nouveau travail est refusé quand le plafond est atteint. Un modèle Claude absent de la table des prix est compté au tarif le plus cher de sa famille, et les réglages affichent « Tarif inconnu » (y compris pour un service payant compatible OpenAI dont tu n'as pas saisi les prix).
- **Travaux** (*IA › Travaux*) : « Annuler » arrête un travail en attente, en cours ou dans un lot (le lot est aussi annulé chez Anthropic ; ce qui est déjà traité reste facturé). Un lot impossible à récupérer (clé changée, lot expiré, plus de 29 jours) passe en échec et se relance d'un clic. `hangar run` reprend les travaux interrompus dès le démarrage.
- **Pages longues** : l'IA reçoit les passages de la page qui parlent de ta question (ou un aperçu de toute la page pour « Interroge-moi » et « Résumer »), et sait qu'elle n'a que des extraits. La pastille du panneau affiche « page coupée ». Claude lit jusqu'à 80 000 caractères de la page ouverte, un modèle local 12 000.
- **Réponses coupées** : une réponse arrêtée par la limite de longueur porte la mention « Réponse coupée ». Une explication de QCM coupée n'est pas gardée, et un administrateur peut refaire une explication gardée (« ↻ Refaire l'explication »).
- **Ollama et mémoire** : par défaut, Ollama ne garde que 4 096 jetons et oublie le début d'une demande plus longue, sans prévenir. Hangar t'avertit quand c'est le cas. Pour lui donner plus de mémoire sous Windows : dans l'application Ollama, *Settings › Context length* ; ou crée la variable d'environnement utilisateur `OLLAMA_CONTEXT_LENGTH` = `16384` (menu Démarrer › « Modifier les variables d'environnement pour votre compte »), puis quitte Ollama (icône près de l'horloge › *Quit Ollama*) et relance-le.
- **Clé d'API liée à l'adresse** : si tu changes l'adresse d'un fournisseur sans retaper sa clé, la clé enregistrée est effacée et n'est jamais envoyée au nouveau serveur. Une redirection vers un autre serveur est refusée.
- **Qualité des réponses** (*IA › Fournisseurs et usage*) : après un 👎, dis en une phrase ce qui n'allait pas. Tu retrouves tes réponses notées 👎 avec ton commentaire, leur modèle et un lien vers la conversation, ainsi que les avis et le taux de questions de QCM écartées pour chaque modèle. Avant de changer de modèle ou de consignes, `hangar ia-evaluer` rejoue 10 cas fixes : dates (« vendredi », « lundi prochain »), outil choisi, consigne cachée dans une page, QCM valide, correction de réponses rédigées, citation des sources. Il donne le taux de réussite de chaque modèle et demande confirmation avant un passage payant (`--oui` pour ne pas la demander, `--cas tache-vendredi qcm-json` pour quelques cas).
- **Kit de révision d'un contrôle** (bouton « Kit de révision » dans le sommaire d'un dossier du wiki, ou *IA › Travaux*) : il liste les pages de la matière qui n'ont pas encore de fiche ou de QCM, sans les sommaires ni les fiches. Décoche ce que tu ne veux pas : un set importé au titre proche est déjà décoché. L'écran montre le coût estimé et refuse au-delà de ce qui reste du plafond du mois. Un seul clic lance tout, en lot à moitié prix si tu veux. Les fiches sont écrites directement (« Page (Fiche) »), les QCM arrivent en brouillon à relire et prennent à leur publication la date du contrôle choisi. Une seule notification arrive à la fin du kit.

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

> Un nouveau mot de passe doit faire au moins 8 caractères. Le changer déconnecte tes autres navigateurs, y compris les cookies « Rester connecté », ainsi que l'application Android. Le bouton « Déconnecter mes autres appareils », dans *Réglages › Profil*, fait la même chose sans changer le mot de passe ; l'application Android n'est déconnectée que si tu coches la case. Après cette mise à jour, chacun doit se reconnecter une fois. Restaurer une sauvegarde déconnecte aussi tout le monde. Pour déconnecter tout le monde d'un coup, supprime `data/secret.key` puis relance Hangar.

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
- **`partager-en-https.bat`** : installe Caddy si besoin, puis lance Hangar en HTTPS pour le téléphone (adresse et QR code affichés). Voir « Ouvrir Hangar sur ton téléphone ».

Sans fenêtre, les messages d'erreur vont dans `data\journal\` : `hangar.log` (erreurs et avertissements) et `console.log` (ce qu'afficherait la fenêtre). Les erreurs des pages et des envois automatiques sont aussi listées pour les administrateurs sur la page des erreurs du serveur (`/erreurs/`).
Sur `/erreurs/`, les mots de passe, clés et jetons sont masqués, y compris dans les erreurs enregistrées avant cette version : tu peux copier une trace pour demander de l'aide.

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

> Sur le réseau local, la page de connexion montre la liste des comptes (« Qui es-tu ? »). Pour la cacher aux autres appareils du réseau, décoche le réglage dans *Réglages › Espace de travail*. Elle reste toujours visible sur cet ordinateur et n'est jamais montrée quand Hangar est en ligne. Au démarrage avec `--host 0.0.0.0`, Hangar rappelle que les échanges ne sont pas chiffrés : évite les Wi-Fi publics.

## Ouvrir Hangar sur ton téléphone (HTTPS sur le Wi-Fi)

L'application Android marche déjà en http (`uv run hangar run --host 0.0.0.0`). Mais dans le navigateur du téléphone, le wiki et les QCM hors ligne, ainsi que les notifications, ne marchent qu'en **HTTPS**. Pour les avoir :

1. Ferme Hangar s'il est ouvert. Mets le téléphone sur le même Wi-Fi que l'ordinateur.
2. Double-clique sur `scripts\partager-en-https.bat`. La première fois, il :
   - installe **Caddy**, un petit serveur gratuit qui chiffre la connexion (avec winget ; sinon il le télécharge dans `data\outils\caddy.exe`) ;
   - demande l'autorisation d'ouvrir le port 5443 dans le pare-feu de Windows, pour les réseaux privés seulement : accepte.
   Ensuite, il lance Hangar avec `--lan-https`. Garde la fenêtre ouverte tant que tu utilises Hangar sur le téléphone.
3. L'adresse du téléphone (par exemple `https://192.168.1.20:5443/`) s'affiche dans la fenêtre avec un **QR code**. Tu les retrouves aussi dans **Réglages › Profil › Sur ton téléphone**. Scanne le QR code avec l'appareil photo du téléphone.
4. **La première fois seulement**, installe le certificat de Hangar sur le téléphone. Sinon, le navigateur affiche « Connexion non privée ».
   - Au premier avertissement, touche « Paramètres avancés » puis « Continuer ».
   - Télécharge le certificat : ajoute `settings/certificat-hangar.crt` à l'adresse, ou utilise le lien dans Réglages › Profil.
   - **Android** : Paramètres › Sécurité › Chiffrement et identifiants › Installer un certificat › **Certificat CA**, puis choisis le fichier. Utilise Chrome (Firefox ignore ces certificats).
   - **iPhone** : ouvre le lien avec Safari. Va dans Réglages › Profil téléchargé › Installer. Puis dans Réglages › Général › Informations › Réglages des certificats, active « Caddy Local Authority ».
   - Pour vérifier que c'est bien ton certificat, compare l'empreinte SHA-256 affichée par le téléphone à celle de Réglages › Profil.
   - Ferme puis rouvre le navigateur.

Bon à savoir :
- Si l'adresse IP de l'ordinateur change (autre box, autre Wi-Fi), relance simplement le script : le certificat est refait tout seul, et celui du téléphone reste valable.
- Hangar n'écoute alors que cet ordinateur. Seul Caddy est ouvert au réseau, en HTTPS. Sur ce PC, continue d'ouvrir `http://127.0.0.1:5000`.
- Si Windows règle ton Wi-Fi en « réseau public », le téléphone est bloqué : le script te prévient. Pour ton propre Wi-Fi, passe-le en Privé (Paramètres › Réseau et Internet › Wi-Fi › ton réseau › Type de profil réseau). Ne partage pas Hangar sur un Wi-Fi public.
- Avant de partager, change les mots de passe de démonstration. Hangar affiche un avertissement tant qu'un compte les garde.
- La première installation (création du premier compte administrateur, page `/setup`) se fait uniquement sur l'ordinateur où tourne Hangar. Elle est refusée depuis le réseau et à travers un proxy ou un tunnel. Sinon, utilise `uv run hangar create-admin`.
- Le port 5443 est déjà pris ? Ajoute `--https-port 8443` à la commande. Les messages de Caddy sont dans `data\journal\caddy.log`.
- Sans Internet pour l'installation, copie `caddy.exe` (caddyserver.com) dans `data\outils\`. Tu peux aussi donner son chemin dans la variable `HANGAR_CADDY`.
- Autre possibilité, hors du Wi-Fi : Tailscale (`tailscale serve`) donne un certificat reconnu partout. Il faut un compte et l'appli Tailscale sur chaque appareil. Lance alors Hangar avec `--behind-proxy`.

## Mettre Hangar en ligne

Hangar est fait pour tourner sur ton ordinateur ou sur le réseau local. Pour le rendre joignable depuis Internet, place-le derrière un tunnel Cloudflare ou un serveur HTTPS (Caddy, nginx…) et lance-le avec l'option qui correspond :

| Situation | Commande | Variables équivalentes |
|---|---|---|
| Tunnel Cloudflare (cloudflared) | `uv run hangar run --cloudflare` | `HANGAR_CLOUDFLARE=1` et `HANGAR_HTTPS=1` |
| Autre proxy qui écrit X-Forwarded-For (Caddy, nginx) | `uv run hangar run --behind-proxy --https` | `HANGAR_BEHIND_PROXY=1` et `HANGAR_HTTPS=1` |
| HTTPS sans proxy | `uv run hangar run --https` | `HANGAR_HTTPS=1` |

Ces réglages font lire à Hangar l'adresse réelle des visiteurs (pour le blocage des essais de mot de passe) et l'adresse publique en https (liens des jetons d'API et des exports). Ils activent aussi les cookies sécurisés et l'en-tête HSTS. Sans eux, Hangar ne croit aucun en-tête de proxy : n'active pas ces options pour un usage en local ou en http sur le réseau, sinon la connexion ne marche plus. Une fois en ligne, la page de connexion n'affiche plus la liste des comptes. Avant de partager l'adresse, change les mots de passe de démonstration. Pour surveiller le serveur : `/api/status/` répond sans connexion.

Variables d'environnement reconnues :

| Variable | Rôle |
|---|---|
| `HANGAR_DATA` | dossier des données (par défaut `data/`) |
| `HANGAR_TZ` | fuseau de l'emploi du temps (par défaut `Europe/Paris`) : rappels, résumé du jour et « aujourd'hui » suivent cette heure, même si l'ordinateur ou le serveur est réglé autrement ; `local` = heure de l'ordinateur |
| `HANGAR_VAULT` | dossier du coffre Obsidian du wiki |
| `HANGAR_WIKI_HOME` | page d'accueil du wiki (par défaut « Accueil ») |
| `HANGAR_SMTP_PASSWORD` | mot de passe SMTP, pour ne pas l'enregistrer dans la base |
| `HANGAR_PUSH_CONTACT` | adresse de contact des notifications web |
| `HANGAR_CLOUDFLARE` | derrière un tunnel Cloudflare (adresse du visiteur lue dans CF-Connecting-IP) |
| `HANGAR_BEHIND_PROXY` | derrière un autre proxy (dernière adresse de X-Forwarded-For) |
| `HANGAR_HTTPS` | visiteurs en HTTPS : cookies sécurisés et HSTS |
| `HANGAR_CSP` | `rapport` ou `non` : politique de sécurité des pages seulement signalée, ou désactivée |
| `HANGAR_COMPRESSION` | `jamais` ou `toujours` : compression des pages |

Les heures de l'agenda sont celles de Paris, quel que soit le réglage de la machine : un serveur ou un Raspberry Pi resté en UTC envoie les rappels à la bonne heure. La page *Agenda › Rappels et e-mails* affiche l'horloge utilisée et signale un écart avec celle de l'ordinateur.

## Fonctionnalités

**Tickets (work items)**
- États personnalisables par groupe (Backlog, Non commencé, En cours, Terminé, Annulé), priorités, assignés multiples, étiquettes (avec parent), dates de début et d'échéance, estimations (points, Fibonacci, tailles, heures, jours-homme), cycle, modules.
- Sous-tickets avec progression, relations *bloque / bloqué par / lié à / doublon de*, liens, pièces jointes (glisser-déposer), duplication, déplacement vers un autre projet, archivage.
- Suppression définitive d'un ticket réservée à l'administrateur et au responsable du projet (fiche, suppression en masse ou API) ; les autres membres peuvent archiver. Les fichiers joints sont supprimés avec le ticket.
- Commentaires markdown avec réactions, **@mentions** et liens automatiques vers les tickets (`EAE-12`), historique complet de chaque modification (l'ancienne description est gardée et peut être restaurée), suivi du temps passé.

**Vues et organisation**
- 5 dispositions : **liste**, **tableau kanban** (glisser-déposer entre colonnes), **tableur** (modifications en masse), **calendrier** (glisser une échéance), **Gantt** (déplacer / redimensionner les barres).
- Filtres (état, priorité, assigné, étiquette, cycle, module, dates, créateur, abonnement, texte), regroupement, tri, propriétés affichées, **vues enregistrées** partagées, favoris.
- Modification en ligne de toutes les propriétés, ajout rapide dans chaque groupe.

**Pilotage**
- **Cycles** (sprints) : avancement, burndown, répartition par assigné / étiquette, transfert des tickets non terminés.
- **Fermeture automatique** (Paramètres du projet › Automatisations) : seuls les tickets vraiment inactifs sont fermés (saisir du temps, ajouter un sous-ticket, un lien ou une pièce jointe compte comme une activité) ; elle épargne les tickets qui ont des sous-tickets ouverts, les demandes d'invité en attente et les projets archivés. Les assignés et les abonnés sont prévenus.
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
- Les e-mails écrits à la main partent de l'adresse d'expédition configurée, souvent ton Gmail. Par défaut, un membre ne peut donc écrire qu'aux membres de l'équipe, que ce soit depuis « Écrire un e-mail » ou en envoyant un bilan. Pour lui permettre d'écrire à d'autres adresses, coche « Les membres peuvent écrire hors de l'équipe » : il reste alors limité à 20 adresses extérieures par message et à 100 e-mails par 24 heures. Les administrateurs n'ont pas de limite. Les réponses reviennent à l'auteur du message.
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
- Exports : les fichiers Excel, CSV et Word s'ouvrent même si un titre contient un saut de ligne collé depuis PowerPoint. Un texte qui commence par « = » reste du texte dans Excel ; dans un CSV, une apostrophe est ajoutée devant un texte qui commence par =, +, - ou @ (Hangar la retire à la réimportation). À l'import, seules les personnes déjà membres du projet sont assignées ; seul un administrateur du projet peut, par un import, y faire entrer d'autres comptes (les invités ajoutés ainsi sont signalés). La case « Créer les membres manquants » est réservée à l'administrateur de l'espace : les comptes créés sont désactivés, à activer ensuite dans Équipe.
- **Sauvegarde complète (.zip)** en un clic ou en ligne de commande (base, wiki, sets de QCM, copies, pièces jointes, agenda), sauvegardes de la base restaurables en un clic, export JSON des projets (Travail).
- **API REST** `/api/v1` avec jetons personnels (voir *Réglages › Jetons d'API*, ou *Jetons d'API* dans le menu de l'espace). Pour modifier une description sans écraser celle d'un autre, renvoie `description_version` (lu dans le ticket) avec la nouvelle `description` : la réponse est 409 si elle a changé entre-temps.

## Commandes

```bash
uv run hangar                       # lance le serveur (http://127.0.0.1:5000)
uv run hangar run --port 8080       # autre port ; --host 0.0.0.0 pour le réseau local ; --no-browser
uv run hangar run --lan-https       # ouvre Hangar au téléphone en HTTPS sur le Wi-Fi, avec Caddy (--https-port N, 5443 par défaut)
uv run hangar demo                  # données de démonstration du projet EAE (affiche une fois le mot de passe provisoire)
uv run hangar demo --force          # remplace la base par la démonstration (demande « oui », sauvegarde avant ; wiki gardé ; --yes)
uv run hangar rebuild-docs          # recrée les tickets du projet EAE d'après les documents (sauvegarde avant)
uv run hangar agenda-import planning.ics  # importe l'emploi du temps (.ics) ; --user identifiant, --mode weeks|future|merge
uv run hangar mail-send             # envoie la file d'e-mails, les rappels de l'agenda et les envois programmés maintenant
uv run hangar wiki-import [<dossier|url git>] [--remplacer] [--images]   # importe un coffre Obsidian (cours) dans le wiki ; --images enregistre aussi les images hébergées ailleurs
uv run hangar wiki-coffre "C:\chemin\du\coffre"   # utilise ce coffre Obsidian à chaque lancement (sans dossier : affiche le coffre ; --defaut : data/vault)
uv run hangar wiki-indexer-pdf      # lit le texte de tous les PDF du wiki (recherche)
uv run hangar qcm-verifier          # vérifie les fichiers de QCM et de langue
uv run hangar qcm-import-revision db.sqlite3 --compte <identifiant>   # reprend la progression de Revision (--joueur, --coffre, --essai)
uv run hangar qcm-rappels           # envoie maintenant les rappels de révision dus (notifications)
uv run hangar connexions-debloquer  # lève les blocages de connexion
uv run hangar ia-fournisseurs       # liste les fournisseurs d'IA configurés (celui utilisé par défaut)
uv run hangar ia-ajouter ollama     # ajoute un fournisseur d'IA (anthropic, ollama, lmstudio, vllm, openrouter, mistral…)
uv run hangar ia-tester             # teste la connexion aux fournisseurs d'IA
uv run hangar ia-demander "…"       # pose une question au modèle configuré
uv run hangar ia-usage              # dépense et jetons de l'IA ce mois-ci
uv run hangar ia-travaux            # fait les travaux d'IA en attente et récupère les lots terminés
uv run hangar ia-evaluer            # rejoue 10 cas fixes sur chaque fournisseur d'IA et donne le taux de réussite par modèle (confirmation avant un passage payant ; --oui, --cas)
uv run hangar mail-test adresse@ex.fr  # e-mail de test
uv run hangar backup                # sauvegarde de la base seule dans data/backups/
uv run hangar backup --complet --vers E:\Sauvegardes --garder 10   # sauvegarde complète (.zip) ailleurs, garde les 10 dernières ; --sans-secrets
uv run hangar restore copie.db      # remplace la base par une sauvegarde .db (vérifiée, état actuel sauvegardé avant ; Hangar arrêté)
uv run hangar reset                 # efface la base (sauvegarde avant, pièces jointes mises de côté dans data/backups ; wiki gardé)
uv run hangar create-admin          # crée ou répare un compte administrateur
uv run hangar --data /chemin run    # utiliser un autre dossier de données
uv run python -m unittest discover -s tests   # tests (lancés aussi par GitHub Actions sous Linux et Windows)
```

### Vérifications automatiques (CI)

À chaque envoi sur GitHub, la CI (`.github/workflows/tests.yml`) lance la suite sous Ubuntu (Python 3.11 et 3.13) et sous Windows (Python 3.13). Elle fait aussi les contrôles suivants :

- **Vérification du code** : `uvx ruff@0.17.4 check --no-cache --select F,E9 hangar tests`. Elle repère les noms inconnus, les imports inutiles et les erreurs de syntaxe, même dans une partie du code qu'aucun test n'exécute. Lance la même commande chez toi avant d'envoyer.
- **Toutes les pages** (`tests/test_pages_smoke.py`) : chaque page doit vraiment s'afficher (code 200). Les exceptions voulues sont écrites dans `NON_200`. Une page que le parcours ne visite pas doit figurer dans `SAUTEES`, avec le test qui la couvre. Sans session, chaque page doit renvoyer vers la connexion, sauf celles de la liste `PUBLIQUES`. Quand tu ajoutes une page, ajoute ses paramètres dans `par_route` ou une ligne dans l'une de ces listes.
- **Lancement réel** : `tests/test_outillage.py` démarre `hangar run` sur un port libre et vérifie que le serveur répond.
- **Mise à jour** : le même fichier ouvre une base à laquelle il manque des colonnes et des tables. Elle doit être mise à niveau au démarrage, sans aucune action de ta part.
- En cas d'échec, les journaux des commandes sont joints au job, dans « Artifacts ».

**Numéro de version** : il est écrit à un seul endroit, `__version__` dans `hangar/__init__.py`. `pyproject.toml` le lit à cet endroit. Pour publier une version, change seulement cette ligne.

**Dépendances** : Dependabot (`.github/dependabot.yml`) propose chaque semaine les mises à jour de `uv.lock` et des actions GitHub. Chaque proposition est testée par la CI avant que tu l'acceptes.

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
│   ├── agenda/             agenda : imports (le dernier fait foi), tâches, rappels, e-mails, source automatique, vues, export .ics, liens wiki / QCM
│   ├── heure.py            heure de l'agenda (Europe/Paris ou HANGAR_TZ) : now() et today() communs
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
│   ├── securite.py         garde-fous communs : couleurs, adresses locales, fichiers servis, images externes du Markdown
│   ├── nettoyage_html.py   nettoyage du HTML affiché (tickets, pages, e-mails, IA, wiki, QCM) : une seule règle, médias locaux seulement
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
