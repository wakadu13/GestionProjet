"""Lecture des fichiers iCalendar (.ics) : l'emploi du temps envoyé chaque semaine.

Gère le format exporté par le logiciel de planning de l'école :

    SUMMARY:Réseaux avancés 3 CM \\n           -> matière « Réseaux avancés », séance 3, type CM
    SUMMARY:Briefing  BRIEFING (PREVISION)\\n  -> type BRIEFING, prévisionnel
    DESCRIPTION:                              -> lignes « - Apprenants : … », « - Intervenants : … »
     - DESCRIPTION : …\\n
     - Apprenants : CL_25 / CM_25_SI\\n
     - Intervenants : Dupont / Martin\\n

et, plus généralement, tout fichier .ics standard (lignes repliées, TZID, heures UTC, journées entières).
Les heures sont rendues en heure locale « naïve » (fuseau du calendrier, Europe/Paris par défaut).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

DEFAULT_TZ = "Europe/Paris"

# Types de séance connus (code -> (libellé, libellé court))
KINDS = {
    "CM": ("Cours magistral", "CM"),
    "TD_TP": ("TD / TP", "TD/TP"),
    "TD": ("Travaux dirigés", "TD"),
    "TP": ("Travaux pratiques", "TP"),
    "CO_ECRIT": ("Contrôle écrit", "Contrôle"),
    "CO_ORAL": ("Contrôle oral", "Oral"),
    "CO_TP": ("Contrôle de TP", "Contrôle"),
    "EXAM": ("Examen", "Examen"),
    "SPORT": ("Sport", "Sport"),
    "MILT": ("Formation militaire", "Militaire"),
    "CONF": ("Conférence", "Conférence"),
    "TPA": ("Temps personnel d’assimilation", "TPA"),
    "BRIEFING": ("Briefing", "Briefing"),
    "PROJET": ("Projet", "Projet"),
}

# « Titre n° TYPE » : le numéro de séance est suivi d'une seule espace. Un emplacement de numéro vide laisse
# deux espaces avant le type (« Anglais niveau 2  CO_ORAL ») : le « 2 » appartient alors au titre.
_SUMMARY_RE = re.compile(
    r"^(?P<title>.*?\S)(?P<sep>\s+)(?:(?P<num>\d{1,3})[ \t](?=\S))?(?P<kind>[A-Z][A-Z0-9_]{1,15})"
    r"(?:\s*\((?P<flag>[^()]*)\))?\s*$"
)
_FLAG_ONLY_RE = re.compile(r"^(?P<title>.*?\S)\s*\((?P<flag>PREVISION|PRÉVISION|ANNUL[EÉ]E?|REPORT[EÉ]E?)\)\s*$", re.I)
_DESC_KEY_RE = re.compile(r"^-\s*(?P<key>[^:]{1,40}?)\s*:\s?(?P<val>.*)$")


class ICSError(ValueError):
    """Fichier illisible ou sans événement."""


@dataclass
class ParsedEvent:
    uid: str
    summary: str
    title: str
    kind: str = ""
    session_no: str = ""
    flag: str = ""
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool = False
    location: str = ""
    groups: str = ""
    teachers: str = ""
    description: str = ""
    cancelled: bool = False

    @property
    def tentative(self) -> bool:
        return "PREVISION" in _ascii_upper(self.flag)

    def values(self) -> dict:
        """Champs comparés d'un import à l'autre (et enregistrés en base)."""
        return {
            "title": self.title, "summary": self.summary, "kind": self.kind, "session_no": self.session_no,
            "tentative": self.tentative, "cancelled": self.cancelled, "start": self.start, "end": self.end,
            "all_day": self.all_day, "location": self.location, "groups": self.groups,
            "teachers": self.teachers, "description": self.description,
        }


@dataclass
class ParsedCalendar:
    events: list[ParsedEvent] = field(default_factory=list)
    name: str = ""
    timezone: str = DEFAULT_TZ
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Lecture bas niveau
# ---------------------------------------------------------------------------

def _ascii_upper(s: str) -> str:
    return (s or "").upper().replace("É", "E").replace("È", "E")


def decode(raw: bytes | str) -> str:
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def unfold(text: str) -> list[str]:
    """Recolle les lignes repliées (une ligne qui commence par un espace prolonge la précédente)."""
    lines: list[str] = []
    # L'export de l'école contient des retours chariot isolés *dans* les descriptions (« M2.1\r\\nTIR… ») :
    # on ne coupe que sur les vraies fins de ligne, et on retire les \r restants.
    if "\n" in text:
        raw_lines = [ln.rstrip("\r").replace("\r", "") for ln in text.split("\n")]
    else:  # ancien format Mac : \r seul comme fin de ligne
        raw_lines = text.split("\r")
    for line in raw_lines:
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return [ln for ln in lines if ln.strip()]


def _split_unquoted(s: str, sep: str) -> list[str]:
    out, cur, quoted = [], [], False
    for ch in s:
        if ch == '"':
            quoted = not quoted
        if ch == sep and not quoted:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def parse_line(line: str) -> tuple[str, dict, str] | None:
    """« NOM;PARAM=valeur:contenu » -> (NOM, {PARAM: valeur}, contenu)."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            head, value = line[:i], line[i + 1:]
            break
    else:
        return None
    parts = _split_unquoted(head, ";")
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.strip().upper()] = v.strip().strip('"')
    return parts[0].strip().upper(), params, value


