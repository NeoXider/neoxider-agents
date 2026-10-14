/* Message composer under the chat: send (Enter; Shift+Enter = new line), "interrupt and send"
   (send --now), Stop / Resume, and a model + effort switch for the next turn (CLI `send|restart
   -m MODEL -f EFFORT`), plus an outbox listing every message sent from this panel with its
   stable UUID and persisted receipt (accepted / delivered / acknowledged). The outbox is
   evidence-only: an unknown outcome is shown as unknown, never as delivered, and nothing is
   resent automatically. Uses jget/jpost/$/esc/spin from util.js, t() from i18n.js,
   beginMessageSubmission/messageSubmissionText from fleet.js, modelDetails from app.js. */

const OUTBOX_CAP = 20;
const taskOutbox = new Map(); // task -> [{id, task, preview, at, phase, receipt, error}]

/* Pure: the exact /api/task/send body for one message. `choice` = {now, terminal, model,
   effort}; empty model/effort mean "keep the task's current one". */
function buildSendRequest(task, text, choice, submission) {
  const body = {task, message: text, message_id: submission.id,
    terminal: !!(choice && choice.terminal), now: !!(choice && choice.now)};
  if (choice && choice.model) body.model = choice.model;
  if (choice && choice.effort) body.effort = choice.effort;
  return body;
}

/* Pure: receipt -> [{key, on}] for the three evidence levels, in order. */
function receiptSteps(entry) {
  const r = entry && entry.receipt;
  return ["accepted", "delivered", "acknowledged"].map(key => ({key, on: !!(r && r[key])}));
}

function outboxFor(task) {
  if (!taskOutbox.has(task)) taskOutbox.set(task, []);
  return taskOutbox.get(task);
}

function recordOutbox(task, submission, text) {
  const list = outboxFor(task);
  const entry = {id: submission.id, task, submission, preview: text.replace(/\s+/g, " ").trim().slice(0, 90),
    at: Date.now(), phase: "sending", receipt: null, error: ""};
  list.push(entry);
  while (list.length > OUTBOX_CAP) list.shift();
  return entry;
}

async function pollOutboxEntry(entry, get) {
  try {
    const result = await (get || jget)("/api/task/receipt?" + new URLSearchParams({task: entry.task, message_id: entry.id}));
    if (result.task !== entry.task || result.message_id !== entry.id || result.status !== "ok" ||
        (result.receipt && result.receipt.id !== entry.id)) throw new Error("Receipt evidence unavailable");
    entry.receipt = result.receipt;
    if (result.receipt) entry.phase = "receipt";
  } catch (_) {
    entry.receipt = null;
    if (entry.phase !== "failed") entry.phase = "unknown";
  }
  return entry;
}

function renderOutbox(task) {
  const box = $("#outbox");
  const list = task ? (taskOutbox.get(task) || []) : [];
  const live = list.filter(e => !(e.receipt && e.receipt.acknowledged) || Date.now() - e.at < 60000);
  if (!live.length) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  box.innerHTML = `<div class="obx-h">${esc(t("composer.outbox"))}</div>` + live.slice(-6).map(e => {
    const steps = receiptSteps(e).map(s =>
      `<span class="rstep ${s.on ? "on" : ""}" title="${esc(t("fleet." + s.key))}">${s.on ? "●" : "○"} ${esc(t("composer.r_" + s.key))}</span>`).join("");
    const phase = e.phase === "failed" ? `<span class="rstep err">${esc(e.error || t("toast.reply_not_sent"))}</span>`
      : !e.receipt ? `<span class="rstep">${e.phase === "sending" ? spin() + " " + esc(t("composer.sending")) : esc(t(e.phase === "unknown" ? "composer.unknown" : "fleet.launcher_pending"))}</span>` : "";
    return `<div class="obx-row" data-mid="${esc(e.id)}"><span class="obx-text" title="${esc(e.preview)}">${esc(e.preview)}</span>
      <span class="mono obx-id" title="${esc(e.id)}">${esc(e.id.slice(0, 8))}</span>${phase}${e.receipt ? steps : ""}</div>`;
  }).join("");
}

async function pollOutbox() {
  const task = SEL;
  if (!task || !taskOutbox.has(task)) return;
  const pending = taskOutbox.get(task).filter(e => e.phase !== "sending" && e.phase !== "failed" &&
    !(e.receipt && e.receipt.acknowledged) && Date.now() - e.at < 6 * 3600 * 1000);
  for (const entry of pending) await pollOutboxEntry(entry);
  if (SEL === task) {
    renderOutbox(task);
    const latest = taskSubmissions.get(task);
    if (latest && $("#chat-submission")) $("#chat-submission").textContent = "";
  }
}

/* ---- model / effort for the next turn ---- */
let composerEngine = "";
async function fillComposerModels(task) {
  const engine = task.engine;
  composerEngine = engine;
  const model = $("#r-model"), effort = $("#r-effort");
  const current = String(task.model || "");
  const opts = [["", t("composer.model_keep").replace("{model}", current || "?")]];
  if (!modelDetails[engine]) {
    try {
      const data = await jget("/api/models?engine=" + encodeURIComponent(engine));
      modelDetails[engine] = data.details || (data.models || []).map(id => ({id}));
    } catch (_) {
      modelDetails[engine] = ((PROVIDERS[engine] || {}).models || []).map(id => ({id: String(id)}));
    }
  }
  if (composerEngine !== engine || !SEL || (CURRENT_TASKS.get(SEL) || {}).engine !== engine) return;
  for (const item of modelDetails[engine]) if (item && item.id) opts.push([item.id, item.id]);
  const previous = model.value;
  model.replaceChildren(...opts.map(([value, label]) => {
    const o = document.createElement("option");
    o.value = value;
    o.textContent = label;
    return o;
  }));
  model.value = opts.some(o => o[0] === previous) ? previous : "";
  syncComposerEffort();
}

