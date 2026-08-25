/* "API" tab: start/stop OpenAI-compatible bridges (agent.sh openai-server), see the ones
   already running, and browse each bridge's request logs inline. Each bridge exposes a chosen
   CLI provider+model as a standard /v1/chat/completions endpoint you can point Cursor/Continue/
   any OpenAI client at. Depends on $/esc/spin/jget/jpost (util.js), t() (i18n.js), toast()
   (toast.js), PROVIDERS (populated by refresh() in tree.js) --
   must load after them (see gui.html script order). */

function switchTab(tab) {
  document.querySelectorAll(".tabbtn").forEach(b => b.classList.toggle("on", b.dataset.tab === tab));
  document.querySelectorAll(".tabview").forEach(v => v.classList.toggle("on", v.id === "tabview-" + tab));
  localStorage.setItem("agentgui_tab", tab);
  if (tab === "bridge") refreshBridgeTab();
}

async function syncBridgeModels() {
  const requestId = (syncBridgeModels.requestId || 0) + 1;
  syncBridgeModels.requestId = requestId;
  const e = $("#brg-engine").value;
  const p = PROVIDERS[e] || {};
  $("#brg-model").placeholder = p.resolved_label
    ? t("form.auto") + " → " + p.resolved_label
    : p.default_model ? t("form.auto") + " (" + p.default_model + ")" : t("form.auto");
  const eff = $("#brg-effort");
  const efforts = p.efforts || [];
  eff.innerHTML = `<option value="">${t("form.auto")}</option>` + efforts.map(f => `<option value="${f}">${f}</option>`).join("");
  eff.disabled = !efforts.length;
  try {
    const d = await jget("/api/models?engine=" + encodeURIComponent(e));
    if (requestId !== syncBridgeModels.requestId || $("#brg-engine").value !== e) return;
    setBridgeModelOptions(d.models || []);
  } catch (err) {
    if (requestId === syncBridgeModels.requestId && $("#brg-engine").value === e)
      setBridgeModelOptions(p.models || []);
  }
}

function setBridgeModelOptions(models) {
  $("#brg-models").replaceChildren(...models.map(model => {
    const option = document.createElement("option");
    option.value = String(model);
    return option;
  }));
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
    api_key: ($("#brg-key") ? $("#brg-key").value.trim() : ""),
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
    $("#brg-key").value = "";
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
  // send the instance_id the list was built from so a stale row can't stop a newer bridge
  const row = document.querySelector(`#brg-list .api-run[data-port="${port}"]`);
  const body = { port };
  if (row && row.dataset.instanceId) body.instance_id = row.dataset.instanceId;
  const r = await jpost("/api/bridge/stop", body);
  // ok:false, or "not killed and not a pruned stale registry", means nothing was stopped
  if (r.error || !r.ok || (!r.killed && !r.stale_registry))
    toast("error", t("bridge.stop_failed"), r.error || t("bridge.stop_failed"));
  else toast("success", t("bridge.stopped"), ":" + port);
  setTimeout(refreshBridgeTab, 400);
}

const BRG_OPEN_LOGS = new Set();   // ports whose log panel is open
const BRG_OPEN_REQS = new Set();   // request task names whose transcript is expanded
const _reqListCache = new Map();   // port -> {key, html}; capped below
const _reqBodyCache = new Map();   // name -> {key, html}; capped below
const _reqControllers = new Map();
function boundedCacheSet(cache, key, value, cap = 50) {
  cache.delete(key);
  cache.set(key, value);
  while (cache.size > cap) cache.delete(cache.keys().next().value);
}

async function toggleBridgeLogs(port) {
  if (BRG_OPEN_LOGS.has(port)) BRG_OPEN_LOGS.delete(port);
  else BRG_OPEN_LOGS.add(port);
  await renderBridgeLogs(port);
}

