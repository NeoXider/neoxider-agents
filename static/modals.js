/* Doctor modal (cached, with a manual refresh button) + folder browser modal.
   Uses $/esc/spin/base/jget/jpost from util.js, t() from i18n.js, toast()/refresh() elsewhere. */

function closeModal(id) {
  $("#" + id).classList.remove("on");
}
document.addEventListener("keydown", e => {
  if (e.key === "Escape") { closeModal("m-doc"); closeModal("m-hist"); closeModal("m-browse"); }
});

/* ---------- doctor (cached; force=1 bypasses the cache) ---------- */
async function openDoctor(force) {
  $("#m-doc").classList.add("on");
  const p = $("#doc");
  // keep showing the last-known-good text while a refresh is in flight -- only replace it
  // once the new data actually arrives, instead of blanking the panel on every open/refresh.
  if (!p.dataset.loaded) p.innerHTML = spin(t("doctor.loading"));
  const d = await jget("/api/doctor" + (force ? "?force=1" : ""));
  p.textContent = d.text + (d.cached ? "\n\n" + t("limits.cached") : "");
  p.dataset.loaded = "1";
}
function refreshDoctor() {
  openDoctor(true);
}

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
