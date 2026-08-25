/* Shared primitives used by every other static/*.js file. Loaded first (plain classic
   scripts, no modules, no build step -- everything shares one global scope, so anything
   declared here must NOT be redeclared elsewhere). */
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const base = p => (!p ? t("tree.no_project") : p.replace(/[\/\\]+$/, "").split(/[\/\\]/).pop() || p);
const isStrike = st => st === "done" || st === "stalled" || st === "error";
/* "idle" = the task's process is ALIVE but has produced no output for a while (agent.sh and
   gui.py compute it identically -- see eff_state in both). A live task, not a dead one. */
const isLive = st => st === "running" || st === "idle";
/* Same wording `agent.sh status` prints, so the panel and the terminal never describe the
   same task differently. */
const stateLabel = (st, idleSec) =>
  st === "idle" ? t("chat.no_output").replace("{m}", Math.floor((idleSec || 0) / 60)) : st;
const spin = text => `<span class="spinner"></span>${text ? " " + esc(text) : ""}`;

function apiErrorMessage(err) {
  return String((err && err.message) || err || "request failed").slice(0, 500);
}
function surfaceApiError(err, title) {
  if (!err || err.name === "AbortError" || err.reported) return;
  err.reported = true;
  if (typeof toast === "function") toast("error", title || "Request failed", apiErrorMessage(err));
}
async function apiJson(u, options) {
  let response;
  try {
    response = await fetch(u, options);
  } catch (err) {
    surfaceApiError(err);
    throw err;
  }
  const contentType = (response.headers.get("content-type") || "").toLowerCase();
  const text = await response.text();
  let data = null;
  if (contentType.includes("application/json")) {
    try { data = JSON.parse(text); }
    catch (err) { data = null; }
  }
  if (!response.ok) {
    const msg = data && data.error ? data.error : text || `HTTP ${response.status}`;
    const err = new Error(String(msg).slice(0, 500));
    err.status = response.status;
    surfaceApiError(err);
    throw err;
  }
  if (!contentType.includes("application/json") || data === null || typeof data !== "object") {
    const err = new Error("Server returned a non-JSON response");
    surfaceApiError(err);
    throw err;
  }
  return data;
}
async function jget(u, options) {
  return apiJson(u, options || {});
}
async function jpost(u, b, options) {
  return apiJson(u, Object.assign({}, options || {}, {
    method: "POST",
    headers: Object.assign({}, (options || {}).headers || {}, { "Content-Type": "application/json" }),
    body: JSON.stringify(b),
  }));
}

/* navigator.clipboard.writeText silently rejects in some contexts (no clipboard permission,
   iframe, older browser) -- the caller never finds out, so the button just looks dead. Fall
   back to the old execCommand("copy") trick and always show the button itself succeeding/
   failing, since a toast is easy to miss right after a click. */
async function copyText(btn, text) {
  let ok = false;
  try {
    await navigator.clipboard.writeText(text);
    ok = true;
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try {
      ok = document.execCommand("copy");
    } catch (e2) {
      ok = false;
    }
    ta.remove();
  }
  if (!btn) return ok;
  const old = btn.textContent;
  btn.textContent = ok ? t("api.copied") : t("api.copy_failed");
  btn.disabled = true;
  setTimeout(() => {
    btn.textContent = old;
    btn.disabled = false;
  }, 1200);
  return ok;
}

/* Shared mutable app state (single declaration point for every other file to read/write). */
let PROVIDERS = {}, ENGINES = [], CWD = "", SEL = null, activeDir = null;
let CURRENT_TASKS = new Map();
let collapsed = new Set(), seenProjects = new Set();
let prevStates = {}, firstLoad = true;
let lastLog = "";
let BROWSE_MODE = "pick-field";
/* Full-dialog view state (chat.js): which task is shown, whether "show the whole dialog" is
   active, which tool calls are expanded (survives auto-refresh), thinking-blocks visibility,
   and the server/client clock offset used for live elapsed times. */
let dlgTask = "", dlgFull = false;
let expandedTools = new Set();
let showThinking = false;
let clockSkew = 0;
let dialogController = null, dialogRequestId = 0;

/* Short human duration, the exact mirror of gui.py's fmt_dur: 1.2s / 45s / 3m 20s / 1h 5m;
   null/NaN/negative -> "" (show nothing rather than a made-up number). */
function fmtDur(s) {
  if (s == null || isNaN(s) || s < 0) return "";
  if (s < 9.95) return s.toFixed(1) + "s";
  const n = Math.round(s);
  if (n < 60) return n + "s";
  const m = Math.floor(n / 60), ss = n % 60;
  if (m < 60) return ss ? m + "m " + ss + "s" : m + "m";
  const h = Math.floor(m / 60), mm = m % 60;
  return mm ? h + "h " + mm + "m" : h + "h";
}
