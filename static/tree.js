/* Project -> subagent tree + task selection. The hierarchy itself (any depth, cycles cut,
   missing parents shown as roots) comes from tasktree.js; this file renders it as an ARIA tree
   with collapsible nodes, subtree counters, a filter, keyboard navigation and persisted
   expansion. Uses shared state and $/esc/base/isStrike from util.js, t() from i18n.js, toast()
   from toast.js, loadThread() from chat.js. */

function notifyTransitions(tasks) {
  for (const task of tasks) {
    const prev = prevStates[task.name];
    if (!firstLoad && prev && prev !== task.state) {
      if (task.state === "done") toast("success", "✔ " + task.name, t("toast.task_done") + (task.files && task.files !== "0" ? ` · ${task.files} files` : ""));
      else if (task.state === "error")
        toast("error", "✖ " + task.name,
              task.timeout ? t("toast.task_timeout").replace("{s}", task.timeout)
                           : t("toast.task_error") + " " + (task.exit || "?"));
      else if (task.state === "waiting") toast("warning", "⏳ " + task.name, t("toast.task_waiting"));
      else if (task.state === "stalled") toast("warning", "⚠ " + task.name, t("toast.task_stalled"));
      else if (["limited", "silent"].includes(task.state)) toast("warning", task.name, task.reason || task.state);
    }
    prevStates[task.name] = task.state;
  }
  firstLoad = false;
}

/* ---- persisted expansion: projects and task nodes (names), both in localStorage ---- */
const TREE_KEY = "agentgui_tree_collapsed", PROJ_KEY = "agentgui_proj_collapsed";
function loadNameSet(key) {
  try {
    const value = JSON.parse(localStorage.getItem(key) || "[]");
    return new Set(Array.isArray(value) ? value.filter(x => typeof x === "string").slice(0, 2000) : []);
  } catch (_) { return new Set(); }
}
function saveNameSet(key, set) {
  try { localStorage.setItem(key, JSON.stringify([...set].slice(-2000))); } catch (_) { /* quota/private mode */ }
}
let collapsedNodes = loadNameSet(TREE_KEY);
let projStateKnown = loadNameSet(PROJ_KEY + "_known");
collapsed = loadNameSet(PROJ_KEY);
for (const dir of projStateKnown) seenProjects.add(dir);
let treeFilter = "", treeStateFilter = "", lastTreeHtml = "", treeModel = null;
/* the selected task survives a reload (restored once, only if it still exists) */
let savedSelection = (() => { try { return localStorage.getItem("agentgui_sel") || ""; } catch (_) { return ""; } })();
function rememberSelection(name) {
  try { if (name) localStorage.setItem("agentgui_sel", name); else localStorage.removeItem("agentgui_sel"); } catch (_) { /* ignore */ }
}

let _refreshing = false, _refreshQueued = false;

function stateClass(st) {
  return /^[a-z_]+$/.test(st || "") ? "s-" + st : "s-unknown";
}

function renderNodeRow(node, level) {
  const x = node.task, kids = node.children.length;
  const open = treeFilterActive() || !collapsedNodes.has(x.name);
  const c = node.counts;
  const counts = kids ? `<span class="tcount" title="${esc(t("tree.counts_title").replace("{total}", c.total - 1).replace("{running}", c.running).replace("{errors}", c.errors))}">
      ${c.total - 1}${c.running ? ` <i class="c-run">● ${c.running}</i>` : ""}${c.errors ? ` <i class="c-err">! ${c.errors}</i>` : ""}</span>` : "";
  const hint = node.missingParent
    ? `<span class="thint" title="${esc(t("tree.parent_missing").replace("{parent}", node.missingParent))}">↑ ${esc(node.missingParent)}</span>`
    : node.cycle ? `<span class="thint warn" title="${esc(t("tree.parent_cycle"))}">↻</span>` : "";
  const otherDir = node.parent && x.dir && node.parent.task.dir && x.dir !== node.parent.task.dir
    ? `<span class="pill" title="${esc(x.dir)}">${esc(base(x.dir))}</span>` : "";
  const sel = x.name === SEL;
  return `<div class="task trow ${stateClass(x.state)} ${sel ? "sel" : ""} ${isStrike(x.state) ? "strike" : ""}" role="treeitem"
      aria-level="${level}" ${kids ? `aria-expanded="${open}"` : ""} aria-selected="${sel}"
      tabindex="${sel ? 0 : -1}" data-task="${esc(x.name)}" data-key="t:${esc(x.name)}" style="--d:${node.depth}">
    <span class="twisty" ${kids ? "data-twisty" : ""} aria-hidden="true">${kids ? "▸" : ""}</span>
    <span class="sdot ${stateClass(x.state)}" role="img" aria-label="${esc(stateLabel(x.state, x.idle_sec))}" title="${esc(stateLabel(x.state, x.idle_sec))}"></span>
    <span class="em ${isLive(x.state) ? "running" : ""}" aria-hidden="true">${x.act || ""}${x.topic || ""}</span>
    <span class="nm" title="${esc(x.name)}">${esc(x.title || x.name)}</span>
    ${hint}${counts}${otherDir}
    ${x.queued ? `<span class="pill qd" title="${esc(t("chat.queued").replace("{n}", x.queued))}">✉ ${esc(x.queued)}</span>` : ""}
    <span class="pill eng pe-${esc(x.engine)}">${esc(x.engine)}/${esc(x.model)}</span>
  </div>`;
}

