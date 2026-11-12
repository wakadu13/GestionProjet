"""Graphiques SVG générés côté serveur (aucune bibliothèque JavaScript externe)."""
from __future__ import annotations

import math

from markupsafe import Markup, escape


def nice_max(v: float) -> float:
    if v <= 0:
        return 4
    exp = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 4, 5, 10):
        if v <= m * exp:
            nm = m * exp
            return max(nm, 4) if v >= 1 else nm
    return v


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and not v.is_integer():
        return f"{v:.1f}".replace(".", ",")
    return str(int(v))


def _trunc(s: str, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def donut(segments, size: int = 170, thickness: int = 22, center_value=None, center_label: str = "") -> Markup:
    """segments : [(libellé, valeur, couleur)]."""
    total = sum(v for _, v, _ in segments) or 0
    r = (size - thickness) / 2
    c = 2 * math.pi * r
    cx = cy = size / 2
    parts = [f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" class="ch-track" stroke-width="{thickness}"/>']
    offset = 0.0
    if total:
        for label, value, color in segments:
            if not value:
                continue
            length = c * value / total
            gap = 1.5 if len([s for s in segments if s[1]]) > 1 else 0
            parts.append(
                f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{escape(color)}" stroke-width="{thickness}" '
                f'stroke-dasharray="{max(length - gap, 0.1):.2f} {c:.2f}" stroke-dashoffset="{-offset:.2f}" '
                f'transform="rotate(-90 {cx} {cy})"><title>{escape(label)} : {_fmt(value)} '
                f'({round(100 * value / total)} %)</title></circle>'
            )
            offset += length
    cv = center_value if center_value is not None else _fmt(total)
    parts.append(f'<text x="{cx}" y="{cy - 2}" text-anchor="middle" class="ch-center">{escape(cv)}</text>')
    if center_label:
        parts.append(f'<text x="{cx}" y="{cy + 16}" text-anchor="middle" class="ch-label">{escape(center_label)}</text>')
    return Markup(
        f'<svg class="chart donut" viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img">'
        + "".join(parts) + "</svg>"
    )


def bar_chart(items, height: int = 220, value_suffix: str = "") -> Markup:
    """Barres verticales. items : [(libellé, valeur, couleur)]."""
    n = max(len(items), 1)
    slot = 64
    left, bottom, top = 34, 40, 18
    width = left + n * slot + 10
    plot_h = height - bottom - top
    vmax = nice_max(max([v for _, v, _ in items] + [0]))
    out = []
    for k in range(5):
        val = vmax * k / 4
        y = top + plot_h - plot_h * k / 4
        out.append(f'<line x1="{left}" x2="{width - 6}" y1="{y:.1f}" y2="{y:.1f}" class="ch-grid"/>')
        out.append(f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end" class="ch-axis">{_fmt(val)}</text>')
    for idx, (label, value, color) in enumerate(items):
        bw = 30
        x = left + idx * slot + (slot - bw) / 2
        h = plot_h * (value / vmax) if vmax else 0
        y = top + plot_h - h
        out.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw}" height="{max(h, 0):.1f}" rx="4" fill="{escape(color)}">'
            f'<title>{escape(label)} : {_fmt(value)}{value_suffix}</title></rect>'
        )
        if value:
            out.append(f'<text x="{x + bw / 2:.1f}" y="{y - 5:.1f}" text-anchor="middle" class="ch-value">'
                       f'{_fmt(value)}</text>')
        out.append(f'<text x="{x + bw / 2:.1f}" y="{height - bottom + 16}" text-anchor="middle" class="ch-axis">'
                   f'{escape(_trunc(label, 10))}<title>{escape(label)}</title></text>')
    return Markup(
        f'<svg class="chart" viewBox="0 0 {width} {height}" preserveAspectRatio="xMinYMid meet" role="img" '
        f'style="max-width:{width}px">' + "".join(out) + "</svg>"
    )


def stacked_bars(categories, series, height: int = 280) -> Markup:
    """categories : [libellé] ; series : [(nom, couleur, [valeurs])]."""
    n = max(len(categories), 1)
    slot = max(44, min(90, 760 // n))
    left, bottom, top = 38, 54, 16
    width = left + n * slot + 10
    plot_h = height - bottom - top
    totals = [sum(s[2][i] for s in series) for i in range(len(categories))]
    vmax = nice_max(max(totals + [0]))
    out = []
    for k in range(5):
        val = vmax * k / 4
        y = top + plot_h - plot_h * k / 4
        out.append(f'<line x1="{left}" x2="{width - 6}" y1="{y:.1f}" y2="{y:.1f}" class="ch-grid"/>')
        out.append(f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end" class="ch-axis">{_fmt(val)}</text>')
    bw = min(34, slot - 12)
    for i, cat in enumerate(categories):
        x = left + i * slot + (slot - bw) / 2
        y_cursor = top + plot_h
        for name, color, values in series:
            v = values[i]
            if not v:
                continue
            h = plot_h * v / vmax
            y_cursor -= h
            out.append(f'<rect x="{x:.1f}" y="{y_cursor:.1f}" width="{bw}" height="{h:.1f}" fill="{escape(color)}">'
                       f'<title>{escape(cat)} · {escape(name)} : {_fmt(v)}</title></rect>')
        if totals[i]:
            out.append(f'<text x="{x + bw / 2:.1f}" y="{y_cursor - 5:.1f}" text-anchor="middle" class="ch-value">'
                       f'{_fmt(totals[i])}</text>')
        lx, ly = x + bw / 2, height - bottom + 14
        out.append(f'<text x="{lx:.1f}" y="{ly}" text-anchor="end" transform="rotate(-30 {lx:.1f} {ly})" '
                   f'class="ch-axis">{escape(_trunc(cat, 14))}<title>{escape(cat)}</title></text>')
    return Markup(
        f'<svg class="chart" viewBox="0 0 {width} {height}" preserveAspectRatio="xMinYMid meet" role="img" '
        f'style="max-width:{width}px">' + "".join(out) + "</svg>"
    )


def line_chart(x_labels, series, height: int = 240, width: int = 680, today_index: int | None = None,
               label_every: int | None = None) -> Markup:
    """series : [{name, color, values: [nombre|None], dashed: bool, fill: bool}]."""
    left, right, bottom, top = 36, 14, 30, 14
    n = max(len(x_labels), 2)
    plot_w = width - left - right
    plot_h = height - top - bottom
    all_vals = [v for s in series for v in s["values"] if v is not None]
    vmax = nice_max(max(all_vals + [0]))
    step = plot_w / (n - 1)

    def px(i):
        return left + i * step

    def py(v):
        return top + plot_h - plot_h * (v / vmax if vmax else 0)

    out = []
    for k in range(5):
        val = vmax * k / 4
        y = top + plot_h - plot_h * k / 4
        out.append(f'<line x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}" class="ch-grid"/>')
        out.append(f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end" class="ch-axis">{_fmt(val)}</text>')
    every = label_every or max(1, math.ceil(n / 8))
    for i, lab in enumerate(x_labels):
        if i % every == 0 or i == n - 1:
            out.append(f'<text x="{px(i):.1f}" y="{height - 10}" text-anchor="middle" class="ch-axis">'
                       f'{escape(lab)}</text>')
    if today_index is not None and 0 <= today_index < n:
        x = px(today_index)
        out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top}" y2="{top + plot_h}" class="ch-today"/>')
        out.append(f'<text x="{x + 4:.1f}" y="{top + 10}" class="ch-axis ch-today-t">Aujourd\'hui</text>')
    for s in series:
        pts = [(px(i), py(v), v, x_labels[i]) for i, v in enumerate(s["values"]) if v is not None]
        if not pts:
            continue
        color = escape(s["color"])
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y, _, _ in pts)
        if s.get("fill"):
            area = d + f" L{pts[-1][0]:.1f},{top + plot_h} L{pts[0][0]:.1f},{top + plot_h} Z"
            out.append(f'<path d="{area}" fill="{color}" opacity="0.12" stroke="none"/>')
        dash = ' stroke-dasharray="5 4"' if s.get("dashed") else ""
        out.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2.2"{dash} '
                   f'stroke-linejoin="round" stroke-linecap="round"/>')
        if not s.get("dashed") and len(pts) <= 60:
            for x, y, v, lab in pts:
                out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.6" fill="{color}">'
                           f'<title>{escape(s["name"])} · {escape(lab)} : {_fmt(v)}</title></circle>')
    return Markup(
        f'<svg class="chart" viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet" role="img">'
        + "".join(out) + "</svg>"
    )


def stacked_area(x_labels, series, height: int = 260, width: int = 680, label_every: int | None = None,
                 today_index: int | None = None) -> Markup:
    """Aires empilées. series : [(nom, couleur, [valeurs])], la première série en bas."""
    left, right, bottom, top = 36, 14, 30, 14
    n = max(len(x_labels), 2)
    plot_w = width - left - right
    plot_h = height - top - bottom
    totals = [sum(s[2][i] for s in series) for i in range(len(x_labels))] or [0]
    vmax = nice_max(max(totals + [0]))
    step = plot_w / (n - 1)

    def px(i):
        return left + i * step

    def py(v):
        return top + plot_h - plot_h * (v / vmax if vmax else 0)

    out = []
    for k in range(5):
        val = vmax * k / 4
        y = top + plot_h - plot_h * k / 4
        out.append(f'<line x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}" class="ch-grid"/>')
        out.append(f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end" class="ch-axis">{_fmt(val)}</text>')
    every = label_every or max(1, math.ceil(n / 8))
    for i, lab in enumerate(x_labels):
        if i % every == 0 or i == n - 1:
            out.append(f'<text x="{px(i):.1f}" y="{height - 10}" text-anchor="middle" class="ch-axis">{escape(lab)}</text>')
    base = [0] * len(x_labels)
    for name, color, values in series:
        topv = [b + v for b, v in zip(base, values)]
        color = escape(color)
        if any(values):
            upper = " L".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(topv))
            lower = " L".join(f"{px(i):.1f},{py(v):.1f}" for i, v in reversed(list(enumerate(base))))
            out.append(f'<path d="M{upper} L{lower} Z" fill="{color}" fill-opacity="0.78" stroke="{color}" '
                       f'stroke-width="1"><title>{escape(name)}</title></path>')
        base = topv
    if today_index is not None and 0 <= today_index < len(x_labels):
        x = px(today_index)
        out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top}" y2="{top + plot_h}" class="ch-today"/>')
    # Zones de survol : détail de chaque jour
    for i, lab in enumerate(x_labels):
        x0 = px(i) - step / 2
        detail = " · ".join(f"{name} : {_fmt(values[i])}" for name, _, values in reversed(series))
        out.append(f'<rect x="{max(left, x0):.1f}" y="{top}" width="{max(step, 1):.1f}" height="{plot_h}" '
                   f'fill="transparent"><title>{escape(lab)} — {escape(detail)}</title></rect>')
    return Markup(
        f'<svg class="chart" viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet" role="img">'
        + "".join(out) + "</svg>"
    )


def sparkline(values, color: str = "#3f76ff", width: int = 120, height: int = 32) -> Markup:
    if not values:
        return Markup("")
    vmax = max(values) or 1
    n = len(values)
    step = width / max(n - 1, 1)
    pts = " ".join(f"{i * step:.1f},{height - 3 - (height - 6) * v / vmax:.1f}" for i, v in enumerate(values))
    return Markup(
        f'<svg class="chart spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}">'
        f'<polyline points="{pts}" fill="none" stroke="{escape(color)}" stroke-width="1.8" stroke-linejoin="round"/></svg>'
    )
