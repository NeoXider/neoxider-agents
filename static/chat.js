/* Chat thread rendering: the WHOLE conversation of a task, Claude-Code style -- every
   run/reply step in order (user/orchestrator messages, assistant text, system notes), tool calls
   collapsed to one line (name + short argument + status/exit code, click for full input/output),
   thinking hidden by default (one thin marker; a global toggle plus a per-message toggle),
   messages still queued in the inbox at the bottom, and the outcome of a finished task.
   History is paged from the end: "load earlier" (button or scrolling to the top) grows the
   window by 30 steps, keeping the reading position. Auto-scroll only when already at the bottom.
   Data comes from /api/dialog (gui.py parses every engine's log format, redacts secrets and
   falls back to raw text). Uses $/esc/spin/base/fmtDur from util.js, t() from i18n.js. */

const DLG_PAGE = 30;
const TOOL_PREVIEW = 4000;   // chars of a tool's input/output shown before "show all"
const PROMPT_PREVIEW = 700;  // chars of a long prompt shown before "show all"
let dlgLimit = DLG_PAGE, dlgHasMore = false, dlgLoadingEarlier = false;
let dlgData = null, dlgBlocks = new Map();
let shownThoughts = new Set(), fullTools = new Set(), fullPrompts = new Set();

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

/* One-line summary of a tool call's argument. JSON inputs show their most telling field
   (command, file path, pattern, URL, query, description); anything else its first line. */
const ARG_KEYS = ["command", "cmd", "filePath", "file_path", "path", "pattern", "url", "query",
  "description", "prompt", "action", "name"];
function shortArg(arg) {
  let s = String(arg || "").trim();
  if (s.startsWith("{")) {
    try {
      const o = JSON.parse(s);
      if (o && typeof o === "object") {
        const key = ARG_KEYS.find(k => typeof o[k] === "string" && o[k].trim());
        if (key) s = o[key];
        else {
          const first = Object.entries(o).find(([, v]) => typeof v === "string" || typeof v === "number");
          s = first ? first[0] + ": " + first[1] : s;
        }
      }
    } catch (_) { /* not JSON: keep the raw text */ }
  }
  s = String(s).split("\n").map(l => l.trim()).filter(Boolean)[0] || "";
  return s.length > 120 ? s.slice(0, 120) + "…" : s;
}

/* Status chip for a collapsed tool call: running spinner, ok tick, error cross + exit code. */
function toolStatus(b) {
  const code = b.code !== null && b.code !== undefined && b.code !== "" ? b.code : null;
  if (b.status === "running") return `<span class="tstat run" title="${esc(t("chat.tool_running"))}">${spin()}</span>`;
  if (b.status === "error" || (code !== null && +code !== 0))
    return `<span class="tstat err" title="${esc(t("chat.tool_failed"))}">✗${code !== null ? " " + esc(t("chat.exit_code").replace("{code}", code)) : ""}</span>`;
  if (b.status === "ok") return `<span class="tstat ok" title="${esc(t("chat.tool_ok"))}">✓${code !== null ? " " + esc(code) : ""}</span>`;
  return "";
}

function clipped(text, id, limit) {
  text = String(text || "");
  if (text.length <= limit || fullTools.has(id)) return {text, more: ""};
  return {text: text.slice(0, limit),
    more: `<button class="linkbtn" data-full-tool="${esc(id)}">${esc(t("chat.show_all").replace("{n}", text.length.toLocaleString()))}</button>`};
}

function toolBody(b, id) {
  const input = clipped(b.arg, id, TOOL_PREVIEW), output = clipped(b.result, id, TOOL_PREVIEW);
  let h = "";
  if (b.arg) h += `<div class="tio"><div class="tlabel">${esc(t("chat.tool_input"))}</div><pre>${esc(input.text)}</pre>${input.more}</div>`;
  if (b.result) h += `<div class="tio"><div class="tlabel">${esc(t("chat.tool_output"))}</div><pre>${esc(output.text)}</pre>${output.more}</div>`;
  if (!b.arg && !b.result) h = `<div class="tio dim">${esc(t(b.status === "running" ? "chat.tool_running" : "chat.tool_no_output"))}</div>`;
  if (b.truncated) h += `<button class="linkbtn" data-show-whole>${esc(t("chat.truncated"))}</button>`;
  return h;
}

