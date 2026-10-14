/* Fleet is an integrated tab. All task/provider text enters the DOM as textContent.
   Polling uses durable task-bound cursors; log SSE is deliberately unrelated. */
function createMessageId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join("");
}
const taskSubmissions = new Map();
function beginMessageSubmission(task) {
  const entry = {task, id: createMessageId(), phase: "unknown", receipt: null};
  if (taskSubmissions.size >= 64) taskSubmissions.delete(taskSubmissions.keys().next().value);
  taskSubmissions.set(task, entry);
  return entry;
}
function messageSubmissionText(entry) {
  if (!entry) return "";
  const receipt = entry.receipt;
  if (!receipt) return entry.id + " · " + t(entry.phase === "started" ? "fleet.launcher_pending" : "fleet.submission_unknown");
  return `${entry.id} · ${t("fleet.accepted")}: ${receipt.accepted} · ${t("fleet.delivered")}: ${receipt.delivered} · ${t("fleet.acknowledged")}: ${receipt.acknowledged}${receipt.ack_source ? " (" + receipt.ack_source + ")" : ""}`;
}
async function pollMessageSubmission(task, get = jget) {
  const entry = taskSubmissions.get(task);
  if (!entry) return "";
  const version = entry.pollVersion = (entry.pollVersion || 0) + 1;
  try {
    const result = await get("/api/task/receipt?" + new URLSearchParams({task, message_id: entry.id}));
    if (taskSubmissions.get(task) !== entry || entry.pollVersion !== version) return messageSubmissionText(taskSubmissions.get(task));
    if (result.task !== task || result.message_id !== entry.id || result.status !== "ok" ||
        (result.receipt && result.receipt.id !== entry.id)) throw new Error("Receipt evidence unavailable");
    entry.receipt = result.receipt;
  } catch (_) {
    if (taskSubmissions.get(task) !== entry || entry.pollVersion !== version) return messageSubmissionText(taskSubmissions.get(task));
    entry.receipt = null;
    entry.phase = "unknown";
  }
  return taskSubmissions.get(task) === entry ? messageSubmissionText(entry) : "";
}

