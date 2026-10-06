"use client";

import clsx from "clsx";
import {
  Crosshair,
  GitFork,
  Lock,
  LockOpen,
  Magnet,
  Minus,
  MoveUpRight,
  Ruler,
  Spline,
  Square,
  Trash2,
  Type,
  type LucideIcon,
} from "lucide-react";

import type { ToolId } from "@/lib/types";

const TOOLS: { id: ToolId; icon: LucideIcon; label: string; key: string }[] = [
  { id: "crosshair", icon: Crosshair, label: "Crosshair / select", key: "Esc" },
  { id: "trendline", icon: MoveUpRight, label: "Trendline", key: "T" },
  { id: "hray", icon: Minus, label: "Horizontal ray", key: "H" },
  { id: "fib", icon: GitFork, label: "Fibonacci retracement", key: "F" },
  { id: "rect", icon: Square, label: "Rectangle", key: "R" },
  { id: "text", icon: Type, label: "Text note", key: "N" },
  { id: "pattern", icon: Spline, label: "XABCD pattern", key: "P" },
  { id: "ruler", icon: Ruler, label: "Measure", key: "M" },
];

export const TOOL_HOTKEYS: Record<string, ToolId> = Object.fromEntries(
  TOOLS.filter((t) => t.key.length === 1).map((t) => [t.key.toLowerCase(), t.id]),
);

interface Props {
  tool: ToolId;
  magnet: boolean;
  locked: boolean;
  hasSelection: boolean;
  drawingCount: number;
  onTool(t: ToolId): void;
  onMagnet(v: boolean): void;
  onLock(v: boolean): void;
  onDelete(): void;
}

function ToolButton({ active, label, onClick, children, danger, disabled }: {
  active?: boolean;
  label: string;
  onClick(): void;
  children: React.ReactNode;
  danger?: boolean;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      aria-pressed={active}
      disabled={disabled}
      onClick={onClick}
      className={clsx(
        "grid h-9 w-9 place-items-center rounded-md transition-colors disabled:cursor-not-allowed disabled:opacity-30",
        active ? "bg-accent/20 text-accent" : "text-mute hover:bg-panel2 hover:text-ink",
        danger && !disabled && "hover:text-down",
      )}
    >
      {children}
    </button>
  );
}

export default function DrawingToolbar(p: Props) {
  return (
    <nav aria-label="Drawing tools" className="flex w-12 shrink-0 flex-col items-center gap-0.5 border-r border-line bg-panel py-2">
      {TOOLS.map(({ id, icon: Icon, label, key }) => (
        <ToolButton key={id} label={`${label} (${key})`} active={p.tool === id} onClick={() => p.onTool(id)} disabled={p.locked && id !== "crosshair"}>
          <Icon className="h-[18px] w-[18px]" strokeWidth={1.75} />
        </ToolButton>
      ))}
      <div className="my-1.5 h-px w-7 bg-line" />
      <ToolButton label="Magnet mode: snap to OHLC" active={p.magnet} onClick={() => p.onMagnet(!p.magnet)}>
        <Magnet className="h-[18px] w-[18px]" strokeWidth={1.75} />
      </ToolButton>
      <ToolButton label={p.locked ? "Unlock drawings" : "Lock drawings"} active={p.locked} onClick={() => p.onLock(!p.locked)}>
        {p.locked ? <Lock className="h-[18px] w-[18px]" strokeWidth={1.75} /> : <LockOpen className="h-[18px] w-[18px]" strokeWidth={1.75} />}
      </ToolButton>
      <ToolButton
        label={p.hasSelection ? "Delete selected (Del)" : "Delete all drawings"}
        onClick={p.onDelete}
        danger
        disabled={p.locked || p.drawingCount === 0}
      >
        <Trash2 className="h-[18px] w-[18px]" strokeWidth={1.75} />
      </ToolButton>
    </nav>
  );
}
