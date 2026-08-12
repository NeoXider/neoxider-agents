/* Doctor modal (cached, with a manual refresh button) + folder browser modal.
   Uses $/esc/spin/base/jget/jpost from util.js, t() from i18n.js, toast()/refresh() elsewhere. */

function closeModal(id) {
  $("#" + id).classList.remove("on");
}
document.addEventListener("keydown", e => {
  if (e.key === "Escape") { closeModal("m-doc"); closeModal("m-hist"); closeModal("m-browse"); }
});

/* ---------- doctor (cache-first; structured data, background refresh) ---------- */
function doctorEsc(s) { const x = document.createElement("span"); x.textContent = s == null ? "" : String(s); return x.innerHTML; }
function doctorWindowName(w) { return w.label || (w.window_minutes ? (w.window_minutes % 1440 === 0 ? (w.window_minutes / 1440) + "d" : (w.window_minutes / 60) + "h") : t("doctor.window")); }
function doctorReset(w) { if (!w.resets_at) return ""; const sec = Math.max(0, Math.floor(w.resets_at - Date.now() / 1000)); if (!sec) return t("doctor.reset_soon"); const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60); return t("doctor.reset_in").replace("{time}", h + "h " + m + "m"); }
function doctorTokens(n) { if (n >= 1e9) return (n / 1e9).toFixed(1) + "B"; if (n >= 1e6) return (n / 1e6).toFixed(1) + "M"; if (n >= 1e3) return Math.round(n / 1e3) + "k"; return String(n || 0); }
function renderDoctor(d) {
  const p = $("#doc"), engines = d.engines || [];
  const codex = engines.find(x => x.engine === "codex"), claude = engines.find(x => x.engine === "claude");
  let h = '<div class="doctor-status">' + (d.cached ? t("doctor.cached") : t("doctor.checking")) + (d.stale ? " · " + t("doctor.stale") : "") + '</div><div class="doctor-limits">';
  const liveLimits = x => ((x || {}).limits || {}).windows || (x && x.limits ? [x.limits.primary, x.limits.secondary].filter(Boolean) : []);
  const limitCard = (engine, title) => { const wins = liveLimits(engine); if (!wins.length) return ""; return wins.map(w => { const used = Math.max(0, Math.min(100, Number(w.used_percent ?? w.utilization ?? 0))); const cls = used >= 80 ? "hi" : used >= 55 ? "mid" : ""; return '<div class="doctor-limit"><div class="doctor-limit-head"><span>' + doctorEsc(title + " · " + doctorWindowName(w)) + '</span><span>' + Math.round(used) + '% ' + t("doctor.used") + '</span></div><div class="bar"><i class="' + cls + '" style="width:' + used + '%"></i></div><div class="doctor-limit-meta">' + doctorEsc(doctorReset(w)) + '</div></div>'; }).join(""); };
  h += limitCard(codex, "Codex");
  h += limitCard(claude, "Claude");
  if (claude && !liveLimits(claude).length) { const u = (claude.usage || {}).windows || []; h += '<div class="doctor-limit"><div class="doctor-limit-head"><span>Claude · ' + t("doctor.estimate") + '</span><span>' + t("doctor.no_remaining") + '</span></div>' + u.map(w => '<div class="doctor-limit-meta">' + doctorEsc(doctorWindowName(w) + ': ' + doctorTokens(w.input_output_tokens) + ' ' + t("doctor.input_output") + ' +' + doctorTokens(w.cache_tokens) + ' ' + t("doctor.cache")) + '</div>').join("") + '<div class="doctor-note">' + doctorEsc(claude.note || t("doctor.estimate_note")) + '</div></div>'; }
  if (!h.endsWith('<div class="doctor-limits">')) h += '</div>'; else h += '<div class="doctor-note">' + t("doctor.no_limit_data") + '</div></div>';
  h += '<div class="doctor-section">' + t("doctor.engines") + '</div><div class="doctor-engines">' + engines.map(e => '<div class="doctor-engine"><span class="doctor-dot ' + doctorEsc(e.state || "checking") + '"></span><span class="doctor-engine-name">' + doctorEsc(e.engine) + '</span><span class="doctor-engine-meta">' + doctorEsc(e.version || t("doctor.checking")) + '</span><span class="doctor-engine-meta">' + doctorEsc(e.login || (e.note && !e.limits ? t("doctor.no_limit_data") : "")) + '</span></div>').join("") + '</div>';
  if (d.raw) h += '<details class="doctor-raw"><summary>' + t("doctor.show_raw") + '</summary><pre>' + doctorEsc(d.raw) + '</pre></details>';
  p.innerHTML = h;
}
async function openDoctor(force) {
  $("#m-doc").classList.add("on");
  const p = $("#doc");
  const c = await jget("/api/doctor?cached=1");
  renderDoctor(c);
  $("#doc-busy").innerHTML = spin();
  try {
    await jget("/api/doctor" + (force ? "?force=1" : ""));
    for (let i = 0; i < 12; i++) { await new Promise(r => setTimeout(r, 500)); const d = await jget("/api/doctor?cached=1"); renderDoctor(d); if (!d.refreshing) break; }
  } finally {
    $("#doc-busy").innerHTML = "";
  }
}
function refreshDoctor() {
  openDoctor(true);
}
async function deepDoctor() { const p = $("#doc"); $("#doc-busy").innerHTML = spin(); try { const d = await jget("/api/doctor?deep=1"); p.innerHTML += '<details class="doctor-raw" open><summary>' + t("doctor.deep_result") + '</summary><pre>' + doctorEsc(d.raw || "") + '</pre></details>'; } finally { $("#doc-busy").innerHTML = ""; } }

