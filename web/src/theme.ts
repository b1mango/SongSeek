import { useSyncExternalStore } from "react";
import { api } from "./api";
import type { ThemePref } from "./types";

/*
 * Appearance: follow the system, or force light / dark. The saved choice arrives in
 * <html data-theme-pref> (written by the server) and index.html resolves it to
 * <html data-theme="light|dark"> before first paint; the stylesheet keys off data-theme.
 */

const root = document.documentElement;
const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
let pref: ThemePref = (["system", "light", "dark"] as const).find((t) => t === root.dataset.themePref) ?? "system";
const listeners = new Set<() => void>();

function apply() {
  const next = pref === "system" ? (systemDark.matches ? "dark" : "light") : pref;
  if (root.dataset.theme === next) return;
  root.dataset.theme = next;
  window.dispatchEvent(new Event("themechange"));
}

systemDark.addEventListener("change", apply);
apply();

export function setThemePref(next: ThemePref) {
  const before = pref;
  pref = next;
  apply();
  listeners.forEach((l) => l());
  return api.saveSettings({ theme: next }).catch((e) => {
    pref = before;
    apply();
    listeners.forEach((l) => l());
    throw e;
  });
}

export function useThemePref(): ThemePref {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => pref,
  );
}
