"""Conversion d'un document LaTeX (un cours) en note Markdown façon Obsidian.

Repris : chapitres et sections (numérotés comme dans le PDF), paragraphes,
gras, italique, listes (étiquettes (i), (a), « Phase 1. »... comprises),
tableaux, formules (les macros \\newcommand sont développées pour KaTeX, les
équations numérotées gardent leur numéro), théorèmes, définitions...
(\\newtheorem) et encadrés (tcolorbox) en callouts, démonstrations repliées,
renvois \\ref, notes de bas de page.

Les figures dessinées en LaTeX (TikZ : circuits, chronogrammes, tableaux de
Karnaugh...) ne peuvent pas être affichées par le wiki : leur légende est
gardée et leur code source rangé dans un encadré replié.
"""

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass

DRAWING_ENVS = {
    "tikzpicture": "Figure", "circuitikz": "Schéma électrique", "tikztimingtable": "Chronogramme",
    "karnaugh-map": "Tableau de Karnaugh", "pgfpicture": "Figure", "tikzcd": "Diagramme",
    "forest": "Arbre", "pspicture": "Figure", "axis": "Graphique", "chemfig": "Formule chimique",
}
MATH_ENVS = {
    "equation", "equation*", "align", "align*", "gather", "gather*", "multline", "multline*",
    "eqnarray", "eqnarray*", "displaymath", "flalign", "flalign*", "alignat", "alignat*",
}
TABLE_ENVS = {"tabular", "tabular*", "tabularx", "tabulary", "longtable", "supertabular"}
LIST_ENVS = {"itemize", "enumerate", "description", "compactitem", "compactenum", "asparaenum", "inparaenum"}
FLOAT_ENVS = {"figure", "figure*", "table", "table*", "wrapfigure", "wraptable", "subfigure", "SCfigure"}
VERBATIM_ENVS = ("verbatim", "verbatim*", "Verbatim", "lstlisting", "minted")
SECTION_LEVELS = {"part": 1, "chapter": 1, "section": 2, "subsection": 3, "subsubsection": 4}

# Type de callout selon le nom de l'environnement (couleurs dans wiki.css).
CALLOUT_KINDS = [
    (("definition", "notation", "vocabulaire", "convention"), "definition"),
    (("theoreme", "theorem", "proposition", "lemme", "lemma", "corollaire", "corollary", "propriete", "loi", "principe"), "theoreme"),
    (("remarque", "remark", "attention", "nota", "rappel"), "remarque"),
    (("exemple", "example", "application"), "exemple"),
    (("methode", "method", "algorithme", "procedure", "recette"), "methode"),
    (("exercice", "exercise", "probleme", "question"), "exercice"),
]

SYMBOLS = {
    "ldots": "…", "dots": "…", "textellipsis": "…", "textendash": "–", "textemdash": "—", "LaTeX": "LaTeX",
    "TeX": "TeX", "og": "« ", "fg": " »", "S": "§", "textbackslash": "\\\\", "textasciitilde": "~",
    "textdegree": "°", "degree": "°", "euro": "€", "texteuro": "€", "copyright": "©", "dag": "†", "ss": "ß",
    "oe": "œ", "OE": "Œ", "ae": "æ", "AE": "Æ", "o": "ø", "O": "Ø", "guillemotleft": "«",
    "guillemotright": "»", "textquoteleft": "‘", "textquoteright": "’", "quad": " ", "qquad": "  ",
    "enspace": " ", "thinspace": "\u202f", "space": " ", "times": "×", "checkmark": "✓",
    "textbullet": "•", "nobreakspace": "\u00a0", "slash": "/", "textunderscore": "\\_", "textbar": "|",
    "textless": "<", "textgreater": ">", "newline": "\ue020", "linebreak": "\ue020", "par": "\ue020",
}
# Commandes de mise en page sans contenu (leurs arguments sont ignorés).
DROP = {
    "label": 1, "index": 1, "hspace": 1, "vspace": 1, "phantom": 1, "hphantom": 1, "vphantom": 1, "rule": 2,
    "setlength": 2, "addtolength": 2, "setcounter": 2, "addtocounter": 2, "stepcounter": 1, "refstepcounter": 1,
    "pagestyle": 1, "thispagestyle": 1, "pagenumbering": 1, "markboth": 2, "markright": 1, "addcontentsline": 3,
    "fontsize": 2, "color": 1, "linespread": 1, "input": 1, "include": 1, "bibliographystyle": 1,
    "bibliography": 1, "nocite": 1, "hypersetup": 1, "definecolor": 3, "usepackage": 1, "graphicspath": 1,
    "ctikzset": 1, "tikzset": 1, "usetikzlibrary": 1, "setlist": 1, "arrayrulecolor": 1, "captionsetup": 1,
    "newcounter": 1, "fancyhead": 1, "fancyfoot": 1, "fancyhf": 1, "fancypagestyle": 2, "titlespacing": 5,
    "titleformat": -1, "theoremstyle": 1, "tcolorboxenvironment": 2, "documentclass": 1, "title": 1,
    "author": 1, "date": 1, "setmainfont": 1, "setsansfont": 1, "setmonofont": 1, "setmathfont": 1,
    "setdefaultlanguage": 1, "setotherlanguage": 1, "newtheoremstyle": 9, "selectlanguage": 1,
    "vskip": 0, "noindent": 0, "centering": 0, "raggedright": 0, "raggedleft": 0, "clearpage": 0,
    "cleardoublepage": 0, "newpage": 0, "pagebreak": 0, "smallskip": 0, "medskip": 0, "bigskip": 0,
    "vfill": 0, "hfill": 0, "maketitle": 0, "tableofcontents": 0, "listoffigures": 0, "listoftables": 0,
    "selectfont": 0, "normalfont": 0, "bfseries": 0, "itshape": 0, "upshape": 0, "scshape": 0, "slshape": 0,
    "mdseries": 0, "rmfamily": 0, "sffamily": 0, "ttfamily": 0, "em": 0, "bf": 0, "it": 0, "tiny": 0,
    "scriptsize": 0, "footnotesize": 0, "small": 0, "normalsize": 0, "large": 0, "Large": 0, "LARGE": 0,
    "huge": 0, "Huge": 0, "frenchspacing": 0, "protect": 0, "relax": 0, "allowbreak": 0, "nopagebreak": 0,
    "printbibliography": 0, "frontmatter": 0, "mainmatter": 0, "backmatter": 0, "arraybackslash": 0,
    "strut": 0, "break": 0, "leavevmode": 0, "indent": 0, "makeatletter": 0, "makeatother": 0,
}
TRANSPARENT = {
    "textsc", "textup", "textrm", "textsf", "textnormal", "textmd", "mbox", "hbox", "text", "underline",
    "uline", "fbox", "framebox", "makebox", "textsuperscript", "textsubscript", "MakeUppercase",
    "MakeLowercase", "foreignlanguage", "textcolor", "colorbox", "raisebox", "enquote", "emph", "textit",
    "textsl", "textbf", "texttt", "url", "href",
}
ACCENTS = {"'": "\u0301", "`": "\u0300", "^": "\u0302", '"': "\u0308", "~": "\u0303", "=": "\u0304",
           ".": "\u0307", "c": "\u0327", "u": "\u0306", "v": "\u030c", "H": "\u030b", "k": "\u0328", "r": "\u030a"}