function renderTool(b, id) {
  const open = expandedTools.has(id);
  const td = b.duration ? `<span class="tdur">${esc(b.duration)}</span>` : "";
  return `<div class="tool${open ? " open" : ""} ts-${esc(b.status || "unknown")}" data-tid="${esc(id)}">
    <button class="tool-h" type="button" data-toggle-tool aria-expanded="${open}">
      <span class="tw" aria-hidden="true">▸</span><b>${esc(b.name)}</b><span class="targ">${esc(shortArg(b.arg))}</span>${toolStatus(b)}${td}
    </button>
    <div class="tool-b">${open ? toolBody(b, id) : ""}</div></div>`;
}

/* Consecutive thinking blocks share one thin marker; the text is in the DOM only when shown. */
function renderThinking(texts, id) {
  const shown = showThinking || shownThoughts.has(id);
  return `<div class="think${shown ? " shown" : ""}" data-thid="${esc(id)}">
    <button class="think-h" type="button" data-toggle-think aria-expanded="${shown}">💭 ${esc(t(shown ? "chat.thinking_shown" : "chat.thinking_hidden"))}</button>
    ${shown ? `<div class="think-b">${esc(texts.join("\n\n"))}</div>` : ""}</div>`;
}

function renderPrompt(st, task) {
  const id = `${task.name}:${st.i}:p`;
  const long = st.prompt.length > PROMPT_PREVIEW && !fullPrompts.has(id);
  const who = st.prompt_label === "ANSWER" ? t("chat.you") : st.i === 0 || st.kind === "run" ? t("chat.task_prompt") : st.kind;
  return `<div class="msg user"><div class="who">${esc(who)}</div>
    <div class="bubble${long ? " clipped" : ""}">${md(long ? st.prompt.slice(0, PROMPT_PREVIEW) + "…" : st.prompt)}</div>
    ${long ? `<button class="linkbtn" data-full-prompt="${esc(id)}">${esc(t("chat.show_all").replace("{n}", st.prompt.length.toLocaleString()))}</button>` : ""}</div>`;
}

function renderStep(task, st, isLastLive) {
  const dur = isLastLive && st.epoch
    ? `<span class="dur" data-t0live="${st.epoch}">${esc(st.duration || "")}</span>`
    : (st.duration ? `<span class="dur">${esc(st.duration)}</span>` : "");
  let h = `<div class="step-sep" role="separator"><span>${esc(st.kind)}${st.ts ? " · " + esc(st.ts) : ""}</span>${dur}</div>`;
  if (st.prompt) h += renderPrompt(st, task);
  let agent = "", thoughts = [], thoughtId = "";
  const flush = () => {
    if (thoughts.length) agent += renderThinking(thoughts, thoughtId);
    thoughts = [];
  };
  for (let j = 0; j < st.blocks.length; j++) {
    const b = st.blocks[j], id = `${task.name}:${st.i}:${j}`;
    if (b.type === "thinking") {
      if (!thoughts.length) thoughtId = id;
      thoughts.push(b.text || "");
      continue;
    }
    flush();
    if (b.type === "tool") {
      // a call that never reported back in a step that is over did not "keep running"
      const shown = b.status === "running" && !isLastLive ? Object.assign({}, b, {status: ""}) : b;
      dlgBlocks.set(id, shown);
      agent += renderTool(shown, id);
    } else if (b.type === "system") {
      agent += `<div class="sysnote" role="note">⚠ ${esc(b.text)}</div>`;
    } else {
      const trunc = b.truncated ? `<button class="linkbtn" data-show-whole>… ${esc(t("chat.truncated"))}</button>` : "";
      agent += `<div class="bubble">${md(b.text)}</div>${trunc}`;
    }
  }
  flush();
  if (agent) h += `<div class="msg agent"><div class="who">${esc(t("chat.assistant"))}</div>${agent}</div>`;
  return h;
}

