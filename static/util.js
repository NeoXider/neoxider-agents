/* Shared primitives used by every other static/*.js file. Loaded first (plain classic
   scripts, no modules, no build step -- everything shares one global scope, so anything
   declared here must NOT be redeclared elsewhere). */
const $ = s => document.querySelector(s);
const esc = s => (s ?? "").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const base = p => (!p ? t("tree.no_project") : p.replace(/[\/\\]+$/, "").split(/[\/\\]/).pop() || p);
const isStrike = st => st === "done" || st === "stalled" || st === "error";
const spin = text => `<span class="spinner"></span>${text ? " " + esc(text) : ""}`;

async function jget(u) {
  return (await fetch(u)).json();
}
async function jpost(u, b) {
  return (await fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) })).json();
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
let collapsed = new Set(), seenProjects = new Set();
let prevStates = {}, firstLoad = true;
let lastLog = "";
let BROWSE_MODE = "pick-field";