BLOCK_TOKEN = re.compile(
    r"\\begin\s*\{(?P<env>[^}]+)\}"
    r"|\\(?P<sec>part|chapter|section|subsection|subsubsection|paragraph|subparagraph)(?![A-Za-z@])(?P<star>\*?)"
    r"|(?P<dmath>(?<!\\)\\\[|(?<!\\)\$\$)"
    r"|(?P<imath>(?<!\\)\$|(?<!\\)\\\()"
    r"|(?P<blank>\n[ \t]*\n)"
    r"|\\(?P<wrap>resizebox|scalebox|rotatebox|adjustbox)(?![A-Za-z@])\*?"
    r"|\\(?P<skip>titleformat|titlespacing|titlecontents|titleclass|fancypagestyle|addcontentsline|hypersetup|setcounter)(?![A-Za-z@])\*?"
    r"|\\(?P<def>newcommand|renewcommand|providecommand|DeclareRobustCommand|DeclareMathOperator|def"
    r"|newtheorem|newtcolorbox|renewtcolorbox|newenvironment|renewenvironment|appendix|caption|footnotetext)(?![A-Za-z@])(?P<defstar>\*?)"
)
CMD = re.compile(r"\\([A-Za-z@]+)(\*?)|\\(.)", re.S)
DIMENSION = re.compile(r"^\s*\[\s*-?[\d.]*\s*(pt|em|ex|mm|cm|in|bp|mu|sp|pc|dd)?\s*\]")


@dataclass
class Macro:
    nargs: int
    default: str | None
    body: str


@dataclass
class Theorem:
    title: str
    counter: str
    parent: str | None
    numbered: bool


def fold(text):
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if not unicodedata.combining(c))


def skip_spaces(s, i, newlines=True):
    while i < len(s) and (s[i] in " \t" or (newlines and s[i] == "\n")):
        i += 1
    return i


def read_group(s, i):
    """Argument ``{...}`` (accolades équilibrées) à partir de ``i`` : (contenu, fin) ou (None, i)."""
    j = skip_spaces(s, i)
    if j >= len(s) or s[j] != "{":
        return None, i
    depth, k = 0, j
    while k < len(s):
        c = s[k]
        if c == "\\":
            k += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[j + 1:k], k + 1
        k += 1
    return s[j + 1:], len(s)


def read_optional(s, i):
    """Argument optionnel ``[...]`` : (contenu, fin) ou (None, i)."""
    j = skip_spaces(s, i, newlines=False)
    if j >= len(s) or s[j] != "[":
        return None, i
    depth, brackets, k = 0, 0, j
    while k < len(s):
        c = s[k]
        if c == "\\":
            k += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "[" and depth == 0:
            brackets += 1
        elif c == "]" and depth == 0:
            brackets -= 1
            if brackets == 0:
                return s[j + 1:k], k + 1
        k += 1
    return None, i


def read_arg(s, i):
    """Argument d'une macro : ``{...}``, une commande ou un caractère."""
    group, j = read_group(s, i)
    if group is not None:
        return group, j
    j = skip_spaces(s, i)
    m = CMD.match(s, j)
    if m:
        return m.group(0), m.end()
    return (s[j], j + 1) if j < len(s) else ("", j)


def find_end(s, name, i):
    """Position du ``\\end{name}`` qui ferme l'environnement ouvert avant ``i``."""
    depth = 1
    for m in re.finditer(r"\\(begin|end)\s*\{" + re.escape(name) + r"\}", s[i:]):
        depth += 1 if m.group(1) == "begin" else -1
        if depth == 0:
            return i + m.start(), i + m.end()
    return len(s), len(s)


