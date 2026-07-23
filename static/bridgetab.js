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
    toast("success", t("bridge.started"),
      r.reassigned ? `${r.base_url} (${t("bridge.port_reassigned")} ${r.asked_port})` : r.base_url);
    // bump the port field so the next launch doesn't collide with this one
    $("#brg-port").value = (r.port || body.port) + 1;
    // the bridge writes its registry file only after it binds (opencode warms up `opencode serve`
    // first) -- poll a few times so a slower-binding bridge still shows up without a manual refresh
    [800, 1600, 2600, 4000, 6000].forEach(ms => setTimeout(refreshBridgeTab, ms));
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

// Inline request logs. Every request (claude/codex/gemini) is a full task named
// openai-<port>-<hex>, so the whole prompt+answer of each call is browsable right here -- no
// jumping to the Tasks tab. Open/expanded state is kept in module Sets so it SURVIVES the tab's
// periodic full rebuild (the list re-renders every few seconds; without this the panel would
// pop open then vanish on the next refresh -- the "opens and immediately closes" bug). Content
// caches make the restore instant (no spinner flash) each time the row is rebuilt.
const BRG_OPEN_LOGS = new Set();   // ports whose log panel is open
const BRG_OPEN_REQS = new Set();   // request task names whose transcript is expanded
const _reqListCache = {};          // port -> requests-list innerHTML
const _reqBodyCache = {};          // name -> transcript innerHTML

// A real toggle: flip the port's open state, then (re)render to match it.
async function toggleBridgeLogs(port) {
  if (BRG_OPEN_LOGS.has(port)) BRG_OPEN_LOGS.delete(port);
  else BRG_OPEN_LOGS.add(port);
  await renderBridgeLogs(port);
}

// Reconcile one bridge's log panel with BRG_OPEN_LOGS. Safe to call after any list rebuild.
async function renderBridgeLogs(port) {
  const row = document.querySelector(`#brg-list .api-run[data-port="${port}"]`);
  if (!row) return;
  let panel = row.querySelector(".brg-logs");
  if (!BRG_OPEN_LOGS.has(port)) { if (panel) panel.remove(); return; }
  if (!panel) {
    panel = document.createElement("div");
    panel.className = "brg-logs";
    row.querySelector(".api-run-body").appendChild(panel);
  }
  if (!panel.innerHTML) panel.innerHTML = _reqListCache[port] || spin(t("limits.loading"));
  let reqs = [];
  try {
    const d = await jget("/api/tasks");
    reqs = (d.tasks || [])
      .filter(x => x.name && x.name.indexOf("openai-" + port + "-") === 0)
      .sort((a, b) => (b.updated || 0) - (a.updated || 0));
  } catch (e) {}
  if (!reqs.length) {
    if (BRG_OPEN_LOGS.has(port)) panel.innerHTML = `<div class="note">${t("bridge.no_requests")}</div>`;
    return;
  }
  const pfx = "openai-" + port + "-";
  const html = reqs
    .map(r => `<div class="brg-req" data-name="${esc(r.name)}">
        <div class="brg-req-hd" onclick="toggleBridgeReq('${esc(r.name)}', this)">
          <span class="em">${r.act || "•"}</span>
          <span class="mono">${esc(r.name.slice(pfx.length))}</span>
          <span class="sp"></span>
          <span class="note">${esc(r.state)}</span>
        </div></div>`)
    .join("");
  _reqListCache[port] = html;
  if (!BRG_OPEN_LOGS.has(port) || !document.body.contains(panel)) return;  // closed while fetching
  panel.innerHTML = html;
  panel.querySelectorAll(".brg-req").forEach(req => {
    if (BRG_OPEN_REQS.has(req.dataset.name)) renderBridgeReq(req.dataset.name, req);
  });
}

// A real toggle for one request's transcript, likewise state-tracked so it survives rebuilds.
async function toggleBridgeReq(name, hd) {
  if (BRG_OPEN_REQS.has(name)) BRG_OPEN_REQS.delete(name);
  else BRG_OPEN_REQS.add(name);
  await renderBridgeReq(name, hd.closest(".brg-req"));
}

