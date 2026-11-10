import { useEffect, useRef } from "react";
import { useTrackProgress } from "./store";
import type { TrackProgress } from "./types";

/**
 * Download progress painted as the row's own background: a dot matrix that dithers in from the
 * left, denser and brighter toward the leading edge, shimmering as it goes (the "ultracode" look).
 * Mounted inside the row's first cell; it sizes itself to the whole <tr> and sits behind the cells.
 */
export function RowFlow({ trackId }: { trackId: number }) {
  const p = useTrackProgress(trackId);
  const ref = useRef<HTMLCanvasElement>(null);
  const goal = useRef(p);
  goal.current = p;

  useEffect(() => {
    const canvas = ref.current!;
    const row = canvas.closest("tr")!;
    const ctx = canvas.getContext("2d")!;
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let w = 0;
    let h = 0;
    let dot = "#7b4fd8";
    let wash = "#ebe2fb";
    let clear = "rgba(235, 226, 251, 0)";

    const readColors = () => {
      const css = getComputedStyle(canvas);
      dot = css.getPropertyValue("--flow").trim() || dot;
      wash = css.getPropertyValue("--flow-soft").trim() || wash;
      ctx.fillStyle = wash;
      const hex = String(ctx.fillStyle); // canvas normalises any CSS colour to #rrggbb
      clear = `rgba(${parseInt(hex.slice(1, 3), 16)}, ${parseInt(hex.slice(3, 5), 16)}, ${parseInt(hex.slice(5, 7), 16)}, 0)`;
    };
    const resize = () => {
      const dpr = window.devicePixelRatio || 1;
      w = row.offsetWidth;
      h = row.offsetHeight;
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      readColors();
      if (still) paint();
    };

    let shown = target(goal.current) ?? 0;
    let t = 0;

    const paint = () => {
      const p = goal.current;
      const f = target(p);
      const finishing = p?.phase === "finishing";
      ctx.clearRect(0, 0, w, h);
      if (!w || !h) return;

      // Where the "fill" ends. Unknown size: a comet that sweeps the row; not started: a faint idle field.
      const sweep = f == null && p != null;
      const edge = f != null ? shown * w : sweep ? ((t * 0.45) % 1.25) * w : 0;
      const start = sweep ? Math.max(0, edge - w * 0.45) : 0;

      if (edge > start) {
        const g = ctx.createLinearGradient(start, 0, edge, 0);
        g.addColorStop(0, clear); // same hue at alpha 0, so the ramp never passes through grey
        g.addColorStop(1, wash);
        ctx.globalAlpha = 0.9;
        ctx.fillStyle = g;
        ctx.fillRect(start, 0, edge - start, h);
      }

      ctx.fillStyle = dot;
      const rate = finishing ? 16 : 9;
      const cols = Math.ceil(w / PITCH);
      const rows = Math.ceil(h / PITCH);
      const oy = (h - (rows - 1) * PITCH - SIZE) / 2;
      for (let i = 0; i < cols; i++) {
        const x = i * PITCH;
        let density: number;
        let bright: number;
        if (x <= edge && x >= start) {
          const u = (x - start) / Math.max(1, edge - start); // 0 at the tail, 1 at the leading edge
          const ripple = 0.12 * Math.sin(x * 0.045 - t * 4.2);
          density = 0.08 + 0.7 * u ** 2.2 + ripple * u;
          bright = 0.3 + 0.6 * u;
          const near = edge - x;
          if (near < 10) {
            density = Math.max(density, 0.95 - near * 0.03);
            bright = 1;
          }
        } else {
          // outside the fill: sparse, dim motes, a few spilling forward from the front
          const ahead = x - edge;
          density = p == null ? 0.04 : ahead < 0 ? 0.015 : 0.015 + 0.25 * Math.exp(-ahead / 14); // behind a sweeping comet: calm
          bright = 0.35;
        }
        if (finishing) {
          density = Math.max(density, 0.35 + 0.25 * Math.sin(x * 0.03 - t * 6));
          bright = Math.max(bright, 0.6);
        }
        if (density <= 0) continue;
        for (let j = 0; j < rows; j++) {
          const seed = hash(i, j);
          // each dot re-rolls on its own beat, so the field dithers instead of blinking in sync
          const beat = Math.floor(t * rate * (0.6 + seed) + seed * 7);
          const roll = hash(i * 7 + beat * 31, j * 13 + beat);
          if (roll > density) continue;
          // fade toward the top and bottom of the row so text sits on a calmer band
          const vy = Math.abs(j / Math.max(1, rows - 1) - 0.5) * 2;
          ctx.globalAlpha = bright * (0.35 + 0.65 * seed) * (1 - 0.35 * vy) * ALPHA;
          ctx.fillRect(x, oy + j * PITCH, SIZE, SIZE);
        }
      }
      ctx.globalAlpha = 1;
    };

    let frame = 0;
    let last = performance.now();
    let acc = 0;
    let visible = true;
    const step = (now: number) => {
      frame = requestAnimationFrame(step);
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      if (!visible) return;
      t += dt;
      const f = target(goal.current);
      if (f != null) shown += (f - shown) * (1 - Math.exp(-dt * 4));
      // the dither reads better (and costs less) at ~30 fps. Keep the remainder (minus a little
      // slack for vsync jitter) so a 60 Hz screen paints exactly every other frame; resetting to 0
      // dropped the remainder and fell into 3-frame gaps (~24 fps).
      const interval = 1 / 30;
      acc += dt;
      if (acc < interval - 0.002) return;
      acc = Math.min(Math.max(0, acc - interval), interval);
      paint();
    };

    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(row);
    const io = new IntersectionObserver(([e]) => (visible = e.isIntersecting));
    io.observe(row);
    const recolor = () => {
      readColors();
      if (still) paint();
    };
    window.addEventListener("themechange", recolor);
    const redraw = () => {
      shown = target(goal.current) ?? 0;
      paint();
    };
    canvas.addEventListener("redraw", redraw);
    if (!still) frame = requestAnimationFrame(step);
    return () => {
      cancelAnimationFrame(frame);
      ro.disconnect();
      io.disconnect();
      window.removeEventListener("themechange", recolor);
      canvas.removeEventListener("redraw", redraw);
    };
  }, []);

  // reduced motion: no loop, repaint the settled state whenever progress changes
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) ref.current?.dispatchEvent(new Event("redraw"));
  }, [p]);

  return <canvas ref={ref} className="row-flow" aria-hidden />;
}