/* ---------- folder browser ---------- */
async function openBrowse(mode) {
  BROWSE_MODE = mode;
  $("#m-browse").classList.add("on");
  const start = mode === "pick-field" ? $("#f-dir").value || CWD
    : mode === "pick-bridge" ? $("#brg-dir").value || CWD
    : activeDir;
  await goBrowse(start);
}
async function goBrowse(path) {
  $("#br-list").innerHTML = `<div class="direntry">${spin(t("browse.loading"))}</div>`;
  const d = await jget("/api/browse?path=" + encodeURIComponent(path || ""));
  $("#br-path").value = d.path;
  $("#br-shortcuts").innerHTML = (d.shortcuts || [])
    .map(s => `<button class="mini" onclick="goBrowse('${esc(s).replace(/'/g, "\\'")}')">${esc(base(s))}</button>`)
    .join("");
  const parts = d.path.split("/").filter(Boolean);
  let acc = d.path.match(/^[A-Za-z]:/) ? "" : "/";
  $("#br-crumbs").innerHTML = parts
    .map((p, i) => {
      acc += i === 0 ? p : "/" + p;
      const full = acc;
      return `<span onclick="goBrowse('${esc(full).replace(/'/g, "\\'")}')">${esc(p)}</span>${i < parts.length - 1 ? " / " : ""}`;
    })
    .join("");
  const up = d.parent ? `<div class="direntry" onclick="goBrowse('${esc(d.parent).replace(/'/g, "\\'")}')">⬆ .. (${t("browse.up")})</div>` : "";
  $("#br-list").innerHTML =
    up +
    (d.dirs.length
      ? d.dirs
          .map(x => {
            const full = d.path.replace(/\/$/, "") + "/" + x;
            return `<div class="direntry" onclick="goBrowse('${esc(full).replace(/'/g, "\\'")}')">📁 ${esc(x)}</div>`;
          })
          .join("")
      : `<div class="direntry" style="cursor:default;color:var(--dim)">${t("browse.no_subdirs")}</div>`);
}
async function chooseBrowsed() {
  const path = $("#br-path").value;
  if (BROWSE_MODE === "add-project") {
    const r = await jpost("/api/project", { dir: path });
    if (r.error) toast("error", t("toast.not_added"), r.error);
    else {
      activeDir = path;
      toast("success", t("toast.project_added"), base(path));
    }
    setTimeout(refresh, 300);
  } else if (BROWSE_MODE === "pick-bridge") {
    $("#brg-dir").value = path;
  } else {
    $("#f-dir").value = path;
  }
  closeModal("m-browse");
}
