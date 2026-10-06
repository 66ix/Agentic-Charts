"use client";

import clsx from "clsx";
import type { LucideIcon } from "lucide-react";
import { X } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

export interface DockTab<T extends string = string> {
  id: T;
  label: string;
  icon: LucideIcon;
  /** Small count on the rail icon (armed alerts, open trades). */
  badge?: number;
  render(): ReactNode;
}

interface Props<T extends string> {
  tabs: DockTab<T>[];
  open: boolean;
  tab: T;
  width: number;
  mobile: boolean;
  onSelect(tab: T): void;
  onClose(): void;
  onWidth(width: number): void;
}

const MIN_W = 280;
const MAX_W = 720;

/**
 * The right-hand dock: an icon rail that is always visible, and one panel next to it that can be resized by
 * dragging its left edge. Clicking the open tab's icon closes the panel. A tab is mounted the first time it is
 * opened and then kept mounted (hidden) so its live data and form state survive switching tabs.
 * On phones the panel covers the chart and the rail is replaced by the bottom tab bar.
 */
export default function Dock<T extends string>(p: Props<T>) {
  const [mounted, setMounted] = useState<Set<T>>(() => new Set(p.open ? [p.tab] : []));
  const drag = useRef<{ x: number; w: number } | null>(null);
  const onWidth = useRef(p.onWidth);
  onWidth.current = p.onWidth;

  useEffect(() => {
    if (p.open) setMounted((m) => (m.has(p.tab) ? m : new Set(m).add(p.tab)));
  }, [p.open, p.tab]);

  useEffect(() => {
    const move = (e: PointerEvent) => {
      if (!drag.current) return;
      onWidth.current(Math.max(MIN_W, Math.min(MAX_W, drag.current.w + drag.current.x - e.clientX)));
    };
    const up = () => {
      if (!drag.current) return;
      drag.current = null;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
  }, []);

  const active = p.tabs.find((t) => t.id === p.tab);
  const panel = (
    <section
      aria-label={active?.label}
      className={clsx(
        "relative flex min-h-0 flex-col bg-panel",
        p.mobile ? "absolute inset-0 z-40" : "shrink-0 border-l border-line",
        !p.open && "hidden",
      )}
      style={p.mobile ? undefined : { width: p.width }}
    >
      {!p.mobile && (
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize panel"
          title="Drag to resize"
          className="absolute -left-1 top-0 z-10 h-full w-2 cursor-col-resize hover:bg-accent/30"
          onPointerDown={(e) => {
            drag.current = { x: e.clientX, w: p.width };
            document.body.style.cursor = "col-resize";
            document.body.style.userSelect = "none";
          }}
        />
      )}
      <div className="flex h-9 shrink-0 items-center gap-2 border-b border-line px-3 text-xs">
        {active && <active.icon className="h-4 w-4 text-accent" />}
        <span className="font-semibold text-ink">{active?.label}</span>
        <div className="flex-1" />
        <button type="button" onClick={p.onClose} className="btn-ghost h-6 w-6 p-0" aria-label="Close panel" title="Close panel (D)">
          <X className="h-4 w-4" />
        </button>
      </div>
      {p.tabs.map((t) =>
        mounted.has(t.id) ? (
          <div key={t.id} className={clsx("min-h-0 flex-1", t.id !== p.tab && "hidden")}>
            {t.render()}
          </div>
        ) : null,
      )}
    </section>
  );

  if (p.mobile) return panel;
  return (
    <>
      {panel}
      <nav aria-label="Panels" className="flex w-11 shrink-0 flex-col items-center gap-0.5 border-l border-line bg-panel py-2">
        {p.tabs.map((t) => {
          const on = p.open && p.tab === t.id;
          return (
            <button
              key={t.id}
              type="button"
              title={t.label}
              aria-label={t.label}
              aria-pressed={on}
              onClick={() => (on ? p.onClose() : p.onSelect(t.id))}
              className={clsx(
                "relative grid h-9 w-9 place-items-center rounded-md transition-colors",
                on ? "bg-accent/15 text-accent" : "text-mute hover:bg-panel2 hover:text-ink",
              )}
            >
              <t.icon className="h-[18px] w-[18px]" />
              {!!t.badge && (
                <span className="absolute right-0.5 top-0.5 grid h-3.5 min-w-3.5 place-items-center rounded-full bg-yellow-400 px-0.5 text-[9px] font-bold text-black">
                  {t.badge}
                </span>
              )}
            </button>
          );
        })}
      </nav>
    </>
  );
}
