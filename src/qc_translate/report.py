"""Generate the QA and image HTML reports for the human reviewer."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Template

from .models import ImageInfo, Segment
from .tm import plain

_QA_TMPL = Template(
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
<table><thead><tr><th>#</th><th>Source (EN)</th><th>Cible (FR-CA)</th><th>QE</th><th>Signalements</th></tr></thead>
<tbody>
{% for s in flagged %}
<tr class="{{ 'qe-low' if s.qe_score is not none and s.qe_score < flag_below else '' }}">
 <td>{{ s.unit_id }}</td>
 <td class="src">{{ s.src }}</td>
 <td>{{ s.tgt }}</td>
 <td>{{ '%.2f'|format(s.qe_score) if s.qe_score is not none else '—' }}</td>
 <td class="flag">{{ s.flags|join('<br>')|safe }}</td>
</tr>
{% endfor %}
</tbody></table>
{% if not flagged %}<p><b>Aucun segment signalé.</b> Une relecture humaine reste recommandée.</p>{% endif %}
</body></html>"""
)

_IMG_TMPL = Template(
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
    rows = [{
        "unit_id": s.unit_id, "src": plain(s.source_xml), "tgt": plain(s.target_xml or ""),
        "qe_score": s.qe_score, "flags": s.qa_flags,
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


def write_image_report(path: str | Path, job: str, images: list[ImageInfo]) -> Path:
    html = _IMG_TMPL.render(
        job=job, total=len(images),
        flagged=sum(1 for i in images if i.requires_recreation), images=images,
    )
    out = Path(path)
    out.write_text(html, encoding="utf-8")
    return out
