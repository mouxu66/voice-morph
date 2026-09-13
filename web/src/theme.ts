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

const SIMPLE_KEY = "vm-simple-mode";

/** 极简模式：默认开启（未设置即视为开启），用户可在设置里关闭，选择跨重启保留。 */
export function getStoredSimpleMode(): boolean {
  return localStorage.getItem(SIMPLE_KEY) !== "0";
}

export function setStoredSimpleMode(v: boolean) {
  localStorage.setItem(SIMPLE_KEY, v ? "1" : "0");
}
