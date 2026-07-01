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

/* Shared mutable app state (single declaration point for every other file to read/write). */
let PROVIDERS = {}, ENGINES = [], CWD = "", SEL = null, activeDir = null;
let collapsed = new Set(), seenProjects = new Set();
let prevStates = {}, firstLoad = true;
let lastLog = "";
let BROWSE_MODE = "pick-field";