function treeFilterActive() { return !!(treeFilter.trim() || treeStateFilter); }

async function refresh() {
  if (_refreshing) { _refreshQueued = true; return; }
  _refreshing = true;
  try {
  const d = await jget("/api/tasks");
  ENGINES = d.engines;
  CWD = d.cwd;
  PROVIDERS = d.providers || {};
  CURRENT_TASKS = new Map((d.tasks || []).map(task => [task.name, task]));
  if (savedSelection) {
    if (!SEL && CURRENT_TASKS.has(savedSelection)) { SEL = savedSelection; $("#btn-reply").disabled = false; }
    savedSelection = "";
  }
  $("#cwd").textContent = d.cwd;
  if (activeDir === null) activeDir = d.cwd;
  if (!$("#f-engine").options.length) {
    $("#f-engine").innerHTML = ENGINES.map(e => `<option value="${e}">${esc((PROVIDERS[e] || {}).label || e)}</option>`).join("");
    syncModels();
  }
  if (!$("#f-dir").value) $("#f-dir").value = activeDir;
  notifyTransitions(d.tasks);
  renderTree(d);
  if (SEL) {
    const task = d.tasks.find(x => x.name === SEL);
    if (task) await loadThread(task);
    else clearDeletedSelection();
  }
  } catch (err) {
    if (err.name !== "AbortError") surfaceApiError(err);
  } finally {
    _refreshing = false;
    if (_refreshQueued) { _refreshQueued = false; refresh(); }
  }
}

