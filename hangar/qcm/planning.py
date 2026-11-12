"""Planning des révisions d'un set : quotas journaliers et estimation de la durée.

Deux quotas par jour, comme dans Anki : les nouvelles questions (jamais vues) et
les révisions (questions déjà vues, revenues à échéance).

L'estimation simule la révision espacée (FSRS, comme la planification réelle)
jour après jour : chaque jour, les questions dues sont révisées dans la limite
des quotas, avec un taux de bonnes réponses égal à la précision du joueur sur ce
set. Une question est « sue » dès que sa stabilité atteint STABILITE_OBJECTIF
jours, même si elle retombe ensuite (compter les questions actuellement stables
ne convergerait pas toujours). Le set est prêt quand une part PART_OBJECTIF des
questions est sue (viser 100 % ferait dépendre l'estimation de la question la
plus malchanceuse).

À l'approche d'un examen, la rétention visée passe de 90 % à RETENTION_EXAMEN :
les révisions se resserrent. Comme quiz.py, ce module ne dépend ni des modèles
ni des vues.
"""

import math
from datetime import timedelta

from . import fsrs, langues, quiz

STABILITE_OBJECTIF = 7.0    # stabilité (jours de rétention à 90 %) à partir de laquelle une question est sue
PART_OBJECTIF = 0.95        # part des questions sues pour considérer le set prêt
RETENTION_EXAMEN = 0.95     # rétention visée quand l'examen approche
JOURS_EXAMEN_PROCHE = 30    # … c'est-à-dire à moins de 30 jours
PRECISION_DEFAUT = 0.8      # tant qu'il y a trop peu de réponses pour la mesurer
PRECISION_MIN, PRECISION_MAX = 0.6, 0.97
REPONSES_MIN_PRECISION = 20
SECONDES_DEFAUT = 20        # temps par question tant qu'il n'est pas mesuré
REPONSES_MIN_CHRONO = 10
HORIZON_JOURS = 365
PLATEAU_JOURS = 60          # arrêt anticipé de la simulation : jours sans progrès (voir simuler)…
SERIES_PLATEAU = 2          # … pendant lesquels chaque question restante a fait 2 fois sa plus longue série de réussites


def faites_aujourdhui(progress, now):
    """(nouvelles, révisions) déjà faites aujourd'hui sur ce set (heure locale).

    Une question découverte aujourd'hui compte comme nouvelle, même si elle a
    été revue ensuite dans la journée.
    """
    jour = quiz.jour(now)
    nouvelles = revisions = 0
    for st in progress.values():
        if st.premiere_revue and quiz.jour(st.premiere_revue) == jour:
            nouvelles += 1
        elif st.last_review and quiz.jour(st.last_review) == jour:
            revisions += 1
    return nouvelles, revisions


def restant(limite, deja):
    """Places restantes aujourd'hui pour un quota (None : pas de limite)."""
    return None if limite is None else max(0, limite - deja)


def retention_cible(date_objectif, now):
    """Rétention visée à l'enregistrement d'une réponse : plus haute (révisions plus
    serrées) dans les JOURS_EXAMEN_PROCHE derniers jours avant l'examen."""
    if date_objectif is None:
        return fsrs.RETENTION
    dispo = (date_objectif - quiz.jour(now)).days
    return RETENTION_EXAMEN if 0 < dispo <= JOURS_EXAMEN_PROCHE else fsrs.RETENTION


