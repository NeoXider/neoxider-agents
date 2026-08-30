/* Toast notifications (auto-dismiss ~3s) + persisted history with an unread badge.
   Uses $/esc/t from util.js/i18n.js (loaded earlier). */
const TOAST_HISTORY_LIMIT = 80;
const TOAST_COUNT_MAX = 999;
const TOAST_COALESCE_MS = 60_000;

function normalizeToastCount(value) {
  const n = Math.floor(Number(value));
  return Number.isFinite(n) ? Math.min(TOAST_COUNT_MAX, Math.max(1, n)) : 1;
}

function normalizeToastRecord(item, now = Date.now()) {
  const rawTs = Number(item && item.ts);
  return {
    kind: String((item && item.kind) || "warning").slice(0, 20),
    title: String((item && item.title) || "").slice(0, 500),
    text: String((item && item.text) || "").slice(0, 1000),
    ts: Number.isFinite(rawTs) ? Math.min(now, Math.max(0, Math.floor(rawTs))) : now,
    seen: Boolean(item && item.seen),
    count: normalizeToastCount(item && item.count),
  };
}

function loadToastHistory() {
  try {
    const parsed = JSON.parse(localStorage.getItem("agentgui_toast_history") || "[]");
    if (!Array.isArray(parsed)) return [];
    const now = Date.now();
    return parsed.slice(0, TOAST_HISTORY_LIMIT)
      .filter(item => item && typeof item === "object")
      .map(item => normalizeToastRecord(item, now));
  } catch (err) {
    localStorage.removeItem("agentgui_toast_history");
    return [];
  }
}
const TOAST_HISTORY = loadToastHistory();

function persistToastHistory() {
  const now = Date.now();
  const safe = TOAST_HISTORY.slice(0, TOAST_HISTORY_LIMIT)
    .filter(item => item && typeof item === "object")
    .map(item => normalizeToastRecord(item, now));
  TOAST_HISTORY.splice(0, TOAST_HISTORY.length, ...safe);
  localStorage.setItem("agentgui_toast_history", JSON.stringify(TOAST_HISTORY));
}

function toast(kind, title, text) {
  const now = Date.now();
  const rec = normalizeToastRecord({ kind, title, text, ts: now }, now);
  const previous = TOAST_HISTORY[0];
  const age = previous ? now - previous.ts : Infinity;
  if (rec.kind === "error" && previous && previous.kind === rec.kind &&
      previous.title === rec.title && previous.text === rec.text &&
      age >= 0 && age <= TOAST_COALESCE_MS) {
    previous.count = normalizeToastCount(Number(previous.count) + 1);
    previous.ts = now;
    previous.seen = false;
    persistToastHistory();
    updateHistBadge();
    return false;
  }
  TOAST_HISTORY.unshift(rec);
  TOAST_HISTORY.splice(TOAST_HISTORY_LIMIT);
  persistToastHistory();
  updateHistBadge();
  const el = document.createElement("div");
  el.className = "toast " + rec.kind;
  el.innerHTML = `<div class="tt">${esc(rec.title)}</div><div>${esc(rec.text)}</div>`;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 320);
  }, 3000);
  return true;
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
  persistToastHistory();
  updateHistBadge();
  const iconFor = k => (k === "success" ? "✅" : k === "error" ? "❌" : "⏳");
  $("#hist-list").innerHTML = TOAST_HISTORY.length
    ? TOAST_HISTORY.map(
        h => `
    <div class="hrow"><span class="hicon">${iconFor(h.kind)}</span>
      <div><div><b>${esc(h.title)}${h.count > 1 ? ` ×${h.count}` : ""}</b> ${esc(h.text || "")}</div>
      <div class="time">${new Date(h.ts).toLocaleString()}</div></div></div>`
      ).join("")
    : `<div class="empty">${t("history.empty")}</div>`;
  $("#m-hist").classList.add("on");
}

function clearHistory() {
  TOAST_HISTORY.length = 0;
  persistToastHistory();
  openHistory();
}