function renderPending(pending) {
  if (!pending || !pending.length) return "";
  return pending.map(p => `<div class="msg user pending">
      <div class="who">${esc(t(p.inflight ? "chat.pending_inflight" : "chat.pending"))} · #${esc(p.sequence)}${p.id ? " · " + esc(p.id.slice(0, 8)) : ""}${p.receipt_state ? " · " + esc(p.receipt_state) : ""}</div>
      <div class="bubble">${md(p.text)}${p.truncated ? "…" : ""}</div></div>`).join("");
}

function renderOutcome(o) {
  if (!o || !o.state) return "";
  const parts = [t("chat.outcome").replace("{state}", stateLabel(o.state))];
  if (o.exit !== "" && o.exit !== undefined && o.exit !== null) parts.push(t("chat.exit_code").replace("{code}", o.exit));
  if (o.reason) parts.push(o.reason);
  return `<div class="sysnote outcome ${stateClass(o.state)}" role="note">${esc(parts.join(" · "))}</div>`;
}

/* ---- interaction (delegated on #chat: survives every rebuild) ---- */
function toggleTool(box) {
  const id = box.dataset.tid, open = !box.classList.contains("open");
  box.classList.toggle("open", open);
  box.querySelector(".tool-h").setAttribute("aria-expanded", open);
  if (open) expandedTools.add(id);
  else expandedTools.delete(id);
  const b = dlgBlocks.get(id);
  box.querySelector(".tool-b").innerHTML = open && b ? toolBody(b, id) : "";
}
function setAllTools(open) {
  document.querySelectorAll("#chat .tool").forEach(box => {
    if (box.classList.contains("open") !== open) toggleTool(box);
  });
}
function toggleThinking() {
  showThinking = !showThinking;
  try { localStorage.setItem("agentgui_show_thinking", showThinking ? "1" : "0"); } catch (_) { /* ignore */ }
  const b = $("#btn-think");
  if (b) { b.classList.toggle("on", showThinking); b.setAttribute("aria-pressed", showThinking); }
  rebuildChat({passive: true});
}
function showWholeDialog() {
  dlgFull = true;
  lastLog = "";
  const task = SEL ? CURRENT_TASKS.get(SEL) : null;
  if (task) loadThread(task);
  else if (SEL) clearDeletedSelection();
}
function loadEarlier() {
  if (!dlgHasMore || dlgLoadingEarlier || dlgFull) return;
  const task = SEL ? CURRENT_TASKS.get(SEL) : null;
  if (!task) return;
  dlgLoadingEarlier = true;
  dlgLimit += DLG_PAGE;
  lastLog = "";
  const btn = $("#chat [data-load-earlier]");
  if (btn) btn.innerHTML = spin(t("chat.loading"));
  loadThread(task, {keepPosition: true}).finally(() => { dlgLoadingEarlier = false; });
}

$("#chat").addEventListener("click", event => {
  const el = event.target.closest("button, [data-toggle-tool]");
  if (!el) return;
  if (el.matches("[data-toggle-tool]")) return toggleTool(el.closest(".tool"));
  if (el.matches("[data-toggle-think]")) {
    const box = el.closest(".think"), id = box.dataset.thid;
    if (showThinking) return toggleThinking();
    if (shownThoughts.has(id)) shownThoughts.delete(id); else shownThoughts.add(id);
    return rebuildChat({passive: true});
  }
  if (el.matches("[data-full-tool]")) {
    fullTools.add(el.dataset.fullTool);
    const box = el.closest(".tool");
    const b = dlgBlocks.get(el.dataset.fullTool);
    if (box && b) box.querySelector(".tool-b").innerHTML = toolBody(b, el.dataset.fullTool);
    return;
  }
  if (el.matches("[data-full-prompt]")) { fullPrompts.add(el.dataset.fullPrompt); return rebuildChat({passive: true}); }
  if (el.matches("[data-show-whole]")) return showWholeDialog();
  if (el.matches("[data-load-earlier]")) return loadEarlier();
});