def simuler(cartes, quota_nouvelles, quota_revisions, precision, deja=(0, 0), retention=fsrs.RETENTION):
    """Simule les révisions. `cartes` : liste de (état FSRS ou None si jamais vue,
    jour où elle est due, jours depuis la dernière révision).

    Les quotas sont des nombres par jour ; `deja` = (nouvelles, révisions) déjà
    faites aujourd'hui. Renvoie (jours nécessaires, nombre de réponses), ou None
    au-delà de HORIZON_JOURS.

    Arrêt anticipé : avec une précision faible (moins de 72 % environ), la plupart des
    questions ne se stabilisent jamais et la simulation parcourait les 365 jours à
    chaque affichage (jusqu'à plusieurs secondes par page). Elle renvoie None plus tôt
    quand la suite est jouée d'avance :
    - toutes les nouvelles sont découvertes ;
    - le quota de révisions ne peut plus jamais couper la file : il y a au plus
      quota_revisions questions vues, donc chaque question est révisée à son échéance et
      évolue seule. Il ne suffit pas que la file n'ait pas été coupée depuis un moment :
      une coupure plus tardive reporte une révision, ce qui allonge l'intervalle et peut
      encore faire progresser la question (cas réel : quota de 1 révision par jour,
      « prêt en 325 jours » au lieu de « impossible »). Quand le quota est plus petit
      que le nombre de questions, la simulation va donc jusqu'au bout ; les appels sans
      plafond de révisions (conseil de rattrapage, dichotomie) gardent l'arrêt anticipé ;
    - depuis PLATEAU_JOURS jours, aucune question n'est devenue « sue » ;
    - pendant ce temps, chaque question restante a fait SERIES_PLATEAU fois la plus
      longue série de réussites que permet la précision (les échecs, réguliers, laissent
      au plus ⌈1/(1-p)⌉ - 1 réussites d'affilée), sans atteindre STABILITE_OBJECTIF :
      les séries suivantes repartent du même état (difficulté au maximum après chaque
      échec) et n'iront pas plus haut. Une seule série ne suffit pas : la première peut
      partir d'un état plus bas, pas encore stabilisé.
    Vérifié aussi contre la simulation complète : résultats identiques sur 5 192
    simulations (base d'un an d'usage, précisions de 60 à 97 %, quotas variés) et des
    dizaines de milliers de tirages au hasard (tests/test_perf.py).
    """
    etats = [e for e, _, _ in cartes]
    dues = [d for _, d, _ in cartes]
    derniere = [-depuis for _, _, depuis in cartes]
    nouvelles = [i for i, (e, _, _) in enumerate(cartes) if e is None]
    vues = [i for i, (e, _, _) in enumerate(cartes) if e is not None]
    sues = {i for i in vues if etats[i].stabilite >= STABILITE_OBJECTIF}
    restantes = math.ceil(PART_OBJECTIF * len(cartes)) - len(sues)
    if restantes <= 0:
        return 0, 0
    # Échecs répartis régulièrement, carte par carte (une réponse sur 1/(1-p), premier
    # échec décalé d'une carte à l'autre) plutôt que tirés au sort : estimation stable,
    # sans carte « malchanceuse » qui concentrerait tous les échecs, et plus de
    # nouvelles par jour donne une durée plus courte (dichotomie ci-dessous).
    echecs = [(i * 0.6180339887) % 1.0 for i in range(len(cartes))]
    # Arrêt anticipé (voir plus haut) : plus longue série de réussites que permet la précision,
    # et pour chaque question, sa série en cours et les jours où elle a atteint cette longueur.
    # `q` est aussi l'incrément des échecs ci-dessous (même valeur que 1 - precision).
    q = 1 - precision
    serie_max = math.ceil(1 / q) - 1 if q > 0 else None
    serie = [0] * len(cartes)
    longues = [[] for _ in cartes]
    calme_depuis = 0  # premier jour sans progrès et sans quota de révisions atteint (le jour 0 peut être coupé par `deja`)
    reponses = 0
    for jour in range(HORIZON_JOURS):
        places_n = max(0, quota_nouvelles - (deja[0] if jour == 0 else 0))
        places_r = max(0, quota_revisions - (deja[1] if jour == 0 else 0))
        dues_du_jour = [i for i in vues if dues[i] <= jour]
        coupee = len(dues_du_jour) > places_r
        if coupee:
            # Même ordre que la file du jour : les plus menacées d'oubli d'abord, puis nouvelles.
            a_revoir = sorted(dues_du_jour, key=lambda i: (fsrs.retrievabilite(jour - derniere[i], etats[i].stabilite),
                                                           dues[i]))[:places_r]
        else:
            a_revoir = dues_du_jour  # toutes révisées aujourd'hui : l'ordre ne change rien
        decouvertes, nouvelles = nouvelles[:places_n], nouvelles[places_n:]
        avant = restantes
        for i in a_revoir + decouvertes:
            reponses += 1
            echecs[i] += q
            reussie = echecs[i] < 1
            if not reussie:
                echecs[i] -= 1
            note = fsrs.BIEN if reussie else fsrs.A_REVOIR
            etats[i] = fsrs.premiere(note) if etats[i] is None else fsrs.suivante(etats[i], jour - derniere[i], note)
            derniere[i] = jour
            dues[i] = jour + (1 if not reussie else max(1, int(fsrs.intervalle_jours(etats[i].stabilite, retention) + 0.5)))
            if etats[i].stabilite >= STABILITE_OBJECTIF and i not in sues:
                sues.add(i)
                restantes -= 1
            serie[i] = serie[i] + 1 if reussie else 0
            if serie_max is not None and serie[i] == serie_max:
                longues[i].append(jour)
        vues += decouvertes
        if restantes <= 0:
            return jour + 1, reponses
        if decouvertes or coupee or restantes < avant:
            calme_depuis = jour + 1
        elif (serie_max is not None and not nouvelles and len(vues) <= quota_revisions
              and jour + 1 - calme_depuis >= PLATEAU_JOURS
              and all(len(longues[i]) >= SERIES_PLATEAU and longues[i][-SERIES_PLATEAU] >= calme_depuis
                      for i in vues if i not in sues)):
            return None
    return None


