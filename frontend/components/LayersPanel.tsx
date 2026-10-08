"use client";

import clsx from "clsx";
import { Eye, EyeOff, PinOff, Trash2 } from "lucide-react";

import { displaySymbol, formatPrice } from "@/lib/format";
import { isVisible, LAYERS, type LayerId, type LayerVisibility } from "@/lib/layers";
import type { Drawing, Overlay } from "@/lib/types";

export interface PinnedSet {
  id: string;
  label: string;
  count: number;
}

const DRAWING_NAMES: Record<Drawing["type"], string> = {
  trendline: "Trendline",
  hray: "Horizontal ray",
  fib: "Fib retracement",
  rect: "Rectangle",
  text: "Note",
  pattern: "XABCD pattern",
  ruler: "Measurement",
};

function drawingTitle(d: Drawing): string {
  if (d.type === "text") return `Note “${(d.text ?? "").slice(0, 24)}”`;
  if (d.type === "hray") return `Horizontal ray ${formatPrice(d.points[0]?.price ?? 0)}`;
  return DRAWING_NAMES[d.type];
}

interface Props {
  symbol: string;
  visibility: LayerVisibility;
  /** How many objects each layer has on the active chart (layers with none are dimmed). */
  counts: Partial<Record<LayerId, number>>;
  pins: PinnedSet[];
  drawings: Drawing[];
  selectedId: string | null;
  onVisibility(next: LayerVisibility): void;
  onUnpin(id: string): void;
  onSelectDrawing(id: string): void;
  onDeleteDrawing(id: string): void;
  /** The agent's levels on the active chart, each removable on its own. */
  aiLevels: Overlay[];
  onDeleteAiLevel(index: number): void;
}

function levelTitle(o: Overlay): string {
  const where =
    o.type === "box" ? `${formatPrice(o.price_low)}–${formatPrice(o.price_high)}` : o.type === "horizontal_line" ? formatPrice(o.price) : "";
  return `${o.label || o.kind || o.type}${where && !(o.label ?? "").includes(where) ? ` · ${where}` : ""}`;
}

/** Eye toggles per layer, the pinned answers and the user's drawings on the active chart. */
export default function LayersPanel(p: Props) {
  const groups = [...new Set(LAYERS.map((l) => l.group))];
  const setAll = (on: boolean) => p.onVisibility(Object.fromEntries(LAYERS.map((l) => [l.id, on])));
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-1 border-b border-line px-3 py-1.5 text-[11px]">
        <span className="text-mute">What&apos;s drawn on {displaySymbol(p.symbol)}</span>
        <div className="flex-1" />
        <button type="button" className="btn-ghost h-6 px-1.5 text-[11px]" onClick={() => setAll(true)}>
          Show all
        </button>
        <button type="button" className="btn-ghost h-6 px-1.5 text-[11px]" onClick={() => setAll(false)}>
          Hide all
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto pb-3 text-[12px]">
        {groups.map((g) => (
          <div key={g} className="pt-2">
            <div className="px-3 pb-1 text-[10px] font-semibold uppercase tracking-wide text-mute">{g}</div>
            {LAYERS.filter((l) => l.group === g).map((l) => {
              const on = isVisible(p.visibility, l.id);
              const n = p.counts[l.id] ?? 0;
              return (
                <button
                  key={l.id}
                  type="button"
                  onClick={() => p.onVisibility({ ...p.visibility, [l.id]: !on })}
                  className={clsx("flex w-full items-center gap-2 px-3 py-1 text-left hover:bg-panel2", !n && "opacity-50")}
                  aria-pressed={on}
                >
                  {on ? <Eye className="h-3.5 w-3.5 text-ink" /> : <EyeOff className="h-3.5 w-3.5 text-mute" />}
                  <span className={clsx("flex-1", on ? "text-ink" : "text-mute line-through")}>{l.label}</span>
                  {n > 0 && <span className="font-mono text-[10px] text-mute">{n}</span>}
                </button>
              );
            })}
          </div>
        ))}

        <div className="px-3 pb-1 pt-4 text-[10px] font-semibold uppercase tracking-wide text-mute">AI levels on this chart</div>
        {p.aiLevels.length === 0 && <p className="px-3 text-[11px] text-mute">The agent hasn&apos;t drawn anything on this chart.</p>}
        {p.aiLevels.map((o, i) => (
          <div key={o.id ?? i} className="flex items-center gap-2 px-3 py-1">
            <span className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: o.color }} />
            <span className="min-w-0 flex-1 truncate text-ink" title={levelTitle(o)}>
              {levelTitle(o)}
            </span>
            <button type="button" className="btn-ghost h-6 w-6 p-0 hover:text-down" title="Remove this level" aria-label="Remove AI level" onClick={() => p.onDeleteAiLevel(i)}>
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
        ))}

        <div className="px-3 pb-1 pt-4 text-[10px] font-semibold uppercase tracking-wide text-mute">Pinned answers</div>
        {p.pins.length === 0 && <p className="px-3 text-[11px] text-mute">Pin an agent answer to keep its drawings when you ask something else.</p>}
        {p.pins.map((pin) => (
          <div key={pin.id} className="flex items-center gap-2 px-3 py-1">
            <span className="min-w-0 flex-1 truncate text-ink" title={pin.label}>
              {pin.label || "Answer"}
            </span>
            <span className="font-mono text-[10px] text-mute">{pin.count}</span>
            <button type="button" className="btn-ghost h-6 w-6 p-0" title="Unpin" aria-label="Unpin" onClick={() => p.onUnpin(pin.id)}>
              <PinOff className="h-3.5 w-3.5" />
            </button>
          </div>
        ))}

        <div className="px-3 pb-1 pt-4 text-[10px] font-semibold uppercase tracking-wide text-mute">My drawings</div>
        {p.drawings.length === 0 && <p className="px-3 text-[11px] text-mute">Nothing drawn on this coin yet.</p>}
        {p.drawings.map((d) => (
          <div key={d.id} className={clsx("flex items-center gap-2 px-3 py-1", d.id === p.selectedId && "bg-accent/10")}>
            <span className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: d.color }} />
            <button type="button" className="min-w-0 flex-1 truncate text-left text-ink hover:text-accent" onClick={() => p.onSelectDrawing(d.id)}>
              {drawingTitle(d)}
              {d.style?.timeframes?.length ? <span className="text-mute"> · {d.style.timeframes.join(", ")} only</span> : null}
            </button>
            <button type="button" className="btn-ghost h-6 w-6 p-0 hover:text-down" title="Delete" aria-label="Delete drawing" onClick={() => p.onDeleteDrawing(d.id)}>
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
