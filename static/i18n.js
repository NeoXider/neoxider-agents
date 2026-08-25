/* i18n: English is the fallback for any key missing from the active locale -- that's what
   makes adding a new language a drop-in-one-file operation (locales/<code>.json can be a
   PARTIAL key set; anything it doesn't cover just falls back to English automatically). */
let LOCALES = [];
let LOCALE_DATA = {};
let FALLBACK_DATA = {};
const DEFAULT_LOCALE = "en";
let localeRequestId = 0;

function t(key, fallback) {
  return LOCALE_DATA[key] ?? FALLBACK_DATA[key] ?? fallback ?? key;
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
