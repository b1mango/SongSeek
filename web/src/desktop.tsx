import { useSyncExternalStore } from "react";
import { api } from "./api";
import type { SystemInfo } from "./types";

/*
 * What the page is running inside: the desktop app's window (native folder picker,
 * links open in the system browser) or a plain browser tab on `songseek serve`.
 */

let info: SystemInfo | null = null;
const listeners = new Set<() => void>();

export function loadSystem() {
  api
    .system()
    .then((s) => {
      info = s;
      document.documentElement.classList.toggle("is-desktop", s.desktop);
      document.documentElement.dataset.platform = s.platform;
      // macOS window: the page runs under a transparent title bar (traffic lights float over the sidebar).
      document.documentElement.classList.toggle("is-mac-app", s.desktop && s.platform === "darwin");
      listeners.forEach((l) => l());
    })
    .catch(() => {
      /* older server: treat as browser */
    });

  // Inside the app window a target=_blank link would open nowhere: hand it to the system browser.
  document.addEventListener("click", (e) => {
    if (!info?.desktop || e.defaultPrevented) return;
    const a = (e.target as HTMLElement).closest?.("a[href]") as HTMLAnchorElement | null;
    if (!a || !/^https?:/i.test(a.href) || new URL(a.href).origin === location.origin) return;
    e.preventDefault();
    api.openUrl(a.href).catch(() => undefined);
  });
}

export function useSystem(): SystemInfo | null {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => info,
  );
}

/** The strip under a transparent macOS title bar: drags the window, double-click zooms like a title bar. */
export function TitleBar() {
  const system = useSystem();
  if (!system?.desktop || system.platform !== "darwin") return null;
  return <div className="titlebar pywebview-drag-region" aria-hidden onDoubleClick={() => api.titlebarDoubleClick().catch(() => undefined)} />;
}