function renderTree(d) {
  treeModel = buildTaskTree(d.tasks || []);
  const visible = filterTaskTree(treeModel, treeFilter, treeStateFilter);
  // group ROOTS by project; children always nest under their parent, whatever their dir
  const groups = {};
  for (const node of treeModel.roots) (groups[node.task.dir] = groups[node.task.dir] || []).push(node);
  for (const p of d.projects || []) groups[p] = groups[p] || [];
  if (!Object.keys(groups).length) groups[d.cwd] = [];
  // a new/empty project defaults to collapsed, EXCEPT the active one -- otherwise one project
  // with fifty tasks covers the rest, and a newly added folder is invisible without scrolling.
  for (const dir of Object.keys(groups)) {
    if (!seenProjects.has(dir)) {
      seenProjects.add(dir);
      projStateKnown.add(dir);
      if (dir !== activeDir && Object.keys(groups).length > 1) collapsed.add(dir);
    }
  }
  saveNameSet(PROJ_KEY + "_known", projStateKnown);
  saveNameSet(PROJ_KEY, collapsed);
  const newest = dir => groups[dir].length ? Math.max(...groups[dir].map(n => n.task.updated || 0)) : 0;
  const dirs = Object.keys(groups).sort((a, b) => {
    if (a === activeDir) return -1;
    if (b === activeDir) return 1; // active project always on top
    return newest(b) - newest(a);
  });
  let html = "", shown = 0;
  for (const dir of dirs) {
    const rows = flattenTaskTree(groups[dir], collapsedNodes, visible);
    if (visible && !rows.length) continue;
    shown += rows.length;
    const total = groups[dir].reduce((n, r) => n + r.counts.total, 0);
    const running = groups[dir].reduce((n, r) => n + r.counts.running, 0);
    const errors = groups[dir].reduce((n, r) => n + r.counts.errors, 0);
    const open = visible ? true : !collapsed.has(dir);
    html += `<div class="proj ${open ? "" : "col"} ${dir === activeDir ? "active" : ""}" data-dir="${esc(dir)}" role="none">
      <div class="ph trow" role="treeitem" aria-level="1" aria-expanded="${open}" tabindex="-1" data-project="${esc(dir)}" data-key="p:${esc(dir)}">
        <span class="caret" data-collapse aria-hidden="true">▾</span>
        <span class="pname" title="${esc(dir || "")}">${esc(base(dir))}</span>
        <span class="cnt" title="${esc(t("tree.counts_title").replace("{total}", total).replace("{running}", running).replace("{errors}", errors))}">${total}${running ? ` <i class="c-run">● ${running}</i>` : ""}${errors ? ` <i class="c-err">! ${errors}</i>` : ""}</span>
        <button class="mini ghost" data-new-task title="${esc(t("tree.new_task_here"))}" aria-label="${esc(t("tree.new_task_here"))}">＋</button>
      </div>
      <div class="tasks" role="group">${open ? (rows.map(node => renderNodeRow(node, node.depth + 2)).join("")
        || `<div class="task empty-row">${t("tree.no_tasks")}</div>`) : ""}</div></div>`;
  }
  if (visible && !shown) html = `<div class="empty pad">${esc(t("tree.no_matches"))}</div>`;
  const box = $("#tree");
  if (html === lastTreeHtml) return;
  const focusedKey = box.contains(document.activeElement) && document.activeElement.dataset
    ? document.activeElement.dataset.key : null;
  const scroll = box.scrollTop;
  lastTreeHtml = html;
  box.innerHTML = html;
  box.scrollTop = scroll;
  if (!box.querySelector('[tabindex="0"]')) {
    const first = box.querySelector("[role=treeitem]");
    if (first) first.tabIndex = 0;
  }
  if (focusedKey) {
    const again = [...box.querySelectorAll("[data-key]")].find(el => el.dataset.key === focusedKey);
    if (again) focusRow(again);
  }
}

function rerenderTree() {
  lastTreeHtml = "";
  refresh();
}

function focusRow(el) {
  if (!el) return;
  $("#tree").querySelectorAll('[role=treeitem][tabindex="0"]').forEach(x => { x.tabIndex = -1; });
  el.tabIndex = 0;
  el.focus({preventScroll: false});
}

function toggleNode(name, open) {
  const isOpen = !collapsedNodes.has(name);
  if (open === isOpen) return false;
  if (isOpen) collapsedNodes.add(name);
  else collapsedNodes.delete(name);
  saveNameSet(TREE_KEY, collapsedNodes);
  rerenderTree();
  return true;
}

function setAllNodes(open) {
  if (!treeModel) return;
  if (open) collapsedNodes.clear();
  else for (const node of treeModel.order) if (node.children.length) collapsedNodes.add(node.name);
  saveNameSet(TREE_KEY, collapsedNodes);
  rerenderTree();
}

/* Delegated pointer handling: one listener survives every re-render. */
$("#tree").addEventListener("click", event => {
  const row = event.target.closest("[role=treeitem]");
  if (!row) return;
  if (row.dataset.project !== undefined) {
    if (event.target.closest("[data-new-task]")) return newHere(row.dataset.project, event);
    if (event.target.closest("[data-collapse]")) return toggleCollapse(row.dataset.project, event);
    return selectProj(row.dataset.project, event);
  }
  if (event.target.closest("[data-twisty]")) {
    event.stopPropagation();
    toggleNode(row.dataset.task);
    return;
  }
  select(row.dataset.task, event);
});

/* WAI-ARIA tree keyboard model: Up/Down move, Right expands or enters, Left collapses or goes
   to the parent, Home/End jump, Enter/Space select (or toggle a project). */
