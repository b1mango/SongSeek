import { useEffect, useState } from "react";

export type Route =
  | { page: "home" }
  | { page: "import" }
  | { page: "search" }
  | { page: "settings" }
  | { page: "playlist"; id: number }
  | { page: "review"; id: number; track?: number };

function parse(hash: string): Route {
  const [path, query = ""] = hash.replace(/^#/, "").split("?");
  const parts = path.split("/").filter(Boolean);
  const params = new URLSearchParams(query);
  if (parts[0] === "import") return { page: "import" };
  if (parts[0] === "search") return { page: "search" };
  if (parts[0] === "settings") return { page: "settings" };
  if (parts[0] === "playlist" && parts[1]) {
    const id = Number(parts[1]);
    if (parts[2] === "review") {
      const t = params.get("track");
      return { page: "review", id, track: t ? Number(t) : undefined };
    }
    return { page: "playlist", id };
  }
  return { page: "home" };
}

export function href(r: Route): string {
  switch (r.page) {
    case "home":
      return "#/";
    case "playlist":
      return `#/playlist/${r.id}`;
    case "review":
      return `#/playlist/${r.id}/review${r.track ? `?track=${r.track}` : ""}`;
    default:
      return `#/${r.page}`;
  }
}

export function navigate(r: Route, replace = false) {
  const h = href(r);
  if (replace) history.replaceState(null, "", h);
  else location.hash = h;
  if (replace) window.dispatchEvent(new HashChangeEvent("hashchange"));
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parse(location.hash));
  useEffect(() => {
    const on = () => setRoute(parse(location.hash));
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}
