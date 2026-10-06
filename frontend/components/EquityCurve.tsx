"use client";

import { useMemo, useState } from "react";

const W = 300;

function day(t: number): string {
  return new Date(t * 1000).toLocaleDateString([], { month: "short", day: "numeric", year: "2-digit" });
}

/**
 * Cumulative R after each closed trade, starting from 0, as a small inline SVG with a hover readout. Used by the
 * journal's Stats tab and the backtest results. One series, so no legend: the caption names it.
 */
export default function EquityCurve({ points, height = 96 }: { points: { time: number; r_cum: number }[]; height?: number }) {
  const [hover, setHover] = useState<number | null>(null);
  const geo = useMemo(() => {
    const ys = [0, ...points.map((p) => p.r_cum)];
    const lo = Math.min(0, ...ys);
    const hi = Math.max(0, ...ys);
    const pad = (hi - lo || 1) * 0.08;
    const y0 = lo - pad;
    const y1 = hi + pad;
    const n = ys.length - 1 || 1;
    const x = (i: number) => (i / n) * W;
    const y = (v: number) => height - ((v - y0) / (y1 - y0)) * height;
    return { ys, x, y, path: ys.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ") };
  }, [points, height]);

  if (!points.length) {
    return <div className="flex h-16 items-center justify-center text-[11px] text-mute">No closed trades yet</div>;
  }
  const last = points[points.length - 1].r_cum;
  const color = last >= 0 ? "#22c55e" : "#ef4444";
  const hi = hover ?? geo.ys.length - 1;
  const readout =
    hi === 0
      ? "Start: 0R"
      : `${points[hi - 1].r_cum >= 0 ? "+" : ""}${points[hi - 1].r_cum.toFixed(2)}R after ${hi} trade${hi === 1 ? "" : "s"} · ${day(points[hi - 1].time)}`;

  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-[11px]">
        <span className="text-mute">Cumulative R</span>
        <span className="font-mono text-ink">{readout}</span>
      </div>
      <svg
        viewBox={`0 0 ${W} ${height}`}
        preserveAspectRatio="none"
        className="block w-full cursor-crosshair"
        style={{ height }}
        role="img"
        aria-label={`Cumulative R over ${points.length} trades, ending at ${last.toFixed(2)}R`}
        onMouseMove={(ev) => {
          const r = ev.currentTarget.getBoundingClientRect();
          const f = Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
          setHover(Math.round(f * (geo.ys.length - 1)));
        }}
        onMouseLeave={() => setHover(null)}
      >
        <line x1={0} x2={W} y1={geo.y(0)} y2={geo.y(0)} stroke="#1f2633" strokeWidth={1} vectorEffect="non-scaling-stroke" />
        <path d={geo.path} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
        {hover !== null && (
          <line x1={geo.x(hover)} x2={geo.x(hover)} y1={0} y2={height} stroke="#6b7280" strokeWidth={1} strokeDasharray="2 2" vectorEffect="non-scaling-stroke" />
        )}
      </svg>
    </div>
  );
}