async function renderBridgeLogs(port, suppliedTasks) {
  const row = document.querySelector(`#brg-list .api-run[data-port="${port}"]`);
  if (!row) return;
  let panel = row.querySelector(".brg-logs");
  if (!BRG_OPEN_LOGS.has(port)) { if (panel) panel.remove(); return; }
  if (!panel) {
    panel = document.createElement("div");
    panel.className = "brg-logs";
    row.querySelector(".api-run-body").appendChild(panel);
  }
  if (!panel.innerHTML) panel.innerHTML = spin(t("limits.loading"));
  let reqs = [];
  try {
    const tasks = suppliedTasks || (await jget("/api/tasks")).tasks || [];
    reqs = tasks
      .filter(x => x.name && x.name.indexOf("openai-" + port + "-") === 0)
      .sort((a, b) => (b.updated || 0) - (a.updated || 0));
  } catch (e) {}
  if (!reqs.length) {
    if (BRG_OPEN_LOGS.has(port)) panel.innerHTML = `<div class="note">${t("bridge.no_requests")}</div>`;
    return;
  }
  const pfx = "openai-" + port + "-";
  const cacheKey = document.documentElement.lang + ":" + reqs.map(r =>
    [r.name, r.updated || 0, r.state || ""].join("@")).join("|");
  const cached = _reqListCache.get(port);
  const html = reqs
    .map(r => `<div class="brg-req" data-name="${esc(r.name)}">
        <div class="brg-req-hd">
          <span class="em">${r.act || "•"}</span>
          <span class="mono">${esc(r.name.slice(pfx.length))}</span>
          <span class="sp"></span>
          <span class="note">${esc(r.state)}</span>
        </div></div>`)
    .join("");
  boundedCacheSet(_reqListCache, port, { key: cacheKey, html });
  if (!BRG_OPEN_LOGS.has(port) || !document.body.contains(panel)) return;  // closed while fetching
  if (!cached || cached.key !== cacheKey || panel.dataset.cacheKey !== cacheKey) {
    panel.innerHTML = html;
    panel.dataset.cacheKey = cacheKey;
  }
  panel.querySelectorAll(".brg-req").forEach(req => {
    const header = req.querySelector(".brg-req-hd");
    if (header && !header.dataset.bound) {
      header.dataset.bound = "1";
      header.addEventListener("click", () => toggleBridgeReq(req.dataset.name, header));
    }
    const task = reqs.find(item => item.name === req.dataset.name);
    if (BRG_OPEN_REQS.has(req.dataset.name)) renderBridgeReq(req.dataset.name, req, task);
  });
}

async function toggleBridgeReq(name, hd) {
  if (BRG_OPEN_REQS.has(name)) BRG_OPEN_REQS.delete(name);
  else BRG_OPEN_REQS.add(name);
  await renderBridgeReq(name, hd.closest(".brg-req"));
}

async function renderBridgeReq(name, req, task) {
  if (!req) return;
  let body = req.querySelector(".brg-req-body");
  if (!BRG_OPEN_REQS.has(name)) { if (body) body.remove(); return; }
  if (!body) {
    body = document.createElement("div");
    body.className = "brg-req-body";
    req.appendChild(body);
  }
  const cacheKey = document.documentElement.lang + ":" + (task ? `${task.updated || 0}:${task.state || ""}` : "manual");
  const cached = _reqBodyCache.get(name);
  if (cached && cached.key === cacheKey) {
    body.innerHTML = cached.html;
    return;
  }
  if (!body.innerHTML) body.innerHTML = spin();
  const previous = _reqControllers.get(name);
  if (previous) previous.abort();
  const controller = new AbortController();
  _reqControllers.set(name, controller);
  let prompt = "—";
  let out = "—";
  try {
    const d = await jget("/api/dialog?task=" + encodeURIComponent(name) + "&full=1",
                         { signal: controller.signal });
    const steps = d.steps || [];
    const last = steps[steps.length - 1];
    if (last) {
      prompt = (last.prompt || "").trim() || "—";
      const blocks = last.blocks || [];
      const texts = blocks.filter(b => b.type === "text").map(b => b.text || "");
      let combined = texts.join("\n\n").trim();
      if (!combined) {
        combined = blocks.map(b => b.text || b.result || "").join("\n\n").trim();
      }
      if (combined) out = combined;
    }
  } catch (e) {
    if (e.name === "AbortError") return;
    // graceful fallback keeps "—" placeholders
  }
  const html =
    `<div class="note">${esc(t("bridge.log_prompt"))}</div><pre class="mono brg-pre">${esc(prompt)}</pre>` +
    `<div class="note">${esc(t("bridge.log_output"))}</div><pre class="mono brg-pre">${esc(out)}</pre>`;
  boundedCacheSet(_reqBodyCache, name, { key: cacheKey, html });
  if (_reqControllers.get(name) === controller) _reqControllers.delete(name);
  if (BRG_OPEN_REQS.has(name) && document.body.contains(body)) body.innerHTML = html;
}

