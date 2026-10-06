"use client";

import clsx from "clsx";
import { ArrowLeftToLine, ArrowRightToLine, Bell, Copy, Pencil, Trash2 } from "lucide-react";
import { useState } from "react";

import { TIMEFRAMES, type Drawing, type DrawingStyle, type Interval, type LineStyleName } from "@/lib/types";

const COLORS = ["#3b82f6", "#22c55e", "#ef4444", "#f59e0b", "#a855f7", "#14b8a6", "#e5e7eb", "#f472b6"];
const DASHES: { v: LineStyleName; label: string; preview: string }[] = [
  { v: "solid", label: "Solid", preview: "—" },
  { v: "dashed", label: "Dashed", preview: "- -" },
  { v: "dotted", label: "Dotted", preview: "···" },
];

interface Props {
  drawing: Drawing;
  interval: Interval;
  canAlert: boolean;
  onChange(next: Drawing): void;
  onDuplicate(): void;
  onDelete(): void;
  onAlert(): void;
}

/** Floating bar over the chart while a drawing is selected: colour, width, line style, extend, timeframes. */
export default function DrawingStyleBar({ drawing: d, interval, canAlert, onChange, onDuplicate, onDelete, onAlert }: Props) {
  const [tfOpen, setTfOpen] = useState(false);
  const [editText, setEditText] = useState<string | null>(null);
  const style = d.style ?? {};
  const set = (patch: Partial<DrawingStyle>) => onChange({ ...d, style: { ...style, ...patch } });
  const lineLike = d.type !== "text";
  const tfs = style.timeframes ?? [];

  return (
    <div
      className="pointer-events-auto absolute left-1/2 top-2 z-30 flex -translate-x-1/2 items-center gap-1 rounded-lg border border-line bg-panel/95 px-1.5 py-1 text-[11px] shadow-xl backdrop-blur"
      onPointerDown={(e) => e.stopPropagation()}
    >
      {COLORS.map((c) => (
        <button
          key={c}
          type="button"
          aria-label={`Colour ${c}`}
          onClick={() => onChange({ ...d, color: c })}
          className={clsx("h-4 w-4 rounded-full border", d.color === c ? "border-white" : "border-transparent")}
          style={{ background: c }}
        />
      ))}
      <div className="mx-1 h-4 w-px bg-line" />
      {lineLike &&
        [1, 2, 3, 4].map((w) => (
          <button
            key={w}
            type="button"
            title={`${w}px`}
            onClick={() => set({ width: w })}
            className={clsx("grid h-6 w-6 place-items-center rounded", style.width === w ? "bg-panel2" : "hover:bg-panel2")}
          >
            <span className="w-3.5 rounded bg-ink" style={{ height: w }} />
          </button>
        ))}
      {lineLike &&
        DASHES.map((s) => (
          <button
            key={s.v}
            type="button"
            title={s.label}
            onClick={() => set({ dash: s.v })}
            className={clsx("h-6 rounded px-1.5 font-mono", (style.dash ?? "solid") === s.v ? "bg-panel2 text-ink" : "text-mute hover:bg-panel2")}
          >
            {s.preview}
          </button>
        ))}
      {(d.type === "trendline" || d.type === "hray") && (
        <button
          type="button"
          title={d.type === "hray" ? "Full-width line" : "Extend left"}
          onClick={() => set({ extendLeft: !style.extendLeft })}
          className={clsx("btn-ghost h-6 w-6 p-0", style.extendLeft && "bg-panel2 text-ink")}
        >
          <ArrowLeftToLine className="h-3.5 w-3.5" />
        </button>
      )}
      {(d.type === "trendline" || d.type === "rect") && (
        <button
          type="button"
          title="Extend right"
          onClick={() => set({ extendRight: !style.extendRight })}
          className={clsx("btn-ghost h-6 w-6 p-0", style.extendRight && "bg-panel2 text-ink")}
        >
          <ArrowRightToLine className="h-3.5 w-3.5" />
        </button>
      )}
      {d.type === "text" && (
        <>
          {[11, 13, 16, 20].map((fs) => (
            <button
              key={fs}
              type="button"
              onClick={() => set({ fontSize: fs })}
              className={clsx("h-6 rounded px-1.5", (style.fontSize ?? 13) === fs ? "bg-panel2 text-ink" : "text-mute hover:bg-panel2")}
            >
              {fs}
            </button>
          ))}
          {editText === null ? (
            <button type="button" className="btn-ghost h-6 w-6 p-0" title="Edit text" onClick={() => setEditText(d.text ?? "")}>
              <Pencil className="h-3.5 w-3.5" />
            </button>
          ) : (
            <input
              autoFocus
              value={editText}
              onChange={(e) => setEditText(e.target.value)}
              onKeyDown={(e) => {
                e.stopPropagation();
                if (e.key === "Enter") {
                  onChange({ ...d, text: editText.trim() || d.text });
                  setEditText(null);
                }
                if (e.key === "Escape") setEditText(null);
              }}
              onBlur={() => {
                onChange({ ...d, text: editText.trim() || d.text });
                setEditText(null);
              }}
              className="h-6 w-40 rounded border border-accent bg-base px-1.5 text-ink outline-none"
            />
          )}
        </>
      )}
      <div className="mx-1 h-4 w-px bg-line" />
      <div className="relative">
        <button
          type="button"
          onClick={() => setTfOpen((v) => !v)}
          className={clsx("btn-ghost h-6 px-1.5 text-[11px]", tfs.length && "text-accent")}
          title="Show this drawing on some timeframes only"
        >
          {tfs.length ? `${tfs.length} timeframe${tfs.length > 1 ? "s" : ""}` : "All timeframes"}
        </button>
        {tfOpen && (
          <div className="absolute left-0 top-7 z-40 w-44 rounded-md border border-line bg-panel p-1 shadow-xl">
            <button type="button" className="w-full rounded px-2 py-1 text-left hover:bg-panel2" onClick={() => set({ timeframes: [] })}>
              All timeframes
            </button>
            <button type="button" className="w-full rounded px-2 py-1 text-left hover:bg-panel2" onClick={() => set({ timeframes: [interval] })}>
              This timeframe only
            </button>
            <div className="my-1 h-px bg-line" />
            <div className="grid grid-cols-5 gap-0.5">
              {TIMEFRAMES.map((t) => {
                const on = tfs.includes(t.value);
                return (
                  <button
                    key={t.value}
                    type="button"
                    onClick={() => set({ timeframes: on ? tfs.filter((x) => x !== t.value) : [...tfs, t.value] })}
                    className={clsx("rounded px-1 py-0.5", on ? "bg-accent/20 text-accent" : "text-mute hover:bg-panel2")}
                  >
                    {t.label}
                  </button>
                );
              })}
            </div>
          </div>
        )}
      </div>
      {canAlert && (
        <button type="button" className="btn-ghost h-6 w-6 p-0" title="Alert me when price reaches this" onClick={onAlert}>
          <Bell className="h-3.5 w-3.5" />
        </button>
      )}
      <button type="button" className="btn-ghost h-6 w-6 p-0" title="Duplicate" onClick={onDuplicate}>
        <Copy className="h-3.5 w-3.5" />
      </button>
      <button type="button" className="btn-ghost h-6 w-6 p-0 hover:text-down" title="Delete (Del)" onClick={onDelete}>
        <Trash2 className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
