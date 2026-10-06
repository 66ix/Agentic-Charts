"use client";

import { X } from "lucide-react";
import { useEffect, type ReactNode } from "react";

/** A centred modal for the settings dialogs; Escape and a click outside close it. */
export default function Dialog({ open, title, width = 520, onClose, children }: {
  open: boolean;
  title: string;
  width?: number;
  onClose(): void;
  children: ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", esc, true);
    return () => window.removeEventListener("keydown", esc, true);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-label={title}
        onMouseDown={(e) => e.stopPropagation()}
        style={{ width: `min(${width}px, 100%)` }}
        className="flex max-h-[85vh] flex-col overflow-hidden rounded-lg border border-line bg-panel shadow-2xl"
      >
        <div className="flex shrink-0 items-center justify-between border-b border-line px-4 py-2.5">
          <h2 className="text-sm font-semibold text-ink">{title}</h2>
          <button type="button" className="btn-ghost" aria-label="Close" onClick={onClose}>
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3 text-[12px] text-ink">{children}</div>
      </div>
    </div>
  );
}

/** A labelled row inside a dialog: label on the left, control on the right. */
export function Row({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="flex items-center justify-between gap-4 py-1.5">
      <span className="min-w-0">
        <span className="text-ink">{label}</span>
        {hint && <span className="block text-[11px] text-mute">{hint}</span>}
      </span>
      <span className="flex shrink-0 items-center gap-1.5">{children}</span>
    </label>
  );
}

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="border-b border-line py-2 last:border-0">
      <div className="pb-1 text-[10px] font-semibold uppercase tracking-wide text-mute">{title}</div>
      {children}
    </div>
  );
}

export function NumberInput({ value, onChange, step = 1, min, max, width = 70, suffix }: {
  value: number;
  onChange(v: number): void;
  step?: number;
  min?: number;
  max?: number;
  width?: number;
  suffix?: string;
}) {
  return (
    <>
      <input
        type="number"
        value={value}
        step={step}
        min={min}
        max={max}
        onChange={(e) => {
          const v = Number(e.target.value);
          if (Number.isFinite(v)) onChange(v);
        }}
        style={{ width }}
        className="h-7 rounded border border-line bg-base px-1.5 text-right font-mono text-[12px] text-ink outline-none focus:border-accent"
      />
      {suffix && <span className="text-[11px] text-mute">{suffix}</span>}
    </>
  );
}

export function ColorInput({ value, onChange }: { value: string; onChange(v: string): void }) {
  return (
    <input
      type="color"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label="Colour"
      className="h-6 w-8 cursor-pointer rounded border border-line bg-base"
    />
  );
}

export function Toggle({ checked, onChange }: { checked: boolean; onChange(v: boolean): void }) {
  return <input type="checkbox" className="h-4 w-4 accent-blue-500" checked={checked} onChange={(e) => onChange(e.target.checked)} />;
}