$("#tree").addEventListener("keydown", event => {
  const row = event.target.closest && event.target.closest("[role=treeitem]");
  if (!row || event.altKey || event.ctrlKey || event.metaKey) return;
  const rows = [...$("#tree").querySelectorAll("[role=treeitem]")];
  const i = rows.indexOf(row), level = +row.getAttribute("aria-level");
  const expanded = row.getAttribute("aria-expanded");
  let handled = true;
  switch (event.key) {
    case "ArrowDown": focusRow(rows[i + 1]); break;
    case "ArrowUp": focusRow(rows[i - 1]); break;
    case "Home": focusRow(rows[0]); break;
    case "End": focusRow(rows[rows.length - 1]); break;
    case "ArrowRight":
      if (expanded === "false") {
        if (row.dataset.project !== undefined) toggleCollapse(row.dataset.project);
        else toggleNode(row.dataset.task, true);
      } else if (expanded === "true" && rows[i + 1] && +rows[i + 1].getAttribute("aria-level") > level) {
        focusRow(rows[i + 1]);
      }
      break;
    case "ArrowLeft":
      if (expanded === "true" && !treeFilterActive()) {
        if (row.dataset.project !== undefined) toggleCollapse(row.dataset.project);
        else toggleNode(row.dataset.task, false);
      } else {
        for (let j = i - 1; j >= 0; j--) {
          if (+rows[j].getAttribute("aria-level") < level) { focusRow(rows[j]); break; }
        }
      }
      break;
    case "Enter":
    case " ":
      if (row.dataset.project !== undefined) toggleCollapse(row.dataset.project);
      else select(row.dataset.task);
      break;
    default: handled = false;
  }
  if (handled) event.preventDefault();
});

function clearDeletedSelection() {
  if (dialogController) dialogController.abort();
  SEL = null;
  rememberSelection("");
  dlgTask = "";
  lastLog = "";
  $("#replybar").hidden = true;
  $("#btn-reply").disabled = true;
  $("#chead").textContent = t("chat.select_prompt");
  $("#chat").innerHTML = `<div class="empty">${esc(t("chat.empty_thread"))}</div>`;
}

// clicking a project row: makes it active (default dir for a new task) and GUARANTEES it's
// expanded -- otherwise the selected project could stay hidden behind its own collapsed state.
function selectProj(dir, e) {
  e && e.stopPropagation();
  activeDir = dir;
  $("#f-dir").value = dir;
  collapsed.delete(dir);
  saveNameSet(PROJ_KEY, collapsed);
  rerenderTree();
}
// clicking the caret: only toggles collapse, without touching the active project (doesn't
// jolt the form on the right)
function toggleCollapse(dir, e) {
  e && e.stopPropagation();
  if (collapsed.has(dir)) collapsed.delete(dir);
  else collapsed.add(dir);
  saveNameSet(PROJ_KEY, collapsed);
  rerenderTree();
}
function newHere(dir, e) {
  e && e.stopPropagation();
  activeDir = dir;
  $("#f-dir").value = dir;
  collapsed.delete(dir);
  saveNameSet(PROJ_KEY, collapsed);
  $("#f-prompt").focus();
}
function select(n, e) {
  e && e.stopPropagation();
  if (!n) return;
  SEL = n;
  rememberSelection(n);
  // make sure the selected node is reachable: expand its ancestors
  if (treeModel && treeModel.byName.has(n)) {
    let changed = false;
    for (let cur = treeModel.byName.get(n).parent; cur; cur = cur.parent) {
      if (collapsedNodes.delete(cur.name)) changed = true;
    }
    if (changed) saveNameSet(TREE_KEY, collapsedNodes);
  }
  $("#btn-reply").disabled = false;
  lastLog = "";
  lastTreeHtml = "";
  refresh();
}

$("#tree-filter").addEventListener("input", event => {
  treeFilter = event.target.value;
  rerenderTree();
});
$("#tree-filter").addEventListener("keydown", event => {
  if (event.key === "Escape") { event.target.value = ""; treeFilter = ""; rerenderTree(); }
  if (event.key === "ArrowDown") { event.preventDefault(); focusRow($("#tree [role=treeitem]")); }
});
$("#tree-state").addEventListener("change", event => {
  treeStateFilter = event.target.value;
  rerenderTree();
});
$("#btn-tree-expand").addEventListener("click", () => setAllNodes(true));
$("#btn-tree-collapse").addEventListener("click", () => setAllNodes(false));
