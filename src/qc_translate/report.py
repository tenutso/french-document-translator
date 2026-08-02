"""Generate the QA and image HTML reports for the human reviewer."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment
from markupsafe import Markup, escape

from .models import (CHANGED, NEW, UNCHANGED_APPROVED, UNCHANGED_MT, ImageInfo,
                     Segment)
from .tm import plain

# Reports render document-derived text (including OCR output) that a reviewer opens
# straight in a browser, so autoescape everything by default.
_ENV = Environment(autoescape=True)

# Order and French labels for the revision report / QA status column.
_STATUS_LABELS = (
    (NEW, "nouveaux"),
    (CHANGED, "modifiés"),
    (UNCHANGED_MT, "inchangés (machine)"),
    (UNCHANGED_APPROVED, "inchangés (approuvés)"),
)

_QA_TMPL = _ENV.from_string(
    """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>Rapport QA — {{ job }}</title>
<style>
 body{font-family:system-ui,Segoe UI,sans-serif;margin:2rem;color:#1a1a1a}
 h1{font-size:1.4rem} .stats{display:flex;gap:1.5rem;margin:1rem 0;flex-wrap:wrap}
 .stat{background:#f4f4f5;border-radius:8px;padding:.6rem 1rem}
 .stat b{display:block;font-size:1.5rem}
 table{border-collapse:collapse;width:100%;font-size:.9rem}
 th,td{border:1px solid #e4e4e7;padding:.5rem;text-align:left;vertical-align:top}
 th{background:#fafafa;position:sticky;top:0}
 .flag{color:#b91c1c;font-weight:600} .qe-low{background:#fef2f2}
 code{background:#f4f4f5;padding:.1rem .3rem;border-radius:4px}
 .src{color:#555}
</style></head><body>
<h1>Rapport d'assurance qualité — {{ job }}</h1>
<div class="stats">
 <div class="stat"><b>{{ total }}</b>segments</div>
 <div class="stat"><b>{{ flagged|length }}</b>à réviser</div>
 <div class="stat"><b>{{ tm_reuse }}</b>réutilisés (MT)</div>
 <div class="stat"><b>{{ avg_qe }}</b>score QE moyen</div>
</div>
<p>Réviser d'abord les segments ci-dessous (triés par gravité / score QE le plus faible).</p>
<table><thead><tr><th>#</th><th>Source (EN)</th><th>Cible (FR-CA)</th><th>Version</th><th>QE</th><th>Signalements</th></tr></thead>
<tbody>
{% for s in flagged %}
<tr class="{{ 'qe-low' if s.qe_score is not none and s.qe_score < flag_below else '' }}">
 <td>{{ s.unit_id }}</td>
 <td class="src">{{ s.src }}</td>
 <td>{{ s.tgt }}</td>
 <td>{{ s.status }}</td>
 <td>{{ '%.2f'|format(s.qe_score) if s.qe_score is not none else '—' }}</td>
 <td class="flag">{{ s.flags }}</td>
</tr>
{% endfor %}
</tbody></table>
{% if not flagged %}<p><b>Aucun segment signalé.</b> Une relecture humaine reste recommandée.</p>{% endif %}
</body></html>"""
)

_CHANGES_TMPL = _ENV.from_string(
    """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>Changements — {{ job }}</title>
<style>
 body{font-family:system-ui,Segoe UI,sans-serif;margin:2rem;color:#1a1a1a}
 h1{font-size:1.4rem} h2{font-size:1.1rem;margin-top:2rem}
 .stats{display:flex;gap:1.5rem;margin:1rem 0;flex-wrap:wrap}
 .stat{background:#f4f4f5;border-radius:8px;padding:.6rem 1rem}
 .stat b{display:block;font-size:1.5rem}
 .stat.new b{color:#b45309} .stat.changed b{color:#1d4ed8}
 table{border-collapse:collapse;width:100%;font-size:.9rem;margin-top:.5rem}
 th,td{border:1px solid #e4e4e7;padding:.5rem;text-align:left;vertical-align:top}
 th{background:#fafafa} .src{color:#555} .prev{color:#777;font-style:italic}
 .lead{max-width:60rem} .none{color:#555}
</style></head><body>
<h1>Changements depuis la version précédente — {{ job }}</h1>
<div class="stats">
 <div class="stat new"><b>{{ counts[new_key] }}</b>nouveaux</div>
 <div class="stat changed"><b>{{ counts[changed_key] }}</b>modifiés</div>
 <div class="stat"><b>{{ counts[unchanged_mt_key] }}</b>inchangés (machine)</div>
 <div class="stat"><b>{{ counts[unchanged_approved_key] }}</b>inchangés (approuvés)</div>
</div>
<p class="lead">Les segments <b>inchangés (approuvés)</b> reprennent mot pour mot une
formulation déjà validée par un réviseur&nbsp;: ils portent l'état <code>signed-off</code>
dans <code>translated.xlf</code> et peuvent être ignorés. Concentrez la révision sur les
segments <b>nouveaux</b> et <b>modifiés</b> ci-dessous.</p>

<h2>Nouveaux segments ({{ new_rows|length }})</h2>
{% if new_rows %}
<table><thead><tr><th>#</th><th>Source (EN)</th><th>Cible (FR-CA)</th></tr></thead><tbody>
{% for s in new_rows %}
<tr><td>{{ s.unit_id }}</td><td class="src">{{ s.src }}</td><td>{{ s.tgt }}</td></tr>
{% endfor %}
</tbody></table>
{% else %}<p class="none">Aucun.</p>{% endif %}

<h2>Segments modifiés ({{ changed_rows|length }})</h2>
{% if changed_rows %}
<table><thead><tr><th>#</th>
{% if show_prev_src %}<th>Source précédente (EN)</th>{% endif %}
<th>Source (EN)</th><th>Cible (FR-CA)</th>
<th>Cible précédente (FR-CA)</th></tr></thead><tbody>
{% for s in changed_rows %}
<tr><td>{{ s.unit_id }}</td>
 {% if show_prev_src %}<td class="prev">{{ s.previous_src or '—' }}</td>{% endif %}
 <td class="src">{{ s.src }}</td><td>{{ s.tgt }}</td>
 <td class="prev">{{ s.previous or '—' }}</td></tr>
{% endfor %}
</tbody></table>
{% else %}<p class="none">Aucun.</p>{% endif %}
</body></html>"""
)

_IMG_TMPL = _ENV.from_string(
    """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>Rapport images — {{ job }}</title>
<style>
 body{font-family:system-ui,Segoe UI,sans-serif;margin:2rem;color:#1a1a1a}
 .card{display:flex;gap:1rem;border:1px solid #e4e4e7;border-radius:8px;padding:1rem;margin:1rem 0}
 .card img{max-width:320px;max-height:320px;border:1px solid #ddd;border-radius:4px}
 .meta{flex:1} .flag{color:#b91c1c;font-weight:600}
 .loc{color:#555;font-size:.9rem} h1{font-size:1.4rem}
 .txt{background:#f4f4f5;border-radius:6px;padding:.5rem;white-space:pre-wrap}
</style></head><body>
<h1>Rapport des images intégrées — {{ job }}</h1>
<p>{{ total }} image(s). {{ flagged }} contiennent du texte et nécessitent une recréation graphique.</p>
{% for im in images %}
<div class="card">
 <div>{% if im.thumbnail_data_uri %}<img src="{{ im.thumbnail_data_uri }}">{% endif %}</div>
 <div class="meta">
   <div class="loc">{{ im.location }} — <code>{{ im.media_path }}</code></div>
   {% if im.requires_recreation %}<p class="flag">⚑ Recréation graphique requise (contient du texte)</p>{% endif %}
   {% if im.ocr_text %}<p><b>Texte détecté (EN):</b></p><div class="txt">{{ im.ocr_text }}</div>{% endif %}
   {% if im.suggested_fr %}<p><b>Traduction suggérée (FR-CA):</b></p><div class="txt">{{ im.suggested_fr }}</div>{% endif %}
 </div>
</div>
{% endfor %}
</body></html>"""
)


def write_qa_report(path: str | Path, job: str, segments: list[Segment],
                    flag_below: float) -> Path:
    flagged = [s for s in segments if s.needs_review]
    # Sort worst-first: lowest QE, then most flags.
    flagged.sort(key=lambda s: (s.qe_score if s.qe_score is not None else 1.0,
                                -len(s.qa_flags)))
    qes = [s.qe_score for s in segments if s.qe_score is not None]
    labels = dict(_STATUS_LABELS)
    rows = [{
        "unit_id": s.unit_id, "src": plain(s.source_xml), "tgt": plain(s.target_xml or ""),
        "qe_score": s.qe_score,
        # Escape each flag individually, then join with a literal (unescaped) <br> so the
        # deliberate line breaks survive autoescaping without exposing glossary-term text
        # embedded in flag strings (e.g. glossary_miss:src->tgt) to HTML injection.
        "flags": Markup("<br>").join(escape(f) for f in s.qa_flags),
        "status": labels.get(s.version_status, "—"),
    } for s in flagged]
    html = _QA_TMPL.render(
        job=job, total=len(segments), flagged=rows,
        tm_reuse=sum(1 for s in segments if s.tm_exact),
        avg_qe=f"{sum(qes) / len(qes):.2f}" if qes else "—",
        flag_below=flag_below,
    )
    out = Path(path)
    out.write_text(html, encoding="utf-8")
    return out


def write_changes_report(path: str | Path, job: str, segments: list[Segment]) -> Path:
    """Write changes.html: what moved since the TM last saw this content.

    Lets a reviewer skip settled text on a revised document instead of re-reading the whole
    thing. Counts cover every segment; the tables list only what needs attention.
    """
    counts = {status: 0 for status, _ in _STATUS_LABELS}
    for s in segments:
        if s.version_status in counts:
            counts[s.version_status] += 1

    def rows(status: str) -> list[dict]:
        return [{
            "unit_id": s.unit_id, "src": plain(s.source_xml),
            "tgt": plain(s.target_xml or ""),
            "previous": plain(s.previous_target) if s.previous_target else "",
            "previous_src": s.previous_source or "",
        } for s in segments if s.version_status == status]

    html = _CHANGES_TMPL.render(
        job=job, counts=counts, new_rows=rows(NEW), changed_rows=rows(CHANGED),
        new_key=NEW, changed_key=CHANGED,
        unchanged_mt_key=UNCHANGED_MT, unchanged_approved_key=UNCHANGED_APPROVED,
        # Only widen the table when a previous job was actually supplied.
        show_prev_src=any(s.previous_source for s in segments),
    )
    out = Path(path)
    out.write_text(html, encoding="utf-8")
    return out


def write_image_report(path: str | Path, job: str, images: list[ImageInfo]) -> Path:
    html = _IMG_TMPL.render(
        job=job, total=len(images),
        flagged=sum(1 for i in images if i.requires_recreation), images=images,
    )
    out = Path(path)
    out.write_text(html, encoding="utf-8")
    return out
