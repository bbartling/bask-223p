"""Self-contained HTML UI for the SPARQL + DuckDB SQL + Live Telemetry Playground."""
from __future__ import annotations

PLAYGROUND_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>live-ingest · SPARQL &amp; DuckDB Playground</title>
<style>
  :root {
    --bg: #0f172a; --panel: #1e293b; --border: #334155;
    --text: #e2e8f0; --muted: #94a3b8; --accent: #38bdf8;
    --ok: #34d399; --err: #f87171;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background: var(--bg); color: var(--text); }
  header { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 20px; border-bottom: 1px solid var(--border); background: #0b1220; }
  header h1 { margin: 0; font-size: 1.15rem; }
  .nav { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .pill { padding: 4px 10px; border-radius: 999px; font-size: 0.8rem; background: var(--panel); border: 1px solid var(--border); color: var(--muted); }
  a.btn, button { background: var(--accent); color: #082f49; border: 0; border-radius: 6px; padding: 7px 12px; font-weight: 600; cursor: pointer; text-decoration: none; font-size: 0.85rem; }
  button.secondary { background: var(--panel); color: var(--text); border: 1px solid var(--border); }
  main { display: grid; grid-template-columns: repeat(auto-fit, minmax(460px, 1fr)); gap: 16px; padding: 18px 20px; }
  section { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px; display: flex; flex-direction: column; gap: 10px; }
  h2 { margin: 0; font-size: 1rem; color: var(--accent); }
  textarea, input, select { width: 100%; background: #0b1220; color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 8px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.82rem; }
  textarea { min-height: 150px; resize: vertical; }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .row > * { flex: 1; }
  .row > button, .row > .shrink { flex: 0 0 auto; }
  .table-wrap { max-height: 260px; overflow: auto; border: 1px solid var(--border); border-radius: 6px; }
  table { width: 100%; border-collapse: collapse; font-size: 0.78rem; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  th { background: #0b1220; position: sticky; top: 0; color: var(--muted); }
  .ref-link { color: var(--accent); cursor: pointer; text-decoration: underline; }
  .status-ok { color: var(--ok); }
  .status-err { color: var(--err); }
  .small { font-size: 0.78rem; color: var(--muted); }
</style>
</head>
<body>
<header>
  <div>
    <h1>live-ingest · SPARQL &amp; DuckDB Playground</h1>
    <div id="healthLine" class="small">Loading status…</div>
  </div>
  <div class="nav">
    <button onclick="pollOnce()">Poll Once</button>
    <a class="btn" href="/docs" target="_blank">Swagger /docs</a>
    <a class="btn" style="background:#334155;color:#e2e8f0" href="/api/model/ttl" target="_blank">Download TTL</a>
  </div>
</header>
<main>
  <section>
    <h2>1. SPARQL Query (pyoxigraph)</h2>
    <div class="row">
      <select id="sparqlPresets" onchange="applySparqlPreset()"></select>
      <input type="file" id="rqFile" accept=".rq,.txt,.sparql" class="shrink" />
      <button class="secondary shrink" onclick="uploadRq()">Upload .rq</button>
      <button class="shrink" onclick="runSparql()">Run SPARQL</button>
    </div>
    <textarea id="sparqlBox"></textarea>
    <div id="sparqlMeta" class="small"></div>
    <div id="sparqlOut" class="table-wrap"></div>
  </section>

  <section>
    <h2>2. DuckDB SQL Playground</h2>
    <div class="row">
      <select id="sqlPresets" onchange="applySqlPreset()">
        <option value="SELECT ref, timestamp_utc, value_num, value_text, units, status FROM telemetry ORDER BY timestamp_utc DESC LIMIT 25">Latest 25 telemetry rows</option>
        <option value="SELECT ref, COUNT(*) AS samples, ROUND(AVG(value_num), 2) AS avg_val, MIN(value_num) AS min_val, MAX(value_num) AS max_val, ANY_VALUE(units) AS units, MAX(timestamp_utc) AS last_seen FROM telemetry GROUP BY ref ORDER BY ref">Summary per bacnet:// ref</option>
        <option value="SELECT ref, timestamp_utc, status FROM telemetry WHERE status &lt;&gt; 'ok' ORDER BY timestamp_utc DESC LIMIT 50">Non-ok statuses</option>
      </select>
      <button class="shrink" onclick="runSql()">Run DuckDB SQL</button>
    </div>
    <textarea id="sqlBox">SELECT ref, COUNT(*) AS samples, ROUND(AVG(value_num), 2) AS avg_val, MAX(timestamp_utc) AS last_seen FROM telemetry GROUP BY ref ORDER BY ref</textarea>
    <div id="sqlMeta" class="small"></div>
    <div id="sqlOut" class="table-wrap"></div>
  </section>

  <section>
    <h2>3. SPARQL → bacnet:// Ref → DuckDB History Join</h2>
    <div class="small">Runs the SPARQL query above (expecting <code>?ref</code>) and joins every matched <code>bacnet://</code> URI with DuckDB time-series metrics.</div>
    <div class="row">
      <button class="shrink" onclick="runJoin()">Run SPARQL → DuckDB Join</button>
    </div>
    <div id="joinMeta" class="small"></div>
    <div id="joinOut" class="table-wrap"></div>
  </section>

  <section>
    <h2>4. Point History (/history?ref=...) &amp; Live WebSocket</h2>
    <div class="row">
      <input id="histRef" placeholder="bacnet://5007/analog-input,1173" />
      <input id="histStart" placeholder="start ISO (optional)" />
      <input id="histEnd" placeholder="end ISO (optional)" />
      <button class="shrink" onclick="loadHistory()">Query History</button>
    </div>
    <div id="histMeta" class="small">Click any bacnet:// ref in SPARQL results or enter one above.</div>
    <div id="histOut" class="table-wrap"></div>
  </section>
</main>
<script>
let presets = [];
function esc(v) {
  return String(v ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function renderTable(elId, cols, rows) {
  const el = document.getElementById(elId);
  if (!cols || !cols.length) { el.innerHTML = "<div class='small' style='padding:8px'>No columns</div>"; return; }
  let html = "<table><thead><tr>" + cols.map(c => "<th>" + esc(c) + "</th>").join("") + "</tr></thead><tbody>";
  for (const r of rows) {
    html += "<tr>" + cols.map(c => {
      const val = r[c];
      if (typeof val === "string" && val.startsWith("bacnet://")) {
        return `<td><span class="ref-link" onclick="pickRef('${esc(val)}')">${esc(val)}</span></td>`;
      }
      return "<td>" + esc(val) + "</td>";
    }).join("") + "</tr>";
  }
  html += "</tbody></table>";
  el.innerHTML = html;
}
function pickRef(ref) {
  document.getElementById("histRef").value = ref;
  loadHistory();
}
async function refreshHealth() {
  const r = await fetch("/health");
  const d = await r.json();
  document.getElementById("healthLine").textContent =
    `source=${d.source} | points=${d.point_count} | triples=${d.graph_triples} | duckdb=${d.duckdb_enabled} | latest=${d.latest_count} | ws=${d.ws_clients}`;
}
async function loadPresets() {
  const r = await fetch("/api/sparql/examples");
  const d = await r.json();
  presets = d.examples || [];
  const sel = document.getElementById("sparqlPresets");
  sel.innerHTML = presets.map((p, i) => `<option value="${i}">${esc(p.name)}</option>`).join("");
  if (presets.length) document.getElementById("sparqlBox").value = presets[0].query;
}
function applySparqlPreset() {
  const idx = document.getElementById("sparqlPresets").value;
  if (presets[idx]) document.getElementById("sparqlBox").value = presets[idx].query;
}
function applySqlPreset() {
  document.getElementById("sqlBox").value = document.getElementById("sqlPresets").value;
}
async function runSparql() {
  const query = document.getElementById("sparqlBox").value;
  const r = await fetch("/api/sparql", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ query }) });
  const d = await r.json();
  if (!r.ok) { document.getElementById("sparqlMeta").textContent = d.detail || "Error"; return; }
  document.getElementById("sparqlMeta").textContent = `engine=${d.engine} | rows=${d.count ?? 1}`;
  if (d.columns) renderTable("sparqlOut", d.columns, d.rows);
}
async function uploadRq() {
  const f = document.getElementById("rqFile").files[0];
  if (!f) return;
  document.getElementById("sparqlBox").value = await f.text();
  await runSparql();
}
async function runSql() {
  const sql = document.getElementById("sqlBox").value;
  const r = await fetch("/api/sql", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ sql }) });
  const d = await r.json();
  if (!r.ok) { document.getElementById("sqlMeta").textContent = d.detail || "Error"; return; }
  document.getElementById("sqlMeta").textContent = `rows=${d.count} (truncated=${d.truncated})`;
  renderTable("sqlOut", d.columns, d.rows);
}
async function runJoin() {
  const query = document.getElementById("sparqlBox").value;
  const r = await fetch("/api/join", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ query }) });
  const d = await r.json();
  if (!r.ok) { document.getElementById("joinMeta").textContent = d.detail || "Error"; return; }
  document.getElementById("joinMeta").textContent = `matched=${d.count} points (SPARQL engine=${d.sparql_engine})`;
  renderTable("joinOut", d.columns, d.rows);
}
async function loadHistory() {
  const ref = document.getElementById("histRef").value.trim();
  const start = document.getElementById("histStart").value.trim();
  const end = document.getElementById("histEnd").value.trim();
  const params = new URLSearchParams();
  if (ref) params.set("ref", ref);
  if (start) params.set("start", start);
  if (end) params.set("end", end);
  params.set("limit", "100");
  const r = await fetch("/history?" + params.toString());
  const d = await r.json();
  if (!r.ok) { document.getElementById("histMeta").textContent = d.detail || "Error"; return; }
  document.getElementById("histMeta").textContent = `store=${d.store} | rows=${d.count}`;
  renderTable("histOut", ["ref", "timestamp_utc", "value", "units", "status", "source"], d.samples);
}
async function pollOnce() {
  await fetch("/poll/once", { method: "POST" });
  await refreshHealth();
  await loadHistory();
}
(async () => {
  await refreshHealth();
  await loadPresets();
  await loadHistory();
})();
</script>
</body>
</html>
"""
