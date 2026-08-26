export type ThemeMode = "dark" | "light" | "system";

const KEY = "vm-theme";
const mql = window.matchMedia("(prefers-color-scheme: dark)");

function resolve(mode: ThemeMode): "dark" | "light" {
  if (mode === "system") return mql.matches ? "dark" : "light";
  return mode;
}

let listener: ((e: MediaQueryListEvent) => void) | null = null;

export function applyTheme(mode: ThemeMode) {
  const root = document.documentElement;
  root.dataset.theme = resolve(mode);
  if (listener) {
    mql.removeEventListener("change", listener);
    listener = null;
  }
  if (mode === "system") {
    listener = () => {
      root.dataset.theme = resolve("system");
    };
    mql.addEventListener("change", listener);
  }
}

export function getStoredTheme(): ThemeMode {
  const v = localStorage.getItem(KEY);
  return v === "dark" || v === "light" || v === "system" ? v : "system";
}

export function setStoredTheme(mode: ThemeMode) {
  localStorage.setItem(KEY, mode);
  applyTheme(mode);
}