function bridgeCurl(rec) {
  const model = rec.model || (PROVIDERS[rec.engine] || {}).default_model || "default";
  const body = JSON.stringify({model, messages: [{role: "user", content: "ping"}]});
  const quote = value => "'" + String(value).replace(/'/g, "'\\''") + "'";
  const lines = [
    "curl " + quote(String(rec.base_url || "") + "/v1/chat/completions"),
    '  -H "Content-Type: application/json"',
  ];
  if (rec.auth) lines.push('  -H "Authorization: Bearer $AGENT_OPENAI_KEY"');
  lines.push("  -d " + quote(body));
  return lines.join(" " + String.fromCharCode(92) + "\n");
}

let _bridgeRefreshing = false, _bridgeRefreshQueued = false, _bridgeRenderKey = "";
async function refreshBridgeTab() {
  if (_bridgeRefreshing) { _bridgeRefreshQueued = true; return; }
  _bridgeRefreshing = true;
  try {
  let d;
  try {
    d = await jget("/api/bridges");
  } catch (e) {
    return;
  }
  const list = $("#brg-list");
  const bridges = d.bridges || [];
  const renderKey = document.documentElement.lang + ":" + JSON.stringify(bridges.map(b => ({
    port: b.port, instance_id: b.instance_id, live: b.live, busy: b.busy, lan: b.lan,
    auth: b.auth, engine: b.engine, model: b.model, label: b.label, effort: b.effort,
    dir: b.dir, base_url: b.base_url, lan_urls: b.lan_urls, public_url: b.public_url,
    requests: b.requests, session_active: (b.health || {}).session_active,
    session_turns: (b.health || {}).session_turns,
  })));
  if (!bridges.length) {
    if (_bridgeRenderKey !== renderKey) list.innerHTML = `<div class="empty">${esc(t("bridge.none"))}</div>`;
    _bridgeRenderKey = renderKey;
    return;
  }
  if (_bridgeRenderKey === renderKey) {
    await refreshOpenBridgeLogs();
    return;
  }
  _bridgeRenderKey = renderKey;
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
      : `<button class="mini" data-action="logs">${esc(t("bridge.logs"))} (${Number(b.requests) || 0})</button>`;
    const reqLine = b.engine === "opencode"
      ? `<div class="kv"><span>${t("bridge.requests")}</span><b>${t("bridge.opencode_proxy")}</b></div>`
      : `<div class="kv"><span>${t("bridge.requests")}</span><b>${b.requests || 0}</b></div>`;
    const row = document.createElement("div");
    row.className = "api-run";
    row.dataset.port = b.port;   // lets renderBridgeLogs find this row after a rebuild
    row.dataset.instanceId = b.instance_id || "";  // stop/restart must name the bridge they target
    row.innerHTML = `
      <div class="api-run-hd">
        <span class="em">${statusEm}</span>
        <b class="mono">${esc(b.base_url)}</b>
        <span class="pill">${esc(b.engine)}</span>
        ${b.lan ? `<span class="pill" title="exposed on the LAN">LAN</span>` : ""}
        <span class="sp"></span>
        <button class="mini" data-action="copy-main">${esc(t("bridge.copy_url"))}</button>
        ${logsBtn}
        <button class="mini danger" data-action="stop">${esc(t("bridge.stop"))}</button>
      </div>
      <div class="api-run-body">
        <div class="kv"><span>${t("form.model")}</span><b>${esc(b.label || b.model || "?")}</b></div>
        <div class="kv"><span>${t("bridge.status")}</span><b>${esc(sess)}</b></div>
        ${reqLine}
        ${(b.lan && (b.lan_urls || []).length)
          ? b.lan_urls.map(u => `<div class="kv"><span>${esc(t("bridge.lan_url"))}</span><b class="mono">${esc(u + "/v1")} <button class="mini" data-copy-url="${esc(u + "/v1")}">${esc(t("bridge.copy_url"))}</button></b></div>`).join("")
          : (b.lan ? `<div class="kv"><span>${t("bridge.lan_url")}</span><b>${t("bridge.lan_unknown")}</b></div>` : "")}
        ${b.public_url
          ? `<div class="kv"><span>${esc(t("bridge.public_url"))}</span><b class="mono">${esc(b.public_url + "/v1")} <button class="mini" data-copy-url="${esc(b.public_url + "/v1")}">${esc(t("bridge.copy_url"))}</button></b></div><div class="note">${esc(t("bridge.public_hint"))}</div>`
          : (b.lan ? `<div class="kv"><span>${t("bridge.public_url")}</span><b>${t("bridge.public_unknown")}</b></div>` : "")}
        ${b.dir ? `<div class="kv"><span>${t("form.project")}</span><b class="mono">${esc(b.dir)}</b></div>` : ""}
        <div class="brg-switch">
          <span class="note">${t("bridge.switch_model")}</span>
          <select class="brg-sw-model"></select>
          <label class="chk"><input type="checkbox" class="brg-sw-local" ${b.lan ? "" : "checked"}> <span>${t("bridge.localhost_short")}</span></label>
          ${b.auth ? `<input type="password" class="brg-sw-key" autocomplete="off" placeholder="API key required to restart">` : ""}
          <button class="mini" data-action="restart">${esc(t("bridge.switch"))}</button>
        </div>
        <div class="snippet"><pre class="mono">${esc(bridgeCurl(b))}</pre><button class="mini" data-action="copy-curl">${esc(t("api.copy"))}</button></div>
      </div>`;
    list.appendChild(row);
    const copyMain = row.querySelector('[data-action="copy-main"]');
    const logs = row.querySelector('[data-action="logs"]');
    const stop = row.querySelector('[data-action="stop"]');
    const restart = row.querySelector('[data-action="restart"]');
    const copyCurl = row.querySelector('[data-action="copy-curl"]');
    copyMain.addEventListener("click", () => copyText(copyMain, v1));
    if (logs) logs.addEventListener("click", () => toggleBridgeLogs(b.port));
    stop.addEventListener("click", event => stopBridge(b.port, event));
    restart.dataset.engine = String(b.engine || "");
    restart.dataset.effort = String(b.effort || "");
    restart.dataset.dir = String(b.dir || "");
    restart.addEventListener("click", () => restartBridge(b.port, restart));
    copyCurl.addEventListener("click", () => copyText(copyCurl, bridgeCurl(b)));
    row.querySelectorAll("[data-copy-url]").forEach(button =>
      button.addEventListener("click", () => copyText(button, button.dataset.copyUrl)));
    fillSwitchModels(row.querySelector(".brg-sw-model"), b.engine, b.model);
  }
  for (const p of [...BRG_OPEN_LOGS]) if (!bridges.some(b => b.port === p)) BRG_OPEN_LOGS.delete(p);
  await refreshOpenBridgeLogs();
  } finally {
    _bridgeRefreshing = false;
    if (_bridgeRefreshQueued) { _bridgeRefreshQueued = false; refreshBridgeTab(); }
  }
}

