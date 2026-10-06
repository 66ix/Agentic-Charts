"use client";

import Dialog from "./Dialog";

export const SHORTCUTS: Array<{ group: string; keys: Array<[string, string]> }> = [
  {
    group: "Anywhere",
    keys: [
      ["/", "Ask the chart agent"],
      ["S or Ctrl+K", "Search a coin"],
      ["Ctrl+Z", "Undo a drawing or AI change"],
      ["Ctrl+Y or Ctrl+Shift+Z", "Redo"],
      ["Ctrl+S", "Save the layout"],
      ["?", "Show this list"],
      ["Esc", "Cancel the tool, close a dialog"],
    ],
  },
  {
    group: "Chart",
    keys: [
      ["1 – 9, 0", "Timeframe 1m, 5m, 15m, 30m, 1h, 3h, 4h, D, W, M"],
      ["[  ]", "Previous / next coin in the watchlist"],
      ["G", "One, two or four charts"],
      ["Alt+R", "Fit the chart"],
      ["Alt+S", "Save a screenshot"],
      ["K", "Kimi Cooked on / off"],
      ["I", "Indicator settings"],
    ],
  },
  {
    group: "Drawing",
    keys: [
      ["T  H  F  R", "Trendline, horizontal ray, Fib, rectangle"],
      ["N  P  M", "Note, XABCD pattern, measure"],
      ["Del", "Delete the selected drawing"],
      ["Ctrl+D", "Duplicate the selected drawing"],
    ],
  },
  {
    group: "Side panel",
    keys: [
      ["D", "Show / hide the side panel"],
      ["W", "Watchlist"],
      ["A", "Alerts"],
      ["L", "Layers"],
      ["J", "Trade journal"],
      ["B", "Grid bots"],
      ["E", "Calendar and news"],
      ["O", "Market data"],
    ],
  },
];

/** The "?" cheat sheet. */
export default function ShortcutsDialog({ open, onClose }: { open: boolean; onClose(): void }) {
  return (
    <Dialog open={open} title="Keyboard shortcuts" width={620} onClose={onClose}>
      <div className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
        {SHORTCUTS.map((g) => (
          <div key={g.group}>
            <div className="pb-1 text-[10px] font-semibold uppercase tracking-wide text-mute">{g.group}</div>
            {g.keys.map(([k, what]) => (
              <div key={k} className="flex items-baseline justify-between gap-3 py-0.5">
                <span className="text-ink/90">{what}</span>
                <kbd className="shrink-0 rounded border border-line bg-base px-1.5 font-mono text-[10px] text-mute">{k}</kbd>
              </div>
            ))}
          </div>
        ))}
      </div>
    </Dialog>
  );
}
