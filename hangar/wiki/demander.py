"""« Demander au wiki » : les extraits du coffre (pages et PDF) qui répondent le mieux à
une question, envoyés à Claude, puis les citations [n] de sa réponse changées en liens."""

import math
import re

from .vault import escape, fold

MAX_PAGES = 6
MAX_PAGES_PDF = 4
BUDGET = 40_000  # caractères d'extraits envoyés à Claude
MAX_CHARS_PAGE = 12_000
MAX_CHARS_PAGE_PDF = 4_000

MOTS_VIDES = {fold(m) for m in """
le la les un une des du de et ou en au aux est sont ete etre avoir avez avons ont ai as ce cet cette ces que qui quoi
quel quelle quels quelles dans par pour sur sous avec sans pas plus moins comme se sa son ses leur leurs il elle ils
elles on nous vous je tu me te moi toi quand comment pourquoi combien peut peux faut entre fait faire dit dire tout tous
toute toutes tres bien aussi donc mais car alors cela ceci ici quelle explique expliquer donne donner exemple
definition definir difference rappelle rappeler what the and how why
""".split()}


def mots_cles(texte, limite=20):
    """Mots porteurs de sens de la question (repliés, sans mots vides ni doublons)."""
    mots = [m for m in re.findall(r"\w+", fold(texte)) if len(m) >= 3 and m not in MOTS_VIDES and not m.isdigit()]
    return list(dict.fromkeys(mots))[:limite]


def pages_pertinentes(vault, mots, limite=MAX_PAGES):
    """Notes classées par pertinence : mots rares pesant plus lourd, titre compté triple."""
    if not mots:
        return []
    notes = list(vault.notes.values())
    total = len(notes) or 1
    idf = {m: math.log(1 + total / (1 + sum(1 for n in notes if m in n.folded_body or m in n.folded_title))) for m in mots}
    scores = []
    for note in notes:
        score = 0.0
        for m in mots:
            tf = note.folded_body.count(m)
            if m in note.folded_title:
                score += 3 * idf[m]
            if tf:
                score += idf[m] * (1 + math.log(tf))
        if score > 0:
            scores.append((score, note))
    scores.sort(key=lambda s: (-s[0], s[1].title.lower()))
    return [note for _, note in scores[:limite]]


def passages(texte, mots, budget):
    """Les paragraphes d'un texte qui parlent le plus des mots-clés, dans l'ordre du texte,
    jusqu'à ``budget`` caractères ; « […] » marque les coupures."""
    if len(texte) <= budget:
        return texte
    blocs = [b for b in re.split(r"\n\s*\n", texte) if b.strip()]
    notes = sorted(((sum(fold(b).count(m) for m in mots), i) for i, b in enumerate(blocs)), key=lambda s: (-s[0], s[1]))
    choisis, total = set(), 0
    for score, i in notes:
        if score == 0:
            break
        bloc = blocs[i][:budget]
        if total + len(bloc) > budget:
            continue
        choisis.add(i)
        total += len(bloc) + 2
    if not choisis:
        return texte[:budget]
    morceaux, precedent = [], None
    for i in sorted(choisis):
        if precedent is not None and i != precedent + 1:
            morceaux.append("[…]")
        morceaux.append(blocs[i][:budget])
        precedent = i
    return "\n\n".join(morceaux)


def rassembler(vault, question, precedentes=()):
    """Extraits numérotés pour la question : [{titre, url, type, texte}] (pages puis PDF)."""
    from . import texte_pdf

    mots = mots_cles(" ".join([question, *precedentes]))
    notes = pages_pertinentes(vault, mots)
    pdfs = texte_pdf.pages_pertinentes(vault, mots, MAX_PAGES_PDF)
    if not notes and not pdfs:
        return []
    part = min(MAX_CHARS_PAGE, BUDGET // (len(notes) + len(pdfs)))
    sources = [{"titre": note.title, "url": note.url, "type": "page", "texte": passages(note.body, mots, part)}
               for note in notes]
    sources += [{"titre": f"{pdf.path.stem} (PDF, page {n})", "url": f"{pdf.view_url}#page={n}", "type": "pdf",
                 "texte": texte[:min(part, MAX_CHARS_PAGE_PDF)]}
                for pdf, n, texte in pdfs]
    return sources


_PROTEGE_RE = re.compile(r'(<(pre|code)\b.*?</\2>|<(span|div) class="math[^"]*">.*?</\3>)', re.S)
_CITATION_RE = re.compile(r"\[(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)\]")


def lier_citations(html, sources):
    """Les « [2] » ou « [1, 3] » de la réponse deviennent des liens vers les sources.
    Renvoie (html, numéros cités). Code et formules restent intacts."""
    cites = set()

    def lien(m):
        numeros = [int(x) for x in re.findall(r"\d+", m.group(1))]
        if not all(1 <= n <= len(sources) for n in numeros):
            return m.group(0)
        cites.update(numeros)
        return "".join(f'<a class="citation" href="{escape(sources[n - 1]["url"])}" title="{escape(sources[n - 1]["titre"])}">[{n}]</a>'
                       for n in numeros)

    morceaux = _PROTEGE_RE.split(html)
    # split() avec groupes : [texte, bloc, groupe 2, groupe 3, texte, …] ; seul le texte est modifié.
    sortie, i = [], 0
    while i < len(morceaux):
        sortie.append(_CITATION_RE.sub(lien, morceaux[i]))
        if i + 1 < len(morceaux):
            sortie.append(morceaux[i + 1])
        i += 4
    return "".join(sortie), sorted(cites)
