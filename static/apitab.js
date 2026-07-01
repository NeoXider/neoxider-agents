/* "API" tab: drive an agent to test a local HTTP API and get a structured pass/fail result,
   plus ready-made snippets (curl/C#) for calling this same endpoint from your own programs/
   test suites -- the GUI is just a thin client over the same /api/test-api the snippets use.
   Depends on parseThread() from chat.js -- must load after it (see gui.html's script order). */

// "view log" on an API-test run: switch back to the Tasks tab AND select the task, otherwise
// the thread loads invisibly behind the still-active API tab and looks like nothing happened.
function viewApiRunLog(name, e) {
  e && e.stopPropagation();
  switchTab("tasks");
  select(name, e);
}

function switchTab(tab) {
  document.querySelectorAll(".tabbtn").forEach(b => b.classList.toggle("on", b.dataset.tab === tab));
  document.querySelectorAll(".tabview").forEach(v => v.classList.toggle("on", v.id === "tabview-" + tab));
  localStorage.setItem("agentgui_tab", tab);
  if (tab === "api") refreshApiTab();
}

async function submitApiTest() {
  const body = {
    base_url: $("#api-url").value.trim(),
    goal: $("#api-goal").value.trim(),
    engine: $("#api-engine").value,
    model: $("#api-model").value.trim(),
    effort: $("#api-effort").value,
    dir: $("#api-dir").value.trim(),
  };
  if (!body.base_url || !body.goal) {
    toast("error", t("toast.empty_prompt_title"), "base URL + goal required");
    return;
  }
  const btn = $("#btn-api-run");
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = spin();
  try {
    const r = await jpost("/api/test-api", body);
    if (r.error) {
      toast("error", t("toast.not_started"), r.error);
      return;
    }
    toast("success", t("toast.task_started"), r.name);
    setTimeout(refreshApiTab, 800);
  } finally {
    btn.disabled = false;
    btn.innerHTML = old;
  }
}

function syncApiModels() {
  const e = $("#api-engine").value;
  const p = PROVIDERS[e] || {};
  $("#api-models").innerHTML = (p.models || []).map(m => `<option value="${m}">`).join("");
  $("#api-model").placeholder = p.resolved_label ? t("form.auto") + " → " + p.resolved_label : p.default_model ? t("form.auto") + " (" + p.default_model + ")" : t("form.auto");
  const eff = $("#api-effort");
  const efforts = p.efforts || [];
  eff.innerHTML = `<option value="">${t("form.auto")}</option>` + efforts.map(f => `<option value="${f}">${f}</option>`).join("");
  eff.disabled = !efforts.length;
}

/* Extract the trailing JSON object from a task's FULL log the same way agent.sh's own --out
   extraction does (first "{" .. last "}"), but only within the LAST message's OUTPUT text --
   never the raw whole-log text, which also contains the test-api PROMPT itself (and that
   prompt embeds an example of the JSON schema, so a naive whole-log scan would grab the first
   "{" from the prompt's example and the last "}" from the real output, spanning garbage
   in between). Reuses parseThread() from chat.js to isolate the output. */
function extractJson(log) {
  const msgs = parseThread(log);
  if (!msgs.length) return null;
  const text = msgs[msgs.length - 1].out.join("\n");
  const i = text.indexOf("{"), j = text.lastIndexOf("}");
  if (i === -1 || j === -1 || j <= i) return null;
  try {
    return JSON.parse(text.slice(i, j + 1));
  } catch (e) {
    return null;
  }
}

async function refreshApiTab() {
  const d = await jget("/api/tasks");
  const runs = d.tasks.filter(x => x.kind === "api-test").sort((a, b) => (b.updated || 0) - (a.updated || 0));
  const list = $("#api-results");
  if (!runs.length) {
    list.innerHTML = `<div class="empty">${t("api.no_runs")}</div>`;
    return;
  }
  list.innerHTML = "";
  for (const run of runs) {
    const row = document.createElement("div");
    row.className = "api-run";
    row.innerHTML = `<div class="api-run-hd">
        <span class="em ${run.state === "running" ? "running" : ""}">${run.act || ""}</span>
        <b>${esc(run.name)}</b>
        <span class="pill pe-${run.engine}">${run.engine}</span>
        <span class="sp"></span>
        <button class="mini" onclick="viewApiRunLog('${run.name}',event)">${t("api.view_log")}</button>
      </div>
      <div class="api-run-body">${spin(t("limits.loading"))}</div>`;
    list.appendChild(row);
    (async () => {
      const thread = await jget("/api/thread?task=" + encodeURIComponent(run.name));
      const body = row.querySelector(".api-run-body");
      const parsed = extractJson(thread.log || "");
      if (!parsed) {
        body.innerHTML = `<span class="note">${run.state === "running" ? t("api.still_running") : t("api.no_json")}</span>`;
        return;
      }
      const passClass = parsed.overall === "pass" ? "success" : parsed.overall === "fail" ? "error" : "warning";
      body.innerHTML = `
        <div class="api-summary ${passClass}">${(parsed.overall || "?").toUpperCase()} — ${(parsed.summary || {}).passed || 0}/${(parsed.summary || {}).total || 0}</div>
        ${(parsed.endpoints || [])
          .map(
            ep => `<div class="api-ep ${ep.result === "pass" ? "ok" : "bad"}">
            <span class="mono">${esc(ep.method)} ${esc(ep.path)}</span> — ${esc(ep.result)}
            ${ep.reason ? `<div class="note">${esc(ep.reason)}</div>` : ""}
          </div>`
          )
          .join("")}`;
    })();
  }
}

/* The base URL/goal fields only had placeholder text -- clicking "run" with nothing typed
   just showed a "base URL + goal required" toast, which reads as the button being broken
   rather than as a hint to fill in two fields. Pre-fill the placeholder text as real starter
   values (once, at load, only if the field is still empty) so the button works immediately
   as a runnable example the user can edit for their own server. */
function applyApiFieldDefaults() {
  const urlEl = $("#api-url"), goalEl = $("#api-goal");
  if (urlEl && !urlEl.value.trim() && urlEl.placeholder) urlEl.value = urlEl.placeholder;
  if (goalEl && !goalEl.value.trim() && goalEl.placeholder) goalEl.value = goalEl.placeholder;
}

function apiSnippets() {
  const url = $("#api-url").value.trim() || "http://127.0.0.1:PORT";
  const goal = ($("#api-goal").value.trim() || "...").replace(/"/g, '\\"');
  const guiUrl = location.origin;
  const curl = `curl -X POST ${guiUrl}/api/test-api \\
  -H "Content-Type: application/json" \\
  -d '{"base_url":"${url}","goal":"${goal}","engine":"codex"}'`;
  const csharp = `// From a Unity/C# test, point this GUI's own API at your local server:
using UnityEngine.Networking;
var body = "{\\"base_url\\":\\"${url}\\",\\"goal\\":\\"${goal}\\",\\"engine\\":\\"codex\\"}";
var req = new UnityWebRequest("${guiUrl}/api/test-api", "POST");
req.uploadHandler = new UploadHandlerRaw(System.Text.Encoding.UTF8.GetBytes(body));
req.downloadHandler = new DownloadHandlerBuffer();
req.SetRequestHeader("Content-Type", "application/json");
yield return req.SendWebRequest();
// response: {"ok":true,"name":"<task-name>"} -- poll GET ${guiUrl}/api/thread?task=<task-name>
// for the log, or GET ${guiUrl}/api/tasks and filter kind=="api-test" for status.`;
  $("#api-curl").textContent = curl;
  $("#api-csharp").textContent = csharp;
}
