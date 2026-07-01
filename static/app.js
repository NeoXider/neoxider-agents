/* Entry point: run/reply actions, the model+effort picker, the cached/refreshable limits
   panel, and final wiring. Loaded last -- everything else is already defined by this point. */

function syncModels() {
  const e = $("#f-engine").value;
  const p = PROVIDERS[e] || {};
  $("#models").innerHTML = (p.models || []).map(m => `<option value="${m}">`).join("");
  $("#f-model").placeholder = p.default_model ? t("form.auto") + " (" + p.default_model + ")" : t("form.auto");

  const effSel = $("#f-effort");
  const efforts = p.efforts || [];
  if (efforts.length) {
    effSel.innerHTML =
      `<option value="">${t("form.auto")}${p.default_effort ? " (" + p.default_effort + ")" : ""}</option>` +
      efforts.map(f => `<option value="${f}">${f}</option>`).join("");
    effSel.disabled = false;
  } else {
    effSel.innerHTML = `<option value="">${t("form.auto")}</option>`;
    effSel.disabled = true;
  }
  loadProvider();
}

async function submitRun() {
  const body = {
    engine: $("#f-engine").value,
    model: $("#f-model").value.trim(),
    effort: $("#f-effort").value,
    dir: $("#f-dir").value.trim(),
    progress: $("#f-prog").checked,
    terminal: $("#f-term").checked,
    prompt: $("#f-prompt").value.trim(),
  };
  if (!body.prompt) {
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
    toast("success", t("toast.task_started"), body.dir ? base(body.dir) : "");
    $("#f-prompt").value = "";
    setTimeout(refresh, 600);
  } finally {
    btn.disabled = false;
    btn.innerHTML = old;
  }
}

async function sendReply() {
  const a = $("#answer").value.trim();
  if (!a || !SEL) return;
  const btn = $("#btn-reply");
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = spin();
  try {
    const r = await jpost("/api/reply", { task: SEL, answer: a, terminal: $("#r-term").checked });
    if (r.error) {
      toast("error", t("toast.reply_not_sent"), r.error);
      return;
    }
    $("#answer").value = "";
    setTimeout(refresh, 600);
  } finally {
    btn.disabled = false;
    btn.innerHTML = old;
  }
}
$("#answer").addEventListener("keydown", e => {
  if (e.ctrlKey && e.key === "Enter") sendReply();
});

/* ---------- limits panel (for the selected provider); cached server-side, with a manual
   refresh button that bypasses the cache. Never blanks the panel while refreshing -- keeps
   showing the last-known-good data with a small inline spinner until new data arrives. ---------- */
async function loadProvider(force) {
  const e = $("#f-engine").value || "codex";
  $("#lim-title").innerHTML = `${t("limits.title")} · ${(PROVIDERS[e] || {}).label || e}
    <button class="mini icon" onclick="loadProvider(true)" data-i18n-title="limits.refresh" title="${t("limits.refresh")}">⟳</button>`;
  const box = $("#limits");
  if (!box.dataset.loaded) box.innerHTML = `<span class="empty">${spin(t("limits.loading"))}</span>`;
  const d = await jget("/api/provider?engine=" + encodeURIComponent(e) + (force ? "&force=1" : ""));
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
function onLocaleChanged() {
  syncModels();
  if (SEL) { lastLog = ""; refresh(); }
}

makeResizer("rez-left", "left", "left");
makeResizer("rez-right", "right", "right");

(async function init() {
  await initI18n();
  updateHistBadge();
  refresh();
  loadProvider();
  setInterval(refresh, 3000);
  setInterval(() => loadProvider(), 15000);
})();
