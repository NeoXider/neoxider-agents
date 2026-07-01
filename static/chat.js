/* Chat thread rendering: compact dependency-free markdown + log-to-messages parsing.
   Uses $/esc/spin/base from util.js, t() from i18n.js. */

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

function parseThread(log) {
  const msgs = [];
  let cur = null, mode = null;
  for (let line of (log || "").split("\n")) {
    let hd = line.match(/^=+\s*\[(\w+)\]\s*(.*?)\s*\|/);
    if (hd) {
      cur = { kind: hd[1], time: hd[2], inp: [], out: [] };
      msgs.push(cur);
      mode = null;
      continue;
    }
    if (!cur) continue;
    if (line === "> PROMPT:" || line === "> ANSWER:") { mode = "in"; continue; }
    if (/^-+\s*output\s*-+$/.test(line)) { mode = "out"; continue; }
    if (mode === "in") cur.inp.push(line);
    else if (mode === "out") cur.out.push(line);
  }
  return msgs;
}

async function loadThread(task) {
  const d = await jget("/api/thread?task=" + encodeURIComponent(task.name));
  $("#chead").innerHTML = `<b>${esc(task.name)}</b>
    <span class="pill pe-${task.engine}">${task.engine}/${esc(task.model)}</span>
    <span class="pill">${task.files} ${t("chat.files")}</span>
    ${task.state === "waiting" ? `<span class="pill" style="color:var(--wait);border-color:var(--wait)">${t("chat.waiting")}</span>` : ""}
    ${task.state === "stalled" ? `<span class="pill" style="color:var(--stall)">${t("chat.stalled")}</span>` : ""}
    ${task.state === "running" ? '<span class="pill" style="color:var(--run);border-color:var(--run)">' + spin(t("chat.running")) + "</span>" : ""}
    <span class="sp"></span><span class="pill" title="dir">${esc(base(task.dir))}</span>`;
  $("#replybar").style.display = "flex";
  if (d.log === lastLog) return; // don't touch the DOM/scroll if nothing changed
  lastLog = d.log;
  const box = $("#chat");
  const near = box.scrollTop + box.clientHeight > box.scrollHeight - 60;
  const msgs = parseThread(d.log);
  box.innerHTML = msgs.length
    ? msgs
        .map(
          m => `
    <div class="msg user"><div class="who">${m.kind} · ${esc(m.time)}</div>
      <div class="bubble">${md(m.inp.join("\n").trim())}</div></div>
    ${
      m.out.join("").trim()
        ? `<div class="msg agent"><div class="who">${task.engine}</div>
      <div class="bubble">${md(m.out.join("\n").trim())}</div></div>`
        : ""
    }
  `
        )
        .join("")
    : `<div class="empty">${t("chat.empty_thread")}</div>`;
  if (near) box.scrollTop = box.scrollHeight;
}