def unescape(value: str) -> str:
    return re.sub(r"\\([\\,;nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value or "")


def _zone(name: str | None):
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # base des fuseaux absente (Windows sans tzdata) ou nom inconnu
        return None


def _to_local(dt: datetime, tz_name: str) -> datetime:
    """Datetime « aware » -> heure locale naïve du calendrier."""
    target = _zone(tz_name)
    if target is not None:
        return dt.astimezone(target).replace(tzinfo=None)
    return dt.astimezone().replace(tzinfo=None)  # fuseau de la machine


def parse_dt(value: str, params: dict, cal_tz: str) -> tuple[datetime, bool]:
    """Retourne (datetime local naïf, journée entière ?)."""
    v = (value or "").strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", v):
        d = datetime.strptime(v[:8], "%Y%m%d")
        return d, True
    m = re.fullmatch(r"(\d{8})T(\d{4})(\d{2})?(Z)?", v)
    if not m:
        raise ValueError(f"date illisible : {v!r}")
    naive = datetime.strptime(m.group(1) + m.group(2) + (m.group(3) or "00"), "%Y%m%d%H%M%S")
    if m.group(4):  # UTC
        return _to_local(naive.replace(tzinfo=timezone.utc), cal_tz), False
    tzid = params.get("TZID")
    if tzid and tzid != cal_tz:
        src = _zone(tzid)
        if src is not None:
            return _to_local(naive.replace(tzinfo=src), cal_tz), False
    return naive, False  # heure « flottante » ou déjà dans le fuseau du calendrier


def parse_duration(value: str) -> timedelta | None:
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", (value or "").strip())
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return sign * timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


# ---------------------------------------------------------------------------
# Interprétation du format de l'école
# ---------------------------------------------------------------------------

def parse_summary(summary: str) -> dict:
    """« Réseaux avancés 3 CM » -> {title, session_no, kind, flag}. Titre inchangé si le motif ne correspond pas."""
    s = re.sub(r"\s+$", "", summary or "")
    m = _SUMMARY_RE.match(s)
    if m:
        kind = m.group("kind")
        num = m.group("num") or ""
        # Le motif « titre [n°] TYPE » n'est retenu que s'il ressemble bien à l'export de l'école :
        # numéro de séance présent, double espace avant le type, type connu ou code avec « _ ».
        if num or len(m.group("sep")) >= 2 or kind in KINDS or "_" in kind:
            return {"title": m.group("title").strip(), "session_no": num, "kind": kind,
                    "flag": (m.group("flag") or "").strip()}
    m = _FLAG_ONLY_RE.match(s)
    if m:
        return {"title": m.group("title").strip(), "session_no": "", "kind": "", "flag": m.group("flag").strip()}
    return {"title": s.strip(), "session_no": "", "kind": "", "flag": ""}


def parse_description(text: str) -> dict:
    """Sépare les rubriques « - Clé : valeur » (Apprenants, Intervenants, DESCRIPTION…)."""
    groups, teachers, desc_lines = "", "", []
    sections: list[list] = []  # [clé, [lignes]]
    for raw in (text or "").split("\n"):
        line = raw.rstrip()
        m = _DESC_KEY_RE.match(line.strip())
        if m:
            sections.append([m.group("key").strip(), [m.group("val").strip()]])
        elif sections:
            sections[-1][1].append(line.strip())
        elif line.strip():
            desc_lines.append(line.strip())
    for key, lines in sections:
        val = "\n".join(x for x in lines if x).strip()
        k = _ascii_upper(key)
        if k.startswith("APPRENANT") or k.startswith("GROUPE") or k.startswith("PROMO"):
            groups = val
        elif k.startswith("INTERVENANT") or k.startswith("ENSEIGNANT") or k.startswith("PROF"):
            teachers = val
        elif k == "DESCRIPTION":
            if val:
                desc_lines.append(val)
        elif val:
            desc_lines.append(f"{key} : {val}")
    return {"groups": groups, "teachers": teachers, "description": "\n".join(desc_lines).strip()}


def _clean(value: str) -> str:
    return re.sub(r"[ \t]*\n[ \t]*$", "", unescape(value)).strip()


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def parse(raw: bytes | str) -> ParsedCalendar:
    text = decode(raw)
    if "BEGIN:VCALENDAR" not in text.upper():
        raise ICSError("Ce fichier n’est pas un calendrier iCalendar (.ics).")
    lines = unfold(text)
    cal = ParsedCalendar()
    for ln in lines:
        p = parse_line(ln)
        if p and p[0] == "X-WR-TIMEZONE" and p[2].strip():
            cal.timezone = p[2].strip()
        elif p and p[0] == "X-WR-CALNAME":
            cal.name = unescape(p[2]).strip()
    depth: list[str] = []
    props: list[tuple[str, dict, str]] = []
    recurring = 0
    bad = 0
    for ln in lines:
        p = parse_line(ln)
        if p is None:
            continue
        name, params, value = p
        if name == "BEGIN":
            depth.append(value.strip().upper())
            if depth[-1] == "VEVENT" and len(depth) >= 1:
                props = []
            continue
        if name == "END":
            comp = value.strip().upper()
            if depth and depth[-1] == comp:
                depth.pop()
            if comp == "VEVENT":
                try:
                    ev = _build_event(props, cal.timezone)
                except ValueError:
                    bad += 1
                    continue
                if ev is not None:
                    if any(n == "RRULE" for n, _, _ in props):
                        recurring += 1
                    cal.events.append(ev)
            continue
        if depth and depth[-1] == "VEVENT":
            props.append((name, params, value))
    if recurring:
        cal.warnings.append(f"{recurring} événement(s) récurrent(s) : seule la première occurrence est importée.")
    if bad:
        cal.warnings.append(f"{bad} événement(s) ignoré(s) (dates illisibles).")
    _dedupe_uids(cal.events)
    return cal


def _build_event(props, cal_tz: str) -> ParsedEvent | None:
    get = {}
    for name, params, value in props:
        get.setdefault(name, (params, value))
    if "DTSTART" not in get:
        return None
    start, all_day = parse_dt(get["DTSTART"][1], get["DTSTART"][0], cal_tz)
    if "DTEND" in get:
        end, _ = parse_dt(get["DTEND"][1], get["DTEND"][0], cal_tz)
    elif "DURATION" in get and parse_duration(get["DURATION"][1]):
        end = start + parse_duration(get["DURATION"][1])
    else:
        end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
    if end < start:
        end = start
    summary = _clean(get.get("SUMMARY", ({}, ""))[1])
    info = parse_summary(summary)
    desc = parse_description(unescape(get.get("DESCRIPTION", ({}, ""))[1]))
    status = get.get("STATUS", ({}, ""))[1].strip().upper()
    uid = get.get("UID", ({}, ""))[1].strip()
    if not uid:
        uid = "sans-uid-" + hashlib.sha1(f"{summary}|{start:%Y%m%dT%H%M}".encode()).hexdigest()[:16]
    return ParsedEvent(
        uid=uid[:250], summary=summary[:500], title=(info["title"] or summary or "Sans titre")[:300],
        kind=info["kind"], session_no=info["session_no"], flag=info["flag"],
        start=start, end=end, all_day=all_day,
        location=_clean(get.get("LOCATION", ({}, ""))[1])[:300],
        groups=desc["groups"][:500], teachers=desc["teachers"][:300], description=desc["description"],
        cancelled=status == "CANCELLED" or "ANNUL" in _ascii_upper(info["flag"]),
    )


def _dedupe_uids(events: list[ParsedEvent]) -> None:
    """Un même UID présent plusieurs fois : on distingue les occurrences par leur date de début."""
    seen: dict[str, int] = {}
    for e in events:
        seen[e.uid] = seen.get(e.uid, 0) + 1
    for e in events:
        if seen[e.uid] > 1:
            e.uid = f"{e.uid}#{e.start:%Y%m%dT%H%M}"[:255]


def kind_label(kind: str, short: bool = False) -> str:
    if not kind:
        return ""
    lab = KINDS.get(kind)
    if lab:
        return lab[1] if short else lab[0]
    return kind.replace("_", " ").title() if not short else kind.replace("_", "/")


EXAM_KINDS = {"CO_ECRIT", "CO_ORAL", "CO_TP", "EXAM", "EXAMEN", "DS", "PARTIEL", "CC", "EVAL", "QCM", "SOUTENANCE"}
_EXAM_WORDS = ("examen", "partiel", "controle ecrit", "controle continu", "devoir surveille", "soutenance", "qcm")


def is_exam(kind: str, title: str) -> bool:
    if kind in EXAM_KINDS or (kind or "").startswith("CO_"):
        return True
    import unicodedata
    t = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode().lower()
    return any(w in t for w in _EXAM_WORDS)


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def combine(d: date, t: time | None, default_hour: int = 8) -> datetime:
    return datetime.combine(d, t or time(default_hour, 0))


# ---------------------------------------------------------------------------
# Écriture (exports .ics de l'agenda et des tickets), conforme à la RFC 5545
# ---------------------------------------------------------------------------

def escape_text(s: str | None) -> str:
    """Valeur TEXT : fins de ligne Windows ramenées à « \\n », puis \\ ; , et retours à la ligne échappés
    (un retour à la ligne brut couperait la ligne et pourrait créer de faux événements)."""
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str) -> list[str]:
    """Replie une ligne en morceaux de 75 octets au plus (UTF-8, sans couper un caractère) ; chaque suite
    commence par une espace."""
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > 75:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return out


