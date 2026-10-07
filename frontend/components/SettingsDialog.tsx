"use client";

import { Download, Upload } from "lucide-react";
import { useRef, useState } from "react";

import { readStored, storedKeys, usePersistentState, writeStored } from "@/hooks/usePersistentState";
import { DEFAULT_SIZING, type SizingSettings } from "@/lib/sizing";
import type { LayoutState } from "@/lib/types";

import { BinanceKeySettings } from "./AccountPanel";
import Dialog, { NumberInput, Row, Section, Toggle } from "./Dialog";

/** Timezones offered for the chart's time axis; "local" follows the browser. */
const ZONES = [
  { value: "local", label: "My timezone" },
  { value: "UTC", label: "UTC (exchange time)" },
  { value: "America/New_York", label: "New York" },
  { value: "America/Chicago", label: "Chicago" },
  { value: "Europe/London", label: "London" },
  { value: "Europe/Berlin", label: "Berlin" },
  { value: "Europe/Istanbul", label: "Istanbul" },
  { value: "Asia/Dubai", label: "Dubai" },
  { value: "Asia/Kolkata", label: "India" },
  { value: "Asia/Singapore", label: "Singapore" },
  { value: "Asia/Hong_Kong", label: "Hong Kong" },
  { value: "Asia/Tokyo", label: "Tokyo" },
  { value: "Australia/Sydney", label: "Sydney" },
];

const BACKUP_VERSION = 1;

interface Props {
  open: boolean;
  layout: LayoutState;
  onLayout(next: LayoutState): void;
  onClose(): void;
}

/** Everything that isn't about one chart: time, sizing, and a backup of the whole app. */
export default function SettingsDialog(p: Props) {
  const [sizing, setSizing] = usePersistentState<SizingSettings>("ac:sizing", DEFAULT_SIZING);
  const [note, setNote] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const exportAll = () => {
    const data: Record<string, unknown> = {};
    for (const k of storedKeys()) data[k] = readStored(k, null);
    const blob = new Blob([JSON.stringify({ version: BACKUP_VERSION, saved_at: Date.now(), data }, null, 2)], {
      type: "application/json",
    });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `agentic-charts-backup-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    setNote(`Saved ${Object.keys(data).length} settings to your downloads.`);
  };

  const importAll = async (file: File) => {
    try {
      const parsed = JSON.parse(await file.text()) as { data?: Record<string, unknown> };
      const data = parsed.data;
      if (!data || typeof data !== "object") throw new Error("not a backup file");
      for (const [k, v] of Object.entries(data)) if (k.startsWith("ac:")) writeStored(k, v);
      setNote("Restored. Reloading…");
      setTimeout(() => window.location.reload(), 600);
    } catch (err) {
      setNote(`That file didn't load: ${(err as Error).message}`);
    }
  };

  return (
    <Dialog open={p.open} title="Settings" onClose={p.onClose}>
      <Section title="Chart">
        <Row label="Log scale" hint="Alt+L">
          <Toggle checked={p.layout.logScale} onChange={(v) => p.onLayout({ ...p.layout, logScale: v })} />
        </Row>
        <Row label="Grid lines">
          <Toggle checked={p.layout.grid} onChange={(v) => p.onLayout({ ...p.layout, grid: v })} />
        </Row>
      </Section>

      <Section title="Time">
        <Row label="Timezone" hint="Used for the time axis and the countdown">
          <select
            value={p.layout.timezone ?? "local"}
            onChange={(e) => p.onLayout({ ...p.layout, timezone: e.target.value })}
            className="h-7 rounded border border-line bg-base px-1.5 text-[12px] text-ink outline-none focus:border-accent"
          >
            {ZONES.map((z) => (
              <option key={z.value} value={z.value}>
                {z.label}
              </option>
            ))}
          </select>
        </Row>
        <Row label="Countdown to candle close" hint="Shown under the price on the right axis">
          <Toggle checked={p.layout.countdown !== false} onChange={(v) => p.onLayout({ ...p.layout, countdown: v })} />
        </Row>
      </Section>

      <Section title="Several charts">
        <Row label="Move the crosshair on every chart" hint="Hovering one chart marks the same time on the others">
          <Toggle checked={p.layout.syncCrosshair !== false} onChange={(v) => p.onLayout({ ...p.layout, syncCrosshair: v })} />
        </Row>
        <Row label="Every chart follows the same coin" hint="Each chart keeps its own timeframe">
          <Toggle checked={!!p.layout.linkSymbol} onChange={(v) => p.onLayout({ ...p.layout, linkSymbol: v })} />
        </Row>
        <Row label="Find levels automatically" hint="Runs the chart agent when a chart opens with nothing on it">
          <Toggle checked={p.layout.autoLevels} onChange={(v) => p.onLayout({ ...p.layout, autoLevels: v })} />
        </Row>
      </Section>

      <Section title="Position sizing">
        <p className="pb-1 text-[11px] text-mute">Used on trade plans to work out the size, risk and fees of a trade.</p>
        <Row label="Account size">
          <NumberInput value={sizing.account} min={0} step={100} width={100} onChange={(v) => setSizing({ ...sizing, account: v })} suffix="USDT" />
        </Row>
        <Row label="Risk per trade">
          <NumberInput value={sizing.riskPct} min={0.1} max={100} step={0.1} onChange={(v) => setSizing({ ...sizing, riskPct: v })} suffix="%" />
        </Row>
        <Row label="Fee per side">
          <NumberInput value={sizing.feePct} min={0} max={1} step={0.01} onChange={(v) => setSizing({ ...sizing, feePct: v })} suffix="%" />
        </Row>
        <Row label="Highest leverage" hint="A plan that needs more than this is flagged">
          <NumberInput value={sizing.maxLeverage} min={1} max={125} step={1} onChange={(v) => setSizing({ ...sizing, maxLeverage: v })} suffix="×" />
        </Row>
      </Section>

      <div className="border-b border-line py-2">
        <BinanceKeySettings />
      </div>

      <Section title="Backup">
        <p className="pb-2 text-[11px] text-mute">
          Everything lives in this browser: your drawings, alerts, chats, watchlists, layouts and settings. Save a file to move
          them to another browser or computer.
        </p>
        <div className="flex items-center gap-2">
          <button type="button" className="btn-ghost h-7 gap-1.5 border border-line px-2 text-[12px]" onClick={exportAll}>
            <Download className="h-3.5 w-3.5" /> Save a backup
          </button>
          <button type="button" className="btn-ghost h-7 gap-1.5 border border-line px-2 text-[12px]" onClick={() => fileRef.current?.click()}>
            <Upload className="h-3.5 w-3.5" /> Restore from a file
          </button>
          <input
            ref={fileRef}
            type="file"
            accept="application/json,.json"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              e.target.value = "";
              if (f) void importAll(f);
            }}
          />
        </div>
        {note && <p className="pt-2 text-[11px] text-accent">{note}</p>}
      </Section>
    </Dialog>
  );
}