/** Small text next to the 下载中 badge: percent, bytes, or phase. */
export function FlowLabel({ trackId }: { trackId: number }) {
  const p = useTrackProgress(trackId);
  return (
    <span className="flow-label mono" title={flowTitle(p)}>
      {flowLabel(p)}
    </span>
  );
}

const PITCH = 4;
const SIZE = 2;
const ALPHA = 0.8;

function target(p: TrackProgress | null) {
  if (!p) return null;
  return p.phase === "finishing" ? 1 : p.fraction;
}

function hash(a: number, b: number) {
  let n = (a * 374761393 + b * 668265263) | 0;
  n = Math.imul(n ^ (n >>> 13), 1274126177);
  return ((n ^ (n >>> 16)) >>> 0) / 4294967296;
}

function flowLabel(p: TrackProgress | null) {
  if (!p) return "准备中";
  if (p.phase === "finishing") return "处理中";
  if (p.fraction != null) return `${Math.floor(p.fraction * 100)}%`;
  return fmtBytes(p.downloaded);
}

function flowTitle(p: TrackProgress | null) {
  if (!p) return "正在获取音频地址";
  if (p.phase === "finishing") return "已下载，正在校验并写入标签";
  return p.total ? `${fmtBytes(p.downloaded)} / ${fmtBytes(p.total)}` : `已下载 ${fmtBytes(p.downloaded)}`;
}

function fmtBytes(n: number) {
  return n >= 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
}
