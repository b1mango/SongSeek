import { useEffect, useRef } from "react";
import { useTrackProgress } from "./store";
import type { TrackProgress } from "./types";

/**
 * Download progress for one track: a liquid fill with a moving wave surface, sparks rising off it,
 * and data motes drifting in from the empty side toward the leading edge.
 * Without a known size (or before the first byte) the whole tube ripples at low strength.
 */
export function TrackWave({ trackId }: { trackId: number }) {
  const p = useTrackProgress(trackId);
  return (
    <span className="wave-progress" title={waveTitle(p)}>
      <WaveCanvas fraction={p?.phase === "finishing" ? 1 : (p?.fraction ?? null)} finishing={p?.phase === "finishing"} />
      <span className="wave-progress-text mono">{waveLabel(p)}</span>
    </span>
  );
}

function waveLabel(p: TrackProgress | null) {
  if (!p) return "准备中";
  if (p.phase === "finishing") return "处理中";
  if (p.fraction != null) return `${Math.floor(p.fraction * 100)}%`;
  return fmtBytes(p.downloaded);
}

function waveTitle(p: TrackProgress | null) {
  if (!p) return "正在获取音频地址";
  if (p.phase === "finishing") return "已下载，正在校验并写入标签";
  return p.total ? `${fmtBytes(p.downloaded)} / ${fmtBytes(p.total)}` : `已下载 ${fmtBytes(p.downloaded)}`;
}

function fmtBytes(n: number) {
  return n >= 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
}

interface Mote {
  x: number;
  y: number;
  vx: number;
  vy: number;
  life: number;
  max: number;
  r: number;
  inflow: boolean;
}

const reduceMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function WaveCanvas({ fraction, finishing }: { fraction: number | null; finishing: boolean }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const target = useRef({ fraction, finishing });
  target.current = { fraction, finishing };

  useEffect(() => {
    const canvas = ref.current!;
    const ctx = canvas.getContext("2d")!;
    const still = reduceMotion();
    let w = 0;
    let h = 0;
    let colors = { ok: "#2f7a4b", rule: "#e6dfd1", ink: "#8a8174" };
    const readColors = () => {
      const css = getComputedStyle(canvas);
      colors = { ok: css.getPropertyValue("--ok").trim(), rule: css.getPropertyValue("--rule").trim(), ink: css.getPropertyValue("--ink-3").trim() };
    };
    const resize = () => {
      const dpr = window.devicePixelRatio || 1;
      w = canvas.clientWidth;
      h = canvas.clientHeight;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      readColors();
    };
    resize();

    let shown = target.current.fraction ?? 0;
    let t = 0;
    let last = performance.now();
    let frame = 0;
    let visible = true;
    const motes: Mote[] = [];

    const surface = (x: number, amp: number, phase: number) =>
      h * 0.3 + amp * Math.sin(x * 0.11 - t * 3.4 + phase) + amp * 0.45 * Math.sin(x * 0.23 + t * 2.2 + phase * 2);

    const draw = () => {
      const { fraction: goal, finishing: done } = target.current;
      const unknown = goal == null;
      const fillW = unknown ? w : Math.max(h * 0.6, shown * w);
      const strength = unknown ? 0.35 : 1;
      const amp = h * (done ? 0.1 : 0.14);

      ctx.clearRect(0, 0, w, h);
      ctx.save();
      ctx.beginPath();
      ctx.roundRect(0, 0, w, h, h / 2);
      ctx.clip();

      ctx.globalAlpha = 0.55;
      ctx.fillStyle = colors.rule;
      ctx.fillRect(0, 0, w, h);

      // back wave: lighter, slightly ahead of the front
      ctx.globalAlpha = 0.3 * strength;
      ctx.fillStyle = colors.ok;
      fillWave(ctx, Math.min(w, fillW + 4), h, (x) => surface(x, amp * 1.1, 1.7));

      // front wave
      ctx.globalAlpha = unknown ? 0.45 : 0.9;
      fillWave(ctx, fillW, h, (x) => surface(x, amp, 0));

      // glow at the leading edge
      if (!unknown && fillW < w - 1) {
        const glow = ctx.createRadialGradient(fillW, h / 2, 0, fillW, h / 2, h);
        glow.addColorStop(0, colors.ok);
        glow.addColorStop(1, "transparent");
        ctx.globalAlpha = 0.45;
        ctx.fillStyle = glow;
        ctx.fillRect(fillW - h, 0, h * 2, h);
      }

      for (const m of motes) {
        const fade = Math.sin((m.life / m.max) * Math.PI);
        const inside = m.x < fillW && m.y > surface(m.x, amp, 0);
        ctx.globalAlpha = fade * (m.inflow ? 0.55 : 0.95);
        ctx.fillStyle = m.inflow ? colors.ink : inside ? "#ffffff" : colors.ok;
        ctx.beginPath();
        ctx.arc(m.x, m.y, m.r, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();
      return fillW;
    };

    const step = (now: number) => {
      frame = requestAnimationFrame(step);
      if (!visible) {
        last = now;
        return;
      }
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      t += dt;
      const goal = target.current.fraction;
      if (goal != null) shown += (goal - shown) * (1 - Math.exp(-dt * 5));
      const fillW = draw();
      const unknown = goal == null;

      // sparks: born under the surface near the front, drift forward and rise out
      const sparkRate = (unknown ? 0.25 : target.current.finishing ? 0.9 : 0.6) * dt * 60;
      if (Math.random() < sparkRate) {
        const x = unknown ? Math.random() * w : fillW - Math.random() * Math.min(fillW, h * 3);
        motes.push({ x, y: h * (0.45 + Math.random() * 0.45), vx: 8 + Math.random() * 18, vy: -(4 + Math.random() * 10), life: 0, max: 0.6 + Math.random() * 0.9, r: 0.6 + Math.random() * 0.9, inflow: false });
      }
      // inflow: motes in the empty part pulled toward the leading edge
      if (!unknown && fillW < w - h && Math.random() < 0.35 * dt * 60) {
        motes.push({ x: fillW + h + Math.random() * (w - fillW - h), y: h * (0.25 + Math.random() * 0.5), vx: -(20 + Math.random() * 30), vy: 0, life: 0, max: 1.2, r: 0.5 + Math.random() * 0.6, inflow: true });
      }
      for (let i = motes.length - 1; i >= 0; i--) {
        const m = motes[i];
        m.life += dt;
        if (m.inflow) {
          m.vx *= 1 + dt * 1.5;
          m.y += (h / 2 - m.y) * dt * 2;
        }
        m.x += m.vx * dt;
        m.y += m.vy * dt;
        if (m.life >= m.max || (m.inflow && m.x <= fillW) || m.x > w + 2) motes.splice(i, 1);
      }
    };

    const ro = new ResizeObserver(resize);
    ro.observe(canvas);
    const io = new IntersectionObserver(([e]) => (visible = e.isIntersecting));
    io.observe(canvas);
    const scheme = window.matchMedia("(prefers-color-scheme: dark)");
    scheme.addEventListener("change", readColors);

    // reduced motion: no loop; repaint the settled bar whenever progress changes
    const redraw = () => {
      shown = target.current.fraction ?? 0;
      draw();
    };
    canvas.addEventListener("redraw", redraw);
    if (still) redraw();
    else frame = requestAnimationFrame(step);
    return () => {
      canvas.removeEventListener("redraw", redraw);
      cancelAnimationFrame(frame);
      ro.disconnect();
      io.disconnect();
      scheme.removeEventListener("change", readColors);
    };
  }, []);

  useEffect(() => {
    if (reduceMotion()) ref.current?.dispatchEvent(new Event("redraw"));
  }, [fraction, finishing]);

  return <canvas ref={ref} className="wave-canvas" aria-hidden />;
}

function fillWave(ctx: CanvasRenderingContext2D, width: number, h: number, y: (x: number) => number) {
  ctx.beginPath();
  ctx.moveTo(0, h);
  for (let x = 0; x <= width; x += 2) ctx.lineTo(x, y(x));
  ctx.lineTo(width, y(width));
  ctx.lineTo(width, h);
  ctx.closePath();
  ctx.fill();
}

