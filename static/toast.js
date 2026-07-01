/* Toast notifications (auto-dismiss ~3s) + persisted history with an unread badge.
   Uses $/esc/t from util.js/i18n.js (loaded earlier). */
const TOAST_HISTORY = JSON.parse(localStorage.getItem("agentgui_toast_history") || "[]");

function toast(kind, title, text) {
  const rec = { kind, title, text, ts: Date.now() };
  TOAST_HISTORY.unshift(rec);
  TOAST_HISTORY.splice(80);
  localStorage.setItem("agentgui_toast_history", JSON.stringify(TOAST_HISTORY));
  updateHistBadge();
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.innerHTML = `<div class="tt">${esc(title)}</div><div>${esc(text || "")}</div>`;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 320);
  }, 3000);
}

function updateHistBadge() {
  const b = $("#hist-badge");
  if (!b) return;
  const n = TOAST_HISTORY.filter(h => !h.seen).length;
  if (n > 0) {
    b.style.display = "inline-block";
    b.textContent = n > 9 ? "9+" : n;
  } else {
    b.style.display = "none";
  }
}

function openHistory() {
  TOAST_HISTORY.forEach(h => (h.seen = true));
  localStorage.setItem("agentgui_toast_history", JSON.stringify(TOAST_HISTORY));
  updateHistBadge();
  const iconFor = k => (k === "success" ? "✅" : k === "error" ? "❌" : "⏳");
  $("#hist-list").innerHTML = TOAST_HISTORY.length
    ? TOAST_HISTORY.map(
        h => `
    <div class="hrow"><span class="hicon">${iconFor(h.kind)}</span>
      <div><div><b>${esc(h.title)}</b> ${esc(h.text || "")}</div>
      <div class="time">${new Date(h.ts).toLocaleString()}</div></div></div>`
      ).join("")
    : `<div class="empty">${t("history.empty")}</div>`;
  $("#m-hist").classList.add("on");
}

function clearHistory() {
  TOAST_HISTORY.length = 0;
  localStorage.setItem("agentgui_toast_history", "[]");
  openHistory();
}
