#!/usr/bin/env node
/* Zero-dependency regression tests for static/toast.js. */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(path.join(__dirname, "..", "static", "toast.js"), "utf8");

function loadToast(seed = [], start = 1_700_000_000_000) {
  const clock = { now: start };
  class FakeDate extends Date {
    constructor(...args) { super(...(args.length ? args : [clock.now])); }
    static now() { return clock.now; }
  }

  const storage = { agentgui_toast_history: JSON.stringify(seed) };
  const elements = {
    "#hist-badge": { style: {}, textContent: "" },
    "#toasts": { children: [], appendChild(el) { this.children.push(el); } },
    "#hist-list": { innerHTML: "" },
    "#m-hist": { classList: { add() {} } },
  };
  const context = {
    Date: FakeDate,
    localStorage: {
      getItem(key) { return Object.hasOwn(storage, key) ? storage[key] : null; },
      setItem(key, value) { storage[key] = String(value); },
      removeItem(key) { delete storage[key]; },
    },
    document: {
      createElement() {
        return { className: "", innerHTML: "", classList: { add() {} }, remove() {} };
      },
    },
    setTimeout() { return 0; },
    esc(value) { return String(value ?? ""); },
    t() { return "empty"; },
  };
  context.$ = selector => elements[selector];
  vm.createContext(context);
  vm.runInContext(SOURCE, context, { filename: "static/toast.js" });
  return {
    clock,
    context,
    elements,
    history: () => JSON.parse(storage.agentgui_toast_history || "[]"),
  };
}

function testRepeatedErrorsCoalesceOnRollingWindow() {
  const env = loadToast();
  assert.equal(env.context.toast("error", "Request failed", "Failed to fetch"), true);
  env.context.openHistory(); // mark the first occurrence seen
  env.clock.now += 59_000;
  assert.equal(env.context.toast("error", "Request failed", "Failed to fetch"), false);
  env.clock.now += 59_000; // >60s from the first, but <60s from the refreshed timestamp
  assert.equal(env.context.toast("error", "Request failed", "Failed to fetch"), false);

  const history = env.history();
  assert.equal(history.length, 1);
  assert.equal(history[0].count, 3);
  assert.equal(history[0].ts, env.clock.now);
  assert.equal(history[0].seen, false);
  assert.equal(env.elements["#toasts"].children.length, 1, "duplicates must not create popups");
  env.context.openHistory();
  assert.match(env.elements["#hist-list"].innerHTML, /×3/);
}

function testOnlyConsecutiveErrorsWithinWindowCoalesce() {
  const env = loadToast();
  env.context.toast("error", "A", "same");
  env.context.toast("warning", "middle", "event");
  env.context.toast("error", "A", "same");
  assert.equal(env.history().length, 3, "an intervening toast ends the consecutive run");

  env.clock.now += 60_001;
  env.context.toast("error", "A", "same");
  assert.equal(env.history().length, 4, "errors outside the rolling window stay distinct");
}

function testRepeatedSuccessesRemainVisibleAndDistinct() {
  const env = loadToast();
  env.context.toast("success", "Started", "one");
  env.context.toast("success", "Started", "one");
  assert.equal(env.history().length, 2);
  assert.equal(env.elements["#toasts"].children.length, 2);
}

function testLegacyAndHostileStoredRecordsAreNormalizedAndBounded() {
  const now = 1_700_000_000_000;
  const seed = Array.from({ length: 90 }, (_, index) => ({
    kind: "error".repeat(10),
    title: "t".repeat(900),
    text: "x".repeat(1800),
    ts: now + 999_999,
    seen: index % 2,
    ...(index === 1 ? {} : { count: 1_000_000 }),
  }));
  const env = loadToast(seed, now);
  env.context.openHistory();
  const history = env.history();
  assert.equal(history.length, 80);
  assert.equal(history[0].kind.length, 20);
  assert.equal(history[0].title.length, 500);
  assert.equal(history[0].text.length, 1000);
  assert.equal(history[0].ts, now);
  assert.equal(history[0].count, 999);
  assert.equal(history[1].count, 1, "legacy records without count mean one occurrence");
}

for (const test of [
  testRepeatedErrorsCoalesceOnRollingWindow,
  testOnlyConsecutiveErrorsWithinWindowCoalesce,
  testRepeatedSuccessesRemainVisibleAndDistinct,
  testLegacyAndHostileStoredRecordsAreNormalizedAndBounded,
]) test();

console.log("ok - toast history coalescing");
