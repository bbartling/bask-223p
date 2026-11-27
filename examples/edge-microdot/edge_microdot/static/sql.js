(function () {
  const statusEl = document.getElementById("status");
  const ruleSelect = document.getElementById("ruleSelect");
  const paramsInput = document.getElementById("paramsInput");
  const sqlEditor = document.getElementById("sqlEditor");
  const runBtn = document.getElementById("runBtn");
  const execInfo = document.getElementById("execInfo");
  const errorBox = document.getElementById("errorBox");
  const resultHead = document.getElementById("resultHead");
  const resultBody = document.getElementById("resultBody");

  const QUICK_QUERIES = [
    {
      filename: "__samples__",
      name: "Latest samples (fdd.input_sample)",
      sql: "SELECT ts, point_key, value FROM fdd.input_sample ORDER BY ts DESC LIMIT 50",
      default_params: [],
    },
    {
      filename: "__points__",
      name: "Bound points",
      sql: "SELECT point_key, equipment_id, role, unit FROM fdd.point ORDER BY point_key",
      default_params: [],
    },
    {
      filename: "__fdd__",
      name: "Recorded FDD results",
      sql: "SELECT window_end, equipment_id, rule_id, subject_key, status, fault_hours, covered_hours, role FROM fdd.rule_result ORDER BY window_end DESC LIMIT 50",
      default_params: [],
    },
  ];

  let allQueries = QUICK_QUERIES.slice();
  let authHeaders = {};

  function renderTable(columns, rows) {
    resultHead.replaceChildren();
    resultBody.replaceChildren();
    const headRow = document.createElement("tr");
    columns.forEach(function (name) {
      const th = document.createElement("th");
      th.textContent = name;
      headRow.appendChild(th);
    });
    resultHead.appendChild(headRow);
    rows.forEach(function (row) {
      const tr = document.createElement("tr");
      row.forEach(function (value) {
        const td = document.createElement("td");
        td.textContent = value === null || value === undefined ? "" : String(value);
        tr.appendChild(td);
      });
      resultBody.appendChild(tr);
    });
  }

  function selectQuery(idx) {
    const query = allQueries[idx] || allQueries[0];
    sqlEditor.value = query.sql;
    paramsInput.value = query.default_params && query.default_params.length
      ? JSON.stringify(query.default_params)
      : "[]";
  }

  async function runQuery() {
    errorBox.textContent = "";
    const sql = sqlEditor.value.trim();
    if (!sql) return;
    const started = performance.now();
    let params = [];
    const rawParams = paramsInput.value.trim();
    if (rawParams) {
      params = JSON.parse(rawParams);
      if (!Array.isArray(params)) throw new Error("params must be a JSON list");
    }
    const resp = await fetch("/api/sql", {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, authHeaders),
      body: JSON.stringify({ sql: sql, params: params }),
    });
    const payload = await resp.json();
    if (!resp.ok) {
      const message = payload && payload.error && payload.error.message
        ? payload.error.message
        : "HTTP " + resp.status;
      throw new Error(message);
    }
    renderTable(payload.columns || [], payload.rows || []);
    const elapsed = (performance.now() - started).toFixed(1);
    const truncated = payload.truncated ? " (truncated)" : "";
    execInfo.textContent = (payload.rows || []).length + " row(s) in " + elapsed + " ms" + truncated;
  }

  ruleSelect.addEventListener("change", function () {
    selectQuery(Number(ruleSelect.value));
  });
  runBtn.addEventListener("click", function () {
    runQuery().catch(function (err) {
      errorBox.textContent = String(err && err.message ? err.message : err);
    });
  });
  sqlEditor.addEventListener("keydown", function (ev) {
    if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") {
      ev.preventDefault();
      runBtn.click();
    }
  });

  async function bootstrap() {
    const tokenResp = await fetch("/auth/ws-token", { credentials: "include" });
    if (!tokenResp.ok) {
      location.href = "/login";
      return;
    }
    const token = await tokenResp.json();
    authHeaders = { Authorization: "Bearer " + token.access_token };
    const cfgResp = await fetch("/api/sql-rules", { headers: authHeaders });
    if (!cfgResp.ok) throw new Error("SQL rules failed: HTTP " + cfgResp.status);
    const cfg = await cfgResp.json();
    allQueries = QUICK_QUERIES.concat(cfg.rules || []);
    ruleSelect.replaceChildren();
    allQueries.forEach(function (item, idx) {
      const opt = document.createElement("option");
      opt.value = String(idx);
      opt.textContent = item.filename.indexOf("__") === 0
        ? item.name
        : "sql/fdd/rules/" + item.filename;
      ruleSelect.appendChild(opt);
    });
    selectQuery(0);
    statusEl.textContent = "server DuckDB";
    runBtn.disabled = false;
    await runQuery();
  }

  bootstrap().catch(function (err) {
    statusEl.textContent = "error";
    errorBox.textContent = String(err && err.message ? err.message : err);
  });
})();
