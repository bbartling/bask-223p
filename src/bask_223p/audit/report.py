"""Self-contained local HTML artifact; escape arbitrary CSV/RDF labels."""
from __future__ import annotations
from html import escape
from pathlib import Path
from typing import Iterable


def write_html(rows: Iterable, summary: dict, output: Path) -> None:
    columns = ['term','ontology','rank','status','label','uri','score','rdf_kind']
    cells = ''.join('<tr>' + ''.join('<td>' + escape(str(getattr(row, col))) + '</td>' for col in columns) + '</tr>' for row in rows)
    head = ''.join('<th>' + escape(name.replace('_', ' ').title()) + '</th>' for name in columns)
    # All inputs are HTML escaped; no remote assets or JavaScript dependencies.
    output.write_text(f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Ontology Coverage v2</title>
<style>body{{font:15px system-ui,sans-serif;max-width:1350px;margin:auto;padding:28px;color:#1c2b41;background:#f5f7fa}}
h1{{color:#14365b}} .metrics{{display:flex;flex-wrap:wrap;gap:16px}} .metric{{padding:18px 28px;background:#fff;border-radius:12px;box-shadow:0 1px 6px #ddd}} .metric b{{display:block;font-size:28px}}
.warning{{border-left:5px solid #d28c13;background:#fff6e6;padding:16px;margin:22px 0}}
section{{overflow:auto;background:#fff;border-radius:12px;padding:10px}}table{{border-collapse:collapse;width:100%}}td,th{{padding:9px;border-bottom:1px solid #e4e8ee;text-align:left;overflow-wrap:anywhere}}th{{background:#ecf1f7}}input{{padding:10px;margin:12px 0;width:min(460px,90%);border-radius:7px;border:1px solid #a7b0c2}}</style></head><body>
<h1>Ontology Coverage v2 · terminology candidates</h1><div class="metrics">
<div class="metric"><b>{summary['terms']}</b>Unique terms</div>
<div class="metric"><b>{summary['terms_with_any_candidate']}</b>With candidates</div>
<div class="metric"><b>{summary['terms_with_exact_label']}</b>Exact label terms</div>
<div class="metric"><b>{summary['terms_without_candidate']}</b>No candidates</div></div>
<div class="warning"><strong>Not SHACL validation:</strong> String similarity does not establish a valid RDF mapping, class assignment, or OWL equivalence. Every candidate requires review.</div>
<label for="filter">Search candidate rows</label><div><input id="filter" type="search" placeholder="filter terms, URIs, ontologies"></div>
<section><table id="results"><thead><tr>{head}</tr></thead><tbody>{cells}</tbody></table></section>
<script>document.getElementById('filter').addEventListener('input',function(){{let q=this.value.toLowerCase();for(let row of document.querySelectorAll('#results tbody tr')){{row.hidden=!row.textContent.toLowerCase().includes(q);}}}});</script>
</body></html>''', encoding='utf-8')