def _cartes(qset, progress, now):
    aujourdhui = quiz.jour(now)
    cartes = []
    for q in qset.questions:
        st = progress.get(q.id)
        if st is None or st.seen == 0 and st.stabilite is None:
            cartes.append((None, 0, 0))
            continue
        if st.stabilite is not None:
            etat = fsrs.Etat(st.stabilite, st.difficulte if st.difficulte is not None else 5.0)
            echeance = st.echeance
        else:
            # Progression antérieure à FSRS : boîte de Leitner.
            etat = fsrs.Etat(quiz.STABILITE_LEITNER.get(st.box, 30.0), 5.0)
            echeance = st.last_review + timedelta(days=quiz.INTERVALLES_JOURS[st.box]) if st.last_review else None
        due = 0 if st.is_due(now) or echeance is None else max(1, (quiz.jour(echeance) - aujourdhui).days)
        cartes.append((etat, due, langues._jours_depuis(st, now)))
    return cartes


def precision(progress):
    reponses = sum(st.seen for st in progress.values())
    if reponses < REPONSES_MIN_PRECISION:
        return PRECISION_DEFAUT, False
    p = sum(st.correct for st in progress.values()) / reponses
    return min(PRECISION_MAX, max(PRECISION_MIN, p)), True


def estimer(qset, progress, deck, now):
    """Durée estimée pour tout savoir, et date à laquelle commencer pour l'examen."""
    aujourdhui = quiz.jour(now)
    cartes = _cartes(qset, progress, now)
    total = len(cartes)
    p, p_mesuree = precision(progress)
    secondes, s_mesure = deck.secondes_par_carte(SECONDES_DEFAUT, REPONSES_MIN_CHRONO)
    deja = faites_aujourdhui(progress, now)
    # La simulation garde la rétention standard : le resserrement avant l'examen
    # (retention_cible) joue sur la planification réelle, pas sur « quand saurai-je ».
    retention = retention_cible(deck.date_objectif, now)

    def duree(nouvelles, revisions):
        return simuler(cartes, nouvelles, revisions, p, deja)

    def par_jour(resultat):
        jours, reponses = resultat
        return round(reponses / jours) if jours else 0

    def minutes(resultat):
        jours, reponses = resultat
        return round(reponses / jours * secondes / 60) if jours else 0

    # Sans limite, toutes les questions peuvent passer le même jour.
    res = duree(deck.nouvelles_jour or total, deck.revisions_jour or total)
    e = {
        "nouvelles_jour": deck.nouvelles_jour,
        "revisions_jour": deck.revisions_jour,
        "auto": deck.auto_quota,
        "precision": round(p * 100),
        "precision_mesuree": p_mesuree,
        "secondes": round(secondes),
        "secondes_mesurees": s_mesure,
        "stabilite_objectif": int(STABILITE_OBJECTIF),
        "part_objectif": round(PART_OBJECTIF * 100),
        "retention": round(retention * 100),
        "su": res == (0, 0),
        "jours": res[0] if res else None,
        "fin": aujourdhui + timedelta(days=res[0] - 1) if res and res[0] else None,
        "questions_jour": par_jour(res) if res else None,
        "minutes_jour": minutes(res) if res else None,
        "objectif": deck.date_objectif,
    }
    if not deck.date_objectif or e["su"]:
        return e

    # Jours de révision disponibles : d'aujourd'hui à la veille de l'examen.
    dispo = (deck.date_objectif - aujourdhui).days
    e["jours_dispo"] = dispo
    if dispo <= 0:
        e["statut"] = "passee"
    elif res and res[0] <= dispo:
        debut = deck.date_objectif - timedelta(days=res[0])
        e["statut"] = "ok"
        e["debut"] = debut
        e["commencer_maintenant"] = debut <= aujourdhui
        e["marge"] = dispo - res[0]
    else:
        # Conseil à la Anki : ne pas plafonner les révisions, et chercher le plus
        # petit nombre de nouvelles questions par jour qui suffit (dichotomie).
        meilleur = duree(total, total)
        if not meilleur or meilleur[0] > dispo:
            # Même sans limite, pas le temps de tout savoir : le mieux est d'avoir vu toutes les
            # questions avant la veille de l'examen (gardée pour les révisions), révisions sans limite.
            e["statut"] = "impossible"
            e["jours_min"] = meilleur[0] if meilleur else None
            e["nouvelles_conseillees"] = nouvelles_avant_la_veille(qset, progress, dispo, deja[0])
            e["nouvelles_auto"] = (None if deck.nouvelles_jour is None
                                   else max(deck.nouvelles_jour, e["nouvelles_conseillees"]))
            e["revisions_limitees"] = bool(deck.revisions_jour)
            return e
        bas, haut = 1, total
        while bas < haut:
            milieu = (bas + haut) // 2
            r = duree(milieu, total)
            if r and r[0] <= dispo:
                haut, meilleur = milieu, r
            else:
                bas = milieu + 1
        e["statut"] = "retard"
        e["nouvelles_conseillees"] = e["nouvelles_auto"] = haut
        e["questions_conseillees"] = par_jour(meilleur)
        e["minutes_conseillees"] = minutes(meilleur)
        e["revisions_limitees"] = bool(deck.revisions_jour)
    return e