function chatNearBottom(box) {
  return box.scrollTop + box.clientHeight > box.scrollHeight - 60;
}
$("#chat").addEventListener("scroll", () => {
  const box = $("#chat");
  if (chatNearBottom(box)) $("#chat-jump").hidden = true;
  if (box.scrollTop < 40 && dlgHasMore && !dlgLoadingEarlier && box.dataset.userScrolled === "1") loadEarlier();
}, {passive: true});
["wheel", "touchmove", "keydown"].forEach(kind =>
  $("#chat").addEventListener(kind, () => { $("#chat").dataset.userScrolled = "1"; }, {passive: true}));
$("#chat-jump").addEventListener("click", () => {
  const box = $("#chat");
  box.scrollTop = box.scrollHeight;
  $("#chat-jump").hidden = true;
});

/* Live elapsed time on the still-running last step (and nothing else — a finished step shows
   the fixed duration the backend measured; a step with no timestamps shows nothing). */
setInterval(() => {
  document.querySelectorAll("#chat [data-t0live]").forEach(el => {
    el.textContent = fmtDur(Date.now() / 1000 + clockSkew - +el.dataset.t0live);
  });
}, 1000);

function chatHtml(task, d) {
  dlgBlocks = new Map();
  const live = isLive(d.state);
  let html = "";
  if (d.has_more && !dlgFull) {
    html += `<div class="earlier"><button class="mini" data-load-earlier>${esc(t("chat.load_earlier").replace("{n}", d.offset))}</button>
      <button class="mini ghost" data-show-whole>${esc(t("chat.show_whole").replace("{n}", d.total_steps))}</button></div>`;
  }
  html += d.steps.length
    ? d.steps.map(st => renderStep(task, st, live && st.i === d.steps[d.steps.length - 1].i)).join("")
    : `<div class="empty">${esc(t(live ? "chat.waiting_first" : "chat.empty_thread"))}</div>`;
  if (live && d.steps.length) html += `<div class="typing" aria-live="polite">${spin(t("chat.agent_working"))}</div>`;
  html += renderPending(d.pending);
  if (!live) html += renderOutcome(d.outcome);
  return html;
}

function rebuildChat(opts) {
  const task = SEL ? CURRENT_TASKS.get(SEL) : null;
  if (!task || !dlgData || dlgData.name !== task.name) return;
  const box = $("#chat");
  const near = chatNearBottom(box);
  const prevHeight = box.scrollHeight, prevTop = box.scrollTop;
  box.innerHTML = chatHtml(task, dlgData);
  if (opts && opts.keepPosition) box.scrollTop = prevTop + (box.scrollHeight - prevHeight);
  else if (opts && opts.forceBottom) box.scrollTop = box.scrollHeight;
  else if (near && !(opts && opts.passive)) box.scrollTop = box.scrollHeight;
  else if (opts && opts.passive) box.scrollTop = prevTop;
  else $("#chat-jump").hidden = false;
}

