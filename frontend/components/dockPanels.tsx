"use client";

import { Grid3x3, type LucideIcon } from "lucide-react";
import type { ComponentType } from "react";

import type { DockPanelProps } from "@/lib/dock";

import GridBotPanel from "./GridBotPanel";

/** A side-panel tab that only needs the shared dock props (the workspace builds the agent, watchlist,
 *  layers and alerts tabs itself, since they need more). */
export interface DockPanelDef {
  id: string;
  label: string;
  icon: LucideIcon;
  /** Single-letter shortcut that opens the tab (see ShortcutsDialog). */
  hotkey?: string;
  Component: ComponentType<DockPanelProps>;
}

export const EXTRA_PANELS: DockPanelDef[] = [
  { id: "gridbots", label: "Grid bots", icon: Grid3x3, hotkey: "b", Component: GridBotPanel },
];
