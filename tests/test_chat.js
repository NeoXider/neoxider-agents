/* Chat rendering (static/chat.js) and message sending (static/composer.js) in a minimal DOM
   stub: whole dialog with collapsed tool calls, hidden thinking, status/exit codes, queued
   messages, outcome; and the /api/task/send body with a stable 32-hex UUID.
   Run: node tests/test_chat.js */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const crypto = require("node:crypto");
const {splitUnifiedDiff} = require("../static/diff.js");

function element() {
  return {
    dataset: {}, hidden: false, value: "", checked: false, disabled: false, innerHTML: "", textContent: "",
    style: {}, scrollTop: 0, scrollHeight: 0, clientHeight: 0, listeners: {},
    classList: {toggle() {}, add() {}, remove() {}, contains() { return false; }},
    addEventListener(kind, fn) { this.listeners[kind] = fn; },
    setAttribute() {}, querySelector() { return element(); }, querySelectorAll() { return []; },
    replaceChildren() {}, closest() { return null; }, focus() {},
  };
}
const elements = {};
const document = {
  querySelector: sel => sel === "#tabview-fleet" ? null : (elements[sel] = elements[sel] || element()),
  querySelectorAll: () => [],
  createElement: () => element(),
  documentElement: element(),
};
const storage = new Map();
const posted = [];
const context = vm.createContext({
  document, crypto, TextDecoder, URLSearchParams, console, setTimeout() {}, setInterval() {},
  localStorage: {getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, String(v))},
  window: {innerHeight: 800},
});
for (const file of ["util.js", "i18n.js", "tasktree.js", "fleet.js", "tree.js", "diff.js", "chat.js", "composer.js"]) {
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../static", file), "utf8"), context, {filename: file});
}
vm.runInContext(`
  t = (key) => key;
  stateClass = st => "s-" + st;
  refresh = () => {}; pollOutbox = () => {};
  toast = (kind, title, text) => { throw new Error("unexpected toast: " + title + " " + text); };
  jpost = async (url, body) => { __posted.push([url, body]); return {ok: true, message_id: body.message_id}; };
`, context);
context.__posted = posted;
const run = code => vm.runInContext(code, context);

const task = {name: "orch", engine: "codex", model: "sol", state: "running"};
context.__task = task;