def serialize(lines: list[str]) -> str:
    """Lignes de contenu -> texte du fichier : lignes repliées, séparées et terminées par CRLF."""
    out = []
    for ln in lines:
        out += fold(ln)
    return "\r\n".join(out) + "\r\n"


def _paris_offset(naive: datetime) -> timedelta:
    """Décalage de Europe/Paris sans base des fuseaux (Windows sans tzdata) : heure d'été du dernier dimanche
    de mars, 2 h, au dernier dimanche d'octobre, 3 h (règle européenne)."""
    def last_sunday(year: int, month: int) -> date:
        d = date(year, month, 31)
        return d - timedelta(days=(d.weekday() + 1) % 7)
    start = datetime.combine(last_sunday(naive.year, 3), time(2))
    end = datetime.combine(last_sunday(naive.year, 10), time(3))
    return timedelta(hours=2 if start <= naive < end else 1)


def local_to_utc(naive: datetime, tz_name: str = DEFAULT_TZ) -> datetime:
    """Heure locale naïve du calendrier -> heure UTC naïve."""
    tz = _zone(tz_name)
    if tz is not None:
        return naive.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    if tz_name == "Europe/Paris":
        return naive - _paris_offset(naive)
    return naive.astimezone(timezone.utc).replace(tzinfo=None)  # fuseau de la machine