async function renderBridgeReq(name, req) {
  if (!req) return;
  let body = req.querySelector(".brg-req-body");
  if (!BRG_OPEN_REQS.has(name)) { if (body) body.remove(); return; }
  if (!body) {
    body = document.createElement("div");
    body.className = "brg-req-body";
    req.appendChild(body);
  }
  if (!body.innerHTML) body.innerHTML = _reqBodyCache[name] || spin();  // instant restore from cache
  let log = "";
  try { log = (await jget("/api/thread?task=" + encodeURIComponent(name))).log || ""; } catch (e) {}
  const msgs = parseThread(log);
  const last = msgs[msgs.length - 1] || { inp: [], out: [] };
  const prompt = last.inp.join("\n").trim() || "—";
  const out = last.out.join("\n").trim() || "—";
  const html =
    `<div class="note">${t("bridge.log_prompt")}</div><pre class="mono brg-pre">${esc(prompt)}</pre>` +
    `<div class="note">${t("bridge.log_output")}</div><pre class="mono brg-pre">${esc(out)}</pre>`;
  _reqBodyCache[name] = html;
  if (BRG_OPEN_REQS.has(name) && document.body.contains(body)) body.innerHTML = html;
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
      : `<button class="mini" onclick="toggleBridgeLogs(${b.port})">${t("bridge.logs")} (${b.requests || 0})</button>`;
    const reqLine = b.engine === "opencode"
      ? `<div class="kv"><span>${t("bridge.requests")}</span><b>${t("bridge.opencode_proxy")}</b></div>`
      : `<div class="kv"><span>${t("bridge.requests")}</span><b>${b.requests || 0}</b></div>`;
    const row = document.createElement("div");
    row.className = "api-run";
    row.dataset.port = b.port;   // lets renderBridgeLogs find this row after a rebuild
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
        ${(b.lan && (b.lan_urls || []).length)
          ? b.lan_urls.map(u => `<div class="kv"><span>${t("bridge.lan_url")}</span><b class="mono">${esc(u + "/v1")} <button class="mini" onclick="copyText(this, ${JSON.stringify(u + "/v1").replace(/"/g, "&quot;")})">${t("bridge.copy_url")}</button></b></div>`).join("")
          : (b.lan ? `<div class="kv"><span>${t("bridge.lan_url")}</span><b>${t("bridge.lan_unknown")}</b></div>` : "")}
        ${b.public_url
          ? `<div class="kv"><span>${t("bridge.public_url")}</span><b class="mono">${esc(b.public_url + "/v1")} <button class="mini" onclick="copyText(this, ${JSON.stringify(b.public_url + "/v1").replace(/"/g, "&quot;")})">${t("bridge.copy_url")}</button></b></div><div class="note">${t("bridge.public_hint")}</div>`
          : (b.lan ? `<div class="kv"><span>${t("bridge.public_url")}</span><b>${t("bridge.public_unknown")}</b></div>` : "")}
        ${b.dir ? `<div class="kv"><span>${t("form.project")}</span><b class="mono">${esc(b.dir)}</b></div>` : ""}
        <div class="brg-switch">
          <span class="note">${t("bridge.switch_model")}</span>
          <select class="brg-sw-model"></select>
          <label class="chk"><input type="checkbox" class="brg-sw-local" ${b.lan ? "" : "checked"}> <span>${t("bridge.localhost_short")}</span></label>
          <button class="mini" data-engine="${esc(b.engine)}" data-effort="${esc(b.effort || "")}" data-dir="${esc(b.dir || "")}" onclick="restartBridge(${b.port}, this)">${t("bridge.switch")}</button>
        </div>
        <div class="snippet"><pre class="mono">${esc(bridgeCurl(b))}</pre><button class="mini" onclick="copyText(this, ${JSON.stringify(bridgeCurl(b)).replace(/"/g, "&quot;")})">${t("api.copy")}</button></div>
      </div>`;
    list.appendChild(row);
    fillSwitchModels(row.querySelector(".brg-sw-model"), b.engine, b.model);
  }
  // Restore inline log panels the user had open -- the rebuild above wiped them from the DOM, but
  // the open state lives in BRG_OPEN_LOGS so we re-render it (this is what makes "logs" a real
  // toggle instead of a panel that reappears-then-vanishes on every periodic refresh).
  for (const p of [...BRG_OPEN_LOGS]) if (!bridges.some(b => b.port === p)) BRG_OPEN_LOGS.delete(p);
  for (const p of BRG_OPEN_LOGS) renderBridgeLogs(p);
}

// --- switch a running bridge's model / local-vs-LAN binding in place (stop + relaunch same port) ---
const _swModelCache = {};

async function fillSwitchModels(sel, engine, current) {
  if (!sel) return;
  let models = _swModelCache[engine];
  if (!models) {
    try { models = (await jget("/api/models?engine=" + encodeURIComponent(engine))).models || []; }
    catch (e) { models = (PROVIDERS[engine] || {}).models || []; }
    _swModelCache[engine] = models;
  }
  const cur = current || "";
  const opts = (cur && !models.includes(cur)) ? [cur, ...models] : models.slice();
  sel.innerHTML = opts.length
    ? opts.map(m => `<option value="${esc(m)}" ${m === cur ? "selected" : ""}>${esc(m)}</option>`).join("")
    : `<option value="">${t("form.auto")}</option>`;
}

async function restartBridge(port, btn) {
  const box = btn.closest(".brg-switch");
  const model = box.querySelector(".brg-sw-model").value;
  const localhost = box.querySelector(".brg-sw-local").checked;
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = spin();
  try {
    const r = await jpost("/api/bridge/restart", {
      port, model, localhost,
      engine: btn.dataset.engine, effort: btn.dataset.effort, dir: btn.dataset.dir,
    });
    if (r.error) { toast("error", t("bridge.stop_failed"), r.error); return; }
    toast("success", t("bridge.switched"), (r.base_url || "") + " · " + (model || t("form.auto")));
    [700, 1500, 2600, 4000, 6000].forEach(ms => setTimeout(refreshBridgeTab, ms));
  } finally {
    btn.disabled = false;
    btn.innerHTML = old;
  }
}
