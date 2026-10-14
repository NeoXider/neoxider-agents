/* Theme: "auto" follows prefers-color-scheme; "light"/"dark" pin it. Loaded in <head> so the
   page never flashes the wrong palette. The header button cycles auto -> light -> dark. */
const THEME_KEY = "agentgui_theme";
function storedTheme() {
  try {
    const v = localStorage.getItem(THEME_KEY);
    return v === "light" || v === "dark" ? v : "auto";
  } catch (_) { return "auto"; }
}
function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === "light" || mode === "dark") root.dataset.theme = mode;
  else delete root.dataset.theme;
}
applyTheme(storedTheme());

function initThemeToggle() {
  const btn = document.getElementById("btn-theme");
  if (!btn) return;
  const icons = {auto: "◐", light: "☀", dark: "☾"};
  const paint = () => {
    const mode = storedTheme();
    btn.textContent = icons[mode];
    const label = (typeof t === "function" ? t("theme." + mode) : mode);
    btn.title = label;
    btn.setAttribute("aria-label", label);
  };
  btn.addEventListener("click", () => {
    const next = {auto: "light", light: "dark", dark: "auto"}[storedTheme()];
    try { localStorage.setItem(THEME_KEY, next); } catch (_) { /* private mode: session only */ }
    applyTheme(next);
    paint();
  });
  paint();
}