function syncComposerEffort() {
  const engine = composerEngine, model = $("#r-model").value;
  const details = (modelDetails[engine] || []).find(item => item.id === model);
  const modes = details && details.effort_source === "cli-advertised" && (details.efforts || []).length
    ? details.efforts : ((PROVIDERS[engine] || {}).efforts || []);
  const select = $("#r-effort"), previous = select.value;
  select.replaceChildren(...["", ...modes].map(mode => {
    const o = document.createElement("option");
    o.value = mode;
    o.textContent = mode ? t("composer.effort_label").replace("{effort}", mode) : t("composer.effort_keep");
    return o;
  }));
  select.value = modes.includes(previous) ? previous : "";
  select.disabled = !modes.length;
}

/* Called by chat.js when another task is selected, and on every refresh of the selected one. */
function onComposerTask(task) {
  $("#r-model").value = "";
  $("#r-now").checked = false;
  fillComposerModels(task);
  renderOutbox(task.name);
}
function updateComposer(task) {
  const live = isLive(task.state);
  $("#btn-stop-task").hidden = !live;
  $("#btn-resume-task").hidden = live;
  $("#r-now-wrap").classList.toggle("disabled", !live);
  $("#r-now").disabled = !live;
  if (!live) $("#r-now").checked = false;
  $("#answer").placeholder = t(live ? (($("#r-now").checked) ? "composer.ph_now" : "composer.ph_queue") : "composer.ph_resume");
  $("#btn-reply").textContent = t($("#r-now").checked && live ? "composer.send_now" : "chat.send_message");
}

async function sendReply() {
  const text = $("#answer").value;
  if (!text.trim() || !SEL || !CURRENT_TASKS.has(SEL)) {
    if (SEL && !CURRENT_TASKS.has(SEL)) clearDeletedSelection();
    return;
  }
  const btn = $("#btn-reply");
  btn.disabled = true;
  const task = SEL, liveTask = isLive((CURRENT_TASKS.get(task) || {}).state);
  const submission = beginMessageSubmission(task);
  const entry = recordOutbox(task, submission, text);
  const choice = {now: liveTask && $("#r-now").checked, terminal: $("#r-term").checked,
    model: $("#r-model").value, effort: $("#r-effort").value};
  renderOutbox(task);
  try {
    const r = await jpost("/api/task/send", buildSendRequest(task, text, choice, submission));
    if (r.error) throw new Error(r.error);
    if (r.message_id !== submission.id) throw new Error("Submission outcome unknown; poll the UUID without resending");
    submission.phase = "started";
    entry.phase = "started";
    if (SEL === task) $("#answer").value = "";
    autosizeComposer();
    setTimeout(refresh, 600);
    setTimeout(pollOutbox, 1500);
  } catch (err) {
    entry.phase = "failed";
    entry.error = apiErrorMessage(err);
    err.reported = true;
    toast("error", t("toast.reply_not_sent"), entry.error);
  } finally {
    btn.disabled = false;
    if (SEL === task) renderOutbox(task);
  }
}

async function stopTask(taskName, btn) {
  if (!CURRENT_TASKS.has(taskName)) return;
  btn.disabled = true;
  try {
    const result = await jpost("/api/task/stop", { task: taskName });
    if (result.ok) {
      toast("success", taskName, t("toast.task_stopped"));
      await refresh();
    }
  } catch (err) {
    surfaceApiError(err);
  } finally {
    btn.disabled = false;
  }
}

async function resumeTask(taskName, btn) {
  if (!CURRENT_TASKS.has(taskName)) return;
  btn.disabled = true;
  const body = {task: taskName, terminal: $("#r-term").checked};
  if ($("#r-model").value) body.model = $("#r-model").value;
  if ($("#r-effort").value) body.effort = $("#r-effort").value;
  try {
    const result = await jpost("/api/task/resume", body);
    if (result.ok) {
      toast("info", taskName, t("composer.resume_started"));
      setTimeout(refresh, 800);
    }
  } catch (err) {
    surfaceApiError(err);
  } finally {
    btn.disabled = false;
  }
}

function autosizeComposer() {
  const ta = $("#answer");
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight + 2, Math.round(window.innerHeight * 0.35)) + "px";
}

$("#answer").addEventListener("keydown", e => {
  if (e.key !== "Enter" || e.isComposing || e.keyCode === 229) return;
  if (e.shiftKey || e.altKey) return; // new line
  e.preventDefault();
  sendReply();
});
$("#answer").addEventListener("input", autosizeComposer);
$("#btn-reply").addEventListener("click", sendReply);
$("#r-model").addEventListener("change", syncComposerEffort);
$("#r-now").addEventListener("change", () => { const task = CURRENT_TASKS.get(SEL); if (task) updateComposer(task); });
$("#btn-stop-task").addEventListener("click", () => SEL && stopTask(SEL, $("#btn-stop-task")));
$("#btn-resume-task").addEventListener("click", () => SEL && resumeTask(SEL, $("#btn-resume-task")));

if (typeof module !== "undefined" && module.exports) {
  module.exports = {buildSendRequest, receiptSteps};
}
