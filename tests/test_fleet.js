/* Built-in Node DOM fixture: safe rendering, durable replay, controls and current state. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const crypto = require("node:crypto");

class Node {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.value = "";
    this.disabled = false;
    this.listeners = {};
    this._text = "";
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(" "); }
  set innerHTML(_) { throw new Error("Unsafe innerHTML used"); }
  appendChild(node) { node.parent = this; this.children.push(node); return node; }
  replaceChildren(...nodes) { this.children = []; this._text = ""; nodes.forEach(node => this.appendChild(node)); }
  remove() { this.parent.children = this.parent.children.filter(node => node !== this); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
}

const context = vm.createContext({document: {createElement: tag => new Node(tag), querySelector: () => null},
  crypto, URLSearchParams, jget() {}, jpost() {}, t: key => key});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../static/fleet.js"), "utf8"), context);
const Controller = vm.runInContext("FleetController", context);

function task(name = "one", state = "done") {
  return {name, state, parent: "/root", group: "C:/project", engine: "codex", model_requested: null,
    model_resolved: "resolved", cost: {amount: null, provenance: "unknown"}, delta: {count: null, paths: []},
    owns: ["src/**"], declared_overlap: [], conflicts: [], controls: {send: true, stop: state === "running", resume: state === "done"},
    receipts: {status: "ok", counts: {accepted: 1, delivered: 0, acknowledged: 0}, items: [{id: "a".repeat(32), state: "accepted", accepted: true, delivered: false, acknowledged: false, delivery_status: "pending"}]}};
}
function event(seq, name = "one") {
  return {task: name, id: seq.toString(16).padStart(32, "0"), seq, kind: "task.event", data: {text: "<img src=x onerror=evil()>"}};
}
function page(events, name = "one", seq = events.at(-1)?.seq || 0) {
  return {task: name, events, cursor: {task: name, seq}, head: seq, more: false, torn: false};
}
function setup(api) {
  const fields = {};
  for (const id of ["fleet-refresh", "fleet-parent", "fleet-group", "fleet-send", "fleet-summary",
    "fleet-notice", "fleet-tasks", "fleet-selected", "fleet-controls", "fleet-message",
    "fleet-control-status", "fleet-event-status", "fleet-receipts", "fleet-events"]) fields[id] = new Node();
  const root = {querySelector: selector => fields[selector.slice(1)]};
  const ctl = new Controller(root, api, key => key);
  ctl.init();
  ctl.active = true;
  ctl.current = true;
  ctl.selected = "one";
  ctl.tasks.set("one", task());
  ctl.renderDetail();
  return {ctl, fields};
}
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}
async function run(name, test) { await test(); console.log("PASS: " + name); }

(async () => {
  await run("safe task, receipt and event rendering; ack follows DOM success", async () => {
    const calls = [];
    let ctl;
    ({ctl} = setup({get: async () => page([event(1), event(2)]), post: async (url, body) => {
      assert.equal(ctl.stream("one").host.children.length, 2);
      calls.push({url, body});
      return {cursor: body.cursor};
    }}));
    const malicious = task("<svg onload=evil()>");
    malicious.model_resolved = "<script>evil()</script>";
    malicious.group = "<img onerror=evil()>";
    ctl.tasks.set(malicious.name, malicious);
    ctl.renderTasks();
    assert.match(ctl.el("fleet-tasks").textContent, /<script>evil\(\)<\/script>/);
    await ctl.poll();
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, "/api/task/events/ack");
    assert.equal(calls[0].body.task, "one");
    assert.match(ctl.stream("one").host.textContent, /<img src=x onerror=evil\(\)>/);
    assert.equal(ctl.stream("one").cursor.seq, 2);
    assert.match(ctl.el("fleet-receipts").textContent, /acknowledged: false/);
  });
  await run("failed ack retries persisted cursor without duplicate rows; reconnect uses confirmed cursor", async () => {
    let reads = 0, acks = 0;
    const {ctl} = setup({get: async url => {
      const after = JSON.parse(new URL("http://fixture" + url).searchParams.get("after"));
      reads++;
      assert.equal(after.task, "one");
      if (reads === 1) { assert.equal(after.seq, 0); return page([event(1), event(2)]); }
      assert.equal(after.seq, 2);
      return page([event(3)]);
    }, post: async (_, body) => {
      if (++acks === 1) throw new Error("network interrupted");
      return {cursor: body.cursor};
    }});
    await ctl.poll();
    assert.equal(ctl.stream("one").cursor.seq, 0);
    assert.equal(ctl.stream("one").host.children.length, 2);
    await ctl.poll();
    assert.equal(reads, 1);
    assert.equal(ctl.stream("one").host.children.length, 2);
    await ctl.poll();
    assert.equal(ctl.stream("one").host.children.length, 3);
    assert.equal(ctl.stream("one").cursor.seq, 3);
  });
  await run("partial DOM failure never acknowledges; duplicate prefix is not displayed twice", async () => {
    let acknowledgments = 0;
    const {ctl} = setup({get: async () => page([event(1), event(2)]), post: async (_, body) => {
      acknowledgments++; return {cursor: body.cursor};
    }});
    const state = ctl.stream("one"), append = state.host.appendChild.bind(state.host);
    let attempts = 0;
    state.host.appendChild = node => { if (++attempts === 2) throw new Error("DOM failed"); return append(node); };
    await ctl.poll();
    assert.equal(acknowledgments, 0);
    assert.equal(state.cursor.seq, 0);
    assert.equal(state.host.children.length, 1);
    await ctl.poll();
    assert.equal(acknowledgments, 1);
    assert.equal(state.host.children.length, 2);
  });
  await run("malformed/cross-task cursors and records fail without acknowledgment", async () => {
    for (const invalid of [page([event(1, "two")]), {...page([event(1)]), cursor: {task: "two", seq: 1}},
      page([event(2), event(1)]), page([{...event(1), id: "bad"}]),
      page([event(1), {...event(2), id: event(1).id}]),
      {...page([event(1)]), cursor: {task: "one", seq: 2}}]) {
      let acks = 0;
      const {ctl} = setup({get: async () => invalid, post: async () => { acks++; }});
      await ctl.poll();
      assert.equal(acks, 0);
      assert.equal(ctl.stream("one").host.children.length, 0);
      assert.equal(ctl.stream("one").cursor.seq, 0);
    }
  });
  await run("late event fetch after selecting another task or leaving tab never renders/acks", async () => {
    for (const leave of [false, true]) {
      const pending = deferred();
      let acks = 0;
      const {ctl} = setup({get: () => pending.promise, post: async () => { acks++; }});
      const poll = ctl.poll();
      if (leave) ctl.activate(false);
      else { ctl.tasks.set("two", task("two")); ctl.select("two"); }
      pending.resolve(page([event(1)]));
      await poll;
      assert.equal(acks, 0);
      assert.equal(ctl.stream("one").host.children.length, 0);
    }
  });
  await run("individual controls use current task and legacy paths; sends carry one stable UUID and remain pending", async () => {
    const calls = [];
    const {ctl, fields} = setup({post: async (url, body) => { calls.push({url, body}); return {ok: true, launcher_started: true, message_id: body.message_id}; }, get: async () => ({})});
    ctl.refresh = async () => {};
    fields["fleet-message"].value = "--help";
    await ctl.control("send");
    assert.equal(calls[0].url, "/api/task/send");
    assert.equal(calls[0].body.message, "--help");
    assert.equal(calls[0].body.terminal, false);
    assert.deepEqual(Object.keys(calls[0].body).sort(), ["message", "message_id", "task", "terminal"]);
    assert.match(calls[0].body.message_id, /^[0-9a-f]{32}$/);
    assert.match(fields["fleet-control-status"].textContent, /fleet.launcher_pending/);
    await ctl.control("stop"); // Done task cannot be stopped from stale button state.
    assert.equal(calls.length, 1);
    await ctl.control("resume");
    assert.equal(calls[1].url, "/api/task/resume");
    ctl.tasks.set("one", task("one", "running"));
    await ctl.control("stop");
    assert.equal(calls[2].url, "/api/task/stop");
    assert.deepEqual(Object.keys(calls[2].body), ["task"]);
    ctl.tasks.clear();
    await ctl.control("send");
    assert.equal(calls.length, 3);
  });
  await run("refresh removes deleted selection and failed refresh disables controls honestly", async () => {
    let fail = false;
    const {ctl, fields} = setup({get: async () => {
      if (fail) throw new Error("offline");
      return {tasks: [], summary: {active: 0, done: 0, attention: 0, error: 0}, parents: [], groups: []};
    }, post: async () => { throw new Error("unexpected action"); }});
    await ctl.refresh();
    assert.equal(ctl.selected, null);
    assert.equal(fields["fleet-send"].disabled, true);
    ctl.tasks.set("one", task());
    ctl.selected = "one";
    fail = true;
    await ctl.refresh();
    assert.equal(ctl.current, false);
    assert.equal(fields["fleet-send"].disabled, true);
    await ctl.control("resume");
    assert.match(fields["fleet-notice"].textContent, /offline/);
  });
  await run("filter race rejects stale responses and fetches updated filter", async () => {
    const pending = deferred(), calls = [];
    const empty = {tasks: [], summary: {}, parents: [], groups: []};
    const {ctl, fields} = setup({get: url => {
      calls.push(url);
      return calls.length === 1 ? pending.promise : Promise.resolve(empty);
    }, post: async () => { throw new Error("unexpected ack"); }});
    const refresh = ctl.refresh();
    fields["fleet-parent"].value = "/root/new";
    fields["fleet-parent"].listeners.change();
    pending.resolve({...empty, tasks: [task("obsolete")]});
    await refresh;
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(calls.length, 2);
    assert.match(calls[1], /parent=%2Froot%2Fnew/);
    assert.equal(ctl.tasks.has("obsolete"), false);
  });
  await run("view bounds old rendered events after persisted ack; independent consumers", async () => {
    const {ctl} = setup({get: async () => page(Array.from({length: 205}, (_, index) => event(index + 1))),
      post: async (_, body) => ({cursor: body.cursor})});
    await ctl.poll();
    assert.equal(ctl.stream("one").host.children.length, 200);
    assert.equal(ctl.stream("one").cursor.seq, 205);
    const other = setup({}).ctl;
    assert.notEqual(ctl.consumer, other.consumer);
  });
  await run("evicted views replay with a fresh consumer and do not acknowledge past their DOM", async () => {
    const persisted = new Map(), consumers = [];
    const {ctl} = setup({get: async () => page([event(1)]), post: async (_, body) => {
      consumers.push(body.consumer);
      const seq = Math.max(persisted.get(body.consumer) || 0, body.cursor.seq);
      persisted.set(body.consumer, seq);
      return {cursor: {task: body.task, seq}};
    }});
    await ctl.poll();
    persisted.set(consumers[0], 99);
    for (let index = 0; index < 16; index++) ctl.stream("other-" + index);
    assert.equal(ctl.streams.has("one"), false);
    ctl.renderDetail();
    await ctl.poll();
    assert.notEqual(consumers[0], consumers[1]);
    assert.equal(ctl.stream("one").cursor.seq, 1);
    assert.equal(ctl.stream("one").host.children.length, 1);
  });
  await run("receipt polling correlates the submitted UUID, rejects unrelated receipts and never resends unknown outcomes", async () => {
    const begin = vm.runInContext("beginMessageSubmission", context);
    const poll = vm.runInContext("pollMessageSubmission", context);
    const entry = begin("exact-task");
    entry.phase = "started";
    let requests = 0;
    let text = await poll("exact-task", async url => {
      requests++;
      const params = new URL("http://fixture" + url).searchParams;
      assert.equal(params.get("task"), "exact-task");
      assert.equal(params.get("message_id"), entry.id);
      return {task: "exact-task", message_id: entry.id, status: "ok", receipt: null};
    });
    assert.match(text, /launcher_pending/);
    text = await poll("exact-task", async () => ({task: "exact-task", message_id: entry.id, status: "ok",
      receipt: {id: "a".repeat(32), accepted: true, delivered: true, acknowledged: true}}));
    assert.match(text, /submission_unknown/);
    text = await poll("exact-task", async () => ({task: "exact-task", message_id: entry.id, status: "ok",
      receipt: {id: entry.id, accepted: true, delivered: false, acknowledged: false}}));
    assert.match(text, /accepted: true/);
    assert.match(text, /delivered: false/);
    assert.match(text, /acknowledged: false/);
    assert.equal(requests, 1);
    assert.equal(entry.id.length, 32);
    assert.notEqual(begin("exact-task").id, entry.id); // Only a new user submission gets a new UUID.
  });
  await run("late receipt responses cannot overwrite newer persisted facts or a newer submission", async () => {
    const begin = vm.runInContext("beginMessageSubmission", context), poll = vm.runInContext("pollMessageSubmission", context);
    const entry = begin("race"), pending = deferred();
    const first = poll("race", () => pending.promise);
    await poll("race", async () => ({task: "race", message_id: entry.id, status: "ok", receipt: {id: entry.id, accepted: true, delivered: true, acknowledged: false}}));
    pending.resolve({task: "race", message_id: entry.id, status: "ok", receipt: null});
    await first;
    assert.equal(entry.receipt.delivered, true);
    const late = deferred(), older = poll("race", () => late.promise);
    const latest = begin("race");
    late.resolve({task: "race", message_id: entry.id, status: "ok", receipt: entry.receipt});
    assert.match(await older, new RegExp(latest.id));
    assert.equal(latest.receipt, null);
  });
})().catch(error => { console.error(error); process.exitCode = 1; });
