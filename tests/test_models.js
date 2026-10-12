/* Per-model mode selection and async provider switching, using only Node built-ins. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const fields = {};
function field(value = "") {
  return {value, children: [], replaceChildren(...children) { this.children = children; },
    disabled: false, title: "", textContent: ""};
}
for (const prefix of ["f", "brg"]) {
  fields[`#${prefix}-engine`] = field("opencode");
  fields[`#${prefix}-model`] = field("opencode/a");
  fields[`#${prefix}-effort`] = field("max");
  fields[`#${prefix}-model-info`] = field();
}
fields["#models"] = field();
let requests = [];
const context = vm.createContext({$: id => fields[id], PROVIDERS: {
  opencode: {efforts: ["high", "max"], models: ["a"]}, other: {efforts: ["low"]}},
  document: {createElement: () => field()}, t: key => key, loadProvider() {},
  jget: url => new Promise(resolve => requests.push({url, resolve}))});
const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
vm.runInContext(source.slice(0, source.indexOf("async function submitRun")), context);
vm.runInContext(`modelDetails.opencode = [{id:'opencode/a', efforts:['high'], effort_source:'cli-advertised',
  limits:{context:2000}, capabilities:{reasoning:true,toolcall:false},free_advertised:true}]; syncModelEfforts('f');`, context);
assert.deepEqual(fields["#f-effort"].children.map(x => x.value), ["", "high"]);
assert.equal(fields["#f-effort"].value, "");
assert.match(fields["#f-model-info"].textContent, /tools: false/);
fields["#f-model"].value = "unknown";
vm.runInContext("syncModelEfforts('f')", context);
assert.match(fields["#f-effort"].children[1].textContent, /unverified/);
assert.match(fields["#f-effort"].title, /does not advertise/);

(async () => {
  const first = vm.runInContext("syncModels()", context);
  fields["#f-engine"].value = "other";
  const second = vm.runInContext("syncModels()", context);
  requests[1].resolve({models: ["other/b"], details: []});
  await second;
  requests[0].resolve({models: ["stale/model"], details: []});
  await first;
  assert.equal(fields["#models"].children[0].value, "other/b");
  assert.deepEqual(fields["#f-effort"].children.map(x => x.value), ["", "low"]);
  console.log("PASS: model-specific modes, unknown hints, metadata, stale response guard");
})().catch(error => { console.error(error); process.exitCode = 1; });