def nouvelles_avant_la_veille(qset, progress, dispo, deja_nouvelles=0):
    """Nouvelles questions par jour pour avoir vu tout le set avant la veille de l'examen (la veille
    reste pour les révisions ; examen demain : tout aujourd'hui). `dispo` : jours d'ici l'examen.
    Les nouvelles déjà découvertes aujourd'hui comptent dans le quota du jour : il ne baisse pas
    au fil de la journée."""
    a_voir = sum(1 for q in qset.questions if q.id not in progress) + deja_nouvelles
    return math.ceil(a_voir / max(1, dispo - 1))


def quotas_effectifs(qset, progress, deck, now):
    """Quotas du jour : ceux du set, ou, si l'ajustement automatique est coché et que
    l'estimation dit « en retard » pour l'examen, les nouvelles conseillées et des
    révisions sans limite. Quand l'examen est trop proche pour tout savoir (« impossible »),
    les nouvelles restantes sont étalées jusqu'à la veille de l'examen (au moins le quota du set),
    toujours sans limite de révisions. Renvoie (nouvelles_jour, revisions_jour)."""
    if deck is None:
        return None, None
    if deck.auto_quota and deck.date_objectif:
        e = estimer(qset, progress, deck, now)
        if e.get("statut") in ("retard", "impossible"):
            return e["nouvelles_auto"], None
    return deck.nouvelles_jour, deck.revisions_jour