async function refreshOpenBridgeLogs() {
  if (!BRG_OPEN_LOGS.size) return;
  let tasks = [];
  try { tasks = (await jget("/api/tasks")).tasks || []; }
  catch (err) { return; }
  await Promise.all([...BRG_OPEN_LOGS].map(port => renderBridgeLogs(port, tasks)));
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
  const values = opts.length ? opts : [""];
  sel.replaceChildren(...values.map(model => {
    const option = document.createElement("option");
    option.value = String(model);
    option.textContent = model || t("form.auto");
    option.selected = model === cur;
    return option;
  }));
}

async function restartBridge(port, btn) {
  const row = btn.closest(".api-run");
  const box = btn.closest(".brg-switch");
  const model = box.querySelector(".brg-sw-model").value;
  const localhost = box.querySelector(".brg-sw-local").checked;
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = spin();
  try {
    const body = {
      port, model, localhost,
      engine: btn.dataset.engine, effort: btn.dataset.effort, dir: btn.dataset.dir,
    };
    const keyInput = box.querySelector(".brg-sw-key");
    if (keyInput) body.api_key = keyInput.value.trim();
    // Legacy rows omit identity so the backend can fail closed with the real verification error.
    if (row && row.dataset.instanceId) body.instance_id = row.dataset.instanceId;
    const r = await jpost("/api/bridge/restart", body);
    if (r.error) { toast("error", t("bridge.stop_failed"), r.error); return; }
    toast("success", t("bridge.switched"), (r.base_url || "") + " · " + (model || t("form.auto")));
    [700, 1500, 2600, 4000, 6000].forEach(ms => setTimeout(refreshBridgeTab, ms));
  } finally {
    btn.disabled = false;
    btn.innerHTML = old;
  }
}
