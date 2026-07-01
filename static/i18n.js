/* i18n: English is the fallback for any key missing from the active locale -- that's what
   makes adding a new language a drop-in-one-file operation (locales/<code>.json can be a
   PARTIAL key set; anything it doesn't cover just falls back to English automatically). */
let LOCALES = [];
let LOCALE_DATA = {};
let FALLBACK_DATA = {};
const DEFAULT_LOCALE = "en";

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
  try {
    LOCALE_DATA = await (await fetch("/locales/" + code + ".json")).json();
  } catch (e) {
    LOCALE_DATA = {};
  }
  document.documentElement.lang = code;
  applyI18n();
  if (typeof onLocaleChanged === "function") onLocaleChanged();
}

async function initI18n() {
  try {
    FALLBACK_DATA = await (await fetch("/locales/" + DEFAULT_LOCALE + ".json")).json();
  } catch (e) {
    FALLBACK_DATA = {};
  }
  try {
    LOCALES = (await (await fetch("/api/locales")).json()).locales || [];
  } catch (e) {
    LOCALES = [{ code: "en", label: "English" }];
  }
  const picker = document.querySelector("#lang-picker");
  if (picker) {
    picker.innerHTML = LOCALES.map(l => `<option value="${l.code}">${l.label}</option>`).join("");
    const saved = localStorage.getItem("agentgui_lang") ||
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