function renderChatHeader(task, d) {
  const parent = task.parent && CURRENT_TASKS.has(task.parent)
    ? `<button class="crumb" data-goto="${esc(task.parent)}" title="${esc(t("chat.parent"))}">↑ ${esc(task.parent)}</button>`
    : task.parent ? `<span class="pill" title="${esc(t("tree.parent_missing").replace("{parent}", task.parent))}">↑ ${esc(task.parent)}</span>` : "";
  const kids = treeModel && treeModel.byName.has(task.name) ? treeModel.byName.get(task.name).counts.total - 1 : 0;
  const stateBadge = `<span class="badge ${stateClass(task.state)}">${isLive(task.state) ? spin() + " " : ""}${esc(stateLabel(task.state, task.idle_sec))}</span>`;
  $("#chead").innerHTML = `<div class="ctitle">${parent}<b title="${esc(task.name)}">${esc(task.title || task.name)}</b>${stateBadge}
      <span class="sp"></span>
      <button class="mini ghost" data-tools="expand">${esc(t("chat.expand_all"))}</button>
      <button class="mini ghost" data-tools="collapse">${esc(t("chat.collapse_all"))}</button>
      <button class="mini ghost${showThinking ? " on" : ""}" id="btn-think" aria-pressed="${showThinking}">💭 ${esc(t("chat.thinking"))}</button></div>
    <div class="cmeta">
      <span class="pill mono" title="${esc(task.name)}">${esc(task.name)}</span>
      <span class="pill pe-${esc(task.engine)}">${esc(task.engine)}/${esc(task.model)}</span>
      ${kids ? `<span class="pill">${esc(t("chat.subagents").replace("{n}", kids))}</span>` : ""}
      <span class="pill">${esc(task.files)} ${esc(t("chat.files"))}</span>
      ${task.queued ? `<span class="pill warn">${esc(t("chat.queued").replace("{n}", task.queued))}</span>` : ""}
      ${task.last_activity && task.last_activity.kind ? `<span class="pill">${esc(t("chat.activity").replace("{kind}", task.last_activity.kind).replace("{age}", task.last_activity.age_sec))}</span>` : ""}
      ${task.state === "error" && task.timeout ? `<span class="pill err">${esc(t("chat.timeout").replace("{s}", task.timeout))}</span>` : ""}
      ${task.reason ? `<span class="pill err" title="${esc(task.reason)}">${esc(task.reason)}</span>` : ""}
      <span class="pill" title="${esc(task.dir)}">${esc(base(task.dir))}</span>
    </div>`;
}

$("#chead").addEventListener("click", event => {
  const el = event.target.closest("button");
  if (!el) return;
  if (el.dataset.tools) return setAllTools(el.dataset.tools === "expand");
  if (el.id === "btn-think") return toggleThinking();
  if (el.dataset.goto) return select(el.dataset.goto);
});

async function loadThread(task, opts) {
  if (dlgTask !== task.name) {
    dlgTask = task.name; dlgFull = false; dlgLimit = DLG_PAGE; dlgData = null;
    $("#chat").dataset.userScrolled = "0";
    $("#chat-jump").hidden = true;
    if (typeof onComposerTask === "function") onComposerTask(task);
  }
  const fullForRequest = dlgFull, limitForRequest = dlgLimit;
  const requestId = ++dialogRequestId;
  if (dialogController) dialogController.abort();
  const controller = new AbortController();
  dialogController = controller;
  let d;
  try {
    d = await jget("/api/dialog?task=" + encodeURIComponent(task.name) +
                   (fullForRequest ? "&full=1" : "&limit=" + limitForRequest),
                   { signal: controller.signal });
  } catch (err) {
    if (err.name === "AbortError") return;
    if (err.status === 404 && task.name === SEL) clearDeletedSelection();
    else surfaceApiError(err);
    return;
  }
  if (requestId !== dialogRequestId || controller.signal.aborted || task.name !== SEL ||
      fullForRequest !== dlgFull || limitForRequest !== dlgLimit) return;
  clockSkew = (d.now || Date.now() / 1000) - Date.now() / 1000;
  dlgHasMore = !!d.has_more;
  renderChatHeader(task, d);
  $("#replybar").hidden = false;
  if (typeof updateComposer === "function") updateComposer(task);
  $("#btn-reply").disabled = false;
  /* Rebuild #chat only when the log actually changed (mtime+size) or the view mode did —
     otherwise the DOM, the scroll position and every expansion stay exactly as they are. */
  const pendingKey = (d.pending || []).map(p => p.sequence + "/" + (p.receipt_state || "") + (p.inflight ? "i" : "")).join(",");
  const key = [d.mtime, d.log_size, d.state, d.offset, d.total_steps, dlgFull, dlgLimit, pendingKey].join(":");
  if (key === lastLog) return;
  const first = !dlgData;
  lastLog = key;
  dlgData = d;
  rebuildChat(first ? {forceBottom: true} : opts);
}

showThinking = (() => { try { return localStorage.getItem("agentgui_show_thinking") === "1"; } catch (_) { return false; } })();