def split_top(s, sep):
    """Découpe ``s`` sur ``sep`` (« \\\\ », « & » ou « \\item ») hors accolades, formules et sous-environnements."""
    parts, depth, env, math, start, i = [], 0, 0, False, 0, 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            if s.startswith(sep, i) and sep != "&" and depth == 0 and env == 0 and not math \
                    and (sep != "\\item" or not s[i + 5:i + 6].isalpha()):
                parts.append(s[start:i])
                i += len(sep)
                start = i
                continue
            if s.startswith("\\begin", i):
                env += 1
            elif s.startswith("\\end", i):
                env -= 1
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "$":
            math = not math
        elif c == "&" and sep == "&" and depth == 0 and env == 0 and not math:
            parts.append(s[start:i])
            start = i + 1
        i += 1
    parts.append(s[start:])
    return parts


def unescaped_percent(line):
    for m in re.finditer("%", line):
        k = m.start()
        while k > 0 and line[k - 1] == "\\":
            k -= 1
        if (m.start() - k) % 2 == 0:
            return m.start()
    return None


def strip_comments(s):
    """Retire les commentaires ``%`` comme LaTeX : une ligne entièrement commentée
    disparaît, et un ``%`` en fin de ligne absorbe le saut de ligne."""
    out, joining = [], False
    for line in s.split("\n"):
        cut = unescaped_percent(line)
        text = line if cut is None else line[:cut]
        if cut is not None and not text.strip():
            continue
        if joining and out:
            out[-1] += text.lstrip(" \t")
        else:
            out.append(text)
        joining = cut is not None
    return "\n".join(out)


