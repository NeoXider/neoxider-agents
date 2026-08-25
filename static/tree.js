/* Project/subagent tree + task selection. Uses shared state and $/esc/base/isStrike from
   util.js, t() from i18n.js, toast() from toast.js, loadThread() from chat.js. */

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
    }
    prevStates[task.name] = task.state;
  }
  firstLoad = false;
}

let _refreshing = false, _refreshQueued = false;

async function refresh() {
  if (_refreshing) { _refreshQueued = true; return; }
  _refreshing = true;
  try {
  const d = await jget("/api/tasks");
  ENGINES = d.engines;
  CWD = d.cwd;
  PROVIDERS = d.providers || {};
  CURRENT_TASKS = new Map((d.tasks || []).map(task => [task.name, task]));
  $("#cwd").textContent = d.cwd;
  if (activeDir === null) activeDir = d.cwd;
  if (!$("#f-engine").options.length) {
    $("#f-engine").innerHTML = ENGINES.map(e => `<option value="${e}">${esc((PROVIDERS[e] || {}).label || e)}</option>`).join("");
    syncModels();
  }
  if (!$("#f-dir").value) $("#f-dir").value = activeDir;
  notifyTransitions(d.tasks);

  // group by project: tasks ∪ explicitly registered (even with 0 tasks) projects
  const groups = {};
  for (const task of d.tasks) {
    (groups[task.dir] = groups[task.dir] || []).push(task);
  }
  for (const p of d.projects || []) groups[p] = groups[p] || [];
  if (!Object.keys(groups).length) groups[d.cwd] = [];
  // a new/empty project defaults to collapsed, EXCEPT the active one -- otherwise one project
  // with fifty tasks covers the rest, and a newly added folder is invisible without scrolling.
  for (const dir of Object.keys(groups)) {
    if (!seenProjects.has(dir)) {
      seenProjects.add(dir);
      if (dir !== activeDir && Object.keys(groups).length > 1) collapsed.add(dir);
    }
  }
  const dirs = Object.keys(groups).sort((a, b) => {
    if (a === activeDir) return -1;
    if (b === activeDir) return 1; // active project always on top
    const ua = groups[a].length ? Math.max(...groups[a].map(x => x.updated || 0)) : 0;
    const ub = groups[b].length ? Math.max(...groups[b].map(x => x.updated || 0)) : 0;
    return ub - ua;
  });
  const sortU = a => a.sort((x, y) => (y.updated || 0) - (x.updated || 0));
  $("#tree").innerHTML = dirs
    .map(dir => {
      const all = groups[dir];
      const byName = {};
      all.forEach(x => (byName[x.name] = x));
      const kids = {}, roots = [];
      all.forEach(x => {
        if (x.parent && byName[x.parent]) (kids[x.parent] = kids[x.parent] || []).push(x);
        else roots.push(x);
      });
      const renderTask = x => {
        const sub = kids[x.name] ? `<div class="tasks">${sortU(kids[x.name]).map(renderTask).join("")}</div>` : "";
        return `<div class="task ${x.name === SEL ? "sel" : ""} ${isStrike(x.state) ? "strike" : ""}" data-task="${esc(x.name)}">
        <span class="em ${isLive(x.state) ? "running" : ""}" title="${esc(stateLabel(x.state, x.idle_sec))}">${x.act || ""}${x.topic || ""}</span>
        <span class="nm" title="${esc(x.name)}">${esc(x.title || x.name)}</span>
        <span class="pill">${esc(x.engine)}/${esc(x.model)}</span>
      </div>${sub}`;
      };
      const rows = sortU(roots).map(renderTask).join("") || `<div class="task empty-row">${t("tree.no_tasks")}</div>`;
      const col = collapsed.has(dir) ? "col" : "";
      const act = dir === activeDir ? "active" : "";
      return `<div class="proj ${col} ${act}" data-dir="${esc(dir)}">
      <div class="ph" data-project="${esc(dir)}">
        <span class="caret" data-collapse>▾</span>
        <span class="pname" title="${esc(dir || "")}">📁 ${esc(base(dir))}</span>
        <span class="cnt">${all.length}</span>
        <button class="mini" data-new-task data-i18n-title="tree.new_task_here" title="${esc(t("tree.new_task_here"))}">＋</button>
      </div>
      <div class="tasks">${rows}</div></div>`;
    })
    .join("");

  document.querySelectorAll("#tree .task[data-task]").forEach(el =>
    el.addEventListener("click", event => select(el.dataset.task, event)));
  document.querySelectorAll("#tree .ph[data-project]").forEach(el => {
    el.addEventListener("click", event => selectProj(el.dataset.project, event));
    const caret = el.querySelector("[data-collapse]");
    const add = el.querySelector("[data-new-task]");
    if (caret) caret.addEventListener("click", event => toggleCollapse(el.dataset.project, event));
    if (add) add.addEventListener("click", event => newHere(el.dataset.project, event));
  });

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

function clearDeletedSelection() {
  if (dialogController) dialogController.abort();
  SEL = null;
  dlgTask = "";
  lastLog = "";
  $("#replybar").style.display = "none";
  $("#btn-reply").disabled = true;
  $("#chead").textContent = t("chat.select_prompt");
  $("#chat").textContent = t("chat.empty_thread");
}

// clicking a project row: makes it active (default dir for a new task) and GUARANTEES it's
// expanded -- otherwise the selected project could stay hidden behind its own collapsed state.
function selectProj(dir, e) {
  e && e.stopPropagation();
  activeDir = dir;
  $("#f-dir").value = dir;
  collapsed.delete(dir);
  refresh();
}
// clicking the caret: only toggles collapse, without touching the active project (doesn't
// jolt the form on the right)
function toggleCollapse(dir, e) {
  e && e.stopPropagation();
  if (collapsed.has(dir)) collapsed.delete(dir);
  else collapsed.add(dir);
  refresh();
}
function newHere(dir, e) {
  e && e.stopPropagation();
  activeDir = dir;
  $("#f-dir").value = dir;
  collapsed.delete(dir);
  $("#f-prompt").focus();
}
function select(n, e) {
  e && e.stopPropagation();
  SEL = n;
  $("#btn-reply").disabled = false;
  lastLog = "";
  refresh();
}
