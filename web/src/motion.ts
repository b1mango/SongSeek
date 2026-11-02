import { useLayoutEffect, useRef, type RefObject } from "react";

export const EASE_OUT = "cubic-bezier(0.22, 1, 0.36, 1)";
const reduced = () => typeof matchMedia !== "undefined" && matchMedia("(prefers-reduced-motion: reduce)").matches;

/**
 * FLIP list animation. Children marked with `data-flip="<stable key>"` drop into place when
 * they first appear (staggered) and glide to their new slot when the order changes.
 * Changing `resetKey` treats every row as new again (e.g. a fresh search).
 */
export function useFlipList(container: RefObject<HTMLElement | null>, signature: string, resetKey?: unknown) {
  const positions = useRef(new Map<string, number>());
  const lastReset = useRef<unknown>(resetKey);

  useLayoutEffect(() => {
    const root = container.current;
    if (!root) return;
    if (lastReset.current !== resetKey) {
      positions.current = new Map();
      lastReset.current = resetKey;
    }
    const origin = root.getBoundingClientRect().top;
    const next = new Map<string, number>();
    const skip = reduced();
    let fresh = 0;
    root.querySelectorAll<HTMLElement>("[data-flip]").forEach((node) => {
      const key = node.dataset.flip!;
      const top = node.getBoundingClientRect().top - origin;
      next.set(key, top);
      if (skip) return;
      const before = positions.current.get(key);
      if (before === undefined) {
        node.animate(
          [
            { opacity: 0, transform: "translateY(-22px)" },
            { opacity: 1, transform: "translateY(2px)", offset: 0.7 },
            { opacity: 1, transform: "none" },
          ],
          { duration: 520, delay: Math.min(fresh++, 12) * 45, easing: EASE_OUT, fill: "backwards" },
        );
      } else if (Math.abs(before - top) > 1) {
        node.animate([{ transform: `translateY(${before - top}px)` }, { transform: "none" }], { duration: 420, easing: EASE_OUT });
      }
    });
    positions.current = next;
  }, [container, signature, resetKey]);
}
