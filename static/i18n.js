/* i18n: English is the fallback for any key missing from the active locale -- that's what
   makes adding a new language a drop-in-one-file operation (locales/<code>.json can be a
   PARTIAL key set; anything it doesn't cover just falls back to English automatically). */
let LOCALES = [];
let LOCALE_DATA = {};
let FALLBACK_DATA = {};
const DEFAULT_LOCALE = "en";
let localeRequestId = 0;
// Fleet strings remain English fallbacks until a locale supplies the same keys.
const FLEET_I18N = {
  "tabs.fleet": "Fleet", "fleet.parent": "Parent", "fleet.group": "Project / group",
  "fleet.refresh": "Refresh fleet", "fleet.all": "All", "fleet.active": "Active",
  "fleet.done": "Done", "fleet.attention": "Attention", "fleet.error": "Error",
  "fleet.page": "Summary of returned tasks", "fleet.partial": "Partial page / comparisons",
  "fleet.stale": "Refresh failed; controls disabled", "fleet.select": "Select a task",
  "fleet.requested": "Requested", "fleet.resolved": "Resolved", "fleet.unknown": "unknown",
  "fleet.cost": "Cost", "fleet.delta": "Cached delta", "fleet.pending": "pending",
  "fleet.owns": "Declared scope", "fleet.overlap": "Possible declaration overlap",
  "fleet.conflict": "Shared cached delta", "fleet.empty": "No tasks in this view",
  "fleet.stop": "Stop", "fleet.resume": "Resume", "fleet.send": "Send message",
  "fleet.message": "Message", "fleet.receipts": "Persisted message receipts",
  "fleet.accepted": "Accepted", "fleet.delivered": "Delivered",
  "fleet.acknowledged": "Recorded acknowledgment evidence",
  "fleet.receipt_unknown": "Receipt counts unknown until persisted evidence is available",
  "fleet.stopped": "Stop command completed; refreshing task state",
  "fleet.launcher_pending": "Launcher started; durable acceptance/delivery pending.",
  "fleet.run_pending": "Task launch started; discover and poll task state in Tasks.",
  "fleet.submission_unknown": "Submission outcome unknown; poll this UUID. Do not resend automatically.",
  "fleet.submission_started": "Submission started",
  "fleet.events": "Durable events", "fleet.replay": "Durable replay; cursor saved after rendering",
  "fleet.torn": "Incomplete event tail; complete events replayed, tail not acknowledged",
  "fleet.retry": "Replay/ack failed; durable cursor preserved for retry"
};

function t(key, fallback) {
  return LOCALE_DATA[key] ?? FALLBACK_DATA[key] ?? FLEET_I18N[key] ?? fallback ?? key;
}

function applyI18n(root) {
  (root || document).querySelectorAll("[data-i18n]").forEach(el => {
    el.textContent = t(el.getAttribute("data-i18n"));
  });
  (root || document).querySelectorAll("[data-i18n-placeholder]").forEach(el => {
    el.placeholder = t(el.getAttribute("data-i18n-placeholder"));
  });
  (root || document).querySelectorAll("[data-i18n-title]").forEach(el => {
    el.title = t(el.getAttribute("data-i18n-title"));
  });
}

async function loadLocale(code) {
  const requestId = ++localeRequestId;
  let loaded = {};
  try {
    loaded = await jget("/locales/" + encodeURIComponent(code) + ".json");
  } catch (e) {
    loaded = {};
  }
  if (requestId !== localeRequestId) return;
  LOCALE_DATA = loaded;
  document.documentElement.lang = code;
  applyI18n();
  if (typeof onLocaleChanged === "function") await onLocaleChanged();
}

async function initI18n() {
  try {
    FALLBACK_DATA = await jget("/locales/" + DEFAULT_LOCALE + ".json");
  } catch (e) {
    FALLBACK_DATA = {};
  }
  try {
    LOCALES = (await jget("/api/locales")).locales || [];
  } catch (e) {
    LOCALES = [{ code: "en", label: "English" }];
  }
  const picker = document.querySelector("#lang-picker");
  if (picker) {
    picker.replaceChildren(...LOCALES.map(locale => {
      const option = document.createElement("option");
      option.value = String(locale.code || "");
      option.textContent = String(locale.label || locale.code || "");
      return option;
    }));
    const stored = localStorage.getItem("agentgui_lang");
    const saved = (stored && LOCALES.some(l => l.code === stored) ? stored : "") ||
      (LOCALES.some(l => l.code === DEFAULT_LOCALE) ? DEFAULT_LOCALE : (LOCALES[0] || {}).code);
    picker.value = saved || DEFAULT_LOCALE;
    picker.addEventListener("change", () => {
      localStorage.setItem("agentgui_lang", picker.value);
      loadLocale(picker.value);
    });
    await loadLocale(picker.value);
  } else {
    await loadLocale(localStorage.getItem("agentgui_lang") || DEFAULT_LOCALE);
  }
}
