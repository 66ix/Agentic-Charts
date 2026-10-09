"use client";

import { Activity, CalendarClock, Coins, CalendarRange, Crosshair, FlaskConical, Gauge, Grid3x3, NotebookPen, Radar, Wallet, type LucideIcon } from "lucide-react";
import type { ComponentType } from "react";

import type { DockPanelProps } from "@/lib/dock";

import AccountPanel from "./AccountPanel";
import BacktestPanel from "./BacktestPanel";
import DcaPanel from "./DcaPanel";
import EventsPanel from "./EventsPanel";
import GridBotPanel from "./GridBotPanel";
import JournalPanel from "./JournalPanel";
import MarketDataPanel from "./MarketDataPanel";
import PaperPanel from "./PaperPanel";
import ScannerPanel from "./ScannerPanel";
import StatusPanel from "./StatusPanel";
import TradeManagerPanel from "./TradeManagerPanel";

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
  { id: "trades", label: "Live trades", icon: Crosshair, hotkey: "q", Component: TradeManagerPanel },
  { id: "journal", label: "Journal", icon: NotebookPen, hotkey: "j", Component: JournalPanel },
  { id: "paper", label: "Paper trading", icon: Coins, hotkey: "y", Component: PaperPanel },
  { id: "backtest", label: "Backtest", icon: FlaskConical, hotkey: "x", Component: BacktestPanel },
  { id: "dca", label: "DCA planner", icon: CalendarRange, hotkey: "c", Component: DcaPanel },
  { id: "market", label: "Market data", icon: Gauge, hotkey: "o", Component: MarketDataPanel },
  { id: "events", label: "Calendar", icon: CalendarClock, hotkey: "e", Component: EventsPanel },
  { id: "gridbots", label: "Grid bots", icon: Grid3x3, hotkey: "b", Component: GridBotPanel },
  { id: "scanner", label: "Scanner", icon: Radar, hotkey: "u", Component: ScannerPanel },
  { id: "account", label: "Account", icon: Wallet, Component: AccountPanel },
  { id: "status", label: "Status", icon: Activity, Component: StatusPanel },
];