def utc_value(naive_utc: datetime) -> str:
    return f"{naive_utc:%Y%m%dT%H%M%S}Z"


def utc_stamp(at: datetime | None = None) -> str:
    """DTSTAMP : toujours en UTC (« Z »)."""
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is not None:
        at = at.astimezone(timezone.utc).replace(tzinfo=None)
    return utc_value(at)


def trigger(minutes_after_start: int) -> str:
    """TRIGGER relatif au début : négatif = avant (« -PT60M »), positif = après (« PT420M »)."""
    m = int(minutes_after_start)
    return f"TRIGGER:{'-' if m < 0 else ''}PT{abs(m)}M"


def vtimezone(tz_name: str = DEFAULT_TZ) -> list[str]:
    """Définition du fuseau Europe/Paris (exigée par la RFC pour chaque TZID, utile à Outlook)."""
    if tz_name != "Europe/Paris":
        return []
    return ["BEGIN:VTIMEZONE", "TZID:Europe/Paris", "X-LIC-LOCATION:Europe/Paris",
            "BEGIN:DAYLIGHT", "TZOFFSETFROM:+0100", "TZOFFSETTO:+0200", "TZNAME:CEST", "DTSTART:19700329T020000",
            "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU", "END:DAYLIGHT",
            "BEGIN:STANDARD", "TZOFFSETFROM:+0200", "TZOFFSETTO:+0100", "TZNAME:CET", "DTSTART:19701025T030000",
            "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", "END:STANDARD", "END:VTIMEZONE"]
