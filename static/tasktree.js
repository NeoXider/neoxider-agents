/* Pure subagent-tree model (no DOM, no globals besides what it defines): orchestrator ->
   subagent -> its subagents, any depth. Built from each task's `parent` field (meta parent=,
   written from -P / AGENT_PARENT; a task's agent passes AGENT_PARENT=<its own name> to anything
   it launches). Mirrors gui.py resolve_tree():
     - a parent that is not a task here (deleted, pruned, external orchestrator id) makes the
       row a root and keeps the name in `missingParent` for a hint;
     - a parent loop (A -> B -> A, or A -> A) is cut at its lexically smallest member, which
       becomes a root flagged `cycle`;
     - depth/counters are computed iteratively, so a deep chain can never overflow the stack.
   Loaded as a classic script by gui.html and require()d by tests/test_tree.js. */

const TREE_LIVE = ["running", "idle"];
const TREE_ERROR = ["error", "limited", "silent", "stalled"];

function buildTaskTree(tasks) {
  const byName = new Map();
  for (const task of tasks || []) {
    if (task && typeof task.name === "string" && task.name && !byName.has(task.name)) {
      byName.set(task.name, {task, name: task.name, children: [], parent: null, depth: 0,
        missingParent: "", cycle: false, counts: null});
    }
  }
  const up = new Map();
  for (const [name, node] of byName) {
    const raw = typeof node.task.parent === "string" ? node.task.parent : "";
    if (raw && !byName.has(raw)) node.missingParent = raw;
    if (raw === name) node.cycle = true;
    up.set(name, raw && raw !== name && byName.has(raw) ? raw : "");
  }
  const done = new Set();
  for (const start of [...byName.keys()].sort()) {
    const index = new Map(), path = [];
    let cur = start;
    while (cur && !done.has(cur) && !index.has(cur)) {
      index.set(cur, path.length);
      path.push(cur);
      cur = up.get(cur);
    }
    if (cur && index.has(cur)) {
      const cut = path.slice(index.get(cur)).sort()[0];
      up.set(cut, "");
      byName.get(cut).cycle = true;
    }
    for (const name of path) done.add(name);
  }
  const roots = [];
  for (const [name, node] of byName) {
    const parent = up.get(name);
    if (parent) {
      node.parent = byName.get(parent);
      node.parent.children.push(node);
    } else {
      roots.push(node);
    }
  }
  const newestFirst = list => list.sort((a, b) =>
    ((b.task.updated || 0) - (a.task.updated || 0)) || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  newestFirst(roots);
  const order = [], stack = roots.slice().reverse().map(node => [node, 0]);
  while (stack.length) {
    const [node, depth] = stack.pop();
    node.depth = depth;
    order.push(node);
    newestFirst(node.children);
    for (let i = node.children.length - 1; i >= 0; i--) stack.push([node.children[i], depth + 1]);
  }
  for (let i = order.length - 1; i >= 0; i--) {
    const node = order[i], state = node.task.state;
    const counts = {total: 1, running: TREE_LIVE.includes(state) ? 1 : 0,
      errors: TREE_ERROR.includes(state) ? 1 : 0, waiting: state === "waiting" ? 1 : 0};
    for (const child of node.children) {
      counts.total += child.counts.total;
      counts.running += child.counts.running;
      counts.errors += child.counts.errors;
      counts.waiting += child.counts.waiting;
    }
    node.counts = counts;
  }
  return {roots, byName, order};
}

/* Which nodes stay visible under a text query and a state filter: every match plus all of
   its ancestors (so a match deep in the tree keeps its path). null = no filter active. */
function filterTaskTree(tree, query, stateFilter) {
  const q = String(query || "").trim().toLowerCase();
  const sf = stateFilter || "";
  if (!q && !sf) return null;
  const stateOk = state =>
    !sf || (sf === "active" ? TREE_LIVE.includes(state)
      : sf === "errors" ? TREE_ERROR.includes(state)
      : sf === "attention" ? ["waiting", "stalled", "limited", "silent", "error"].includes(state)
      : sf === "done" ? state === "done" : true);
  const visible = new Set();
  for (const node of tree.order) {
    const t = node.task;
    const hay = [t.name, t.title, t.engine, t.model, t.state, t.dir, t.parent].join(" ").toLowerCase();
    if ((!q || hay.includes(q)) && stateOk(t.state)) {
      for (let cur = node; cur && !visible.has(cur.name); cur = cur.parent) visible.add(cur.name);
    }
  }
  return visible;
}

/* Depth-first list of the rows to draw for one set of roots, honouring collapsed nodes and
   the filter (an active filter shows every surviving match, collapsed or not). */
function flattenTaskTree(roots, collapsedNodes, visible) {
  const rows = [], stack = roots.slice().reverse();
  while (stack.length) {
    const node = stack.pop();
    if (visible && !visible.has(node.name)) continue;
    rows.push(node);
    const open = visible ? true : !collapsedNodes.has(node.name);
    if (open) for (let i = node.children.length - 1; i >= 0; i--) stack.push(node.children[i]);
  }
  return rows;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {buildTaskTree, filterTaskTree, flattenTaskTree};
}