class FleetController {
  constructor(root, api, translate) {
    this.root = root;
    this.api = api;
    this.t = translate;
    this.tasks = new Map();
    this.streams = new Map();
    this.streamSerial = 0;
    this.selected = null;
    this.active = false;
    this.current = false;
    this.refreshing = false;
    this.polling = false;
    this.controlling = false;
    this.epoch = 0;
    this.viewEpoch = 0;
    // getRandomValues also works on authenticated LAN HTTP; randomUUID is secure-context-only.
    this.consumer = "gui-" + Array.from(crypto.getRandomValues(new Uint8Array(16)),
      byte => byte.toString(16).padStart(2, "0")).join("");
  }
  el(id) { return this.root.querySelector("#" + id); }
  node(tag, text, cls) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = String(text);
    if (cls) node.className = cls;
    return node;
  }
  label(key) { return this.t("fleet." + key); }
  init() {
    this.el("fleet-refresh").addEventListener("click", () => this.refresh());
    for (const id of ["fleet-parent", "fleet-group"]) {
      this.el(id).addEventListener("change", () => {
        this.select(null);
        this.tasks.clear();
        this.current = false;
        // A filter change during a fetch invalidates its response and schedules the new one.
        this.filterVersion = (this.filterVersion || 0) + 1;
        this.refresh();
      });
    }
    this.el("fleet-send").addEventListener("click", () => this.control("send"));
  }
  activate(active) {
    this.active = active;
    this.epoch++;
    this.viewEpoch++;
    if (active) this.refresh();
  }
  options(id, values) {
    const select = this.el(id), previous = select.value;
    const all = this.node("option", this.label("all"));
    all.value = "";
    select.replaceChildren(all, ...values.filter(Boolean).map(value => {
      const option = this.node("option", value);
      option.value = value;
      return option;
    }));
    select.value = values.includes(previous) ? previous : "";
  }
  async refresh() {
    if (this.refreshing || !this.active) return;
    this.refreshing = true;
    const version = this.filterVersion || 0;
    const viewEpoch = this.viewEpoch;
    const params = new URLSearchParams();
    for (const id of ["parent", "group"]) {
      const value = this.el("fleet-" + id).value;
      if (value) params.set(id, value);
    }
    try {
      const data = await this.api.get("/api/fleet?" + params);
      if (!this.active || viewEpoch !== this.viewEpoch || version !== (this.filterVersion || 0)) return;
      if (!Array.isArray(data.tasks) || !data.summary) throw new Error("Invalid fleet response");
      this.tasks = new Map(data.tasks.map(task => [task.name, task]));
      this.current = true;
      this.options("fleet-parent", data.parents || []);
      this.options("fleet-group", data.groups || []);
      this.el("fleet-summary").replaceChildren(...["active", "done", "attention", "error"].map(key =>
        this.node("span", `${this.label(key)}: ${data.summary[key] ?? "?"}`, "pill")));
      this.el("fleet-notice").textContent = this.label("page") + " · " +
        (data.incomplete || data.comparison_incomplete ? this.label("partial") + " · " : "") +
        String(data.attribution || "");
      this.renderTasks();
      if (this.selected && !this.tasks.has(this.selected)) this.select(null);
      else this.renderDetail();
      if (this.selected && taskSubmissions.has(this.selected)) {
        const selected = this.selected, text = await pollMessageSubmission(selected, this.api.get);
        if (this.selected === selected) this.el("fleet-control-status").textContent = text;
      }
      await this.poll();
    } catch (error) {
      this.current = false;
      this.el("fleet-notice").textContent = this.label("stale") + ": " + String(error.message || error);
      this.renderDetail();
    } finally {
      this.refreshing = false;
      if (this.active && (viewEpoch !== this.viewEpoch || version !== (this.filterVersion || 0))) this.refresh();
    }
  }
  renderTasks() {
    const cards = [...this.tasks.values()].map(task => {
      const card = this.node("article", undefined, "box fleetcard");
      const choose = this.node("button", `${task.name} · ${task.state}`);
      choose.addEventListener("click", () => this.select(task.name));
      card.appendChild(choose);
      card.appendChild(this.node("p", `${task.parent || "—"} · ${task.group || "—"}`, "subtitle"));
      card.appendChild(this.node("p", `${task.engine} · ${this.label("requested")}: ${task.model_requested ?? this.label("unknown")} · ${this.label("resolved")}: ${task.model_resolved ?? this.label("unknown")}`));
      const cost = task.cost || {};
      card.appendChild(this.node("p", `${this.label("cost")}: ${cost.amount == null ? this.label("unknown") : cost.amount + " " + cost.currency} · ${cost.provenance || this.label("unknown")}`, "subtitle"));
      const delta = task.delta || {};
      card.appendChild(this.node("p", `${this.label("delta")}: ${delta.count ?? this.label("unknown")}${delta.pending ? " · " + this.label("pending") : ""} · ${(delta.paths || []).join(", ")}${delta.omitted ? " · +" + delta.omitted : ""}`));
      if (delta.warning) card.appendChild(this.node("p", delta.warning, "subtitle"));
      card.appendChild(this.node("p", `${this.label("owns")}: ${(task.owns || []).join(", ") || "—"}${task.owns_omitted ? " · +" + task.owns_omitted : ""}`));
      if ((task.declared_overlap || []).length) card.appendChild(this.node("p", this.label("overlap") + ": " + task.declared_overlap.join(", "), "fleetattention"));
      for (const conflict of task.conflicts || []) card.appendChild(this.node("p", `${this.label("conflict")}: ${conflict.task} · ${(conflict.paths || []).join(", ")} · ${conflict.note}`, "fleetattention"));
      if (task.reason) card.appendChild(this.node("p", task.reason, "fleetattention"));
      return card;
    });
    this.el("fleet-tasks").replaceChildren(...(cards.length ? cards : [this.node("p", this.label("empty"))]));
  }
  select(name) {
    this.epoch++;
    this.selected = this.tasks.has(name) ? name : null;
    this.el("fleet-message").value = "";
    this.el("fleet-control-status").textContent = "";
    this.el("fleet-event-status").textContent = "";
    this.renderDetail();
    this.poll();
  }
  stream(name) {
    if (!this.streams.has(name)) {
      if (this.streams.size >= 16) this.streams.delete(this.streams.keys().next().value);
      this.streams.set(name, {cursor: {task: name, seq: 0}, pending: null,
        consumer: this.consumer + ":" + (++this.streamSerial),
        ids: new Set(), nodes: [], host: this.node("div")});
    }
    return this.streams.get(name);
  }
  renderDetail() {
    const task = this.tasks.get(this.selected);
    this.el("fleet-selected").textContent = task ? `${task.name} · ${task.state}` : this.label("select");
    const controls = [];
    if (task) {
      for (const action of ["stop", "resume"]) {
        const button = this.node("button", this.label(action));
        button.disabled = !this.current || this.controlling || !(task.controls || {})[action];
        if (action === "stop") button.className = "danger";
        button.addEventListener("click", () => this.control(action));
        controls.push(button);
      }
    }
    this.el("fleet-controls").replaceChildren(...controls);
    this.el("fleet-send").disabled = !task || !this.current || this.controlling || !(task.controls || {}).send;
    this.el("fleet-message").disabled = !task || !this.current;
    const receipt = task && task.receipts;
    const receiptNodes = [];
    if (!receipt || receipt.status !== "ok" || !receipt.counts) {
      receiptNodes.push(this.node("p", (receipt && receipt.error) || this.label("receipt_unknown")));
    } else {
      receiptNodes.push(this.node("p", `${this.label("accepted")}: ${receipt.counts.accepted} · ${this.label("delivered")}: ${receipt.counts.delivered} · ${this.label("acknowledged")}: ${receipt.counts.acknowledged}`));
      for (const item of receipt.items || []) receiptNodes.push(this.node("p", `${item.id} · ${item.state} · ${this.label("accepted")}: ${item.accepted} · ${this.label("delivered")}: ${item.delivered} · ${this.label("acknowledged")}: ${item.acknowledged} · ${item.delivery_status}`, "mono"));
      if (receipt.omitted) receiptNodes.push(this.node("p", "+" + receipt.omitted));
    }
    this.el("fleet-receipts").replaceChildren(...receiptNodes);
    this.el("fleet-events").replaceChildren(...(task ? [this.stream(task.name).host] : []));
  }
  async control(action) {
    const name = this.selected, task = this.tasks.get(name), epoch = this.epoch;
    if (!this.current || this.controlling || !task || !(task.controls || {})[action]) return;
    const body = {task: name};
    let submission;
    if (action === "send") {
      body.message = this.el("fleet-message").value;
      if (!body.message.trim()) return;
      submission = beginMessageSubmission(name);
      body.message_id = submission.id;
      body.terminal = false;
    }
    if (action === "resume") body.terminal = false;
    this.controlling = true;
    this.renderDetail();
    try {
      const result = await this.api.post("/api/task/" + action, body);
      if (result.error || result.ok !== true) throw new Error(result.error || "Control command did not succeed");
      if (submission) {
        if (result.message_id !== submission.id) throw new Error("Submission outcome unknown; poll this UUID without resending");
        submission.phase = "started";
      }
      if (this.selected === name && this.epoch === epoch) {
        this.el("fleet-control-status").textContent = action === "stop" ? this.label("stopped") :
          submission ? messageSubmissionText(submission) : this.label("launcher_pending");
        if (action === "send") this.el("fleet-message").value = "";
      }
      await this.refresh();
    } catch (error) {
      if (this.selected === name && this.epoch === epoch) this.el("fleet-control-status").textContent =
        (submission ? messageSubmissionText(submission) + " · " : "") + String(error.message || error);
    } finally {
      this.controlling = false;
      this.renderDetail();
    }
  }
  async poll() {
    const name = this.selected, epoch = this.epoch;
    if (!this.active || !this.current || !this.tasks.has(name) || this.polling) return;
    this.polling = true;
    const state = this.stream(name);
    const valid = () => this.active && this.current && this.selected === name && this.epoch === epoch;
    try {
      if (!state.pending) {
        const params = new URLSearchParams({task: name, after: JSON.stringify(state.cursor), consumer: state.consumer});
        const page = await this.api.get("/api/task/events?" + params);
        if (!valid()) return;
        if (page.task !== name || !Array.isArray(page.events) || !page.cursor || page.cursor.task !== name ||
            !Number.isSafeInteger(page.cursor.seq) || page.cursor.seq < state.cursor.seq) throw new Error("Invalid task-bound event page");
        let previous = state.cursor.seq;
        const pageIds = new Set();
        for (const event of page.events) {
          if (event.task !== name || !Number.isSafeInteger(event.seq) || event.seq <= previous ||
              !/^[0-9a-f]{32}$/.test(event.id) || pageIds.has(event.id) ||
              (state.ids.has(event.id) && !state.nodes.some(node => node.id === event.id && node.seq === event.seq)))
            throw new Error("Invalid durable event");
          pageIds.add(event.id);
          previous = event.seq;
        }
        if (page.cursor.seq !== previous) throw new Error("Cursor does not match rendered events");
        // Build and attach before any ack. A DOM failure leaves the durable cursor intact.
        for (const event of page.events) {
          if (state.ids.has(event.id)) continue;
          const row = this.node("p", `${event.seq} · ${event.kind} · ${JSON.stringify(event.data).slice(0, 16000)}`, "mono fleetevent");
          state.host.appendChild(row);
          state.nodes.push({id: event.id, seq: event.seq, row});
          state.ids.add(event.id);
        }
        if (page.events.length) state.pending = page.cursor;
        this.el("fleet-event-status").textContent = page.torn ? this.label("torn") : this.label("replay");
      }
      if (state.pending && valid()) {
        const result = await this.api.post("/api/task/events/ack", {task: name, consumer: state.consumer, cursor: state.pending});
        if (!result.cursor || result.cursor.task !== name || result.cursor.seq !== state.pending.seq) throw new Error("Invalid persisted acknowledgment");
        state.cursor = result.cursor;
        state.pending = null;
        // A compact view may discard older rendered rows only after durable acknowledgement.
        while (state.nodes.length > 200) {
          const oldest = state.nodes.shift();
          oldest.row.remove();
          state.ids.delete(oldest.id);
        }
      }
    } catch (error) {
      if (valid()) this.el("fleet-event-status").textContent = this.label("retry") + ": " + String(error.message || error);
    } finally {
      this.polling = false;
    }
  }
}

const fleetController = document.querySelector("#tabview-fleet") ?
  new FleetController(document.querySelector("#tabview-fleet"), {get: jget, post: jpost}, key => t(key)) : null;
if (fleetController) fleetController.init();
