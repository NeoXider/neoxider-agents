/* "API" tab: start/stop OpenAI-compatible bridges (agent.sh openai-server), see the ones
   already running, and browse each bridge's request logs inline. Each bridge exposes a chosen
   CLI provider+model as a standard /v1/chat/completions endpoint you can point Cursor/Continue/
   any OpenAI client at. Depends on $/esc/spin/jget/jpost (util.js), t() (i18n.js), toast()
   (toast.js), parseThread()/md() (chat.js), PROVIDERS (populated by refresh() in tree.js) --
   must load after them (see gui.html script order). */

// Tab switcher (the two remaining tabs: Tasks + API). Kept here since the API tab is the only
// non-Tasks view now that the standalone test-api tab was folded away.
function switchTab(tab) {
  document.querySelectorAll(".tabbtn").forEach(b => b.classList.toggle("on", b.dataset.tab === tab));
  document.querySelectorAll(".tabview").forEach(v => v.classList.toggle("on", v.id === "tabview-" + tab));
  localStorage.setItem("agentgui_tab", tab);
  if (tab === "bridge") refreshBridgeTab();
}

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

// Toggle an inline request-log panel under a bridge row. Every request (claude/codex/gemini) is
// a full task named openai-<port>-<hex>, so the whole prompt+answer of each call is browsable
// right here -- no jumping to the Tasks tab. Click a request to expand its transcript in place.
async function toggleBridgeLogs(port, btn) {
  const row = btn.closest(".api-run");
  const existing = row.querySelector(".brg-logs");
  if (existing) { existing.remove(); return; }
  const panel = document.createElement("div");
  panel.className = "brg-logs";
  panel.innerHTML = spin(t("limits.loading"));
  row.querySelector(".api-run-body").appendChild(panel);
  let reqs = [];
  try {
    const d = await jget("/api/tasks");
    reqs = (d.tasks || [])
      .filter(x => x.name && x.name.indexOf("openai-" + port + "-") === 0)
      .sort((a, b) => (b.updated || 0) - (a.updated || 0));
  } catch (e) {}
  if (!reqs.length) {
    panel.innerHTML = `<div class="note">${t("bridge.no_requests")}</div>`;
    return;
  }
  const pfx = "openai-" + port + "-";
  panel.innerHTML = reqs
    .map(r => `<div class="brg-req">
        <div class="brg-req-hd" onclick="toggleBridgeReq(this, '${esc(r.name)}')">
          <span class="em">${r.act || "•"}</span>
          <span class="mono">${esc(r.name.slice(pfx.length))}</span>
          <span class="sp"></span>
          <span class="note">${esc(r.state)}</span>
        </div></div>`)
    .join("");
}

// Expand/collapse one request's full transcript (prompt + model output) inline.
async function toggleBridgeReq(hd, name) {
  const req = hd.parentElement;
  const open = req.querySelector(".brg-req-body");
  if (open) { open.remove(); return; }
  const body = document.createElement("div");
  body.className = "brg-req-body";
  body.innerHTML = spin();
  req.appendChild(body);
  let log = "";
  try { log = (await jget("/api/thread?task=" + encodeURIComponent(name))).log || ""; } catch (e) {}
  const msgs = parseThread(log);
  const last = msgs[msgs.length - 1] || { inp: [], out: [] };
  const prompt = last.inp.join("\n").trim() || "—";
  const out = last.out.join("\n").trim() || "—";
  body.innerHTML =
    `<div class="note">${t("bridge.log_prompt")}</div><pre class="mono brg-pre">${esc(prompt)}</pre>` +
    `<div class="note">${t("bridge.log_output")}</div><pre class="mono brg-pre">${esc(out)}</pre>`;
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
    const statusEm = !b.live ? "⚠️" : b.busy ? "🟡" : "🟢";
    const sess = !b.live
      ? t("bridge.unreachable")
      : b.busy
      ? t("bridge.busy")
      : h.session_active ? `${t("bridge.session_on")} · ${h.session_turns || 0} ${t("bridge.turns")}` : t("bridge.session_idle");
    const v1 = b.base_url + "/v1";
    // opencode proxies to `opencode serve` and doesn't create per-request task logs; every other
    // engine spawns an openai-<port>-* task whose full transcript shows up in the Tasks tab.
    const logsBtn = b.engine === "opencode"
      ? ""
      : `<button class="mini" onclick="toggleBridgeLogs(${b.port}, this)">${t("bridge.logs")} (${b.requests || 0})</button>`;
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
