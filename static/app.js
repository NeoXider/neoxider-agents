/* Entry point: run/reply actions, the model+effort picker, the cached/refreshable limits
   panel, and final wiring. Loaded last -- everything else is already defined by this point. */

const modelDetails = {}; // engine -> /api/models details (new-task form, API tab, composer.js)

function syncModelEfforts(prefix) {
  const engine = $("#" + prefix + "-engine").value;
  const model = $("#" + prefix + "-model").value.trim();
  const provider = PROVIDERS[engine] || {};
  const details = (modelDetails[engine] || []).find(item => item.id === model);
  const advertised = details && details.effort_source === "cli-advertised";
  const modes = advertised ? details.efforts : (provider.efforts || []);
  const select = $("#" + prefix + "-effort");
  const previous = select.value;
  select.replaceChildren(...["", ...modes].map(mode => {
    const option = document.createElement("option");
    option.value = mode;
    option.textContent = mode ? mode + (advertised ? "" : " (unverified)") : t("form.auto");
    return option;
  }));
  select.value = modes.includes(previous) ? previous : "";
  select.disabled = !modes.length;
  select.title = advertised ? "Modes advertised by this model; effective execution needs a probe."
    : "This model does not advertise modes. Provider hints are unverified; availability and quota are unknown.";
  const info = $("#" + prefix + "-model-info");
  if (info) {
    const caps = (details && details.capabilities) || {};
    info.textContent = details ? `context: ${(details.limits || {}).context ?? "unknown"} · reasoning: ${caps.reasoning ?? "unknown"} · tools: ${caps.toolcall ?? "unknown"} · free: ${details.free_advertised ?? "unknown"} · availability: unprobed`
      : "Model metadata unknown; availability and quota are unprobed.";
  }
}

async function syncModels() {
  const requestId = (syncModels.requestId || 0) + 1;
  syncModels.requestId = requestId;
  const e = $("#f-engine").value;
  const p = PROVIDERS[e] || {};
  $("#models").replaceChildren(...(p.models || []).map(model => {
    const option = document.createElement("option");
    option.value = String(model);
    return option;
  }));
  $("#f-model").placeholder = p.resolved_label ? t("form.auto") + " → " + p.resolved_label : p.default_model ? t("form.auto") + " (" + p.default_model + ")" : t("form.auto");

  syncModelEfforts("f");
  loadProvider();
  try {
    const data = await jget("/api/models?engine=" + encodeURIComponent(e));
    if (requestId !== syncModels.requestId || $("#f-engine").value !== e) return;
    modelDetails[e] = data.details || [];
    $("#models").replaceChildren(...(data.models || []).map(model => {
      const option = document.createElement("option");
      option.value = String(model);
      return option;
    }));
    syncModelEfforts("f");
  } catch (err) {
    if (requestId === syncModels.requestId && $("#f-engine").value === e) {
      modelDetails[e] = [];
      syncModelEfforts("f");
    }
  }
}

async function submitRun() {
  const body = {
    engine: $("#f-engine").value,
    model: $("#f-model").value.trim(),
    effort: $("#f-effort").value,
    dir: $("#f-dir").value.trim(),
    progress: $("#f-prog").checked,
    terminal: $("#f-term").checked,
    prompt: $("#f-prompt").value,
  };
  if (!body.prompt.trim()) {
    toast("error", t("toast.empty_prompt_title"), t("toast.empty_prompt_text"));
    return;
  }
  const btn = $("#btn-run");
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = spin();
  try {
    const r = await jpost("/api/run", body);
    if (r.error) {
      toast("error", t("toast.not_started"), r.error);
      return;
    }
    toast("info", t("fleet.submission_started"), t("fleet.run_pending"));
    $("#f-prompt").value = "";
    setTimeout(refresh, 600);
  } finally {
    btn.disabled = false;
    btn.innerHTML = old;
  }
}

/* ---------- limits panel (for the selected provider); cached server-side, with a manual
   refresh button that bypasses the cache. Never blanks the panel while refreshing -- keeps
   showing the last-known-good data with a small inline spinner until new data arrives. ---------- */