def roman(n):
    out = ""
    for value, letters in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
                           (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= value:
            out += letters
            n -= value
    return out


class Converter:
    def __init__(self, source):
        self.source = source
        self.macros = {}
        self.theorems = {}
        self.boxes = {}
        self.counters = defaultdict(int)
        self.labels = {}
        self.target = None  # (numéro, titre de section ou None) que désigne le prochain \label
        self.footnotes = []
        self.appendix = False
        self.has_chapters = False
        self.run_in = None
        self.drawings = None
        self.figure_count = 0
        self.proofname = "Démonstration"
        self.verbatim = []

    # -- Entrée ----------------------------------------------------------------------

    def convert(self):
        text = self.source.replace("\r\n", "\n").lstrip("\ufeff").strip()
        # Document collé entre $$ ... $$ par erreur.
        if text.startswith("$$") and text.endswith("$$") and re.search(r"\\(documentclass|begin\s*\{document\}|chapter|section)\b", text):
            text = text[2:-2].strip()
        text = self.protect_verbatim(text)
        text = strip_comments(text)
        start = re.search(r"\\begin\s*\{document\}", text)
        if start:
            preamble, body = text[:start.start()], text[start.end():]
            end = re.search(r"\\end\s*\{document\}", body)
            body = body[:end.start()] if end else body
        else:
            preamble, body = "", text
        self.harvest(preamble)
        self.harvest(body)
        body = self.expand(body, structural=True)
        self.has_chapters = bool(re.search(r"\\chapter\b", body))
        markdown = self.blocks(body)
        markdown = self.resolve_refs(markdown)
        if self.footnotes:
            markdown += "\n\n" + "\n".join(f"[^{n}]: {text}" for n, text in enumerate(self.footnotes, 1) if text)
        if self.figure_count:
            several = self.figure_count > 1
            markdown = (
                "> [!info] Converti depuis LaTeX\n"
                f"> {'Les' if several else 'La'} {self.figure_count} figure{'s' if several else ''} dessinée{'s' if several else ''} "
                "en LaTeX (TikZ : circuits, chronogrammes, tableaux de Karnaugh…) ne peu"
                f"{'vent' if several else 't'} pas être affichée{'s' if several else ''} par le wiki : "
                "la légende est gardée et le code source rangé dans un encadré replié.\n\n" + markdown
            )
        markdown = self.restore_verbatim(markdown)
        return re.sub(r"\n{3,}", "\n\n", markdown).strip() + "\n"

    def protect_verbatim(self, text):
        def keep(code, inline=False):
            self.verbatim.append((code, inline))
            return f"\ue010{len(self.verbatim) - 1}\ue011"

        names = "|".join(re.escape(n) for n in VERBATIM_ENVS)
        text = re.sub(r"\\begin\{(" + names + r")\}(?:\[[^\]]*\])?(?:\{[^}]*\})?\n?(.*?)\\end\{\1\}",
                      lambda m: "\n\n" + keep(m.group(2).rstrip("\n")) + "\n\n", text, flags=re.S)
        return re.sub(r"\\verb\*?(.)(.*?)\1", lambda m: keep(m.group(2), inline=True), text)

    def restore_verbatim(self, text):
        def back(m):
            code, inline = self.verbatim[int(m.group(1))]
            return f"`{code}`" if inline else f"```\n{code}\n```"

        return re.sub(r"\ue010(\d+)\ue011", back, text)

    def harvest(self, preamble):
        """Macros, théorèmes et encadrés définis dans le préambule."""
        for m in re.finditer(r"\\(newcommand|renewcommand|providecommand|DeclareRobustCommand|DeclareMathOperator"
                             r"|def|newtheorem|newtcolorbox|renewtcolorbox|newenvironment|renewenvironment)(?![A-Za-z@])(\*?)",
                             preamble):
            try:
                self.define(m.group(1), m.group(2), preamble, m.end())
            except (ValueError, IndexError):
                continue

    # -- Définitions ------------------------------------------------------------------

    def define(self, kind, star, s, i):
        if kind in ("newcommand", "renewcommand", "providecommand", "DeclareRobustCommand"):
            j = skip_spaces(s, i)
            if j < len(s) and s[j] == "{":
                name, j = read_group(s, j)
            else:
                m = CMD.match(s, j)
                if not m:
                    return i
                name, j = m.group(0), m.end()
            nargs, j = read_optional(s, j)
            default, j = read_optional(s, j)
            body, j = read_group(s, j)
            name = (name or "").strip().lstrip("\\")
            if name and body is not None:
                self.macros[name] = Macro(int(nargs) if nargs and nargs.strip().isdigit() else 0, default, body)
                if name == "proofname":
                    self.proofname = self.inline(body)
            return j
        if kind == "DeclareMathOperator":
            name, j = read_group(s, i)
            body, j = read_group(s, j)
            if name and body is not None:
                self.macros[name.strip().lstrip("\\")] = Macro(0, None, "\\operatorname{" + body + "}")
            return j
        if kind == "def":
            m = re.compile(r"\s*\\([A-Za-z@]+)((?:#\d)*)").match(s, i)
            if not m:
                return i
            body, j = read_group(s, m.end())
            if body is not None:
                self.macros[m.group(1)] = Macro(len(m.group(2)) // 2, None, body)
            return j
        if kind == "newtheorem":
            env, j = read_group(s, i)
            shared, j = read_optional(s, j)
            title, j = read_group(s, j)
            parent, j = read_optional(s, j)
            if env and title is not None:
                env = env.strip()
                self.theorems[env] = Theorem(self.inline(title), (shared or env).strip(), (parent or "").strip() or None, not star)
            return j
        if kind in ("newtcolorbox", "renewtcolorbox", "newenvironment", "renewenvironment"):
            name, j = read_group(s, i)
            _, j = read_optional(s, j)
            default, j = read_optional(s, j)
            while True:
                group, k = read_group(s, j)
                if group is None:
                    break
                j = k
            if name and kind.endswith("tcolorbox"):
                self.boxes[name.strip()] = self.inline(default) if default else name.strip().capitalize()
            return j
        return i

    def expand(self, s, depth=0, structural=False):
        """Développe les macros de l'utilisateur (\\newcommand) dans ``s`` ; avec
        ``structural``, seulement celles qui contiennent des environnements ou des titres."""
        if not self.macros or depth > 30:
            return s
        out, i = [], 0
        for m in re.finditer(r"\\\\|\\([A-Za-z@]+)", s):
            if m.start() < i:
                continue
            name = m.group(1)
            macro = self.macros.get(name) if name else None
            if macro is not None and re.search(r"\\(?:(?:re)?newcommand|providecommand|DeclareRobustCommand)\*?\s*\{?\s*$|\\def\s*$",
                                               s[max(0, m.start() - 30):m.start()]):
                continue
            if macro is None or (structural and not re.search(r"\\(begin|chapter|section|subsection|item)\b", macro.body)):
                continue
            out.append(s[i:m.start()])
            j, args = m.end(), []
            if macro.default is not None:
                optional, j = read_optional(s, j)
                args.append(optional if optional is not None else macro.default)
            while len(args) < macro.nargs:
                arg, j = read_arg(s, j)
                args.append(arg)
            body = macro.body
            for n, arg in enumerate(args, 1):
                body = body.replace(f"#{n}", arg)
            # Une macro suivie d'une lettre (« \nQ_t ») reste séparée.
            out.append(self.expand(body, depth + 1, structural) + (" " if j < len(s) and s[j].isalpha() else ""))
            i = j
        out.append(s[i:])
        return "".join(out)

    # -- Numérotation et renvois -----------------------------------------------------------

    def chapter_number(self):
        n = self.counters["chapter"]
        return chr(64 + n) if self.appendix and 0 < n <= 26 else str(n)

    def parent_number(self, parent):
        if parent == "chapter":
            return self.chapter_number()
        if parent == "section":
            return f"{self.chapter_number()}.{self.counters['section']}" if self.has_chapters else str(self.counters["section"])
        return ""

    def next_number(self, counter, parent):
        self.counters[counter] += 1
        prefix = self.parent_number(parent)
        return f"{prefix}.{self.counters[counter]}" if prefix else str(self.counters[counter])

    def reset_after(self, level):
        order = ["chapter", "section", "subsection", "subsubsection"]
        if level in order:
            for name in order[order.index(level) + 1:]:
                self.counters[name] = 0
        if level == "chapter" and self.has_chapters:
            for name in ("figure", "table", "equation"):
                self.counters[name] = 0
        for theorem in self.theorems.values():
            if theorem.parent == level:
                self.counters[theorem.counter] = 0

    def label(self, name):
        if self.target is not None:
            self.labels[name.strip()] = self.target

    def resolve_refs(self, text):
        def ref(m):
            plain, name, kind = m.group(1) == "\ue002", m.group(2).strip(), m.group(3)
            number, heading = self.labels.get(name, ("??", None))
            number = f"({number})" if kind == "eq" else number
            if plain or not heading:
                return number
            heading = re.sub(r"[\[\]|#^]", "", heading)
            return f"[[#{heading}|{number}]]"

        return re.sub(r"([\ue000\ue002])([^\ue001]*)\ue001(\w*)\ue003", ref, text)

    # -- Blocs ------------------------------------------------------------------------------

    def blocks(self, s):
        out, para, i = [], [], 0

        def flush():
            text = "".join(para)
            para.clear()
            md = self.paragraph(text)
            if md and self.run_in:
                md = f"**{self.run_in}** {md}"
                self.run_in = None
            if md:
                out.append(md)

        def add(md):
            if self.run_in:
                out.append(f"**{self.run_in}**")
                self.run_in = None
            if md:
                out.append(md)

        while i < len(s):
            m = BLOCK_TOKEN.search(s, i)
            if not m:
                para.append(s[i:])
                break
            para.append(s[i:m.start()])
            i = m.end()
            if m.group("blank"):
                flush()
            elif m.group("imath"):
                close = "\\)" if m.group("imath") == "\\(" else "$"
                j = i
                while True:
                    j = s.find(close, j)
                    if j < 0 or s[j - 1] != "\\":
                        break
                    j += 1
                j = len(s) if j < 0 else j
                para.append(s[m.start():j + len(close)])
                i = j + len(close)
            elif m.group("env"):
                env = m.group("env").strip()
                end, after = find_end(s, env, i)
                content, i = s[i:end], after
                flush()
                add(self.environment(env, content))
            elif m.group("sec"):
                optional, i = read_optional(s, i)
                title, i = read_group(s, i)
                flush()
                md = self.heading(m.group("sec"), bool(m.group("star")), title or "")
                if md:
                    out.append(md)
            elif m.group("dmath"):
                close = "\\]" if m.group("dmath") == "\\[" else "$$"
                j = s.find(close, i)
                j = len(s) if j < 0 else j
                content, i = s[i:j], j + len(close)
                flush()
                add(self.display_math(content))
            elif m.group("wrap"):
                command = m.group("wrap")
                _, i = read_optional(s, i)
                for _ in range({"resizebox": 2, "scalebox": 1, "rotatebox": 1, "adjustbox": 1}[command]):
                    _, i = read_group(s, i)
                    _, i = read_optional(s, i)
                content, i = read_group(s, i)
                flush()
                add(self.blocks(content or ""))
            elif m.group("skip"):
                while True:
                    group, j = read_group(s, i)
                    if group is None:
                        group, j = read_optional(s, i)
                    if group is None:
                        break
                    i = j
            elif m.group("def"):
                kind = m.group("def")
                if kind == "appendix":
                    self.appendix = True
                    self.counters["chapter"] = 0
                elif kind == "caption":
                    _, i = read_optional(s, i)
                    caption, i = read_group(s, i)
                    para.append(caption or "")
                elif kind == "footnotetext":
                    _, i = read_optional(s, i)
                    note, i = read_group(s, i)
                    if self.footnotes and not self.footnotes[-1]:
                        self.footnotes[-1] = self.paragraph(note or "")
                else:
                    i = self.define(kind, m.group("defstar"), s, i)
        flush()
        add("")
        return "\n\n".join(out)

    def paragraph(self, text):
        md = self.inline(text)
        md = re.sub(r"[ \t\u00a0]*\ue020[ \t]*", "\ue020", md)
        md = re.sub(r"[ \t]{2,}", " ", md).strip(" \t\ue020")
        md = md.replace("\ue020", "\n")
        # Pas de liste, de titre ni de citation involontaire en début de ligne.
        return "\n".join(re.sub(r"^(\s*)([>+\-*]|\d+[.)])(?=\s)", r"\1\\\2", line) for line in md.split("\n")).strip()

    def heading(self, command, star, title_tex):
        title = self.paragraph(title_tex).replace("\n", " ")
        if command in ("paragraph", "subparagraph"):
            self.run_in = title
            return ""
        level = SECTION_LEVELS[command]
        if not self.has_chapters and command != "part":
            level = max(level, 2)
        if star or command == "part":
            self.target = None
            return "#" * level + " " + title
        if command == "chapter":
            self.counters["chapter"] += 1
            self.reset_after("chapter")
            number = self.chapter_number()
            text = f"{'Annexe' if self.appendix else 'Chapitre'} {number} — {title}"
        else:
            self.counters[command] += 1
            self.reset_after(command)
            parts = [self.chapter_number()] if self.has_chapters else []
            for name in ("section", "subsection", "subsubsection"):
                parts.append(str(self.counters[name]))
                if name == command:
                    break
            number = ".".join(parts)
            text = f"{number} {title}"
        self.target = (number, text)
        return "#" * level + " " + text

    def environment(self, env, content):
        base = env.rstrip("*")
        try:
            if env == "document":
                return self.blocks(content)
            if env == "titlepage":
                return self.titlepage(content)
            if base in LIST_ENVS:
                return self.list_env(base, content)
            if env in MATH_ENVS or base == "math":
                return self.display_math(content, env)
            if env in TABLE_ENVS:
                return self.table(env, content)
            if env in FLOAT_ENVS:
                return self.float_env(base, content)
            if env in DRAWING_ENVS or base in DRAWING_ENVS:
                return self.drawing(env, content)
            if env in self.theorems or base == "proof":
                return self.theorem(env, content)
            if env in self.boxes or base in ("tcolorbox", "mdframed", "framed", "shaded"):
                return self.box(env, content)
            if base in ("quote", "quotation", "verse"):
                return self.callout_lines(self.blocks(content), prefix="> ")
            if base == "abstract":
                return self.callout("abstract", "Résumé", self.blocks(content))
            if base in ("minipage", "multicols", "otherlanguage", "adjustbox"):
                _, content = self.skip_args(content, optional=True)
                if base == "minipage":
                    _, content = self.skip_args(content)
            return self.blocks(content)
        except (RecursionError, ValueError, IndexError):
            return f"```latex\n\\begin{{{env}}}{content}\\end{{{env}}}\n```"

    def skip_args(self, content, optional=False):
        value, j = (read_optional if optional else read_group)(content, 0)
        return value, content[j:]

    def callout(self, kind, title, body, folded=False):
        head = f"> [!{kind}]{'-' if folded else ''} {title}".rstrip()
        return head + ("\n" + self.callout_lines(body) if body.strip() else "")

    @staticmethod
    def callout_lines(body, prefix="> "):
        return "\n".join(prefix + line if line.strip() else prefix.rstrip() for line in body.split("\n"))

    def titlepage(self, content):
        lines = []
        for piece in re.split(r"\\par\b|\\\\(?:\[[^\]]*\])?|\n[ \t]*\n", content):
            md = self.paragraph(piece).replace("\n", " ").strip()
            if md and md not in lines:
                lines.append(md)
        return self.callout("info", "Page de titre", "\n".join(lines)) if lines else ""

    def theorem(self, env, content):
        title_tex, rest = self.skip_args(content, optional=True)
        extra = f" ({self.paragraph(title_tex)})" if title_tex else ""
        if env.rstrip("*") == "proof":
            return self.callout("demonstration", self.paragraph(title_tex) if title_tex else self.proofname, self.blocks(rest), folded=True)
        spec = self.theorems[env]
        owner = self.theorems.get(spec.counter, spec)  # \\newtheorem{proposition}[definition]{...}
        title = spec.title
        saved = self.target
        if spec.numbered:
            number = self.next_number(spec.counter, owner.parent)
            title = f"{title} {number}"
            self.target = (number, None)
        body = self.blocks(rest)
        self.target = saved
        kind = next((k for names, k in CALLOUT_KINDS if fold(env).startswith(names) or fold(spec.title).startswith(names)), "note")
        return self.callout(kind, title + extra, body)

    def box(self, env, content):
        title_tex, rest = self.skip_args(content, optional=True)
        title = self.paragraph(title_tex) if title_tex else self.boxes.get(env, "")
        return self.callout("retenir" if fold(title).startswith("a retenir") or env == "retenir" else "note", title, self.blocks(rest))

    # -- Listes --------------------------------------------------------------------------

    def list_env(self, kind, content):
        options, content = self.skip_args(content, optional=True)
        template = None
        if kind == "enumerate" and options:
            for option in split_top(options, "&")[0].split(","):
                option = option.strip()
                if option.startswith("label="):
                    template = option[6:].strip()
                    break
                if "=" not in option and option:
                    template = option
                    break
        items = split_top(content, "\\item")[1:]
        lines = []
        for n, item in enumerate(items, 1):
            label_tex, body = self.skip_args(item, optional=True)
            body_md = self.blocks(body).strip()
            if label_tex is not None:
                marker = f"- **{self.paragraph(label_tex)}** "
            elif kind == "enumerate" and template:
                label = self.enum_label(template, n)
                marker = f"- {label} " if label.startswith("**") else f"- **{label}** "
            elif kind == "enumerate":
                marker = f"{n}. "
            else:
                marker = "- "
            first, *rest = body_md.split("\n") if body_md else [""]
            lines.append((marker + first).rstrip())
            lines.extend("    " + line if line.strip() else "" for line in rest)
        return "\n".join(lines)

    def enum_label(self, template, n):
        counters = {"\\arabic*": str(n), "\\alph*": chr(96 + n), "\\Alph*": chr(64 + n),
                    "\\roman*": roman(n), "\\Roman*": roman(n).upper()}
        for key, value in counters.items():
            if key in template:
                return self.paragraph(template.replace(key, value))
        # Étiquettes courtes d'enumitem : « (i) », « a) », « 1. »...
        m = re.search(r"(?<![\\A-Za-z])([1aAiI])(?![A-Za-z])", template)
        if not m:
            return self.paragraph(template)
        value = {"1": str(n), "a": chr(96 + n), "A": chr(64 + n), "i": roman(n), "I": roman(n).upper()}[m.group(1)]
        return self.paragraph(template[:m.start()] + value + template[m.end():])

    # -- Formules ------------------------------------------------------------------------

    def math(self, s, table=False):
        s = self.expand(s)
        for name in re.findall(r"\\label\s*\{([^}]*)\}", s):
            self.label(name)
        s = re.sub(r"\\label\s*\{[^}]*\}|\\(nonumber|notag)\b", "", s)
        s = re.sub(r"\\mbox\b", r"\\text", s).replace("\\ensuremath", "")
        if table:
            s = s.replace("\\|", "\\Vert ").replace("|", "\\vert ")
        return re.sub(r"\s*\n\s*", " ", s).strip() if not table else re.sub(r"\s+", " ", s).strip()

    def display_math(self, content, env=None):
        base = (env or "").rstrip("*")
        numbered = env in ("equation", "align", "gather", "multline", "eqnarray", "flalign", "alignat")
        if base == "alignat":
            _, content = self.skip_args(content)
        if base in ("align", "eqnarray", "flalign", "alignat"):
            content = "\\begin{aligned}" + content.replace("&=&", "&=") + "\\end{aligned}"
        elif base in ("gather", "multline"):
            content = "\\begin{gathered}" + content + "\\end{gathered}"
        saved = self.target
        tag = ""
        if numbered:
            number = self.next_number("equation", "chapter" if self.has_chapters else None)
            self.target = (number, None)
            tag = f" \\tag{{{number}}}"
        lines = [line.strip() for line in content.split("\n")]
        body = self.math("\n".join(line for line in lines if line))
        self.target = saved
        return f"$$\n{body}{tag}\n$$" if body else ""

    # -- Tableaux ------------------------------------------------------------------------

    def table(self, env, content):
        if env in ("tabular*", "tabularx", "tabulary"):
            _, content = self.skip_args(content)
        _, content = self.skip_args(content, optional=True)
        _, content = self.skip_args(content)
        rows = []
        for row in split_top(content.replace("\\tabularnewline", "\\\\"), "\\\\"):
            row = DIMENSION.sub("", row)
            row = re.sub(r"\\(hline|toprule|midrule|bottomrule|endhead|endfirsthead|endfoot|endlastfoot)\b", "", row)
            row = re.sub(r"\\(cline|cmidrule)(\([^)]*\))?\s*\{[^}]*\}|\\(rowcolor|cellcolor|arrayrulecolor)\s*(\[[^\]]*\])?\s*\{[^}]*\}"
                         r"|\\addlinespace(\[[^\]]*\])?|\\hhline\{[^}]*\}", "", row)
            if not row.strip():
                continue
            cells = []
            for cell in split_top(row, "&"):
                span = 1
                m = re.match(r"\s*\\multicolumn\s*\{(\d+)\}", cell)
                if m:
                    span = int(m.group(1))
                    _, j = read_group(cell, m.end())
                    text, _ = read_group(cell, j)
                    cell = text or ""
                m = re.match(r"\s*\\multirow\s*(\[[^\]]*\])?\s*\{[^}]*\}\s*(\[[^\]]*\])?\s*\{[^}]*\}", cell)
                if m:
                    text, _ = read_group(cell, m.end())
                    cell = text or ""
                cells.append(self.cell(cell))
                cells.extend([""] * (span - 1))
            rows.append(cells)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(lines)

    def cell(self, tex):
        md = self.inline(tex, table=True)
        md = md.replace("\ue020", " ")
        return re.sub(r"\s+", " ", md).strip()

    # -- Figures -------------------------------------------------------------------------

    def float_env(self, kind, content):
        _, content = self.skip_args(content, optional=True)
        if kind in ("wrapfigure", "wraptable"):
            _, content = self.skip_args(content)
        caption = None
        m = re.search(r"\\caption(?:of\s*\{[^}]*\})?\s*(\[[^\]]*\])?\s*\{", content)
        if m:
            text, end = read_group(content, m.end() - 1)
            caption = self.paragraph(text or "").replace("\n", " ")
            content = content[:m.start()] + content[end:]
        labels = re.findall(r"\\label\s*\{([^}]*)\}", content)
        content = re.sub(r"\\label\s*\{[^}]*\}", "", content)
        word = "Tableau" if kind.startswith(("table", "wraptable")) else "Figure"
        parent = "chapter" if self.has_chapters else None
        number = self.next_number("table" if word == "Tableau" else "figure", parent) if caption is not None else None
        for name in labels:
            self.labels[name.strip()] = (number or "??", None)
        title = " ".join(filter(None, [word, number])) + (f" — {caption}" if caption else "")
        saved, self.drawings = self.drawings, []
        inner = self.blocks(content).strip()
        drawings, self.drawings = self.drawings, saved
        parts = [inner] if inner else []
        if drawings:
            self.figure_count += 1
            parts.append(self.drawing_callout(title if not inner else "Schéma (TikZ)", drawings))
        if caption is not None and (inner or not drawings):
            parts.append(f"*{title}*")
        return "\n\n".join(parts)

    def drawing(self, env, content):
        source = f"\\begin{{{env}}}{content}\\end{{{env}}}"
        if self.drawings is not None:
            self.drawings.append(source)
            return ""
        self.figure_count += 1
        return self.drawing_callout(DRAWING_ENVS.get(env.rstrip("*"), "Figure"), [source])

    def drawing_callout(self, title, sources):
        code = "\n\n".join(s.strip("\n") for s in sources)
        body = ("*Dessin LaTeX (TikZ) : le wiki ne peut pas l'afficher. Code source :*\n"
                f"```latex\n{code}\n```")
        return self.callout("figure", title, body, folded=True)

    # -- Texte ---------------------------------------------------------------------------

    def inline(self, s, table=False):
        out, i, expansions = [], 0, 0
        while i < len(s):
            c = s[i]
            if c == "\\":
                m = CMD.match(s, i)
                if not m:
                    i += 1
                    continue
                i = m.end()
                if m.group(3) is not None:
                    i = self.inline_symbol(m.group(3), s, i, out, table)
                    continue
                name = m.group(1)
                if name in self.macros and name not in ("arraystretch", "proofname") and expansions < 500:
                    # Développe en place puis relit (la macro peut contenir de la mise en forme).
                    expansions += 1
                    macro = self.macros[name]
                    j, args = i, []
                    if macro.default is not None:
                        optional, j = read_optional(s, j)
                        args.append(optional if optional is not None else macro.default)
                    while len(args) < macro.nargs:
                        arg, j = read_arg(s, j)
                        args.append(arg)
                    body = macro.body
                    for n, arg in enumerate(args, 1):
                        body = body.replace(f"#{n}", arg)
                    s = s[:m.start()] + body + s[j:]
                    i = m.start()
                    continue
                i = self.inline_command(name, s, i, out, table)
            elif c == "$" or (c == "\ue010"):
                if c == "\ue010":
                    end = s.find("\ue011", i)
                    out.append(s[i:end + 1])
                    i = end + 1
                    continue
                display = s.startswith("$$", i)
                start = i + (2 if display else 1)
                j = start
                while True:
                    j = s.find("$$" if display else "$", j)
                    if j < 0 or s[j - 1] != "\\":
                        break
                    j += 1
                j = len(s) if j < 0 else j
                math = self.math(s[start:j], table=table)
                if math:
                    out.append(f"$${math}$$" if display else f"${math}$")
                i = j + (2 if display else 1)
            elif c in "{}":
                i += 1
            elif c == "~":
                out.append("\u00a0")
                i += 1
            elif s.startswith("---", i):
                out.append("—")
                i += 3
            elif s.startswith("--", i):
                out.append("–")
                i += 2
            elif s.startswith("``", i):
                out.append("“")
                i += 2
            elif s.startswith("''", i):
                out.append("”")
                i += 2
            elif c == "\n":
                out.append(" ")
                i += 1
            elif c in "*_#[]":
                out.append("\\" + c)
                i += 1
            elif c == "|" and table:
                out.append("\\|")
                i += 1
            else:
                out.append(c)
                i += 1
        return "".join(out)

    def inline_symbol(self, char, s, i, out, table):
        if char == "\\":
            _, i = read_optional(s, i)
            out.append(" " if table else "\ue020")
        elif char in ",:;":
            out.append("\u202f" if char == "," else " ")
        elif char in " \n\t":
            out.append(" ")
        elif char in "%&#$_{}":
            out.append({"$": "\\$", "_": "\\_", "#": "\\#"}.get(char, char))
        elif char == "(":
            end = s.find("\\)", i)
            end = len(s) if end < 0 else end
            math = self.math(s[i:end], table=table)
            if math:
                out.append(f"${math}$")
            i = end + 2
        elif char in ACCENTS:
            letter, i = read_arg(s, i)
            out.append(unicodedata.normalize("NFC", (letter or "")[:1] + ACCENTS[char]) + (letter or "")[1:])
        # \- \/ \@ \! : rien
        return i

    def inline_command(self, name, s, i, out, table):
        if name in ("emph", "textit", "textsl", "textbf", "texttt"):
            arg, i = read_group(s, i)
            text = self.inline(arg or "", table).strip()
            mark = {"textbf": "**", "texttt": "`"}.get(name, "*")
            if text:
                out.append(f"{mark}{text}{mark}")
            return i
        if name in ("ref", "autoref", "cref", "Cref", "eqref", "pageref", "vref"):
            label, i = read_group(s, i)
            if name != "pageref":
                kind = "eq" if name == "eqref" else ""
                out.append(f"{chr(0xE002) if table else chr(0xE000)}{label or ''}\ue001{kind}\ue003")
            return i
        if name == "label":
            label, i = read_group(s, i)
            self.label(label or "")
            return i
        if name == "footnote":
            _, i = read_optional(s, i)
            note, i = read_group(s, i)
            self.footnotes.append(self.paragraph(note or ""))
            out.append(f"[^{len(self.footnotes)}]")
            return i
        if name == "footnotemark":
            _, i = read_optional(s, i)
            self.footnotes.append("")
            out.append(f"[^{len(self.footnotes)}]")
            return i
        if name == "url":
            url, i = read_group(s, i)
            out.append(f"<{(url or '').strip()}>")
            return i
        if name == "href":
            url, i = read_group(s, i)
            text, i = read_group(s, i)
            out.append(f"[{self.inline(text or '', table).strip()}]({(url or '').strip()})")
            return i
        if name in ("textcolor", "colorbox", "foreignlanguage"):
            _, i = read_optional(s, i)
            _, i = read_group(s, i)
            return i
        if name in ("MakeUppercase", "MakeLowercase"):
            arg, i = read_group(s, i)
            text = self.inline(arg or "", table)
            out.append(text.upper() if name == "MakeUppercase" else text.lower())
            return i
        if name == "enquote":
            arg, i = read_group(s, i)
            out.append(f"« {self.inline(arg or '', table).strip()} »")
            return i
        if name == "includegraphics":
            _, i = read_optional(s, i)
            path, i = read_group(s, i)
            out.append(f"*(image : {(path or '').strip()})*")
            return i
        if name in ("makebox", "framebox", "raisebox"):
            if name == "raisebox":
                _, i = read_group(s, i)
            while True:
                optional, j = read_optional(s, i)
                if optional is None:
                    break
                i = j
            return i
        if name in TRANSPARENT:
            return i
        if name in SYMBOLS:
            out.append(SYMBOLS[name])
            if s[i:i + 2] == "{}":
                i += 2
            return i
        if name in ACCENTS:
            letter, i = read_arg(s, i)
            out.append(unicodedata.normalize("NFC", (letter or "")[:1] + ACCENTS[name]) + (letter or "")[1:])
            return i
        if name in DROP:
            count = DROP[name]
            while True:
                optional, j = read_optional(s, i)
                if optional is None:
                    break
                i = j
            for _ in range(count if count >= 0 else 20):
                group, j = read_group(s, i)
                if group is None:
                    break
                i = j
                while True:
                    optional, j = read_optional(s, i)
                    if optional is None:
                        break
                    i = j
            return i
        # Commande inconnue : on garde le texte de ses arguments.
        return i


def convert(source):
    """Markdown (syntaxe Obsidian) équivalent au document LaTeX ``source``."""
    return Converter(source).convert()


def looks_like_latex(text):
    """Vrai pour un document ou un extrait LaTeX structuré (pas une simple formule)."""
    return bool(re.search(r"\\documentclass|\\begin\s*\{document\}", text)) or \
        len(re.findall(r"\\(chapter|section|subsection|begin\s*\{(itemize|enumerate|theorem|definition|tabular|figure))\b", text)) >= 3