// ---- one step: prompt, thinking, tool calls (ok / failed with exit code / running), text, system note
const step = {
  i: 0, kind: "run", ts: "2026-10-14 10:00:00", epoch: 1, duration: "", prompt: "fix the tests", prompt_label: "PROMPT",
  blocks: [
    {type: "thinking", text: "SECRET-PLAN first idea"},
    {type: "thinking", text: "second idea"},
    {type: "tool", name: "exec", arg: "pytest -q", result: "1 failed", status: "error", code: 2},
    {type: "tool", name: "read", arg: JSON.stringify({filePath: "src/app.py"}), result: "print(1)", status: "ok"},
    {type: "tool", name: "exec", arg: "sleep 5", result: "", status: "running"},
    {type: "text", text: "One test fails; fixing."},
    {type: "system", text: "turn failed: rate limit"},
  ],
};
context.__step = step;
let html = run("renderStep(__task, __step, false)");
// thinking hidden by default: a single thin marker, the text itself is not in the DOM
assert.equal((html.match(/data-toggle-think/g) || []).length, 1, "consecutive thinking shares one marker");
assert.ok(html.includes("chat.thinking_hidden"));
assert.ok(!html.includes("SECRET-PLAN"), "thinking text must not be rendered while hidden");
// tool calls collapsed: one line each with name + short argument + status, no body until opened
assert.equal((html.match(/class="tool /g) || []).length, 3);
assert.ok(!/class="tool open/.test(html), "tool calls start collapsed");
assert.ok(html.includes("<b>exec</b>") && html.includes("pytest -q"));
assert.ok(html.includes("src/app.py") && !html.includes("filePath"), "JSON args summarised by their telling field");
assert.ok(html.includes("tstat err") && html.includes("chat.exit_code"));
assert.ok(html.includes("tstat ok") && !html.includes("tstat run"), "an unanswered call in a finished step is not shown as running");
assert.ok(run("renderStep(__task, __step, true)").includes("tstat run"), "live step shows the running spinner");
assert.ok(!html.includes("1 failed") && !html.includes("print(1)"), "tool output only rendered when expanded");
assert.ok(html.includes("One test fails; fixing.") && html.includes("sysnote") && html.includes("rate limit"));
assert.ok(html.includes('class="msg user"') && html.includes("fix the tests"));

// per-message thinking toggle and global toggle reveal the text
run(`shownThoughts.add("orch:0:0")`);
assert.ok(run("renderStep(__task, __step, false)").includes("SECRET-PLAN"));
run(`shownThoughts.clear(); showThinking = true`);
assert.ok(run("renderStep(__task, __step, false)").includes("second idea"));
run(`showThinking = false`);

// expanded tool shows input and output; long output is clipped with "show all"
run(`expandedTools.add("orch:0:2")`);
html = run("renderStep(__task, __step, false)");
assert.ok(/class="tool open/.test(html) && html.includes("1 failed") && html.includes("chat.tool_input"));
context.__big = {type: "tool", name: "exec", arg: "cat big", result: "x".repeat(10000), status: "ok"};
const body = run("toolBody(__big, 'id-big')");
assert.ok(body.includes("data-full-tool") && !body.includes("x".repeat(5000)));
run(`fullTools.add("id-big")`);
assert.ok(run("toolBody(__big, 'id-big')").includes("x".repeat(10000)));

// whole dialog: several steps + earlier-page button + queued messages + outcome when finished
context.__payload = {name: "orch", state: "error", offset: 30, total_steps: 32, has_more: true,
  steps: [step, Object.assign({}, step, {i: 1, kind: "reply", prompt: "and the docs", prompt_label: "ANSWER", blocks: [{type: "text", text: "done"}]})],
  pending: [{sequence: 3, text: "queued follow-up", inflight: false, id: "ab".repeat(16), receipt_state: "accepted"}],
  outcome: {state: "error", exit: "3", reason: "provider failed"}};
html = run("chatHtml(__task, __payload)");
assert.ok(html.includes("data-load-earlier") && html.includes("data-show-whole"));
assert.ok(html.includes("and the docs") && html.includes("chat.you"));
assert.ok(html.includes("queued follow-up") && html.includes("pending"));
assert.ok(html.includes("outcome") && html.includes("provider failed"));
// XSS: everything is escaped
context.__evil = {i: 0, kind: "run", ts: "", prompt: "", blocks: [{type: "tool", name: "<img src=x onerror=1>", arg: "<script>", status: "ok"}]};
html = run("renderStep(__task, __evil, false)");
assert.ok(!html.includes("<img src=x") && !html.includes("<script>"));

// ---- diff parsing and rendering
const diff = [
  "diff --git a/<file>.txt b/<file>.txt", "index abcd..efab 100644", "--- a/<file>.txt", "+++ b/<file>.txt",
  "@@ -2,2 +7,2 @@ section", " context", "--- old", "+++ <script>", "\\ No newline at end of file",
  "@@ -40 +45 @@", "-another old", "+another new",
  "--- /dev/null", '+++ "b/\\320\\260.txt"', "@@ -0,0 +1 @@", "+new file",
  "--- a/deleted.txt", "+++ /dev/null", "@@ -5 +0,0 @@", "-deleted",
].join("\r\n");
const sections = splitUnifiedDiff("Output:\r\n" + diff + "\r\nDone");
assert.equal(sections[0].text, "Output:");
assert.equal(sections[4].text, "Done");
const [first, added, removed] = sections.slice(1, 4).map(s => s.file);
assert.deepEqual([first.oldPath, first.newPath, first.added, first.removed], ["<file>.txt", "<file>.txt", 2, 2]);
assert.deepEqual(first.hunks[0].rows.map(r => [r.kind, r.oldLine, r.newLine]), [
  ["context", 2, 7], ["remove", 3, undefined], ["add", undefined, 8], ["note", undefined, undefined],
]);
assert.equal(first.hunks[1].rows[1].newLine, 45);
assert.deepEqual([added.oldPath, added.newPath, added.hunks[0].rows[0].newLine], ["/dev/null", "а.txt", 1]);
assert.deepEqual([removed.newPath, removed.hunks[0].rows[0].oldLine], ["/dev/null", 5]);
const incomplete = "--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n-before\n+after";
assert.deepEqual(splitUnifiedDiff(incomplete), [{text: incomplete}]);
assert.equal(splitUnifiedDiff(incomplete, {partial: true})[0].file.added, 1);
assert.equal(splitUnifiedDiff("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\nunexpected")[1].text, "unexpected");
assert.deepEqual(splitUnifiedDiff("ordinary\n+ output\n- no patch"), [{text: "ordinary\n+ output\n- no patch"}]);
const rename = splitUnifiedDiff("diff --git a/old b/new\nsimilarity index 100%\nrename from old\nrename to new");
assert.deepEqual([rename[0].file.oldPath, rename[0].file.newPath, rename[0].file.hunks.length], ["old", "new", 0]);

context.__diff = diff;
context.__patchStep = {
  i: 29, kind: "run", blocks: [
    {type: "text", text: "**Changes**\r\n```diff\r\n" + diff + "\r\n```\r\nAfter"},
    {type: "tool", name: "apply_patch", result: "Output:\n" + diff},
    {type: "text", text: "```c++\n" + diff + "\n```"},
  ],
};
run('expandedTools.add("orch:29:1")');
html = run("renderStep(__task, __patchStep, false)");
assert.equal((html.match(/class="diff-file"/g) || []).length, 6);
assert.match(html, /<b>Changes<\/b>.*diff-header/s);
assert.match(html, /class="diff-plus">\+2<\/span><span class="diff-minus">-2/);
assert.match(html, /class="diff-number">2<\/td><td class="diff-number">7<\/td>/);
assert.ok(html.includes("&lt;file&gt;.txt") && html.includes("&lt;script&gt;") && !html.includes("<script>"));
assert.ok(html.includes("<pre>Output:</pre>") && html.includes('<pre class="code"><code>'));
context.__longDiff = "--- a/x\n+++ b/x\n@@ -0,0 +1,50 @@\n" + Array.from({length: 50}, () => "+" + "x".repeat(90)).join("\n");
html = run('toolBody({result: __longDiff}, "long")');
const hunkKey = html.match(/data-diff-hunk="([^"]+)">/)[1];
assert.ok(html.includes("data-full-tool") && !html.includes("diff-number\">50<"));
elements["#chat"].listeners.toggle({target: {dataset: {diffHunk: hunkKey}, open: true}});
assert.ok(run('toolBody({result: __longDiff}, "long")').includes('data-diff-hunk="' + hunkKey + '" open'));
run('fullTools.add("long")');
assert.ok(run('toolBody({result: __longDiff}, "long")').includes('class="diff-number">50<'));
elements["#chat"].listeners.toggle({target: {dataset: {diffHunk: hunkKey}, open: false}});
assert.ok(!run('toolBody({result: __longDiff}, "long")').includes('data-diff-hunk="' + hunkKey + '" open'));
assert.ok(run('md("```diff\\n" + __longDiff.slice(0, 4000), "assistant", true)').includes("diff-hunk"));

const strip = elements["#active-task-list"];
strip.contains = () => false;
strip.scrollLeft = 50;
context.__liveTasks = [
  {name: "closed", state: "done"}, {name: "parent", state: "done", dir: "other"},
  {name: "child", state: "idle", idle_sec: 500, parent: "parent", engine: "codex", model: "sol", dir: "child-dir",
    age_sec: 95, last_activity: {kind: "tool", age_sec: 20}},
  {name: "running<img>", state: "running", engine: "claude", model: "sonnet", age_sec: 4},
];
run("renderActiveTasks(__liveTasks)");
assert.equal(elements["#active-tasks"].hidden, false);
assert.ok(strip.innerHTML.includes("s-idle") && strip.innerHTML.includes("codex/sol") &&
  strip.innerHTML.includes("1m 35s") && strip.innerHTML.includes("tool · 20s"));
assert.ok(!strip.innerHTML.includes("closed") && strip.innerHTML.includes("running&lt;img&gt;"));
assert.equal(strip.scrollLeft, 50);
run(`treeModel = buildTaskTree(__liveTasks); treeFilter = "hide-child"; treeStateFilter = "done";
  collapsed = new Set(["other"]); collapsedNodes = new Set(["parent"]);`);
strip.listeners.click({target: {closest: () => ({dataset: {activeTask: "child"}})}});
assert.ok(run('SEL === "child" && !collapsed.has("other") && !collapsedNodes.has("parent")'));
assert.ok(run('!treeFilter && !treeStateFilter && revealActiveTask === "child"'));
run("renderActiveTasks([])");
assert.equal(elements["#active-tasks"].hidden, true);

// ---- sending: stable UUID, Enter-to-send body, interrupt (send --now) and next-turn model/effort
const submission = run(`beginMessageSubmission("orch")`);
assert.match(submission.id, /^[0-9a-f]{32}$/);
context.__sub = submission;
const req = run(`buildSendRequest("orch", "hello", {now: true, terminal: false, model: "sonnet", effort: "high"}, __sub)`);
assert.deepEqual(JSON.parse(JSON.stringify(req)), {task: "orch", message: "hello", message_id: submission.id,
  terminal: false, now: true, model: "sonnet", effort: "high"});
const plain = run(`buildSendRequest("orch", "hi", {now: false, model: "", effort: ""}, __sub)`);
assert.ok(!("model" in plain) && !("effort" in plain) && plain.now === false);
assert.notEqual(run(`beginMessageSubmission("orch").id`), submission.id, "each message gets its own UUID");
const steps = run(`receiptSteps({receipt: {accepted: true, delivered: true, acknowledged: false}})`);
assert.deepEqual(JSON.parse(JSON.stringify(steps)), [{key: "accepted", on: true}, {key: "delivered", on: true}, {key: "acknowledged", on: false}]);
assert.deepEqual(JSON.parse(JSON.stringify(run(`receiptSteps({receipt: null})`))).map(s => s.on), [false, false, false]);

// end-to-end through sendReply: posts once with the UUID that the outbox shows
(async () => {
  run(`SEL = "orch"; CURRENT_TASKS = new Map([["orch", __task]]); $("#answer").value = "ship it"; $("#r-now").checked = true; $("#r-model").value = "opus"; $("#r-effort").value = "";`);
  await run("sendReply()");
  assert.equal(posted.length, 1);
  const [url, sent] = posted[0];
  assert.equal(url, "/api/task/send");
  assert.equal(sent.message, "ship it");
  assert.match(sent.message_id, /^[0-9a-f]{32}$/);
  assert.equal(sent.now, true);
  assert.equal(sent.model, "opus");
  const outbox = run(`taskOutbox.get("orch")`);
  assert.equal(outbox.length, 1);
  assert.equal(outbox[0].id, sent.message_id);
  assert.equal(outbox[0].phase, "started");
  console.log("test_chat.js: ok");
})().catch(err => { console.error(err); process.exit(1); });
