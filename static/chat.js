/* Chat thread rendering: full-dialog view, Claude-Code style — the WHOLE conversation of a
   task (every run/reply step in order, with separators), tool calls collapsed to one compact
   line, thinking blocks hidden by default (kept in the data, toggled via a button), and real
   durations per step / per tool call wherever the log carries timestamps.
   Data comes from /api/dialog (gui.py parses every engine's log format and falls back to raw
   text). Uses $/esc/spin/base/fmtDur from util.js, t() from i18n.js. */

function md(src) {
  const blocks = [];
  src = (src || "").replace(/```(\w*)\n?([\s\S]*?)```/g, (m, l, c) => {
    blocks.push('<pre class="code"><code>' + esc(c) + "</code></pre>");
    return "§" + (blocks.length - 1) + "§";
  });
  let out = [], inList = false;
  for (let raw of src.split("\n")) {
    let line = raw.trim();
    if (/^§\d+§$/.test(line)) {
      if (inList) { out.push("</ul>"); inList = false; }
      out.push(line);
      continue;
    }
    let l = esc(raw).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
    let h = l.match(/^(#{1,6})\s+(.*)/);
    if (h) {
      if (inList) { out.push("</ul>"); inList = false; }
      out.push('<div class="mh' + Math.min(h[1].length, 3) + '">' + h[2] + "</div>");
      continue;
    }
    let li = l.match(/^\s*[-*]\s+(.*)/);
    if (li) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push("<li>" + li[1] + "</li>");
      continue;
    }
    if (inList) { out.push("</ul>"); inList = false; }
    out.push(line === "" ? "<br>" : "<div>" + l + "</div>");
  }
  if (inList) out.push("</ul>");
  return out.join("").replace(/§(\d+)§/g, (m, i) => blocks[+i]);
}

/* Full one-line summary of a tool call: first line of its argument (a file path or the start
   of a command), capped so the collapsed row stays a single line. */
function shortArg(arg) {
  let s = (arg || "").split("\n").map(l => l.trim()).filter(Boolean)[0] || "";
  return s.length > 100 ? s.slice(0, 100) + "…" : s;
}

/* Tool-call expansion state lives in a Set keyed by task:step:block, NOT in the DOM, so the
   3s auto-refresh (which rebuilds #chat) restores it exactly — losing an expansion on every
   poll was the single most annoying failure mode of the old view. */
function toggleTool(headEl, id) {
  const box = headEl.parentElement;
  box.classList.toggle("open");
  if (box.classList.contains("open")) expandedTools.add(id);
  else expandedTools.delete(id);
}
function setAllTools(open) {
  document.querySelectorAll("#chat .tool").forEach(box => {
    box.classList.toggle("open", open);
    if (open) expandedTools.add(box.dataset.tid);
    else expandedTools.delete(box.dataset.tid);
  });
}
function toggleThinking() {
  showThinking = !showThinking;
  $("#chat").classList.toggle("show-think", showThinking);
  const b = $("#btn-think");
  if (b) b.classList.toggle("on", showThinking);
}
function showWholeDialog() {
  dlgFull = true;
  lastLog = "";
  const task = SEL ? CURRENT_TASKS.get(SEL) : null;
  if (task) loadThread(task);
  else if (SEL) clearDeletedSelection();
}

/* Live elapsed time on the still-running last step (and nothing else — a finished step shows
   the fixed duration the backend measured; a step with no timestamps shows nothing). */
setInterval(() => {
  document.querySelectorAll("#chat [data-t0live]").forEach(el => {
    el.textContent = fmtDur(Date.now() / 1000 + clockSkew - +el.dataset.t0live);
  });
}, 1000);

function renderStep(task, st, isLastLive) {
  const dur = isLastLive && st.epoch
    ? `<span class="dur" data-t0live="${st.epoch}">${esc(st.duration || "")}</span>`
    : (st.duration ? `<span class="dur">${esc(st.duration)}</span>` : "");
  let h = `<div class="step-sep"><span>${esc(st.kind)}${st.ts ? " · " + esc(st.ts) : ""}</span>${dur}</div>`;
  if (st.prompt) {
    h += `<div class="msg user"><div class="who">${esc(st.prompt_label === "ANSWER" ? t("chat.you") : st.kind)}</div>
      <div class="bubble">${md(st.prompt)}</div></div>`;
  }
  let agent = "";
  for (let j = 0; j < st.blocks.length; j++) {
    const b = st.blocks[j];
    if (b.type === "thinking") {
      agent += `<div class="think">💭 ${esc(b.text)}</div>`;
    } else if (b.type === "tool") {
      const id = `${task.name}:${st.i}:${j}`;
      const open = expandedTools.has(id) ? " open" : "";
      const td = b.duration ? ` <span class="tdur">${esc(b.duration)}</span>` : "";
      const trunc = b.truncated ? `\n\n… ${t("chat.truncated")}` : "";
      agent += `<div class="tool${open}" data-tid="${esc(id)}">
        <div class="tool-h" data-toggle-tool><span class="tw">▸</span> <b>${esc(b.name)}</b> <span class="targ">${esc(shortArg(b.arg))}</span>${td}</div>
        <pre class="tool-b">${esc((b.arg || "") + (b.result ? "\n\n" + b.result : ""))}${esc(trunc)}</pre></div>`;
    } else {
      const trunc = b.truncated ? `<button class="trunc" data-show-whole>… ${esc(t("chat.truncated"))}</button>` : "";
      agent += `<div class="bubble">${md(b.text)}</div>${trunc}`;
    }
  }
  if (agent) h += `<div class="msg agent">${agent}</div>`;
  return h;
}

async function loadThread(task) {
  if (dlgTask !== task.name) { dlgTask = task.name; dlgFull = false; }
  const fullForRequest = dlgFull;
  const requestId = ++dialogRequestId;
  if (dialogController) dialogController.abort();
  const controller = new AbortController();
  dialogController = controller;
  let d;
  try {
    d = await jget("/api/dialog?task=" + encodeURIComponent(task.name) + (fullForRequest ? "&full=1" : ""),
                   { signal: controller.signal });
  } catch (err) {
    if (err.name === "AbortError") return;
    if (err.status === 404 && task.name === SEL) clearDeletedSelection();
    else surfaceApiError(err);
    return;
  }
  if (requestId !== dialogRequestId || controller.signal.aborted || task.name !== SEL || fullForRequest !== dlgFull) return;
  clockSkew = (d.now || Date.now() / 1000) - Date.now() / 1000;
  const whole = d.has_more && !dlgFull
    ? `<button class="mini" data-show-whole>${esc(t("chat.show_whole").replace("{n}", d.total_steps))}</button>` : "";
  $("#chead").innerHTML = `<b>${esc(task.name)}</b>
    <span class="pill">${esc(task.engine)}/${esc(task.model)}</span>
    <span class="pill">${esc(task.files)} ${esc(t("chat.files"))}</span>
    ${task.state === "waiting" ? `<span class="pill" style="color:var(--wait);border-color:var(--wait)">${t("chat.waiting")}</span>` : ""}
    ${task.state === "stalled" ? `<span class="pill" style="color:var(--stall)">${t("chat.stalled")}</span>` : ""}
    ${task.state === "running" ? '<span class="pill" style="color:var(--run);border-color:var(--run)">' + spin(t("chat.running")) + "</span>" : ""}
    ${task.state === "idle" ? '<span class="pill" style="color:var(--run);border-color:var(--run)">' + spin(stateLabel("idle", task.idle_sec)) + "</span>" : ""}
    ${task.state === "error" && task.timeout ? `<span class="pill" style="color:var(--stall)">${t("chat.timeout").replace("{s}", task.timeout)}</span>` : ""}
    <span class="sp"></span><span class="pill" title="dir">${esc(base(task.dir))}</span>
    ${whole}
    <button class="mini" data-tools="expand">${esc(t("chat.expand_all"))}</button>
    <button class="mini" data-tools="collapse">${esc(t("chat.collapse_all"))}</button>
    <button class="mini${showThinking ? " on" : ""}" id="btn-think">💭 ${esc(t("chat.thinking"))}</button>`;
  const wholeButton = $("#chead [data-show-whole]");
  if (wholeButton) wholeButton.addEventListener("click", showWholeDialog);
  const expandButton = $("#chead [data-tools=expand]");
  const collapseButton = $("#chead [data-tools=collapse]");
  if (expandButton) expandButton.addEventListener("click", () => setAllTools(true));
  if (collapseButton) collapseButton.addEventListener("click", () => setAllTools(false));
  $("#btn-think").addEventListener("click", toggleThinking);
  $("#replybar").style.display = "flex";
  $("#btn-reply").disabled = false;
  /* Rebuild #chat only when the log actually changed (mtime+size) or the view mode did —
     otherwise the DOM, the scroll position and every expansion stay exactly as they are. */
  const key = [d.mtime, d.log_size, d.state, d.offset, d.total_steps, dlgFull].join(":");
  if (key === lastLog) return;
  lastLog = key;
  const box = $("#chat");
  box.classList.toggle("show-think", showThinking);
  const near = box.scrollTop + box.clientHeight > box.scrollHeight - 60;
  const live = isLive(d.state);
  box.innerHTML = d.steps.length
    ? d.steps.map(st => renderStep(task, st, live && st.i === d.steps[d.steps.length - 1].i)).join("")
    : `<div class="empty">${t("chat.empty_thread")}</div>`;
  box.querySelectorAll("[data-toggle-tool]").forEach(head =>
    head.addEventListener("click", () => toggleTool(head, head.closest(".tool").dataset.tid)));
  box.querySelectorAll("[data-show-whole]").forEach(button => button.addEventListener("click", showWholeDialog));
  if (near) box.scrollTop = box.scrollHeight;
}