let providerController = null, providerRequestId = 0;
async function loadProvider(force) {
  const e = $("#f-engine").value || "codex";
  if (providerController) providerController.abort();
  const controller = new AbortController();
  providerController = controller;
  const requestId = ++providerRequestId;
  const title = $("#lim-title");
  title.textContent = `${t("limits.title")} · ${(PROVIDERS[e] || {}).label || e} `;
  const refreshButton = document.createElement("button");
  refreshButton.className = "mini icon";
  refreshButton.title = t("limits.refresh");
  refreshButton.textContent = "⟳";
  refreshButton.addEventListener("click", () => loadProvider(true));
  title.appendChild(refreshButton);
  const box = $("#limits");
  if (!box.dataset.loaded) box.innerHTML = `<span class="empty">${spin(t("limits.loading"))}</span>`;
  let d;
  try {
    d = await jget("/api/provider?engine=" + encodeURIComponent(e) + (force ? "&force=1" : ""),
      { signal: controller.signal });
  } catch (err) {
    if (err.name === "AbortError") return;
    throw err;
  } finally {
    if (providerController === controller) providerController = null;
  }
  if (requestId !== providerRequestId || controller.signal.aborted ||
      ($("#f-engine").value || "codex") !== e) return;
  if (!d.available) {
    box.innerHTML = `<span class="empty">${esc(e)} ${t("limits.not_found")}</span>`;
    box.dataset.loaded = "1";
    return;
  }
  let html = `<div class="kv"><span>${t("limits.version")}</span><b>${esc((d.version || "?").slice(0, 32))}</b></div>`;
  if (d.login) html += `<div class="kv"><span>${t("limits.login")}</span><b>${esc(d.login)}</b></div>`;
  if (d.limits) {
    const rl = d.limits, now = d.now;
    const w = (win, lbl) => {
      if (!win) return "";
      const up = win.used_percent || 0, cls = up >= 80 ? "hi" : up >= 50 ? "mid" : "";
      let left = "";
      if (win.resets_at) {
        const s = win.resets_at - now;
        left = s > 0 ? `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m` : "soon";
      }
      const wl = win.window_minutes ? (win.window_minutes % 1440 === 0 ? win.window_minutes / 1440 + "d" : Math.round(win.window_minutes / 60) + "h") : "";
      return `<div class="lim"><div class="t"><span>${lbl} (${wl})</span><span>${up}% · ${left}</span></div>
        <div class="bar"><i class="${cls}" style="width:${up}%"></i></div></div>`;
    };
    html += `<div class="kv"><span>${t("limits.plan")}</span><b>${rl.plan_type || "?"}</b></div>` + w(rl.primary, "primary") + w(rl.secondary, "secondary");
  } else if (d.note) {
    html += `<div class="note">${esc(d.note)}</div>`;
  }
  if (d.cached) html += `<div class="note">${t("limits.cached")}</div>`;
  box.innerHTML = html;
  box.dataset.loaded = "1";
}

/* re-render whatever's currently dynamic when the language changes (static text is handled by
   applyI18n()'s data-i18n scan; these bits build their own HTML strings with t() baked in) */
async function onLocaleChanged() {
  if (fleetController && fleetController.active) await fleetController.refresh();
  syncModels();
  lastTreeHtml = "";
  if (!SEL) await refresh();
  if (SEL) {
    lastLog = "";
    const task = CURRENT_TASKS.get(SEL);
    if (task) { fillComposerModels(task); renderOutbox(task.name); }
    await refresh();
  }
  if (typeof syncBridgeModels === "function" && $("#brg-engine") && $("#brg-engine").options.length) {
    await syncBridgeModels();
    if (localStorage.getItem("agentgui_tab") === "bridge") await refreshBridgeTab();
  }
}

makeResizer("rez-left", "left", "left");
makeResizer("rez-right", "right", "right");

$("#btn-doctor").addEventListener("click", () => openDoctor());
$("#btn-history").addEventListener("click", openHistory);
$("#btn-refresh").addEventListener("click", refresh);
document.querySelectorAll(".tabbtn[data-tab]").forEach(button =>
  button.addEventListener("click", () => {
    switchTab(button.dataset.tab);
    if (fleetController) fleetController.activate(button.dataset.tab === "fleet");
  }));
$("#btn-add-project").addEventListener("click", () => openBrowse("add-project"));
$("#btn-pick-field").addEventListener("click", () => openBrowse("pick-field"));
$("#f-engine").addEventListener("change", syncModels);
$("#f-model").addEventListener("input", () => syncModelEfforts("f"));
$("#btn-run").addEventListener("click", submitRun);
$("#brg-engine").addEventListener("change", syncBridgeModels);
$("#brg-model").addEventListener("input", () => syncModelEfforts("brg"));
$("#btn-pick-bridge").addEventListener("click", () => openBrowse("pick-bridge"));
$("#btn-brg-start").addEventListener("click", submitBridgeStart);
$("#btn-brg-refresh").addEventListener("click", refreshBridgeTab);
$("#btn-doctor-refresh").addEventListener("click", refreshDoctor);
$("#btn-doctor-deep").addEventListener("click", deepDoctor);
$("#btn-history-clear").addEventListener("click", clearHistory);
$("#btn-browse-choose").addEventListener("click", chooseBrowsed);
document.querySelectorAll("[data-close-modal]").forEach(button =>
  button.addEventListener("click", () => closeModal(button.dataset.closeModal)));

(async function init() {
  await initI18n();
  initThemeToggle();
  updateHistBadge();
  await refresh(); // awaited once so CWD/ENGINES/PROVIDERS are populated before the API tab reads them
  loadProvider();

  let savedTab = localStorage.getItem("agentgui_tab") || "tasks";
  if (!["tasks", "bridge", "fleet"].includes(savedTab)) savedTab = "tasks";
  if (!$("#brg-engine").options.length) {
    // default the bridge provider to opencode + big-pickle when present (its dynamic catalog is the point)
    $("#brg-engine").replaceChildren(...ENGINES.map(engine => {
      const option = document.createElement("option");
      option.value = engine;
      option.textContent = (PROVIDERS[engine] || {}).label || engine;
      return option;
    }));
    if (ENGINES.includes("opencode")) {
      $("#brg-engine").value = "opencode";
      if (!$("#brg-model").value) $("#brg-model").value = "opencode/big-pickle";
    }
    $("#brg-dir").value = CWD;
    syncBridgeModels();
  }
  switchTab(savedTab);
  if (fleetController) fleetController.activate(savedTab === "fleet");

  setInterval(refresh, 3000);
  setInterval(() => { if (fleetController && fleetController.active) fleetController.refresh(); }, 3000);
  setInterval(pollOutbox, 3000);
  setInterval(() => loadProvider(), 15000);
  // keep the bridge list fresh (health/session) only while that tab is visible
  setInterval(() => { if (localStorage.getItem("agentgui_tab") === "bridge") refreshBridgeTab(); }, 4000);
})();
