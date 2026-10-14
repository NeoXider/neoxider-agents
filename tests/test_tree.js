/* Subagent tree model (static/tasktree.js): depth, cycles, orphans, counters, filter, flatten.
   Run: node tests/test_tree.js */
const assert = require("node:assert/strict");
const path = require("node:path");
const {buildTaskTree, filterTaskTree, flattenTaskTree} = require(path.join(__dirname, "../static/tasktree.js"));

const task = (name, parent = "", state = "done", updated = 0, extra = {}) =>
  Object.assign({name, parent, state, updated, engine: "codex", model: "sol", title: name, dir: "/p"}, extra);

// depth 4: orchestrator -> sub -> subsub -> d3 -> d4
{
  const tree = buildTaskTree([
    task("d4", "d3", "error", 5), task("orch", "", "running", 1), task("sub", "orch", "running", 2),
    task("subsub", "sub", "done", 3), task("d3", "subsub", "waiting", 4)]);
  assert.deepEqual(tree.roots.map(n => n.name), ["orch"]);
  const d4 = tree.byName.get("d4");
  assert.equal(d4.depth, 4);
  assert.equal(d4.parent.name, "d3");
  const orch = tree.byName.get("orch");
  assert.deepEqual(orch.counts, {total: 5, running: 2, errors: 1, waiting: 1});
  assert.deepEqual(tree.byName.get("subsub").counts, {total: 3, running: 0, errors: 1, waiting: 1});
  // flatten honours collapse; filter keeps the ancestors of a deep match
  assert.deepEqual(flattenTaskTree(tree.roots, new Set(), null).map(n => n.name), ["orch", "sub", "subsub", "d3", "d4"]);
  assert.deepEqual(flattenTaskTree(tree.roots, new Set(["sub"]), null).map(n => n.name), ["orch", "sub"]);
  const visible = filterTaskTree(tree, "D4", "");
  assert.deepEqual(flattenTaskTree(tree.roots, new Set(["sub"]), visible).map(n => n.name), ["orch", "sub", "subsub", "d3", "d4"]);
  const errors = filterTaskTree(tree, "", "errors");
  assert.ok(errors.has("d4") && errors.has("orch") && !flattenTaskTree(tree.roots, new Set(), errors).some(n => n.name === "zzz"));
  assert.equal(filterTaskTree(tree, "", ""), null);
}

// cycle: a -> b -> c -> a is cut at "a" (lexically smallest); self-parent is a root
{
  const tree = buildTaskTree([task("b", "a"), task("c", "b"), task("a", "c"), task("self", "self")]);
  const a = tree.byName.get("a");
  assert.equal(a.parent, null);
  assert.equal(a.cycle, true);
  assert.equal(tree.byName.get("c").depth, 2);
  assert.equal(tree.byName.get("self").cycle, true);
  assert.deepEqual(tree.roots.map(n => n.name).sort(), ["a", "self"]);
  assert.equal(tree.order.length, 4); // every task drawn exactly once
}

// orphan: the parent was deleted (or is an external orchestrator id) -> root with a hint
{
  const tree = buildTaskTree([task("kid", "deleted-parent"), task("grandkid", "kid")]);
  const kid = tree.byName.get("kid");
  assert.equal(kid.parent, null);
  assert.equal(kid.missingParent, "deleted-parent");
  assert.equal(tree.byName.get("grandkid").depth, 1);
}

// a very deep chain is handled iteratively (no stack overflow), duplicates/junk ignored
{
  const rows = [task("n0")];
  for (let i = 1; i < 20000; i++) rows.push(task("n" + i, "n" + (i - 1)));
  rows.push(task("n5"), null, {name: ""});
  const tree = buildTaskTree(rows);
  assert.equal(tree.byName.get("n19999").depth, 19999);
  assert.equal(tree.byName.get("n0").counts.total, 20000);
}

// siblings: newest first
{
  const tree = buildTaskTree([task("p"), task("old", "p", "done", 1), task("new", "p", "done", 9)]);
  assert.deepEqual(tree.byName.get("p").children.map(n => n.name), ["new", "old"]);
}

console.log("test_tree.js: ok");
