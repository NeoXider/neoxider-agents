/* "LLM API" tab: start/stop OpenAI-compatible bridges (agent.sh openai-server) and see the
   ones already running. Each bridge exposes a chosen CLI provider+model as a standard
   /v1/chat/completions endpoint you can point Cursor/Continue/any OpenAI client at.
   Depends on $/esc/spin/jget/jpost (util.js), t() (i18n.js), toast() (toast.js), PROVIDERS
   (populated by refresh() in tree.js) -- must load after them (see gui.html script order). */

// opencode has a rich dynamic catalog (provider/model) fetched server-side; other engines use
// the static provider.json list. Populate the datalist + effort dropdown for the picked engine.
async function syncBridgeModels() {
  const e = $("#brg-engine").value;
  const p = PROVIDERS[e] || {};
  $("#brg-model").placeholder = p.resolved_label
    ? t("form.auto") + " → " + p.resolved_label
    : p.default_model ? t("form.auto") + " (" + p.default_model + ")" : t("form.auto");
  const eff = $("#brg-effort");
  const efforts = p.efforts || [];
  eff.innerHTML = `<option value="">${t("form.auto")}</option>` + efforts.map(f => `<option value="${f}">${f}</option>`).join("");
  eff.disabled = !efforts.length;
  // dynamic model list (opencode = live `opencode models`, others = provider.json)
  try {
    const d = await jget("/api/models?engine=" + encodeURIComponent(e));
    $("#brg-models").innerHTML = (d.models || []).map(m => `<option value="${esc(m)}">`).join("");
  } catch (err) {
    $("#brg-models").innerHTML = (p.models || []).map(m => `<option value="${esc(m)}">`).join("");
  }
}

async function submitBridgeStart() {
  const body = {
    engine: $("#brg-engine").value,
    model: $("#brg-model").value.trim(),
    effort: $("#brg-effort").value,
    port: parseInt($("#brg-port").value, 10) || 8801,
    dir: $("#brg-dir").value.trim(),
    localhost: $("#brg-localhost").checked,
    terminal: $("#brg-term").checked,
  };
  const btn = $("#btn-brg-start");
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = spin();
  try {
    const r = await jpost("/api/bridge/start", body);
    if (r.error) {
      toast("error", t("toast.not_started"), r.error);
      return;
    }
    toast("success", t("bridge.started"), r.base_url);
    // the bridge writes its registry file only after it binds -- give it a beat, then refresh
    setTimeout(refreshBridgeTab, 1200);
    setTimeout(refreshBridgeTab, 3000);
  } finally {
    btn.disabled = false;
    btn.innerHTML = old;
  }
}

async function stopBridge(port, e) {
  e && e.stopPropagation();
  const r = await jpost("/api/bridge/stop", { port });
  if (r.error) toast("error", t("bridge.stop_failed"), r.error);
  else toast("success", t("bridge.stopped"), ":" + port);
  setTimeout(refreshBridgeTab, 400);
}

// Jump to the Tasks tab and open the newest request transcript for this bridge. Every request
// (claude/codex/gemini) is a full task named openai-<port>-<hex> -- so the entire prompt+answer
// of each call is already browsable there, no separate log viewer needed.
async function viewBridgeLogs(port) {
  switchTab("tasks");
  try {
    const d = await jget("/api/tasks");
    const mine = (d.tasks || [])
      .filter(x => x.name && x.name.indexOf("openai-" + port + "-") === 0)
      .sort((a, b) => (b.updated || 0) - (a.updated || 0));
    if (mine.length) select(mine[0].name);
    else toast("success", t("bridge.logs"), t("bridge.no_requests"));
  } catch (e) {}
}

function bridgeCurl(rec) {
  const model = rec.model || (PROVIDERS[rec.engine] || {}).default_model || "default";
  return `curl ${rec.base_url}/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -d '{"model":"${esc(model)}","messages":[{"role":"user","content":"ping"}]}'`;
}

async function refreshBridgeTab() {
  let d;
  try {
    d = await jget("/api/bridges");
  } catch (e) {
    return;
  }
  const list = $("#brg-list");
  const bridges = d.bridges || [];
  if (!bridges.length) {
    list.innerHTML = `<div class="empty">${t("bridge.none")}</div>`;
    return;
  }
  list.innerHTML = "";
  for (const b of bridges) {
    const h = b.health || {};
    const statusEm = b.live ? "🟢" : "⚠️";
    const sess = b.live
      ? (h.session_active ? `${t("bridge.session_on")} · ${h.session_turns || 0} ${t("bridge.turns")}` : t("bridge.session_idle"))
      : t("bridge.unreachable");
    const v1 = b.base_url + "/v1";
    // opencode proxies to `opencode serve` and doesn't create per-request task logs; every other
    // engine spawns an openai-<port>-* task whose full transcript shows up in the Tasks tab.
    const logsBtn = b.engine === "opencode"
      ? ""
      : `<button class="mini" onclick="viewBridgeLogs(${b.port})">${t("bridge.logs")} (${b.requests || 0})</button>`;
    const reqLine = b.engine === "opencode"
      ? `<div class="kv"><span>${t("bridge.requests")}</span><b>${t("bridge.opencode_proxy")}</b></div>`
      : `<div class="kv"><span>${t("bridge.requests")}</span><b>${b.requests || 0}</b></div>`;
    const row = document.createElement("div");
    row.className = "api-run";
    row.innerHTML = `
      <div class="api-run-hd">
        <span class="em">${statusEm}</span>
        <b class="mono">${esc(b.base_url)}</b>
        <span class="pill pe-${esc(b.engine)}">${esc(b.engine)}</span>
        ${b.lan ? `<span class="pill" title="exposed on the LAN">LAN</span>` : ""}
        <span class="sp"></span>
        <button class="mini" onclick="copyText(this, ${JSON.stringify(v1).replace(/"/g, "&quot;")})">${t("bridge.copy_url")}</button>
        ${logsBtn}
        <button class="mini danger" onclick="stopBridge(${b.port}, event)">${t("bridge.stop")}</button>
      </div>
      <div class="api-run-body">
        <div class="kv"><span>${t("form.model")}</span><b>${esc(b.label || b.model || "?")}</b></div>
        <div class="kv"><span>${t("bridge.status")}</span><b>${esc(sess)}</b></div>
        ${reqLine}
        ${b.dir ? `<div class="kv"><span>${t("form.project")}</span><b class="mono">${esc(b.dir)}</b></div>` : ""}
        <div class="snippet"><pre class="mono">${esc(bridgeCurl(b))}</pre><button class="mini" onclick="copyText(this, ${JSON.stringify(bridgeCurl(b)).replace(/"/g, "&quot;")})">${t("api.copy")}</button></div>
      </div>`;
    list.appendChild(row);
  }
}
